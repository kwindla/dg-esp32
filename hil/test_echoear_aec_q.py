"""AEC-only qualification (decisions 88-92): gate lists, R1 echo decision, R9 loopback and precedence, the original
clip findings, the raw-only near-end delivery acceptance, verified overlap, AFE clipping in the scored interval, the
unset local-content rule, constructed edits with coverage, acquisition-time baseline/near validity, baseline reopen
dependency, word-run loopback attribution, the late-seal fault with the shared AEC-validity checks, the two-hold
calibration plan with distinct takes, the frozen-rule manifest, the per-hold timing estimator, swappable eligibility /
local rules, and fake-hardware end-to-ends through the inherited hold."""
import json
from pathlib import Path

import numpy as np
import pytest

import echoear_aec_q as Q
import echoear_aec_q_near as near
import echoear_aec_q_plan as qplan

EVIDENCE = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')
GOOD = dict(whole=dict(scorable=True, integrated_reduction_db=25), first_500ms=dict(scorable=True, integrated_reduction_db=15),
            post_guard=dict(scorable=True, residual_dbfs=-60), source_first_500ms_dbfs=-25)


@pytest.fixture(scope='module')
def plan():
    return qplan.plan_from()


def play(**over):
    return dict(mapped=True, latency=None, latency_passed=True, afe_clipped_samples=0, echo=dict(GOOD, **over))


# ---- decision-88 lists ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('gate,role', [
    ('device.reboot_crash', 'fault'), ('hardware.aec_errors', 'fault'),
    ('noise.cell_quiet_windows', 'validity'), ('clip.mic_room', 'validity'), ('clip.reference', 'validity'),
    ('timing.single_gap', 'validity'), ('alignment.near_witness', 'validity'), ('harness.jabra_duration', 'validity'),
    ('capture.cap', 'report'),                                                   # duration alone is not a gate (decision 92)
    ('unknown.unclassified', 'report'), ('observer.health', 'report'), ('transport.downlink_barge', 'report'),
    ('resources.heap', 'report'), ('admission.control_loss', 'report'), ('clip.afe_output', 'report')])
def test_gate_roles(plan, gate, role):
    assert Q.gate_role(gate, plan['q']) == role


def test_only_crash_and_aec_errors_fail(plan):
    import echoear_aec_gate_registry as registry
    assert {g['id'] for g in registry.REGISTRY if Q.gate_role(g['id'], plan['q']) == 'fault'} == {'device.reboot_crash', 'hardware.aec_errors'}


# ---- R1 echo decision ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('plays,expect', [
    ([play()], 'pass'),
    ([play(whole=dict(scorable=True, integrated_reduction_db=19))], 'miss'),
    ([play(post_guard=dict(scorable=True, residual_dbfs=-45))], 'miss'),
    ([play(guard_passed=False)], 'pass'),                                         # guard timing never a miss
    ([dict(play(), latency_passed=False, latency=dict(upper_s=1.21))], 'pass'),     # late playback is still mapped
    ([play(gate_unknown_active_frames=3)], 'unknown'),
    ([play(reason='No reference-active frames')], 'unknown'),
    ([play(source_first_500ms_dbfs=None)], 'unknown'),                           # Fable N1: not an acoustic miss
    ([dict(play(), mapped=False)], 'unknown'),
    ([], 'unknown'),
    ([dict(play(), afe_clipped_samples=4)], 'miss'),                             # AFE clipping inside the extent
])
def test_echo_decide(plays, expect):
    assert Q.echo_decide(plays)['result'] == expect


def test_classify_echo_and_required_failures(plan):
    q = plan['q']
    assert Q.classify(dict(kind='echo'), [], dict(q_echo=dict(result='miss')), q)['result'] == 'miss'
    assert Q.classify(dict(kind='echo'), [dict(gate='capture.cap')], dict(q_echo=dict(result='miss')), q)['result'] == 'miss'
    assert Q.classify(dict(kind='echo'), [dict(gate='clip.reference')], dict(q_echo=dict(result='miss')), q)['outcome'] == 'invalid'
    assert Q.classify(dict(kind='echo'), [], dict(q_echo=dict(result='unknown')), q)['outcome'] == 'invalid'
    assert Q.classify(dict(kind='echo'), [], {}, q, required_error='KeyError: x')['outcome'] == 'invalid'
    assert Q.classify(dict(kind='echo'), [], {}, q, acquired=False)['outcome'] == 'invalid'
    # optional v9 analysis failure is not part of the classification
    assert Q.classify(dict(kind='barge-firm-stop'), [], dict(v9_report_error='ValueError'), q)['result'] == 'deferred'


def test_score_proxy_keeps_the_original_clip_findings():
    import echoear_aec_v9_score as score
    p = Q._ScoreProxy(score)
    sc, f = p.combine(dict(kind='single-talk'), [dict(gate='clip.reference', delta=1)], {}, [dict(gate='x')], {})
    assert [x['gate'] for x in f] == ['clip.reference', 'x']
    assert p.load_attempt is score.load_attempt


# ---- R9 loopback ------------------------------------------------------------------------------------------------

def test_loopback_rule():
    reply = 'The ocean covers most of the planet and holds most of its water.'
    assert Q.loopback(['covers most of the'], reply) == ['covers most of the']
    assert Q.loopback(['the planet'], reply) == ['the planet']
    assert Q.loopback(['Stop.', 'what time is it'], reply) == []
    assert Q.loopback(['tell me the time in Paris'], 'Please tell me the time in Paris now', near_text='Please tell me the time in Paris') == []


def test_loopback_mixed_near_and_far_transcript():
    """Astra B4 / Fable N6: attribution by word runs; a transcript with the near sentence AND far-specific reply text
    is a hit; text explained only together with the near fixture is shared (reported, not a hit)."""
    reply = 'The ocean covers most of the planet and holds most of its water.'
    near_text = 'Please tell me the time in Paris'
    mixed = 'Please tell me the time in Paris the ocean covers most of the planet'
    lr = Q.loopback_runs([mixed], reply, near_text=near_text)
    assert [h['text'] for h in lr['hits']] == [mixed] and lr['hits'][0]['runs'] == ['the ocean covers most of the planet']
    assert Q.loopback([mixed], reply, near_text=near_text) == [mixed]
    assert Q.loopback(['the planet please tell me the time in paris'], reply, near_text=near_text) == []   # 2 words only
    shared_reply = 'Please tell me the time in Paris now'
    lr = Q.loopback_runs(['tell me the time in Paris'], shared_reply, near_text=near_text)
    assert lr['hits'] == [] and lr['shared'][0]['text'] == 'tell me the time in Paris'
    lr = Q.loopback_runs(['tell me the time in Paris now'], shared_reply, near_text=near_text)
    assert lr['hits'] == [] and lr['shared']            # 'in paris now' overlaps near-explained words: ambiguous
    lr = Q.loopback_runs(['what time is it in Paris', 'Stop.'], reply, near_text=near_text)
    assert lr == dict(hits=[], shared=[])


def _q(plan, tmp):
    r = Q.Q(plan, tmp); r.started = 0.0; r.clock = lambda: 0.0
    return r


def _all_measured(r, plan, *, echo='pass', near=lambda lid: dict(status='evaluated', valid=True, eligible=True, success=True)):
    for c in plan['q']['cells']:
        res = 'baseline' if c['kind'] == 'single-talk' else (echo if c['kind'] == 'echo' else 'deferred')
        st = dict(acquisitions=1, final=dict(outcome='measured', result=res, folder='x-'+c['logical_id'], acquisition=1), attempts=[])
        if c['kind'] == 'single-talk': st['baseline'] = dict(valid=True, stats=dict(path_gain_db=-15.0))
        r.state[c['logical_id']] = st
    for c in plan['q']['cells']:
        if c['kind'].startswith('barge-'):
            n = near(c['logical_id'])
            if n.get('status') == 'evaluated': n['baseline'] = r.baseline_ref(r.baseline_of[c['cell']['fixture']])
            r.state[c['logical_id']]['near'] = n


def test_verdict_precedence(plan, tmp_path):
    r = _q(plan, tmp_path)
    _all_measured(r, plan)
    assert r.verdict()['outcome'] == 'PASS'
    echo0 = [c['logical_id'] for c in plan['q']['cells'] if c['kind'] == 'echo' and c['volume_db'] == 0][:3]
    for lid in echo0: r.state[lid]['final']['result'] = 'miss'
    assert r.verdict()['outcome'] == 'FAIL'
    r.loopbacks = [dict(id='x', transcripts=['covers most of the'])]
    v = r.verdict(); assert v['outcome'] == 'INVESTIGATE' and v['acoustic_fail']   # R9 precedes ordinary FAIL
    r.fault = dict(gate='device.reboot_crash')
    assert r.verdict()['outcome'] == 'FAIL'                                        # crash stays FAIL


def test_unset_local_rule_and_missing_baseline_are_inconclusive(plan, tmp_path):
    r = _q(plan, tmp_path)
    _all_measured(r, plan, near=lambda lid: dict(status='evaluated', valid=True, eligible=True, success=None))
    assert r.verdict()['outcome'] == 'INCONCLUSIVE'
    _all_measured(r, plan, near=lambda lid: dict(status='waiting-for-baseline'))
    assert r.verdict()['outcome'] == 'INCONCLUSIVE'


def test_retake_policy(plan, tmp_path):
    r = _q(plan, tmp_path)
    lid = plan['q']['order'][0]
    r.state[lid] = dict(acquisitions=2, final=None, attempts=[])
    assert lid in r.todo()
    r.state[lid]['acquisitions'] = 3
    assert lid not in r.todo()
    other = plan['q']['order'][1]
    r.state[other] = dict(acquisitions=1, final=dict(outcome='measured', result='miss'), attempts=[])
    assert other not in r.todo()


def test_acquisition_time_baseline_and_near_validity(plan, tmp_path):
    """Decision 92: baseline template support and near-end delivery are evaluated when acquired, so the remaining
    acquisitions are usable; a barge cell waits for its baseline and is reopened when its delivery is not witnessed."""
    r = _q(plan, tmp_path)
    fixture = next(c for c in plan['q']['cells'] if c['kind'] == 'single-talk')['fixture']
    st_lid = r.baseline_of[fixture]
    barge = next(lid for lid, c in r.cells.items() if c['kind'].startswith('barge-') and c['cell']['fixture'] == fixture)
    calls = []
    r.baseline_eval = lambda cell, row: dict(valid=len(calls) > 0, reason='not single talk', stats={})
    r.near_eval = lambda cell, row, b: calls.append(1) or dict(valid=False, reason='near-end delivery not witnessed')
    r.state[barge] = dict(acquisitions=1, final=dict(folder='x'), attempts=[]); r._after_measured(barge)
    assert r.state[barge]['near']['status'] == 'waiting-for-baseline' and r.state[barge]['final'] is not None
    r.state[st_lid] = dict(acquisitions=1, final=dict(folder='x'), attempts=[]); r._after_measured(st_lid)
    assert r.state[st_lid]['final'] is None and st_lid in r.todo()             # baseline invalid -> reopened
    calls.append(0)
    r.state[st_lid]['final'] = dict(folder='y'); r._after_measured(st_lid)
    assert r.state[st_lid]['baseline']['valid'] and r.state[barge]['final'] is None and barge in r.todo()


