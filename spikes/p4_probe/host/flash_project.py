#!/usr/bin/env python3
"""Flash a P4 spike from its generated manifest (coordinator only, no lock here)."""
import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path
from restore_factory import PORT
from hardware import checked_esptool_python, require_bench, check_project_config, FLASH_TIMEOUT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    build = args.build.resolve()
    manifest = json.loads((build / 'flasher_args.json').read_text())
    project = json.loads((build / 'project_description.json').read_text())
    if project['target'] != 'esp32p4' or manifest['extra_esptool_args']['chip'] != 'esp32p4':
        raise ValueError('P4 build required')
    check_project_config(project, build)
    bench_fd = None if args.dry_run else require_bench()
    python = checked_esptool_python()
    images = []
    for address, name in sorted(manifest['flash_files'].items(), key=lambda entry: int(entry[0], 0)):
        path = (build / name).resolve()
        if not path.is_relative_to(build) or not path.is_file(): raise ValueError('Unsafe flash file')
        images.extend([address, name])
    if len(images) != 6: raise ValueError('Expected bootloader, partitions and app only')
    command = [python, '-m', 'esptool', '--chip', 'esp32p4', '--port', PORT,
               '--baud', '921600', '--after', 'no-reset', 'write-flash', *manifest['write_flash_args'], *images]
    print('cd ' + shlex.quote(str(build)) + ' && ' + shlex.join(command), flush=True)
    if not args.dry_run:
        start = time.monotonic()
        try:
            subprocess.run(command, cwd=build, check=True, timeout=FLASH_TIMEOUT, pass_fds=(bench_fd,))
        finally:
            print(f'FLASH write elapsed_s={time.monotonic()-start:.3f} timeout_s={FLASH_TIMEOUT}', flush=True)


if __name__ == '__main__': main()
