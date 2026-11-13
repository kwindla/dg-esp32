"""Start only our runner, capture one ten-session boot, leave cleanup to finally."""
import argparse
from pathlib import Path
import os
import socket
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'hil'))
from esp_serial import EspSerial
from flash import firmware_info
import json
p = argparse.ArgumentParser()
p.add_argument('--out', type=Path, required=True)
p.add_argument('--esp32', action='store_true')
a = p.parse_args()
out = a.out.resolve()
if out.exists() and any(out.iterdir()):
    raise RuntimeError('Choose a new evidence directory; existing captures are preserved')
out.mkdir(parents=True, exist_ok=True)
project = Path(__file__).resolve().parent
(out/'firmware.json').write_text(json.dumps(firmware_info(project), indent=2)+'\n')
private = {line.split('=',1)[0] for line in (ROOT/'sdkconfig.local').read_text().splitlines()
           if line.startswith('CONFIG_') and '=' in line}
requested = set((project/'sdkconfig.defaults').read_text().splitlines())
resolved = (project/'sdkconfig').read_text().splitlines()
difference = [line.split('=',1)[0]+'=<redacted>' if line.split('=',1)[0] in private else line
              for line in resolved if line.startswith('CONFIG_') and line not in requested]
(out/'sdkconfig-diff.txt').write_text('\n'.join(difference)+'\n')
with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(('0.0.0.0',7861))
    except OSError as exc:
        raise RuntimeError('Port 7861 is occupied; refusing to touch its process') from exc
env = {k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
env.update(BOT_MODE='offline-test', RECORD_INPUT='1', RECORD_INPUT_DIR=str(out/'input'),
    EVENTS_PATH=str(out/'events.jsonl'), SPIKE_EVIDENCE_DIR=str(out),
    PIPECAT_SCTP_MAX_CHUNK_SIZE='1100', HF_HUB_OFFLINE='1', DG_OFFLINE_GUARD='1',
    PYTHONPATH=str(ROOT/'server/tests/offline_guard'))
cmd = [str(ROOT/'server/.venv/bin/python'),str(Path(__file__).with_name('server_evidence.py')),
       '--host','192.168.7.222' if a.esp32 else '0.0.0.0','--port','7861']
if a.esp32: cmd.append('--esp32')
with (out/'server.log').open('w') as log:
    server = subprocess.Popen(cmd, cwd=ROOT/'server',env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        for _ in range(150):
            if server.poll() is not None: raise RuntimeError('Runner exited')
            with socket.socket() as probe:
                if probe.connect_ex(('192.168.7.222' if a.esp32 else '127.0.0.1',7861)) == 0: break
            time.sleep(.2)
        else: raise TimeoutError('Runner startup')
        with EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as device:
            cursor=device.cursor; device.reset()
            line = device.wait_for('MATRIX DONE|abort\\(\\)|Guru Meditation|assert failed', 600, since=cursor)
            if 'MATRIX DONE' in line:
                # Let the final pipeline finish its cancellation/WAV close.
                time.sleep(2)
            if 'MATRIX DONE' not in line:
                time.sleep(3)
                raise RuntimeError('Device failure; see serial.log')
    finally:
        server.terminate()
        try: server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill(); server.wait()
