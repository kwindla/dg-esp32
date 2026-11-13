"""AEC promotion smoke runner (two parts). Fakes only: no bench, serial, socket, ALSA, bot or flash.

Part 1 is exercised through the REAL echoear_aec_v9.run_part with the v9 lifecycle test's hardware
fakes (test_echoear_aec_v9_lifecycle.Env: real V9Hold classes, real Runner, real ledger/rules, real
part_audit), with the promotion acoustic policy hook. Part 2's real reconnect path is exercised in
test_echoear_aec_reconnect.py; here only its composition into the promotion verdict is tested.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

import echoear_aec_promotion_smoke as s

QUAL = 'c'*64
NEW = 'a'*64
NORMAL = json.loads(s.SPEC.read_text())['normal_elf_sha256']
Q1_PROFILE = json.loads(Path(json.loads(s.SPEC.read_text())['base_plan']['path']).read_text())['profile']   # the real q1 profile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_world(tmp, *, proof_ok=True):
    bundle = tmp/'bundle-aec'
    (bundle/'bootloader').mkdir(parents=True)
    for name, data in (('dg-esp32.elf', b'elf'), ('dg-esp32.bin', b'bin'), ('bootloader/bootloader.bin', b'boot')):
        (bundle/name).write_bytes(data)
    files = {n: sha(bundle/n) for n in ('dg-esp32.elf', 'dg-esp32.bin', 'bootloader/bootloader.bin')}
    cfg = dict(CONFIG_DG_ECHOEAR_AEC=1, CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15, CONFIG_DG_ECHOEAR_AEC_GUARD_MS=400,
               CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS=2000, CONFIG_DG_DEMO_AUTOSTART=1, CONFIG_DG_WIFI_OBSERVE=1)
    manifest = dict(elf_sha256=NEW, aec_enabled=True, autostart=True, boot_ref_gain_db=15, firmware_commit='f'*40,
                    public_config=cfg, sha256=files)
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    proof = dict(masked_equal=proof_ok, unmasked_differing_bytes=0 if proof_ok else 4, flash_options_equal=True,
                 images=dict(app=dict(right=dict(sha256=files['dg-esp32.bin'], descriptor=dict(elf_sha256=NEW)),
                                      left=dict(descriptor=dict(elf_sha256=QUAL))),
                             bootloader=dict(right=dict(sha256=files['bootloader/bootloader.bin'])),
                             **{'partition-table': dict(raw_equal=True)}))
    (tmp/'proof.json').write_text(json.dumps(proof))
    (tmp/'stop.wav').write_bytes(b'stop')
    cells = [dict(logical_id=lid, kind='barge-firm-stop' if 'stop' in lid else 'echo', cell=dict(id=lid)) for lid in s.SCHEDULED]
    cells[2]['cell'].update(fixture=str(tmp/'stop.wav'), fixture_sha256=sha(tmp/'stop.wav'), stop_offset_s=0.9)
    base = dict(elf_sha256=QUAL, image='/old', boot_ref_gain_db=15, gain_db=15, blocks=[], outputs={}, reconnects=[],
                candidate=dict(elf_sha256=QUAL, firmware_commit='5'*40, playback_config=dict(elf_sha256=QUAL)),
                v9=dict(cells=cells, smoke_schedule=[]), sessions=[dict(id='qualification-01', volume_db=-6)],
                bot_config=dict(ECHOEAR_AEC_FULL_DUPLEX=None, ECHOEAR_LIVENESS_SECONDS='0.5'),
                profile=Q1_PROFILE)
    (tmp/'plan.json').write_text(json.dumps(base))
    plan2 = json.loads((s.ROOT/'hil/plans/echoear-aec-reconnect-v1.json').read_text())
    plan2['image'].update(bundle=str(bundle), elf_sha256=NEW, firmware_commit='f'*40)
    rel = tmp/'reconnect-plan.json'
    rel.write_text(json.dumps(plan2))
    spec = json.loads(s.SPEC.read_text())
    spec.update(image=dict(spec['image'], bundle=str(bundle), elf_sha256=NEW, firmware_commit='f'*40, proof_of_record=str(tmp/'proof.json')),
                qualified=dict(spec['qualified'], elf_sha256=QUAL),
                base_plan=dict(path=str(tmp/'plan.json'), sha256=sha(tmp/'plan.json')),
                reconnect_part=dict(spec['reconnect_part'], plan=str(rel), plan_sha256=sha(rel)),
                output=str(tmp/'out'),
                budget=dict(spec['budget'], exclusion_local_minutes=[180, 180]),       # no exclusion in tests
                stop_fixture=dict(path=str(tmp/'stop.wav'), sha256=sha(tmp/'stop.wav'), offset_s=0.9, cell='v9-stop-firm-r01'),
                bot=dict(spec['bot'], bot_config=dict(ECHOEAR_AEC_FULL_DUPLEX=None, ECHOEAR_LIVENESS_SECONDS='0.5')))
    (tmp/'spec.json').write_text(json.dumps(spec))
    return spec, bundle/'manifest.json'


# -- inputs ------------------------------------------------------------------------------------

def test_real_spec_is_bound_to_the_frozen_inputs():
    spec, _ = s.load_spec()
    assert spec['image']['elf_sha256'].startswith('69d5d764') and spec['qualified']['elf_sha256'].startswith('cbabb650')
    assert spec['schedule']['blocks'][0]['attempts'] == list(s.SCHEDULED)
    assert spec['normal_elf_sha256'].startswith('9936da45') and spec['output'].endswith('merge-9d-r3/aec-promotion-smoke-04') and spec['id'] == 'aec-promotion-smoke-04'
    plan2, plan2_sha = s.reconnect_plan(spec)
    assert plan2['id'] == 'aec-reconnect-01' and plan2_sha == spec['reconnect_part']['plan_sha256']
    assert 'reconnect' not in spec and '133f587' in spec['bot']['commit']
    for step in ('cold', 'warm'):
        assert 'TTSSpeakFrame' in next(x for x in spec['steps'] if x['id'] == step)['check']


def test_bundle_manifest_sha_identity_files_and_proof_are_all_required(tmp_path):
    spec, manifest = make_world(tmp_path)
    assert s.verify_bundle_manifest(spec, manifest, sha(manifest))['elf_sha256'] == NEW
    with pytest.raises(ValueError, match='sha256 mismatch'):
        s.verify_bundle_manifest(spec, manifest, '0'*64)
    (manifest.parent/'dg-esp32.bin').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        s.verify_bundle_manifest(spec, manifest, sha(manifest))
    spec2, manifest2 = make_world(tmp_path/'b', proof_ok=False)
    with pytest.raises(ValueError, match='Masked-compare'):
        s.verify_bundle_manifest(spec2, manifest2, sha(manifest2))


def test_part1_plan_changes_only_the_candidate_identity_and_schedule(tmp_path):
    spec, manifest = make_world(tmp_path)
    m = s.verify_bundle_manifest(spec, manifest, sha(manifest))
    base = json.loads((tmp_path/'plan.json').read_text())
    p = s.build_plan(spec, m)
    assert p['elf_sha256'] == NEW and p['image'] == spec['image']['bundle'] and p['reconnects'] == [] and p['blocks'] == []
    changed = {k for k in set(p) | set(base) if p.get(k) != base.get(k)}
    assert changed == {'elf_sha256', 'image', 'candidate', 'v9', 'outputs', 'promotion_smoke', 'profile'}   # profile: decision 113
    assert {k for k in p['profile'] if p['profile'][k] != base['profile'][k]} == {'code_sha256', 'config_sha256'}
    assert p['v9']['cells'] == base['v9']['cells'] and p['bot_config'] == base['bot_config']
    (tmp_path/'plan.json').write_text(json.dumps(dict(base, reconnects=[dict(id='r')])))
    bad = copy.deepcopy(spec); bad['base_plan']['sha256'] = sha(tmp_path/'plan.json')
    with pytest.raises(ValueError, match='no reconnect'):
        s.build_plan(bad, m)


def test_pins_bot_settings_stop_fixture_and_reconnect_plan(tmp_path):
    spec, manifest = make_world(tmp_path)
    m = s.verify_bundle_manifest(spec, manifest, sha(manifest))
    bad = copy.deepcopy(spec); bad['bot']['bot_config']['ECHOEAR_LIVENESS_SECONDS'] = '0'
    with pytest.raises(ValueError, match='Bot settings'):
        s.build_plan(bad, m)
    bad = copy.deepcopy(spec); bad['reconnect_part']['plan_sha256'] = '0'*64
    with pytest.raises(ValueError, match='Reconnect plan changed'):
        s.reconnect_plan(bad)
    (tmp_path/'stop.wav').write_bytes(b'changed')
    with pytest.raises(ValueError, match='Stop fixture'):
        s.build_plan(spec, m)


# -- Astra 3: the promotion gate policy in acquisition --------------------------------------------

def test_policy_hook_moves_acoustic_score_findings_to_evidence_and_keeps_gated_ones():
    def base(view, plan, entry, a):
        return (dict(acoustic=dict(result='miss', established=True), events=dict(result='miss')),
                [dict(gate='echo.hard_floor'), dict(gate='echo.whole_reply'), dict(gate='echo.false_turns'),
                 dict(gate='flush.callback'), dict(gate='hardware.aec_errors')], dict(x=1))
    sc, kept, rec = s.promotion_acoustic(base)(None, None, dict(kind='echo'), None)
    assert {f['gate'] for f in kept} == {'echo.false_turns', 'flush.callback', 'hardware.aec_errors'}
    assert {f['gate'] for f in rec['promotion_report_only']} == {'echo.hard_floor', 'echo.whole_reply'}
    assert sc['acoustic']['result'] == 'ineligible' and rec['promotion_acoustic_score']['result'] == 'miss'
    assert sc['events']['result'] == 'miss'                    # false-event harm is not touched
    ro = s.report_only_gates()
    assert 'echo.hard_floor' in ro and 'echo.false_turns' not in ro and 'flush.stale_audio' not in ro


@pytest.fixture(scope='module')
def promotion_plan():
    import echoear_aec_v9_plan as plan_mod
    if not plan_mod.SOURCE.exists():
        pytest.skip('frozen inputs not on this host')
    spec, _ = s.load_spec()
    manifest = Path(spec['image']['bundle'])/'manifest.json'
    return s.build_plan(spec, s.verify_bundle_manifest(spec, manifest, sha(manifest)))


def real_part1(tmp_path, monkeypatch, plan, scenario):
    import echoear_aec_v9 as v9
    from echoear_aec_v9_ledger import Ledger
    from test_echoear_aec_v9_lifecycle import Env, FakeBudget
    env = Env(tmp_path, monkeypatch, scenario=scenario); env.p = plan; env._part = 1
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='smoke', plan_sha256='f'*64, runtime_sha256={}, drain_allowance_s=4.0))
    v9.run_part(plan, out, ledger, 1, mode='smoke', allowance_s=4.0, hold_factory=env.hold_factory,
                budget_factory=lambda contract: FakeBudget(env.clock, contract), acoustic=s.promotion_acoustic(env.acoustic),
                clock=env.clock, sleep=lambda x: env.clock.advance(x))
    end = [r for r in ledger.rows if r['type'] == 'part-end'][-1]
    return env, ledger, end


def test_real_run_part_acoustic_hard_floor_does_not_stop_the_smoke(tmp_path, monkeypatch, promotion_plan):
    env, ledger, end = real_part1(tmp_path, monkeypatch, promotion_plan, {('score', s.SCHEDULED[0], 1): 'hard_floor'})
    assert end['reason'] == 'complete' and 'echo.hard_floor' not in end['gates'] and end['restore']['clean']
    row = next(r for r in ledger.rows if r['type'] == 'attempt' and r['logical_id'] == s.SCHEDULED[0])
    assert 'echo.hard_floor' in {f['gate'] for f in row['record']['promotion_report_only']}
    assert [r['logical_id'] for r in ledger.rows if r['type'] == 'attempt' and r['kind'] != 'near-witness'] == list(s.SCHEDULED)


@pytest.mark.parametrize('spec,gate', [('fault', 'hardware.aec_errors'), ('false', 'echo.false_turns')])
def test_real_run_part_hardware_fault_or_gated_false_turn_stops(tmp_path, monkeypatch, promotion_plan, spec, gate):
    env, ledger, end = real_part1(tmp_path, monkeypatch, promotion_plan, {('score', s.SCHEDULED[0], 1): spec})
    assert end['reason'] == 'STOP' and gate in end['gates'] and end['restore']['clean']


def test_real_run_part_audit_is_the_unmodified_single_generation_auditor(tmp_path, monkeypatch, promotion_plan):
    env, ledger, end = real_part1(tmp_path, monkeypatch, promotion_plan, {})
    assert end['reason'] == 'complete' and end['audit']['clean'], end['gates']
    assert not any(f['gate'] == 'admission.counter_schema' for f in end['audit']['findings'])


# -- Fable B2: Stop accounting by attempt windows ---------------------------------------------------

def write_hold(out, *, cancels=((150, 1),), witness_cancels=((50, 1), (250, 1)), events_delta=1, aec_errors=0,
               restore_clean=True, capability=True, rendered=(10, 30), stamps=True):
    hold = out/'part-1'
    (hold/'bot').mkdir(parents=True)
    lines = ['I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+NEW]
    if capability:
        lines.append('I (2) app: AUDIO_CAPABILITY aec=1 ref_gain_db=15 guard_ms=400')
    for us, applied in tuple(witness_cancels)+tuple(cancels):
        lines.append(f'I (3) app: AUDIO_CANCEL callback_us=1 device_us={us} gen=1 applied={applied} mute_ms=150')
    (hold/'candidate-serial.log').write_text('\n'.join(lines)+'\n')
    (hold/'candidate-image.json').write_text(json.dumps(dict(elf_sha256=NEW)))
    (hold/'candidate-boot-gain.json').write_text(json.dumps(dict(verified=True, declared_gain_db=15, boot_line='MIC3_GAIN=0x15 analog_db=15')))
    (hold/'bot/server-1.log').write_text('DEMO_DUPLEX aec_full_duplex=True user_mute=False source=capability-or-safe-fallback\n')
    aec = dict(enabled=True, errors=aec_errors, reference_clips=0, output_clips=0,
               capture=dict(read_errors=0, read_timeouts=0, adc_clips=0, gain_clips=0))
    (hold/'connected.json').write_text(json.dumps(dict(aec=aec, display=dict(rendered=rendered[0], failed=False, dead=False))))
    (hold/'settled-end.json').write_text(json.dumps(dict(aec=aec, display=dict(rendered=rendered[1], failed=False, dead=False),
                                                         liveness=dict(pings=100, pongs=100, timeouts=0))))
    windows = [('v9-witness-p1-01', 'near-witness', 40, 60), (s.SCHEDULED[0], 'echo', 70, 90), (s.SCHEDULED[1], 'echo', 100, 120),
               (s.SCHEDULED[2], 'barge-firm-stop', 140, 160), ('v9-witness-p1-02', 'near-witness', 240, 260)]
    rows = []
    for lid, kind, lo, hi in windows:
        folder = hold/f'{lid}-a1'
        folder.mkdir()
        ev = events_delta if kind == 'barge-firm-stop' else 1 if kind == 'near-witness' else 0
        (folder/'before.json').write_text(json.dumps(dict(device_us=lo, cancellation=dict(events=10, duplicate_notices=3))))
        (folder/'after.json').write_text(json.dumps(dict(device_us=hi, cancellation=dict(events=10+ev, duplicate_notices=4))))
        rows.append(dict(type='attempt', attempt_id=f'p1/{lid}-a1', logical_id=lid, kind=kind, error=None, pending=False,
                         findings=[dict(gate='echo.whole_reply')], folder=f'part-1/{lid}-a1'))
    rows.append(dict(type='part-end', reason='complete', error=None, over_budget=False, audit=dict(clean=True), gates=[],
                     restore=dict(clean=restore_clean, restored_elf_sha256=NORMAL)))
    if stamps:      # the promotion wrapper's final-disconnect stamp (decision 114; required for a completed part)
        (out/s.DISCONNECT_STAMPS).write_text(json.dumps([dict(command='disconnect', ok=True, lower_wall=1000.0,
                                                               sent_wall=1000.001, sent_mono=10.0)]))
    return rows


def judged(tmp_path, **kw):
    spec, _ = make_world(tmp_path)
    return s.judge(spec, {}, write_hold(tmp_path/'o', **kw), tmp_path/'o')


def test_part1_passes_with_witness_stops_attributed_and_one_stop_cancel(tmp_path):
    v = judged(tmp_path)
    assert v['outcome'] == 'PASS', {k: c for k, c in v['checks'].items() if not c['passed']}
    stop = v['checks']['stop']
    assert stop['stop_window_cancels'] == 1 and stop['witness_cancels'] == {'p1/v9-witness-p1-01-a1': 1, 'p1/v9-witness-p1-02-a1': 1}
    assert stop['total_cancel_lines'] == 3 and not stop['unattributed_cancels']


@pytest.mark.parametrize('kw', [dict(cancels=((150, 1), (155, 1))), dict(cancels=((150, 0),)), dict(cancels=()),
                                dict(cancels=((150, 1), (80, 1))), dict(cancels=((150, 1), (200, 1))), dict(events_delta=2)])
def test_stop_accounting_rejects_extra_unapplied_missing_echo_window_or_unattributed_cancels(tmp_path, kw):
    v = judged(tmp_path, **kw)
    assert not v['checks']['stop']['passed'] and v['outcome'] != 'PASS'


@pytest.mark.parametrize('kw,check', [(dict(aec_errors=1), 'counters'), (dict(restore_clean=False), 'restore'),
                                      (dict(capability=False), 'capability'), (dict(rendered=(10, 10)), 'display')])
def test_judge_fails_each_declared_check(tmp_path, kw, check):
    v = judged(tmp_path, **kw)
    assert v['outcome'] != 'PASS' and not v['checks'][check]['passed']


def test_judge_gates_false_turns_and_flush_but_not_acoustics(tmp_path):
    spec, _ = make_world(tmp_path)
    rows = write_hold(tmp_path/'o')
    rows[1]['findings'].append(dict(gate='echo.false_turns'))
    rows[3]['findings'].append(dict(gate='flush.stale_audio'))
    v = s.judge(spec, {}, rows, tmp_path/'o')
    assert not v['checks']['cold']['passed'] and not v['checks']['stop']['passed'] and not v['checks']['recovery']['passed']


# -- composition, launch, ledger ------------------------------------------------------------------

class FakeLedger:
    def __init__(self, path, create=False):
        assert create and not Path(path).exists()
        Path(path).touch(); self.rows = []
    def append(self, row):
        self.rows.append(row); return row


def run(tmp_path, monkeypatch, *, part1_ok=True, part2_ok=True):
    spec, manifest = make_world(tmp_path)
    monkeypatch.setattr(s.sys.stdin, 'isatty', lambda: True)
    import echoear_rollback as r
    monkeypatch.setattr(r, 'head', lambda: 'h'*40)
    seen = {}
    def fake_run_part(p, out, ledger, part, **kw):
        seen.update(part=part, kw=sorted(kw), elf=p['elf_sha256'], campaign=ledger.rows[0])
        for row in write_hold(out, restore_clean=part1_ok):
            ledger.append(row)
    class Part2:
        def __init__(self, decl, plan, out): seen.update(decl=decl, plan2=plan['id'], out2=out)
    def execute(live, out):
        (out/'gate.json').write_text(json.dumps(dict(verdict=dict(outcome='PASS' if part2_ok else 'FAIL'))))
        return dict(passed=part2_ok, lock_released=True, cleanup_attempts=[dict(restored_elf_sha256=NORMAL)])
    v = s.run(manifest, sha(manifest), tmp_path/'out', spec_path=tmp_path/'spec.json', run_part=fake_run_part,
              ledger_type=FakeLedger, part2=Part2, execute=execute)
    return v, seen


def test_run_composes_part1_then_part2_and_records_runtime_identity(tmp_path, monkeypatch):
    v, seen = run(tmp_path, monkeypatch)
    assert v['outcome'] == 'PASS' and v['part1']['outcome'] == 'PASS' and v['part2']['outcome'] == 'PASS'
    assert seen['kw'] == ['acoustic', 'allowance_s', 'mode'] and seen['part'] == 1 and seen['elf'] == NEW
    assert seen['campaign']['runtime_sha256'] and 'hil/echoear_aec_promotion_smoke.py' in seen['campaign']['runtime_sha256']
    assert seen['plan2'] == 'aec-reconnect-01' and seen['decl']['bot_commit'] == 'h'*40
    assert json.loads((tmp_path/'out/promotion-verdict.json').read_text())['outcome'] == 'PASS'


def test_part2_runs_only_after_a_part1_pass_and_any_failure_fails(tmp_path, monkeypatch):
    v, seen = run(tmp_path, monkeypatch, part1_ok=False)
    assert v['part2']['outcome'] == 'NOT-RUN' and 'plan2' not in seen and v['outcome'] == 'FAIL'
    (tmp_path/'b').mkdir()
    v, seen = run(tmp_path/'b', monkeypatch, part2_ok=False)
    assert v['part1']['outcome'] == 'PASS' and v['part2']['outcome'] == 'FAIL' and v['outcome'] == 'FAIL'


def test_run_refuses_existing_output_terminal_and_exclusion(tmp_path, monkeypatch):
    spec, manifest = make_world(tmp_path)
    monkeypatch.setattr(s.sys.stdin, 'isatty', lambda: True)
    (tmp_path/'out').mkdir()
    with pytest.raises(FileExistsError):
        s.launch_checks(spec, tmp_path/'out')
    (tmp_path/'out').rmdir()
    monkeypatch.setattr(s.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(ValueError, match='terminal'):
        s.launch_checks(spec, tmp_path/'out')
    import datetime
    real, _ = s.load_spec()
    with pytest.raises(ValueError, match='exclusion'):
        s.launch_checks(real, real['output'], now=datetime.datetime(2026, 10, 26, 2, 30))


def test_launcher_pins_head_refuses_existing_output_and_verifies_the_restores():
    launcher = Path(json.loads(s.SPEC.read_text())['launcher'])
    text = launcher.read_text()
    assert 'set -euo pipefail' in text and '[[ -e "$OUT" ]]' in text and 'test -t 0 && test -t 1' in text
    assert 'EXPECTED_HEAD=' in text and 'git rev-parse HEAD' in text
    assert 'echoear_aec_promotion_smoke.py run' in text and '--confirm-bench' in text
    assert '9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758' in text and 'part-2/result.json' in text
    out = Path(json.loads(s.SPEC.read_text())['output'])
    assert launcher.parent == out.parent and f'EV={out.parent}' in text and f'OUT="$EV/{out.name}"' in text   # Fable r6 should-fix 5



@pytest.mark.parametrize('witness', [((50, 1), (55, 1), (250, 1)), ((50, 0), (250, 1))])
def test_witness_window_cancels_are_bounded_by_their_events_delta_and_applied(tmp_path, witness):
    v = judged(tmp_path, witness_cancels=witness)
    assert not v['checks']['stop']['passed'] and v['checks']['stop']['unbounded_windows']


R = dict(passed=True, lock_released=True, cleanup_attempts=[dict(restored_elf_sha256=NORMAL, errors=[])])


@pytest.mark.parametrize('result,gate,outcome', [
    (R, 'PASS', 'PASS'),
    (dict(R, passed=False, error='RuntimeError: after the gate'), 'PASS', 'INCOMPLETE'),
    (dict(R, passed=False, cleanup_attempts=[dict(errors=['RESTORE: x']), dict(restored_elf_sha256=NORMAL, errors=[])]), 'PASS', 'INCOMPLETE'),
    (dict(R, passed=False, error='TimeoutError: Owned bot startup'), None, 'INCOMPLETE'),
    (dict(R, passed=False, error='RuntimeError: AEC reconnect check FAIL'), 'FAIL', 'FAIL'),
    (dict(R, passed=False, error='RuntimeError: INVALID'), 'INVALID', 'INVALID'),
    (dict(R, passed=False, error='RuntimeError: INCOMPLETE'), 'INCOMPLETE', 'INCOMPLETE'),
    (dict(R, lock_released=False), 'PASS', 'INCOMPLETE'),
])
def test_part2_outcome_honours_the_execution_result(result, gate, outcome):
    o = s.part2_outcome(result, dict(verdict=dict(outcome=gate)) if gate else None, NORMAL)
    assert o['outcome'] == outcome
    if outcome != 'PASS':
        assert o['outcome'] != 'PASS'
    if len(result['cleanup_attempts']) > 1:
        assert o['reason'].startswith('first cleanup failed') and o['bench_restored']


def test_run_never_reports_pass_over_a_failed_part2_execution(tmp_path, monkeypatch):
    spec, manifest = make_world(tmp_path)
    monkeypatch.setattr(s.sys.stdin, 'isatty', lambda: True)
    import echoear_rollback as r
    monkeypatch.setattr(r, 'head', lambda: 'h'*40)
    def fake_run_part(p, out, ledger, part, **kw):
        for row in write_hold(out):
            ledger.append(row)
    class Part2:
        def __init__(self, decl, plan, out): pass
    seen = {}
    def execute(live, out):
        import signal as sg
        seen['term'] = sg.getsignal(sg.SIGTERM); seen['alarm'] = sg.getitimer(sg.ITIMER_REAL)[0]
        (out/'gate.json').write_text(json.dumps(dict(verdict=dict(outcome='PASS'))))
        return dict(passed=False, error='RuntimeError: injected execution failure after gate', lock_released=True,
                    cleanup_attempts=[dict(restored_elf_sha256=NORMAL, errors=[])])
    v = s.run(manifest, sha(manifest), tmp_path/'out', spec_path=tmp_path/'spec.json', run_part=fake_run_part,
              ledger_type=FakeLedger, part2=Part2, execute=execute)
    assert v['outcome'] == 'INCOMPLETE' and v['part2']['outcome'] == 'INCOMPLETE' and not v['part2']['execution_passed']
    import signal as sg
    assert seen['term'] not in (sg.SIG_DFL, None) and seen['alarm'] > 0          # part 2 ran inside the protected wrapper


def test_part2_crash_escaping_the_wrapper_is_recorded_not_misreported(tmp_path, monkeypatch):
    spec, manifest = make_world(tmp_path)
    monkeypatch.setattr(s.sys.stdin, 'isatty', lambda: True)
    import echoear_rollback as r
    monkeypatch.setattr(r, 'head', lambda: 'h'*40)
    def fake_run_part(p, out, ledger, part, **kw):
        for row in write_hold(out):
            ledger.append(row)
    class Part2:
        def __init__(self, decl, plan, out): pass
    def execute(live, out):
        raise OSError('release failed after a verified restore')
    with pytest.raises(OSError):
        s.run(manifest, sha(manifest), tmp_path/'out', spec_path=tmp_path/'spec.json', run_part=fake_run_part,
              ledger_type=FakeLedger, part2=Part2, execute=execute)
    v = json.loads((tmp_path/'out/promotion-verdict.json').read_text())
    assert v['part1']['outcome'] == 'PASS' and v['part2']['outcome'] == 'INCOMPLETE' and 'raised' in v['part2']['reason']
    assert v['outcome'] == 'INCOMPLETE'


def test_part2_outcome_reason_on_the_journal_failure_path():
    o = s.part2_outcome(dict(passed=False, lock_released=True, cleanup_attempts=[dict(restored_elf_sha256=NORMAL, errors=[])]),
                        dict(verdict=dict(outcome='PASS')), NORMAL)
    assert o['outcome'] == 'INCOMPLETE' and 'retry' not in o['reason'] and 'one clean cleanup' in o['reason']


def test_r7_part2_crash_after_a_witnessed_gate_fail_keeps_the_fail(tmp_path, monkeypatch):
    """Fable r6 should-fix 4: a crash escaping the wrapper must not downgrade a witnessed FAIL to INCOMPLETE."""
    spec, manifest = make_world(tmp_path)
    monkeypatch.setattr(s.sys.stdin, 'isatty', lambda: True)
    import echoear_rollback as r
    monkeypatch.setattr(r, 'head', lambda: 'h'*40)
    def fake_run_part(p, out, ledger, part, **kw):
        for row in write_hold(out):
            ledger.append(row)
    class Part2:
        def __init__(self, decl, plan, out): pass
    def execute(live, out):
        (out/'gate.json').write_text(json.dumps(dict(verdict=dict(outcome='FAIL'))))
        raise OSError('release failed after a verified restore')
    with pytest.raises(OSError):
        s.run(manifest, sha(manifest), tmp_path/'out', spec_path=tmp_path/'spec.json', run_part=fake_run_part,
              ledger_type=FakeLedger, part2=Part2, execute=execute)
    v = json.loads((tmp_path/'out/promotion-verdict.json').read_text())
    assert v['part2']['outcome'] == 'FAIL' and v['part2']['gate_outcome'] == 'FAIL' and 'raised' in v['part2']['reason']
    assert v['outcome'] == 'FAIL'
