from step8e2b_analysis import gap_summary


def test_gap_covariates_do_not_cross_sessions_and_episodes_are_explicit():
    streams={('pc1',1):[dict(t=t) for t in [0,20,81,182,202,323]],
             ('pc2',2):[dict(t=t) for t in [9000,9020]]}
    r=gap_summary(streams,lambda p:p['t'])
    assert r['gaps_ms']['n']==6
    assert r['count_over60ms']==3 and r['count_over100ms']==2
    assert r['episodes_over100ms']==1
    assert r['gaps_ms']['maximum']==121
    assert r['abs_gap_minus20_ms']['p50']==0


def test_device_cadence_target_ceiling_and_declared_windows_are_distinct():
    from step8e2b_analysis import device_send_cadence
    rows=[dict(kind=12,reserved=1,ssrc=7,device_us=t) for t in (0,40000,100000,161000)]
    rows.append(dict(kind=12,reserved=2,ssrc=7,device_us=900000))
    raw=device_send_cadence(rows,0,1000000)
    assert raw['gaps_ms']['n']==3 and raw['gaps_ms']['maximum']==61
    assert raw['over40ms']==2 and raw['over60ms']==1
    healthy=device_send_cadence(rows,0,1000000,[dict(start_us=100001,end_us=161000,category='fault')])
    assert healthy['target_passed'] and healthy['hard_ceiling_passed']
    assert healthy['target_p99_ms']==45
    assert len(healthy['excluded'])==1 and healthy['gaps_ms']['maximum']==60
    empty=device_send_cadence(rows,0,1000000,[dict(start_us=0,end_us=1000000,category='fault')])
    assert empty['target_passed'] is None and empty['hard_ceiling_passed'] is None


def test_link_rate_exact_limit_hard_cap_and_source_separation():
    from uplink_continuity import link_tail
    def population(slow):
        t=0;rows=[dict(pc_id='p',ssrc=1,seq=0,timestamp=0,observed_ms=0)]
        for i in range(1,101):
            t+=slow if i==40 else 20
            rows.append(dict(pc_id='p',ssrc=1,seq=i,timestamp=i*960,observed_ms=t))
        return rows
    rows=population(150);gate=link_tail(rows,0,rows[-1]['observed_ms']/1000,startup_seconds=0)
    assert gate['passed'] and gate['tail_rate']==.01 and gate['intervals']==100
    rows=population(151);assert link_tail(rows,0,rows[-1]['observed_ms']/1000,startup_seconds=0)['passed'] is False
    rows=population(80);rows.append(dict(rows[0],pc_id='other',ssrc=2,observed_ms=90000))
    assert link_tail(rows,0,rows[-2]['observed_ms']/1000,startup_seconds=0)['whole_capture_maximum_ms']==80


def test_startup_and_declared_fault_are_separate_unexplained_tail_stays_eligible():
    from uplink_continuity import link_tail
    rows=[dict(pc_id='p',ssrc=1,seq=i,timestamp=i*960,observed_ms=t) for i,t in enumerate([0,250,5000,5020,5100,5120])]
    g=link_tail(rows,0,5.12)
    assert g['categories']['startup']['maximum_ms']==4750
    assert g['eligible_maximum_ms']==80 and g['passed'] is False
    g=link_tail(rows,0,5.12,excluded_windows=[dict(start_s=5.02,end_s=5.1,category='imposed_fault')])
    assert 'imposed_fault' in g['categories']


