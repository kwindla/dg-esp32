"""No devices: product voice profile, real capture replay and stack provenance."""
import copy
import importlib.util
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
import p4_audio as base
import board_smoke as smoke
from wire_contract import ContractError
from test_wire_contract import capture, serialize, IDENTITY
from test_p4_audio import cost_fixture as base_cost_fixture

source = Path(os.environ.get('DG_P4_AUDIO_FIX4_SOURCE', Path(__file__).with_name('p4_audio_fix4.py')))
spec = importlib.util.spec_from_file_location('fix4_under_test', source)
fix = importlib.util.module_from_spec(spec); spec.loader.exec_module(fix)
REAL = Path(os.environ.get('DG_P4_AUDIO_FIX4_EVIDENCE', Path.home()/'src/evidence/echoear/p4/step-12b/fix-1'))


def patch_method(monkeypatch, name, stub):
    assert inspect.signature(stub) == inspect.signature(getattr(smoke.LiveIO, name))
    monkeypatch.setattr(smoke.LiveIO, name, stub)


def prepare_manual(self, board):
    board.command('audio 0')


def cost_fixture(path):
    row = base_cost_fixture(path); row['rx_stack_bytes'] = 8192
    for item in path.glob('perf-*'): item.write_text(json.dumps(row))
    return row


class InitConsole:
    def __init__(self, initialized=False, broken=False):
        self.initialized, self.broken = initialized, broken; self.commands = []; self.cursor = 0
    def command(self, text, timeout=5):
        self.commands.append(text); self.cursor += 1
        if text == 'disp init' and not self.broken: self.initialized = True
    def wait_for(self, *args, **kwargs):
        return 'PERF '+json.dumps(dict(display=dict(lcd_initialized=self.initialized)))


@pytest.mark.parametrize('board,initialized,expected', [('echoear', False, 1), ('echoear', True, 0), ('p4xc', True, 0)])
def test_headless_s3_initializes_display_once_before_connect_but_skips_all_ui(monkeypatch, board, initialized, expected):
    patch_method(monkeypatch, 'prepare_manual', prepare_manual)
    io = object.__new__(fix.VoiceIO); io.args = SimpleNamespace(board=board, headless=True)
    console = InitConsole(initialized); io.prepare_manual(console)
    assert console.commands.count('disp init') == expected
    assert not any('connect' in cmd or 'character' in cmd or 'demo on' in cmd or 'touch' in cmd or 'http' in cmd for cmd in console.commands)
    assert io.display_initialized is True


def test_failed_s3_display_init_cannot_be_silently_accepted(monkeypatch):
    patch_method(monkeypatch, 'prepare_manual', prepare_manual)
    io = object.__new__(fix.VoiceIO); io.args = SimpleNamespace(board='echoear', headless=True)
    with pytest.raises(ContractError, match='did not initialize'): io.prepare_manual(InitConsole(broken=True))


def capture_lines(board, aec=False):
    value = capture('esp32s3' if board == 'echoear' else 'esp32p4', aec)
    rows = serialize(value)
    return [line for line in rows if not line.startswith('DEMO_CAPABILITY')]+['PHASE closed'], [line for line in rows if line.startswith('DEMO_CAPABILITY')]


@pytest.mark.parametrize('board', ['echoear', 'p4xc'])
@pytest.mark.parametrize('missing', [None, 'display-ready', 'opus_enc', 'UPLINK_INTERVALS', 'DEMO_CAPABILITY'])
def test_both_legs_require_exactly_the_product_evidence_profile(board, missing):
    lines, capability = capture_lines(board)
    if missing == 'DEMO_CAPABILITY': capability = []
    elif missing: lines = [line for line in lines if missing not in line]
    if missing:
        with pytest.raises(ContractError): fix.product_capture(lines, capability, manifest=dict(aec_enabled=False), board=board, calibration=True)
    else:
        result, _, _ = fix.product_capture(lines, capability, manifest=dict(aec_enabled=False), board=board, calibration=True)
        assert result.capability == [False]


