import asyncio
import json
from types import SimpleNamespace
import wave
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from aec_session_recorder import SessionRecorder


def events(rec):
    return [json.loads(x) for x in (rec.directory/'events.jsonl').read_text().splitlines()]


def test_disabled(monkeypatch):
    monkeypatch.delenv('ECHOEAR_AEC_SESSION_DIR', raising=False)
    assert SessionRecorder.from_environment() is None


def test_per_session_lifecycle(tmp_path):
    a, b = SessionRecorder(tmp_path), SessionRecorder(tmp_path)
    assert a.directory != b.directory
    frame = InputAudioRawFrame(audio=b'\x01\x00'*320, sample_rate=16000, num_channels=1)
    a.audio('uplink', frame)
    a.close('cancel')
    a.close()
    b.close('error')
    with wave.open(str(a.directory/'uplink.wav')) as w:
        assert w.getnframes() == 320
    assert events(a)[1]['sample_start'] == 0
    assert events(a)[1]['sample_end'] == 320
    assert events(a)[-1]['reason'] == 'cancel'
    assert a.events.closed and b.events.closed


def test_gap_and_format_drop_are_not_silence(tmp_path):
    r = SessionRecorder(tmp_path)
    frame = InputAudioRawFrame(audio=b'\x01\x00'*320, sample_rate=16000, num_channels=1)
    r.audio('uplink', frame)
    r.previous['uplink'] = (0, 320)
    r.audio('uplink', frame)
    bad = InputAudioRawFrame(audio=b'\x01\x00'*320, sample_rate=48000, num_channels=1)
    r.audio('uplink', bad)
    r.close()
    rows = events(r)
    assert rows[2]['gap_suspected']
    assert rows[3]['kind'] == 'gap' and rows[3]['drop']
    with wave.open(str(r.directory/'uplink.wav')) as w:
        assert w.getnframes() == 640


@pytest.mark.asyncio
async def test_tap_direct_order_and_error_cleanup(tmp_path):
    r = SessionRecorder(tmp_path)
    tap = r.tap('flux')
    assert tap._enable_direct_mode
    frame = InterruptionFrame()
    tap.push_frame = AsyncMock()
    await tap.process_frame(frame, FrameDirection.DOWNSTREAM)
    tap.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)
    assert any(x['kind'] == 'flux-frame' and x['frame'] == 'InterruptionFrame' for x in events(r))
    tap.push_frame = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await tap.process_frame(frame, FrameDirection.DOWNSTREAM)
    assert r.closed


@pytest.mark.asyncio
async def test_observer_events_and_rtvi(tmp_path):
    r = SessionRecorder(tmp_path)
    frame = InterruptionFrame()
    data = SimpleNamespace(frame=frame, source=object(), direction=FrameDirection.DOWNSTREAM, timestamp=123)
    await r.observer.on_push_frame(data)
    await r.observer.on_push_frame(data)
    data.frame = SimpleNamespace(id=987654, message={'label': 'rtvi-ai', 'type': 'bot-interrupted'})
    await r.observer.on_push_frame(data)
    r.close()
    assert len([x for x in events(r) if x['kind'] == 'frame']) == 1
    assert any(x['kind'] == 'rtvi-output' for x in events(r))


def test_close_all_files_if_log_fails(tmp_path):
    r = SessionRecorder(tmp_path)
    with patch.object(r, 'log', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            r.close()
    assert r.closed and r.events.closed
    assert all(x._file is None for x in r.streams.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError('setup failed'), asyncio.CancelledError()])
async def test_demo_wrapper_closes_on_setup_error_and_cancel(tmp_path, monkeypatch, failure):
    import demo_bot
    recorder = SessionRecorder(tmp_path)
    monkeypatch.setenv('ECHOEAR_AEC_SESSION_DIR', str(tmp_path))
    monkeypatch.setattr(SessionRecorder, 'from_environment', lambda: recorder)
    monkeypatch.setattr(demo_bot, '_bot', AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await demo_bot.bot(None)
    assert recorder.closed and recorder.events.closed


def test_transport_write_anchor_records_silence_and_non_silence(tmp_path):
    from pipecat.frames.frames import TTSAudioRawFrame
    r = SessionRecorder(tmp_path)
    for value in (12, 32, 33):
        r.transport_write(TTSAudioRawFrame(audio=value.to_bytes(2, 'little')*640,
                                         sample_rate=16000, num_channels=1))
    r.close()
    assert [e['non_silent'] for e in events(r) if e['kind']=='tts-transport-write'] == [False, False, True]


@pytest.mark.asyncio
async def test_written_context_follows_output_boundaries_not_new_requests(tmp_path):
    from unittest.mock import AsyncMock
    from pipecat.frames.frames import TTSStartedFrame,TTSStoppedFrame,TTSAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection
    r=SessionRecorder(tmp_path);tap=r.tap('tts-written');tap.push_frame=AsyncMock()
    for cid in ('old','new'):
        await tap.process_frame(TTSStartedFrame(context_id=cid),FrameDirection.DOWNSTREAM)
        await tap.process_frame(TTSAudioRawFrame(bytes(1280),16000,1),FrameDirection.DOWNSTREAM)
        await tap.process_frame(TTSStoppedFrame(context_id=cid),FrameDirection.DOWNSTREAM)
    r.close()
    events=[json.loads(line) for line in (r.directory/'events.jsonl').read_text().splitlines()]
    assert [e['context_id'] for e in events if e['kind']=='tts-transport-write']==['old','new']


@pytest.mark.asyncio
async def test_nova_result_word_times_retained_without_mutating_frame(tmp_path):
    from pipecat.frames.frames import TranscriptionFrame, InterimTranscriptionFrame
    r=SessionRecorder(tmp_path);tap=r.tap('stt');tap.push_frame=AsyncMock()
    payload=dict(is_final=True,speech_final=False,channel=dict(alternatives=[dict(
        transcript='cooking.',words=[dict(word='cooking',start=1.2,end=1.5)])]))
    for frame_type in (TranscriptionFrame,InterimTranscriptionFrame):
        result=SimpleNamespace(model_dump=lambda **kw:payload)
        frame=frame_type('cooking.','user','time',result=result)
        await tap.process_frame(frame,FrameDirection.DOWNSTREAM)
        assert frame.result is result and frame.text=='cooking.'
    r.close()
    kept=[e for e in events(r) if e['kind']=='stt-frame']
    assert len(kept)==2 and all(e['vendor_result']==payload for e in kept)


@pytest.mark.asyncio
async def test_llm_output_start_text_end_retained_once(tmp_path):
    from pipecat.frames.frames import LLMFullResponseStartFrame,LLMTextFrame,LLMFullResponseEndFrame
    r=SessionRecorder(tmp_path)
    for frame in (LLMFullResponseStartFrame(),LLMTextFrame('Real reply.'),LLMFullResponseEndFrame()):
        data=SimpleNamespace(frame=frame,source=object(),direction=FrameDirection.DOWNSTREAM,timestamp=123)
        await r.observer.on_push_frame(data)
        await r.observer.on_push_frame(data)
    r.close()
    kept=[e for e in events(r) if e['kind']=='frame']
    assert [e['frame'] for e in kept]==['LLMFullResponseStartFrame','LLMTextFrame','LLMFullResponseEndFrame']
    assert kept[1]['text']=='Real reply.'
