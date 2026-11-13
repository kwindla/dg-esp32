#!/usr/bin/env python3
"""Measured step-6a levers against a saved committed control, boot by boot.

Artifacts live outside the repository. Only allowlisted configuration values
are exported. Formal sessions and every rerun remain separate populations.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import numpy as np
from esp_serial import ROOT, port_lock
from phase1_campaign import PYTHON, run
from phase1_summary import summarize

EVIDENCE = ROOT.parent/'evidence/step6a-p2'
METRICS = ['encode_p50_us','encode_p95_us','encode_max_us','decode_p50_us','decode_p95_us','decode_max_us',
           'enqueue_send_mean_us','enqueue_send_max_us','loop_max_us','dtls_duration_us',
           'late','lost','concealed','underruns','playout_dropped','queue_dropped','rebuffers','drift_drops','plc_frames',
           'i2s_underflows','decode_errors','send_errors']

def write_json(path, value):
    path.write_text(json.dumps(value,indent=2)+'\n')

def distribution(values):
    values = [float(v) for v in values if v is not None]
    if not values: return None
    return dict(n=len(values), min=min(values), p50=float(np.median(values)),
                p90=float(np.percentile(values,90,method='lower')), max=max(values))

def build(name, settings):
    out=EVIDENCE/'images'/name
    if (out/'identity.json').exists():
        assert json.loads((out/'settings.json').read_text())==settings, 'Saved image settings differ'
        return out
    out.mkdir(parents=True,exist_ok=True)
    base=(EVIDENCE/'control-defaults.txt').read_text()
    lines=[]; seen=set()
    for line in base.splitlines():
        if line.startswith('CONFIG_'):
            key=line.split('=',1)[0];seen.add(key)
            if key in settings: line=f'{key}={settings[key]}'
        lines.append(line)
    lines += ['', '# Step 6a measured configuration.']+[f'{k}={v}' for k,v in settings.items() if k not in seen]
    (ROOT/'sdkconfig.defaults').write_text('\n'.join(lines)+'\n')
    write_json(out/'settings.json',settings)
    shutil.copyfile(ROOT/'sdkconfig.defaults',out/'defaults.txt')
    for command,label in [([ROOT/'scripts/idf.sh','fullclean'],'clean')]:
        if run(command,out/f'{label}.log'): raise RuntimeError(f'{name}: {label} failed')
    (ROOT/'sdkconfig').unlink(missing_ok=True)
    for command,label in [([ROOT/'scripts/idf.sh','reconfigure'],'configure'),
                          ([sys.executable,ROOT/'scripts/check-sdkconfig.py'],'config-check'),
                          ([ROOT/'scripts/idf.sh','build'],'build')]:
        if run(command,out/f'{label}.log'): raise RuntimeError(f'{name}: {label} failed; see retained log')
    for suffix in ['bin','elf','map']: shutil.copyfile(ROOT/f'build/dg-esp32.{suffix}',out/f'dg-esp32.{suffix}')
    shutil.copyfile(ROOT/'build/bootloader/bootloader.bin',out/'bootloader.bin')
    shutil.copyfile(ROOT/'build/partition_table/partition-table.bin',out/'partition-table.bin')
    spec=importlib.util.spec_from_file_location('sdkchecker',ROOT/'scripts/check-sdkconfig.py')
    checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
    actual=checker.settings(ROOT/'sdkconfig');expected=checker.settings(ROOT/'sdkconfig.defaults')
    write_json(out/'resolved-defaults.json',{k:actual.get(k) for k in expected})
    write_json(out/'identity.json',{suffix:hashlib.sha256((out/f'dg-esp32.{suffix}').read_bytes()).hexdigest() for suffix in ['bin','elf','map']})
    print(f'Built {name}',flush=True)
    return out

def flash(image, log):
    log.parent.mkdir(parents=True,exist_ok=True)
    with port_lock(),log.open('w') as stream:
        subprocess.run(['bash','-c','. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null; exec python -m esptool "$@"',
                        'flash-saved','--chip','esp32s3','--port','/dev/ttyACM0','--baud','460800','write-flash',
                        '0x0',str(image/'bootloader.bin'),'0x8000',str(image/'partition-table.bin'),
                        '0x10000',str(image/'dg-esp32.bin')],stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=120)

def capture(image, directory, sessions=3, concurrent=False):
    directory.parent.mkdir(parents=True,exist_ok=True)
    flash(image,directory.parent/f'{directory.name}-flash.log')
    argv=[PYTHON,ROOT/'hil/transport_test.py','--campaign','--rerun-failed','--sessions',str(sessions),
          '--elf',image/'dg-esp32.elf','--out',directory]
    if concurrent: argv.append('--concurrent-sctp')
    code=run(argv,directory.parent/f'{directory.name}.log')
    print(f'{directory.parent.name}/{directory.name}: exit={code}',flush=True)
    if len(list(directory.glob('[0-9]*/result.json')))!=sessions:
        raise RuntimeError(f'Incomplete capture: {directory}; samples retained')
    return code

def cohort(directory):
    result=summarize(directory)
    sessions=[];reruns=[]
    for boot in sorted(directory.glob('boot*')):
        if not boot.is_dir():continue
        top=json.loads((boot/'result.json').read_text())
        previous_underflows=0
        # Hardware underflows belong to the persistent audio pipeline. Convert
        # its boot-cumulative counter into session deltas without losing totals.
        for s in top.get('sessions',[]):
            ordered=[s]+[r for r in top.get('reruns',[]) if r['index']==s['index']]
            for item in ordered:
                total=item['closed'].get('i2s_underflows',0)
                item['closed']['i2s_underflows_total']=total
                item['closed']['i2s_underflows']=max(0,total-previous_underflows)
                previous_underflows=total
            sessions.append(dict(boot=boot.name,**s))
            reruns.extend(dict(boot=boot.name,**r) for r in ordered[1:])
    for path in sorted((directory/'stat-outliers').glob('*/source.json')):
        source=json.loads(path.read_text());top=json.loads((path.parent/'result.json').read_text())
        if top.get('sessions'):
            reruns.append(dict(top['sessions'][0],boot=source['boot'],index=source['index'],statistical_outlier=True))
    values={k:[s['closed'][k] for s in sessions if k in s['closed']] for k in METRICS}
    values.update({k:[s['rtp'][k] for s in sessions] for k in ['missing','max_gap_ms','p95_gap_ms']})
    values.update({f'{k}_correlation':[s[k]['correlation'] for s in sessions] for k in ['greeting','reply']})
    values.update({k:[s.get('setup',{}).get(k) for s in sessions] for k in ['start_ready_us','open_connected_us','wifi_to_ready_us']})
    # Boot-only association time has one observation per boot.
    values['wifi_ready_us']=[float(v) for boot in directory.glob('boot*/serial.log')
                            for v in re.findall(r'WIFI READY t=(\d+)',boot.read_text(errors='replace'))[-1:]]
    result['metrics']={k:distribution(v) for k,v in values.items()}
    result['samples']=sessions;result['reruns']=reruns
    result['events']={k:sum(bool(s['rtp']['missing'] if k=='missing' else s['closed'].get(k,0)) for s in sessions)
                      for k in ['missing','i2s_underflows','rebuffers']}
    # Use formal session snapshots for ready/streaming; exclude reruns/speech.
    for phase in ['ready','streaming','after-disconnect']:
        rows=[r for s in sessions for r in s.get('heap_rows',[]) if r['phase']==phase]
        if rows:result['heap_phases'][phase]={k:distribution(r.get(k) for r in rows) for k in rows[0] if k!='phase'}
    write_json(directory/'summary.json',result)
    return result

def failures(sample):
    """All observable failures, including those masked by an earlier assert."""
    result=set()
    for name in ['greeting','reply']:
        if name in sample and not sample[name]['passed']:result.add(name+'_fixture')
    if sample.get('tone',{}).get('rejected_samples',0):result.add('source_backpressure')
    r=sample.get('rtp',{});c=sample.get('closed',{})
    if r.get('missing',0):result.add('rtp_loss')
    if r.get('timestamp_steps_correct') is False:result.add('rtp_timestamp')
    if r.get('max_gap_ms',0)>=200 or ('duration_ms' in r and abs(r['duration_ms']-9980)>=500):result.add('rtp_pacing')
    for key in ['queue_dropped','decode_errors','send_errors','i2s_underflows','rebuffers']:
        if c.get(key,0):result.add(key)
    if c.get('tasks_exited') is False:result.add('tasks_exited')
    for key,size in [('tx_stack',c.get('tx_stack_bytes',32768)),('owner_stack',c.get('owner_stack_bytes',10240)),
                     ('rx_stack',c.get('rx_stack_bytes',8192)),('decode_stack',c.get('playback_stack_bytes',17408))]:
        if key in c and c[key]<size*.25:result.add(key)
    if 'acceptance_error' in sample:
        aliases={'Fixture correlation/duration failed':'fixture','Server RTP loss/timing':'rtp_loss_timing',
                 'Server RTP pacing':'rtp_pacing','Tone source PCM backpressure':'source_backpressure','Local queue overflow or invalid frames':'queue_dropped'}
        message=sample['acceptance_error']
        if 'margin below 25%' in message:result.add(message.split()[0])
        elif message.startswith('i2s_underflows:'):
            if c.get('i2s_underflows',0):result.add('i2s_underflows')
        elif message.startswith('rebuffers:'):result.add('rebuffers')
        elif message=='Fixture correlation/duration failed' and ('greeting' in sample or 'reply' in sample):pass
        elif message=='Server RTP loss/timing' and r:pass
        else:result.add(aliases.get(message,message or 'unspecified_acceptance'))
    return result


def compare(a,b,expected=9,performance=False):
    checks={};details={}
    checks['complete']=a['media_sessions']==b['media_sessions']==expected and not a['boot_errors'] and not b['boot_errors']
    for key,x in a['metrics'].items():
        y=b['metrics'].get(key)
        if not x or not y:checks[key]=False;continue
        # Timing floor: 1 ms. Counts/gaps: 1 packet / 1 ms.
        floor=1000 if key.endswith('_us') else 1
        if key.endswith('_correlation'):
            bound=x['p50']*.9;passed=y['p50']>=max(.95,bound)
        else:
            bound=x['p50']+max(x['p50']*.1,floor);passed=y['p50']<=bound
        checks[key]=bool(passed);details[key]=dict(A=x,B=y,p50_bound=bound)
    for key in ['missing','i2s_underflows','rebuffers']:
        checks[f'{key}_event_count']=b['events'][key]<a['events'][key]+2
    # Nonreproducing single media outliers are recorded, never removed.
    reproduced=[];not_reproduced=[]
    for s in b['samples']:
        if 'acceptance_error' not in s:continue
        repeats=[r for r in b['reruns'] if r['boot']==s['boot'] and r['index']==s['index']]
        row=dict(boot=s['boot'],index=s['index'],error=s['acceptance_error'])
        overlap=failures(s)&failures(repeats[0]) if repeats else failures(s)
        row['reproduced_metrics']=sorted(overlap)
        if repeats and not overlap:not_reproduced.append(row)
        else:reproduced.append(row)
    checks['reproduced_media_failures']=not reproduced
    # Keep formal event counts unchanged; the explicit rerun exception affects
    # the decision only. A measured repeat with zero events does not reproduce
    # that original event, even if a different assertion fails in the repeat.
    event_exceptions={}
    for key in ['missing','i2s_underflows','rebuffers']:
        cleared=[]
        for s in b['samples']:
            repeats=[r for r in b['reruns'] if r['boot']==s['boot'] and r['index']==s['index']]
            original=s.get('rtp',{}).get(key,0) if key=='missing' else s.get('closed',{}).get(key,0)
            repeated=(repeats[0].get('rtp',{}).get(key) if key=='missing' else repeats[0].get('closed',{}).get(key)) if repeats else None
            if original and repeated==0:cleared.append(dict(boot=s['boot'],index=s['index']))
        event_exceptions[key]=cleared
        checks[f'{key}_event_count']=b['events'][key]-len(cleared)<a['events'][key]+2
    checks['capture_errors']=bool(b['audio_counters']) and all(not any(row) for row in b['audio_counters'])
    ready_a=a['heap_phases'].get('ready',{}).get('general_internal')
    ready_b=b['heap_phases'].get('ready',{}).get('general_internal')
    gain=ready_b['p50']-ready_a['p50'] if ready_a and ready_b else None
    checks['ready_gain']=gain is not None and gain>=1024 if not performance else all(b['metrics']['encode_p95_us'][q]<a['metrics']['encode_p95_us'][q] for q in ['p50','p90'])
    return dict(passed=all(checks.values()),checks=checks,metrics=details,ready_gain_bytes=gain,
                reproduced_failures=reproduced,nonreproducing_failures=not_reproduced,
                A_events=a['events'],B_events=b['events'],nonreproducing_event_exceptions=event_exceptions)

def campaign(name, image, boots=3, concurrent=False,performance=False):
    out=EVIDENCE/name;out.mkdir(parents=True,exist_ok=True)
    for i in range(1,boots+1):
        for label,firmware in [('A',EVIDENCE/'images/control'),('B',image)]:
            directory=out/label/f'boot{i}'
            if (directory/'result.json').exists():
                if len(list(directory.glob('[0-9]*/result.json')))!=3:raise RuntimeError(f'Incomplete existing {directory}')
                continue
            capture(firmware,directory,concurrent=concurrent)
    comparison=compare(cohort(out/'A'),cohort(out/'B'),boots*3,performance)
    write_json(out/'comparison.json',comparison)
    print(f'{name}: passed={comparison["passed"]}, gain={comparison["ready_gain_bytes"]}, failures='+','.join(k for k,v in comparison['checks'].items() if not v),flush=True)
    return comparison

def main():
    p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('--settings',default='{}');p.add_argument('--boots',type=int,default=3)
    p.add_argument('--concurrent-sctp',action='store_true');p.add_argument('--performance',action='store_true');p.add_argument('--build-only',action='store_true')
    a=p.parse_args();image=build(a.name,json.loads(a.settings))
    if not a.build_only:campaign(a.name,image,a.boots,a.concurrent_sctp,a.performance)
if __name__=='__main__':main()
