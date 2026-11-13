"""v9 gate registry: completeness against a reviewed inventory, and table-driven verdict
semantics of echoear_aec_v9_rules. Host-only; never imports the hardware campaign module."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

import echoear_aec_gate_registry as r
import echoear_aec_v9_rules as v

ROOT = Path(__file__).resolve().parents[1]
PLAN = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9/freeze-v8.7-nova-v86/plan.json')
FIELDS = ('id', 'cls', 'applicability', 'description', 'evidence', 'score_effect', 'control_action',
          'release_effect', 'reset_scope')


def v87_keys():
    return json.loads((ROOT/r.V87_KEYS_FILE).read_text())


def reviewed():
    return json.loads((ROOT/r.INVENTORY_FILE).read_text())


# ---- completeness -------------------------------------------------------------------------

def test_snapshot_matches_frozen_v87_plan():
    data = v87_keys()
    assert data['plan_sha256'] == r.V87_PLAN_SHA256
    if not PLAN.exists():
        pytest.skip('v8.7 freeze bundle not on this host; snapshot checked against registry only')
    assert hashlib.sha256(PLAN.read_bytes()).hexdigest() == r.V87_PLAN_SHA256
    assert data['keys'] == r.plan_gate_keys(json.loads(PLAN.read_text()))


def test_every_v87_plan_key_has_exactly_one_owner():
    keys = v87_keys()['keys']
    assert not [k for k in keys if r.owner_of(k) is None]
    stale = [p for g in r.REGISTRY for p in g['plan_keys'] if not any(r.covers(p, k) for k in keys)]
    stale += [p for p in r.NON_GATE if not any(r.covers(p, k) for k in keys)]
    assert not stale


def test_fresh_scan_equals_reviewed_inventory():
    fresh = r.scan(ROOT, v87_keys()['keys'])
    assert r.check_inventory(fresh, reviewed()) == []
    claims = r.literal_claims()
    used = {(s[0], s[3]) for s in fresh['raise_sites']}
    assert set(claims) <= used, set(claims) - used
    for target in r.SCOPES.values():
        assert target.startswith('EXCLUDED: ') or target in r.BY_ID, target
    assert {t for g in r.REGISTRY for t in g['terms']} == set(fresh['terms'])


def test_new_or_unrecognized_gate_requires_classification(tmp_path):
    base = reviewed()
    # A new raise inside the acquisition path: no literal claim and no scope -> unclassified.
    src = tmp_path/'hil'/'echoear_aec_campaign.py'
    src.parent.mkdir()
    src.write_text((ROOT/'hil/echoear_aec_campaign.py').read_text().replace(
        "        began=time.monotonic();anchors=[capture_start(self.board)]",
        "        if self.number<0:raise RuntimeError('New v9 gate')\n"
        "        began=time.monotonic();anchors=[capture_start(self.board)]", 1))
    site = next(s for s in r.raise_sites(tmp_path, 'hil/echoear_aec_campaign.py') if s[3] == 'New v9 gate')
    assert site[0] == 'BenchHold.capture'
    assert r.resolve('hil/echoear_aec_campaign.py', site[0], site[3]) is None
    fresh = copy.deepcopy(base)
    fresh['raise_sites'].append(['hil/echoear_aec_campaign.py', site[0], site[2], site[3], None])
    kinds = {p[0] for p in r.check_inventory(fresh, base)}
    assert {'unreviewed raise site', 'unclassified raise site'} <= kinds
    # A raise that a scope would auto-classify still needs a reviewed inventory row.
    assert r.resolve('hil/echoear_aec_campaign.py', 'run', 'Another new gate') == 'parts.ledger'
    fresh = copy.deepcopy(base)
    fresh['raise_sites'].append(['hil/echoear_aec_campaign.py', 'run', 'RuntimeError', 'Another new gate', 'parts.ledger'])
    assert [p[0] for p in r.check_inventory(fresh, base)] == ['unreviewed raise site']
    # A new nested plan leaf under an owned prefix and an unowned leaf.
    fresh = copy.deepcopy(base)
    fresh['plan_leaves']['gates.echo.new_threshold_db'] = r.owner_of('gates.echo.new_threshold_db')
    fresh['plan_leaves']['gates.brand_new.limit'] = r.owner_of('gates.brand_new.limit')
    kinds = [p for p in r.check_inventory(fresh, base)]
    assert ('unreviewed plan_leaves', 'gates.echo.new_threshold_db') in kinds
    assert ('unclassified plan_leaves', 'gates.brand_new.limit') in kinds
    # A new check name and a silently changed owner.
    fresh = copy.deepcopy(base)
    fresh['terms']['resources:new_counter'] = None
    fresh['terms']['resources:aec.errors'] = 'clip.afe_output'
    kinds = {p[0] for p in r.check_inventory(fresh, base)}
    assert {'unreviewed terms', 'unclassified terms', 'changed owner terms'} <= kinds


def test_entries_are_complete_and_classified():
    ids = [g['id'] for g in r.REGISTRY]
    assert len(ids) == len(set(ids))
    for g in r.REGISTRY:
        assert g['cls'] in r.CLASSES, g['id']
        assert g['applicability'] and set(g['applicability']) <= set(r.KINDS), g['id']
        for field in FIELDS: assert g[field], (g['id'], field)
        if g['new']: assert not g['source'], g['id']
        else: assert g['source'] or g['plan_keys'], g['id']
        assert set(g['invalidates']) <= set(r.ENDPOINTS), g['id']
        assert bool(g['invalidates']) == (g['cls'] == 'INVALID-CELL'), g['id']
        if g['hard']: assert g['cls'] == 'SCORE', g['id']
        if g['aggregate']: assert g['cls'] == 'PAUSE-PART', g['id']
        if g['room_witness'] or g['outcome_dependent']: assert g['cls'] == 'INVALID-CELL', g['id']


def test_sources_point_at_enforcement_lines():
    for g in r.REGISTRY:
        for path, line, anchor in g['source']:
            lines = (ROOT/path).read_text().splitlines()
            assert 1 <= line <= len(lines) and anchor in lines[line-1], (g['id'], path, line, anchor)


def test_doc_table_matches_registry():
    doc = (ROOT/'docs/echoear-aec-acceptance-v9.md').read_text()
    begin, end = '<!-- registry:begin -->\n', '\n<!-- registry:end -->'
    assert doc[doc.index(begin)+len(begin):doc.index(end)] == r.markdown_table()
    for cls, n in r.counts().items():
        assert f'| {cls} | {n} |' in doc


def test_required_classifications():
    c = {g['id']: g['cls'] for g in r.REGISTRY}
    assert c['unknown.unclassified'] == 'PAUSE-PART'
    assert (c['clip.mic_room'], c['clip.afe_output'], c['clip.mic_unattributed']) == ('INVALID-CELL', 'SCORE', 'PAUSE-PART')
    assert (c['admission.control_loss'], c['admission.drain_timeout'], c['admission.counter_schema']) == \
        ('CANDIDATE-FAIL', 'PAUSE-PART', 'PAUSE-PART')
    assert (c['transport.downlink_echo'], c['transport.downlink_barge']) == ('MONITOR', 'INVALID-CELL')
    # Transport/network never invalidates an echo cell.
    netlike = ('transport.', 'rf.capture', 'playback.transport', 'delivery.send')
    assert not [g['id'] for g in r.REGISTRY if g['id'].startswith(netlike) and g['cls'] == 'INVALID-CELL'
                and 'echo' in g['applicability']]
    assert (c['barge.recognition'], c['barge.latency'], c['bot.service_outage']) == ('SCORE', 'SCORE', 'INVALID-CELL')
    assert c['echo.zero_db_floor'] == 'MONITOR'                                   # decision 72
    assert (c['tts.long_lead'], c['timing.single_gap'], c['capture.gap_firmware']) == ('MONITOR', 'PAUSE-PART', 'CANDIDATE-FAIL')
    assert c['resources.part_audit'] == 'PAUSE-PART' and r.BY_ID['resources.part_audit']['aggregate']
    assert {g['id'] for g in r.REGISTRY if g['hard']} == {'echo.hard_floor', 'echo.false_turns', 'barge.false_interruption'}
    assert r.V9_PLAN_VALUES['timing.timer_lateness_max_s'] == 0.100
    for gate in ('freeze.admission_regression', 'freeze.burst_smoke', 'freeze.lifecycle_test',
                 'freeze.analyzer_dry_run', 'freeze.scoring_cost'):
        assert c[gate] == 'PAUSE-PART'
    for gate in ('device.reboot_crash', 'hardware.aec_errors', 'resources.heap', 'resources.stacks',
                 'resources.failed_alloc', 'hardware.audio_io', 'flush.stale_audio', 'delivery.firmware_drops'):
        assert c[gate] == 'CANDIDATE-FAIL'


# ---- verdict semantics (table-driven) ---------------------------------------------------------

def F(gate, **kw): return dict(gate=gate, **kw)
E1, E2 = dict(id='e1', t=1.0), dict(id='e2', t=5.0)
MISS = {'acoustic': dict(result='miss'), 'events': dict(result='pass')}
PASS = {'acoustic': dict(result='pass'), 'events': dict(result='pass')}

CASES = [
    # name, findings, scores, block_closed, expected action, hard, candidate, credited acoustic, counts invalid
    ('valid miss is never replaced', [], MISS, True, 'CONTINUE', [], [], 'miss', False),
    ('room noise invalidates pass and miss alike', [F('noise.cell_quiet_windows')], PASS, True, 'RETAKE', [], [], None, True),
    ('invalidity cannot hide a candidate fault', [F('noise.cell_quiet_windows'), F('hardware.aec_errors')], MISS, True,
     'STOP', [], ['hardware.aec_errors'], None, True),
    ('false turn survives unrelated room noise', [F('noise.cell_quiet_windows'), F('echo.false_turns', events=[E1])],
     PASS, True, 'STOP', ['echo.false_turns'], [], None, True),
    ('time-associated room witness erases the false turn', [F('noise.cell_quiet_windows', explains=['e1']),
     F('echo.false_turns', events=[E1])], PASS, True, 'RETAKE', [], [], None, True),
    ('one witness cannot erase an unrelated second false event', [F('noise.cell_quiet_windows', explains=['e1']),
     F('echo.false_turns', events=[E1, E2])], PASS, True, 'STOP', ['echo.false_turns'], [], None, True),
    ('false event without an id is never erased', [F('noise.cell_quiet_windows', explains=['e1']),
     F('barge.false_interruption')], PASS, True, 'STOP', ['barge.false_interruption'], [], None, True),
    ('false turn survives alignment loss', [F('echo.false_turns')], PASS, False, 'STOP', ['echo.false_turns'], [], None, True),
    ('false turn survives event-coverage loss', [F('echo.event_coverage'), F('echo.false_turns')], PASS, True,
     'STOP', ['echo.false_turns'], [], 'pass', True),
    ('event-only loss with an acoustic pass needs a new exposure', [F('echo.event_coverage')], PASS, True,
     'RETAKE', [], [], 'pass', True),
    ('event-only loss keeps a valid acoustic miss final', [F('echo.event_coverage')], MISS, True,
     'CONTINUE', [], [], 'miss', True),
    ('late associated barge response is an ordinary miss', [], {'acoustic': dict(result='miss', established=True),
     'events': dict(result='pass')}, True, 'CONTINUE', [], [], 'miss', False),
    ('numeric hard limit needs a valid measurement', [F('echo.unscorable'), F('echo.hard_floor')], MISS, True,
     'RETAKE', [], [], None, True),
    ('numeric hard limit on a valid cell', [F('echo.hard_floor')], MISS, True, 'STOP', ['echo.hard_floor'], [], 'miss', False),
    ('unsolicited interruption in a barge cell', [F('barge.false_interruption')], PASS, True, 'STOP',
     ['barge.false_interruption'], [], 'pass', False),
    ('capture cap cannot erase an established miss', [F('capture.cap')],
     {'acoustic': dict(result='miss', established=True)}, True, 'CONTINUE', [], [], 'miss', False),
    ('capture cap invalidates an unresolved endpoint', [F('capture.cap')], {'acoustic': dict(result='miss')}, True,
     'RETAKE', [], [], None, True),
    ('long TTS lead is diagnostic when complete', [F('tts.long_lead')], MISS, True, 'CONTINUE', [], [], 'miss', False),
    ('cancelled lull without established opportunity', [F('barge.cancelled_lull')], MISS, True, 'RETAKE', [], [], None, True),
    ('aggregate dispatches an AFE clip as a scored miss', [F('resources.part_audit', leaves=[F('clip.afe_output')])],
     MISS, True, 'CONTINUE', [], [], 'miss', False),
    ('aggregate dispatches a room clip as invalid', [F('resources.part_audit', leaves=[F('clip.mic_room')])],
     PASS, True, 'RETAKE', [], [], None, True),
    ('unresolved aggregate pauses', [F('resources.part_audit')], PASS, True, 'PAUSE-PART', [], [], 'pass', False),
    ('room-witnessed MIC clip', [F('clip.mic_room')], MISS, True, 'RETAKE', [], [], None, True),
    ('stimulus-caused MIC clip', [F('clip.mic_stimulus')], PASS, True, 'STOP', [], ['clip.mic_stimulus'], 'pass', False),
    ('unattributed MIC clip', [F('clip.mic_unattributed')], PASS, True, 'PAUSE-PART', [], [], 'pass', False),
    ('transport fault on echo is recorded only', [F('transport.downlink_echo')], MISS, True, 'CONTINUE', [], [], 'miss', False),
    ('unknown cause pauses', [F('unknown.unclassified')], PASS, True, 'PAUSE-PART', [], [], 'pass', False),
]


@pytest.mark.parametrize('name,findings,scores,closed,action,hard,candidate,acoustic,invalid', CASES, ids=[c[0] for c in CASES])
def test_classify_attempt(name, findings, scores, closed, action, hard, candidate, acoustic, invalid):
    out = v.classify_attempt(findings, scores, block_closed=closed)
    assert (out['action'], out['hard_events'], out['candidate_faults']) == (action, hard, candidate)
    assert out['credited'].get('acoustic') == acoustic and out['counts_invalid_attempt'] == invalid
    if action != 'RETAKE': assert not out['retake'] or out['action'] == 'RETAKE'


@pytest.mark.parametrize('offsets,ok', [([1020, 1030], True), ([1024, 1024], True), ([1200, 1200], False),
                                        ([1003, 1024], False), ([1004, 1044], False), ([1024], False)])
def test_witness_compatibility(offsets, ok):
    assert v.witnesses_ok(offsets) is ok


def test_unclosed_block_keeps_faults_and_counts_each_attempt_once():
    attempts = [v.classify_attempt([], PASS), v.classify_attempt([F('echo.false_turns')], PASS),
                v.classify_attempt([F('noise.cell_quiet_windows')], PASS)]
    out = v.close_block(attempts, [1200, 1200])
    assert out['action'] == 'STOP' and out['invalid_attempts'] == 3   # the false event forces STOP
    assert [a['credited']['acoustic'] for a in out['attempts']] == [None, None, None]
    assert out['attempts'][1]['hard_events'] == ['echo.false_turns']
    assert v.close_block(attempts, [1022, 1026])['invalid_attempts'] == 1


def test_whole_block_admission():
    assert v.block_fits([2900, 2970, 3020], [60, 45, 45], active_s=3240)
    assert not v.block_fits([2900, 2970, 3100], [60, 45, 45], active_s=3240)   # witness under 210 s


S, M, I, P, X = 'success', 'miss', 'ineligible', 'pending', 'missing'


@pytest.mark.parametrize('states,expected', [
    ([P] + [S]*8 + [M, M] + [S], 'OPEN'),           # Astra counterexample: rank 1 pending, misses at 10 and 11
    ([S] + [S]*8 + [M, M] + [S], 'PASS'),           # ... resolved: first ten hold one miss
    ([P, S, M, M] + [S]*8, 'FAIL'),                  # two misses inside the final ten under every completion
    ([S]*9 + [M] + [M, S], 'PASS'),
    ([S]*8 + [M, M, S, S], 'FAIL'),
    ([I, I, I] + [S]*9, 'INCONCLUSIVE'),             # fewer than ten eligible
    ([X] + [S]*11, 'INCONCLUSIVE'),
    ([X, M, M] + [S]*9, 'FAIL'),
])
def test_barge_group_membership(states, expected):
    assert v.barge_group(states) == expected


@pytest.mark.parametrize('results,expected', [
    (['pass']*18 + ['miss']*2, 'PASS'), (['miss']*3 + ['pending']*17, 'FAIL'),
    (['pass']*17 + ['miss']*2 + ['missing'], 'INCONCLUSIVE'), (['pass']*19 + ['pending'], 'OPEN')])
def test_echo_stratum(results, expected):
    assert v.echo_stratum(results) == expected


def counters(**kw):
    drops = {k: 0 for k in v.DROP_REASONS}
    drops.update(kw.pop('drops', {}))
    base = dict(pc_id='pc', generation=1, received=10, dispatched=10, deferred=0, admission_overflow=0, drops=drops)
    base.update(kw); return base


def without(d, *keys): return {k: x for k, x in d.items() if k not in keys}


@pytest.mark.parametrize('after,progress,expected', [
    (counters(received=30, dispatched=30, deferred=4), False, None),
    (counters(received=30, dispatched=30, admission_overflow=1), False, 'admission.control_loss'),
    (counters(received=30, dispatched=30, drops={'admission_budget': 1}), False, 'admission.control_loss'),
    (counters(received=30, dispatched=28), False, 'admission.drain_timeout'),
    ({k: x for k, x in counters().items() if k != 'admission_overflow'}, False, 'admission.counter_schema'),
    (counters(generation=2), False, 'admission.counter_schema'),
    (counters(received=5, dispatched=5), False, 'admission.counter_schema'),   # reset inside the interval
    (without(counters(received=30, dispatched=30), 'drops'), False, 'admission.counter_schema'),
    (counters(received=30, dispatched=30, drops={'message_queue': -1}), False, 'admission.counter_schema'),
    (counters(received=30, dispatched=30, drops={'message_queue': 1.0}), False, 'admission.counter_schema'),
    (counters(received='30', dispatched=30), False, 'admission.counter_schema'),
    (dict(counters(received=30, dispatched=30), drops={'message_queue': 0}), False, 'admission.counter_schema'),
    (counters(received=30, dispatched=31), False, 'admission.counter_schema'),
    (counters(received=30, dispatched=30, drops={'message_queue': 2}), False, 'admission.control_loss'),
])
def test_admission_interval(after, progress, expected):
    assert v.admission_interval(counters(), after) == expected


def test_admission_absent_identity_and_drops_never_pass():
    # Astra re-review probes: absent evidence is not zero, and a reset never nets against a new drop.
    bare = without(counters(), 'pc_id', 'generation', 'drops')
    assert v.admission_interval(bare, dict(bare, received=30, dispatched=30)) == 'admission.counter_schema'
    assert v.admission_interval(counters(), without(counters(received=30, dispatched=30), 'drops')) == \
        'admission.counter_schema'
    before = counters(drops={'message_queue': 1})
    after = counters(received=30, dispatched=30, drops={'message_queue': 0, 'admission_budget': 1})
    assert v.admission_interval(before, after) == 'admission.control_loss'
    # Sol's probe with an undeclared counter: the proven positive drop stays loss, else schema.
    sol_b = dict(counters(), drops={'admission_budget': 0, 'admission_queue_full': 10})
    sol_a = dict(counters(received=30, dispatched=30), drops={'admission_budget': 1, 'admission_queue_full': 9})
    assert v.admission_interval(sol_b, sol_a) == 'admission.control_loss'
    assert v.admission_interval(sol_b, dict(sol_a, drops={'admission_budget': 0, 'admission_queue_full': 9})) == \
        'admission.counter_schema'
    nulls = counters(pc_id=None, generation=None)
    assert v.admission_interval(nulls, dict(nulls, received=30, dispatched=30)) == 'admission.counter_schema'
    reset_only = counters(received=30, dispatched=30, drops={'message_queue': 0})
    assert v.admission_interval(before, reset_only) == 'admission.counter_schema'


def test_cell_result_one_exposure_and_cap():
    events_lost = v.classify_attempt([F('echo.event_coverage')], PASS)
    miss_lost = v.classify_attempt([F('echo.event_coverage')], MISS)
    clean_pass, clean_miss = v.classify_attempt([], PASS), v.classify_attempt([], MISS)
    assert events_lost['counts_invalid_attempt'] and events_lost['action'] == 'RETAKE'
    assert miss_lost['counts_invalid_attempt'] and miss_lost['action'] == 'CONTINUE'
    # A valid acoustic miss is final; a later exposure never replaces it.
    assert v.cell_result([miss_lost]) == 'miss'
    assert v.cell_result([clean_miss, clean_pass]) == 'miss'
    # No combining favourable endpoints across exposures: attempt 1's acoustic pass is not credited
    # with attempt 2's harm coverage; attempt 2 decides as a whole.
    noisy_second = v.classify_attempt([F('noise.cell_quiet_windows')], PASS)
    assert v.cell_result([events_lost, noisy_second]) == 'support-exhausted'
    assert v.cell_result([events_lost, clean_miss]) == 'miss'
    assert v.cell_result([events_lost, clean_pass]) == 'pass'
    assert v.cell_result([events_lost]) == 'retake'
    assert v.cell_result([dict(clean_pass, pending=True)]) == 'pending'
    assert v.cell_result([v.classify_attempt([], PASS, block_closed=None)]) == 'pending'
    stop = v.classify_attempt([F('echo.event_coverage'), F('echo.false_turns', events=[E1])], MISS)
    assert stop['action'] == 'STOP' and stop['hard_events'] == ['echo.false_turns']


def test_close_block_finalizes_validity_and_action():
    fault = v.classify_attempt([F('hardware.aec_errors')], PASS, block_closed=None)
    out = v.close_block([fault, v.classify_attempt([], PASS, block_closed=None)], [1200, 1200])
    assert out['action'] == 'STOP' and out['attempts'][0]['candidate_faults'] == ['hardware.aec_errors']
    false_event = v.classify_attempt([F('echo.false_turns', events=[E1])], PASS, block_closed=None)
    assert v.close_block([false_event], [1200, 1200])['attempts'][0]['action'] == 'STOP'
    # A numeric hard limit is provisional while the block is open and dropped when closure fails.
    floor = v.classify_attempt([F('echo.hard_floor')], MISS, block_closed=None)
    assert floor['hard_events'] == [] and floor['provisional_hard'] == ['echo.hard_floor'] and floor['action'] == 'CONTINUE'
    lost = v.close_block([floor], [1200, 1200])
    assert lost['action'] == 'PAUSE-PART' and lost['attempts'][0]['hard_events'] == []
    assert lost['attempts'][0]['credited']['acoustic'] is None and lost['attempts'][0]['invalid_endpoints'] == ['acoustic']
    kept = v.close_block([floor], [1024, 1030])
    assert kept['action'] == 'STOP' and kept['attempts'][0]['hard_events'] == ['echo.hard_floor']
    assert v.close_block([v.classify_attempt([], PASS, block_closed=None)], [1024, 1030])['action'] == 'CONTINUE'


@pytest.mark.parametrize('kw,expected', [
    (dict(overflow_delta=0, deferred_delta=3, sent_ids=[1, 2, 3], received_ids=[1, 2, 3]), 'covered'),
    (dict(overflow_delta=0, deferred_delta=0, sent_ids=[1, 2, 3], received_ids=[1, 2, 3]), 'coverage-not-achieved'),
    (dict(overflow_delta=0, deferred_delta=3, sent_ids=[1, 2, 3], received_ids=[1, 3, 2]), 'fail'),
    (dict(overflow_delta=0, deferred_delta=3, sent_ids=[1, 2, 3], received_ids=[1, 2, 2, 3]), 'fail'),
    (dict(overflow_delta=1, deferred_delta=3, sent_ids=[1, 2], received_ids=[1, 2]), 'fail'),
])
def test_burst_coverage(kw, expected):
    assert v.burst_coverage(**kw) == expected
