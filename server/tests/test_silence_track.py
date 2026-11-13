import asyncio
import time
import pytest
from silence_track import PacedIdleAudioTrack

@pytest.mark.asyncio
async def test_idle_sends_nothing_and_resume_is_paced():
    track=PacedIdleAudioTrack(16000,auto_silence=False)
    pending=asyncio.create_task(track.recv())
    await asyncio.sleep(.12)
    assert not pending.done()
    completion=track.add_audio_bytes(bytes(160*2*4))
    first=await asyncio.wait_for(pending,1)
    times=[time.monotonic()];stamps=[first.pts]
    for _ in range(3):
        frame=await asyncio.wait_for(track.recv(),1)
        times.append(time.monotonic());stamps.append(frame.pts)
    await completion
    assert stamps==[0,160,320,480]
    assert times[-1]-times[0]>=.025
    # A second gap also preserves RTP sample time and normal packet pacing.
    await asyncio.sleep(.12)
    completion=track.add_audio_bytes(bytes(160*2*2))
    a=await track.recv();start=time.monotonic();b=await track.recv()
    assert time.monotonic()-start>=.007
    assert (a.pts,b.pts)==(640,800)
    await completion

@pytest.mark.asyncio
async def test_continuous_silence_uses_native_behavior():
    track=PacedIdleAudioTrack(16000,auto_silence=True)
    a=await track.recv();b=await track.recv()
    assert (a.pts,b.pts)==(0,160)
    assert not any(bytes(a.planes[0]))

@pytest.mark.asyncio
async def test_bounded_tail_flushes_then_stops_and_resumes_without_burst():
    track=PacedIdleAudioTrack(16000,auto_silence=False)
    completion=track.add_audio_bytes(bytes(160*2*2))
    await track.recv();await track.recv();await completion
    began=time.monotonic()
    tail=[await track.recv() for _ in range(16)]
    assert [f.pts for f in tail]==list(range(320,2880,160))
    assert time.monotonic()-began>=.14
    assert not any(any(bytes(f.planes[0])) for f in tail)
    waiting=asyncio.create_task(track.recv())
    await asyncio.sleep(.12)
    assert not waiting.done()
    completion=track.add_audio_bytes(bytes(160*2*2))
    first=await asyncio.wait_for(waiting,1);started=time.monotonic();second=await track.recv()
    assert (first.pts,second.pts)==(2880,3040)
    assert time.monotonic()-started>=.007
    await completion

@pytest.mark.asyncio
async def test_idle_wait_is_event_driven_and_cancellation_preserves_next_handoff(monkeypatch):
    track=PacedIdleAudioTrack(16000,auto_silence=False)
    sleeps=[];real_sleep=asyncio.sleep
    async def sleep(delay):
        sleeps.append(delay)
        await real_sleep(delay)
    monkeypatch.setattr('silence_track.asyncio.sleep',sleep)
    waiting=asyncio.create_task(track.recv())
    await real_sleep(.025)
    assert not sleeps, 'Idle recv polled rather than waiting for producer notification'
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):await waiting
    completion=track.add_audio_bytes(bytes(320))
    frame=await asyncio.wait_for(track.recv(),1)
    assert frame.pts==0
    await completion
