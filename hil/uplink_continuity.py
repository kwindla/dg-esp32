"""Continuous-session link tail, with explicit startup/idle/fault exclusions."""
from collections import defaultdict


def audio_idle_windows(states,*,clock='host_s',end=None):
    """Declare idle only from device acknowledgements, never from RTP gaps.

    Serial capture preserves device_us and monotonic host receipt time in
    serial.audio.jsonl. Use host_s for server RTP, device_us for send traces.
    A trailing audio 0 may be closed at the explicit capture endpoint.
    """
    windows=[];start=None;previous=None
    for row in states:
        at=row[clock]
        if previous is not None and at<previous:start=None
        previous=at
        if row['enabled']==0:
            if start is None:start=at
        elif start is not None:
            windows.append(dict(start_s=start,end_s=at,category='idle') if clock=='host_s'
                else dict(start_us=start,end_us=at,category='idle'))
            start=None
    if start is not None and end is not None and end>=start:
        windows.append(dict(start_s=start,end_s=end,category='idle') if clock=='host_s'
            else dict(start_us=start,end_us=end,category='idle'))
    return windows


def recorded_audio_idle_windows(serial_path,*,clock='host_s',end=None):
    import json
    from pathlib import Path
    path=Path(serial_path).with_suffix('.audio.jsonl')
    states=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return audio_idle_windows(states,clock=clock,end=end)


def idle_interval(left,right,start,end,*,seq_step=None,timestamp_step=None,slack=.1):
    """Return containment and unexcused duration, in seconds.

    In-flight packets may straddle either acknowledgement by at most 100 ms.
    Sequence loss independently prevents an idle exclusion. For a boundary
    violation, retain the entire excess on that side, without subtracting slack.
    Contained loss stays eligible at the larger of the missing 20 ms packet
    duration and the RTP timestamp advance (48 kHz), retaining raw gaps upstream.
    """
    contained=left>=start-slack and right<=end+slack
    if contained and (seq_step is None or seq_step==1):return True,right-left
    if contained and seq_step>1:
        return False,max((seq_step-1)*.02,(timestamp_step or 0)/48000)
    excess=sum(value for value in (max(0,start-left),max(0,right-end)) if value>slack)
    return False,excess if excess else right-left


RESET_DEPTH=2048       # r11 (Fable r10 N1): ~41 s of packets, well clear of straggler batches (retained maxima: 247
                       # netem fault matrix, 238 real EchoEar downlink-diag-04) and far below the 32768 sign boundary
RESET_RUN=64           # consecutive nonforward packets (~1.3 s); longest retained run: 25 (netem fault matrix)


