#!/usr/bin/env python3
"""Interleave saved original/final images, measuring cold and warm turns.

Uses the step-7 acoustic, VAD, gate, leakage and live transcript assertions.
Original samples and their one-time reruns remain separate populations.
"""
import argparse
import json
import os
from pathlib import Path
import numpy as np
from alsa import resolve_jabra,set_volumes
from esp_serial import EspSerial
from phase2_campaign import flash
from step7_support import BotServer,deepgram_key
from e2e_common import turn,verify_session_continuity


def run(a):
    if a.pairs<10:raise ValueError('At least ten cold/warm pairs per image and mode')
    if not deepgram_key():raise RuntimeError('Live latency measurements require credentials')
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False,samples=[],reruns=[],pairs=a.pairs)
    def save():(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        os.environ['OFFLINE_BENCH_CONTINUOUS_AUDIO']='0'
        card,device=resolve_jabra();set_volumes(card,out/'mixer.log')
        for mode,live in [('offline-test',False),('live-test',True)]:
            for boot in range(1,a.pairs+1):
                for label,image in [('A',a.control.resolve()),('B',a.candidate.resolve())]:
                    directory=out/mode/label/f'boot{boot}';directory.mkdir(parents=True)
                    os.environ['AUDIO_OUT_AUTO_SILENCE']='1' if label=='A' else a.b_auto_silence
                    os.environ['AUDIO_OUT_10MS_CHUNKS']='4' if label=='A' else str(a.b_chunks)
                    flash(image,directory/'flash.log')
                    with BotServer(directory/'bot',mode) as bot,EspSerial('/dev/ttyACM0',log_path=directory/'serial.log') as board:
                        active=False;previous_pc=None
                        for temperature in ('cold','warm'):
                            sample=turn(board,bot,device,directory/temperature,live,
                                reset=not active,disconnect=False,elf=image/'dg-esp32.elf',options=a.b_option if label=='B' else ())
                            verify_session_continuity(sample,previous_pc)
                            sample.update(label=label,mode=mode,boot=boot,requested_temperature=temperature)
                            result['samples'].append(sample);active=sample['passed'];save()
                            if active:previous_pc=sample['pc_id']
                            print(mode,boot,label,temperature,sample.get('latency_ms',sample.get('error')),flush=True)
                            if not sample['passed']:
                                if sample.get('turn_assertions_passed'):
                                    # The turn helper left its successful audio
                                    # session connected before this extra check.
                                    try:
                                        board.command('audio 0');board.command('disconnect',40)
                                    except Exception as exc:
                                        sample['cleanup_error']=f'{type(exc).__name__}: {exc}'
                                    save()
                                repeat=turn(board,bot,device,directory/(temperature+'-rerun'),live,
                                    reset=True,disconnect=False,elf=image/'dg-esp32.elf',options=a.b_option if label=='B' else ())
                                verify_session_continuity(repeat)
                                repeat.update(label=label,mode=mode,boot=boot,original_temperature=temperature)
                                result['reruns'].append(repeat);active=repeat['passed'];save()
                                if active:previous_pc=repeat['pc_id']
                        board.command('audio 0');board.command('disconnect',40)
        result['distributions']={}
        for mode in ('offline-test','live-test'):
            result['distributions'][mode]={}
            for label in ('A','B'):
                result['distributions'][mode][label]={}
                for temperature in ('cold','warm'):
                    samples=[s for s in result['samples'] if s['mode']==mode and s['label']==label and s.get('temperature')==temperature]
                    summary={}
                    for stage in ('vad_stop','transcript','first_tts_byte','first_fixture_byte','audible_reply'):
                        values=[s['latency_ms'][stage] for s in samples if s.get('latency_ms',{}).get(stage) is not None]
                        summary[stage]=dict(n=len(values),median=float(np.median(values)) if values else None,
                            p95=float(np.percentile(values,95,method='lower')) if values else None)
                    setup={}
                    for stage in ('app_start_to_ready_including_retries','successful_start_to_ready',
                            'open_to_connected','dtls','wifi_ready_since_boot','certificate_generation'):
                        values=[s['setup_timings_ms'][stage] for s in samples if s.get('setup_timings_ms',{}).get(stage) is not None]
                        setup[stage]=dict(n=len(values),median=float(np.median(values)) if values else None,
                            p95=float(np.percentile(values,95,method='lower')) if values else None)
                    result['distributions'][mode][label][temperature]=dict(turns=len(samples),
                        passed=sum(s['passed'] for s in samples),stages_ms=summary,setup_ms=setup)
        coverage={}
        for mode,labels in result['distributions'].items():
            required=('vad_stop','audible_reply','transcript','first_tts_byte') if mode=='live-test' else (
                'vad_stop','audible_reply','first_fixture_byte')
            for label,temperatures in labels.items():
                for temperature,data in temperatures.items():
                    key='/'.join((mode,label,temperature))
                    coverage[key+':turns']=data['turns']>=10
                    for stage in required:
                        coverage[key+':'+stage]=data['stages_ms'][stage]['n']>=10
                    if temperature=='cold':
                        for stage in ('app_start_to_ready_including_retries','successful_start_to_ready',
                                'open_to_connected','dtls'):
                            coverage[key+':'+stage]=data['setup_ms'][stage]['n']>=10
        result['coverage_checks']=coverage
        result['coverage_passed']=all(coverage.values())
        result['passed']=all(s['passed'] for s in result['samples']) and result['coverage_passed']
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:save()
    return 0 if result['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--control',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--pairs',type=int,default=10);p.add_argument('--b-auto-silence',choices=['0','1'],default='1')
    p.add_argument('--b-chunks',type=int,default=4)
    p.add_argument('--b-option',action='append',default=[])
    raise SystemExit(run(p.parse_args()))
