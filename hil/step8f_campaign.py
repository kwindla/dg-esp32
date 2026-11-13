#!/usr/bin/env python3
"""Predeclared six-pair event-wake crossover on one production image."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import time
import numpy as np
from esp_serial import ROOT,EspSerial
from step7_support import BotServer
from ethernet_bench import ethernet_bench
from netem import Netem
from quiet_nic import QuietNic,wait_for_quiet
from arrival_trace import drain
from transport_test import perf
from step8e2b_analysis import console_windows,device_send_cadence,owner_gate,lcd_memory_gate
from uplink_continuity import link_tail


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def read_perf(board):
    began=time.monotonic();sample=perf(board);sample['host_before_s']=began;sample['host_after_s']=time.monotonic();return sample


def arm(path,elf,wake):
    path.mkdir();wait_for_quiet(path/'quiet-preflight.json')
    values=dict(OFFLINE_SKIP_GREETING='1',RTP_EVIDENCE_DIR=str(path/'rtp'),
                RTP_EVIDENCE_MAX_PACKETS='100000',OFFLINE_IDLE_TIMEOUT_SECS='2100')
    previous={k:os.environ.get(k) for k in values};os.environ.update(values)
    result=dict(event_wake=wake,completed=False,strata={})
    try:
        from alsa import record_room_baseline
        result['room_baseline']=record_room_baseline(path/'room-baseline',path.name)
        with Netem('192.168.4.29',['loss','0%'],'both',state=path/'tc-state.json',account_both=True),QuietNic(path/'quiet-nic.json'),ethernet_bench(path/'ethernet'),BotServer(path/'bot') as bot,EspSerial('/dev/ttyACM0',log_path=path/'serial.log') as board:
            try:
                cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                identity=board.wait_for('elf_sha256=',3,since=cursor)
                assert hashlib.sha256(elf.read_bytes()).hexdigest() in identity
                for cmd in ('server-url '+os.environ['BOT_BENCH_SERVER_URL'],f'peer-option event-wake {wake}',
                            'peer-option call-timing 1','peer-option arrival-trace 1','quiet 1',
                            'mic-fixture 1','gate open','connect','audio 1'):
                    board.command(cmd,90 if cmd=='connect' else 10)
                board.command('play-long 1',30);began=time.monotonic();time.sleep(5)
                for name in ('busy','quiet'):
                    if name=='quiet':
                        time.sleep(max(0,began+65-time.monotonic()))
                        board.command('gate closed')
                    snapshots=[read_perf(board)];drain(board,path/'arrivals.jsonl')
                    for _ in range(2):
                        deadline=time.monotonic()+20
                        while time.monotonic()<deadline:
                            time.sleep(min(5,deadline-time.monotonic()));drain(board,path/'arrivals.jsonl')
                        snapshots.append(read_perf(board))
                    result['strata'][name]=dict(snapshots=snapshots)
                    save(path/'result.json',result)
                    print(path.name+' '+name+' captured',flush=True)
                board.command('wifi-stats',10);drain(board,path/'arrivals.jsonl')
                result['completed']=True
            finally:
                for cmd in ('audio 0','heap-sample-compare 0','gate auto','mic-fixture 0','quiet 1','disconnect'):
                    try:board.command(cmd,40)
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
        trace=[json.loads(l) for l in (path/'arrivals.jsonl').read_text().splitlines()]
        packets=[json.loads(l) for l in (path/'rtp/rtp.jsonl').read_text().splitlines()]
        windows=console_windows(path/'serial.log')
        for name,data in result['strata'].items():
            first,last=data['snapshots'][0],data['snapshots'][-1]
            start,end=first['device_us'],last['device_us']
            data['device_cadence']=device_send_cadence(trace,start,end,windows,snapshots=data['snapshots'])
            slow=[r for r in trace if r['kind']==8 and start<=r['device_us']<=end]
            calls=last['peer_profile']['loop_iteration']['calls']-first['peer_profile']['loop_iteration']['calls']
            data['owner']=owner_gate(slow,calls,covered=last['peer_profile']['trace']['overwritten']==0,trace=trace,snapshots=data['snapshots'])
            # RTP endpoints bracket the device snapshots through host receive stamps.
            rows=[r for r in packets if r['pc_id']==last['pc_id']]
            data['link']=link_tail(rows,first['host_after_s'],last['host_before_s'])
            data['counts']={k:last[k]-first[k] for k in ('late','lost','concealed','send_errors','queue_dropped')}
            cpu=defaultdict(list)
            for snapshot in data['snapshots'][1:]:
                for task in snapshot['tasks']:
                    if task['cpu_percent'] is not None:cpu[task['name']].append(task['cpu_percent'])
            data['cpu']={k:float(np.mean(v)) for k,v in cpu.items()}
        result['quiet_nic']=json.loads((path/'quiet-nic.json').read_text())
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        save(path/'result.json',result)
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
    return result


def per_task_cpu(data):
    values=defaultdict(list)
    for snapshot in data['snapshots'][1:]:
        workers=sorted((r for r in snapshot['tasks'] if r['name']=='pipecat'),key=lambda r:r['id'])
        roles={r['id']:('pipecat-event-loop' if i==0 else 'pipecat-request-timer') for i,r in enumerate(workers)}
        for task in snapshot['tasks']:
            if task['cpu_percent'] is not None:values[roles.get(task['id'],task['name'])].append(task['cpu_percent'])
    return {name:float(np.mean(samples)) for name,samples in values.items()}


def summarize(runs,rule):
    for run in runs:
        for data in run['strata'].values():
            data['cpu']=per_task_cpu(data)
            data['lcd_memory']=lcd_memory_gate(data['snapshots'],reconnects=None)
    strata={};adopt=True
    for name in ('busy','quiet'):
        pairs=[(runs[2*i]['strata'][name],runs[2*i+1]['strata'][name]) for i in range(6)]
        task_names=set.intersection(*(set(a['cpu'])&set(b['cpu']) for a,b in pairs))
        cpu={k:dict(A=[a['cpu'][k] for a,b in pairs],B=[b['cpu'][k] for a,b in pairs],
            pair_deltas=[b['cpu'][k]-a['cpu'][k] for a,b in pairs],
            mean_pair_delta=float(np.mean([b['cpu'][k]-a['cpu'][k] for a,b in pairs]))) for k in sorted(task_names)}
        cadence=[b['device_cadence']['gaps_ms']['p99']-a['device_cadence']['gaps_ms']['p99'] for a,b in pairs]
        cpu_ok=all(v['mean_pair_delta']<=2 if not k.startswith('IDLE') else v['mean_pair_delta']>=-5 for k,v in cpu.items())
        healthy=all(b['device_cadence']['hard_ceiling_passed'] and b['owner']['passed'] for a,b in pairs)
        network_ok=all(b['counts']['send_errors']==0 for a,b in pairs) and sum(b['counts']['late'] for a,b in pairs)<=sum(a['counts']['late'] for a,b in pairs)+6
        improved=float(np.mean(cadence))<=-2 and all(b['device_cadence']['gaps_ms']['p99']<=40 for a,b in pairs)
        strata[name]=dict(cpu=cpu,cadence_pair_deltas_ms=cadence,mean_pair_delta_ms=float(np.mean(cadence)),
            cpu_passed=cpu_ok,healthy_passed=healthy,network_passed=network_ok,cadence_improved=improved)
        adopt &= cpu_ok and healthy and network_ok and improved
    quiet_valid=all(run.get('quiet_nic',{}).get('valid') is True for run in runs)
    return dict(adopt_event_wake=bool(adopt and quiet_valid),quiet_nic_all_valid=quiet_valid,strata=strata,rule=rule,
                statistic='Mean of six adjacent B-minus-A boot deltas; two within-boot windows averaged, busy/quiet strata separate. No retakes.')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    rule=dict(order=[f'{arm}{i}' for i in range(1,7) for arm in 'AB'],boots_per_arm=6,
        image_sha256=hashlib.sha256(a.elf.read_bytes()).hexdigest(),
        A_event_wake=0,B_event_wake=1,busy='fixed microphone speech, continuous offline reply',quiet='gated digital silence, reply ended, connected',
        windows_per_stratum=2,window_seconds=20,adoption='Both strata mean paired p99 delta <= -2 ms; each task CPU mean paired increase <=2 pp and IDLE decrease <=5 pp; B p99 <=40 ms, healthy send ceiling <=60 ms and owner <=40 ms; B late total <=A+6, no send errors; every arm quiet-NIC valid.',
        cadence_target_before_ab_ms=45,cadence_target_if_adopted_ms=40)
    save(out/'predeclared-rule.json',rule);runs=[]
    for label in rule['order']:
        print('START '+label,flush=True)
        row=arm(out/label,a.elf.resolve(),int(label[0]=='B'));runs.append(row)
        save(out/'progress.json',dict(completed=len(runs),order=rule['order'][:len(runs)]))
    save(out/'summary.json',summarize(runs,rule))


if __name__=='__main__':main()
