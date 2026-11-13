"""N-1 survival (v9.5-v9.7): single immediate injected exception, clean audit/restore. One non-candidate
PAUSE-PART is injected at every schedule point of a fault-free trace (the setup of each entered part, each witness,
each scored attempt), through the real run_part / Runner / ledger path with the lifecycle fakes. Injection is at
acquisition entry (before capture time is spent); a setup injection is a link-probe failure after connect;
restoration costs 0 s unless stated. Point sets are derived per timing baseline from its own fault-free trace
(typical: 2 parts, 94 captures; stress: 3 parts, 95 captures incl. the part-3 setup and the final witness).
At typical timings every point must PASS within the 150 min ceiling and 3 entered parts. Stress results are
reported, not asserted. Separate targeted cases (restore cost 90 s / 360 s, a delayed mid-capture failure, an
invalid witness, setup noise) are reported alongside; they are not covered by the sweep counts.

The full sweeps run when V9_N1_FULL=1 (freeze precondition); the default suite runs a spread of points."""
import json
import os
from pathlib import Path

import pytest

import test_echoear_aec_v9_lifecycle as lc
import echoear_aec_v9 as R
from echoear_aec_v9_ledger import verdict

pytestmark = pytest.mark.skipif(not lc.plan_mod.SOURCE.exists(), reason='frozen v8.7 plan not on this host')
LABEL = 'single immediate injected exception, clean audit/restore'
TRACES = dict(typical=dict(parts=2, captures=94), stress=dict(parts=3, captures=95))   # checked by test_traces
BASELINE_CAPTURES = TRACES['typical']['captures']
FULL = os.environ.get('V9_N1_FULL') == '1'


def points(trace):
    return [('setup', n) for n in range(1, trace['parts']+1)]+[('capture', n) for n in range(1, trace['captures']+1)]


POINTS = points(TRACES['typical'])
STRESS_POINTS = points(TRACES['stress'])
QUICK = [('setup', 1), ('setup', 2), ('capture', 1), ('capture', 2), ('capture', 24), ('capture', 47),
         ('capture', 48), ('capture', 70), ('capture', 94)]
EXTRAS = [('restore-90s', ('setup', 1), dict(restore_delay_s=90)),
          ('restore-360s', ('setup', 1), dict(restore_delay_s=360)),
          ('restore-90s', ('capture', 48), dict(restore_delay_s=90)),
          ('restore-360s', ('capture', 48), dict(restore_delay_s=360)),
          ('delayed-mid-capture', ('capture', 24), dict(inject_delayed=True)),
          ('delayed-mid-capture', ('capture', 70), dict(inject_delayed=True)),
          # v9.10: three ambiguous acquisitions at one mid-part position (the retakes are spent) pause that part
          ('invalid-witness-x3', ('none', 0), dict(scenario={('witness-invalid', f'v9-witness-p1-0{i}'): True for i in (4, 5, 6)})),
          ('invalid-witness-x3', ('none', 0), dict(scenario={('witness-invalid', f'v9-witness-p2-0{i}'): True for i in (4, 5, 6)})),
          ('setup-noise', ('none', 0), dict(scenario={('loud', 1): True})),
          # v9.9: about 5 backlog boundaries per ~190-boundary campaign (pooled smokes: 3 of 90, 1.40-1.54 s)
          ('slow-drain-5x1.6s', ('none', 0), dict(slow_drain_s=1.6, slow_drain_at=(10, 30, 50, 70, 90)))]
DRAIN_REPORT = [('slow-drain-every-1.6s', dict(slow_drain_s=1.6, slow_drain_at=range(1, 200))),
                ('slow-drain-every-3.0s', dict(slow_drain_s=3.0, slow_drain_at=range(1, 200))),
                ('slow-drain-5x4.5s', dict(slow_drain_s=4.5, slow_drain_at=(10, 30, 50, 70, 90)))]


