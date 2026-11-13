"""No hardware: growing logs, real clocks, and retained timing failure evidence."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from echoear_aec_event_tail import JsonlTail, EventCache, ContextIdentity, EventStreamChanged
from echoear_aec_stimulus import TransportEvents, schedule_from_tts, schedule_active_window


def append(path, row):
    with path.open('a') as stream: stream.write(json.dumps(row)+'\n')


def write(t, active=True, pc='p', context='c'):
    return dict(kind='tts-transport-write', monotonic_ns=int(t*1e9), non_silent=active,
                pc_id=pc, context_id=context)


def test_partial_line_once_and_eof_priming(tmp_path):
    path=tmp_path/'events';path.write_bytes(b'{"old":1}\n{"oldpartial":')
    tail=JsonlTail([path],at_end=True)
    with path.open('ab') as f:f.write(b'2}\n{"new":')
    assert tail.poll()==[]
    with path.open('ab') as f:f.write(b'3}\n')
    assert tail.poll()==[dict(new=3)]
    assert tail.poll()==[] and tail.last_poll['bytes_consumed']==0


@pytest.mark.parametrize('fault',['rotation','truncation','rewrite','disappearance'])
def test_changed_stream_fails_closed(tmp_path,fault):
    p=tmp_path/'events';p.write_text('{"old":1}\n');tail=JsonlTail(lambda:list(tmp_path.glob('events')),at_end=True)
    if fault=='rotation':
        p.rename(tmp_path/'old');p.write_text('{"new":1}\n')
    elif fault=='truncation':p.write_text('')
    elif fault=='rewrite':p.write_text('{"new":1}\n'+(' '*100))
    else:p.unlink()
    with pytest.raises(EventStreamChanged):tail.poll()


def test_identity_cached_but_conflicting_future_start_rejected(tmp_path):
    p=tmp_path/'events';p.touch();identity=ContextIdentity([p],'case')
    append(p,dict(case_id='other',event='tts-generation-metric-start',pc_id='q',context_id='z'))
    assert identity() is None
    append(p,dict(case_id='case',event='tts-generation-metric-start',pc_id='p',context_id='c'))
    assert identity()==dict(pc_id='p',context_id='c')
    assert identity()==dict(pc_id='p',context_id='c') and identity.tail.last_poll['bytes_consumed']==0
    append(p,dict(case_id='case',event='tts-generation-metric-start',pc_id='q',context_id='d'))
    with pytest.raises(EventStreamChanged,match='ambiguous'):identity()


def test_bounded_backlog_is_not_fresh_evidence(tmp_path):
    p=tmp_path/'events';p.touch();reader=TransportEvents([p],0)
    reader.tail.max_bytes=200
    for _ in range(20):append(p,write(time.monotonic()))
    from echoear_aec_stimulus import active_transport_witness
    assert not active_transport_witness([p],0,reader=reader)['active']
    assert reader.tail.last_poll['bytes_consumed']==200


@pytest.mark.parametrize('history_mb',[0,90])
def test_large_history_concurrent_writes_and_capture_polling_under_20ms(tmp_path,history_mb):
    session=tmp_path/'session';evaluation=tmp_path/'evaluation'
    # Real approximately 90 MB JSONL history, not a sparse file or one huge line.
    old=(json.dumps(dict(kind='old',padding='x'*900))+'\n').encode()
    with session.open('wb') as f:
        for _ in range(history_mb*1_000_000//len(old)):f.write(old)
    evaluation.touch()
    capture=EventCache([evaluation])
    start=time.monotonic_ns()
    reader=TransportEvents([session],start,at_end=True)
    identity=ContextIdentity([evaluation],'case')
    called=[];stop=threading.Event()
    def feed():
        append(evaluation,dict(case_id='case',event='tts-generation-metric-start',pc_id='p',context_id='c'))
        anchor=time.monotonic()
        for n in range(34):
            if stop.wait(max(0,anchor+n*.04-time.monotonic())):break
            append(session,write(time.monotonic()))
            # Evaluation grows concurrently and is consumed by the capture owner.
            for _ in range(6):append(evaluation,dict(case_id='other',event='frame',t=time.monotonic()))
    def play():
        called.append(time.monotonic());return dict(request_monotonic_s=called[-1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        timer=pool.submit(schedule_active_window,lambda:[session],start,play,identity,reader=reader)
        feeder=pool.submit(feed)
        while not timer.done():
            capture.read('case');time.sleep(.01)
        result=timer.result();stop.set();feeder.result()
    assert not result['skipped'] and result['timing_passed'] and len(called)==1
    assert 0<=result['actual_minus_requested_ms']<20
    assert max(p['wall_s'] for p in result['polls'])<.02
    assert sum(p['bytes_consumed'] for p in result['polls'])<50_000
    print('REAL_TIMER',history_mb,'MB',json.dumps(dict(error_ms=result['actual_minus_requested_ms'],
          max_poll_ms=max(p['wall_s'] for p in result['polls'])*1000,
          consumed=sum(p['bytes_consumed'] for p in result['polls']))))


def test_delayed_read_cannot_disconnect_after_window(tmp_path):
    p=tmp_path/'events';p.touch();now=time.monotonic();start=int((now-1.4)*1e9)
    append(p,write(start/1e9));append(p,write(now))
    reader=TransportEvents([p],start);poll=reader.tail.poll
    def slow():time.sleep(.15);return poll()
    reader.tail.poll=slow;calls=[]
    result=schedule_active_window(lambda:[p],start,lambda:calls.append(1),lambda:dict(pc_id='p',context_id='c'),reader=reader)
    assert result['skipped'] and not calls
    assert result['timer_fired_s']>result['deadline_s']


def test_ordinary_worker_delayed_read_is_harness_failure_without_play(tmp_path):
    p=tmp_path/'events';now=time.monotonic();append(p,write(now));reader=TransportEvents([p],0)
    poll=reader.tail.poll
    def slow():time.sleep(.12);return poll()
    reader.tail.poll=slow;calls=[]
    r=schedule_from_tts(lambda:[p],0,0,lambda:calls.append(1),reader=reader)
    assert r['skipped'] and r['harness_failure']=='stimulus-timing' and not calls


def test_actual_alsa_timestamp_gate_and_original_witness_retained(tmp_path):
    p=tmp_path/'events';append(p,write(time.monotonic()))
    def play():
        time.sleep(.11)
        return dict(request_monotonic_s=time.monotonic(),duration_valid=True)
    r=schedule_from_tts(lambda:[p],0,0,play)
    assert not r['skipped'] and r['jabra']['duration_valid']
    assert r['actual_minus_requested_ms']>100 and r['harness_failure']=='stimulus-timing'
    from echoear_aec_campaign import HarnessFailure, exception_class
    assert exception_class(HarnessFailure('stimulus-timing','late'))=='harness'


def test_cancel_during_read_never_invokes(tmp_path):
    p=tmp_path/'events';append(p,write(time.monotonic()))
    reader=TransportEvents([p],0);cancel=threading.Event();poll=reader.tail.poll
    def read():cancel.set();return poll()
    reader.tail.poll=read;calls=[]
    r=schedule_from_tts(lambda:[p],0,0,lambda:calls.append(1),reader=reader,cancel=cancel)
    assert r['skipped'] and not calls


@pytest.mark.parametrize('payload',[False,True])
def test_wifi_ack_only_warns_without_failing_boundary(tmp_path,monkeypatch,capsys,payload):
    from types import SimpleNamespace
    import echoear_aec_campaign as c
    text='OK #1 wifi-stats' + ('\nI (1) wifi: BUFFER:' if payload else '')
    board=SimpleNamespace(cursor=0,lines_since=lambda cursor:[(1,0,text)])
    monkeypatch.setattr(c,'command',lambda *a:None)
    monkeypatch.setattr(c,'admission_boundary',lambda *a:('endpoint',[]))
    assert c.boundary(board,tmp_path,'before')==('endpoint',[])
    r=json.loads((tmp_path/'before-wifi-stats.json').read_text())
    assert r['completed'] and r['payload_present']==payload
    assert bool(r['warning']) is not payload
    assert ('WARNING' in capsys.readouterr().out) is not payload


def test_append_during_read_is_not_history_backlog(tmp_path,monkeypatch):
    """A growing file is normal; a post-read fstat must not turn it into a fault."""
    p=tmp_path/'events';p.touch();cache=EventCache([p],at_end=True)
    append(p,dict(case_id='case',event='first'))
    original=p.__class__.open
    class Reader:
        def __init__(self,f):self.f=f
        def __enter__(self):return self
        def __exit__(self,*a):self.f.close()
        def __getattr__(self,key):return getattr(self.f,key)
        def read(self,n):
            data=self.f.read(n)
            if n==cache.tail.max_bytes:
                with original(p,'a') as writer:writer.write('{"case_id":"case","event":"second"}\n')
            return data
    def opening(path,*a,**kw):
        stream=original(path,*a,**kw)
        return Reader(stream) if path==p and a and a[0]=='rb' else stream
    monkeypatch.setattr(p.__class__,'open',opening)
    assert [e['event'] for e in cache.read('case')]==['first']
    assert not cache.tail.backlog


@pytest.mark.parametrize('history_mb',[0,90])
def test_ordinary_timer_history_cost_and_lateness_under_20ms(tmp_path,history_mb):
    p=tmp_path/'session'
    line=(json.dumps(dict(kind='old',padding='x'*900))+'\n').encode()
    with p.open('wb') as f:
        for _ in range(history_mb*1_000_000//len(line)):f.write(line)
    start=time.monotonic_ns();reader=TransportEvents([p],start,at_end=True)
    append(p,write(time.monotonic()))
    r=schedule_from_tts(lambda:[p],start,.12,lambda:dict(request_monotonic_s=time.monotonic()),reader=reader)
    assert r['timing_passed'] and 0<=r['actual_minus_requested_ms']<20
    assert max(p['wall_s'] for p in r['polls'])<.02
    assert sum(p['bytes_consumed'] for p in r['polls'])<2000
    print('REAL_ORDINARY_TIMER',history_mb,'MB',json.dumps(dict(error_ms=r['actual_minus_requested_ms'],
          max_poll_ms=max(p['wall_s'] for p in r['polls'])*1000)))


def test_capture_index_after_long_idle_catches_up_outside_critical_path(tmp_path):
    p=tmp_path/'evaluation';p.touch();cache=EventCache([p])
    row=json.dumps(dict(case_id='old',event='frame',padding='x'*900))+'\n'
    with p.open('a') as f:
        for _ in range(5500):f.write(row)
    assert len(cache.read())==5500  # Idle link-probe audit, no timed stimulus.
    append(p,dict(case_id='new',event='frame'))
    began=time.monotonic()
    assert cache.read('new')==[dict(case_id='new',event='frame')]
    assert time.monotonic()-began<.02
    assert cache.tail.last_poll['bytes_consumed']<100


def test_case_less_snapshot_is_separate_from_full_history_and_does_not_poll(tmp_path,monkeypatch):
    p=tmp_path/'events';cache=EventCache([p])
    own=dict(case_id='case',event='frame',frame='BotStartedSpeakingFrame')
    alarm=dict(case_id=None,event='liveness-alarm')
    missing_key=dict(event='frame',frame='ErrorFrame')
    for row in (own,alarm,missing_key):append(p,row)
    assert cache.read('case')==[own]
    monkeypatch.setattr(cache.tail,'poll',lambda:pytest.fail('Snapshot must not poll again'))
    assert cache.case_rows(None)==[alarm,missing_key]
    assert cache.case_rows('case')==[own]
    result=cache.case_rows(None);result.clear()
    assert cache.case_rows(None)==[alarm,missing_key]


def test_recorded_growing_history_only_decodes_appended_rows(tmp_path,monkeypatch):
    """Measure the production recorded() path after 0, 5 and 90 MB cumulative history."""
    from echoear_aec_campaign import BenchHold
    hold=BenchHold({}, {},tmp_path)
    p=tmp_path/'sessions'/'one'/'events.jsonl';p.parent.mkdir(parents=True);p.touch()
    original=json.loads;decoded=[]
    def loads(value,*args,**kwargs):
        decoded.append(1);return original(value,*args,**kwargs)
    monkeypatch.setattr(json,'loads',loads)
    line=(json.dumps(dict(kind='rtvi-input',padding='x'*900))+'\n').encode()
    expected=0
    for history_mb in (0,5,90):
        count=max(0,(history_mb*1_000_000-p.stat().st_size)//len(line))
        with p.open('ab') as stream:
            for _ in range(count):stream.write(line)
        expected+=count
        assert len(hold.recorded())==expected  # Each new batch is ingested once.
        decoded.clear()
        fresh=dict(kind='rtvi-output',monotonic_ns=history_mb,source='RTVIProcessor',direction='DOWNSTREAM')
        append(p,fresh);expected+=1
        started=time.monotonic();result=hold.recorded();elapsed=time.monotonic()-started
        assert len(result)==expected and result[-1]==fresh and len(decoded)==1
        assert hold.recorded_cache.tail.last_poll['rows_consumed']==1
        assert hold.recorded_cache.tail.last_poll['bytes_consumed']<200
        assert elapsed<.02
        decoded.clear();assert len(hold.recorded())==expected
        assert not decoded and hold.recorded_cache.tail.last_poll['bytes_consumed']==0
        print('RECORDED_HISTORY',json.dumps(dict(history_mb=history_mb,rows=expected,
              appended_read_ms=elapsed*1000,decoded_rows=1)))


def test_recorded_retains_partial_lines_and_discovers_new_sessions(tmp_path):
    from echoear_aec_campaign import BenchHold
    from echoear_aec_checks import outgoing_rtvi_count
    hold=BenchHold({}, {},tmp_path);assert hold.recorded()==[]
    p=tmp_path/'sessions'/'one'/'events.jsonl';p.parent.mkdir(parents=True)
    row=dict(kind='rtvi-output',source='RTVIProcessor',direction='DOWNSTREAM',monotonic_ns=10)
    append(p,row)
    with p.open('a') as stream:stream.write('{"kind":"partial"')
    assert hold.recorded()==[row]
    q=tmp_path/'sessions'/'two'/'events.jsonl';q.parent.mkdir();append(q,dict(kind='session-start'))
    with p.open('a') as stream:stream.write('}\n')
    result=hold.recorded()
    assert result==[row,dict(kind='partial'),dict(kind='session-start')]
    assert outgoing_rtvi_count(result,0,20)==1
    result.clear()  # Returned list is a snapshot, not the cache's mutable list.
    assert len(hold.recorded())==3


@pytest.mark.parametrize('fault',['rotation','truncation','rewrite','disappearance'])
def test_recorded_changed_session_fails_closed(tmp_path,fault):
    from echoear_aec_campaign import BenchHold
    hold=BenchHold({}, {},tmp_path)
    p=tmp_path/'sessions'/'one'/'events.jsonl';p.parent.mkdir(parents=True);append(p,dict(kind='old'))
    assert hold.recorded()==[dict(kind='old')]
    if fault=='rotation':p.rename(p.with_name('retained'));append(p,dict(kind='new'))
    elif fault=='truncation':p.write_text('')
    elif fault=='rewrite':p.write_text('{"kind":"new"}\n'*10)
    else:p.unlink()
    with pytest.raises(EventStreamChanged):hold.recorded()
