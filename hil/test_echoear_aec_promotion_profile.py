"""Astra r8 blocker: part 1's inherited runtime profile guard (NovaHold.connect -> profile_witness) must see the
decision-113 bot profile, derived from the frozen q1 profile with only the reviewed demo_bot.py delta.

Through the real inherited V9Hold -> NovaHold.connect; only its acquisition superclass is faked.
"""
import copy
import json
from pathlib import Path

import pytest

import echoear_aec_nova_prestep as n
import echoear_aec_promotion_smoke as s
from demo_policy import profile_manifest

SPEC, _ = s.load_spec()
MANIFEST = Path(SPEC['image']['bundle'])/'manifest.json'
Q1 = json.loads(Path(SPEC['base_plan']['path']).read_text())['profile']


def plan():
    return s.build_plan(SPEC, json.loads(MANIFEST.read_text()))


def connect(tmp, monkeypatch, p, observed):
    """The real inherited connect(); FakeAcquisition stands in for BenchHold's hardware connect only."""
    import echoear_aec_v9 as v9
    class FakeAcquisition:
        def connect(self, cells, reconnects):
            self.acquired = True
    monkeypatch.setattr(n.diagnostic, 'diagnostic_hold', lambda budget: FakeAcquisition)
    H = v9.hold_type(None)
    assert 'NovaHold' in [c.__name__ for c in H.__mro__]
    h = H.__new__(H)
    h.p, h.out = p, tmp
    rows = [dict(event='stt-profile', **observed)]
    h.events = lambda: rows
    h.connect([], [])
    return h


def test_pinned_runtime_profile_equals_the_checked_out_bot():
    """Host-side tripwire: any future bot/policy/settings/package/model edit fails here, before a bench run."""
    live = profile_manifest('dev')
    assert plan()['profile'] == live
    assert live['code_sha256']['demo_bot.py'] == SPEC['runtime_profile']['demo_bot_sha256']
    assert live['config_sha256'] == SPEC['runtime_profile']['config_sha256']


def test_derivation_changes_exactly_the_bot_hash_and_its_aggregate():
    d = plan()['profile']
    assert {k for k in d if d[k] != Q1[k]} == {'code_sha256', 'config_sha256'}
    assert {k for k in d['code_sha256'] if d['code_sha256'][k] != Q1['code_sha256'][k]} == {'demo_bot.py'}
    assert Q1['code_sha256']['demo_bot.py'] == SPEC['runtime_profile']['qualified_demo_bot_sha256'] == 'c0cfd66be0ac17daa5a174e43553b0b4573d9f281ab33ef6930a9cd725126b32'


def test_inherited_connect_accepts_the_r8_bot_profile(tmp_path, monkeypatch):
    h = connect(tmp_path, monkeypatch, plan(), profile_manifest('dev'))
    assert h.acquired and json.loads((tmp_path/'connected-profile.json').read_text())['passed']


def test_inherited_connect_rejects_the_old_qualified_bot_profile(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match='Unexpected live Nova profile/vendor'):
        connect(tmp_path, monkeypatch, plan(), Q1)


def test_inherited_connect_with_the_unmodified_q1_plan_rejects_the_r8_bot(tmp_path, monkeypatch):
    """Astra's reproduction: without the derivation the r8 bot cannot connect."""
    with pytest.raises(RuntimeError, match='Unexpected live Nova profile/vendor'):
        connect(tmp_path, monkeypatch, dict(plan(), profile=Q1), profile_manifest('dev'))


def mutate(profile, path, value):
    p = copy.deepcopy(profile)
    d = p
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value
    p['config_sha256'] = s.profile_config_sha256(p)
    return p


@pytest.mark.parametrize('path,value', [
    (('code_sha256', 'demo_bot.py'), 'e'*64),                    # an unrelated further demo_bot edit
    (('code_sha256', 'demo_policy.py'), 'e'*64),                 # another server file
    (('config', 'vad', 'stop_secs'), 0.3),                       # a setting
    (('versions', 'pipecat-ai'), '1.13.0'),                      # a package version
    (('local_model_sha256', 'audio/vad/data/silero_vad.onnx'), 'e'*64),   # a model hash
])
def test_any_other_live_difference_fails_at_validate_and_run_time(path, value):
    live = mutate(profile_manifest('dev'), path, value)
    with pytest.raises(ValueError, match='Checked-out bot profile differs'):
        s.check_live_profile(plan(), live)


@pytest.mark.parametrize('path,value', [(('code_sha256', 'demo_policy.py'), 'e'*64), (('config', 'vad', 'stop_secs'), 0.3)])
def test_a_base_profile_other_than_the_pinned_qualified_one_fails_at_build_time(path, value):
    with pytest.raises(ValueError, match='Qualified profile differs'):
        s.derive_runtime_profile(SPEC, mutate(Q1, path, value))


def test_an_inconsistent_base_profile_fails_at_build_time():
    bad = copy.deepcopy(Q1); bad['config']['vad']['stop_secs'] = 0.3          # aggregate not recomputed
    with pytest.raises(ValueError, match='not self-consistent'):
        s.derive_runtime_profile(SPEC, bad)


def test_a_wrong_pinned_bot_hash_fails_at_build_time():
    spec = copy.deepcopy(SPEC); spec['runtime_profile']['demo_bot_sha256'] = 'e'*64
    with pytest.raises(ValueError, match='config_sha256 differs'):
        s.derive_runtime_profile(spec, Q1)


def test_validate_cli_checks_the_live_profile(monkeypatch):
    import demo_policy
    real = demo_policy.profile_manifest
    monkeypatch.setattr(demo_policy, 'profile_manifest', lambda *a: mutate(real(*a), ('code_sha256', 'demo_bot.py'), 'e'*64))
    with pytest.raises(ValueError, match='Checked-out bot profile differs'):
        s.main(['validate', '--manifest', str(MANIFEST), '--manifest-sha256', s.sha(MANIFEST)])


def test_run_checks_the_live_profile_before_any_acquisition(tmp_path, monkeypatch):
    called = []
    live = mutate(profile_manifest('dev'), ('config', 'vad', 'stop_secs'), 0.3)
    monkeypatch.setattr(s, 'launch_checks', lambda spec, out: None)
    with pytest.raises(ValueError, match='Checked-out bot profile differs'):
        s.run(MANIFEST, s.sha(MANIFEST), tmp_path/'out', run_part=lambda *a, **k: called.append(1), ledger_type=object,
              live_profile=live)
    assert not called and not (tmp_path/'out').exists()
