#!/usr/bin/env python3
"""Step 8e2a2 final-image pause exercise, four 300 s arms, and 5-turn e2e."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from esp_serial import EspSerial,ROOT
from step7_support import BotServer
from step8e_matrix import arm,write
from step8e2a_analysis import analyze
from phase2_endurance import snapshot
from arrival_trace import drain

CELLS=(('control',['loss','0%']),('loss-3pct-downlink',['loss','3%']),
       ('bursty-2pct-downlink',['loss','gemodel','0.2%','10%','100%','0%']),
       ('reorder-5pct-downlink',['delay','20ms','reorder','5%','50%']))


def pauses(out,elf):
    out.mkdir(parents=True,exist_ok=False)
    env=dict(BOT_ICE_INTERFACE='wlp8s0',BOT_ICE_REMOTE_NETWORK_ONLY='0',OFFLINE_SKIP_GREETING='1',
        OFFLINE_BENCH_CONTINUOUS_AUDIO='1',OFFLINE_IDLE_TIMEOUT_SECS='2100',
        RTP_EVIDENCE_DIR=str(out/'rtp'),RTP_EVIDENCE_MAX_PACKETS='200000',RTP_EVIDENCE_PAYLOAD_BYTES='1')
    previous={k:os.environ.get(k) for k in env};os.environ.update(env)
    result=dict(completed=False,passed=False,pauses=[])
    try:
        with BotServer(out/'bot'),EspSerial('/dev/ttyACM0',log_path=out/'serial.log') as board:
            cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
            identity=board.wait_for('elf_sha256=',3,since=cursor)
            assert re.search(r'elf_sha256=([a-f0-9]{64})',identity)[1]==hashlib.sha256(elf.read_bytes()).hexdigest()
            board.command('playback-selftest',30)
            line=board.wait_for('PLAYBACK_SELFTEST ',5,since=cursor)
            result['vendor_decoder_selftest']=json.loads(line.split('PLAYBACK_SELFTEST ',1)[1])
            for cmd in ('server-url http://192.168.7.222:7861','peer-option arrival-trace 1','quiet 1','mic-tone 1','gate open'):
                board.command(cmd)
            board.command('connect',90);board.command('audio 1');board.command('play-long 1',20);time.sleep(3)
            result['before']=snapshot(board,active=True,allow_incomplete=True);drain(board,out/'arrivals.jsonl')
            for ms in (200,700,1000):
                before=snapshot(board,active=True,allow_incomplete=True);at=board.cursor
                board.command(f'playback-pause {ms}');board.wait_for(f'PLAYBACK_PAUSE end ms={ms}',10,since=at)
                time.sleep(1.5);after=snapshot(board,active=True,allow_incomplete=True);drain(board,out/'arrivals.jsonl')
                a=before['perf']['playout'];b=after['perf']['playout']
                delta={k:b[k]-a[k] for k in ('media_advance','decoded_packets','missing_slots','drift_drop','latency_trim','device_silence_blocks','starvation','prefills') if k in a}
                delta['prefills']=after['perf']['prefills']-before['perf']['prefills']
                error=delta['media_advance']-sum(delta[k] for k in ('decoded_packets','missing_slots','drift_drop','latency_trim','device_silence_blocks'))
                passed=delta['device_silence_blocks']>0 and delta['starvation']==0 and delta['prefills']==0 and abs(error)<=1 and b['rejected']['window']==a['rejected']['window']
                ca,cb=a['clock'],b['clock']
                device=dict(same_epoch=ca['device_generation']==cb['device_generation'],
                    rendered_samples=320*(b['render_decisions']-a['render_decisions']),
                    silence_samples=cb['device_silence_samples']-ca['device_silence_samples'],
                    submitted_samples=cb['submitted_samples']-ca['submitted_samples'],
                    completed_samples=cb['completed_samples']-ca['completed_samples'])
                device['error']=device['submitted_samples']-device['rendered_samples']-device['silence_samples']
                passed=passed and device['same_epoch'] and abs(device['error'])<=320
                result['pauses'].append(dict(ms=ms,before=before,after=after,delta=delta,
                    source_conservation_error=error,device_conservation=device,passed=passed))
                write(out/'result.json',result)
            result['after']=snapshot(board,active=True,allow_incomplete=True);drain(board,out/'arrivals.jsonl')
            board.command('audio 0');board.command('disconnect',30)
        result['completed']=True;result['passed']=all(r['passed'] for r in result['pauses']) and result['vendor_decoder_selftest']['passed']
    except Exception as error:
        result['error']=f'{type(error).__name__}: {error}'
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        write(out/'result.json',result)
    if not result['passed']:raise RuntimeError('Pause/selftest failed; retained evidence: '+str(out))
    return result


def final_e2e(out,elf):
    # Match 8e2a/8c3 final acceptance: live STT/TTS, qualified Ethernet ICE
    # path, one cold turn plus four warm turns, and local STUN only.
    env=dict(BOT_ICE_INTERFACE='enp7s0',BOT_ICE_REMOTE_NETWORK_ONLY='1',
        BOT_BENCH_SERVER_URL='http://192.168.42.75:7861',STEP8E_TRACE='0')
    previous={k:os.environ.get(k) for k in env};os.environ.update(env)
    command=[str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/e2e_voice_test.py'),
        '--out',str(out),'--runs','5','--warm','--elf',str(elf),
        '--server-url','http://192.168.42.75:7861','--ice-servers','stun:192.168.42.75:3478']
    try:
        with out.with_suffix('.stun.log').open('w') as stun_log:
            stun=subprocess.Popen([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/netprobe.py'),
                '--stun-bind','192.168.42.75','--duration','900','--out',str(out.with_suffix('.stun.jsonl'))],
                stdout=stun_log,stderr=subprocess.STDOUT)
            try:
                with out.with_suffix('.log').open('w') as log:
                    return subprocess.run(command,stdout=log,stderr=subprocess.STDOUT).returncode
            finally:
                stun.terminate();stun.wait(timeout=15)
    finally:
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v


def run(out,elf):
    result=dict(completed=False,arms=[]);out.mkdir(parents=True,exist_ok=True)
    result['pauses']=pauses(out/'pauses',elf)
    print('PAUSES PASS',flush=True)
    for name,args in CELLS:
        print('START '+name,flush=True);target=out/'matrix-final'/name
        raw=arm(target,elf,name,args,'downlink',None,'192.168.4.29',seconds=300)
        audit=analyze(target);write(target/'single-buffer-analysis.json',audit)
        result['arms'].append(dict(name=name,completed=raw.get('completed'),passed=raw.get('passed'),error=raw.get('error')))
        write(out/'campaign.json',result)
        if not raw.get('completed'):raise RuntimeError('Incomplete '+name)
        print('COMPLETE '+name,flush=True)
    result['e2e_exit_code']=final_e2e(out/'e2e-final-ethernet',elf)
    result['completed']=True;write(out/'campaign.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,required=True)
    a=p.parse_args();run(a.out.resolve(),a.elf.resolve())
