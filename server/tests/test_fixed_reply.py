import asyncio
from types import SimpleNamespace
from bot import FixedReply

class Log:
    def __init__(self): self.rows=[]
    def write(self,event,**data):self.rows.append((event,data))

async def test_fixed_reply_is_gated_on_turn_end_and_deduplicates_events():
    log=Log();audit=SimpleNamespace(turn=1);reply=FixedReply(log,audit,settle_secs=.01);emitted=[]
    async def emit(frames):emitted.extend(frames)
    assert not emitted
    await reply.user_turn_stopped(1,'Please tell me the capital of France.',emit)
    await reply.user_turn_stopped(1,'Please tell me the capital of France.',emit)
    await asyncio.sleep(.03)
    assert len(emitted)==1
    audit.turn=2
    await reply.user_turn_stopped(2,'Another question.',emit)
    await asyncio.sleep(.03)
    assert len(emitted)==2
    await reply.user_turn_stopped(3,'',emit)
    assert len(emitted)==2
    assert [r[1]['turn'] for r in log.rows]==[1,2]

async def test_tts_audit_does_not_change_vad_state_and_logs_first_byte_once():
    from bot import AuditObserver
    from pipecat.frames.frames import VADUserStartedSpeakingFrame,TTSStartedFrame,TTSAudioRawFrame,TTSStoppedFrame
    transport=SimpleNamespace(input=lambda:None,output=lambda:None)
    log=Log();audit=AuditObserver(log,transport)
    for frame in [VADUserStartedSpeakingFrame(),TTSStartedFrame(),TTSAudioRawFrame(b'\0\0'*320,16000,1),
                  TTSAudioRawFrame(b'\0\0'*320,16000,1),TTSStoppedFrame()]:
        await audit.on_push_frame(SimpleNamespace(frame=frame))
    assert audit.speaking
    assert audit.turn==1
    assert [e for e,_ in log.rows]==['vad-start','tts-start','first-tts-byte','tts-stop']

async def test_multiple_final_transcripts_are_forwarded_without_early_reply():
    from pipecat.frames.frames import TranscriptionFrame,TTSSpeakFrame
    from pipecat.processors.frame_processor import FrameDirection
    log=Log();reply=FixedReply(log,SimpleNamespace(turn=1));forwarded=[]
    async def push(frame,direction=FrameDirection.DOWNSTREAM):forwarded.append(frame)
    reply.push_frame=push
    for text in ('Please tell me the capital','of France.'):
        await reply.process_frame(TranscriptionFrame(text=text,user_id='user',timestamp='test'),FrameDirection.DOWNSTREAM)
    assert len(forwarded)==2
    assert not any(isinstance(f,TTSSpeakFrame) for f in forwarded)
    assert [d['turn'] for e,d in log.rows if e=='final-transcript']==[1,1]

async def test_late_final_is_coalesced_before_one_reply():
    log=Log();audit=SimpleNamespace(turn=1,speaking=False)
    reply=FixedReply(log,audit,settle_secs=.04);emitted=[];published=[]
    async def emit(frames):emitted.extend(frames)
    async def publish(turn,text):published.append((turn,text))
    await reply.user_turn_stopped(1,'Please tell me the capital of',emit,publish)
    await asyncio.sleep(.02)
    assert not emitted
    await reply.user_turn_stopped(1,'France.',emit,publish)
    await asyncio.sleep(.02)
    assert not emitted
    await asyncio.sleep(.04)
    assert len(emitted)==1
    assert published==[(1,'Please tell me the capital of France.')]
    await reply.user_turn_stopped(1,'France.',emit,publish)
    assert len(emitted)==1
    await reply.close()

async def test_pending_reply_cancels_on_disconnect_or_new_user_turn():
    log=Log();audit=SimpleNamespace(turn=1,speaking=False)
    reply=FixedReply(log,audit,settle_secs=.01);emitted=[]
    async def emit(frames):emitted.extend(frames)
    await reply.user_turn_stopped(1,'old turn',emit)
    await reply.close()
    await asyncio.sleep(.02)
    assert not emitted
    await reply.user_turn_stopped(1,'old turn',emit)
    audit.turn=2
    await asyncio.sleep(.02)
    assert not emitted
    await reply.close()

async def test_noise_turn_during_settle_defers_and_recovers_pending_reply():
    log=Log();audit=SimpleNamespace(turn=1,speaking=False)
    reply=FixedReply(log,audit,settle_secs=.01);emitted=[];published=[]
    async def emit(frames):emitted.extend(frames)
    async def publish(turn,text):published.append((turn,text))
    await reply.user_turn_stopped(1,'The capital of France?',emit,publish)
    audit.turn=2;audit.speaking=True
    await asyncio.sleep(.03)
    assert not emitted
    audit.speaking=False
    await reply.user_turn_stopped(2,'',emit,publish)
    await asyncio.sleep(.03)
    assert len(emitted)==1
    assert published==[(1,'The capital of France?')]
    await reply.user_turn_stopped(2,'',emit,publish)
    await asyncio.sleep(.02)
    assert len(emitted)==1
    await reply.close()

async def test_strict_display_mode_scripts_long_reply(monkeypatch):
    from settings import REPLY
    monkeypatch.setenv('ECHOEAR_STRICT_MERGE','1')
    reply=FixedReply(Log(),SimpleNamespace(turn=1),settle_secs=.01)
    emitted=[]
    async def emit(frames):emitted.extend(frames)
    await reply.user_turn_stopped(1,'Please tell me the capital of France.',emit)
    await asyncio.sleep(.03)
    assert len(emitted)==1
    assert len(emitted[0].text.split())>=35
    assert 'Pineapple' in emitted[0].text
    await reply.close()
    monkeypatch.delenv('ECHOEAR_STRICT_MERGE')
    ordinary=FixedReply(Log(),SimpleNamespace(turn=1))
    assert ordinary.reply_text==REPLY
    await ordinary.close()