def test_finish_enforces_product_inside_acquisition_before_restore_success(monkeypatch, tmp_path):
    lines, capability = capture_lines('echoear')
    calls = []
    def finish(self, board, label, *, hold=False):
        calls.append((board, label, hold)); return lines, capability
    patch_method(monkeypatch, 'finish', finish)
    assert inspect.signature(fix.VoiceIO.finish) == inspect.signature(finish)
    io = object.__new__(fix.VoiceIO); io.args = SimpleNamespace(board='echoear', calibration=True)
    io.manifest = dict(aec_enabled=False)
    row = cost_fixture(tmp_path/'cost'); row['generation'] = 1; io.stack_samples = [row, row]
    board = object()
    assert io.finish(board, 'main', hold=True) == (lines, capability)
    assert calls == [(board, 'main', True)]
    assert io.product_evidence_verified is True
    assert io.task_stack_reports[0]['stack_checks_passed']
    capability.clear()
    with pytest.raises(ContractError): io.finish(object(), 'main')


def test_voice_adapter_keeps_sweep_gate_and_frozen_executor_without_patching_it(tmp_path, monkeypatch):
    manifest = dict(profile='normal', aec_enabled=False)
    monkeypatch.setattr(base, 'verify_bundle', create_autospec(base.verify_bundle, return_value=(manifest, {})))
    io = SimpleNamespace(product_evidence_verified=True, display_initialized=True)
    monkeypatch.setattr(fix, 'VoiceIO', create_autospec(fix.VoiceIO, return_value=io))
    def execute(args, *, backend=None):
        assert args.headless and args.calibration and backend is io
        args.out.mkdir(); return dict(acquisition_passed=True, restore=dict(clean=True))
    monkeypatch.setattr(smoke, 'execute', execute)
    identity = tmp_path/'identity.json'; identity.write_text(json.dumps(IDENTITY))
    args = SimpleNamespace(board='echoear', bundle=tmp_path/'bundle', headless=True, calibration=True,
                           bot_identity=identity, out=tmp_path/'run')
    assert fix.voice_smoke(args) == 0
    result = json.loads((args.out/'result.json').read_text())
    assert result['voice_evidence_profile'] == 'product' and result['product_evidence_verified']
    assert result['ui_checks_skipped'] and result['display_initialized']


def test_voice_success_without_product_check_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(base, 'verify_bundle', create_autospec(base.verify_bundle, return_value=(dict(profile='normal', aec_enabled=False), {})))
    monkeypatch.setattr(fix, 'VoiceIO', create_autospec(fix.VoiceIO, return_value=SimpleNamespace()))
    monkeypatch.setattr(smoke, 'execute', create_autospec(smoke.execute, return_value=dict(acquisition_passed=True)))
    identity = tmp_path/'identity.json'; identity.write_text(json.dumps(IDENTITY))
    args = SimpleNamespace(board='echoear', bundle=tmp_path, headless=True, bot_identity=identity, out=tmp_path)
    with pytest.raises(ContractError, match='lacks product evidence'): fix.voice_smoke(args)


@pytest.mark.parametrize('field,task', list(fix.STACK_TASKS.items()))
def test_direct_zero_is_unavailable_but_census_zero_is_a_measured_low_water_mark(field, task):
    result = fix.stack_measurements([{field: 0}])[field]
    assert result['available'] is False and result['free_bytes'] is None
    row = {field: 0, 'tasks': [dict(name=task, id=42, stack_bytes=8192, stack_free_bytes=0)]}
    result = fix.stack_measurements([row])[field]
    assert result['available'] is True and result['free_bytes'] == 0
    row['tasks'][0]['stack_free_bytes'] = 6344
    result = fix.stack_measurements([row])[field]
    assert result['free_bytes'] == 6344 and result['sources'][0]['source'] == 'PERF.tasks'


def test_minimum_over_every_snapshot_and_identity_must_be_preserved():
    samples = [dict(tx_stack=0, tasks=[dict(name='audio-tx', id=1, stack_free_bytes=value)]) for value in (8000, 6344, 7000)]
    assert fix.stack_measurements(samples)['tx_stack']['free_bytes'] == 6344
    samples[-1]['tasks'][0]['id'] = 2
    with pytest.raises(ContractError, match='identity changed'): fix.stack_measurements(samples)


