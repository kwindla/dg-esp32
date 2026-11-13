from types import SimpleNamespace
import wave
from pipecat.frames.frames import (LLMFullResponseStartFrame, TTSStartedFrame,
    TTSAudioRawFrame, TTSStoppedFrame, InterruptionFrame)
from pipecat.processors.frame_processor import FrameDirection
from bot import TTSReference


async def test_cancelled_tts_prefix_is_saved_separately_from_next_complete_segment(tmp_path,monkeypatch):
    monkeypatch.setenv('TTS_REFERENCE_DIR',str(tmp_path))
    rows=[]
    log=SimpleNamespace(pc_id='test',write=lambda event,**data:rows.append((event,data)))
    reference=TTSReference(log);forwarded=[]
    async def push(frame,direction):forwarded.append(frame)
    async def restart():pass
    reference.push_frame=push;reference._start_interruption=restart
    first=b'\x10\x00'*160;second=b'\x20\x00'*320
    frames=[LLMFullResponseStartFrame(),TTSStartedFrame(),TTSAudioRawFrame(first,16000,1),
            InterruptionFrame(),LLMFullResponseStartFrame(),TTSStartedFrame(),
            TTSAudioRawFrame(second,16000,1),TTSStoppedFrame()]
    for frame in frames:await reference.process_frame(frame,FrameDirection.DOWNSTREAM)
    assert len(forwarded)==len(frames)
    assert [r['response_id'] for _,r in rows]==[1,2]
    assert [r['cancelled'] for _,r in rows]==[True,False]
    for (_,row),expected in zip(rows,[first,second]):
        with wave.open(row['path']) as stream:
            assert stream.getnframes()==len(expected)//2
            assert stream.readframes(stream.getnframes())==expected
