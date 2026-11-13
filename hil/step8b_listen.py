"""Leave an owned offline bot and the board connected after the four requested plays."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from esp_serial import EspSerial,ROOT
from step7_support import BotServer,pcm
from alsa import resolve_jabra,set_volumes
from transport_test import perf
p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--target',type=int,required=True);p.add_argument('--ceiling',type=int,default=26);p.add_argument('--connect-only',action='store_true',help='Restore an already-played comparison connection without greeting or playback');a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=False);r={'passed':False,'plays':[]};bot=None
try:
    card,_=resolve_jabra();set_volumes(card,a.out/'mixer.log')
    os.environ['OFFLINE_IDLE_TIMEOUT_SECS']='86400'
    if a.connect_only:os.environ['OFFLINE_SKIP_GREETING']='1'
    bot=BotServer(a.out/'bot',detach=True);bot.start();r['server_pid']=bot.proc.pid
    with EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
        cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
        identity=board.wait_for('elf_sha256=',3,since=cur)
        r['elf_sha256']=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
        assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==r['elf_sha256']
        board.command(f'playback-loudness {a.target} {a.ceiling}')
        cur=connected_cursor=board.cursor;board.command('connect',90);board.command('audio 1')
        if not a.connect_only:
            board.wait_for('GATE closed',10,since=cur)
            time.sleep(len(pcm(ROOT/'server/fixtures/greeting.wav'))/16000+.8)
            board.wait_for('GATE open',15,since=cur)
            r['plays'].append(dict(level='chosen',fixture='greeting.wav',automatic_on_connect=True))
            for level,name in [('chosen','reply_pineapple.wav'),('old','greeting.wav'),('old','reply_pineapple.wav')]:
                set_volumes(card,a.out/f'mixer-{level}-{name}.log')
                if level=='old':board.command('playback-settings 33 26')
                time.sleep(1);cur=board.cursor;board.command(f'play-fixture {name}')
                board.wait_for('GATE closed',10,since=cur)
                time.sleep(len(pcm(ROOT/'server/fixtures'/name))/16000+.8)
                board.wait_for('GATE open',15,since=cur)
                r['plays'].append(dict(level=level,fixture=name));print(level,name,flush=True)
        board.command(f'playback-loudness {a.target} {a.ceiling}')
        cur=board.cursor;r['final_perf']=perf(board)
        assert r['final_perf']['pc_id'] and bot.proc.poll() is None
        session_text='\n'.join(l for _,_,l in board.lines_since(connected_cursor))
        states=re.findall(r'STATE (\w+)',session_text)
        assert states and states[-1]=='Ready' and 'SESSION FAIL' not in session_text
        text='\n'.join(l for _,_,l in board.lines_since(cur))
        assert f'LOUDNESS target_dbfs={a.target}' in text
        assert f'ceiling_percent={a.ceiling}' in text
        if a.connect_only:
            assert not any(e['event']=='fixture' for e in bot.events())
        r.update(passed=True,connected=True,connect_only=a.connect_only,mode='offline-test',port=7861,target_dbfs=a.target,ceiling_percent=a.ceiling)
        # No further playback or disconnect. The requested owned server survives
        # this process; its PID, log and input evidence remain in the handoff.
    (a.out/'server.pid').write_text(str(bot.proc.pid)+'\n')
except Exception as exc:
    r['error']=f'{type(exc).__name__}: {exc}'
    if bot:bot.stop()
finally:(a.out/'result.json').write_text(json.dumps(r,indent=2)+'\n')
raise SystemExit(0 if r['passed'] else 1)
