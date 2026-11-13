#!/usr/bin/env python3
"""One paired native/Jabra capture: three tone levels plus the loudest TTS.

A differential distortion/compression probe, not a component-specific proof.
No THD is inferred from speech, and no nonlinear response is fitted away.
"""
import argparse,hashlib,json,math
from pathlib import Path
import numpy as np
from esp_serial import ROOT
from step7_support import pcm,write_pcm
from echoear_hot_stt import align_prompt
from echoear_echo_stress import run as stress_run
from echoear_acoustic_qualification import verify_plan
from speaker_sweep_test import distortion


def prepare(out,bank):
    manifest=json.loads((bank/'manifest.json').read_text())
    name=max(manifest,key=lambda k:manifest[k]['active_rms_dbfs'])
    speech_path=bank/name
    assert hashlib.sha256(speech_path.read_bytes()).hexdigest()==manifest[name]['sha256']
    speech=pcm(speech_path);rate=16000;parts=[np.zeros(3200)];windows=[]
    for db in (-12,-6,0):
        at=sum(map(len,parts))/rate
        t=np.arange(rate)/rate;envelope=np.ones(rate)
        ramp=np.sin(np.linspace(0,np.pi/2,160))**2
        envelope[:160]=ramp;envelope[-160:]=ramp[::-1]
        parts.extend([32700*10**(db/20)*np.sin(2*np.pi*1000*t)*envelope,np.zeros(1600)])
        windows.append(dict(relative_source_db=db,hz=1000,start=at+.2,end=at+.8))
    speech_start=sum(map(len,parts))/rate;parts.append(speech)
    source=np.concatenate(parts)
    if len(source)/rate>28:raise ValueError('Probe exceeds native capture capacity with timing guards')
    out.mkdir(parents=True,exist_ok=False);path=out/'tts_1.wav';write_pcm(path,source)
    value={'tts_1.wav':dict(seconds=len(source)/rate,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        probe=dict(tone_windows=windows,speech_start=speech_start,speech_source=str(speech_path.resolve()),
        speech_sha256=manifest[name]['sha256'],speech_active_rms_dbfs=manifest[name]['active_rms_dbfs'],
        source_peak=32700,volume_relative_db=0,notes='One uninterrupted scored capture; 1 kHz source steps then complete loudest fixture through production playback DSP'))}
    (out/'manifest.json').write_text(json.dumps(value,indent=2)+'\n')
    return value


def compare(rows):
    if len(rows)!=3:raise ValueError('Three tone windows required')
    low=rows[0];comparisons=[]
    for row in rows[1:]:
        b,j=row['board'],row['jabra'];bl,jl=low['board'],low['jabra']
        compression=(b['fundamental_dbfs']-bl['fundamental_dbfs'])-(j['fundamental_dbfs']-jl['fundamental_dbfs'])
        excess={h:(b['harmonics_dbc'][h]-bl['harmonics_dbc'][h])-(j['harmonics_dbc'][h]-jl['harmonics_dbc'][h]) for h in ('H2','H3')}
        # These are predeclared investigation flags, not analog-component certification.
        flag=compression < -1 or any(v>3 for h,v in excess.items() if b['harmonics_dbc'][h]>-40)
        comparisons.append(dict(relative_source_db=row['relative_source_db'],board_minus_jabra_compression_db=compression,
            differential_harmonic_growth_db=excess,investigate_analog_nonlinearity=flag))
    return dict(comparisons=comparisons,investigation_required=any(r['investigate_analog_nonlinearity'] for r in comparisons),
        interpretation='Same-frequency low/high comparison cancels fixed linear response; excess native compression or harmonic growth is a suspicion, not isolation of ES7210. Speaker/Jabra nonlinearity, noise and placement remain confounders.')


def analyze(out,bank):
    result=json.loads((out/'result.json').read_text())
    for path in (bank/'manifest.json',bank/'tts_1.wav'):
        assert hashlib.sha256(path.read_bytes()).hexdigest()==result['qualification_plan']['reference_hashes'][str(path.resolve())],'Frozen probe bank changed'
    meta=json.loads((bank/'manifest.json').read_text())['tts_1.wav']['probe']
    assert hashlib.sha256(Path(meta['speech_source']).read_bytes()).hexdigest()==meta['speech_sha256'],'Frozen speech reference changed'
    source=pcm(bank/'tts_1.wav');point=out/'tts_1';rows=[];alignment={}
    for chain,file in (('board','native.wav'),('jabra','jabra.wav')):
        # Align the known tone-level program, not arbitrary speech whose
        # acoustic transfer differs between microphones and the dry source.
        _,alignment[chain]=align_prompt(source[:round(meta['speech_start']*16000)],pcm(point/file))
    for window in meta['tone_windows']:
        row=dict(relative_source_db=window['relative_source_db'],hz=window['hz'])
        for chain,file in (('board','native.wav'),('jabra','jabra.wav')):
            offset=alignment[chain]['offset_samples']/16000
            row[chain]=distortion(point/file,window['hz'],offset+window['start'],offset+window['end'])
        rows.append(row)
    native=pcm(point/'native.wav');at=alignment['board']['offset_samples']+round(meta['speech_start']*16000)
    speech_samples=len(pcm(Path(meta['speech_source'])))
    assert len(native)>=at+speech_samples,'Native speech capture truncated'
    speech_peak=float(np.max(np.abs(native[at:at+speech_samples])))
    tone_peaks=[]
    for w in meta['tone_windows']:
        offset=alignment['board']['offset_samples'];segment=native[offset+round(w['start']*16000):offset+round(w['end']*16000)]
        tone_peaks.append(float(np.max(np.abs(segment))))
    coverage=max(tone_peaks)>=speech_peak
    comparison=compare(rows)
    measurement_valid=all(m['thd_complete'] and m['clipping_count']==0 and m['fundamental_dbfs']-m['noise_dbfs']>=30 for row in rows for m in (row['board'],row['jabra']))
    probe=dict(rows=rows,alignment=alignment,comparison=comparison,native_speech_peak=speech_peak,
        native_tone_peaks=tone_peaks,tone_reaches_speech_peak=coverage,measurement_valid=measurement_valid,
        passed=bool(result['passed'] and measurement_valid and coverage and not comparison['investigation_required']),
        scope='No evidence of extra analog saturation in this one capture only; not a sweep or component isolation. THD rows are tones only; speech THD is undefined.')
    result['analog_probe']=probe;result['passed']=probe['passed']
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--prepare',action='store_true')
    p.add_argument('--out',type=Path,required=True);p.add_argument('--bank',type=Path,required=True)
    p.add_argument('--plan',type=Path);p.add_argument('--analyze',action='store_true')
    a=p.parse_args()
    if a.prepare:print(json.dumps(prepare(a.out,a.bank.resolve()),indent=2))
    else:
        if a.analyze:r=analyze(a.out,a.bank.resolve())
        else:
            if not a.plan:p.error('--plan required')
            frozen,_=verify_plan(a.plan)
            r=stress_run(a.out,a.bank.resolve(),Path(frozen['budget']),plan=a.plan,calibrate_prompt=False)
            if r['points']:r=analyze(a.out,a.bank.resolve())
        print(json.dumps(dict(passed=r['passed'],analog_probe=r.get('analog_probe'),error=r.get('error'))))
        raise SystemExit(0 if r['passed'] else 1)
