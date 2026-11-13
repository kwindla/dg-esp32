#!/usr/bin/env python3
"""Receive-path closing evidence, immutable DIO production CPU A/A and A/B."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from esp_serial import ROOT,EspSerial
from phase2_campaign import flash
from step8c_analysis import summarize_cpu


def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def command(log,args):
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w') as stream:
        result=subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT)
    return result.returncode


def cpu(out,label,image,seconds=140):
    target=out/'cpu'/label
    if target.exists():raise RuntimeError('Retained CPU block exists: '+label)
    flash(image,target.with_suffix('.flash.log'))
    code=command(target.with_suffix('.log'),[ROOT/'hil/step8c_capture.py','--out',target,
        '--elf',image/'dg-esp32.elf','--wifi-host','192.168.7.222','--ethernet-host','192.168.42.75',
        '--duration',seconds,'--cpu-window',30,'--fixed-mic','--cpu-only','--gate','open','--payload-sizes',
        '--option','call-timing=0','--label',label])
    if code:raise RuntimeError('CPU capture failed: '+label)
    summary=summarize_cpu(target)
    if not summary['stimulus_complete']:raise RuntimeError('CPU stimulus incomplete: '+label)
    snapshots=json.loads((target/'webrtc-ethernet/cpu-snapshots.json').read_text())
    coverage=[]
    for before,after in zip(snapshots,snapshots[1:]):
        seconds=(after['device_us']-before['device_us'])/1e6
        if seconds<20:continue
        decoded=(after['playout']['decoded_packets']-before['playout']['decoded_packets']
            if after.get('playout_schema')==2 else after['decode_frames']-before['decode_frames'])
        coverage.append(dict(duration_s=seconds,decoded=decoded,minimum=int(seconds*2900/60)))
    checks=dict(codec_stimulus=bool(coverage) and all(r['decoded']>=r['minimum'] for r in coverage),
        production=all(r.get('uplink_frame_ms')==20 and r.get('opus_fec') is False and
            r.get('eager_codec_initialization') is True and r.get('receive_budget_after_read') is True for r in snapshots),
        diagnostics_off=all(r['peer_profile']['full_call_timing'] is False and
            r['peer_profile']['trace']['enabled'] is False for r in snapshots))
    save(target/'protocol-audit.json',dict(checks=checks,decode_coverage=coverage))
    if not all(checks.values()):raise RuntimeError('CPU protocol mismatch: '+label)
    save(target/'summary.json',summary)
    return summary


def crossover(out,name,left,right,pairs=4):
    record=dict(completed=False,name=name,runs=[],protocol=dict(fresh_boots_per_arm=pairs,
        duration_s=140,window_s=30,arrival_trace=False,call_timing=False,cache_counters=False,
        production_dsp_target_dbfs=-22,production_ceiling_percent=26))
    try:
        for pair in range(pairs):
            for arm in ('A','B') if pair%2==0 else ('B','A'):
                image=left if arm=='A' else right
                label=f'{name}-{arm}{pair+1:02}'
                print('CPU '+label,flush=True)
                summary=cpu(out,label,image)
                record['runs'].append(dict(pair=pair+1,arm=arm,image=str(image),
                    elf_sha256=hashlib.sha256((image/'dg-esp32.elf').read_bytes()).hexdigest(),summary=summary))
                save(out/(name+'.json'),record)
        record['completed']=True
    finally:save(out/(name+'.json'),record)


def closing(out,image,baseline):
    elf=image/'dg-esp32.elf';calibration=out/'voice-final/control/voice'
    prior=json.loads((calibration/'result.json').read_text())
    if 'calibration' not in prior:raise RuntimeError('No usable same-image calibration')
    result=dict(completed=False,checks={})
    try:
        os.environ.update(BOT_ICE_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',
            BOT_BENCH_SERVER_URL='http://192.168.7.222:7861',STEP8E_TRACE='1')
        print('CONTROL supplemental confirmation; original retained',flush=True)
        result['checks']['control_confirmation']=command(out/'control-confirmation.log',
            [ROOT/'hil/agent_conversation_test.py','--out',out/'control-confirmation','--elf',elf,
             '--runs',1,'--turn-limit',1,'--fault-free','--calibration-from',calibration])
        print('TWO-HOP 300 seconds',flush=True)
        result['checks']['two_hop']=command(out/'two-hop.log',[ROOT/'hil/step8c_capture.py',
            '--out',out/'two-hop','--elf',elf,'--wifi-host','192.168.7.222','--ethernet-host','192.168.42.75',
            '--duration',300,'--cpu-window',30,'--fixed-mic','--gate','auto','--voice-path','wifi','--label','8e2a-two-hop'])
        print('TWO-HOP strict voice',flush=True)
        os.environ.update(BOT_ICE_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',
            BOT_BENCH_SERVER_URL='http://192.168.7.222:7861',BOT_BENCH_STUN_URL='stun:192.168.42.75:3478')
        with (out/'two-hop-voice-stun.log').open('w') as log:
            stun=subprocess.Popen([sys.executable,str(ROOT/'hil/netprobe.py'),'--stun-bind','192.168.42.75',
                '--duration','600','--out',str(out/'two-hop-voice-stun.jsonl')],stdout=log,stderr=subprocess.STDOUT)
            try:
                result['checks']['two_hop_voice']=command(out/'two-hop-voice.log',[ROOT/'hil/agent_conversation_test.py',
                    '--out',out/'two-hop-voice','--elf',elf,'--runs',1,'--turn-limit',1,'--fault-free',
                    '--calibration-from',calibration])
            finally:
                stun.terminate();stun.wait(timeout=15)
                os.environ.pop('BOT_BENCH_STUN_URL',None)
        env={'BOT_ICE_INTERFACE':'wlp8s0','BOT_ICE_REMOTE_NETWORK_ONLY':'0',
             'BOT_BENCH_SERVER_URL':'http://192.168.7.222:7861','BOT_VOICE_POLICY':'smartturn',
             'STEP8E_TRACE':'1'}
        os.environ.update(env)
        print('ONSET six turns',flush=True)
        result['checks']['onset']=command(out/'onset.log',[ROOT/'hil/agent_conversation_test.py',
            '--out',out/'onset-six','--elf',elf,'--runs',1,'--turn-limit',6,'--fault-free',
            '--calibration-from',calibration])
        print('SOAK 30 timed turns plus warmup',flush=True)
        result['checks']['soak']=command(out/'soak.log',[ROOT/'hil/agent_conversation_test.py',
            '--out',out/'soak-30','--elf',elf,'--soak-minutes',10,'--fault-free',
            '--calibration-from',calibration])
        os.environ.update(BOT_ICE_INTERFACE='enp7s0',BOT_ICE_REMOTE_NETWORK_ONLY='1',
            BOT_BENCH_SERVER_URL='http://192.168.42.75:7861',STEP8E_TRACE='0')
        print('FINAL fault-free e2e on the qualified 8c2 Ethernet path',flush=True)
        with (out/'final-e2e-stun.log').open('w') as log:
            stun=subprocess.Popen([sys.executable,str(ROOT/'hil/netprobe.py'),'--stun-bind','192.168.42.75',
                '--duration','900','--out',str(out/'final-e2e-stun.jsonl')],stdout=log,stderr=subprocess.STDOUT)
            try:
                result['checks']['e2e']=command(out/'final-e2e.log',[ROOT/'hil/e2e_voice_test.py',
                    '--out',out/'final-e2e','--elf',elf,'--runs',5,'--warm',
                    '--server-url','http://192.168.42.75:7861','--ice-servers','stun:192.168.42.75:3478'])
            finally:stun.terminate();stun.wait(timeout=15)
        print('SERVER acceptance with bot port free',flush=True)
        with (out/'server-final.log').open('w') as stream:
            code=subprocess.run([str(ROOT/'server/.venv/bin/python'),'-m','pytest','server/tests','-q'],
                cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT).returncode
        result['checks']['server_tests']=code
        # A/A uses the exact new image, then the committed 8c2 production image
        # supplies A for A/B. No experimental QIO/XIP image is admissible.
        crossover(out,'cpu-aa',image,image)
        crossover(out,'cpu-ab',baseline,image)
        result['completed']=True
    finally:
        flash(image,out/'final-production-restore.log')
        with EspSerial('/dev/ttyACM0',log_path=out/'final-production-boot.log') as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            identity=board.wait_for('elf_sha256=',3,since=cursor)
            expected=hashlib.sha256((image/'dg-esp32.elf').read_bytes()).hexdigest()
            assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==expected
            result['restored_identity_line']=identity
        save(out/'closing-result.json',result)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--image',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True)
    a=p.parse_args();closing(a.out.resolve(),a.image.resolve(),a.baseline.resolve())
