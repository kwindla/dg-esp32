"""AEC-only qualification (decision 88, spec rev 2 R3/R4): near-end preservation on the local AFE output.

Pure functions. Per barge cell: raw MIC1, AFE output, reference (MIC3) with its quiet reference, the Jabra request
time mapped to a capture sample, and the dry source PCM. Per fixture a single-talk baseline cell supplies the paired
templates (raw and AFE) and the raw-mic acoustic path gains. The AFE is never consulted for alignment, the window,
eligibility or validity (R3).

Alignment and delivery (R3/R6, decision 92): the paired single-talk RAW capture of the same fixture is matched against
the double-talk raw mic within +-150 ms of the playback-timing prediction (request time + fixture lead + the declared
median Jabra delay). Acceptance is raw-only and a VALIDITY guard (failure -> outcome unknown -> retake, never a
miss): the peak correlation, its ambiguity (ratio to the best competing peak beyond +-5 ms), a peak not at the search
boundary, and the near-component level (least-squares projection of the raw mic on the template, in dB; 0 dB =
delivered at the single-talk level). Thresholds are PROVISIONAL (plan), to be frozen from calibration hold A.

Double-talk overlap (R4): source-relative frames are mapped sample-accurately to the capture. A frame is VERIFIED
double talk when the dry source is active and >= 50 % of its samples have measured reference activity (no padding).
Eligibility is a swappable per-class rule (ELIGIBILITY, named in the plan), declared before hold A (decisions 93/94):
'coverage75' over one EXPOSURE per cell, used for the coverage denominator, the verified minimum, the content mask and
the uncovered report alike. Stop: the exposure is the whole fixture (denominator = every source-active frame,
whenever the bot yields); eligible iff verified >= max(10, ceil(0.75 x 15)) = 12. Sentences: the exposure ends with
the far end's measured active extent (the reference-active run overlapping the stimulus, pauses <= 200 ms bridged;
the post-yield remainder is single talk, witnessed by the baselines; a later run after a longer pause -- in-reply TTS
pauses of 220-520 ms occur -- is outside the exposure and excluded from all four); eligible iff verified inside the
exposure >= max(10, ceil(0.75 x exposed source-active frames)) and the first verified source-active frame is within
the first 5 source-active frames (uncovered prefix <= 4 frames). Otherwise ineligible by acoustic evidence (never a
miss). Reported per cell: uncovered frames by position (prefix / internal / suffix), the onset 5-bit mask, leading
verified run and count, the sample-level intersection, onset / end coverage, the far end's measured end, and (report
only) the AFE correlation over in-dip onset frames of the first 200 ms of source-active content. The 3-frame padded
count is reported separately.

Timing (decision 92 item 6): the playback-timing prediction uses a raw-only per-hold delay estimate (hold_delay):
the declared prior plus the median offset of the hold's own valid single-talk captures once enough exist.

Content bar (decision 91/92):
  b-whole  |corr| AFE vs the paired single-talk AFE template, 100 Hz - 4 kHz, best lag +-20 ms, over the verified
           frames >= T_c (0.50, fixed).
  local    a pluggable local-content (hole) rule over the 200 ms segment correlations, UNSET until designed on hold A
           (decision 92); while unset there is no near-end success (None). Serializable: LOCAL_RULES[name](params).
           Decision 97: 'level-hole' (hole_profile: sliding 200 ms band level relative to the whole overlap, plus a
           fixed-segment correlation clause for full-level splices); targets >= 400 ms holes and 200 ms ending mutes;
           200 ms internal holes and onset mutes are a documented detection limit (docs/echoear-aec-q-scope.md).
  lvl      whole-overlap AFE level - (dry source level + raw-mic path gain G): PROVISIONAL floor, reported, not gated
           until frozen.
  clip     AFE output clipping inside the verified overlap of an eligible cell is a miss.
  report   per-100 ms deficits, the 1-4 kHz band deficit, the level loss, segment correlations, padded overlap.
"""
import wave

import numpy as np
from scipy.signal import butter, correlate, resample_poly, sosfiltfilt

RATE, FRAME, AFE_DELAY = 16000, 320, 1024
LAG = 320                      # +-20 ms
ALIGN_HALF = 2400              # +-150 ms around the playback-timing prediction (decision 92)
AMBIG_EXCL = 80                # competing peaks beyond +-5 ms
MIN_DT_FRAMES = 15             # 300 ms
SUB = 5                        # 100 ms sub-windows
T_C = .50                      # b-whole, frozen (nearend-validation-content.md); the rest is in the plan
_SOS = butter(4, (100, 4000), btype='band', fs=RATE, output='sos')
_HF = butter(4, (1000, 4000), btype='band', fs=RATE, output='sos')


def read_source(path):
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2: raise ValueError('fixture must be mono 16-bit PCM')
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float); rate = w.getframerate()
    if rate != RATE: x = resample_poly(x, RATE, rate)
    return x, active_span(x)


def frame_power(x):
    n = len(x)//FRAME
    return np.mean(np.asarray(x[:n*FRAME], float).reshape(n, FRAME)**2, axis=1) if n else np.zeros(0)


def active_span(x):
    p = frame_power(x)
    if not len(p): return None
    a = np.flatnonzero(p > max(1.0, float(p.max())*.001))
    return (int(a[0])*FRAME, (int(a[-1])+1)*FRAME) if len(a) else None


def _db(e): return 10*np.log10(max(float(e), 1e-12)/32768**2)


