from types import SimpleNamespace
import pytest
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from voice_audit import VoiceResponseAudit, VoiceSTTAudit, half_duplex_strategies, VOICE_SPEECH_TIMEOUT
from test_fixed_reply import Log


async def test_response_evidence_snapshots_turn_and_does_not_count_sentences():
    log=Log(); audit=SimpleNamespace(turn=7); observer=VoiceResponseAudit(log,audit); forwarded=[]
    async def push(frame,direction): forwarded.append((frame,direction))
    observer.push_frame=push
    frames=[LLMFullResponseStartFrame(), LLMTextFrame('Paris.'), LLMTextFrame(' It is in France.'), LLMFullResponseEndFrame()]
    for i,frame in enumerate(frames):
        if i==2:audit.turn=8
        await observer.process_frame(frame,FrameDirection.DOWNSTREAM)
    assert [r[0] for r in log.rows]==['llm-start','llm-first-token','llm-response']
    assert all(data['turn']==7 for _,data in log.rows)
    assert log.rows[-1][1]['text']=='Paris. It is in France.'
    assert len(forwarded)==len(frames)
    await observer.process_frame(LLMFullResponseStartFrame(),FrameDirection.DOWNSTREAM)
    assert log.rows[-1][1]['response_id']==2


async def test_stt_audit_keeps_final_fragments_and_ignores_upstream():
    log=Log(); observer=VoiceSTTAudit(log,SimpleNamespace(turn=3)); forwarded=[]
    async def push(frame,direction):forwarded.append(frame)
    observer.push_frame=push
    for text in ['capital of','France']:
        await observer.process_frame(TranscriptionFrame(text,'user','test'),FrameDirection.DOWNSTREAM)
    await observer.process_frame(TranscriptionFrame('upstream','user','test'),FrameDirection.UPSTREAM)
    assert [d['text'] for _,d in log.rows]==['capital of','France']
    assert len(forwarded)==3


def test_half_duplex_disables_both_start_paths_and_keeps_smartturn():
    strategies=half_duplex_strategies()
    assert [type(x).__name__ for x in strategies.start]==['VADUserTurnStartStrategy','TranscriptionUserTurnStartStrategy']
    assert all(not x._enable_interruptions for x in strategies.start)
    assert [type(x).__name__ for x in strategies.stop]==['TurnAnalyzerUserTurnStopStrategy']


@pytest.mark.parametrize('vad_present', [True, False])
async def test_voice_settlement_coalesces_final_fragments_before_inference(vad_present):
    import asyncio
    from pipecat.frames.frames import VADUserStartedSpeakingFrame,VADUserStoppedSpeakingFrame,LLMContextFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker,PipelineParams
    from pipecat.workers.runner import WorkerRunner
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair,LLMUserAggregatorParams
    from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
    strategies=half_duplex_strategies(settle_voice=True)
    assert isinstance(strategies.stop[0],SpeechTimeoutUserTurnStopStrategy)
    # Use the real policy, including the measured 1.33-second no-VAD gap.
    context=LLMContext();user,assistant=LLMContextAggregatorPair(context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=strategies))
    snapshots=[]
    class Sink(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            if isinstance(frame,LLMContextFrame) and direction==FrameDirection.DOWNSTREAM:
                snapshots.append([dict(m) for m in frame.context.get_messages()])
            await self.push_frame(frame,direction)
    worker=PipelineWorker(Pipeline([user,Sink()]),enable_rtvi=False,
                          params=PipelineParams(audio_in_sample_rate=16000))
    runner=WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker);task=asyncio.create_task(runner.run())
    try:
        frames = [VADUserStartedSpeakingFrame(),
            TranscriptionFrame('What is the capital','user','test',finalized=True),
            VADUserStoppedSpeakingFrame(stop_secs=.2)] if vad_present else [
                TranscriptionFrame("What's",'user','test',finalized=False)]
        await worker.queue_frames(frames)
        await asyncio.sleep(.15 if vad_present else 1.35)
        assert snapshots==[]  # The old SmartTurn path could already infer.
        await worker.queue_frames([TranscriptionFrame('of France?' if vad_present else 'two plus three?',
            'user','test',finalized=vad_present)])
        await asyncio.sleep(VOICE_SPEECH_TIMEOUT+.2)
        assert len(snapshots)==1
        assert ('What is the capital of France?' if vad_present else "What's two plus three?") in snapshots[0][-1]['content']
    finally:
        await worker.cancel();await asyncio.wait_for(task,5)


