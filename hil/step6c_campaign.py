#!/usr/bin/env python3
"""Alternate server encoders on the unchanged board image; retain every attempt."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from esp_serial import ROOT
from phase2_campaign import cohort, write_json
from phase2_outliers import isolated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--elf', type=Path, default=ROOT/'build/dg-esp32.elf')
    parser.add_argument('--outliers-only', action='store_true',
                        help='Retain and repeat isolated statistical tails once after the formal campaign')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=args.outliers_only)
    runs = json.loads((out/'runs.json').read_text()) if args.outliers_only else []

    def run(mode, name, script, *options):
        directory = out/mode/name
        directory.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, BOT_OPUS=mode)
        with directory.with_suffix('.log').open('w') as log:
            code = subprocess.run([str(ROOT/'hil/.venv/bin/python'), str(ROOT/'hil'/script),
                '--out', str(directory), *map(str, options)], env=env,
                stdout=log, stderr=subprocess.STDOUT, cwd=ROOT).returncode
        runs.append(dict(mode=mode, name=name, exit_code=code))
        (out/'runs.json').write_text(json.dumps(runs, indent=2)+'\n')
        print(f'{mode}/{name}: exit={code}', flush=True)
        return directory

    def outliers():
        for mode in ['aiortc', 'lean']:
            pop = cohort(out/mode)
            notes = []
            for (boot, index), reasons in isolated(pop).items():
                prior = any(r['boot'] == boot and r['index'] == index for r in pop['reruns'])
                note = dict(boot=boot, index=index, reasons=reasons, already_rerun=prior)
                notes.append(note)
                if prior:
                    continue
                name = f'stat-outliers/{boot}-session{index}'
                directory = out/mode/name
                if (directory/'result.json').exists():
                    continue
                directory.mkdir(parents=True, exist_ok=True)
                write_json(directory/'source.json', note)
                run(mode, name, 'transport_test.py', '--campaign', '--no-speech',
                    '--sessions', '1', '--elf', args.elf)
            write_json(out/mode/'stat-outliers.json', notes)

    if args.outliers_only:
        outliers()
        return
    for boot in range(1, 6):
        for mode in ['aiortc', 'lean']:
            directory = run(mode, f'boot{boot}', 'transport_test.py', '--campaign',
                '--rerun-failed', '--sessions', '3', '--elf', args.elf)
            result = json.loads((directory/'result.json').read_text())
            if len(result['sessions']) != 3:
                raise RuntimeError(f'Incomplete capture retained at {directory}')
    for index in range(1, 3):
        for mode in ['aiortc', 'lean']:
            run(mode, f'greeting{index}', 'greeting_test.py')
    run('lean', 'loop', 'audio_loop_test.py', '--firmware', 'app')
    outliers()


if __name__ == '__main__':
    main()
