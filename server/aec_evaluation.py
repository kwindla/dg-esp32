"""Explicit scored-test instrumentation for the real Flux demo pipeline.

No transcripts or user audio are injected. Only predeclared TTSSpeakFrame
texts and explicitly declared scripted interruptions can be requested. Importing this module opens no file or service.
"""
import asyncio
import hashlib
from collections import deque
import json
import os
from pathlib import Path
import time

from pipecat.frames.frames import (
    LLMContextFrame, TTSAudioRawFrame, TTSSpeakFrame, InterruptionFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


def enabled(directory, device_aec):
    if directory and not device_aec:
        raise ValueError('AEC evaluation requires firmware reporting aec=true')
    return bool(directory)


class Evaluation:
    def __init__(self, directory, connection, *, profile="demo"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        plan = json.loads((self.directory/'protocol.json').read_text())
        self.scripts = {row['id']:row['text'] for row in plan['echo_replies']}
        self.tts_trim_by_id = {row['id']:row.get('trim_leading',False) for row in plan['echo_replies']}
        self.scripts.update({key:plan['barge_bot_text'] for key in plan['barge_ids']})
        self.ids = list(self.scripts)
        self.index = 0
        self.declared_once = plan.get("dispatch") == "declared-once"
        self.used = set()
        self.real_bot_ids = set(plan.get("real_bot_ids", []))
        self.listen_ids = set(plan.get("listen_ids", []))
        self.context_pcm = {}
        self.first_context = {}
        self.interrupt_ids = set(plan.get('interrupt_ids', []))
        self.interrupted_ids = set()
        self.host_interrupts = plan.get('host_interrupts', {})
        if any(key not in self.interrupt_ids or value.get('next_delay_s') != 1.0 or
               not isinstance(value.get('next_text'), str) or not value['next_text'].strip()
               for key,value in self.host_interrupts.items()):
            raise ValueError('Invalid declared host next-reply')
        # Decision-71 smoke burst: inert unless the protocol declares it. A bounded,
        # predeclared number of bursts of back-to-back server messages, test-only.
        burst = plan.get('burst')
        if burst is not None and (burst.get('messages') != 40 or type(burst.get('max_bursts')) is not int
                                  or not 1 <= burst['max_bursts'] <= 10 or burst.get('path') != 'datachannel-direct'):
            raise ValueError('Invalid declared burst')
        self.burst = burst
        self.bursts_sent = 0
        self.prepare_tts = None
        self.current = None
        self.connection = connection
        self.pc_id = connection.pc_id
        self.command_id = 0
        # A second peer/generation can never inherit a partly scored sequence.
        with (self.directory/'session-owner.json').open('x') as stream:
            json.dump(dict(pid=os.getpid(), pc_id=self.pc_id), stream)
        self.log('evaluation-start', aec_full_duplex=True, user_mute=False,
                 profile=profile, stt='DeepgramSTTService' if profile == 'dev' else 'DeepgramFluxSTTService', tts='DeepgramFluxTTSService')
        self.observer = EvaluationObserver(self)
        self.context_filter = ScriptOnlyContext(self)
        self.tts_reference = TTSReference(self)

    def log(self, event, **data):
        with (self.directory/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(t=time.monotonic(), event=event, pc_id=self.pc_id,
                pid=os.getpid(), case_id=self.current, **data), ensure_ascii=False)+'\n')

    def observer_health(self):
        receivers=self.connection.pc.getReceivers()
        senders=self.connection.pc.getSenders()
        auth=[dict(packets=len(getattr(r,'_dg_rtp',[])),
                   truncated=getattr(r,'_dg_rtp_truncated',False)) for r in receivers]
        sockets=[]
        for sender in senders:
            ice=sender.transport.transport._connection
            sockets.append(dict(packets=getattr(ice,'_dg_socket_count',0),
                                truncated=getattr(ice,'_dg_socket_truncated',False)))
        import rtp_evidence
        self.log('rtp-observer-health',authenticated=auth,sockets=sockets,
                 sctp=rtp_evidence.sctp_status(self.connection),
                 passed=bool(auth and sockets) and all(r['packets']>0 and not r['truncated'] for r in auth+sockets))

    async def control(self, worker):
        path = self.directory/'command.json'
        try:
            while True:
                if path.exists():
                    request = json.loads(path.read_text())
                    number = request.get('number')
                    if type(number) is not int or number < self.command_id:
                        raise ValueError('Invalid or regressed evaluation command')
                    if number > self.command_id:
                        if number != self.command_id+1:
                            raise ValueError('Missing evaluation command')
                        self.command_id = number
                        if request.get('op') in ('speak', 'begin'):
                            key = request.get('case_id')
                            if (self.current is not None or self.index >= len(self.ids)
                                    or (key not in self.scripts or key in self.used if self.declared_once else key != self.ids[self.index])):
                                raise ValueError('Duplicate, out-of-order or overlapping original')
                            if request["op"] == "begin" and key not in self.listen_ids:
                                raise ValueError("Undeclared listening control")
                            self.used.add(key)
                            self.current = key
                            self.index += 1
                            if request['op'] == 'begin':
                                self.log('listen-begin', number=number)
                                continue
                            self.log('script-request', text=self.scripts[key], number=number)
                            if self.prepare_tts is not None:
                                await self.prepare_tts()
                            self.log('script-queued', number=number)
                            await worker.queue_frame(TTSSpeakFrame(self.scripts[key]))
                        elif request.get('op') == 'interrupt':
                            key = request.get('case_id')
                            if key != self.current or key not in self.interrupt_ids or key in self.interrupted_ids:
                                raise ValueError('Undeclared or duplicate scripted interruption')
                            self.interrupted_ids.add(key)
                            interrupt=InterruptionFrame()
                            self.log('script-interrupt', number=number, host_issued_s=request.get('host_issued_s'), frame_id=interrupt.id)
                            await worker.queue_frame(interrupt)
                            if key in self.host_interrupts:
                                follow=self.host_interrupts[key]
                                if follow['next_delay_s'] != 1.0 or not follow['next_text']:
                                    raise ValueError('Invalid declared host next-reply')
                                await asyncio.sleep(follow['next_delay_s'])
                                if self.prepare_tts is not None:
                                    await self.prepare_tts()
                                self.log('host-next-reply-queued', text=follow['next_text'])
                                await worker.queue_frame(TTSSpeakFrame(follow['next_text']))
                        elif request.get('op') == 'skip':
                            key = request.get('case_id')
                            if (self.current is not None or self.index >= len(self.ids)
                                    or key != self.ids[self.index]
                                    or request.get('reason') not in ('noise-precheck-failed', 'retry-not-required')):
                                raise ValueError('Invalid skipped acquisition')
                            self.index += 1
                            self.log('case-skipped', skipped_case_id=key, reason=request['reason'], number=number)
                        elif request.get('op') == 'seal' and request.get('case_id') == self.current:
                            self.observer_health()
                            self.log('case-sealed', number=number)
                            self.current = None
                        elif request.get('op') == 'burst' and self.current is None:
                            index = request.get('burst')
                            send = getattr(self.connection, 'send_app_message', None)
                            if (self.burst is None or send is None or
                                    index != self.bursts_sent+1 or index > self.burst['max_bursts']):
                                raise ValueError('Undeclared or out-of-order burst')
                            self.bursts_sent = index
                            # Synchronous writes to the data channel with no await in between: every chunk is
                            # queued for one SCTP flight (pipeline sends were paced at ~36/s and never bunched).
                            first = time.monotonic()
                            for i in range(self.burst['messages']):
                                send({'label': 'rtvi-ai', 'type': 'server-message', 'data': {'t': 'burst', 'd': {'b': index, 'id': i}}})
                            self.log('burst-sent', number=number, burst=index, messages=self.burst['messages'],
                                     path='datachannel-direct', first_send_s=first, last_send_s=time.monotonic())
                        elif request.get('op') == 'health' and self.current is None:
                            self.observer_health()
                            self.log('health-ack',number=number)
                        else:
                            raise ValueError('Unknown evaluation operation or case')
                await asyncio.sleep(.02)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log('evaluation-error', error=type(exc).__name__+': '+str(exc))
            await worker.cancel()
            raise


class ScriptOnlyContext(FrameProcessor):
    """Suppress automatic LLM responses after the real user aggregator.

    Start/end, input audio, STT transcripts, turn proposals and interruptions
    still propagate. No replacement frames or transcripts are created.
    """
    def __init__(self, evaluation):
        super().__init__()
        self.evaluation = evaluation

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self.evaluation.current not in self.evaluation.real_bot_ids:
                self.evaluation.log('automatic-llm-response-suppressed')
                return
            self.evaluation.log('real-bot-llm-request')
        await self.push_frame(frame, direction)


class TTSReference(FrameProcessor):
    def __init__(self, evaluation):
        super().__init__()
        self.evaluation = evaluation
        self.offsets = {}

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            e = self.evaluation
            if e.current is None or frame.sample_rate != 16000 or frame.num_channels != 1 or len(frame.audio)%2:
                e.log('evaluation-error', error='Unattributed or non-16k mono TTS PCM')
                raise ValueError('Invalid evaluation TTS PCM')
            context = getattr(frame, 'context_id', None)
            if e.declared_once and not context:
                raise ValueError('Campaign TTS requires context attribution')
            e.first_context.setdefault(e.current, context)
            if not e.declared_once or context == e.first_context[e.current]:
                with (e.directory/(e.current+'.pcm')).open('ab') as stream:
                    stream.write(frame.audio)
            if context:
                name = 'context-'+hashlib.sha256(context.encode()).hexdigest()+'.pcm'
                with (e.directory/name).open('ab') as stream:
                    stream.write(frame.audio)
                if context not in e.context_pcm:
                    e.context_pcm[context] = name
                    e.log('tts-context-pcm', context_id=context, path=name)
            self.offsets[e.current] = self.offsets.get(e.current, 0)+len(frame.audio)//2
            e.log('tts-pcm', context_id=context, samples_total=self.offsets[e.current], bytes=len(frame.audio))
        await self.push_frame(frame, direction)


class EvaluationObserver(BaseObserver):
    TYPES = {
        'ProposedUserStartedSpeakingFrame', 'ProposedUserStoppedSpeakingFrame',
        'VADUserStartedSpeakingFrame', 'VADUserStoppedSpeakingFrame',
        'UserStartedSpeakingFrame', 'UserStoppedSpeakingFrame',
        'TranscriptionFrame', 'InterimTranscriptionFrame', 'EagerTranscriptionFrame',
        'UserMuteStartedFrame', 'UserMuteStoppedFrame', 'InterruptionFrame',
        'BotStartedSpeakingFrame', 'BotStoppedSpeakingFrame',
        'TTSStartedFrame', 'TTSStoppedFrame', 'ErrorFrame', 'FatalErrorFrame',
    }

    def __init__(self, evaluation):
        super().__init__()
        self.evaluation = evaluation
        self.seen, self.order = set(), deque()
        self.bot_active = False

    async def on_push_frame(self, data):
        frame = data.frame
        name = type(frame).__name__
        # Count logical broadcast events once, as RTVIObserver does.
        if frame.broadcast_sibling_id is not None and data.direction != FrameDirection.DOWNSTREAM:
            return
        if name not in self.TYPES or frame.id in self.seen:
            return
        self.seen.add(frame.id); self.order.append(frame.id)
        if len(self.order)>10000:
            self.seen.discard(self.order.popleft())
        if name == 'BotStartedSpeakingFrame': self.bot_active = True
        if name == 'BotStoppedSpeakingFrame': self.bot_active = False
        self.evaluation.log('frame', frame=name, frame_id=frame.id, source=type(data.source).__name__,
                            direction=getattr(getattr(data,'direction',None),'name',None),
                            bot_active=self.bot_active, vad_start=name == 'VADUserStartedSpeakingFrame',
                            accepted_turn=name == 'UserStartedSpeakingFrame', interruption=name == 'InterruptionFrame',
                            text=getattr(frame, 'text', None), error=getattr(frame, 'error', None),
                            context_id=getattr(frame, 'context_id', None))
