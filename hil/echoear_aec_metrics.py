"""Frozen demo measurement definitions v1; PCM is signed 16-bit at 16 kHz.

Activity never depends on AFE/uplink output. See docs/echoear-aec-bench.md.
No delay search, waveform identity gate, or qualification thresholds live here.
"""
import numpy as np

RATE = 16000
FRAME = 320
AFE_DELAY = 1024  # Only MR, filter=4, FD_LOW_COST, 512-sample adapter, ESP-SR 2.4.7.


def pcm(values):
    x = np.asarray(values, dtype=np.float64)
    if x.ndim != 1 or not len(x) or not np.isfinite(x).all():
        raise ValueError('Expected nonempty finite mono PCM')
    return x


def power(x):
    return float(np.mean(pcm(x) ** 2))


def dbfs(x):
    return float(10 * np.log10(max(power(x), 1e-20) / 32768**2))


def frame_power(x):
    x = pcm(x)
    n = len(x) // FRAME
    if not n:
        raise ValueError('At least one complete 20 ms frame required')
    return np.mean(x[:n * FRAME].reshape(n, FRAME)**2, axis=1)


def reference_threshold(quiet):
    # 6 dB above the loudest complete quiet frame; at least 2 LSB RMS.
    # A separate settled quiet capture is required at EACH reference gain.
    return max(4.0, float(np.max(frame_power(quiet))) * 10**(6/10))


def activity(reference, quiet, windows):
    p = frame_power(reference)
    known = np.zeros(len(p), dtype=bool)
    for start, end in windows:
        if not 0 <= start < end <= len(reference):
            raise ValueError('Stimulus extent outside capture')
        # Complete frames only; uncertainty at either boundary is not silence.
        known[(np.arange(len(p))*FRAME >= start) &
              ((np.arange(len(p))+1)*FRAME <= end)] = True
    return (p > reference_threshold(quiet)) & known


def reduction(raw, output, mask, *, delay=AFE_DELAY):
    raw, output = pcm(raw), pcm(output)
    if type(delay) is not int or delay < 0 or delay >= len(output):
        raise ValueError('Invalid independently measured delay')
    mask = np.asarray(mask, dtype=bool)
    indices = np.flatnonzero(mask)
    if not len(indices):
        return dict(scorable=False, reason='No independently active complete frames')
    if (indices[-1]+1)*FRAME > min(len(raw), len(output)-delay):
        return dict(scorable=False, reason='Missing delayed output; never pad with silence')
    a = np.concatenate([raw[i*FRAME:(i+1)*FRAME] for i in indices])
    b = np.concatenate([output[i*FRAME+delay:(i+1)*FRAME+delay] for i in indices])
    ratios = 10*np.log10(np.maximum(frame_power(a), 1e-20) /
                         np.maximum(frame_power(b), 1e-20))
    return dict(scorable=True, active_frames=len(indices), active_samples=len(a),
                integrated_reduction_db=float(10*np.log10(max(power(a), 1e-20)/max(power(b), 1e-20))),
                median_frame_reduction_db=float(np.median(ratios)),
                raw_dbfs=dbfs(a), residual_dbfs=dbfs(b))


