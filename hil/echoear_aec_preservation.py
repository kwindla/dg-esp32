"""STT-independent, report-only spectral/envelope preservation diagnostics.

The existing timed fixture waveform witness supplies the extent, without STT.
Barge-in witnesses match AFE; near-only controls match raw MIC1. The fixed
1024-sample source delay is retained. No spectral/envelope score optimizes lag.
Echo residual is not separated from near speech by these measurements.
"""
import math
import wave
import numpy as np
from scipy.signal import resample_poly
from echoear_aec_metrics import frame_power, dbfs

BANDS = [(300,600),(600,1200),(1200,2400),(2400,4000),(4000,7000)]


def band_power(x):
    x=np.asarray(x,dtype=float)
    spectrum=np.abs(np.fft.rfft((x-x.mean())*np.hanning(len(x))))**2
    hz=np.fft.rfftfreq(len(x),1/16000)
    return np.array([spectrum[(hz>=lo)&(hz<hi)].sum() for lo,hi in BANDS])


def compare(fixture, post):
    a,b=np.asarray(fixture,float),np.asarray(post,float)
    if len(a)!=len(b) or len(a)<640 or not np.isfinite(a).all() or not np.isfinite(b).all():
        return dict(scorable=False,gate=False,reason='Incomplete or nonfinite matched extent')
    ap,bp=band_power(a),band_power(b)
    if min(ap.sum(),bp.sum(),np.mean(a*a),np.mean(b*b))<=1e-12:
        return dict(scorable=False,gate=False,reason='Silent source/output')
    # Equal broadband RMS: envelope shape and relative spectrum are distinct
    # from overall attenuation, which remains visible in level_delta_db.
    gain=float(np.sqrt(np.mean(a*a)/np.mean(b*b)))
    matched=b*gain
    ae=np.sqrt(frame_power(a));be=np.sqrt(frame_power(matched))
    floor=max(float(ae.max())*.01,1e-12)
    ad=20*np.log10(np.maximum(ae,floor));bd=20*np.log10(np.maximum(be,floor))
    correlation=float(np.corrcoef(ae,be)[0,1]) if min(np.std(ae),np.std(be))>1e-12 else None
    relative=10*np.log10(np.maximum(bp/bp.sum(),1e-15)/np.maximum(ap/ap.sum(),1e-15))
    return dict(scorable=True,gate=False,report_only=True,bands_hz=BANDS,
        relative_band_delta_db=relative.tolist(),source_band_fractions=(ap/ap.sum()).tolist(),
        post_band_fractions=(bp/bp.sum()).tolist(),matched_rms_gain=gain,
        fixture_dbfs=dbfs(a),post_dbfs=dbfs(b),level_delta_db=dbfs(b)-dbfs(a),
        envelope=dict(frame_ms=20,log_floor_db_below_fixture_peak=40,correlation=correlation,
                      rmse_db=float(np.sqrt(np.mean((ad-bd)**2))),
                      fixture_db=ad.tolist(),matched_post_db=bd.tolist()))


def preservation(cell,row,raw,afe,*,blocks=None,anchors=None):
    if cell.get('stimulus_skipped'):
        return dict(scorable=False,gate=False,reason='Stimulus not delivered')
    with wave.open(cell['fixture']) as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2:
            return dict(scorable=False,gate=False,reason='Fixture must be mono signed 16-bit PCM')
        source=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float);rate=w.getframerate()
    if rate!=16000:source=resample_poly(source,16000,rate)
    power=frame_power(source);active=np.flatnonzero(power>max(1,float(power.max(initial=0))*.001))
    if not len(active):return dict(scorable=False,gate=False,reason='Silent fixture')
    first=int(active[0])*320;end=(int(active[-1])+1)*320
    onset=row.get('near_onset',{})
    start=onset.get('raw_onset_sample') if onset.get('valid') else None
    alignment='existing timed post-AEC/fixture waveform match; no STT'
    if start is None and cell['kind']=='near':
        extent=row.get('near',{}).get('known_extent')
        start=extent[0] if extent else None
        alignment='existing raw MIC1/fixture near-only waveform match; no STT'
    if start is None:return dict(scorable=False,gate=False,reason='No validated fixture waveform extent')
    start=int(start);stop=start+end-first
    if start<0 or stop+1024>len(afe):
        return dict(scorable=False,gate=False,reason='Incomplete delayed post-AEC extent')
    result=compare(source[first:end],afe[start+1024:stop+1024])
    result.update(alignment=alignment,fixture_extent=[first,end],raw_extent=[start,stop],afe_delay_samples=1024,
        voice_class=cell.get('voice_class','firm'),kind=cell['kind'],
        echo_condition='near-only' if cell['kind']=='near' else 'during-echo',
        attribution='Post-AEC includes residual echo; relative spectrum does not isolate vendor suppression.')
    return result


def propose_tolerances(rows):
    """Descriptive extrema + explicitly chosen rounding margin, NOT a gate."""
    firm=[r for r in rows if r.get('voice_class')=='firm' and r.get('scorable') and r.get('clean')]
    if not firm:return dict(report_only=True,gate=False,reason='No clean retained firm rows')
    bands=np.asarray([r['relative_band_delta_db'] for r in firm])
    rmses=[r['envelope']['rmse_db'] for r in firm]
    return dict(report_only=True,gate=False,requires_user_decision=True,n=len(firm),
        derivation='Observed clean firm maxima, rounded up: bands +1dB, envelope RMSE +0.5dB. In-sample, not qualification.',
        band_abs_max_db=np.max(abs(bands),axis=0).tolist(),
        proposed_band_abs_limit_db=(np.ceil(np.max(abs(bands),axis=0))+1).tolist(),
        envelope_rmse_max_db=max(rmses),proposed_envelope_rmse_max_db=math.ceil(max(rmses)*2)/2+.5)
