#!/usr/bin/env python3
"""Interleaved individual performance qualification with unchanged media gates.

Fresh original A, matched observer C, and one candidate B per boot. Three
digital PCM/RTP sessions per boot follow the 6a method; idle/duplex windows
separately measure every task. Failed samples and their repeats stay separate.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from esp_serial import ROOT
from phase2_campaign import flash,cohort,compare,write_json,distribution
from phase2_cpu import summarize as cpu_summary
from step8_campaign import capture_profile,RUNTIME,run_logged
from phase2_outliers import isolated
from step8_profile_policy import repeat_failed_profiles

def run(a):
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    records=[]
    options={'A':[],'C':a.control_option,'B':[] if a.candidate_defaults else a.option or RUNTIME.get(a.name,[])}
    images={'A':a.original.resolve(),'C':a.observer.resolve(),'B':a.candidate.resolve()}
    labels=('A','B') if a.no_observer else ('A','C','B')
    for boot in range(1,a.boots+1):
        for label in labels:
            env=os.environ.copy();env['AUDIO_OUT_AUTO_SILENCE']='0' if label=='B' and a.name=='server-idle' else '1'
            env['AUDIO_OUT_10MS_CHUNKS']='2' if label=='B' and a.name=='server-chunks2' else '4'
            directory=out/label/f'boot{boot}';directory.parent.mkdir(parents=True,exist_ok=True)
            flash(images[label],directory.with_suffix('.flash.log'))
            argv=[sys.executable,ROOT/'hil/transport_test.py','--out',directory,
                '--elf',images[label]/'dg-esp32.elf','--campaign','--rerun-failed','--sessions','3']
            for option in options[label]:argv+=['--option',option]
            if label!='A':argv+=['--arrival-trace']
            code=run_logged(argv,directory.with_suffix('.log'),env)
            raw=json.loads((directory/'result.json').read_text())
            records.append(dict(label=label,boot=boot,exit_code=code,path=str(directory),passed=raw['passed']))
            write_json(out/'runs.json',records)
            print(a.name,label,boot,'sessions',len(raw['sessions']),'exit',code,flush=True)
            if len(raw['sessions'])!=3:raise RuntimeError('Incomplete original capture retained at '+str(directory))
            if any(s.get('capture_complete') is False for s in raw['sessions']):
                raise RuntimeError('Partial original capture and its repeat retained at '+str(directory))
        profile_dir=out/'profiles'/f'boot{boot}';profile_dir.mkdir(parents=True)
        for label in labels:
            capture_profile(a.name,label,images[label],profile_dir,20,options[label])
    for label in labels:
        pop=cohort(out/label);notes=[]
        for (boot,index),reasons in isolated(pop).items():
            prior=any(r['boot']==boot and r['index']==index for r in pop['reruns'])
            note=dict(boot=boot,index=index,reasons=reasons,already_rerun=prior);notes.append(note)
            if prior:continue
            directory=out/label/'stat-outliers'/f'{boot}-session{index}';directory.mkdir(parents=True)
            write_json(directory/'source.json',note)
            env=os.environ.copy();env['AUDIO_OUT_AUTO_SILENCE']='0' if label=='B' and a.name=='server-idle' else '1'
            env['AUDIO_OUT_10MS_CHUNKS']='2' if label=='B' and a.name=='server-chunks2' else '4'
            flash(images[label],directory/'flash.log')
            argv=[sys.executable,ROOT/'hil/transport_test.py','--out',directory,
                '--elf',images[label]/'dg-esp32.elf','--campaign','--no-speech','--sessions','1']
            for option in options[label]:argv+=['--option',option]
            if label!='A':argv+=['--arrival-trace']
            run_logged(argv,directory/'run.log',env)
        write_json(out/label/'stat-outliers.json',notes)
    populations={label:cohort(out/label) for label in labels}
    for label,pop in populations.items():
        pop['cpu']=cpu_summary(sorted((out/label).glob('boot*/serial.log')))
        for name,cost in pop['cpu']['task_cpu'].items():pop['metrics']['cpu:'+name]=cost
        write_json(out/label/'summary.json',pop)
    result={}
    for control in labels[:-1]:
        # Memory gain is a 6a memory-placement gate, not a CPU tuning gate.
        comparison=compare(populations[control],populations['B'],expected=a.boots*3)
        comparison['checks'].pop('ready_gain')
        for metric in list(comparison['checks']):
            if metric.startswith('cpu:'):
                x=populations[control]['metrics'][metric];y=populations['B']['metrics'].get(metric)
                name=metric.split(':',1)[1].split('/',1)[0]
                if name not in ('peer-loop','audio-tx','audio-playback','peer-rx'):
                    comparison['checks'].pop(metric);continue
                comparison['checks'][metric]=bool(x and y and y['p50']<=x['p50']*1.1)
        if a.name=='server-idle':
            # Suppression changes the mix of decoded frames; compare speech
            # with speech using the observer, retaining whole-stream quantiles.
            for metric in ('decode_p50_us','decode_p95_us'):comparison['checks'].pop(metric,None)
            if control=='C':
                for metric in ('speech_decode_p50_us','speech_decode_p95_us'):
                    x=distribution(s['closed'][metric] for s in populations[control]['samples'])
                    y=distribution(s['closed'][metric] for s in populations['B']['samples'])
                    comparison['metrics'][metric]=dict(A=x,B=y)
                    comparison['checks'][metric]=y['p50']<=x['p50']+max(x['p50']*.1,1000)
            for fixture in ('greeting','reply'):
                for metric in ('correlation','rms','peak_step'):
                    x=distribution(s[fixture]['onset_100ms'][metric] for s in populations[control]['samples'])
                    y=distribution(s[fixture]['onset_100ms'][metric] for s in populations['B']['samples'])
                    name=fixture+'_onset_100ms_'+metric
                    comparison['metrics'][name]=dict(A=x,B=y)
                    comparison['checks'][name]=(y['p50']>=max(.95,x['p50']*.9) if metric=='correlation'
                        else abs(y['p50']-x['p50'])<=x['p50']*.1 if metric=='rms'
                        else y['p50']<=x['p50']*1.1)
        comparison['passed']=all(comparison['checks'].values());result[control+'-B']=comparison
    write_json(out/'comparison.json',result)
    print(json.dumps({label:[k for k,v in comparison['checks'].items() if not v] for label,comparison in result.items()}),flush=True)
    repeat_failed_profiles(out,a.name,images,options,labels)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('--out',type=Path,required=True)
    p.add_argument('--original',type=Path,required=True);p.add_argument('--observer',type=Path,required=True)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--boots',type=int,default=3)
    p.add_argument('--option',action='append',default=[]);p.add_argument('--control-option',action='append',default=[])
    p.add_argument('--no-observer',action='store_true',help='Final A/B comparison after matched observer qualification')
    p.add_argument('--candidate-defaults',action='store_true',help='Measure the candidate compiled defaults without runtime overrides')
    a=p.parse_args()
    if a.boots<3:p.error('At least three boots per image')
    if a.candidate_defaults and a.option:p.error('--candidate-defaults excludes runtime --option overrides')
    run(a)