def band(x, sos=_SOS):
    x = np.asarray(x, float)
    return sosfiltfilt(sos, x) if len(x) > 30 else x


def _ncc(t, x):
    """|normalized cross-correlation| of template t over every lag of x (len(x) >= len(t))."""
    t = t-t.mean(); n = len(t)
    dots = correlate(x, t, mode='valid')
    ex = correlate(x*x, np.ones(n), mode='valid')-correlate(x, np.ones(n), mode='valid')**2/n
    den = np.sqrt(np.maximum(ex, 1e-12))*np.sqrt(max(float(np.dot(t, t)), 1e-12))
    return np.abs(dots/den)


def baseline(raw, afe, source, span, onset):
    """Single-talk baseline of one fixture: raw/AFE templates and raw-mic path gains (broadband, 1-4 kHz).
    onset: raw sample of the source's first active sample (the raw/dry match of a near-only capture)."""
    first, last = span; n = last-first
    if onset is None or onset < 0 or onset+n+AFE_DELAY > len(afe): return None
    src, r = np.asarray(source[first:last], float), np.asarray(raw[onset:onset+n], float)
    act = frame_power(src) > max(1.0, float(frame_power(src).max())*.001)
    idx = np.concatenate([np.arange(k*FRAME, (k+1)*FRAME) for k in np.flatnonzero(act)])
    g = _db(np.mean(r[idx]**2))-_db(np.mean(src[idx]**2))
    g_hf = _db(np.mean(band(r, _HF)[idx]**2))-_db(np.mean(band(src, _HF)[idx]**2))
    at = np.asarray(afe[onset+AFE_DELAY:onset+AFE_DELAY+n], float)
    floor, tband = band_floor(afe, onset+AFE_DELAY), template_band_db(at)
    above = [int(k) for k in np.flatnonzero(act) if floor is not None and k < len(tband) and tband[k] >= floor+BAND_MARGIN_DB]
    cfloor, cband = band_floor(afe, onset+AFE_DELAY, _SOS), template_band_db(at, _SOS)
    above_c = [int(k) for k in np.flatnonzero(act) if cfloor is not None and k < len(cband) and cband[k] >= cfloor+BAND_MARGIN_DB]
    return dict(raw_template=r, afe_template=at,
                path_gain_db=float(g), path_gain_hf_db=float(g_hf), raw_dbfs=_db(np.mean(r[idx]**2)),
                afe_dbfs=_db(np.mean(at[idx]**2)), onset=int(onset), afe_band_floor_dbfs=floor,
                afe_corr_band_floor_dbfs=cfloor, corr_band_onset_frame=above_c[0] if above_c else None,
                template_band_dbfs=[float(x) for x in tband], band_onset_frame=above[0] if above else None)


BAND_MARGIN_DB = 15.0          # report-only comparator (decision 95): template 1-4 kHz band >= AFE floor + 15 dB


def template_band_db(x, sos=_HF):
    """Per-frame band level (dBFS) of an AFE window: 1-4 kHz by default, the 100 Hz-4 kHz correlation band with
    sos=_SOS."""
    p = frame_power(band(np.asarray(x, float), sos))
    return np.array([_db(v) for v in p])


def band_floor(afe, a0, sos=_HF):
    """The AFE 1-4 kHz band floor of a single-talk capture: median frame level over the 0.45 s before the template
    (ending 50 ms before it); None when the capture has no such pre-roll."""
    lo, hi = max(0, a0-8000), a0-800
    if hi-lo < 3200: return None
    return float(np.median(template_band_db(np.asarray(afe[lo:hi], float), sos)))


def predicted_onset(c, blocks, anchors, span, delay_samples):
    """Playback-timing prediction of the source's first active raw sample: the Jabra request time mapped to a capture
    sample through the clock anchors, plus the fixture lead and the declared median Jabra delay. Raw-side only."""
    from echoear_aec_timing import sample_interval
    j = c.get('jabra') or {}
    t = j.get('request_monotonic_s')
    if t is None or not j.get('duration_valid', True): return None
    best = None
    for b in blocks:
        iv = sample_interval(int(b[0]), blocks, anchors)
        if iv and iv[0] <= t: best = (int(b[0]), iv)
    if best is None: return None
    s0, iv = best
    return s0+int(round((t-(iv[0]+iv[1])/2)*RATE))+span[0]+int(delay_samples)


def single_talk_onset(raw, source, span):
    from echoear_aec_accept_analysis import match
    first, last = span
    m = match(raw, source[first:min(last, first+8000)])
    return int(m['at']) if m.get('valid') else None


def align(raw, base, predicted, *, half=ALIGN_HALF):
    """Raw-only onset: the paired single-talk raw template searched within +-half of the timing prediction."""
    if base is None or predicted is None: return dict(valid=False, reason='no baseline / no playback timing')
    t = band(base['raw_template']); n = len(t)
    lo = max(0, int(predicted)-half); seg = band(np.asarray(raw[lo:int(predicted)+half+n], float))
    if len(seg) < n+1: return dict(valid=False, reason='window outside the capture')
    c = _ncc(t, seg); i = int(np.argmax(c))
    other = c.copy(); other[max(0, i-AMBIG_EXCL):i+AMBIG_EXCL+1] = 0
    onset = lo+i
    r = np.asarray(raw[onset:onset+n], float); tr = np.asarray(base['raw_template'], float)
    proj = float(np.dot(r, tr)/max(float(np.dot(tr, tr)), 1e-12))
    return dict(valid=True, onset=onset, corr=float(c[i]), ambiguity=float(c[i]/max(float(other.max()), 1e-9)),
                boundary=bool(i < AMBIG_EXCL or i > len(c)-1-AMBIG_EXCL), near_level_db=float(20*np.log10(max(abs(proj), 1e-6))),
                predicted=int(predicted), offset=onset-int(predicted))


