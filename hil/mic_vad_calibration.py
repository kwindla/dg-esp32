#!/usr/bin/env python3
"""Re-run the unchanged server VAD on retained physical-mic PCM at fixed gains."""
import argparse
import asyncio
import json
import wave
from pathlib import Path
import numpy as np
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams,VADState
class MeasuredVAD(SileroVADAnalyzer):
    def voice_confidence(self,buffer):
        value=super().voice_confidence(buffer);self.confidences.append(value);return value
    def _get_smoothed_volume(self,buffer):
        value=super()._get_smoothed_volume(buffer);self.volumes.append(value);return value
async def run(a):
    with wave.open(str(a.input)) as w:
        x=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float)
    rows=[]
    for factor in (1,2,3,4,6,8):
        y=np.clip(x*factor,-32768,32767).astype('<i2')
        vad=MeasuredVAD(params=VADParams(stop_secs=.2));vad.confidences=[];vad.volumes=[];vad.set_sample_rate(16000)
        speaking=[]
        for at in range(0,len(y),320):
            state=await vad.analyze_audio(y[at:at+320].tobytes())
            if state==VADState.SPEAKING:speaking.append(at/16000)
        await vad.cleanup()
        rows.append(dict(mic_gain_percent=200*factor,clipped_samples=int(np.count_nonzero(abs(x*factor)>=32767)),speaking_seconds=speaking,max_confidence=float(np.max(vad.confidences)),max_volume=float(np.max(vad.volumes))))
    a.output.write_text(json.dumps(dict(input=str(a.input.resolve()),params=VADParams(stop_secs=.2).model_dump(),rows=rows),indent=2)+'\n')
    print([(r['mic_gain_percent'],len(r['speaking_seconds']),r['clipped_samples']) for r in rows])
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('output',type=Path);asyncio.run(run(p.parse_args()))
