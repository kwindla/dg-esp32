#!/usr/bin/env python3
"""Record the installed server encoder's actual steady-silence payload."""
import argparse
import fractions
import hashlib
import importlib.metadata
import json
from pathlib import Path
import av
from aiortc.codecs.opus import OpusEncoder

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=True);encoder=OpusEncoder();packets=[]
    # RawAudioTrack emits mono PCM16 in 10 ms chunks; aiortc resamples and
    # frames these as 20 ms, 48 kHz stereo libopus VOIP/96 kbps packets.
    for index in range(1004):
        frame=av.AudioFrame(format='s16',layout='mono',samples=160)
        for plane in frame.planes:plane.update(bytes(plane.buffer_size))
        frame.sample_rate=16000;frame.pts=index*160;frame.time_base=fractions.Fraction(1,16000)
        payloads,_=encoder.encode(frame);packets.extend(payloads)
        if len(packets)>=500:break
    assert len(packets)==500 and all(packet==bytes.fromhex('fcfffe') for packet in packets), 'Server silence fixture changed'
    raw=b''.join(len(packet).to_bytes(2,'little')+packet for packet in packets)
    (a.out/'server-silence.bin').write_bytes(raw)
    metadata=dict(packets=500,unique_payloads=['fcfffe'],sizes=[3],input_sample_rate=16000,input_channels=1,input_chunk_ms=10,
        output_sample_rate=48000,output_channels=2,bitrate=96000,application='voip',
        sha256=hashlib.sha256(raw).hexdigest(),versions={name:importlib.metadata.version(name) for name in ['aiortc','av','pipecat-ai']})
    (a.out/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n');print(json.dumps(metadata))
if __name__=='__main__':main()
