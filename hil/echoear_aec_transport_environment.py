"""Strict host witnesses for declared link exclusions; no acquisition on import."""
import math
import numpy as np
from pathlib import Path


def select(records, pc_id, lo, hi, field, scale):
    return [r for r in records if r.get('pc_id') == pc_id and
            type(r.get(field)) in (int, float) and lo <= r[field]/scale <= hi]


def uplink_loss(records, pc_id, lo, hi):
    """Unique sequence loss; unwrap/reordering/duplicates, with fixed host-second bins.

    Missing IDs are assigned to the first later received sequence's host second.
    Late packets received anywhere in the window are removed from the missing set.
    Partial endpoint seconds are retained; silence is never zero loss.
    """
    own = sorted(select(records, pc_id, lo, hi, 'observed_ms', 1000), key=lambda r:r['observed_ms'])
    points = {}; high = None; valid = hi > lo and len(own) >= 2
    streams = {r.get('ssrc') for r in own}
    valid &= len(streams) == 1 and None not in streams
    for r in own:
        seq = r.get('seq')
        if type(seq) is not int or not 0 <= seq < 65536:
            valid = False; continue
        extended = seq if high is None else high + ((seq-high+32768) % 65536)-32768
        high = extended if high is None else max(high, extended)
        points.setdefault(extended, r['observed_ms']/1000)
    buckets = {}; missing = 0
    keys = sorted(points)
    for i, seq in enumerate(keys):
        lost = seq-keys[i-1]-1 if i else 0
        missing += lost
        bucket = buckets.setdefault(math.floor(points[seq]), dict(received=0, missing=0))
        bucket['received'] += 1; bucket['missing'] += lost
    for second in range(math.floor(lo), math.floor(hi)+1):
        buckets.setdefault(second, dict(received=0, missing=0))
    values = [dict(second=s, **b, expected=b['received']+b['missing'],
                   loss_fraction=b['missing']/max(1,b['received']+b['missing'])) for s,b in sorted(buckets.items())]
    # A stopped/missing ring or gross sequence ambiguity is not a good link.
    coverage = bool(own and own[0]['observed_ms']/1000-lo <= 1 and hi-own[-1]['observed_ms']/1000 <= 1)
    valid &= coverage and all(b['expected'] > 0 for b in values if lo <= b['second'] and b['second']+1 <= hi)
    return dict(valid=bool(valid),pc_id=pc_id,window_s=[lo,hi],received=len(keys),missing=missing,
                expected=len(keys)+missing,loss_fraction=missing/max(1,len(keys)+missing),
                buckets=values,coverage=coverage,
                bucket_basis='fixed host seconds; missing sequence assigned to next received sequence; window-wide late deduplication')


