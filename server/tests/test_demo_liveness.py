import asyncio
from unittest.mock import AsyncMock
import pytest
from demo_liveness import Liveness, cadence

@pytest.mark.parametrize('value', ['nan','inf','-1','.01','11'])
def test_bad_cadence(value):
    with pytest.raises(ValueError): cadence(value)

async def test_matched_pong_duplicates_and_disabled():
    now=[0.];events=[]
    l=Liveness(AsyncMock(),AsyncMock(),event=lambda *a,**k:events.append((a,k)),clock=lambda:now[0])
    await l.tick();assert l.seq==1
    assert not l.pong({'seq':True}) and not l.pong({'seq':2})
    assert l.pong({'seq':1}) and not l.pong({'seq':1})
    now[0]=1;await l.tick();assert l.misses==0
    off=Liveness(AsyncMock(),AsyncMock(),period=0);off.start();await off.tick()
    assert off.seq==0 and not off.tasks

async def test_two_misses_close_once_even_if_send_is_blocked():
    blocked=asyncio.Event();close=AsyncMock();events=[]
    async def send(data):await blocked.wait()
    l=Liveness(send,close,period=.1,event=lambda name,**kw:events.append((name,kw)))
    l.start()
    try:
        async with asyncio.timeout(3):
            while not l.alarms:await asyncio.sleep(.01)
        await l.tick()
        assert l.alarms==l.recoveries==1 and close.await_count==1
        assert any(n=='liveness-alarm' and d['failed'] for n,d in events)
        assert l.seq==2 # no unbounded work queued behind SCTP
    finally:await l.stop()

async def test_late_pong_does_not_hide_two_misses():
    now=[0.];l=Liveness(AsyncMock(),AsyncMock(),clock=lambda:now[0])
    await l.tick();now[0]=2.1
    assert l.pong({'seq':1}) and l.misses==1
    await l.tick();now[0]=4.2;await l.tick()
    assert l.alarms==1

async def test_installed_rtvi_ping_and_canonical_client_pong():
    from pipecat.processors.frameworks.rtvi.processor import RTVIProcessor
    from pipecat.processors.frameworks.rtvi.models import RawClientMessageData
    rtvi=RTVIProcessor();rtvi.push_transport_message=AsyncMock();rtvi.push_frame=AsyncMock()
    l=Liveness(rtvi.send_server_message,AsyncMock())
    @rtvi.event_handler('on_client_message')
    async def on_message(processor,message):
        if message.type=='pong':l.pong(message.data)
    l.start()
    try:
        async with asyncio.timeout(1):
            while not rtvi.push_transport_message.await_count:await asyncio.sleep(.001)
        wire=rtvi.push_transport_message.await_args.args[0].model_dump()
        assert wire['type']=='server-message' and wire['data']=={'t':'ping','seq':1}
        await rtvi._handle_client_message('test',RawClientMessageData(t='pong',d={'seq':1}))
        async with asyncio.timeout(1):
            while not l.pongs:await asyncio.sleep(.001)
        assert l.pongs==1 and not l.pending
    finally:await l.stop()

async def test_half_second_cadence_accepts_pong_at_period_plus_point_one():
    now=[0.];l=Liveness(AsyncMock(),AsyncMock(),period=.5,clock=lambda:now[0])
    await l.tick();l.queue.get_nowait()
    now[0]=.5;await l.tick();l.queue.get_nowait()
    now[0]=.6;assert l.pong({'seq':1}) and l.misses==0
    now[0]=2.5;await l.tick();assert not l.alarms
    now[0]=2.6;await l.tick();assert l.alarms==1

async def test_two_lost_half_second_datagrams_allow_recovery_before_two_seconds():
    now=[0.];close=AsyncMock();l=Liveness(AsyncMock(),close,period=.5,clock=lambda:now[0])
    for t in (0,.5,1):
        now[0]=t;await l.tick();l.queue.get_nowait()
    now[0]=1.1;assert l.pong({'seq':3})
    for t in (1.5,2,2.5,3):
        now[0]=t;await l.tick();l.queue.get_nowait()
    assert not l.alarms
    now[0]=3.1;await l.tick();assert l.alarms==1 and close.await_count==1

async def _alarm_line(**kw):
    from loguru import logger
    lines=[];sink=logger.add(lambda m:lines.append(m.record['message']),level='ERROR')
    try:
        now=[0.];l=Liveness(AsyncMock(),AsyncMock(),clock=lambda:now[0],**kw)
        await l.tick();now[0]=2.5;await l.tick()
        assert l.alarms==1 and l.close.await_count==1
    finally:logger.remove(sink)
    return [x for x in lines if 'LIVENESS alarm' in x]

async def test_alarm_line_carries_the_peer_label():
    # decision 113: identity bound at emission; the scorer attributes by this pc_id
    pc='SmallWebRTCConnection#0-0fac7ccbe12140bca907af26dc18547d'
    assert await _alarm_line(label=pc)==['LIVENESS alarm: no timely pong for 2.0 s; closing peer pc_id='+pc+' silent_s=2.500']

async def test_alarm_line_without_label_is_unchanged():
    assert await _alarm_line()==['LIVENESS alarm: no timely pong for 2.0 s; closing peer']

def test_demo_bot_passes_the_connection_pc_id_as_label():
    import pathlib,re
    src=pathlib.Path(__file__).resolve().parents[1].joinpath('demo_bot.py').read_text()
    call=src[src.index('liveness = Liveness('):src.index('@worker.rtvi.event_handler("on_client_ready")')]
    assert re.search(r'label=runner_args\.webrtc_connection\.pc_id\)',call)

async def test_labelled_alarm_line_carries_the_measured_silence_not_the_threshold():
    # decision 115: an overdue watchdog callback reports the actual silence (here 4.5 s), as the event does
    from loguru import logger
    lines=[];events=[];sink=logger.add(lambda m:lines.append(m.record['message']),level='ERROR')
    try:
        now=[0.];l=Liveness(AsyncMock(),AsyncMock(),clock=lambda:now[0],label='pc',
                            event=lambda name,**kw:events.append((name,kw)))
        await l.tick();now[0]=0.25;assert l.pong({'seq':1})
        now[0]=4.75;await l.tick()
    finally:logger.remove(sink)
    alarm=[x for x in lines if 'LIVENESS alarm' in x]
    assert alarm==['LIVENESS alarm: no timely pong for 2.0 s; closing peer pc_id=pc silent_s=4.500']
    assert [kw['silent_s'] for n,kw in events if n=='liveness-alarm']==[4.5]
