#!/usr/bin/env python3
"""Task CPU occupancy between ready and final streaming stats snapshots.

Runtime deltas are percentages of one core, not percentages of both CPUs.
Use stable task IDs when available; duplicate names in older images are omitted.
"""
import argparse
import json
from pathlib import Path
import re
from phase2_campaign import distribution


def intervals(path):
    text=path.read_text(errors='replace')
    metadata=path.with_name('result.json')
    formal_ready=None
    if metadata.exists():
        top=json.loads(metadata.read_text())
        formal_ready={r['t'] for s in top.get('sessions',[]) for r in s.get('heap_rows',[])
                      if r['phase']=='ready'}
        if not formal_ready:formal_ready=None
    starts=list(re.finditer(r'^.*ESP32-S3 step \d+ (?:audio|transport) elf_sha256=',text,re.M))
    if starts:text=text[starts[-1].start():]
    snapshots=[];current=None
    for line in text.splitlines():
        heap=re.search(r'HEAP phase=(\S+).*?t=(\d+)',line)
        if heap:
            current=dict(phase=heap[1],time=int(heap[2]),tasks={});snapshots.append(current)
        task=re.search(r'STACK task=(.*?) free_bytes=(\d+) priority=(\d+) runtime_us=(\d+)(?: core=(-?\d+) task_id=(\d+))?',line)
        if task and current:
            name=task[1];key=task[6] or name
            row=dict(name=name,free=int(task[2]),priority=int(task[3]),runtime_us=int(task[4]),
                     core=int(task[5]) if task[5] else None,task_id=int(task[6]) if task[6] else None)
            stamp=re.search(r'[IWE] \((\d+)\)',line)
            row['time_us']=int(stamp[1])*1000 if stamp else current['time']
            if key in current['tasks']:current['tasks'][key]=None
            else:current['tasks'][key]=row
    result=[]
    for i,first in enumerate(snapshots):
        if first['phase']!='ready':continue
        if formal_ready is not None and first['time'] not in formal_ready:continue
        window=[first]
        for following in snapshots[i+1:]:
            if following['phase'] in ['before-connect','after-disconnect']:break
            window.append(following)
        # Speech/VAD and greeting diagnostics do not contain the formal
        # duplex tone's streaming marker; keep them out of this population.
        if not any(s['phase']=='streaming' for s in window):continue
        rows=[]
        for key in {key for s in window for key in s['tasks']}:
            points=[(s['tasks'][key]['time_us'],s['tasks'][key]) for s in window if s['tasks'].get(key)]
            if len(points)<2:continue
            start,row=points[0];finish,end=points[-1]
            elapsed=finish-start
            if elapsed<5_000_000:continue
            delta=end['runtime_us']-row['runtime_us']
            if delta<0:continue
            rows.append(dict(row,cpu_percent=100*delta/elapsed,free=end['free'],elapsed_us=elapsed))
        result.append(dict(ready_us=first['time'],tasks=rows))
    return result


def summarize(paths):
    samples=[s for path in paths for s in intervals(path)];groups={}
    for sample in samples:
        for task in sample['tasks']:
            # Same-name workers remain separate by task ID, which is stable
            # for the three sessions within a boot of the same image.
            suffix=f'/id{task["task_id"]}' if task['name']=='pipecat' else ''
            key=f'{task["name"]}/core{task["core"]}{suffix}'
            groups.setdefault(key,[]).append(task['cpu_percent'])
    return dict(intervals=samples,task_cpu={k:distribution(v) for k,v in groups.items()})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('logs',nargs='+',type=Path);args=parser.parse_args()
    print(json.dumps(summarize(args.logs),indent=2))
