from step8e2a_analysis import receive_audit


def r(kind,stamp,us,seq=10,flags=0,tick=0):
    return dict(kind=kind,timestamp=stamp,device_us=us,seq=seq,ssrc=7,reserved=1,
                flags=flags,value=tick,aux=1,bytes=20)


def test_authoritative_ingress_does_not_inherit_native_hold_or_duplicate_insert():
    rows=[r(2,960,1000),r(13,960,1100),r(3,0,190000),r(14,960,101000)]
    rows[2]['value']=20
    audit=receive_audit(rows,[])
    assert audit['auth_to_ingress_ms']['p50']==.1
    assert audit['native_shadow_hold_ms']['p50']==189
    assert audit['ingress_to_render_completion_ms']['p50']==99.9
    assert audit['ingress_unmatched_authenticated']==0
    assert audit['render_kinds']=={0:1}


def test_isolated_loss_and_stall_release_are_distinct_censuses():
    sent=[dict(ssrc=7,timestamp=i*960,seq=10+i,pc_id='p',send_ms=i*20) for i in range(3)]
    rows=[r(13,0,1000),r(13,1920,41000,12),r(14,0,101000,tick=0),
          r(14,960,121000,flags=1,tick=1),r(14,1920,141000,tick=2)]
    audit=receive_audit(rows,sent)
    assert audit['isolated_loss_count']==1
    assert audit['missing_attribution']=={'absent at authoritative ingress':1}
    rows.append(r(13,960,150000,11))
    audit=receive_audit(rows,sent)
    assert audit['isolated_loss_count']==0
    assert audit['missing_attribution']=={'late at authoritative ingress':1}


def test_lost_sender_packets_do_not_turn_receive_gaps_into_sender_pacing():
    sent=[dict(ssrc=7,timestamp=i*960,seq=10+i,pc_id='p',send_ms=i*20) for i in range(8)]
    rows=[r(1,0,1000),r(1,6720,141000,17)]
    stall=receive_audit(rows,sent)['stalls_over_reserve'][0]
    assert stall['sender_gap_ms']==140 and stall['sender_max_adjacent_gap_ms']==20
    assert stall['basis']=='loss burst (sent, never at wire)'
    sent=sent[:1]+sent[-1:]
    # Retransmission observations cannot replace first successful-send timing.
    sent.append(dict(sent[0],send_ms=150))
    stall=receive_audit(rows,sent)['stalls_over_reserve'][0]
    assert stall['sender_max_adjacent_gap_ms']==140 and stall['basis']=='server pacing'


def test_resumption_prefill_zeros_are_not_network_missing_or_short_burst():
    rows=[r(16,960,100000,seq=0),r(14,960,101000,seq=0,flags=2,tick=20),
          r(14,960,121000,seq=0,flags=2,tick=21),r(13,960,99000),
          r(14,960,141000,seq=0,tick=22)]
    audit=receive_audit(rows,[])
    assert audit['prefill_zero_decisions']==2
    assert audit['missing_attribution']=={}
    assert audit['short_burst_cases']==[]
    assert audit['render_kinds']=={2:2,0:1}


def test_sample_counter_deltas_are_unavailable_across_device_epochs(tmp_path):
    import json
    from step8e2a_analysis import analyze
    counters=dict(media_advance=10,decoded_packets=10,missing_slots=0,drift_drop=0,
        latency_trim=0,device_silence_blocks=0,decode_errors=0,render_decisions=10)
    clock=dict(device_generation=1,completed_samples=3200,submitted_samples=3840,
        device_silence_samples=960,partial_remainder=0)
    before=dict(playout_schema=2,playout=dict(**counters,clock=clock))
    after=dict(playout_schema=2,playout=dict(**counters,clock=dict(clock,device_generation=3,completed_samples=320)))
    path=tmp_path/'result.json'
    path.write_text(json.dumps(dict(before=dict(perf=before),after=dict(perf=after))))
    audit=analyze(tmp_path)['device_conservation']
    assert not audit['available']
    assert audit['completed_delta'] is audit['submitted_delta'] is audit['silence_samples_delta'] is None
    after['playout']['clock']['device_generation']=1
    after['playout']['clock']['completed_samples']=3520
    path.write_text(json.dumps(dict(before=dict(perf=before),after=dict(perf=after))))
    audit=analyze(tmp_path)['device_conservation']
    assert audit['available'] and audit['completed_delta']==320


def test_tick_uniqueness_is_scoped_to_stream_and_device_generation():
    rows=[r(14,0,100,tick=0),r(14,960,200,tick=0),r(14,1920,300,tick=0)]
    rows[-1]['aux']=2
    assert receive_audit(rows,[])['duplicate_render_ticks']==1


