#!/usr/bin/env python3
"""Same fixed-PCM RTVI sweep on an immutable baseline or candidate image."""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from esp_serial import ROOT, EspSerial
from ethernet_bench import ethernet_bench
from step7_support import BotServer
from phase2_endurance import snapshot
from arrival_trace import drain
from quiet_nic import wait_for_quiet,QuietNic
from netem import Netem
from uplink_continuity import link_tail
from step8e2b_analysis import device_send_cadence,owner_gate,console_windows,perf_history


def _run(out,elf,options=(),sustained_seconds=0,capture_seconds=None):
    out.mkdir(parents=True, exist_ok=True)
    result = dict(completed=False, elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),
                  stimulus='fixed PCM, forced open mic, continuous offline bot speech', samples=[],
                  sustained_seconds=sustained_seconds,capture_seconds=capture_seconds)
    values = dict(OFFLINE_RTVI_BURST_SWEEP='1',OFFLINE_RTVI_SUSTAINED_SECONDS=str(sustained_seconds),OFFLINE_BENCH_CONTINUOUS_AUDIO='1',
                  RTP_EVIDENCE_DIR=str(out/'rtp'), RTP_EVIDENCE_MAX_PACKETS='100000',
                  OFFLINE_IDLE_TIMEOUT_SECS='2100', OFFLINE_SKIP_GREETING='1')
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        with ethernet_bench(out/'ethernet-stun'), BotServer(out/'bot') as bot:
            with EspSerial('/dev/ttyACM0', log_path=out/'serial.log') as board:
                try:
                    start = board.cursor
                    board.reset()
                    board.wait_for('READY transport-console', 60, since=start)
                    identity = board.wait_for('elf_sha256=', 3, since=start)
                    assert re.search(r'elf_sha256=([a-f0-9]{64})', identity)[1] == result['elf_sha256']
                    board.command('server-url '+os.environ['BOT_BENCH_SERVER_URL'])
                    for cmd in ('peer-option call-timing 1', 'peer-option arrival-trace 1',
                                'wifi-log 1','quiet 1', 'mic-fixture 1', 'gate open'):
                        board.command(cmd)
                    for option in options:
                        name,value=option.split('=',1);board.command(f'peer-option {name} {value}')
                    board.command('connect', 90)
                    board.command('audio 1')
                    board.command('play-long 7' if sustained_seconds else 'play-long 2',30)
                    result['before'] = snapshot(board, active=True, allow_incomplete=True)
                    began=time.monotonic();deadline=began+(capture_seconds or (90+sustained_seconds))+20
                    while not any(e['event']=='rtvi-burst-complete' for e in bot.events()) or (capture_seconds and time.monotonic()<began+capture_seconds):
                        assert time.monotonic()<deadline, 'Incomplete RTVI sweep'
                        time.sleep(4)
                        result['samples'].append(snapshot(board, active=True, allow_incomplete=True))
                        drain(board,out/'arrivals.jsonl');board.command('wifi-stats',10)
                    result['after'] = snapshot(board, active=True, allow_incomplete=True)
                    drain(board, out/'arrivals.jsonl')
                    result['completed'] = True
                finally:
                    for cmd in ('audio 0','mic-fixture 0','gate auto','quiet 0','wifi-log 0','disconnect'):
                        try: board.command(cmd, 40)
                        except Exception as exc: result.setdefault('cleanup_errors', []).append(str(exc))
        packets = [json.loads(s) for s in (out/'rtp/rtp.jsonl').read_text().splitlines()]
        from step8e_analysis import authenticated_socket_packets
        socket_path=out/'rtp/socket-rtp.jsonl'
        socket_packets=authenticated_socket_packets(
            [json.loads(s) for s in socket_path.read_text().splitlines()],packets) if socket_path.exists() else []
        result['receive_boundary']='aiortc RTP handler after ICE/SRTP'
        for event in bot.events():
            if event['event'] != 'rtvi-burst-start': continue
            begin = event['t']*1000
            gaps = [dict(gap_ms=b['observed_ms']-a['observed_ms'],
                         seq_step=(b['seq']-a['seq'])%65536,
                         timestamp_step=(b['timestamp']-a['timestamp'])%2**32)
                    for a,b in zip(packets, packets[1:])
                    if a['observed_ms']<=begin+1800 and b['observed_ms']>=begin-100]
            result.setdefault('bursts', []).append(dict(count=event['count'],repeat=event['repeat'],
                max_gap_ms=max((g['gap_ms'] for g in gaps),default=None),
                gaps_over_60_ms=[g for g in gaps if g['gap_ms']>60]))
            callback_gaps=[b['observed_ms']-a['observed_ms'] for a,b in zip(socket_packets,socket_packets[1:])
                if a['observed_ms']<=begin+1800 and b['observed_ms']>=begin-100]
            covered=bool(socket_packets and socket_packets[0]['observed_ms']<=begin-100
                         and socket_packets[-1]['observed_ms']>=begin+1800)
            result['bursts'][-1].update(udp_callback_covered=covered,
                udp_callback_max_gap_ms=max(callback_gaps,default=None),
                udp_callback_passed=covered and bool(callback_gaps) and max(callback_gaps)<=60)
        from step8e_analysis import uplink_gap_attribution
        rows=[json.loads(s) for s in (out/'arrivals.jsonl').read_text().splitlines()]
        result['uplink_gap_attribution']=uplink_gap_attribution(rows,packets,
            result['before']['observed_monotonic_s'],result['after']['observed_monotonic_s'])
        start_us=result['before']['perf']['device_us'];end_us=result['after']['perf']['device_us']
        slow=[r for r in rows if r['kind']==8 and r['value']>40000
              and r['device_us']>=start_us and r['device_us']-r['value']<=end_us]
        sends=[r for r in rows if r['kind']==12 and start_us<=r['device_us']<=end_us]
        n50=[b for b in result.get('bursts',[]) if b['count']==50]
        trace=result['after']['perf']['peer_profile']['trace']
        covered=bool(sends) and sends[0]['device_us']-start_us<1000000 and end_us-sends[-1]['device_us']<1000000
        calls=result['after']['perf']['peer_profile']['loop_iteration']['calls']-result['before']['perf']['peer_profile']['loop_iteration']['calls']
        ordinary_slow=slow
        if sustained_seconds:
            offset=start_us-result['before']['observed_monotonic_s']*1e6
            ordinary_slow=[r for r in slow if any(e['event']=='rtvi-burst-start' and e['t']*1e6+offset-100000<=r['device_us']<=e['t']*1e6+offset+1800000 for e in bot.events())]
        ordinary_gate=owner_gate(ordinary_slow,calls,trace=rows,snapshots=perf_history(out/'serial.log'),covered=covered and not trace['overwritten'])
        result['owner_burst_gate']=dict(n50_repeats=len(n50),intervals_over_40ms=slow,
            trace_overwritten=trace['overwritten'],trace_covered=covered,
            passed=len(n50)==3 and ordinary_gate['passed'] is True,gate=ordinary_gate,
            basis='All owner intervals overlapping the post-connect sweep, including three N=50 bursts; SlowLoop records intervals >40 ms')
        result['link_tail']=link_tail(packets,result['before']['observed_monotonic_s'],result['after']['observed_monotonic_s'])
        result['device_send_cadence']=device_send_cadence(rows,start_us,end_us,console_windows(out/'serial.log'),snapshots=perf_history(out/'serial.log'))
        result['overflow']=dict(messages_sent=sum(e['count'] for e in bot.events() if e['event']=='rtvi-overflow-sent'),
            sustained_completed=any(e['event']=='rtvi-overflow-complete' for e in bot.events()),
            callback_drops=result['after']['perf']['drop_reasons']['message_queue']-result['before']['perf']['drop_reasons']['message_queue'] if 'message_queue' in result['after']['perf']['drop_reasons'] else None,
            session_survived=result['before']['perf']['pc_id']==result['after']['perf']['pc_id'])
        result['passed']=(result['owner_burst_gate']['passed'] and result['completed']
            and result['link_tail']['passed'] is True
            and result['device_send_cadence']['target_passed'] is True
            and result['device_send_cadence']['hard_ceiling_passed'] is True)
        if sustained_seconds:
            result['sustained_owner']=dict(slowloop_count=len(slow),slowloop_max_ms=max((r['value']/1000 for r in slow),default=None),
                main_max_us=result['after']['perf']['peer_profile']['main_loop']['max_us'],
                gate=owner_gate(slow,calls,sustained=True,covered=covered and not trace['overwritten'],trace=rows,snapshots=perf_history(out/'serial.log')))
            result['passed'] &= result['sustained_owner']['gate']['passed'] is True
            result['passed'] &= result['overflow']['sustained_completed'] and result['overflow']['session_survived'] and bool(result['overflow']['callback_drops'])
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        for key,value in previous.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def run(out,elf,options=(),sustained_seconds=0,capture_seconds=None):
    out.mkdir(parents=True,exist_ok=False);wait_for_quiet(out/'quiet-preflight.json')
    with Netem('192.168.4.29',['loss','0%'],'both',state=out/'tc-state.json'),QuietNic(out/'quiet-nic.json',seconds=capture_seconds):
        return _run(out,elf,options,sustained_seconds,capture_seconds)


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--out',type=Path,required=True)
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf')
    p.add_argument('--option',action='append',default=[]);p.add_argument('--sustained-seconds',type=int,default=0);p.add_argument('--capture-seconds',type=int)
    a=p.parse_args(); result=run(a.out.resolve(),a.elf.resolve(),a.option,a.sustained_seconds,a.capture_seconds)
    print(json.dumps({k:v for k,v in result.items() if k in ('passed','completed','bursts','error')},indent=2))
    raise SystemExit(0 if result.get('passed') else 1)
