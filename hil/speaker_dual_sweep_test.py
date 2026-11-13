#!/usr/bin/env python3
"""Simultaneous raw board/Jabra speaker sweep, with per-chain response correction.

Tone uses the production speaker write/gate path with PlaybackDsp BYPASSED:
unity gain, no high-pass, no limiter. Native mic PCM precedes gain/filter/gate.
All points, raw WAVs and one repeat of failed points are retained separately.
"""
import argparse
import base64
import hashlib
import json
import re
import time
import wave
from pathlib import Path
import numpy as np
from alsa import resolve_jabra, set_volumes, record_wav
from esp_serial import ROOT, EspSerial
from speaker_sweep_test import distortion

FREQUENCIES = (500, 800, 1000, 1500, 3000)
AMPLITUDES = (2, 3, 4, 6, 8, 10, 15, 20, 30)
CALIBRATION = sorted({f*h for f in FREQUENCIES for h in range(1,6) if f*h<8000})

class MicDumpUnavailable(ValueError):
    """The acknowledged dump lacks complete PCM; unrelated device errors propagate."""

def dump_mic(board, path):
    cursor=board.cursor; seq=board.send('dump-mic')
    try:ack=board.wait_for(rf'^(?:OK|ERR) #{seq}(?: |$)',30,since=cursor)
    except TimeoutError as exc:
        if getattr(board,'display_monitor',None):board.display_monitor.check()
        raise MicDumpUnavailable('Raw mic dump response unavailable') from exc
    if re.match(rf'^ERR #{seq}(?: |$)',ack):raise RuntimeError('Mic dump command rejected: '+ack)
    lines=[line for _,_,line in board.lines_since(cursor)]
    header=re.search(r'MIC_DUMP BEGIN samples=(\d+)', '\n'.join(lines))
    if not header:raise MicDumpUnavailable('Missing raw mic dump header')
    count=int(header[1])
    chunks=[]; position=0
    for line in lines:
        m=re.fullmatch(r'MIC_PCM (\d+) (\S+)',line)
        if not m: continue
        if int(m[1])!=position:raise MicDumpUnavailable('Missing raw mic chunk')
        data=base64.b64decode(m[2],validate=True);chunks.append(data);position+=len(data)//2
    if position!=count or count<=0:raise MicDumpUnavailable('Incomplete raw mic dump')
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(b''.join(chunks))

def capture(board, device, out, name, hz, amp):
    with record_wav(out/f'{name}-jabra.wav',device,3,16000) as recording:
        time.sleep(.5);board.command('mic-record 1'); mic_start=time.monotonic()
        time.sleep(.3);cursor=board.cursor;seq=board.send(f'tone {hz} 1200 {amp}')
        _,onset,_=board.wait_for_entry(rf'^OK #{seq}$',5,since=cursor)
        board.wait_for(rf'^DONE #{seq}$',5,since=cursor)
        time.sleep(.3);board.command('mic-record 0');recording.wait()
    dump_mic(board,out/f'{name}-board.wav')
    windows={'board':[onset-mic_start+.25,onset-mic_start+1.05],
             'jabra':[onset-recording.ready_at+.25,onset-recording.ready_at+1.05]}
    chains={c:distortion(out/f'{name}-{c}.wav',hz,*window) for c,window in windows.items()}
    for c in chains:
        with wave.open(str(out/f'{name}-{c}.wav')) as w:
            x=np.frombuffer(w.readframes(w.getnframes()),'<i2')
        chains[c]['peak_dbfs']=float(20*np.log10(max(float(np.max(np.abs(x.astype(float))))/32768,1e-12)))
    return dict(name=name,hz=hz,amplitude_percent=amp,windows=windows,chains=chains)