@pytest.mark.parametrize('kind',['InterruptionFrame','CancelFrame'])
async def test_cancelled_generation_is_logged_once_and_tokens_do_not_leak(kind):
    from pipecat.frames import frames
    log=Log(); observer=VoiceResponseAudit(log,SimpleNamespace(turn=2));forwarded=[]
    async def push(frame,direction):forwarded.append(frame)
    observer.push_frame=push
    # This unit test calls processors directly; pipeline lifecycle is covered
    # by the real-worker settlement test. Suppress the base interruption task restart.
    async def restart(): pass
    observer._start_interruption=restart
    sequence=[LLMFullResponseStartFrame(),LLMTextFrame('The capital'),getattr(frames,kind)(),
              LLMTextFrame('late'),LLMFullResponseEndFrame(),LLMFullResponseStartFrame(),
              LLMTextFrame('Paris.'),LLMFullResponseEndFrame()]
    for frame in sequence:await observer.process_frame(frame,FrameDirection.DOWNSTREAM)
    cancelled=[d for event,d in log.rows if event=='llm-cancelled']
    assert cancelled==[dict(turn=2,response_id=1,text='The capital',reason=kind,generation_ended=False)]
    assert log.rows[-1][1]['text']=='Paris.' and len(forwarded)==len(sequence)


def test_voice_default_pins_smartturn_and_interrupts_both_start_paths():
    from voice_audit import voice_strategies
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    strategy=voice_strategies('smartturn')
    assert all(s._enable_interruptions for s in strategy.start)
    assert isinstance(strategy.stop[0]._turn_analyzer,LocalSmartTurnAnalyzerV3)
    assert strategy.stop[0]._turn_analyzer.params.stop_secs==3
    live=half_duplex_strategies()
    assert isinstance(live.stop[0]._turn_analyzer,LocalSmartTurnAnalyzerV3)
    assert live.stop[0]._turn_analyzer.params.stop_secs==3


def test_diagnostic_mute_comparison_keeps_interruptions_and_default(monkeypatch):
    from voice_audit import configure_voice_turns, voice_mute_strategies
    monkeypatch.setenv('STEP8E_DISABLE_BOT_MUTE','1')
    params={};strategies=configure_voice_turns(params,'smartturn')
    assert params['user_mute_strategies']==[]
    assert all(s._enable_interruptions for s in strategies.start)
    monkeypatch.delenv('STEP8E_DISABLE_BOT_MUTE')
    assert [type(s).__name__ for s in voice_mute_strategies()]==['AlwaysUserMuteStrategy']

async def test_broadcast_cancellation_precedes_service_end_and_is_deduplicated():
    log=Log();audit=SimpleNamespace(turn=1);observer=VoiceResponseAudit(log,audit)
    async def push(frame,direction):pass
    observer.push_frame=push
    await observer.process_frame(LLMFullResponseStartFrame(),FrameDirection.DOWNSTREAM)
    # The service handles this signal and emits End before forwarding it.
    audit.response_audit.cancel('InterruptionFrame')
    await observer.process_frame(LLMFullResponseEndFrame(),FrameDirection.DOWNSTREAM)
    audit.response_audit.cancel('InterruptionFrame')
    assert [event for event,_ in log.rows]==['llm-start','llm-cancelled']

@pytest.mark.parametrize('policy',['smartturn','timeout','8d'])
async def test_every_voice_arm_mutes_only_while_bot_speaks(policy):
    from voice_audit import configure_voice_turns
    from pipecat.turns.user_mute import AlwaysUserMuteStrategy
    from pipecat.frames.frames import BotStartedSpeakingFrame,BotStoppedSpeakingFrame,InputAudioRawFrame,InterruptionFrame
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
    params={};strategies=configure_voice_turns(params,policy)
    configured=LLMUserAggregatorParams(**params)
    assert configured.user_turn_strategies is strategies
    assert len(configured.user_mute_strategies)==1
    mute=configured.user_mute_strategies[0]
    assert isinstance(mute,AlwaysUserMuteStrategy)
    audio=InputAudioRawFrame(b'\0'*640,16000,1)
    assert not await mute.process_frame(audio)
    assert not await mute.process_frame(InterruptionFrame())
    assert await mute.process_frame(BotStartedSpeakingFrame())
    assert await mute.process_frame(audio)
    assert await mute.process_frame(InterruptionFrame())
    assert not await mute.process_frame(BotStoppedSpeakingFrame())
    assert not await mute.process_frame(audio)
    assert all(s._enable_interruptions==(policy!='8d') for s in strategies.start)


@pytest.mark.parametrize('timeout',[False,True])
async def test_audited_smartturn_separates_actual_timeout_from_model(monkeypatch,timeout):
    from voice_audit import AuditedSmartTurn
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn
    monkeypatch.setattr(LocalSmartTurnAnalyzerV3,'__init__',BaseSmartTurn.__init__)
    async def analyze(self):
        return EndOfTurnState.INCOMPLETE, None if timeout else SimpleNamespace(probability=.1)
    monkeypatch.setattr(LocalSmartTurnAnalyzerV3,'analyze_end_of_turn',analyze)
    log=Log();analyzer=AuditedSmartTurn(log);analyzer.set_sample_rate(16000)
    if timeout:
        analyzer.append_audio(bytes(640),True)
        for _ in range(150):analyzer.append_audio(bytes(640),False)
    await analyzer.analyze_end_of_turn()
    event,data=log.rows[-1]
    assert event=='smartturn-inference'
    assert data['kind']==('stop_secs_timeout' if timeout else 'model')
    assert data['verdict']==(None if timeout else 'INCOMPLETE')
    assert not analyzer._audit_timeout_pending


