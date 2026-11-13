#!/usr/bin/env python3
"""Retain failed OFF link controls under the explicit orchestrator ruling."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from step9a_campaign import arm, save
from step9a_resume import identities


def allowed_control(row):
    if row['mode'] != 'off' or not row['completed'] or not row['quiet_nic']['valid']:
        return False
    for data in row['strata'].values():
        memory = data['memory']['checks']
        if not (data['device_cadence']['target_passed'] and
                data['device_cadence']['hard_ceiling_passed'] and data['owner']['passed'] and
                all(memory[k] for k in ('session_window_internal', 'largest_internal',
                                         'failed_internal_allocations'))):
            return False
    return any(not data['link']['passed'] for data in row['strata'].values())


def comparison(runs, mode, exclude=()):
    aa = {r['label']: r for r in runs if r['label'].startswith('aa-')}
    selected = {r['label']: r for r in runs if r['label'].startswith(mode + '-')}
    numbers = sorted(int(label.split('-A')[1]) for label in selected if '-A' in label
                     and mode + '-B' + label.split('-A')[1] in selected
                     and int(label.split('-A')[1]) not in exclude)
    pairs = [(selected[f'{mode}-A{i}'], selected[f'{mode}-B{i}']) for i in numbers]
    output = {'pair_numbers': numbers, 'strata': {},
              'achieved_fps': [b['display_rate']['achieved_fps'] for _, b in pairs]}
    for stratum in ('busy', 'quiet'):
        names = set.intersection(*(set(a['strata'][stratum]['cpu']) &
                                   set(b['strata'][stratum]['cpu']) for a, b in pairs))
        tasks = {}
        for name in sorted(names):
            floor = abs(aa['aa-B']['strata'][stratum]['cpu'].get(name, 0) -
                        aa['aa-A']['strata'][stratum]['cpu'].get(name, 0))
            deltas = [b['strata'][stratum]['cpu'][name] -
                      a['strata'][stratum]['cpu'][name] for a, b in pairs]
            median = float(np.median(deltas))
            threshold = max(floor, 5 if name.startswith('IDLE') else 2)
            tasks[name] = {'paired_deltas_pp': deltas, 'median_delta_pp': median,
                           'aa_floor_pp': floor, 'threshold_pp': threshold,
                           'material': median < -threshold if name.startswith('IDLE')
                           else median > threshold}
        output['strata'][stratum] = {'cpu': tasks, 'windows_per_arm': len(pairs) * 4}
    return output


def summary(runs):
    result = {'absolute_passed': all(r['absolute_passed'] for r in runs),
              'retained_failed_controls': [r['label'] for r in runs if not r['absolute_passed']],
              'ruling_accepted': all(r['absolute_passed'] or allowed_control(r) for r in runs),
              'arms': {}}
    for mode in ('cat', 'full', 'fetch', '5b'):
        if any(r['label'] == mode + '-B1' for r in runs):
            result['arms'][mode] = comparison(runs, mode)
    if 'full' in result['arms'] and 5 in result['arms']['full']['pair_numbers']:
        result['full_excluding_pair5'] = comparison(runs, 'full', (5,))
        result['materiality_verdict_differences'] = [
            {'stratum': stratum, 'task': name, 'including_pair5': data['material'],
             'excluding_pair5': result['full_excluding_pair5']['strata'][stratum]['cpu'][name]['material']}
            for stratum, row in result['arms']['full']['strata'].items()
            for name, data in row['cpu'].items()
            if data['material'] != result['full_excluding_pair5']['strata'][stratum]['cpu'][name]['material']]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--stage', choices=('render', 'store', 'resume-store'), required=True)
    args = parser.parse_args()
    progress = json.loads((args.prior / 'progress.json').read_text())
    rules = json.loads((args.prior / 'predeclared-rule.json').read_text())
    assert hashlib.sha256(args.elf.read_bytes()).hexdigest() == rules['elf_sha256']
    paths = [Path(p) for p in progress['evidence_paths']]
    runs = []
    for label, path in zip(progress['order'], paths):
        row = json.loads((path / 'result.json').read_text())
        assert row['completed'] and (row['absolute_passed'] or allowed_control(row)), label
        assert row['elf_sha256'] == rules['elf_sha256']
        assert set(row['strata']) == {'busy', 'quiet'}
        assert all(len(d['snapshots']) == 5 for d in row['strata'].values())
        row.update(label=label, evidence_path=str(path))
        runs.append(row)
    if args.stage == 'render':
        assert progress['order'][-1] == 'full-A5'
        order = [('full-B5', 'full', False), ('full-A6', 'off', False),
                 ('full-B6', 'full', False), ('full-A7', 'off', False),
                 ('full-B7', 'full', False)]
    elif args.stage == 'store':
        assert progress['order'][-1] == 'full-B7'
        order = [row for row in rules['order'] if row[0].startswith(('fetch-', '5b-'))]
    else:
        assert progress['order'][-1] == 'fetch-B3'
        held = args.prior / 'fetch-A4'
        held_result = json.loads((held / 'result.json').read_text())
        assert held_result.get('acoustic_hold') == 'noise'
        assert not held_result['completed'] and not held_result['strata']
        assert not (held / 'serial.log').exists()
        store_order = [row for row in rules['order'] if row[0].startswith(('fetch-', '5b-'))]
        finished = [label for label in progress['order'] if label.startswith(('fetch-', '5b-'))]
        assert finished == [row[0] for row in store_order[:6]]
        order = store_order[6:]
        assert order[0][0] == 'fetch-A4'
        paths.append(held)
    args.out.mkdir(exist_ok=False, parents=True)
    protected = identities(paths)
    save(args.out / 'retained-input-sha256.json', protected)
    rules.update(continuation_order=order, added_full_pair=7,
                 outage_rule='Retain and continue pure link failures in rendering-OFF controls; stop any rendering-ON absolute failure; acoustic hold stops; no retakes',
                 stage=args.stage)
    save(args.out / 'predeclared-rule.json', rules)
    save(args.out / 'source-identity.json', {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
        (Path(__file__), Path(__file__).with_name('step9a_campaign.py'))})
    try:
        for label, mode, fetch in order:
            print('START ' + label, flush=True)
            try:
                row = arm(args.out / label, args.elf, mode, fetch,
                          label.startswith('5b-B'), label.startswith('5b-'))
            except Exception as error:
                save(args.out / 'blocked.json', {'label': label, 'reason': str(error), 'retained': True})
                if str(error).startswith('ACOUSTIC HOLD'):
                    print(str(error), flush=True)
                    return 2
                raise
            row.update(label=label, evidence_path=str(args.out / label))
            runs.append(row)
            save(args.out / 'progress.json', {'completed': len(runs),
                 'order': [r['label'] for r in runs], 'evidence_paths': [r['evidence_path'] for r in runs]})
            if not row['absolute_passed']:
                if allowed_control(row):
                    print('RETAIN FAILED OFF CONTROL ' + label, flush=True)
                else:
                    save(args.out / 'blocked.json', {'label': label, 'reason': 'absolute gates', 'retained': True})
                    save(args.out / 'partial-summary.json', summary(runs))
                    return 1
        save(args.out / 'summary.json', summary(runs))
        return 0
    finally:
        unchanged = identities(paths) == protected
        save(args.out / 'retained-input-verification.json', {'unchanged': unchanged, 'files': len(protected)})
        assert unchanged


if __name__ == '__main__':
    raise SystemExit(main())
