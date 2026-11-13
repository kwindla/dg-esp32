"""HISTORICAL/UNUSED by the demo plan; see docs/echoear-aec-bench.md.

First scored AEC: calibrated activity, conservative timing, strict integrity.

Pure host analysis. Old echoear_aec_checks v1 results are never reinterpreted.
All sample coordinates are on the common 16 kHz capture clock. ERLE delay is
supplied by an independent DAC-silent near-end capture, never optimized here.
"""
import math
import re
import numpy as np
from scipy import signal
from playback_quality import TRANSPORT_COUNTERS, microphone_clip_gate
from step9a_merge_checks import memory_gate
from uplink_continuity import link_tail

RATE = 16000
FRAME = 320
METHOD = 'independent-quiet-v2'


def rms(x):
    x = np.asarray(x, dtype=float)
    if not x.size or not np.isfinite(x).all():
        raise ValueError('Finite nonempty samples required')
    return float(np.sqrt(np.mean(x*x)))


def dbfs(x):
    return 20*math.log10(max(rms(x), 1e-12)/32768)


def saturation(pcm):
    x = np.asarray(pcm)
    if x.ndim != 2 or x.shape[1] != 5 or not len(x) or not np.isfinite(x).all():
        return dict(passed=False, reason='Complete schema2 raw slots required')
    counts = np.count_nonzero((x >= 32767) | (x <= -32768), axis=0).tolist()
    return dict(passed=not any(counts), counts=counts,
                order=['mic1', 'mic2', 'mic3', 'mic4', 'processed'],
                basis='Entire original capture, including alignment prefixes and tails')


def reference_threshold(quiet):
    q = np.asarray(quiet, dtype=float)
    if q.ndim != 1 or len(q) < 8000 or not np.isfinite(q).all():
        raise ValueError('Independent >=0.5s quiet raw MIC3 required')
    return dict(method=METHOD, quiet_rms_lsb=rms(q), quiet_dbfs=dbfs(q),
                threshold_rms_lsb=max(2., rms(q)*10**.5), minimum_rise_db=10,
                minimum_rms_lsb=2., samples=len(q))


def reply_erle(pcm, *, quiet_reference, delay_samples, reply_start, reply_end):
    result = dict(passed=False, available=False, method=METHOD,
                  saturation=saturation(pcm), target_db=20)
    x = np.asarray(pcm, dtype=float)
    if (x.ndim != 2 or x.shape[1] != 5 or not len(x) or not np.isfinite(x).all()
            or type(delay_samples) is not int or not 0 <= delay_samples <= 4096
            or type(reply_start) is not int or type(reply_end) is not int
            or not 0 <= reply_start < reply_end <= len(x)-delay_samples):
        return dict(result, reason='Complete capture, independently measured delay and reply bounds required')
    try:
        threshold = reference_threshold(quiet_reference)
    except ValueError as exc:
        return dict(result, reason=str(exc))
    starts = np.arange(reply_start, reply_end-FRAME+1, FRAME)
    rows = []
    for at in starts:
        mic = x[at:at+FRAME, 0]
        ref = x[at:at+FRAME, 2]
        out = x[at+delay_samples:at+delay_samples+FRAME, 4]
        active = rms(ref) >= threshold['threshold_rms_lsb']
        rows.append(dict(sample=int(at), reference_rms_lsb=rms(ref), active=active,
                         erle_db=10*math.log10(max(rms(mic)**2, 1)/max(rms(out)**2, 1))))
    values = [row['erle_db'] for row in rows if row['active']]
    median = float(np.median(values)) if len(values) >= 5 else None
    return dict(result, available=median is not None,
                passed=median is not None and median >= 20 and result['saturation']['passed'],
                median_db=median, active_frames=len(values), frames=rows, threshold=threshold,
                delay_samples=delay_samples, residual_floor_lsb_squared=1)