@pytest.mark.parametrize('change', ['duplicate', 'negative', 'boolean', 'over-budget'])
def test_invalid_or_ambiguous_census_fails_closed(change):
    row = dict(tasks=[dict(name='audio-tx', id=1, stack_bytes=8192, stack_free_bytes=6344)])
    if change == 'duplicate': row['tasks'].append(copy.deepcopy(row['tasks'][0]))
    else: row['tasks'][0]['stack_free_bytes'] = {'negative': -1, 'boolean': True, 'over-budget': 9000}[change]
    with pytest.raises(ContractError): fix.stack_measurements([row])


def test_valid_costs_keep_existing_resource_and_codec_gates(tmp_path):
    directory = tmp_path/'run'; cost_fixture(directory)
    result = fix.session_costs(directory, tmp_path/'costs.json')
    assert result['checks_passed'] and result['codec_costs_available'] and result['stack_checks_passed']
    assert result['metrics']['encode_max_us'] == 6000
    assert result['stack_measurements']['tx_stack']['free_bytes'] == 6000
    with pytest.raises(ContractError, match='new cost'): fix.session_costs(directory, tmp_path/'costs.json')


@pytest.mark.parametrize('value,available', [(0, False), (4095, True)])
def test_unavailable_or_low_stack_retains_cost_figures_but_never_claims_pass(tmp_path, value, available):
    directory = tmp_path/'run'; row = cost_fixture(directory)
    row['tx_stack'] = value
    for path in directory.glob('perf-*'): path.write_text(json.dumps(row))
    result = fix.session_costs(directory, tmp_path/'costs.json')
    assert result['codec_costs_available'] and not result['checks_passed'] and not result['stack_checks_passed']
    assert result['stack_measurements']['tx_stack']['available'] is available
    if not available: assert result['metrics']['tx_stack'] is None
    assert result['metrics']['encode_max_us'] == 6000


@pytest.mark.parametrize('key,value', [('encode_frames', 499), ('speech_decode_frames', 0), ('uplink_frame_ms', 40), ('decode_errors', 1)])
def test_missing_live_work_or_bad_codec_still_fails_without_output(tmp_path, key, value):
    directory = tmp_path/'run'; row = cost_fixture(directory); row[key] = value
    for path in directory.glob('perf-*'): path.write_text(json.dumps(row))
    with pytest.raises(ContractError): fix.session_costs(directory, tmp_path/'costs.json')
    assert not (tmp_path/'costs.json').exists()


@pytest.mark.skipif(not (REAL/'coordinator-voice-p4xc').is_dir() or not (REAL/'attempt1-voice-echoear-missing-display-ready').is_dir(), reason='local step-12b acquisition evidence is absent')
def test_real_captures_replay_without_repairing_the_missing_s3_record():
    s3 = REAL/'attempt1-voice-echoear-missing-display-ready'; p4 = REAL/'coordinator-voice-p4xc'
    assert (s3/'main-serial.log').is_file() and (p4/'main-serial.log').is_file()
    bad, good = fix.replay_capture(s3), fix.replay_capture(p4)
    assert not bad['evidence_passed'] and bad['error'] == 'missing mandatory wire evidence'
    assert good['evidence_passed'] and good['profile'] == 'product' and good['interval_count'] >= 500
    assert not bad['original_acquisition_passed'] and good['original_acquisition_passed']
    for directory in (s3, p4):
        samples = [json.loads(path.read_text()) for path in sorted(directory.glob('perf-*.json'))]
        assert all(row['tx_stack'] == 0 and row['owner_stack'] == 0 and row['rx_stack'] == 0 for row in samples)
        assert fix.stack_measurements(samples)['tx_stack']['available'] is True


@pytest.mark.skipif(not (REAL/'coordinator-voice-p4xc').is_dir() or not (REAL/'attempt1-voice-echoear-missing-display-ready').is_dir(), reason='local step-12b acquisition evidence is absent')
def test_saved_p4_costs_recover_encoder_mark_and_apply_decided_floors(tmp_path):
    directory = REAL/'coordinator-voice-p4xc'
    result = fix.session_costs(directory, tmp_path/'saved-costs.json')
    assert result['metrics']['encode_p50_us'] == 3199 and result['metrics']['encode_p95_us'] == 4399
    assert result['metrics']['encode_max_us'] == 6485 and result['metrics']['decode_max_us'] == 2491
    assert result['metrics']['tx_stack'] is None
    assert result['stack_measurements']['tx_stack']['free_bytes'] == 6344
    assert result['stack_measurements']['rx_stack']['free_bytes'] == 2264
    assert result['stack_measurements']['decode_stack']['free_bytes'] == 4772
    assert result['stack_floors_bytes'] == dict(tx_stack=4096, decode_stack=4096, owner_stack=4096, rx_stack=1744)
    assert result['stack_findings'] == []
    assert result['codec_costs_available'] and result['stack_checks_passed'] and not result['qualification']