def delivery_failures(al, rule):
    """The delivery clauses an alignment fails, as [(clause, message)]: alignment | corr | ambiguity | boundary | level."""
    if not al.get('valid'): return [('alignment', al.get('reason', 'no alignment'))]
    out = []
    if al['corr'] < rule['corr_min']: out.append(('corr', f"raw corr {al['corr']:.3f} < {rule['corr_min']}"))
    if al['ambiguity'] < rule['ambiguity_min']: out.append(('ambiguity', f"ambiguity {al['ambiguity']:.2f} < {rule['ambiguity_min']}"))
    if al['boundary']: out.append(('boundary', 'peak at the search boundary'))
    if 'offset_max_samples' in rule and abs(al['offset']) > rule['offset_max_samples']:
        out.append(('offset', f"raw onset {al['offset']} samples from the timing prediction (> {rule['offset_max_samples']})"))
    if not rule['level_min_db'] <= al['near_level_db'] <= rule['level_max_db']:
        out.append(('level', f"near component {al['near_level_db']:.1f} dB outside [{rule['level_min_db']}, {rule['level_max_db']}]"))
    return out


def delivery_ok(al, rule):
    """Raw-only delivery/alignment acceptance (validity). rule: plan['q']['nearend']['delivery'] (PROVISIONAL)."""
    why = delivery_failures(al, rule)
    return (not why), '; '.join(m for _, m in why) or None


def wrong_stimulus_nulls(templates, captures, *, half=ALIGN_HALF):
    """Delivery null from the hold's own single-talk captures: every fixture's raw template searched in every OTHER
    fixture's single-talk raw capture at that capture's onset (near speech of the wrong stimulus is the hardest
    null). templates: {fixture: baseline}; captures: {fixture: (raw, onset)}. Report rows; no threshold is set here."""
    out = []
    for ft, base in templates.items():
        for fc, (raw, onset) in captures.items():
            if fc == ft or base is None or onset is None: continue
            al = align(raw, base, onset, half=half)
            out.append(dict(template=str(ft).split('/')[-1], capture=str(fc).split('/')[-1], valid=al.get('valid'),
                            corr=al.get('corr'), ambiguity=al.get('ambiguity'), near_level_db=al.get('near_level_db'),
                            boundary=al.get('boundary')))
    return out


def hold_delay(offsets, rule):
    """Raw-only per-hold Jabra delay (declared before hold A, plan nearend.timing): offsets are the hold's valid
    single-talk raw/dry onsets minus their playback-timing prediction at the prior. Below min_baselines the prior is
    used. Returns dict(delay_samples, source, n, median_offset)."""
    import statistics
    xs = sorted(int(x) for x in offsets if x is not None)
    if len(xs) < rule['min_baselines']:
        return dict(delay_samples=int(rule['prior_samples']), source='prior', n=len(xs), median_offset=None)
    m = statistics.median(xs)
    return dict(delay_samples=int(round(rule['prior_samples']+m)), source='hold', n=len(xs), median_offset=float(m),
                spread=xs[-1]-xs[0])


def min_verified_frames(params):
    """Eligibility 'min-verified-frames': at least params['frames'] verified double-talk frames."""
    k = int(params['frames'])
    def rule(dt, s_act, ctx=None):
        n = int(np.sum(dt))
        return (n >= k), (None if n >= k else f'verified double talk {n*20} ms < {k*20} ms'), {}
    return rule


def far_end_extent(ref_active, onset, n_frames, *, bridge_frames=10):
    """The far end's measured active extent around a stimulus, in capture frames: the reference-active run (gaps of
    <= bridge_frames bridged) that first overlaps the stimulus span [onset, onset + n_frames*FRAME). None if none."""
    ra = np.asarray(ref_active, bool)
    if not ra.any(): return None
    idx = np.flatnonzero(ra)
    runs, a, b = [], int(idx[0]), int(idx[0])
    for k in idx[1:]:
        if k-b-1 <= bridge_frames: b = int(k)
        else: runs.append((a, b)); a = b = int(k)
    runs.append((a, b))
    f0, f1 = onset//FRAME, (onset+n_frames*FRAME-1)//FRAME
    for a, b in runs:
        if b >= f0 and a <= f1: return (a, b)
    return None


