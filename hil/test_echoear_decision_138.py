"""r15: decision 138a (S1 device send cadence gate, S2 counter stand-in), 138b (resume after a clean deferral) and the
SIGINT final-record fix. Host-only; device, bench and bot boundaries are faked as in test_echoear_requal_n1_r2."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_echoear_requal_n1_r2 as T
from test_echoear_requal_n1_r2 import fake_restore, fake_item2_environment, seg, run_row, final, write_attempt, acceptance_population  # noqa: F401
import echoear_item2_campaign as item2
import echoear_image_profile as profile
import echoear_source_cadence as sc
import e2e_common
from echoear_requalification import load_plan, observation_gate

D138 = T.N1_PLAN.with_name('echoear-item2-requalification-n1-d138.json')
D139 = T.N1_PLAN.with_name('echoear-item2-requalification-n1-d139.json')


# ---- plan revision -----------------------------------------------------------------------------------------------

def test_d138_plan_is_a_revision_derived_from_the_frozen_plan():
    new, old = load_plan(D138), json.loads(T.N1_PLAN.read_text())
    assert new['derived_from']['sha256'] == hashlib.sha256(T.N1_PLAN.read_bytes()).hexdigest()
    assert 'decision_138' not in old and new['decision_138']['uplink_gate'] == 'source-cadence'
    assert new['decision_138']['source_cadence']['limit_ms'] == sc.SOURCE_GAP_LIMIT_MS == 60
    assert new['decision_138']['s2']['enqueue_send_max_us'] == sc.ENQUEUE_SEND_BOUND_US
    assert new['decision_138']['resume_after_clean_deferral'] is True
    changed = {k for k in set(new) | set(old) if new.get(k) != old.get(k)}
    assert changed == {'status', 'name', 'derived_from', 'previous_derived_from', 'strict_gates', 'environment', 'decision_138'}
    assert {k for k in new['strict_gates'] if new['strict_gates'][k] != old['strict_gates'][k]} == {'uplink_gate'}
    assert {k for k in new['environment'] if new['environment'][k] != old['environment'][k]} == {'deferrals'}
    assert {k for k in new['environment']['deferrals'] if new['environment']['deferrals'].get(k) != old['environment']['deferrals'].get(k)} == {'policy_d138'}


# ---- S1 source window ----------------------------------------------------------------------------------------------

def trace_rows(n, *, t0_us=1_000_000, gen=1, ssrc=7, period_us=20000, gap_at=None, gap_us=0, skip_seq_at=None):
    rows, t, seq = [], t0_us, 100
    for i in range(n):
        if gap_at is not None and i == gap_at: t += gap_us
        if skip_seq_at is not None and i == skip_seq_at: seq += 1
        rows.append(dict(device_us=t, timestamp=seq*960, ssrc=ssrc, value=0, aux=0, seq=seq % 65536, bytes=8, kind=12, flags=0, reserved=gen))
        t += period_us; seq += 1
    return rows


def sample_for(rows, *, start_us=1_500_000, end_us=4_000_000, gen=1, overwritten=(0, 0, 0), enabled=True, pc='p'):
    trace = lambda o: dict(enabled=enabled, overwritten=o, capacity=8192)
    before = dict(device_us=start_us-400_000, pc_id=pc, generation=gen, peer_profile=dict(trace=trace(overwritten[0])))
    after = dict(device_us=end_us, pc_id=pc, generation=gen, peer_profile=dict(trace=trace(overwritten[1])))
    return dict(before_perf=before, before_perf_completed_at=100.0, prompt_start=100.4,
                reply_window_transport=dict(after=after), source_trace_export=dict(overwritten=overwritten[2]))


def test_s1_passes_a_clean_cadence_and_fails_a_source_gap():
    rows = trace_rows(250)
    r = sc.source_window(sample_for(rows), rows)
    assert r['complete'] and r['passed'] is True and r['max_gap_ms'] == 20 and r['over_limit'] == 0
    gap = trace_rows(250, gap_at=100, gap_us=45000)          # one 65 ms send interval inside the window
    r = sc.source_window(sample_for(gap), gap)
    assert r['complete'] and r['passed'] is False and r['over_limit'] == 1 and r['max_gap_ms'] == 65


def test_s1_console_window_is_informational():
    gap = trace_rows(250, gap_at=100, gap_us=45000)
    t = gap[99]['device_us']
    r = sc.source_window(sample_for(gap), gap, [dict(start_us=t-120000, end_us=t+120000, category='console')])
    assert r['complete'] and r['passed'] is True and r['console_excluded']['count'] >= 1 and r['console_excluded']['max_gap_ms'] == 65


@pytest.mark.parametrize('defect', ['overwritten', 'export-overwritten', 'disabled', 'other-session', 'no-start-cover', 'no-end-cover', 'seq-skip', 'two-streams', 'no-endpoints'])
def test_s1_incomplete_trace_fails_closed_as_harness_failure(defect):
    rows = trace_rows(250); kw = {}
    if defect == 'overwritten': kw = dict(overwritten=(0, 5, 5))
    if defect == 'export-overwritten': kw = dict(overwritten=(0, 0, 3))
    if defect == 'disabled': kw = dict(enabled=False)
    if defect == 'no-start-cover': rows = [r for r in rows if r['device_us'] > 1_600_000]
    if defect == 'no-end-cover': rows = [r for r in rows if r['device_us'] < 3_900_000]
    if defect == 'seq-skip': rows = trace_rows(250, skip_seq_at=120)
    if defect == 'two-streams': rows = rows+trace_rows(5, ssrc=9)
    sample = sample_for(rows, **kw)
    if defect == 'other-session': sample['reply_window_transport']['after']['generation'] = 2
    if defect == 'no-endpoints': sample.pop('prompt_start')
    r = sc.source_window(sample, rows)
    assert r['complete'] is False and r['passed'] is None and r['harness_failure']


# ---- S2 ------------------------------------------------------------------------------------------------------------

ITEM1_SAMPLE = dict(sent=773, encode_frames=774, encoded_queue=1, enqueue_send_max_us=21862, send_errors=0, send_budget_cutoffs=0,
                    stale_audio_skipped=0, pcm_queue_drops=0, drop_reasons=dict(encoded_queue=0, uplink_gap=0, send_cutoff=0))


@pytest.mark.parametrize('change,ok', [({}, True), (dict(send_errors=1), False), (dict(enqueue_send_max_us=40001), False),
                                       (dict(sent=770), False), (dict(sent=775), False), (dict(drop_reasons=dict(uplink_gap=1)), False),
                                       (dict(encoded_queue=0, sent=772), True), (dict(encode_frames=None), False)])
def test_s2_counters(change, ok):
    r = sc.s2_counters(dict(ITEM1_SAMPLE, **change))
    assert r['passed'] is ok and 'cannot see a stall before encoding' in r['limit']


def cmp_row(stage='quiet_offline', **deltas):
    base = dict(sent=250, encode_frames=250, send_errors=0, send_budget_cutoffs=0, stale_audio_skipped=0, pcm_queue_drops=0,
                lost=0, concealed=0, plc_frames=0, playout_dropped=0)
    return dict(stage=stage, same_session=True, deltas=dict(base, **deltas), enqueue_send_max_us=22000, internal_minimum_free=90000)


def test_trace_configuration_comparison_rules():
    """Astra r15 Q2 / Fable N1: per counter, per-window deltas, matched stages, explicit insufficient evidence."""
    off = [cmp_row(pcm_queue_drops=1), cmp_row()]                     # the baseline already has a PCM drop window
    assert sc.trace_configuration_comparison([cmp_row(), cmp_row()], off)['result'] == 'not detected in these samples'
    r = sc.trace_configuration_comparison([cmp_row(send_errors=1), cmp_row()], off)   # newly nonzero counter: detected
    assert r['changes_detected'] is True and r['per_stage']['quiet_offline']['counters']['send_errors']['detected']
    lossy_off = [cmp_row(lost=3)]
    assert sc.trace_configuration_comparison([cmp_row(lost=1), cmp_row(lost=2)], lossy_off)['changes_detected'] is False
    assert sc.trace_configuration_comparison([cmp_row(lost=5), cmp_row(lost=4)], lossy_off)['changes_detected'] is True   # more loss
    assert sc.trace_configuration_comparison([dict(cmp_row(), enqueue_send_max_us=35000)], off)['changes_detected'] is True
    assert sc.trace_configuration_comparison([dict(cmp_row(), internal_minimum_free=80000)], off)['changes_detected'] is True
    for on, base in (([], off), ([cmp_row()], []), ([dict(cmp_row(), internal_minimum_free=None)], off),
                     ([cmp_row(stage='strict_voice_cat')], off), ([dict(cmp_row(), same_session=False)], off)):
        assert sc.trace_configuration_comparison(on, base)['result'] == 'insufficient_evidence'


def test_trace_off_baseline_is_declared_and_loadable():
    root = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-10/requal-n1')
    plan = load_plan(D139)
    assert plan['decision_138']['trace_comparison']['baseline_trace_off'] == [str(root/x) for x in sc.TRACE_OFF_BASELINE]
    if not (root/sc.TRACE_OFF_BASELINE[0]).exists():pytest.skip('evidence tree not present')
    rows = sc.rows_from_records([root/x for x in sc.TRACE_OFF_BASELINE])
    assert {r['stage'] for r in rows} == {'strict_voice_cat', 'quiet_offline'} and len(rows) >= 20


# ---- e2e_common source-trace readout points --------------------------------------------------------------------------

def test_source_trace_mode_reads_out_only_outside_the_scored_window(monkeypatch, tmp_path):
    drains = []
    import arrival_trace
    monkeypatch.setattr(arrival_trace, 'drain', lambda board, path: drains.append(path.name))
    monkeypatch.setattr(e2e_common, 'perf', lambda board: dict(peer_profile=dict(trace=dict(overwritten=0))))
    board = SimpleNamespace(e2e_display_trace=True, e2e_source_trace=True)
    e2e_common.display_trace(board, tmp_path)                                   # pre-prompt: drained
    e2e_common.display_trace(board, tmp_path, acquisition=True)                 # inside the window: never
    e2e_common.display_trace(board, tmp_path, acquisition=True, boundary=True)  # after the reply-end PERF: drained
    assert drains == ['arrivals.jsonl', 'arrivals.jsonl'] and board.e2e_export_trace['overwritten'] == 0
    plain = SimpleNamespace(e2e_display_trace=True)                             # non-source modes unchanged
    e2e_common.display_trace(plain, tmp_path, acquisition=True)
    assert len(drains) == 3


# ---- observation gate: link_tail report-only, S1 gates (d138 plan only) -----------------------------------------------

def gate_result(link_passed, cadence):
    runs = [dict(run=i, passed=True, scored=True, pc_id='p', source_cadence=cadence) for i in (1, 2, 3, 4, 5)]
    return dict(runs=runs, reruns=[], link_tail=dict(passed=link_passed), display_monitor=dict(passed=True))


@pytest.mark.parametrize('plan_path', [T.N1_PLAN, D138])
def test_uplink_gate_follows_the_plan(plan_path, monkeypatch):
    import echoear_acoustic_qualification as aq
    monkeypatch.setattr(aq, 'gate', lambda result, peers: dict(passed=result['link_tail']['passed'], checks=dict(
        five_originals=True, uplink=result['link_tail']['passed'] is True)))
    plan = load_plan(plan_path)
    ok_cadence = dict(complete=True, passed=True)
    g = observation_gate(plan, 'quiet_offline', gate_result(False, ok_cadence), '')
    if plan_path == D138:
        assert g['source']['passed'] is True and g['uplink']['link_tail_report_only'] is False
        bad = observation_gate(plan, 'quiet_offline', gate_result(True, dict(complete=True, passed=False)), '')
        assert bad['source']['passed'] is False
        incomplete = observation_gate(plan, 'quiet_offline', gate_result(True, dict(complete=False, passed=None)), '')
        assert incomplete['source']['passed'] is False and incomplete['uplink']['harness_failure'] is True
    else:
        assert g['source']['passed'] is False and g['uplink'] is None


# ---- 138b: resume after a clean deferral -------------------------------------------------------------------------------

HOLD_OK = dict(image_profile=dict(passed=True), measurement_volume=dict(passed=True), stage_nic=dict(passed=True))


def deferral_segment(rows, slot):
    return dict(seg(rows), deferred=True, next_original_slot=slot, exit_code=2, error=None, deferral_resources=dict(passed=True),
                restore=dict(clean=True, attempts=[dict(label='restore-1', error=None)]), passed=False, **HOLD_OK)


@pytest.mark.parametrize('stage,runs', [('strict_voice_cat', 3), ('quiet_offline', 5), ('quiet_live', 5)])
def test_aggregate_resumes_after_a_clean_deferral(stage, runs):
    first = deferral_segment([run_row(1), run_row(2)], 3)
    cont = seg([run_row(3, pc='p2', warm='cold')]+[run_row(i, pc='p2') for i in range(4, runs+1)])
    a = item2.aggregate_quiet_live([first, cont], runs, stage=stage, resume=True)
    assert a['passed'] and a['complete'] and a['slots'] == list(range(1, runs+1))
    planned = item2.aggregate_quiet_live([first], runs, stage=stage, resume=True)
    assert planned['next_original_slot'] == 3 and not planned['problems']
    frozen = item2.aggregate_quiet_live([first, cont], runs, stage=stage, resume=False)    # r1 plan: still terminal
    assert not frozen['passed'] and any('eligibility deferral' in p for p in frozen['problems'])
    for unclean in (dict(error='RuntimeError: boom'), dict(exit_code=1), dict(restore=dict(clean=True, attempts=[{}, {}])),
                    dict(next_original_slot=4), dict(interrupted=True)):
        bad = item2.aggregate_quiet_live([dict(first, **unclean), cont], runs, stage=stage, resume=True)
        assert not bad['passed'] and bad['problems']


def test_setup_failure_continuation_stays_quiet_live_only():
    first = seg([run_row(1), run_row(2, scored=False)])
    cont = seg([run_row(2, pc='p2', warm='cold')]+[run_row(i, pc='p2') for i in (3, 4, 5)])
    assert item2.aggregate_quiet_live([first, cont], 5, stage='quiet_live', resume=True)['passed']
    assert not item2.aggregate_quiet_live([first, cont], 5, stage='quiet_offline', resume=True)['passed']


def test_resume_execute_strict_from_a_real_deferral_and_collect(tmp_path, monkeypatch, fake_restore):
    """Real execute(): strict deferral after originals 1-2 (exit 2) under the d138 plan, continued at original 3 by a
    fresh session; the collector counts attempt-1 as `continued`. Under the frozen plan the continuation is refused."""
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    first = dict(seg([run_row(1), run_row(2)]), deferred=True, next_original_slot=3, scored_runs=2, passed=False)
    cont = seg([run_row(3, pc='p2', warm='cold')])
    results = {'attempt-1': first, 'attempt-2': cont}
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'acc', binding_overrides=dict(normal_elf_sha256=item2.NORMAL))
    env = fake_item2_environment(tmp_path, monkeypatch, stage='strict_voice_cat', run_result=lambda a: results[a.out.name], plan_path=D138)
    real = item2.verified_binding
    monkeypatch.setattr(item2, 'verified_binding', lambda p: (lambda b, i: (dict(b, output_root=str(root)), i))(*real(p)))
    monkeypatch.setattr(item2, 'validate_deferral', lambda out, result: result.update(deferral_resources=dict(passed=True)) or True)
    monkeypatch.setattr(item2, 'segment_observation', lambda plan, seg, text, current=False, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    import shutil
    shutil.rmtree(root/'strict_voice_cat')
    a1 = root/'strict_voice_cat'/'attempt-1'
    assert item2.execute(binding, 'strict_voice_cat', a1, restore_wait=lambda: None) == 2
    (root/'strict_voice_cat'/'attempt-1.typescript').write_text('EXITED 2\n')
    r1 = json.loads((a1/'result.json').read_text())
    assert item2.resumable_deferral('strict_voice_cat', r1)
    import time
    real_mono = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: real_mono()+120)
    a2 = a1.with_name('attempt-2')
    code = item2.execute(binding, 'strict_voice_cat', a2, continue_from=[a1], restore_wait=lambda: None)
    r2 = json.loads((a2/'result.json').read_text())
    assert code == 0, {k: r2.get(k) for k in ('stage_aggregate', 'error', 'passed', 'item2_observations')}
    (root/'strict_voice_cat'/'attempt-2.typescript').write_text('EXITED 0\n')
    r2 = json.loads((a2/'result.json').read_text())
    assert r2['stage_aggregate']['passed'] and r2['stage_aggregate']['slots'] == [1, 2, 3] and r2['passed'] is True
    q = collect(load_plan(D138), binding, digest)
    assert q['slots']['strict_voice_cat']['problems'] == [] and q['slots']['strict_voice_cat']['history'] == [dict(attempt=1, state='continued')]
    q_old = collect(load_plan(T.N1_PLAN), binding, digest)                       # the frozen plan does not count it
    assert q_old['slots']['strict_voice_cat']['history'] == [dict(attempt=1, state='unaccounted')]


def test_frozen_plan_still_refuses_a_non_quiet_live_continuation(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    prior = binding.parent/'quiet_offline'/'attempt-1'; prior.mkdir(parents=True)
    with pytest.raises(RuntimeError, match='Only quiet_live'):
        item2.execute(binding, 'quiet_offline', prior.with_name('attempt-2'), continue_from=[prior])
    assert 'lock-enter' not in trace


def test_aggregate_a6_resumes_at_the_next_level():
    pts = lambda *ts: [dict(target_dbfs=t, acquisition_passed=True) for t in ts]
    d = dict(deferred=True, points=pts(-39), next_original_slot=2, exit_code=2, error=item2.A6_DEFERRAL_ERROR, deferral_resources=dict(passed=True),
             restore=dict(clean=True, attempts=[{}]), item2_resources=dict(passed=True), **HOLD_OK)
    cont = dict(points=pts(-39, -27), segment_points=pts(-27))
    assert item2.aggregate_a6('a6_hot_stt', [d], [-39, -27])['next_original_slot'] == 2
    a = item2.aggregate_a6('a6_hot_stt', [d, cont], [-39, -27], current=True)
    assert a['complete'] and not a['problems']
    assert item2.aggregate_a6('a6_hot_stt', [dict(d, exit_code=1), cont], [-39, -27], current=True)['problems']
    assert item2.aggregate_a6('a6_hot_stt', [d, dict(points=pts(-39), segment_points=pts(-39))], [-39, -27], current=True)['problems']   # repeat
    # r16 (Astra r15 B1): a failed prior hold gate is never washed out by a later clean deferral.
    for gate in ('image_profile', 'stage_nic', 'item2_resources'):
        bad = item2.aggregate_a6('a6_hot_stt', [dict(d, **{gate: dict(passed=False)}), cont], [-39, -27], current=True)
        assert any('hold.'+gate in p for p in bad['problems'])
    assert item2.aggregate_a6('a6_hot_stt', [dict(d, cleanup_errors=['x']), cont], [-39, -27], current=True)['problems']


# ---- SIGINT: final, collectible interrupted FAIL ------------------------------------------------------------------------

def test_sigint_in_the_hold_writes_a_final_collectible_fail(tmp_path, monkeypatch, fake_restore):
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'acc', binding_overrides=dict(normal_elf_sha256=item2.NORMAL))
    fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline', body_error=KeyboardInterrupt())
    real = item2.verified_binding
    monkeypatch.setattr(item2, 'verified_binding', lambda p: (lambda b, i: (dict(b, output_root=str(root)), i))(*real(p)))
    import shutil
    shutil.rmtree(root/'quiet_offline'); out = root/'quiet_offline'/'attempt-1'
    with pytest.raises(KeyboardInterrupt):
        item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None)
    r = json.loads((out/'result.json').read_text())
    assert r['interrupted'] is True and r['exit_code'] == 1 and r['kind'] == 'item2-stage' and r['passed'] is False
    (root/'quiet_offline'/'attempt-1.typescript').write_text('INTERRUPTED\nEXITED 1\n')
    q = collect(plan, binding, digest)
    assert q['slots']['quiet_offline']['problems'] == []                        # collectible
    assert q['passed'] is False and q['verdict']['stages']['quiet_offline']['passed'] is False   # an interrupted FAIL


def test_sigint_main_exits_1(monkeypatch):
    import runpy, sys
    monkeypatch.setattr(item2, 'execute', lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    src = Path(item2.__file__).read_text()
    assert "except KeyboardInterrupt:" in src and "code=1" in src.split("if __name__=='__main__':")[1]


# ---- r16: Astra r15 B1 (A6 prior hold gates) and B2 (continuation identity through interrupts) -----------------------

ARMS = dict(a6_echo_stress=['tts_1.wav', 'tts_2.wav'], a6_hot_stt=[-39, -27])


def a6_environment(tmp_path, monkeypatch, stage, script):
    """Real execute()/aggregate/finalisation/collector; faked: lock/flash/restore, sensors, the arm loop body."""
    import echoear_hot_stt, echoear_echo_stress
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'acc', binding_overrides=dict(normal_elf_sha256=item2.NORMAL))
    env = fake_item2_environment(tmp_path, monkeypatch, stage=stage, plan_path=D139)
    real = item2.verified_binding
    monkeypatch.setattr(item2, 'verified_binding', lambda p: (lambda b, i: (dict(b, output_root=str(root)), i))(*real(p)))
    monkeypatch.setattr(item2, 'a6_arm_ids', lambda st, b: list(ARMS[st]))
    monkeypatch.setattr(item2, 'a6_resources', lambda out, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    monkeypatch.setattr(item2, 'validate_deferral', lambda out, result: result.update(deferral_resources=dict(passed=True)) or True)
    def preflight(plan_, st, journal):
        def before(board, device, point):
            ok = not (point.parent.name == 'attempt-1' and script.get('defer_at') == point.name)
            before.observations.append(dict(passed=ok))
            if not ok: raise RuntimeError('Item2 eligibility deferred before scored acoustic arm')
        before.observations = []
        return before
    monkeypatch.setattr(item2, 'a6_preflight', preflight)
    def body(out, pre, subset):
        out.mkdir(parents=True); (out/'serial.log').write_text('')
        script['attempt'] = out.name
        ids = ARMS[stage] if subset is None else list(subset); result = dict(passed=False, points=[])
        try:
            for arm in ids:
                point = out/str(arm); point.mkdir()
                if script.get('interrupt') == out.name: raise KeyboardInterrupt
                pre(None, None, point)
                key = 'target_dbfs' if stage == 'a6_hot_stt' else 'fixture'
                result['points'].append({key: arm, 'passed': script.get('fail_point') != (out.name, arm), 'acquisition_passed': True,
                                         'before': dict(output_volume_db=-6), 'after': dict(output_volume_db=-6)})
            result['passed'] = all(p['passed'] for p in result['points'])
        except RuntimeError as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
        (out/'result.json').write_text(json.dumps(result))
        return result
    if stage == 'a6_hot_stt':
        def acquire(plan_, out, preflight, targets=None):
            r = body(out, preflight, targets); r['acquisition_complete'] = r['passed'] and len(r['points']) == len(targets or ARMS[stage]); r['passed'] = None
            (out/'result.json').write_text(json.dumps(r)); return r
        monkeypatch.setattr(echoear_hot_stt, 'acquire', acquire)
        monkeypatch.setattr(echoear_hot_stt, 'score', lambda out: dict(json.loads((out/'result.json').read_text()), passed=True))
    else:
        monkeypatch.setattr(echoear_echo_stress, 'run', lambda out, bank, budget, plan, calibrate_prompt, preflight, arms=None: body(out, preflight, arms))
    import shutil
    shutil.rmtree(root/stage)
    return plan, root, binding, digest


def run_and_type(binding, stage, out, **kw):
    try:code = item2.execute(binding, stage, out, restore_wait=lambda: None, **kw)
    except KeyboardInterrupt:code = 1
    (out.parent/(out.name+'.typescript')).write_text('EXITED %d\n' % code)
    return code


@pytest.mark.parametrize('stage', ['a6_echo_stress', 'a6_hot_stt'])
def test_astra_r15_b1_failed_prior_profile_is_not_washed_out(tmp_path, monkeypatch, fake_restore, stage):
    """Astra r15 B1 composed regression: attempt-1 acquires the first arm/level, its REAL session_gate_text() reports
    missing profile evidence (passed=false), then defers before the second. A healthy continuation must not turn it
    into a counted PASS: the continuation is refused before the lock and the slot stays a FAIL; a continuation record
    produced anyway (older harness) is not collected as continued."""
    import time
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    script = dict(attempt=None, defer_at=str(ARMS[stage][1]))
    plan, root, binding, digest = a6_environment(tmp_path, monkeypatch, stage, script)
    a1 = root/stage/'attempt-1'
    real_gate = profile.session_gate_text
    assert run_and_type(binding, stage, a1) == 2
    r1 = json.loads((a1/'result.json').read_text())
    assert r1['deferred'] is True and r1['image_profile']['passed'] is False and len(r1['points']) == 1
    assert item2.prior_hold_problems(stage, r1) == ['hold.image_profile'] or 'hold.image_profile' in item2.prior_hold_problems(stage, r1)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))    # healthy current hold
    mono = time.monotonic; monkeypatch.setattr(time, 'monotonic', lambda: mono()+120); del trace[:]
    with pytest.raises(RuntimeError, match='No continuable'):
        item2.execute(binding, stage, a1.with_name('attempt-2'), continue_from=[a1], restore_wait=lambda: None)
    assert 'lock-enter' not in trace
    q = collect(load_plan(D139), binding, digest)
    assert q['verdict']['stages'][stage]['passed'] is False
    # A continuation record made anyway is not collected as `continued`.
    sha = hashlib.sha256((a1/'result.json').read_bytes()).hexdigest()
    write_attempt(root, stage, 2, dict(final(stage, passed=True), normal_elf_sha256=item2.NORMAL, release_binding=str(binding.resolve()),
                  binding_sha256=digest, counted=True, purpose='acceptance', stage_aggregate=dict(passed=True),
                  continued_from=[dict(path=str(a1.resolve()), result_sha256=sha)]))
    q = collect(load_plan(D139), binding, digest)
    assert q['passed'] is False and q['slots'][stage]['history'] == [dict(attempt=1, state='unaccounted')]


@pytest.mark.parametrize('stage', ['a6_echo_stress', 'a6_hot_stt'])
def test_a6_healthy_continuation_and_point_failure_controls(tmp_path, monkeypatch, fake_restore, stage):
    import time
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    for case in ('healthy', 'point-fail'):
        work = tmp_path/case; work.mkdir()
        script = dict(attempt=None, defer_at=str(ARMS[stage][1]), fail_point=('attempt-1', ARMS[stage][0]) if case == 'point-fail' else None)
        if stage == 'a6_hot_stt' and case == 'point-fail': continue      # hot_stt arm verdicts come from scoring
        plan, root, binding, digest = a6_environment(work, monkeypatch, stage, script)
        monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
        a1 = root/stage/'attempt-1'
        assert run_and_type(binding, stage, a1) == 2
        mono = time.monotonic; monkeypatch.setattr(time, 'monotonic', lambda: mono()+120)
        a2 = a1.with_name('attempt-2')
        code = run_and_type(binding, stage, a2, continue_from=[a1])
        r2 = json.loads((a2/'result.json').read_text())
        assert [item2.a6_point_id(stage, p) for p in r2['points']] == ARMS[stage]
        q = collect(load_plan(D139), binding, digest)
        assert q['slots'][stage]['history'] == [dict(attempt=1, state='continued')] and q['slots'][stage]['problems'] == []
        # (other slots of this synthetic population carry a different image hash; judge this slot's verdict)
        if case == 'healthy':assert code == 0 and q['verdict']['stages'][stage]['passed'] is True
        else:assert code == 1 and q['verdict']['stages'][stage]['passed'] is False and r2['continuation_outlook']['cannot_pass'] is True
        monkeypatch.undo() if False else None


def e2e_rows(start, n, pc):
    return [run_row(start+i, pc=pc, warm='cold' if i == 0 else None) for i in range(n)]


@pytest.mark.parametrize('stage,shape', [('strict_voice_cat', 'deferral'), ('quiet_offline', 'deferral'), ('quiet_live', 'deferral'),
                                         ('quiet_live', 'd6-1+4'), ('quiet_live', 'd6-0+5')])
def test_astra_r15_b2_interrupted_e2e_continuation_collects_as_interrupted_fail(tmp_path, monkeypatch, fake_restore, stage, shape):
    import time
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    runs = 3 if stage == 'strict_voice_cat' else 5
    if shape == 'deferral':
        first = dict(seg(e2e_rows(1, 2, 'p1')), deferred=True, next_original_slot=3, scored_runs=2, passed=False)
    elif shape == 'd6-1+4':
        first = seg([run_row(1), run_row(2, scored=False)])
    else:
        first = seg([run_row(1, scored=False)])
    def result_for(a):
        if a.out.name == 'attempt-2':raise KeyboardInterrupt
        return first
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'acc', binding_overrides=dict(normal_elf_sha256=item2.NORMAL))
    fake_item2_environment(tmp_path, monkeypatch, stage=stage, run_result=result_for, plan_path=D139)
    real = item2.verified_binding
    monkeypatch.setattr(item2, 'verified_binding', lambda p: (lambda b, i: (dict(b, output_root=str(root)), i))(*real(p)))
    monkeypatch.setattr(item2, 'validate_deferral', lambda out, result: result.update(deferral_resources=dict(passed=True)) or True)
    monkeypatch.setattr(item2, 'segment_observation', lambda plan, seg, text, current=False, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    import shutil
    shutil.rmtree(root/stage)
    a1 = root/stage/'attempt-1'
    run_and_type(binding, stage, a1)
    mono = time.monotonic; monkeypatch.setattr(time, 'monotonic', lambda: mono()+120)
    a2 = a1.with_name('attempt-2')
    assert run_and_type(binding, stage, a2, continue_from=[a1]) == 1
    r2 = json.loads((a2/'result.json').read_text())
    assert r2['interrupted'] is True and r2['exit_code'] == 1 and r2['continued_from'][0]['path'] == str(a1)
    key = 'quiet_live_aggregate' if stage == 'quiet_live' else 'stage_aggregate'
    assert r2[key]['passed'] is False
    q = collect(load_plan(D139), binding, digest)
    assert q['slots'][stage]['problems'] == [] and q['slots'][stage]['history'] == [dict(attempt=1, state='continued')]
    assert q['passed'] is False and q['verdict']['stages'][stage]['passed'] is False       # an interrupted FAIL, collected


@pytest.mark.parametrize('stage', ['a6_echo_stress', 'a6_hot_stt'])
def test_astra_r15_b2_interrupted_a6_continuation_collects(tmp_path, monkeypatch, fake_restore, stage):
    import time
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    script = dict(attempt=None, defer_at=str(ARMS[stage][1]), interrupt='attempt-2')
    plan, root, binding, digest = a6_environment(tmp_path, monkeypatch, stage, script)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    a1 = root/stage/'attempt-1'
    assert run_and_type(binding, stage, a1) == 2
    mono = time.monotonic; monkeypatch.setattr(time, 'monotonic', lambda: mono()+120)
    a2 = a1.with_name('attempt-2')
    assert run_and_type(binding, stage, a2, continue_from=[a1]) == 1
    r2 = json.loads((a2/'result.json').read_text())
    assert r2['interrupted'] is True and r2['continued_from'][0]['path'] == str(a1) and r2['stage_aggregate']['passed'] is False
    q = collect(load_plan(D139), binding, digest)
    assert q['slots'][stage]['problems'] == [] and q['slots'][stage]['history'] == [dict(attempt=1, state='continued')]
    assert q['passed'] is False


# ---- r16: decision 139 plan fields -------------------------------------------------------------------------------------

def test_d139_plan_is_derived_from_the_bound_d138_revision():
    new, old = load_plan(D139), json.loads(D138.read_text())
    assert new['derived_from']['sha256'] == hashlib.sha256(D138.read_bytes()).hexdigest()
    assert hashlib.sha256(D138.read_bytes()).hexdigest() == '95a95f4dbea6ad150554a312fceaad112f55d70c36ddc23776c40bee0a6ea278'   # unedited
    assert {k for k in set(new) | set(old) if new.get(k) != old.get(k)} == {'status', 'name', 'derived_from', 'previous_derived_from', 'decision_138', 'decision_139'}
    d = new['decision_138']
    assert d['source_cadence']['excuse_firmware_backpressure'] is False and d['item1_s2']['pcm_queue_drops_max'] == 1
    assert item2.source_cadence_config(new) == dict(excuse_backpressure=False)
    assert 'three drains' in d['source_cadence']['readout']


@pytest.mark.parametrize('drops,ok', [(0, True), (1, True), (2, False)])
def test_item1_s2_pcm_queue_drops_tolerance_is_one_per_session(drops, ok):
    """Decision 139c, declared before data: pcm_queue_drops <= 1 per item1 session; every other counter 0."""
    plan = load_plan(D139)
    limit = plan['decision_138']['item1_s2']['pcm_queue_drops_max']
    assert sc.s2_counters(dict(ITEM1_SAMPLE, pcm_queue_drops=drops), pcm_queue_drops_max=limit)['passed'] is ok
    assert sc.s2_counters(dict(ITEM1_SAMPLE, pcm_queue_drops=1, send_errors=1), pcm_queue_drops_max=limit)['passed'] is False
    assert sc.s2_counters(dict(ITEM1_SAMPLE, pcm_queue_drops=1))['passed'] is False      # item2 report-only default 0


def test_excuse_backpressure_field_is_honoured_both_ways(monkeypatch):
    seen = []
    import step8e2b_analysis
    real = step8e2b_analysis.device_send_cadence
    monkeypatch.setattr(step8e2b_analysis, 'device_send_cadence', lambda *a, **k: (seen.append(k.get('snapshots')), real(*a, **k))[1])
    rows = trace_rows(250)
    snap = [dict(device_us=0, generation=1)]
    sc.source_window(sample_for(rows), rows, snapshots=snap)
    sc.source_window(sample_for(rows), rows, excuse_backpressure=True, snapshots=snap)
    assert seen == [(), snap]


def test_drains_and_trace_cost_are_recorded(monkeypatch, tmp_path):
    import arrival_trace
    monkeypatch.setattr(arrival_trace, 'drain', lambda board, path: [1, 2, 3])
    monkeypatch.setattr(e2e_common, 'perf', lambda board: dict(peer_profile=dict(trace=dict(overwritten=0, pending=7))))
    board = SimpleNamespace(e2e_display_trace=True, e2e_source_trace=True)
    for phase, kw in (('pre-prompt', {}), ('post-reply', dict(acquisition=True, boundary=True)), ('post-turn', {})):
        e2e_common.display_trace(board, tmp_path, phase=phase, **kw)
    drains = board.e2e_trace_drains[str(tmp_path)]
    assert [d['phase'] for d in drains] == ['pre-prompt', 'post-reply', 'post-turn'] and all(d['rows'] == 3 for d in drains)
    rows = trace_rows(250)
    sample = dict(sample_for(rows), source_trace_drains=drains)
    cost = sc.source_window(sample, rows)['trace_cost']
    assert cost['drain_count'] == 3 and cost['rows_per_s'] and cost['max_pending'] == 7 and cost['overwritten_delta'] == 0


# ---- r17: smoke-r17 S1 overwrite diagnosis; Fable r16 N1/N3/N4 ------------------------------------------------------

def test_pre_prompt_readout_loops_until_the_ring_is_nearly_empty(monkeypatch, tmp_path):
    import arrival_trace
    pend = iter([8192-660, 40, 0, 0, 0])
    monkeypatch.setattr(arrival_trace, 'drain', lambda board, path: [0]*16)
    monkeypatch.setattr(e2e_common, 'perf', lambda board: dict(device_us=1, peer_profile=dict(trace=dict(overwritten=60, pending=next(pend)))))
    board = SimpleNamespace(e2e_display_trace=True, e2e_source_trace=True)
    e2e_common.display_trace(board, tmp_path, phase='pre-prompt')
    drains = board.e2e_trace_drains[str(tmp_path)]
    assert [d['iteration'] for d in drains] == [0, 1] and drains[-1]['export']['pending'] == 40 and drains[-1]['export']['device_us'] == 1
    e2e_common.display_trace(board, tmp_path, acquisition=True, boundary=True, phase='post-reply')     # single drain elsewhere
    assert len(board.e2e_trace_drains[str(tmp_path)]) == 3


def r17_sample(rows, *, before_ow, drain_ow, reply_ow, export_ow, drain_device_us=1_000_000):
    s = sample_for(rows, overwritten=(before_ow, reply_ow, export_ow))
    s['source_trace_drains'] = [dict(phase='pre-prompt', iteration=0, export=dict(overwritten=drain_ow-1, pending=661)),
                                dict(phase='pre-prompt', iteration=1, host_end_s=100.2, export=dict(overwritten=drain_ow, pending=40, device_us=drain_device_us))]
    return s


def test_overwrite_baseline_is_the_last_pre_prompt_drain():
    """smoke-r17 shape: the ring overflowed between originals (5665 at the pre-prompt PERF) and 60 more old records
    were overwritten during the full pre-prompt dump (5725); nothing after the last drain: complete. An overwrite after
    the last drain, or a last drain that ends after the window start, fails closed."""
    rows = trace_rows(250)
    assert sc.source_window(r17_sample(rows, before_ow=5665, drain_ow=5725, reply_ow=5725, export_ow=5725), rows)['complete'] is True
    bad = sc.source_window(r17_sample(rows, before_ow=5665, drain_ow=5725, reply_ow=5726, export_ow=5726), rows)
    assert bad['complete'] is False and 'since the last pre-prompt drain' in bad['harness_failure'][0]
    late = sc.source_window(r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0, drain_device_us=2_000_000), rows)
    assert late['complete'] is False and any('after the window start' in p for p in late['harness_failure'])
    # a missing kind-12 record inside the span is still caught by sequence contiguity even with a clean counter
    gap = trace_rows(250, skip_seq_at=60)
    assert sc.source_window(r17_sample(gap, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0), gap)['complete'] is False


def test_fable_r16_n1_hot_stt_scoring_interrupt_keeps_the_stage_aggregate(tmp_path, monkeypatch, fake_restore):
    import time
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    stage = 'a6_hot_stt'
    script = dict(attempt=None, defer_at=str(ARMS[stage][1]))
    plan, root, binding, digest = a6_environment(tmp_path, monkeypatch, stage, script)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    a1 = root/stage/'attempt-1'
    assert run_and_type(binding, stage, a1) == 2
    mono = time.monotonic; monkeypatch.setattr(time, 'monotonic', lambda: mono()+120)
    def interrupted(out, result): raise KeyboardInterrupt   # before the pre-scoring write: disk holds the acquisition record
    monkeypatch.setattr(item2, 'score_hot_once', interrupted)
    a2 = a1.with_name('attempt-2')
    assert run_and_type(binding, stage, a2, continue_from=[a1]) == 1
    r2 = json.loads((a2/'result.json').read_text())
    assert r2['scoring_failed'] and r2['interrupted'] and r2['stage_aggregate']['passed'] is False and r2['continued_from']
    q = collect(load_plan(D139), binding, digest)
    assert q['slots'][stage]['problems'] == [] and q['slots'][stage]['history'] == [dict(attempt=1, state='continued')]


def test_fable_r16_n4_scored_deferral_records_resumability(tmp_path, monkeypatch, fake_restore, capsys):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    stage = 'a6_echo_stress'
    script = dict(attempt=None, defer_at=str(ARMS[stage][1]))
    plan, root, binding, digest = a6_environment(tmp_path, monkeypatch, stage, script)
    a1 = root/stage/'attempt-1'                       # real session_gate_text fails on the empty serial: not resumable
    assert run_and_type(binding, stage, a1) == 2
    r1 = json.loads((a1/'result.json').read_text())
    assert r1['resumable']['continuable'] is False and 'hold.image_profile' in r1['resumable']['prior_hold_problems']
    assert 'NOT resumable' in capsys.readouterr().out


def test_fable_r16_n3_outlook_failure_never_refuses(tmp_path, monkeypatch, fake_restore, capsys):
    import time
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    stage = 'a6_echo_stress'
    script = dict(attempt=None, defer_at=str(ARMS[stage][1]))
    plan, root, binding, digest = a6_environment(tmp_path, monkeypatch, stage, script)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    a1 = root/stage/'attempt-1'
    assert run_and_type(binding, stage, a1) == 2
    mono = time.monotonic; monkeypatch.setattr(time, 'monotonic', lambda: mono()+120)
    monkeypatch.setattr(item2, 'continuation_outlook', lambda *a: (_ for _ in ()).throw(KeyError('x')))
    a2 = a1.with_name('attempt-2')
    assert run_and_type(binding, stage, a2, continue_from=[a1]) == 0
    assert json.loads((a2/'result.json').read_text())['continuation_outlook']['error'].startswith('KeyError')
    assert 'outlook unavailable' in capsys.readouterr().out


def test_comparison_attributes_downlink_detections_and_the_cli(tmp_path):
    off = [cmp_row(), cmp_row()]
    r = sc.trace_configuration_comparison([cmp_row(lost=1)], off)
    assert r['changes_detected'] is True and r['device_side_detected'] is False and r['raises_trace_question'] is False
    assert r['downlink_detected'].startswith('detected, not attributable')
    r = sc.trace_configuration_comparison([cmp_row(send_errors=1)], off)
    assert r['raises_trace_question'] is True and r['downlink_detected'] is None


def test_d139_r2_plan_changes_only_the_s1_readout_and_completeness_text():
    r2 = load_plan(T.N1_PLAN.with_name('echoear-item2-requalification-n1-d139-r2.json')); old = json.loads(D139.read_text())
    assert r2['derived_from']['sha256'] == hashlib.sha256(D139.read_bytes()).hexdigest()
    assert {k for k in set(r2) | set(old) if r2.get(k) != old.get(k)} == {'status', 'name', 'derived_from', 'previous_derived_from', 'decision_138'}
    a, b = r2['decision_138'], old['decision_138']
    assert {k for k in a if a[k] != b.get(k)} == {'source_cadence'}
    assert {k for k in a['source_cadence'] if a['source_cadence'][k] != b['source_cadence'].get(k)} == {'completeness', 'readout', 'completeness_r2_reason'}


# ---- r18: hardening (Astra r17 / Fable r17 N2-N4) ---------------------------------------------------------------------

def test_r18_drain_end_must_be_proven_before_the_window():
    rows = trace_rows(250)
    ok = r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0)
    ok['source_trace_drains'][-1]['host_end_s'] = 100.2                     # prompt_start 100.4
    assert sc.source_window(ok, rows)['complete'] is True
    for change in (dict(device_us=None), dict(device_us='1'), dict(host_end=None), dict(host_end=100.5)):
        s = r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0)
        s['source_trace_drains'][-1]['host_end_s'] = change.get('host_end', 100.2)
        if 'device_us' in change: s['source_trace_drains'][-1]['export']['device_us'] = change['device_us']
        r = sc.source_window(s, rows)
        assert r['complete'] is False and r['passed'] is None


def test_r18_unconverged_readout_is_flagged_not_failed():
    rows = trace_rows(250)
    s = r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0)
    s['source_trace_drains'][-1].update(host_end_s=100.2); s['source_trace_drains'][-1]['export']['pending'] = 700
    r = sc.source_window(s, rows)
    assert r['complete'] is True and r['passed'] is True and r['pre_prompt_converged'] is False


def test_r18_device_send_failure_is_a_cadence_fail_not_a_harness_failure():
    rows = trace_rows(250, skip_seq_at=120)
    s = r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0)
    s['source_trace_drains'][-1].update(host_end_s=100.2)
    s['before_perf']['send_errors'] = 0; s['reply_window_transport']['after']['send_errors'] = 1
    r = sc.source_window(s, rows)
    assert r['complete'] is True and r['passed'] is False and r['device_send_failure']['missing_sequence'] == 1 and r['device_send_failure']['send_errors_delta'] == 1
    s['reply_window_transport']['after']['send_errors'] = 0                   # no device error: the trace lost it
    assert sc.source_window(s, rows)['complete'] is False


# ---- r19: Astra r18 BLOCKING (a duplicate cancelled a hole) ------------------------------------------------------------

def _shape(rows, *, drop=None, dup=None, swap=None):
    rows = [dict(r) for r in rows]
    if dup is not None:rows.append(dict(rows[dup], device_us=rows[dup]['device_us']+1))   # same seq, different time
    if drop is not None:rows.pop(drop)
    if swap is not None:rows[swap]['seq'], rows[swap+1]['seq'] = rows[swap+1]['seq'], rows[swap]['seq']
    return rows


@pytest.mark.parametrize('case', ['drop-and-dup', 'dup-only', 'reorder-only', 'drop-only', 'drop-and-dup-with-send-error'])
def test_astra_r18_nonforward_steps_never_cancel_a_hole(case):
    base = trace_rows(250)
    rows = dict(**{'drop-and-dup': lambda: _shape(base, drop=60, dup=90), 'dup-only': lambda: _shape(base, dup=90),
                   'reorder-only': lambda: _shape(base, swap=90), 'drop-only': lambda: _shape(base, drop=60),
                   'drop-and-dup-with-send-error': lambda: _shape(base, drop=60, dup=90)})[case]()
    s = r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0)
    s['before_perf']['send_errors'] = 0
    s['reply_window_transport']['after']['send_errors'] = 1 if case == 'drop-and-dup-with-send-error' else 0
    r = sc.source_window(s, rows)
    assert r['passed'] is not True
    assert r['complete'] is False and r['harness_failure'] and r['device_send_failure'] is None   # never a PASS, never relabelled
    if case != 'drop-only':assert any('nonforward' in p for p in r['harness_failure'])


def test_astra_r18_exact_repro_on_smoke_r18_strict_run1():
    """In-memory copy of smoke-r18 strict run 1: one interior send removed, another duplicated at device_us+1."""
    from step8e2b_analysis import console_windows
    from uplink_continuity import recorded_audio_idle_windows
    d = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-10/requal-n1/smoke-r18/strict_voice_cat/attempt-1')
    if not d.exists():pytest.skip('evidence tree not present')
    record = json.loads((d/'result.json').read_text()); run = record['runs'][0]
    rows = [json.loads(l) for l in (d/'run1'/'arrivals.jsonl').read_text().splitlines()]
    w = console_windows(d/'serial.log')+recorded_audio_idle_windows(d/'serial.log', clock='device_us')
    assert sc.source_window(run, rows, w)['passed'] is True                                   # unmodified: PASS
    start, end = sc._window(run)[2:]
    sends = sorted((i for i, r in enumerate(rows) if r['kind'] == 12 and start < r['device_us'] < end), key=lambda i: rows[i]['device_us'])
    victim, twin = sends[len(sends)//3], sends[2*len(sends)//3]
    mutated = [r for i, r in enumerate(rows) if i != victim]+[dict(rows[twin], device_us=rows[twin]['device_us']+1)]
    r = sc.source_window(run, mutated, w)
    assert r['passed'] is None and r['complete'] is False and any('nonforward' in p for p in r['harness_failure'])


@pytest.mark.parametrize('errors', [0, 1])
def test_fable_r18_steps_0_then_2_are_incomplete(errors):
    """Fable r18: steps [0, 2] (a duplicate immediately followed by a hole) used to sum to missing = 0."""
    rows = [dict(r) for r in trace_rows(250)]
    k = 80
    rows[k+1] = dict(rows[k+1], seq=rows[k]['seq'], timestamp=rows[k]['timestamp'])        # step 0
    for r in rows[k+2:]:r['seq'] = (r['seq']) % 65536                                       # next step is +2
    steps = [(b['seq']-a['seq']) % 65536 for a, b in zip(rows[k:k+3], rows[k+1:k+3])]
    assert steps == [0, 2]
    s = r17_sample(rows, before_ow=0, drain_ow=0, reply_ow=0, export_ow=0)
    s['before_perf']['send_errors'] = 0; s['reply_window_transport']['after']['send_errors'] = errors
    r = sc.source_window(s, rows)
    assert r['complete'] is False and r['passed'] is None and r['device_send_failure'] is None


def test_converged_is_none_without_a_pre_prompt_drain():
    rows = trace_rows(250)
    assert sc.source_window(sample_for(rows), rows)['pre_prompt_converged'] is None
