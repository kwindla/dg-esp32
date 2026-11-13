"""Prospective v8.5 contract: no hardware, timing and failure-path witnesses."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from echoear_aec_v85_transport import audit,span
from echoear_aec_v85 import make_plan,validate,POLICY
from echoear_aec_stimulus import schedule_active_window,StimulusPreconditionFailure
from echoear_aec_campaign import Schedule,exception_class,protocol,soak_cells


def plan():
    return json.loads(Path(__file__).with_name('plans').joinpath('echoear-aec-accept-v8.5.json').read_text())


def perf(t,received,lost=0,dropped=None,ge6=0,zeros=0):
    return dict(pc_id='p',generation=1,device_us=t,received=received,lost=lost,
        playout_dropped=lost if dropped is None else dropped,
        playout=dict(missing_run_ended=[lost,0,0,0,0,ge6],post_plc_zeros=zeros,starvation=0),
        send_errors=0,send_budget_cutoffs=0,aec=dict(capture=dict(rx_overflows=0)),drop_reasons={'late':0})


def test_cell_ratio_unconditional_and_whole_short_epoch_report_only():
    a,b=perf(1,0),perf(2,269,3)
    r=audit([a,b],[])
    assert r['passed'] and not r['aggregates'][0]['ratio_gated']
    assert r['aggregates'][0]['ratio']==6/269
    r=audit([a,b],[span(a,b,'cell','c')])
    assert not r['passed'] and not r['cells'][0]['passed']


def test_piecewise_merge_excludes_cells_exports_and_merges_tail_backward():
    v=[perf(1,0),perf(2,269,3),perf(3,1269,3),perf(4,2269,33,ge6=1,zeros=1),
       perf(5,3769,33,ge6=1,zeros=1),perf(6,3869,34,ge6=1,zeros=1)]
    r=audit(v,[span(v[1],v[2],'cell','c'),span(v[2],v[3],'export','c')])
    assert r['passed']
    assert len(r['aggregates'])==1
    assert r['aggregates'][0]['delta']==dict(received=1869,lost=4,playout_dropped=4)
    assert r['aggregates'][0]['intervals']==[[1,2],[4,5],[5,6]]
    export=r['intervals'][2]
    assert export['scope']=='export' and export['passed'] and not export['hard_passed']
    assert not r['raw_first_to_last']['passed']


@pytest.mark.parametrize('fault',['burst','zeros','delivery','epoch','missing'])
def test_short_original_interval_hard_failures_never_merge_away(fault):
    a,b,c=perf(1,0),perf(2,20),perf(3,2000)
    spans=[]
    if fault=='burst':b['playout']['missing_run_ended'][5]=c['playout']['missing_run_ended'][5]=1
    if fault=='zeros':b['playout']['post_plc_zeros']=c['playout']['post_plc_zeros']=1
    if fault=='delivery':b['send_errors']=c['send_errors']=1
    if fault=='epoch':b['generation']=c['generation']=2
    if fault=='missing':spans=[dict(pc_id='p',generation=1,lo=1,hi=4,kind='cell',id='c')]
    assert not audit([a,b,c],spans)['passed']


class Clock:
    def __init__(self,start=10):self.now=start
    def __call__(self):return self.now
    def is_set(self):return False
    def wait(self,seconds):self.now+=seconds


def write_events(path,rows):path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
def w(t,active=True,pc='p',cid='c'):
    return dict(kind='tts-transport-write',pc_id=pc,context_id=cid,non_silent=active,monotonic_ns=int(t*1e9))


def test_reconnect_window_waits_through_pause_and_ignores_other_context(tmp_path):
    path=tmp_path/'writes';write_events(path,[w(10),w(11,False),w(11.01,cid='wrong'),w(11.04)])
    clock=Clock();called=[]
    def play():called.append(clock());return dict(request_monotonic_s=clock())
    r=schedule_active_window(lambda:[path],int(10e9),play,lambda:dict(pc_id='p',context_id='c'),clock=clock,cancel=clock)
    assert not r['skipped'] and len(called)==1 and 1.04<=r['actual_invocation_offset_s']<1.05
    assert r['active_tts_witness']['active']


@pytest.mark.parametrize('mode',['silent','stale','missing','wrongpeer','ambiguous','cancelled'])
def test_reconnect_unsatisfied_window_never_invokes(tmp_path,mode):
    path=tmp_path/'writes';events=[w(10),w(11,False)]
    if mode=='stale':events=[w(10)]
    if mode=='missing':events=[]
    if mode=='wrongpeer':events=[w(10,pc='q'),w(11.01,pc='q')]
    write_events(path,events);clock=Clock();calls=[]
    def identity():
        if mode=='ambiguous':raise StimulusPreconditionFailure('ambiguous')
        return dict(pc_id='p',context_id='c')
    if mode=='cancelled':clock.is_set=lambda:True
    r=schedule_active_window(lambda:[path],int(10e9),lambda:calls.append(1),identity,clock=clock,cancel=clock,timeout_s=2)
    assert r['skipped'] and not calls
    assert exception_class(StimulusPreconditionFailure(r['reason']))=='stimulus-precondition'


def test_new_reply_waits_for_last_context_write_and_quiet_tail():
    from echoear_aec_reconnect import completed_new_reply
    events=[dict(case_id='new',pc_id='p2',context_id='c',event='tts-generation-metric-start'),
            dict(case_id='new',event='frame',frame='BotStartedSpeakingFrame',t=10),
            dict(case_id='new',event='frame',frame='BotStoppedSpeakingFrame',t=21.8)]
    writes=[w(22,pc='p2'),w(100,pc='other')]
    assert completed_new_reply(events,writes,'new','p1',24.29) is None
    r=completed_new_reply(events,writes,'new','p1',24.31)
    assert r['quiet_s']>=2.3 and r['last_write_s']==22
    assert completed_new_reply(events,writes,'wrong','p1',25) is None
    with pytest.raises(RuntimeError):completed_new_reply(events,writes,'new','p2',25)


def test_long_tts_limits_shared_budget_neutral_streak_and_whole_pair():
    from echoear_aec_v85_tts import admit,exclusions
    witness=dict(complete=True,long_generations=[{'lead_s':1}],case_ids=['a','b'],
                 audit=dict(sessions=[dict(generations=[dict(case_id='a',context_id='x'),dict(case_id='b',context_id='y')])]))
    saved={}
    assert admit(saved,1,'pair',witness,True)['admitted']
    assert exclusions(saved)=={('a','x'),('b','y')}
    assert admit(saved,2,'other',witness,True)['admitted']
    assert not admit(saved,1,'third',witness,True)['admitted']
    for bad,integrity,attempt in [(dict(witness,complete=False),True,1),(witness,False,1),(witness,True,3)]:
        assert not admit({},attempt,'x',bad,integrity)['admitted']
    p=plan();schedule=Schedule(p['cases'][:1],[],p['noise_gate'])
    group,n=schedule.next();schedule.queue.noisy_cells=['noise-1','noise-2']
    schedule.finish(group,n,False,{},dict(kind='long-tts'))
    assert schedule.queue.noisy_cells==['noise-1','noise-2'] and not schedule.queue.paused
    assert schedule.state()['pending'][0]['attempt']==2


def test_v85_counts_budget_protocol_and_report_only_spares():
    p=plan();assert validate(p)
    b=p['time_estimate']['v85_budget'];assert b['nominal_s']==4290 and b['rtp_evidence_max_packets']==6200000
    assert b['max_attempts']==213 and b['hard_wallclock_bound_s'] is None
    spec=protocol(soak_cells(p),p['reconnects'])
    ids={c['id'] for c in spec['echo_replies']}
    assert all(f"{c['id']}-a{a}" in ids for c in soak_cells(p) for a in (1,2,3))
    c=next(c for c in p['cases'] if c.get('report_only') and c['kind']=='stop')
    s=Schedule([c],[],p['noise_gate']);g,n=s.next();s.finish(g,n,True,{c['id']:'unscorable-missing-witness'})
    assert not s.unresolved and not s.state()['pending']
    for mutate in [lambda q:q['sessions'].append(q['sessions'][0]),lambda q:q['v85']['transport'].update(cell_ratio_max=.03),lambda q:q['reconnects'][0].update(second_reply_window_seconds=8)]:
        q=copy.deepcopy(p);mutate(q)
        with pytest.raises(ValueError):validate(q)


def test_empty_v85_verdict_is_fail_closed():
    from echoear_aec_campaign_analysis import campaign_verdict
    r=campaign_verdict(plan(),[])
    assert not r['passed'] and not r['qualification'] and not r['complete_denominators']
    assert r['opportunity_strata']['strict']['eligible']==0


def test_reconnect_skip_persisted_before_raise_and_single_capture_end_perf(tmp_path,monkeypatch):
    import time
    import echoear_aec_reconnect as r
    import echoear_aec_resource_trial as resource
    calls=[]
    monkeypatch.setattr(r,'schedule_active_window',lambda *a,**k:dict(skipped=True,reason='no speech'))
    def snap(board,folder,label):calls.append(label);write_events(folder/(label+'.json'),[{'device_us':1}])
    monkeypatch.setattr(resource,'snapshot',snap)
    anchor=lambda *a:dict(coordinate=1,lower_s=0,upper_s=0)
    with pytest.raises(StimulusPreconditionFailure):
        r.capture_reconnect(None,lambda *a:None,lambda *a:None,lambda:[],lambda *a:None,tmp_path,
            dict(id='r',active_speech_window_s=[1,1.5]),start=anchor,stop=anchor,anchor=anchor,
            sleep=lambda n:time.sleep(.001 if n==2.3 else min(n,.001)),context_identity=lambda:None)
    assert json.loads((tmp_path/'timer.json').read_text())['skipped']
    assert calls==['capture-end'] and (tmp_path/'anchors.json').exists()


@pytest.mark.parametrize('phase',['old','new'])
def test_reconnect_failure_seals_actual_case_before_health_and_keeps_soak_protocol(tmp_path,monkeypatch,phase):
    import time
    import echoear_aec_campaign as c
    import echoear_aec_reconnect as r
    h=c.BenchHold(plan(),plan()['sessions'][0],tmp_path)
    h.evaluation=tmp_path/'evaluation';h.evaluation.mkdir();h.board=None
    h.baseline=-60;h.identity={};events=[];calls=[]
    h.events=lambda:events;h.paths=lambda:[]
    h.save=lambda:None
    def send(op,cid):
        calls.append((op,cid));h.number+=1
        events.append(dict(event='ack',number=h.number))
        if op=='speak':
            events.extend([dict(case_id=cid,event='frame',frame='BotStartedSpeakingFrame',t=time.monotonic()-5),dict(case_id=cid,event='frame',frame='BotStoppedSpeakingFrame',t=time.monotonic()-3)])
        if op=='seal':events.append(dict(case_id=cid,event='case-sealed'))
    h.send=send
    def fail(board,command,boundary,tracked,paths,prepare,out,cell,*rest):
        tracked('speak',cell['id']+'-old')
        if phase=='new':
            prepare(cell['id']+'-new');tracked('speak',cell['id']+'-new')
            ids={v['id'] for v in json.loads((h.evaluation/'protocol.json').read_text())['echo_replies']}
            assert all(f"{s['id']}-a1" in ids for s in soak_cells(h.p))
        raise StimulusPreconditionFailure('skipped')
    monkeypatch.setattr(r,'run_reconnect',fail);monkeypatch.setattr(c,'command',lambda *a:None)
    with pytest.raises(StimulusPreconditionFailure):h.reconnect(h.p['reconnects'][0])
    h.settle_attempt()
    assert calls[-2:]==[('seal','reconnect-01-a1-'+phase),('health',None)]


def test_host_audit_unreached_is_not_missing_attempt(tmp_path,monkeypatch):
    import echoear_aec_host_gate as gate
    from test_echoear_aec_v85_host_gate import witness
    from test_aec_host_wifi_diagnostics import raw
    h=SimpleNamespace(out=tmp_path,p={'host_environment':gate.V85_POLICY},result={},save=lambda:None)
    g=gate.HostGate(h);witnesses=witness()
    def query(argv,**kw):
        if argv[0]=='iw':return raw('Connected to '+gate.BSSID+'\n freq: 5580\n signal: -45 dBm')
        if argv[0]=='nmcli':
            if 'GENERAL.CONNECTION,GENERAL.CON-UUID' in argv:return witnesses['before']
            if 'connection.id,connection.uuid,802-11-wireless.bssid' in argv:return witnesses['profile']
            return raw(gate.BSSID)
        return raw('')
    monkeypatch.setattr(gate.wifi,'query',query)
    for label in ('hold-start','hold-end'):g.boundary(tmp_path,label)
    h.result['error_class']='stimulus-precondition'
    (tmp_path/'result.json').write_text(json.dumps(h.result))
    r=gate.audit(tmp_path,gate.V85_POLICY)
    assert r['passed'] and sum(b.get('status')=='not-reached' for b in r['boundaries'])==2
    h.result['host_boundary_attempts'].append('never-written/before')
    (tmp_path/'result.json').write_text(json.dumps(h.result))
    assert not gate.audit(tmp_path,gate.V85_POLICY)['passed']


def test_replayed_long_generation_requires_complete_whole_group():
    from echoear_aec_v85_tts import inspect
    from echoear_aec_accept import rows
    root=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9/campaign-09/qualification-01')
    if not root.exists():pytest.skip('Retained evidence unavailable')
    events=rows(root/'evaluation/events.jsonl')
    writes=[e for path in (root/'sessions').glob('*/events.jsonl') for e in rows(path)]
    case='qualification-01-echo-06-a1';r=inspect(events,writes,[case])
    assert r['complete'] and r['long_generations'][0]['lead_s']==1.073
    assert not inspect(events,writes,[case,'missing-pair-member'])['complete']
    assert not inspect([e for e in events if e['event']!='tts-generation-stopped'],writes,[case])['complete']
    assert not inspect(events,[],[case])['complete']


def test_single_hold_runner_orchestrates_once_without_real_hardware(tmp_path,monkeypatch):
    import echoear_aec_campaign as c
    calls=[];p=plan();path=tmp_path/'plan.json';path.write_text(json.dumps(p))
    monkeypatch.setattr(c,'validate_campaign',lambda p:None)
    monkeypatch.setattr(c,'check_esptool',lambda:None)
    monkeypatch.setattr(c,'finalize_analysis',lambda *a:None)
    monkeypatch.setattr(c.signal,'signal',lambda *a:None)
    monkeypatch.setattr(c,'run_schedule',lambda *a:calls.append('schedule'))
    monkeypatch.setattr(c,'run_soak',lambda *a:calls.append('soak'))
    class Hold:
        def __init__(self,*a):calls.append('construct')
        def __enter__(self):calls.append('flash');return self
        def __exit__(self,*a):calls.append('restore')
        def connect(self,cells,reconnects):calls.append('connect');assert len([v for v in cells if v['kind']=='soak-echo'])==10
        def link_probe(self):calls.append('link-probe')
        def reconnect(self,cell):calls.append(cell['id'])
        def finish(self):calls.append('finish')
        def probe(self,cell):calls.append('probe')
    c.run(path,tmp_path/'output',hold_factory=Hold)
    assert calls==['construct','flash','connect','link-probe','schedule','reconnect-01','reconnect-02','soak','finish','probe','restore']


def test_v85_verdict_firm_and_report_only_denominators():
    from echoear_aec_campaign_analysis import campaign_verdict
    p=plan();cases=[]
    for c in p['cases']+[c for c in p['controls'] if c['kind']!='probe']+soak_cells(p):
        cases.append(dict(c,cell_id=c['id'],clean=True,barge_success=True,opportunity='eligible',quick_gap=dict(witnessed=c.get('quick_gap_s') is not None)))
    h=dict(id='qualification-01',acceptance_credit=True,passed=True,cases=cases,
           reconnects=[dict(passed=True,noise_passed=True)]*2,probes=[dict(noise=True,passed=True)],soak=dict(passed=True))
    assert campaign_verdict(p,[h])['passed']
    normal=next(c for c in cases if c.get('report_only') and c['kind']=='stop');normal['barge_success']=False
    assert campaign_verdict(p,[h])['passed']
    stops=[c for c in cases if not c.get('report_only') and c['kind']=='stop']
    stops[0]['barge_success']=False;assert campaign_verdict(p,[h])['passed']
    stops[1]['barge_success']=False;assert not campaign_verdict(p,[h])['passed']


def test_long_tts_soak_retry_waits_until_original_schedule_finishes(tmp_path,monkeypatch):
    import echoear_aec_campaign as c
    clock=[0.];requests=[]
    monkeypatch.setattr(c,'snapshot',lambda *a:None)
    monkeypatch.setattr(c.host_nic,'snapshot',lambda:{})
    h=SimpleNamespace(p=plan(),board=None,out=tmp_path)
    def acquire(hold,g,a,scheduled_s,resource_tick):
        clock[0]=scheduled_s;requests.append((g['run_id'],a,scheduled_s))
        for _ in range(3):clock[0]+=8;resource_tick()
        bad=len(requests)==1
        return (False,[],dict(kind='long-tts',retry_safe=True)) if bad else (True,[],None)
    monkeypatch.setattr(c,'acquire',acquire)
    r=c.run_soak(h,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert r['passed'] and [v[2] for v in requests[:10]]==list(range(10,551,60))
    assert requests[-1][0]==requests[0][0] and requests[-1][1]==2 and requests[-1][2]>=610