def test_cpu_tail_guard_catches_a_regression_hidden_by_boot_medians(tmp_path):
    import json
    from step8e2a_analysis import cpu_decision
    def campaign(spike=False):
        runs=[]
        for pair in range(1,5):
            for arm in ('A','B'):
                cpu=12 if spike and pair==1 and arm=='B' else 2
                runs.append(dict(pair=pair,arm=arm,summary=dict(stimulus_complete=True,
                    cpu_windows=[dict(tasks=[dict(name='peer-loop',cpu=cpu)]) for _ in range(2)])))
        return dict(completed=True,name='test',runs=runs)
    aa=tmp_path/'aa.json';ab=tmp_path/'ab.json'
    aa.write_text(json.dumps(campaign()));ab.write_text(json.dumps(campaign(True)))
    result=cpu_decision(aa,ab)
    assert not result['passed']
    row=result['tasks']['peer-loop']
    assert row['delta_pp']==0 and row['tail_delta_pp']==10 and not row['passed']
    ab.write_text(json.dumps(campaign()))
    assert cpu_decision(aa,ab)['passed']


def test_six_slot_burst_uses_five_plc_then_one_zero_with_on_time_survivors():
    sent=[dict(ssrc=7,timestamp=i*960,seq=10+i,pc_id='p',send_ms=i*20) for i in range(8)]
    rows=[r(13,0,1000),r(13,6720,141000,17),r(14,0,101000,tick=0)]
    rows += [r(14,i*960,101000+i*20000,flags=1 if i<=5 else 2,tick=i) for i in range(1,7)]
    rows.append(r(14,6720,241000,tick=7))
    audit=receive_audit(rows,sent)
    assert audit['isolated_loss_count']==0
    assert audit['short_burst_cases']==[dict(first_timestamp=960,ssrc=7,slots=6,plc_calls=5,post_plc_zeros=1,passed=True)]


def test_cpu_worker_roles_survive_startup_retry_without_combining_samples(tmp_path):
    import json,pytest
    from step8e2a_analysis import cpu_decision
    runs=[]
    for pair in range(1,5):
        for arm in ('A','B'):
            start=27 if pair==1 and arm=='A' else 17
            tasks=[dict(name=f'pipecat#{start}',core=1,priority=5,cpu=.5),
                   dict(name=f'pipecat#{start+1}',core=1,priority=5,cpu=.01)]
            runs.append(dict(pair=pair,arm=arm,summary=dict(stimulus_complete=True,
                cpu_windows=[dict(tasks=tasks),dict(tasks=tasks)])))
    p=tmp_path/'cpu.json';p.write_text(json.dumps(dict(completed=True,runs=runs)))
    d=cpu_decision(p,p)
    assert d['passed'] and len(d['tasks'])==2
    assert d['tasks']['pipecat-event-loop']['boot_median_A']==.5
    assert d['tasks']['pipecat-request-timer']['boot_median_A']==.01
    assert d['worker_identities']['aa'][0]['raw_to_role']=={
        'pipecat#27':'pipecat-event-loop','pipecat#28':'pipecat-request-timer'}
    # A worker replacement during one boot is not a steady paired window.
    runs[0]['summary']['cpu_windows'][1]=dict(tasks=[dict(tasks[0],name='pipecat#29'),dict(tasks[1],name='pipecat#30')])
    p.write_text(json.dumps(dict(completed=True,runs=runs)))
    with pytest.raises(ValueError,match='changed within CPU boot'):cpu_decision(p,p)


def test_late_wire_identities_distinguish_delay_stall_from_loss_burst():
    sent=[dict(ssrc=7,timestamp=i*960,seq=10+i,pc_id='p',send_ms=i*20) for i in range(8)]
    rows=[r(1,0,1000),r(1,6720,141000,17)]
    loss=receive_audit(rows,sent,tc_model=['loss','gemodel'])['stalls_over_reserve'][0]
    assert loss['intermediate_sent']==6 and loss['intermediate_at_wire']==0
    assert loss['tc_crosscheck']=='consistent with loss model'
    rows.extend(r(1,i*960,142000+i,10+i) for i in range(1,7))
    delay=receive_audit(rows,sent,tc_model=['loss','0%'])['stalls_over_reserve'][0]
    assert delay['basis']=='delay stall (arrived late)'
    assert delay['intermediate_at_wire']==6
    assert delay['tc_crosscheck']=='delay observed without imposed delay'


def test_explicit_prefill_after_120ms_and_device_epoch_boundary_join():
    rows=[r(17,5,1),r(14,0,120000,flags=7,tick=0),r(14,0,140000,tick=1),
          r(17,6,150000),r(14,960,160000,tick=0)]
    rows[0]['value']=rows[3]['value']=0
    audit=receive_audit(rows,[])
    assert audit['prefill_zero_decisions']==1 and audit['prefill_basis']=='explicit kind'
    assert audit['duplicate_render_ticks']==0 and audit['device_epochs']==[5,6]
    assert audit['renders_without_device_epoch']==0


def test_consecutive_wire_identities_with_regular_sender_cadence_are_delay():
    sent=[dict(ssrc=7,timestamp=i*960,seq=10+i,pc_id='p',send_ms=i*20) for i in range(2)]
    rows=[r(1,0,1000),r(1,960,141000,11)]
    stall=receive_audit(rows,sent)['stalls_over_reserve'][0]
    assert stall['basis']=='delay stall (arrived late)'
    assert stall['intermediate_sent']==stall['intermediate_at_wire']==0