def known_match(source, captured, *, minimum_correlation=.6, minimum_gap=.05):
    """Complete aperiodic source match; rejects ambiguity outside +/-4 samples."""
    a, b = [np.asarray(x, dtype=float).reshape(-1) for x in (source, captured)]
    if len(a) < 1600 or len(b) <= len(a) or not np.isfinite(a).all() or not np.isfinite(b).all():
        return dict(available=False, reason='Complete >=100ms source and longer capture required')
    sos = signal.butter(4, [500, 2500], fs=RATE, btype='bandpass', output='sos')
    a, b = signal.sosfilt(sos, a), signal.sosfilt(sos, b)
    numerator = signal.correlate(b, a, mode='valid', method='fft')
    cumulative = np.r_[0.,np.cumsum(b*b)]
    power = np.maximum(0.,cumulative[len(a):]-cumulative[:-len(a)])
    # FFT roundoff in an exactly silent tail must never divide by ~zero and
    # manufacture a huge correlation. One LSB-squared product is the floor.
    values = numerator/np.sqrt(np.maximum(power*float(a@a), 1.))
    at = int(np.argmax(np.abs(values)))
    other = np.abs(values[np.abs(np.arange(len(values))-at)>4])
    peak = float(abs(values[at])); second = float(other.max()) if len(other) else 0.
    return dict(available=minimum_correlation <= peak <= 1.000001 and peak-second >= minimum_gap,
                start=at, end=at+len(a), correlation=peak, secondary=second,
                minimum_correlation=minimum_correlation, minimum_gap=minimum_gap)


def quiet_stop(pcm, receipts, *, request_s, word_end, marker_start, quiet_rms):
    """Upper bound from receipts, freshness/coverage from two acoustic witnesses.

    The caller must independently prove the word was played after request_s,
    and marker after request_s+2.1s. This function cannot infer either from
    delayed pipe timestamps. Every sample through the marker is examined.
    """
    x = np.asarray(pcm, dtype=float).reshape(-1)
    receipts = np.asarray(receipts, dtype=float)
    result = dict(passed=False, available=False, method='receipt-bound-witness-v1')
    if (not np.isfinite(x).all() or receipts.ndim != 2 or receipts.shape[1] != 2
            or not np.isfinite(receipts).all() or len(receipts) < 2
            or not np.all(np.diff(receipts[:, 0]) == FRAME)
            or not np.all(np.diff(receipts[:, 1]) >= 0)
            or receipts[0, 0] != FRAME or receipts[-1, 0] != len(x)
            or not math.isfinite(request_s) or not math.isfinite(quiet_rms) or quiet_rms < 0
            or type(word_end) is not int or type(marker_start) is not int
            or not 0 < word_end < marker_start <= len(x)):
        return dict(result, reason='Complete ordered receipts and independently verified acoustic witnesses required')
    # A received block is wholly quiet only when every contained 20ms frame is
    # quiet. Word/marker boundary frames are excluded conservatively.
    begin = math.ceil(word_end/FRAME)
    end = marker_start//FRAME
    levels = [rms(x[i*FRAME:(i+1)*FRAME]) for i in range(begin, end)]
    quiet = [value <= max(1., quiet_rms*10**(6/20)) for value in levels]
    candidate = None
    for i in range(max(0, len(quiet)-9)):
        if all(quiet[i:]):
            candidate = begin+i+10  # ten complete quiet frames =200ms
            if candidate <= end:
                break
            candidate = None
    if candidate is None:
        return dict(result, reason='No 200ms quiet followed by continuous quiet through late marker', levels=levels)
    upper = float(receipts[candidate-1, 1])
    latency = upper-request_s
    return dict(result, available=True, passed=0 <= latency <= 1,
                stop_upper_s=upper, onset_lower_s=request_s, latency_upper_s=latency,
                quiet_confirmation_samples=3200, quiet_until_sample=marker_start,
                threshold_rms_lsb=max(1., quiet_rms*10**(6/20)), levels=levels)


def native_stop(pcm, *, word_match, marker_match, bot_match, quiet_reference, word_active_offset=0):
    """Common ADC clock: no continued DAC hidden by Jabra self suppression.

    Word and late-marker matches on raw MIC1 order the electrical reference,
    avoiding an invented serial-to-sample clock. Quiet through the late marker
    is conservative; MIC3 speech resumption after the word fails.
    """
    x = np.asarray(pcm)
    result = dict(passed=False)
    if (x.ndim != 2 or x.shape[1] != 5
            or not all(m.get('available') is True for m in (word_match, marker_match, bot_match))):
        return dict(result, reason='Verified bot, word and late marker on common ADC clock required')
    a, b = word_match['end'], marker_match['start']
    threshold = reference_threshold(quiet_reference)['threshold_rms_lsb']
    onset=word_match.get('start',0)+word_active_offset
    if type(onset) is not int or onset<960 or onset+320>len(x):
        return dict(result,reason='Missing source-defined near-end onset')
    overlap=[rms(x[at:at+FRAME,2]) for at in range(onset-960,onset+1,FRAME)]
    active_at_onset=all(value>=threshold for value in overlap)
    if not bot_match['end'] < a < b or b-a < 3200:
        return dict(result, reason='Invalid source ordering or insufficient quiet tail')
    levels = [rms(x[i:i+FRAME, 2]) for i in range(a, b-FRAME+1, FRAME)]
    return dict(result, passed=active_at_onset and bool(levels) and all(v < threshold for v in levels),
                active_at_user_onset=active_at_onset,onset_reference_rms_lsb=overlap,
                levels=levels, threshold_rms_lsb=threshold, word_end=a, marker_start=b)


