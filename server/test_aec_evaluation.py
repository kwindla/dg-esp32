import asyncio
import json
import time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import (
    LLMContextFrame, InterruptionFrame, InputAudioRawFrame, TranscriptionFrame,
    ProposedUserStartedSpeakingFrame, TTSAudioRawFrame, TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from aec_evaluation import Evaluation, enabled


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'protocol.json').write_text(json.dumps(dict(
            echo_replies=[dict(id='echo-01',text='Paris is the capital.')],
            barge_ids=['barge-01'],barge_bot_text='Long reply.')))
        self.receiver=SimpleNamespace(_dg_rtp=[{}],_dg_rtp_truncated=False)
        self.ice=SimpleNamespace(_dg_socket_count=1,_dg_socket_truncated=False)
        self.connection=SimpleNamespace(pc_id='test-peer',pc=SimpleNamespace(
            getReceivers=lambda:[self.receiver],getSenders=lambda:[SimpleNamespace(
                transport=SimpleNamespace(transport=SimpleNamespace(_connection=self.ice)))]))
        self.evaluation = Evaluation(self.root,self.connection)

    def events(self):
        return [json.loads(line) for line in (self.root/'events.jsonl').read_text().splitlines()]

    def test_aec_capability_and_single_peer_required(self):
        self.assertFalse(enabled('',False))
        with self.assertRaises(ValueError):enabled('dir',False)
        self.assertTrue(enabled('dir',True))
        with self.assertRaises(FileExistsError):Evaluation(self.root,SimpleNamespace(pc_id='second-peer'))

    def test_observer_health_fails_missing_or_truncated_capture(self):
        self.evaluation.observer_health();self.assertTrue(self.events()[-1]['passed'])
        self.receiver._dg_rtp_truncated=True
        self.evaluation.observer_health();self.assertFalse(self.events()[-1]['passed'])
        self.receiver._dg_rtp_truncated=False;self.ice._dg_socket_count=0
        self.evaluation.observer_health();self.assertFalse(self.events()[-1]['passed'])

    async def test_only_llm_context_is_suppressed(self):
        processor=self.evaluation.context_filter
        processor.push_frame=AsyncMock()
        frames=[InterruptionFrame(),InputAudioRawFrame(b'\0'*640,16000,1),
                TranscriptionFrame('actual words','user','now'),ProposedUserStartedSpeakingFrame(),
                TTSSpeakFrame('Scripted')]
        # Policy unit test: the framework's lifecycle/task manager is owned by
        # PipelineWorker, not this manually invoked processor. Preserve the
        # actual subclass process_frame body while stubbing only its parent.
        with patch.object(FrameProcessor,'process_frame',new=AsyncMock()):
            for frame in frames:
                await processor.process_frame(frame,FrameDirection.DOWNSTREAM)
            await processor.process_frame(LLMContextFrame(LLMContext()),FrameDirection.DOWNSTREAM)
        self.assertEqual([call.args[0] for call in processor.push_frame.await_args_list],frames)
        self.assertEqual(sum(e['event']=='automatic-llm-response-suppressed' for e in self.events()),1)

    async def test_policy_runs_inside_real_worker_lifecycle(self):
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker
        from pipecat.workers.runner import WorkerRunner
        from pipecat.frames.frames import EndFrame
        seen=[]
        class Collect(FrameProcessor):
            async def process_frame(self,frame,direction):
                await super().process_frame(frame,direction)
                seen.append(type(frame).__name__)
                await self.push_frame(frame,direction)
        worker=PipelineWorker(Pipeline([self.evaluation.context_filter,Collect()]),enable_rtvi=False)
        runner=WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        running=asyncio.create_task(runner.run())
        await worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(.05)
        await worker.queue_frames([InputAudioRawFrame(b'\0'*640,16000,1),
            TranscriptionFrame('real provider result','user','now'),
            LLMContextFrame(LLMContext()),TTSSpeakFrame('Known script'),EndFrame()])
        await asyncio.wait_for(running,3)
        self.assertIn('InterruptionFrame',seen)
        self.assertIn('InputAudioRawFrame',seen)
        self.assertIn('TranscriptionFrame',seen)
        self.assertIn('TTSSpeakFrame',seen)
        self.assertNotIn('LLMContextFrame',seen)

    async def test_real_pcm_retained_before_forward_and_no_unattributed_output(self):
        p=self.evaluation.tts_reference
        p.push_frame=AsyncMock()
        frame=TTSAudioRawFrame(b'\1\0'*320,16000,1)
        with self.assertRaises(ValueError):await p.process_frame(frame,FrameDirection.DOWNSTREAM)
        self.evaluation.current='echo-01'
        await p.process_frame(frame,FrameDirection.DOWNSTREAM)
        self.assertEqual((self.root/'echo-01.pcm').read_bytes(),frame.audio)
        self.assertEqual(self.events()[-1]['samples_total'],320)
        p.push_frame.assert_awaited_once_with(frame,FrameDirection.DOWNSTREAM)

    async def test_control_cannot_inject_transcript_or_retake_original(self):
        worker=SimpleNamespace(queue_frame=AsyncMock(),cancel=AsyncMock())
        path=self.root/'command.json'
        path.write_text(json.dumps(dict(number=1,op='speak',case_id='echo-01',text='fake transcript')))
        task=asyncio.create_task(self.evaluation.control(worker))
        await asyncio.sleep(.04)
        frame=worker.queue_frame.await_args.args[0]
        self.assertIsInstance(frame,TTSSpeakFrame)
        self.assertEqual(frame.text,'Paris is the capital.')
        path.write_text(json.dumps(dict(number=2,op='seal',case_id='echo-01')))
        await asyncio.sleep(.04)
        path.write_text(json.dumps(dict(number=3,op='speak',case_id='echo-01')))
        with self.assertRaises(ValueError):await asyncio.wait_for(task,.2)
        self.assertEqual(worker.queue_frame.await_count,1)
        worker.cancel.assert_awaited_once()
        self.assertFalse(any(e['event']=='user-turn-stopped' for e in self.events()))

    async def test_preparation_finishes_before_speak_is_queued(self):
        order=[]
        async def prepare():
            await asyncio.sleep(.01)
            order.append('prepared')
        async def queue(frame):order.append('queued')
        self.evaluation.prepare_tts=prepare
        worker=SimpleNamespace(queue_frame=queue,cancel=AsyncMock())
        (self.root/'command.json').write_text(json.dumps(dict(number=1,op='speak',case_id='echo-01')))
        task=asyncio.create_task(self.evaluation.control(worker))
        await asyncio.sleep(.06)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(order,['prepared','queued'])
        self.assertEqual([e['event'] for e in self.events() if e['event'].startswith('script-')],['script-request','script-queued'])

    async def test_scripted_interruption_requires_declaration_and_is_single_use(self):
        self.evaluation.current='echo-01'
        self.evaluation.interrupt_ids={'echo-01'}
        worker=SimpleNamespace(queue_frame=AsyncMock(),cancel=AsyncMock())
        path=self.root/'command.json'
        path.write_text(json.dumps(dict(number=1,op='interrupt',case_id='echo-01')))
        task=asyncio.create_task(self.evaluation.control(worker));await asyncio.sleep(.04)
        self.assertIsInstance(worker.queue_frame.await_args.args[0],InterruptionFrame)
        path.write_text(json.dumps(dict(number=2,op='interrupt',case_id='echo-01')))
        with self.assertRaises(ValueError):await asyncio.wait_for(task,.2)
        self.assertEqual(worker.queue_frame.await_count,1)

    async def test_skip_consumes_only_next_declared_case_without_audio_or_retake(self):
        worker=SimpleNamespace(queue_frame=AsyncMock(),cancel=AsyncMock())
        path=self.root/'command.json'
        path.write_text(json.dumps(dict(number=1,op='skip',case_id='echo-01',reason='noise-precheck-failed')))
        task=asyncio.create_task(self.evaluation.control(worker))
        await asyncio.sleep(.04)
        self.assertEqual(self.evaluation.index,1)
        worker.queue_frame.assert_not_awaited()
        self.assertEqual(self.events()[-1]['event'],'case-skipped')
        path.write_text(json.dumps(dict(number=2,op='speak',case_id='echo-01')))
        with self.assertRaises(ValueError):await asyncio.wait_for(task,.2)
        worker.queue_frame.assert_not_awaited()

    async def test_empty_user_start_and_mute_are_audited_and_deduplicated(self):
        from pipecat.frames.frames import UserStartedSpeakingFrame,UserMuteStartedFrame
        observer=self.evaluation.observer
        for frame in (UserStartedSpeakingFrame(),UserMuteStartedFrame()):
            data=SimpleNamespace(frame=frame,source=SimpleNamespace())
            await observer.on_push_frame(data);await observer.on_push_frame(data)
        self.assertEqual([e['frame'] for e in self.events() if e['event']=='frame'],
                         ['UserStartedSpeakingFrame','UserMuteStartedFrame'])