def test_pair_cli_delegates_to_the_frozen_product_pair(tmp_path, monkeypatch):
    check = Mock(return_value=dict(profile='product', checks_passed=True))
    monkeypatch.setattr(base, 'product_pair', check)
    assert fix.main(['pair', str(tmp_path/'s3'), str(tmp_path/'p4'), '--out', str(tmp_path/'pair.json')]) == 0
    assert check.call_args.args == (tmp_path/'s3', tmp_path/'p4', tmp_path/'pair.json', smoke.BOUNDS_FILE)


def test_p4_display_runtime_must_already_be_initialized(monkeypatch):
    patch_method(monkeypatch, 'prepare_manual', prepare_manual)
    io = object.__new__(fix.VoiceIO); io.args = SimpleNamespace(board='p4xc', headless=True)
    console = InitConsole(False)
    with pytest.raises(ContractError, match='p4xc display runtime did not initialize'): io.prepare_manual(console)
    assert 'disp init' not in console.commands and io.display_initialized is False


def test_aborted_cleanup_keeps_primary_error_without_secondary_product_check(monkeypatch):
    calls = []
    def finish(self, board, label, *, hold=False):
        calls.append((label, hold)); return [], []
    patch_method(monkeypatch, 'finish', finish)
    io = object.__new__(fix.VoiceIO); io.active = True; io.manifest = dict(profile='normal', aec_enabled=False, autostart=False)
    # Exercise the real cleanup method: its failed session has no wire records.
    smoke.LiveIO.cleanup(io, object())
    assert calls == [('failed', False)]
    assert not getattr(io, 'product_evidence_verified', False)


@pytest.mark.parametrize('board', ['echoear', 'p4xc'])
def test_product_capture_uses_actual_bundle_aec_setting(board):
    lines, capability = capture_lines(board, aec=True)
    result, _, _ = fix.product_capture(lines, capability, manifest=dict(aec_enabled=True), board=board, calibration=True)
    assert result.capability == [True]
    with pytest.raises(ContractError):
        fix.product_capture(lines, capability, manifest=dict(aec_enabled=False), board=board, calibration=True)


def test_snapshot_stubs_keep_real_signature_and_collect_all_rows(monkeypatch):
    row = dict(generation=1)
    def snapshot(self, board):
        return row
    patch_method(monkeypatch, 'snapshot', snapshot)
    io = object.__new__(fix.VoiceIO)
    assert io.snapshot(object()) is row and io.snapshot(object()) is row
    assert io.stack_samples == [row, row]


@pytest.mark.parametrize('field', list(fix.STACK_TASKS))
def test_one_missing_snapshot_makes_stack_unavailable_even_with_other_measurements(field):
    result = fix.stack_measurements([{field: 6000}, {}])[field]
    assert result['available'] is False and result['free_bytes'] == 6000
    assert result['missing_snapshots'] == [1]


def test_other_session_perf_samples_cannot_change_minimum_or_resource_gate(tmp_path):
    directory = tmp_path/'run'; row = cost_fixture(directory)
    foreign = copy.deepcopy(row)
    foreign.update(generation=1, encode_frames=10, decode_errors=1, tx_stack=1, output_volume_db=0,
                   tasks=[dict(name='foreign-only', id=99, stack_free_bytes=0)])
    (directory/'perf-000.json').write_text(json.dumps(foreign))
    result = fix.session_costs(directory, tmp_path/'costs.json')
    assert result['checks_passed'] and result['generation'] == 2
    assert result['stack_measurements']['tx_stack']['free_bytes'] == 6000
    assert all(row['task'] != 'foreign-only' for row in result['task_minima'])


@pytest.mark.parametrize('board', ['echoear', 'p4xc'])
@pytest.mark.parametrize('allocation,free,floor,passed', [
    (4096, 1744, 1744, True), (4096, 1743, 1744, False),
    (8192, 2048, 2048, True), (8192, 2047, 2048, False),
    (6977, 1744, 1745, False), (6977, 1745, 1745, True)])
