import asyncio
from unittest.mock import AsyncMock,patch
import pytest
import tts_connection_probe as probe
from tts_connection_probe import ConnectionProbeTTS,EXPECTED_HASHES,supported
from pipecat.frames.frames import TTSStoppedFrame


def service():
    p=ConnectionProbeTTS(api_key='test-key')
    p._disconnect=AsyncMock();p._connect=AsyncMock();p._transport_is_active=lambda:True
    return p


@pytest.mark.parametrize('change',['version','hash','missing','noncallable'])
@pytest.mark.asyncio
async def test_guard_fails_closed_once_without_private_calls(change):
    p=service();events=[]
    with patch.object(probe,'SOURCE_HASHES',dict(EXPECTED_HASHES)),patch.object(probe.pipecat,'__version__','1.16.0'):
        if change=='version':probe.pipecat.__version__='1.16.1'
        if change=='hash':probe.SOURCE_HASHES['tts']='changed'
        if change=='missing':
            # A complete interface is required even when two methods exist.
            assert not supported(object(),'1.16.0',EXPECTED_HASHES)
            p._transport_is_active=None
        if change=='noncallable':p._transport_is_active=1
        for _ in range(2):assert not await p.prepare_generation(lambda name,**kw:events.append(name))
        assert events==['tts-reconnect-refused'] * 2
        p._disconnect.assert_not_awaited();p._connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_and_shutdown_refuse_then_completed_context_allows_cycle():
    p=service();events=[];event=lambda name,**kw:events.append(name)
    p._generation_active=True
    assert not await p.prepare_generation(event)
    with patch.object(probe.DeepgramFluxTTSService,'push_frame',AsyncMock()):
        await p.push_frame(TTSStoppedFrame(context_id='one'))
    assert await p.prepare_generation(event)
    assert events[-2:]==['tts-reconnect-start','tts-reconnect-ready']
    p._probe_closing=True
    assert not await p.prepare_generation(event)
    assert p._disconnect.await_count==1


@pytest.mark.asyncio
async def test_metrics_wait_for_reconnect_and_failure_is_not_ready():
    p=service();entered=asyncio.Event();release=asyncio.Event();events=[]
    async def disconnect():entered.set();await release.wait()
    p._disconnect=disconnect
    reconnect=asyncio.create_task(p.prepare_generation(lambda name,**kw:events.append(name)))
    await entered.wait()
    with patch.object(probe.DeepgramFluxTTSService,'start_ttfb_metrics',AsyncMock()) as start:
        metric=asyncio.create_task(p.start_ttfb_metrics());await asyncio.sleep(0)
        start.assert_not_awaited();release.set();await reconnect;await metric
        start.assert_awaited_once()
    p._generation_active=False;p._transport_is_active=lambda:False
    with pytest.raises(RuntimeError):await p.prepare_generation(lambda name,**kw:events.append(name))
    assert events[-1]=='tts-reconnect-start'
