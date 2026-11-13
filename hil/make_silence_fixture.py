#!/usr/bin/env python3
"""Generate the frozen lean packet sequence for board decoder-state comparison.

Run with `uv run --project server python hil/make_silence_fixture.py`.
"""
import fractions
import hashlib
import json
import struct
import sys
import wave
from pathlib import Path
import numpy as np
from av import AudioFrame
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from lean_opus import LeanOpusEncoder

def fixture(name):
    with wave.open(str(ROOT/'server/fixtures'/f'{name}.wav')) as w:
        assert (w.getframerate(),w.getnchannels(),w.getsampwidth())==(16000,1,2)
        return np.frombuffer(w.readframes(w.getnframes()),'<i2')

def main():
    encoder=LeanOpusEncoder();packets=[]
    source=np.r_[np.zeros(16000,dtype=np.int16),fixture('greeting'),
                 np.zeros(48000,dtype=np.int16),fixture('reply_pineapple'),np.zeros(16000,dtype=np.int16)]
    source=np.pad(source,(0,(-len(source))%320))
    for at in range(0,len(source),320):
        frame=AudioFrame.from_ndarray(source[at:at+320].reshape(1,-1),format='s16',layout='mono')
        frame.sample_rate=16000;frame.pts=at;frame.time_base=fractions.Fraction(1,16000)
        payloads,_=encoder.encode(frame);packets.extend(payloads)
    raw=b'DSIL'+struct.pack('<I',len(packets))+b''.join(struct.pack('<H',len(p))+p for p in packets)
    path=ROOT/'hil/fixtures/lean_resume.opus';path.write_bytes(raw)
    path.with_suffix('.json').write_text(json.dumps(dict(frames=len(packets),sha256=hashlib.sha256(raw).hexdigest(),
        bitrate=30000,cutoff_hz=8000,decoder_rate=16000,frame_ms=20,
        source='one second zeros, greeting, three seconds zeros, reply_pineapple, one second zeros'),indent=2)+'\n')
if __name__=='__main__':main()