def run(tmp_path, monkeypatch, plan, point, *, stress=False, restore_delay_s=0, inject_delayed=False, scenario=None,
        slow_drain_s=0, slow_drain_at=()):
    kind, n = point
    scenario = dict(scenario or {})
    if kind == 'setup': scenario[('link', n)] = True
    env = lc.Env(tmp_path, monkeypatch, scenario=scenario, stress=stress); env.p = plan
    env.restore_delay_s = restore_delay_s; env.inject_delayed = inject_delayed
    env.allowance_s = plan['v9']['admission']['drain_allowance_s']          # v9.9 fixed declared allowance
    env.slow_drain_s, env.slow_drain_at = slow_drain_s, tuple(slow_drain_at)
    if kind == 'capture': env.inject_at = n
    out = tmp_path/'out'; out.mkdir()
    ledger = lc.Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=lc.REVIEWED, runtime_sha256={},
                       drain_allowance_s=plan['v9']['admission']['drain_allowance_s']))
    for part in range(1, 5):
        if ledger.state.closed: break
        if part > 1: ledger = lc.Ledger(out/'ledger.jsonl')
        env._part = part; env.run_part(out, ledger, part, mode='acceptance')
    ends = [r for r in ledger.rows if r['type'] == 'part-end']
    return dict(point=f'{kind}:{n}', outcome=(ledger.state.closed or {}).get('reason'),
                verdict=verdict(ledger.state, plan)['outcome'], entered=sum(1 for e in ends if e.get('entered', True)),
                wall_s=sum(e['wall_s'] for e in ends), parts=[e['reason'] for e in ends], captures=len(env.attempts))


@pytest.fixture(scope='module')
def plan():
    return lc.plan_mod.plan_from()


def test_fault_free_capture_count(tmp_path, monkeypatch, plan):
    env, out, ledger = lc.campaign(tmp_path, monkeypatch, plan)
    assert len(env.attempts) == BASELINE_CAPTURES and ledger.state.closed['reason'] == 'PASS'


@pytest.mark.parametrize('point', POINTS if FULL else QUICK, ids=lambda p: f'{p[0]}-{p[1]}')
def test_n1_survival_typical(monkeypatch, plan, point):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:          # removed at once: a full sweep writes ~2000 files per point
        r = run(Path(tmp), monkeypatch, plan, point)
    assert r['outcome'] == 'PASS' and r['verdict'] == 'PASS', r
    assert r['entered'] <= 3 and r['wall_s'] <= plan['v9']['parts']['ceiling_s'], r


@pytest.mark.parametrize('timing', ['typical', 'stress'])
def test_traces(monkeypatch, plan, timing):
    """The point sets are those of the fault-free trace of each timing baseline."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = run(Path(tmp), monkeypatch, plan, ('none', 0), stress=timing == 'stress')
    assert r['outcome'] == 'PASS' and r['entered'] == TRACES[timing]['parts'] and r['captures'] == TRACES[timing]['captures'], r


@pytest.mark.parametrize('case', EXTRAS, ids=lambda c: f"{c[0]}-{c[1][0]}{c[1][1]}-{sorted(c[2].get('scenario', {}) or c[2])[0]}".replace(' ', ''))
def test_n1_targeted_cases_typical(monkeypatch, plan, case):
    """Targeted cases outside the immediate-exception sweep, at typical timings: restoration costs 90 s (observed
    planning allowance) and 360 s (the full reserve) on every part, a failure after half the capture window, an
    invalid (ambiguous) witness opening a mid-part block, a loud room at the part-1 baseline. Stress: report."""
    import tempfile
    name, point, knobs = case
    with tempfile.TemporaryDirectory() as tmp:
        r = run(Path(tmp), monkeypatch, plan, point, **knobs)
    assert r['outcome'] == 'PASS' and r['entered'] <= 3 and r['wall_s'] <= plan['v9']['parts']['ceiling_s'], (name, r)
    if not name.startswith('slow-drain'): assert 'PAUSE-PART' in r['parts'], (name, r)   # the injected fault paused a part
    else: assert 'PAUSE-PART' not in r['parts'], (name, r)                                   # a 1.6 s drain never pauses


def test_n1_slow_drains_do_not_pause_a_stress_campaign(monkeypatch, plan):
    """v9.9: about 5 backlog boundaries at 1.6 s during a STRESS campaign (fixed 4.0 s allowance) never pause."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = run(Path(tmp), monkeypatch, plan, ('none', 0), stress=True, slow_drain_s=1.6, slow_drain_at=(10, 30, 50, 70, 90))
    assert r['outcome'] == 'PASS' and 'PAUSE-PART' not in r['parts'] and r['entered'] == 3, r


