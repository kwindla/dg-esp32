"""Settled heap fits and explicit reconnect budgets for endurance evidence.

Free-heap slope is negative for growth. A failure requires the *upper* two-SE
bound on free slope below -64 B/min (equivalently growth - 2*SE > 64).
Short/missing windows have no verdict; they are listed separately.
"""
import copy
import math
import numpy as np

HEAP_GROWTH_LIMIT_BYTES_PER_MINUTE = 64
SETTLE_MINUTES = 5
MINIMUM_FIT_SPAN_MINUTES = 10
RECONNECT_TRANSIENT_BUDGET_BYTES = 16*1024
CAPS = ('internal', 'psram', 'dma')
SESSION_TRANSIENT_LIMIT_BYTES = dict(internal=4096, dma=4096, psram=64*1024)
STACK_SIZES = {'tx_stack':'tx_stack_bytes', 'owner_stack':'owner_stack_bytes',
               'rx_stack':'rx_stack_bytes', 'decode_stack':'playback_stack_bytes'}


# Each session needs a post-five-minute-settle observation before teardown.
# A three-minute tail also preserves >=10 pooled fitted minutes with 30-second
# sampling and recovery gaps, without changing any memory acceptance limit.
SOAK_FAULT_SCHEDULE=((480,'reconnect'),(960,'server-restart'),(1440,'reconnect'))
QUALIFICATION_MEMORY_TAIL_SECONDS=180


def ready_times_from_device_log(observations, ready_device_us, clock_slack_seconds=.05):
    """Retain samples, map Ready to a conservative host-time upper bound.

    PERF device time precedes completion of its host observation. Use the
    first observation of each generation, so clock extrapolation is short;
    add 50 ms slack. A device-clock reset cannot be mapped across boots.
    """
    rows=copy.deepcopy(observations)
    if any(b['perf']['device_us']<a['perf']['device_us'] for a,b in zip(rows,rows[1:])):
        raise ValueError('Device clock reset; Ready mapping needs separate boot epochs')
    bounds={}
    for row in rows:
        generation=row['perf']['generation']
        if generation not in bounds:
            device=row['perf']['device_us']
            earlier=[t for t in ready_device_us if t<=device]
            if not earlier:raise ValueError('No prior Ready timestamp for generation')
            ready=max(earlier)
            bounds[generation]=(row['elapsed_s']-(device-ready)/1e6+clock_slack_seconds,ready)
        bound,ready=bounds[generation]
        row.update(ready_elapsed_s=bound,ready_device_us=ready,
                   ready_mapping_basis='Conservative first PERF observation completion bound +50 ms slack')
    return rows


def settled_groups(observations, settle_minutes):
    groups = {}
    for row in observations:
        groups.setdefault(row['perf']['generation'], []).append(row)
    for generation, original in groups.items():
        original.sort(key=lambda r:r['elapsed_s'])
        explicit = [r['ready_elapsed_s'] for r in original if r.get('ready_elapsed_s') is not None]
        ready = min(explicit) if explicit else original[0]['elapsed_s']
        rows = [r for r in original if r['elapsed_s'] >= ready+settle_minutes*60]
        yield generation, original, rows, ready, bool(explicit)


