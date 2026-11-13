from array import array
from unittest.mock import AsyncMock
import pytest
from pipecat.frames.frames import TTSAudioRawFrame,TTSStartedFrame,TTSStoppedFrame,InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from tts_leading_silence import LeadingSilence,TTSLeadingSilence


def pcm(value,n=320):return array('h',[value]*n).tobytes()


@pytest.mark.parametrize('chunk',[2,106,640,1280,4096])
def test_partition_invariant_preserves_onset_and_all_interior_pcm(chunk):
    quiet=pcm(31,16000);speech=pcm(33)+pcm(0,16000)+pcm(2000)+pcm(0,71)
    source=quiet+speech;t=LeadingSilence();out=b''
    for i in range(0,len(source),chunk):out+=t.feed(source[i:i+chunk])
    out+=t.finish()
    assert out==quiet[-1280:]+speech
    assert t.removed_samples==16000-640


def test_threshold_and_short_all_silent_tail():
    t=LeadingSilence()
    assert t.feed(pcm(32,1000))==b''
    assert t.finish()==pcm(32,680)
    assert t.removed_samples==320


@pytest.mark.asyncio
async def test_context_reset_cancel_passthrough_and_frame_metadata():
    enabled=True;events=[]
    p=TTSLeadingSilence(lambda:enabled,lambda name,**kw:events.append((name,kw)))
    p.push_frame=AsyncMock();d=FrameDirection.DOWNSTREAM
    await p.process_frame(TTSStartedFrame(context_id='one'),d)
    await p.process_frame(TTSAudioRawFrame(pcm(0,16000),16000,1,context_id='one'),d)
    assert p.push_frame.await_count==1
    active=TTSAudioRawFrame(pcm(1000),16000,1,context_id='one');active.pts=123
    await p.process_frame(active,d)
    output=p.push_frame.call_args.args[0]
    assert output.audio==pcm(0,640)+pcm(1000) and output.num_frames==960
    assert output.context_id=='one' and output.pts==123
    await p.process_frame(InterruptionFrame(),d)
    enabled=False
    await p.process_frame(TTSStartedFrame(context_id='two'),d)
    quiet=TTSAudioRawFrame(pcm(0,16000),16000,1,context_id='two')
    await p.process_frame(quiet,d)
    assert p.push_frame.call_args.args[0] is quiet
    assert any(n=='tts-leading-trim' and v['removed_samples']==15360 for n,v in events)


@pytest.mark.asyncio
async def test_all_silent_end_flushes_only_retained_tail():
    p=TTSLeadingSilence(True);p.push_frame=AsyncMock();d=FrameDirection.DOWNSTREAM
    await p.process_frame(TTSStartedFrame(context_id='one'),d)
    await p.process_frame(TTSAudioRawFrame(pcm(1,1000),16000,1,context_id='one'),d)
    await p.process_frame(TTSStoppedFrame(context_id='one'),d)
    assert p.push_frame.call_args_list[-2].args[0].audio==pcm(1,680)
    assert isinstance(p.push_frame.call_args.args[0],TTSStoppedFrame)
