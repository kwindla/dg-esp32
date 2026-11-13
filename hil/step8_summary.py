#!/usr/bin/env python3
"""Summarize raw step-8 profiles without substituting missing observations."""
import argparse
import json
import re
from pathlib import Path
import numpy as np

def network(packets,field):
    gaps=[b[field]-a[field] for a,b in zip(packets,packets[1:])]
    highest=packets[0]['seq'] if packets else 0;seen=set()
    for p in packets:
        seq=highest+(p['seq']-highest%65536+32768)%65536-32768
        seen.add(seq);highest=max(highest,seq)
    return dict(packets=len(packets),loss=max(seen)-min(seen)+1-len(seen) if seen else None,
        gap_p50_ms=float(np.median(gaps)) if gaps else None,
        gap_p95_ms=float(np.percentile(gaps,95,method='lower')) if gaps else None,
        gap_max_ms=max(gaps) if gaps else None)

def profile(path):
    result=json.loads(Path(path).read_text());output=[]
    serial=Path(path).with_name('serial.log')
    text=serial.read_text(errors='replace') if serial.exists() else ''
    startup={}
    for key,pattern in [('wifi_ready_boot_us',r'WIFI READY t=(\d+)'),
        ('cert_generate_us',r'CERT duration_us=(\d+)'),
        ('start_to_ready_us',r'START_READY .*duration_us=(\d+)')]:
        matches=re.findall(pattern,text);startup[key]=int(matches[-1]) if matches else None
    for phase in result.get('phases',[]):
        a,b=phase['before'],phase['after'];duration=b['observed_monotonic_s']-a['observed_monotonic_s']
        elapsed_us=b['perf']['device_us']-a['perf']['device_us']
        if elapsed_us<=0:raise ValueError('Nonpositive device profile interval')
        tasks=[]
        def tasks_at(point):
            embedded=point['perf'].get('tasks')
            if embedded is not None:
                return {t['id']:dict(t,name=t['name'],runtime_us=t['runtime_us'],free=t['stack_free_bytes']) for t in embedded}
            return {t['task_id']:t for t in point['tasks'] if t['task_id'] is not None}
        first,last=tasks_at(a),tasks_at(b)
        for task_id,t in last.items():
            if task_id not in first:continue
            delta=(t['runtime_us']-first[task_id]['runtime_us'])%2**32
            tasks.append(dict(name=t['name'],id=task_id,core=t['core'],
                cpu_percent=100*delta/elapsed_us,stack_free_bytes=t['free'],
                stack_margin_percent=t.get('stack_margin_percent')))
        parts={};ap=a['perf'].get('peer_profile');bp=b['perf'].get('peer_profile')
        if ap and bp:
            iterations=bp['iterations']-ap['iterations']
            for name,value in bp.items():
                if not isinstance(value,dict) or 'calls' not in value or not value.get('enabled',True):continue
                calls=value['calls']-ap[name]['calls'];wall=value['wall_us']-ap[name]['wall_us'];cycles=value['cycles']-ap[name]['cycles']
                parts[name]=dict(calls=calls,calls_per_s=calls/duration,wall_us=wall,
                    mean_us=wall/calls if calls else None,inclusive_cycles=cycles,
                    inclusive_wall_percent=100*wall/(duration*1e6))
            parts['cadence']=dict(iterations=iterations,iterations_per_s=iterations/duration,
                empty_iterations=bp['empty_iterations']-ap['empty_iterations'],
                received_per_iteration=(b['perf']['received']-a['perf']['received'])/iterations if iterations else None)
        nets={}
        for filename,field in [('rtp.jsonl','arrival_ms'),('downlink.jsonl','send_ms')]:
            file=Path(path).parent/filename
            packets=[json.loads(s) for s in file.read_text().splitlines()] if file.exists() else []
            if filename=='rtp.jsonl':
                # aiortc arrival_ms is milliseconds since 1900 (NTP), whereas
                # our added observation clock and downlink send_ms are monotonic.
                packets=[r for r in packets if (a['observed_monotonic_s']*1000<=r['observed_ms']<=b['observed_monotonic_s']*1000
                    if 'observed_ms' in r else
                    (a['observed_unix_s']+2208988800)*1000<=r[field]<=(b['observed_unix_s']+2208988800)*1000)]
            else:
                packets=[r for r in packets if a['observed_monotonic_s']*1000<=r[field]<=b['observed_monotonic_s']*1000]
            nets[filename]=network(packets,field)
        output.append(dict(name=phase['name'],duration_s=duration,device_duration_s=elapsed_us/1e6,cpu_clock_basis="device runtime / device elapsed",tasks=tasks,parts=parts,network=nets,
            encode_p50_us=b['perf']['encode_p50_us'],encode_p95_us=b['perf']['encode_p95_us'],
            decode_p50_us=b['perf']['decode_p50_us'],decode_p95_us=b['perf']['decode_p95_us'],
            counters={key:b['perf'][key]-a['perf'][key] for key in ('rebuffers','drift_drops','plc_frames','i2s_underflows','decode_errors','queue_dropped')}))
        output[-1]['traffic_frames']={key:b['perf'][key]-a['perf'][key] for key in ('sent','received')}
        output[-1]['transport_metrics']={key:b['perf'].get(key) for key in (
            'enqueue_send_mean_us','enqueue_send_max_us','loop_max_us','dtls_duration_us',
            'speech_decode_frames','speech_decode_p50_us','speech_decode_p95_us',
            'silence_decode_frames','silence_decode_p50_us','silence_decode_p95_us')}
        output[-1]['connect_timings']=b['perf'].get('connect_timings')
        output[-1]['heap']=b['perf'].get('heap')
        output[-1]['startup']=startup
    return output

def duplex_coverage(samples):
    """Require the existing 2,900 frames/minute continuous-traffic floor.

    Idle output may intentionally contain no packets. A duplex result requires
    actual source and device traffic in both directions, not only an open gate.
    """
    duplex=[s for s in samples if s['name']=='duplex']
    if len(duplex)!=1:return dict(passed=False,error='Missing unique duplex window')
    sample=duplex[0];minimum=int(sample['duration_s']*2900/60)
    counts={'device_sent':sample['traffic_frames']['sent'],
        'device_received':sample['traffic_frames']['received'],
        'server_uplink':sample['network']['rtp.jsonl']['packets'],
        'server_downlink':sample['network']['downlink.jsonl']['packets']}
    return dict(passed=minimum>0 and all(v>=minimum for v in counts.values()),
        minimum_frames=minimum,counts=counts,basis='Existing continuous soak floor: 2900 frames/minute')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('result',type=Path);a=p.parse_args();print(json.dumps(profile(a.result),indent=2))
