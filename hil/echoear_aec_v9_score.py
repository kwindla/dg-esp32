"""v9 per-attempt scorer: O(1) in the hold's history (spec sections 2-3, 6, 8, 9).

score_attempt reads one attempt folder plus incremental tails of the hold's event, recorder,
SCTP and serial streams (byte offsets, never a whole-hold re-read) and returns registry findings,
endpoint scores and the retained record. The measurement building blocks are the frozen v8.7
analyzer functions (accept/campaign analysis, lull, metrics); only the per-attempt orchestration
is new. It never opens hardware.
"""
import json
import re
import wave
from pathlib import Path

import numpy as np

from echoear_aec_accept_analysis import (match, wer, classify_opportunity, gate_at_near, check_render_ledger,
                                         read)
from echoear_aec_bench import load_capture, sha
from echoear_aec_campaign_analysis import (context_playback, source_write_witness, echo_measure, flush_measure,
                                           interruption_copies, transport, delivery_counters, near_preservation,
                                           turn_events)
from echoear_aec_event_tail import JsonlTail
from echoear_aec_metrics import activity, frame_power, dbfs, interval_latency
from echoear_aec_noise_gate import highpass100
from echoear_aec_timing import sample_interval
import echoear_aec_gate_registry as registry
import echoear_aec_v9_rules as rules

SERIAL_KEEP = re.compile(r'GATE (?:closed|open) |AUDIO_CANCEL |SERVER |rst:|Guru Meditation|abort\(\)|panic|'
                         r'Backtrace|STATE Boot|Rebooting|AEC_GUARD_CONFIG')
REBOOT = re.compile(r'rst:0x|Guru Meditation|abort\(\) was called|Rebooting\.\.\.|Backtrace:')
ACCEPTED = ('UserStartedSpeakingFrame', 'InterruptionFrame')
LATENCY_LIMIT_S = {'stop': 1.2, 'sentence': 2.0}
TERM_OWNER = {t: g['id'] for g in registry.REGISTRY for t in g['terms']}
DELTA_COUNTERS = {('aec', 'capture', 'adc_clips'): 'mic', ('aec', 'capture', 'gain_clips'): 'mic',
                  ('aec', 'output_clips'): 'clip.afe_output', ('aec', 'reference_clips'): 'clip.reference'}


def load(path):
    """bench.load_capture, plus the named four-channel arms-diagnostic schema (dry run of retained
    aec-arms evidence only: microphone/reference/processed/linear)."""
    try:
        return load_capture(path)
    except ValueError:
        with np.load(path, allow_pickle=False) as data:
            names = [str(x) for x in data['channels']] if 'channels' in data else []
            if int(data['rate']) != 16000 or names[:3] != ['microphone', 'reference', 'processed']: raise
            x = data['pcm'].astype(float)
            timing = data['timing'].copy() if 'timing' in data else np.empty((0, 5))
        return x[:, 0], x[:, 2], x[:, 1], timing


class _AnchorIndex:
    """Validated, sorted anchors with a bisect index: the same result and the same exceptions as
    echoear_aec_timing.map_interval, without re-sorting and re-validating the anchors on every call."""
    def __init__(self, anchors):
        import math
        rows = sorted(anchors, key=lambda r: r['coordinate'])
        for i, row in enumerate(rows):
            if not all(math.isfinite(row[k]) for k in ('coordinate', 'lower_s', 'upper_s')):
                raise ValueError('Nonfinite anchor')
            if row['lower_s'] > row['upper_s'] or (i and row['coordinate'] <= rows[i-1]['coordinate']):
                raise ValueError('Invalid/duplicate clock anchor')
        self.rows, self.keys, self.n = rows, [r['coordinate'] for r in rows], len(anchors)


def fast_map_interval(value, anchors, _cache={}):
    import bisect, math
    if not math.isfinite(value):
        raise ValueError('Nonfinite clock coordinate')
    entry = _cache.get(id(anchors))
    if entry is None or entry[0] is not anchors or entry[1].n != len(anchors):
        entry = (anchors, _AnchorIndex(anchors))      # raises exactly as the original when invalid
        _cache.clear(); _cache[id(anchors)] = entry
    index = entry[1]
    i = bisect.bisect_left(index.keys, value)
    if i < len(index.keys) and index.keys[i] == value:
        return [index.rows[i]['lower_s'], index.rows[i]['upper_s']]
    if i == 0 or i == len(index.keys):
        return None
    lo, hi = index.rows[i-1]['lower_s'], index.rows[i]['upper_s']
    if lo > hi:
        raise ValueError('Nonmonotonic clock mapping')
    return [lo, hi]


import contextlib


@contextlib.contextmanager
def fast_timing():
    """Scoring-only: route the analyzer's interval mapping through fast_map_interval (equivalence-tested)."""
    import echoear_aec_timing as timing, echoear_aec_campaign_analysis as ca, echoear_aec_lull as lull
    old = (timing.map_interval, ca.map_interval, lull.map_interval)
    timing.map_interval = ca.map_interval = lull.map_interval = fast_map_interval
    try:
        yield
    finally:
        timing.map_interval, ca.map_interval, lull.map_interval = old


def F(gate, **kw):
    if gate not in registry.BY_ID: raise KeyError('Unregistered gate '+gate)
    return dict(gate=gate, **kw)


def jsonable(value):
    """NumPy-safe JSON conversion (freeze.analyzer_dry_run covers serialization)."""
    if isinstance(value, dict): return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray): return jsonable(value.tolist())
    if isinstance(value, np.bool_): return bool(value)
    if isinstance(value, np.integer): return int(value)
    if isinstance(value, np.floating):
        v = float(value); return v if np.isfinite(v) else None
    if isinstance(value, float) and not np.isfinite(value): return None
    return value


class HoldView:
    """Per-part cache: quiet reference, plan and byte-offset tails. Rows older than the current attempt
    window are dropped, so memory and per-attempt work stay bounded."""
    def __init__(self, root, plan, *, retain=False):
        self.root, self.p, self.retain = Path(root), plan, retain    # retain: offline replays may revisit rows
        qraw, qafe, qref, _ = load(self.root/'quiet.npz')
        self.qraw, self.qafe, self.qref = qraw, qafe, qref
        self.serial_path = self.root/'candidate-serial.log'
        self.serial_offset = 0; self.serial_lines = []; self.boot_mark = None
        self.recorded_tail = JsonlTail(lambda: sorted((self.root/'sessions').glob('*/events.jsonl')), max_bytes=1 << 30)
        self.sctp_tail = JsonlTail(lambda: [p for p in [self.root/'rtp/sctp.jsonl'] if p.exists()], max_bytes=1 << 30)
        self.downlink_tail = JsonlTail(lambda: [p for p in [self.root/'rtp/downlink.jsonl'] if p.exists()], max_bytes=1 << 30)
        self.recorded, self.sctp, self.downlink = [], [], []
        from echoear_aec_lull import declared_rule
        from echoear_aec_playback_template import declared_corner
        self.lull_policy = declared_rule(plan); self.highpass_hz = declared_corner(plan)

    def poll(self, keep_from_ns):
        if self.serial_path.exists():
            with self.serial_path.open('rb') as stream:
                stream.seek(self.serial_offset); data = stream.read()
            cut = data.rfind(b'\n')+1
            self.serial_offset += cut
            for line in data[:cut].decode('utf-8', 'replace').splitlines():
                if SERIAL_KEEP.search(line): self.serial_lines.append(line)
                # The part's own boot ends at its AEC_GUARD_CONFIG banner; only later banners are reboots.
                if self.boot_mark is None and 'AEC_GUARD_CONFIG' in line: self.boot_mark = len(self.serial_lines)
        if self.retain: keep_from_ns = 0
        self.recorded = [e for e in self.recorded if e.get('monotonic_ns', 0) >= keep_from_ns]
        self.recorded += [e for e in self.recorded_tail.poll() if e.get('monotonic_ns', 0) >= keep_from_ns]
        self.sctp = [e for e in self.sctp if e.get('monotonic_ns', 0) >= keep_from_ns]
        self.sctp += [e for e in self.sctp_tail.poll() if e.get('monotonic_ns', 0) >= keep_from_ns]
        keep_ms = keep_from_ns/1e6
        self.downlink = [e for e in self.downlink if e.get('send_ms', 0) >= keep_ms]
        self.downlink += [e for e in self.downlink_tail.poll() if e.get('send_ms', 0) >= keep_ms]

    def serial(self):
        return '\n'.join(self.serial_lines)

    def reboots(self):
        return [l for l in self.serial_lines[self.boot_mark or 0:] if REBOOT.search(l)] if self.boot_mark is not None else []


