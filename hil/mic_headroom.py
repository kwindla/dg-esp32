"""Reconstruct EchoEar's pre-mute digital peak from retained native PCM."""
import math
import numpy as np


def microphone_headroom(raw, digital_percent, protection=None):
    # Match mic.cpp's float32 operation order, including truncation before clamp.
    # Initial filter state is unknown at diagnostic-record start. After100 ms
    # its contribution is <3.1e-14; firmware's full in-turn clip gate is separate.
    raw = np.asarray(raw)
    skip = 1600
    if raw.ndim != 1 or len(raw) <= skip or not np.isfinite(raw).all() or not 1 <= digital_percent <= 1600:
        return dict(available=False, passed=False, reason='Missing PCM or invalid gain')
    if protection is not None and protection != dict(schema=1,knee=8192,ceiling=16384):
        return dict(available=False,passed=False,reason='Unknown microphone protection profile')
    previous = filtered = np.float32(0)
    gain = np.float32(digital_percent) / np.float32(100)
    peak = 0.0
    clips = adc_clips = limited_samples = unprotected_clips = 0
    unprotected_peak = 0.0
    for index, value in enumerate(raw):
        sample = np.float32(value)
        filtered = np.float32(.980742) * (filtered + sample - previous)
        previous = sample
        if index < skip:
            continue
        gained = float(filtered * gain)
        unprotected_peak=max(unprotected_peak,abs(gained))
        unprotected_clips+=int(gained)>32767 or int(gained)<-32768
        adc_clips+=sample>=32767 or sample<=-32768
        if protection:
            knee=np.float32(8192)/gain;width=np.float32(8192)/gain
            excess=abs(filtered)-knee
            if excess>0:
                limited_samples+=1
                value=np.copysign(knee+width*(excess/(width+excess)),filtered)
                gained=float(value*gain)
        peak = max(peak, abs(gained))
        clips += int(gained) > 32767 or int(gained) < -32768
    headroom = 20 * math.log10(32767 / max(peak, 1e-12))
    unprotected_headroom = 20 * math.log10(32767 / max(unprotected_peak, 1e-12))
    native_peak = float(np.max(np.abs(raw[skip:].astype(float))))
    return dict(available=True, passed=clips == 0 and adc_clips == 0 and unprotected_headroom >= 3,
                digital_percent=digital_percent, peak_before_clamp=peak,
                headroom_db=headroom, minimum_headroom_db=3, reconstructed_clips=int(clips),
                headroom_gate_basis='unprotected high-pass plus digital gain peak',
                unprotected_headroom_db=unprotected_headroom,
                native_peak=native_peak,native_headroom_db=20*math.log10(32767/max(native_peak,1e-12)),
                protection=protection,adc_clips=int(adc_clips),limited_samples=limited_samples,
                unprotected_peak=unprotected_peak,unprotected_clips=int(unprotected_clips),
                samples=len(raw)-skip, initialization_discard_samples=skip,
                basis='Native PCM replay through float32 high-pass and gain before mute; 3 dB gate uses unprotected peak, protected peak is diagnostic; native word saturation and firmware split clip gates remain independent; below-full-scale analog distortion requires a separate probe')
