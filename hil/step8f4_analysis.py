#!/usr/bin/env python3
"""Offline 8f4 idle/quiet corrections against frozen 8f3 rules."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import numpy as np
from esp_serial import ROOT
from playback_quality import decoded_match
from step7_support import pcm
from step8f2_analysis import load,write,perf_history,traces
from step8e2b_analysis import device_send_cadence,console_windows,lcd_memory_gate
from uplink_continuity import link_tail,recorded_audio_idle_windows

BASELINE_REF='27ce97903cb0f5b5b8977c5c0508ce059841f308'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--baseline-ref',default=BASELINE_REF)
    p.add_argument('--evidence-root',type=Path,default=ROOT.parent/'evidence')
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    paired=a.out/'paired-analysis'
    subprocess.run([sys.executable,str(ROOT/'hil/step8f3_analysis.py'),
        '--baseline-ref',a.baseline_ref,'--evidence-root',str(a.evidence_root),'--out',str(paired)],check=True)
    spec=importlib.util.spec_from_file_location('baseline_quality',paired/'playback-quality-base.py')
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    source=pcm(ROOT/'hil/fixtures/step8f3-tts-reference.wav');regressions={}
    for speech_ms in (250,375):
        decoded=source.copy();n=speech_ms*16
        decoded[16*1600:16*1600+n]=source[3200:3200+n]
        decoded=np.r_[np.zeros(500),decoded,np.zeros(500)]
        regressions[f'speech-in-pause-{speech_ms}ms']=dict(
            before=baseline.decoded_match(decoded,source),after=decoded_match(decoded,source))
    for pause_ms in (400,1000):
        n=pause_ms*16;cut=16000
        reference=np.r_[source[:cut],np.random.default_rng(8).normal(0,1,n),source[cut:]]
        decoded=np.r_[np.zeros(500),source[:cut],np.zeros(n),source[cut:],np.zeros(500)]
        regressions[f'quiet-pause-{pause_ms}ms']=dict(
            before=baseline.decoded_match(decoded,reference),after=decoded_match(decoded,reference))
    write(a.out/'pause-regressions.json',regressions)
    offline=a.evidence_root/'step8f3/offline/test'
    r=load(offline/'result.json');samples=perf_history(offline/'serial.log');trace=traces(offline)
    packets=[json.loads(line) for line in (offline/'rtp/rtp.jsonl').read_text().splitlines()]
    start=min(t['prompt_start'] for t in r['runs'] if 'prompt_start' in t)
    end=max(t['reply_window_transport']['end'] for t in r['runs'] if 'reply_window_transport' in t)
    windows=recorded_audio_idle_windows(offline/'serial.log',end=end)
    link=link_tail(packets,start,end,excluded_windows=windows)
    old=load(offline/'result.json')['link_tail']
    before={(g['pc_id'],g['ssrc'],g['start_s'],g['end_s']):g for g in old['gaps']}
    movements=[]
    for g in link['gaps']:
        prior=before[(g['pc_id'],g['ssrc'],g['start_s'],g['end_s'])]
        if prior['category']!=g['category']:
            window=next(w for w in windows if g['start_s']<=w['end_s'] and g['end_s']>=w['start_s'])
            movements.append(dict(before=prior,after=g,window=window,
                excess_before_ms=max(0,window['start_s']-g['start_s'])*1000,
                excess_after_ms=max(0,g['end_s']-window['end_s'])*1000))
    counter_brackets=[]
    for before_run,after_run in zip(r['runs'],r['runs'][1:]):
        before_perf,after_perf=before_run['after_perf'],after_run['before_perf']
        if before_perf.get('peer_profile',{}).get('socket_send_enomem')==after_perf.get('peer_profile',{}).get('socket_send_enomem'):continue
        fields=('dropped','late','lost','send_budget_cutoffs','stale_audio_skipped')
        counter_brackets.append(dict(before={k:before_perf.get(k) for k in fields},
            after={k:after_perf.get(k) for k in fields},
            socket_before={k:before_perf['peer_profile'].get(k) for k in ('socket_send_enomem','socket_send_errors')},
            socket_after={k:after_perf['peer_profile'].get(k) for k in ('socket_send_enomem','socket_send_errors')},
            send_cutoff_before=before_perf['drop_reasons']['send_cutoff'],
            send_cutoff_after=after_perf['drop_reasons']['send_cutoff']))
    write(a.out/'offline-gates.json',dict(passes=r['pass_count'],acquisition_passed=r['passed'],
        baseline_link_passed=old['passed'],link=link,movements=movements,counter_brackets=counter_brackets,
        lcd=lcd_memory_gate(samples),cadence=device_send_cadence(trace,samples[0]['device_us'],samples[-1]['device_us'],
            console_windows(offline/'serial.log')+recorded_audio_idle_windows(offline/'serial.log',clock='device_us',end=samples[-1]['device_us']),snapshots=samples)))
    print(json.dumps(dict(offline=dict(passed=link['passed'],intervals=link['intervals'],
        over60=link['over_threshold'],extremes=link['over_extreme_threshold'],maximum_ms=link['eligible_maximum_ms'],
        idle=link['categories']['idle'],movements=movements),
        regressions={name:{phase:dict(passed=gate['passed'],correlation=gate['correlation'],quiet_failures=gate.get('quiet_block_failures'))
            for phase,gate in rows.items()} for name,rows in regressions.items()}),indent=2))


if __name__=='__main__':main()