def test_reopened_baseline_invalidates_dependent_near_results(plan, tmp_path):
    """Astra B3 / Fable B2: a reopened baseline (unresolved closing witness) stops lending its template; dependent near
    results become unsupported (waiting) and are re-evaluated from their retained acquisitions, not reacquired, once a
    valid replacement exists; results() requires the recorded baseline acquisition to be the final, valid one."""
    r = _q(plan, tmp_path)
    _all_measured(r, plan)
    assert r.verdict()['outcome'] == 'PASS'
    base = [lid for lid, c in r.cells.items() if c['kind'] == 'single-talk']
    r.unconfirmed = list(base); r.invalidate_unconfirmed()
    v = r.verdict()
    assert v['outcome'] == 'INCONCLUSIVE' and not any(b['valid'] for b in v['baselines'].values())
    assert all((r.state[l]['near']['status'] == 'waiting-for-baseline') for l, c in r.cells.items() if c['kind'].startswith('barge-'))
    # the replacement baseline: dependent results are re-evaluated from their retained acquisitions
    st_lid = base[0]; fixture = r.cells[st_lid]['fixture']
    dep = [l for l, c in r.cells.items() if c['kind'].startswith('barge-') and c['cell']['fixture'] == fixture]
    seen = []
    r.baseline_eval = lambda cell, row: dict(valid=True, stats={})
    r.near_eval = lambda cell, row, b: seen.append(row['folder']) or dict(valid=True, eligible=True, success=False)
    r.state[st_lid]['acquisitions'] = 2
    r.state[st_lid]['final'] = dict(outcome='measured', result='baseline', folder='replacement', acquisition=2)
    r._after_measured(st_lid)
    assert sorted(seen) == sorted('x-'+l for l in dep)                       # retained acquisitions re-evaluated
    for l in dep:
        n = r.state[l]['near']
        assert n['status'] == 'evaluated' and n['baseline'] == dict(id=st_lid, acquisition=2, folder='replacement')
        assert r.state[l]['acquisitions'] == 1 and r.state[l]['final'] is not None  # a valid miss is not reacquired
    states = r.results()['group_states']
    assert 'miss' in sum(states.values(), [])
    r.state[st_lid]['final']['folder'] = 'other'                             # the recorded acquisition is no longer final
    assert 'miss' not in sum(r.results()['group_states'].values(), [])


def test_evaluator_exceptions_are_bounded_unknown_retakes(plan, tmp_path):
    r = _q(plan, tmp_path)
    fixture = next(c for c in plan['q']['cells'] if c['kind'] == 'single-talk')['fixture']
    st_lid = r.baseline_of[fixture]
    barge = next(lid for lid, c in r.cells.items() if c['kind'].startswith('barge-') and c['cell']['fixture'] == fixture)
    def boom(*a): raise KeyError('x')
    r.baseline_eval = boom
    r.state[st_lid] = dict(acquisitions=1, final=dict(folder='x', acquisition=1), attempts=[]); r._after_measured(st_lid)
    assert r.state[st_lid]['final'] is None and 'evaluation error' in r.state[st_lid]['baseline']['reason'] and st_lid in r.todo()
    r.baseline_eval = lambda cell, row: dict(valid=True, stats={})
    r.state[st_lid]['final'] = dict(folder='y', acquisition=2); r._after_measured(st_lid)
    r.near_eval = boom
    r.state[barge] = dict(acquisitions=1, final=dict(folder='z', acquisition=1), attempts=[]); r._after_measured(barge)
    assert r.state[barge]['final'] is None and barge in r.todo() and 'near-end evaluation error' in r.state[barge]['attempts'][-1]['reason']
    r.state[barge]['acquisitions'] = 3
    assert barge not in r.todo()                                              # bounded by the retake budget


def test_unresolved_rank_keeps_group_inconclusive():
    import echoear_aec_v9_rules as rules
    states = ['missing', 'miss']+['success']*9+['ineligible']
    assert rules.barge_group(states) == 'INCONCLUSIVE' and rules._group_final(states) == 'PASS'


# ---- near-end measure ---------------------------------------------------------------------------------------------