async def test_real_aggregator_suppresses_continuation_and_interruptions_only_while_muted():
    import asyncio
    from pipecat.frames.frames import BotStartedSpeakingFrame,BotStoppedSpeakingFrame,InputAudioRawFrame,InterruptionFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator,LLMUserAggregatorParams
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker,PipelineParams
    from pipecat.workers.runner import WorkerRunner
    from pipecat.processors.frame_processor import FrameProcessor
    from voice_audit import voice_mute_strategies
    context=LLMContext()
    user=LLMUserAggregator(context,params=LLMUserAggregatorParams(user_mute_strategies=voice_mute_strategies()))
    def frames():return [TranscriptionFrame('of France?','user','test'),InterruptionFrame(),InputAudioRawFrame(bytes(640),16000,1)]
    assert not any([await user._maybe_mute_frame(f) for f in frames()])
    forwarded=[]
    class Sink(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            forwarded.append(frame)
            await self.push_frame(frame,direction)
    worker=PipelineWorker(Pipeline([user,Sink()]),enable_rtvi=False,params=PipelineParams(audio_in_sample_rate=16000))
    runner=WorkerRunner(handle_sigint=False);await runner.add_workers(worker);task=asyncio.create_task(runner.run())
    try:
        await worker.queue_frames([BotStartedSpeakingFrame()]);await asyncio.sleep(.1)
        muted=frames();assert all([await user._maybe_mute_frame(f) for f in muted])
        await worker.queue_frames(muted);await asyncio.sleep(.1)
        assert not any(f.id in {m.id for m in muted} for f in forwarded)
        assert not user._aggregation
        await worker.queue_frames([BotStoppedSpeakingFrame()]);await asyncio.sleep(.1)
        assert not any([await user._maybe_mute_frame(f) for f in frames()])
        unmuted=frames()
        await worker.queue_frames(unmuted[1:]);await asyncio.sleep(.1)
        assert all(any(f.id==sent.id for f in forwarded) for sent in unmuted[1:])
        await worker.queue_frames(unmuted[:1]);await asyncio.sleep(.1)
        assert any(message['content']=='of France?' for message in context.get_messages())
    finally:
        await worker.cancel();await asyncio.wait_for(task,5)


async def test_smartturn_vad_missed_second_fragment_uses_silence_fallback_with_full_text():
    import asyncio
    from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn,SmartTurnParams
    from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
    from pipecat.frames.frames import InputAudioRawFrame,VADUserStartedSpeakingFrame,VADUserStoppedSpeakingFrame,InterimTranscriptionFrame,LLMContextFrame,STTMetadataFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker,PipelineParams
    from pipecat.workers.runner import WorkerRunner
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair,LLMUserAggregatorParams
    from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    class IncompleteAnalyzer(BaseSmartTurn):
        model_calls=0
        def _predict_endpoint(self,audio):return dict(prediction=0,probability=0.)
        def _process_speech_segment(self,audio):
            if audio:self.model_calls+=1
            return EndOfTurnState.INCOMPLETE,None
    analyzer=IncompleteAnalyzer(params=SmartTurnParams(stop_secs=3))
    context=LLMContext();user,_=LLMContextAggregatorPair(context,user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)])))
    snapshots=[]
    class Sink(FrameProcessor):
        async def process_frame(self,frame,direction):
            await super().process_frame(frame,direction)
            if isinstance(frame,LLMContextFrame) and direction==FrameDirection.DOWNSTREAM:
                snapshots.append([dict(m) for m in frame.context.get_messages()])
            await self.push_frame(frame,direction)
    worker=PipelineWorker(Pipeline([user,Sink()]),enable_rtvi=False,params=PipelineParams(audio_in_sample_rate=16000))
    runner=WorkerRunner(handle_sigint=False);await runner.add_workers(worker);task=asyncio.create_task(runner.run())
    try:
        await worker.queue_frames([STTMetadataFrame(service_name='test',ttfs_p99_latency=.01),
            VADUserStartedSpeakingFrame(),InputAudioRawFrame(bytes(640),16000,1),
            TranscriptionFrame('What is the capital','user','test',finalized=True),VADUserStoppedSpeakingFrame(stop_secs=.2)])
        await asyncio.sleep(.1)
        assert not snapshots and analyzer.model_calls==1
        # STT sees the continuation; VAD does not emit another edge. No
        # second model verdict is possible. Its fallback is a silence timeout.
        await worker.queue_frames([InterimTranscriptionFrame('of France?','user','test'),
            TranscriptionFrame('of France?','user','test',finalized=False)])
        await asyncio.sleep(.05)  # System audio frames can overtake queued data frames.
        await worker.queue_frames([InputAudioRawFrame(bytes(640),16000,1) for _ in range(150)])
        await asyncio.sleep(.2)
        assert len(snapshots)==1 and analyzer.model_calls==1
        assert 'What is the capital of France?' in snapshots[0][-1]['content']
    finally:
        await worker.cancel();await asyncio.wait_for(task,5)
