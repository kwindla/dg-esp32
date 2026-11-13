"""Prospective v8.4 option (a): measured MIC3 span, bounded horizon, cancel guard.

Not adopted or frozen. A declaration must explicitly select the reviewed rule.
Source continuation is an identity/existence witness, never an amplitude model.
"""
import math
import re

import numpy as np

from echoear_aec_metrics import FRAME, frame_power, reference_threshold
from echoear_aec_timing import sample_interval, map_interval


RULE = 'ongoing-reply-lull-v84'
POLICY = dict(rule=RULE, span_max_frames=5, horizon_after_latest_ms=120,
              alignment_origin_tolerance_samples=2, cancelled_expected=0)
UNCERTAINTY_SAMPLES = 320
ALIGNMENT_SAMPLES = 3200
ALIGNMENT_DRIFT_SAMPLES = 2


def eligible(opportunity, policy=None):
    if policy is not None and policy != RULE:
        raise ValueError('Unknown prospective lull policy')
    return opportunity == 'eligible' or policy == RULE and opportunity == 'ongoing-reply-lull'


def covered(blocks, start, end):
    """Require contiguous complete timing coverage, rejecting gaps/overlaps."""
    if start < 0 or end <= start:
        return False
    cursor = start
    for a, b, _lo, _hi, gap in blocks:
        if b <= start or a >= end:
            continue
        if gap or a > cursor or (cursor > start and a != cursor) or b <= a:
            return False
        cursor = int(b)
    return cursor >= end


def declared_rule(plan):
    policy = plan.get('ongoing_reply_lull')
    if policy is None:
        return None
    if policy != POLICY:
        raise ValueError('Unknown/changed v8.4 lull declaration')
    return RULE


def cancelled_check(cases):
    ids = [r.get('id') for r in cases if r.get('opportunity') == 'unscorable-lull-cancelled']
    return dict(passed=not ids, expected=0, observed=len(ids), ids=ids)


def measure_lulls(reference, quiet, blocks, onset):
    """Lazy frame reads, never past the closure or L+120 ms hard horizon.

    Bound the outer SPAN of all touched runs, including active frames between
    them. Stop after proving >5, or if coverage/closure is missing. Such a
    failure can still be cancellation-censored by the independent guard.
    """
    threshold = reference_threshold(quiet)
    lo, hi = onset-UNCERTAINTY_SAMPLES, onset+UNCERTAINTY_SAMPLES
    first, last = lo//FRAME, hi//FRAME
    limit = hi+1920
    result = dict(rule=RULE, passed=False, threshold_power=threshold,
                  onset_interval_samples=[lo,hi], runs=[], span_frames=None,
                  horizon_limit_sample=limit, horizon_end_sample=limit,
                  max_mic3_sample_read_exclusive=0, uses_post_earliest_mic3=False,
                  capture_end_missing=False)
    cache = {}

    def active(i):
        if i in cache:return cache[i]
        a,b=i*FRAME,(i+1)*FRAME
        if b>len(reference):result['capture_end_missing']=True
        if a<0 or b>len(reference) or b>limit or not covered(blocks,a,b):
            return None
        # Slice first: np.asarray(reference) or frame_power(full_capture) would
        # accidentally inspect post-horizon samples in the decision path.
        value=bool(frame_power(reference[a:b])[0]>threshold)
        result['max_mic3_sample_read_exclusive']=max(result['max_mic3_sample_read_exclusive'],b)
        cache[i]=value
        return value

    candidates=[active(i) for i in range(first,last+1)]
    if None in candidates:return dict(result,reason='Incomplete/gapped onset frames')
    inactive=[i for i in range(first,last+1) if not cache[i]]
    if not inactive:return dict(result,reason='Strict active onset; no lull')
    a,b=min(inactive),max(inactive)
    while True:
        value=active(a-1)
        if value is None:return dict(result,reason='Missing/gapped left closure')
        if value:break
        a-=1
        if b-a+1>5:return dict(result,span_frames=b-a+1,reason='Inactive span exceeds five frames')
    while True:
        value=active(b+1)
        if value is None:return dict(result,reason='Missing/gapped right closure')
        if value:break
        b+=1
        if b-a+1>5:return dict(result,span_frames=b-a+1,reason='Inactive span exceeds five frames')
    horizon=(b+2)*FRAME
    span=b-a+1
    result.update(span_frames=span,span_ms=span*20,frame_range=[a,b+1],
                  closing_frame_end_sample=horizon,
                  horizon_end_sample=max(horizon,result['max_mic3_sample_read_exclusive']),
                  uses_post_earliest_mic3=horizon>lo)
    # Include every intervening frame, even an active middle between two runs.
    if any(active(i) is None for i in range(a-1,b+2)):
        return dict(result,reason='Gap in closed span')
    result['passed']=span<=5 and horizon<=limit
    result['reason']='Measured bounded span' if result['passed'] else 'Inactive span exceeds five frames'
    result['runs']=[dict(frame_range=[a,b+1],duration_ms=span*20,closed=True,
                         closing_active_frame_end=horizon)]
    return result


