import json
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
from echoear_item2_campaign import refuse_replayed_population,a6_preflight
from echoear_requalification import load_plan


@pytest.fixture(autouse=True)
def no_hardware():
    with patch('subprocess.Popen',side_effect=AssertionError('Host tests forbid device/process access')):
        yield


def test_only_unscored_eligibility_deferrals_can_be_retried(tmp_path):
    root=tmp_path/'stage';attempt=root/'attempt-1';attempt.mkdir(parents=True)
    p=attempt/'result.json'
    # r13 (Fable r12 N2): a replayable deferral is a CLEAN one (exit 2, first-attempt clean restore, no error).
    p.write_text(json.dumps(dict(deferred=True,runs=[],points=[],deferral_resources=dict(passed=True),retry_after_host_s=0,exit_code=2,
                                 restore=dict(clean=True,attempts=[dict(label='restore-1',error=None)]))))
    refuse_replayed_population(root)
    for previous in (dict(deferred=True,runs=[dict(scored=True)]),dict(deferred=True,points=[{}]),dict(passed=False),dict(passed=True)):
        p.write_text(json.dumps(previous))
        with pytest.raises(RuntimeError,match='cannot be replayed'):refuse_replayed_population(root)
    p.unlink()
    with pytest.raises(RuntimeError,match='incomplete population'):refuse_replayed_population(root)


def test_flash_only_crash_is_also_incomplete_not_a_new_clean_cohort(tmp_path):
    (tmp_path/'attempt-1-identity').mkdir()
    with pytest.raises(RuntimeError,match='incomplete population'):refuse_replayed_population(tmp_path)


@pytest.mark.parametrize('sensor',['jabra','board'])
def test_a6_deferral_records_both_room_values_before_any_original(tmp_path,sensor):
    import alsa,bench_calibration,host_nic
    before=dict(monotonic_s=0,interfaces={n:dict(rx_bytes=0,tx_bytes=0) for n in ('wlp8s0','enp7s0')})
    after=dict(monotonic_s=31,interfaces={n:dict(rx_bytes=10,tx_bytes=10) for n in ('wlp8s0','enp7s0')})
    point=tmp_path/'original';point.mkdir()
    with patch.object(host_nic,'snapshot',side_effect=[before,after]),\
         patch.object(alsa,'record_room_baseline',return_value=dict(baseline_dbfs=-54 if sensor=='jabra' else -60,wav='jabra.wav')),\
         patch.object(bench_calibration,'record_board_room',return_value=-54 if sensor=='board' else -60):
        callback=a6_preflight(load_plan(),'a6_echo_stress',tmp_path/'deferrals.jsonl')
        with pytest.raises(RuntimeError,match='eligibility deferred'):callback(Mock(),'device',point)
    event=json.loads((tmp_path/'deferrals.jsonl').read_text());assert event['scored'] is False
    assert not callback.observations[-1]['passed']
    assert set(event['eligibility']['rooms'])=={'jabra','board'}


def test_a6_strict_device_failure_cannot_become_a_room_deferral(tmp_path):
    import alsa,bench_calibration,host_nic
    point=tmp_path/'original';point.mkdir()
    with patch.object(host_nic,'snapshot',return_value={}),\
         patch.object(alsa,'record_room_baseline',return_value=dict(baseline_dbfs=-60,wav='jabra.wav')),\
         patch.object(bench_calibration,'record_board_room',side_effect=RuntimeError('DMA fault')):
        callback=a6_preflight(load_plan(),'a6_hot_stt',tmp_path/'deferrals.jsonl')
        with pytest.raises(RuntimeError,match='DMA fault'):callback(Mock(),'device',point)
    assert not (tmp_path/'deferrals.jsonl').exists()


def test_a6_resource_gate_rejects_locks_unknown_snapshot_and_raw_errors(tmp_path):
    import copy
    from echoear_item2_campaign import a6_resources
    row=dict(heap=dict(internal=dict(minimum_free=90000,largest=49000)),failed_allocations=dict(internal=0,total=0),
        display=dict(lcd_initialized=True,engine_snapshot=True,dead=False,failed=False,dma_errors=0,
                     lock_timeouts=0,lock_failures=0,send_failures=0,late_callbacks=0))
    log=tmp_path/'serial.log'
    with patch('step9a_r2_checks.audit',return_value=dict(passed=True,app_control_min_free=9000)):
        log.write_text('PERF '+json.dumps(row)+'\n');assert a6_resources(tmp_path)['passed']
        for key,value in [('lock_timeouts',1),('lock_failures',1),('engine_snapshot',False)]:
            bad=copy.deepcopy(row);bad['display'][key]=value;log.write_text('PERF '+json.dumps(bad)+'\n')
            assert not a6_resources(tmp_path)['passed']
        log.write_text('');assert not a6_resources(tmp_path)['passed']
        assert a6_resources(tmp_path,allow_no_samples=True)['passed']
    with patch('step9a_r2_checks.audit',side_effect=RuntimeError('Unexpected AGENT error')):
        assert not a6_resources(tmp_path,allow_no_samples=True)['passed']


def test_stress_cleanup_attempts_every_command_after_an_error():
    from echoear_echo_stress import cleanup_stress_board
    board=Mock();board.command.side_effect=[RuntimeError('first cleanup failed'),None,None,None,None]
    errors=cleanup_stress_board(board)
    assert len(errors)==1 and board.command.call_count==5
    assert board.command.call_args_list[-1].args[0]=='disp bench off'


def test_hot_scoring_retains_resource_and_nic_fields_before_api_call(tmp_path):
    from echoear_item2_campaign import score_hot_once
    result=dict(passed=False,item2_resources=dict(passed=True),stage_nic=dict(passed=False))
    def score(out):
        stored=json.loads((out/'result.json').read_text());assert stored==result
        stored['passed']=True;stored['scored']=True;return stored
    with patch('echoear_hot_stt.score',side_effect=score) as scoring:
        final=score_hot_once(tmp_path,result)
    scoring.assert_called_once_with(tmp_path);assert final['scored'] and not final['passed']


@pytest.mark.parametrize('fatal',['cleanup','raw'])
def test_noisy_eligibility_cannot_hide_cleanup_or_raw_failure(tmp_path,fatal):
    from echoear_item2_campaign import validate_deferral
    result=dict(deferred=True,runs=[],deferrals=[dict(preflight_deferred=True,cleanup_error='ERR #1 audio 0' if fatal=='cleanup' else None)])
    raw='I (1) startup\n'+('E (2) spi_master: DMA TX underflow\n' if fatal=='raw' else '')
    (tmp_path/'serial.log').write_text(raw)
    assert not validate_deferral(tmp_path,result)
    assert result['deferred'] is False and result['passed'] is False


def test_verified_unscored_deferral_retains_missing_measurement_scope(tmp_path):
    from echoear_item2_campaign import validate_deferral
    (tmp_path/'serial.log').write_text('I (1) startup\n')
    result=dict(deferred=True,runs=[],deferrals=[dict(preflight_deferred=True)])
    assert validate_deferral(tmp_path,result)
    assert not result['deferral_resources']['samples_available']
    assert result['retry_after_host_s']>0
