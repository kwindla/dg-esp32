import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from echoear_aec_stimulus import schedule_from_tts


def test_timer_uses_retained_transport_write_while_serial_owner_is_busy(tmp_path):
    path=tmp_path/'events.jsonl';start=time.monotonic_ns()
    path.write_text(json.dumps(dict(kind='tts-transport-write',non_silent=False,monotonic_ns=start))+'\n')
    called=[]
    def play():
        called.append(time.monotonic())
        return dict(request_monotonic_s=called[-1],duration_valid=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(schedule_from_tts,lambda:[path],start,.06,play)
        anchor=time.monotonic_ns()
        with path.open('a') as stream:
            stream.write(json.dumps(dict(kind='tts-transport-write',non_silent=True,monotonic_ns=anchor))+'\n')
            stream.write('{"partial":')
        time.sleep(.2)  # serial polling can be blocked; timer is independent
        result=future.result()
    assert result['anchor_s']==anchor/1e9
    assert len(called)==1
    assert .06<=result['actual_invocation_offset_s']<.18


def test_missing_anchor_and_cancel_never_play(tmp_path):
    path=tmp_path/'events.jsonl';path.write_text('')
    called=[]
    with pytest.raises(RuntimeError,match='No non-silent'):
        schedule_from_tts(lambda:[path],0,.3,lambda:called.append(1),timeout_s=.01)
    cancel=threading.Event();cancel.set()
    with pytest.raises(RuntimeError,match='No non-silent'):
        schedule_from_tts(lambda:[path],0,.3,lambda:called.append(1),cancel=cancel)
    assert not called


def test_fixed_guard_never_requests_capture_discontinuity():
    from echoear_aec_stimulus import set_guard_if_changed
    commands=[]
    def command(board,text):commands.append(text)
    current=400
    for _ in range(30):current=set_guard_if_changed(None,command,current,400)
    assert commands==[]
    current=set_guard_if_changed(None,command,current,300)
    assert commands==['audio 0','aec-guard-ms 300','audio 1']
    commands.clear()
    set_guard_if_changed(None,command,current,300)
    assert not commands


def test_active_deadline_does_not_slide_or_play_during_witnessed_pause(tmp_path):
    path=tmp_path/'events.jsonl';now=time.monotonic_ns()
    path.write_text('\n'.join(json.dumps(row) for row in [
        dict(kind='tts-transport-write',non_silent=True,monotonic_ns=now-100_000_000),
        dict(kind='tts-transport-write',non_silent=False,monotonic_ns=now-1_000_000)])+'\n')
    played=[]
    result=schedule_from_tts(lambda:[path],0,0,lambda:played.append(True),require_active=True)
    assert result['skipped'] and not played
    assert result['deadline_s']==(now-100_000_000)/1e9
    assert not result['active_tts_witness']['active']


def test_active_deadline_keeps_independent_play_witness(tmp_path):
    path=tmp_path/'events.jsonl';now=time.monotonic_ns()
    path.write_text(json.dumps(dict(kind='tts-transport-write',non_silent=True,monotonic_ns=now))+'\n')
    result=schedule_from_tts(lambda:[path],0,0,lambda:dict(request_monotonic_s=time.monotonic()),require_active=True)
    assert not result['skipped']
    assert result['active_tts_witness']['active']
    assert result['jabra']['request_monotonic_s']>=now/1e9
