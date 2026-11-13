import json
from unittest.mock import Mock,patch

import pytest
import echoear_geometry_capture as capture


@pytest.fixture(autouse=True)
def no_hardware():
    with patch('subprocess.Popen',side_effect=AssertionError('Host tests forbid hardware and services')):
        yield


def image(tmp_path,flash_name='dg-esp32.bin'):
    (tmp_path/'project_description.json').write_text(json.dumps(dict(project_version='frozen')))
    (tmp_path/'flasher_args.json').write_text(json.dumps(dict(flash_files={'0x10000':flash_name})))
    (tmp_path/'dg-esp32.elf').write_bytes(b'elf')
    (tmp_path/'dg-esp32.bin').write_bytes(b'bin')
    return tmp_path


def test_image_identity_binds_flash_inputs_and_rejects_substitution(tmp_path):
    path=image(tmp_path)
    with patch('demo_session.firmware_artifacts',return_value='old'):
        before=capture.image_identity(path,'old')
        (path/'dg-esp32.bin').write_bytes(b'changed')
        assert capture.image_identity(path,'old')!=before
        with pytest.raises(RuntimeError,match='Exact frozen image'):capture.image_identity(path,'new')


def test_image_rejects_flash_path_outside_bundle(tmp_path):
    with patch('demo_session.firmware_artifacts',return_value='old'):
        with pytest.raises(RuntimeError,match='escapes frozen'):
            capture.image_identity(image(tmp_path,'../other.bin'),'old')


def test_missing_old_image_stops_before_output_or_bench(tmp_path):
    plan=tmp_path/'plan.json';out=tmp_path/'capture'
    plan.write_text(json.dumps(dict(new_image='unused',old_elf_sha256='old')))
    with pytest.raises(FileNotFoundError):
        capture.main(['--plan',str(plan),'--old-image',str(tmp_path/'missing'),
                      '--budget','unused','--out',str(out)])
    assert not out.exists()


def test_frozen_helper_or_budget_changes_stop_capture(tmp_path):
    import hashlib
    path=tmp_path/'budget.json';path.write_text('{}')
    frozen={str(path):hashlib.sha256(path.read_bytes()).hexdigest()}
    capture.verify_inputs(frozen)
    path.write_text('{"changed":true}')
    with pytest.raises(RuntimeError,match='Frozen diagnostic input changed'):capture.verify_inputs(frozen)


def test_closed_serial_audit_catches_late_error_even_after_prior_snapshot(tmp_path):
    log=tmp_path/'serial.log';log.write_text('I (1) startup: Ready\n')
    assert capture.closed_serial_audit(log)['passed']
    with log.open('a') as stream:stream.write('E (2) spi_master: DMA TX underflow\n')
    verdict=capture.closed_serial_audit(log)
    assert not verdict['passed'] and 'spi_master' in verdict['error']


def test_mixer_verification_never_writes_and_rejects_changed_gain():
    controls=['Mono: Playback 8 [73%] [-4.00dB] [on]', 'Mono: Capture 6 [86%] [3.00dB] [on]']
    with patch.object(capture.subprocess,'check_output',side_effect=controls) as command:
        assert capture.mixer_readback('Jabra')['Mic']==['Capture',6,86,3]
        assert all(call.args[0][3]=='sget' for call in command.call_args_list)
    with patch.object(capture.subprocess,'check_output',side_effect=[controls[0],controls[1].replace('[86%]','[100%]')]):
        with pytest.raises(RuntimeError,match='mixer controls differ'):capture.mixer_readback('Jabra')


@pytest.mark.parametrize('room,accepted',[(-60,True),(-54,False)])
def test_eligibility_reads_rooms_without_mixer_writes_and_keeps_wifi_window(tmp_path,room,accepted):
    import alsa,bench_calibration,host_nic,step7_support
    before=dict(monotonic_s=0,interfaces={n:dict(rx_bytes=0,tx_bytes=0) for n in ('wlp8s0','enp7s0')})
    after=dict(monotonic_s=31,interfaces={n:dict(rx_bytes=20,tx_bytes=20) for n in ('wlp8s0','enp7s0')})
    with patch.object(alsa,'set_volumes',side_effect=AssertionError('Mixer must remain fixed')),\
         patch.object(alsa,'record_wav') as recording,\
         patch.object(alsa,'room_baseline_level',return_value=dict(baseline_dbfs=room)),\
         patch.object(step7_support,'pcm',return_value=[0]*48000),\
         patch.object(bench_calibration,'record_board_room',return_value=-60),\
         patch.object(host_nic,'snapshot',side_effect=[before,after]),\
         patch.object(capture.time,'monotonic',return_value=10),\
         patch.object(capture.time,'sleep') as sleep:
        if accepted:assert capture.eligibility(Mock(),'device',tmp_path,tmp_path/'deferred.jsonl')['passed']
        else:
            with pytest.raises(RuntimeError,match='eligibility failed'):
                capture.eligibility(Mock(),'device',tmp_path,tmp_path/'deferred.jsonl')
        sleep.assert_called_once_with(20)
        recording.assert_called_once_with(tmp_path/'eligibility/room-jabra.wav','device',3,16000)
    stored=json.loads((tmp_path/'eligibility/result.json').read_text())
    assert stored['passed']==accepted and set(stored['rooms'])=={'jabra','board'}


