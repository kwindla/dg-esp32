"""Floor-relative echo metrics for EchoEar AEC replies (prospective, report-only).

Proposal for a v8.8 echo contract. No existing gate imports this module and no
historical verdict changes. The frozen gate couples echo to the room through an
absolute full-band AFE residual (-50 dBFS) and an integrated reduction that is
itself floor-limited when the residual approaches the room floor.

Masks are never re-derived here. Callers pass the analyzer's own inputs: the
reference, its per-gain quiet capture, the extent the analyzer scored and,
optionally, the per-frame source-gate state from ``source_gate``. Activity
comes from ``echoear_aec_metrics.activity``; the AFE output is read at the
frozen ``AFE_DELAY`` offset; the absolute quantities are the frozen
``reduction`` values and the frozen ``echo_verdict``, both imported.

Verdict rule (asymmetric; review round 1, coordinator decision 61):
* PASS only when the frozen absolute clause clears (integrated reduction >= 20,
  first 500 ms >= 10 when the source is louder than -30 dBFS, post-guard
  full-band AFE residual <= -50 dBFS). Floor correction never creates a PASS:
  subtracting a silent-state floor assumes the background during playback
  equals the background outside it, which the AFE (state-dependent noise
  transfer) and the room (nonstationarity) both violate in the direction that
  hides echo.
* FAIL, per check ('bounded-miss'), when the absolute value misses and the
  favourable bound still misses: the absolute quantity credited with the most
  the measured room can explain. The AFE background upper bound is the larger
  of the floor mean and either pre/post half, plus resolve_factor sigma; the
  mic background lower bound is the smaller, minus resolve_factor sigma
  (clipped at 0). Post-guard: active AFE power less the AFE background upper
  bound must still exceed -50 dBFS. ERLE: (mic active - mic background lower
  bound) / (AFE active - AFE background upper bound) must still be below its
  limit. The bound needs only a floor, not a scorable corrected estimate, and
  is never less favourable than the corrected interval's optimistic end, so
  it replaces both earlier FAIL clauses (review round 2: a 'room share below
  0.5 dB' FAIL misattributed threshold-near misses the room fully explains).
* Otherwise 'floor-limited' (unscorable, never a pass): the measured room
  could explain the miss, or there is no floor estimate.
Intent: a louder room (floor raised before, during and after the reply) can
explain the miss, so it becomes floor-limited; a real echo miss in a quiet
room misses even after the room is credited and stays FAIL. Residual limit:
background louder during the reply than anywhere in the floor window is
indistinguishable from echo by power, exactly as in the frozen gate, and
stable bookends do not prove the floor bounds the active-state background.

Per reply:
* floor: local reference-silent mic/AFE floor (mean power of silent frames
  within +/-floor_window_s of the reply's active frames, excluding an echo-tail
  margin after, and an onset margin before, every reference-active frame).
  Uncertainty from 0.5 s blocks cut inside contiguous silent runs only (never
  across a gap). Pre- and post-reply halves are compared; a difference above
  ``floor_half_max_diff_db`` (or a missing half) disables the corrected
  estimate, and the larger half bounds the room share.
* floor-corrected: ERLE = (mic_active - mic_floor) / (afe_active - afe_floor)
  and the AFE residual echo (afe_active - afe_floor), each with a
  +/- resolve_factor sigma interval that includes the background variance of
  the active mean (one block's at most for long windows: no credit for
  averaging over slow room changes; short windows get short-window variance).
* coherent: mic and AFE power linearly coherent with the reference
  (non-overlapping Hann segments fully inside the mask), bias measured with
  time-shifted reference segments. Report-only.
* burst: worst 20 ms local ERLE (mic envelope max over +/-40 ms vs AFE frame)
  over echo-dominated frames; ``overshoot`` keeps the plain same-frame
  AFE-over-mic maximum. Report-only.
"""
import numpy as np
from echoear_aec_metrics import FRAME, AFE_DELAY, RATE, activity, frame_power, pcm, reduction
from echoear_aec_campaign_analysis import echo_verdict

