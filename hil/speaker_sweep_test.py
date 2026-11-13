#!/usr/bin/env python3
"""Root-firmware raw tone sweep; retains every repeat and calibrated analysis."""
import argparse
import hashlib
import json
import re
import time
import wave
from pathlib import Path
import numpy as np
from alsa import resolve_jabra, set_volumes, record_wav
from esp_serial import ROOT, EspSerial

AMPLITUDES = (5, 10, 20, 30, 40, 50, 70, 100)
FREQUENCIES = (300, 1000, 1500, 3000)

def distortion(path, hz, start, end):
    with wave.open(str(path), 'rb') as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').reshape(-1, w.getnchannels())[:, 0].astype(float)/32768
    # plughw can upsample a 16 kHz USB capture into a 48 kHz WAV. Those
    # interpolation images are not measured speaker harmonics. Recover the
    # native points from the documented linear interpolator for old captures.
    requested_rate=rate
    alsa_log=Path(path).with_suffix('.alsa.log')
    native_rate=rate
    if alsa_log.exists():
        match=re.search(r'Rate conversion PCM \((\d+),',alsa_log.read_text())
        if match: native_rate=int(match[1])
    recovered_phase=None
    if native_rate!=rate:
        if rate%native_rate: raise ValueError('Unsupported resampled measurement; record native rate')
        factor=rate//native_rate
        # One phase contains exact native PCM points; the other phases are
        # integer-rounded interpolants. Select by interpolation residual.
        best=None
        for phase in range(factor):
            native=x[phase::factor]
            n=min(len(native)-1,(len(x)-phase-factor)//factor)
            error=0.0
            for offset in range(1,factor):
                observed=x[phase+offset:phase+offset+n*factor:factor]
                predicted=native[:n]*(1-offset/factor)+native[1:n+1]*(offset/factor)
                error+=float(np.mean((observed-predicted)**2))
            if best is None or error<best[0]:best=(error,phase)
        recovered_phase=best[1];x=x[recovered_phase::factor];rate=native_rate
    x = x[int(start*rate):int(end*rate)]
    if len(x) < rate*.4: raise ValueError('Too short measurement')
    clips = int(np.count_nonzero(np.abs(x) >= 32767/32768))
    x -= x.mean()
    window = np.hanning(len(x))
    power = 2*np.abs(np.fft.rfft(x*window))**2/(len(x)*np.sum(window**2))
    f = np.fft.rfftfreq(len(x), 1/rate)
    # Find the actual fundamental, allowing clock mismatch, then integrate
    # leakage skirts. Above-native-Nyquist harmonics are unavailable.
    near = np.flatnonzero(np.abs(f-hz) <= 20)
    fundamental_hz = float(f[near[np.argmax(power[near])]])
    masks = [np.abs(f-k*fundamental_hz) <= 20 for k in range(1, 6)]
    levels = [float(power[m].sum()) if k*fundamental_hz+20<rate/2 else None for k,m in enumerate(masks,1)]
    noise_mask = (f >= 100) & (f <= min(20000, rate/2)) & ~np.logical_or.reduce(masks)
    db = lambda p: float(10*np.log10(max(p, 1e-24)))
    return dict(fundamental_hz=fundamental_hz, fundamental_dbfs=db(levels[0]),
                harmonics_dbc={f'H{k}': (db(levels[k-1])-db(levels[0]) if levels[k-1] is not None else None) for k in range(2, 6)},
                native_capture_rate_hz=rate, wav_rate_hz=requested_rate, recovered_phase=recovered_phase,
                thd_complete=all(p is not None for p in levels),
                thd_percent=float(100*np.sqrt(sum(p for p in levels[1:] if p is not None)/max(levels[0], 1e-24))),
                noise_dbfs=db(float(power[noise_mask].sum())), clipping_count=clips)

def summarize(points):
    clean = {}
    for hz in sorted({p['hz'] for p in points}):
        accepted = []
        # Require every lower level and both repeats to pass; don't choose an
        # isolated high-amplitude result through noise or nonmonotonic behavior.
        for amplitude in sorted({p['amplitude_percent'] for p in points}):
            rows = [p for p in points if p['hz']==hz and p['amplitude_percent']==amplitude]
            if len(rows)!=2 or not all(p.get('thd_complete',True) and p['clipping_count']==0 and p['thd_percent']<=5 and p['harmonics_dbc']['H2']<=-26 and p['harmonics_dbc']['H3']<=-26 for p in rows): break
            accepted.append(amplitude)
        clean[str(hz)] = max(accepted, default=0) if all(p.get('thd_complete',True) for p in points if p['hz']==hz) else None
    return dict(clean_amplitude_percent=clean,
                apparent_response_dbfs={str(hz):(float(np.mean([p['fundamental_dbfs'] for p in points if p['hz']==hz and p['amplitude_percent']==5])) if any(p['hz']==hz and p['amplitude_percent']==5 for p in points) else None) for hz in FREQUENCIES})

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',default='/dev/ttyACM0');p.add_argument('--out',type=Path,required=True);p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf');p.add_argument('--amplitudes',type=int,nargs='+',default=AMPLITUDES);p.add_argument('--frequencies',type=int,nargs='+',default=FREQUENCIES);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False); points=[]
    card,device=resolve_jabra();set_volumes(card,a.out/'mixer.log')
    try:
        with EspSerial(a.port,log_path=a.out/'serial.log') as board:
            time.sleep(.2); cursor=board.cursor; boot=board.reset();board.wait_for('READY transport-console',40,since=cursor)
            boot=board.wait_for(r'elf_sha256=([0-9a-f]{64})',3,since=cursor)
            match=re.search(r'elf_sha256=([0-9a-f]{64})',boot)
            expected=hashlib.sha256(a.elf.read_bytes()).hexdigest()
            if not match or match[1]!=expected: raise RuntimeError('Running firmware differs from root ELF')
            board.command('quiet 1');board.command('audio 0')
            for repeat in range(1,3):
                for hz in a.frequencies:
                    for amp in a.amplitudes:
                        path=a.out/f'r{repeat}-{hz}-{amp}.wav'
                        with record_wav(path,device,3,16000) as recording:
                            time.sleep(.7); cursor=board.cursor;seq=board.send(f'tone {hz} 1200 {amp}')
                            _,stamp,_=board.wait_for_entry(rf'^OK #{seq}$',5,since=cursor)
                            board.wait_for(rf'^DONE #{seq}$',5,since=cursor);recording.wait()
                        start=stamp-recording.ready_at+.25; end=stamp-recording.ready_at+1.05
                        item=dict(repeat=repeat,hz=hz,amplitude_percent=amp,window=[start,end],**distortion(path,hz,start,end))
                        points.append(item);print(f'r{repeat} {hz} Hz {amp}%: THD={item["thd_percent"]:.2f}%',flush=True)
                        (a.out/'points.json').write_text(json.dumps(points,indent=2)+'\n')
    finally:
        result=dict(points=points,**summarize(points))
        (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        lines=['| Repeat | Hz | % | Fundamental dBFS | H2 dBc | H3 dBc | H4 dBc | H5 dBc | THD % | Noise dBFS |','|---|---|---|---|---|---|---|---|---|---|']
        for r in points:
            values=[r['repeat'],r['hz'],r['amplitude_percent'],r['fundamental_dbfs'],*r['harmonics_dbc'].values(),r['thd_percent'],r['noise_dbfs']]
            lines.append('| '+' | '.join(('N/A' if v is None else f'{v:.2f}' if isinstance(v,float) else str(v)) for v in values)+' |')
        (a.out/'table.md').write_text('\n'.join(lines)+'\n')
    return 0

if __name__=='__main__': raise SystemExit(main())
