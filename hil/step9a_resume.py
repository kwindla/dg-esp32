#!/usr/bin/env python3
"""Continue a preflight-held campaign without reacquiring completed boots."""
import argparse
import hashlib
import json
from pathlib import Path
from step9a_campaign import arm, save, summarize


def identities(paths):
    return {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in paths for p in sorted(folder.rglob('*')) if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    args = parser.parse_args()
    prior = args.prior.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    rules = json.loads((prior / 'predeclared-rule.json').read_text())
    order = rules['order']
    progress = json.loads((prior / 'progress.json').read_text())
    completed = progress['order']
    paths = [Path(p) for p in progress.get('evidence_paths', [str(prior / label) for label in completed])]
    assert completed == [row[0] for row in order[:len(completed)]], 'Non-prefix completed boot list'
    assert hashlib.sha256(args.elf.read_bytes()).hexdigest() == rules['elf_sha256'], 'Frozen ELF changed'
    runs = []
    for label, path in zip(completed, paths):
        row = json.loads((path / 'result.json').read_text())
        assert row['completed'] and row['absolute_passed'] and row['elf_sha256'] == rules['elf_sha256'], label
        assert set(row['strata']) == {'busy', 'quiet'}
        assert all(len(value['snapshots']) == 5 for value in row['strata'].values())
        row['label'] = label
        row['evidence_path'] = str(path.resolve())
        runs.append(row)
    next_label = order[len(completed)][0]
    assert not (prior / next_label / 'serial.log').exists(), 'Next boot already acquired board data'
    if (prior / 'boundary-pause.json').exists():
        pause=json.loads((prior / 'boundary-pause.json').read_text())
        assert pause['fresh_next']==next_label and not pause['board_data_started'] and pause['after_completed']==completed[-1]
        assert next_label=='fetch-A1' and completed[-1]=='full-B6'
        basis='controlled boundary pause after rendering; next fetch preflight never scored'
    else:
        assert next_label == 'cat-A2', 'User authorized this specific held resumption point'
        held = json.loads((prior / next_label / 'result.json').read_text())
        assert held.get('acoustic_hold') == 'noise' and not held['completed'] and not held['strata']
        basis='user-cleared acoustic hold; held cat-A2 preflight never scored'
    protected = paths + [prior / next_label]
    before = identities(protected)
    save(args.out / 'retained-input-sha256.json', before)
    (args.out / 'predeclared-rule.json').write_bytes((prior / 'predeclared-rule.json').read_bytes())
    save(args.out / 'resumption-plan.json', {
        'prior': str(prior), 'protected_completed_boots': completed,
        'fresh_start': next_label, 'held_preflight_preserved': str(prior / next_label),
        'authorization': 'Continue all remaining9a; '+basis+'; no completed boot retakes',
        'per_boot_acquisition': 'Unchanged step9a_campaign.arm and retained predeclared windows/strata/outage rules',
        'campaign_source_sha256': hashlib.sha256(Path(__file__).with_name('step9a_campaign.py').read_bytes()).hexdigest(),
        'elf_sha256': rules['elf_sha256']})
    try:
        for label, mode, fetch in order[len(completed):]:
            print('START ' + label, flush=True)
            try:
                row = arm(args.out / label, args.elf.resolve(), mode, fetch,
                          label.startswith('5b-B'), label.startswith('5b-'))
            except Exception as error:
                reason = str(error)
                save(args.out / 'blocked.json', {'label': label, 'reason': reason, 'retained': True})
                if reason.startswith('ACOUSTIC HOLD'):
                    print(reason, flush=True)
                    return 2
                raise
            row['label'] = label
            row['evidence_path'] = str(args.out / label)
            runs.append(row)
            save(args.out / 'progress.json', {'completed': len(runs), 'order': [r['label'] for r in runs],
                                             'evidence_paths': [r['evidence_path'] for r in runs]})
            if not row['absolute_passed']:
                save(args.out / 'blocked.json', {'label': label, 'reason': 'absolute gates', 'retained': True})
                return 1
        save(args.out / 'summary.json', summarize(runs))
        return 0
    finally:
        unchanged = identities(protected) == before
        save(args.out / 'retained-input-verification.json', {'unchanged': unchanged, 'files': len(before)})
        assert unchanged, 'Retained completed or held evidence changed'


if __name__ == '__main__':
    raise SystemExit(main())
