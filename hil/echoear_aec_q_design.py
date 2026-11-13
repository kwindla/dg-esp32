"""Host-only A-only rule design check (decision 97): the constructed-edit separation of a calibration hold, through the
production evaluation path (default_baseline_eval -> per-hold timing -> default_near_eval -> near.measure with the
declared eligibility and local rule). Read-only over the hold; writes one JSON report.

  python hil/echoear_aec_q_design.py catalog --hold-a DIR [--supplement DIR] --out CATALOG.json
  python hil/echoear_aec_q_design.py separation (--hold DIR | --catalog CATALOG.json) --rules RULES.json --out REPORT.json

Per eligible final double-talk take: the intact result (eligible, b-whole, hole profile minimum and its position,
segment correlations, success) and the same for every constructed edit (near.constructed_edits on the take's scored
frames). Summary per edit class: n covered, n passing b-whole, max hole minimum, max segment-correlation minimum,
caught by the rule. The intact minimum and each target class maximum give the margins for the declared thresholds."""
import argparse
import copy
import json
from pathlib import Path

import numpy as np

import echoear_aec_q as Q
import echoear_aec_q_near as near
import echoear_aec_q_plan as qplan

TARGETS = ('mute-internal-400ms', 'mute-ending-400ms', 'splice-400ms', 'mute-ending-200ms')
LIMITS = ('mute-internal-200ms', 'mute-onset-80ms', 'mute-onset-120ms', 'mute-onset-200ms', 'mute-onset-400ms', 'splice-200ms')


def utterance(fixture):
    """The spoken sentence of a fixture: the firm (-48k) and normal fixtures of a sentence are one utterance ~6 dB apart."""
    import re
    return re.sub(r'-(48k|normal)\.wav$', '', str(fixture).split('/')[-1])


def bound_q(plan, rules):
    q = copy.deepcopy(plan['q'])
    r = rules
    q['nearend'].update(t_c=r['t_c'], level_floor_db=r['level_floor_db'], delivery=r['delivery'], timing=r['timing'],
                        eligibility=r['eligibility'], local_rule=r['local_rule'])
    return q


def _sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _record(path, label):
    path = Path(path)
    cs = json.loads((path/'calibration-set.json').read_text())
    if cs.get('hold') != label: raise ValueError(f'{path}: calibration set is hold {cs.get("hold")!r}, not {label!r}')
    if cs.get('fault') or not cs.get('restores_clean'): raise ValueError(f'{path}: fault or unclean restore')
    return dict(dir=str(path), cs=cs, plan=json.loads((path/'plan.json').read_text()))


def build_catalog(hold_a, supplement=None):
    """The derived development catalog (decision 97 review): references -- by path and sha256 -- the immutable hold-A
    record and, when given, the A-supp record; never rewrites either. Per fixture it selects explicitly:
      takes     every final double-talk take of hold A; a take number A lacks is taken from the supplement;
      baseline  per take, the valid single-talk baseline of the take's OWN record, else the other record's
                (cross_record=True: a template from a different session, flagged, never presented as contemporaneous);
      timing    the per-hold delay estimate of the take's own record (computed at evaluation)."""
    recs = {'A': _record(hold_a, 'A')}
    if supplement: recs['A-supp'] = _record(supplement, 'A-supp')
    fixtures = {}
    for label, r in recs.items():
        for fx, v in r['cs']['fixtures'].items():
            if fx == 'echo-null': continue
            e = fixtures.setdefault(fx, dict(fixture=fx, baselines={}, takes={}, slots=set()))
            st = (v.get('single_talk') or [None])[0]
            if st and st.get('folder') and st.get('baseline_valid'): e['baselines'][label] = dict(record=label, id=st['id'], folder=st['folder'])
            for t in v.get('double_talk') or []:
                if label == 'A': e['slots'].add(t['take'])          # the planned take slots are hold A's
                if t.get('folder') and t['take'] not in e['takes']:
                    e['takes'][t['take']] = dict(record=label, id=t['id'], take=t['take'], folder=t['folder'])
    out = []
    for fx, e in sorted(fixtures.items()):
        takes = []
        for n, t in sorted(e['takes'].items()):
            b = e['baselines'].get(t['record']) or next(iter(e['baselines'].values()), None)
            takes.append(dict(t, baseline=b, cross_record=bool(b) and b['record'] != t['record']))
        missing = [dict(take=n, reason='no final capture in any record') for n in sorted(e['slots']-set(e['takes']))]
        missing += [dict(take=t['take'], reason='no valid baseline in any record') for t in takes if not t['baseline']]
        out.append(dict(fixture=fx, takes=takes, missing=missing, slots=sorted(e['slots']),
                        supported=len(takes) >= 2 and all(t['baseline'] for t in takes)))
    return dict(schema='aec-q-dev-catalog-1',
                records={k: dict(dir=r['dir'], calibration_set_sha256=_sha(Path(r['dir'])/'calibration-set.json'),
                                 plan_sha256=_sha(Path(r['dir'])/'plan.json')) for k, r in recs.items()},
                fixtures=out, supported=sum(1 for x in out if x['supported']), total=len(out),
                slots=sum(len(x['slots']) for x in out))