def session_integrity(samples, packets, *, start_s, end_s):
    result = dict(passed=False)
    if len(samples) < 2:
        return dict(result, reason='Before/after active peer samples required')
    first = samples[0]
    identity = (first.get('pc_id'), first.get('generation'))
    same = bool(identity[0]) and type(identity[1]) is int and all(
        (p.get('pc_id'), p.get('generation')) == identity for p in samples)
    clocks = all(type(p.get('device_us')) is int for p in samples)
    if clocks:
        clocks = all(b['device_us']>a['device_us'] for a,b in zip(samples,samples[1:]))
    deltas = []
    for a, b in zip(samples, samples[1:]):
        row = {key:b[key]-a[key] if type(a.get(key)) is int and type(b.get(key)) is int else None
               for key in TRANSPORT_COUNTERS}
        deltas.append(row)
    transport = all(p.get('playout_schema') == 2 for p in samples) and all(
        value == 0 for row in deltas for value in row.values())
    clips = [microphone_clip_gate(a, b) for a, b in zip(samples, samples[1:])]
    memory = memory_gate(samples)
    sampled = [p.get('heap',{}).get('internal',{}).get('sampled_largest_minimum') for p in samples]
    sampled_ok = all(v is None or type(v) is int and v >=32768 for v in sampled)
    task_checks = []
    for p in samples:
        tasks = {t['name']:t for t in p.get('tasks', [])}
        required = ('audio-capture', 'audio-playback', 'audio-tx', 'peer-loop', 'app-control')
        task_checks.append(all(name in tasks and type(tasks[name].get('stack_free_bytes')) is int
            and tasks[name]['stack_free_bytes'] >= (2048 if name=='app-control' else
                max(4096, .25*(tasks[name].get('stack_bytes') or 0)) if name=='audio-playback' else 4096)
            for name in required))
    audio = all(p.get('aec', {}).get('enabled') is True
        and p.get('aec', {}).get('ns_enabled') is False
        and p.get('aec', {}).get('agc_enabled') is False and all(
        p.get('aec', {}).get(key) == 0 for key in ('errors', 'reference_clips', 'output_clips'))
        and all(p.get('audio', {}).get(key) == 0 for key in
                ('drops', 'read_errors', 'write_errors', 'write_timeouts', 'clips'))
        and p.get('output_volume_relative_db')==0 for p in samples)
    streams = {(r.get('pc_id'), r.get('ssrc')) for r in packets}
    uplink = link_tail(packets, start_s, end_s, startup_seconds=0, excluded_windows=())
    # No startup, console or dump exclusion. Identity, packet continuity and
    # complete endpoint coverage are additional to existing tail thresholds.
    seq = bool(uplink['gaps']) and all(g['seq_step'] == 1 and g['timestamp_step'] == 960
                                     for g in uplink['gaps'])
    display = all(p.get('display',{}).get('lcd_initialized') is True
                  and p.get('display',{}).get('animation') is True
                  and p.get('display',{}).get('tx_pri') == 1 for p in samples)
    fps = []
    if clocks:
        for a,b in zip(samples,samples[1:]):
            duration=(b['device_us']-a['device_us'])/1e6
            ra,rb=(p.get('display',{}).get('rendered') for p in (a,b))
            fps.append((rb-ra)/duration if duration>0 and type(ra) is int and type(rb) is int else None)
        scored_fps=[v for v,a,b in zip(fps,samples,samples[1:]) if b['device_us']-a['device_us']>=1000000]
        display = display and bool(scored_fps) and all(v is not None and v>=24 for v in scored_fps)
    checks = dict(identity=same, clocks=clocks, transport=transport, clips=all(c['passed'] for c in clips),
                  memory=memory['passed'] and sampled_ok, tasks=all(task_checks), audio=audio, display=display,
                  uplink=uplink['passed'] is True and seq and len(streams)==1
                    and next(iter(streams))[0]==identity[0])
    return dict(result, passed=all(checks.values()), checks=checks,
                deltas=deltas, clips=clips, memory=memory, uplink=uplink, interval_display_fps=fps)


