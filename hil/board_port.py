"""EchoEar-only bench identity. Never probe a user-supplied serial device."""
import os
from pathlib import Path
import re
import subprocess
import sys

ECHOEAR_MAC = 'b4:3a:45:16:2d:94'
ECHOEAR_PORT = Path('/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00')

def resolve_port(requested=None):
    board = os.environ.get('DG_BOARD', 'echoear')
    if board != 'echoear':
        raise RuntimeError(f'Hardware access disabled for DG_BOARD={board!r}; only EchoEar is authorized')
    if sys.platform == 'darwin':
        return _resolve_port_macos(requested)
    resolved = ECHOEAR_PORT.resolve(strict=True)
    if requested and Path(requested).resolve() != resolved:
        raise RuntimeError(f'Refusing serial port {requested!r}: not the pinned EchoEar')
    # ttyACM numbers change on replug and are never board identities.
    # Read USB identity without opening/resetting a device. USB Serial/JTAG's
    # serial is the base MAC. Fail closed if udev/sysfs identity is unavailable.
    device = (Path('/sys/class/tty') / resolved.name / 'device').resolve(strict=True)
    usb_serial = next((p / 'serial' for p in (device, *device.parents)
                       if (p / 'serial').is_file()), None)
    mac = usb_serial.read_text().strip().lower() if usb_serial else ''
    if mac != ECHOEAR_MAC:
        raise RuntimeError(f'EchoEar USB MAC mismatch: {mac!r}')
    return str(ECHOEAR_PORT)

def _resolve_port_macos(requested=None):
    # macOS has no /dev/serial/by-id or sysfs. Identify the board by its USB
    # descriptor (Espressif VID, USB Serial/JTAG PID, serial = base MAC) from
    # IOKit via pyserial, without opening the device. cu.usbmodem* names are
    # never identities; exactly one matching device is required.
    from serial.tools import list_ports
    matches = [p.device for p in list_ports.comports()
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
