import pytest
from step8c_analysis import bootstrap_difference, compare, compare_cpu, compare_network, compare_quality_network, compare_network_screen, compare_voice, cpu_windows, enqueue_interval, summarize_cpu, uplink_timestamp_check, uplink_payload_summary, quality_cpu_guard


def test_dtx_cpu_stimulus_requires_continuous_encoding_and_downlink(tmp_path):
    import json
    arm=tmp_path/'webrtc-ethernet';arm.mkdir()
    metadata=arm/'metadata.json';metadata.write_text(json.dumps(dict(options=['dtx=1'])))
    first=dict(device_us=0,sent=0,received=0,encode_frames=0,tasks=[])
    last=dict(device_us=30_000_000,sent=5,received=1500,encode_frames=1500,tasks=[],encode_p50_us=1,
              encode_p95_us=1,enqueue_send_mean_us=1,enqueue_send_max_us=1,heap={},peer_profile={})
    snapshots=arm/'cpu-snapshots.json'
    snapshots.write_text(json.dumps([first,last]))
    assert summarize_cpu(tmp_path)['stimulus_complete']
    metadata.write_text(json.dumps(dict(options=[])))
    assert not summarize_cpu(tmp_path)['stimulus_complete']
    metadata.write_text(json.dumps(dict(options=['dtx=1'])))
    for key in ('encode_frames','received'):
        snapshots.write_text(json.dumps([first,dict(last,**{key:0})]))
        assert not summarize_cpu(tmp_path)['stimulus_complete']
    snapshots.write_text(json.dumps([first,dict(last,send_errors=1500)]))
    assert not summarize_cpu(tmp_path)['stimulus_complete']  # Failed sends are not DTX suppression.

    # Production defaults are measured through PERF, even with no runtime
    # frame option. A mismatched claimed option cannot qualify the stimulus.
    metadata.write_text(json.dumps(dict(options=[])))
    snapshots.write_text(json.dumps([first,dict(last,sent=750,encode_frames=750,
                                                uplink_frame_ms=40,opus_dtx=False)]))
    assert summarize_cpu(tmp_path)['stimulus_complete']
    metadata.write_text(json.dumps(dict(options=['frame-ms=20'])))
    assert not summarize_cpu(tmp_path)['stimulus_complete']


def test_enqueue_latency_removes_warmup_and_refuses_ambiguous_send_counts():
    rows=[dict(sent=20,send_errors=0,enqueue_send_mean_us=100),dict(sent=120,send_errors=0,enqueue_send_mean_us=50)]
    result=enqueue_interval(rows)
    assert result['mean_us_estimate']==40 and result['packets']==100
    assert result['rounding_error_bound_us']==1.4
    assert enqueue_interval([rows[0],dict(rows[1],send_errors=1)]) is None


def test_40ms_RTP_timeline_preserves_wrap_reordering_duplicates_and_missing_PCM():
    rows=[dict(pc_id='test',ssrc=1,seq=s,timestamp=t) for s,t in
          [(65535,2**32-1920),(1,1920),(0,0),(0,0),(2,4800)]]
    assert uplink_timestamp_check(rows,40)['verified']
    rows[-1]['timestamp']=2880
    result=uplink_timestamp_check(rows,40)
    assert result['verified'] is False and result['compressed_timeline_intervals']==1
    assert uplink_timestamp_check([],20)['verified'] is None


def test_payload_rate_keeps_window_duplicates_and_missing_observation_distinct():
    rows=[dict(pc_id='test',ssrc=1,seq=s,timestamp=t,observed_ms=at,payload_bytes=size)
          for s,t,at,size in [(65535,0,0,9),(0,960,20,1),(0,960,21,1),(1,1920,2000,9)]]
    result=uplink_payload_summary(rows,0,1_000_000_000)
    assert result['observable'] and result['received_unique_packets']==2
    assert result['codec_payload_bytes_per_second']==10
    rows[1]['payload_bytes']=None
    result=uplink_payload_summary(rows,0,1_000_000_000)
    assert not result['observable'] and result['codec_payload_bytes_per_second'] is None


def test_cpu_runtime_wrap_and_recreated_task():
    def snapshot(stamp, runtime, task_id=1):
        return dict(device_us=stamp, sent=0, received=0, tasks=[dict(id=task_id, name='peer-loop',
                    core=0, priority=6, runtime_us=runtime)])
    result = cpu_windows([snapshot(1, 2**32-100), snapshot(30_000_001, 14_999_900)])
    assert result[0]['tasks'][0]['cpu'] == 50
    result = cpu_windows([snapshot(1, 0), snapshot(30_000_001, 15_000_000, 2)])
    assert result[0]['tasks'] == []


