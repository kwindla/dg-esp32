"""Off-bench, fixed AEC word/coverage-witness preparation; no board access."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import wave
import numpy as np
import requests
from scipy import signal
from step7_support import deepgram_key, pcm, write_pcm


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def trim_word(samples):
    x=np.asarray(samples,dtype=float)
    powers=signal.convolve(x*x,np.ones(320)/320,mode='same')
    active=np.flatnonzero(powers>max(1.,float(powers.max())*.001))
    if not len(active):raise ValueError('Empty word')
    a,b=max(0,int(active[0])-320),min(len(x),int(active[-1])+321)
    duration=(b-a)/16000
    if not .15<=duration<=.55:raise ValueError(f'Fixed-tempo word active duration {duration:.3f}s outside .15..55')
    return np.r_[np.zeros(1280),x[a:b],np.zeros(1600)]


def marker_pcm():
    rng=np.random.default_rng(20261015)
    x=signal.sosfilt(signal.butter(4,[500,2500],fs=16000,btype='bandpass',output='sos'),rng.normal(size=4800))
    x*=np.r_[np.linspace(0,1,320),np.ones(4160),np.linspace(1,0,320)]
    return x*3000/np.max(np.abs(x))


def playback48(path,samples):
    x=np.rint(signal.resample_poly(samples,3,1))
    if np.max(np.abs(x))>=32767:raise ValueError('Stimulus overflow')
    with wave.open(str(path),'wb') as wav:
        wav.setparams((1,2,48000,0,'NONE','not compressed'));wav.writeframes(x.astype('<i2').tobytes())


def prepare(out):
    out.mkdir(parents=True,exist_ok=False)
    key=deepgram_key()
    if not key:raise RuntimeError('Approved Deepgram credential unavailable')
    response=requests.post('https://api.deepgram.com/v1/speak',
        params=dict(model='aura-2-thalia-en',encoding='linear16',sample_rate=16000,container='wav'),
        headers={'Authorization':'Token '+key,'Content-Type':'application/json'},
        json={'text':'Stop.'},timeout=45)
    if response.status_code!=200:raise RuntimeError(f'Stimulus TTS HTTP {response.status_code}')
    source=out/'stop-source.wav';source.write_bytes(response.content)
    tempo=out/'stop-tempo.wav'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),'-af','atempo=1.25',
                    '-ar','16000','-ac','1',str(tempo)],check=True,timeout=15)
    word=trim_word(pcm(tempo));write_pcm(out/'stop-16k.wav',word)
    marker=marker_pcm();write_pcm(out/'marker-16k.wav',marker)
    playback48(out/'stop-48k.wav',word);playback48(out/'marker-48k.wav',marker)
    manifest=dict(schema=1,text='Stop.',tempo=1.25,marker_seed=20261015,
                  word_seconds=len(word)/16000,marker_seconds=len(marker)/16000,
                  files={p.name:sha(p) for p in out.glob('*.wav')},calibrated=False)
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    print(json.dumps(prepare(p.parse_args().out),indent=2))