def fixture_template(path, n=8000):
    from scipy.signal import resample_poly
    with wave.open(str(path)) as w:
        source = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(float); rate = w.getframerate()
    if rate != 16000: source = resample_poly(source, 16000, rate)
    power = frame_power(source); active = np.flatnonzero(power > max(1, float(power.max())*.001))
    first = int(active[0])*320
    return source, source[first:min(len(source), first+n)], first, (int(active[-1])-int(active[0])+1)*320


def floors(raw, afe, delay=1024):
    """Decision 72 room-inclusive record: raw MIC1 and pre-guard AFE, full band and 100 Hz HPF."""
    out = {}
    hraw, hafe = highpass100(raw), highpass100(afe)
    for name, sl, asl in (('pre', slice(0, 32000), slice(delay, 32000+delay)), ('post', slice(-32000, None), slice(-32000, None))):
        out[name] = dict(raw_dbfs=dbfs(raw[sl]), afe_dbfs=dbfs(afe[asl]), raw_hpf_dbfs=dbfs(hraw[sl]), afe_hpf_dbfs=dbfs(hafe[asl]))
    return out


def baseline_floors(view):
    return floors(view.qraw, view.qafe)


def _signed_curve(target, template):
    """The normalized correlation of accept_analysis.match, returned signed for every lag."""
    from scipy.signal import correlate
    target = np.asarray(target, float); t = np.array(template, dtype=float, copy=True); t -= t.mean(); n = len(t)
    dots = correlate(target, t, mode='valid', method='fft'); ones = np.ones(n)
    energy = np.maximum(correlate(target**2, ones, mode='valid', method='fft')-correlate(target, ones, mode='valid', method='fft')**2/n, 1e-12)
    return dots/np.sqrt(energy*np.sum(t**2))


WITNESS_NOMINAL, WITNESS_TOLERANCE = 1024, 20
ENV_BAND_HZ, ENV_SEARCH, ENV_MIN_CORRELATION = (200, 3400), (384, 1665), .5


def _pitch_period(template, lo=32, hi=400, floor=.3):
    """Voiced pitch period of the fixture (samples): the strongest normalized autocorrelation peak in 40-500 Hz;
    None when the fixture is not clearly periodic."""
    t = np.asarray(template, float)-np.mean(template)
    ac = np.correlate(t, t, mode='full')[len(t)-1:]
    if ac[0] <= 0: return None
    ac = ac/ac[0]; lag = lo+int(np.argmax(ac[lo:hi]))
    return lag if ac[lag] >= floor else None


def envelope_delay(raw, afe, raw_at, n):
    """Polarity-free raw -> AFE delay: the lag maximizing the normalized correlation of the magnitude of the
    band-limited (200-3400 Hz) analytic signals over the fixture's raw interval. The magnitude is unchanged
    by inversion and keeps the pitch-rate ripple, so a half-period alias moves it by the alias."""
    from scipy.signal import butter, sosfiltfilt, hilbert
    sos = butter(4, ENV_BAND_HZ, btype='band', fs=16000, output='sos')
    env = lambda x: np.abs(hilbert(sosfiltfilt(sos, np.asarray(x, float))))
    er, ea = env(raw), env(afe)
    seg = er[raw_at:raw_at+n]-er[raw_at:raw_at+n].mean()
    best, lag = -2.0, None
    for L in range(*ENV_SEARCH):
        b = ea[raw_at+L:raw_at+L+n]
        if len(b) < n: break
        b = b-b.mean(); v = float(np.dot(seg, b)/np.sqrt(np.dot(seg, seg)*np.dot(b, b)+1e-12))
        if v > best: best, lag = v, L
    return dict(lag=lag, correlation=best)


def _peak_checks(curve, at):
    """accept_analysis.match validity for one chosen lag: |c| >= 0.25 and >= 1.1x any peak beyond +-640."""
    mag = np.abs(curve); peak = float(mag[at]); other = mag.copy(); other[max(0, at-640):at+641] = 0
    alternative = float(other.max()) if len(other) else 0.0
    return dict(valid=peak >= .25 and peak >= 1.1*alternative, at=int(at), correlation=float(curve[at]),
                alternative_peak=alternative)


REL_AMBIGUITY_MARGIN, REL_COMPETITOR_SPAN = .01, 64


def relative_delay(raw, afe, raw_at, n):
    """Relative raw -> AFE delay (v9.8, Sol v9.7): AFE matched directly against raw MIC1's own fixture-anchored
    n-sample interval with accept_analysis.match (|c| >= 0.25, >= 1.1x any peak beyond +-640). Ambiguity margin:
    any other local maximum of |c| within +-64 samples (about one pitch period) of the winner that lies outside
    1004-1044 or more than 20 samples from the winner and scores >= (1 - 0.01) x the winner makes it ambiguous.
    Declared from the 31 valid retained captures: their closest such competitor is 0.982 of the winner."""
    seg = np.asarray(raw[raw_at:raw_at+n], float)
    m = match(afe, seg)
    if not m.get('valid'): return dict(m, lag=None, ambiguous=False)
    curve = np.abs(_signed_curve(afe, seg)); w = int(m['at']); lag = w-raw_at; top = float(curve[w])
    worst, who = 0.0, None
    for i in range(max(1, w-REL_COMPETITOR_SPAN), min(len(curve)-1, w+REL_COMPETITOR_SPAN+1)):
        if abs(i-w) <= 2 or not (curve[i] >= curve[i-1] and curve[i] >= curve[i+1]): continue
        L = i-raw_at
        if abs(L-WITNESS_NOMINAL) <= WITNESS_TOLERANCE and abs(L-lag) <= WITNESS_TOLERANCE: continue
        if curve[i]/top > worst: worst, who = float(curve[i]/top), L
    return dict(m, lag=lag, competitor_ratio=worst, competitor_lag=who, ambiguous=worst >= 1-REL_AMBIGUITY_MARGIN)


