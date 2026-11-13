"""Diagnostic on-board near-stimulus window levels; never an eligibility gate."""
import wave
import numpy as np
from scipy.signal import butter,sosfiltfilt,resample_poly
from echoear_aec_metrics import frame_power,dbfs


def window_levels(raw,afe,qraw,qafe,start,end):
    if start is None or end is None or start<0 or end+1024>min(len(raw),len(afe)) or end-start<320:
        return dict(scorable=False,reason='Missing or incomplete matched source/delayed extent',gate=False)
    sos=butter(4,[300,3400],btype='bandpass',fs=16000,output='sos')
    result=dict(scorable=True,gate=False,extent_samples=[int(start),int(end)],band_hz=[300,3400],frame_ms=20,
                attribution='Raw MIC1 may include echo; AFE may include residual echo. Not an isolated near-end estimate.')
    for name,signal,quiet,delay in [('raw_mic1',raw,qraw,0),('afe',afe,qafe,1024)]:
        filtered=sosfiltfilt(sos,np.asarray(signal,dtype=float));floor=dbfs(sosfiltfilt(sos,np.asarray(quiet,dtype=float)))
        powers=frame_power(filtered[start+delay:end+delay]);active=powers>32768**2*10**((floor+15)/10)
        result[name]=dict(quiet_floor_dbfs=floor,active_frames=int(active.sum()),total_frames=len(active),
                          active_rms_dbfs=float(10*np.log10(np.mean(powers[active])/32768**2)) if active.any() else None)
    return result


def stimulus_levels(cell,row,raw,afe,qraw,qafe):
    if cell.get('stimulus_skipped'):return dict(scorable=False,reason='Stimulus not played',gate=False)
    onset=row.get('near_onset',{})
    if onset.get('valid') and onset.get('raw_onset_sample') is not None:
        with wave.open(cell['fixture']) as w:
            source=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float);rate=w.getframerate()
        if rate!=16000:source=resample_poly(source,16000,rate)
        power=frame_power(source);active=np.flatnonzero(power>max(1,float(power.max())*.001))
        if len(active):
            start=int(onset['raw_onset_sample']);end=start+(int(active[-1])-int(active[0])+1)*320
            return window_levels(raw,afe,qraw,qafe,start,end)
    known=row.get('near',{}).get('known_extent')
    if known:return window_levels(raw,afe,qraw,qafe,*known)
    return dict(scorable=False,reason='No independently matched near-source extent',gate=False)
