#!/usr/bin/env python3
"""Short causal transport probes; run under bench.py, never a soak/qualification."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from contextlib import nullcontext
from esp_serial import ROOT, EspSerial
from step7_support import BotServer
from transport_test import perf
from arrival_trace import drain

class TimedSerial(EspSerial):
    def command(self, command, timeout=5):
        start=time.monotonic()
        try:
            return super().command(command,timeout)
        finally:
            with (self.log_path.parent/'commands.jsonl').open('a') as f:
                f.write(json.dumps(dict(command=command,start=start,end=time.monotonic()))+'\n')

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path)
    p.add_argument('--mode',choices=('logging','tone','tone-guard','tail','dump'),default='logging');p.add_argument('--packet-capture',action='store_true');p.add_argument('--display',choices=('cat',));a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    os.environ['RTP_EVIDENCE_DIR']=str(a.out/'rtp')
    os.environ['STEP8E_EVENT_LOOP_PROBE']='1'
    result=dict(qualification=False,phases=[]);capture=None;capture_log=None
    origin=nullcontext()
    if a.display:
        os.environ['STEP9A_FIXTURES']=str(ROOT/'hil/fixtures/echoear360')
        os.environ['STEP9A_BUDGET_PATH']=str(a.out/'arena-budget.json')
        from step9a_assets import asset_server
        origin=asset_server()
    try:
        with origin, BotServer(a.out/'bot') as bot, TimedSerial(log_path=a.out/'serial.log',display_checks=bool(a.display)) as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            identity=board.wait_for(r'elf_sha256=',3,since=cursor)
            assert hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest() in identity,'ELF mismatch'
            if a.display:
                heap=perf(board)['heap']['psram']
                budget=dict(budget=491520,F=heap['free'],W=1287760,largest=heap['largest'])
                (a.out/'arena-budget.json').write_text(json.dumps(budget)+'\n')
                from step9a_support import setup
                result['display_setup']=setup(board,mode=a.display)
            for cmd in ('audio 0','quiet 1','mic-tone 1','gate open','peer-option arrival-trace 1','connect'):
                board.command(cmd,60)
            if a.packet_capture:
                pairs=[json.loads(line) for line in (a.out/'rtp/ice-pairs.jsonl').read_text().splitlines()]
                remote=pairs[-1]['pairs'][0]['remote'][0]
                capture_log=(a.out/'packet-capture.log').open('w')
                result['packet_clock']=dict(wall_s=time.time(),monotonic_s=time.monotonic())
                capture=subprocess.Popen(['dumpcap','-i','any','-p','-f',f'udp and host {remote}',
                    '-s','128','-a','duration:90','-w',str(a.out/'packets.pcapng')],stdout=capture_log,stderr=subprocess.STDOUT)
                time.sleep(.5)
                assert capture.poll() is None,'Packet capture failed; see packet-capture.log'
            board.command('audio 1');time.sleep(4)
            phases=[('quiet1',1,False),('quiet1-stats',1,True),('quiet0',0,False),('quiet0-stats',0,True)] if a.mode=='logging' else [('control',1,False),('direct-tone',1,False),('fixture',1,False)]
            if a.mode=='tone-guard':phases=[('direct-tone',1,False)]
            if a.mode=='tail':phases=[('fixture-tail',1,False)]
            if a.mode=='dump':
                board.command('mic-record 1')
                phases=[('control',1,False),('dump-rx',1,False),('dump-mic',1,False)]
            for name,quiet,stats in phases:
                board.command(f'quiet {quiet}');before=perf(board)
                drain(board,a.out/'arrivals.jsonl');time.sleep(1)
                row=dict(name=name,before=before,start=time.monotonic());result['phases'].append(row)
                if name=='direct-tone':
                    cursor=board.cursor
                    try:
                        board.command('tone 1500 1200 20');board.wait_for(r'^DONE #',5,since=cursor)
                        row['direct_tone_rejected']=False
                    except RuntimeError as exc:
                        if not re.fullmatch(r'ERR #\d+ tone',str(exc)):raise
                        row['direct_tone_rejected']=True;row['tone_response']=str(exc)
                elif name in ('fixture','fixture-tail'):board.command('play-fixture reply_pineapple.wav')
                time.sleep(3)
                if name=='dump-rx':board.command('dump-rx',40)
                if name=='dump-mic':
                    board.command('mic-record 0');board.command('dump-mic',40)
                if stats:perf(board)
                time.sleep(max(0,row['start']+(20 if a.mode=='tail' else 7)-time.monotonic()))
                row['end']=time.monotonic();row['after']=perf(board)
                drain(board,a.out/'arrivals.jsonl')
                print(name,{k:row['after'].get(k,0)-before.get(k,0) for k in ('lost','concealed','i2s_underflows','i2s_write_errors','send_errors')},flush=True)
                (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            if a.display:
                result['display_monitor']=board.display_monitor.result()
                board.display_cleanup=True;board.command('disp bench off')
            for cmd in ('audio 0','mic-tone 0','gate auto','quiet 1','disconnect'):
                board.command(cmd,40)
    except BaseException as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if capture:
            capture.terminate();capture.wait(timeout=10);capture_log.close()
        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':main()
