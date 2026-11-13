"""Estimate DAC/ADC clock ratio from the per-run speaker reference, not speech."""
from pathlib import Path
import numpy as np
from scipy import optimize,signal
from step7_support import pcm

def tone_frequency(path,window,hz=1500):
    x=pcm(path)[int(window[0]*16000):int(window[1]*16000)]/32768
    if len(x)<8000:raise ValueError('Clock reference too short')
    x=(x-x.mean())*np.hanning(len(x));t=np.arange(len(x))/16000
    def cost(f):
        # Weighted coherent tone power; room response can change amplitude and
        # constant phase but does not change a steady sinusoid's frequency.
        z=np.dot(x,np.exp(-2j*np.pi*f*t))
        return -float(abs(z)**2)
    r=optimize.minimize_scalar(cost,bounds=(hz-3,hz+3),method='bounded',options={'xatol':1e-8})
    if not r.success or abs(r.x-hz)>2:raise ValueError('Unqualified audio clock reference')
    return float(r.x)

def clock_ratios(calibration):
    ref=calibration['output_reference'];out={}
    for chain in ('board','jabra'):
        path=calibration.get('directory')
        if not path:raise ValueError('Calibration directory required')
        f=tone_frequency(Path(path)/f'output-reference-{chain}.wav',ref['windows'][chain])
        out[chain]=dict(frequency_hz=f,ratio=f/1500,ppm=(f/1500-1)*1e6)
    return out

def warp_reference(x,ratio):
    # N source samples occupy N/ratio recorded samples at these clock rates.
    return signal.resample(x,int(round(len(x)/ratio)))