def test_duplicate_worker_names_do_not_hide_one_workers_regression():
    def population(busy):
        snapshots=[dict(device_us=i*30_000_000,sent=i*1500,received=i*1500,tasks=[
            dict(id=17,name='pipecat',core=1,priority=5,runtime_us=int(i*busy*300_000)),
            dict(id=18,name='pipecat',core=1,priority=5,runtime_us=0)]) for i in range(9)]
        windows=cpu_windows(snapshots)
        return [dict(tasks={t['name']:{} for t in windows[0]['tasks']},cpu_windows=windows)]
    a,b,x,y=map(population,(1,3,1,1.5))
    result=compare_cpu(a,b,x,y)
    guard=quality_cpu_guard(result)
    assert set(result['cpu'])=={'pipecat#17','pipecat#18'}
    assert guard['CPU_regressions_beyond_AA']==['pipecat#17']
    assert not guard['old_any_task_CPU_guard']
    assert guard['quality_adoption_CPU_guard']  # Exactly 2 pp is immaterial.


def test_any_task_guard_requires_coverage_and_treats_less_idle_as_regression():
    def population(idle,other=True):
        tasks=[dict(name='IDLE0',cpu=idle)]+([dict(name='worker',cpu=1)] if other else [])
        return [dict(tasks={t['name']:{} for t in tasks},cpu_windows=[dict(tasks=tasks)]*8)]
    result=compare_cpu(population(40),population(38),population(40),population(41))
    assert quality_cpu_guard(result)['CPU_regressions_beyond_AA']==['IDLE0']
    result=compare_cpu(population(40),population(40,False),population(40),population(41))
    assert result['cpu_tasks_with_incomplete_coverage']==['worker']
    assert not quality_cpu_guard(result)['quality_adoption_CPU_guard']


def test_materiality_guard_boundaries_and_larger_matched_spread():
    def result(idle,delta,spread):
        return dict(cpu={'task':dict(p50_reduction=delta, idle_task=idle,AA_p50_spread=spread)},
                    cpu_windows_sufficient=True,cpu_stimulus_complete=True,cpu_task_coverage_complete=True)
    for idle,delta,spread in [(False,-2,0.1),(True,5,0.1),(False,-3,3),(True,6,6)]:
        assert quality_cpu_guard(result(idle,delta,spread))['quality_adoption_CPU_guard']
    for idle,delta,spread in [(False,-2.01,0.1),(True,5.01,0.1),(False,-3.01,3),(True,6.01,6)]:
        assert not quality_cpu_guard(result(idle,delta,spread))['quality_adoption_CPU_guard']
    inherited=result(False,-2.5,4)
    inherited['cpu']['task']['AA_observed_p50_spread']=.5
    assert not quality_cpu_guard(inherited)['quality_adoption_CPU_guard']
    inherited['cpu']['task']['matched_AA_p50_spread']=3
    assert quality_cpu_guard(inherited)['quality_adoption_CPU_guard']


def test_bootstrap_uses_blocks_and_is_reproducible():
    a, b = [1, 2, 3, 4], [2, 3, 4, 5]
    first = bootstrap_difference(a, b)
    assert first == bootstrap_difference(a, b)
    assert first['mean_difference'] == 1
    assert first['blocks_A'] == first['blocks_B'] == 4
    assert first['ci95'][0] < 1 < first['ci95'][1]


def test_user_short_network_screen_keeps_variation_and_missing_rates_explicit():
    def row(value):
        return dict(path='retained',minutes=310/60,plc_per_minute=value,uplink_loss_rate=0,
                    rebuffers_per_hour=0,conceal_or_drift_per_minute=value)
    assert compare_network_screen(row(9),row(3),row(8),row(10))['network_acceptable']
    assert not compare_network_screen(row(9),row(12),row(8),row(10))['network_acceptable']
    assert not compare_network_screen(row(9),dict(row(3),uplink_loss_rate=None),row(8),row(10))['network_acceptable']


def test_voice_guard_user_latency_boundary_and_additional_transcript_error():
    def voice(value):return dict(passed=True,runs=[dict(latency_ms=dict(audible_reply=value))]*5)
    def stt(score):return dict(runs=[dict(word_accuracy=score,transcript='retained')]*5)
    assert compare_voice(voice(800),voice(900),stt(1),stt(1))['passed']
    assert not compare_voice(voice(800),voice(901),stt(1),stt(1))['passed']
    assert not compare_voice(voice(800),voice(850),stt(1),stt(.875))['passed']
    assert not compare_voice(voice(800),dict(voice(850),runs=[]),stt(1),stt(1))['passed']


