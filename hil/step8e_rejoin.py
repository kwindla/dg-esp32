#!/usr/bin/env python3
"""Physical WiFi rejoins; DHCP lease administration is outside this bench."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from esp_serial import ROOT,EspSerial
from step7_support import BotServer
from transport_test import perf
from phase2_endurance import snapshot
from failure_snapshot import capture_failure


def rejoin_sequence(lines):
    """One WiFi rejoin may include justified, serial setup retries."""
    states=[];failures=[];wifi_ready=[]
    for index,line in enumerate(lines):
        match=re.search(r'STATE (\w+) gen=(\d+).* t=(\d+)',line)
        if match:states.append(dict(index=index,state=match[1],generation=int(match[2]),us=int(match[3])))
        match=re.search(r'SESSION FAIL gen=(\d+).*retry_ms=(\d+)',line)
        if match:failures.append(dict(index=index,generation=int(match[1]),retry_ms=int(match[2])))
        if 'WIFI READY' in line:wifi_ready.append(index)
    starts=[s for s in states if s['state']=='Starting']
    retries=[]
    for before,after in zip(starts,starts[1:]):
        failed=[f for f in failures if before['index']<f['index']<after['index'] and f['generation']==before['generation']]
        closed=[s for s in states if before['index']<s['index']<after['index'] and s['state']=='Disconnected']
        valid=len(failed)==1 and len(closed)==1 and after['generation']>closed[0]['generation']>before['generation']
        elapsed=(after['us']-closed[0]['us'])/1000 if closed else None
        valid=valid and elapsed>=failed[0]['retry_ms']-25
        retries.append(dict(justified=valid,elapsed_ms=elapsed,failure=failed[0] if failed else None))
    passed=bool(starts and len(wifi_ready)==1 and starts[0]['index']>wifi_ready[0]
        and sum(s['state']=='WifiConnecting' for s in states)==1
        and sum(s['state']=='Ready' for s in states)==1 and states[-1]['state']=='Ready'
        and states[-1]['generation']==starts[-1]['generation'] and all(r['justified'] for r in retries))
    return dict(passed=passed,start_attempts=len(starts),retries=retries,states=states,
                basis='One WiFi-down/up cycle, one final Ready; extra starts require a matching failure, intervening Disconnected and logged backoff')


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);result=dict(passed=False,rejoins=[])
    os.environ.update(BOT_ICE_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',OFFLINE_SKIP_GREETING='1')
    try:
        with BotServer(out/'bot'),EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
            try:
                cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
                identity=board.wait_for('elf_sha256=',3,since=cur)
                assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
                board.command('server-url http://192.168.7.222:7861');board.command('connect',90)
                result['baseline_resource']=snapshot(board,active=True,allow_incomplete=True)
                for index in range(5):
                    before=perf(board);cur=board.cursor;began=time.monotonic();board.command('wifi-drop',10)
                    ready=board.wait_for_entry('STATE Ready',90,since=cur);time.sleep(3);after=perf(board)
                    lines=[l for _,_,l in board.lines_since(cur)]
                    sequence=rejoin_sequence(lines)
                    result['rejoins'].append(dict(index=index,before=before,after=after,
                        duration_s=ready[1]-began,state_lines=[l for l in lines if re.search(r'STATE |WIFI READY|got ip|SESSION FAIL',l)],
                        sequence=sequence,passed=after['pc_id']!=before['pc_id'] and sequence['passed']))
                    result['rejoins'][-1]['resource_after']=snapshot(board,active=True,allow_incomplete=True)
                    if not result['rejoins'][-1]['passed']:
                        result['rejoins'][-1]['failure_snapshot']=capture_failure(board,out/f'cycle-{index}',
                            'Rejoin sequence includes an unexplained restart or violates retry backoff')
                    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
            except BaseException as exc:
                result['failure_snapshot']=capture_failure(board,out,exc);raise
            finally:
                board.command('audio 0');board.command('disconnect',40)
                result['closed_resource']=snapshot(board,allow_incomplete=True)
        result['passed']=len(result['rejoins'])==5 and all(r['passed'] for r in result['rejoins'])
        result['ip_change_qualification']='Not exercised: no router/DHCP lease administration, and sudo authorization is limited to tc. Same-IP physical rejoins complement the exact IP-changing ordering host test.'
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='rejoins'}),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
