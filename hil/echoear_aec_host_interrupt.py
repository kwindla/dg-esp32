"""Declared host interruption cell, using the same non-silent-write timer.

No acoustic near-end stimulus or transcript is injected. The next reply is
scripted explicitly to test playback recovery, not application recognition.
"""
import time
from echoear_aec_accept import control


def cell(identifier, text, next_text, *, offset_s=1.0, volume_db=-6):
    if type(offset_s) not in (int,float) or not 0 <= offset_s <= 3 or volume_db not in (0,-6) or not isinstance(text,str) or not text.strip() or not isinstance(next_text,str) or not next_text.strip():
        raise ValueError('Invalid host interruption cell')
    return dict(id=identifier,kind='host-interrupt',text=text,next_text=next_text,
        host_interrupt_offset_s=offset_s,next_delay_s=1.0,volume_db=volume_db,
        guard_ms=400,stop_offset_s=None,expected_class='firmware-direct',real_bot_reply=False)


def issue(path, number, case_id):
    # Called in the timer worker with an exclusively reserved command number.
    # No event parsing or acknowledgement wait is on the invocation path.
    sent=time.monotonic()
    control(path,dict(number=number,op='interrupt',case_id=case_id,host_issued_s=sent))
    return dict(request_monotonic_s=sent,write_complete_s=time.monotonic(),number=number)


def validate(c):
    if (c.get('fixture') or c.get('stop_offset_s') is not None or c.get('real_bot_reply') or
        c.get('next_delay_s') != 1.0 or not isinstance(c.get('next_text'),str) or not c['next_text'].strip() or
        type(c.get('host_interrupt_offset_s')) not in (int,float) or
        not 0<=c['host_interrupt_offset_s']<=3):
        raise ValueError('Host interruption needs a declared timer and scripted next reply, no near stimulus')
