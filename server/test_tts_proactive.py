import asyncio
from unittest.mock import AsyncMock,patch
import pytest
import tts_connection_probe as probe
from pipecat.frames.frames import TTSStoppedFrame,EndFrame,CancelFrame,InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection


def service():
    events=[]
    p=probe.ProactiveConnectionTTS(api_key='test-key',probe_event=lambda name,**kw:events.append((name,kw)))
    p._disconnect=AsyncMock();p._connect=AsyncMock();p._transport_is_active=lambda:True
    return p,events


async def completed(p,cid):
    with patch.object(probe.DeepgramFluxTTSService,'push_frame',AsyncMock()):
        await p.push_frame(TTSStoppedFrame(context_id=cid))
    await p.on_audio_context_completed(cid)


@pytest.mark.asyncio
async def test_stop_then_context_cleanup_starts_one_task_and_fast_turn_waits():
    p,events=service();entered=asyncio.Event();release=asyncio.Event()
    async def connect():entered.set();await release.wait()
    p._connect=connect
    with patch.object(probe.DeepgramFluxTTSService,'push_frame',AsyncMock()):
        await p.push_frame(TTSStoppedFrame(context_id='one'))
    assert p._proactive_task is None
    await p.on_audio_context_completed('one');await entered.wait()
    original=p._proactive_task
    p._schedule_reconnect('one');p._schedule_reconnect('duplicate')
    assert p._proactive_task is original
    with patch.object(probe.DeepgramFluxTTSService,'on_turn_context_created',AsyncMock()) as turn:
        follow=asyncio.create_task(p.on_turn_context_created('two'))
        await asyncio.sleep(0);turn.assert_not_awaited()
        release.set();await follow;turn.assert_awaited_once_with('two')
    assert not original.cancelled();p._disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_waiter_cancellation_never_cancels_reconnect():
    p,_=service();release=asyncio.Event()
    p._connect=release.wait
    await completed(p,'one');await asyncio.sleep(0)
    waiter=asyncio.create_task(p._wait_reconnect());await asyncio.sleep(0);waiter.cancel()
    with pytest.raises(asyncio.CancelledError):await waiter
    assert not p._proactive_task.cancelled()
    release.set();await p._proactive_task


@pytest.mark.asyncio
async def test_failure_falls_through_and_no_reconnect_on_interruption_or_shutdown():
    p,events=service();p._connect=AsyncMock(side_effect=OSError('test'))
    await completed(p,'one');await p._wait_reconnect()
    assert any(e[0]=='tts-reconnect-failed' for e in events)
    with patch.object(probe.DeepgramFluxTTSService,'on_turn_context_created',AsyncMock()) as turn:
        await p.on_turn_context_created('two');turn.assert_awaited_once()
    for frame in (InterruptionFrame(),EndFrame(),CancelFrame()):
        q,_=service()
        with patch.object(probe.DeepgramFluxTTSService,'process_frame',AsyncMock()):
            await q.process_frame(frame,FrameDirection.DOWNSTREAM)
        assert q._proactive_task is None
        if not isinstance(frame,InterruptionFrame):
            await completed(q,'after-shutdown');assert q._proactive_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize('ack_first',[False,True])
async def test_interruption_reconnects_after_local_cleanup_without_ack_gate(ack_first):
    p,events=service();p._generation_active=True;p._probe_context_id='cancelled'
    async def cleanup(self,frame,direction):
        assert p._proactive_task is None
        if ack_first:
            await p._handle_message(dict(type='SpeechInterrupted'))
            assert p._proactive_task is None
    with patch.object(probe.DeepgramFluxTTSService,'process_frame',cleanup),patch.object(probe.DeepgramFluxTTSService,'_handle_message',AsyncMock()):
        await p.process_frame(InterruptionFrame(),FrameDirection.DOWNSTREAM)
        assert p._proactive_task is not None and not p._generation_active
        await p._wait_reconnect()
        if not ack_first:
            await p._handle_message(dict(type='SpeechInterrupted'))
    names=[e[0] for e in events]
    assert names.index('tts-reconnect-start')>names.index('tts-interruption-local-cleanup')
    if not ack_first:
        assert names.index('tts-reconnect-start')<names.index('tts-interruption-provider-stop')
    p._disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_ack_new_generation_at_50ms_starts_within_10ms():
    p,events=service();p._generation_active=True;p._probe_context_id='old'
    with patch.object(probe.DeepgramFluxTTSService,'process_frame',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'on_turn_context_created',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'start_ttfb_metrics',AsyncMock()):
        await p.process_frame(InterruptionFrame(),FrameDirection.DOWNSTREAM)
        await asyncio.sleep(.05)
        async def new_generation():
            await p.on_turn_context_created('new')
            await p.start_ttfb_metrics()
        await asyncio.wait_for(new_generation(),timeout=.010)
    assert p._generation_active
    assert p._cancel_context_id=='old'  # No provider acknowledgment ever arrived.
    assert [e[1]['context_id'] for e in events if e[0]=='tts-reconnect-scheduled']==['old']
    assert not any(e[0]=='tts-interruption-provider-stop' for e in events)


