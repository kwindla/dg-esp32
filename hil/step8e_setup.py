#!/usr/bin/env python3
"""Five-minute setup-phase loss arms, selected inside the board IP flow."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from esp_serial import ROOT,EspSerial
from netem import Netem,command as tc
from step7_support import BotServer,pcm,transcribe
from phase2_endurance import snapshot
from arrival_trace import drain
from failure_snapshot import capture_failure
from transport_test import dump
from playback_quality import decoded_match
from step8e_analysis import uplink_packet_summary


SELECTORS={
    'ice':['match','ip','protocol','17','0xff','match','u32','0x2112a442','0xffffffff','at','32'],
    'dtls':['match','ip','protocol','17','0xff','match','u8','22','0xff','at','28'],
    'sctp':['match','ip','protocol','17','0xff','match','u8','23','0xff','at','28'],
}


def arm(out,phase,direction,board_ip):
    out.mkdir(parents=True,exist_ok=False)
    result=dict(phase=phase,direction=direction,loss_hold_s=25,duration_requested_s=300,
                selector=SELECTORS[phase],completed=False,passed=False,samples=[])
    values=dict(BOT_ICE_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',
        RTP_EVIDENCE_DIR=str(out/'rtp'),RTP_EVIDENCE_MAX_PACKETS='100000',
        OFFLINE_SKIP_GREETING='1',OFFLINE_BENCH_CONTINUOUS_AUDIO='1')
    previous={k:os.environ.get(k) for k in values};os.environ.update(values)
    try:
        with Netem(board_ip,['loss','0%'],direction,state=out/'tc-state.json',selector=SELECTORS[phase]) as fault:
            with BotServer(out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
                try:
                    cur=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cur)
                    identity=board.wait_for('elf_sha256=',3,since=cur)
                    assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
                    for cmd in ('server-url http://192.168.7.222:7861','quiet 1','peer-option arrival-trace 1','peer-option call-timing 1'):
                        board.command(cmd)
                    cur=board.cursor;fault.update(['loss','100%']);began=time.monotonic()
                    seq=board.send('connect')
                    time.sleep(25)
                    result['blocked_tc_counters']={dev:json.loads(tc('-s','-j','qdisc','show','dev',dev)) for dev in ('wlp8s0','ifb0')}
                    result['blocked_state_lines']=[l for _,_,l in board.lines_since(cur) if re.search(r'STATE |PHASE |SESSION FAIL|START_READY',l)]
                    fault.update(['loss','0%']);result['released_s']=time.monotonic()
                    ready=board.wait_for_entry('STATE Ready',90,since=cur);result['ready_s']=ready[1]
                    result['connect_ack']=board.wait_for(rf'^(OK|ERR) #{seq}(?:\s|$)',5,since=cur)
                    result['recovery_ms']=1000*(ready[1]-result['released_s'])
                    # Diagnostic fixture requires capture disabled while installing.
                    board.command('audio 0');board.command('mic-tone 1');board.command('gate open');board.command('audio 1')
                    board.command('play-long 6',20)
                    while time.monotonic()-began<300:
                        result['samples'].append(snapshot(board,active=True,allow_incomplete=True))
                        drain(board,out/'arrivals.jsonl')
                        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
                        time.sleep(min(10,max(0,300-(time.monotonic()-began))))
                    result['elapsed_s']=time.monotonic()-began
                    result['state_lines']=[l for _,_,l in board.lines_since(cur) if re.search(r'STATE |PHASE |SESSION FAIL|START_READY',l)]
                    result['completed']=True
                    failures=[int(m[1]) for l in result['state_lines'] if (m:=re.search(r'STATE Disconnected .*t=(\d+)',l))]
                    starts=[int(m[1]) for l in result['state_lines'] if (m:=re.search(r'STATE Starting .*t=(\d+)',l))]
                    result['retry_delays_ms']=[(min(t for t in starts if t>failure)-failure)/1000
                        for failure in failures if any(t>failure for t in starts)]
                    result['retry_under_backoff']=bool(result['retry_delays_ms']) and all(t>=999 for t in result['retry_delays_ms'])
                    result['decoded_content']=decoded_match(dump(board,out),pcm(ROOT/'server/fixtures/reply_pineapple.wav'))
                    try:result['decoded_transcript']=transcribe(out/'rx.wav')
                    except Exception as exc:result['stt_error']=f'{type(exc).__name__}: {exc}'
                    result['passed']=result['completed'] and result['retry_under_backoff']
                except BaseException as exc:
                    result['failure_snapshot']=capture_failure(board,out,exc);raise
                finally:
                    fault.update(['loss','0%'])
                    for cmd in ('audio 0','mic-tone 0','disconnect'):
                        try:board.command(cmd,40)
                        except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
            path=out/'rtp/rtp.jsonl'
            packets=[json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
            result['uplink']=uplink_packet_summary(packets)
            result['rtvi_server_messages']=sum(e['event']=='rtvi-out' for e in bot.events())
            result['rtvi_delivery_basis']='All server audit messages versus last active native callback counter, without per-message acknowledgements; setup retries are separate sessions.'
            if result['samples']:
                result['rtvi_callbacks_received']=result['samples'][-1]['perf']['peer_profile']['data_callback']['calls']
                result['trace_complete']=result['samples'][-1]['perf']['peer_profile']['trace']['overwritten']==0
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('samples','state_lines','blocked_tc_counters')}),flush=True)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--board-ip',default='192.168.4.29');p.add_argument('--only');a=p.parse_args();out=a.out.resolve()
    out.mkdir(parents=True,exist_ok=True);results=[]
    for phase in SELECTORS:
        for direction in ('downlink','uplink','both'):
            label=phase+'-'+direction
            if a.only and a.only!=label:continue
            target=out/label
            if (target/'result.json').exists():
                result=json.loads((target/'result.json').read_text())
                if not result.get('completed'):raise RuntimeError('Failed setup arm retained; use a new output directory: '+label)
            else:result=arm(target,phase,direction,a.board_ip)
            results.append(result)
            (out/'result.json').write_text(json.dumps(results,indent=2)+'\n')
    return 0 if all(r['passed'] for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