def fit_heap(groups, caps, minimum_span_minutes):
    usable = [r for r in groups if len(r)>=3]
    complete = all(row['perf'].get('heap', {}).get(caps, {}).get('free') is not None
                   for rows in usable for row in rows)
    span = sum((rows[-1]['elapsed_s']-rows[0]['elapsed_s'])/60 for rows in usable)
    n = sum(len(rows) for rows in usable)
    valid = bool(usable) and complete and span >= minimum_span_minutes and n > len(usable)+1
    slope = se = ols_se = residual_sd = largest_step = None
    if valid:
        xs, ys = [], []
        largest_step = 0
        for rows in usable:
            x = np.array([r['elapsed_s']/60 for r in rows])
            y = np.array([r['perf']['heap'][caps]['free'] for r in rows], dtype=float)
            xs.append(x-x.mean()); ys.append(y-y.mean())
            largest_step = max(largest_step, float(max(0, -np.min(np.diff(y)))))
        x = np.concatenate(xs); y = np.concatenate(ys); xx = np.dot(x,x)
        slope = float(np.dot(x,y)/xx); residual = y-slope*x
        residual_sd = float(np.sqrt(np.dot(residual,residual)/(n-len(usable)-1)))
        ols_se = residual_sd/math.sqrt(xx)
        # Newey-West within generations: no covariance across reconnects.
        variance = 0.; at = 0
        for rows in usable:
            z = (x*residual)[at:at+len(rows)]; at += len(rows)
            lags = min(len(z)-1, max(1, int(len(z)**(1/3))))
            variance += float(np.dot(z,z))
            for lag in range(1,lags+1):
                variance += 2*(1-lag/(lags+1))*float(np.dot(z[lag:],z[:-lag]))
        hac_se = math.sqrt(max(0,variance)*n/(n-len(usable)-1))/xx
        se = max(ols_se, hac_se)
    passed = bool(slope+2*se >= -HEAP_GROWTH_LIMIT_BYTES_PER_MINUTE) if valid else None
    return dict(samples=n, generations=len(usable), span_minutes=span,
        minimum_fit_span_minutes=minimum_span_minutes, free_slope_bytes_per_minute=slope,
        slope_se_bytes_per_minute=se, ols_slope_se_bytes_per_minute=ols_se,
        uncertainty_method='max(OLS, within-generation Newey-West) is an SE floor, not a guaranteed autocorrelation correction; common slope with per-generation intercepts',
        residual_sd_bytes=residual_sd, largest_single_sample_drop_bytes=largest_step,
        growth_bytes_per_minute=max(0,-slope) if valid else None,
        growth_lower_two_se_bytes_per_minute=-slope-2*se if valid else None,
        minimum_detectable_growth_bytes_per_minute=HEAP_GROWTH_LIMIT_BYTES_PER_MINUTE+2*se if valid else None,
        passed=passed, verdict='unknown' if passed is None else 'pass' if passed else 'fail',
        unknown_reason=None if valid else 'Need >=3 complete samples per fitted generation and >=10 total fitted minutes after settlement')


def heap_trends(observations, *, settle_minutes=SETTLE_MINUTES,
                minimum_span_minutes=MINIMUM_FIT_SPAN_MINUTES):
    trends = {}; all_settled = []
    for ordinal,(generation, original, rows, ready, explicit) in enumerate(settled_groups(observations, settle_minutes),1):
        all_settled.append(rows)
        trends[str(generation)] = {}
        for caps in CAPS:
            value = fit_heap([rows], caps, minimum_span_minutes)
            value.update(session_ordinal=ordinal,device_generation=generation,samples=len(rows), original_samples=len(original),
                settle_samples_excluded=len(original)-len(rows), ready_elapsed_s=ready,
                ready_basis='Ready host timestamp' if explicit else 'first live observation (Ready timestamp unavailable)',
                settle_minutes=settle_minutes)
            trends[str(generation)][caps] = value
    trends['common'] = {caps:fit_heap(all_settled, caps, minimum_span_minutes) for caps in CAPS}
    return trends


def growth_verdict(trends):
    candidates = trends.get('common')
    if candidates is not None:
        values = [v['passed'] for v in candidates.values()]
        return all(values) if all(v is not None for v in values) else None
    known = [v['passed'] for group in trends.values() for v in group.values() if v['passed'] is not None]
    return all(known) if known else None


def unknown_heap_windows(trends):
    return [dict(generation=generation, capability=caps, reason=value['unknown_reason'],
                 samples=value['samples'], span_minutes=value['span_minutes'])
            for generation,group in trends.items() if generation != 'common'
            for caps,value in group.items() if value['passed'] is None]


def closed_stack_margins(closed):
    return {key:100*closed[key]/closed[size] for key,size in STACK_SIZES.items()
            if closed.get(size) and closed.get(key) is not None}


def closed_stack_unknown_sizes(closed):
    return [key for key,size in STACK_SIZES.items()
            if not closed.get(size) or closed.get(key) is None]