def test_task_floors_follow_board_specific_addendum(tmp_path, board, allocation, free, floor, passed):
    directory = tmp_path/'run'; row = cost_fixture(directory)
    result_path = directory/'result.json'; result = json.loads(result_path.read_text()); result['board'] = board
    result_path.write_text(json.dumps(result))
    row.update(rx_stack=0, rx_stack_bytes=allocation)
    row['tasks'] += [dict(name='peer-rx', id=7, stack_bytes=allocation, stack_free_bytes=free),
                     dict(name='ungated-task', id=8, stack_bytes=512, stack_free_bytes=123)]
    for path in directory.glob('perf-*'): path.write_text(json.dumps(row))
    result = fix.session_costs(directory, tmp_path/'costs.json')
    assert result['stack_policy'] == 'decision-2026-11-01-p4-task-stack-floors'
    assert result['stack_policy_addendum'] == 'decision-2026-11-01-p4-task-stack-floors-addendum'
    assert result['stack_floors_bytes']['rx_stack'] == (floor if board == 'p4xc' else None)
    assert result['stack_checks_passed'] is (passed if board == 'p4xc' else True)
    assert result['stack_report_only'] == (['rx_stack'] if board == 'echoear' else [])
    assert result['stack_measurements']['rx_stack']['free_bytes'] == free
    assert all(result['stack_floors_bytes'][key] == 4096 for key in ('tx_stack', 'decode_stack', 'owner_stack'))
    assert next(task for task in result['task_minima'] if task['task'] == 'ungated-task')['free_bytes'] == 123


def test_rx_allocation_is_required_and_sources_must_agree(tmp_path):
    directory = tmp_path/'run'; row = cost_fixture(directory); row.pop('rx_stack_bytes')
    for path in directory.glob('perf-*'): path.write_text(json.dumps(row))
    result = fix.session_costs(directory, tmp_path/'costs.json')
    assert not result['checks_passed'] and result['stack_floors_bytes']['rx_stack'] is None
    assert result['stack_findings'] == ['rx_stack: unavailable allocation']
    row.update(rx_stack_bytes=8192); row['tasks'].append(dict(name='peer-rx', stack_bytes=4096, stack_free_bytes=1744))
    with pytest.raises(ContractError, match='sources disagree'): fix.stack_measurements([row])


def test_voice_resource_floor_failure_is_preserved_in_report(monkeypatch, tmp_path):
    lines, capability = capture_lines('p4xc')
    def finish(self, board, label, *, hold=False):
        return lines, capability
    patch_method(monkeypatch, 'finish', finish)
    io = object.__new__(fix.VoiceIO); io.args = SimpleNamespace(board='p4xc', calibration=True)
    io.manifest = dict(aec_enabled=False)
    row = cost_fixture(tmp_path/'cost'); row.update(generation=1, tx_stack=4095); io.stack_samples = [row, row]
    with pytest.raises(ContractError, match='tx_stack: measured free stack below 4096'): io.finish(object(), 'main')
    assert not io.task_stack_reports[0]['stack_checks_passed']


def voice_io(monkeypatch, board):
    lines, capability = capture_lines(board)
    def finish(self, board, label, *, hold=False):
        return lines, capability
    patch_method(monkeypatch, 'finish', finish)
    io = object.__new__(fix.VoiceIO); io.args = SimpleNamespace(board=board, calibration=True)
    io.manifest = dict(aec_enabled=False)
    return io, lines, capability


def test_finish_filters_foreign_generation_before_stack_report(monkeypatch, tmp_path):
    io, lines, capability = voice_io(monkeypatch, 'p4xc')
    row = cost_fixture(tmp_path/'cost'); row['generation'] = 1
    foreign = copy.deepcopy(row); foreign.update(generation=2, tx_stack=1)
    foreign['tasks'].append(dict(name='foreign-only', id=99, stack_free_bytes=0))
    io.stack_samples = [foreign, row, row]
    assert io.finish(object(), 'main', hold=True) == (lines, capability)
    report = io.task_stack_reports[0]
    assert report['generation'] == 1 and report['stack_checks_passed']
    assert report['stack_measurements']['tx_stack']['free_bytes'] == 6000
    assert all(task['task'] != 'foreign-only' for task in report['task_minima'])


