"""Evaluate actual Silero/volume API on native calibrated PCM, never read package source."""
import argparse
import asyncio
import json
import wave
from pathlib import Path
import numpy as np
from scipy.signal import lfilter
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

class ObservedSilero(SileroVADAnalyzer):
    def voice_confidence(self, audio):
        confidence=super().voice_confidence(audio)
        self.last_confidence=np.asarray(confidence).item()
        return confidence

async def probe(path, gain, minimum):
    with wave.open(str(path)) as w: raw=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float)
    x=lfilter([.980742,-.980742],[1,-.980742],raw)*gain
    x=np.clip(x,-32768,32767).astype('<i2')
    vad=ObservedSilero(sample_rate=16000,params=VADParams(stop_secs=.2,min_volume=minimum))
    vad.set_sample_rate(16000)
    rows=[]
    for at in range(0,len(x)-511,512):
        audio=x[at:at+512].tobytes()
        state=await vad.analyze_audio(audio)
        # These are the values used by the actual installed analyzer.
        rows.append(dict(t=at/16000,state=str(state),confidence=float(vad.last_confidence),volume=float(vad._prev_volume)))
    await vad.cleanup()
    return dict(gain=gain,min_volume=minimum,rms_dbfs=float(20*np.log10(max(np.sqrt(np.mean(x.astype(float)**2))/32768,1e-6))),
                speaking_windows=sum('SPEAKING' in r['state'] for r in rows),windows=rows)

async def main():
    p=argparse.ArgumentParser();p.add_argument('path',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    rows=[]
    for gain in (2,4,8,16):
        for minimum in (.6,.4,.3):
            r=await probe(a.path,gain,minimum);rows.append(r)
            print(gain,minimum,r['speaking_windows'],round(max(x['volume'] for x in r['windows']),3),round(max(x['confidence'] for x in r['windows']),3))
    a.out.write_text(json.dumps(rows,indent=2)+'\n')
if __name__=='__main__':asyncio.run(main())