@pytest.mark.skipif(not FULL, reason='stress sweep runs with V9_N1_FULL=1')
def test_n1_stress_report(plan):
    import tempfile
    rows, extras = [], []
    for point in STRESS_POINTS:
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
            rows.append(run(Path(tmp), mp, plan, point, stress=True))
    for name, point, knobs in EXTRAS:
        for timing in ('typical', 'stress'):
            with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
                extras.append(dict(run(Path(tmp), mp, plan, point, stress=timing == 'stress', **knobs), case=name, timing=timing))
    for name, knobs in DRAIN_REPORT:          # boundary-drain tolerance of the 4 s allowance (reported)
        for timing in ('typical', 'stress'):
            with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
                extras.append(dict(run(Path(tmp), mp, plan, ('none', 0), stress=timing == 'stress', **knobs), case=name, timing=timing))
    report = dict(label=LABEL, stress_points=len(rows), stress_pass=sum(r['outcome'] == 'PASS' for r in rows), stress=rows,
                  targeted=extras)
    out = os.environ.get('V9_N1_REPORT')
    if out: Path(out).write_text(json.dumps(report, indent=1))
    worst = max((r for r in rows if r['outcome'] == 'PASS'), key=lambda r: r['wall_s'])
    print('N1_STRESS', json.dumps(dict(points=len(rows), passed=report['stress_pass'], worst_pass=worst,
                                       not_pass=[r['point'] for r in rows if r['outcome'] != 'PASS'],
                                       targeted=[(e['case'], e['point'], e['timing'], e['outcome'], round(e['wall_s'])) for e in extras])))
    assert all(r['wall_s'] <= plan['v9']['parts']['ceiling_s'] and r['entered'] <= 3 for r in rows+extras)
    assert all(e['outcome'] == 'PASS' for e in extras if e['case'] == 'slow-drain-5x1.6s')


# ---- v9.6 (decision 81): the last allowed part takes the ceiling left -------------------------------

@pytest.mark.parametrize('point', [('setup', 1), ('capture', 8)], ids=lambda p: f'{p[0]}-{p[1]}')
def test_v96_stress_early_pause_passes(monkeypatch, plan, point):
    # v9.5 closed both INCONCLUSIVE: setup:1 on the in-part capacity check of a 60 min part 3 (1782 s needed,
    # 1781.6 s left), capture:8 one cell short with three parts used.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)/'out'
        r = run(Path(tmp), monkeypatch, plan, point, stress=True)
        rows = [json.loads(l) for l in (out/'ledger.jsonl').read_text().splitlines()]
    assert r['outcome'] == 'PASS' and r['verdict'] == 'PASS', r
    starts = [x for x in rows if x['type'] == 'part-start']
    ends = [x for x in rows if x['type'] == 'part-end']
    assert len(starts) == 3 and r['entered'] <= 3 and r['wall_s'] <= plan['v9']['parts']['ceiling_s'], r
    used = sum(e['wall_s'] for e in ends[:2])
    assert starts[2]['budget']['total_s'] == pytest.approx(min(plan['v9']['parts']['ceiling_s']-used, plan['v9']['parts']['last_part_total_max_s']))
    assert starts[2]['budget']['total_s'] > plan['v9']['parts']['planned_total_s']     # uncapped beyond 60 min


