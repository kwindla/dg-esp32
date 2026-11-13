"""Pure v9 verdict rules over registry findings (docs/echoear-aec-acceptance-v9.md sections 6-7).

No I/O, no hardware. The v9 runner and scorer call these; the registry tests exercise them
table-driven. A finding is dict(gate=<registry id>, leaves=[findings], explains=[event ids] for a room
witness, events=[dict(id, t)] for a false-event finding).
"""
import itertools

from echoear_aec_gate_registry import BY_ID, ENDPOINTS

WITNESS_DELAY_SAMPLES = 1024
WITNESS_TOLERANCE_SAMPLES = 20
WITNESS_SPREAD_SAMPLES = 20


def expand(findings):
    """Aggregates dispatch to their leaves; an aggregate without leaves stays (PAUSE-PART)."""
    out = []
    for f in findings:
        gate = BY_ID[f['gate']]
        if gate['aggregate'] and f.get('leaves'):
            out.extend(expand(f['leaves']))
        else:
            out.append(f)
    return out


NUMERIC_HARD = ('echo.hard_floor',)


def _unexplained(f, explained):
    """A false-event finding survives unless EVERY event it carries is explained by a
    time-associated room witness (Sol re-review 2). Events without ids cannot be associated."""
    ids = [e['id'] for e in f.get('events', ())]
    return not ids or any(i not in explained for i in ids)


def classify_attempt(findings, scores=None, *, block_closed=True):
    """Endpoint validity first, then sticky faults and hard events, then the control action.

    findings: dict(gate=<id>, leaves=[...]); a room witness carries explains=[event ids] (the
    false events it is time-associated with); a false-event finding carries events=[dict(id, t)].
    scores: {endpoint: dict(result='pass'|'miss'|'ineligible', established=bool)} for this exposure.
    block_closed: True (closed), False (closure failed) or None (block still open: acoustic credit
    and numeric hard limits are provisional until the closing witness).
    The result keeps its inputs so close_block can finalize validity and action together."""
    scores = scores or {}
    leaves = expand(findings)
    gates = [(BY_ID[f['gate']], f) for f in leaves]
    explained = {i for g, f in gates if g['room_witness'] for i in f.get('explains', ())}
    invalid = set()
    for g, f in gates:
        if g['cls'] != 'INVALID-CELL':
            continue
        for ep in g['invalidates']:
            if ep == 'events' and g['room_witness'] and not f.get('explains'):
                continue                   # room witnesses invalidate events only when time-associated
            if g['outcome_dependent'] and scores.get(ep, {}).get('result') == 'miss' and scores[ep].get('established'):
                continue
            invalid.add(ep)
    if block_closed is False:
        invalid.add('acoustic')
    candidate = [g['id'] for g, _ in gates if g['cls'] == 'CANDIDATE-FAIL']
    hard, provisional = [], []
    for g, f in gates:
        if not g['hard']:
            continue
        if g['id'] in NUMERIC_HARD:        # numeric hard limits need a final valid measurement
            if 'acoustic' in invalid:
                continue
            (provisional if block_closed is None else hard).append(g['id'])
        elif _unexplained(f, explained):   # positive false-event evidence is sticky
            hard.append(g['id'])
    pause = [g['id'] for g, _ in gates if g['cls'] == 'PAUSE-PART']
    credited = {ep: (None if ep in invalid else s['result']) for ep, s in scores.items()}
    # Any applicable endpoint without support counts once toward the common invalid cap.
    counts_invalid = bool(invalid)
    # Credit unit is one exposure: acoustic and harm evidence must come from the same attempt.
    # A valid acoustic miss is final (never retaken, needs no harm coverage to be a failure);
    # any other missing support needs a whole new exposure.
    valid_miss = 'acoustic' not in invalid and scores.get('acoustic', {}).get('result') == 'miss'
    needs_exposure = counts_invalid and not valid_miss
    if candidate or hard:
        action = 'STOP'
    elif pause:
        action = 'PAUSE-PART'
    elif needs_exposure:
        action = 'RETAKE'
    else:
        action = 'CONTINUE'
    return dict(credited=credited, candidate_faults=candidate, hard_events=hard, provisional_hard=provisional,
                pause=pause, invalid_endpoints=sorted(invalid), counts_invalid_attempt=counts_invalid,
                retake=action == 'RETAKE', action=action, block_closed=block_closed,
                inputs=dict(findings=findings, scores=scores))