def witness_offset(raw, afe, fixture):
    """Near-only alignment witness (v9.8): raw MIC1 -> AFE delay of the fixture onset, nominal 1024 (512 framer
    priming + 512 FD_LOW_COST), gate 1024 +- 20.

    Three estimators must agree (each within +-20 of the chosen fixture offset, all inside 1004-1044):
    (1) the fixture match: the unconstrained match in each channel (|c| >= 0.25, >= 1.1x any peak beyond +-640),
        or else an alternative AFE peak accepted only with its own |c| >= 0.25 / 1.1x checks, the raw peak's
        polarity with the primary AFE peak opposite, and half a fixture pitch period from it (+-25 %);
    (2) the polarity-free envelope delay (|analytic 200-3400 Hz| correlation >= 0.5);
    (3) the relative delay (AFE against raw's own fixture-anchored interval), valid by its own checks and not
        ambiguous under its declared 1 % margin.
    Fixture match and envelope carry no margin: on every real capture their one-pitch-period aliases sit within
    0.4 % of the winner, so they are agreement checks only. Anything unresolved is ambiguous: offset None,
    alignment.near_witness PAUSE-PART. v9.4-acceptance-01's raw -/AFE + tie (1001) resolves to 1024 via the
    alternative; the noisy, inverted and shifted wrong-delay constructions do not."""
    _, template, _, _ = fixture_template(fixture)
    a, b = match(raw, template), match(afe, template)
    out = dict(raw=a, afe=b, afe_unconstrained=b, offset=None, method='primary')
    if not (a.get('valid') and b.get('valid')):
        return dict(out, valid=False, method='invalid', reason='primary match failed its own checks')
    primary = int(b['at'])-int(a['at'])
    env = envelope_delay(raw, afe, int(a['at']), len(template))
    rel = relative_delay(raw, afe, int(a['at']), len(template))
    gate = lambda o: o is not None and abs(o-WITNESS_NOMINAL) <= WITNESS_TOLERANCE
    env_ok = lambda o: env['lag'] is not None and env['correlation'] >= ENV_MIN_CORRELATION and gate(env['lag']) and abs(env['lag']-o) <= WITNESS_TOLERANCE
    rel_ok = lambda o: rel.get('valid') and not rel['ambiguous'] and gate(rel['lag']) and abs(rel['lag']-o) <= WITNESS_TOLERANCE
    checks = dict(primary_offset=primary, envelope=env, relative=rel, primary_envelope_agrees=env_ok(primary),
                  primary_relative_agrees=bool(rel_ok(primary)))
    if rel.get('valid') and rel['ambiguous']:
        return dict(out, valid=False, method='ambiguous', checks=checks, reason='relative estimate ambiguous (1 % margin)')
    if gate(primary) and env_ok(primary) and rel_ok(primary):
        return dict(out, valid=True, offset=primary, checks=checks)
    curve = _signed_curve(afe, template)
    period = _pitch_period(template); checks['pitch_period'] = period
    sign_raw = 1.0 if a['correlation'] >= 0 else -1.0
    sign_afe = 1.0 if b['correlation'] >= 0 else -1.0
    if period is None or sign_afe == sign_raw:
        return dict(out, valid=False, method='ambiguous', checks=checks,
                    reason='primary outside the gate or contradicted by envelope/relative; no opposite-polarity half-period alias')
    lo, hi = max(0, int(b['at'])-period), min(len(curve), int(b['at'])+period+1)
    alt_at = lo+int(np.argmax(curve[lo:hi]*sign_raw))
    alt = _peak_checks(curve, alt_at)
    distance = abs(alt_at-int(b['at']))
    offset = alt_at-int(a['at'])
    checks.update(alternative=alt, distance=distance, half_period=period/2, alternative_offset=offset,
                  strength=alt['valid'],
                  alias=bool((1.0 if alt['correlation'] >= 0 else -1.0) == sign_raw and abs(distance-period/2) <= .25*period/2),
                  envelope_agrees=env_ok(offset), relative_agrees=bool(rel_ok(offset)), in_gate=gate(offset))
    keys = ('strength', 'alias', 'envelope_agrees', 'relative_agrees', 'in_gate')
    if all(checks[k] for k in keys):
        return dict(out, valid=True, afe=dict(alt, polarity='same-as-raw'), offset=offset, method='alternative', checks=checks)
    return dict(out, valid=False, method='ambiguous', checks=checks,
                reason='alternative failed: '+', '.join(k for k in keys if not checks[k]))


def _event_id(e, i):
    return f"{e.get('frame', e.get('event'))}:{e.get('frame_id', i)}:{e['t']:.3f}"


def quiet_windows(anchors):
    start, end = anchors[0]['lower_s'], anchors[-1]['upper_s']
    return dict(pre=(start, start+2.3+1.0), post=(end-2.3, end+1.0))


def room_witness(gate, noise, events, anchors):
    """A failed quiet window explains the accepted events inside it (+1 s STT/VAD lag) and nothing else."""
    windows = quiet_windows(anchors)
    failed = [windows[k] for k in ('pre', 'post') if not noise.get(k, {}).get('passed', False)]
    explains = [eid for eid, e in events if any(lo <= e['t'] <= hi for lo, hi in failed)]
    return F(gate, explains=explains, windows=failed)


def counter(p, path):
    for k in path: p = p.get(k) if isinstance(p, dict) else None
    return p


CLIP_CHECKS = ('aec.capture.adc_clips', 'aec.capture.gain_clips', 'aec.output_clips', 'aec.reference_clips')


def leaf_findings(checks, *, skip=()):
    """Dispatch every failing leaf of a legacy resource composite to its registry gate. Missing or
    malformed data is resources.coverage, never a measured breach."""
    out = []
    if not isinstance(checks, dict): return [F('resources.coverage', reason='missing resource checks')]
    for name, check in checks.items():
        if check.get('passed') or name in skip: continue
        if name.startswith('core_') and not check.get('windows'):
            out.append(F('resources.coverage', reason=name+' no qualifying window')); continue
        if 'value' in check and check.get('value') is None:
            out.append(F('resources.coverage', reason=name+' missing')); continue
        out.append(F(TERM_OWNER.get('resources:'+name, 'unknown.unclassified'), check=name))
    return out


def clip_times(raw, blocks, anchors, limit=64):
    """Host-time intervals of clipped raw MIC1 samples (a spread subset); None when a sample is unmapped."""
    clipped = np.flatnonzero(np.abs(raw) >= 32767)
    if len(clipped) > limit: clipped = clipped[np.linspace(0, len(clipped)-1, limit).astype(int)]
    return [sample_interval(int(x), blocks, anchors) for x in clipped]


