#!/usr/bin/env python3
"""One reviewed image, two raw-slot captures; no bot/acoustic qualification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from echoear_aec_capture import dump_capture
from echoear_aec_alignment import analyze, probe_pcm, load_nearend_source
from echoear_aec_path import analyze_path, METHOD
from echoear_aec_resource_trial import command, snapshot, guard
from host_nic import snapshot as nic_snapshot, gate as nic_gate


def main(args):
    from demo_session import firmware_artifacts
    from esp_serial import EspSerial
    from step9a_echoear import verify, check_esptool, esptool_python
    from alsa import resolve_jabra, play_wav
    from echoear_uplink_ab import mixer
    from step9a_r2_checks import audit
    if not os.getenv('DG_BENCH_LOCK_FD'):raise RuntimeError('Use bench.py after Astra review and explicit B handoff')
    nearend_source=load_nearend_source(args.prompt) # Validate before board access.
    version=json.loads((args.image/'project_description.json').read_text())['project_version']
    if firmware_artifacts(args.image,version)!=args.elf:raise RuntimeError('Frozen ELF/bin mismatch')
    check_esptool()
    flash=json.loads((args.image/'flasher_args.json').read_text())
    files={args.image/name for name in ('dg-esp32.elf','flasher_args.json','project_description.json',*flash['flash_files'].values())}
    if any(not p.resolve().is_relative_to(args.image.resolve()) for p in files):raise RuntimeError('Flash input outside frozen bundle')
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files|{args.prompt}}
    hashes['generated_probe_pcm16_sha256']=hashlib.sha256(probe_pcm().astype('<i2').tobytes()).hexdigest()
    args.out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False,qualification=False,elf_sha256=args.elf,input_hashes=hashes,
                nic_before=nic_snapshot(),snapshots=[],cleanup_errors=[])
    board=None;ready=False;recording=False
    def save():(args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    def deadline(*_):raise TimeoutError('Single slot hold reached 175-second deadline')
    signal.signal(signal.SIGALRM,deadline);signal.alarm(175)
    try:
        port=verify(args.out)
        cmd=[esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800',
             '--after','no-reset','write-flash',*flash['write_flash_args']]
        for address,name in flash['flash_files'].items():cmd.extend([address,name])
        with (args.out/'flash.log').open('w') as log:
            subprocess.run(cmd,cwd=args.image,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=95)
        card,device=resolve_jabra();result['mixer_before']=mixer(card) # Read only.
        board=EspSerial(port,log_path=args.out/'serial.log',reset_on_open=False,display_checks=True)
        board.reset();board.wait_for('READY transport-console',35,since=0)
        board.wait_for('elf_sha256='+args.elf,3,since=0);ready=True
        for text in ('demo hold','audio 0','quiet 1','playback-volume -6'):
            command(board,text,38)
        def check(label):
            p=snapshot(board,args.out,label);guard(p)
            if p['audio'].get('write_timeouts')!=0:raise RuntimeError('I2S write timeout during slot trial')
            result['snapshots'].append(label);save();return p
        check('before-recording')
        captures={}
        for stage in ('dac','nearend'):
            command(board,'aec-record-slots 1');recording=True
            check(stage+'-allocated')
            time.sleep(1 if stage=='dac' else .3)
            result[stage+'_stimulus_host_start_s']=time.monotonic()
            if stage=='dac':
                cursor,seq=command(board,'aec-probe',8)
                board.wait_for(rf'^DONE #{seq} aec-probe$',5,since=cursor)
            else:
                # No output command/peer in this stage. Actual MIC3 must remain
                # quiet; otherwise the independent AFE-delay estimate fails.
                play_wav(args.prompt,device)
            result[stage+'_stimulus_host_end_s']=time.monotonic()
            time.sleep(.5)
            command(board,'aec-record-slots 0');recording=False
            check(stage+'-after')
            capture=dump_capture(board,args.out/(stage+'.npz'))
            if capture['schema']!=2 or capture['pcm'].shape[1]!=5:raise RuntimeError('All four raw slots required')
            captures[stage]=capture['pcm']
        analyzer=analyze_path if args.analysis_method==METHOD else analyze
        result['analysis_method']=args.analysis_method
        result['analysis']=analyzer(captures['dac'],captures['nearend'],nearend_source)
        result['mixer_after']=mixer(card)
        result['monitor']=board.display_monitor.result()
        result['passed']=bool(result['analysis']['passed'] and result['monitor']['passed'] and
                              result['mixer_before']==result['mixer_after'])
    except BaseException as exc:
        result['error']=type(exc).__name__+': '+str(exc)
    finally:
        signal.alarm(0)
        if board:
            board.display_cleanup=True
            if ready and recording:
                try:command(board,'aec-record-slots 0',3)
                except Exception as exc:result['cleanup_errors'].append(str(exc))
            board.close()
            try:
                result['raw_audit']=audit((args.out/'serial.log').read_bytes())
                result['boot_count']=(args.out/'serial.log').read_text().count('ESP-ROM:')
                result['passed'] &= result['raw_audit']['passed'] and result['boot_count']==1
            except Exception as exc:result.update(passed=False,raw_error=type(exc).__name__+': '+str(exc))
        for name,wanted in hashes.items():
            if name=='generated_probe_pcm16_sha256':continue
            if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=wanted:
                result.update(passed=False,input_changed=name)
        result['nic_after']=nic_snapshot();result['nic']=nic_gate(result['nic_before'],result['nic_after'])
        result['nic_policy']='Observe-only resource/alignment diagnostic; no network qualification'
        result['passed'] &= not result['cleanup_errors'];save()
    print(json.dumps(dict(passed=result['passed'],analysis_method=result.get('analysis_method'),
                          result_path=str(args.out/'result.json'),error=result.get('error'))),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',type=Path,required=True);p.add_argument('--elf',required=True)
    p.add_argument('--prompt',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--analysis-method',choices=['correlation-v1',METHOD],default='correlation-v1')
    raise SystemExit(main(p.parse_args()))
