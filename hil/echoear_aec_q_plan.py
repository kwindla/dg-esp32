"""AEC-only qualification plan (decisions 88-89, spec rev 2): the frozen v9 cells (40 echo, 3 x 12 barge) plus one
unscored single-talk baseline cell per distinct barge fixture (R5), in one frozen randomized order with the baselines
spread through it. Built on the v9 plan generator (same candidate, ELF, sessions, texts, fixtures); only the q
section is new. Also: the unscored bench calibration capture (all 21 fixtures, single and double talk) for R8."""
import copy
import random
from pathlib import Path

import echoear_aec_v9_plan as v9plan

SEED = 'aec-q-2026-10-22'
DESTINATION = v9plan.EVIDENCE/'freeze-q1-aec-v87'
OUTPUT = v9plan.EVIDENCE/'q1-aec-v87-01'
CALIBRATION_OUTPUT = v9plan.EVIDENCE/'q1-nearend-calibration-01'
BARGE_KINDS = ('barge-firm-stop', 'barge-firm-sentence', 'barge-normal-sentence')
MAX_WITNESSES = 80
JABRA_DELAY_SAMPLES = -178      # median request -> raw onset offset beyond the fixture lead, campaign-10 (R8 replay)

Q = dict(
    rule='aec-only-qualification-q1 rev2 (decision 88): a check decides the verdict only if it measures AEC or '
         'protects an AEC measurement\'s validity; everything else is report-only',
    echo=dict(volumes=[-6, 0], cells_per_volume=20, max_misses=2,
              measure='frozen echo_measure acoustic clauses only (whole >= 20 dB, first-500 ms >= 10 dB when strong, '
                      'post-guard <= -50 dBFS); guard timing and gate-unknown are not misses (R1)'),
    nearend=dict(endpoint='local AFE output (R2); received uplink and transport gaps report-only',
                 measure='content bar, decision 91 (echoear_aec_q_near.measure), pluggable',
                 status='decision 92: b-whole T_c = 0.50 fixed; the level floor PROVISIONAL (reported, not gated); the '
                 'local-content (hole) rule UNSET, to be designed on calibration hold A and validated on hold B; '
                 'delivery thresholds PROVISIONAL (degenerate-case only), to be frozen from hold A',
                 t_c=.50, local_rule=None, level_floor_db=None, level_floor_provisional_db=-8.0,
                 level_floor_basis='calibration-only recipe (campaign-10 minimum -2.0 dB - 6 dB); provisional, not gated',
                 delivery=dict(corr_min=.05, ambiguity_min=1.0, level_min_db=-20.0, level_max_db=10.0, offset_max_samples=2400, status='PROVISIONAL',
                               basis='rejects an absent / mis-routed stimulus (zero raw: corr 0, level -120 dB) only; '
                                     'per-fixture values from hold A (with the echo-only null cells)',
                               ambiguity_note='ambiguity_min = 1.0 is VACUOUS: the statistic is max / second max, >= 1 by '
                                              'construction; hold A sets a value > 1 from the null second-peak '
                                              'distribution or the clause is dropped in the manifest'),
                 overlap='verified reference-active samples (>= 50 % of a frame); padding reported',
                 eligibility={k: dict(name='coverage75', params=dict(fraction=.75, min_frames=10, bridge_frames=10,
                                                                     exposure='fixture' if k == 'barge-firm-stop' else 'far-end',
                                                                     onset_within_frames=0 if k == 'barge-firm-stop' else 5))
                              for k in BARGE_KINDS},
                 eligibility_rule='decisions 93/94, declared before hold A: one exposure per cell for the coverage '
                                  'denominator, the verified minimum, the content mask and the uncovered report. Stop: '
                                  'the whole fixture (every source-active frame, whenever the bot yields); eligible iff '
                                  'verified >= max(10, ceil(0.75 x 15)) = 12. Sentences: the far end\'s measured active '
                                  'extent (pauses <= 200 ms bridged; a later run after a longer pause is excluded from '
                                  'all four; the post-yield remainder is single talk, witnessed by the baselines; no '
                                  'sustained far-end playback); eligible iff verified >= max(10, ceil(0.75 x exposed '
                                  'source-active frames)) and the first verified source-active frame within the first 5 '
                                  '(uncovered prefix <= 4 frames). Otherwise ineligible by acoustic evidence. Reported: '
                                  'uncovered frames by position, the onset 5-bit mask / leading run / count, in-dip '
                                  'onset AFE correlation (first 200 ms).',
                 release_window='report only: AFE content correlation over the 300 ms of source-active frames after '
                                'the far end\'s measured end',
                 timing=dict(estimator='per-hold-median-v1', prior_samples=JABRA_DELAY_SAMPLES, min_baselines=5,
                             rule='raw-only, declared before hold A: each valid single-talk capture of the hold gives '
                                  'raw/dry onset minus the playback-timing prediction at the prior; once >= 5 exist, '
                                  'every later near-end alignment of the hold is centred at prior + their median, '
                                  'before that at the prior; the estimate used is recorded per evaluation '
                                  '(echoear_aec_q_near.hold_delay)'),
                 report_only=['per-100 ms deficits (R4(a) withdrawn)', '1-4 kHz band deficit', 'double-talk level loss'],
                 jabra_delay_samples=JABRA_DELAY_SAMPLES,   # record of the original prior; the prior in use is timing.prior_samples
                 align_half_samples=2400,
                 group_rule='first 10 eligible of 12 ranked cells, at most 1 miss (rules.barge_group)'),
    validity=['noise.cell_quiet_windows', 'quiet.board_silent', 'timing.single_gap', 'timing.capture_bracket',
              'capture.gap_host_export', 'capture.export', 'timing.stimulus_lateness', 'timing.host_clock',
              'clip.mic_room', 'clip.mic_unattributed', 'clip.reference', 'harness.jabra_duration', 'alignment.near_witness',
              'echo.unscorable', 'q.support_unknown'],
    faults=['device.reboot_crash', 'hardware.aec_errors'],
    retake=dict(max_acquisitions_per_cell=3, rule='3 total acquisitions per logical cell; only when the outcome is '
                'unknown (a validity guard failed or the acoustic record is unscorable); a completely captured valid miss '
                'stays a miss whatever a later acknowledgement / bot / link step does; exhaustion -> missing'),
    loopback=dict(rule='R9: an STT transcript in an echo cell, or before the near-end stimulus of a barge cell, '
                  'sharing >= 3 consecutive words with the bot reply text (or wholly contained in it, >= 2 words) -> '
                  'verdict INVESTIGATE', min_run_words=3),
    witness=dict(every=8, acquisitions=3, rule='v9.8 three-way estimator; an unresolved witness invalidates the scored '
                 'cells since the last valid witness'),
    cap_s=9000, restore_reserve_s=360, max_holds=4, warmup='one unscored echo reply after every connect',
    cap_rule='acquisition budget with protected cleanup (decision 104 / Astra qualify review): no cell or hold starts '
             'once the remaining budget is below the cell window + the 360 s restore reserve; restoration is never '
             'killed at the deadline, so the process lifetime can exceed cap_s by the cleanup; cells not reached are '
             'missing -> INCONCLUSIVE, never FAIL',
)