def coverage(dt, s_act, ctx, *, exposure='fixture', bridge_frames=10):
    """One cell's exposure and coverage (decision 94). The EXPOSURE is the set of source frames the claim covers, used
    for all four of: the coverage denominator, the verified minimum, the content mask and the uncovered report.
      'fixture'   (stop): every source frame; the denominator is every source-active frame of the fixture whenever
                  the bot yields, and post-yield frames are uncovered.
      'far-end'   (sentences): source frames that start before the end of the far end's measured active extent (the
                  reference-active run first overlapping the stimulus, pauses <= bridge_frames bridged). A later
                  reference run separated by a longer pause is not part of the exposure: its verified frames are
                  excluded from the minimum and the content score (reported as verified_after_exposure).
    Also: the onset report (the first five source-active frames: mask, leading verified run, count, index of the
    first verified source-active frame), onset / end coverage, the sample-level intersection, the far end's end."""
    onset, ra = ctx['onset'], np.asarray(ctx['ref_active'], bool)
    n = len(s_act)
    ext = far_end_extent(ra, onset, n, bridge_frames=bridge_frames)
    end_sample = (ext[1]+1)*FRAME if ext else None
    if exposure == 'fixture': expo = np.ones(n, bool)
    else: expo = np.array([bool(ext) and onset+k*FRAME < end_sample for k in range(n)])
    enclosed = expo & s_act
    vm = dt & expo                                         # verified frames inside the exposure: the scored mask
    act = np.flatnonzero(s_act); ver = np.flatnonzero(vm)
    unc = [int(k) for k in np.flatnonzero(enclosed & ~dt)]
    lo, hi = (int(ver[0]), int(ver[-1])) if len(ver) else (None, None)
    where = dict(prefix=[k for k in unc if lo is None or k < lo], internal=[k for k in unc if lo is not None and lo < k < hi],
                 suffix=[k for k in unc if hi is not None and k > hi])
    def run(seq):
        c = 0
        for k in seq:
            if not vm[k]: break
            c += 1
        return c
    first5 = act[:5]
    act_ver = [i for i, k in enumerate(act) if vm[k]]
    samples = np.repeat(ra, FRAME)
    inter = sum(int(np.sum(samples[max(0, onset+k*FRAME):max(0, onset+(k+1)*FRAME)])) for k in np.flatnonzero(enclosed))
    enc = np.flatnonzero(enclosed)
    return dict(exposure=exposure, source_active_frames=len(act), enclosed_frames=int(enclosed.sum()),
                verified_frames=int(vm.sum()), verified_after_exposure=int(np.sum(dt & ~expo)),
                fraction=(int(vm.sum())/int(enclosed.sum())) if enclosed.sum() else 0.0,
                uncovered=where, uncovered_count=len(unc), after_far_end_frames=int(np.sum(s_act & ~expo)),
                post_far_end_frames=int(sum(1 for k in act if not (bool(ext) and onset+k*FRAME < end_sample))),
                onset_mask=''.join('1' if vm[k] else '0' for k in first5), leading_verified_frames=run(first5),
                verified_count_first5=int(sum(bool(vm[k]) for k in first5)),
                first_verified_active_index=act_ver[0] if act_ver else None,
                onset_covered_ms=20*run(act), end_covered_ms=20*run(enc[::-1]) if len(enc) else 0,
                intersection_samples=inter, intersection_ms=inter/16,
                far_end_end_ms=None if end_sample is None else (end_sample-onset)/16,
                far_end_extent_frames=list(ext) if ext else None, _scored=vm, _exposure=expo)


def coverage75(params):
    """Eligibility 'coverage75' (decisions 93/94): with the exposure of coverage() (params['exposure']: 'fixture' for
    the stop, 'far-end' for sentences), verified frames inside the exposure >= max(min_frames, ceil(fraction x the
    exposed source-active frames)) -- the stop: max(10, ceil(.75 x 15)) = 12 of 15; and, with
    params['onset_within_frames'] = N > 0, the first verified source-active frame lies within the first N
    source-active frames (uncovered prefix <= N-1 frames)."""
    import math
    frac, kmin = params.get('fraction', .75), int(params.get('min_frames', 10))
    within, bridge, expo = int(params.get('onset_within_frames', 0)), int(params.get('bridge_frames', 10)), params.get('exposure', 'fixture')
    def rule(dt, s_act, ctx):
        cov = coverage(dt, s_act, ctx, exposure=expo, bridge_frames=bridge)
        need = max(kmin, math.ceil(frac*cov['enclosed_frames']-1e-9))
        cov['required_frames'] = need
        why = []
        if cov['verified_frames'] < need:
            why.append(f"verified double talk {cov['verified_frames']} of {cov['enclosed_frames']} exposed source-active "
                       f"frames < {need} (max({kmin}, {frac:.0%}))")
        if within and (cov['first_verified_active_index'] is None or cov['first_verified_active_index'] >= within):
            why.append(f"onset not covered: first verified source-active frame {cov['first_verified_active_index']} "
                       f"not within the first {within}")
        return (not why), ('; '.join(why) or None), cov
    return rule


def segment_min_corr(params):
    """Local rule 'segment-min-corr' (a candidate form for hold A): every counted 200 ms segment (origin 'source' or
    'first_dt') has |corr| >= params['min_corr'], with at least params.get('min_segments', 1) segments."""
    key = 'segments_from_first_dt' if params.get('origin') == 'first_dt' else 'segments'
    def rule(m):
        segs = m.get(key) or []
        return len(segs) >= params.get('min_segments', 1) and all(x['corr'] >= params['min_corr'] for x in segs)
    return rule


ELIGIBILITY = {'min-verified-frames': min_verified_frames, 'coverage75': coverage75}
HOLE_WINDOW, HOLE_MIN_VERIFIED = 10, 5      # sliding 200 ms windows, counted with >= 5 verified frames (decision 97)