@pytest.mark.asyncio
async def test_pending_cleanup_never_blocks_new_context_or_reconnects_it():
    p,events=service();p._generation_active=True;p._probe_context_id='old'
    release=asyncio.Event()
    async def cleanup(self,frame,direction):await release.wait()
    with patch.object(probe.DeepgramFluxTTSService,'process_frame',cleanup),patch.object(probe.DeepgramFluxTTSService,'on_turn_context_created',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'start_ttfb_metrics',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'_handle_message',AsyncMock()):
        old=asyncio.create_task(p.process_frame(InterruptionFrame(),FrameDirection.DOWNSTREAM))
        await asyncio.sleep(.05)
        await asyncio.wait_for(p.on_turn_context_created('new'),timeout=.010)
        await p.start_ttfb_metrics()
        release.set();await old
        await p._handle_message(dict(type='SpeechInterrupted'))
    assert p._generation_active and p._proactive_task is None
    assert not any(e[0]=='tts-reconnect-scheduled' for e in events)
    assert next(e for e in events if e[0]=='tts-interruption-provider-stop')[1]['context_id']=='old'


@pytest.mark.asyncio
async def test_late_ack_500ms_does_not_delay_300ms_generation_beyond_reconnect():
    p,events=service();p._generation_active=True;p._probe_context_id='old'
    loop=asyncio.get_running_loop();started=loop.time();connect_at=[]
    async def connect():
        connect_at.append(loop.time()-started)
        await asyncio.sleep(.36)
    p._connect=connect
    with patch.object(probe.DeepgramFluxTTSService,'process_frame',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'_handle_message',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'on_turn_context_created',AsyncMock()),patch.object(probe.DeepgramFluxTTSService,'start_ttfb_metrics',AsyncMock()):
        await p.process_frame(InterruptionFrame(),FrameDirection.DOWNSTREAM)
        async def ack():
            await asyncio.sleep(.5)
            await p._handle_message(dict(type='SpeechInterrupted'))
        acknowledgment=asyncio.create_task(ack())
        await asyncio.sleep(.3)
        requested=loop.time()
        await p.on_turn_context_created('new');await p.start_ttfb_metrics()
        waited=loop.time()-requested
        assert connect_at[0]<.01
        assert .03<waited<.15  # Only the ~60ms remaining connect, not the 200ms ack.
        assert not acknowledgment.done()
        await acknowledgment
    assert p._generation_active  # Late ack cannot clear the new generation.
    assert [e[1]['context_id'] for e in events if e[0]=='tts-reconnect-scheduled']==['old']
    p._disconnect.assert_awaited_once()

@pytest.mark.asyncio
async def test_context_request_precedes_inflight_wait_and_metrics_keep_context():
    from pipecat.frames.frames import MetricsFrame
    from pipecat.metrics.metrics import TTFAMetricsData
    p,events=service();release=asyncio.Event();p._connect=release.wait
    await completed(p,'old');await asyncio.sleep(0)
    with patch.object(probe.DeepgramFluxTTSService,'on_turn_context_created',AsyncMock()):
        task=asyncio.create_task(p.on_turn_context_created('new'));await asyncio.sleep(0)
        names=[n for n,k in events]
        assert names.index('tts-context-request')<names.index('tts-reconnect-wait-start')
        release.set();await task
    with patch.object(probe.DeepgramFluxTTSService,'push_frame',AsyncMock()):
        await p.push_frame(MetricsFrame([TTFAMetricsData(processor='test',ttfa=.2,ttfb=.1,leading_silence=.1)]))
    metric=next(k for n,k in events if n=='tts-context-metric')
    assert metric['context_id']=='new' and metric['data']['leading_silence']==.1
