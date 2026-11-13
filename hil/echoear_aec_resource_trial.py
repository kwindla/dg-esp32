#!/usr/bin/env python3
"""One approved frozen-image startup/capture/tone resource trial; no services.

No NVS, mixer, geometry or network settings are changed. The caller owns the
bench. This is not a transport/session/acoustic qualification or an ERLE test.
The passed field covers startup/resource/export guards ONLY. Offline analysis
of the sole approved run found a wrong reference selector on this frozen image
(MIC2 instead of MIC3); see docs/echoear-aec.md. Do not treat resource success
as a slot/reference pass or rerun without a separate bench authorization.
"""
import argparse,hashlib,json,os,re,signal,subprocess,time
from pathlib import Path
import numpy as np
from step9a_echoear import verify,check_esptool,esptool_python
from esp_serial import EspSerial
from echoear_aec_capture import dump_capture

IMAGE=Path('/home/khkramer/src/evidence/echoear/A/aec-38d0e16-experimental-image')
EXPECTED='82d032bd0cd7725eb6e33c6b5220be895499dd79424766f980b865422946411e'
FATAL=r'abort\(\)|Mem alloc fail|Guru Meditation|panic.ed|Stack canary|stack overflow|assert failed|CORRUPT HEAP'

def frozen():
    m=json.loads((IMAGE/'manifest.json').read_text())
    assert m['elf_sha256']==EXPECTED
    for name,row in m['files'].items():
        p=IMAGE/name
        assert p.stat().st_size==row['bytes'] and hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'],name
    return m

def nic():
    result={'host_s':time.monotonic()}
    for line in Path('/proc/net/dev').read_text().splitlines():
        if ':' not in line:continue
        name,values=line.split(':',1);name=name.strip();v=values.split()
        if name in ('enp7s0','wlp8s0'):result[name]={'rx_bytes':int(v[0]),'tx_bytes':int(v[8])}
    return result

def command(board,text,timeout=8):
    cursor=board.cursor;seq=board.send(text)
    line=board.wait_for(rf'^(?:OK|ERR) #{seq}(?:\s|$)|{FATAL}',timeout,since=cursor)
    if not line.startswith('OK'):raise RuntimeError(line)
    return cursor,seq

def snapshot(board,out,label):
    cursor,_=command(board,'stats')
    entries=board.lines_since(cursor)
    p=json.loads(next(line[5:] for _,_,line in entries if line.startswith('PERF ')))
    audio=[dict((k,int(v)) for k,v in re.findall(r'(\w+)=(\d+)',line)) for _,_,line in entries if 'AUDIO drops=' in line]
    if audio:p['audio']=audio[-1]
    (out/(label+'.json')).write_text(json.dumps(p,indent=2)+'\n')
    return p

def guard(p):
    from step9a_merge_checks import memory_gate
    gate=memory_gate([p]);assert gate['passed'],f'Memory gate failed: {gate}'
    assert p['aec']['enabled'] and all(p['aec'][k]==0 for k in ('errors','reference_clips','output_clips')),p['aec']
    for k in ('read_errors','write_errors','clips','drops'):
        assert p['audio'].get(k)==0,(k,p.get('audio'))
    for task in p['tasks']:
        if task['name']=='app-control':assert task['stack_free_bytes']>=2048,task
        if task['name'] in ('audio-capture','audio-playback','audio-tx'):
            assert task['stack_free_bytes']>=max(4096,task['stack_bytes']*.25 if task['name']=='audio-playback' else 4096),task
    return gate

def main(out):
    manifest=frozen();check_esptool()
    assert os.getenv('DG_BENCH_LOCK_FD'),'Run under hil/bench.py'
    out.mkdir(parents=True,exist_ok=False)
    result=dict(passed=False,acoustic_qualification=False,transport_qualification=False,source_elf=EXPECTED,
        commit=manifest['commit'],phase='identity',snapshots=[],tones=[],cleanup=[],nic_before=nic())
    board=None;ready=False;recording=False
    def deadline(*_):raise TimeoutError('Single short hold reached 175-second limit')
    signal.signal(signal.SIGALRM,deadline);signal.alarm(175)
    try:
        port=verify(out)
        result['phase']='flash'
        with (out/'flash.log').open('w') as log:
            subprocess.run([esptool_python(),'-m','esptool','--chip','esp32s3','--port',port,'--baud','460800',
                '--after','no-reset','write-flash','@flash_args'],cwd=IMAGE,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=95)
        result['phase']='startup'
        board=EspSerial(log_path=out/'serial.log',reset_on_open=False)
        board.reset(timeout=15)
        line=board.wait_for(r'READY transport-console|'+FATAL,40,since=0)
        if not line.startswith('READY transport-console'):raise RuntimeError('Fatal startup: '+line)
        board.wait_for(EXPECTED,3,since=0);ready=True
        result['phase']='hold'
        command(board,'demo hold',38) # RAM-only intent; never demo configure/NVS.
        command(board,'quiet 1')
        p=snapshot(board,out,'before-recording');result['snapshots'].append('before-recording');guard(p)
        result['phase']='recording-allocation'
        command(board,'aec-record 1');recording=True
        p=snapshot(board,out,'recording-start');result['snapshots'].append('recording-start');guard(p)
        result['phase']='capture-cpu'
        time.sleep(3)
        p=snapshot(board,out,'capture-steady');result['snapshots'].append('capture-steady');guard(p)
        result['phase']='loopback-tones'
        for hz in (997,1637,2293):
            cursor,seq=command(board,f'tone {hz} 600 10',8)
            board.wait_for(rf'^DONE #{seq}(?:\s|$)|{FATAL}',5,since=cursor)
            result['tones'].append(dict(hz=hz,ms=600,amplitude_percent=10,host_done_s=time.monotonic()))
            time.sleep(.17)
        time.sleep(.25)
        command(board,'aec-record 0');recording=False
        p=snapshot(board,out,'after-tones');result['snapshots'].append('after-tones');guard(p)
        result['phase']='export'
        capture=dump_capture(board,out/'capture.npz')
        result['capture_frames']=capture['pcm'].shape[0]
        result['adapter_delay_samples']=capture['adapter_delay_samples']
        result['capture_peaks']=[int(np.max(np.abs(capture['pcm'][:,i].astype(np.int32)))) for i in range(3)]
        result['phase']='complete';result['passed']=True
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
        print(result['phase'],result['error'],flush=True)
    finally:
        signal.alarm(0)
        if board:
            if ready and recording:
                try:command(board,'aec-record 0',3)
                except Exception as exc:result['cleanup'].append(str(exc))
            board.close()
        result['nic_after']=nic();result['bench_released_by_exit']=True
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args();raise SystemExit(main(args.out))
