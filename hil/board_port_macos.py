"""EchoEar bench identity on macOS. hil/board_port.py is the Linux resolver of record.

macOS has no /dev/serial/by-id or sysfs, so the board is identified by its USB
descriptor (Espressif VID, USB Serial/JTAG PID, serial = base MAC) from IOKit
through pyserial, without opening the device. cu.usbmodem* names are never
identities, and exactly one matching device is required. scripts/idf.sh calls
this on Darwin; the Linux HIL runners keep importing board_port.
"""
import os
import re
import subprocess
import sys

from board_port import ECHOEAR_MAC


def _comports():
    # Imported here: pyserial's macOS backend loads IOKit at import time.
    from serial.tools import list_ports
    return list_ports.comports()


def resolve_port(requested=None):
    board = os.environ.get('DG_BOARD', 'echoear')
    if board != 'echoear':
        raise RuntimeError(f'Hardware access disabled for DG_BOARD={board!r}; only EchoEar is authorized')
    matches = [p.device for p in _comports()
               if p.vid == 0x303a and p.pid == 0x1001
               and (p.serial_number or '').lower() == ECHOEAR_MAC
               and p.device.startswith('/dev/cu.')]
    if len(matches) != 1:
        raise RuntimeError(f'EchoEar USB identity {ECHOEAR_MAC} matched {len(matches)} ports')
    if requested and os.path.realpath(requested) != os.path.realpath(matches[0]):
        raise RuntimeError(f'Refusing serial port {requested!r}: not the pinned EchoEar')
    return matches[0]


def verify_flash_mac(port=None):
    port = resolve_port(port)
    result = subprocess.run([sys.executable, '-m', 'esptool', '--chip', 'esp32s3',
                             '--port', port, 'read-mac'], capture_output=True, text=True, timeout=30)
    macs = re.findall(r'MAC:\s*([0-9a-f:]{17})', result.stdout, re.I)
    if result.returncode or not macs or any(m.lower() != ECHOEAR_MAC for m in macs):
        raise RuntimeError('esptool failed to verify the EchoEar MAC; refusing flash')
    return port


if __name__ == '__main__':
    print(verify_flash_mac(sys.argv[1] if len(sys.argv) > 1 else None))