def transient_memory(observations, closed, faults):
    rows = sorted(observations, key=lambda r:r['elapsed_s'])
    augmented = copy.deepcopy(faults)
    for fault in augmented:
        before = [r for r in rows if r['elapsed_s'] <= fault['started_s']]
        after = [r for r in rows if r['elapsed_s'] >= fault['recovered_s']]
        fault['minimum_free_step_bytes'] = {caps:max(0,
            before[-1]['perf']['heap'][caps]['minimum_free']-after[0]['perf']['heap'][caps]['minimum_free'])
            for caps in CAPS} if before and after else None
        fault['minimum_free_step_basis'] = 'Last pre-fault / first recovered observation; boot-cumulative high-water mark'
    per_generation = {}
    for generation, original, settled, ready, explicit in settled_groups(rows, SETTLE_MINUTES):
        baseline_index = len(original)-len(settled) if settled else None
        baseline = original[baseline_index] if baseline_index is not None else None
        pre_drop = {caps:max(0, original[0]['perf']['heap'][caps]['minimum_free']-
            baseline['perf']['heap'][caps]['minimum_free']) for caps in CAPS} if baseline else None
        settlement_gap = (baseline['elapsed_s']-original[baseline_index-1]['elapsed_s']
                          if baseline_index else 0) if baseline else None
        settlement_step = {caps:max(0, original[baseline_index-1]['perf']['heap'][caps]['minimum_free']-
            baseline['perf']['heap'][caps]['minimum_free']) if baseline_index else 0
            for caps in CAPS} if baseline else None
        decrease = None
        windows = []
        if settled:
            endpoints = list(settled)
            if generation == rows[-1]['perf']['generation'] and (
                closed.get('heap_window',{}).get('end_us') is None or
                closed.get('heap_window',{}).get('end_us')!=settled[-1]['perf'].get('heap_window',{}).get('end_us')):
                endpoints.append(dict(perf=closed,elapsed_s=None,heap_interval=closed.get('heap_interval')))
            decrease = {caps:0 for caps in CAPS}
            for before, after in zip(endpoints, endpoints[1:]):
                bp, ap = before['perf'], after['perf']
                steps = {caps:max(0, bp['heap'][caps]['minimum_free']-
                             ap['heap'][caps]['minimum_free']) for caps in CAPS}
                # Window minima are reset by every PERF read. Intermediate
                # snapshots must be merged, or the interval is unavailable.
                merged=after.get('heap_interval')
                hw=ap.get('heap_window',{})
                prev=bp.get('heap_window',{})
                continuous=(hw.get('schema')==1 and prev.get('schema')==1 and
                    hw.get('start_us')==prev.get('end_us') and hw.get('samples',0)>0)
                minimum=(merged.get('minimum_free',{}) if merged else
                    {c:ap['heap'][c].get('window_minimum_free') for c in CAPS})
                measured=merged.get('coverage_complete') is True if merged else continuous
                depth={c:max(0,bp['heap'][c]['free']-minimum[c])
                    if measured and minimum.get(c) is not None else None for c in CAPS}
                legacy_depth={c:max(0,bp['heap'][c]['free']-ap['heap'][c]['minimum_free'])
                    if steps[c] else 0 for c in CAPS}
                headroom = {caps:max(0, bp['heap'][caps]['free']-
                             original[0]['perf']['heap'][caps]['minimum_free']) for caps in CAPS}
                increments = {key:max(0, ap.get(key,0)-bp.get(key,0))
                              for key in ('rebuffers','plc_frames')}
                reasons = ['rebuffers'] if increments['rebuffers']>0 else []
                if after.get('host_uplink_max_gap_ms',0)>=1000:
                    reasons.append('host_uplink_gap_ge_1s')
                # Forced fault windows retain their independent 16 KiB budget.
                fault_window = any(before['elapsed_s']<=f['recovered_s'] and
                    (after['elapsed_s'] is None or after['elapsed_s']>=f['started_s']) for f in faults)
                disturbed = bool(reasons)
                over_limit=any(depth[c] is not None and depth[c]>SESSION_TRANSIENT_LIMIT_BYTES[c] for c in CAPS)
                depth_available=all(depth[c] is not None for c in CAPS)
                window = dict(generation=generation, before_elapsed_s=before['elapsed_s'],
                    elapsed_s=after['elapsed_s'], minimum_free_step_bytes=steps,
                    connect_trough_headroom_bytes=headroom, actual_depth_bytes=depth,
                    legacy_cumulative_depth_bytes=legacy_depth,
                    plc_increment= increments['plc_frames'],depth_available=depth_available,
                    disturbed_over_limit=disturbed and over_limit,
                    network_disturbed=disturbed, disturbance_reasons=reasons,
                    forced_fault_window=fault_window,
                    host_uplink_max_gap_ms=after.get('host_uplink_max_gap_ms'),
                    passed=None if disturbed or fault_window or not depth_available else not over_limit)
                windows.append(window)
                if not disturbed and not fault_window:
                    for caps in CAPS:decrease[caps]+=steps[caps]
        per_generation[str(generation)] = dict(
            baseline_elapsed_s=settled[0]['elapsed_s'] if settled else None,
            ready_elapsed_s=ready, pre_settle_minimum_decrease_bytes=pre_drop,
            settlement_sample_gap_seconds=settlement_gap, settlement_sample_step_bytes=settlement_step,
            connect_trough_headroom_bytes={c:max(0,baseline['perf']['heap'][c]['free']-
                original[0]['perf']['heap'][c]['minimum_free']) for c in CAPS} if baseline else None,
            informational_clean_minimum_decrease_bytes=decrease,
            minimum_decrease_bytes=decrease, windows=windows,
            passed=(False if any(w['passed'] is False for w in windows) else
                None if any(w['disturbed_over_limit'] for w in windows) else
                None if not windows or
                    any(not w['depth_available'] and not w['forced_fault_window'] for w in windows) or
                    not any(w['passed'] is not None for w in windows) else True))
    available = [r['minimum_decrease_bytes'] for r in per_generation.values() if r['minimum_decrease_bytes'] is not None]
    decrease = {caps:max(r[caps] for r in available) if available else None for caps in CAPS}
    known_steps = [v for f in augmented if f['minimum_free_step_bytes'] is not None
                   for v in f['minimum_free_step_bytes'].values()]
    budget = (all(v<=RECONNECT_TRANSIENT_BUDGET_BYTES for v in known_steps)
              if all(f['minimum_free_step_bytes'] is not None for f in augmented) else None)
    known_transients = [r['passed'] for r in per_generation.values() if r['passed'] is not None]
    return dict(faults=augmented, heap_minimum_decrease_bytes=decrease,
        session_transient_limits_bytes=SESSION_TRANSIENT_LIMIT_BYTES,
        network_disturbances=[w for r in per_generation.values() for w in r['windows'] if w['network_disturbed']],
        transient_depth_basis='Previous sampled free minus interval loop-sampled minimum. Every PERF reset must be covered; missing window minima are unknown. Legacy cumulative depths are informational and censored by the deepest boot trough, including earlier generations.',
        transient_generations=per_generation,
        transient_baseline_basis='First post-five-minute-settle observation of each generation; fault and network-disturbed windows reported separately; all samples retained for growth fit',
        reconnect_transient_budget_bytes=RECONNECT_TRANSIENT_BUDGET_BYTES,
        reconnect_transient_passed=budget,
        heap_transient_depth_passed=(False if False in known_transients else all(known_transients) if per_generation and len(known_transients)==len(per_generation) else None),
        informational_heap_minimum_decrease_bytes=decrease,
        unknown_transient_generations=[g for g,r in per_generation.items() if r['passed'] is None],
        unsettled_generations=[g for g,r in per_generation.items() if r['passed'] is None])