def resource_findings(resources, before, after, raw, stimulus_s, blocks=None, anchors=None):
    """Leaf dispatch of the legacy resource composite; clip counters by attempt delta, attributed time-locally
    against the declared stimulus host-time ranges (TTS reply writes, Jabra playback)."""
    checks = (resources or {}).get('checks')
    out = leaf_findings(checks, skip=CLIP_CHECKS+('data_admission_counters_only',))
    for path, kind in DELTA_COUNTERS.items():
        a, b = counter(before, path), counter(after, path)
        if type(a) is not int or type(b) is not int or b < a:
            out.append(F('resources.coverage', reason='.'.join(path)+' missing/reset')); continue
        if b == a: continue
        if kind != 'mic':
            out.append(F(kind, delta=b-a)); continue
        times = clip_times(raw, blocks, anchors) if blocks is not None else []
        inside = [t for t in times if t is None or any(t[1] >= lo and t[0] <= hi for lo, hi in stimulus_s)]
        if times and not inside: out.append(F('clip.mic_room', delta=b-a, clipped_samples=len(times)))
        else: out.append(F('clip.mic_unattributed', delta=b-a, clipped_samples=len(times), inside=len(inside)))
    return out


def delivery_findings(before, after):
    d = delivery_counters(before, after)
    out = []
    for key, value in d['delta'].items():
        if value is None: out.append(F('admission.counter_schema' if key.startswith('drop_reasons') else 'resources.coverage', counter=key))
        elif value > 0: out.append(F('delivery.send_errors' if key == 'send_errors' else 'delivery.firmware_drops', counter=key, delta=value))
    if not before.get('drop_reasons'): out.append(F('admission.counter_schema', counter='drop_reasons'))
    return out, d


def admission_observation(p):
    da = p.get('data_admission', {}) if isinstance(p, dict) else {}
    drops = p.get('drop_reasons') if isinstance(p, dict) else None
    return dict(pc_id=p.get('pc_id'), generation=p.get('generation'), received=da.get('received'),
                dispatched=da.get('dispatched'), deferred=da.get('deferred'),
                admission_overflow=drops.get('admission_overflow') if isinstance(drops, dict) else None,
                drops=drops)


def admission_findings(folder, before, after):
    out = []
    gate = rules.admission_interval(admission_observation(before), admission_observation(after))
    if gate: out.append(F(gate, before=admission_observation(before), after=admission_observation(after)))
    for label in ('before', 'after'):
        drain = read(folder/f'{label}-drain.json') if (folder/f'{label}-drain.json').exists() else None
        if drain and drain['status'] == 'drain-timeout' and not gate: out.append(F('admission.drain_timeout', boundary=label))
        if drain and drain['status'] == 'schema': out.append(F('admission.counter_schema', boundary=label))
    return out


def _source(fixture):
    return fixture_template(fixture)