def unwrap(value, high, modulus):
    return value if high is None else high + ((value-high+modulus//2) % modulus)-modulus//2


def uplink_excursion(records, pc_id, lo, hi, cells=(), export_window=None):
    """Window-local RTP/48kHz delay, unique loss and reorder diagnostics.

    Host-second bins are floor(observed_ms/1000). A qualifying run contains at
    least 25 >100ms packets in a sliding, inclusive five-second span within that
    run. Gaps include endpoint coverage separately so a truncated/stopped tail
    cannot masquerade as a healthy ring. Sequence/timestamp wraps are unwrapped
    in arrival order, retaining late arrivals and duplicates in delay statistics.
    """
    own=sorted(select(records,pc_id,lo,hi,'observed_ms',1000),key=lambda r:r['observed_ms'])
    loss=uplink_loss(own,pc_id,lo,hi)
    valid=loss['valid'];timestamps=[];seqs=[];depths=[];thigh=shigh=None
    for r in own:
        ts,seq=r.get('timestamp'),r.get('seq')
        if type(ts) is not int or not 0<=ts<2**32 or type(seq) is not int or not 0<=seq<65536:
            valid=False;break
        t=unwrap(ts,thigh,2**32);q=unwrap(seq,shigh,65536)
        depths.append(max(0,(shigh if shigh is not None else q)-q))
        timestamps.append(t);seqs.append(q)
        thigh=t if thigh is None else max(thigh,t);shigh=q if shigh is None else max(shigh,q)
    result=dict(valid=bool(valid),pc_id=pc_id,window_s=[lo,hi],unique_missing=loss['missing'],
        unique_loss_fraction=loss['loss_fraction'],unique_received=loss['received'],expected=loss['expected'],
        max_reorder_depth=max(depths,default=0),naive_forward_gap_sum=sum(max(0,b-a-1) for a,b in zip(seqs,seqs[1:])),
        excursion_seconds=0,packets_over_100ms_5s_max=0,excursion_count=0,excursions=[],overlaps_cell=False,
        max_delay_ms=None,delay_p50_ms=None,delay_p99_ms=None,baseline_ms=None,per_second=[],
        max_gap_ms=None,stall=True,passed=False,
        baseline_basis='5th percentile of observed_ms - unwrapped RTP timestamp / 48 within this window',
        bucket_basis='fixed integer host seconds; sliding inclusive 5s span inside each consecutive excursion run')
    if not own or len(timestamps)!=len(own):return result
    arrival=np.array([r['observed_ms'] for r in own],dtype=float)
    gaps=np.diff(arrival);max_gap=float(gaps.max()) if len(gaps) else 0.
    edge=max(arrival[0]-lo*1000,hi*1000-arrival[-1])
    offset=arrival-np.array(timestamps,dtype=float)/48
    baseline=float(np.percentile(offset,5));delays=offset-baseline
    seconds={}
    for t,d,depth in zip(arrival,delays,depths):
        bucket=seconds.setdefault(math.floor(t/1000),dict(max_delay_ms=float('-inf'),packets_over_100ms=0,max_reorder_depth=0))
        bucket['max_delay_ms']=max(bucket['max_delay_ms'],float(d))
        bucket['packets_over_100ms']+=int(d>100);bucket['max_reorder_depth']=max(bucket['max_reorder_depth'],depth)
    def densest(times):
        left=0;best=0;span=None
        for right,t in enumerate(times):
            while t-times[left]>5000:left+=1
            if right-left+1>best:best=right-left+1;span=[float(times[left]/1000),float(t/1000)]
        return best,span
    all_count,_=densest(arrival[delays>100])
    runs=[]
    for second,bucket in sorted(seconds.items()):
        if bucket['max_delay_ms']<120:continue
        if runs and runs[-1][-1]+1==second:runs[-1].append(second)
        else:runs.append([second])
    excursions=[]
    for run in runs:
        if len(run)<3:continue
        start,end=run[0],run[-1]+1
        times=arrival[(arrival>=start*1000)&(arrival<end*1000)&(delays>100)]
        count,span=densest(times)
        if count<25:continue
        overlap=[c for c in cells if start<c['end_ns']/1e9 and end>c['start_ns']/1e9]
        excursions.append(dict(start_s=start,end_s=end,seconds=len(run),packets_over_100ms_5s_max=count,
            densest_span_s=span,overlapping_cells=[c.get('run_id',c.get('id')) for c in overlap],
            during_export=(bool(start<export_window[1] and end>export_window[0]) if export_window is not None else None)))
    stall=bool(max(max_gap,edge)>=1000)
    overlaps=any(e['overlapping_cells'] for e in excursions)
    result.update(baseline_ms=baseline,max_delay_ms=float(delays.max()),delay_p50_ms=float(np.percentile(delays,50)),
        delay_p99_ms=float(np.percentile(delays,99)),max_gap_ms=max_gap,max_endpoint_gap_ms=float(edge),stall=stall,
        per_second=[dict(second=k,**v) for k,v in sorted(seconds.items())],excursions=excursions,
        excursion_count=len(excursions),excursion_seconds=max((e['seconds'] for e in excursions),default=0),
        packets_over_100ms_5s_max=all_count,overlaps_cell=overlaps,passed=bool(valid and not stall and len(excursions)>=1))
    return result


def reconnect_witness(events, pc_id, window, cells=()):
    """Completed proactive cycles may occupy queued playback after TTS stops.

    Reply windows use the existing context generation start/stop lifecycle, not
    the downstream BotStopped notification (which includes buffered PCM). Near
    stimuli use their actual Jabra request/duration. Cell start/end remains the
    wider reply-overlap diagnostic for the independent link witness.
    """
    lo,hi=window
    scoped=[e for e in events if e.get('pc_id')==pc_id]
    valid_time=lambda e:type(e.get('t')) in (int,float) and math.isfinite(e['t'])
    timed=[e for e in scoped if valid_time(e)]
    # Retained replay cannot use a later ready/stop to cure a live missing witness.
    own=sorted([e for e in timed if e['t']<=hi],key=lambda e:e['t'])
    problems=[] if pc_id is not None and len(timed)==len(scoped) else ['missing peer identity/event timestamp']
    active=[];cycles=[];pending=None
    for e in own:
        if e['event']=='tts-generation-metric-start':
            stop=[x for x in own if x.get('context_id')==e.get('context_id') and x['t']>=e['t'] and x['event'] in ('tts-generation-stopped','tts-interruption-stopped')]
            end=stop[0]['t'] if len(stop)==1 else hi
            if e['t']<hi and end>lo:
                if len(stop)!=1:problems.append('missing/duplicate reply generation stop')
                active.append(dict(kind='reply-generation',start_s=e['t'],end_s=end,context_id=e.get('context_id')))
        if e['event']=='tts-reconnect-start':
            if pending is not None and e['t']>=lo and pending['t']<=hi:problems.append('overlapping reconnect cycles')
            pending=e
        elif e['event'] in ('tts-reconnect-ready','tts-reconnect-failed','tts-reconnect-aborted'):
            if pending is None:
                if lo<=e['t']<=hi:problems.append('reconnect endpoint without start')
            else:
                if pending['t']<=hi and e['t']>=lo:
                    cycles.append(dict(start_s=pending['t'],end_s=e['t'],completed=e['event']=='tts-reconnect-ready'))
                pending=None
    if pending is not None and pending['t']<=hi:problems.append('unfinished reconnect')
    for c in cells:
        j=c.get('jabra')
        if j:
            begin=j.get('request_monotonic_s');duration=j.get('elapsed_seconds')
            if type(begin) not in (int,float) or type(duration) not in (int,float):problems.append('missing stimulus interval')
            else:active.append(dict(kind='stimulus',start_s=begin,end_s=begin+duration,cell_id=c.get('run_id')))
    failed=[e for e in own if lo<=e['t']<=hi and e['event'] in ('tts-reconnect-refused','tts-reconnect-failed')]
    for cycle in cycles:
        cycle['overlaps']=[w for w in active if cycle['start_s']<w['end_s'] and cycle['end_s']>w['start_s']]
        if cycle['overlaps']:problems.append('reconnect overlaps stimulus/reply generation')
        if not cycle['completed']:problems.append('reconnect did not complete')
    return dict(passed=not problems and not failed,cycles=cycles,active_intervals=active,problems=problems,failed=failed,
        reply_basis='tts-generation-metric-start through matching tts-generation-stopped/tts-interruption-stopped',
        raw_lifecycle_events=[e for e in own if e['event'].startswith(('tts-reconnect','tts-generation','tts-interruption-stopped'))])


def link_probe(records, pc_id, lo, hi):
    link=uplink_excursion(records,pc_id,lo,hi)
    passed=hi-lo>=240 and link['valid'] and link['unique_loss_fraction']<=.005 and not link['stall']
    return dict(passed=bool(passed),duration_s=hi-lo,uplink=link,
                result='LINK PROBE PASSED' if passed else 'LINK PROBE FAILED, retry later')


def observer_healthy(events):
    health = [e for e in events if e.get('event')=='rtp-observer-health']
    return bool(health) and all(e.get('passed') and e.get('sctp',{}).get('supported') and
        e['sctp'].get('flight_fix_enabled') and e['sctp'].get('records',0)>0 and
        not e['sctp'].get('truncated',True) and not e['sctp'].get('unavailable',True) for e in health)


def classify(before, after, checks, pre_rf, events, downlink, sctp, uplink, window, cells=(), attempt_timing=None, *, device_alive_rule=None, recorded=(), arrivals=()):
    """All declared conditions must pass; retain raw inputs even on refusal."""
    from echoear_aec_campaign_analysis import delivery_counters
    from echoear_aec_resources import liveness_reply
    lo,hi = window; pc = before.get('pc_id')
    own = select(events,pc,lo,hi,'t',1)
    down = sorted(select(downlink,pc,lo,hi,'send_ms',1000),key=lambda r:r['send_ms'])
    chunks = select(sctp,pc,lo,hi,'monotonic_ns',1e9)
    up = select(uplink,pc,lo,hi,'observed_ms',1000)
    live = liveness_reply(before,after)
    prohibited = [e for e in own if e.get('frame') in ('ErrorFrame','FatalErrorFrame') or
                  e.get('event') in ('liveness-alarm','evaluation-error')]
    reconnects=reconnect_witness(events,pc,window,cells)
    def delta(path):
        a,b = before,after
        for key in path:
            a=a.get(key) if isinstance(a,dict) else None
            b=b.get(key) if isinstance(b,dict) else None
        return b-a if type(a) is int and type(b) is int and b>=a else None
    playout = {k:delta(('playout',k)) for k in ('post_plc_zeros','starvation','missing_slots')}
    playout['late'] = delta(('late',))
    playout['late_to_missing_ratio'] = (playout['late']/playout['missing_slots']
        if playout['late'] is not None and playout['missing_slots'] else None)
    consecutive = bool(len(down)>=2 and len({r.get('ssrc') for r in down})==1 and
        all(type(r.get('seq')) is int for r in down) and
        all((b['seq']-a['seq'])%65536==1 for a,b in zip(down,down[1:])))
    max_gap = max((b['send_ms']-a['send_ms'] for a,b in zip(down,down[1:])),default=None)
    down_coverage = bool(down and down[0]['send_ms']/1000-lo<=.060 and hi-down[-1]['send_ms']/1000<=.060)
    bad_sctp = [e for e in chunks if e.get('event')=='t3-expired' or e.get('chunk_type')=='AbortChunk']
    timing=attempt_timing or {}
    end=timing.get('capture_end_s');after_s=timing.get('after_boundary_s')
    export_window=([end,after_s] if type(end) in (int,float) and type(after_s) in (int,float) and lo<=end<=after_s<=hi else None)
    link = uplink_excursion(up,pc,lo,hi,cells,export_window)
    link['export_window_s']=export_window
    delivery = delivery_counters(before,after)
    i2s = {k:[before.get(k),after.get(k)] for k in ('i2s_underflows','i2s_write_errors','i2s_write_timeouts')}
    conditions = dict(
        only_transport_failed=checks.get('resources',{}).get('all_passed') is True and
            all(checks.get(k,{}).get('passed') is True for k in ('admission','rf','delivery')) and
            checks.get('transport',{}).get('passed') is False and delivery['passed'] and
            all(v==[0,0] for v in i2s.values()) and observer_healthy(own),
        liveness_intact=live['passed'] and not prohibited and reconnects['passed'],
        device_alive=all(playout[k]==0 for k in ('post_plc_zeros','starvation')) and
            all(playout[k] is not None for k in ('late','missing_slots')),
        server_pacing=consecutive and max_gap is not None and max_gap<=60 and down_coverage and bool(chunks) and not bad_sctp,
        independent_link=link['passed'],
        both_rf_gates=pre_rf.get('passed') is True and checks.get('rf',{}).get('passed') is True)
    zero_evidence=None
    if device_alive_rule is not None:
        if device_alive_rule!='f1b-v1':raise ValueError('Unknown device-alive rule')
        from echoear_aec_f1b import zero_witness
        zero_evidence=zero_witness(before,after,playout,recorded,arrivals,window)
        conditions['device_alive']=(playout['starvation']==0 and conditions['liveness_intact'] and
            checks.get('resources',{}).get('all_passed') is True and
            all(playout[k] is not None for k in ('late','missing_slots')))
        conditions['post_plc_zero_witness']=zero_evidence['passed']
    return dict(passed=all(conditions.values()),conditions=conditions,window_s=window,
        **(dict(device_alive_rule=device_alive_rule,post_plc_zero_witness=zero_evidence) if device_alive_rule else {}),
        liveness=live,prohibited_events=prohibited,reconnects=reconnects,playout=playout,delivery=delivery,i2s=i2s,
        server=dict(contiguous=consecutive,max_gap_ms=max_gap,coverage=down_coverage,sctp_bad=bad_sctp),uplink=link,
        raw=dict(before=before,after=after,checks=checks,pre_rf=pre_rf,events=own,downlink=down,sctp=chunks,uplink=up,cells=cells,attempt_timing=timing))


def probe_epoch_scope(root):
    """Re-evaluate raw probe packets, never trust a saved PASS to waive loss.

    Called only for declarations explicitly adopting probe-own-rule-v1. Older
    frozen declarations keep their original epoch interpretation.
    """
    import json
    from echoear_aec_accept import rows
    root=Path(root);folder=root/'link-probe'
    read=lambda name:json.loads((folder/name).read_text())
    before,after,saved=read('before.json'),read('after.json'),read('result.json')
    verdict=link_probe(rows(root/'rtp/socket-rtp.jsonl'),before['pc_id'],*saved['uplink']['window_s'])
    if before['pc_id']!=after['pc_id'] or before['generation']!=after['generation']:
        raise ValueError('Probe crosses transport epoch')
    return dict(pc_id=before['pc_id'],generation=before['generation'],
                before_device_us=before['device_us'],after_device_us=after['device_us'],
                verdict=verdict)


def piecewise_transport(values, failures, probe=None, policy=None):
    from echoear_aec_campaign_analysis import transport
    values = sorted({v['device_us']:v for v in values}.values(),key=lambda v:v['device_us'])
    spans = [f['excluded_transport_interval'] for f in failures
             if f.get('kind')=='transport-environmental' and f.get('retry_safe') and f.get('witnesses',{}).get('passed')]
    intervals=[]
    probe_seen=False
    for a,b in zip(values,values[1:]):
        skipped=any(s['pc_id']==a['pc_id']==b['pc_id'] and s['generation']==a['generation']==b['generation'] and
                    s['before_device_us']<=a['device_us'] and b['device_us']<=s['after_device_us'] for s in spans)
        row=dict(before_device_us=a['device_us'],after_device_us=b['device_us'],
                 excluded_environmental=skipped,**transport(a,b,policy))
        if probe and probe['pc_id']==a['pc_id']==b['pc_id'] and probe['generation']==a['generation']==b['generation'] and probe['before_device_us']<=a['device_us']<b['device_us']<=probe['after_device_us']:
            probe_seen=True
            row.update(rule='probe-own-rule-v1',per_cell_diagnostic=transport(a,b,policy),
                       passed=probe['verdict']['passed'],excluded_environmental=False)
        intervals.append(row)
    probe_covered=(not probe or probe_seen and
                   any(v['device_us']==probe['before_device_us'] for v in values) and
                   any(v['device_us']==probe['after_device_us'] for v in values))
    result=dict(passed=bool(intervals) and probe_covered and all(x['passed'] for x in intervals if not x['excluded_environmental']),
                intervals=intervals,raw_first_to_last=transport(values[0],values[-1],policy))
    if probe:result.update(probe=probe,probe_covered=probe_covered)
    return result


def export_load_hypothesis(failures):
    """Report-only signal for re-examination, never an acquisition/score gate."""
    environmental=[f for f in failures if f.get('kind')=='transport-environmental']
    rows=[]
    for f in environmental:
        excursions=f.get('witnesses',{}).get('uplink',{}).get('excursions',[])
        flags=[e.get('during_export') for e in excursions]
        rows.append(dict(id=f.get('id'),attempt=f.get('attempt'),during_export=any(v is True for v in flags),
            export_only=bool(flags) and all(v is True for v in flags),
            timing_unknown=not flags or any(v is None for v in flags)))
    all_export=bool(rows) and all(r['during_export'] for r in rows)
    return dict(report_only=True,environmental_attempts=len(rows),
        attempts_with_export_excursion=sum(r['during_export'] for r in rows),
        attempts_export_only=sum(r['export_only'] for r in rows),
        attempts_with_unknown_export_timing=sum(r['timing_unknown'] for r in rows),
        all_environmental_attempts_during_export=all_export,attempts=rows,
        prominent_warning=('EXPORT-LOAD HYPOTHESIS: every environmental attempt includes an export-tail excursion; re-examine the environmental class before further use.' if all_export else None))


def environmental_summary(root, saved):
    from echoear_aec_accept import rows
    failures=[f for f in saved.get('harness_failures',[]) if f['kind']=='transport-environmental']
    attempts=len({p.parent for name in ('capture-duration.json','acquisition-failure.json') for p in Path(root).rglob(name)})
    records=rows(Path(root)/'rtp/socket-rtp.jsonl');links=[]
    for pc in sorted({r['pc_id'] for r in records}):
        own=[r for r in records if r['pc_id']==pc]
        links.append(uplink_loss(own,pc,min(r['observed_ms'] for r in own)/1000,max(r['observed_ms'] for r in own)/1000))
    return dict(export_load_hypothesis=export_load_hypothesis(failures),environmental_attempts=len(failures),capture_attempts=attempts,
                environmental_fault_rate=len(failures)/attempts if attempts else None,
                attempts=failures,hold_uplink=links)
