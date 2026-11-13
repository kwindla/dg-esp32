from echoear_wifi_correlation import correlate


def fixture():
    obs=dict(rows=[dict(sequence=1,counter_begin_us=0,sample_begin_us=90000,sample_end_us=100000,
        ps_error=0,ps_mode=0,ap_error=0,rssi_dbm=-40,primary_channel=6,secondary_channel=0,
        bssid='00:01:02:03:04:05',tx_done_success=50,tx_done_failed=1)],
        callback_activity_observed=True,retained_history_not_overwritten=True,available=True)
    packets=[dict(pc_id='pc',ssrc=1,seq=i,timestamp=i*960,observed_ms=t)
             for i,t in enumerate([1000,7000,7080,7100])]
    trace=[dict(kind=12,ssrc=1,seq=i,timestamp=i*960,device_us=t,reserved=1)
           for i,t in enumerate([1000,20000,40000,60000])]
    window=dict(index=1,pc_id='pc',generation=1,start_s=7.01,end_s=7.1,trace_overwritten=0,receive_capture_status=dict(post_window_reports=1,truncated=False))
    return obs,[window],packets,{1:trace}


def test_identity_join_needs_no_clock_offset_and_never_invents_retry_values():
    r=correlate(*fixture());p=r['phases'][0];tail=p['tail_pairs'][0]
    assert p['over60']==1 and p['receive_intervals']==2
    assert tail['right_send_sample']['rssi_dbm']==-40
    assert tail['right_send_sample']['tx_done_failed']==1
    assert tail['tx_retries'] is None and r['availability']['tx_retries'] is None
    assert not r['qualification'] and not r['scoring_performed'] and not r['causal_attribution']
    assert p['all_retained_pairs_matched'] and p['all_right_send_bins_matched']


def test_missing_send_identity_and_wrong_generation_remain_unmatched():
    o,w,p,t=fixture();t[1][2]['reserved']=2
    r=correlate(o,w,p,t)['phases'][0]
    assert not r['all_retained_pairs_matched'] and r['foreign_generation_send_records']==1
    assert r['tail_pairs'][0]['device_after_us'] is None
    assert r['tail_pairs'][0]['right_send_sample'] is None


def test_missing_sample_and_api_error_do_not_borrow_prior_rssi_or_ps():
    o,w,p,t=fixture();o['rows'][0]['sample_end_us']=30000
    assert not correlate(o,w,p,t)['phases'][0]['all_right_send_bins_matched']
    o,w,p,t=fixture();o['rows'][0].update(ap_error=-1,ps_error=-1,rssi_dbm=None,ps_mode=None,
                                       primary_channel=None,secondary_channel=None,bssid=None)
    r=correlate(o,w,p,t)
    assert r['observed_states']['rssi_min'] is None
    assert r['phases'][0]['tail_pairs'][0]['right_send_sample']['ps_mode'] is None


def test_receive_reordering_with_intervening_sends_does_not_invent_source_stall():
    o,w,p,t=fixture();p[2]['observed_ms']=7100;p[3]['observed_ms']=7080
    r=correlate(o,w,p,t)['phases'][0]
    assert r['tail_pairs'][0]['device_intervening_sends']==1
    assert r['tail_pairs'][0]['device_max_adjacent_gap_us']==20000
    assert 'after ESP socket send-return' in r['tail_pairs'][0]['attribution']


def test_bin_boundary_has_one_right_send_membership_but_preserves_coarse_overlap():
    o,w,p,t=fixture();first=o['rows'][0];first['sample_end_us']=40000
    second=dict(first,sequence=2,counter_begin_us=40000,sample_begin_us=80000,sample_end_us=100000)
    o['rows'].append(second)
    r=correlate(o,w,p,t);tail=r['phases'][0]['tail_pairs'][0]
    assert tail['right_send_sample']['sequence']==1
    assert sum(b['receive_intervals'] for b in r['bins'])==r['phases'][0]['right_send_wifi_bins_matched']


def test_partial_receiver_or_unknown_truncation_is_explicitly_inconclusive():
    o,w,p,t=fixture();p=p[:-1]
    r=correlate(o,w,p,t)
    assert not r['phases'][0]['receive_evidence_available']
    assert not r['phases'][0]['correlation_available']
    assert r['status']=='INCONCLUSIVE_EVIDENCE'
    o,w,p,t=fixture();w[0]['receive_capture_status']['truncated']=None
    assert not correlate(o,w,p,t)['phases'][0]['correlation_available']


def test_three_complete_windows_are_observations_never_qualification():
    o,w,p,t=fixture()
    windows=[dict(w[0],index=i) for i in (1,2,3)]
    r=correlate(o,windows,p,{i:t[1] for i in (1,2,3)})
    assert r['correlation_available'] and r['status']=='DESCRIPTIVE_OBSERVATION'
    assert not r['qualification']
    for key,value in [('trace_overwritten',1),('mid_window_commands',[dict(command='stats')])]:
        changed=[dict(x) for x in windows];changed[0][key]=value
        assert not correlate(o,changed,p,{i:t[1] for i in (1,2,3)})['correlation_available']
    o['retained_history_not_overwritten']=False
    assert not correlate(o,windows,p,{i:t[1] for i in (1,2,3)})['correlation_available']


def test_first_udp_callback_join_uses_same_identity_and_retains_missing_boundary():
    o,w,p,t=fixture();callbacks=[dict(row) for row in p]
    callbacks[2]['observed_ms']-=10
    r=correlate(o,w,p,t,callbacks)['phases'][0]['tail_pairs'][0]
    assert r['udp_callback_gap_ms']==70 and r['authenticated_vs_callback_spacing_change_ms']==10
    callbacks.pop(2)
    assert correlate(o,w,p,t,callbacks)['phases'][0]['tail_pairs'][0]['udp_callback_gap_ms'] is None
