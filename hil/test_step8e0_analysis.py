import step8e0_analysis as analysis


def test_callback_audit_includes_unplayed_frames_and_requires_observation_tail():
    auth={1:dict(device_us=1000),2:dict(device_us=2000),3:dict(device_us=500000)}
    callbacks={1:dict(device_us=3000)}
    result=analysis.callback_delivery_summary(auth,callbacks,[dict(device_us=400000)],
                                             0,10_000_000,0,True)
    assert result['authenticated']==2 and result['without_callback']==1
    result=analysis.callback_delivery_summary(auth,callbacks,[dict(device_us=11000)],
                                             0,10_000_000,0,True)
    assert result['unmatched_in_retained_trace']==1 and result['without_callback'] is None
    assert not result['observable']


def test_playout_rate_excludes_setup_and_teardown_and_distinguishes_drop_kind():
    rows=[dict(device_us=t,kind=k,flags=f) for t,k,f in
          [(0,5,0),(1000,5,0),(2000,6,0),(3000,7,0),(3000,7,1),(4000,5,0)]]
    result=analysis.playout_trace_summary(rows,2_000_000,4_000_000,1_000_000)
    assert result==dict(rebuffers=1,drift_markers=1,explicit_playout_drop_records=1)


def test_probe_and_voice_mailbox_overflow_is_not_air_loss(tmp_path,monkeypatch):
    import json
    arm=tmp_path/'arm';arm.mkdir();rtp=arm/'rtp';rtp.mkdir()
    def write(name,value):
        (arm/name).write_text(json.dumps(value))
    def ledger(name,records):
        (arm/name).write_text(''.join(json.dumps(r)+'\n' for r in records))
    packets=[dict(ssrc=1,seq=s,timestamp=s*960,send_ms=t,pc_id='test') for s,t in [(6,1),(7,21),(8,41)]]
    ledger('rtp/downlink.jsonl',packets);ledger('rtp/rtp.jsonl',[])
    ledger('rtp/ice-pairs.jsonl',[dict(pairs=[dict(remote_type='host',remote=['192.0.2.1',50000])])])
    ledger('host.jsonl',[dict(kind='send',path='esp-0',send_ns=t) for t in [0,500_000_000]])
    before=dict(device_us=0,udp_drop=0,udp_memerr=0,ip_drop=0,ip_memerr=0,udp_recvmbox_full=0,
                udp_recvmbox_ports=[dict(local_port=47000,full=0),dict(local_port=50000,full=0)])
    after=dict(before,device_us=500000,udp_recvmbox_full=2,udp_recvmbox_last_us=130000,
               udp_recvmbox_ports=[dict(local_port=47000,full=1),dict(local_port=50000,full=1)])
    write('device-before.json',before);write('device-after.json',after);write('device-samples.json',[])
    (tmp_path/'serial.log').write_text('')
    rows=[dict(kind=k,ssrc=1,seq=s,timestamp=s*960,device_us=t,reserved=0,aux=0,value=0,flags=0)
          for s,t in [(6,1000),(8,130000)] for k in [1,2]]
    rows.append(dict(kind=10,ssrc=1,seq=8,timestamp=8*960,device_us=130000,reserved=0,aux=6,value=1,flags=0))
    monkeypatch.setattr(analysis,'trace_rows',lambda path:rows)
    paths={'esp-0':dict(downlink=dict(loss_events=[dict(time_ns=21_000_000,packets=1)])),
           'esp-1':dict(clock_mapping=dict(offset_ns=0))}
    event=analysis.attribute_rtp(arm,paths)['rtp_loss_events'][0]
    assert event['probe_matches']['esp-0']
    assert event['voice_port_overflow_bracket']=={'50000':1}
    assert event['category']=='UDP receive mailbox overflow'
    assert event['preceding_socket_stalls'][0]['gap_ms']==129


def test_rtp_absence_counts_packets_not_retransmission_attempts():
    records=[dict(ssrc=1,seq=7,timestamp=100,send_ms=1),dict(ssrc=1,seq=7,timestamp=100,send_ms=2),
             dict(ssrc=1,seq=7,timestamp=200,send_ms=3),dict(ssrc=2,seq=7,timestamp=100,send_ms=4)]
    result=analysis.unique_rtp_sends(records)
    assert len(result)==3
    assert result[0]['send_ms']==1
    assert result[1]['timestamp']==200 and result[2]['ssrc']==2


def test_loss_window_boundary_is_inclusive():
    assert analysis.near([100_000_000],0)
    assert not analysis.near([100_000_001],0)
    assert analysis.near([0,200_000_000],100_000_000)
    assert not analysis.near([],1)


def test_long_calls_keep_part_and_inclusive_duration():
    result=analysis.slow_call_summary([dict(kind=11,aux=1,value=120000,device_us=200000),
                                       dict(kind=11,aux=11,value=40000,device_us=200000)])
    assert result['parts']['send_audio']['calls_ge_120ms']==1
    assert result['parts']['socket_select']['calls_ge_120ms']==0
    assert result['parts']['socket_select']['wall_ms']['max']==40


