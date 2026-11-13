#!/usr/bin/env python3
"""Isolated DSI regression mutants; run under scripts/build_locked.sh."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent/'backends/jd9365_dsi'
ROOT = HERE.parents[2]
MUTANTS = (
    ('y-overlap-always', 'framebuffer.cpp', 'old.rect.y0<rect.y1 && rect.y0<old.rect.y1', 'true'),
    ('frame-wrap-broken', 'framebuffer.cpp', 'uint32_t(frame-entry.frame)<2', 'frame<entry.frame || frame-entry.frame<2'),
    ('extra-swreset', 'backend.cpp', 'dg_board::lcd::reset()!=ESP_OK ||',
     'dg_board::lcd::reset()!=ESP_OK || esp_lcd_panel_reset(panel_)!=ESP_OK ||'),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('DG_BUILD_LOCK_HELD') != '1': raise RuntimeError('Use scripts/build_locked.sh')
    args.out.mkdir(parents=True, exist_ok=False)
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    libraries = [args.build.resolve()/'lib'/name for name in ('libgtest_main.a', 'libgtest.a')]
    if not all(path.is_file() for path in libraries): raise RuntimeError('Build the host display suite first')
    includes = [HERE.parent/'include', ROOT/'components/dg_board/include', BACKEND,
                args.build.resolve()/'_deps/googletest-src/googletest/include', HERE/'dsi_fakes',
                ROOT/'components/dg_diagnostics/include']
    results = []
    with tempfile.TemporaryDirectory(prefix='dsi-mutations-', dir=temporary) as work:
        work = Path(work)
        cases = [('control-copy', 'framebuffer.cpp', None, None), ('control-init', 'backend.cpp', None, None), *MUTANTS]
        for name, file, before, after in cases:
            original = (BACKEND/file).read_text()
            if before is not None and original.count(before) != 1: raise RuntimeError('Anchor drift: '+name)
            candidate = work/(name+'.cpp'); candidate.write_text(original.replace(before, after) if before else original)
            test = 'dsi_init_test.cpp' if file == 'backend.cpp' else 'dsi_framebuffer_test.cpp'
            sources = [HERE/test, candidate, HERE.parent/'src/present/logical_adapter.cpp', HERE.parent/'src/present/presenter.cpp']
            if file == 'backend.cpp': sources.append(BACKEND/'framebuffer.cpp')
            binary = work/name
            command = ['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread']
            for include in includes: command += ['-I', str(include)]
            command += [*map(str, sources), *map(str, libraries), '-o', str(binary)]
            with (args.out/(name+'-build.log')).open('w') as log:
                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
            with (args.out/(name+'-test.log')).open('w') as log:
                code = subprocess.run([str(binary)], stdout=log, stderr=subprocess.STDOUT).returncode
            expected = 0 if before is None else 1
            if code != expected: raise RuntimeError(f'{name}: expected test exit {expected}, got {code}')
            results.append(dict(case=name, test_exit=code, killed=before is not None))
            print(name+(': PASS' if before is None else ': killed'), flush=True)
    (args.out/'results.json').write_text(json.dumps(dict(passed=True, cases=results), indent=2)+'\n')
    return 0


if __name__ == '__main__': raise SystemExit(main())
