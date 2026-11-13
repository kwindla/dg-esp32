from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
from echoear_requalification import load_plan,environment_gate,preflight_callback,dry_run,observation_gate,PLAN
from echoear_requalification_dry_run import observation


def test_predeclared_plan_has_pending_release_and_fixed_populations():
    plan=load_plan()
    assert not plan['execution_authorized'] and plan['final_elf_sha256'] is None
    assert [s['runs'] for s in plan['order'] if 'runs' in s]==[3,5,5]


def test_weakened_plan_is_rejected(tmp_path):
    plan=load_plan();plan['order'][0]['retakes']=1
    path=tmp_path/'invalid.json';path.write_text(json.dumps(plan))
    with pytest.raises(AssertionError):load_plan(path)


def test_both_room_sensors_and_wifi_are_required_at_inclusive_limit(tmp_path):
    plan=load_plan()
    assert environment_gate(plan,observation())['passed']
    for changes in [dict(jabra=-54.999),dict(board=-54.999),dict(board=None),dict(jabra=float('nan')),dict(wifi=2000001)]:
        check=preflight_callback(plan,'quiet',tmp_path/'deferrals.jsonl',synthetic=True)
        assert not check(1,observation(**changes))['passed']
    rows=[json.loads(line) for line in (tmp_path/'deferrals.jsonl').read_text().splitlines()]
    assert len(rows)==5 and all(not row['scored'] and row['synthetic'] for row in rows)
    assert all(row['eligibility']['rooms'].keys()=={'jabra','board'} for row in rows)


def test_short_nic_observation_is_not_eligibility():
    data=observation(wifi=0);data['nic_after']['monotonic_s']=29.999
    result=environment_gate(load_plan(),data)
    assert not result['passed'] and any('shorter than 30' in reason for reason in result['reasons'])


@pytest.mark.parametrize('case',['noisy','missing','unavailable','fatal'])
def test_production_turn_defers_only_room_measurement_failures_before_playback(tmp_path,case):
    import e2e_common as e2e
    import alsa,bench_calibration,host_nic
    plan=load_plan();board=Mock(cursor=0);board.lines_since.return_value=[]
    board.command.return_value='SYNTHETIC OK'
    obs=observation()
    calibration=dict(room_floor_dbfs=-54,analog_gain_db=30,digital_gain_percent=125)
    if case=='missing':calibration.pop('room_floor_dbfs')
    error=bench_calibration.BoardRoomUnavailable('missing PCM') if case=='unavailable' else RuntimeError('Codec guard failed') if case=='fatal' else None
    with patch.object(alsa,'record_room_baseline',return_value=dict(noise_contaminated=False,baseline_dbfs=-60,wav='jabra.wav')),\
         patch.object(e2e,'resolve_jabra',return_value=('card','device')),patch.object(e2e,'set_volumes'),\
         patch.object(bench_calibration,'calibrate',return_value=calibration,side_effect=error),\
         patch.object(bench_calibration,'echoear_gain_path',return_value=dict(analog_db=30,digital_percent=125)),\
         patch.object(e2e,'calibrate_response',return_value={}),\
         patch.object(host_nic,'snapshot',side_effect=[obs['nic_before'],obs['nic_after'],obs['nic_after']]),\
         patch.object(e2e,'play_wav') as play,patch.object(e2e,'record_wav') as record,\
         patch('failure_snapshot.capture_failure',return_value={}),\
         patch.dict('os.environ',dict(DG_BOARD='echoear',ECHOEAR_ACOUSTIC_QUALIFICATION='1')):
        result=e2e.turn(board,Mock(),'device',tmp_path/'turn',True,reset=False,display='cat',
            preflight=preflight_callback(plan,'quiet',tmp_path/'deferrals.jsonl',synthetic=True))
    if case=='fatal':
        assert not result.get('preflight_deferred') and 'Codec guard failed' in result['error']
        assert not (tmp_path/'deferrals.jsonl').exists()
    else:assert result['preflight_deferred'] and result['scored'] is False and not result['passed']
    play.assert_not_called();record.assert_not_called()


def test_board_room_capture_does_not_hide_device_errors(tmp_path):
    import bench_calibration as calibration
    from speaker_dual_sweep_test import MicDumpUnavailable
    board=Mock()
    with patch.object(calibration.time,'sleep'),patch.object(calibration,'dump_mic',side_effect=MicDumpUnavailable('no PCM')):
        with pytest.raises(calibration.BoardRoomUnavailable):calibration.record_board_room(board,tmp_path)
    for error in (RuntimeError('strict DMA error'),OSError('serial hardware loss')):
        with patch.object(calibration.time,'sleep'),patch.object(calibration,'dump_mic',side_effect=error):
            with pytest.raises(type(error),match=str(error)):calibration.record_board_room(board,tmp_path)


def test_actual_dump_err_ack_is_fatal_not_a_room_deferral(tmp_path):
    import re
    from speaker_dual_sweep_test import dump_mic
    class Board:
        cursor=0
        display_monitor=None
        def send(self,command):assert command=='dump-mic';return 7
        def wait_for(self,pattern,timeout,since):
            ack='ERR #7 dump-mic no capture'
            if re.search(pattern,ack):return ack
            raise TimeoutError('Expected OK never arrived')
    with pytest.raises(RuntimeError,match='command rejected: ERR #7'):
        dump_mic(Board(),tmp_path/'room.wav')