def exact_transcript(text, expected='Stop.'):
    return bool(re.findall(r'\w+', expected.lower())) and re.findall(r'\w+', text.lower()) == re.findall(r'\w+', expected.lower())


def rtp_coverage(socket, authenticated, start_s, end_s):
    def identity(row):return tuple(row[k] for k in ('pc_id','ssrc','seq','timestamp'))
    def rows(items):return [r for r in items if start_s-.1<=r['observed_ms']/1000<=end_s+.1]
    a,b=rows(socket),rows(authenticated)
    left,right={identity(r) for r in a},{identity(r) for r in b}
    # Boundary timestamps can differ while the original receive handler
    # finishes. Compare all identities in the strict interior, requiring each
    # to exist in the full other trace, never inventing or padding packets.
    all_a,all_b={identity(r) for r in socket},{identity(r) for r in authenticated}
    missing_auth=left-all_b;missing_socket=right-all_a
    return dict(passed=bool(left and right) and not missing_auth and not missing_socket
                and len(left)==len(a) and len(right)==len(b),
                socket_packets=len(a),authenticated_packets=len(b),
                missing_authenticated=[list(r) for r in sorted(missing_auth)],
                missing_socket=[list(r) for r in sorted(missing_socket)])


def future_speech(source, *, source_offset, generated_before_trigger):
    """Counterfactual at verified near-end onset on the DAC sample clock.

    Offset comes from raw MIC3/source matching and the raw MIC1 word witness,
    never from AFE residual or a stop-maximizing search. Require >=2s already
    generated before the trigger and no natural >=200ms silence in that span.
    """
    x = np.asarray(source, dtype=float).reshape(-1)
    valid = (type(source_offset) is int and source_offset >= 0
             and type(generated_before_trigger) is int
             and source_offset+2*RATE <= min(len(x), generated_before_trigger)
             and np.isfinite(x).all())
    if not valid:
        return dict(passed=False, reason='Two seconds of attributable future PCM not already generated')
    block = x[source_offset:source_offset+2*RATE].reshape(-1, FRAME)
    # Fixed source-only absolute activity, not an AEC output threshold.
    quiet = np.sqrt(np.mean(block*block, axis=1)) < 32768*10**(-45/20)
    longest = run = 0
    for value in quiet:
        run = run+1 if value else 0
        longest = max(longest, run)
    return dict(passed=longest < 10, source_offset=source_offset,
                generated_before_trigger=generated_before_trigger, proof_samples=2*RATE,
                quiet_threshold_dbfs=-45, longest_quiet_seconds=longest*.02)


def final_transcript(events):
    # Real aggregator messages are authoritative. Repeated STT interim/final
    # updates remain in the audit but cannot be cherry-picked for a match.
    finals = [e.get('text') for e in events if e.get('event')=='user-turn-stopped']
    provider = [e.get('text') for e in events if e.get('event')=='frame'
                and e.get('frame')=='TranscriptionFrame' and e.get('text')]
    available=len(finals)==1 and type(finals[0]) is str and bool(provider)
    return dict(available=available, raw=finals,provider_finals=provider,
                correct=available and exact_transcript(finals[0])
                    and all(type(text) is str and exact_transcript(text) for text in provider))


def adjudicate_transcripts(rows,events):
    """Recompute from COMPLETE session evidence, including after seal/export.

    Never preserve a correct verdict sampled before a late conflicting final.
    Every actual final belongs to exactly one original and its fixed deadline.
    """
    def is_final(e):
        return e.get('event')=='user-turn-stopped' or (e.get('event')=='frame'
            and e.get('frame')=='TranscriptionFrame' and bool(e.get('text')))
    finals=[e for e in events if is_final(e)]
    unassigned=[]
    for e in finals:
        owners=[r for r in rows if r.get('kind')=='barge' and r.get('id')==e.get('case_id')
                and r['start_s']<=e['t']<=r['end_s']]
        if len(owners)!=1:unassigned.append(e)
    scores={}
    for row in rows:
        if row.get('kind')!='barge':continue
        attributed=[e for e in events if e.get('case_id')==row['id']]
        score=final_transcript(attributed)
        late=[e for e in attributed if is_final(e) and not row['start_s']<=e['t']<=row['end_s']]
        score['out_of_window_finals']=late
        if late:score.update(available=False,correct=False)
        scores[row['id']]=score
    return dict(scores=scores,unassigned=unassigned,
                correct=sum(s['correct'] is True for s in scores.values()))
