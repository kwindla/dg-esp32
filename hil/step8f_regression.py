#!/usr/bin/env python3
"""Quiet-0 stats collision regression and owner/all-loop sampler comparison."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from esp_serial import ROOT,EspSerial
from step7_support import BotServer
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import QuietNic,wait_for_quiet
from arrival_trace import drain
from transport_test import perf
from step8e2b_analysis import console_windows,device_send_cadence,perf_history


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    values=dict(OFFLINE_SKIP_GREETING='1',RTP_EVIDENCE_DIR=str(out/'rtp'),RTP_EVIDENCE_MAX_PACKETS='100000')
    prior={k:os.environ.get(k) for k in values};os.environ.update(values)
    result=dict(passed=False)
    try:
        from alsa import record_room_baseline
        result['room_baseline']=record_room_baseline(out/'room-baseline',out.name)
        if result['room_baseline']['noise_contaminated']:
            result['acoustic_hold']='noise'
            print('ACOUSTIC HOLD: noise',flush=True)
            return 2
        wait_for_quiet(out/'quiet-preflight.json')
        with Netem('192.168.4.29',['loss','0%'],'both',state=out/'tc-state.json',account_both=True),QuietNic(out/'quiet-nic.json'),ethernet_bench(out/'ethernet'),BotServer(out/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
            try:
                start=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=start)
                assert hashlib.sha256(a.elf.read_bytes()).hexdigest() in board.wait_for('elf_sha256=',3,since=start)
                result['idle_perf']=perf(board)
                for cmd in ('server-url '+os.environ['BOT_BENCH_SERVER_URL'],
                            'peer-option call-timing 1','peer-option arrival-trace 1',
                            'mic-fixture 1','gate open','connect','audio 1','quiet 0','heap-sample-compare 1','play-long 2'):
                    board.command(cmd,90 if cmd=='connect' else 30)
                result['before']=perf(board);result['samples']=[]
                for i in range(12):
                    time.sleep(5);result['samples'].append(perf(board));drain(board,out/'arrivals.jsonl')
                    print('quiet0 stats sample '+str(i+1),flush=True)
                result['after']=perf(board)
                drain(board,out/'arrivals.jsonl')
            finally:
                for cmd in ('heap-sample-compare 0','audio 0','mic-fixture 0','gate auto','quiet 1','disconnect'):
                    try:board.command(cmd,40)
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
        trace=[json.loads(l) for l in (out/'arrivals.jsonl').read_text().splitlines()]
        result['cadence']=device_send_cadence(trace,result['before']['device_us'],result['after']['device_us'],console_windows(out/'serial.log'),snapshots=perf_history(out/'serial.log'))
        comparisons=[s['heap_sampling_comparison'] for s in result['samples'] if 'heap_sampling_comparison' in s]
        result['sampling_comparisons']=comparisons
        result['sampling_equal']=bool(comparisons) and all(r['owner_minima']==r['all_loop_minima'] for r in comparisons)
        result['sampling_max_delta_bytes']=[max((r['owner_minima'][i]-r['all_loop_minima'][i] for r in comparisons),default=None) for i in range(3)]
        result['console_regression_passed']=result['cadence']['raw_over60ms']==0
        result['passed']=result['console_regression_passed'] and result['cadence']['target_passed']
        result['diagnostic_log_drops']=result['after']['diagnostic_log_drops']-result['before']['diagnostic_log_drops']
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        for k,v in prior.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('samples','before','after','idle_perf','sampling_comparisons','cadence')}),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