def hole_profile(dt, tb, seg, lag):
    """Decision 97 measure: over sliding 200 ms windows (step one frame) of the scored frames with >= 5 verified frames,
    the 100 Hz-4 kHz band level of the AFE (at the cell's selected lag) minus the paired template's, relative to the
    same difference over the whole scored overlap (dB). A hole (muted / removed near end) is a deep negative window;
    a uniform attenuation is 0. Returns dict(min_db, start_frame, position, windows) or None without a window."""
    idx = _fidx(list(np.flatnonzero(dt)))
    if not len(idx): return None
    whole = 10*np.log10(max(float(np.mean(seg[idx+lag]**2)), 1e-12)/max(float(np.mean(tb[idx]**2)), 1e-12))
    ver = np.flatnonzero(dt); lo_v, hi_v = int(ver[0]), int(ver[-1])
    best, at, n = None, None, 0
    for s0 in range(0, len(dt)-HOLE_WINDOW+1):
        ks = [k for k in range(s0, s0+HOLE_WINDOW) if dt[k]]
        if len(ks) < HOLE_MIN_VERIFIED: continue
        ii = _fidx(ks); n += 1
        d = 10*np.log10(max(float(np.mean(seg[ii+lag]**2)), 1e-12)/max(float(np.mean(tb[ii]**2)), 1e-12))-whole
        if best is None or d < best: best, at = float(d), s0
    if best is None: return None
    pos = 'onset' if at <= lo_v else ('ending' if at+HOLE_WINDOW > hi_v else 'internal')
    return dict(min_db=best, start_frame=at, position=pos, windows=n, whole_db=float(whole))


def lag_tolerant(segs, dt, tb, seg, lag, window):
    """Decision 100: each fixed 200 ms segment's |corr| maximised over lags within +-window samples of the cell's
    selected lag (clipped to the +-20 ms search range); adds corr_lagtol and lagtol_offset (relative to the cell lag)."""
    for x in segs:
        ks = [k for k in range(x['start_frame'], min(x['start_frame']+SEG, len(dt))) if dt[k]]
        ii = _fidx(ks)
        best = max((_corr_at(tb[ii], seg, ii, L), L-lag) for L in range(max(0, lag-window), min(2*LAG, lag+window)+1))
        x['corr_lagtol'], x['lagtol_offset'] = float(best[0]), int(best[1])
    return segs


def level_hole(params):
    """Local rule 'level-hole' (decision 97): content preserved iff the hole profile minimum >= level_threshold_db
    and, when seg_corr_threshold is a number, every counted 200 ms segment correlation >= it (a full-level splice of
    other speech is invisible to the level clause). window_frames / min_verified must equal the measure's constants.
    Declared targets: holes >= 400 ms (internal, ending, splice) and 200 ms ending mutes. Detection limit: 200 ms
    internal holes and onset mutes (b-whole >= 0.50 remains the only check there). None when no window exists."""
    if params['window_frames'] != HOLE_WINDOW or params['min_verified'] != HOLE_MIN_VERIFIED:
        raise ValueError('level-hole: window_frames / min_verified differ from the measure')
    w = params['seg_lag_window_samples']
    if type(w) is not int or not 0 <= w <= LAG: raise ValueError(f'level-hole: seg_lag_window_samples {w!r} not an integer in [0, {LAG}]')
    th, sc = float(params['level_threshold_db']), params['seg_corr_threshold']
    def rule(m):
        h = m.get('hole')
        if not h: return None
        if h['min_db'] < th: return False
        segs = m.get('segments') or []
        if sc is not None and segs and any('corr_lagtol' not in x for x in segs):
            raise ValueError('level-hole: segments lack the lag-tolerant correlation (measure / rule mismatch)')
        if sc is not None and any(x['corr_lagtol'] < sc for x in segs): return False
        return True
    rule.seg_lag_window = w
    return rule


LOCAL_RULES = {'segment-min-corr': segment_min_corr, 'level-hole': level_hole}


PARAMS = {'min-verified-frames': (('frames',), ()),          # (required, optional): every parameter is required, none
          'coverage75': (('exposure', 'onset_within_frames', 'fraction', 'min_frames', 'bridge_frames'), ()),   # defaults
          'segment-min-corr': (('min_corr', 'origin', 'min_segments'), ()),
          'level-hole': (('level_threshold_db', 'seg_corr_threshold', 'seg_lag_window_samples', 'window_frames', 'min_verified'), ())}                 # silently (decision 96)


def rule_from(spec, registry):
    """A serialized rule dict(name, params) -> callable; None -> None (unset). Unknown or missing required parameter
    keys are refused (decision 95): a misspelt or stale key in a manifest cannot silently drop a clause."""
    if spec is None: return None
    if set(spec) - {'name', 'params'}: raise ValueError(f"rule spec keys {sorted(set(spec)-{'name', 'params'})} unknown")
    params = spec.get('params') or {}
    req, opt = PARAMS[spec['name']]
    if set(params) - set(req) - set(opt): raise ValueError(f"{spec['name']}: unknown parameter keys {sorted(set(params)-set(req)-set(opt))}")
    if set(req) - set(params): raise ValueError(f"{spec['name']}: missing parameter keys {sorted(set(req)-set(params))}")
    if spec['name'] == 'coverage75' and params['exposure'] not in ('fixture', 'far-end'):
        raise ValueError(f"coverage75: exposure {params['exposure']!r}")
    return registry[spec['name']](params)


DEFAULT_ELIGIBILITY = dict(name='coverage75', params=dict(fraction=.75, min_frames=10, onset_within_frames=0, bridge_frames=10,
                                                         exposure='fixture'))


