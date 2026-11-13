#!/usr/bin/env python3
"""Board identities. EchoEar delegates to frozen board_port; P4 is explicit."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import board_port

BOARDS = {
    'echoear': dict(by_id=str(board_port.ECHOEAR_PORT), usb_serial='B4:3A:45:16:2D:94',
        chip='esp32s3', efuse_mac=board_port.ECHOEAR_MAC, app_baud=115200, rom_baud=115200,
        reset_on_open=True, boot_pattern=r'dg-esp32: ESP32-S3', build_only=False),
    # 2 Mbaud requires CONFIG_PM_ENABLE=n: IDF 6.1 esp_stdio/Kconfig caps
    # ESP_CONSOLE_UART_BAUDRATE at 1 Mbaud when power management is enabled.
    'p4xc': dict(by_id='/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00',
        usb_serial='5B90169930', chip='esp32p4', efuse_mac='e8:f6:0a:e0:b8:76',
        app_baud=2000000, rom_baud=115200, reset_on_open=True,
        boot_pattern=r'DG_BOOT board=p4xc', build_only=False),
    'waveshare_146': dict(build_only=True),
}
BENCH_LOCK = Path('/tmp/claude-1000/echoear-bench.lock')


def hardware_board(board):
    if board not in BOARDS: raise ValueError(f'Unknown board: {board}')
    if BOARDS[board]['build_only']: raise RuntimeError(f'{board}: build-only board; hardware access refused')
    return BOARDS[board]


def require_bench():
    """Validate the runner-owned inherited flock; never acquire a bench here."""
    if os.environ.get('DG_BUILD_LOCK_HELD') == '1': raise RuntimeError('Release build lock before bench access')
    try:
        fd = int(os.environ['DG_BENCH_LOCK_FD'])
        held, expected = os.fstat(fd), BENCH_LOCK.stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError('Inherited FD is not the bench lock')
        with BENCH_LOCK.open('rb') as probe:
            try: fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: pass
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                raise RuntimeError('Bench lock is not held')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (KeyError, ValueError, OSError) as exc:
        raise RuntimeError('Run under hil/bench.py with the inherited bench lock') from exc
    return fd


def resolve_port(requested=None, *, board='echoear'):
    info = hardware_board(board)
    if board == 'echoear': return board_port.resolve_port(requested)
    pinned = Path(info['by_id']); resolved = pinned.resolve(strict=True)
    if requested and Path(requested).resolve() != resolved:
        raise RuntimeError('Refusing serial port: not the pinned P4')
    device = (Path('/sys/class/tty') / resolved.name / 'device').resolve(strict=True)
    serial_path = next((parent/'serial' for parent in (device, *device.parents)
                        if (parent/'serial').is_file()), None)
    if serial_path is None or serial_path.read_text().strip() != info['usb_serial']:
        raise RuntimeError('P4 USB serial identity mismatch')
    return str(pinned)


def checked_esptool_python():
    python = os.environ.get('ESPTOOL_PYTHON')
    if not python:
        env = os.environ.get('IDF_PYTHON_ENV_PATH')
        python = str(Path(env)/'bin/python') if env else str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python')
    version = subprocess.run([python, '-c', 'import esptool; print(esptool.__version__)'],
                             capture_output=True, text=True, timeout=10, check=True)
    if version.stdout.strip() != '5.5.0': raise RuntimeError('ESPTOOL_PYTHON must provide esptool 5.5.0')
    return python


def verify_flash_mac(port=None, *, board='echoear'):
    info = hardware_board(board)
    if board == 'echoear': return board_port.verify_flash_mac(port)
    require_bench()
    port = resolve_port(port, board=board)
    python = checked_esptool_python()
    result = subprocess.run([python, '-m', 'esptool', '--chip', info['chip'], '--port', port, 'read-mac'],
                            capture_output=True, text=True, timeout=30)
    macs = re.findall(r'MAC:\s*([0-9a-f:]{17})', result.stdout, re.I)
    if result.returncode or not macs or any(mac.lower() != info['efuse_mac'] for mac in macs):
        raise RuntimeError('esptool failed to verify the P4 eFuse MAC; refusing access')
    return port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', default='echoear'); parser.add_argument('--resolve-only', action='store_true')
    parser.add_argument('--build', type=Path, help='verify an already-built project target before a hardware action')
    parser.add_argument('port', nargs='?', default=None)
    args = parser.parse_args()
    info = hardware_board(args.board)
    if args.build is not None:
        metadata = json.loads((args.build/'project_description.json').read_text())
        if metadata['target'] != info['chip']: raise RuntimeError('Build target does not match selected board')
    call = resolve_port if args.resolve_only else verify_flash_mac
    print(call(args.port, board=args.board))


if __name__ == '__main__': main()