def link_tail(packets,start,end,*,threshold_ms=60,maximum_ms=2000,rate_limit=.01,
              extreme_threshold_ms=150,extreme_rate_limit=.0005,
              startup_seconds=5,excluded_windows=()):
    streams=defaultdict(list)
    for row in packets:streams[(row['pc_id'],row['ssrc'])].append(row)
    gaps=[];disorder=[];covered_start=False;covered_end=False;longest_run=0
    for (pc,ssrc),stream in streams.items():
        rows=sorted(stream,key=lambda r:r['observed_ms'])
        if not rows:continue
        origin=rows[0]['observed_ms']/1000
        covered_start|=rows[0]['observed_ms']<=start*1000<=rows[-1]['observed_ms']
        covered_end|=rows[0]['observed_ms']<=end*1000<=rows[-1]['observed_ms']
        # r9: intervals run between consecutive *forward* packets. A packet whose 16-bit sequence number does
        # not advance past the stream head (signed difference <= 0) is a reorder (late packet) or a duplicate:
        # it is counted separately and never forms an interval, so wrapped unsigned steps (65535, 2**32-960)
        # cannot become giant gaps. The head keeps its arrival time, so a stall that ends with a late packet
        # is still measured from the head to the next forward packet.
        head=rows[0];run=0
        for b in rows[1:]:
            a=head
            seq_delta=((b['seq']-a['seq']+32768)%65536)-32768
            # r11 (Fable r10 N2): the run is scoped to the window like the depth (nonforward arrivals inside it).
            if seq_delta>0:run=0
            elif start<=b['observed_ms']/1000<=end:run+=1;longest_run=max(longest_run,run)
            if seq_delta<=0:
                if a['observed_ms']/1000<=end and b['observed_ms']/1000>=start:
                    disorder.append(dict(pc_id=pc,ssrc=ssrc,at_s=b['observed_ms']/1000,seq=b['seq'],head_seq=a['seq'],
                        seq_delta=seq_delta,kind='duplicate' if seq_delta==0 else 'reorder'))
                continue
            head=b
            left,right=a['observed_ms']/1000,b['observed_ms']/1000
            if left>end or right<start:continue
            category='startup' if left<origin+startup_seconds else 'eligible'
            seq_step=seq_delta
            timestamp_step=max(0,((b['timestamp']-a['timestamp']+2**31)%2**32)-2**31)
            raw_gap=b['observed_ms']-a['observed_ms'];gap=raw_gap
            for window in excluded_windows:
                if left<=window['end_s'] and right>=window['start_s']:
                    if window['category']=='idle':
                        idle,gap_s=idle_interval(left,right,window['start_s'],window['end_s'],
                            seq_step=seq_step,timestamp_step=timestamp_step)
                        category='idle' if idle else 'eligible';gap=gap_s*1000
                        if not idle:continue
                    else:category=window['category'];gap=raw_gap
                    break
            gaps.append(dict(pc_id=pc,ssrc=ssrc,start_s=left,end_s=right,
                gap_ms=gap,raw_gap_ms=raw_gap,category=category,
                seq_step=seq_step,
                timestamp_step=timestamp_step))
        # r10 (Astra r9 #1): a late/duplicate packet after the last forward packet must not certify the suffix.
        # If nonforward rows follow the head and the head arrived before the window end, the interval from the
        # head to the declared end is unresolved: it is measured (net of excluded windows) as an eligible gap.
        head_s=head['observed_ms']/1000;last_s=rows[-1]['observed_ms']/1000
        if rows[-1] is not head and head_s<end and last_s>head_s and last_s>=start:
            # Measured to the last arrival of the stream, capped at the declared end: a late packet past the
            # end covers the endpoint only by charging the whole unmeasured suffix to the gate.
            left=max(head_s,start);right=min(end,last_s)
            overlap=sum(max(0,min(right,w['end_s'])-max(left,w['start_s'])) for w in excluded_windows)
            gap=max(0.0,(right-left)-overlap)*1000
            category=('startup' if left<origin+startup_seconds else 'eligible') if gap>0 else 'excluded'
            gaps.append(dict(pc_id=pc,ssrc=ssrc,start_s=left,end_s=right,gap_ms=gap,raw_gap_ms=(right-left)*1000,
                category=category,seq_step=None,timestamp_step=None,unresolved=True))
    coverage=covered_start and covered_end
    eligible=[g for g in gaps if g['category']=='eligible']
    count=sum(g['gap_ms']>threshold_ms for g in eligible)
    extreme_count=sum(g['gap_ms']>extreme_threshold_ms for g in eligible)
    extreme_rate=extreme_count/len(eligible) if eligible else None
    maximum=max((g['gap_ms'] for g in eligible),default=None)
    rate=count/len(eligible) if eligible else None
    groups={category:dict(intervals=len(group),over60=sum(g['gap_ms']>60 for g in group),
        maximum_ms=max((g['gap_ms'] for g in group),default=None))
        for category in sorted({g['category'] for g in gaps})
        for group in [[g for g in gaps if g['category']==category]]}
    reorders=sum(d['kind']=='reorder' for d in disorder);duplicates=sum(d['kind']=='duplicate' for d in disorder)
    # r10 (Fable r9 N1): a same-stream sequence reset (or a long late run) would leave every later packet
    # nonforward and hide gaps behind it. Fail closed instead: a reorder deeper than RESET_DEPTH packets, or a run
    # of more than RESET_RUN consecutive nonforward packets, makes the verdict False.
    deepest=min((d['seq_delta'] for d in disorder),default=0)
    reset_suspected=deepest<-RESET_DEPTH or longest_run>RESET_RUN
    return dict(covered=coverage,endpoint_coverage=dict(start=covered_start,end=covered_end),intervals=len(eligible),over_threshold=count,tail_rate=rate,
        reorders=reorders,duplicates=duplicates,disorder=disorder[:200],deepest_reorder=deepest,longest_nonforward_run=longest_run,reset_suspected=reset_suspected,
        unresolved_tail_ms=max((g['gap_ms'] for g in gaps if g.get('unresolved')),default=None),
        threshold_ms=threshold_ms,rate_limit=rate_limit,hard_cap_ms=maximum_ms,
        extreme_threshold_ms=extreme_threshold_ms,extreme_rate_limit=extreme_rate_limit,
        over_extreme_threshold=extreme_count,extreme_tail_rate=extreme_rate,
        eligible_maximum_ms=maximum,whole_capture_maximum_ms=max((g['raw_gap_ms'] for g in gaps),default=None),
        startup_seconds=startup_seconds,categories=groups,gaps=gaps,
        passed=(rate<=rate_limit and extreme_rate<=extreme_rate_limit and maximum<=maximum_ms and not reset_suspected) if coverage and eligible else None,
        basis='Adjacent receive intervals within (pc_id, SSRC); first five seconds and explicitly declared windows reported separately. Idle requires containment within 100 ms of acknowledgements and seq_step 1; boundary excess remains eligible; contained loss uses max((seq_step-1)*20, timestamp_step/48) ms. Unexplained steady-state tails remain eligible. Reordered/duplicate packets (signed 16-bit sequence step <= 0 from the stream head) form no interval and are counted separately; a late/duplicate packet after the last forward packet leaves the head-to-end interval unresolved and measured; a suspected sequence reset (reorder deeper than 2048 packets, or more than 64 consecutive nonforward packets inside the window) fails the gate.')


def score_uplink(packets,events,pc_id,start,end,threshold_ms=60):
    rows=[r for r in packets if r['pc_id']==pc_id]
    gate=link_tail(rows,start,end,threshold_ms=threshold_ms)
    clock=[e for e in events if e.get('pc_id')==pc_id and e['event']=='input-clock'
           and e.get('gap_s') is not None and e['t']>=start and e['t']-e['gap_s']<=end
           and e['gap_s']*1000>threshold_ms]
    pipeline_covered=any(e.get('pc_id')==pc_id and e['event']=='input-level' and start<=e['t']<=end for e in events)
    return dict(socket_covered=gate['covered'],socket_max_gap_ms=gate['whole_capture_maximum_ms'],
        socket_gaps_over_threshold=[g for g in gate['gaps'] if g['gap_ms']>threshold_ms],
        pipeline_covered=pipeline_covered,pipeline_gaps_over_threshold=clock,
        pipeline_max_reported_gap_ms=max((e['gap_s']*1000 for e in clock),default=None),
        pipeline_reporting_floor_ms=threshold_ms,link_tail=gate,
        pipeline_rate_verdict=None,pipeline_rate_basis='Sparse input-clock reports have no complete interval denominator',
        passed=gate['passed'],
        damage_basis='RTP arrival link gate only. Sparse input-clock gaps are delivery after aiortc jitter-buffer prefetch, decoding and pipeline scheduling; informational without a complete denominator')
