#!/usr/bin/env python3
"""First hardware gate: identity receiver hook versus unchanged native callback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from esp_serial import EspSerial
from step7_support import BotServer
from arrival_trace import drain
from netem import Netem
from transport_test import perf


def qualify(out, board_ip):
    out.mkdir(parents=True, exist_ok=False)
    os.environ.update(BOT_ICE_INTERFACE='wlp8s0', BOT_ICE_REMOTE_NETWORK_ONLY='0',
        RTP_EVIDENCE_DIR=str(out/'rtp'), RTP_EVIDENCE_MAX_PACKETS='20000',
        RTP_EVIDENCE_PAYLOAD_BYTES='1', OFFLINE_SKIP_GREETING='1',
        OFFLINE_BENCH_CONTINUOUS_AUDIO='1', AUDIO_OUT_AUTO_SILENCE='1')
    rows=[]
    capture_log=(out/'tcpdump.log').open('w')
    capture=subprocess.Popen(['tcpdump','-i','wlp8s0','-nn','-s','0','-w',str(out/'board-flow.pcap'),
        'host',board_ip,'and','udp'], stdout=capture_log, stderr=subprocess.STDOUT)
    try:
        with BotServer(out/'bot') as bot, EspSerial('/dev/ttyACM0', log_path=out/'serial.log') as board:
            try:
                cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                board.command('disconnect',45);board.command('audio 0');board.command('quiet 1')
                board.command('server-url http://192.168.7.222:7861')
                board.command('peer-option arrival-trace 1');board.command('audio 1')
                board.command('connect',95);board.command('play-long 3',20)
                phases=[('control',['loss','0%'],30),('loss',['loss','1%'],45),
                    ('reorder',['delay','40ms','reorder','25%','50%'],45)]
                for label,args,seconds in phases:
                    with Netem(board_ip,args,'downlink',state=out/(label+'-tc.json')):
                        began=time.monotonic()
                        while time.monotonic()-began<seconds:
                            time.sleep(3);rows.extend(drain(board,out/'arrivals.jsonl'))
                        (out/(label+'-perf.json')).write_text(json.dumps(perf(board),indent=2))
                board.command('audio 0');board.command('disconnect',45)
                rows.extend(drain(board,out/'arrivals.jsonl'))
            finally:
                try:board.command('disconnect',45);board.command('audio 0');board.command('quiet 0')
                except Exception:pass
    finally:
        if capture.poll() is None:capture.terminate();capture.wait(timeout=10)
        capture_log.close()
    auth={(r['reserved'],r['ssrc'],r['seq'],r['timestamp']):r for r in rows if r['kind']==2}
    hooks=[r for r in rows if r['kind']==13]
    delays=[];unmatched=0;reorder=0;highest={}
    for h in hooks:
        key=(h['reserved'],h['ssrc'],h['seq'],h['timestamp'])
        if key in auth:delays.append((h['device_us']-auth[key]['device_us'])/1000)
        else:unmatched+=1
        source=(h['reserved'],h['ssrc']);old=highest.get(source,h['seq'])
        delta=(h['seq']-old+32768)%65536-32768
        if delta<0:reorder+=1
        if delta>0 or source not in highest:highest[source]=h['seq']
    def stats(x):
        x=sorted(x)
        return {'n':len(x),'p50_ms':x[(len(x)-1)//2], 'p99_ms':x[int((len(x)-1)*.99)],'max_ms':x[-1]} if x else None
    result=dict(hook_packets=len(hooks),unmatched_auth=unmatched,reordered_hook_packets=reorder,
        auth_to_hook=stats(delays),native_cache_ms=200,native_cache_bytes=16384,
        passed=bool(hooks) and not unmatched and reorder>0 and stats(delays)['p99_ms']<5,
        tcpdump_status=capture.returncode)
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--board-ip',default='192.168.4.29');a=p.parse_args()
    raise SystemExit(qualify(a.out.resolve(),a.board_ip))
