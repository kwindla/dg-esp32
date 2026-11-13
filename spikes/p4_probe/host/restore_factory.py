#!/usr/bin/env python3
"""Verify the pinned backup pair, restore P4, verify flash, reset, and witness boot.

Takes no lock. The coordinator must launch this under hil/bench.py, never under
build_locked.sh. Dry-run validates files and prints commands without opening a port.
"""
import argparse
import hashlib
import re
import shlex
import subprocess
import time
from pathlib import Path
from hardware import checked_esptool_python, esptool_python, require_bench, WRITE_TIMEOUT, VERIFY_TIMEOUT

PORT = '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00'
SIZE = 33_554_432
SHA = '37db1cc6fcc1da5956d76dfb4100cff5a06b1112bbda862b6c4b98c644bacdcd'
BANNER = b'Project name:     esp_brookesia_demo'


def check_port(value):
    if value != PORT:
        raise argparse.ArgumentTypeError('Only the pinned P4 CH343 by-id port is allowed')
    return value


def verify_backup(directory):
    directory = Path(directory).resolve()
    hashes, sizes = {}, {}
    for line in (directory / 'sha256.txt').read_text().splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) == 2 and re.fullmatch('[0-9a-fA-F]{64}', fields[0]):
            name, value = fields[1].removeprefix('*'), fields[0].lower()
            if name not in ('factory-a.bin', 'factory-b.bin') or name in hashes:
                raise ValueError('Unexpected or duplicate backup checksum')
            hashes[name] = value
        elif len(fields) == 2 and fields[0] in ('factory-a.bin', 'factory-b.bin') and fields[1].isdigit():
            if fields[0] in sizes:
                raise ValueError('Duplicate backup size')
            sizes[fields[0]] = int(fields[1])
        else:
            raise ValueError('Unrecognized sha256.txt line')
    if set(hashes) != {'factory-a.bin', 'factory-b.bin'}:
        raise ValueError('Both backup checksums are required')
    if set(sizes) != {'factory-a.bin', 'factory-b.bin'}:
        raise ValueError('Both backup sizes are required')
    for name, expected in hashes.items():
        path = directory / name
        if path.is_symlink() or path.stat().st_size != SIZE or sizes[name] != SIZE:
            raise ValueError('Incorrect backup size or symlink: ' + name)
        if expected != SHA:
            raise ValueError('Checksum differs from pinned factory backup: ' + name)
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('Backup checksum mismatch: ' + name)
    return directory / 'factory-a.bin'


def commands(backup, port=PORT, python=None):
    check_port(port)
    base = [python or esptool_python(), '-m', 'esptool', '--chip', 'esp32p4', '--port', port, '--baud', '921600']
    return [base + ['--after', 'no-reset', 'write-flash', '--flash-mode', 'keep', '--flash-freq', 'keep',
                    '--flash-size', 'keep', '0x0', str(backup)],
            base + ['--after', 'no-reset', 'verify-flash', '0x0', str(backup)]]


def wait_factory(port=PORT):
    from console_rate import Board
    with Board(port, 115200) as board:
        board.reset()
        lines = board.until(lambda line: BANNER in line, 15)
        if not any(BANNER in line for line in lines):
            raise RuntimeError('Factory banner missing')
        print('Factory boot verified: ' + BANNER.decode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--port', type=check_port, default=PORT)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    bench_fd = None if args.dry_run else require_bench()
    python = checked_esptool_python()
    backup = verify_backup(args.backup)
    for phase, timeout, command in zip(('write', 'verify'), (WRITE_TIMEOUT, VERIFY_TIMEOUT), commands(backup, args.port, python)):
        print(shlex.join(command), flush=True)
        if not args.dry_run:
            start = time.monotonic()
            try:
                subprocess.run(command, check=True, timeout=timeout, pass_fds=(bench_fd,))
            finally:
                print(f'RESTORE {phase} elapsed_s={time.monotonic()-start:.3f} timeout_s={timeout}', flush=True)
    if args.dry_run:
        print(f'SERIAL {args.port} 115200 DTR=0 RTS=0; hard-reset RTS=1, wait 150ms, RTS=0; wait 15s for {BANNER.decode()!r}')
    else:
        # Witness with IDF's pyserial even when this tool was launched by uv/server.
        subprocess.run([python, '-c', 'from restore_factory import wait_factory; wait_factory()'],
                       cwd=Path(__file__).resolve().parent, check=True, timeout=30, pass_fds=(bench_fd,))


if __name__ == '__main__':
    main()