def near_onset(c, raw, afe, blocks, anchors, mask):
    """Block-analysis near-onset witness (accept_analysis.main), per attempt."""
    source, template, first, _ = fixture_template(c['fixture'])
    j = c['jabra']; start = j['request_monotonic_s']; end = start+j['elapsed_seconds']
    eligible = [int(b[0]) for b in blocks if (lambda t: t and t[1] >= start-.2 and t[0] <= end+.3)(sample_interval(int(b[0]), blocks, anchors))]
    near = match(afe[min(eligible)+1024:min(len(afe), max(eligible)+320+1024)], template) if eligible else dict(valid=False)
    row = {}
    if near.get('at') is not None:
        onset = min(eligible)+near['at']
        near.update(raw_onset_sample=onset, host_interval=sample_interval(onset, blocks, anchors), uncertainty_samples=320)
        left = sample_interval(max(0, onset-320), blocks, anchors); right = sample_interval(min(len(raw)-1, onset+320), blocks, anchors)
        near['host_interval_with_onset_uncertainty'] = [left[0], right[1]] if left and right else None
        ff = max(0, (onset-320)//320); lf = min(len(mask), (onset+320)//320+1); window = mask[ff:lf]
        row['mic3_state'] = 'active' if len(window) and window.all() else ('silent' if len(window) and not window.any() else 'uncertain')
        row['mic3_active_at_near_onset'] = bool(near['valid'] and row['mic3_state'] == 'active')
    return near, row, start, end


def attribute_accepted(own, kind, expected_text, lower, near_end):
    """Sol re-review 3. Accepted turns/interruptions before the near-onset lower bound are unsolicited. The
    first accepted response at or after onset (and its companions within 0.5 s) is associated with the near
    utterance however late: lateness is a latency miss, not a false event. A further accepted turn after
    near end + 2 s is unsolicited unless a transcript within 3 s matches the near text."""
    accepted = [(i, e) for i, e in enumerate(own) if e.get('frame') in ACCEPTED]
    unsolicited, associated = [], None
    for i, e in accepted:
        if e['t'] < lower: unsolicited.append((_event_id(e, i), e)); continue
        if associated is None or e['t']-associated <= .5:
            associated = e['t'] if associated is None else associated; continue
        if e['t'] > near_end+2.0:
            words = [x.get('text') for x in own if x.get('frame') == 'TranscriptionFrame' and abs(x['t']-e['t']) <= 3]
            if not any((re.search(r'\bstop\b', w or '', re.I) if kind == 'stop' else wer(expected_text, w or '') <= .5) for w in words):
                unsolicited.append((_event_id(e, i), e))
    return unsolicited


def score_barge(view, folder, c, raw, afe, ref, blocks, anchors, own):
    root = view.root; serial = view.serial(); qref = view.qref
    mask = activity(ref, qref, [(0, len(ref)-1024)])
    text = ' '.join(dict.fromkeys(e.get('text') or '' for e in own if e.get('frame') == 'TranscriptionFrame'))
    row = dict(kind=c['kind'], transcript=text, stimulus_skipped=bool(c.get('stimulus_skipped')))
    findings = []
    if c.get('stimulus_skipped') or not c.get('jabra'):
        return dict(row, opportunity='not-delivered-no-active-tts'), findings, None
    near, extra, start, end = near_onset(c, raw, afe, blocks, anchors, mask)
    row.update(extra, near_onset=near)
    ref_rows = view.recorded
    if near.get('at') is not None:
        onset = near['raw_onset_sample']
        row['in_window'] = gate_at_near(serial, blocks, onset)
        n = min(3200, onset); pcm = root/'evaluation'/(c['run_id']+'.pcm')
        tts = np.fromfile(pcm, dtype='<i2').astype(float) if pcm.exists() else np.array([])
        far = match(tts, ref[onset-n:onset]) if n else dict(valid=False)
        if far.get('at') is not None:
            position = far['at']+n; future = tts[position:]; pp = frame_power(tts); fp = frame_power(future)
            count = int(np.sum(fp > max(1, float(pp.max())*.001))) if len(pp) and len(fp) else 0
            far.update(source_onset_sample=position, future_active_ms=count*20, verified=bool(far['valid'] and count >= 5))
        row['future_speech'] = far
    row['recognized'] = bool(re.search(r'\bstop\b', text, re.I)) if c['kind'] == 'stop' else wer(c['expected_text'], text) <= .2
    if c['kind'] == 'sentence': row['wer'] = wer(c['expected_text'], text)
    interrupts = [e for e in own if e.get('frame') == 'InterruptionFrame' and e['t'] >= start]
    row['interruption_after_stimulus'] = bool(interrupts)
    row['near_onset_to_interruption'] = interval_latency(near.get('host_interval_with_onset_uncertainty') if near.get('valid') else None,
                                                         [interrupts[0]['t'], interrupts[0]['t']] if interrupts else None)
    far = row.get('future_speech', {})
    if view.lull_policy and near.get('valid') and row.get('mic3_state') == 'uncertain':
        from echoear_aec_lull import case_witness
        row['lull_witness'] = case_witness(root, c, row, ref, qref, blocks, anchors, ref_rows, serial)
    row['opportunity'] = classify_opportunity(clean=True, delivered=True, near_valid=near.get('valid', False),
        gate=row.get('in_window'), mic3=row.get('mic3_state'), future=far.get('verified', False) if far.get('valid') else None,
        lull=row.get('lull_witness'), lull_policy=view.lull_policy, integrity_passed=True)
    if row['opportunity'] == 'unscorable-lull-cancelled' and far.get('valid') and far.get('verified'):
        row['cancelled_lull_established'] = True       # Sol rule (12.8): independently established ongoing reply
    # False-interruption attribution (Sol re-review 3): before the near-onset lower bound is unsolicited;
    # the first accepted turn at/after onset is associated; a further one beyond near end + 2 s is
    # unsolicited unless its transcript matches the near text.
    bracket = near.get('host_interval_with_onset_uncertainty') if near.get('valid') else None
    lower = bracket[0] if bracket else start
    near_end = (bracket[1] if bracket else start)+(fixture_template(c['fixture'])[3]/16000)
    unsolicited = attribute_accepted(own, c['kind'], c.get('expected_text'), lower, near_end)
    if unsolicited:
        findings.append(F('barge.false_interruption', events=[dict(id=i, t=e['t'], frame=e.get('frame')) for i, e in unsolicited]))
    return row, findings, unsolicited


UNSUPPORTED_PLAYBACK = ('Short/missing context PCM', 'Missing prefix/onset coverage', 'Unscorable source/MIC3 match',
                        'No speech-level source')


def load_attempt(view, folder, *, case=None, partial=None):
    """One attempt's evidence. partial: a descriptor for an attempt the inherited capture ended early
    (capture deadline): the exported capture, anchors and timer exist, result.json does not."""
    folder = Path(folder)
    if partial is not None:
        c = partial
    else:
        cases = read(folder/'result.json')['cases']
        if case is None and len(cases) != 1: raise ValueError('v9 attempts hold exactly one case')
        c = cases[0] if case is None else case
    view.poll(c['start_ns']-int(15e9))      # keeps the sends before the 'before' boundary (drain anchors)
    raw, afe, ref, blocks = load(folder/'capture.npz')
    before = read(folder/'before.json')
    after = read(folder/'after.json') if (folder/'after.json').exists() else None
    scored_after = read(folder/'capture-end.json') if (folder/'capture-end.json').exists() else after
    own = c.get('events', [])
    return dict(folder=folder, c=c, raw=raw, afe=afe, ref=ref, blocks=blocks, anchors=read(folder/'anchors.json'),
                before=before, after=after, scored_after=scored_after, partial=partial is not None,
                noise=read(folder/'noise-gate.json') if (folder/'noise-gate.json').exists() else {}, own=own,
                accepted=[(_event_id(e, i), e) for i, e in enumerate(own) if e.get('frame') in ACCEPTED])


def case_writes(view, c):
    """Non-silent transport writes per TTS context of the case, in context order."""
    out = []
    for context in [e for e in c.get('events', []) if e.get('event') == 'tts-context-pcm']:
        writes = [e for e in view.recorded if e.get('kind') == 'tts-transport-write' and e.get('context_id') == context['context_id']
                  and e.get('pc_id') == context['pc_id'] and e.get('non_silent')]
        out.append((context, writes))
    return out


def stimulus_ranges(view, c):
    """Declared stimulus host-time ranges: every TTS context of the case and the Jabra playback."""
    ranges = [(min(e['monotonic_ns'] for e in w)/1e9-.2, max(e['monotonic_ns'] for e in w)/1e9+1.0)
              for _, w in case_writes(view, c) if w]
    j = c.get('jabra')
    if j: ranges.append((j['request_monotonic_s']-.2, j['request_monotonic_s']+j['elapsed_seconds']+.3))
    return ranges


def barge_window(view, plan, c):
    """Stimulus/flush interval of a barge attempt (host seconds): from the Jabra request to the next reply's
    first write + its 1 s render deadline (or the first interruption + the declared tail, or the stimulus end)."""
    t = plan['v9']['transport_window'] if 'v9' in plan else dict(pre_s=.2, flush_tail_s=2.0)
    j = c['jabra']; lo = j['request_monotonic_s']-t['pre_s']; hi = j['request_monotonic_s']+j['elapsed_seconds']
    interrupts = [e['t'] for e in c.get('events', []) if e.get('frame') == 'InterruptionFrame' and e['t'] >= lo]
    writes = case_writes(view, c)
    nxt = [min(e['monotonic_ns'] for e in w)/1e9 for _, w in writes[1:] if w]
    if nxt: hi = max(hi, min(nxt)+1.0)
    elif interrupts: hi = max(hi, interrupts[0]+t['flush_tail_s'])
    return [lo, hi]


def transport_window_findings(view, plan, c, window):
    """Interval-specific transport validity (spec 5): host downlink send gaps and SCTP T3 without clean same-peer
    recovery are witnessed with host timestamps; only overlap with the stimulus/flush window invalidates."""
    out, record = [], dict(window_s=window)
    t = plan['v9']['transport_window'] if 'v9' in plan else dict(downlink_send_gap_s=.12)
    pcs = {e.get('pc_id') for e in c.get('events', []) if e.get('pc_id')}
    import bisect
    if getattr(view, '_dl_len', None) != len(view.downlink):     # sorted index, rebuilt only when the tail grew
        view._dl = sorted(view.downlink, key=lambda e: e.get('send_ms', 0)); view._dl_keys = [e.get('send_ms', 0) for e in view._dl]
        view._dl_len = len(view.downlink)
    lo_i, hi_i = bisect.bisect_left(view._dl_keys, (window[0]-1)*1000), bisect.bisect_right(view._dl_keys, (window[1]+1)*1000)
    sends = [e['send_ms']/1000 for e in view._dl[lo_i:hi_i] if e.get('pc_id') in pcs]
    gaps = [(a, b) for a, b in zip(sends, sends[1:]) if b-a >= t['downlink_send_gap_s'] and b >= window[0] and a <= window[1]]
    record.update(downlink_packets=len(sends), downlink_gaps_in_window=[[a, b] for a, b in gaps])
    if pcs and not sends: record['downlink_coverage'] = 'no host send records in window'
    if gaps: out.append(F('transport.downlink_barge', witness='host downlink send gap', gaps=[[a, b] for a, b in gaps]))
    rows = [e for e in view.sctp if e.get('pc_id') in pcs]
    spans = []
    for timer in [e for e in rows if e.get('event') == 't3-expired']:
        rec = next((e['monotonic_ns'] for e in rows if e['monotonic_ns'] > timer['monotonic_ns'] and
                    e.get('event') == 'receive-sack-complete' and e.get('flight_size') == e.get('sent_queue') ==
                    e.get('outbound_queue') == 0), None)
        lo, hi = timer['monotonic_ns']/1e9, (rec/1e9 if rec is not None else float('inf'))
        if hi >= window[0] and lo <= window[1]: spans.append([lo, None if rec is None else hi])
    record['sctp_unrecovered_spans_in_window'] = spans
    if spans: out.append(F('transport.sctp_recovery', spans=spans))
    return out, record


def reply_reconciliation(view, c, before, after):
    """Frozen per_reply_device_received_and_dispatched_min (Sol 3): the device receive and dispatch deltas of the
    attempt epoch must each cover the recorder's downstream RTVIProcessor messages in the same window; equal
    counters alone are not delivery. A shortage is unknown (PAUSE-PART): host evidence never resolves delivery."""
    from echoear_aec_checks import outgoing_rtvi_count
    b, a = admission_observation(before), admission_observation(after)
    if any(type(x.get(k)) is not int for x in (b, a) for k in ('received', 'dispatched')) or \
            (b['pc_id'], b['generation']) != (a['pc_id'], a['generation']):
        return dict(checked=False, reason='counters/identity unavailable (admission.counter_schema covers it)'), []
    lo, hi = c['start_ns'], c['end_ns']
    sent = outgoing_rtvi_count(view.recorded, lo, hi)
    rd, dd = a['received']-b['received'], a['dispatched']-b['dispatched']
    rec = dict(checked=True, recorder_messages=sent, received_delta=rd, dispatched_delta=dd, window_ns=[lo, hi])
    if rd >= sent and dd >= sent: return dict(rec, passed=True), []
    rec['passed'] = False
    # v9.3: host evidence never resolves transport delivery, so a recorder-vs-device shortfall is always unknown
    # (PAUSE-PART). Control loss is CANDIDATE-FAIL only from device-side positive witnesses (overflow / drop /
    # rejection deltas, duplicate or misordered application receipt), all classified elsewhere. (v9.10: there is
    # no device-side witness of permanent non-dispatch; an unmatched count stays drain_timeout PAUSE-PART.)
    return rec, [F('unknown.unclassified', reason='device deltas short of recorder RTVI count (delivery unresolved)',
                   **{k: rec[k] for k in ('recorder_messages', 'received_delta', 'dispatched_delta')})]


def integrity(view, plan, entry, a):
    """Live device/admission/observer/harness classification, independent of fallible acoustic analysis."""
    c, folder, findings, record = a['c'], a['folder'], [], {}
    record['floors'] = floors(a['raw'], a['afe'])
    if view.reboots(): findings.append(F('device.reboot_crash', lines=view.reboots()[:5]))
    timer = c.get('stimulus_timer') or {}
    if timer.get('timer_lateness_ms') is not None and abs(timer['timer_lateness_ms']) > plan['timing']['timer_lateness_max_s']*1000:
        findings.append(F('timing.stimulus_lateness', lateness_ms=timer['timer_lateness_ms']))
    if a['partial']:
        return findings, record       # the retry-clean witness (recover_harness) covers the device interval
    noise, own = a['noise'], a['own']
    if not c.get('noise_gate_passed'):
        findings.append(room_witness('noise.cell_quiet_windows', noise, a['accepted'], a['anchors']))
    if not c.get('quiet_windows_valid', True):
        findings.append(F('quiet.board_silent' if not noise.get('board_silent', True) else 'timing.single_gap'))
    before, after, scored_after = a['before'], a['after'], a['scored_after']
    findings += admission_findings(folder, before, after)
    rec, rfind = reply_reconciliation(view, dict(c, _folder=str(folder)), before, after)
    record['reply_admission'] = rec; findings += rfind
    dfind, record['delivery'] = delivery_findings(before, scored_after)
    findings += dfind
    record['transport'] = transport(before, scored_after, plan['gates']['transport']['rule'])
    ledger = check_render_ledger([read(f) for f in sorted(folder.glob('*-boundary-*.json'))])
    if not ledger['passed']: findings.append(F('hardware.render_ledger'))
    rh = (after.get('aec', {}).get('reference_health', {}).get('low_windows'), before.get('aec', {}).get('reference_health', {}).get('low_windows'))
    if None in rh: findings.append(F('resources.coverage', reason='reference_health missing'))
    elif rh[0] != rh[1]: findings.append(F('reference.health', low_windows_delta=rh[0]-rh[1]))
    from echoear_aec_campaign import observer_ok
    if not observer_ok(own): findings.append(F('observer.health'))
    duration = read(folder/'capture-duration.json').get('elapsed_s', 100) if (folder/'capture-duration.json').exists() else 100
    if duration > 28: findings.append(F('capture.cap', elapsed_s=duration))
    # wifi-stats is evidence only (registry rf.diagnostics, MONITOR): a boundary without the driver payload is
    # recorded as missing diagnostics and never gates (smoke finding S4).
    wifi = {f.name.split('-wifi-stats')[0]: read(f).get('payload_present') for f in sorted(folder.glob('*-wifi-stats.json'))}
    record['wifi_stats_payload'] = wifi
    if any(v is False for v in wifi.values()):
        findings.append(F('rf.diagnostics', missing_payload=[k for k, v in wifi.items() if v is False]))
    rf = read(folder/'rf.json') if (folder/'rf.json').exists() else None
    record['rf'] = dict(passed=(rf or {}).get('passed'), capture_window='unmeasured attribution: no per-second counters '
                        'are acquired, so rf.capture_window is not demonstrated either way (never a pass)',
                        effect='none: a whole-capture total alone cannot place an excess in the stimulus window'
                        if rf and not rf.get('passed') else None)
    if rf is None: findings.append(F('resources.coverage', reason='rf.json missing'))
    if entry['kind'].startswith('barge-') and c.get('jabra'):
        window = barge_window(view, plan, c)
        tf, record['transport_window'] = transport_window_findings(view, plan, c, window)
        findings += tf
        if not record['transport']['passed']:
            # Astra v9.1 blocker 5: the registry's device-playout gate (loss <= 2 %, ended run >= 6, post-PLC zeros,
            # starvation, histogram) needs interval evidence the v9 acquisition does not record (PERF exists only at
            # before / capture-end / after; no per-packet device trace). Its overlap with the stimulus/flush window
            # is therefore UNRESOLVED, which is unknown cause -> PAUSE-PART, never a pass and never a silent
            # substitution. Pending a policy decision on time-local evidence (see report).
            record['transport_unlocalized'] = 'attempt-interval device transport fault; window attribution unresolved'
            findings.append(F('unknown.unclassified', reason='barge device transport fault: interval attribution '
                              'unresolved (no time-local playout evidence acquired)', transport=record['transport']))
    elif entry['kind'] == 'echo' and not record['transport']['passed']:
        findings.append(F('transport.downlink_echo'))
    findings += resource_findings(c.get('resources'), before, scored_after, a['raw'], stimulus_ranges(view, c),
                                  a['blocks'], a['anchors'])
    return findings, record


def playbacks_of(view, a):
    out = []
    for context, writes in case_writes(view, a['c']):
        source = np.fromfile(view.root/'evaluation'/context['path'], dtype='<i2').astype(float)
        play = context_playback(source, a['ref'], a['blocks'], a['anchors'], min(e['monotonic_ns'] for e in writes)/1e9,
                                view.highpass_hz) if writes else dict(passed=False, reason='No non-silent write')
        play['source_write_witness'] = source_write_witness(play, source, view.recorded, context['pc_id'], context['context_id'])
        play.update(context_id=context['context_id'], write_s=min(e['monotonic_ns'] for e in writes)/1e9 if writes else None,
                    source=source)
        out.append(play)
    return out


def acoustic(view, plan, entry, a):
    """Acoustic endpoints of one exposure. Returns (scores, findings, record)."""
    c, kind, own, findings, record = a['c'], entry['kind'], a['own'], [], {}
    raw, afe, ref, blocks, anchors = a['raw'], a['afe'], a['ref'], a['blocks'], a['anchors']
    accepted = a['accepted']
    if a['partial']:
        return partial_acoustic(view, plan, entry, a)
    scores = {}
    if kind == 'echo':
        playbacks = playbacks_of(view, a)
        for play in playbacks:
            play['echo'] = echo_measure(raw, afe, ref, view.qref, play.pop('source'), play, view.serial(), blocks)
        record['playbacks'] = [dict(context_id=x.get('context_id'), passed=x.get('passed'), reason=x.get('reason'),
                                    echo={k: x['echo'].get(k) for k in ('passed', 'reason', 'whole', 'first_500ms', 'post_guard',
                                          'guard_passed', 'guard_at_speech', 'preceding_silence_s', 'gate_unknown_active_frames',
                                          'first_500ms_gated', 'source_first_500ms_dbfs', 'muted_fraction')}) for x in playbacks]
        record['mapping_intact'] = bool(playbacks) and all(x.get('passed') for x in playbacks)
        unscorable = not playbacks or any(x['echo'].get('reason') or x['echo'].get('gate_unknown_active_frames') for x in playbacks)
        if unscorable:
            findings.append(F('echo.unscorable', reasons=[x['echo'].get('reason') for x in playbacks] or ['no playback context']))
            result = 'miss'
        else:
            result = 'pass' if all(x['echo']['passed'] for x in playbacks) else 'miss'
            for x in playbacks:
                e = x['echo']
                if e['whole']['integrated_reduction_db'] < 15 or e['post_guard'].get('residual_dbfs', -999) > -40:
                    findings.append(F('echo.hard_floor', whole_db=e['whole']['integrated_reduction_db'],
                                      post_guard_dbfs=e['post_guard'].get('residual_dbfs')))
        if accepted:
            findings.append(F('echo.false_turns', events=[dict(id=i, t=e['t'], frame=e.get('frame')) for i, e in accepted]))
        stops = [e['t'] for e in own if e.get('frame') == 'BotStoppedSpeakingFrame']
        starts = [e['t'] for e in own if e.get('frame') == 'BotStartedSpeakingFrame']
        sealed = [e['t'] for e in own if e.get('event') == 'case-sealed']
        covered = bool(starts and stops and max(stops) > max(starts) and sealed and max(sealed) >= max(stops)+2.0)
        record['event_coverage'] = dict(covered=covered, starts=len(starts), stops=len(stops), sealed=bool(sealed))
        if not covered: findings.append(F('echo.event_coverage'))
        record['turn_events'] = turn_events(own)
        scores = dict(acoustic=dict(result=result), events=dict(result='miss' if accepted else 'pass'))
    elif kind.startswith('barge-'):
        row, bf, unsolicited = score_barge(view, a['folder'], c, raw, afe, ref, blocks, anchors, own)
        findings += bf
        record['barge'] = {k: v for k, v in row.items() if k not in ('lull_witness',)}
        op = row['opportunity']
        if op == 'not-delivered-no-active-tts': findings.append(F('barge.undelivered'))
        elif op == 'in-window': findings.append(F('barge.in_window'))
        elif op == 'unscorable-lull-cancelled' and not row.get('cancelled_lull_established'): findings.append(F('barge.cancelled_lull'))
        elif op.startswith('unscorable-') and op != 'unscorable-lull-cancelled': findings.append(F('barge.unscorable', opportunity=op))
        eligible = op in ('eligible', 'ongoing-reply-lull') or row.get('cancelled_lull_established')
        result, established = ('ineligible' if op == 'no-opportunity' else 'miss'), False
        if eligible:
            playbacks = playbacks_of(view, a)
            for x in playbacks: x.pop('source')
            record['mapping_intact'] = bool(playbacks) and bool(playbacks[0].get('passed'))
            flush = flush_measure(c, row, ref, view.qref, blocks, anchors, view.serial(),
                                  [x for x in playbacks if x.get('write_s') is not None] or playbacks[:1], plan['gates']['flush']) \
                if playbacks else dict(passed=False, reason='No old-reply context')
            lo, hi = c['start_ns']-int(5e9), c['end_ns']+int(5e9)   # the attempt window only (O(1) in hold length)
            window = lambda rows: [e for e in rows if lo <= e.get('monotonic_ns', 0) <= hi]
            copies = [x for x in interruption_copies(window(view.recorded), window(view.sctp))
                      if c['start_ns'] <= x['first_send_ns'] <= c['end_ns']]
            flush['copies_observed'] = len(copies) == 2
            flush['passed'] = bool(flush['passed'] and flush['copies_observed'])
            if flush.get('stale_frames_after_mute'): findings.append(F('flush.stale_audio', frames=flush['stale_frames_after_mute']))
            latency = (row.get('near_onset_to_interruption') or {}).get('upper_s')
            limit = LATENCY_LIMIT_S[c['kind']]
            credited = bool(row['recognized'] and row['interruption_after_stimulus'] and row.get('future_speech', {}).get('valid')
                            and row.get('future_speech', {}).get('verified'))
            # flush.next_reply: after an accepted interruption the next non-silent TTS reaches MIC3 within 1 s.
            # Absent or late next audio is a scored miss; an unsupported measurement is invalid, never a pass.
            nxt = dict(required=bool(row['interruption_after_stimulus']))
            if nxt['required']:
                later = playbacks[1:]
                if not later: nxt.update(result='miss', reason='no next TTS context')
                elif later[0].get('passed'): nxt.update(result='pass', latency=later[0].get('latency'))
                elif later[0].get('reason') in UNSUPPORTED_PLAYBACK:
                    nxt.update(result='unsupported', reason=later[0]['reason'])
                    findings.append(F('barge.unscorable', opportunity='next-reply-'+later[0]['reason']))
                else: nxt.update(result='miss', reason=later[0].get('reason') or 'next reply later than 1 s', latency=later[0].get('latency'))
            record['next_reply'] = nxt
            success = bool(credited and latency is not None and latency <= limit and flush['passed'] and
                           (not nxt['required'] or nxt.get('result') == 'pass'))
            result = 'pass' if success else 'miss'
            established = bool(not success and (not row['recognized'] or (latency is not None and latency > limit)))
            record['flush'] = {k: flush.get(k) for k in ('passed', 'reason', 'callback_to_last_mic3_s', 'server_to_last_mic3',
                                                          'stale_frames_after_mute', 'copies_observed', 'callback_witness')}
            record.update(latency_upper_s=latency, latency_limit_s=limit, credited=credited, success=success)
        scores = dict(acoustic=dict(result=result, established=established),
                      events=dict(result='miss' if unsolicited else 'pass'))
    elif kind == 'near-witness':
        record['witness'] = witness_offset(raw, afe, c['fixture'])
        record['near_preservation'] = near_preservation(c, raw, afe, ref, view.qref)
        early = [(i, e) for i, e in accepted if c.get('jabra') and e['t'] < c['jabra']['request_monotonic_s']]
        if early: findings.append(F('barge.false_interruption', events=[dict(id=i, t=e['t']) for i, e in early]))
    return scores, findings, record


def partial_acoustic(view, plan, entry, a):
    """Capture-deadline attempt (Astra v9.1 blocker 3): the opportunity is established exactly as for a
    complete barge (score_barge: mapped near onset, guard state, MIC3 activity or the lull witness, future
    speech) before any endpoint is kept. Only then is a latency miss established: the conservative lower
    latency bound of the first accepted interruption exceeds the limit, or none arrived while the capture
    covered onset + limit. Witnessed silence is an established ineligible opportunity; any other opportunity
    class keeps its own validity finding and no score. Positive false events are kept separately."""
    c, kind = a['c'], entry['kind']
    if not kind.startswith('barge-') or not c.get('jabra') or not c.get('fixture'):
        return {}, [], dict(partial=True, established=False)
    row, findings, unsolicited = score_barge(view, a['folder'], c, a['raw'], a['afe'], a['ref'], a['blocks'], a['anchors'], a['own'])
    op = row['opportunity']; limit = LATENCY_LIMIT_S[c['kind']]
    record = dict(partial=True, opportunity=op, near_onset=row.get('near_onset'), latency_limit_s=limit, established=False)
    scores = {}
    if unsolicited: scores['events'] = dict(result='miss')
    if op == 'in-window': findings.append(F('barge.in_window'))
    elif op == 'not-delivered-no-active-tts': findings.append(F('barge.undelivered'))
    elif op == 'unscorable-lull-cancelled' and not row.get('cancelled_lull_established'): findings.append(F('barge.cancelled_lull'))
    elif op.startswith('unscorable-') and op != 'unscorable-lull-cancelled': findings.append(F('barge.unscorable', opportunity=op))
    if op == 'no-opportunity':
        scores['acoustic'] = dict(result='ineligible', established=True); record['established'] = True
    elif op in ('eligible', 'ongoing-reply-lull') or row.get('cancelled_lull_established'):
        bracket = (row.get('near_onset') or {}).get('host_interval_with_onset_uncertainty')
        latency = row.get('near_onset_to_interruption') or {}
        covered_until = a['anchors'][-1]['lower_s']
        late = latency.get('lower_s') is not None and latency['lower_s'] > limit
        absent = not row.get('interruption_after_stimulus') and bracket and covered_until > bracket[1]+limit
        record['latency'] = latency
        if late or absent:
            scores['acoustic'] = dict(result='miss', established=True); record['established'] = True
    return scores, findings, record


def combine(entry, integrity_findings, scores, findings, record):
    """Clip findings are ordinary acoustic misses on a valid measurement (clip.afe_output; clip.reference with
    intact reference mapping, else unknown.unclassified). An invalid measurement stays unsupported."""
    out, scores = [], {k: dict(v) for k, v in scores.items()}
    for f in integrity_findings:
        if f['gate'] in ('clip.afe_output', 'clip.reference'):
            scored = entry['kind'] == 'echo' or entry['kind'].startswith('barge-')
            mapped = f['gate'] == 'clip.afe_output' or record.get('mapping_intact')
            if not scored or not mapped:
                out.append(F('unknown.unclassified', reason=f['gate']+(' outside a scored cell' if not scored else ' broken mapping')))
                continue
            if scores.get('acoustic', {}).get('result') in ('pass', 'ineligible'):
                scores['acoustic'] = dict(scores['acoustic'], result='miss', clip_miss=f['gate'])
            if 'acoustic' in scores: scores['acoustic']['established'] = True
        out.append(f)
    return scores, out+findings


def score_attempt(view, folder, plan, entry, *, case=None, partial=None):
    """Full per-attempt scoring (integrity + acoustic + combine). Returns dict(scores, findings, record)."""
    a = load_attempt(view, folder, case=case, partial=partial)
    with fast_timing():
        ifind, irec = integrity(view, plan, entry, a)
        scores, afind, arec = acoustic(view, plan, entry, a)
    scores, findings = combine(entry, ifind, scores, afind, dict(irec, **arec))
    record = dict(attempt=Path(folder).name, kind=entry['kind'], volume_db=a['c'].get('volume_db'), **irec, **arec)
    record['source_sha256'] = {n: sha(Path(folder)/n) for n in ('capture.npz', 'anchors.json', 'result.json') if (Path(folder)/n).exists()}
    return jsonable(dict(scores=scores, findings=findings, record=record))


def retained_kind(c):
    """Map a retained v8.x case onto a v9 scorer kind (dry run only; no credit)."""
    if c['kind'] == 'echo': return 'echo'
    if c['kind'] == 'near': return 'near-witness'
    if c['kind'] in ('stop', 'sentence') and c.get('fixture') and c.get('stop_offset_s') is not None:
        voice = c.get('voice_class', 'firm')
        return {'stop': 'barge-firm-stop', 'sentence': f'barge-{voice}-sentence'}[c['kind']]
    return 'report-only'


def dry_run(hold, *, limit=None, timer_lateness_max_s=.100):
    """Exploratory v9 scoring of a retained hold, in acquisition order (no credit). Every case is scored
    through score_attempt with the hold's own declaration; an exception is recorded as pending, as
    in the live runner (section 9)."""
    import copy, time
    hold = Path(hold)
    plan = copy.deepcopy(read(hold/'declaration.json'))
    try:
        view = HoldView(hold, plan, retain=True)
    except ValueError:
        # Diagnostic holds (aec-arms) declare no acceptance gates: score with the frozen v8.7 contract.
        from echoear_aec_v9_plan import SOURCE
        plan = read(SOURCE); view = HoldView(hold, plan, retain=True)
    plan.setdefault('timing', {})['timer_lateness_max_s'] = timer_lateness_max_s
    result = read(hold/'result.json')
    order = sorted(result['cases'], key=lambda c: c['start_ns'])[:limit]
    rows = []
    for c in order:
        entry = dict(kind=retained_kind(c), logical_id=c['id'])
        started = time.perf_counter()
        try:
            value = score_attempt(view, hold/c.get('capture_dir', c['run_id']), plan, entry, case=c)
            json.dumps(value, allow_nan=False)
            classification = rules.classify_attempt(value['findings'], value['scores'], block_closed=True)
            rows.append(dict(attempt=c['run_id'], kind=entry['kind'], pending=False, scores=value['scores'],
                             findings=[f['gate'] for f in value['findings']], action=classification['action'],
                             credited=classification['credited'], seconds=time.perf_counter()-started,
                             record=value['record']))
        except Exception as exc:
            rows.append(dict(attempt=c['run_id'], kind=entry['kind'], pending=True, error=f'{type(exc).__name__}: {exc}',
                             seconds=time.perf_counter()-started))
    return dict(hold=str(hold), exploratory=True, credit=False, attempts=rows,
                errors=[r for r in rows if r['pending']], analyzer_sha256=sha(Path(__file__)))
