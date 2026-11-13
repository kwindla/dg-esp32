"""Hash-chained v9 attempt ledger and its pure verdict fold (spec sections 1, 6, 7, 9).

The ledger is append-only JSON lines; each row carries seq, prev (previous row sha256) and sha256
over its canonical JSON. The verdict is a pure function of the rows: State.apply folds one row
in O(1), fold(rows) replays a ledger. No I/O beyond the ledger file itself.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

import echoear_aec_v9_rules as rules

SCORED = ('echo', 'barge-firm-stop', 'barge-firm-sentence', 'barge-normal-sentence')
GENESIS = '0'*64


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(row):
    return hashlib.sha256(canonical({k: v for k, v in row.items() if k != 'sha256'}).encode()).hexdigest()


def verify(rows):
    prev = GENESIS
    for i, row in enumerate(rows):
        if row.get('seq') != i or row.get('prev') != prev or row.get('sha256') != digest(row):
            raise ValueError(f'Ledger hash chain broken at row {i}')
        prev = row['sha256']
    return prev


class Ledger:
    def __init__(self, path, *, create=False):
        self.path = Path(path)
        if create:
            if self.path.exists(): raise FileExistsError('Ledger exists')
            self.path.touch()
        self.rows = [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
        self.head = verify(self.rows)
        self.state = fold(self.rows)

    def append(self, row):
        row = dict(copy.deepcopy(row), seq=len(self.rows), prev=self.head)
        row['sha256'] = digest(row)
        line = canonical(row)+'\n'
        with self.path.open('a') as stream:
            stream.write(line); stream.flush(); os.fsync(stream.fileno())
        self.rows.append(row); self.head = row['sha256']
        self.state.apply(row)
        return row


class State:
    """Incremental fold. Attempts stay provisional until their block closes (or the part ends with the
    block unclosed); finalization re-classifies from the retained findings and scores. Candidate faults,
    hard events and invalid-attempt accounting are derived per attempt from its effective version, so an
    offline rescore replaces (never duplicates or drops) an attempt's contribution."""
    def __init__(self):
        self.campaign = None
        self.parts = []                 # part-start rows, with 'end' once closed
        self.attempts = {}              # attempt_id -> row (with 'final' classification once finalized)
        self.by_cell = {}               # logical_id -> [attempt_id] in acquisition order
        self.open_blocks = {}           # block key -> [attempt_id]
        self.blocks = {}                # block key -> close record
        self.streak = 0
        self.other_candidate = []       # part-end / burst candidate faults
        self.pending = set()
        self.amendments = []
        self.bursts = []
        self.closed = None              # campaign-close row
        self.drain = []
        self.floors = []
        self.reaudits = []

    @property
    def candidate(self):
        return [dict(gate=g, attempt_id=aid) for aid, a in self.attempts.items()
                for g in self._effective(a)['candidate_faults']]+list(self.other_candidate)

    @property
    def hard(self):
        return [dict(gate=g, attempt_id=aid) for aid, a in self.attempts.items() for g in self._effective(a)['hard_events']]

    @property
    def invalid(self):
        return sum(1 for a in self.attempts.values() if a['kind'] in SCORED and a['final'] is not None
                   and a['final']['counts_invalid_attempt'])

    @staticmethod
    def _effective(a):
        c = a['final'] or a['classification']
        extra = [f['gate'] for f in a.get('part_findings', ()) if rules.BY_ID[f['gate']]['cls'] == 'CANDIDATE-FAIL']
        return dict(c, candidate_faults=list(dict.fromkeys(c['candidate_faults']+extra)))

    # -- folding ------------------------------------------------------------------------------
    def apply(self, row):
        t = row['type']
        if t == 'campaign':
            self.campaign = row
        elif t == 'part-start':
            self.parts.append(dict(row, end=None)); self.streak = 0
        elif t == 'attempt':
            self._attempt(row)
        elif t == 'block-close':
            self._close(row['block_key'], row['closed'], row)
        elif t == 'rescore':
            a = self.attempts[row['attempt_id']]
            a.update(findings=row['findings'], scores=row['scores'], pending=row.get('pending', False))
            if a['pending']: self.pending.add(row['attempt_id'])
            else: self.pending.discard(row['attempt_id'])
            a['classification'] = rules.classify_attempt(a['findings'], a['scores'], block_closed=None)
            if a['final'] is not None: a['final'] = rules.classify_attempt(a['findings'], a['scores'],
                                                                           block_closed=a['final']['block_closed'])
        elif t == 'amendment':
            self.amendments.append(row)
        elif t == 'burst':
            self.bursts.append(row)
            for f in row.get('findings', ()):
                if rules.BY_ID[f['gate']]['cls'] == 'CANDIDATE-FAIL': self.other_candidate.append(dict(gate=f['gate'], burst=row['burst']))
        elif t == 'drain':
            self.drain.append(row)
        elif t == 'burst-reaudit':
            self.reaudits.append(row)            # its CANDIDATE findings are carried by the part-end part_findings
        elif t == 'floors':
            self.floors.append(row)
        elif t == 'part-end':
            for key in [k for k in self.open_blocks if k.startswith(f"p{row['part']}/")]:
                self._close(key, False, row)
            self.parts[-1]['end'] = row
            for f in row.get('part_findings', ()):
                self.other_candidate.append(dict(gate=f['gate'], part=row['part']))
        elif t == 'campaign-close':
            self.closed = row
        else:
            raise ValueError('Unknown ledger row type '+t)

    def _attempt(self, row):
        a = copy.deepcopy(row)
        a['classification'] = rules.classify_attempt(a['findings'], a['scores'], block_closed=None)
        a['final'] = None
        self.attempts[a['attempt_id']] = a
        if a.get('pending'): self.pending.add(a['attempt_id'])
        if a['kind'] in SCORED:
            self.by_cell.setdefault(a['logical_id'], []).append(a['attempt_id'])
            self.open_blocks.setdefault(a['block_key'], []).append(a['attempt_id'])
            self.streak = self.streak+1 if a['classification']['counts_invalid_attempt'] else 0

    def _close(self, key, closed, row):
        for aid in self.open_blocks.pop(key, []):
            a = self.attempts[aid]
            a['final'] = rules.classify_attempt(a['findings'], a['scores'], block_closed=closed)
        self.blocks[key] = dict(closed=closed, offsets=row.get('offsets'), seq=row['seq'])

    # -- derived ------------------------------------------------------------------------------
    def attempts_of(self, logical_id):
        return [self.attempts[i] for i in self.by_cell.get(logical_id, [])]

    def cell(self, logical_id, *, final=False, max_attempts=2):
        rows = self.attempts_of(logical_id)
        if not rows: return 'missing' if final else 'unattempted'
        if any(r['pending'] for r in rows): return 'pending'
        if any(r['final'] is None for r in rows): return 'pending'
        return rules.cell_result([dict(r['final'], pending=r['pending']) for r in rows], max_attempts=max_attempts)