def test_stack_counter_wrap_retains_sample_interval():
    before=dict(device_us=1000,lwip_counter_bits=16,udp_drop=65535,udp_memerr=0,ip_drop=2,ip_memerr=0,udp_recvmbox_full=5)
    after=dict(before,device_us=3000,udp_drop=1,udp_recvmbox_full=6)
    result=analysis.stack_delta([dict(stats=before),dict(stats=after)],2_000_000,0)
    assert result['deltas']['udp_drop']==2
    assert result['deltas']['udp_recvmbox_full']==1
    assert result['interval_us']==[1000,3000]
    assert not analysis.stack_delta([dict(stats=before),dict(stats=after)],4_000_000,0)['available']


def test_mailbox_bracket_preserves_distinct_probe_and_voice_ports():
    before=dict(device_us=0,udp_drop=0,udp_memerr=0,ip_drop=0,ip_memerr=0,udp_recvmbox_full=3,
                udp_recvmbox_ports=[dict(local_port=47000,full=3),dict(local_port=50000,full=0)])
    after=dict(before,device_us=1_000_000,udp_recvmbox_full=8,
               udp_recvmbox_ports=[dict(local_port=47000,full=3),dict(local_port=50000,full=5)])
    result=analysis.stack_delta([dict(stats=before),dict(stats=after)],500_000_000,0)
    assert result['per_port_deltas']=={'47000':0,'50000':5}


def test_uplink_wrap_reorder_and_expected_span():
    rows=[dict(pc_id='test',ssrc=1,seq=s,timestamp=i*960,arrival_ms=t) for i,(s,t) in enumerate([(65534,0),(0,40),(65535,20),(2,80)])]
    result=analysis.audit_uplink(rows,0,100_000_000,{})
    stream=result['streams'][0]
    assert stream['expected_sequence_span']==5
    assert stream['received_unique']==4
    assert stream['lost_in_span']==1
    assert stream['burst_histogram']['1']==1
    assert result['missing_packets_by_category']=={'unexplained':1}


def test_late_frames_keep_censored_callback_and_inclusive_stall():
    events=[dict(boundary='native jitter cache',value=10,authenticated_us=1000,device_us=2000,callback_us=3000),
            dict(boundary='native jitter cache',value=30,authenticated_us=4000,device_us=5000,callback_us=None)]
    result=analysis.late_frame_summary(events,[dict(kind=8,device_us=3500,value=2000)],[])
    assert result['frames']==2 and result['callback_observed']==1
    assert result['auth_to_callback_ms']['max']==2
    assert result['callback_late_after_plc_ms']['max']==1
    assert result['slow_loop_overlap_frames']==1


def test_wifi_dump_pressure_is_not_retry_loss():
    text='I (1) wifi: BUFFER:\nI (2) wifi: 1 16 16 100 100 7 0\nI (3) wifi: LMAC TX:\nI (4) wifi: 200 0 0 0 0\nI (5) wifi: LMAC RX:\nOK #2 wifi-stats\n'
    assert analysis.wifi_dump_blocks(text)==[text]
    parsed=analysis.parse_wifi_dump(text)
    assert parsed['buffers']['1']['fail_fc']==7
    assert parsed['buffers']['1']['fail_oom']==0
    assert parsed['lmac_tx'][0]==dict(tx_all=200,lrc=0,src=0,age=0,to=0)


def test_uplink_window_uses_monotonic_but_jitter_uses_arrival_clock():
    rows=[dict(pc_id='test',ssrc=1,seq=s,timestamp=s*960,arrival_ms=4_000_000+s*20,observed_ms=100+s*25) for s in (0,2)]
    result=analysis.audit_uplink(rows,100_000_000,200_000_000,{})
    stream=result['streams'][0]
    assert stream['expected_sequence_span']==3 and stream['lost_in_span']==1
    assert stream['max_gap_ms']==40
    assert stream['jitter_abs_transit_difference_ms']['max']==0
    assert stream['events'][0]['time_ns']==125_000_000


def test_probe_local_failure_is_tx_association_not_air():
    rows=[dict(pc_id='test',ssrc=1,seq=s,timestamp=s*960,arrival_ms=s*20) for s in (0,2)]
    paths={'esp-0':dict(clock_mapping=dict(offset_ns=0),uplink=dict(local_send_failures=[dict(arrival_us=20000)],wire_loss_events=[]))}
    event=analysis.audit_uplink(rows,0,100_000_000,paths)['streams'][0]['events'][0]
    assert event['category']=='TX side'
    assert event['local_probe_send_failure_nearby']
    assert not event['probe_matches']['esp-0']


def test_esp_and_reference_loss_does_not_claim_air_or_tx_queue():
    rows=[dict(pc_id='test',ssrc=1,seq=s,timestamp=s*960,arrival_ms=s*20) for s in (0,2)]
    paths={name:dict(uplink=dict(wire_loss_events=[dict(time_ns=20_000_000,packets=1)])) for name in ('esp-0','spark-0')}
    event=analysis.audit_uplink(rows,0,100_000_000,paths)['streams'][0]['events'][0]
    assert event['category']=='ESP hop (radio or TX queue)'
    assert event['reference_client_loss_nearby']
    assert not event['local_probe_send_failure_nearby']


def test_wifi_header_survives_interleaved_audio_logger():
    text='I (1) wifi:I (2) dg-audio: MIC_GATE\n BUFFER:\nI (3) wifi: t max alloc total_alloc total_free fail_fc fail_oom\nI (4) wifi: 1 16 16 100 100 7 0\nOK #2 wifi-stats\n'
    assert analysis.parse_wifi_dump(text)['buffers']['1']['fail_fc']==7
    assert len(analysis.wifi_dump_blocks(text))==1
