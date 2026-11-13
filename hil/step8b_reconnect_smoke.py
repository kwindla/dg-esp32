"""Brief-authorized ten-cycle reconnect smoke on the chosen exact image."""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step7_support import BotServer
from alsa import resolve_jabra,set_volumes
p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=False);r={'passed':False,'cycles':[]}
def save():(a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
try:
    card,_=resolve_jabra();set_volumes(card,a.out/'mixer.log')
    with BotServer(a.out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
        start=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=start)
        identity=board.wait_for('elf_sha256=',3,since=start)
        r['elf_sha256']=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
        assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==r['elf_sha256']
        for i in range(1,11):
            cursor=board.cursor;begin=time.monotonic();board.command('connect',90)
            board.wait_for('STATE Ready',5,since=cursor)
            assert not any('SESSION FAIL' in l for _,_,l in board.lines_since(cursor))
            closing=board.cursor;board.command('disconnect',40);board.wait_for('STATE Idle',5,since=closing)
            after=board.cursor;time.sleep(.2)
            assert not any('STATE Starting' in l for _,_,l in board.lines_since(after))
            r['cycles'].append(dict(cycle=i,seconds=time.monotonic()-begin,passed=True));save();print(f'cycle {i}/10 pass',flush=True)
        after=board.cursor;time.sleep(2)
        assert not any('STATE Starting' in l for _,_,l in board.lines_since(after))
        text='\n'.join(l for _,_,l in board.lines_since(start))
        assert len(re.findall('ESP32-S3 step 7 audio elf_sha256=',text))==1
        assert not re.search(r'Guru Meditation|abort\(\)|assert failed|Task watchdog',text)
        r['passed']=True
except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}'
finally:save()
raise SystemExit(0 if r['passed'] else 1)
