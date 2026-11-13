#!/usr/bin/env python3
"""Run the conditional diagnostic and always restore the retained production app."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from esp_serial import EspSerial, ROOT
from step9a_campaign import save

EVIDENCE = Path('/home/khkramer/src/evidence/step9a/resume')
FROZEN = '0457e428e6817aba15d8cfdd4cdf528674975fee50c80a22ebf8c9d143d66f67'


def execute(script, *arguments):
    subprocess.run([sys.executable, str(ROOT / 'hil' / script), *map(str, arguments)],
                   cwd=ROOT, check=True)


def main():
    rendering = json.loads((EVIDENCE / 'campaign-continuation-02/summary.json').read_text())
    assert rendering['arms']['full']['pair_numbers'] == list(range(1, 8))
    material = any(data['material'] for row in rendering['arms']['full']['strata'].values()
                   for data in row['cpu'].values())
    assert material, 'Diagnostic is conditional on material full-arm cost'
    assert hashlib.sha256((ROOT / 'build/dg-esp32.elf').read_bytes()).hexdigest() == FROZEN
    out = EVIDENCE / 'xip-off-attribution'
    out.mkdir(exist_ok=False)
    save(out / 'plan.json', {'purpose': 'one diagnostic OFF/full pair after seven full pairs',
                             'production_elf_sha256': FROZEN, 'production_restored_afterwards': True,
                             'retained_failed_OFF_link_controls': True, 'ON_absolute_failure_stops': True})
    execute('step9a_build_xip_off.py')
    identity = json.loads((EVIDENCE / 'xip-off-image/identity.json').read_text())
    directory = Path(identity['build_dir'])
    code = 1
    try:
        execute('step9a_flash_images.py', '--build-dir', directory, '--out', out / 'flash-diagnostic')
        completed = subprocess.run([sys.executable, str(ROOT / 'hil/step9a_xip_pair.py'),
                                    '--out', str(out / 'pair'), '--elf', str(directory / 'dg-esp32.elf')],
                                   cwd=ROOT)
        code = completed.returncode
    finally:
        execute('step9a_flash_images.py', '--build-dir', ROOT / 'build', '--out', out / 'restore-production',
                '--retained-app', EVIDENCE / 'final-image/dg-esp32.bin')
        with EspSerial('/dev/ttyACM0', log_path=out / 'production-restored-serial.log') as board:
            cursor = board.cursor
            board.reset()
            board.wait_for('READY transport-console', 60, since=cursor)
            line = board.wait_for('elf_sha256=', 3, since=cursor)
            assert FROZEN in line
            board.command('audio 0')
            board.command('disconnect', 45)
        save(out / 'production-restored.json', {'elf_sha256': FROZEN, 'verified': True,
                                                'storage': 'preserved; no filesystem image flashed'})
    return code


if __name__ == '__main__':
    raise SystemExit(main())