def verify_catalog(cat):
    """Fable d98 B1: a catalog is accepted only as the deterministic function of the records it names. Rebuild it from
    those records and refuse unless it equals the given content (sort_keys JSON round trip)."""
    check_catalog(cat)
    recs = cat['records']
    rebuilt = build_catalog(recs['A']['dir'], (recs.get('A-supp') or {}).get('dir'))
    norm = lambda x: json.loads(json.dumps(x, sort_keys=True))
    if norm(rebuilt) != norm(cat): raise ValueError('catalog content does not match a rebuild from its records')
    return cat


def check_catalog(cat):
    """Refuses a catalog whose referenced records changed."""
    for k, r in cat['records'].items():
        if _sha(Path(r['dir'])/'calibration-set.json') != r['calibration_set_sha256'] or _sha(Path(r['dir'])/'plan.json') != r['plan_sha256']:
            raise ValueError(f'catalog record {k} changed: {r["dir"]}')
    return cat


def separation(source, rules):
    """source: a hold directory (single-record catalog) or a catalog dict."""
    cat = check_catalog(source if isinstance(source, dict) else build_catalog(source))
    recs = {k: _record(r['dir'], k) for k, r in cat['records'].items()}
    qs, cells, baselines, timing = {}, {}, {}, {}
    for k, r in recs.items():
        q = qs[k] = bound_q(r['plan'], rules)
        cal = qplan.calibration_plan(r['plan'])
        cells[k] = {c['logical_id']: c for c in r['plan']['q']['cells']+cal['take_cells']}
        baselines[k], offs = {}, []
        for fx, v in r['cs']['fixtures'].items():
            if fx == 'echo-null': continue
            st = v['single_talk'][0]
            if not (st['folder'] and st['baseline_valid']): continue
            b = Q.default_baseline_eval(cells[k][st['id']], dict(folder=st['folder']), q)
            if b.get('valid'):
                baselines[k][fx] = b; offs.append(b['stats'].get('jabra_delay_offset_samples'))
        timing[k] = near.hold_delay(offs, q['nearend']['timing'])
    rows = []
    skipped = []
    for e in cat['fixtures']:
        fx = e['fixture']
        for mi in e.get('missing') or []:
            if mi['reason'] == 'no final capture in any record':
                skipped.append(dict(fixture=fx.split('/')[-1], take=mi['take'], reason=mi['reason']))
        for t in e['takes']:
            if not t['baseline']:
                skipped.append(dict(fixture=fx.split('/')[-1], take=t['take'], record=t['record'], reason='no valid baseline in any record')); continue
            base = baselines[t['baseline']['record']].get(fx)
            if base is None:
                skipped.append(dict(fixture=fx.split('/')[-1], take=t['take'], record=t['record'],
                                    reason=f"baseline {t['baseline']['record']}/{t['baseline']['id']} invalid on recomputation")); continue
            k = t['record']; q = qs[k]
            cell, row = cells[k][t['id']], dict(folder=t['folder'])
            r = Q.default_near_eval(cell, row, base, q, timing=timing[k])
            rec = dict(fixture=fx.split('/')[-1], kind=cell['kind'], take=t['take'], record=k, baseline_record=t['baseline']['record'],
                       cross_record=t['cross_record'], intact=_slim(r))
            m = r.get('measure') or {}
            if r.get('valid') and m.get('eligible'):
                folder, raw, afe, ref, blocks = Q._load(row)
                src, span = near.read_source(fx)
                onset = m['align']['onset']
                cov_rule = near.rule_from(qplan.eligibility_spec(q['nearend']['eligibility'], cell['kind']), near.ELIGIBILITY)
                from echoear_aec_bench import load_capture
                from echoear_aec_metrics import activity
                ra = activity(ref, load_capture(folder.parent/'quiet.npz')[2], [(0, len(ref)-1024)])
                dt, s_act = near.dt_mask(ra, src, span, onset)
                vm = cov_rule(dt, s_act, dict(onset=onset, span=span, ref_active=ra))[2]['_scored']
                pool = {**baselines['A'], **{x: y for kk in baselines for x, y in baselines[kk].items()}}
                other = next(b['base']['raw_template'] for kx, b in pool.items() if utterance(kx) != utterance(fx))
                edits = []
                for ed in near.constructed_edits(afe, onset, span, vm, other=other, echo=ref[onset:onset+len(src)]):
                    re_ = Q.default_near_eval(cell, row, base, q, timing=timing[k], arrays=(folder, raw, ed['afe'], ref, blocks))
                    edits.append(dict(edit=ed['name'], label=ed['label'], covered=ed['covered'], **_slim(re_)))
                rec['edits'] = edits
            rows.append(rec)
    summary = summarize(rows)
    summary['inventory'] = inventory(cat, rows, skipped)
    return dict(catalog=dict(records=cat['records'], supported=cat['supported'], total=cat['total']), timing=timing,
                rules=rules, takes=rows, skipped=skipped, summary=summary)