def _ledger(tmp_path, walls):
    led = lc.Ledger(tmp_path/'ledger.jsonl', create=True)
    led.append(dict(type='campaign', mode='acceptance', plan_sha256=lc.REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for n, wall in enumerate(walls, 1):
        led.append(dict(type='part-start', part=n, slot=n, budget=dict(total_s=3600)))
        led.append(dict(type='part-end', part=n, slot=n, entered=True, started=True, reason='PAUSE-PART', gates=[],
                        wall_s=wall, audit=dict(clean=True, findings=[], notes=[]), restore=dict(clean=True)))
    return led


@pytest.mark.parametrize('walls', [(0,), (331, 2862), (331, 331), (180, 180), (3600, 3600), (3600, 5000), (8999, 0)])
def test_v96_budget_never_exceeds_ceiling(tmp_path, plan, walls):
    v9 = plan['v9']
    led = _ledger(tmp_path, walls)
    used = sum(walls)
    for slot in (1, 2, 3):
        b = R.part_budget(plan, led, slot, mode='acceptance')
        assert b['total_s'] <= v9['parts']['ceiling_s']-used
        if slot < 3: assert b['total_s'] <= v9['parts']['planned_total_s']
        else: assert b['total_s'] == min(v9['parts']['ceiling_s']-used, v9['parts']['last_part_total_max_s'])
        assert b['total_s']*v9['parts']['rtp_packets_per_s'] <= plan['time_estimate']['v85_budget']['rtp_evidence_max_packets']
    # capacity() never counts more than the ceiling left, net of setup and reserve per future part
    ok, cap = R.capacity(plan, led, [], part=len(walls))
    assert cap['have_s'] <= max(0, v9['parts']['ceiling_s']-used)


def test_v96_three_part_maximum_holds(tmp_path, monkeypatch, plan):
    led = _ledger(tmp_path, (331, 331, 331))
    env = lc.Env(tmp_path, monkeypatch); env.p = plan
    out = tmp_path/'out'; out.mkdir()
    env.run_part(out, led, 4, mode='acceptance')
    rows = led.rows
    assert not [x for x in rows if x['type'] == 'part-start' and x['part'] == 4]
    assert rows[-1]['type'] == 'campaign-close' and rows[-1]['reason'] == 'INCONCLUSIVE'
    assert rows[-1]['gates'] == ['parts.time_caps']


# ---- v9.10: noisy witness at every witness position -------------------------------------------------------

def _witness_positions(plan, monkeypatch, stress):
    """Witness ids of the fault-free trace (one per position, in acquisition order)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        env = lc.Env(Path(tmp), monkeypatch, stress=stress); env.p = plan
        env.allowance_s = plan['v9']['admission']['drain_allowance_s']
        out = Path(tmp)/'out'; out.mkdir()
        ledger = lc.Ledger(out/'ledger.jsonl', create=True)
        ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=lc.REVIEWED, runtime_sha256={}, drain_allowance_s=4.0))
        for part in range(1, 4):
            if ledger.state.closed: break
            if part > 1: ledger = lc.Ledger(out/'ledger.jsonl')
            env._part = part; env.run_part(out, ledger, part, mode='acceptance')
        return [cid for _, cid, _ in env.attempts if cid.startswith('v9-witness')]


WITNESS_TRACES = dict(typical=18, stress=19)     # witness positions of the fault-free traces (checked below)


@pytest.mark.parametrize('timing', ['typical', 'stress'])
def test_n1_noisy_witness_at_every_position_passes(monkeypatch, plan, timing):
    """v9.10: the first acquisition at each witness position is noise-contaminated (INVALID-CELL), the retake is
    valid. Every position must PASS within the ceiling and 3 entered parts, without a paused part. The full set
    runs with V9_N1_FULL=1; the default suite runs the first, a mid-part and the last position of each part."""
    import tempfile
    with pytest.MonkeyPatch.context() as mp:
        ids = _witness_positions(plan, mp, timing == 'stress')
    assert len(ids) == WITNESS_TRACES[timing], ids
    chosen = ids if FULL else sorted({ids[0], ids[len(ids)//2], ids[-1]}, key=ids.index)
    for wid in chosen:
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
            r = run(Path(tmp), mp, plan, ('none', 0), stress=timing == 'stress', scenario={('score', wid, 1): 'noisy'})
        assert r['outcome'] == 'PASS' and 'PAUSE-PART' not in r['parts'], (wid, r)
        assert r['entered'] <= 3 and r['wall_s'] <= plan['v9']['parts']['ceiling_s'], (wid, r)