def eligibility_spec(e, kind):
    """The eligibility rule of a barge class: a single serialized rule, or a {kind: rule} mapping."""
    return e if 'name' in e else e[kind]


def barge_fixtures(cells):
    """Distinct barge fixtures in plan order: (fixture path, class, first cell using it)."""
    out = {}
    for c in cells:
        if c['kind'] in BARGE_KINDS:
            out.setdefault(c['cell']['fixture'], (c['kind'], c))
    return out


def baseline_cells(cells, witness):
    """R5: one single-talk cell per distinct barge fixture (the near-only witness cell shape, the fixture swapped)."""
    out = []
    for i, (fixture, (kind, src)) in enumerate(barge_fixtures(cells).items(), 1):
        c = copy.deepcopy(witness)
        lid = f'q-st-{i:02d}'
        cell = src['cell']
        c.update(id=lid, fixture=fixture, fixture_id=cell.get('fixture_id'), fixture_sha256=cell.get('fixture_sha256'),
                 voice_class=cell.get('voice_class', 'firm'), report_only=True, real_bot_reply=False)
        for k in ('expected_text',):
            if k in cell: c[k] = cell[k]
        out.append(dict(logical_id=lid, kind='single-talk', fixture=fixture, barge_class=kind, cell=c))
    return out


