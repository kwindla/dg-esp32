#!/usr/bin/env python3
"""Summarize complete and failed phase-1 campaigns without discarding failures."""
import argparse
import json
from pathlib import Path
import re
import numpy as np

def distribution(values):
    values=list(values)
    if not values: return None
    return dict(n=len(values), min=float(min(values)), p50=float(np.percentile(values,50)),
                p95=float(np.percentile(values,95)), max=float(max(values)), mean=float(np.mean(values)))

def summarize(directory):
    sessions=[]; boots=[]; boot_errors=[]; regions=[]; heap_phases={}; timings={}; audio=[]; dtls=[]; cpu={}
    costs=[]; certificates=[]; cpu_loads={}
    for boot in sorted(directory.glob('boot*')):
        if not boot.is_dir(): continue
        top=boot/'result.json'
        boots.append(json.loads(top.read_text()) if top.exists() else {'passed':False,'missing_summary':str(boot)})
        failure=boots[-1].get('error') or boots[-1].get('missing_summary')
        boot_log=directory/f'{boot.name}.log'
        if not failure and boot_log.exists():
            captured=boot_log.read_text(errors='replace')
            if 'Traceback (most recent call last)' in captured:
                failure=captured.strip().splitlines()[-1]
        if failure: boot_errors.append(dict(boot=boot.name,error=failure))
        for path in sorted(boot.glob('[0-9]*/result.json')):
            item=json.loads(path.read_text())
            for recorded in boots[-1].get('sessions',[]):
                if recorded['index']==item['index'] and 'acceptance_error' in recorded:
                    item['acceptance_error']=recorded['acceptance_error']
            item['boot']=boot.name; sessions.append(item)
        log=boot/'serial.log'
        if not log.exists(): continue
        text=log.read_text(errors='replace')
        starts=list(re.finditer(r'^.*ESP32-S3 step \d+ (?:audio|transport) elf_sha256=',text,re.M))
        if starts: text=text[starts[-1].start():]
        costs.extend([dict(frames=int(m[1]),mean_us=int(m[2]),max_us=int(m[3]),fast=int(m[4])) for m in re.finditer(r'MIC_COST measure_frames=(\d+) mean_us=(\d+) max_us=(\d+) fast=(\d+)',text)])
        certificates.extend(map(int,re.findall(r'CERT duration_us=(\d+)',text)))
        snapshots=[]; current=None
        for line in text.splitlines():
            m=re.search(r'HEAP phase=(\S+).*?t=(\d+)',line)
            if m:
                current=dict(phase=m[1],time=int(m[2]),tasks={});snapshots.append(current)
            m=re.search(r'STACK task=(\S+) .*runtime_us=(\d+)',line)
            if m and current: current['tasks'][m[1]]=int(m[2])
        for i,first in enumerate(snapshots):
            if first['phase']!='ready':continue
            last=None
            for following in snapshots[i+1:]:
                if following['phase']=='before-connect':break
                if following['phase']=='stats':last=following
            if last and last['time']>first['time']:
                for name,value in first['tasks'].items():
                    if name in last['tasks'] and last['tasks'][name]>=value:
                        cpu_loads.setdefault(name,[]).append(100*(last['tasks'][name]-value)/(last['time']-first['time']))
        for label,regex in [('start_ready_us',r'START_READY .*duration_us=(\d+)'),('wifi_ready_us',r'WIFI READY t=(\d+)')]:
            values=list(map(int,re.findall(regex,text)))
            timings.setdefault(label,[]).extend(values[:3] if label=='start_ready_us' else values[:1])
        wifi=re.findall(r'WIFI READY t=(\d+)',text); ready=re.findall(r'on_bot_ready t=(\d+)',text)
        if wifi and ready: timings.setdefault('wifi_to_ready_us',[]).append(int(ready[0])-int(wifi[0]))
        opens=list(map(int,re.findall(r'PHASE open t=(\d+)',text)))
        connected=list(map(int,re.findall(r'PHASE connected t=(\d+)',text)))
        timings.setdefault('open_connected_us',[]).extend(b-a for a,b in list(zip(opens,connected))[:3])
        for m in re.finditer(r'(?:HEAP phase=|PHASE )(\S+) (.*)',text):
            phase=m[1]; row={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',m[2])}
            for k,v in row.items(): heap_phases.setdefault(phase,{}).setdefault(k,[]).append(v)
        for m in re.finditer(r'REGION phase=(\S+) .*kind=(\S+) start=(\S+) length=(\d+) free=(\d+) largest=(\d+) minimum=(\d+)',text):
            regions.append(dict(phase=m[1],kind=m[2],start=m[3],length=int(m[4]),free=int(m[5]),largest=int(m[6]),minimum=int(m[7])))
        audio.extend([list(map(int,m)) for m in re.findall(r'AUDIO drops=(\d+) read_errors=(\d+) write_errors=(\d+)',text)])
        dtls.extend([list(map(int,m)) for m in re.findall(r'DTLS duration_us=(\d+) sampled_min=(\d+) local_min_sum=(\d+)',text) if int(m[0])])
        for m in re.finditer(r'STACK task=(\S+) .*runtime_us=(\d+)',text): cpu.setdefault(m[1],[]).append(int(m[2]))
    metric_names=['encode_p50_us','encode_p95_us','encode_max_us','decode_p50_us','decode_p95_us','decode_max_us',
                  'enqueue_send_mean_us','enqueue_send_max_us','late','lost','concealed','underruns','playout_dropped','queue_dropped','dtls_duration_us','prefills','rebuffers','current_depth','target_depth','drift_drops','plc_frames','i2s_underflows']
    metrics={k:distribution(s['closed'][k] for s in sessions if k in s['closed'] and (not k.startswith(('encode_p','decode_p')) or s['closed'].get(k.split('_')[0]+'_frames',1)>0)) for k in metric_names}
    metrics.update({k:distribution(s['rtp'][k] for s in sessions) for k in ['missing','max_gap_ms','p95_gap_ms']})
    metrics.update({f'{name}_correlation':distribution(s[name]['correlation'] for s in sessions) for name in ['greeting','reply']})
    failures=[dict(boot=s['boot'],index=s['index'],rtp_loss=s['rtp']['missing'],greeting=s['greeting']['passed'],reply=s['reply']['passed'],acceptance_error=s.get('acceptance_error'))
              for s in sessions if s.get('acceptance_error') or s['rtp']['missing'] or not s['greeting']['passed'] or not s['reply']['passed']]
    extras={}
    for name in ['greeting1','greeting2','loop','console']:
        path=directory/name/'result.json'
        extras[name]=json.loads(path.read_text()).get('passed',False) if path.exists() else None
    return dict(boots=len(boots),boot_failures=[i+1 for i,boot in enumerate(boots) if not boot.get('passed')],boot_errors=boot_errors,media_sessions=len(sessions),failures=failures,extras=extras,metrics=metrics,timings={k:distribution(v) for k,v in timings.items()},
                heap_phases={p:{k:distribution(v) for k,v in fields.items()} for p,fields in heap_phases.items()},regions=regions,
                audio_counters=audio,dtls_samples=dtls,task_runtime_us=cpu,task_cpu_percent={k:distribution(v) for k,v in cpu_loads.items()},mic_cost=costs,certificate_us=distribution(certificates))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    summary=summarize(a.directory);a.out.write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k in ['boots','media_sessions','failures','metrics','timings']},indent=2))