def stopped_population(tmp_path):
    row=dict(role='A-old',diagnostic_valid=False,measurements=dict(passed=True),eligibility=dict(passed=True),
        transport=dict(passed=True,deltas=dict(drift_inserts=2,latency_trim=4)),clips=dict(passed=True),
        monitor=dict(passed=True),closed_raw_monitor=dict(passed=True),nic=dict(passed=True),cleanup_errors=[],
        before_perf=dict(output_volume_db=0),after_perf=dict(output_volume_db=0),
        mixer_before={'fixed':1},mixer_after_eligibility={'fixed':1},mixer_after={'fixed':1})
    plan=dict(arm_order=['A-old','B-new','A-old-repeat'])
    result=dict(passed=False,arms=[row],error='RuntimeError: Original diagnostic arm failed; no automatic replay')
    (tmp_path/'result.json').write_text(json.dumps(result))
    budget=tmp_path/'budget.json';budget.write_text(json.dumps(dict(budget=1,F=2,W=3,largest=4)))
    import hashlib
    hashes={str(budget):hashlib.sha256(budget.read_bytes()).hexdigest()}
    (tmp_path/'predeclared.json').write_text(json.dumps(dict(plan=plan,images=[],input_sha256=hashes,harness_commit='old')))
    return plan,result


def test_continuation_keeps_failed_original_and_archives_pre_amendment_verdict(tmp_path):
    plan,original=stopped_population(tmp_path)
    with patch.object(capture.subprocess,'check_output',return_value='new'):
        frozen,result=capture.prepare_continuation(tmp_path,plan,[],tmp_path/'budget.json')
    assert not result['passed'] and not result['arms'][0]['diagnostic_valid']
    assert result['arms']==original['arms'] and result['supplemental_continuation']['retakes']==0
    assert json.loads((tmp_path/'result-before-continuation.json').read_text())==original
    assert (tmp_path/'continuation.json').exists()


@pytest.mark.parametrize('change',['raw','cleanup','damage','previously_started','plan'])
def test_continuation_rejects_fatal_prior_errors_started_slots_and_changed_controls(tmp_path,change):
    plan,result=stopped_population(tmp_path)
    if change=='raw':result['arms'][0]['closed_raw_monitor']['passed']=False
    if change=='cleanup':result['arms'][0]['cleanup_errors']=['disconnect failed']
    if change=='damage':result['arms'][0]['transport']['passed']=False
    if change=='previously_started':(tmp_path/'2-B-new').mkdir()
    if change=='plan':plan=dict(plan,changed_control=True)
    (tmp_path/'result.json').write_text(json.dumps(result))
    with pytest.raises(RuntimeError):capture.prepare_continuation(tmp_path,plan,[],tmp_path/'budget.json')
    assert not (tmp_path/'continuation.json').exists()


def test_continuation_rejects_different_budget_path_even_with_same_contents(tmp_path):
    plan,_=stopped_population(tmp_path)
    other=tmp_path/'other-budget.json';other.write_bytes((tmp_path/'budget.json').read_bytes())
    with pytest.raises(RuntimeError,match='original frozen budget path'):
        capture.prepare_continuation(tmp_path,plan,[],other)
    assert not (tmp_path/'continuation.json').exists()


def test_reply_window_requires_explicit_exact_protocol_and_preserves_legacy_default():
    assert capture.measurement_window({})['kind']=='original-whole-recording'
    spec=dict(kind='priming-through-reply-end',settle_seconds=6,retain_full_arm=True)
    assert capture.measurement_window(dict(measurement_window=spec))==spec
    with pytest.raises(RuntimeError,match='Unsupported declared'):
        capture.measurement_window(dict(measurement_window=dict(spec,settle_seconds=0)))


def test_calibration_window_zero_gate_and_whole_arm_damage_remain_independent():
    from playback_quality import TRANSPORT_COUNTERS
    before=dict.fromkeys(TRANSPORT_COUNTERS,0)
    before.update(playout_schema=2,pc_id='same',generation=1,mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0)
    after=dict(before,drift_inserts=2,latency_trim=4)
    # Declared NEW diagnostic only: retain late editing counters, require
    # zero changes during priming/reply, no whole-arm damage or any clips.
    result=capture.capture_transport_checks(before,before,before,after)
    assert result['passed'] and result['whole_arm']['deltas']['latency_trim']==4
    assert not capture.capture_transport_checks(before,after,before,after)['passed']
    for counter in ('lost','concealed','drift_drops','i2s_write_errors'):
        assert not capture.capture_transport_checks(before,before,before,dict(before,**{counter:1}))['passed']
    clipped=dict(before,mic_in_turn_clips=1,mic_gate_closed_clips=1)
    assert not capture.capture_transport_checks(before,before,before,clipped)['passed']