if __name__=='__main__':unittest.main()

class CampaignEvaluationTests(EvaluationTests):
    async def test_real_response_is_forwarded_only_for_declared_case(self):
        self.evaluation.current='barge-01';self.evaluation.real_bot_ids={'barge-01'}
        p=self.evaluation.context_filter;p.push_frame=AsyncMock()
        frame=LLMContextFrame(LLMContext())
        with patch.object(FrameProcessor,'process_frame',new=AsyncMock()):
            await p.process_frame(frame,FrameDirection.DOWNSTREAM)
        p.push_frame.assert_awaited_once_with(frame,FrameDirection.DOWNSTREAM)
        self.assertEqual(self.events()[-1]['event'],'real-bot-llm-request')

    async def test_declared_once_dispatch_preserves_retries_and_context_pcm_separation(self):
        self.evaluation.declared_once=True
        self.evaluation.current='echo-01'
        p=self.evaluation.tts_reference;p.push_frame=AsyncMock()
        for cid,value in [('old',1),('new',2)]:
            await p.process_frame(TTSAudioRawFrame(bytes([value,0])*320,16000,1,context_id=cid),FrameDirection.DOWNSTREAM)
        self.assertEqual((self.root/'echo-01.pcm').read_bytes(),b'\1\0'*320)
        contexts=[e for e in self.events() if e['event']=='tts-context-pcm']
        self.assertEqual({e['context_id'] for e in contexts},{'old','new'})
        self.assertEqual(len(list(self.root.glob('context-*.pcm'))),2)

