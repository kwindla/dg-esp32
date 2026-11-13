"""Declared exploratory noise screening; never retry an acoustic/STT failure."""
from collections import deque
import math
import numpy as np
from scipy.signal import butter, sosfiltfilt

V3_FILTER = dict(kind="butterworth-highpass", order=4, cutoff_hz=100,
                 sample_rate_hz=16000, application="sosfiltfilt-per-window",
                 padtype="odd", padlen=15)
from echoear_aec_metrics import RATE, FRAME, dbfs, frame_power


def validate_noise_gate(policy):
    if policy is None:
        return
    version = policy['version']
    limits_valid = (version == 1 and policy.get('level_tolerance_db') == 3 and
                    policy['transient_max_dbfs'] == -52) or (
                    version in (2, 3) and policy.get('level_max_dbfs') == -55 and
                    policy.get('level_rise_max_db') == 6 and policy['transient_max_dbfs'] == -46)
    if (not limits_valid or (version == 3 and (policy.get('filter') != V3_FILTER or policy.get('rise_reference_min_dbfs') != -61)) or policy['frame_ms'] != 20 or
            policy['max_retries'] != 2 or policy['pause_consecutive_cells'] != 3 or
            policy['quiet_seconds'] != 2 or policy['baseline_max_attempts'] != 3 or policy['baseline_seconds'] != 6 or
            policy['settle_seconds'] != 5 or policy['baseline_max_dbfs'] != -53):
        raise ValueError('Noise gate must match the frozen exploratory v1/v2/v3 contract')


def highpass100(raw):
    """Fixed diagnostic/quiet filter, no thresholds or activity selection."""
    raw = np.asarray(raw, dtype=float)
    if raw.ndim != 1 or not np.isfinite(raw).all():
        raise ValueError('Filter signal must be finite mono PCM')
    sos = butter(4, 100, btype='highpass', fs=RATE, output='sos')
    return sosfiltfilt(sos, raw, padtype='odd', padlen=15)


def gate_signal(raw, policy):
    raw = np.asarray(raw, dtype=float)
    if raw.ndim != 1 or not np.isfinite(raw).all():
        raise ValueError('Quiet signal must be finite mono PCM')
    if policy['version'] != 3:
        return raw
    validate_noise_gate(policy)
    return highpass100(raw)


def baseline_level(raw, policy):
    """Use exactly the same per-window filter as quiet_gate, including padding."""
    return dbfs(gate_signal(raw, policy))


def quiet_gate(raw, baseline_dbfs, policy, mic3=None):
    raw = np.asarray(raw, dtype=float)
    if len(raw) < int(policy['quiet_seconds']*RATE) or not math.isfinite(baseline_dbfs):
        raise ValueError('Incomplete quiet window or invalid settled baseline')
    filtered = gate_signal(raw, policy)
    level = dbfs(filtered)
    def peak_dbfs(x):
        return float(10*np.log10(max(float(frame_power(x).max()), 1e-20)/32768**2))
    peak = peak_dbfs(filtered)
    diagnostics = {}
    if policy['version'] == 3:
        lowpass = butter(4, 100, btype='lowpass', fs=RATE, output='sos')
        def low_level(x):
            return dbfs(sosfiltfilt(lowpass, x, padtype='odd', padlen=15))
        if mic3 is not None:
            mic3 = np.asarray(mic3, dtype=float)
            if mic3.shape != raw.shape or not np.isfinite(mic3).all():
                raise ValueError('MIC3 diagnostic must match the quiet window')
        diagnostics = dict(gate_dbfs=level, full_band_max_20ms_dbfs=peak_dbfs(raw),
                           mic1_lowpass100_dbfs=low_level(raw),
                           mic3_lowpass100_dbfs=None if mic3 is None else low_level(mic3),
                           filter=V3_FILTER.copy())
    rise_reference = (max(baseline_dbfs, policy['rise_reference_min_dbfs'])
                      if policy['version'] == 3 else baseline_dbfs)
    if policy['version'] == 3:
        diagnostics.update(rise_reference_dbfs=rise_reference,
                           filtered_baseline_delta_db=level-baseline_dbfs)
    delta = level-rise_reference
    level_passed = (abs(delta) <= policy['level_tolerance_db'] if policy['version'] == 1 else
                    level <= policy['level_max_dbfs'] and delta <= policy['level_rise_max_db'])
    return dict(raw_dbfs=dbfs(raw), **diagnostics, baseline_dbfs=baseline_dbfs, delta_db=delta,
                max_20ms_dbfs=peak, samples=len(raw),
                level_passed=level_passed,
                transient_passed=peak <= policy['transient_max_dbfs'],
                passed=level_passed and peak <= policy['transient_max_dbfs'])


class NoiseRetryQueue:
    """Retries go after remaining cells; three distinct consecutive noisy cells pause.

    A clean gate resets the streak, even if the acoustic/STT result fails. Missing
    gates are acquisition failures, not permission to retry. Exhausted cells stay
    unresolved. Repeated attempts of one cell cannot impersonate three cells.
    """
    def __init__(self, trials, policy):
        starts = [t.get('start_attempt', 1) for t in trials]
        if any(type(a) is not int or not 1 <= a <= policy['max_retries']+1 for a in starts):
            raise ValueError('Starting attempt exceeds the lifetime cell retry budget')
        self.pending = deque(zip(trials, starts))
        self.policy = policy
        self.noisy_cells = []
        self.exhausted = []
        self.paused = False

    def next(self):
        return self.pending.popleft() if self.pending and not self.paused else None

    def finish(self, trial, attempt, noise_passed):
        cell = trial['run_id']
        if noise_passed is None:
            return 'acquisition-failed'
        if noise_passed is True:
            self.noisy_cells.clear()
            return 'accepted'
        if cell not in self.noisy_cells:
            self.noisy_cells.append(cell)
        if attempt <= self.policy['max_retries']:
            self.pending.append((trial, attempt+1))
            action = 'retry-queued'
        else:
            self.exhausted.append(cell)
            action = 'retries-exhausted'
        if len(self.noisy_cells) >= self.policy['pause_consecutive_cells']:
            self.paused = True
            return 'pause-three-noisy-cells'
        return action
