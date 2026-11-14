"""EchoEar-only bench entry: caller holds flock before any device/service access."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import fcntl
import os
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
BY_ID=Path('/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00')
MAC='b4:3a:45:16:2d:94'
LOCK=Path(f'/tmp/claude-{os.getuid()}/echoear-bench.lock')

@contextmanager
def bench():
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    with LOCK.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=os.environ.get('DG_BENCH_LOCK_FD')
        os.environ['DG_BENCH_LOCK_FD']=str(lock.fileno())
        try:yield
        finally:
            if previous is None:os.environ.pop('DG_BENCH_LOCK_FD',None)
            else:os.environ['DG_BENCH_LOCK_FD']=previous
            fcntl.flock(lock,fcntl.LOCK_UN)

def esptool_python():
    # HIL/bot dependencies and Espressif's tool environment are independent.
    return os.environ.get('ESPTOOL_PYTHON', sys.executable)


def check_esptool():
    """Host-only dependency check; call before taking the shared bench."""
    python = esptool_python()
    result = subprocess.run([python, '-c', 'import esptool'], capture_output=True,
                            text=True, timeout=10)
    if result.returncode:
        raise RuntimeError('esptool unavailable: set ESPTOOL_PYTHON to the IDF Python environment')
    return python

def verify(out):
    """Call inside bench lock before each flash/serial session. Abort on mismatch."""
    port=BY_ID.resolve(strict=True)
    result=subprocess.run([esptool_python(),'-m','esptool','--chip','esp32s3','--port',str(BY_ID),'read-mac'],capture_output=True,text=True,timeout=30)
    (out/'identity.log').write_text(result.stdout+result.stderr)
    found=re.findall(r'MAC:\s*([0-9a-f:]{17})',result.stdout,re.I)
    if result.returncode or not found or any(m.lower()!=MAC for m in found):raise RuntimeError('EchoEar MAC verification failed')
    return str(BY_ID)

def require_echoear_image():
    # Refuse legacy audio even when a pre-port image happened to drive the LCD.
    config=(ROOT/'build/config/sdkconfig.h').read_text()
    elf=(ROOT/'build/dg-esp32.elf').read_bytes()
    if '#define CONFIG_DG_BOARD_ECHOEAR 1' not in config or b'echoear-audio' not in elf:
        raise RuntimeError('Build the rebased EchoEar board/audio image before hardware access')

def flash(out):
    require_echoear_image()
    port=verify(out)
    with (out/'flash.log').open('w') as log:
        subprocess.run([esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800','write-flash','@flash_args'],cwd=ROOT/'build',stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)

def check_firmware(board,since,elf=None,profile=None):
    """Boot proof for the running image.

    elf=None keeps the legacy local-build route (ROOT/build). A bound external image (step 10 requal-n1) passes its
    frozen ELF and expected public profile instead: identity and board come from the ELF bytes and the device's
    own boot lines; the generated build configuration and the generic build/ ELF are never read."""
    if elf is None:
        require_echoear_image()
        expected=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
    else:
        image=Path(elf).read_bytes()
        if b'echoear-audio' not in image:raise RuntimeError('Bound ELF is not an EchoEar board/audio image')
        expected=hashlib.sha256(image).hexdigest()
    lines=[line for _,_,line in board.lines_since(since)]
    if any('Virtual address not enough for PSRAM' in line for line in lines):
        raise RuntimeError('PSRAM mapping truncated; fix the XIP/virtual address budget before HIL')
    if not any('echoear-audio: register readback' in line for line in lines):
        raise RuntimeError('Missing EchoEar codec register readback proof')
    if any('gpio: conflict found for GPIO' in line for line in lines):
        raise RuntimeError('GPIO ownership conflict in boot log')
    found=[m.group(1) for line in lines if (m:=re.search(r'elf_sha256=([0-9a-f]{64})',line))]
    if not found or found[-1]!=expected:
        raise RuntimeError('Board ELF differs from this worktree; flash before testing')
    if profile is not None:
        from echoear_image_profile import boot_gate
        verdict=boot_gate(lines,profile)
        if not verdict['passed']:raise RuntimeError('Boot public profile mismatch: '+json.dumps(verdict['checks']))
    return expected