class HostInterruptEvaluationTests(EvaluationTests):
    async def test_host_cancel_then_declared_next_reply_no_transcript(self):
        self.evaluation.current='echo-01'
        self.evaluation.interrupt_ids={'echo-01'}
        self.evaluation.host_interrupts={'echo-01':dict(next_delay_s=1.0,next_text='Recovery reply.')}
        worker=SimpleNamespace(queue_frame=AsyncMock(),cancel=AsyncMock())
        (self.root/'command.json').write_text(json.dumps(dict(number=1,op='interrupt',case_id='echo-01',host_issued_s=1.25)))
        task=asyncio.create_task(self.evaluation.control(worker))
        await asyncio.sleep(1.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        frames=[c.args[0] for c in worker.queue_frame.await_args_list]
        self.assertEqual([type(f).__name__ for f in frames],['InterruptionFrame','TTSSpeakFrame'])
        self.assertEqual(frames[1].text,'Recovery reply.')
        events=self.events();issued=next(e for e in events if e['event']=='script-interrupt')
        queued=next(e for e in events if e['event']=='host-next-reply-queued')
        self.assertEqual(issued['host_issued_s'],1.25)
        self.assertGreaterEqual(queued['t']-issued['t'],1.0)

    async def test_vad_and_bot_state_are_explicit(self):
        from pipecat.frames.frames import VADUserStartedSpeakingFrame,BotStartedSpeakingFrame,InterruptionFrame
        observer=self.evaluation.observer
        for frame in (BotStartedSpeakingFrame(),VADUserStartedSpeakingFrame(),InterruptionFrame()):
            await observer.on_push_frame(SimpleNamespace(frame=frame,source=SimpleNamespace()))
        rows=[e for e in self.events() if e['event']=='frame']
        self.assertTrue(rows[1]['vad_start']);self.assertTrue(rows[1]['bot_active'])
        self.assertTrue(rows[2]['interruption']);self.assertFalse(rows[2]['accepted_turn'])


class BurstEvaluationTests(EvaluationTests):
    async def run_commands(self, commands, *, wait=.3):
        worker=SimpleNamespace(queue_frame=AsyncMock(),cancel=AsyncMock())
        task=asyncio.create_task(self.evaluation.control(worker))
        for c in commands:
            (self.root/'command.json').write_text(json.dumps(c));await asyncio.sleep(wait)
        task.cancel()
        try:await task
        except (asyncio.CancelledError,ValueError):pass

    async def test_burst_is_inert_unless_declared(self):
        sent=[];self.connection.send_app_message=sent.append
        await self.run_commands([dict(number=1,op='burst',burst=1)])
        self.assertEqual(sent,[])
        self.assertTrue(any(e['event']=='evaluation-error' for e in self.events()))

    async def test_declared_bursts_are_bounded_ordered_synchronous_and_logged(self):
        sent=[];yields=[]
        def send(message):
            sent.append(message)
        self.connection.send_app_message=send
        self.evaluation.burst=dict(messages=40,max_bursts=2,path='datachannel-direct')
        await self.run_commands([dict(number=1,op='burst',burst=1),dict(number=2,op='burst',burst=2),
                                 dict(number=3,op='burst',burst=3)])
        self.assertEqual([m['data'] for m in sent],[{'t':'burst','d':{'b':b,'id':i}} for b in (1,2) for i in range(40)])
        self.assertTrue(all(m['label']=='rtvi-ai' and m['type']=='server-message' for m in sent))
        logged=[e for e in self.events() if e['event']=='burst-sent']
        self.assertEqual([e['burst'] for e in logged],[1,2])
        self.assertTrue(all(e['first_send_s']<=e['last_send_s'] and e['path']=='datachannel-direct' for e in logged))
        self.assertTrue(any(e['event']=='evaluation-error' for e in self.events()))   # third burst refused

    def test_invalid_burst_declaration_is_refused(self):
        # max_bursts 1..10 accepted (v9.4)
        for ok in (1, 10):
            (self.root/'session-owner.json').unlink(missing_ok=True)
            (self.root/'protocol.json').write_text(json.dumps(dict(echo_replies=[],barge_ids=[],barge_bot_text='',
                burst=dict(messages=40,max_bursts=ok,path='datachannel-direct'))))
            self.assertEqual(Evaluation(self.root,self.connection).burst['max_bursts'],ok)
        for burst in (dict(messages=10,max_bursts=2,path='datachannel-direct'),dict(messages=40,max_bursts=2,path='pipeline'),
                      dict(messages=40,max_bursts=11,path='datachannel-direct'),dict(messages=40,max_bursts=0,path='datachannel-direct')):
            (self.root/'session-owner.json').unlink(missing_ok=True)
            (self.root/'protocol.json').write_text(json.dumps(dict(echo_replies=[],barge_ids=[],barge_bot_text='',burst=burst)))
            with self.assertRaises(ValueError):Evaluation(self.root,self.connection)


class BurstFlightTests(unittest.IsolatedAsyncioTestCase):
    """Host-side aiortc SCTP check (smoke finding S3): synchronous send_app_message writes of the 40-message
    burst leave the association as one back-to-back flight, before any SACK can return, versus the ~28 ms
    pacing of the pipeline path. aiortc never bundles DATA chunks (one chunk per SCTP packet), so a single
    packet cannot carry more than 16 burst chunks; device-side carry-over still depends on the flight
    arriving within one owner pass."""
    async def test_synchronous_burst_is_one_sctp_flight(self):
        from aiortc.rtcdatachannel import RTCDataChannel, RTCDataChannelParameters
        from aiortc.rtcsctptransport import RTCSctpTransport
        from aiortc.rtcsctptransport import parse_packet, DataChunk, SackChunk
        loop=asyncio.get_running_loop()
        class Dtls:
            def __init__(self,role):self.state='connected';self.transport=SimpleNamespace(role=role);self.peer=None;self.receiver=None;self.sent=[]
            def _register_data_receiver(self,r):self.receiver=r
            def _unregister_data_receiver(self,r):self.receiver=None
            async def _send_data(self,data):
                self.sent.append((time.monotonic(),data))
                if self.peer.receiver:loop.call_later(.005,lambda:loop.create_task(self.peer.receiver._handle_data(data)))  # 5 ms path
        a,b=Dtls('controlling'),Dtls('controlled');a.peer,b.peer=b,a
        client,server=RTCSctpTransport(a),RTCSctpTransport(b)
        await server.start(client.getCapabilities(),client.port);await client.start(server.getCapabilities(),server.port)
        for _ in range(100):
            if client._association_state==client.State.ESTABLISHED:break
            await asyncio.sleep(.01)
        channel=RTCDataChannel(client,RTCDataChannelParameters(label='chat'))
        await asyncio.sleep(.1)
        a.sent.clear();b.sent.clear()
        connection=SimpleNamespace(send_app_message=lambda m:channel.send(json.dumps(m)))
        for i in range(40):connection.send_app_message({'label':'rtvi-ai','type':'server-message','data':{'t':'burst','d':{'b':1,'id':i}}})
        await asyncio.sleep(.5)
        data=[(t,c) for t,d in a.sent for c in parse_packet(d)[3] if isinstance(c,DataChunk) and c.protocol==51]
        per_packet=[sum(isinstance(c,DataChunk) for c in parse_packet(d)[3]) for t,d in a.sent]
        sacks=[t for t,d in b.sent for c in parse_packet(d)[3] if isinstance(c,SackChunk)]
        self.assertEqual(len(data),40)
        self.assertEqual(max(per_packet),1)                     # aiortc: no bundling
        first,last=data[0][0],data[-1][0]
        in_first_flight=[t for t,_ in data if not sacks or t<min(sacks)]
        print('BURST_FLIGHT',dict(span_ms=(last-first)*1000,before_first_sack=len(in_first_flight),packets=len(per_packet)))
        self.assertLess(last-first,.028*5)                      # far tighter than the paced pipeline path
        self.assertGreaterEqual(len(in_first_flight),17)       # > 16 chunks leave before the first SACK returns
        await client.stop();await server.stop()