def dt_mask(ref_active, source, span, onset, *, dilate=0):
    """Double-talk frames over the source span, mapped sample-accurately: source active AND >= 50 % of the frame's
    capture samples reference-active. dilate=0 is the verified count; dilate>0 the padded (reported) count."""
    first, last = span
    src = np.asarray(source[first:last], float)
    p = frame_power(src); s_act = p > max(1.0, float(p.max())*.001)
    ra = np.asarray(ref_active, bool)
    if dilate: ra = np.convolve(ra.astype(int), np.ones(2*dilate+1, int), mode='same') > 0
    samples = np.repeat(ra, FRAME)
    r_act = np.array([float(np.mean(samples[onset+k*FRAME:onset+(k+1)*FRAME])) >= .5 if 0 <= onset+k*FRAME and onset+(k+1)*FRAME <= len(samples)
                      else False for k in range(len(s_act))])
    return s_act & r_act, s_act


SEG = 10                       # 200 ms segments (report / input to the local-content rule)
SEG_MIN_DT = 5                 # a segment counts when >= 5 of its 10 frames are verified double talk


def _corr_at(t, seg, idx, lag):
    """|ncc| of template samples t (at idx) against seg[idx+lag]; 0 for a zero/constant-variance window."""
    y = seg[idx+lag]; y = y-y.mean(); tt = t-t.mean()
    d = float(np.sqrt(np.dot(tt, tt)))*float(np.sqrt(np.dot(y, y)))
    return abs(float(np.dot(tt, y)/d)) if d > 1e-9 else 0.0


def _fidx(ks):
    return np.concatenate([np.arange(k*FRAME, (k+1)*FRAME) for k in ks]) if len(ks) else np.zeros(0, int)


def onset_dip_report(dt, expo, s_act, tb, seg, lag, a, src, base, ref_active, onset, *, a_lag=None):
    """Report only (decisions 94 c / 95): the first 10 source-active frames (200 ms of source-active content). Per frame:
    verified or a dip (exposed, unverified: a far-end pause), its position (prefix before the first verified frame,
    else internal), the reference-active fraction, the single-frame AFE-vs-template correlation at the cell's lag, the
    AFE level deficit, the template's 1-4 kHz band level and whether it is >= the AFE band floor + 15 dB (frames at the
    floor cannot show content loss in single talk either). Summaries per group (prefix dips, internal dips, verified
    comparator): multi-frame correlation over all, and over the above-floor frames only. None without a dip."""
    act0 = np.flatnonzero(s_act)[:10]
    dips = [int(k) for k in act0 if not dt[k] and expo[k]]
    if not dips: return None
    ver = np.flatnonzero(dt); first = int(ver[0]) if len(ver) else len(dt)
    tband = template_band_db(base['afe_template']); floor = base.get('afe_band_floor_dbfs')
    tcb = template_band_db(base['afe_template'], _SOS); cfloor = base.get('afe_corr_band_floor_dbfs')
    ahf = template_band_db(a_lag) if a_lag is not None else None
    acb = template_band_db(a_lag, _SOS) if a_lag is not None else None
    samples = np.repeat(np.asarray(ref_active, bool), FRAME)
    rows = []
    for k in act0:
        k = int(k); ii = _fidx([k])
        role = 'verified' if dt[k] else ('dip' if expo[k] else 'unexposed')
        fr = samples[max(0, onset+k*FRAME):max(0, onset+(k+1)*FRAME)]
        rows.append(dict(frame=k, role=role, position=None if role != 'dip' else ('prefix' if k < first else 'internal'),
                         ref_fraction=float(np.mean(fr)) if len(fr) else None, corr=_corr_at(tb[ii], seg, ii, lag),
                         level_deficit_db=float(_db(np.mean(a[ii]**2))-(_db(np.mean(src[ii]**2))+base['path_gain_db'])),
                         template_band_dbfs=float(tband[k]) if k < len(tband) else None,
                         above_floor=None if floor is None or k >= len(tband) else bool(tband[k] >= floor+BAND_MARGIN_DB),
                         template_corr_band_dbfs=float(tcb[k]) if k < len(tcb) else None,
                         corr_band_above_floor=None if cfloor is None or k >= len(tcb) else bool(tcb[k] >= cfloor+BAND_MARGIN_DB),
                         afe_band_dbfs=float(ahf[k]) if ahf is not None and k < len(ahf) else None,
                         afe_corr_band_dbfs=float(acb[k]) if acb is not None and k < len(acb) else None))
    def summary(sel):
        ks = [r['frame'] for r in sel]; up = [r['frame'] for r in sel if r['above_floor']]
        cb = [r['frame'] for r in sel if r['corr_band_above_floor']]
        c = lambda xs: _corr_at(tb[_fidx(xs)], seg, _fidx(xs), lag) if xs else None
        return dict(frames=ks, corr=c(ks), above_floor_frames=up, corr_above_floor=c(up),      # 1-4 kHz comparator
                    above_corr_band_floor_frames=cb, corr_above_corr_band_floor=c(cb))        # 100 Hz-4 kHz comparator
    return dict(lag_samples=int(lag-LAG), band_floor_dbfs=floor, corr_band_floor_dbfs=cfloor, margin_db=BAND_MARGIN_DB, frames=len(dips),
                prefix=summary([r for r in rows if r['position'] == 'prefix']),
                internal=summary([r for r in rows if r['position'] == 'internal']),
                verified=summary([r for r in rows if r['role'] == 'verified']), per_frame=rows)