def fold(rows):
    s = State()
    for row in rows: s.apply(row)
    return s


def verdict(state, plan, *, final=None):
    """Outcome with separate completeness, acoustic and integration fields. final: no further part
    can run (defaults to a campaign-close row being present)."""
    final = state.closed is not None if final is None else final
    cells = plan['v9']['cells']
    max_attempts = plan['v9']['caps']['attempts_per_logical_cell']
    def mapped(lid):
        r = state.cell(lid, final=final, max_attempts=max_attempts)
        if r in ('retake', 'unattempted'): return 'missing' if final else 'pending'
        if r == 'support-exhausted': return 'missing'
        return r
    strata, missing = {}, []
    for v in (-6, 0):
        ids = [c['logical_id'] for c in cells if c['kind'] == 'echo' and c['volume_db'] == v]
        results = [mapped(i) for i in ids]
        missing += [i for i, r in zip(ids, results) if r == 'missing']
        strata[str(v)] = dict(result=rules.echo_stratum(results), misses=results.count('miss'),
                              passes=results.count('pass'), pending=results.count('pending'),
                              missing=results.count('missing'),
                              failed_texts=sorted({c['text_id'] for c, r in zip([c for c in cells if c['logical_id'] in ids], results) if r == 'miss'}))
    groups = {}
    for g in ('barge-firm-stop', 'barge-firm-sentence', 'barge-normal-sentence'):
        ranked = sorted([c for c in cells if c['kind'] == g], key=lambda c: c['rank'])
        states = []
        for c in ranked:
            r = mapped(c['logical_id'])
            states.append({'pass': 'success', 'miss': 'miss', 'ineligible': 'ineligible'}.get(r, r))
        missing += [c['logical_id'] for c, s in zip(ranked, states) if s == 'missing']
        groups[g] = dict(result=rules.barge_group(states), states=states)
    caps = dict(invalid_attempts=state.invalid, limit=plan['v9']['caps']['invalid_attempts_per_campaign'])
    caps['exhausted'] = state.invalid > caps['limit']
    results = [s['result'] for s in strata.values()] + [g['result'] for g in groups.values()]
    if state.hard or ('FAIL' in results and not state.pending): acoustic = 'FAIL'   # pending withholds score decisions
    elif all(r == 'PASS' for r in results) and not state.pending: acoustic = 'PASS'
    elif final or caps['exhausted']: acoustic = 'INCONCLUSIVE'
    else: acoustic = 'OPEN'
    restores = [p['end'].get('restore', {}) for p in state.parts if p['end']]
    restore_clean = bool(restores) and all(r.get('clean') for r in restores) and all(p['end'] for p in state.parts)
    caps.update(budgets(state, plan))
    audits_clean = all((p['end'] or {}).get('audit', {}).get('clean') is True for p in state.parts)
    if state.candidate: integration = 'FAIL'
    elif restore_clean and audits_clean and caps['time_ok'] and caps['parts_ok']: integration = 'PASS'
    elif state.parts and all(p['end'] for p in state.parts): integration = 'INCOMPLETE'
    else: integration = 'OPEN'
    complete = not missing and not state.pending and all(r != 'OPEN' for r in results)
    if state.candidate or acoustic == 'FAIL': outcome = 'FAIL'
    elif caps['exhausted']: outcome = 'INCONCLUSIVE'          # an exhausted invalid cap blocks PASS (Sol 1)
    elif acoustic == 'PASS' and complete and integration == 'PASS': outcome = 'PASS'
    elif final: outcome = 'INCONCLUSIVE'
    else: outcome = 'OPEN'
    # A computed verdict closes the plan: a terminal campaign-close is immutable (no later fold or repair
    # turns INCONCLUSIVE or FAIL into anything else); evidence found later is still reported in the fields.
    closed = (state.closed or {}).get('reason')
    if closed in ('FAIL', 'INCONCLUSIVE'):
        outcome = closed                       # negative closures are immutable
    elif closed == 'PASS' and outcome != 'PASS':
        # The historical PASS record stays, but the current outcome follows the current evidence: a repair that
        # removes support or exhausts the cap withdraws it to INCONCLUSIVE, a candidate fault to FAIL. Acquisition
        # never reopens (the closure row stays and resume refuses).
        outcome = 'FAIL' if outcome == 'FAIL' else 'INCONCLUSIVE'
    return dict(outcome=outcome, acoustic=acoustic, integration=integration,
                audits_clean=audits_clean, completeness=dict(complete=complete, missing_cells=sorted(set(missing)), pending_attempts=sorted(state.pending)),
                echo=strata, barge=groups, caps=caps, candidate_faults=state.candidate, hard_events=state.hard,
                parts=[dict(part=p['part'], role=p['role'], end=(p['end'] or {}).get('reason')) for p in state.parts],
                closed=(state.closed or {}).get('reason'), credit=(state.campaign or {}).get('mode') == 'acceptance')


def budgets(state, plan):
    """Part-count and wall-time caps as verdict prerequisites (section 7): every entered bench part counts
    (no fourth part; 2 for the smoke); each part's wall time within its own budget and no recorded overrun;
    the sum within the 150 min (smoke 30 min) ceiling. A restore still finishes after an overrun; the run
    just cannot pass."""
    smoke = (state.campaign or {}).get('mode') == 'smoke'
    v9 = plan['v9']
    ceiling = v9['smoke']['ceiling_s'] if smoke else v9['parts']['ceiling_s']
    limit = 2 if smoke else v9['parts']['planned']+v9['parts']['recovery']
    ends = [p for p in state.parts if p['end']]
    over = [p['part'] for p in ends if p['end'].get('over_budget') or
            (p['end'].get('wall_s') or 0) > p.get('budget', {}).get('total_s', float('inf'))]
    entered = sum(1 for p in ends if p['end'].get('entered', True))
    wall = sum(p['end'].get('wall_s') or 0 for p in ends)
    return dict(wall_s=wall, ceiling_s=ceiling, over_budget_parts=over, entered_parts=entered, part_limit=limit,
                time_ok=not over and wall <= ceiling, parts_ok=entered <= limit)
