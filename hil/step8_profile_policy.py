#!/usr/bin/env python3
"""Compare retained idle/duplex windows; missing task observations stay missing.

These results supplement the digital media comparator. A profile's `passed`
flag establishes collection only, so it cannot itself qualify a setting.
"""
import argparse
import json
from pathlib import Path
import numpy as np
from step8_summary import profile

KEY_TASKS={'peer-loop','audio-tx','audio-playback','peer-rx'}

def distribution(values):
    values=[float(v) for v in values if v is not None]
    if not values:return None
    return dict(n=len(values),p50=float(np.median(values)),
        p90=float(np.percentile(values,90,method='lower')),
        p95=float(np.percentile(values,95,method='lower')),min=min(values),max=max(values))

def population(directory,label,only_boot=None,*,profiles='profiles'):
    phases={'idle':{},'duplex':{}};captures=[]
    for boot in sorted((directory/profiles).glob('boot*')):
        if only_boot is not None and boot.name!=only_boot:continue
        # Capture retries are supplemental. Preserve original numeric windows
        # even when a later phase or teardown failed to collect.
        path=boot/f'{label}1/result.json'
        if not path.exists():continue
        raw=json.loads(path.read_text())
        captures.append(dict(boot=boot.name,path=str(path),passed=raw['passed'],error=raw.get('error')))
        for sample in profile(path):
            metrics=phases[sample['name']]
            for name,value in sample['counters'].items():metrics.setdefault('counter:'+name,[]).append(value)
            for name,value in sample['network']['rtp.jsonl'].items():
                if name!='packets':metrics.setdefault('uplink:'+name,[]).append(value)
            task_groups={}
            for t in sample['tasks']:
                # Keep same-name SDK workers separate, without comparing
                # unstable task IDs across boots or images.
                key=t['name']+'/core'+str(t['core'])
                task_groups.setdefault(key,[]).append(t['cpu_percent'])
            for name,values in task_groups.items():
                for i,value in enumerate(sorted(values,reverse=True)):
                    suffix='/worker'+str(i+1) if len(values)>1 else ''
                    metrics.setdefault('cpu:'+name+suffix,[]).append(value)
    return dict(captures=captures,phases={phase:{k:distribution(v) for k,v in metrics.items()} for phase,metrics in phases.items()})

def compare(a,b,expected):
    checks={};metrics={}
    checks['complete']=len(a['captures'])==len(b['captures'])==expected and all(c['passed'] for c in a['captures']+b['captures'])
    for phase in ('idle','duplex'):
        metrics[phase]={}
        for name in KEY_TASKS:
            for role,pop in (('A',a),('B',b)):
                observed=[v for k,v in pop['phases'][phase].items()
                    if k.startswith('cpu:'+name+'/') and v]
                checks[phase+':'+role+':observed:'+name]=bool(observed) and all(v['n']==expected for v in observed)
        for key,x in a['phases'][phase].items():
            y=b['phases'][phase].get(key);metrics[phase][key]=dict(A=x,B=y)
            if key.startswith('cpu:'):
                if key.split(':',1)[1].split('/',1)[0] not in KEY_TASKS:continue
                bound=x['p50']*1.1 if x else None
            else:
                floor=0 if key in ('counter:i2s_underflows','counter:decode_errors','counter:queue_dropped') else 1
                bound=x['p50']+max(x['p50']*.1,floor) if x else None
            checks[phase+':'+key]=bool(x and y and y['p50']<=bound)
            metrics[phase][key]['p50_bound']=bound
        for key in ('counter:i2s_underflows','counter:decode_errors','counter:queue_dropped'):
            y=b['phases'][phase].get(key)
            checks[phase+':'+key+':all_zero']=bool(y and y['max']==0)
    return dict(passed=all(checks.values()),checks=checks,metrics=metrics)

def repeat_failed_profiles(directory,name,images,options,labels,seconds=20):
    """Fresh matched repeats are evidence, never replacements for originals."""
    from step8_campaign import capture_profile
    notes=[]
    for boot in sorted((directory/'profiles').glob('boot*')):
        pops={label:population(directory,label,boot.name) for label in labels}
        comparisons={label+'-B':compare(pops[label],pops['B'],1) for label in labels[:-1]}
        failed={label:[key for key,value in result['checks'].items() if not value]
                for label,result in comparisons.items() if not result['passed']}
        if not failed:continue
        # A failed B collection has already had its one capture retry. Do not
        # give that same source a third attempt under another label.
        prior_labels=[label for label in labels if (boot/(label+'2')/'result.json').exists()]
        prior='B' in prior_labels
        note=dict(boot=boot.name,failed_checks=failed,already_rerun=prior,
            prior_capture_reruns=prior_labels)
        notes.append(note)
        if prior:continue
        repeat=directory/'profile-reruns'/boot.name
        repeat.mkdir(parents=True,exist_ok=False)
        (repeat/'source.json').write_text(json.dumps(note,indent=2)+'\n')
        for label in labels:
            # A/C collection failures have also already had their once-only
            # repeat. Retain those captures rather than making a third attempt.
            if label in prior_labels:continue
            # capture_profile normally retries a collection failure. This is
            # already the once-only repeat, so disable another nested retry.
            capture_profile(name,label,images[label],repeat,seconds,options[label],attempts=1)
        repeated={label:population(directory,label,boot.name,profiles='profile-reruns') for label in labels}
        repeat_comparisons={label+'-B':compare(repeated[label],repeated['B'],1) for label in labels[:-1]}
        (repeat/'comparison.json').write_text(json.dumps(repeat_comparisons,indent=2)+'\n')
    (directory/'profile-reruns.json').write_text(json.dumps(notes,indent=2)+'\n')
    return notes

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--boots',type=int,required=True)
    p.add_argument('--no-observer',action='store_true');a=p.parse_args()
    labels=('A','B') if a.no_observer else ('A','C','B')
    pops={label:population(a.directory,label) for label in labels}
    result={label+'-B':compare(pops[label],pops['B'],a.boots) for label in labels[:-1]}
    (a.directory/'profile-populations.json').write_text(json.dumps(pops,indent=2)+'\n')
    (a.directory/'profile-comparison.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({label:[k for k,v in item['checks'].items() if not v] for label,item in result.items()}))