def test_impaired_conformance_keeps_strict_and_unknown_distinct_from_edits():
    from playback_quality import impaired_conformance,TRANSPORT_COUNTERS
    before=dict(playout_schema=2,pc_id='p',generation=1,**{k:0 for k in TRANSPORT_COUNTERS},
        playout={k:0 for k in ('missing_slots','plc_calls','post_plc_zeros','starvation','drift_drop','drift_insert','decode_errors','latency_trim','discontinuity')})
    import copy
    after=copy.deepcopy(before);after['playout'].update(missing_slots=1,plc_calls=1);after.update(lost=1,concealed=1,plc_frames=1)
    facts=dict(coverage_complete=True,reply_packets=100,absent_at_ingress=1,longest_concealment_run=1,longest_wire_absent_run=1,starvation_without_wire_gap=0)
    g=impaired_conformance(before,after,facts,board_stt_correct=True,decoded_correlation=.9)
    assert g['passed'] and not g['strict']['passed'] and g['acoustic_scoring_eligible']
    facts['coverage_complete']=False
    assert impaired_conformance(before,after,facts,board_stt_correct=True,decoded_correlation=.99)['passed'] is None
    facts['coverage_complete']=True;after=copy.deepcopy(before);after['playout']['drift_insert']=1;after['drift_inserts']=1
    g=impaired_conformance(before,after,facts,board_stt_correct=True,decoded_correlation=.99)
    assert g['classification']=='edited' and g['strict']['passed']
    after=copy.deepcopy(before);after['playout_dropped']=1;after['playout']['rejected']={'duplicate':1}
    g=impaired_conformance(before,after,facts,board_stt_correct=True,decoded_correlation=.99)
    assert g['classification']=='clean' and not g['strict']['passed']


def test_transient_plc_only_stays_gated_disturbed_over_limit_and_all_excused_unknown():
    from soak_memory import transient_memory
    from test_soak_memory import rows
    data=rows([0,5,6,7],[100000]*4)
    for i,r in enumerate(data):r['perf'].update(rebuffers=0,plc_frames=i)
    data[2]['perf']['heap']['internal']['window_minimum_free']=95000
    result=transient_memory(data,data[-1]['perf'],[])
    assert result['heap_transient_depth_passed'] is False
    assert not result['network_disturbances']
    for i,r in enumerate(data):r['perf']['rebuffers']=i
    result=transient_memory(data,data[-1]['perf'],[])
    assert result['heap_transient_depth_passed'] is None
    assert result['transient_generations']['1']['windows'][0]['disturbed_over_limit']
    data[2]['perf']['heap']['internal']['window_minimum_free']=100000
    result=transient_memory(data,data[-1]['perf'],[])
    assert result['heap_transient_depth_passed'] is None and result['unknown_transient_generations']==['1']


def test_all_perf_resets_are_merged_missing_reset_is_unknown():
    from soak_memory import attach_heap_intervals
    from test_soak_memory import rows
    data=rows([0,5,6,7],[100000]*4)
    middle=__import__('copy').deepcopy(data[2]['perf']);middle['heap_window'].update(start_us=300000000,end_us=330000000)
    middle['heap']['internal']['window_minimum_free']=94000
    data[2]['perf']['heap_window']['start_us']=330000000
    complete=attach_heap_intervals(data,[middle]+[r['perf'] for r in data])
    assert complete[2]['heap_interval']['coverage_complete'] and complete[2]['heap_interval']['minimum_free']['internal']==94000
    missing=attach_heap_intervals(data,[r['perf'] for r in data])
    assert missing[2]['heap_interval']['coverage_complete'] is False


def test_mixed_trace_drain_is_resplit_by_actual_session_source():
    from arrival_trace import split_session_archive
    raw=[dict(reserved=g,ssrc=s,kind=13) for g,s in [(1,11),(3,33),(1,11),(3,33)]]
    split=split_session_archive(raw,{1:'old',3:'new'})
    assert {r['ssrc'] for r in split['old']}=={11} and {r['ssrc'] for r in split['new']}=={33}
    assert len(split['old'])+len(split['new'])==len(raw)


def test_uplink_only_fault_owns_both_counters_but_changes_only_ingress(monkeypatch,tmp_path):
    import netem
    n=netem.Netem('192.168.4.29',['loss','0%'],'uplink',state=tmp_path/'tc.json',account_both=True)
    created=netem.commands(n.board_ip,n.args,'both')
    assert any(cmd[:2]==['qdisc','add'] and 'wlp8s0' in cmd and 'netem' in cmd for cmd in created)
    calls=[]
    def command(*args,**kwargs):
        calls.append(args)
        return '[]'
    monkeypatch.setattr(netem,'command',command)
    n.update(['loss','100%'])
    changes=[c for c in calls if c[:2]==('qdisc','change')]
    assert len(changes)==1 and 'ifb0' in changes[0] and 'wlp8s0' not in changes[0]