def _synth(seed=3, ref_frames=None):
    rng = np.random.default_rng(seed)
    src = np.zeros(20000); env = np.sin(np.linspace(0, 12, 12000))**2
    src[3000:15000] = rng.standard_normal(12000)*6000*env
    span = near.active_span(src); n = span[1]-span[0]
    mic = lambda x: np.convolve(x, [1, .5, .2], mode='same')*10**(-15/20)
    st_raw = np.zeros(60000); o = 20000; st_raw[o:o+n] = mic(src[span[0]:span[1]])
    st_afe = np.zeros(60000); st_afe[o+1024:o+1024+n] = st_raw[o:o+n]
    base = near.baseline(st_raw, st_afe, src, span, o)
    raw = st_raw.copy()+rng.standard_normal(60000)*800
    afe = st_afe.copy()+rng.standard_normal(60000)*20
    ref = np.ones(60000//320, bool) if ref_frames is None else ref_frames
    return src, span, o, base, raw, afe, ref


DELIVERY = qplan.Q['nearend']['delivery']
ACCEPT = lambda m: True


def test_delivery_acceptance_is_raw_only_and_rejects_an_absent_stimulus():
    src, span, o, base, raw, afe, ref = _synth()
    al = near.align(raw, base, o+1900)                                          # +-150 ms search (decision 92)
    assert al['valid'] and abs(al['onset']-o) <= 2 and near.delivery_ok(al, DELIVERY)[0]
    assert -3 < al['near_level_db'] < 3
    zero = near.align(np.zeros_like(raw), base, o+200)
    ok, why = near.delivery_ok(zero, DELIVERY)
    assert not ok and 'raw corr' in why                                         # unknown -> retake, never a miss
    assert near.ALIGN_HALF == 2400 and qplan.Q['nearend']['align_half_samples'] == 2400


def test_verified_overlap_counts_reference_samples_not_padding():
    src, span, o, base, raw, afe, _ = _synth()
    ref = np.zeros(60000//320, bool)
    k0 = (o+3000)//320; ref[k0:k0+9] = True                                     # 180 ms of reference activity
    m = near.measure(raw, afe, ref, src, span, o, base, local_rule=ACCEPT)
    assert m['eligible'] is False and m['dt_frames'] < 15 and m['dt_frames_padded'] >= 15


def test_frame_mapping_is_sample_accurate():
    src, span, o, base, raw, afe, ref = _synth()
    ra = np.zeros(60000//320, bool); ra[(o+160)//320:] = True
    dt, s_act = near.dt_mask(ra, src, span, o+160)
    assert dt[0] or not s_act[0]


def test_content_bar_local_rule_and_clipping():
    src, span, o, base, raw, afe, ref = _synth()
    m = near.measure(raw, afe, ref, src, span, o, base)
    assert m['eligible'] and m['clause_corr'] and m['success'] is None           # local-content rule unset
    m = near.measure(raw, afe, ref, src, span, o, base, local_rule=ACCEPT, level_floor_report_db=-8.0)
    assert m['success'] is True and m['clause_level'] is None                   # level floor provisional: not gated
    assert m['report']['level_floor_provisional_pass'] is True and m['report']['level_floor_provisional_db'] == -8.0
    clipped = afe.copy(); clipped[o+1024+2000] = 32767
    assert near.measure(raw, clipped, ref, src, span, o, base, local_rule=ACCEPT)['success'] is False
    outside = afe.copy(); outside[100] = 32767
    assert near.measure(raw, outside, ref, src, span, o, base, local_rule=ACCEPT)['success'] is True


def _cov_rule(exposure='far-end', within=5):
    return near.rule_from(dict(name='coverage75', params=dict(fraction=.75, min_frames=10, bridge_frames=10, exposure=exposure,
                                                              onset_within_frames=within)), near.ELIGIBILITY)


def test_coverage75_measure_reports():
    """Decisions 93/94 on a synthetic cell through measure(): full exposure, an internal pause, the bot yielding early,
    the in-dip onset report, the release window."""
    src, span, o, base, raw, afe, _ = _synth()
    n = (span[1]-span[0])//320
    _, s_act = near.dt_mask(np.ones(60000//320, bool), src, span, o)
    act = np.flatnonzero(s_act)
    f0 = o//320
    def ref(on):
        r = np.zeros(60000//320, bool); r[on] = True; return r
    full = ref(slice(f0-30, f0+n+30))
    m = near.measure(raw, afe, full, src, span, o, base, eligibility=_cov_rule())
    c = m['coverage']
    assert m['eligible'] and c['fraction'] == 1.0 and c['uncovered_count'] == 0 and c['onset_covered_ms'] == 20*len(act)
    assert c['onset_mask'] == '11111' and c['leading_verified_frames'] == 5 and c['verified_count_first5'] == 5
    assert c['intersection_samples'] == 320*len(act) and c['far_end_end_ms'] > (span[1]-span[0])/16
    assert m['report']['onset_dip'] is None
    dip = full.copy(); k = f0+int(act[len(act)//2]); dip[k:k+3] = False            # a 3-capture-frame pause inside
    m = near.measure(raw, afe, dip, src, span, o, base, eligibility=_cov_rule())
    # onset is half a frame off the capture grid: 2 source frames fall below 50 % reference activity
    assert m['eligible'] and m['coverage']['uncovered']['internal'] == [int(act[len(act)//2])+i for i in range(2)]
    pre = full.copy(); pre[f0:f0+int(act[0])+2] = False                             # onset dip: 2 frames, within 5
    m = near.measure(raw, afe, pre, src, span, o, base, eligibility=_cov_rule())
    assert m['eligible'] and m['coverage']['uncovered']['prefix'] and m['coverage']['onset_mask'].startswith('0')
    assert m['report']['onset_dip']['frames'] == len(m['coverage']['uncovered']['prefix'])   # in-dip onset AFE corr
    k_end = int(act[0])+20                                                          # the bot yields: far end ends early
    half = ref(slice(f0-30, f0+k_end))
    m = near.measure(raw, afe, half, src, span, o, base, eligibility=_cov_rule())
    c = m['coverage']
    assert m['eligible'] and c['enclosed_frames'] == int(s_act[:k_end].sum()) and c['after_far_end_frames'] == int(s_act[k_end:].sum())
    assert abs(c['far_end_end_ms']-k_end*20) <= 20
    assert m['report']['release_window'] and m['report']['release_window']['frames'] >= 5
    few = ref(slice(f0-30, f0+int(act[0])+9))                                       # 9 verified frames < 10
    m = near.measure(raw, afe, few, src, span, o, base, eligibility=_cov_rule())
    assert m['eligible'] is False and '< 10' in m['reason']


def _cell(n, verified, *, ref_frames=None):
    """Frame-aligned pure probe: n source-active frames at capture frame 50; ref_frames: capture frames active."""
    s_act = np.ones(n, bool)
    ra = np.zeros(400, bool)
    for k in (ref_frames if ref_frames is not None else verified): ra[50+k] = True
    dt = np.array([bool(ra[50+k]) for k in range(n)])
    return dt, s_act, dict(onset=50*320, span=(0, n*320), ref_active=ra)


@pytest.mark.parametrize('last,ok', [(10, False), (11, False), (12, True)])
def test_stop_denominator_is_the_whole_fixture(plan, last, ok):
    """Decision 94 (a): the reference ends after frame 10 / 11 / 12 of the 15-frame stop: 10/15 and 11/15 are
    ineligible, 12/15 eligible; the post-yield frames are reported uncovered (suffix)."""
    rule = near.rule_from(plan['q']['nearend']['eligibility']['barge-firm-stop'], near.ELIGIBILITY)
    dt, s_act, ctx = _cell(15, range(-20, last))
    good, why, cov = rule(dt, s_act, ctx)
    assert good is ok and cov['required_frames'] == 12 and cov['enclosed_frames'] == 15
    assert cov['uncovered']['suffix'] == list(range(last, 15)) and cov['after_far_end_frames'] == 0


def test_stop_internal_gap(plan):
    rule = near.rule_from(plan['q']['nearend']['eligibility']['barge-firm-stop'], near.ELIGIBILITY)
    for gap, ok in ((3, True), (4, False)):
        dt, s_act, ctx = _cell(15, [k for k in range(-20, 40) if not 5 <= k < 5+gap])
        assert rule(dt, s_act, ctx)[0] is ok


@pytest.mark.parametrize('pause,expect', [(10, (20, 10)), (11, (5, 5))])
def test_sentence_exposure_is_one_interval(plan, pause, expect):
    """Decision 94 (b), Astra's probe: 50 source-active frames, 5 verified at onset, a pause, 5 more verified. A 200 ms
    pause is bridged (10 of 20 exposed: ineligible); a 220 ms pause ends the exposure, and the later run's 5 frames
    are excluded from the minimum and the content mask too (5 of 5, < 10: still ineligible)."""
    rule = near.rule_from(plan['q']['nearend']['eligibility']['barge-firm-sentence'], near.ELIGIBILITY)
    dt, s_act, ctx = _cell(50, list(range(-20, 5))+list(range(5+pause, 10+pause)))
    good, why, cov = rule(dt, s_act, ctx)
    assert good is False and (cov['enclosed_frames'], cov['verified_frames']) == expect
    assert cov['verified_after_exposure'] == (0 if pause == 10 else 5)


@pytest.mark.parametrize('mask,ok', [('11110', True), ('01111', True), ('11100', True), ('00001', True),
                                     ('00000', False)])
def test_sentence_onset_within_first_five(plan, mask, ok):
    """Decision 94 (c): the first verified source-active frame within the first 5 (uncovered prefix <= 4)."""
    rule = near.rule_from(plan['q']['nearend']['eligibility']['barge-firm-sentence'], near.ELIGIBILITY)
    head = [k for k, b in enumerate(mask) if b == '1']
    dt, s_act, ctx = _cell(40, head+list(range(6, 60)), ref_frames=[-3, -2, -1]+head+list(range(6, 60)))
    if mask == '00000': dt, s_act, ctx = _cell(40, list(range(6, 60)), ref_frames=list(range(-20, -1))+list(range(6, 60)))
    good, why, cov = rule(dt, s_act, ctx)
    assert good is ok, why
    assert cov['onset_mask'] == mask and cov['verified_count_first5'] == mask.count('1')
    assert cov['leading_verified_frames'] == len(mask)-len(mask.lstrip('1'))


def test_stop_onset_report_has_five_frames(plan):
    rule = near.rule_from(plan['q']['nearend']['eligibility']['barge-firm-stop'], near.ELIGIBILITY)
    dt, s_act, ctx = _cell(15, [k for k in range(-20, 40) if k != 1])
    cov = rule(dt, s_act, ctx)[2]
    assert cov['onset_mask'] == '10111' and cov['leading_verified_frames'] == 1 and cov['verified_count_first5'] == 4


def test_onset_dip_report_separates_prefix_and_internal_with_band_floor():
    """Decision 95 (report only): per-frame rows of the first 10 source-active frames with position, reference
    fraction, single-frame correlation, level deficit and the template band level against the AFE floor + 15 dB;
    prefix dips, internal dips and the verified comparator summarised apart; a per-fixture band onset."""
    src, span, o, base, raw, afe, _ = _synth()
    assert base['band_onset_frame'] is not None and base['afe_band_floor_dbfs'] is not None
    _, s_act = near.dt_mask(np.ones(60000//320, bool), src, span, o)
    act = np.flatnonzero(s_act); f0 = o//320; n = (span[1]-span[0])//320
    r = np.zeros(60000//320, bool); r[f0-30:f0+n+30] = True
    r[f0:f0+int(act[0])+2] = False                                                  # prefix dip
    k = f0+int(act[6]); r[k:k+3] = False                                            # internal dip
    m = near.measure(raw, afe, r, src, span, o, base, eligibility=_cov_rule())
    d = m['report']['onset_dip']
    assert m['eligible'] and d['prefix']['frames'] and d['internal']['frames'] and d['verified']['frames']
    assert set(d['prefix']['frames']).isdisjoint(d['internal']['frames'])
    assert all(x['position'] == ('prefix' if x['frame'] < min(d['verified']['frames']) else 'internal')
               for x in d['per_frame'] if x['role'] == 'dip')
    row = d['per_frame'][0]
    assert {'ref_fraction', 'corr', 'level_deficit_db', 'template_band_dbfs', 'above_floor'} <= set(row)
    assert d['margin_db'] == 15.0 and isinstance(row['above_floor'], bool)


def test_rules_reject_unknown_or_missing_parameter_keys(plan):
    ok = plan['q']['nearend']['eligibility']['barge-firm-sentence']
    assert near.rule_from(ok, near.ELIGIBILITY)
    stale = dict(name='coverage75', params=dict(fraction=.75, min_frames=10, bridge_frames=10, onset_ms=100))   # d93 spec
    with pytest.raises(ValueError, match='unknown parameter keys'): near.rule_from(stale, near.ELIGIBILITY)
    with pytest.raises(ValueError, match='missing parameter keys'):
        near.rule_from(dict(name='coverage75', params=dict(exposure='far-end')), near.ELIGIBILITY)
    for k in ok['params']:                                              # decision 96: no parameter defaults silently
        with pytest.raises(ValueError, match='missing parameter keys'):
            near.rule_from(dict(name='coverage75', params={x: y for x, y in ok['params'].items() if x != k}), near.ELIGIBILITY)
    with pytest.raises(ValueError, match='missing parameter keys'):
        near.rule_from(dict(name='segment-min-corr', params=dict(min_corr=.3)), near.LOCAL_RULES)
    with pytest.raises(ValueError, match='unknown parameter keys'):
        near.rule_from(dict(name='coverage75', params=dict(ok['params'], onset_within=5)), near.ELIGIBILITY)
    with pytest.raises(ValueError, match='exposure'):
        near.rule_from(dict(name='coverage75', params=dict(ok['params'], exposure='sentence')), near.ELIGIBILITY)
    with pytest.raises(ValueError, match='unknown parameter keys'):
        near.rule_from(dict(name='segment-min-corr', params=dict(min_corr=.3, orign='first_dt')), near.LOCAL_RULES)
    with pytest.raises(ValueError): qplan.build_manifest(dict(RULES, eligibility=stale), Path('/nonexistent'))


LEVEL_HOLE = dict(name='level-hole', params=dict(level_threshold_db=-18.7, seg_corr_threshold=0.12, seg_lag_window_samples=8,
                                                 window_frames=10, min_verified=5))


def test_level_hole_rule_catches_400ms_holes_not_attenuation():
    """Decision 97: the sliding 200 ms band level relative to the whole overlap. A 400 ms internal mute and a 200 ms
    ending mute are holes; a uniform attenuation is not; the minimum window and its position are reported."""
    src, span, o, base, raw, afe, ref = _synth()
    rule = near.rule_from(LEVEL_HOLE, near.LOCAL_RULES)
    m = near.measure(raw, afe, ref, src, span, o, base, local_rule=rule, eligibility=_cov_rule())
    assert m['success'] is True and m['hole']['min_db'] > -18.7 and m['hole']['position'] in ('onset', 'internal', 'ending')
    dt, s_act = near.dt_mask(ref, src, span, o)
    edits = {e['name']: e for e in near.constructed_edits(afe, o, span, dt)}
    for name in ('mute-internal-400ms', 'mute-ending-200ms', 'mute-ending-400ms'):
        me = near.measure(raw, edits[name]['afe'], ref, src, span, o, base, local_rule=rule, eligibility=_cov_rule())
        assert me['clause_corr'] and me['success'] is False and me['hole']['min_db'] < -18.7, (name, me['hole'])
    me = near.measure(raw, edits['mute-internal-400ms']['afe'], ref, src, span, o, base, local_rule=rule, eligibility=_cov_rule())
    assert me['hole']['position'] == 'internal'
    for name in ('window-minus-20dB', 'dt-only-minus-10dB'):
        me = near.measure(raw, edits[name]['afe'], ref, src, span, o, base, local_rule=rule, eligibility=_cov_rule())
        assert abs(me['hole']['min_db']) < 3 and me['clause_local'] is True, (name, me['hole'])
    no_splice = near.rule_from(dict(LEVEL_HOLE, params=dict(LEVEL_HOLE['params'], seg_corr_threshold=None)), near.LOCAL_RULES)
    assert no_splice(dict(hole=dict(min_db=-1.0), segments=[dict(corr=0.01, corr_lagtol=0.01)])) is True
    assert rule(dict(hole=dict(min_db=-1.0), segments=[dict(corr=0.01, corr_lagtol=0.01)])) is False   # the splice clause
    assert rule(dict(hole=dict(min_db=-1.0), segments=[dict(corr=0.01, corr_lagtol=0.5)])) is True      # lag-tolerant (d100)
    with pytest.raises(ValueError, match='lag-tolerant'): rule(dict(hole=dict(min_db=-1.0), segments=[dict(corr=0.5)]))
    assert rule(dict(hole=None)) is None                                                     # no window: unknown
    with pytest.raises(ValueError, match='window_frames'):
        near.rule_from(dict(LEVEL_HOLE, params=dict(LEVEL_HOLE['params'], window_frames=12)), near.LOCAL_RULES)
    with pytest.raises(ValueError, match='missing parameter keys'):
        near.rule_from(dict(name='level-hole', params=dict(level_threshold_db=-18.7)), near.LOCAL_RULES)


def test_splice_window_is_a_strict_declared_parameter():
    p = LEVEL_HOLE['params']
    with pytest.raises(ValueError, match='missing parameter keys'):
        near.rule_from(dict(name='level-hole', params={k: v for k, v in p.items() if k != 'seg_lag_window_samples'}), near.LOCAL_RULES)
    for bad in (8.0, -1, near.LAG+1, True):
        with pytest.raises(ValueError, match='seg_lag_window_samples'):
            near.rule_from(dict(name='level-hole', params=dict(p, seg_lag_window_samples=bad)), near.LOCAL_RULES)
    assert near.rule_from(LEVEL_HOLE, near.LOCAL_RULES).seg_lag_window == 8


def _shift_region(afe, a0, lo, hi, d):
    out = np.asarray(afe, float).copy(); out[a0+lo:a0+hi] = np.asarray(afe, float)[a0+lo-d:a0+hi-d]; return out


def test_lag_window_tolerates_jitter_but_not_leakage():
    """Decision 100: inside the declared +-8-sample window, a segment of the true near end that is locally shifted
    (timing jitter) passes; level-matched leakage of unrelated signal fails at every lag inside the window; a shift
    beyond the window is not rescued."""
    src, span, o, base, raw, afe, ref = _synth()
    rule = near.rule_from(LEVEL_HOLE, near.LOCAL_RULES)
    rule0 = near.rule_from(dict(LEVEL_HOLE, params=dict(LEVEL_HOLE['params'], seg_lag_window_samples=0)), near.LOCAL_RULES)
    kw = lambda r: dict(local_rule=r, eligibility=_cov_rule(), level_floor_db=-25.0)
    a0, n = o+near.AFE_DELAY, span[1]-span[0]
    lo, hi = n//2-3200, n//2+3200
    jit = _shift_region(afe, a0, lo, hi, 6)                                       # near end locally late by 6 samples
    m0, m8 = near.measure(raw, jit, ref, src, span, o, base, **kw(rule0)), near.measure(raw, jit, ref, src, span, o, base, **kw(rule))
    assert m0['success'] is False and m8['success'] is True, (min(x['corr'] for x in m0['segments']), min(x['corr_lagtol'] for x in m8['segments']))
    far = _shift_region(afe, a0, lo, hi, 40)                                      # outside the window: not rescued
    assert near.measure(raw, far, ref, src, span, o, base, **kw(rule))['success'] is False
    rng = np.random.default_rng(11)
    leak = rng.standard_normal(n+64)*1.0
    for d in range(-8, 9, 2):                                                      # level-matched leakage at in-window lags
        x = np.asarray(afe, float).copy(); w = x[a0+lo:a0+hi]
        e = leak[32+lo-d:32+hi-d]; x[a0+lo:a0+hi] = e*np.sqrt(np.mean(w**2))/np.sqrt(np.mean(e**2))
        m = near.measure(raw, x, ref, src, span, o, base, **kw(rule))
        assert m['clause_corr'] and m['success'] is False and min(s_['corr_lagtol'] for s_ in m['segments']) < 0.12, d
    dt, _ = near.dt_mask(ref, src, span, o)
    ed = {e['name']: e for e in near.constructed_edits(afe, o, span, dt, other=rng.standard_normal(20000)*1000,
                                                       echo=rng.standard_normal(20000)*500)}
    for name in ('leak-echo-local-400ms', 'leak-other-local-400ms'):
        assert ed[name]['label'] == 'content-loss' and ed[name]['covered'] > 0
        assert near.measure(raw, ed[name]['afe'], ref, src, span, o, base, **kw(rule))['success'] is False, name


def test_manifest_revalidates_bound_rules(tmp_path):
    """d100: check_manifest re-validates the bound rules; a d99-style level-hole without the declared window (or a
    tampered window) is refused even with a matching manifest sha."""
    a = tmp_path/'A'; a.mkdir()
    (a/'calibration-set.json').write_text(json.dumps(dict(hold='A', fault=None, restores_clean=True, complete_fixtures=21, total_fixtures=21)))
    rules = dict(RULES, local_rule=LEVEL_HOLE)
    m = qplan.build_manifest(rules, a)
    p = tmp_path/'m.json'; p.write_text(json.dumps(m)); assert qplan.check_manifest(p, qplan.manifest_sha(m))[0]['rules']['local_rule'] == LEVEL_HOLE
    old = json.loads(json.dumps(m)); del old['rules']['local_rule']['params']['seg_lag_window_samples']
    p.write_text(json.dumps(old))
    with pytest.raises(ValueError, match='missing parameter keys'): qplan.check_manifest(p, qplan.manifest_sha(old))
    bad = json.loads(json.dumps(m)); bad['rules']['local_rule']['params']['seg_lag_window_samples'] = 9.5
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match='seg_lag_window_samples'): qplan.check_manifest(p, qplan.manifest_sha(bad))
    with pytest.raises(ValueError, match='sha256'): qplan.check_manifest(p, qplan.manifest_sha(m))


def test_delivery_clause_names():
    al = dict(valid=True, corr=.01, ambiguity=1.0, boundary=True, near_level_db=-40.0)
    assert [k for k, _ in near.delivery_failures(dict(al, offset=10), DELIVERY)] == ['corr', 'boundary', 'level']
    rule = dict(DELIVERY, offset_max_samples=640)
    assert [k for k, _ in near.delivery_failures(dict(al, offset=700), rule)] == ['corr', 'boundary', 'offset', 'level']
    assert 'offset' not in [k for k, _ in near.delivery_failures(dict(al, offset=-640), rule)]
    assert near.delivery_failures(dict(valid=False, reason='x'), DELIVERY) == [('alignment', 'x')]


def test_wrong_stimulus_nulls():
    a = _synth(3); b = _synth(9)
    rows = near.wrong_stimulus_nulls({'a': a[3], 'b': b[3]}, {'a': (a[4], a[2]), 'b': (b[4], b[2])}, half=2400)
    assert {(r['template'], r['capture']) for r in rows} == {('a', 'b'), ('b', 'a')}
    assert all(r['corr'] < near.align(a[4], a[3], a[2])['corr'] for r in rows)


def test_echo_mapping_keeps_the_causality_bound():
    assert Q.playback_causal((10.0, 10.1), 10.05) and Q.playback_causal((10.0, 11.5), 9.0)      # late is still mapped
    assert not Q.playback_causal((9.0, 9.1), 10.0) and not Q.playback_causal(None, 10.0)


def test_constructed_edits_label_and_coverage():
    src, span, o, base, raw, afe, ref = _synth()
    dt, _ = near.dt_mask(ref, src, span, o)
    other = np.random.default_rng(2).standard_normal(len(src))*1000
    edits = near.constructed_edits(afe, o, span, dt, other=other, echo=np.random.default_rng(4).standard_normal(20000)*500)
    names = {e['name']: e for e in edits}
    for k in ('zeroed', 'mute-onset-80ms', 'mute-onset-400ms', 'mute-ending-200ms', 'mute-internal-400ms', 'splice-200ms',
              'wrong-speech', 'echo-level-matched', 'envelope-matched-noise'):
        assert names[k]['label'] == 'content-loss' and names[k]['covered'] > 0, k
    assert names['dt-only-minus-20dB']['label'] == 'attenuation (not content loss)'
    short = np.zeros_like(dt); short[:16] = dt[:16]                             # too little overlap for a 400 ms splice
    e2 = {e['name']: e for e in near.constructed_edits(afe, o, span, short)}
    assert e2['splice-400ms']['covered'] == 0                                   # a no-op is reported, not claimed


V910 = EVIDENCE/'v9.10-acceptance-01'
C10 = EVIDENCE/'campaign-10'


@pytest.mark.skipif(not (V910/'ledger.jsonl').exists() or not (C10/'analysis.json').exists(), reason='retained captures not on this host')
def test_retained_firm_stops_raw_alignment_delivery_and_content():
    from echoear_aec_bench import load_capture
    from echoear_aec_metrics import activity
    h = json.load(open(C10/'analysis.json'))['holds'][0]
    res = {c['run_id']: c for c in json.load(open(C10/'qualification-01/result.json'))['cases']}
    nc = next(c for c in h['cases'] if c['kind'] == 'near' and 'stop' in res[c['id']]['fixture'])
    braw, bafe, _, _ = load_capture(C10/'qualification-01'/nc['capture_dir']/'capture.npz')
    src, span = near.read_source(res[nc['id']]['fixture'])
    base = near.baseline(braw, bafe, src, span, near.single_talk_onset(braw, src, span))
    rows = [json.loads(l) for l in open(V910/'ledger.jsonl')]
    stops = [r for r in rows if r['type'] == 'attempt' and r['kind'] == 'barge-firm-stop']
    assert len(stops) == 11
    for r in stops:
        folder = V910/r['folder']; c = json.loads((folder/'result.json').read_text())['cases'][0]
        raw, afe, ref, blocks = load_capture(folder/'capture.npz')
        pred = near.predicted_onset(c, blocks, json.loads((folder/'anchors.json').read_text()), span, qplan.JABRA_DELAY_SAMPLES)
        al = near.align(raw, base, pred)
        assert near.delivery_ok(al, DELIVERY)[0], r['logical_id']
        assert abs(al['onset']-r['record']['barge']['near_onset']['raw_onset_sample']) <= 40, r['logical_id']
        qref = load_capture(folder.parent/'quiet.npz')[2]
        m = near.measure(raw, afe, activity(ref, qref, [(0, len(ref)-1024)]), src, span, al['onset'], base)
        assert m['eligible'] is False or m['corr'] >= .85, (r['logical_id'], m.get('corr'))
        assert m['dt_frames_padded'] >= m['dt_frames']


# ---- plan and calibration -------------------------------------------------------------------------------------------

def test_plan(plan):
    q = plan['q']
    fixtures = {c['cell']['fixture'] for c in q['cells'] if c['kind'].startswith('barge-')}
    base = [c for c in q['cells'] if c['kind'] == 'single-talk']
    assert {b['fixture'] for b in base} == fixtures and len(base) == 21
    assert q['nearend']['t_c'] == .5 and q['nearend']['level_floor_db'] is None and q['nearend']['local_rule'] is None
    assert q['nearend']['delivery']['status'] == 'PROVISIONAL' and 'capture.cap' not in q['validity']


def test_calibration_two_holds(plan):
    cal = qplan.calibration_plan(plan)
    assert set(cal['holds']) == {'A', 'B'} and cal['fixtures'] == 21
    for name, h in cal['holds'].items():
        assert h['acquisitions'] == 21*3+2 and sum(1 for x in h['order'] if x in h['null_cells']) == 2
    b = cal['holds']['B']['order']                                              # B in the qualification spread order
    assert cal['holds']['B']['order_mode'].startswith('spread') and cal['holds']['A']['order_mode'] == 'adjacent'
    adjacent = sum(1 for t in cal['takes'] if b[b.index(t['single'])+1:b.index(t['single'])+3] == t['takes'])
    assert adjacent == 0
    singles = [i for i, x in enumerate(b) if x.startswith('q-st-')]
    assert max(singles[i+1]-singles[i] for i in range(len(singles)-1)) <= 5
    assert all(3600 <= h['estimate_s'] <= 5400 for h in cal['holds'].values())
    assert cal['holds']['A']['order'] != cal['holds']['B']['order']             # seeded per hold, declared up front
    assert cal == qplan.calibration_plan(plan)                                   # deterministic
    stop = next(t for t in cal['takes'] if t['fixture'].endswith('stop-48k.wav'))
    assert stop['takes'][0] != stop['takes'][1] and stop['offsets_s'][0] != stop['offsets_s'][1]
    assert all(t['expected_double_talk_s'] for t in cal['takes'])
    # Astra/Fable B1: 65 distinct planned exposures per hold (retries, warm-up and witnesses excluded)
    for h in cal['holds'].values():
        assert len(h['order']) == len(set(h['order'])) == 65
    known = {c['logical_id'] for c in plan['q']['cells']} | {c['logical_id'] for c in cal['take_cells']}
    assert all(lid in known for h in cal['holds'].values() for lid in h['order'])
    assert len(cal['take_cells']) == 21 and len({c['cell']['id'] for c in cal['take_cells']}) == 21
    for t, c in zip(cal['takes'], cal['take_cells']):
        first = next(x for x in plan['q']['cells'] if x['logical_id'] == t['takes'][0])
        assert c['logical_id'] == first['logical_id']+'-t2' == t['takes'][1] and c['cell']['id'] == first['cell']['id']+'-t2'
        assert c['take_of'] == dict(logical_id=first['logical_id'], fixture=t['fixture'], rank=first['rank'],
                                    stop_offset_s=first['cell']['stop_offset_s'])
        assert c['cell']['fixture'] == t['fixture'] and t['offsets_s'][0] != t['offsets_s'][1]   # a different far-end position


# ---- fake-hardware end-to-ends ----------------------------------------------------------------------------------------

def _env(plan, monkeypatch, tmp_path, scenario=None):
    import test_echoear_aec_v9_lifecycle as lc
    env = lc.Env(tmp_path, monkeypatch, scenario=scenario or {}); env.p = plan
    kinds = {c['cell']['id']: c['kind'] for c in plan['q']['cells']+qplan.calibration_plan(plan)['take_cells']}
    env.kind_of = lambda cid: 'near-witness' if cid.startswith(('q-witness', 'v9-witness')) else (
        'echo' if cid.startswith('q-warmup') else ('near-witness' if kinds[cid] == 'single-talk' else kinds[cid]))
    env._part = 1
    return lc, env


def _acoustic(view, p, entry, a):
    if entry['kind'] == 'near-witness': return {}, [], dict(witness=dict(valid=True, offset=1024))
    return {}, [], {}


def _stubs():
    return dict(echo_eval=lambda view, a, rec: dict(result='pass'), baseline_eval=lambda cell, row: dict(valid=True, stats={}),
                near_eval=lambda cell, row, b: dict(valid=True, eligible=True, success=True))


def test_fake_hardware_end_to_end(plan, monkeypatch, tmp_path):
    lc, env = _env(plan, monkeypatch, tmp_path)
    r = Q.Q(plan, tmp_path/'out', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), **_stubs())
    v = r.run()
    assert v['outcome'] == 'PASS', (v['echo'], v['groups'], v['fault'], v['loopbacks'])
    rows = [json.loads(l) for l in open(tmp_path/'out'/'q-ledger.jsonl')]
    assert [x['type'] for x in rows].count('warmup') == 1 and sum(1 for x in rows if x['type'] == 'cell') == 97
    assert v['restores_clean'] and len(v['baselines']) == 21


@pytest.mark.parametrize('case', ['clean', 'clipped-raw', 'clipped-reference', 'aec-error'])
def test_late_seal_failure_scores_the_exported_capture(plan, monkeypatch, tmp_path, case):
    """Decision 92 / Astra B2: the seal acknowledgement fails after a complete capture; the exported PCM is scored (a
    valid miss stays a miss) after the same required AEC-validity checks as an ordinary acquisition: input clipping in
    the PCM -> unknown -> retake; an AEC-error observation -> FAIL."""
    import echoear_aec_campaign as C
    echo = next(c['logical_id'] for c in plan['q']['cells'] if c['kind'] == 'echo')
    echo_cell = next(c['cell']['id'] for c in plan['q']['cells'] if c['logical_id'] == echo)
    lc, env = _env(plan, monkeypatch, tmp_path, scenario={(echo, 1): 'late-seal-aec-error' if case == 'aec-error' else 'late-seal'})
    dump = C.dump_capture
    def clipped(board, path, timeout=150):
        dump(board, path, timeout=timeout)
        if case.startswith('clipped') and Path(path).parent.name == f'{echo_cell}-a1':
            with np.load(path) as d: pcm, timing = d['pcm'].copy(), d['timing'].copy()
            pcm[48000, 0 if case == 'clipped-raw' else 1] = 32767                  # mid-capture, not in the quiet windows
            np.savez(path, pcm=pcm, rate=16000, timing=timing)
    monkeypatch.setattr(C, 'dump_capture', clipped)
    stubs = _stubs(); stubs['echo_eval'] = lambda view, a, rec: dict(result='miss' if a['c']['id'].startswith(echo_cell) else 'pass')
    r = Q.Q(plan, tmp_path/'out', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), **stubs)
    v = r.run()
    assert any(x for x in env.attempts if x[1] == echo_cell)
    st = r.state.get(echo)
    if case == 'clean':
        assert st['acquisitions'] == 1 and st['final']['result'] == 'miss', st['attempts']
    elif case == 'aec-error':
        assert v['outcome'] == 'FAIL' and v['fault']['gate'] == 'hardware.aec_errors' and v['fault']['where'] == echo
    else:
        gate = 'clip.mic_unattributed' if case == 'clipped-raw' else 'clip.reference'
        assert st['attempts'][0]['outcome'] == 'invalid' and gate in st['attempts'][0]['reason'], st['attempts']
        assert st['acquisitions'] == 2 and st['final']['result'] == 'miss'   # retake (clean) measured


def test_aec_validity_unknown_without_end_observation(tmp_path):
    import echoear_aec_campaign as C
    folder = tmp_path/'a'; folder.mkdir()
    np.savez(folder/'capture.npz', pcm=np.zeros((32000, 3), '<i2'), rate=16000, timing=np.zeros((0, 5)))
    snap = dict(aec=dict(errors=0, reference_clips=0, capture=dict(adc_clips=0, gain_clips=0)))
    (folder/'before.json').write_text(json.dumps(snap))
    assert [f['gate'] for f in Q.Q.aec_validity(folder)] == ['q.support_unknown']
    (folder/'capture-end.json').write_text(json.dumps(dict(aec=dict(errors=0, reference_clips=0, capture=dict(adc_clips=0)))))
    assert [f['gate'] for f in Q.Q.aec_validity(folder)] == ['q.support_unknown']            # gain_clips missing
    (folder/'capture-end.json').write_text(json.dumps(snap))
    assert Q.Q.aec_validity(folder) == []


def test_hold_a_defects_retake_folders_and_dead_stimulus(plan, monkeypatch, tmp_path):
    """Hold A: (1) a single-talk retake used folder -a1 again (v9 counted only SCORED kinds) and was 'measured' on the
    stale capture; (2) a near-end stimulus that never played was charged as an acquisition while the session was dead.
    Now every acquisition has its own folder, and an unplayed stimulus ends the hold (acquisition failure)."""
    lc, env = _env(plan, monkeypatch, tmp_path)
    st_lid = next(x for x in qplan.calibration_plan(plan)['holds']['A']['order'] if x.startswith('q-st-'))
    barge = next(x for x in qplan.calibration_plan(plan)['holds']['A']['order'] if x.startswith('v9-') and '-t2' in x)
    calls = {}
    stubs = _stubs()
    def base_eval(cell, row):
        calls[cell['logical_id']] = calls.get(cell['logical_id'], 0)+1
        return dict(valid=not (cell['logical_id'] == st_lid and calls[st_lid] == 1), reason='no raw/dry onset', stats={})
    stubs['baseline_eval'] = base_eval
    orig, fired = Q.Q._case, []
    def case(folder):
        c = orig(folder)
        if Path(folder).name == f'{barge}-a1' and not fired: fired.append(1); return dict(c or {}, jabra=None)
        return c
    monkeypatch.setattr(Q.Q, '_case', staticmethod(case))
    r = Q.Q(plan, tmp_path/'cal', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), calibration='A', **stubs)
    v = r.run()
    rows = [json.loads(l) for l in open(tmp_path/'cal'/'q-ledger.jsonl')]
    cells = [x for x in rows if x['type'] == 'cell']
    assert len({x['folder'] for x in cells}) == len(cells)                     # never a reused capture folder
    st_rows = [x for x in cells if x['id'] == st_lid]
    assert [Path(x['folder']).name for x in st_rows if x['hold'] == 1][:2] == [f'{st_lid}-a1', f'{st_lid}-a2']
    ends = [x for x in rows if x['type'] == 'hold-end']
    assert ends[0]['reason'] == 'acquisition-failed' and len(ends) == 2 and ends[1]['reason'] == 'complete'
    b = [x for x in cells if x['id'] == barge]
    assert b[0]['outcome'] == 'invalid' and b[0]['reason'] == 'acquisition not completed' and 'q.support_unknown' in b[0]['findings']
    assert v['complete_fixtures'] == 21 and all(e['identity']['digest'] == v['identity']['start']['digest'] for e in ends)
    assert v['identity']['changed'] is False and Q.calibration_exit(v) == 0


@pytest.mark.parametrize('state', ['peer-lost', 'recovery-unproven'])
def test_session_health_ends_or_checks_the_hold(plan, monkeypatch, tmp_path, state):
    """Hold A: after the bot's liveness alarm the peer was gone and every later acquisition was dead. A witnessed peer
    loss now ends the hold at once (unconfirmed cells reopened); an unproven recovery runs a closing witness first."""
    lc, env = _env(plan, monkeypatch, tmp_path)
    first = plan['q']['order'][3]
    hit = []
    def health(self, runner, folder):
        if folder is not None and Path(folder).name == f'{first}-a1' and not hit: hit.append(1); return state
        return None
    monkeypatch.setattr(Q.Q, 'health', health)
    r = Q.Q(plan, tmp_path/'out', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), **_stubs())
    v = r.run()
    rows = [json.loads(l) for l in open(tmp_path/'out'/'q-ledger.jsonl')]
    ends = [x['reason'] for x in rows if x['type'] == 'hold-end']
    if state == 'peer-lost':
        assert ends == ['peer-lost', 'complete'] and any(x['type'] == 'cell-reopened' and x['id'] == first for x in rows)
    else:
        assert ends == ['complete'] and any(x['type'] == 'session-check' for x in rows)
    assert v['outcome'] == 'PASS'


def _fake_runner(tmp_path, *, aec_end, jabra=None, reuse=False):
    import types
    out = tmp_path/'run'; hold = out/'hold-1'; folder = hold/'q-st-01-a1'; folder.mkdir(parents=True)
    np.savez(folder/'capture.npz', pcm=np.zeros((32000, 3), '<i2'), rate=16000, timing=np.zeros((0, 5)))
    snap = lambda e: dict(aec=dict(errors=e, reference_clips=0, capture=dict(adc_clips=0, gain_clips=0)))
    (folder/'before.json').write_text(json.dumps(snap(0))); (folder/'capture-end.json').write_text(json.dumps(snap(aec_end)))
    (folder/'partial-descriptor.json').write_text(json.dumps(dict(jabra=jabra, events=[])))
    row = dict(attempt_id='p1/q-st-01-a1', record={}, folder='hold-1/q-st-01-a1', partial=True, error='HarnessFailure: ack')
    st = types.SimpleNamespace(attempts={'p1/q-st-01-a1': dict(findings=[dict(gate='harness.evaluation_ack')], pending=False)})
    return types.SimpleNamespace(acquire=lambda lid, key: row, ledger=types.SimpleNamespace(state=st), out=out,
                                 hold=types.SimpleNamespace(out=hold, events=lambda: []))


@pytest.mark.parametrize('aec_end,reuse,expect', [(1, False, 'fault'), (0, False, 'invalid'), (1, True, 'fault')])
def test_unusable_acquisition_still_dispatches_aec_fault(plan, tmp_path, aec_end, reuse, expect):
    """Astra d97 B1: an unplayed stimulus (or a reused folder) makes the measurement unusable, but a retained positive
    aec.errors delta is still a FAIL, dispatched before any retry or end-of-hold handling; clean -> unknown."""
    r = _q(plan, tmp_path)
    runner = _fake_runner(tmp_path, aec_end=aec_end)
    lid = r.baseline_of[next(iter(r.baseline_of))]
    if reuse: r.used_folders.add(str(runner.out/'hold-1/q-st-01-a1'))
    e, f, rec, acq, folder, err = r.acquire(runner, lid)
    assert acq is False and any(x['gate'] == 'q.support_unknown' for x in f)
    if expect == 'fault':
        assert any(x['gate'] == 'hardware.aec_errors' for x in f)
        with pytest.raises(Q._Fault): r.check_crash(runner, f, lid)
        assert r.fault['gate'] == 'hardware.aec_errors'
    else:
        assert not any(Q.gate_role(x['gate'], r.q) == 'fault' for x in f)
        r.check_crash(runner, f, lid)
        assert r.fault is None and Q.classify(e, f, rec, r.q, acquired=acq)['outcome'] == 'invalid'


def test_preflight_refuses_before_the_bench(monkeypatch):
    import step9a_echoear
    def bad(): raise RuntimeError('esptool unavailable: set ESPTOOL_PYTHON')
    monkeypatch.setattr(step9a_echoear, 'check_esptool', bad)
    with pytest.raises(SystemExit, match='preflight: esptool'): Q.preflight()


def test_optional_v9_analysis_failure_never_retakes(plan, monkeypatch, tmp_path):
    lc, env = _env(plan, monkeypatch, tmp_path)
    def broken(view, p, entry, a):
        if entry['kind'].startswith('barge-'): raise ValueError('report-only analysis failed')
        return _acoustic(view, p, entry, a)
    r = Q.Q(plan, tmp_path/'out', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=broken, clock=env.clock, sleep=lambda s: env.clock.advance(s), **_stubs())
    v = r.run()
    assert v['outcome'] == 'PASS' and all(s['acquisitions'] == 1 for s in r.state.values())


def test_fake_hardware_calibration_hold(plan, monkeypatch, tmp_path):
    lc, env = _env(plan, monkeypatch, tmp_path)
    r = Q.Q(plan, tmp_path/'cal', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), calibration='A', **_stubs())
    v = r.run()
    assert v['hold'] == 'A' and v['complete_fixtures'] == v['total_fixtures'] == 21 and v['restores_clean'], \
        v['incomplete']
    assert len(v['fixtures']['echo-null']['null']) == 2 and Q.calibration_exit(v) == 0
    rows = [json.loads(l) for l in open(tmp_path/'cal'/'q-ledger.jsonl')]
    cells = [x for x in rows if x['type'] == 'cell']
    assert len(cells) == len({x['id'] for x in cells}) == 65                 # 65 distinct planned exposures, one each
    for k, f in v['fixtures'].items():
        if k == 'echo-null': continue
        dt = f['double_talk']
        assert len(dt) == 2 and len({x['folder'] for x in dt}) == 2 and [x['take'] for x in dt] == [1, 2]
        assert dt[1]['take_of']['logical_id'] == dt[0]['id'] and dt[0]['stop_offset_s'] != dt[1]['stop_offset_s']
        assert all(a['folder'] for x in dt+f['single_talk'] for a in x['attempts'])   # Fable N7: folder per attempt
    v2 = dict(v, complete_fixtures=20)
    assert Q.calibration_exit(v2) == 1                                         # incomplete support -> nonzero
    assert (tmp_path/'cal'/'calibration-set.json').exists() and not (tmp_path/'cal'/'verdict.json').exists()


def test_supplementary_capture_plan_and_fake_run(plan, monkeypatch, tmp_path):
    """Decision 97 item 3: the hold-A supplement (sentence-10-normal baseline; sentence-02-48k baseline + take 2),
    unscored, declared here and not launched."""
    sup = qplan.calibration_plan(plan)['supplement']
    names = {c['logical_id']: c for c in plan['q']['cells']}
    assert sup['order'][0].startswith('q-st-') and names[sup['order'][0]]['fixture'].endswith('sentence-10-normal.wav')
    assert names[sup['order'][1]]['fixture'].endswith('sentence-02-48k.wav') and sup['order'][2].endswith('-t2')
    assert sup['acquisitions'] == 3 and sup['output'].endswith('-A-supp') and 'not launched' in sup['role']
    lc, env = _env(plan, monkeypatch, tmp_path)
    r = Q.Q(plan, tmp_path/'sup', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), calibration='A-supp', **_stubs())
    v = r.run()
    assert v['hold'] == 'A-supp' and v['total_fixtures'] == 2 and v['complete_fixtures'] == 2, v['incomplete']
    assert Q.calibration_exit(v) == 0


def test_manifest_declared_exclusions(tmp_path):
    a = tmp_path/'A'; a.mkdir()
    cs = dict(hold='A', fault=None, restores_clean=True, complete_fixtures=19, total_fixtures=21,
              incomplete=['sentence-02-48k.wav', 'sentence-10-normal.wav'])
    (a/'calibration-set.json').write_text(json.dumps(cs))
    with pytest.raises(ValueError, match='incomplete'): qplan.build_manifest(RULES, a)
    with pytest.raises(ValueError, match='incomplete'): qplan.build_manifest(RULES, a, excluded={'sentence-02-48k.wav': 'peer loss'})
    with pytest.raises(ValueError, match='reason'):
        qplan.build_manifest(RULES, a, excluded={'sentence-02-48k.wav': 'peer loss', 'sentence-10-normal.wav': ' '})
    ex = {'sentence-02-48k.wav': 'take 2 lost to the hold-1 peer loss', 'sentence-10-normal.wav': 'baseline lost (peer loss + retake folder defect)'}
    m = qplan.build_manifest(RULES, a, excluded=ex)
    assert m['hold_a']['excluded'] == ex and m['hold_a']['complete_fixtures'] == 19
    (a/'calibration-set.json').write_text(json.dumps(dict(cs, complete_fixtures=21, incomplete=[])))
    with pytest.raises(ValueError, match='complete hold A'): qplan.build_manifest(RULES, a, excluded=ex)


def _rec(d, hold, fixtures):
    d.mkdir()
    (d/'plan.json').write_text(json.dumps(dict(hold=hold)))
    fx = {}
    for name, (base, takes) in fixtures.items():
        fx[f'/x/{name}'] = dict(single_talk=[dict(id=f'st-{name}', folder=str(d/f'st-{name}') if base else None, baseline_valid=base)],
                                double_talk=[dict(id=f'{name}-t{n}', take=n, folder=str(d/f'{name}-t{n}') if n in takes else None)
                                             for n in ((1, 2) if hold == 'A' else takes)])     # a hold-A record lists both slots
    inc = [n for n, (b, t) in fixtures.items() if not (b and len(t) == 2)]
    (d/'calibration-set.json').write_text(json.dumps(dict(hold=hold, fault=None, restores_clean=True, fixtures=fx,
                                                          complete_fixtures=len(fixtures)-len(inc), total_fixtures=len(fixtures), incomplete=inc)))


def test_development_catalog_and_its_manifest_binding(tmp_path):
    """Decision 97 review: the derived catalog references and hashes both immutable records (A, A-supp), selects
    baselines and takes explicitly (a take uses its own record's baseline, else the other record's, flagged
    cross-record), and is bound in the manifest; a changed record is refused."""
    import echoear_aec_q_design as design
    a, sup = tmp_path/'A', tmp_path/'S'
    _rec(a, 'A', {'x.wav': (True, [1, 2]), 'normal-10.wav': (False, [1, 2]), 'sent-02.wav': (True, [1])})
    _rec(sup, 'A-supp', {'normal-10.wav': (True, []), 'sent-02.wav': (True, [2])})
    cat = design.build_catalog(a, sup)
    by = {x['fixture'].split('/')[-1]: x for x in cat['fixtures']}
    assert cat['supported'] == cat['total'] == 3
    n10 = by['normal-10.wav']['takes']
    assert [t['record'] for t in n10] == ['A', 'A'] and all(t['baseline']['record'] == 'A-supp' and t['cross_record'] for t in n10)
    s02 = by['sent-02.wav']['takes']
    assert [(t['take'], t['record'], t['baseline']['record'], t['cross_record']) for t in s02] == [(1, 'A', 'A', False), (2, 'A-supp', 'A-supp', False)]
    only_a = design.build_catalog(a)
    assert only_a['supported'] == 1 and set(only_a['records']) == {'A'} and only_a['slots'] == 6
    miss = {x['fixture'].split('/')[-1]: x['missing'] for x in only_a['fixtures']}
    assert miss['sent-02.wav'] == [dict(take=2, reason='no final capture in any record')]
    assert [m['reason'] for m in miss['normal-10.wav']] == ['no valid baseline in any record']*2
    # Fable d98 B1: a tampered 'supported' flag is refused (the catalog must equal a rebuild from its records)
    forged = json.loads(json.dumps(only_a)); forged['supported'] = 3
    for x in forged['fixtures']: x['supported'] = True
    fp = tmp_path/'forged.json'; fp.write_text(json.dumps(forged, sort_keys=True))
    with pytest.raises(ValueError, match='does not match a rebuild'): qplan.build_manifest(RULES, a, catalog=fp)
    with pytest.raises(ValueError, match='does not match a rebuild'): design.verify_catalog(forged)
    assert design.verify_catalog(json.loads(json.dumps(only_a, sort_keys=True)))
    p = tmp_path/'catalog.json'; p.write_text(json.dumps(cat, sort_keys=True))
    m = qplan.build_manifest(RULES, a, catalog=p)
    assert m['hold_a']['catalog']['sha256'] == qplan._sha_file(p) and m['hold_a']['excluded'] == {}
    assert set(m['hold_a']['catalog']['records']) == {'A', 'A-supp'}
    (tmp_path/'m.json').write_text(json.dumps(m)); sha = qplan.manifest_sha(m)
    assert qplan.check_manifest(tmp_path/'m.json', sha)[1] == sha
    (sup/'calibration-set.json').write_text((sup/'calibration-set.json').read_text().replace('"fault": null', '"fault": null, "x": 1'))
    with pytest.raises(ValueError, match='catalog record A-supp changed'): qplan.check_manifest(tmp_path/'m.json', sha)
    with pytest.raises(ValueError, match='not \'A\''): design.build_catalog(sup)
    with pytest.raises(ValueError, match='timing'):
        qplan.build_manifest(dict(RULES, timing={k: v for k, v in RULES['timing'].items() if k != 'prior_samples'}), a, catalog=p)
    with pytest.raises(ValueError, match='level_floor_db must be a number'):            # Fable d98 N2: null refused
        qplan.build_manifest(dict(RULES, level_floor_db=None), a, catalog=p)


def test_replay_inventory_accounts_for_every_slot():
    """Astra d98 caveat / Fable N5: all planned take slots accounted for; complete only when all are valid, eligible
    intact successes and none was skipped."""
    import echoear_aec_q_design as design
    cat = dict(slots=4, fixtures=[])
    ok = lambda f, t, cr=False: dict(fixture=f, take=t, record='A', cross_record=cr,
                                     intact=dict(valid=True, eligible=True, success=True, reason=None))
    inv = design.inventory(cat, [ok('a', 1), ok('a', 2), ok('b', 1, True)], [dict(fixture='b', take=2, reason='no final capture in any record')])
    assert (inv['slots'], inv['evaluated'], inv['skipped'], inv['eligible'], inv['cross_record'], inv['complete']) == (4, 3, 1, 3, 1, False)
    bad = ok('b', 2); bad['intact'] = dict(valid=False, eligible=None, success=None, reason='near-end delivery not witnessed: raw corr')
    inv = design.inventory(cat, [ok('a', 1), ok('a', 2), ok('b', 1), bad], [])
    assert inv['invalid'] == 1 and inv['complete'] is False and inv['rows'][-1]['reason'].startswith('near-end delivery')
    assert design.inventory(cat, [ok('a', 1), ok('a', 2), ok('b', 1), ok('b', 2)], [])['complete'] is True
    extra = ok('c', 1); extra['intact'] = dict(valid=True, eligible=False, success=None, reason='ineligible')
    assert design.inventory(cat, [ok('a', 1), ok('a', 2), ok('b', 1), ok('b', 2), extra], [])['complete'] is False   # evaluated != slots


def test_loose_level_floor_gates_total_suppression_only():
    """Fable d97 B1: a declared loose floor (-25 dB) fails total suppression, never the attenuation controls."""
    src, span, o, base, raw, afe, ref = _synth()
    rule = near.rule_from(LEVEL_HOLE, near.LOCAL_RULES)
    kw = dict(local_rule=rule, eligibility=_cov_rule(), level_floor_db=-25.0)
    assert near.measure(raw, afe, ref, src, span, o, base, **kw)['success'] is True
    dt, _ = near.dt_mask(ref, src, span, o)
    ed = {e['name']: e for e in near.constructed_edits(afe, o, span, dt)}
    assert ed['window-minus-30dB']['label'] == 'total suppression (level floor)'
    for name, ok in (('window-minus-20dB', True), ('dt-only-minus-20dB', True), ('window-minus-30dB', False), ('window-minus-40dB', False)):
        m = near.measure(raw, ed[name]['afe'], ref, src, span, o, base, **kw)
        assert m['success'] is ok and m['clause_level'] is ok, (name, m['level_deficit_db'])


def _qmanifest(digest=None):
    return dict(schema='aec-q-frozen-rules-1', rules=dict(RULES), scorer=dict(digest=digest or qplan.scorer_identity()['digest']))


def _qualify(plan, monkeypatch, tmp_path, scenario=None, cap_s=None, manifest=None, perf=None, **kw):
    lc, env = _env(plan, monkeypatch, tmp_path, scenario=scenario)
    env.perf.update(perf or {})
    p = plan if cap_s is None else dict(plan, q=dict(plan['q'], cap_s=cap_s))
    m = manifest or _qmanifest()
    r = Q.Q(p, tmp_path/'qual', hold_factory=env.hold_factory, budget_factory=lambda c: lc.FakeBudget(env.clock, c),
            acoustic=_acoustic, clock=env.clock, sleep=lambda s: env.clock.advance(s), manifest=m, qualify=True, **dict(_stubs(), **kw))
    return r, r.run(), env


def test_qualify_requires_a_frozen_manifest(plan, tmp_path):
    with pytest.raises(ValueError, match='requires the frozen-rule manifest'): Q.Q(plan, tmp_path/'x', qualify=True)
    with pytest.raises(ValueError, match='no scorer digest'): Q.Q(plan, tmp_path/'x2', qualify=True, manifest=dict(rules=dict(RULES)))
    with pytest.raises(ValueError, match='exclusive'): Q.Q(plan, tmp_path/'y', qualify=True, calibration='A', manifest={})
    with pytest.raises(SystemExit, match='requires --manifest'): Q.main(['qualify', '--confirm-bench'])
    with pytest.raises(SystemExit, match='both'): Q.main(['qualify', '--manifest', str(tmp_path/'m.json')])
    a = tmp_path/'A'; a.mkdir()
    (a/'calibration-set.json').write_text(json.dumps(dict(hold='A', fault=None, restores_clean=True, complete_fixtures=21, total_fixtures=21)))
    mm = qplan.build_manifest(RULES, a); p = tmp_path/'m.json'; p.write_text(json.dumps(mm))
    with pytest.raises(SystemExit, match='manifest refused'): Q.main(['qualify', '--manifest', str(p), '--manifest-sha256', '0'*64])
    with pytest.raises(SystemExit, match='manifest refused'): Q.main(['qualify', '--manifest', str(tmp_path/'none.json'), '--manifest-sha256', '0'*64])
    assert Q.main(['qualify', '--dry-plan', '--manifest', str(p), '--manifest-sha256', qplan.manifest_sha(mm)]) == 0
    with pytest.raises(SystemExit, match='confirm-bench'):
        Q.main(['qualify', '--manifest', str(p), '--manifest-sha256', qplan.manifest_sha(mm)])
    est = qplan.qualification_estimate(plan)
    assert est['cells'] == 97 and est['by_kind']['echo'] == 40 and est['cap_s'] == 9000 and est['estimate_s'] < est['cap_s']
    assert 'protected cleanup' in plan['q']['cap_rule'] and plan['q']['max_holds'] == 4 and plan['q']['restore_reserve_s'] == 360


def test_qualify_fake_run_pass_and_verdict_json(plan, monkeypatch, tmp_path):
    r, v, env = _qualify(plan, monkeypatch, tmp_path)
    assert v['outcome'] == 'PASS' and v['run_end'] == 'complete' and v['run_valid'] and v['manifest_sha256'] == qplan.manifest_sha(_qmanifest())
    sup = next(c for c in v['cells'] if c['kind'].startswith('barge-'))['support']
    assert set(sup) >= {'baseline', 'baseline_age_s', 'same_hold', 'path_gain_db', 'jabra_delay_offset_samples', 'delivery_margins'}
    assert len(v['cells']) == 97 and {c['state'] for c in v['cells'] if c['kind'] == 'echo'} == {'pass'}
    assert all(c['state'] == 'success' for c in v['cells'] if c['kind'].startswith('barge-'))
    on_disk = json.loads((tmp_path/'qual'/'verdict.json').read_text())
    assert on_disk['outcome'] == 'PASS' and on_disk['identity']['changed'] is False and Q.qualify_exit(on_disk) == 0


def test_qualify_retake_exhaustion_is_inconclusive(plan, monkeypatch, tmp_path):
    echo = next(c['logical_id'] for c in plan['q']['cells'] if c['kind'] == 'echo')
    r, v, env = _qualify(plan, monkeypatch, tmp_path, scenario={('score', echo, a): 'noisy' for a in (1, 2, 3)})
    row = next(c for c in v['cells'] if c['id'] == echo)
    assert row['acquisitions'] == 3 and row['state'] == 'missing' and all(x['outcome'] == 'invalid' for x in row['attempts'])
    assert v['outcome'] == 'INCONCLUSIVE' and 'INCONCLUSIVE' in v['echo'].values() and Q.qualify_exit(v) == 1


def test_qualify_crash_fails(plan, monkeypatch, tmp_path):
    calls = []
    def crash(out):
        calls.append(1)
        return (['Guru Meditation Error: Core 0 panic'] if len(calls) > 20 else []), None
    monkeypatch.setattr(Q.v9, 'candidate_crash_lines', crash)
    r, v, env = _qualify(plan, monkeypatch, tmp_path)
    assert v['outcome'] == 'FAIL' and v['fault']['gate'] == 'device.reboot_crash' and v['run_end'] == 'fault'
    r.loopbacks = [dict(id='x', transcripts=['covers most of the'])]
    assert r.verdict()['outcome'] == 'FAIL'                                   # a fault FAIL precedes INVESTIGATE


@pytest.mark.parametrize('finish_raises', [False, True])
def test_qualify_final_aec_error_fails_and_restores(plan, monkeypatch, tmp_path, finish_raises):
    """Astra qualify B1: an aec.errors increment that appears only at hold finalisation (settled-end), also when
    finish() then raises, is a sticky fault FAIL on otherwise passing cells; the board is still restored."""
    sc = {('final', 1): dict(perf=dict(aec_errors=1))}
    if finish_raises: sc[('finish-raise', 1)] = True
    r, v, env = _qualify(plan, monkeypatch, tmp_path, scenario=sc, perf=dict(aec_errors=0))
    assert v['outcome'] == 'FAIL' and v['fault']['gate'] == 'hardware.aec_errors' and v['fault']['where'] == 'hold-end'
    assert all(c['state'] in ('pass', 'success', 'baseline-valid') for c in v['cells']) and v['restores_clean']
    assert Q.qualify_exit(v) == 1
    assert Q.Q.final_aec_faults(tmp_path/'nope', dict(resources=dict(checks={'aec.errors': dict(passed=False, value=2)}))) == \
        [dict(where='resources.aec.errors', value=2)]


def test_final_aec_leaf_needs_positive_evidence(tmp_path):
    """Astra qualify r2 R1: check_snapshots marks unknown / missing data {'value': None, 'passed': False}; only a
    positive int leaf (or a positive counter delta) is a fault; null / missing / malformed / zero / bool leaves are
    diagnostics."""
    F = Q.Q.final_aec_faults
    leaf = lambda v, **k: dict(resources=dict(checks={'aec.errors': dict(dict(passed=False, value=v), **k)}))
    for v in (None, 0, 'x', 1.5, True, -1):
        diag = []
        assert F(tmp_path, leaf(v), diag) == [] and diag and diag[0]['value'] == v, v
    diag = []
    assert F(tmp_path, dict(resources=dict(checks={})), diag) == [] and diag == []                 # missing leaf
    assert F(tmp_path, dict(resources=dict(checks={'aec.errors': dict(value=None)})), diag) == []  # missing 'passed'
    assert F(tmp_path, leaf(3)) == [dict(where='resources.aec.errors', value=3)]                   # positive int
    snap = lambda e: json.dumps(dict(aec=dict(errors=e)))
    (tmp_path/'connected.json').write_text(snap(0)); (tmp_path/'settled-end.json').write_text(snap(2))
    diag = []
    assert F(tmp_path, leaf(None), diag) == [dict(where='settled-end', delta=2)] and diag       # null leaf + positive counter: FAIL


@pytest.mark.parametrize('case', ['unavailable', 'stable-other', 'mid-run'])
def test_qualify_identity_anchored_to_the_manifest(plan, monkeypatch, tmp_path, case):
    """Astra qualify B2: an unreadable identity or a digest other than the manifest's makes the run invalid (exit
    nonzero); at the start nothing is acquired; a change at a hold end stops after the hold has restored."""
    real = qplan.scorer_identity()
    calls = []
    def ident(root=None):
        calls.append(1)
        if case == 'unavailable': raise OSError('read failed')
        if case == 'mid-run' and len(calls) > 1: return dict(real, digest='f'*64)
        return real
    m = _qmanifest('e'*64) if case == 'stable-other' else _qmanifest(real['digest'])
    monkeypatch.setattr(qplan, 'scorer_identity', ident)
    r, v, env = _qualify(plan, monkeypatch, tmp_path, manifest=m)
    assert v['run_valid'] is False and v['run_end'] == 'identity invalid' and Q.qualify_exit(v) == 1
    if case == 'mid-run':
        assert env.attempts and v['restores_clean'] and len(v['identity']['hold_ends']) == 1
    else:
        assert not env.attempts and v['outcome'] == 'INCONCLUSIVE'                 # nothing acquired before the check
        if case == 'unavailable': assert v['identity']['start']['digest'] is None and 'read failed' in v['identity']['start']['error']


def test_qualify_failed_closing_witness_starts_another_hold(plan, monkeypatch, tmp_path):
    """Fable qualify B1 (its probe): the closing alignment witness after the last cell fails, the cells since the
    last valid witness are reopened, and the hold ends 'complete' with cells to do. The run must start another hold
    (time and holds remain) and reacquire them, not end INCONCLUSIVE."""
    orig, failed = Q.Q.witness, []
    def witness(self, runner, hold_no):
        if not self.todo() and not failed: failed.append(hold_no); return False     # the closing witness, once
        return orig(self, runner, hold_no)
    monkeypatch.setattr(Q.Q, 'witness', witness)
    r, v, env = _qualify(plan, monkeypatch, tmp_path)
    rows = [json.loads(l) for l in open(tmp_path/'qual'/'q-ledger.jsonl')]
    reopened = [x['id'] for x in rows if x['type'] == 'cell-reopened']
    assert failed == [1] and reopened and [x['hold'] for x in rows if x['type'] == 'hold-start'] == [1, 2]
    assert all(r.state[l]['acquisitions'] == 2 and r.state[l]['final'] for l in reopened)
    assert v['outcome'] == 'PASS' and v['run_end'] == 'complete' and v['run_valid']


def test_qualify_run_end_is_decided_not_wall_cap(plan, monkeypatch, tmp_path):
    r, v, env = _qualify(plan, monkeypatch, tmp_path, echo_eval=lambda view, a, rec: dict(result='miss'))
    assert v['outcome'] == 'FAIL' and v['run_end'] == 'decided'


def test_qualify_wall_cap(plan, monkeypatch, tmp_path):
    r, v, env = _qualify(plan, monkeypatch, tmp_path, cap_s=3000)
    assert v['run_end'] == 'wall cap' and v['wall_s'] <= 3000 and v['outcome'] == 'INCONCLUSIVE'
    assert any(c['state'] == 'missing' for c in v['cells']) and v['restores_clean']


def test_cli_gating(tmp_path):
    with pytest.raises(SystemExit, match='confirm-bench'):
        Q.main(['calibration', '--hold', 'A', '--out', str(tmp_path/'x')])
    with pytest.raises(SystemExit, match='manifest'):
        Q.main(['calibration', '--hold', 'B', '--out', str(tmp_path/'x'), '--confirm-bench'])
    with pytest.raises(ValueError, match='manifest'):
        Q.Q(qplan.plan_from(), tmp_path/'y', calibration='B')


RULES = dict(t_c=.5, level_floor_db=-25.0, delivery=dict(corr_min=.1, ambiguity_min=1.3, level_min_db=-12.0, level_max_db=6.0, offset_max_samples=640),
             timing=qplan.Q['nearend']['timing'], eligibility=dict(name='min-verified-frames', params=dict(frames=15)),
             local_rule=dict(name='segment-min-corr', params=dict(min_corr=.3, origin='first_dt', min_segments=1)),
             control_positions=[dict(null='q-echo-m6', offsets_samples=[-4800, 4800])])


def test_freeze_manifest_binds_hold_b(plan, tmp_path, monkeypatch):
    a = tmp_path/'A'; a.mkdir()
    cs = dict(hold='A', fault=None, restores_clean=True, complete_fixtures=20, total_fixtures=21)
    (a/'calibration-set.json').write_text(json.dumps(cs))
    with pytest.raises(ValueError, match='incomplete'): qplan.build_manifest(RULES, a)
    (a/'calibration-set.json').write_text(json.dumps(dict(cs, complete_fixtures=21)))
    with pytest.raises(ValueError, match='local_rule'): qplan.build_manifest(dict(RULES, local_rule=None), a)
    with pytest.raises(ValueError, match='missing'): qplan.build_manifest({k: v for k, v in RULES.items() if k != 'timing'}, a)
    rules = tmp_path/'rules.json'; rules.write_text(json.dumps(RULES))
    with pytest.raises(SystemExit, match='evidence tree'):                     # Fable d98 N3: never in /tmp
        Q.main(['freeze-manifest', '--rules', str(rules), '--hold-a', str(a), '--out', str(tmp_path/'m.json')])
    monkeypatch.setattr(qplan, 'PERSISTENT_ROOTS', (tmp_path,))
    assert Q.main(['freeze-manifest', '--rules', str(rules), '--hold-a', str(a), '--out', str(tmp_path/'m.json')]) == 0
    m = json.loads((tmp_path/'m.json').read_text()); sha = qplan.manifest_sha(m)
    assert qplan.check_manifest(tmp_path/'m.json', sha)[1] == sha
    with pytest.raises(ValueError, match='sha256'): qplan.check_manifest(tmp_path/'m.json', '0'*64)
    r = Q.Q(plan, tmp_path/'B', calibration='B', manifest=m)
    assert r.q['nearend']['local_rule'] == RULES['local_rule'] and r.q['nearend']['delivery']['ambiguity_min'] == 1.3
    assert r.q['manifest_sha256'] == sha
    (a/'calibration-set.json').write_text(json.dumps(dict(cs, complete_fixtures=21, note='edited')))
    with pytest.raises(ValueError, match='hold-A'): qplan.check_manifest(tmp_path/'m.json', sha)


def test_scorer_identity_covers_every_local_module(tmp_path):
    """Decision 95: the identity fingerprints every local hil/*.py; changing an imported helper in a temporary copy
    (the partial-path quiet gate, the playback template filter) makes check_manifest refuse hold B."""
    import shutil
    here = Path(qplan.__file__).parent
    ident = qplan.scorer_identity()
    assert set(ident['files']) == {p.name for p in here.glob('*.py')}
    for m in ('echoear_aec_v9_rules.py', 'echoear_aec_gate_registry.py', 'echoear_aec_v9.py', 'echoear_aec_noise_gate.py',
              'echoear_aec_nova_prestep.py', 'echoear_aec_playback_template.py'):
        assert m in ident['files']
    code = tmp_path/'hil'; code.mkdir()
    for p in here.glob('*.py'): shutil.copy2(p, code/p.name)
    a = tmp_path/'A'; a.mkdir()
    (a/'calibration-set.json').write_text(json.dumps(dict(hold='A', fault=None, restores_clean=True, complete_fixtures=21, total_fixtures=21)))
    m = qplan.build_manifest(RULES, a, root=code)
    (tmp_path/'m.json').write_text(json.dumps(m)); sha = qplan.manifest_sha(m)
    assert qplan.check_manifest(tmp_path/'m.json', sha, root=code)[1] == sha
    for name, old, new in (('echoear_aec_noise_gate.py', 'def quiet_gate(raw, baseline_dbfs, policy, mic3=None):\n',
                            'def quiet_gate(raw, baseline_dbfs, policy, mic3=None):\n    return dict(passed=True)\n'),
                           ('echoear_aec_playback_template.py', 'def filter_source(source,hz):\n',
                            'def filter_source(source,hz):\n    return source*0\n')):
        orig = (code/name).read_text(); assert old in orig
        (code/name).write_text(orig.replace(old, new, 1))
        with pytest.raises(ValueError, match=name):
            qplan.check_manifest(tmp_path/'m.json', sha, root=code)
        (code/name).write_text(orig)
        assert qplan.check_manifest(tmp_path/'m.json', sha, root=code)[1] == sha
    (code/'echoear_aec_new_helper.py').write_text('x = 1\n')                     # an added local module also differs
    with pytest.raises(ValueError, match='echoear_aec_new_helper.py'): qplan.check_manifest(tmp_path/'m.json', sha, root=code)


def test_hold_delay_estimator():
    rule = qplan.Q['nearend']['timing']
    assert near.hold_delay([10, 20, 30, 40], rule) == dict(delay_samples=-178, source='prior', n=4, median_offset=None)
    e = near.hold_delay([10, 20, 30, 40, 400, None], rule)
    assert e['source'] == 'hold' and e['delay_samples'] == -178+30 and e['n'] == 5


def test_eligibility_and_local_rule_are_swappable():
    src, span, o, base, raw, afe, ref = _synth()
    strict = near.rule_from(dict(name='min-verified-frames', params=dict(frames=10**4)), near.ELIGIBILITY)
    m = near.measure(raw, afe, ref, src, span, o, base, eligibility=strict)
    assert m['eligible'] is False and 'ms <' in m['reason']
    lr = near.rule_from(dict(name='segment-min-corr', params=dict(min_corr=.5, origin='source', min_segments=1)), near.LOCAL_RULES)
    assert near.measure(raw, afe, ref, src, span, o, base, local_rule=lr)['success'] is True
    lr = near.rule_from(dict(name='segment-min-corr', params=dict(min_corr=1.01, origin='source', min_segments=1)), near.LOCAL_RULES)
    assert near.measure(raw, afe, ref, src, span, o, base, local_rule=lr)['success'] is False
    assert near.rule_from(None, near.LOCAL_RULES) is None


def test_default_near_eval_applies_plan_rules_and_timing(plan, monkeypatch, tmp_path):
    """The production evaluator passes the plan's local rule and eligibility, and the per-hold delay estimate."""
    import echoear_aec_bench as bench, echoear_aec_metrics as metrics
    src, span, o, base, raw, afe, ref = _synth()
    folder = tmp_path/'c-a1'; folder.mkdir()
    (folder/'anchors.json').write_text('[]'); (folder/'result.json').write_text(json.dumps(dict(cases=[{}])))
    monkeypatch.setattr(Q, '_load', lambda row: (folder, raw, afe, np.zeros(60000), None))
    monkeypatch.setattr(near, 'read_source', lambda p: (src, span))
    seen = {}
    monkeypatch.setattr(near, 'predicted_onset', lambda c, b, a, sp, d: seen.setdefault('delay', d) and o)
    monkeypatch.setattr(bench, 'load_capture', lambda p: (None, None, np.zeros(10), None))
    monkeypatch.setattr(metrics, 'activity', lambda *a: ref)
    q = json.loads(json.dumps(plan['q']))
    q['nearend']['local_rule'] = dict(name='segment-min-corr', params=dict(min_corr=1.01, origin='source', min_segments=1))
    cell = next(c for c in plan['q']['cells'] if c['kind'] == 'barge-firm-stop')
    r = Q.default_near_eval(cell, dict(folder=str(folder)), dict(base=base), q, timing=dict(delay_samples=-150, source='hold'))
    assert seen['delay'] == -150 and r['valid'] and r['success'] is False and r['measure']['clause_local'] is False
    assert r['timing']['source'] == 'hold'