def frozen_order(scored, baselines, seed=SEED):
    """Seeded shuffle of the scored cells (volumes and kinds interleaved), the baseline cells spread evenly."""
    order = [c['logical_id'] for c in scored]
    random.Random(seed).shuffle(order)
    step = len(order)/len(baselines)
    for i, b in enumerate(baselines):
        order.insert(int(round((i+.5)*step))+i, b['logical_id'])
    return order


def plan_from():
    p = v9plan.plan_from()
    cells = p['v9']['cells']
    base = baseline_cells(cells, p['v9']['witness'])
    q = copy.deepcopy(Q)
    q.update(cells=cells+base, order=frozen_order(cells, base), seed=SEED, destination=str(DESTINATION), output=str(OUTPUT))
    p['q'] = q
    p.update(schema='aec-only-qualification-q1-rev2', declaration_id='step-9-aec-q1-aec-v87', status='PREPARED; reviews required')
    return p


# ---- unscored bench calibration capture, two holds (decision 92 C) -----------------------------------------

def qualification_estimate(p):
    """Declared wall-time estimate of the qualification run (decision 103 dry plan): one hold, warm-up, every cell
    of the frozen order once, a near-only witness every 8 scored acquisitions plus the opening one, setup and restore.
    No retake or second-hold allowance is included; the budget is plan q.cap_s (an acquisition budget with protected
    cleanup, q.cap_rule)."""
    q = p['q']
    kinds = {}
    for c in q['cells']: kinds[c['kind']] = kinds.get(c['kind'], 0)+1
    per = dict(echo=WINDOW_S['echo'], single=WINDOW_S['single'], double=WINDOW_S['double'])
    n = len(q['order'])
    witnesses = 1+(n+q['witness']['every']-1)//q['witness']['every']
    barge = sum(v for k, v in kinds.items() if k.startswith('barge-'))
    cells_s = kinds.get('echo', 0)*per['echo']+kinds.get('single-talk', 0)*per['single']+barge*per['double']
    est = SETUP_S+WINDOW_S['warmup']+cells_s+witnesses*WINDOW_S['witness']+RESTORE_S
    return dict(cells=n, by_kind=kinds, witnesses=witnesses, estimate_s=est, cap_s=q['cap_s'],
                headroom_s=q['cap_s']-est, setup_s=SETUP_S, restore_s=RESTORE_S, window_s=dict(WINDOW_S),
                note='no retake / extra-hold allowance; a second hold adds setup + warm-up + witness')

WINDOW_S = dict(single=43, double=65, echo=59, witness=43, warmup=45)   # stress windows incl. scoring (v9 + 2 s)
SETUP_S, RESTORE_S = 330, 90
INTERRUPT_S = .9               # retained near onset -> accepted interruption, median (bot speech ends about here)
HOLDS = dict(A=dict(role='development: metric, masks, delivery guards, local-content rule and thresholds are '
                         'designed and frozen from A only', seed='aec-q-cal-A-2026-10-22', order='adjacent'),
             B=dict(role='prospective validation of the rules frozen on A; if B causes a redesign, B becomes '
                         'development evidence and a new hold is needed', seed='aec-q-cal-B-2026-10-22',
                    order='spread: the qualification order (seeded shuffle of the takes and nulls, the single-talk '
                          'baselines spread evenly), so B sees the run\'s baseline-to-cell drift'))


