#!/usr/bin/env python3
"""Replay received board speech to Deepgram, then send zero/noise gated tails.

No VAD Finalize is sent until after the observation window. This directly tests
provider endpointing on digital zeros independently of Pipecat turn strategy.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
import wave

import numpy as np
from scipy import signal
from dotenv import dotenv_values
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


def pcm(path):
    with wave.open(str(path)) as stream:
        assert (stream.getnchannels(),stream.getsampwidth(),stream.getframerate()) == (1,2,16000)
        return np.frombuffer(stream.readframes(stream.getnframes()),'<i2').astype(float)


def speech_clip(run):
    result = json.loads((run/'result.json').read_text())
    input_path = next((run/'bot/input').glob('*.wav'))
    source = pcm(ROOT/'hil/fixtures/agent_conversation/france.wav')
    x = pcm(input_path)
    sos = signal.butter(3,[500,1500],btype='bandpass',fs=16000,output='sos')
    ref = signal.sosfilt(sos, source); y = signal.sosfilt(sos,x)
    corr = signal.correlate(y,ref,mode='valid',method='fft')
    power = signal.convolve(y*y,np.ones(len(ref)),mode='valid',method='fft')
    norm = corr/np.sqrt(np.maximum(power,1)*np.dot(ref,ref))
    peaks,_ = signal.find_peaks(norm,height=.4,distance=16000)
    if not len(peaks): raise RuntimeError('Received board speech alignment unavailable')
    at = int(peaks[0])
    return np.rint(x[at:at+len(source)]).astype('<i2').tobytes(),dict(
        input_path=str(input_path.resolve()),source_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        offset_samples=at,correlation=float(norm[at]),elf_sha256=result['elf_sha256'],
        mic_gain_percent=result['mic_gain_percent'],voice_policy=result['voice_policy'])


async def probe(out, speech, endpointing, tail, key, split_finalize=False):
    params = dict(model='nova-3',encoding='linear16',sample_rate=16000,
                  interim_results='true',smart_format='true',endpointing=endpointing)
    events = []; begun = time.monotonic()
    async with connect('wss://api.deepgram.com/v1/listen?'+urlencode(params),
                       additional_headers={'Authorization':'Token '+key}) as ws:
        async def receive():
            async for message in ws:
                value=json.loads(message)
                if value.get('type') == 'Results':
                    events.append(dict(elapsed_s=time.monotonic()-begun,**value))
        task=asyncio.create_task(receive())
        rng=np.random.default_rng(8)
        def silence(seconds):
            if tail == 'zeros':return bytes(round(seconds*16000)*2)
            return np.rint(rng.normal(0,32768*10**(-55/20),round(seconds*16000))).astype('<i2').tobytes()
        audio=silence(.3)+speech+silence(3)
        speech_end=.3+len(speech)/32000
        mid_finalize_at = None
        for at in range(0,len(audio),640):
            if split_finalize and mid_finalize_at is None and at/32000 >= 1.5:
                await ws.send(json.dumps({'type':'Finalize'}))
                mid_finalize_at = time.monotonic()-begun
            await ws.send(audio[at:at+640])
            await asyncio.sleep(max(0,begun+(at+640)/32000-time.monotonic()))
        before_finalize=time.monotonic()-begun
        await ws.send(json.dumps({'type':'Finalize'}));await asyncio.sleep(.5)
        await ws.send(json.dumps({'type':'CloseStream'}))
        await asyncio.wait_for(task,5)
    finals=[e for e in events if e.get('speech_final') and e['elapsed_s']<before_finalize]
    value=dict(endpointing=endpointing,tail=tail,speech_end_s=speech_end,
        mid_finalize_at_s=mid_finalize_at,tail_endpointed=any('france' in e.get('channel',{}).get('alternatives',[{}])[0].get('transcript','').lower()
            and not e.get('from_finalize') for e in finals),
        finalized_at_s=before_finalize,endpointed_before_finalize=bool(finals),
        endpoint_latencies_ms=[1000*(e['elapsed_s']-speech_end) for e in finals],events=events)
    (out/f"{endpointing}-{tail}-{'split' if split_finalize else 'normal'}.json").write_text(json.dumps(value,indent=2)+'\n')
    print(endpointing,tail,'endpointed',bool(finals),flush=True)
    return value


async def run(a):
    a.out.mkdir(parents=True,exist_ok=False)
    speech,identity=speech_clip(a.from_run.resolve())
    with wave.open(str(a.out/'received-speech.wav'),'wb') as stream:
        stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(speech)
    key=dotenv_values(ROOT.parent/'pipecat/.env')['DEEPGRAM_API_KEY']
    result=dict(identity=identity,probes=[])
    for split in (False,True):
        for endpointing in (10,500):
            for tail in ('zeros','noise'):
                result['probes'].append(await probe(a.out,speech,endpointing,tail,key,split))
                (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--from-run',type=Path,required=True)
    asyncio.run(run(p.parse_args()))
