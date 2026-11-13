#!/usr/bin/env python3
"""Run step-8c voice gates with an archived image and owned one-hop STUN.

Addresses remain arguments and RAM console settings. This wrapper never
changes host networking; port 7860 is not used.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback

from esp_serial import ROOT, EspSerial
from phase2_campaign import flash


def qualify(a):
    out=a.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False,mode=a.mode,image=str(a.image.resolve()),
                voice_path='ethernet',options=a.option)
    responder=None
    flashed=False
    try:
        with socket.socket() as check:
            if check.connect_ex(('127.0.0.1',7861))==0:
                raise RuntimeError('Port 7861 occupied before owned qualification')
        with (out/'stun-command.log').open('w') as log:
            responder=subprocess.Popen([sys.executable,str(ROOT/'hil/netprobe.py'),
                '--stun-bind',a.ethernet_host,'--duration','3600',
                '--out',str(out/'stun.jsonl')],stdout=log,stderr=subprocess.STDOUT)
            time.sleep(.3)
            if responder.poll() is not None:raise RuntimeError('Owned STUN responder exited')
            flash(a.image.resolve(),out/'flash.log')
            flashed=True
            target=out/'test'
            scripts={'offline':'e2e_offline_test.py','live':'e2e_voice_test.py','reconnect':'app_reconnect_test.py','soak':'soak_test.py'}
            command=[sys.executable,str(ROOT/'hil'/scripts[a.mode]),'--out',str(target),
                     '--elf',str(a.image.resolve()/'dg-esp32.elf'),
                     '--server-url','http://'+a.ethernet_host+':7861',
                     '--ice-servers','stun:'+a.ethernet_host+':3478']
            if a.mode in ('offline','live'):command+=['--runs','5','--spl',str(a.spl)]
            elif a.mode=='reconnect':command+=['--cycles','50']
            else:command+=['--minutes','30','--option','arrival-trace=1',
                           '--memory-tail-seconds',str(getattr(a,'memory_tail_seconds',180))]
            for option in a.option:command+=['--option',option]
            env={**os.environ,'BOT_ICE_INTERFACE':a.ethernet_interface,
                 'BOT_ICE_REMOTE_NETWORK_ONLY':'1','BOT_ICE_DEBUG_PATH':str(out/'ice-debug.log'),
                 'RTP_EVIDENCE_DIR':str(out/'rtp'),'RTP_EVIDENCE_MAX_PACKETS':'200000',
                 'RTP_EVIDENCE_PAYLOAD_BYTES':'0'}
            # Soak owns its own RTP directory to keep its existing attribution
            # choreography intact. Other voice gates share this wrapper's dir.
            pairs_root=(target if a.mode=='soak' else target/'rtp' if a.mode in ('offline','live') else out/'rtp')
            pairs_file=pairs_root/'ice-pairs.jsonl'
            with (out/'test-command.log').open('w') as log:
                completed=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            result['test_exit_code']=completed.returncode
            result['test_result']=json.loads((target/'result.json').read_text())
            pairs=[json.loads(line) for line in pairs_file.read_text().splitlines()]
            result['nominated_pairs']=pairs
            path_ok=bool(pairs) and all(record['interface']==a.ethernet_interface and record['pairs'] and
                all(pair['local'][0]==a.ethernet_host for pair in record['pairs']) for record in pairs)
            result['one_hop_path_verified']=path_ok
            result['passed']=completed.returncode==0 and result['test_result']['passed'] and path_ok
    except Exception:
        (out/'FAILED.txt').write_text(traceback.format_exc())
        result['error']='Qualification failed; see retained FAILED.txt and raw logs'
    finally:
        if responder and responder.poll() is None:
            responder.terminate()
            try:responder.wait(timeout=15)
            except subprocess.TimeoutExpired:responder.kill();responder.wait()
        if flashed:
            try:
                with EspSerial('/dev/ttyACM0',log_path=out/'cleanup-serial.log') as board:
                    for command in ('audio 0','disconnect','netprobe stop'):
                        board.command(command,timeout=45)
                result['board_cleanup_complete']=True
            except Exception:
                (out/'cleanup-FAILED.txt').write_text(traceback.format_exc())
                result['board_cleanup_complete']=False
                result['passed']=False
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(a.mode+': '+('passed' if result['passed'] else 'failed; evidence retained'),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--image',type=Path,required=True,help='Archived ELF, app, bootloader and partition binaries')
    parser.add_argument('--ethernet-host',required=True)
    parser.add_argument('--ethernet-interface',default='enp7s0')
    parser.add_argument('--mode',choices=('offline','live','reconnect','soak'),required=True)
    parser.add_argument('--option',action='append',default=[])
    parser.add_argument('--spl',type=float,choices=(65,58),default=65)
    parser.add_argument('--memory-tail-seconds',type=int,choices=(0,90,180),default=180)
    raise SystemExit(qualify(parser.parse_args()))
