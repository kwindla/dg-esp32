"""Fixed limits apply even when bot content is calibration-only."""
import json
import pytest
import board_smoke as smoke
import board_smoke_bounds as adapter
from test_board_smoke import FakeIO, image, options
from test_wire_contract import capture, serialize, IDENTITY
from wire_contract import ContractError


def test_calibration_with_explicit_bounds_keeps_false_verdicts(tmp_path):
    root, manifest = image(tmp_path)
    args = options(tmp_path, root)
    result = adapter.execute(args, backend=FakeIO(args, manifest))
    assert result['acquisition_passed'] and not result['passed'] and not result['qualification']
    assert result['sessions'][0]['timing']['p99_us'] == 20000
    assert result['bounds_sha256'] == smoke.digest(args.bounds)
    assert smoke.check_capture is adapter._check_capture


@pytest.mark.parametrize('field', ['max_us', 'last_ok_age_us'])
def test_calibration_cannot_self_calibrate_away_fixed_failure(field):
    value = capture(); value.intervals[0][field] = 68412
    with pytest.raises(ContractError, match='stall bound'):
        adapter.check_capture(serialize(value)+['PHASE closed'], [], manifest=dict(aec_enabled=False),
            board='echoear', calibration=True, bounds_path=smoke.BOUNDS_FILE)


def run_dir(tmp_path, bad=False):
    smoke.write(tmp_path/'result.json', dict(board='echoear', variant='normal', calibration=True,
        identity=IDENTITY, passed=False, acquisition_passed=True, error='retained original turn finding'))
    smoke.write(tmp_path/'identity.json', IDENTITY)
    value = capture()
    if bad:
        value.intervals[0]['last_ok_age_us'] = 68412
        value.intervals[0]['end_us'] += 48412  # keep hist span consistent; only fixed stall fails
    lines = serialize(value)
    (tmp_path/'main-serial.log').write_text('\n'.join(l for l in lines if not l.startswith('DEMO_CAPABILITY'))+'\nPHASE closed\n')
    (tmp_path/'main-bot.log').write_text('\n'.join(l for l in lines if l.startswith('DEMO_CAPABILITY'))+'\n')
    return smoke.digest(tmp_path/'result.json')


@pytest.mark.parametrize('bad', [False, True])
def test_recheck_enforces_fixed_limits_and_preserves_original(tmp_path, bad):
    original = run_dir(tmp_path, bad)
    result = adapter.recheck_run(tmp_path)
    assert result['checks_passed'] is not bad
    if bad: assert 'trailing open gap exceeds stall bound' in result['error']
    assert not result['passed'] and not result['qualification']
    assert result['source_error'] == 'retained original turn finding'
    assert smoke.digest(tmp_path/'result.json') == original
    with pytest.raises(Exception, match='exists'): adapter.recheck_run(tmp_path)


def test_cli_dispatch_is_scoped_and_requires_no_live_io(tmp_path):
    run_dir(tmp_path)
    assert adapter.main(['--recheck', str(tmp_path), '--bounds', str(smoke.BOUNDS_FILE)]) == 0
    assert smoke.execute is adapter._execute
    assert json.loads((tmp_path/'result-recheck-bounds.json').read_text())['checks_passed']


def test_explicit_calibration_bounds_fail_before_live_io(tmp_path):
    root,manifest=image(tmp_path);args=options(tmp_path,root,bounds=tmp_path/'missing-bounds.json')
    io=FakeIO(args,manifest)
    with pytest.raises(FileNotFoundError):adapter.execute(args,backend=io)
    assert io.calls==[] and not args.out.exists()
