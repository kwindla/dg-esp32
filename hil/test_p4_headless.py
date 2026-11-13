"""Offline headless lifecycle tests: real smoke restoration holds, mocked IO."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import board_smoke as smoke
import p4_headless as headless
from wire_contract import ContractError


@pytest.fixture
def case(tmp_path, monkeypatch):
    (tmp_path/'dependencies.lock').write_text('test-s3-lock')
    (tmp_path/'dependencies.p4xc.lock').write_text('test-p4-lock')
    identity = tmp_path/'identity.json'; identity.write_text('{}')
    monkeypatch.setattr(headless, 'ROOT', tmp_path)
    monkeypatch.setattr(headless, 'verify_bundle', lambda *args: (dict(profile='normal', aec_enabled=False), {}))
    args = SimpleNamespace(board='p4xc', bundle=tmp_path/'bundle', out=tmp_path/'run', bot_identity=identity,
                           pair_left=None, p4_restore_bundle=None, p4_restore_elf=None, p4_factory_backup=tmp_path/'backup')
    parent = SimpleNamespace(args=args, out=args.out, python='mock-idf', preflight_restore=Mock())
    parent.hold = lambda: smoke.LiveIO.hold(parent)
    return args, parent


def acquired(*args):
    return dict(boot_verified=True, session_generation=1)


def analyzed(*args):
    return dict(wire_sha256='checked', wire_pair_checked=False)


@pytest.mark.parametrize('failure', [None, RuntimeError('device lost'), KeyboardInterrupt('abort')])
def test_s3_restores_image_of_record_on_success_failure_and_abort(case, monkeypatch, failure):
    args, parent = case; args.board = 'echoear'
    import echoear_item2_campaign as campaign
    import echoear_restore_verify as restore
    events = []
    monkeypatch.setattr(restore, 'boot_proof', lambda path, expected: events.append(('proof', expected)) or dict(passed=True))
    def restore_bench(out, *, wait=None, verify=None):
        events.append(('restore', campaign.NORMAL)); verify(out, 'restored')
        return dict(clean=True, restored_elf_sha256=campaign.NORMAL)
    monkeypatch.setattr(campaign, 'restore_bench', restore_bench)
    def acquisition(*unused):
        if failure: raise failure
        return acquired()
    def analysis(*unused):
        events.append(('analysis', None)); return analyzed()
    result = headless.execute(args, io=parent, acquire_fn=acquisition, analyze_fn=analysis)
    assert events[:2] == [('restore', smoke.NORMAL_HASH), ('proof', smoke.NORMAL_HASH)]
    assert result['restore']['restored_elf_sha256'] == smoke.NORMAL_HASH
    assert result['acquisition_passed'] is (failure is None)
    assert (('analysis', None) in events) == (failure is None)


def test_p4_restore_retries_and_parks_without_releasing_the_hold(case, monkeypatch):
    args, parent = case
    from echoear_rollback import Live
    monkeypatch.setattr(headless.boards, 'require_bench', lambda: 42)
    calls = []
    def restore(command, **kw):
        assert kw['pass_fds'] == (42,)
        assert 'restore_factory.py' in command[1]
        calls.append('restore')
        if len(calls) < 3: raise OSError('CH343 temporarily absent')
    monkeypatch.setattr(smoke.subprocess, 'run', restore)
    parked = Mock(); monkeypatch.setattr(Live, 'wait_recovery', parked)
    def lost(*unused): raise RuntimeError('partial flash')
    result = headless.execute(args, io=parent, acquire_fn=lost, analyze_fn=Mock())
    assert calls == ['restore']*3 and parked.call_count == 2
    assert len(result['restore']['attempts']) == 2
    assert json.loads((args.out/'restore.json').read_text())['lock_retained'] is True
    assert result['device_acquisition_failed'] and not result['acquisition_passed']


def test_host_analysis_failure_leaves_successfully_acquired_p4_installed(case, monkeypatch):
    args, parent = case
    restore = Mock(); monkeypatch.setattr(smoke.subprocess, 'run', restore)
    def broken_analysis(*unused): raise KeyError('missing host reference')
    result = headless.execute(args, io=parent, acquire_fn=acquired, analyze_fn=broken_analysis)
    restore.assert_not_called()
    assert result['restore']['action'] == 'keep-tested-p4'
    assert result['analysis_failed'] and result['analysis_disposition'] == 'acquired P4 left installed'
    assert not result['acquisition_passed']
    image = json.loads((args.out/'image.json').read_text())
    assert result['target_dependencies_lock_sha256'] == headless.digest(headless.ROOT/'dependencies.p4xc.lock')
    assert result['target_dependencies_lock_sha256'] == image['target_dependencies_lock_sha256']


@pytest.mark.parametrize('passed', [False, None, 'true'])
def test_pair_left_requires_a_passed_s3_acquisition_before_io(case, passed):
    args, parent = case; left = args.out.parent/'left'; left.mkdir(); args.pair_left = left
    (left/'wire.log').write_text('incomplete wire capture')
    headless.write(left/'acquisition.json', dict(board='echoear', boot_verified=True, acquisition_passed=passed))
    with pytest.raises(ContractError, match='S3 acquisition did not pass'):
        headless.execute(args, io=parent, acquire_fn=Mock(), analyze_fn=Mock())
    parent.preflight_restore.assert_not_called(); assert not args.out.exists()


def test_pair_left_checks_boot_board_identity_and_wire_integrity(case):
    args, _ = case; left = args.out.parent/'left'; left.mkdir(); args.pair_left = left
    (left/'wire.log').write_text('complete wire capture'); headless.write(left/'identity.json', {})
    record = dict(board='echoear', boot_verified=True, acquisition_passed=True, wire_sha256=headless.digest(left/'wire.log'))
    headless.write(left/'acquisition.json', record); headless.check_pair_left(args, {})
    for key, value in (('board', 'p4xc'), ('boot_verified', False), ('wire_sha256', 'wrong')):
        headless.write(left/'acquisition.json', dict(record, **{key: value}))
        with pytest.raises(ContractError): headless.check_pair_left(args, {})
    headless.write(left/'acquisition.json', record)
    with pytest.raises(ContractError, match='bot identity differs'): headless.check_pair_left(args, {'changed': True})