def cell_result(attempts, *, max_attempts=2):
    """Credit of one logical cell over its finalized attempts, in acquisition order.

    Each attempt is one exposure; endpoints are never combined across exposures. The first
    attempt with a valid acoustic miss, or with valid acoustic and harm results, decides the cell
    and is never replaced. A pending (scorer-exception) attempt withholds the result.
    Returns 'pass'|'miss'|'ineligible'|'pending'|'retake'|'support-exhausted'."""
    for a in attempts:
        if a.get('pending') or a.get('block_closed') is None:
            return 'pending'
        c = a['credited']
        if c.get('acoustic') == 'miss':
            return 'miss'
        if c.get('acoustic') is not None and ('events' not in c or c['events'] is not None):
            return c['acoustic']
    return 'support-exhausted' if len(attempts) >= max_attempts else 'retake'


def witnesses_ok(offsets):
    """Two witnesses, each individually compatible with the fixed delay, and a small mutual spread."""
    return (len(offsets) == 2 and
            all(abs(o - WITNESS_DELAY_SAMPLES) <= WITNESS_TOLERANCE_SAMPLES for o in offsets) and
            max(offsets) - min(offsets) <= WITNESS_SPREAD_SAMPLES)


SEVERITY = {a: i for i, a in enumerate(('STOP', 'PAUSE-PART', 'RETAKE', 'CONTINUE'))}


def close_block(attempts, offsets):
    """Finalize a block: every attempt is re-classified from its retained inputs with the final
    closure, so validity, numeric hard limits and action stay consistent (Sol re-review 4). An
    unclosed block loses acoustic credit and counts each attempt once; candidate faults and
    positive false events stay and force STOP. The block action is the most severe of the
    attempts' actions and PAUSE-PART for a failed closure."""
    closed = witnesses_ok(offsets)
    out = [classify_attempt(a['inputs']['findings'], a['inputs']['scores'], block_closed=closed) for a in attempts]
    actions = [a['action'] for a in out] + ([] if closed else ['PAUSE-PART'])
    action = min(actions, key=SEVERITY.get) if actions else 'CONTINUE'
    return dict(closed=closed, attempts=out, action=action,
                invalid_attempts=sum(a['counts_invalid_attempt'] for a in out))


def block_fits(starts_s, durations_s, *, active_s, min_start_remaining_s=210):
    """Whole-block admission: every attempt incl. the closing witness starts with >= 210 s left
    and finishes inside the active budget."""
    return all(active_s - s >= min_start_remaining_s and s + d <= active_s for s, d in zip(starts_s, durations_s))


def echo_stratum(results):
    """results: 20 entries 'pass'|'miss'|'pending'|'missing' (closed-block credits only)."""
    misses = results.count('miss')
    if misses >= 3:
        return 'FAIL'
    if 'pending' in results:
        return 'OPEN'
    if 'missing' in results:
        return 'INCONCLUSIVE'
    return 'PASS'


def _group_final(states):
    eligible = [s for s in states if s in ('success', 'miss')]
    if len(eligible) < 10:
        return 'INCONCLUSIVE'
    return 'FAIL' if eligible[:10].count('miss') >= 2 else 'PASS'


