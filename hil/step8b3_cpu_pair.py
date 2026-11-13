#!/usr/bin/env python3
"""Alternating DSP images with identical continuous-fixture sequence and counters."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
import numpy as np
from esp_serial import ROOT
from step8b2_listen import flash_image
from step8_summary import profile

def direct_cost(phase):
    before=phase['before'];after=phase['after']
    a=before['playback_cost'];b=after['playback_cost']
    if a is None or b is None:raise RuntimeError('PLAYBACK_COST unavailable')
    frames=b['frames']-a['frames'];total=b['total_us']-a['total_us']
    elapsed=after['perf']['device_us']-before['perf']['device_us']
    if frames<=0 or elapsed<=0:raise RuntimeError('No DSP cost coverage')
    return dict(frames=frames,mean_us=total/frames,max_us_since_boot=b['max_us'],
        dsp_cpu_percent=100*total/elapsed,elapsed_us=elapsed,
        timing_basis='dsp.process wall time, including preemption; task-runtime CPU reported separately')

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--old',type=Path,required=True);p.add_argument('--new',type=Path,required=True)
    p.add_argument('--target',type=int,default=-25)
    p.add_argument('--server-url')
    p.add_argument('--ice-servers')
    p.add_argument('--pairs',type=int,default=3);p.add_argument('--seconds',type=int,default=30);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    result=dict(completed=False,target_dbfs=a.target,runs=[],summary={})
    try:
        for pair in range(a.pairs):
            for label in (('old','new') if pair%2==0 else ('new','old')):
                directory=getattr(a,label).resolve();name=f'pair{pair+1}-{label}'
                flash_image(directory/'dg-esp32.bin',out/f'{name}-flash.log')
                with (out/f'{name}.log').open('w') as log:
                    bench=[]
                    if a.server_url:bench+=['--server-url',a.server_url]
                    if a.ice_servers:bench+=['--ice-servers',a.ice_servers]
                    child=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/perf_test.py'),
                        '--out',str(out/name),'--elf',str(directory/'dg-esp32.elf'),'--seconds',str(a.seconds),'--no-trace','--playback-target',str(a.target)]+bench,
                        stdout=log,stderr=subprocess.STDOUT,timeout=240)
                data=json.loads((out/name/'result.json').read_text())
                row=dict(pair=pair+1,label=label,returncode=child.returncode,elf_sha256=hashlib.sha256((directory/'dg-esp32.elf').read_bytes()).hexdigest(),
                    phases=[dict(name=phase['name'],cost=direct_cost(phase)) for phase in data['phases']],
                    task_profiles=profile(out/name/'result.json'),traffic_coverage=data.get('traffic_coverage'))
                result['runs'].append(row)
                (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(name,row['phases'],flush=True)
                if not data['passed']:raise RuntimeError('Incomplete stimulus: '+str(data.get('error')))
        for phase in ('idle','duplex'):
            groups={label:[next(p['cost']['mean_us'] for p in r['phases'] if p['name']==phase) for r in result['runs'] if r['label']==label] for label in ('old','new')}
            old,new=(float(np.median(groups[label])) for label in ('old','new'))
            task_drift={}
            for task in ('audio-tx','audio-capture','audio-playback'):
                samples={label:[next(t['cpu_percent'] for t in next(p['tasks'] for p in r['task_profiles'] if p['name']==phase) if t['name']==task) for r in result['runs'] if r['label']==label] for label in ('old','new')}
                task_drift[task]=dict(samples=samples,median_delta_percentage_points=float(np.median(samples['new'])-np.median(samples['old'])))
            result['summary'][phase]=dict(mean_us_samples=groups,old_median_us=old,new_median_us=new,relative_increase_percent=100*(new/old-1),
                unchanged_task_noise=task_drift)
        result['completed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        flash_image(a.new.resolve()/'dg-esp32.bin',out/'restore-new.log')
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return 0 if result['completed'] else 1
if __name__=='__main__':raise SystemExit(main())
