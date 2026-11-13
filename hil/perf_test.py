#!/usr/bin/env python3
"""Short peer-loop investigation; saved image, all samples retained."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from esp_serial import ROOT,EspSerial
from step7_support import BotServer
from phase2_endurance import snapshot
from arrival_trace import drain,attribute
from step8_summary import profile,duplex_coverage

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    p.add_argument('--seconds',type=int,default=20);p.add_argument('--option',action='append',default=[])
    p.add_argument('--playback-target',type=int,help='Same runtime DSP target in both comparison images')
    p.add_argument('--server-url',help='RAM-only bench server override')
    p.add_argument('--ice-servers',help='RAM-only bench STUN override')
    p.add_argument('--no-trace',action='store_true');a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'phases':[],'options':a.option};rows=[]
    try:
        os.environ.update(RTP_EVIDENCE_DIR=str(out),RTP_EVIDENCE_MAX_PACKETS='100000',OFFLINE_IDLE_TIMEOUT_SECS='2100',
            OFFLINE_BENCH_CONTINUOUS_AUDIO='1')
        result['stimulus']='Continuous offline fixture with user interruptions disabled for forced-open duplex mic'
        with BotServer(out/'bot',ice_servers=(a.ice_servers,) if a.ice_servers else ()) as bot,EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            identity=board.wait_for('elf_sha256=',3,since=cursor)
            assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==hashlib.sha256(a.elf.read_bytes()).hexdigest()
            if a.server_url:board.command('server-url '+a.server_url)
            for option in a.option:
                name,value=option.split('=');board.command(f'peer-option {name} {int(value)}')
            board.command('connect',90);time.sleep(5);board.command('quiet 1')
            if a.playback_target is not None:board.command(f'playback-loudness {a.playback_target} 26')
            for name in ['idle','duplex']:
                board.command('gate closed' if name=='idle' else 'gate open');board.command('audio 1')
                if name=='duplex':board.command('play-long 1',30)
                before=snapshot(board,active=True,allow_incomplete=True);began=time.monotonic()
                while time.monotonic()-began<a.seconds:time.sleep(max(0,min(1,a.seconds-(time.monotonic()-began))))
                after=snapshot(board,active=True,allow_incomplete=True)
                result['phases'].append(dict(name=name,before=before,after=after,elapsed_s=time.monotonic()-began))
                if not a.no_trace:rows.extend(drain(board,out/'arrivals.jsonl'))
                (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                print(f'{name} done: peer profile={after["perf"].get("peer_profile",{})}',flush=True)
            board.command('audio 0');board.command('disconnect',40)
            if not a.no_trace:rows.extend(drain(board,out/'arrivals.jsonl'))
        server=[json.loads(s) for s in (out/'downlink.jsonl').read_text().splitlines()]
        result['server_audio_configs']=[e for e in bot.events() if e['event']=='audio-output-config']
        assert result['server_audio_configs'] and all(e.get('continuous_bench') for e in result['server_audio_configs']), \
            'Server did not establish the continuous duplex bench stimulus'
        result['attribution']=attribute(rows,server) if rows else None
        result['traffic_coverage']=duplex_coverage(profile(out/'result.json'))
        assert result['traffic_coverage']['passed'],'Incomplete continuous duplex stimulus'
        result['passed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
