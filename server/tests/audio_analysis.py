"""Shared digital PCM analysis, with no WebRTC runtime dependency."""
import numpy as np
from scipy.signal import correlate

def correlation(recording, reference):
    """Normalized sliding correlation; silence/energy alone can never pass."""
    x, y = np.asarray(recording, dtype=float), np.asarray(reference, dtype=float)
    # Remove fixture leading/trailing silence, retaining the actual spoken waveform.
    active = np.flatnonzero(np.abs(y) > max(50, np.max(np.abs(y))*0.01))
    if not len(active):
        raise ValueError("Silent reference")
    y = y[max(0, active[0]-160):min(len(y), active[-1]+161)].copy()
    if len(x) < len(y):
        return 0.
    y -= y.mean()
    numerator = correlate(x, y, mode="valid", method="fft")
    # Cumulative sums avoid FFT cancellation artifacts in silent windows.
    sums = np.r_[0., np.cumsum(x)]
    squares = np.r_[0., np.cumsum(x*x)]
    energy = squares[len(y):] - squares[:-len(y)]
    centered = np.maximum(energy - (sums[len(y):]-sums[:-len(y)])**2/len(y), 0)
    reference_energy = np.dot(y,y)
    valid = centered > max(1., reference_energy * 1e-8)
    scores = np.zeros(len(numerator))
    scores[valid] = numerator[valid] / np.sqrt(centered[valid]*reference_energy)
    return float(np.clip(np.max(scores), -1, 1))
