#!/usr/bin/env python3
"""60-second continuous decoded bot fixture; retain depth trend and counters."""
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
import numpy as np
from esp_serial import ROOT, EspSerial
from transport_test import wait_until, perf, dump, fixture
from step6b_checks import playout_checks

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    out=a.out.resolve();proc=None;result={'passed':False,'samples':[]}
    try:
        with socket.socket() as probe: assert probe.connect_ex(('127.0.0.1',7861))!=0,'7861 occupied'
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')};env.update(BOT_MODE='offline-test',SERVER_PORT='7861',EVENTS_PATH=str(out/'events.jsonl'),PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
        with (out/'server.log').open('w') as log:
            proc=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),'bot.py','--host','0.0.0.0','--port','7861'],cwd=ROOT/'server',env=env,stdout=log,stderr=subprocess.STDOUT)
        def up():
            try:return urllib.request.urlopen('http://127.0.0.1:7861/',timeout=.5).status<500
            except Exception:return False
        wait_until(up,30)
        with EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
            time.sleep(.2);cursor=board.cursor;board.reset();board.wait_for('READY transport-console',40,since=cursor)
            boot=board.wait_for('elf_sha256=',3,since=cursor)
            assert re.search(r'elf_sha256=([0-9a-f]{64})',boot)[1]==hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
            board.command('connect',timeout=30);time.sleep(4);board.command('audio 1');board.command('play-long',timeout=15)
            started=time.monotonic();base=perf(board)
            for i in range(31):
                if i: time.sleep(max(0,started+i*2-time.monotonic()))
                row=perf(board);result['samples'].append(dict(elapsed_s=time.monotonic()-started,**row))
            board.command('audio 0');board.command('disconnect',timeout=15)
            result['decoded_fixture']=fixture(dump(board,out),'reply_pineapple.wav')
        rows=result['samples'];last=rows[-1];elapsed=(last['pull_span_us']-base['pull_span_us'])/1e6
        outputs=last['pulled_frames']-base['pulled_frames'];arrivals=last['received']-base['received']
        t=np.array([r['elapsed_s'] for r in rows]);depth=np.array([r['playout']['physical_occupancy'] for r in rows])
        slope=float(np.polyfit(t,depth,1)[0])
        result.update(sender_i2s_count_ratio=arrivals/outputs,depth_slope_frames_per_s=slope,
                      sender_i2s_depth_ratio=1+slope/50,observed_pull_rate_hz=outputs/elapsed,
                      ratio_resolution=1/outputs,base=base,final=last)
        playout_checks(last);assert last['drift_drops']==base['drift_drops'],'Drift drops during minute'
        assert result['decoded_fixture']['correlation']>=.95
        assert 59.9<=t[-1]<=65
        result['passed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if proc:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='samples'},indent=2));return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
