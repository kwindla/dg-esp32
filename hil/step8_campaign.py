#!/usr/bin/env python3
"""Retained step-8 screens against a fresh, saved 4097a13 control per knob.

Screens nominate candidates; they are not acceptance or formal comparisons.
Builds use the fresh-config procedure and export only requested defaults.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import numpy as np
import phase2_campaign as campaign
from esp_serial import ROOT
from step8_summary import profile

EVIDENCE=ROOT.parent/'evidence/step8'
CONFIGS={
    'data-cache64':{'CONFIG_ESP32S3_DATA_CACHE_64KB':'y'},
    'cache-line64':{'CONFIG_ESP32S3_DATA_CACHE_LINE_64B':'y'},
    'flash-qio80':{'CONFIG_ESPTOOLPY_FLASHMODE_QIO':'y'},
    'udp-mbox16':{'CONFIG_LWIP_UDP_RECVMBOX_SIZE':'16'},
    'wifi-rx64':{'CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM':'64'},
    'ampdu-tx-off':{'CONFIG_ESP_WIFI_AMPDU_TX_ENABLED':'n'},
    'ampdu-rx-off':{'CONFIG_ESP_WIFI_AMPDU_RX_ENABLED':'n'},
    'mic-dma4':{'CONFIG_DG_MIC_DMA_DESCRIPTORS':'4'},
    'speaker-dma6':{'CONFIG_DG_SPEAKER_DMA_DESCRIPTORS':'6'},
    'cert-overlap':{'CONFIG_DG_CERT_OVERLAP_WIFI':'y'},
    'mic-dma320':{'CONFIG_DG_MIC_DMA_FRAMES':'320'},
    'speaker-dma160':{'CONFIG_DG_SPEAKER_DMA_FRAMES':'160'},
}
RUNTIME={
    'direct-socket-select':['socket-select=1'],
    'native-wait20':['recv=20'], 'native-event-wait':['delay=0'],
    'extra-wait10':['delay=10'], 'opus16':['bitrate=16000'],
    'opus32':['bitrate=32000'], 'playout40':['target=2'],
    'playout60':['target=3'], 'playout80':['target=4'],
    'jitter60':['jitter=60'], 'owner-priority8':['priority=8'],
    'dtls-aes-gcm':['aes-gcm=1'],
    'server-chunks2':[], 'server-idle': ['idle-gaps=1'],
    'producer-event-wake':['event-wake=1'],
}

def run_logged(argv,path,env=None):
    with path.open('w') as stream:
        return subprocess.run([str(s) for s in argv],cwd=ROOT,env=env,
            stdout=stream,stderr=subprocess.STDOUT).returncode

def builds():
    base=EVIDENCE/'control-defaults.txt'
    if not base.exists():base.write_text((ROOT/'sdkconfig.defaults').read_text())
    campaign.EVIDENCE=EVIDENCE
    try:
        for name,settings in CONFIGS.items():campaign.build(name,settings)
    finally:(ROOT/'sdkconfig.defaults').write_text(base.read_text())

def capture_profile(name,label,image,directory,seconds,options=(),attempts=2):
    role=label[0]
    env=os.environ.copy()
    env['AUDIO_OUT_AUTO_SILENCE']='0' if role=='B' and name=='server-idle' else '1'
    env['AUDIO_OUT_10MS_CHUNKS']='2' if role=='B' and name=='server-chunks2' else '4'
    outputs=[]
    for attempt in range(1,attempts+1):
        sample=directory/f'{label}{attempt}'
        campaign.flash(image,directory/f'{label}{attempt}-flash.log')
        argv=[sys.executable,ROOT/'hil/perf_test.py','--seconds',str(seconds),
            '--out',sample,'--elf',image/'dg-esp32.elf']
        if role=='A':argv+=['--no-trace']
        for option in options:argv+=['--option',option]
        code=run_logged(argv,directory/f'{label}{attempt}.log',env)
        raw=json.loads((sample/'result.json').read_text())
        if raw['passed']:
            summary=profile(sample/'result.json')
            (sample/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        outputs.append(dict(label=label,attempt=attempt,exit_code=code,
            passed=raw['passed'],path=str(sample)))
        print(name,label,attempt,'captured' if raw['passed'] else raw.get('error'),flush=True)
        if raw['passed']:break
    return outputs

def screen(name,seconds):
    directory=EVIDENCE/'screens'/name;directory.mkdir(parents=True,exist_ok=False)
    outputs=[]
    for label in ('A','B'):
        image=EVIDENCE/'control-image' if label=='A' else EVIDENCE/'images'/name if name in CONFIGS else EVIDENCE/'images/instrument-baseline-v7'
        outputs+=capture_profile(name,label,image,directory,seconds,RUNTIME.get(name,[]) if label=='B' else [])
    checks=screen_checks(directory)
    if checks and not all(checks.values()):
        for label in ('A-repeat','B-repeat'):
            image=EVIDENCE/'control-image' if label[0]=='A' else EVIDENCE/'images'/name if name in CONFIGS else EVIDENCE/'images/instrument-baseline-v7'
            outputs+=capture_profile(name,label,image,directory,seconds,RUNTIME.get(name,[]) if label[0]=='B' else [])
    (directory/'screen.json').write_text(json.dumps(dict(captures=outputs,original_checks=checks),indent=2)+'\n')

def screen_checks(directory):
    first={}
    for label in ('A','B'):
        file=directory/f'{label}1/result.json'
        if not file.exists() or not json.loads(file.read_text())['passed']:return None
        first[label]=profile(file)
    checks={}
    for phase in ('idle','duplex'):
        a=next(p for p in first['A'] if p['name']==phase);b=next(p for p in first['B'] if p['name']==phase)
        raw=json.loads((directory/'B1/result.json').read_text())
        after=next(p['after'] for p in raw['phases'] if p['name']==phase)
        checks[phase+':internal_floor']=after['heap'].get('general_internal',0)>=120*1024
        for key,value in a['counters'].items():checks[phase+':'+key]=b['counters'][key]<=value+max(.1*value,1 if key not in ('decode_errors','i2s_underflows','queue_dropped') else 0)
        for key,value in a['network']['rtp.jsonl'].items():
            if key=='packets':continue
            candidate=b['network']['rtp.jsonl'][key]
            checks[phase+':uplink:'+key]=value is not None and candidate is not None and candidate<=value+max(.1*value,1)
        for key in ('encode_p50_us','encode_p95_us','decode_p50_us','decode_p95_us'):
            value=a[key];checks[phase+':'+key]=b[key]<=value+max(.1*value,1000)
    return checks

def distribution(values):
    return dict(n=len(values),p50=float(np.median(values)),
        p90=float(np.percentile(values,90,method='lower'))) if values else None

def formal(name,boots,candidate):
    if boots<3:raise ValueError('At least three interleaved boots')
    directory=EVIDENCE/'formal'/name;directory.mkdir(parents=True,exist_ok=False)
    records=[]
    for boot in range(1,boots+1):
        out=directory/f'boot{boot}';out.mkdir()
        for label,image in [('A',EVIDENCE/'control-image'),('C',EVIDENCE/'images/instrument-baseline-v7'),('B',candidate)]:
            records+=capture_profile(name,label,image,out,20,RUNTIME.get(name,[]) if label=='B' else [])
            (directory/'captures.json').write_text(json.dumps(records,indent=2)+'\n')
    cohorts={}
    for label in ('A','C','B'):
        # Failed first attempts are not replaced by their successful rerun.
        samples=[profile(Path(r['path'])/'result.json') for r in records if r['label']==label and r['attempt']==1 and r['passed']]
        cohorts[label]={}
        for phase in ('idle','duplex'):
            values={}
            for sample in samples:
                point=next(p for p in sample if p['name']==phase)
                for metric in ('encode_p50_us','encode_p95_us','decode_p50_us','decode_p95_us'):
                    values.setdefault(metric,[]).append(point[metric])
                cpu={}
                for t in point['tasks']:cpu[t['name']]=cpu.get(t['name'],0)+t['cpu_percent']
                for task,cost in cpu.items():values.setdefault('cpu:'+task,[]).append(cost)
                for metric,cost in point['counters'].items():values.setdefault(metric,[]).append(cost)
                for metric,cost in point['network']['rtp.jsonl'].items():
                    if cost is not None:values.setdefault('uplink:'+metric,[]).append(cost)
            cohorts[label][phase]={k:distribution(v) for k,v in values.items()}
    (directory/'distributions.json').write_text(json.dumps(cohorts,indent=2)+'\n')
    return cohorts

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--build-all',action='store_true')
    p.add_argument('--screen',nargs='*');p.add_argument('--seconds',type=int,default=20)
    p.add_argument('--formal');p.add_argument('--boots',type=int,default=3)
    p.add_argument('--candidate',type=Path)
    a=p.parse_args()
    if a.build_all:builds()
    for name in a.screen or []:
        if name not in CONFIGS and name not in RUNTIME:p.error('Unknown screen '+name)
        screen(name,a.seconds)
    if a.formal:
        if a.formal not in CONFIGS and a.formal not in RUNTIME:p.error('Unknown formal comparison')
        formal(a.formal,a.boots,a.candidate.resolve() if a.candidate else EVIDENCE/'images/instrument-baseline-v7')