DEFINITIONS = 'echoear-aec-floor-relative-v1-proposal'
PARAMS = dict(
    floor_window_s=3.0,       # silent frames considered on each side of the active span
    tail_exclusion_s=0.30,    # reverberation/AFE tail after any active frame is not floor
    onset_exclusion_s=0.04,   # frame-quantized onset uncertainty before active frames
    block_frames=25,          # 0.5 s blocks, cut inside contiguous silent runs only
    min_floor_blocks=3,       # complete contiguous blocks required for a floor estimate
    min_half_frames=25,       # each of the pre/post-reply floor halves
    floor_half_max_diff_db=3.0,
    min_active_frames=10,     # corrected estimate needs >= 200 ms of active frames
    min_echo_snr_db=10.0,     # mic echo must clearly exceed the mic floor to be scorable
    resolve_factor=3.0,       # interval half-width in sigma
    segment=1024,             # coherence segment (64 ms), non-overlapping
    min_segments=20,
    null_shifts=8,
    overshoot_floor_margin_db=10.0,
    burst_envelope_frames=2,  # mic envelope = max mic frame within +/-2 frames (+/-40 ms)
    burst_mic_snr_db=20.0,    # only echo-dominated mic envelope frames are judged
)
# Frozen limits, reapplied; absolute clause is the frozen echo_verdict itself.
PROPOSED = dict(whole_erle_min_db=20.0, first_500ms_erle_min_db=10.0, post_guard_residual_echo_max_dbfs=-50.0)
EPS = 1e-20
INF = float('inf')


def db(ratio):
    return float(10 * np.log10(max(float(ratio), EPS)))


def ratio_db(x, y):
    """10 log10(x / y) for powers, defined at y == 0 (inf, or None for 0/0)."""
    x, y = float(x), float(y)
    if y > 0:
        return db(x / y)
    return INF if x > 0 else None


def dbfs_power(p):
    return db(p / 32768**2)


def aligned_frame_powers(raw, afe, *, delay=AFE_DELAY):
    """Mic and delayed-AFE frame powers on the mic frame grid (complete frames only)."""
    raw, afe = pcm(raw), pcm(afe)
    if type(delay) is not int or delay < 0 or delay >= len(afe):
        raise ValueError('Invalid independently measured delay')
    n = min(len(raw), len(afe) - delay) // FRAME
    if n < 1:
        raise ValueError('No complete aligned frame')
    return frame_power(raw[:n * FRAME]), frame_power(afe[delay:delay + n * FRAME])


def reply_masks(ref, qref, extent, *, gate=None, n_frames=None, params=PARAMS):
    """Analyzer masks plus a local reference-silent floor mask.

    ``extent`` is the analyzer's scored extent (playback ``source_extent``; the
    start is clipped at 0 as in ``echo_measure``) or an ``echo_metrics`` window.
    ``gate`` is the analyzer's per-frame ``source_gate`` list (True closed,
    False open, None unknown) or None when the hold had no reply-start guard.
    With ``n_frames=None`` the whole/first/post masks are exactly the masks
    ``echo_measure`` passes to ``reduction``.
    """
    start, end = int(max(0, extent[0])), int(extent[1])
    whole = activity(ref, qref, [(start, end)])
    everywhere = activity(ref, qref, [(0, len(ref))])
    n = len(whole) if n_frames is None else min(len(whole), n_frames)
    whole, everywhere = whole[:n], everywhere[:n]
    post = whole.copy() if gate is None else whole & np.array([g is False for g in gate[:n]], dtype=bool)
    ids = np.flatnonzero(whole)
    first = whole.copy()
    if len(ids):
        first[ids[0] + 25:] = False
    tail = int(round(params['tail_exclusion_s'] * RATE / FRAME))
    pre = int(np.ceil(params['onset_exclusion_s'] * RATE / FRAME))
    near_active = everywhere.copy()
    for i in np.flatnonzero(everywhere):
        near_active[max(0, i - pre):i + tail + 1] = True
    silent = ~near_active
    if len(ids):
        span = int(round(params['floor_window_s'] * RATE / FRAME))
        local = np.zeros(n, dtype=bool)
        local[max(0, ids[0] - span):ids[-1] + span + 1] = True
        silent &= local
    else:
        silent[:] = False
    return dict(whole=whole, post_guard=post, first_500ms=first, floor=silent)