def pre_near_source_witness(reference, quiet, blocks, anchors, onset, source,
                            recorded, pc_id, context_id, *, horizon_end_sample=None):
    """Two disjoint pre-earliest MIC3 matches; same-context written continuation.

    Only the successfully written prefix before the earliest HOST onset is
    searched. Source PCM generated later and later write/drop events cannot
    alter the witness. The amplitude threshold for SOURCE continuation is the
    existing recorder speech threshold (>1024 LSB²), not the MIC3 threshold.
    Alignment drift is the reviewed two-sample tolerance, not fitted per case.
    """
    from echoear_aec_accept_analysis import match

    result = dict(rule=RULE, passed=False, matches=[], pc_id=pc_id, context_id=context_id)
    lo, hi = onset - UNCERTAINTY_SAMPLES, onset + UNCERTAINTY_SAMPLES
    if not pc_id or not context_id or lo < 2*ALIGNMENT_SAMPLES:
        return dict(result, reason='Missing context or pre-onset alignment coverage')
    try:
        left = sample_interval(lo, blocks, anchors)
        right = sample_interval(hi, blocks, anchors)
    except (ValueError, TypeError, KeyError):
        return dict(result, reason='Invalid onset timing')
    if left is None or right is None or not math.isfinite(left[0]) or left[0] > right[1]:
        return dict(result, reason='Unmapped onset timing')
    result['host_onset_interval'] = [left[0], right[1]]
    if not covered(blocks, lo-2*ALIGNMENT_SAMPLES, hi+1):
        return dict(result, reason='Missing/gapped pre-onset or onset coverage')
    prefix = []
    for e in recorded:
        if (e.get('kind') == 'gap' and e.get('stream') == 'tts-written' and e.get('pc_id') == pc_id
                and (type(e.get('monotonic_ns')) is not int or e['monotonic_ns']/1e9 < left[0])):
            return dict(result, reason='Pre-onset transport write gap')
        if e.get('kind') != 'tts-transport-write' or e.get('pc_id') != pc_id or e.get('context_id') != context_id:
            continue
        ns = e.get('monotonic_ns')
        if type(ns) is not int:
            return dict(result, reason='Missing source write timestamp')
        if ns/1e9 < left[0]:
            prefix.append(e)
    prefix.sort(key=lambda e: e['monotonic_ns'])
    cursor = 0
    ranges = []
    for e in prefix:
        n = e.get('samples')
        if e.get('drop') is not False or type(n) is not int or n <= 0:
            return dict(result, reason='Missing/dropped pre-onset source write')
        ranges.append(dict(start=cursor, end=cursor+n, host_s=e['monotonic_ns']/1e9,
                           non_silent=e.get('non_silent') is True))
        cursor += n
    if not ranges or cursor > len(source):
        return dict(result, reason='Missing source PCM/write coverage')
    written = np.asarray(source[:cursor], dtype=float)
    if not np.isfinite(written).all():
        return dict(result, reason='Nonfinite written source')
    result['written_prefix_samples'] = cursor
    origins = []
    for start in (lo-2*ALIGNMENT_SAMPLES, lo-ALIGNMENT_SAMPLES):
        m = match(written, reference[start:start+ALIGNMENT_SAMPLES])
        result['matches'].append(dict(mic3_samples=[start, start+ALIGNMENT_SAMPLES], match=m))
        if not m.get('valid'):
            return dict(result, reason='Ambiguous pre-onset source alignment')
        origins.append(start-m['at'])
    result['source_origins'] = origins
    if max(origins)-min(origins) > ALIGNMENT_DRIFT_SAMPLES:
        return dict(result, reason='Independent pre-onset source matches disagree')
    # Fully observed active MIC3 frame, certainly before earliest raw onset.
    power = frame_power(reference[:lo])
    active = np.flatnonzero(power > reference_threshold(quiet))
    if not len(active):
        return dict(result, reason='No complete pre-earliest active MIC3 frame')
    recent = int(active[-1])
    if not covered(blocks, recent*FRAME, (recent+1)*FRAME):
        return dict(result, reason='Gapped pre-earliest active MIC3 frame')
    # One bound on the WHOLE run is applied separately; no second age bound.
    result['pre_earliest_active_frame'] = [recent*FRAME, (recent+1)*FRAME]
    pending = []
    for i in np.flatnonzero(frame_power(written) > 1024):
        a, b = int(i)*FRAME, (int(i)+1)*FRAME
        if b+min(origins) <= hi or a+max(origins) > hi+1920:
            continue
        coverage = [w for w in ranges if w['start'] < b and w['end'] > a]
        if coverage and coverage[0]['start'] <= a and coverage[-1]['end'] >= b and all(w['non_silent'] for w in coverage):
            pending.append(dict(source_samples=[a, b], mic3_start_bounds=[a+min(origins), a+max(origins)]))
    result['written_continuation'] = pending[:1]
    result['written_last_sample_lower_bound'] = cursor-1+min(origins)
    tail = horizon_end_sample is None or result['written_last_sample_lower_bound'] > horizon_end_sample
    result['written_tail_beyond_closure'] = tail
    result['passed'] = bool(pending) and tail
    result['reason'] = 'Pre-onset aligned written continuation' if result['passed'] else 'Missing active written continuation or tail beyond closure'
    return result


