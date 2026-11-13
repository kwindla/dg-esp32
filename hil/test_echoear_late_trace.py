from copy import deepcopy
from echoear_late_trace import analyze


def fixture():
    counters=dict(lost=0,concealed=0,drift_drops=0,drift_inserts=0,i2s_underflows=0,i2s_write_errors=0)
    snapshots=[dict(counters,device_us=t,pc_id='a',generation=1,peer_profile=dict(trace=dict(enabled=True,overwritten=0))) for t in (1000000,2000000,3000000)]
    snapshots[-1].update(lost=1,concealed=1)
    sample=dict(before_perf=snapshots[0],reply_window_transport=dict(after=snapshots[1]),after_perf=snapshots[2])
    trace=[]
    for n,t in enumerate((900000,1500000,2500000,3100000)):
        for kind in (1,2,13,14):
            # Frame2 arrives after its PLC decision, all observations retained.
            at=t+1000 if n==2 and kind!=14 else t
            trace.append(dict(device_us=at,kind=kind,flags=int(n==2 and kind==14),reserved=1,ssrc=10,seq=n,timestamp=960*n,aux=1,value=n))
    trace.sort(key=lambda r:r['device_us'])
    sent=[dict(send_ms=t,ssrc=10,seq=n,timestamp=960*n,pc_id='a') for n,t in enumerate((900,1500,2500,3100))]
    return sample,trace,sent,[dict(command='stats',start=2.1,end=2.3)],[dict(host_s=1,device_us=1000000)],dict(overwritten=0)


def test_late_concealment_keeps_boundaries_and_command_timing():
    result=analyze(*fixture())
    assert result['available']
    reply,late=result['phases']
    assert reply['counter_deltas']['lost']==0
    assert late['counter_deltas']['lost']==1
    assert reply['commands']==[]
    assert late['commands'][0]['command']=='stats'
    event=late['concealment_boundaries'][0]
    assert event['boundaries']['13']==[2501000]
    assert event['render']['device_us']==2500000
    assert event['server'][0]['seq']==2
    assert not result['qualification']


def test_overwrite_missing_end_and_changed_session_fail_coverage():
    for change in ('overwrite','export_overwrite','missing','session','disabled'):
        sample,trace,sent,commands,anchors,export=deepcopy(fixture())
        if change=='overwrite':sample['after_perf']['peer_profile']['trace']['overwritten']=1
        if change=='export_overwrite':export['overwritten']=1
        if change=='missing':trace=[r for r in trace if r['device_us']<3000000]
        if change=='session':sample['after_perf']['pc_id']='b'
        if change=='disabled':sample['after_perf']['peer_profile']['trace']['enabled']=False
        assert not analyze(sample,trace,sent,commands,anchors,export)['available'],change


def test_missing_snapshot_is_unavailable():
    assert not analyze({},[],[],[],[])['available']


def test_deferred_trace_exports_only_outside_acquisition(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from e2e_common import display_trace
    calls=[]
    monkeypatch.setattr('arrival_trace.drain',lambda board,path:calls.append(path))
    monkeypatch.setattr('e2e_common.perf',lambda board:dict(peer_profile=dict(trace=dict(overwritten=0))))
    board=SimpleNamespace(e2e_display_trace=True,e2e_defer_trace=True)
    display_trace(board,tmp_path)
    display_trace(board,tmp_path,acquisition=True)
    display_trace(board,tmp_path,acquisition=True)
    display_trace(board,tmp_path)
    assert calls==[tmp_path/'arrivals.jsonl']*2
    board.e2e_defer_trace=False
    display_trace(board,tmp_path,acquisition=True)
    assert len(calls)==3
