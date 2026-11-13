#!/usr/bin/env python3
"""R4 board-selection gate; use --root for an isolated negative witness."""
import argparse
from pathlib import Path
import subprocess
import sys


def allowed(path, line):
    if path.startswith(('components/dg_board/', 'components/dg_display/backends/')):
        return True
    name = Path(path).name
    if name in ('Kconfig', 'Kconfig.projbuild', 'CMakeLists.txt', 'idf_component.yml'):
        return True
    if '/test_host/' in path:
        tail = path.split('/test_host/', 1)[1]
        if any(part.endswith('fakes') for part in Path(tail).parts) or tail.startswith('include/dg_test_'):
            return True
        # These build scripts generate board-defined host hardware fakes.
        if name.endswith('.cmake') and 'target_compile_definitions' in line:
            return True
        if name == 'compare_pre_refactor.py' and 'target_compile_definitions' in line:
            return True
    # No codec-file exception is currently necessary: complete implementations
    # are CMake-selected. A future exception must be table selection only.
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = subprocess.run(['rg', '--no-ignore', '--hidden', '-n', '--no-heading', 'CONFIG_DG_BOARD_', 'main', 'components'],
                            cwd=args.root, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        sys.stderr.write(result.stderr); return 2
    rejected = [row for row in result.stdout.splitlines() if not allowed(row.split(':', 2)[0], row.split(':', 2)[2])]
    if rejected:
        print('R4 board selection outside the allow-list:\n'+'\n'.join(rejected), file=sys.stderr)
        return 1
    print(f'R4 board-neutral gate passed ({len(result.stdout.splitlines())} allowed hits; no codec board branches).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