def test_host_rehearsal_exercises_original_counts_failure_and_deferral(tmp_path):
    result=dry_run(PLAN,tmp_path/'rehearsal')
    assert result['passed'] and not result['hardware_qualified'] and not result['scored_campaign_started']
    assert [c['runs'] for c in result['cohort_calls']]==[3,5,5,2,0,1,4]
    assert result['cohort_calls'][-1]['scenario']=='synthetic-priority-resume'
    assert all(c['reruns']==0 for c in result['cohort_calls'])


@pytest.fixture
def synthetic_strict(tmp_path):
    out=tmp_path/'rehearsal';assert dry_run(PLAN,out)['passed']
    return json.loads((out/'strict_voice_cat/result.json').read_text()),(out/'strict_voice_cat/serial.log').read_text()


@pytest.mark.parametrize('case',['cold_heap','largest','retake','four_runs','lock','unknown_snapshot','environment','post_reply_nic','expected_loss','drift_insert','latency_trim','missing_raw_stack'])
def test_observation_gate_recomputes_instead_of_trusting_pass_flags(synthetic_strict,case):
    result,serial=deepcopy(synthetic_strict);rows=serial.splitlines()
    if case in ('cold_heap','largest','lock','unknown_snapshot'):
        at=next(i for i,line in enumerate(rows) if line.startswith('PERF '));sample=json.loads(rows[at][5:])
        if case=='cold_heap':sample['heap']['internal'].update(minimum_free=80372,free=100000)
        if case=='largest':sample['heap']['internal']['largest']=32767
        if case=='lock':sample['display']['lock_timeouts']=1
        if case=='unknown_snapshot':sample['display']['engine_snapshot']=False
        rows[at]='PERF '+json.dumps(sample)
    if case=='retake':result['reruns']=[dict(passed=True)]
    if case=='four_runs':result['runs'].append(deepcopy(result['runs'][0]))
    if case=='environment':result['runs'][0]['eligibility']['rooms']['board']['dbfs']=-54
    if case=='post_reply_nic':result['runs'][0]['host_nic']['after']['interfaces']['wlp8s0']['rx_bytes']=3000000
    if case=='expected_loss':rows.append('E (86004) AGENT: 0 Peer not response for binding after 30000')
    if case=='drift_insert':result['runs'][0]['after_perf']['drift_inserts']=1
    if case=='latency_trim':result['runs'][0]['after_perf']['latency_trim']=1
    if case=='missing_raw_stack':rows=[line for line in rows if not line.startswith('STACK ')]
    verdict=observation_gate(load_plan(),'strict_voice_cat',result,'\n'.join(rows)+'\n')
    assert not verdict['observations_passed'] and not verdict['hardware_qualified']
    if case=='cold_heap':assert verdict['memory']['minimum_internal']==80372


def test_pre_lcd_snapshot_is_not_a_runtime_lock_failure(synthetic_strict):
    result,serial=synthetic_strict
    sample=json.loads(next(line[5:] for line in serial.splitlines() if line.startswith('PERF ')))
    sample['display'].update(lcd_initialized=False,engine_snapshot=False)
    serial='PERF '+json.dumps(sample)+'\n'+serial
    assert observation_gate(load_plan(),'strict_voice_cat',result,serial)['observations_passed']
    sample['heap']['internal']['minimum_free']=80372
    assert not observation_gate(load_plan(),'strict_voice_cat',result,'PERF '+json.dumps(sample)+'\n'+serial)['observations_passed']


def test_requal_n1_plan_predeclares_decision_134_without_touching_item2_strict_gates():
    n1=load_plan(PLAN.with_name('echoear-item2-requalification-n1.json'));base=load_plan()
    d=n1['decision_134']
    assert d['D5_item1_transport']==('"Allow ≤1 lost frame". At most 1 lost/concealed/PLC/playout-dropped downlink frame per '
        'session, with the RF pre-gate recorded. This reverses the all-zero rule for item1 only; the item2 strict stage\'s own '
        'transport gates are unchanged.')
    assert d['D6_defaults']==['a6_analog_probe is report-only','a quiet_live setup failure is unscored and filled by a continuation']
    assert n1['order'][3]['id']=='a6_analog_probe' and n1['order'][3]['report_only'] is True
    assert 'unscored' in n1['order'][5]['setup_failure']
    # Only the geometry-rebased Jabra target differs from the master predeclared strict gates; the transport rule is untouched.
    changed={k for k in base['strict_gates'] if base['strict_gates'][k]!=n1['strict_gates'][k]}
    assert changed=={'jabra_reply_target_dbfs'} and set(base['strict_gates'])==set(n1['strict_gates'])
    assert n1['strict_gates']['jabra_reply_target_dbfs']==pytest.approx(-29.845806865819114)
    assert n1['strict_gates']['transport_counter_deltas'].startswith('all zero')
    assert [s['id'] for s in n1['order']]==[s['id'] for s in base['order']]
    assert n1['environment']==base['environment'] and n1['bot']==base['bot']
    assert {k:v for k,v in n1['firmware'].items() if k!='volume_scope'}==base['firmware']   # r3 adds only the declared scope text
