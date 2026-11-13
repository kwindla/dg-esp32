"""Final-image 120 s room-noise observation with the normal automatic mic gate."""
import argparse
import json
import hashlib
import re
import time
from pathlib import Path
import numpy as np
from alsa import resolve_jabra,set_volumes,record_wav
from bench_calibration import calibrate,dbfs
from esp_serial import EspSerial,ROOT
from step7_support import BotServer,pcm
from transport_test import perf
p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=False);r={'passed':False}
try:
    card,device=resolve_jabra()
    with BotServer(a.out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
        cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
        identity=board.wait_for('elf_sha256=',3,since=cur)
        r['elf_sha256']=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
        assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==r['elf_sha256']
        r['calibration']=calibrate(board,device,a.out/'calibration',65)
        cur=board.cursor;board.command('connect',90);board.command('audio 1');board.wait_for('GATE open',15,since=cur)
        r['greeting_settle_seconds']=len(pcm(ROOT/'server/fixtures/greeting.wav'))/16000+.5
        time.sleep(r['greeting_settle_seconds'])
        pc=perf(board)['pc_id'];began=time.monotonic();floors=[]
        for i in range(2):
            set_volumes(card,a.out/f'mixer-silence-{i}.log')
            with record_wav(a.out/f'room-{i}.wav',device,60,16000) as rec:rec.wait()
            floors.append(dbfs(pcm(a.out/f'room-{i}.wav')));print(f'silent minute {i+1}/2 complete',flush=True)
        events=[e for e in bot.events() if e['pc_id']==pc and e['t']>=began]
        starts=[e for e in events if e['event']=='vad-start']
        telemetry=[e['message']['data']['d']['rms_dbfs'] for e in events if e['event']=='rtvi-out' and e['message'].get('data',{}).get('t')=='user-audio']
        r.update(seconds=time.monotonic()-began,jabra_floor_dbfs=floors,server_input_rms_median_dbfs=float(np.median(telemetry)) if telemetry else None,
                 spurious_vad=starts,passed=not starts)
        board.command('audio 0');board.command('disconnect',40)
except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}'
finally:(a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
raise SystemExit(0 if r['passed'] else 1)
