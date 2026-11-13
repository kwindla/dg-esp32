import pytest
from echoear_aec_resources import STACK_FLOORS, check_snapshots


def snapshot():
    return dict(tasks=[dict(name=n, stack_bytes=8192, stack_free_bytes=max(f, 4096))
                       for n, f in STACK_FLOORS.items()] +
                      [dict(name='IDLE'+str(i), window_us=5000000) for i in (0, 1)],
                heap={'internal': dict(minimum_free=81920, largest=32768)},
                pc_id="test", generation=1, encode_frames=1, encode_p95_us=19999, encode_max_us=39999,
                per_core_idle_percent=[25, 25], uplink_frame_ms=20,
                drop_reasons={'admission_budget': 0}, data_admission={'received': 2, 'dispatched': 2},
                failed_allocations={'total': 0}, pcm_queue_drops=0,
                i2s_underflows=0, i2s_write_errors=0, i2s_write_timeouts=0,
                audio=dict(drops=0, read_errors=0, write_errors=0, write_timeouts=0),
                console_output=dict(full=0, transport_drops=0),
                aec=dict(errors=0, reference_clips=0, output_clips=0,
                         capture=dict(read_errors=0, read_timeouts=0, adc_clips=0, gain_clips=0)))


def test_good_boundary():
    assert all(c['passed'] for c in check_snapshots([snapshot()]).values())


@pytest.mark.parametrize('key,value', [('encode_p95_us', 20000), ('encode_max_us', 40000)])
def test_encoder_strict_and_every_session(key, value):
    bad = snapshot(); bad[key] = value
    assert not check_snapshots([bad, snapshot()])[key]['passed']


@pytest.mark.parametrize('value,absolute', [(24.99, True), (20, True), (19.99, False)])
def test_cpu_floor(value, absolute):
    p = snapshot(); p['per_core_idle_percent'][1] = value
    c = check_snapshots([p])['core_1_steady_idle_percent']
    assert not c['passed']
    assert c['absolute_floor_passed'] == absolute


def test_short_windows_cannot_qualify():
    p = snapshot(); p['tasks'][-1]['window_us'] = 4999999
    assert not check_snapshots([p])['core_0_steady_idle_percent']['passed']


def test_missing_encoder_measurement_fails():
    p = snapshot(); del p['encode_p95_us']
    assert not check_snapshots([p])['encode_p95_us']['passed']


def test_reconnect_low_stack_not_hidden():
    p = snapshot(); p['tasks'][2]['stack_free_bytes'] = 4095
    assert not check_snapshots([p, snapshot()])['stack_audio-tx']['passed']


def test_empty_fails():
    assert not check_snapshots([])['snapshots_present']['passed']


def test_preconnection_has_no_transport_counters():
    p = snapshot(); del p['generation']; del p['pcm_queue_drops']
    assert all(c['passed'] for c in check_snapshots([p, snapshot()]).values())


def test_playback_effective_floor_and_margin():
    p = snapshot()
    playback = p['tasks'][0]
    playback.update(stack_bytes=17152, stack_free_bytes=4508)
    c = check_snapshots([p])['stack_audio-playback']
    assert c['floor'] == 4288 and c['minimum_margin_bytes'] == 220
    playback['stack_free_bytes'] = 4287
    assert not check_snapshots([p])['stack_audio-playback']['passed']


@pytest.mark.parametrize('key', ['i2s_underflows', 'i2s_write_errors', 'i2s_write_timeouts'])
def test_hardware_failure_is_not_hidden_by_clean_pipeline_audio_line(key):
    p = snapshot(); p[key] = 1
    assert not check_snapshots([p])[key]['passed']