def correct_measurement(measurement, hz, response):
    harmonics={}
    for h,dbc in measurement['harmonics_dbc'].items():
        f=hz*int(h[1:])
        harmonics[h]=None if dbc is None else dbc-(response[str(f)]-response[str(hz)])
    measurement['corrected_harmonics_dbc']=harmonics
    measurement['corrected_thd_percent']=float(100*np.sqrt(sum(10**(v/10) for v in harmonics.values() if v is not None)))
    measurement['passed']=bool(measurement['clipping_count']==0 and
        measurement['corrected_thd_percent']<=5 and harmonics['H2']<=-26 and
        (harmonics['H3'] is None or harmonics['H3']<=-26))
    return measurement

def correct(row, response):
    for chain,measurement in row['chains'].items():
        correct_measurement(measurement,row['hz'],response[chain])
    values=[m['corrected_harmonics_dbc']['H3'] for m in row['chains'].values()]
    row['h3_disagreement_db']=abs(values[0]-values[1]) if all(v is not None for v in values) else None
    row['passed']=all(m['passed'] for m in row['chains'].values())
    return row

def run(a):
    a.out.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'tone_dsp':'bypass: unity gain, high-pass off, limiter off','calibration':[], 'points':[], 'reruns':[]}
    def save(): (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    try:
        card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
        with EspSerial(a.port,log_path=a.out/'serial.log') as board:
            time.sleep(.2);cursor=board.cursor;board.reset();board.wait_for('READY transport-console',40,since=cursor)
            boot=board.wait_for(r'elf_sha256=([0-9a-f]{64})',3,since=cursor)
            result['elf_sha256']=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()
            assert re.search(r'elf_sha256=([0-9a-f]{64})',boot)[1]==result['elf_sha256']
            board.command('quiet 1');board.command('audio 0')
            for hz in CALIBRATION:
                row=capture(board,device,a.out,f'cal-{hz}',hz,2);result['calibration'].append(row);save()
                print(f'cal {hz} Hz',flush=True)
            response={c:{str(r['hz']):r['chains'][c]['fundamental_dbfs'] for r in result['calibration']} for c in ('board','jabra')}
            result['response_dbfs']=response
            # Fixed-frequency doubling checks overlap with the amplitude sweep.
            linearity=[]
            for amp in (1,2,4,8):
                row=capture(board,device,a.out,f'linear-{amp}',1000,amp);linearity.append(row);save()
            result['linearity']=linearity
            result['linearity_doubling_db']={c:[b['chains'][c]['fundamental_dbfs']-x['chains'][c]['fundamental_dbfs'] for x,b in zip(linearity,linearity[1:])] for c in response}
            result['mic_linear_low_level']=all(abs(v-6.0206)<=1 for v in result['linearity_doubling_db']['board'][:2])
            for repeat in (1,2):
                for hz in FREQUENCIES:
                    for amp in AMPLITUDES:
                        row=correct(capture(board,device,a.out,f'r{repeat}-{hz}-{amp}',hz,amp),response)
                        row['repeat']=repeat;result['points'].append(row);save()
                        print(f'r{repeat} {hz} Hz {amp}%: '+', '.join(f'{c} THD={m["corrected_thd_percent"]:.2f}%' for c,m in row['chains'].items())+f' pass={row["passed"]}',flush=True)
            for row in result['points']:
                if not row['passed']:
                    rerun=correct(capture(board,device,a.out,row['name']+'-rerun',row['hz'],row['amplitude_percent']),response)
                    rerun['original']=row['name'];result['reruns'].append(rerun);save()
            result['passing_amplitudes']=[amp for amp in AMPLITUDES if all(r['passed'] for r in result['points'] if r['amplitude_percent']==amp) and result['mic_linear_low_level']]
            result['candidate_amplitude']=max(result['passing_amplitudes'],default=None)
            result['h3_disagreements']=[r['name'] for r in result['points'] if (r['h3_disagreement_db'] or 0)>6]
            result['passed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    finally:save()
    return 0 if result['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',default='/dev/ttyACM0');p.add_argument('--out',type=Path,required=True)
    raise SystemExit(run(p.parse_args()))
