"""Independent raw-slot/near-end alignment; never optimize an ERLE score."""
import numpy as np
from scipy import signal
import wave
from math import gcd


def load_nearend_source(path):
    """Exact frozen mono PCM16 prompt, resampled once for the common ADC clock."""
    with wave.open(str(path),'rb') as wav:
        if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getcomptype()!='NONE':
            raise ValueError('Known near-end prompt must be mono uncompressed PCM16')
        rate=wav.getframerate()
        samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)
    if rate not in (16000,48000) or not 4096<=len(samples)*16000/rate<=24*16000:
        raise ValueError('Known near-end prompt must be 16/48 kHz and 0.256..24 seconds')
    factor=gcd(rate,16000)
    return signal.resample_poly(samples,16000//factor,rate//factor)


def probe_pcm():
    state = 0x13579bdf
    output = np.zeros(38400, dtype=np.int16)
    for index in range(3200, 35200):
        state ^= (state << 13) & 0xffffffff
        state ^= state >> 17
        state ^= (state << 5) & 0xffffffff
        value = state >> 16
        if value >= 32768:value -= 65536
        value = int(value / 10)
        output[index] = int(value * min(160, index-3200, 35200-index) / 160)
    return output


def alignment(first, second, *, minimum=-2048, maximum=2048):
    """Positive lag means second follows first, on their common 16 kHz clock.

    Identical causal bandpass filters cancel their own relative delay. Report
    the dominant correlation peak, not an acoustic first-arrival claim. A sine
    must fail the peak-isolation requirement even when its correlation is high.
    """
    a, b = [np.asarray(x, dtype=float).reshape(-1) for x in (first, second)]
    if (len(a) != len(b) or len(a) < 4096 or not np.isfinite(a).all() or
            not np.isfinite(b).all() or minimum > maximum or minimum <= -len(a) or maximum >= len(a)):
        return dict(available=False, reason='Complete finite equal-length captures and bounded lags required')
    sos = signal.butter(4, [500, 2500], fs=16000, btype='bandpass', output='sos')
    a, b = signal.sosfilt(sos, a), signal.sosfilt(sos, b)
    corr = signal.correlate(b, a, mode='full', method='fft')
    lags = np.arange(minimum, maximum+1)
    pa, pb = np.r_[0, np.cumsum(a*a)], np.r_[0, np.cumsum(b*b)]
    values = []
    for lag in lags:
        left_a, right_a = max(0, -lag), min(len(a), len(a)-lag)
        left_b, right_b = left_a+lag, right_a+lag
        norm = np.sqrt(max(1, (pa[right_a]-pa[left_a])*(pb[right_b]-pb[left_b])))
        values.append(corr[len(a)-1+lag]/norm)
    values = np.asarray(values);best = int(np.argmax(np.abs(values)));lag = int(lags[best])
    peak = float(values[best])
    other = np.abs(values[np.abs(lags-lag)>4])
    secondary = float(other.max()) if len(other) else 0
    available = abs(peak) >= .6 and abs(peak)-secondary >= .05 and minimum < lag < maximum
    return dict(available=bool(available), lag_samples=lag if available else None,
        diagnostic_peak_lag_samples=lag, signed_correlation=peak, secondary_peak=secondary,
        minimum_abs_correlation=.6, minimum_peak_separation=.05, peak_exclusion_samples=4,
        diagnostic_lag_ms=lag/16, polarity=1 if peak>=0 else -1,
        lag_window_samples=[minimum, maximum], band_hz=[500, 2500],
        basis='Independent common-clock dominant correlation peak; not physical first arrival or ERLE-maximizing alignment')


def source_match(source, capture):
    """Locate a complete known stimulus, retaining quiet capture margins."""
    source = np.asarray(source, dtype=float)
    if (source.ndim != 1 or len(source) < 4096 or len(source) >= len(capture) or
            not np.isfinite(source).all()):
        return dict(available=False, reason='Complete finite known stimulus required')
    return alignment(np.pad(source, (0, len(capture)-len(source))), capture,
                     minimum=0, maximum=len(capture)-len(source))


def analyze(dac, nearend, nearend_source=None):
    dac, nearend = np.asarray(dac), np.asarray(nearend)
    if (dac.ndim != 2 or nearend.ndim != 2 or dac.shape[1] != 5 or nearend.shape[1] != 5 or
            min(len(dac),len(nearend)) < 8192 or not np.isfinite(dac).all() or not np.isfinite(nearend).all()):
        return dict(passed=False, reason='Schema2 all-four-slot plus processed captures required')
    # Raw MIC3 -> raw MIC1 uses simultaneous slots; AFE output is NEVER used
    # to choose this reference alignment. The independent near-end run has no
    # EchoEar output and supplies the raw-mic -> AFE delay separately.
    relative = alignment(dac[:, 2], dac[:, 0])
    afe_candidate = alignment(nearend[:, 0], nearend[:, 4], minimum=0, maximum=4096)
    rms = lambda x: float(np.sqrt(np.mean(np.asarray(x, dtype=float)**2)))
    db = lambda x: float(20*np.log10(max(rms(x), 1e-12)/32768))
    near_source = source_match(nearend_source, nearend[:,0])
    excitation = False
    rise = None; near_delta = None; ref_near = None; mic_active = None
    # Source-defined active 20 ms blocks, within 30 dB of its loudest block.
    # This selection depends only on the frozen stimulus, never on AFE output.
    # The first 200 ms of the capture must precede ALL source-active blocks.
    if near_source['available']:
        source_samples = np.asarray(nearend_source, dtype=float)
        blocks = len(source_samples)//320
        powers = np.mean(source_samples[:blocks*320].reshape(blocks,320)**2,axis=1)
        active = np.repeat(powers > max(1e-24,float(powers.max())*.001),320)
        active = np.r_[active,np.zeros(len(source_samples)-len(active),dtype=bool)]
        onset = near_source['lag_samples']
        indices = onset+np.flatnonzero(active)
        if len(indices)>=3200 and indices[0]>=3200:
            mic_active = db(nearend[indices,0])
            rise = mic_active-db(nearend[:3200,0])
            ref_near = db(nearend[indices,2])
            near_delta = ref_near-db(nearend[:3200,2])
            excitation = rise>=10
    ref_dac = db(dac[:, 2])
    ref_silent = excitation and ref_near <= -60 and near_delta <= 6
    afe = afe_candidate if ref_silent else dict(available=False,
        reason='Verified near-end excitation with quiet electrical reference required',
        diagnostic_candidate=afe_candidate)
    # Source onset is an offset within this capture, not DAC conversion latency.
    source = source_match(probe_pcm(),dac[:,2])
    ref_difference = ref_dac-ref_near if ref_near is not None else None
    return dict(passed=bool(source['available'] and relative['available'] and afe['available'] and ref_silent and ref_difference>=10),
        physical_reference_to_mic_peak=relative, independent_nearend_afe_delay=afe,
        known_source_to_reference=source,
        known_nearend_source_to_mic=near_source, nearend_excitation_verified=bool(excitation),
        nearend_active_mic_rms_dbfs=mic_active, nearend_mic_active_vs_quiet_db=rise,
        minimum_nearend_mic_rise_db=10, nearend_active_block_ms=20,
        nearend_active_block_floor_relative_db=-30,
        reference_dac_rms_dbfs=ref_dac, reference_nearend_rms_dbfs=ref_near,
        reference_nearend_vs_quiet_db=near_delta, reference_silent_for_independent_afe=bool(ref_silent),
        reference_dac_vs_nearend_db=ref_difference,
        source_pattern_offset_in_capture_samples=source.get('lag_samples'),
        absolute_dac_conversion_delay_samples=None,
        dac_slot_rms_dbfs=[db(dac[:, i]) for i in range(4)],
        nearend_slot_rms_dbfs=[db(nearend[:, i]) for i in range(4)],
        erle=None, acoustic_qualification=False,
        limits='Raw channel labels follow Fig2e; DAC excitation plus near-end rejection corroborates electrical MIC3 identity. Exact DAC converter latency requires a synchronized output timing reference not present here. No ERLE, STT or barge-in score.')