def barge_group(states):
    """states by frozen logical rank (12): 'success'|'miss'|'ineligible'|'pending'|'missing'.
    Membership = first ten eligible by rank. FAIL early only if every legal completion fails;
    never PASS while anything is pending; missing support -> INCONCLUSIVE unless FAIL is certain."""
    open_ranks = [i for i, s in enumerate(states) if s in ('pending', 'missing')]
    outcomes = set()
    for fill in itertools.product(('success', 'miss', 'ineligible'), repeat=len(open_ranks)):
        trial = list(states)
        for i, v in zip(open_ranks, fill): trial[i] = v
        outcomes.add(_group_final(trial))
    if outcomes == {'FAIL'}:
        return 'FAIL'
    if 'pending' in states:
        return 'OPEN'
    if 'missing' in states:
        return 'INCONCLUSIVE'
    return _group_final(states)


DROP_REASONS = ('invalid_data', 'message_queue', 'encoded_queue', 'message_parse', 'event_queue', 'invalid_sdp',
                'offer_queue', 'worker_exception', 'outbound_queue', 'uplink_gap', 'send_cutoff', 'admission_budget',
                'admission_shape', 'admission_heap', 'admission_overflow')   # aec-v87 PERF drop_reasons schema
ADMISSION_KEYS = ('received', 'dispatched', 'deferred', 'admission_overflow')


def _counter(v):
    return type(v) is int and v >= 0


def admission_interval(before, after, *, drop_schema=DROP_REASONS):
    """Decision 71 live check over one peer epoch; `after` is the observation at the end of the
    frozen drain allowance. Returns the registry gate that applies, or None.

    Absent evidence is never zero (Astra re-review 2): both boundaries need a non-empty pc_id, an
    integer generation, the four admission counters and the complete declared drop schema, each a
    non-negative integer. A positive delta in any drop/overflow field is loss even if another field
    reset; otherwise any reset or malformed interval is counter_schema."""
    def identity(p):
        return isinstance(p.get('pc_id'), str) and bool(p['pc_id']) and type(p.get('generation')) is int
    def valid(p):
        drops = p.get('drops')
        return (identity(p) and all(_counter(p.get(k)) for k in ADMISSION_KEYS) and isinstance(drops, dict) and
                set(drops) == set(drop_schema) and all(_counter(v) for v in drops.values()))
    if not (identity(before) and identity(after)) or \
            (before['pc_id'], before['generation']) != (after['pc_id'], after['generation']):
        return 'admission.counter_schema'
    # Independently proven loss: a positive delta of any one counter present as a valid integer at
    # both boundaries, never netted against another counter's reset.
    pairs = [(before.get('admission_overflow'), after.get('admission_overflow'))]
    bd, ad = before.get('drops'), after.get('drops')
    if isinstance(bd, dict) and isinstance(ad, dict):
        pairs += [(bd[k], ad[k]) for k in set(bd) & set(ad) & set(drop_schema)]
    if any(_counter(a) and _counter(b) and b > a for a, b in pairs):
        return 'admission.control_loss'
    if not (valid(before) and valid(after)):
        return 'admission.counter_schema'
    if any(after[k] < before[k] for k in ADMISSION_KEYS) or any(after['drops'][k] < before['drops'][k] for k in drop_schema) \
            or after['dispatched'] > after['received']:
        return 'admission.counter_schema'
    if after['received'] != after['dispatched']:
        # v9.10: the aec-v87 firmware has no device-side witness of permanent non-dispatch (its PERF exposes only
        # received/dispatched/deferred/backpressure counters), so an unmatched count is drain_timeout PAUSE-PART.
        return 'admission.drain_timeout'
    return None


def burst_coverage(*, overflow_delta, deferred_delta, sent_ids, received_ids):
    """Decision 71 smoke burst: ordered exactly-once application receipt and positive deferral."""
    if overflow_delta > 0 or received_ids != sent_ids:
        return 'fail'
    if deferred_delta <= 0:
        return 'coverage-not-achieved'
    return 'covered'
