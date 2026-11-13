"""Offline real-aggregator regression: retained audio and STT delivery events.

No network/service/transport/LLM/TTS. This isolates turn strategy ordering, not
recognizer accuracy. Vendor replays use hil/echoear_aec_nova_replay.py instead.
"""
import asyncio
import time
from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame, STTMetadataFrame, EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from demo_policy import turn_policy, NOVA_TTFS_P99_S, transcript_boundary


async def replay(pcm, timeline, frames, *, budget=NOVA_TTFS_P99_S, boundary=True):
    params = turn_policy(profile='dev')
    stop = params.user_turn_strategies.stop[0]
    smart = stop._turn_analyzer
    predictions = []
    analyze = smart.analyze_end_of_turn
    async def measured():
        result = await analyze()
        predictions.append(dict(t=time.monotonic()-start, state=str(result[0]),
            timeout=stop._stt_timeout, finalized=stop._transcript_finalized,
            expired=stop._timeout_expired))
        return result
    smart.analyze_end_of_turn = measured
    user, _ = LLMContextAggregatorPair(LLMContext(), user_params=params)
    turns = []
    @user.event_handler('on_user_turn_stopped')
    async def stopped(aggregator, strategy, message):
        turns.append(dict(t=time.monotonic()-start, text=message.content))
    class Pass(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
    worker=PipelineWorker(Pipeline([Pass(), *([transcript_boundary('dev')] if boundary else []), user,Pass()]), enable_rtvi=False,
        idle_timeout_secs=None, params=PipelineParams(audio_in_sample_rate=16000))
    runner=WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    running=asyncio.create_task(runner.run())
    await asyncio.sleep(.1)
    await worker.queue_frame(STTMetadataFrame(service_name="RetainedNova",ttfs_p99_latency=budget))
    events=[dict(e,kind='audio') for e in timeline if e['kind']=='audio']
    events += [dict(e,kind='text') for e in frames if e['frame'] in ('TranscriptionFrame','InterimTranscriptionFrame')]
    start=time.monotonic()
    try:
        for e in sorted(events,key=lambda r:r['t']):
            await asyncio.sleep(max(0,start+e['t']-time.monotonic()))
            if e['kind']=='audio':f=InputAudioRawFrame(pcm[e['a']*2:e['b']*2],16000,1)
            else:
                cls=TranscriptionFrame if e['frame']=='TranscriptionFrame' else InterimTranscriptionFrame
                f=cls(e['text'],'',str(e['t']))
                if isinstance(f,TranscriptionFrame):f.finalized=e.get('finalized',False)
            await worker.queue_frame(f)
        await asyncio.sleep(1.2)
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(running,10)
    finally:
        if not running.done():
            await worker.cancel();await running
    return dict(turns=turns,predictions=predictions)
