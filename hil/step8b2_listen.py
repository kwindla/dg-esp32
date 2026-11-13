#!/usr/bin/env python3
"""Announced, recorded comparison: step 8, step 8b's old limiter, chosen step 8b2.

The archived pre-8b2 firmware supplies the first two levels, so the old limiter
is reproduced exactly. The current firmware is always restored, even on error.
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import time
import wave
from pathlib import Path
import requests
import numpy as np
from alsa import resolve_jabra,set_volumes,record_wav,play_wav
from bench_calibration import playback
from esp_serial import ROOT,EspSerial,port_lock
from step7_support import BotServer,pcm,deepgram_key

DEFAULT_EVIDENCE=ROOT.parent/'evidence/step8b2'

def flash_image(image,out):
    with port_lock(),out.open('w') as log:
        subprocess.run(['bash','-c','. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null && python -m esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 write-flash 0x10000 "$1"','flash',str(image)],
            stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)

def announcements(out):
    out.mkdir(parents=True,exist_ok=True)
    texts={'step8':'Step eight. Original playback level.',
        'step8b':'Step eight B. Previous normalization and limiter.',
        'step8b2':'Step eight B two. New chosen playback level.'}
    for name,text in texts.items():
        path=out/f'{name}.wav'
        if not path.exists():
            key=deepgram_key()
            if not key:raise RuntimeError('Announcements require the approved Deepgram credential')
            response=requests.post('https://api.deepgram.com/v1/speak',params=dict(model='aura-2-thalia-en',encoding='linear16',container='wav',sample_rate=16000),
                headers={'Authorization':'Token '+key},json={'text':text},timeout=45)
            if response.status_code!=200:raise RuntimeError(f'Announcement TTS HTTP {response.status_code}')
            with wave.open(io.BytesIO(response.content)) as source:
                x=np.frombuffer(source.readframes(source.getnframes()),'<i2').astype(float)
            playback(path,x*.25)
    return {name:out/f'{name}.wav' for name in texts}

def play_levels(board,device,card,out,levels,expected_hash,announcement_paths):
    cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
    identity=board.wait_for('elf_sha256=',3,since=cur)
    assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==expected_hash,'Comparison firmware mismatch'
    board.command('connect',90);board.command('audio 0')
    rows=[]
    for name,setting in levels:
        set_volumes(card,out/f'{name}-mixer.log')
        print(f'Playing {name}: {setting}',flush=True)
        play_wav(announcement_paths[name],device);time.sleep(.6)
        board.command(setting)
        reference=ROOT/'server/fixtures/greeting.wav';seconds=len(pcm(reference))/16000
        with record_wav(out/f'{name}-jabra.wav',device,int(seconds)+5,16000) as rec:
            time.sleep(.6);cursor=board.cursor;board.command('play-fixture greeting.wav')
            board.wait_for('GATE closed',10,since=cursor);time.sleep(seconds+.8)
            board.wait_for('GATE open',15,since=cursor);rec.wait()
        rows.append(dict(level=name,setting=setting,fixture='greeting.wav',recording=str(out/f'{name}-jabra.wav'),elf_sha256=expected_hash))
    return rows

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--selection',type=Path,default=DEFAULT_EVIDENCE/'selection.json')
    p.add_argument('--old-bin',type=Path,default=DEFAULT_EVIDENCE/'start.bin');p.add_argument('--old-elf',type=Path,default=DEFAULT_EVIDENCE/'start.elf')
    p.add_argument('--new-bin',type=Path,default=ROOT/'build/dg-esp32.bin');p.add_argument('--new-elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    p.add_argument('--announcements',type=Path,default=DEFAULT_EVIDENCE/'announcements');p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--leave-connected',action='store_true');a=p.parse_args()
    selected=json.loads(a.selection.read_text());target=selected['target_dbfs'];ceiling=selected['ceiling_percent']
    paths=announcements(a.announcements.resolve())
    if a.prepare_only:print('Announcements and firmware comparison ready.');return 0
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False);r=dict(passed=False,plays=[]);bot=None;old_flashed=False
    try:
        card,device=resolve_jabra();os.environ.update(OFFLINE_SKIP_GREETING='1',OFFLINE_IDLE_TIMEOUT_SECS='86400')
        bot=BotServer(a.out/'bot',detach=a.leave_connected);bot.start()
        flash_image(a.old_bin.resolve(),a.out/'flash-old.log');old_flashed=True
        with EspSerial('/dev/ttyACM0',log_path=a.out/'serial-old.log') as board:
            r['plays']+=play_levels(board,device,card,a.out,[('step8','playback-settings 33 26'),('step8b','playback-loudness -25 26')],
                hashlib.sha256(a.old_elf.read_bytes()).hexdigest(),paths)
            board.command('disconnect',40)
        flash_image(a.new_bin.resolve(),a.out/'flash-new.log');old_flashed=False
        with EspSerial('/dev/ttyACM0',log_path=a.out/'serial-new.log') as board:
            r['plays']+=play_levels(board,device,card,a.out,[('step8b2',f'playback-loudness {target} {ceiling}')],
                hashlib.sha256(a.new_elf.read_bytes()).hexdigest(),paths)
            if not a.leave_connected:board.command('disconnect',40)
        if a.leave_connected:(a.out/'server.pid').write_text(str(bot.proc.pid)+'\n')
        r.update(passed=True,target_dbfs=target,ceiling_percent=ceiling,connected=a.leave_connected)
    except Exception as exc:r['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if bot and (not a.leave_connected or not r['passed']):bot.stop()
        if old_flashed:flash_image(a.new_bin.resolve(),a.out/'restore-new.log')
        (a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
    return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