def echo_metrics(raw, afe, reference, quiet, windows, *, delay=AFE_DELAY):
    mask = activity(reference, quiet, windows)
    replies = []
    for start, end in windows:
        local = mask.copy()
        local[:int(np.ceil(start/FRAME))] = False
        local[end//FRAME:] = False
        active = np.flatnonzero(local)
        first = local.copy()
        if len(active):
            first[active[0]+25:] = False
        replies.append(dict(stimulus_samples=[start, end],
                            reference_onset_sample=int(active[0]*FRAME) if len(active) else None,
                            whole=reduction(raw, afe, local, delay=delay),
                            first_500ms=reduction(raw, afe, first, delay=delay)))
    return dict(definitions='echoear-aec-metrics-v1', delay_samples=delay,
                reference_threshold_power=reference_threshold(quiet),
                integrated=reduction(raw, afe, mask, delay=delay), replies=replies)


def near_metrics(raw, afe, windows, *, delay=AFE_DELAY):
    # Known source extent alone selects near-only control; no AFE selection.
    mask = np.zeros(len(raw)//FRAME, dtype=bool)
    for start, end in windows:
        if not 0 <= start < end <= len(raw):
            raise ValueError('Near stimulus outside capture')
        mask[int(np.ceil(start/FRAME)):end//FRAME] = True
    result = reduction(raw, afe, mask, delay=delay)
    if result['scorable']:
        result['near_only_attenuation_db'] = result.pop('integrated_reduction_db')
        # This is a report, not a new release threshold.
        result['preservation_bound_db'] = 1.0
        result['within_preservation_bound'] = abs(result['near_only_attenuation_db']) <= 1.0
    return result


def interval_latency(start, end):
    """Bounds in seconds, including clock/sample uncertainty, never midpoint credit."""
    if start is None or end is None:
        return dict(scorable=False, reason='Unresolved clock/sample mapping')
    if not all(np.isfinite(start + end)) or start[0] > start[1] or end[0] > end[1]:
        raise ValueError('Invalid time interval')
    return dict(scorable=True, lower_s=end[0]-start[1], upper_s=end[1]-start[0])


def missing_playback(start_interval, active_intervals, coverage):
    """All BotStarted attempts remain in denominator, including missing/unknown."""
    if start_interval is None or coverage is None:
        return dict(outcome='timing-unscorable', failed=True, acoustically_scorable=False)
    lo, hi = start_interval
    # Any overlap with the uncertain one-second window prevents false absence credit.
    possible = any(b >= lo and a <= hi+1 for a, b in active_intervals)
    certain = any(a >= hi and b <= lo+1 for a, b in active_intervals)
    if certain:
        return dict(outcome='playback', failed=False, acoustically_scorable=True)
    if not possible and coverage[0] <= lo and coverage[1] >= hi+1:
        return dict(outcome='missing-playback', failed=True, acoustically_scorable=False)
    return dict(outcome='timing-unscorable', failed=True, acoustically_scorable=False)


def post_opus_residual(uplink, mask, mapping):
    """Use independently supplied board->uplink ranges; never fit to output.

    Rows: board_start, board_end, uplink_start, gap_free, evidence. Coordinates
    refer to the delayed AFE signal (raw index + 1024), after Opus decoding.
    Unknown/gapped coverage invalidates the result instead of inserting zeros.
    """
    uplink = pcm(uplink)
    selected = []
    for i in np.flatnonzero(mask):
        a, b = int(i*FRAME+AFE_DELAY), int((i+1)*FRAME+AFE_DELAY)
        rows = [r for r in mapping if r['board_start'] <= a and r['board_end'] >= b]
        if len(rows) != 1 or not rows[0].get('gap_free') or not rows[0].get('evidence'):
            return dict(scorable=False, reason='Missing independent gap-free uplink coverage')
        row = rows[0]
        start = row['uplink_start'] + a-row['board_start']
        if start < 0 or start+FRAME > len(uplink):
            return dict(scorable=False, reason='Missing uplink samples')
        selected.append(uplink[start:start+FRAME])
    if not selected:
        return dict(scorable=False, reason='No independently active frames')
    x = np.concatenate(selected)
    return dict(scorable=True, residual_dbfs=dbfs(x), active_samples=len(x),
                median_frame_residual_dbfs=float(np.median(10*np.log10(np.maximum(frame_power(x), 1e-20)/32768**2))))


def character_health(rows, display_errors):
    """Character mode uses adaptive animation; no artificial 24 fps floor."""
    enabled = bool(rows) and all(r.get('enabled') is True and r.get('errors') == 0 for r in rows)
    activity_seen = len(rows) >= 2 and rows[-1].get('posts', 0) > rows[0].get('posts', 0)
    return dict(enabled=enabled, activity=activity_seen, display_errors=display_errors,
                passed=enabled and activity_seen and display_errors == 0)