SUPPLEMENT = dict(single_of='/sentence-10-normal.wav', take2_of='/sentence-02-48k.wav',
                  role='supplementary DEVELOPMENT capture for hold A (decision 97 + reviews, not launched): the '
                       'sentence-10-normal single-talk baseline (its two retained hold-A takes become evaluable against it, '
                       'cross-record, with hold A\'s own delivery and timing) and the sentence-02-48k take 2 with a fresh '
                       'sentence-02-48k baseline; unscored; preferred before freeze by both reviewers; incorporated only '
                       'through the derived development catalog (echoear_aec_q_design catalog), never by rewriting hold A')


def _duration_s(fixture):
    import echoear_aec_q_near as near
    src, span = near.read_source(fixture)
    return (span[1]-span[0])/16000 if span else None


def take_two(first, cells):
    """The second calibration take of a fixture: its own logical id '<id>-t2' and cell id '<cell id>-t2' (own capture
    folder, own retake budget), the same stimulus at the next stop offset of the class's declared offset cycle (a
    different far-end position under the near end), linked to the original fixture, rank and offset."""
    cycle = sorted({c['cell']['stop_offset_s'] for c in cells if c['kind'] == first['kind'] and c['cell'].get('stop_offset_s') is not None})
    o1 = first['cell'].get('stop_offset_s')
    o2 = cycle[(cycle.index(o1)+1) % len(cycle)] if o1 in cycle and len(cycle) > 1 else o1
    t2 = copy.deepcopy(first)
    t2.update(logical_id=first['logical_id']+'-t2', take=2,
              take_of=dict(logical_id=first['logical_id'], fixture=first['cell']['fixture'], rank=first['rank'], stop_offset_s=o1))
    t2['cell'].update(id=first['cell']['id']+'-t2', stop_offset_s=o2, report_only=True)
    return t2


def calibration_takes(cells):
    """Per fixture: the single-talk cell and two double-talk takes, each a distinct planned exposure. Take 1 is the
    lowest rank using the fixture; take 2 a derived cell (take_two) at a different stop offset. Each take reports the
    expected verified double talk (bot speech ends about INTERRUPT_S after the near onset): onset content is always
    exercised; internal / ending content only when the utterance is shorter than that (sentence exposure design: HOLD,
    with the user)."""
    out = []
    for i, (fixture, (kind, first)) in enumerate(barge_fixtures(cells).items(), 1):
        second = take_two(first, cells)
        dur = _duration_s(fixture)
        exp = min(dur, INTERRUPT_S) if dur else None
        out.append(dict(fixture=fixture, kind=kind, single=f'q-st-{i:02d}', takes=[first['logical_id'], second['logical_id']],
                        offsets_s=[first['cell'].get('stop_offset_s'), second['cell'].get('stop_offset_s')],
                        rank=first['rank'], take_cells=[second], duration_s=dur, expected_double_talk_s=exp,
                        exercises=('onset+internal+ending' if dur and dur <= INTERRUPT_S else 'onset (+ early internal)')))
    return out


