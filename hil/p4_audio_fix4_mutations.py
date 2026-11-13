#!/usr/bin/env python3
"""Isolated harness mutations; never edit acquisition evidence or source files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
MUTATIONS = (
    ('s3-display-init', "board.command('disp init', 60)", 'pass'),
    ('product-profile', 'headless=False, bounds=bounds', 'headless=True, bounds=bounds'),
    ('zero-is-measured', 'direct > 0:', 'direct >= 0:'),
    ('census-ignored', "if task.get('name') == name]", 'if False]'),
    ('maximum-stack', 'free_bytes=min(values) if values else None', 'free_bytes=max(values) if values else None'),
    ('weakened-stack-floor', 'STACK_FLOOR = 4096', 'STACK_FLOOR = 1024'),
    ('positional-hold', 'super().finish(board, label, hold=hold)', 'super().finish(board, label, hold)'),
    ('missing-snapshot-ignored', 'available = bool(values) and not missing', 'available = bool(values)'),
    ('generations-mixed', "samples = [row for row in all_samples if row.get('generation') == final['generation']]", 'samples = all_samples'),
    ('rx-reference-weakened', 'RX_REFERENCE_FLOOR = 1744', 'RX_REFERENCE_FLOOR = 1024'),
    ('rx-percentage-ignored', 'max(RX_REFERENCE_FLOOR, (allocation+3)//4)', 'RX_REFERENCE_FLOOR'),
    ('rx-percentage-rounded-down', '(allocation+3)//4', 'allocation//4'),
    ('aborted-product-check', "if label == 'failed': return lines, capability", 'if False: return lines, capability'),
    ('bundle-aec-ignored', 'manifest=manifest, board=board', 'manifest=dict(aec_enabled=False), board=board'),
    ('finish-generations-mixed', "samples = [row for row in getattr(self, 'stack_samples', []) if row.get('generation') == generation]", "samples = getattr(self, 'stack_samples', [])"),
    ('finish-single-snapshot', "require(len(samples) >= 2, 'before/after stack snapshots from the measured generation required')", 'pass'),
    ('allocation-change-ignored', "require(len(set(allocations)) <= 1, 'allocation changed within generation: '+name)", 'pass'),
    ('direct-exceeds-allocation', "require(direct <= sizes[0], 'direct stack exceeds allocation: '+name)", 'pass'),
    ('maximum-task-minima', "minima[key]['free_bytes'] = min(minima[key]['free_bytes'], free)", "minima[key]['free_bytes'] = max(minima[key]['free_bytes'], free)"),
    ('s3-rx-gated', "report_only = ['rx_stack'] if board == 'echoear' else []", 'report_only = []'),
    ('p4-rx-report-only', "report_only = ['rx_stack'] if board == 'echoear' else []", "report_only = ['rx_stack']"),
    ('finish-board-ignored', 'stack_report(samples, board=self.args.board)', "stack_report(samples, board='p4xc')"),
    ('costs-board-ignored', "stack_report(samples, board=run['board'])", "stack_report(samples, board='p4xc')"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); args.out.mkdir(parents=True, exist_ok=False)
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    source = (HERE/'p4_audio_fix4.py').read_text(); results = []
    with tempfile.TemporaryDirectory(prefix='p4-audio-fix4-mutations-', dir=temporary) as work:
        work = Path(work)
        for name, before, after in MUTATIONS:
            if source.count(before) != 1: raise RuntimeError('mutation anchor drift: '+name)
            mutant = work/(name+'.py'); mutant.write_text(source.replace(before, after))
            env = dict(os.environ, DG_P4_AUDIO_FIX4_SOURCE=str(mutant), PYTHONDONTWRITEBYTECODE='1')
            command = [sys.executable, '-m', 'pytest', str(HERE/'test_p4_audio_fix4.py'),
                       '--basetemp', str(work/(name+'-pytest')), '-q']
            with (args.out/(name+'.log')).open('w') as log:
                code = subprocess.run(command, cwd=HERE.parent, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
            # Exit 1 proves assertion failures; collection/import errors (2+)
            # cannot serve as a killed semantic mutation witness.
            if code != 1: raise RuntimeError(f'{name}: expected test failure, got exit {code}')
            results.append(dict(mutation=name, killed=True, pytest_exit=code))
            print(name+': killed', flush=True)
    (args.out/'results.json').write_text(json.dumps(dict(passed=True, mutations=results), indent=2)+'\n')
    return 0


if __name__ == '__main__': raise SystemExit(main())