def cancellation_witness(blocks, anchors, serial, events, pc_id, context_start_s):
    """Earliest possible raw sample, not a fitted event-time point estimate.

    Device callbacks are independently mapped through device anchors. Copies
    are case-scoped and peer-scoped by the caller. Unmapped relevant events
    fail closed. Later recognition/cleanup events are not consulted.
    """
    try:
        mapped=[(int(b[0]),int(b[1]),sample_interval(int(b[0]),blocks,anchors)) for b in blocks]
        known=[r for r in mapped if r[2] is not None]
        if not known:return dict(valid=False,reason='Missing cancellation timing coverage')
        unknown=[]
        for block,entry in zip(blocks,mapped):
            if entry[2] is None:
                left=map_interval(block[2],anchors);right=map_interval(block[3],anchors)
                unknown.append([left[0],right[1]] if left is not None and right is not None else None)
        witnesses=[]
        def add(interval,kind):
            if interval[1]<context_start_s:return
            if any(t is None or t[1]>=interval[0] and t[0]<=interval[1] for t in unknown):
                raise ValueError('Cancellation bracket intersects unknown/gapped timing')
            candidates=[a for a,b,t in known if t[1]>=interval[0] and t[0]<=interval[1]]
            if candidates:sample=min(candidates)
            elif interval[0]>max(t[1] for a,b,t in known):sample=max(b for a,b,t in known)
            elif interval[1]<min(t[0] for a,b,t in known):sample=0
            else:raise ValueError('Unmapped cancellation inside capture')
            witnesses.append(dict(kind=kind,host_interval=interval,lower_sample=sample))
        for event in events:
            if event.get('event')!='interrupt-copy' or event.get('pc_id')!=pc_id:continue
            t=event.get('t')
            if not isinstance(t,(int,float)) or not math.isfinite(t):raise ValueError('Invalid interrupt-copy time')
            add([t,t],'interrupt-copy')
        for value in re.findall(r'AUDIO_CANCEL callback_us=(\d+)',serial):
            value=int(value)
            # Callback outside this capture cannot mutate its measurement.
            if value<blocks[0][2] or value>blocks[-1][3]:continue
            interval=map_interval(value,anchors)
            if interval is None:raise ValueError('Unmapped callback')
            add(interval,'device-callback')
        first=min((w['lower_sample'] for w in witnesses),default=None)
        return dict(valid=True,cancel_sample=first,witnesses=witnesses)
    except (ValueError,TypeError,KeyError,IndexError) as exc:
        return dict(valid=False,reason=str(exc))