def calibration_plan(p=None):
    """Two separate holds (A development, B prospective validation), each: warm-up; every fixture's single-talk cell
    and two double-talk takes; two echo-only null cells (raw delivery null: the alignment peak with no near end); the
    v9 alignment witnesses every 8 acquisitions; per-hold Jabra delay and level from the single-talk cells. Seeded
    per-hold orders and the A/B roles are declared here, before any capture (hash-bound with the plan)."""
    p = p or v9plan.plan_from()
    cells = p['v9']['cells']
    takes = calibration_takes(cells)
    echo = [c['logical_id'] for c in cells if c['kind'] == 'echo']
    nulls = [next(l for l in echo if '-m6-' in l), next(l for l in echo if '-0-' in l)]
    holds = {}
    for name, h in HOLDS.items():
        rng = random.Random(h['seed'])
        if h['order'] == 'adjacent':
            groups = [[t['single']]+t['takes'] for t in takes]
            rng.shuffle(groups)
            order = [lid for g in groups for lid in g]
            k = len(order)//3
            order = order[:k]+[nulls[0]]+order[k:2*k]+[nulls[1]]+order[2*k:]
        else:                                      # as frozen_order: scored shuffled, baselines spread
            order = [lid for t in takes for lid in t['takes']]+nulls
            rng.shuffle(order)
            singles = [t['single'] for t in takes]; rng.shuffle(singles)
            step = len(order)/len(singles)
            for i, b in enumerate(singles): order.insert(int(round((i+.5)*step))+i, b)
        n = len(order)
        witnesses = 1+(n+7)//8
        est = (SETUP_S+WINDOW_S['warmup']+len(takes)*(WINDOW_S['single']+2*WINDOW_S['double'])+2*WINDOW_S['echo']
               +witnesses*WINDOW_S['witness']+RESTORE_S)
        holds[name] = dict(role=h['role'], seed=h['seed'], order_mode=h['order'], order=order, acquisitions=n, witnesses=witnesses, null_cells=nulls,
                           estimate_s=est, output=str(CALIBRATION_OUTPUT.parent/f'{CALIBRATION_OUTPUT.name}-{name}'))
    sup = [t['single'] for t in takes if t['fixture'].endswith(SUPPLEMENT['single_of'])]
    two = next(t for t in takes if t['fixture'].endswith(SUPPLEMENT['take2_of']))
    sup += [two['single'], two['takes'][1]]
    w = 1+(len(sup)+7)//8
    supplement = dict(role=SUPPLEMENT['role'], order_mode='explicit', order=sup, acquisitions=len(sup), witnesses=w,
                      null_cells=[], estimate_s=SETUP_S+WINDOW_S['warmup']+2*WINDOW_S['single']+WINDOW_S['double']
                      +w*WINDOW_S['witness']+RESTORE_S, output=str(CALIBRATION_OUTPUT.parent/f'{CALIBRATION_OUTPUT.name}-A-supp'))
    return dict(fixtures=len(takes), takes=[{k: v for k, v in t.items() if k != 'take_cells'} for t in takes], supplement=supplement,
                take_cells=[c for t in takes for c in t['take_cells']], holds=holds, scored=False,
                records=['per-hold Jabra delay: single-talk raw/dry onset minus the timing prediction (median, spread)',
                         'per-hold Jabra level: single-talk raw dBFS and raw path gain per fixture',
                         'raw delivery null: the paired-template alignment peak on the echo-only null cells',
                         'transcript corroboration per double-talk take (report only, never a label by itself)'],
                permission='smoke standing permission (unscored, no USER-GO); host lock, normal restore with verification')


# ---- the frozen-rule manifest: produced from hold A, required (by hash) to run and analyse hold B (decision 92) ----

PERSISTENT_ROOTS = (v9plan.EVIDENCE,)   # Fable d98 N3: the catalog and manifest live beside the records, never in /tmp


def persistent(path):
    p = Path(path).resolve()
    return any(p == Path(r).resolve() or Path(r).resolve() in p.parents for r in PERSISTENT_ROOTS)


MANIFEST_RULES = ('t_c', 'level_floor_db', 'delivery', 'timing', 'eligibility', 'local_rule', 'control_positions')
# control_positions is recorded and bound for the hold-A/B report (the declared delivery-null search positions); no
# scorer code reads it (Fable d93 N2): it decides nothing.
DT_MASK = 'verified: source-active frame with >= 50 % reference-active capture samples, sample-accurate, no padding'