@pytest.mark.parametrize('count', [0, 1])
def test_finish_requires_two_samples_from_capture_generation(monkeypatch, tmp_path, count):
    io, _, _ = voice_io(monkeypatch, 'p4xc')
    row = cost_fixture(tmp_path/'cost'); row['generation'] = 1
    foreign = dict(row, generation=2)
    io.stack_samples = [row]*count+[foreign, foreign]
    with pytest.raises(ContractError, match='before/after stack snapshots from the measured generation required'):
        io.finish(object(), 'main', hold=True)
    assert not getattr(io, 'task_stack_reports', [])


@pytest.mark.parametrize('field,task', list(fix.STACK_TASKS.items()))
def test_stack_allocation_cannot_change_within_generation(field, task):
    samples = [dict(generation=1, tasks=[dict(name=task, id=42, stack_bytes=size, stack_free_bytes=6000)])
               for size in (8192, 9216)]
    with pytest.raises(ContractError, match='allocation changed within generation: '+task):
        fix.stack_measurements(samples)


@pytest.mark.parametrize('field,task', list(fix.STACK_TASKS.items()))
def test_direct_stack_cannot_exceed_agreeing_allocation(field, task):
    row = {field: 8193, 'tasks': [dict(name=task, id=42, stack_bytes=8192, stack_free_bytes=6000)]}
    if field == 'rx_stack': row['rx_stack_bytes'] = 8192
    with pytest.raises(ContractError, match='direct stack exceeds allocation: '+task):
        fix.stack_measurements([row])


def test_all_task_minima_take_minimum_and_keep_distinct_ids(tmp_path):
    row = cost_fixture(tmp_path/'cost'); samples = []
    for free in (8000, 6000, 7000):
        sample = copy.deepcopy(row)
        sample['tasks'] += [dict(name='pipecat', id=41, stack_bytes=8192, stack_free_bytes=free),
                            dict(name='pipecat', id=42, stack_bytes=8192, stack_free_bytes=free-1000)]
        samples.append(sample)
    report = fix.stack_report(samples, board='p4xc')
    minima = {(task['task'], task['task_id']): task['free_bytes'] for task in report['task_minima']}
    assert minima[('pipecat', 41)] == 6000 and minima[('pipecat', 42)] == 5000


@pytest.mark.parametrize('board', ['echoear', 'p4xc'])
@pytest.mark.parametrize('free', [0, 1743, None])
def test_rx_is_report_only_on_s3_in_live_and_offline_paths(monkeypatch, tmp_path, board, free):
    directory = tmp_path/'run'; row = cost_fixture(directory)
    result_path = directory/'result.json'; run = json.loads(result_path.read_text()); run['board'] = board
    result_path.write_text(json.dumps(run))
    row.update(generation=1, rx_stack=0); row.pop('rx_stack_bytes')
    if free is not None:
        row['rx_stack_bytes'] = 4096
        row['tasks'].append(dict(name='peer-rx', id=42, stack_bytes=4096, stack_free_bytes=free))
    for path in directory.glob('perf-*'): path.write_text(json.dumps(row))
    report = fix.session_costs(directory, tmp_path/'costs.json')
    assert report['stack_checks_passed'] is (board == 'echoear')
    assert report['stack_measurements']['rx_stack']['free_bytes'] == free
    io, lines, capability = voice_io(monkeypatch, board); io.stack_samples = [row, row]
    if board == 'echoear':
        assert io.finish(object(), 'main', hold=True) == (lines, capability)
        assert io.task_stack_reports[0]['stack_report_only'] == ['rx_stack']
    else:
        with pytest.raises(ContractError, match='rx_stack:'): io.finish(object(), 'main', hold=True)
    assert io.task_stack_reports[0]['stack_checks_passed'] is (board == 'echoear')


@pytest.mark.parametrize('board', ['echoear', 'p4xc'])
@pytest.mark.parametrize('field', ['tx_stack', 'decode_stack', 'owner_stack'])
def test_other_three_floors_remain_gated_on_both_boards(tmp_path, board, field):
    row = cost_fixture(tmp_path/'cost'); row[field] = 4095
    report = fix.stack_report([row, row], board=board)
    assert not report['stack_checks_passed']
    assert report['stack_findings'] == [field+': measured free stack below 4096']