def lull_witness(reference, quiet, blocks, anchors, onset, source, recorded,
                 pc_id, context_id, *, cancel=None):
    measurement=measure_lulls(reference,quiet,blocks,onset)
    cancel=cancel if cancel is not None else dict(valid=True,cancel_sample=None,witnesses=[])
    sample=cancel.get('cancel_sample')
    censored=bool(not measurement['capture_end_missing'] and cancel.get('valid') and sample is not None and sample<measurement.get('closing_frame_end_sample',measurement['horizon_end_sample']))
    proof=(dict(passed=False,reason='Cancellation censoring; source proof not used') if censored else
           pre_near_source_witness(reference,quiet,blocks,anchors,onset,source,recorded,
                                   pc_id,context_id,horizon_end_sample=measurement.get('closing_frame_end_sample',measurement['horizon_end_sample'])))
    # If cancellation erased the closure, the failed measurement retains the
    # bounded maximum horizon; the cancelled class takes precedence over a
    # now-long/silent tail. No eligible/no-opportunity inference from that tail.
    passed=bool(measurement['passed'] and proof['passed'] and cancel.get('valid') and not censored)
    return dict(rule=RULE,passed=passed,cancelled=censored,measurement=measurement,
                pre_near_source=proof,cancellation=cancel)


def case_witness(root, case, row, reference, quiet, blocks, anchors, recorded, serial):
    from pathlib import Path
    contexts=[e for e in case['events'] if e.get('event')=='tts-context-pcm']
    if not contexts:return dict(passed=False,cancelled=False,reason='Missing old source context')
    context=contexts[0];pc,cid=context.get('pc_id'),context.get('context_id')
    own=[e for e in recorded if e.get('kind')=='tts-transport-write' and e.get('pc_id')==pc and e.get('context_id')==cid]
    if not own:return dict(passed=False,cancelled=False,reason='Missing old-context write evidence')
    path=Path(root)/'evaluation'/context['path']
    if not path.exists():return dict(passed=False,cancelled=False,reason='Missing old-context PCM')
    source=np.fromfile(path,dtype='<i2').astype(float)
    # Case events already belong to this capture; subsequent contexts can only
    # have later copies. Use the earliest witness, never recognition outcomes.
    cancel=cancellation_witness(blocks,anchors,serial,case['events'],pc,
                                min(e['monotonic_ns'] for e in own)/1e9)
    return lull_witness(reference,quiet,blocks,anchors,row['near_onset']['raw_onset_sample'],
                        source,recorded,pc,cid,cancel=cancel)


def stratum_summary(cases):
    """Always expose empty strata; a rate without a denominator is null."""
    result = {}
    for name, opportunity in (('strict', 'eligible'), ('ongoing-reply-lull', 'ongoing-reply-lull')):
        group = [r for r in cases if r.get('opportunity') == opportunity]
        n = len(group)
        recognized = sum(bool(r.get('recognized')) for r in group)
        interrupted = sum(bool(r.get('interruption_after_stimulus')) for r in group)
        success = sum(bool(r.get('barge_success', r.get('credited_barge_in', False))) for r in group)
        result[name] = dict(eligible=n, recognized=recognized, interrupted=interrupted, successes=success,
                            recognition_rate=recognized/n if n else None,
                            interruption_rate=interrupted/n if n else None,
                            success_rate=success/n if n else None)
    a, b = result['strict']['success_rate'], result['ongoing-reply-lull']['success_rate']
    result['lull_minus_strict_success_rate'] = b-a if a is not None and b is not None else None
    result['cancelled_lull_check'] = cancelled_check(cases)
    result['review_note'] = 'A materially higher lull success rate requires review; no numerical materiality gate is adopted.'
    return result
