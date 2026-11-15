"""Bench identities on macOS. hil/board_port.py and hil/boards.py are the Linux resolvers of record.

macOS has no /dev/serial/by-id or sysfs, so a board is identified by its USB
descriptor from IOKit through pyserial, without opening the device: the EchoEar
by Espressif VID, USB Serial/JTAG PID and serial = base MAC; the P4 by its
CH343 bridge's VID, PID and serial. cu.* names are never identities, and exactly
one matching device is required. scripts/idf.sh calls this on Darwin; the Linux
HIL runners keep importing board_port and boards.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from board_port import ECHOEAR_MAC
import boards

# USB (vendor, product) and the descriptor's serial string, per board.
USB = {
    'echoear': (0x303a, 0x1001, ECHOEAR_MAC),
    'p4xc': (0x1a86, 0x55d3, boards.BOARDS['p4xc']['usb_serial']),
}


def _comports():
    # Imported here: pyserial's macOS backend loads IOKit at import time.
    from serial.tools import list_ports
    return list_ports.comports()


def resolve_port(requested=None, *, board='echoear'):
    if board == 'echoear':
        selected = os.environ.get('DG_BOARD', 'echoear')
        if selected != 'echoear':
            raise RuntimeError(f'Hardware access disabled for DG_BOARD={selected!r}; only EchoEar is authorized')
    elif board != 'p4xc':
        boards.hardware_board(board)  # Unknown and build-only boards keep their refusals.
        raise RuntimeError(f'{board}: no macOS identity')
    vid, pid, serial = USB[board]
    matches = [p.device for p in _comports()
               if p.vid == vid and p.pid == pid
               and (p.serial_number or '').lower() == serial.lower()
               and p.device.startswith('/dev/cu.')]
    if len(matches) != 1:
        raise RuntimeError(f'{board} USB identity {serial} matched {len(matches)} ports')
    if requested and os.path.realpath(requested) != os.path.realpath(matches[0]):
        raise RuntimeError(f'Refusing serial port {requested!r}: not the pinned {board}')
    return matches[0]


def verify_flash_mac(port=None, *, board='echoear'):
    info = boards.hardware_board(board)
    if board == 'p4xc':
        boards.require_bench()  # As on Linux: the P4 is reached only under the bench lock.
    port = resolve_port(port, board=board)
    # The P4 keeps the Linux esptool pin; the EchoEar path is as it was.
    python = boards.checked_esptool_python() if board == 'p4xc' else sys.executable
    result = subprocess.run([python, '-m', 'esptool', '--chip', info['chip'],
                             '--port', port, 'read-mac'], capture_output=True, text=True, timeout=30)
    macs = re.findall(r'MAC:\s*([0-9a-f:]{17})', result.stdout, re.I)
    if result.returncode or not macs or any(m.lower() != info['efuse_mac'] for m in macs):
        raise RuntimeError(f'esptool failed to verify the {board} eFuse MAC; refusing access')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--board', default='echoear')
    parser.add_argument('--build', type=Path, help='verify an already-built project target before a hardware action')
    parser.add_argument('port', nargs='?', default=None)
    args = parser.parse_args(argv)
    info = boards.hardware_board(args.board)
    if args.build is not None:
        metadata = json.loads((args.build/'project_description.json').read_text())
        if metadata['target'] != info['chip']: raise RuntimeError('Build target does not match selected board')
    print(verify_flash_mac(args.port or None, board=args.board))


if __name__ == '__main__': main()
