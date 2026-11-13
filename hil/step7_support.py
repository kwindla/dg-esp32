"""Shared step-7 bench helpers; owned server only, no credential logging."""
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from contextlib import contextmanager
import wave
import numpy as np
from scipy import signal
import requests
from dotenv import dotenv_values
from esp_serial import ROOT

def deepgram_key():
    return os.getenv('DEEPGRAM_API_KEY') or dotenv_values(ROOT.parent/'pipecat/.env').get('DEEPGRAM_API_KEY')

def transcribe(path):
    key=deepgram_key()
    if not key: raise RuntimeError('No Deepgram credentials')
    r=requests.post('https://api.deepgram.com/v1/listen',params={'model':'nova-3','smart_format':'true'},
        headers={'Authorization':'Token '+key,'Content-Type':'audio/wav'},data=Path(path).read_bytes(),timeout=45)
    if r.status_code!=200: raise RuntimeError(f'Deepgram prerecorded STT HTTP {r.status_code}')
    return r.json()['results']['channels'][0]['alternatives'][0]['transcript']

def word_accuracy(expected, actual):
    a=re.findall(r'\w+',expected.lower());b=re.findall(r'\w+',actual.lower()); row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nextrow=[i]
        for j,y in enumerate(b,1):nextrow.append(min(nextrow[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=nextrow
    return max(0,1-row[-1]/len(a))

def pcm(path):
    with wave.open(str(path)) as w:
        assert w.getframerate()==16000 and w.getsampwidth()==2
        return np.frombuffer(w.readframes(w.getnframes()),'<i2').reshape(-1,w.getnchannels())[:,0].astype(float)

def write_pcm(path,x):
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(np.rint(x).astype('<i2').tobytes())

def acoustic_match(path, reference, first, ready_at, baseline_end,last=None,reference_pcm=None,server_send_at=None):
    x=pcm(path);source=pcm(reference);ref=source if reference_pcm is None else np.asarray(reference_pcm)
    sos=signal.butter(4,[500,1500],btype='bandpass',fs=16000,output='sos')
    y=signal.sosfilt(sos,x);r=signal.sosfilt(sos,ref);begin=int(first*16000)
    search=y[begin:None if last is None else int(last*16000)]
    if len(search)<len(r):raise AssertionError('Reply window too short')
    corr=signal.correlate(search,r,mode='valid',method='fft')
    power=signal.convolve(search*search,np.ones(len(r)),mode='valid',method='fft')
    normalized=corr/np.sqrt(np.maximum(power,1)*np.dot(r,r));at=int(np.argmax(normalized));offset=(begin+at)/16000
    base=x[int(.2*16000):int(baseline_end*16000)];match=x[begin+at:begin+at+len(ref)]
    baseline_dbfs=float(20*np.log10(max(np.sqrt(np.mean(base**2)),1)/32768)) if len(base) else None
    baseline_sane=baseline_dbfs is not None and baseline_dbfs<=-50
    envelope=np.sqrt(np.convolve(source**2,np.ones(320)/320,mode='valid'));active=np.flatnonzero(envelope>max(80,envelope.max()*.08))
    onset=ready_at+offset+active[0]/16000
    if server_send_at is not None and onset<server_send_at:
        raise AssertionError("Acoustic onset precedes server send")
    return dict(correlation=float(normalized[at]),offset_s=offset,
        baseline_dbfs=baseline_dbfs,acoustic_timing_available=baseline_sane,
        acoustic_timing_reason=None if baseline_sane else 'Jabra baseline above -50 dBFS or unavailable',
        rms_rise_db=float(20*np.log10(max(np.sqrt(np.mean(match**2)),1)/max(np.sqrt(np.mean(base**2)),1))) if len(base) else None,
        onset=onset,end=ready_at+offset+(active[-1]+320)/16000)

def leakage(lines,onset=None,end=None):
    gates=[];mic=[]
    for _,t,line in lines:
        m=re.search(r'GATE (open|closed) t=(\d+)',line)
        if m:gates.append((t,m[1],int(m[2])))
        m=re.search(r'MIC_GATE t=(\d+) closed=(\d+) pre=(\S+) post=(\S+)',line)
        if m:mic.append(dict(host_t=t,device_us=int(m[1]),closed=int(m[2]),pre=float(m[3]),post=float(m[4])))
    closed=[r for r in mic if r['closed']]
    assert closed,'Missing closed-gate microphone samples'
    assert all(r['post']<=-119.9 for r in closed),'Nonzero post-gate PCM'
    if onset is not None:
        audible=[r for r in mic if onset+.1<=r['host_t']<=end-.1]
        assert audible,'Missing audible microphone observations'
        assert all(r['closed'] and r['post']<=-119.9 for r in audible),'Playback leakage'
    return dict(samples=len(closed),maximum_post_dbfs=max(r['post'] for r in closed),gates=gates)

class BotServer:
    def __init__(self,out,mode='offline-test',port=7861,*,detach=False,ice_servers=(),entrypoint='bot.py'):self.entrypoint=entrypoint;self.out=Path(out).resolve();self.mode=mode;self.port=port;self.proc=None;self.starts=0;self.detach=detach;self.ice_servers=ice_servers or tuple(filter(None,[os.getenv("BOT_BENCH_STUN_URL")]))
    def start(self,startup_timeout=60):
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1',self.port))==0:raise RuntimeError(f'Port {self.port} occupied')
        self.out.mkdir(parents=True,exist_ok=True)
        env={k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(BOT_MODE=self.mode,SERVER_PORT=str(self.port),EVENTS_PATH=str(self.out/'events.jsonl'),
            RECORD_INPUT='1',RECORD_INPUT_DIR=str(self.out/'input'),TTS_REFERENCE_DIR=str(self.out/'tts'),
            PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
        if os.getenv('BOT_ICE_INTERFACE'):
            env.setdefault('BOT_ICE_DEBUG_PATH',str(self.out/'ice-debug.log'))
            env.setdefault('RTP_EVIDENCE_DIR',str(self.out/'rtp'))
        # Live startup resolves the approved credential file itself.
        self.starts+=1
        self.log=(self.out/f'server-{self.starts}.log').open('w')
        command=[str(ROOT/'server/.venv/bin/python'),self.entrypoint,'--host','0.0.0.0','--port',str(self.port)]
        if self.ice_servers:command+=['--ice-servers']+list(self.ice_servers)
        self.proc=subprocess.Popen(command,cwd=ROOT/'server',env=env,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=self.detach)
        deadline=time.monotonic()+startup_timeout
        while time.monotonic()<deadline:
            if self.proc.poll() is not None:raise RuntimeError('Owned bot exited; see server log')
            try:
                if urllib.request.urlopen(f'http://127.0.0.1:{self.port}/',timeout=.5).status<500:return
            except Exception:pass
            time.sleep(.1)
        raise TimeoutError('Owned bot startup')
    def stop(self,kill=False):
        if self.proc:
            (self.proc.kill if kill else self.proc.terminate)()
            try:self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
            self.proc=None;self.log.close()
    def events(self):
        p=self.out/'events.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    def __enter__(self):
        try:self.start();return self
        except BaseException:self.stop();raise
    def __exit__(self,*args):self.stop()


def prepare_prompt(out):
    # The bench Jabra advertises 16 kHz playback but drains it at ~3x speed.
    # Its 48 kHz playback is qualified by the existing loopback tone test.
    # Resample the immutable speech fixture, keeping its duration and pitch.
    from scipy.signal import resample_poly
    import hashlib
    source=ROOT/'hil/fixtures/prompt_speech.wav'
    x=pcm(source);path=Path(out)/'prompt-playback-48k.wav'
    y=resample_poly(x,3,1)
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(np.rint(y).astype('<i2').tobytes())
    (Path(out)/'prompt-identity.json').write_text(json.dumps({'source':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'source_rate':16000,'playback_rate':48000,'duration_s':len(x)/16000},indent=2)+'\n')
    return path
