#!/usr/bin/env python3
"""Credential-free acoustic greeting, gate, leakage and connected heap acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
import wave
import numpy as np
from scipy import signal
from alsa import resolve_jabra, set_volumes, record_wav
from esp_serial import ROOT, EspSerial
from transport_test import wait_until, perf, dump, fixture
from step6b_checks import playout_checks


def pcm(path):
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), '<i2').reshape(-1,w.getnchannels())[:,0].astype(float)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--port', default='/dev/ttyACM0')
    p.add_argument('--rms-rise-db',type=float,help='Calibration probe threshold; recorded explicitly')
    a=p.parse_args(); calibration=json.loads((ROOT/'hil/speaker_calibration.json').read_text());threshold=a.rms_rise_db if a.rms_rise_db is not None else calibration['greeting_rms_rise_db']; out=a.out.resolve(); out.mkdir(parents=True,exist_ok=True)
    result={'passed':False}; proc=None
    try:
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1',7861)) != 0, '7861 occupied'
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(BOT_MODE='offline-test', SERVER_PORT='7861', RECORD_INPUT='1', RECORD_INPUT_DIR=str(out/'input'),
                   EVENTS_PATH=str(out/'events.jsonl'), PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
        with (out/'server.log').open('w') as log:
            proc=subprocess.Popen([str(ROOT/'server/.venv/bin/python'),'bot.py','--host','0.0.0.0','--port','7861'],
                                  cwd=ROOT/'server',env=env,stdout=log,stderr=subprocess.STDOUT)
        def up():
            try: return urllib.request.urlopen('http://127.0.0.1:7861/',timeout=.5).status<500
            except Exception: return False
        wait_until(up,30)
        card,device=resolve_jabra(); set_volumes(card,out/'mixer.log')
        with EspSerial(a.port,log_path=out/'serial.log') as esp:
            time.sleep(.2)
            cursor=esp.cursor; esp.reset(); esp.wait_for('READY transport-console',40,since=cursor)
            boot=esp.wait_for(r'elf_sha256=([0-9a-f]{64})',3,since=cursor)
            assert re.search(r'elf_sha256=([0-9a-f]{64})',boot)[1] == hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
            with record_wav(out/'greeting.wav',device,12) as rec:
                time.sleep(1.2); trigger=time.monotonic(); start=esp.cursor
                esp.command('connect',timeout=30)
                esp.command('audio 1')
                esp.wait_for('GATE closed',5,since=start)
                esp.wait_for('GATE open',8,since=start)
                rec.wait()
                stats=perf(esp)
            esp.command('audio 0'); esp.command('disconnect',timeout=15)
            result['decoded_greeting']=fixture(dump(esp,out),'greeting.wav')
            assert result['decoded_greeting']['correlation']>=.95
            lines=esp.lines_since(cursor)
            text='\n'.join(x[2] for x in lines)
            assert not re.search(r'Guru Meditation|assert failed|abort\(\)|TRANSPORT FAIL|Task watchdog got triggered',text)
            audio=re.findall(r'AUDIO drops=(\d+) read_errors=(\d+) write_errors=(\d+) clips=(\d+) transitions=(\d+) capture_stack=(\d+) playback_stack=(\d+)',text)
            assert audio, 'Missing audio counters'
            counters=list(map(int,audio[-1])); result['audio']=counters
            assert not re.search(r'PLAYBACK gain_percent=\d+ clips=[1-9]',text), 'Speaker clipping'
            gains=re.findall(r'PLAYBACK gain_percent=(\d+)',text)
            assert gains and all(int(g)==calibration['playback_gain_percent'] for g in gains), 'Playback gain differs from measured clean staging'
            result['playback_gain_percent']=int(gains[-1])
            assert counters[:4]==[0,0,0,0], 'Audio drop/error/clip'
            assert counters[5]>=16384*.25 and counters[6]>=stats["playback_stack_bytes"]*.25
            playout_checks(stats)
            result['stats']=stats
            heaps=[(phase,int(free),int(largest)) for phase,free,largest in re.findall(r'(?:HEAP phase=|PHASE )(\S+).*?internal=(\d+) largest=(\d+)',text)]
            result['heaps']=heaps
            assert all(f>=122880 and l>=32768 for phase,f,l in heaps if phase in {'ready','stats','channel-open'})
            reserve_floors=[int(lb) for phase,lb in re.findall(r'(?:HEAP phase=|PHASE )(\S+).*?excluding_reserve_lb=(\d+)',text)
                            if phase in {'ready','stats','channel-open'}]
            result['excluding_reserve_lb']=reserve_floors
            assert reserve_floors and min(reserve_floors)>=122880, 'Heap floor excluding configured reserve'
            gates=[(t,match[1],int(match[2])) for _,t,line in lines if (match:=re.search(r'GATE (open|closed) t=(\d+)',line))]
            result['gates']=gates
            mic=[(t,int(m[1]),float(m[2]),float(m[3])) for _,t,line in lines if (m:=re.search(r'MIC_GATE .*closed=(\d+) pre=(\S+) post=(\S+)',line))]
            assert any(closed for _,closed,_,_ in mic)
            assert all(post<=-119 for _,closed,_,post in mic if closed), 'Nonzero gated microphone'
        x=pcm(out/'greeting.wav'); ref=pcm(ROOT/'server/fixtures/greeting.wav')
        sos=signal.butter(4,calibration['greeting_correlation_band_hz'],btype='bandpass',fs=16000,output='sos')
        y=signal.sosfilt(sos,x); r=signal.sosfilt(sos,ref)
        # Search only after the command; baseline is captured before it.
        first=int((trigger-rec.ready_at)*16000)
        search=y[first:]; corr=signal.correlate(search,r,mode='valid',method='fft')
        energy=signal.convolve(search*search,np.ones(len(r)),mode='valid',method='fft')
        normalized=corr/np.sqrt(np.maximum(energy,1)*np.dot(r,r))
        at=int(np.argmax(normalized)); score=float(normalized[at]); offset=(first+at)/16000
        base=x[int(.2*16000):int((trigger-rec.ready_at-.1)*16000)]
        matched=x[first+at:first+at+len(ref)]
        rise=float(20*np.log10(max(np.sqrt(np.mean(matched**2)),1)/max(np.sqrt(np.mean(base**2)),1)))
        # Audible bounds use the reference envelope, mapped into the causal match.
        envelope=np.sqrt(np.convolve(ref**2,np.ones(320)/320,mode='valid'))
        active=np.flatnonzero(envelope>max(80,envelope.max()*.08))
        onset=rec.ready_at+offset+active[0]/16000
        end=rec.ready_at+offset+(active[-1]+320)/16000
        closed_at=next(t for t,state,_ in gates if state=='closed')
        open_at=next(t for t,state,_ in gates if state=='open' and t>closed_at)
        result.update(correlation_band_hz=calibration['greeting_correlation_band_hz'],correlation=score,rms_rise_db=rise,offset_s=offset,audible_duration_s=end-onset,
                      gate_close_relative_onset_s=closed_at-onset,gate_open_relative_end_s=open_at-end)
        result['calibration']=calibration
        result['rms_rise_threshold_db']=threshold
        result['checks']={'correlation':score>=.6, 'rms_rise':rise>=threshold,
                          'gate_timing':bool(closed_at<=onset+.1 and open_at>=end-.1 and open_at-end<=2.3),
                          'gated_microphone_silence':True}
        if rise<threshold:
            result['known_issue']='Clean-level acoustic RMS criterion failed; do not increase gain.'
        assert result['checks']['gate_timing'], 'Gate acoustic timing'
        assert score>=.6 and rise>=threshold, 'Acoustic greeting correlation/RMS rise'
        result['passed']=True
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if proc:
            proc.terminate()
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2)); return 0 if result['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