def test_short_population_cannot_pass_network_qualification():
    def row(cpu, plc):
        return dict(minutes=10, tasks={'peer-loop': {}}, cpu_windows=[dict(tasks=[dict(name='peer-loop', cpu=cpu)])]*8,
                    plc_per_minute=plc, uplink_loss_rate=0, rebuffers_per_hour=0)
    aa, control, candidate = [row(40, 2)], [row(41, 2)], [row(30, 1)]
    result = compare(control, candidate, aa, control)
    assert result['cpu']['peer-loop']['measured_win']
    assert result['cpu']['peer-loop']['AA_p50_spread'] == 1
    assert result['cpu_windows_sufficient']
    assert not result['network_duration_sufficient']
    assert not result['network_acceptable']


def test_degenerate_single_block_AA_cannot_qualify_long_AB():
    row=dict(minutes=10,tasks={'peer-loop':{}},cpu_windows=[dict(tasks=[dict(name='peer-loop',cpu=40)])]*8,
             plc_per_minute=1,uplink_loss_rate=0,rebuffers_per_hour=0)
    result=compare([row]*6,[row]*6,[row],[row])
    assert result['network_duration_sufficient']
    assert not result['network_AA_blocks_sufficient']
    assert not result['network_acceptable']


def test_suppressed_uplink_is_unobservable_instead_of_zero_loss():
    row=dict(minutes=10,plc_per_minute=1,uplink_loss_rate=0,rebuffers_per_hour=0)
    candidate=dict(row,uplink_loss_rate=None)
    result=compare_network([row]*6,[candidate]*6,[row]*2,[row]*2)
    assert result['network_duration_sufficient']
    assert not result['network_rates_observable']
    assert not result['network']['uplink_loss_rate']['observable']
    assert not result['network_acceptable']


def test_idle_cpu_reduction_is_not_an_efficiency_win():
    def row(idle):
        return dict(tasks={'IDLE0':{}},cpu_windows=[dict(tasks=[dict(name='IDLE0',cpu=idle)])]*8)
    result=compare_cpu([row(40)],[row(30)],[row(40)],[row(41)])
    assert not result['cpu']['IDLE0']['measured_win']
    result=compare_cpu([row(40)],[row(50)],[row(40)],[row(41)])
    assert result['cpu']['IDLE0']['measured_win']


def test_prior_AA_spread_is_not_lowered_for_later_candidate():
    def row(cpu):
        return dict(tasks={'audio-tx':{}},cpu_windows=[dict(tasks=[dict(name='audio-tx',cpu=cpu)])]*8)
    result=compare_cpu([row(40)],[row(38)],[row(40)],[row(41)],spread_floor={'audio-tx':3})
    assert result['cpu']['audio-tx']['AA_observed_p50_spread']==1
    assert result['cpu']['audio-tx']['AA_p50_spread']==3
    assert not result['cpu']['audio-tx']['measured_win']


def test_regression_must_exceed_AA_interval():
    def population(cpu, plc):
        row = dict(minutes=10, tasks={'peer-loop': {}}, cpu_windows=[dict(tasks=[dict(name='peer-loop', cpu=cpu)])]*8,
                   plc_per_minute=plc, uplink_loss_rate=0, rebuffers_per_hour=0)
        return [row]*6
    result = compare(population(40, 2), population(30, 4), population(40, 2), population(40, 2.5))
    assert result['network_duration_sufficient']
    assert result['network']['plc_per_minute']['worse_beyond_AA']
    assert not result['network_acceptable']


def test_cache_callback_win_cannot_hide_displaced_output_damage_or_censoring():
    def population(plc,drift,missing):
        row=dict(minutes=10,plc_per_minute=plc,uplink_loss_rate=0,rebuffers_per_hour=0,
                 absent_socket=0,conceal_or_drift_per_minute=plc+drift,
                 all_authenticated_callback_delivery=dict(observable=True,without_callback=missing))
        return [row]*6
    control=population(4,0,30);aa_left=population(4,0,30);aa_right=population(4.5,0,35)
    # PLC and missing callbacks improve, but extra drift loses more output.
    candidate=population(1,5,0)
    result=compare_quality_network(control,candidate,aa_left,aa_right)
    assert 'all_authenticated_no_callback_per_minute' in result['measured_quality_improvements']
    assert result['quality_boundaries']['conceal_or_drift_per_minute']['worse_beyond_AA']
    assert not result['quality_network_qualified']
    candidate=population(1,0,0)
    assert compare_quality_network(control,candidate,aa_left,aa_right)['quality_network_qualified']
    candidate[0]=dict(candidate[0],all_authenticated_callback_delivery=None)
    result=compare_quality_network(control,candidate,aa_left,aa_right)
    assert not result['quality_boundaries']['all_authenticated_no_callback_per_minute']['observable']
    assert not result['quality_network_qualified']