def segments(dt, tb, seg, lag, *, origin):
    """200 ms segment correlations over the verified frames (origin: source frame index of the first segment)."""
    out = []
    for s0 in range(origin, len(dt), SEG):
        ks = [k for k in range(s0, min(s0+SEG, len(dt))) if dt[k]]
        if len(ks) < SEG_MIN_DT: continue
        ii = _fidx(ks)
        out.append(dict(seg=(s0-origin)//SEG, start_frame=s0, frames=len(ks), corr=_corr_at(tb[ii], seg, ii, lag)))
    return out


def measure(raw, afe, ref_active, source, span, onset, base, *, t_c=T_C, local_rule=None, level_floor_db=None,
            eligibility=None, level_floor_report_db=None):
    """Content bar for one barge cell at a raw-derived onset (raw-only delivery checked by the caller).
    local_rule: callable(measure dict) -> bool, or None (unset: no success). level_floor_db None: not gated
    (PROVISIONAL, reported). eligibility: callable(dt, s_act, ctx) -> (bool, reason); None = DEFAULT_ELIGIBILITY."""
    if base is None: return dict(scorable=False, reason='no single-talk baseline for this fixture')
    if onset is None: return dict(scorable=False, reason='no raw-mic onset')
    first, last = span; n = last-first
    if onset < 0 or onset+n > len(raw) or onset+AFE_DELAY-LAG < 0 or onset+AFE_DELAY+n+LAG > len(afe):
        return dict(scorable=False, reason='window outside the capture')
    dt, s_act = dt_mask(ref_active, source, span, onset)
    padded, _ = dt_mask(ref_active, source, span, onset, dilate=3)
    frames = int(dt.sum())
    out = dict(scorable=True, dt_frames=frames, dt_frames_padded=int(padded.sum()), src_frames=int(s_act.sum()), onset=int(onset))
    ctx = dict(onset=onset, span=span, ref_active=ref_active)
    ok, why, cov = (eligibility or rule_from(DEFAULT_ELIGIBILITY, ELIGIBILITY))(dt, s_act, ctx)
    cov = cov or coverage(dt, s_act, ctx)
    sm, expo = cov.pop('_scored'), cov.pop('_exposure')   # content mask: verified frames inside the exposure (d94)
    out['coverage'] = cov
    if not ok: return dict(out, eligible=False, reason=why)
    if not sm.any(): return dict(out, eligible=False, reason='no verified frame inside the exposure')
    dt = sm
    src = np.asarray(source[first:last], float)
    a = np.asarray(afe[onset+AFE_DELAY:onset+AFE_DELAY+n], float)
    idx = _fidx(np.flatnonzero(dt))
    tb = band(base['afe_template'])
    seg = band(np.asarray(afe[onset+AFE_DELAY-LAG:onset+AFE_DELAY+n+LAG], float))
    best, lag_at = -1.0, 0
    for L in range(0, 2*LAG+1, 2):
        c = _corr_at(tb[idx], seg, idx, L)
        if c > best: best, lag_at = c, L
    first_dt = int(np.flatnonzero(dt)[0])
    segs = segments(dt, tb, seg, lag_at, origin=0)
    segs_dt = segments(dt, tb, seg, lag_at, origin=first_dt)
    level = _db(np.mean(a[idx]**2))-(_db(np.mean(src[idx]**2))+base['path_gain_db'])
    subs = []
    for b0 in range(0, len(dt), SUB):
        ks = [k for k in range(b0, min(b0+SUB, len(dt))) if dt[k]]
        if len(ks) >= 3:
            ii = _fidx(ks); subs.append(_db(np.mean(a[ii]**2))-(_db(np.mean(src[ii]**2))+base['path_gain_db']))
    hf = _db(np.mean(band(a, _HF)[idx]**2))-(_db(np.mean(band(src, _HF)[idx]**2))+base['path_gain_hf_db'])
    clipped = int(np.sum(np.abs(a[idx]) >= 32767))
    # release window (report only): the 300 ms of source-active frames after the far end's measured end
    rel = None
    e = out['coverage'].get('far_end_end_ms')
    if e is not None:
        k0 = int(np.ceil(e*16/FRAME))
        ks = [k for k in range(max(0, k0), min(len(s_act), k0+15)) if s_act[k]]
        if len(ks) >= SEG_MIN_DT: rel = dict(frames=len(ks), corr=_corr_at(tb[_fidx(ks)], seg, _fidx(ks), lag_at))
    # in-dip onset frames (report only): exposed source-active frames among the first 200 ms of source-active content
    # that are not verified double talk (far-end pauses: single talk at the mic); does the AFE keep the near end there?
    a_lag = np.asarray(afe[onset+AFE_DELAY+lag_at-LAG:onset+AFE_DELAY+lag_at-LAG+n], float)   # AFE at the selected lag
    dip_corr = onset_dip_report(dt, expo, s_act, tb, seg, lag_at, a, src, base, ref_active, onset, a_lag=a_lag)
    out.update(eligible=True, corr=float(best), corr_lag_samples=int(lag_at-LAG), segments=segs, segments_from_first_dt=segs_dt,
               level_deficit_db=float(level), afe_clipped_samples=clipped,
               report=dict(sub100_min_deficit_db=min(subs, default=None), band_hf_deficit_db=float(hf), level_loss_db=float(level),
                           level_floor_provisional_db=level_floor_report_db,
                           level_floor_provisional_pass=None if level_floor_report_db is None else bool(level >= level_floor_report_db),
                           release_window=rel, onset_dip=dip_corr, seg_corr_min=min((x['corr'] for x in segs), default=None)))
    out['clause_corr'] = bool(best >= t_c)
    out['clause_level'] = None if level_floor_db is None else bool(level >= level_floor_db)
    out['hole'] = hole_profile(dt, tb, seg, lag_at)          # decision 97 measure; reported for every eligible cell
    w = getattr(local_rule, 'seg_lag_window', None)
    if w is not None: lag_tolerant(out['segments'], dt, tb, seg, lag_at, w)   # decision 100 splice clause input
    cl = None if local_rule is None else local_rule(out)
    out['clause_local'] = None if cl is None else bool(cl)
    if clipped: out['success'] = False; out['reason'] = 'AFE output clipping inside the verified overlap'
    elif not out['clause_corr']: out['success'] = False
    elif out['clause_level'] is False: out['success'] = False
    elif out['clause_local'] is None: out['success'] = None          # local-content rule unset (decision 92)
    else: out['success'] = bool(out['clause_local'])
    return out


# ---- constructed edits (R8 / decision 92 D): labelled, with edited-sample coverage inside the scored overlap ------

def constructed_edits(afe, onset, span, dt, *, other=None, echo=None, rng=None):
    """Edits of the AFE window at the cell's verified overlap. Each edit carries its label and the number of changed
    samples inside the verified overlap; an edit that changes none (a no-op, e.g. a splice without later material) is
    reported with covered=0 and must be excluded from any separation claim."""
    from scipy.signal import butter as _b, sosfiltfilt as _f
    rng = rng or np.random.default_rng(5)
    first, last = span; n = last-first; a0 = onset+AFE_DELAY
    A = np.asarray(afe, float); w = A[a0:a0+n].copy()
    over = np.zeros(n, bool); over[_fidx(np.flatnonzero(dt))] = True
    ks = np.flatnonzero(dt); f0, f1 = int(ks[0])*FRAME, (int(ks[-1])+1)*FRAME; mid = (f0+f1)//2
    def mute(lo, hi): v = w.copy(); v[max(0, lo):min(n, hi)] = 0; return v
    edits = {'content-loss': {}, 'attenuation (not content loss)': {}, 'band (report)': {}, 'total suppression (level floor)': {}}
    cl = edits['content-loss']
    cl['zeroed'] = np.zeros(n)
    for ms in (80, 120, 200, 400): cl[f'mute-onset-{ms}ms'] = mute(f0, f0+ms*16)
    cl['mute-ending-200ms'] = mute(f1-3200, f1); cl['mute-ending-400ms'] = mute(f1-6400, f1)
    cl['mute-internal-200ms'] = mute(mid-1600, mid+1600); cl['mute-internal-400ms'] = mute(mid-3200, mid+3200)
    for ms in (200, 400):
        k = ms*16; v = w.copy()
        src = mid+k if mid+2*k <= f1 else (f0 if f0+k <= mid else None)      # later material, else earlier
        if src is not None: v[mid:mid+k] = w[src:src+k]
        cl[f'splice-{ms}ms'] = v
    if other is not None:
        t = np.resize(np.asarray(other, float), n); cl['wrong-speech'] = t*np.sqrt(np.mean(w**2)/max(np.mean(t**2), 1e-9))
    if echo is not None and len(echo) >= n:
        e = np.asarray(echo[:n], float); cl['echo-level-matched'] = e*np.sqrt(np.mean(w**2)/max(np.mean(e**2), 1e-9))
    env = np.resize(np.sqrt(np.repeat(frame_power(w), FRAME)), n); cl['envelope-matched-noise'] = rng.standard_normal(n)*env
    dtm = np.repeat(dt, FRAME)[:n]
    edits['attenuation (not content loss)'].update({'dt-only-minus-10dB': np.where(dtm, w*10**(-.5), w),
                                                     'dt-only-minus-20dB': np.where(dtm, w*.1, w), 'window-minus-20dB': w*.1})
    edits['total suppression (level floor)'].update({'window-minus-30dB': w*10**(-1.5), 'window-minus-40dB': w*.01})
    lo, hi = max(0, mid-3200), min(n, mid+3200)                # d97/d98: 400 ms local leakage at the overlap midpoint
    rms = np.sqrt(np.mean(w[lo:hi]**2))
    if echo is not None and len(echo) >= hi:
        e = np.asarray(echo[lo:hi], float); v = w.copy(); v[lo:hi] = e*rms/max(np.sqrt(np.mean(e**2)), 1e-9); cl['leak-echo-local-400ms'] = v
    if other is not None:
        o = np.resize(np.asarray(other, float), n)[lo:hi]; v = w.copy(); v[lo:hi] = o*rms/max(np.sqrt(np.mean(o**2)), 1e-9)
        cl['leak-other-local-400ms'] = v
    lp = _b(8, 2000, btype='low', fs=RATE, output='sos')
    edits['band (report)']['remove-above-2kHz'] = _f(lp, w)
    out = []
    for label, group in edits.items():
        for name, v in group.items():
            changed = np.abs(v-w) > 1e-6
            B = A.copy(); B[a0:a0+n] = v
            out.append(dict(name=name, label=label, afe=B, covered=int(np.sum(changed & over)), changed=int(np.sum(changed))))
    return out
