"""Distributed raw reference->microphone path; no AFE/ERLE optimization.

This measures an impulse response with held-out prediction, not a scalar
physical delay. Never use its energy descriptors as the independent AFE delay.
"""
import numpy as np
from scipy import signal
from scipy.linalg import solve_toeplitz
from echoear_aec_alignment import analyze

METHOD = 'raw-path-response-v2'
RATE = 16000
LOW, HIGH = -128, 1023
NFFT = 2048
RIDGE = .001


def path_response(reference, microphone):
    """Two independent 1 s noise halves; each predicts the other, unchanged.

    Solve regularized stationary normal equations for fixed FIR lags
    -128..1023 (ridge=0.001*input mean-square), and predict the opposite half.
    Welch spectra (2048 samples, 50% overlap) check excitation only; windowed
    cross-spectrum division would bias estimates at larger delays.
    Exclude a fixed 2048 samples at both ends only for convolution boundaries.
    Availability needs broadband excitation, >=90% explained variance in BOTH
    directions and repeatable 5/50/95% response-energy lags within four samples.
    Negative lags are retained, never discarded to force a causal answer.
    """
    a,b=[np.asarray(x,dtype=float) for x in (reference,microphone)]
    if (a.shape!=(32000,) or b.shape!=(32000,) or not np.isfinite(a).all() or
            not np.isfinite(b).all() or np.max(np.abs(a))>32768 or np.max(np.abs(b))>32768):
        return dict(available=False,reason='Exactly two complete finite 1 s probe halves required')
    estimates=[]
    for train,test in ((slice(0,16000),slice(16000,32000)),(slice(16000,32000),slice(0,16000))):
        x,y=a[train],b[train]
        f,power=signal.welch(x,fs=RATE,nperseg=NFFT,noverlap=NFFT//2)
        band=power[(f>=300)&(f<=6000)]
        flatness=float(np.exp(np.mean(np.log(np.maximum(band,1e-24))))/max(float(band.mean()),1e-24))
        concentration=float(band.max()/max(float(band.sum()),1e-24))
        size=HIGH-LOW+1;center=len(x)-1
        autocorr=signal.correlate(x,x,mode='full',method='fft')[center:center+size]/len(x)
        cross=signal.correlate(y,x,mode='full',method='fft')[center+LOW:center+HIGH+1]/len(x)
        autocorr[0]+=max(float(autocorr[0])*RIDGE,1e-24)
        h=solve_toeplitz(autocorr,cross,check_finite=True)
        pred=signal.fftconvolve(a[test],h)[-LOW:-LOW+16000]
        actual=b[test][NFFT:-NFFT];pred=pred[NFFT:-NFFT]
        variance=float(np.var(actual))
        explained=1-float(np.mean((actual-pred)**2))/max(variance,1e-24)
        energy=h*h;total=float(energy.sum());cdf=np.cumsum(energy)/max(total,1e-24)
        quantiles=[int(np.searchsorted(cdf,q))+LOW for q in (.05,.5,.95)] if total>0 else None
        estimates.append(dict(explained_variance=explained,reference_rms_lsb=float(np.sqrt(np.mean(x*x))),
            spectral_flatness=flatness,maximum_bin_power_fraction=concentration,
            energy_lags_samples=quantiles,impulse_response=h.tolist()))
    excitation=all(e['reference_rms_lsb']>=2 and e['spectral_flatness']>=.1 and
                   e['maximum_bin_power_fraction']<=.05 for e in estimates)
    prediction=all(e['explained_variance']>=.9 for e in estimates)
    stable=all(e['energy_lags_samples'] is not None for e in estimates)
    if stable:stable=max(abs(x-y) for x,y in zip(estimates[0]['energy_lags_samples'],estimates[1]['energy_lags_samples']))<=4
    # A response concentrated at an estimate boundary may continue outside it.
    interior=all(e['energy_lags_samples'] is not None and LOW+16<e['energy_lags_samples'][0] and
                 e['energy_lags_samples'][2]<HIGH-16 for e in estimates)
    return dict(available=bool(excitation and prediction and stable and interior),method=METHOD,
        checks=dict(broadband_excitation=excitation,held_out_prediction=prediction,
                    repeatability=bool(stable),response_inside_window=interior),
        estimates=estimates,rate=RATE,lag_window_samples=[LOW,HIGH],welch_samples=NFFT,
        ridge_fraction=RIDGE,minimum_reference_rms_lsb=2,minimum_spectral_flatness=.1,
        maximum_bin_power_fraction=.05,minimum_explained_variance=.9,
        maximum_energy_quantile_disagreement_samples=4,energy_quantiles=[.05,.5,.95],
        scalar_physical_delay_samples=None,first_arrival_samples=None,
        basis='Distributed regularized raw MIC3-to-MIC1 path; energy quantiles are NOT first arrival, AFE delay or an ERLE shift')


def analyze_path(dac,nearend,nearend_source):
    legacy=analyze(dac,nearend,nearend_source)
    source=legacy.get('known_source_to_reference',{})
    path=dict(available=False,reason='Verified complete DAC probe required')
    if source.get('available'):
        # Exact emitted source has 3200 quiet + 32000 noise + 3200 quiet samples.
        onset=source['lag_samples'];active=np.asarray(dac)[onset+3200:onset+35200]
        path=path_response(active[:,2],active[:,0])
    identity=(source.get('available') is True and legacy.get('reference_silent_for_independent_afe') is True and
              legacy.get('reference_dac_vs_nearend_db',float('-inf'))>=10)
    return dict(method=METHOD,passed=bool(identity and path['available'] and
                legacy.get('independent_nearend_afe_delay',{}).get('available')),
        scope='Distributed path/resource diagnostic only; not scalar physical alignment or AEC qualification',
        raw_reference_to_microphone_path=path,legacy_correlation_analysis=legacy,
        independent_nearend_afe_delay=legacy.get('independent_nearend_afe_delay'),
        scalar_physical_delay_samples=None,absolute_dac_conversion_delay_samples=None,
        erle=None,acoustic_qualification=False)