def contiguous_blocks(ids, block):
    """Index blocks of ``block`` frames cut inside contiguous runs of ``ids``."""
    if not len(ids):
        return []
    runs = np.split(ids, np.flatnonzero(np.diff(ids) > 1) + 1)
    return [run[j * block:(j + 1) * block] for run in runs for j in range(len(run) // block)]


def mean_sigma(n, frame_sigma, block_sigma, block):
    """Sigma of the background mean over ``n`` active frames.

    ``block_sigma`` is the observed sigma of a ``block``-frame mean (it carries
    the frame-to-frame correlation). No averaging credit is given beyond one
    block: a +/-3 s floor cannot show that the room is stationary on the
    multi-second scale of a reply (appliance cycles, slow modulation), so a
    long active window is known only to one block's precision. For a short
    window the variance is never below the independent-frame value nor the
    block value scaled up as if independent, and never above a single frame's.
    """
    n = max(int(n), 1)
    if n >= block:
        return float(block_sigma)
    return float(min(frame_sigma, max(frame_sigma / np.sqrt(n), block_sigma * np.sqrt(block / n))))


def floor_estimate(mic_p, afe_p, silent, active_ids=None, *, params=PARAMS):
    ids = np.flatnonzero(silent)
    b = params['block_frames']
    blocks = contiguous_blocks(ids, b)
    base = dict(frames=int(len(ids)), blocks=len(blocks))
    if len(blocks) < params['min_floor_blocks']:
        return dict(base, scorable=False, reason='Too few contiguous local reference-silent floor blocks')
    out = dict(base, scorable=True)
    for name, p in (('mic', mic_p), ('afe', afe_p)):
        x = p[ids]
        means = np.array([p[k].mean() for k in blocks])
        frame_sigma = float(np.std(x, ddof=1))
        block_sigma = float(np.std(means, ddof=1))
        # Never below the independent-frame value: few blocks must not look certain.
        block_sigma = max(block_sigma, frame_sigma / np.sqrt(b))
        out.update({f'{name}_power': float(x.mean()), f'{name}_dbfs': dbfs_power(x.mean()),
                    f'{name}_frame_sigma': frame_sigma, f'{name}_block_sigma': block_sigma,
                    f'{name}_mean_sigma': block_sigma / np.sqrt(len(blocks))})
    a = afe_p[ids]
    out['afe_spread_db'] = ratio_db(np.percentile(a, 90), np.percentile(a, 10))
    # Pre/post-reply halves: a drifting room (or slow AFE recovery after the
    # reply) makes the silent floor a poor stand-in for the active background.
    halves = dict(stable=None, reason='No active frames')
    if active_ids is not None and len(active_ids):
        pre, post = ids[ids < active_ids[0]], ids[ids > active_ids[-1]]
        halves = dict(pre_frames=int(len(pre)), post_frames=int(len(post)))
        if min(len(pre), len(post)) < params['min_half_frames']:
            halves.update(stable=None, reason='Pre- or post-reply floor half too short to check drift')
        else:
            diffs = []
            for name, p in (('mic', mic_p), ('afe', afe_p)):
                hp, hq = float(p[pre].mean()), float(p[post].mean())
                halves.update({f'{name}_pre_dbfs': dbfs_power(hp), f'{name}_post_dbfs': dbfs_power(hq),
                               f'{name}_max_half_power': max(hp, hq), f'{name}_min_half_power': min(hp, hq)})
                d = ratio_db(hp, hq)
                diffs.append(0.0 if d is None else abs(d))
            halves['max_diff_db'] = max(diffs)
            ok = halves['max_diff_db'] <= params['floor_half_max_diff_db']
            halves.update(stable=ok, reason=None if ok else 'Pre/post-reply floor halves differ')
    out['halves'] = halves
    # Favourable background levels for the room credit: the larger AFE and the
    # smaller mic level among the mean and either half.
    out['afe_power_hi'] = max(out['afe_power'], halves.get('afe_max_half_power', 0.0))
    out['mic_power_lo'] = min(out['mic_power'], halves.get('mic_min_half_power', INF))
    return out


def floor_corrected(mic_p, afe_p, mask, floor, *, params=PARAMS):
    """Floor-corrected estimate over one mask, plus the favourable room credit.

    ``scorable`` refers to the corrected estimate. ``room_share_db`` and the
    ``favourable_*`` bounds (need only a floor) are computed whenever a floor
    exists: ``favourable_residual_dbfs`` = AFE active less the AFE background
    upper bound; ``favourable_erle_db`` = (mic active less the mic background
    lower bound) / (AFE active less the AFE background upper bound), INF when
    the AFE background bound covers the active power, None when the mic has
    nothing above its background bound.
    """
    ids = np.flatnonzero(mask)
    none = dict(resolved=False, excess_db=None, room_share_db=None, favourable_erle_db=None,
                favourable_residual_dbfs=None)
    if not len(ids):
        return dict(none, scorable=False, reason='No independently active complete frames')
    n = len(ids)
    m, a = float(mic_p[ids].mean()), float(afe_p[ids].mean())
    out = dict(active_frames=int(n), mic_dbfs=dbfs_power(m), afe_dbfs=dbfs_power(a), raw_reduction_db=ratio_db(m, a))
    if not floor.get('scorable'):
        return dict(out, **none, scorable=False, reason=floor['reason'])
    k, B = params['resolve_factor'], params['block_frames']
    # Floor-mean uncertainty plus the background share of the active mean.
    s_mic = k * float(np.hypot(floor['mic_mean_sigma'],
                               mean_sigma(n, floor['mic_frame_sigma'], floor['mic_block_sigma'], B)))
    s_afe = k * float(np.hypot(floor['afe_mean_sigma'],
                               mean_sigma(n, floor['afe_frame_sigma'], floor['afe_block_sigma'], B)))
    e_mic, e_afe = m - floor['mic_power'], a - floor['afe_power']
    lo, hi = e_afe - s_afe, e_afe + s_afe
    f_hi = floor['afe_power_hi'] + s_afe
    num, den = m - max(0.0, floor['mic_power_lo'] - s_mic), a - f_hi
    out.update(excess_db=ratio_db(a, floor['afe_power']),
               room_share_db=ratio_db(a, den) if den > 0 else INF,
               favourable_residual_dbfs=dbfs_power(den) if den > 0 else -INF,
               favourable_erle_db=INF if den <= 0 else (ratio_db(num, den) if num > 0 else None),
               mic_echo_snr_db=ratio_db(e_mic, floor['mic_power']) if e_mic > 0 else None,
               afe_resolution_dbfs=dbfs_power(s_afe), resolved=bool(lo > 0), below_floor=bool(hi <= 0))
    stable = floor['halves'].get('stable')
    if n < params['min_active_frames']:
        reason = 'Too few active frames for a corrected estimate'
    elif stable is not True:
        reason = floor['halves'].get('reason')
    elif e_mic - s_mic <= 0 or out['mic_echo_snr_db'] < params['min_echo_snr_db']:
        reason = 'Mic echo not sufficiently above mic floor'
    elif hi <= 0:
        # Not evidence of zero echo: inconsistent with a nonnegative echo
        # power plus the silent-state background.
        reason = 'Active AFE power below its floor interval (model-inconsistent)'
    else:
        reason = None
    if reason:
        return dict(out, scorable=False, reason=reason)
    out.update(scorable=True,
               erle_db=ratio_db(e_mic, e_afe) if e_afe > 0 else None,
               erle_low_db=ratio_db(e_mic - s_mic, hi),
               erle_high_db=ratio_db(e_mic + s_mic, lo) if lo > 0 else INF,
               residual_echo_dbfs=dbfs_power(e_afe) if e_afe > 0 else None,
               residual_echo_upper_dbfs=dbfs_power(hi),
               residual_echo_lower_dbfs=dbfs_power(lo) if lo > 0 else -INF)
    return out


def coherence_starts(mask, L, n_raw, n_afe, delay):
    """Sample starts of non-overlapping L-sample segments fully inside ``mask``."""
    per = -(-L // FRAME)  # frames covering L samples (1024 -> 4, not 3)
    starts, i = [], 0
    while i + per <= len(mask):
        if mask[i:i + per].all() and i * FRAME + L + delay <= n_afe and i * FRAME + L <= n_raw:
            starts.append(i * FRAME)
            i += per
        else:
            i += 1
    return starts


def coherent(raw, afe, ref, mask, *, delay=AFE_DELAY, params=PARAMS):
    """Report-only: reference-coherent power in mic and delayed AFE."""
    raw, afe, ref = pcm(raw), pcm(afe), pcm(ref)
    L = params['segment']
    starts = coherence_starts(mask, L, len(raw), len(afe), delay)
    K = len(starts)
    if K < params['min_segments']:
        return dict(scorable=False, reason='Too few complete active coherence segments', segments=K)
    w = np.hanning(L)
    R = np.array([np.fft.rfft(w * ref[s:s + L]) for s in starts])
    shifts = sorted({int(round(x)) for x in np.linspace(K / 4, 3 * K / 4, params['null_shifts'])} - {0, K})
    out = dict(segments=K, null_shifts=len(shifts))

    def fraction(Y, Rs, Syy):
        Srr = np.mean(np.abs(Rs)**2, 0) + EPS
        C = np.abs(np.mean(Y * np.conj(Rs), 0))**2 / (Srr * Syy)
        return float(np.sum(C * Syy) / np.sum(Syy))

    for name, x, off in (('mic', raw, 0), ('afe', afe, delay)):
        Y = np.array([np.fft.rfft(w * x[s + off:s + off + L]) for s in starts])
        Syy = np.mean(np.abs(Y)**2, 0) + EPS
        total = float(np.mean([np.mean(x[s + off:s + off + L]**2) for s in starts]))
        raw_frac = fraction(Y, R, Syy)
        null = np.array([fraction(Y, np.roll(R, s, axis=0), Syy) for s in shifts])
        null_mean = float(null.mean())
        if null_mean >= 1 - 1e-9:  # stationary tone: time-shifted reference is as coherent as the true one
            return dict(out, scorable=False, reason='Reference coherence not distinguishable from time shifts')
        sigma = max(float(null.std(ddof=1)) if len(null) > 1 else 0.0,
                    float(np.sqrt(np.sum(Syy**2)) / K / np.sum(Syy)))
        frac = (raw_frac - null_mean) / (1 - null_mean)
        r = params['resolve_factor'] * sigma / (1 - null_mean)
        out[name] = dict(total_dbfs=dbfs_power(total), coherent_fraction=frac, null_fraction=null_mean,
                         coherent_dbfs=dbfs_power(frac * total) if frac > 0 else None,
                         power=frac * total, resolution_power=r * total)
    mic, af = out['mic'], out['afe']
    if mic['power'] - mic['resolution_power'] <= 0:
        return dict(out, scorable=False, reason='No resolvable reference-coherent mic echo')
    lo, hi = af['power'] - af['resolution_power'], af['power'] + af['resolution_power']
    return dict(out, scorable=True, resolved=lo > 0,
                erle_db=ratio_db(mic['power'], af['power']) if af['power'] > 0 else None,
                erle_low_db=ratio_db(mic['power'] - mic['resolution_power'], hi) if hi > 0 else None,
                erle_high_db=ratio_db(mic['power'] + mic['resolution_power'], lo) if lo > 0 else INF)


def overshoot(mic_p, afe_p, mask, floor, *, params=PARAMS):
    ids = np.flatnonzero(mask)
    if not len(ids) or not floor.get('scorable'):
        return dict(scorable=False)
    keep = ids[afe_p[ids] > floor['afe_power'] * 10**(params['overshoot_floor_margin_db'] / 10)]
    if not len(keep):
        return dict(scorable=True, max_db=None, frames=0)
    r = 10 * np.log10(np.maximum(afe_p[keep], EPS) / np.maximum(mic_p[keep], EPS))
    j = int(np.argmax(r))
    return dict(scorable=True, max_db=float(r[j]), at_s=float(keep[j] * FRAME / RATE), frames=int(len(keep)))


def burst(mic_p, afe_p, mask, floor, *, params=PARAMS):
    """Worst 20 ms local ERLE: mic envelope (+/-40 ms max) over the AFE frame.

    Only frames whose mic envelope is echo-dominated (>= floor + burst_mic_snr_db)
    and whose AFE frame exceeds its floor by overshoot_floor_margin_db are
    judged, so room noise alone cannot make a burst; the envelope removes
    frame-quantization artifacts at word onsets and ends.
    """
    ids = np.flatnonzero(mask)
    if not len(ids) or not floor.get('scorable'):
        return dict(scorable=False)
    k = params['burst_envelope_frames']
    env = np.array([mic_p[max(0, i - k):i + k + 1].max() for i in ids])
    keep = (env >= floor['mic_power'] * 10**(params['burst_mic_snr_db'] / 10)) & \
           (afe_p[ids] > floor['afe_power'] * 10**(params['overshoot_floor_margin_db'] / 10))
    if not keep.any():
        return dict(scorable=True, min_local_erle_db=None, frames=0)
    local = 10 * np.log10(np.maximum(env[keep], EPS) / np.maximum(afe_p[ids][keep], EPS))
    j = int(np.argmin(local))
    return dict(scorable=True, min_local_erle_db=float(local[j]), at_s=float(ids[keep][j] * FRAME / RATE),
                afe_dbfs=dbfs_power(afe_p[ids][keep][j]), frames=int(keep.sum()))


MASKS = ('whole', 'first_500ms', 'post_guard')


def floor_relative_metrics(raw, afe, ref, qref, extent, *, gate=None, delay=AFE_DELAY, params=PARAMS):
    mic_p, afe_p = aligned_frame_powers(raw, afe, delay=delay)
    frozen_masks = reply_masks(ref, qref, extent, gate=gate, params=params)
    # Absolute quantities: the frozen reduction on the frozen (untruncated) masks.
    absolute = {name: reduction(raw, afe, frozen_masks[name], delay=delay) for name in MASKS}
    n = len(mic_p)
    masks = {k: v[:n] for k, v in frozen_masks.items()}
    fl = floor_estimate(mic_p, afe_p, masks['floor'], np.flatnonzero(masks['whole']), params=params)
    tail_mask = masks['whole'].copy()
    ids = np.flatnonzero(tail_mask)
    if len(ids):  # include the echo tail where a late burst would sit
        tail_mask[ids[0]:min(len(tail_mask), ids[-1] + int(params['tail_exclusion_s'] * RATE / FRAME) + 1)] = True
    result = dict(definitions=DEFINITIONS, delay_samples=delay, params=dict(params), floor=fl, absolute=absolute,
                  overshoot=overshoot(mic_p, afe_p, tail_mask, fl, params=params),
                  burst=burst(mic_p, afe_p, tail_mask, fl, params=params))
    for name in MASKS:
        result[name] = floor_corrected(mic_p, afe_p, masks[name], fl, params=params)
    result['coherent_whole'] = coherent(raw, afe, ref, masks['whole'], delay=delay, params=params)
    result['coherent_post_guard'] = coherent(raw, afe, ref, masks['post_guard'], delay=delay, params=params)
    return result


CHECKS = dict(whole=('whole_erle_min_db', 'integrated_reduction_db', True),
              first_500ms=('first_500ms_erle_min_db', 'integrated_reduction_db', True),
              post_guard=('post_guard_residual_echo_max_dbfs', 'residual_dbfs', False))


def _check(absolute, corrected, limit, key, at_least):
    fav_key = 'favourable_erle_db' if at_least else 'favourable_residual_dbfs'
    c = dict(resolved=bool(corrected.get('resolved')), excess_db=corrected.get('excess_db'),
             room_share_db=corrected.get('room_share_db'), favourable=corrected.get(fav_key),
             corrected_scorable=bool(corrected.get('scorable')), corrected_reason=corrected.get('reason'))
    if corrected.get('scorable'):
        lo, mid, hi = (('erle_low_db', 'erle_db', 'erle_high_db') if at_least else
                       ('residual_echo_lower_dbfs', 'residual_echo_dbfs', 'residual_echo_upper_dbfs'))
        c.update(corrected=corrected[mid], corrected_low=corrected[lo], corrected_high=corrected[hi])
    if not absolute.get('scorable'):
        return dict(c, outcome='unscorable', clause='absolute-unscorable', reason=absolute.get('reason'))
    v = absolute[key]
    c['absolute'] = v
    if (v >= limit) if at_least else (v <= limit):
        return dict(c, outcome='pass', clause='absolute')
    # Favourable bound on the corrected-mask powers, applied to the frozen
    # absolute as a dB credit (identical powers except trailing truncation).
    fav = c['favourable']
    if fav is not None:
        base = corrected['raw_reduction_db'] if at_least else corrected['afe_dbfs']
        fav = v + (fav - base) if np.isfinite(fav) and base is not None and np.isfinite(base) else fav
        c['favourable_absolute'] = fav
        if (fav < limit) if at_least else (fav > limit):
            return dict(c, outcome='fail', clause='bounded-miss')
    no_floor = corrected.get('room_share_db') is None
    return dict(c, outcome='floor-limited', clause='floor-limited',
                reason='No floor estimate' if no_floor else 'Absolute miss the measured room may explain')


def proposed_verdict(m, *, source_dbfs):
    """Asymmetric prospective verdict: 'pass', 'fail', 'floor-limited' or 'unscorable'.

    ``source_dbfs`` is the analyzer's first-500 ms source level (None when it
    could not be measured, which the frozen gate never passes). 'pass' is
    exactly the frozen ``echo_verdict``; floor correction only decides between
    'fail' and 'floor-limited' for absolute misses. 'floor-limited' and
    'unscorable' are never credited as a pass.
    """
    t = PROPOSED
    ab = m['absolute']
    frozen = echo_verdict(ab['whole'], ab['first_500ms'], source_dbfs, ab['post_guard'])
    names = [n for n in MASKS if n != 'first_500ms' or frozen['first_500ms_gated']]
    checks = {n: _check(ab[n], m[n], t[CHECKS[n][0]], CHECKS[n][1], CHECKS[n][2]) for n in names}
    outcomes = [c['outcome'] for c in checks.values()]
    if frozen['passed']:
        verdict = 'pass'
    elif 'fail' in outcomes:
        verdict = 'fail'
    elif 'unscorable' in outcomes or source_dbfs is None:
        verdict = 'unscorable'
    else:
        verdict = 'floor-limited'
    if verdict == 'pass':
        assert all(o == 'pass' for o in outcomes)
    clauses = sorted({c['clause'] for c in checks.values() if c['outcome'] == verdict})
    return dict(verdict=verdict, passed=verdict == 'pass', frozen_passed=frozen['passed'], carried_by=clauses,
                checks=checks, thresholds=dict(t), first_500ms_gated=frozen['first_500ms_gated'],
                report_only=dict(burst_min_local_erle_db=m['burst'].get('min_local_erle_db'),
                                 coherent_post_guard_erle_db=m['coherent_post_guard'].get('erle_db'),
                                 post_guard_excess_db=m['post_guard'].get('excess_db')))
