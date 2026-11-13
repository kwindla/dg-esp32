#!/usr/bin/env python3
"""Host-only limiter distortion: coherent float32 tones, no acoustic claims."""
import argparse,json
from pathlib import Path
import numpy as np


def characterize(hz,overdrive_db):
    rate=16000;n=rate*2;gain=np.float32(1.25);knee=np.float32(8192)/gain
    source=(float(knee)*10**(overdrive_db/20)*np.sin(2*np.pi*hz*np.arange(n)/rate)).astype(np.float32)
    excess=np.abs(source)-knee
    protected=np.where(excess>0,np.copysign(knee+knee*np.maximum(excess,0)/(knee+np.maximum(excess,0)),source),source)*gain
    spectrum=np.abs(np.fft.rfft(protected.astype(float)))**2
    fundamental=spectrum[hz*2];harmonics=[k*hz*2 for k in range(2,8000//hz+1) if k*hz<8000]
    harmonic=float(sum(spectrum[k] for k in harmonics))
    remaining=spectrum.copy();remaining[0]=0;remaining[hz*2]=0;remaining[harmonics]=0
    alias_db=float(10*np.log10(max(float(remaining.sum())/fundamental,1e-24)))
    return dict(hz=hz,overdrive_db=overdrive_db,output_peak=float(np.max(np.abs(protected))),
        thd_percent=float(100*np.sqrt(harmonic/fundamental)),nonharmonic_db=alias_db,
        basis='2 s coherent 16 kHz tones, float32 limiter only before PCM truncation; all baseband harmonics counted; nonharmonic residual includes folded aliases/roundoff, excludes aliases coinciding with harmonic bins')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    rows=[characterize(hz,db) for db in (0,3,6,9,12) for hz in (300,2500)]
    with a.out.open('x') as f:f.write(json.dumps(rows,indent=2)+'\n')
    for row in rows:print(row['hz'],row['overdrive_db'],round(row['output_peak'],2),round(row['thd_percent'],3),round(row['nonharmonic_db'],2))