def host_uplink_gap_windows(observations, packets):
    """Attach host receive gaps to PERF intervals without changing observations.

    Use host monotonic clocks, group by peer ID and SSRC, and never compare
    endpoints across sessions. Windows intersecting a gap retain it, including
    a gap spanning an observation endpoint. This is receipt evidence, not loss.
    """
    rows = copy.deepcopy(observations)
    streams = {}
    for packet in packets:
        streams.setdefault((packet['pc_id'],packet['ssrc']), []).append(packet)
    gaps = {}
    for (pc, _), stream in streams.items():
        ordered = sorted(stream,key=lambda p:p['observed_ms'])
        gaps.setdefault(pc, []).extend((a['observed_ms'],b['observed_ms'])
            for a,b in zip(ordered,ordered[1:]))
    previous = {}
    for row in sorted(rows,key=lambda r:r['elapsed_s']):
        pc=row['perf'].get('pc_id');end=row.get('observed_monotonic_s')
        start=previous.get(pc)
        if end is not None:
            relevant=[b-a for a,b in gaps.get(pc,[]) if start is not None and a<=end*1000 and b>=start*1000]
            row['host_uplink_max_gap_ms']=max(relevant,default=0)
            previous[pc]=end
    return rows


def attach_heap_intervals(observations,perf_windows):
    """Merge all retained PERF resets between observation endpoints."""
    rows=copy.deepcopy(observations)
    for before,after in zip(rows,rows[1:]):
        left=before['perf'].get('heap_window',{}).get('end_us')
        right=after['perf'].get('heap_window',{}).get('end_us')
        if left is None or right is None:continue
        selected=sorted((p for p in perf_windows if p.get('heap_window',{}).get('schema')==1 and
            left<p['heap_window']['end_us']<=right),key=lambda p:p['heap_window']['end_us'])
        expected=left;complete=bool(selected);mins={c:None for c in CAPS}
        for p in selected:
            w=p['heap_window'];complete &= w['start_us']==expected and w['samples']>0
            expected=w['end_us']
            for c in CAPS:
                value=p.get('heap',{}).get(c,{}).get('window_minimum_free')
                if value is None:complete=False
                else:mins[c]=value if mins[c] is None else min(mins[c],value)
        after['heap_interval']=dict(start_us=left,end_us=right,perf_windows=len(selected),
            coverage_complete=complete and expected==right,minimum_free=mins)
    return rows
