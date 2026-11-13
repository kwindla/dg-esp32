"""Host-only classification, recovery and launch-gate regression checks."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import echoear_aec_campaign as runner
from echoear_aec_campaign_analysis import transport
from echoear_aec_transport_environment import classify,link_probe,uplink_loss,uplink_excursion,reconnect_witness,piecewise_transport


def snapshot(t=0):
    return dict(device_us=t,pc_id='pc',generation=1,received=1000+t//20000,lost=0,late=0,playout_dropped=0,
        send_errors=0,send_budget_cutoffs=0,drop_reasons={'send':0},aec={'capture':{'rx_overflows':0}},
        liveness=dict(pings=10+t//500000,pongs=10+t//500000,timeouts=0),
        playout=dict(post_plc_zeros=0,starvation=0,missing_slots=0),
        i2s_underflows=0,i2s_write_errors=0,i2s_write_timeouts=0)


def health(t=3):
    return dict(event='rtp-observer-health',pc_id='pc',t=t,passed=True,
                sctp=dict(supported=True,flight_fix_enabled=True,records=1,truncated=False,unavailable=False))


def records(duration=10,loss=False,excursion=False):
    # 50Hz through both boundaries, including sequence rollover.
    return [dict(pc_id='pc',ssrc=6,seq=(65500+i)%65536,observed_ms=i*20+(200 if excursion and 100<=i<250 else 0),timestamp=(i*960)%2**32)
            for i in range(int(duration*50)+1) if not (loss and 100<=i<120)]


def inputs():
    before=snapshot();after=snapshot(10000000)
    after.update(lost=20,late=20,playout_dropped=20);after['playout']['missing_slots']=20
    checks=dict(resources={'all_passed':True},admission={'passed':True},rf={'passed':True},delivery={'passed':True},transport=transport(before,after))
    return dict(before=before,after=after,checks=checks,pre_rf={'passed':True},events=[health()],
        downlink=[dict(pc_id='pc',ssrc=1,seq=i,send_ms=i*20) for i in range(501)],
        sctp=[dict(pc_id='pc',monotonic_ns=3_000_000_000,event='receive-chunk',chunk_type='SackChunk')],
        uplink=records(loss=True,excursion=True),window=[0,10],cells=[dict(run_id='cell',start_ns=1_000_000_000,end_ns=7_000_000_000)])


def test_all_six_and_unique_ratio():
    v=inputs();r=classify(**v)
    assert r['passed'] and all(r['conditions'].values())
    assert r['playout']['late_to_missing_ratio']==1
    assert r['uplink']['unique_missing']==20 and r['uplink']['excursion_seconds']>=3
    assert r['raw']['before']==v['before']
    assert v['checks']['transport']['unique_unusable_fraction']==.04


@pytest.mark.parametrize('fault',['resources','admission','rf','delivery','send_errors','send_budget_cutoffs','new_drop','rx_overflows','i2s','observer','timeouts','pongs','alarm','error','fatal','evaluation','reconnect','zeros','starvation','missing_playout','down_gap','down_seq','down_missing','down_edge','sctp_missing','sctp_t3','sctp_abort','uplink_missing','uplink_no_loss','uplink_other_pc','uplink_truncated','timestamp_missing','outside_window','pre_rf'])
def test_each_witness_fails_closed(fault):
    v=inputs();a=v['after']
    if fault in ('resources','admission','rf','delivery'):v['checks'][fault]['all_passed' if fault=='resources' else 'passed']=False
    elif fault in ('send_errors','send_budget_cutoffs'):a[fault]=1
    elif fault=='new_drop':a['drop_reasons']['new']=1
    elif fault=='rx_overflows':a['aec']['capture']['rx_overflows']=1
    elif fault=='i2s':a['i2s_underflows']=1
    elif fault=='observer':v['events'][0]['passed']=False
    elif fault=='timeouts':a['liveness']['timeouts']=1
    elif fault=='pongs':a['liveness']['pongs']-=2
    elif fault in ('alarm','error','fatal','evaluation','reconnect'):
        v['events'].append(dict(pc_id='pc',t=4,event={'alarm':'liveness-alarm','evaluation':'evaluation-error','reconnect':'tts-reconnect-start'}.get(fault,'frame'),frame={'error':'ErrorFrame','fatal':'FatalErrorFrame'}.get(fault)))
    elif fault in ('zeros','starvation'):a['playout']['post_plc_zeros' if fault=='zeros' else fault]=1
    elif fault=='missing_playout':del a['playout']['starvation']
    elif fault=='down_gap':v['downlink'][100]['send_ms']+=100
    elif fault=='down_seq':v['downlink'][100]['seq']+=1
    elif fault=='down_missing':v['downlink']=[]
    elif fault=='down_edge':v['downlink']=v['downlink'][10:]
    elif fault=='sctp_missing':v['sctp']=[]
    elif fault=='sctp_t3':v['sctp'][0]['event']='t3-expired'
    elif fault=='sctp_abort':v['sctp'][0]['chunk_type']='AbortChunk'
    elif fault=='uplink_missing':v['uplink']=[]
    elif fault=='uplink_no_loss':v['uplink']=records()
    elif fault=='uplink_other_pc':
        for r in v['uplink']:r['pc_id']='else'
    elif fault=='uplink_truncated':v['uplink']=v['uplink'][:-100]
    elif fault=='timestamp_missing':del v['uplink'][0]['timestamp']
    elif fault=='outside_window':
        for r in v['uplink']:r['observed_ms']+=20000
    elif fault=='pre_rf':v['pre_rf']['passed']=False
    assert not classify(**v)['passed'],fault


def test_probe_exact_limits_and_missing_coverage():
    r=records(300)
    assert link_probe(r,'pc',0,300)['passed']
    assert not link_probe(r,'pc',0,239.999)['passed']
    assert not link_probe([],'pc',0,300)['passed']
    assert not link_probe(r[:-100],'pc',0,300)['passed']
    # A 10% second is no longer a gate; its aggregate unique loss is tiny.
    burst=[x for i,x in enumerate(r) if not 100<=i<105]
    assert link_probe(burst,'pc',0,300)['passed']
    # Distributed two percent with no 10% bucket also rejects.
    steady=[x for i,x in enumerate(r) if i%50!=10]
    out=link_probe(steady,'pc',0,300)
    assert not out['passed'] and out['uplink']['unique_loss_fraction']>.005
    assert out['result']=='LINK PROBE FAILED, retry later'


def test_uplink_wrap_duplicate_reordering_and_other_peer():
    r=records();r[20],r[21]=r[21],r[20]
    r+=copy.deepcopy(r[:20])+[dict(pc_id='else',ssrc=2,seq=100,observed_ms=50)]
    assert uplink_loss(r,'pc',0,10)['missing']==0


def test_piecewise_only_witnessed_recovered_span_excluded():
    a=snapshot();b=snapshot(5_000_000);c=snapshot(10_000_000);d=snapshot(15_000_000)
    for x in (c,d):x.update(lost=20,playout_dropped=20)
    f=dict(kind='transport-environmental',retry_safe=True,witnesses={'passed':True},excluded_transport_interval=dict(pc_id='pc',generation=1,before_device_us=b['device_us'],after_device_us=c['device_us']))
    r=piecewise_transport([a,b,c,d],[f]);assert r['passed'] and not r['raw_first_to_last']['passed']
    assert [x['excluded_environmental'] for x in r['intervals']]==[False,True,False]
    f['retry_safe']=False;assert not piecewise_transport([a,b,c,d],[f])['passed']
    f['retry_safe']=True;d['lost']+=10
    assert not piecewise_transport([a,b,c,d],[f])['passed']


def test_caps_shared_campaign_and_new_hold():
    p={'failure_policy':{'transport_environmental':dict(max_per_hold=3,max_per_campaign=6)}}
    counts={'campaign':0};h=SimpleNamespace(p=p,environmental_counts=counts)
    f={'kind':'transport-environmental'}
    for _ in range(3):runner.environmental_cap(h,f)
    with pytest.raises(runner.EnvironmentalPause):runner.environmental_cap(h,f)
    h=SimpleNamespace(p=p,environmental_counts=counts)
    for _ in range(2):runner.environmental_cap(h,f)
    with pytest.raises(runner.EnvironmentalPause):runner.environmental_cap(h,f)
    assert counts['campaign']==7


@pytest.mark.parametrize('dirty',[None,'transport','resources','delivery','admission','duration','t3'])
def test_environmental_recovery_uses_second_clean_interval(tmp_path,monkeypatch,dirty):
    h=runner.BenchHold({}, {}, tmp_path);g=dict(run_id='a',cells=[]);folder=tmp_path/'a-a1';folder.mkdir()
    h.attempt_before=snapshot();h.attempt_pre=[];h.attempt_started_ns=0
    h.settle_attempt=lambda:None;h.events=lambda:[health()];h.recorded=lambda:[]
    first=snapshot(10_000_000);last=snapshot(15_000_000 if dirty!='duration' else 14_000_000)
    for x in (first,last):x.update(lost=20,playout_dropped=20)
    if dirty=='transport':last['lost']+=20
    if dirty=='delivery':last['send_errors']=1
    tick=[0.];monkeypatch.setattr(runner.time,'monotonic',lambda:tick[0])
    monkeypatch.setattr(runner.time,'sleep',lambda n:tick.__setitem__(0,tick[0]+n))
    calls=[]
    def boundary(board,path,label,*,admission_window=None):
        if admission_window is not None:admission_window.update(end_ns=1_000_000_000)
        calls.append(label);return (first if label=='retry-check' else last),[]
    monkeypatch.setattr(runner,'boundary',boundary)
    monkeypatch.setattr(runner,'check_reply_resources',lambda *a,**k:dict(all_passed=dirty!='resources'))
    monkeypatch.setattr(runner,'live_admission',lambda *a,**k:dict(passed=dirty!='admission'))
    if dirty=='t3':
        (tmp_path/'rtp').mkdir();(tmp_path/'rtp/sctp.jsonl').write_text(json.dumps(dict(pc_id='pc',event='t3-expired',monotonic_ns=1))+'\n')
    exc=runner.HarnessFailure('transport-environmental','link');exc.witnesses={'passed':True}
    if dirty:
        with pytest.raises(RuntimeError):h.recover_harness(g,1,exc)
    else:assert h.recover_harness(g,1,exc)['retry_safe']
    assert calls==['retry-check','retry-check-2']
    saved=json.loads((folder/'acquisition-failure.json').read_text())
    assert saved['witnesses']['passed'] and saved['retry_safe']==(dirty is None)
    assert saved['transport']['delta']['lost']==(20 if dirty=='transport' else 0)


@pytest.mark.parametrize('bad',[False,True])
def test_idle_probe_records_and_stops_without_launch(tmp_path,monkeypatch,bad):
    h=runner.BenchHold(dict(link_probe={'duration_s':240}),{},tmp_path)
    tick=[0.];monkeypatch.setattr(runner.time,'monotonic',lambda:tick[0])
    monkeypatch.setattr(runner.time,'sleep',lambda n:tick.__setitem__(0,tick[0]+n))
    monkeypatch.setattr(runner,'boundary',lambda *args:(snapshot(int(tick[0]*1e6)),[snapshot()]))
    monkeypatch.setattr(runner,'check_reply_resources',lambda *a,**kw:dict(all_passed=True))
    h.events=lambda:[health(240)];h.send=lambda *args:None
    (tmp_path/'rtp').mkdir();packet_rows=records(240)
    if bad:packet_rows=[r for i,r in enumerate(packet_rows) if not 100<=i<150]
    (tmp_path/'rtp/socket-rtp.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in packet_rows))
    if bad:
        with pytest.raises(runner.LinkProbeFailed,match='LINK PROBE FAILED, retry later'):h.link_probe()
    else:h.link_probe()
    value=json.loads((tmp_path/'link-probe/result.json').read_text())
    assert value['duration_s']==240 and value['passed']==(not bad)
    assert h.result['link_probe']==value


def test_retained_campaigns_and_transport_passing_control(tmp_path):
    from echoear_aec_transport_review_replay import replay
    base=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')
    if not (base/'campaign-02').exists():pytest.skip('Retained review evidence is local to the bench host')
    a=replay(base/'campaign-01/qualification-01','qualification-01-echo-08-a1',tmp_path/'c1.json')
    assert not a['classification']['passed']
    assert not a['classification']['conditions']['only_transport_failed']
    assert not a['classification']['conditions']['device_alive']
    b=replay(base/'campaign-02/qualification-01','qualification-01-echo-pair-4-a1',tmp_path/'c2.json')
    assert b['whole_uplink']['missing']==7 and b['whole_uplink']['expected']==9736
    assert b['naive_forward_gap_sum']==308
    c=b['classification']
    assert c['uplink']['unique_missing']==7 and c['uplink']['expected']==3570
    assert c['uplink']['excursion_seconds']==5 and c['uplink']['packets_over_100ms_5s_max']>=25
    assert c['passed'] and len(c['reconnects']['cycles'])==2
    d=replay(base/'campaign-01/qualification-01','qualification-01-echo-07-a1',tmp_path/'c1-cell7.json')['classification']
    assert d['uplink']['excursion_seconds']>=3 and d['uplink']['packets_over_100ms_5s_max']>=25
    # Preserve the actual counter/co-timing facts; no retrospective waiver.
    assert not d['raw']['checks']['transport']['passed']
    assert d['raw']['checks']['transport']['loss_fraction']==16/1795
    assert not d['uplink']['overlaps_cell'] and d['passed']
    assert all(e['during_export'] is True for e in d['uplink']['excursions'])
    assert all(e['during_export'] is False for e in c['uplink']['excursions'])
    assert not d['raw']['checks']['transport']['passed']
    v=inputs();v['after'].update(lost=0,late=0,playout_dropped=0);v['after']['playout']['missing_slots']=0
    v['checks']['transport']=transport(v['before'],v['after'])
    r=classify(**v)
    assert r['conditions']['independent_link'] and not r['conditions']['only_transport_failed'] and not r['passed']


@pytest.mark.parametrize('kind',['completed','failed','refused','unfinished','reply-overlap','stimulus-overlap','cross-boundary','aborted'])
def test_reconnect_lifecycle_and_overlap(kind):
    events=[dict(event='tts-generation-metric-start',pc_id='pc',context_id='g',t=1),
            dict(event='tts-generation-stopped',pc_id='pc',context_id='g',t=2),
            dict(event='tts-reconnect-start',pc_id='pc',t=3)]
    if kind!='unfinished':events.append(dict(event='tts-reconnect-ready',pc_id='pc',t=4))
    cells=[]
    if kind in ('failed','aborted'):events[-1]['event']='tts-reconnect-'+kind
    if kind=='refused':events.append(dict(event='tts-reconnect-refused',pc_id='pc',t=3.5))
    if kind=='reply-overlap':events[1]['t']=3.5
    if kind=='cross-boundary':events[-2]['t']=-1;events[-1]['t']=1.5
    if kind=='stimulus-overlap':cells=[dict(run_id='near',jabra={'request_monotonic_s':3.5,'elapsed_seconds':1})]
    r=reconnect_witness(events,'pc',[0,10],cells)
    assert r['passed']==(kind=='completed')
    if kind=='completed':assert r['cycles'][0]['completed'] and not r['cycles'][0]['overlaps']


def test_probe_reports_excursions_without_gating_and_rejects_stall():
    assert link_probe(records(300,excursion=True),'pc',0,300)['passed']
    assert link_probe(records(300,excursion=True),'pc',0,300)['uplink']['excursion_count']==1
    # Exactly 1s inter-arrival gap: remove 49 consecutive 20ms packets.
    r=[x for i,x in enumerate(records(300)) if not 100<=i<149]
    v=link_probe(r,'pc',0,300)
    assert not v['passed'] and v['uplink']['max_gap_ms']==1000


def test_delay_thresholds_density_and_co_timing():
    cells=[dict(id='c',start_ns=0,end_ns=10_000_000_000)]
    def data(indices):
        r=records()
        for i in indices:r[i]['observed_ms']+=200
        return r
    assert uplink_excursion(data(range(100,250)),'pc',0,10,cells)['passed']
    assert not uplink_excursion(data(list(range(100,108))+list(range(150,158))+list(range(200,208))),'pc',0,10,cells)['passed'] #24 packets
    assert not uplink_excursion(data(range(100,180)),'pc',0,10,cells)['passed'] #2 seconds
    assert not uplink_excursion(data(list(range(100,115))+list(range(200,215))+list(range(300,315))),'pc',0,10,cells)['passed'] #not consecutive
    assert uplink_excursion(data(range(100,250)),'pc',0,10,[])['passed']


def test_window_local_baseline_and_timestamp_wrap():
    r=records()
    for x in r:x['timestamp']=(x['timestamp']+2**32-48000)%2**32
    v=uplink_excursion(r,'pc',0,10)
    assert v['valid'] and v['max_delay_ms']<.001
    shifted=copy.deepcopy(r)
    for x in shifted:x['observed_ms']+=5000
    w=uplink_excursion(shifted,'pc',5,15)
    assert w['valid'] and w['max_delay_ms']<.001
    assert abs((w['baseline_ms']-v['baseline_ms'])-5000)<.001


def test_probe_half_percent_boundary_and_serializable_stall():
    # 12,000 unique expected IDs; 60/12,000 ==0.5%, 61 exceed it.
    r=records(240)[:-1]
    for count,passed in ((60,True),(61,False)):
        omitted={50+190*i for i in range(count)}
        sample=[x for i,x in enumerate(r) if i not in omitted]
        value=link_probe(sample,'pc',0,240)
        assert value['passed']==passed
        assert value['uplink']['unique_loss_fraction']==count/12000
        json.dumps(value,allow_nan=False)
    sample=[x for i,x in enumerate(r) if not 100<=i<149]
    value=link_probe(sample,'pc',0,240)
    assert value['uplink']['stall'] and not value['passed']
    json.dumps(value,allow_nan=False)


def test_malformed_lifecycle_refuses_classification():
    v=inputs();v['events'].append(dict(event='tts-reconnect-start',pc_id='pc'))
    assert not classify(**v)['conditions']['liveness_intact']


def test_later_ready_cannot_cure_incomplete_reconnect_at_attempt_end():
    events=[dict(event='tts-reconnect-start',pc_id='pc',t=9),
            dict(event='tts-reconnect-ready',pc_id='pc',t=11)]
    assert not reconnect_witness(events,'pc',[0,10])['passed']



def test_probe_fits_pinned_pipecat_idle_timeout():
    from pipecat.pipeline.worker import IDLE_TIMEOUT_SECS
    plan=json.loads((Path(__file__).parent/'plans/echoear-aec-accept-v1.json').read_text())
    probe=plan['link_probe']
    assert probe['duration_s']==240
    assert probe['connect_overhead_s']==9 and probe['idle_safety_margin_s']==45
    assert probe['connect_overhead_s']+probe['duration_s']+45 < IDLE_TIMEOUT_SECS
    for wrong in (239,241,300):
        altered=copy.deepcopy(plan);altered['link_probe']['duration_s']=wrong
        with pytest.raises(ValueError,match='Link probe thresholds changed'):runner.validate_spec(altered)


def test_no_cell_overlap_is_diagnostic_and_export_is_reported():
    v=inputs();v['cells'][0].update(start_ns=8_000_000_000,end_ns=9_000_000_000)
    v['attempt_timing']=dict(capture_end_s=1,after_boundary_s=10)
    r=classify(**v)
    assert r['passed'] and not r['uplink']['overlaps_cell']
    assert all(e['during_export'] and not e['overlapping_cells'] for e in r['uplink']['excursions'])
    v['attempt_timing']['capture_end_s']=7
    r=classify(**v)
    assert r['passed'] and all(e['during_export'] is False for e in r['uplink']['excursions'])
    del v['attempt_timing']
    assert all(e['during_export'] is None for e in classify(**v)['uplink']['excursions'])


def test_export_load_flag_is_prominent_but_does_not_change_verdict():
    from echoear_aec_transport_environment import export_load_hypothesis
    from echoear_aec_campaign_analysis import campaign_verdict
    from test_aec_campaign import plan,passing_holds
    def failure(flag):
        return dict(kind='transport-environmental',id='a',attempt=1,witnesses={'uplink':{'excursions':[{'during_export':flag}]}})
    assert not export_load_hypothesis([])['all_environmental_attempts_during_export']
    assert not export_load_hypothesis([failure(None)])['all_environmental_attempts_during_export']
    assert not export_load_hypothesis([failure(True),failure(False)])['all_environmental_attempts_during_export']
    p=plan();holds=passing_holds(p)
    holds[0]['environmental']=dict(attempts=[failure(True)],environmental_attempts=1,capture_attempts=10)
    result=campaign_verdict(p,holds)
    assert result['passed'] # report-only; the warning cannot fail acceptance
    assert result['export_load_hypothesis']['all_environmental_attempts_during_export']
    assert 're-examine' in result['export_load_hypothesis']['prominent_warning']


def test_excursion_outside_attempt_cannot_satisfy_link_witness():
    # The source contains a real excursion at2–5s, but the selected attempt is10–20s.
    r=uplink_excursion(records(30,excursion=True),'pc',10,20)
    assert r['valid'] and not r['stall'] and r['excursion_count']==0 and not r['passed']


def test_saved_attempt_window_replays_live_export_witness(tmp_path):
    from echoear_aec_transport_review_replay import replay
    root=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9/campaign-03/qualification-01')
    if not root.exists():pytest.skip('Retained campaign-03 unavailable')
    name='qualification-01-echo-09-a1'
    r=replay(root,name,tmp_path/'replay.json')
    assert r['window_mapping']=='saved host attempt-window.json'
    assert r['classification']==json.loads((root/name/'transport-environmental-witness.json').read_text())
    assert r['classification']['uplink']['excursions'][0]['during_export'] is True


@pytest.mark.parametrize('fault',[None,'ratio','gap','outside','missing-boundary'])
def test_probe_own_rule_does_not_apply_burst_bound_to_240_seconds(fault):
    a=snapshot();b=snapshot(240_000_000);c=snapshot(250_000_000)
    for v in (b,c):v.update(lost=11,late=11,playout_dropped=11)
    packets=records(240)
    if fault=='ratio':packets=[r for i,r in enumerate(packets) if i%50!=10]
    if fault=='gap':packets=[r for r in packets if not 100_000<=r['observed_ms']<=101_000]
    probe=dict(pc_id='pc',generation=1,before_device_us=0,after_device_us=b['device_us'],
               verdict=link_probe(packets,'pc',0,240))
    if fault=='outside':c.update(lost=15,late=15,playout_dropped=15)
    if fault=='missing-boundary':probe['before_device_us']=-1
    r=piecewise_transport([a,b,c],[],probe)
    assert r['passed']==(fault is None)
    assert not piecewise_transport([a,b,c],[])['passed'] # v6 remains reproducible
    if fault is None:
        assert r['intervals'][0]['rule']=='probe-own-rule-v1'
        assert not r['intervals'][0]['per_cell_diagnostic']['passed']
        assert r['intervals'][1]['passed']


def test_probe_scope_rederives_retained_packets_and_keeps_cell09_failure():
    from echoear_aec_transport_environment import probe_epoch_scope
    root=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9/campaign-03/qualification-01')
    if not root.exists():pytest.skip('Retained campaign-03 unavailable')
    probe=probe_epoch_scope(root)
    assert probe['verdict']['passed'] and probe['verdict']['uplink']['unique_missing']==6
    read=lambda name:json.loads((root/name).read_text())
    values=[read('link-probe/before.json'),read('link-probe/after.json'),
            read('qualification-01-echo-09-a1/before.json'),read('qualification-01-echo-09-a1/after.json')]
    r=piecewise_transport(values,[],probe)
    assert r['intervals'][0]['passed'] and not r['intervals'][-1]['passed']
    assert not r['passed']