def _sha_file(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest_sha(m):
    import hashlib, json
    return hashlib.sha256(json.dumps(m, sort_keys=True, default=str).encode()).hexdigest()


def scorer_identity(root=None):
    """Code identity of a hold-B analysis (decisions 94 d / 95): every local hil/*.py file, sorted by name, as
    {name: sha256}, plus one digest over the sorted (name, content) pairs. Deliberately over-inclusive: any local edit
    after the freeze (an imported helper, a verdict rule, a test) makes hold B refuse to start."""
    import hashlib
    root = Path(root) if root else Path(__file__).parent
    files = sorted(p for p in root.glob('*.py') if p.is_file())
    h = hashlib.sha256()
    for p in files:
        data = p.read_bytes()
        h.update(p.name.encode()+b'\0'+str(len(data)).encode()+b'\0'+data)
    return dict(files={p.name: _sha_file(p) for p in files}, digest=h.hexdigest())


def validate_rules(rules):
    """Every frozen rule present, serialized rule specs valid under strict keys, delivery/timing/floor well formed.
    Used at freeze and again by check_manifest (hold B / C binding)."""
    import json
    missing = [k for k in MANIFEST_RULES if k not in rules]
    if missing: raise ValueError(f'manifest rules missing: {missing}')
    if rules['local_rule'] is None: raise ValueError('local_rule must be designed on hold A before B')
    import echoear_aec_q_near as near
    near.rule_from(rules['local_rule'], near.LOCAL_RULES)
    for k in BARGE_KINDS: near.rule_from(eligibility_spec(rules['eligibility'], k), near.ELIGIBILITY)
    for k in ('corr_min', 'ambiguity_min', 'level_min_db', 'level_max_db', 'offset_max_samples'):
        if k not in rules['delivery']: raise ValueError(f'delivery.{k} missing')
    for k in ('estimator', 'prior_samples', 'min_baselines'):             # Fable d97 N3: refuse at freeze, not in B
        if k not in rules['timing']: raise ValueError(f'timing.{k} missing')
    if rules['timing']['estimator'] != 'per-hold-median-v1' or type(rules['timing']['min_baselines']) is not int:
        raise ValueError('timing: unknown estimator or non-integer min_baselines')
    if isinstance(rules['level_floor_db'], bool) or not isinstance(rules['level_floor_db'], (int, float)):
        raise ValueError('level_floor_db must be a number: the loose floor is a gated clause (decision 98, Fable d98 N2)')


def build_manifest(rules, hold_a_dir, *, root=None, excluded=None, catalog=None):
    """The frozen rules designed on hold A only. rules: every MANIFEST_RULES key, explicitly (level_floor_db a number:
    the declared loose floor is gated). Refuses an unfinished hold A, unless its incomplete fixtures are exactly the
    declared excluded ones ({fixture name: reason}): they are recorded in the manifest, and the design did not use
    them (decision 97: hold A 19/21, both lost to a peer loss and a runner defect, not to acoustics). catalog: the path
    of a derived development catalog (echoear_aec_q_design.build_catalog) over hold A and its supplement; it must
    reference this hold A, its records must be unchanged, and when it supports every fixture no exclusion is needed.
    Its sha256 and records are bound in the manifest."""
    import json
    hold_a_dir = Path(hold_a_dir)
    validate_rules(rules)
    cs = hold_a_dir/'calibration-set.json'
    if not cs.exists(): raise ValueError(f'hold A not finished: {cs} missing')
    a = json.loads(cs.read_text())
    excluded = dict(excluded or {})
    if a.get('hold') != 'A' or a.get('fault') or not a.get('restores_clean'):
        raise ValueError('hold A incomplete (fault or restore): no freeze')
    cat_rec = None
    if catalog is not None:
        import echoear_aec_q_design as design
        cat = json.loads(Path(catalog).read_text())
        design.verify_catalog(cat)
        if Path(cat['records']['A']['dir']).resolve() != hold_a_dir.resolve() or cat['records']['A']['calibration_set_sha256'] != _sha_file(cs):
            raise ValueError('catalog does not reference this hold A')
        missing_support = sorted(x['fixture'].split('/')[-1] for x in cat['fixtures'] if not x['supported'])
        if set(missing_support) != set(excluded): raise ValueError(f'catalog unsupported fixtures {missing_support} != exclusions {sorted(excluded)}')
        cat_rec = dict(path=str(catalog), sha256=_sha_file(catalog), records=cat['records'], supported=cat['supported'], total=cat['total'])
        a = dict(a, complete_fixtures=cat['supported'], total_fixtures=cat['total'], incomplete=missing_support)
    inc = list(a.get('incomplete') or [])
    if a.get('complete_fixtures') != a.get('total_fixtures') and (set(inc) != set(excluded) or not excluded
                                                                 or len(inc) != a['total_fixtures']-a['complete_fixtures']):
        raise ValueError(f"hold A incomplete (fixture support {sorted(a.get('incomplete') or [])}; declared exclusions "
                         f"{sorted(excluded)}): no freeze")
    if a.get('complete_fixtures') == a.get('total_fixtures') and excluded: raise ValueError('exclusions declared for a complete hold A')
    if any(not str(r).strip() for r in excluded.values()): raise ValueError('every exclusion needs a reason')
    return dict(schema='aec-q-frozen-rules-1', scorer=scorer_identity(root), dt_mask=DT_MASK,
                rules={k: rules[k] for k in MANIFEST_RULES},
                hold_a=dict(output=str(hold_a_dir), calibration_set_sha256=_sha_file(cs), complete_fixtures=a['complete_fixtures'],
                            total_fixtures=a.get('total_fixtures'), excluded=excluded, catalog=cat_rec))


def check_manifest(path, expected_sha, *, root=None):
    """Hold B's binding: the manifest hash equals the declared one, its hold-A record is unchanged and the scorer
    files are those it froze. Returns (manifest, sha)."""
    import json
    m = json.loads(Path(path).read_text())
    sha = manifest_sha(m)
    if sha != expected_sha: raise ValueError(f'manifest sha256 {sha} != declared {expected_sha}')
    if m.get('schema') != 'aec-q-frozen-rules-1' or set(m.get('rules', {})) != set(MANIFEST_RULES): raise ValueError('manifest schema')
    validate_rules(m['rules'])                 # d100: the bound rules are re-validated, not trusted
    cs = Path(m['hold_a']['output'])/'calibration-set.json'
    if not cs.exists() or _sha_file(cs) != m['hold_a']['calibration_set_sha256']: raise ValueError('hold-A calibration set changed or missing')
    now = scorer_identity(root)
    cat = m['hold_a'].get('catalog')
    if cat:
        import echoear_aec_q_design as design
        if not Path(cat['path']).exists() or _sha_file(cat['path']) != cat['sha256']: raise ValueError('development catalog changed or missing')
        design.verify_catalog(json.loads(Path(cat['path']).read_text()))
    if m['scorer'] != now:
        changed = sorted(k for k in set(now['files']) | set(m['scorer'].get('files', {}))
                         if now['files'].get(k) != m['scorer'].get('files', {}).get(k))
        raise ValueError(f'scorer files differ from the frozen manifest: {changed}')
    return m, sha


def bind_manifest(q, m):
    """Apply the frozen rules to the plan's near-end section (hold B is analysed with these and nothing else)."""
    r = m['rules']
    q['nearend'].update(t_c=r['t_c'], level_floor_db=r['level_floor_db'], delivery=r['delivery'], timing=r['timing'],
                        eligibility=r['eligibility'], local_rule=r['local_rule'], control_positions=r['control_positions'])
    q['manifest_sha256'] = manifest_sha(m)
    return q