def inventory(cat, rows, skipped):
    """Astra d98 caveat / Fable N5: every planned take slot accounted for -- evaluated rows by validity, eligibility and
    outcome, with their cross-record flag, and every skipped slot with its reason. complete is True only when every
    slot is an evaluated, valid, eligible intact take (the condition for freezing on the catalog)."""
    slots = cat.get('slots') or sum(len(x.get('slots') or []) for x in cat['fixtures'])
    ev = [r['intact'] | dict(fixture=r['fixture'], take=r['take'], record=r['record'], cross_record=r['cross_record']) for r in rows]
    n = lambda f: sum(1 for x in ev if f(x))
    inv = dict(slots=slots, evaluated=len(ev), skipped=len(skipped), valid=n(lambda x: x['valid']), invalid=n(lambda x: not x['valid']),
               eligible=n(lambda x: x['eligible']), ineligible=n(lambda x: x['valid'] and not x['eligible']),
               success=n(lambda x: x['success'] is True), miss=n(lambda x: x['success'] is False),
               unknown=n(lambda x: x['eligible'] and x['success'] is None), cross_record=n(lambda x: x['cross_record']),
               rows=[{k: x[k] for k in ('fixture', 'take', 'record', 'cross_record', 'valid', 'eligible', 'success', 'reason')} for x in ev],
               skipped_rows=skipped)
    inv['complete'] = len(ev) == slots and inv['eligible'] == slots and inv['success'] == slots and not skipped
    return inv


def _slim(r):
    m = r.get('measure') or {}
    h = m.get('hole') or {}
    return dict(valid=r.get('valid'), eligible=m.get('eligible'), corr=m.get('corr'), clause_corr=m.get('clause_corr'),
                clause_level=m.get('clause_level'), level_deficit_db=m.get('level_deficit_db'),
                clause_local=m.get('clause_local'), success=m.get('success'), clipped=m.get('afe_clipped_samples'),
                hole_min_db=h.get('min_db'), hole_position=h.get('position'), hole_start_frame=h.get('start_frame'),
                seg_min=min((x.get('corr_lagtol', x['corr']) for x in m.get('segments') or []), default=None),
                seg_min_cell_lag=min((x['corr'] for x in m.get('segments') or []), default=None), reason=r.get('reason'))


def summarize(rows):
    intact = [r['intact'] for r in rows if r['intact']['eligible']]
    out = dict(intact=dict(n=len(intact), hole_min_db=min((x['hole_min_db'] for x in intact), default=None),
                           seg_min=min((x['seg_min'] for x in intact if x['seg_min'] is not None), default=None),
                           success=sum(1 for x in intact if x['success']), failed=[(r['fixture'], r['take']) for r in rows
                                                                                  if r['intact']['eligible'] and r['intact']['success'] is False]))
    classes = {}
    for r in rows:
        for e in r.get('edits') or []:
            c = classes.setdefault(e['edit'], dict(label=e['label'], n=0, covered=0, bwhole_pass=0, caught=0, hole_max_db=None, seg_max=None))
            c['n'] += 1
            if not e['covered']: continue
            c['covered'] += 1
            passing = e['eligible'] and e['clause_corr'] and not e['clipped']
            if passing:
                c['bwhole_pass'] += 1
                if e['hole_min_db'] is not None: c['hole_max_db'] = e['hole_min_db'] if c['hole_max_db'] is None else max(c['hole_max_db'], e['hole_min_db'])
                if e['seg_min'] is not None: c['seg_max'] = e['seg_min'] if c['seg_max'] is None else max(c['seg_max'], e['seg_min'])
            if e['success'] is False: c['caught'] += 1
    out['classes'] = classes
    out['targets'] = {k: classes.get(k) for k in TARGETS}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('catalog', help='write the derived development catalog (A, optionally A-supp)')
    c.add_argument('--hold-a', type=Path, required=True)
    c.add_argument('--supplement', type=Path)
    c.add_argument('--out', type=Path, required=True)
    s = sub.add_parser('separation', help='constructed-edit separation over a hold or a catalog')
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument('--hold', type=Path)
    g.add_argument('--catalog', type=Path)
    s.add_argument('--rules', type=Path, required=True)
    s.add_argument('--out', type=Path, required=True)
    a = ap.parse_args(argv)
    if a.out.exists(): raise SystemExit(f'output exists: {a.out}')
    if a.cmd == 'catalog':
        if not qplan.persistent(a.out): raise SystemExit(f'{a.out}: the catalog must live under the evidence tree (Fable d98 N3)')
        cat = build_catalog(a.hold_a, a.supplement)
        a.out.write_text(json.dumps(cat, indent=1, sort_keys=True))
        print(json.dumps(dict(supported=cat['supported'], total=cat['total'], sha256=_sha(a.out)), indent=1))
        return 0
    rep = separation(json.loads(a.catalog.read_text()) if a.catalog else a.hold, json.loads(a.rules.read_text()))
    a.out.write_text(json.dumps(rep, indent=1, default=lambda x: x.tolist() if hasattr(x, 'tolist') else str(x)))
    print(json.dumps(rep['summary'], indent=1, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
