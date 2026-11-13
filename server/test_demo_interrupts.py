"""Use the installed public RTVI serializer; no transport or service required."""
import asyncio
import time
from unittest.mock import AsyncMock
from demo_interrupts import RTVIProcessor
from pipecat.processors.frameworks.rtvi.models import BotInterruptedMessage, BotStartedSpeakingMessage

async def test_two_identical_notices_and_normal_other_message():
    events=[]
    rtvi=RTVIProcessor(event=lambda event,**kw:events.append((event,kw,time.monotonic())))
    rtvi.push_frame=AsyncMock()
    await rtvi.push_transport_message(BotInterruptedMessage())
    assert len(rtvi.push_frame.await_args_list)==1
    await rtvi.push_transport_message(BotStartedSpeakingMessage())
    await asyncio.sleep(.13)
    frames=[c.args[0].message for c in rtvi.push_frame.await_args_list]
    assert [f['type'] for f in frames]==['bot-interrupted','bot-started-speaking','bot-interrupted']
    assert frames[0]==frames[2]
    assert frames[0]['data']=={'interrupt_seq':1}
    assert .09<=events[1][2]-events[0][2]<.3
    await rtvi.push_transport_message(BotInterruptedMessage())
    assert rtvi.push_frame.await_args_list[-1].args[0].message['data']['interrupt_seq']==2
    await rtvi.stop_interrupt_repeats()
    n=rtvi.push_frame.await_count
    await asyncio.sleep(.12)
    assert rtvi.push_frame.await_count==n
    assert not rtvi._interrupt_repeats

async def test_ninth_repeat_drops_only_insurance_copy_without_raising():
    events=[];rtvi=RTVIProcessor(event=lambda event,**kw:events.append((event,kw)))
    rtvi.push_frame=AsyncMock()
    for _ in range(9):await rtvi.push_transport_message(BotInterruptedMessage())
    assert rtvi.push_frame.await_count==9 and len(rtvi._interrupt_repeats)==8
    assert events[-1]==('interrupt-repeat-dropped',{'seq':9,'capacity':8})
    await rtvi.stop_interrupt_repeats()
