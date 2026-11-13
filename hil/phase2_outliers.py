#!/usr/bin/env python3
"""Rerun isolated statistical tails once, without replacing formal samples."""
import argparse
import json
from pathlib import Path
from esp_serial import ROOT
from phase2_campaign import EVIDENCE, PYTHON, cohort, compare, flash, run, write_json


def isolated(pop):
    found={}
    for metric in pop['metrics']:
        rows=[]
        for s in pop['samples']:
            value=s['closed'].get(metric,s['rtp'].get(metric,s.get('setup',{}).get(metric)))
            if metric.endswith('_correlation'):value=s[metric.split('_')[0]]['correlation']
            if value is not None:rows.append((value,s))
        if len(rows)<3:continue
        rows.sort(key=lambda x:x[0])
        bad,other=(rows[0],rows[1]) if metric.endswith('_correlation') else (rows[-1],rows[-2])
        floor=1000 if metric.endswith('_us') else (0 if metric.endswith('_correlation') else 1)
        distance=abs(bad[0]-other[0]);bound=max(abs(other[0])*.1,floor)
        if distance>bound:
            s=bad[1];key=(s['boot'],s['index'])
            found.setdefault(key,[]).append(dict(metric=metric,original=bad[0],next_worst=other[0]))
    return found


def rerun(name, image=None):
    out=EVIDENCE/name
    image=Path(image) if image is not None else EVIDENCE/'images'/name
    settings=json.loads((image/'settings.json').read_text())
    concurrent='CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM' in settings and settings['CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM']!='16'
    for label,image in [('A',EVIDENCE/'images/control'),('B',image)]:
        pop=cohort(out/label);found=isolated(pop)
        notes=[]
        for (boot,index),reasons in found.items():
            prior=[r for r in pop['reruns'] if r['boot']==boot and r['index']==index]
            directory=out/label/'stat-outliers'/f'{boot}-session{index}'
            note=dict(boot=boot,index=index,reasons=reasons,already_rerun=bool(prior))
            notes.append(note)
            if prior or (directory/'result.json').exists():continue
            directory.mkdir(parents=True,exist_ok=True);write_json(directory/'source.json',note)
            flash(image,directory/'flash.log')
            argv=[PYTHON,ROOT/'hil/transport_test.py','--campaign','--no-speech','--sessions','1','--elf',image/'dg-esp32.elf','--out',directory]
            if concurrent:argv.append('--concurrent-sctp')
            code=run(argv,directory/'run.log')
            if not (directory/'1/result.json').exists():raise RuntimeError(f'Incomplete outlier rerun {directory}')
            print(f'{name}/{label}/{boot}/{index} statistical rerun: exit={code}',flush=True)
        write_json(out/label/'stat-outliers.json',notes)
    a=cohort(out/'A');b=cohort(out/'B')
    result=compare(a,b,expected=len(list((out/'A').glob('boot*/result.json')))*3,performance=name=='tx-core0')
    write_json(out/'comparison.json',result)
    print(f'{name}: final individual decision={result["passed"]}',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('names',nargs='+');p.add_argument('--image',type=Path);a=p.parse_args()
    if a.image and len(a.names)!=1:p.error('--image requires exactly one campaign name')
    for name in a.names:rerun(name,a.image)
