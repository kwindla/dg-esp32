"""Control-arm covariates from complete traces; no image-causal inference."""
from collections import defaultdict
from uplink_continuity import idle_interval
import json
from pathlib import Path
from step8e0_analysis import trace_rows, wifi_counters
from step8e2a_analysis import receive_audit


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def quantiles(values):
    values=sorted(values)
    return dict(n=len(values),p50=values[int((len(values)-1)*.5)],
                p90=values[int((len(values)-1)*.9)],p99=values[int((len(values)-1)*.99)],
                maximum=values[-1]) if values else dict(n=0,p50=None,p90=None,p99=None,maximum=None)


def gap_summary(streams, clock, *, expected_ms=20, join_ms=5000):
    gaps=[]
    for stream in streams.values():
        ordered=sorted(stream,key=clock)
        for a,b in zip(ordered,ordered[1:]):
            gaps.append(dict(at_ms=clock(b),gap_ms=clock(b)-clock(a)))
    slow=sorted((r for r in gaps if r['gap_ms']>100),key=lambda r:r['at_ms'])
    episodes=[]
    for r in slow:
        if not episodes or r['at_ms']-episodes[-1]['last_ms']>join_ms:
            episodes.append(dict(first_ms=r['at_ms'],last_ms=r['at_ms'],count=1,
                                 maximum_gap_ms=r['gap_ms']))
        else:
            e=episodes[-1];e['last_ms']=r['at_ms'];e['count']+=1
            e['maximum_gap_ms']=max(e['maximum_gap_ms'],r['gap_ms'])
    return dict(gaps_ms=quantiles(r['gap_ms'] for r in gaps),
                abs_gap_minus20_ms=quantiles(abs(r['gap_ms']-expected_ms) for r in gaps),
                count_over60ms=sum(r['gap_ms']>60 for r in gaps),count_over100ms=len(slow),
                episodes_over100ms=len(episodes),episode_join_ms=join_ms,episodes=episodes)


def summarize_control(path):
    path=Path(path);d=path/'webrtc-ethernet'
    snapshots=json.loads((d/'cpu-snapshots.json').read_text());first,last=snapshots[0],snapshots[-1]
    trace=trace_rows(d/'trace-serial.log');start,end=first['device_us'],last['device_us']
    active=[r for r in trace if start<=r['device_us']<=end]
    wire=defaultdict(list)
    for r in active:
        if r['kind']==1:wire[(r['reserved'],r['ssrc'])].append(r)
    socket=gap_summary(wire,lambda r:r['device_us']/1000)
    # Use the same PERF bracket, with the recorded host timestamps. Separate
    # sources/sessions so reconnect and startup are never artificial RTP gaps.
    host_start=first['host_after_ns']/1e6;host_end=last['host_before_ns']/1e6
    uplink=defaultdict(list)
    for r in rows(d/'rtp/rtp.jsonl'):
        if host_start<=r['observed_ms']<=host_end:
            uplink[(r['pc_id'],r['ssrc'])].append(r)
    audit=receive_audit(trace,rows(d/'rtp/downlink.jsonl'),start,end,tc_model=['loss','0%'])
    ingress={}
    for r in trace:
        if r['kind']==13:
            ingress.setdefault((r['reserved'],r['ssrc'],r['timestamp']),r['device_us'])
    minute_residence=defaultdict(list)
    for r in active:
        key=(r['reserved'],r['ssrc'],r['timestamp'])
        if r['kind']==14 and r['flags']==0 and key in ingress:
            minute_residence[int((r['device_us']-start)//60000000)].append(
                (r['device_us']-ingress[key])/1000)
    same=first['generation']==last['generation'] and first['pc_id']==last['pc_id']
    if not same:raise ValueError('Control changed sessions; counter deltas unavailable')
    delta=lambda k:last['playout'][k]-first['playout'][k]
    probes=json.loads((d/'analysis.json').read_text())
    probe_summary={n:{direction:dict(stalls=r[direction]['stalls']['stall_count'],
            episodes=r[direction]['stalls']['episode_count'],
            maximum_gap_ms=r[direction]['longest_gap_ms']) for direction in ('uplink','downlink')}
        for n,r in probes.items()}
    wifi=wifi_counters(d)
    samples=json.loads((d/'device-samples.json').read_text())
    rssi=[r['stats']['wifi']['rssi_dbm'] for r in samples if r['stats'].get('wifi',{}).get('rssi_dbm') is not None]
    slow=[r for r in active if r['kind']==8]
    timing={}
    for name,kind,aux in [('main_loop',11,0),('loop_iteration',8,None)]:
        events=[r['value']/1000 for r in active if r['kind']==kind and (aux is None or r['aux']==aux)]
        n=last['peer_profile'][name]['calls']-first['peer_profile'][name]['calls']
        timing[name]=dict(calls=n,active_calls_over40ms=len(events),
            active_max_ms=max(events) if events else None,
            active_max_upper_bound_ms=max(events) if events else 40,
            active_p99_upper_bound_ms=40 if len(events)<=n*.01 else
                sorted(events)[min(len(events)-1,max(0,int(n*.99)-(n-len(events))))],
            session_cumulative_max_ms=last['peer_profile'][name]['max_us']/1000,
            session_cumulative_p95_bucket_upper_ms=last['peer_profile'][name]['p95_us']/1000,
            p99_basis='Calls >=40 ms traced; sub-threshold p99 censored at <=40 ms. Firmware exports p50/p95, not p99/bins. Setup cumulative maxima reported separately.')
    label=path.name
    gate_path=path.parent/(label+'-quiet-nic.json')
    result=dict(label=label,image='8c3' if label.startswith('A') else '8e2a2',
        elf_sha256=json.loads((path/'identity.json').read_text())['elf_sha256'],
        capture_passed=json.loads((path/'result.json').read_text())['passed'],
        quiet_nic=json.loads(gate_path.read_text()) if gate_path.exists() else None,
        probe_capture_seconds=300,perf_bracket_seconds=(end-start)/1e6,
        missing=delta('missing_slots'),late=last['late']-first['late'],plc=delta('plc_calls'),
        post_plc_zeros=delta('post_plc_zeros'),starvation=delta('starvation'),
        drift_insert=delta('drift_insert'),drift_drop=delta('drift_drop'),
        socket_read=socket,server_observed_uplink=gap_summary(uplink,lambda r:r['observed_ms']),
        residence_ms=audit['ingress_to_render_completion_ms'],
        residence_by_minute_ms={str(k):quantiles(v) for k,v in minute_residence.items()},missing_attribution=audit['missing_attribution'],
        auth_to_ingress_ms=audit['auth_to_ingress_ms'],slowloop_count=len(slow),owner_timing=timing,
        fail_fc={k:v['fail_fc'] for k,v in wifi.get('buffer_deltas',{}).items()},
        rssi_dbm=dict(minimum=min(rssi) if rssi else None,maximum=max(rssi) if rssi else None,n=len(rssi)),
        probe_stalls=probe_summary,trace_overwritten=last['peer_profile']['trace']['overwritten'],
        trace_decoded_records=len(trace),same_session=same)
    (path/'control-covariates.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def campaign(path):
    path=Path(path);result=[]
    for n in range(1,5):
        for arm in 'AB':
            p=path/f'{arm}{n}'
            if (p/'result.json').exists():result.append(summarize_control(p))
    (path/'control-covariates.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def concealment_facts(trace,sent,before,after):
    """Identity/run evidence for one continuous schema-2 reply bracket."""
    first,last=before.get('device_us'),after.get('device_us')
    same=before.get('generation')==after.get('generation') and before.get('pc_id')==after.get('pc_id')
    if first is None or last is None or not same:
        return dict(coverage_complete=False,reason='Missing bracket or changed session')
    selected=sorted((r for r in trace if r['kind']==14 and first<=r['device_us']<=last),key=lambda r:r['device_us'])
    ingress={(r['reserved'],r['ssrc'],r['timestamp']) for r in trace if r['kind']==13}
    wire={(r['reserved'],r['ssrc'],r['timestamp']) for r in trace if r['kind']==1}
    missing=[r for r in selected if r['flags'] in (1,2)]
    run=maximum=0;previous=None;starves=[]
    for r in selected:
        contiguous=previous is not None and (r['reserved'],r['ssrc'],r['aux'])==(previous['reserved'],previous['ssrc'],previous['aux']) and (r['timestamp']-previous['timestamp'])%2**32==960
        run=run+1 if r['flags'] in (1,2) and contiguous else int(r['flags'] in (1,2))
        maximum=max(maximum,run)
        if r['flags']==7 and previous is not None and previous['flags'] in (1,2):starves.append(r)
        previous=r
    sources=defaultdict(list)
    for r in selected:
        if r['flags'] in (0,1,2,3):sources[(r['reserved'],r['ssrc'])].append(r)
    absent_runs=[];packet_count=0;starvation_without_gap=0;starvation_coverage=True
    for (tag,ssrc),renders in sources.items():
        origin=renders[0]['timestamp']
        extend=lambda t:(t-origin+2**31)%2**32-2**31
        low=min(extend(r['timestamp']) for r in renders);high=max(extend(r['timestamp']) for r in renders)
        packets={}
        for r in sent:
            if r['pc_id']==before.get('pc_id') and r['ssrc']==ssrc and low<=extend(r['timestamp'])<=high:
                packets.setdefault((r['seq'],r['timestamp']),r)
        ordered=sorted(packets.values(),key=lambda r:extend(r['timestamp']));packet_count+=len(ordered)
        n=0;prev=None
        for r in ordered:
            absent=(tag,ssrc,r['timestamp']) not in wire
            contiguous=prev is not None and (r['timestamp']-prev['timestamp'])%2**32==960
            n=n+1 if absent and contiguous else int(absent);absent_runs.append(n);prev=r
        times=sorted(r['device_us'] for r in trace if r['kind']==1 and r['reserved']==tag and r['ssrc']==ssrc)
        for r in (r for r in starves if (r['reserved'],r['ssrc'])==(tag,ssrc)):
            left=max((t for t in times if t<=r['device_us']),default=None)
            right=min((t for t in times if t>=r['device_us']),default=None)
            if left is None or right is None:starvation_coverage=False
            elif right-left<500000:starvation_without_gap+=1
    a,b=before.get('playout',{}),after.get('playout',{})
    counters=all(k in a and k in b for k in ('missing_slots','starvation'))
    covered=(before.get('playout_schema')==after.get('playout_schema')==2 and bool(selected) and counters and
        abs((b['missing_slots']-a['missing_slots'])-len(missing))<=1 and
        (b['starvation']-a['starvation'])==len(starves) and starvation_coverage and
        after.get('peer_profile',{}).get('trace',{}).get('overwritten')==0)
    return dict(coverage_complete=covered,reply_packets=packet_count,
        absent_at_ingress=sum((r['reserved'],r['ssrc'],r['timestamp']) not in ingress for r in missing),
        longest_concealment_run=maximum,longest_wire_absent_run=max(absent_runs,default=0),
        starvation_without_wire_gap=starvation_without_gap if starvation_coverage else None,
        observed_starvation_entries=len(starves),missing_render_decisions=len(missing),
        basis='Same PERF bracket and full RTP timestamp/source. Explicit Prefill excluded from concealment; absent run uses unique first-send identities. Incomplete starvation endpoints or retired sessions are unknown.')


def link_backpressure_windows(trace=(),snapshots=()):
    """Declared device evidence only; never infer this from host arrival gaps."""
    windows=[]
    for r in trace:
        # PeerPart IDs are stable: send_audio=1, control=3, dtls_write=10.
        # Firmware emits these only with call-timing=1 and at >=40 ms.
        # control also includes local event/heap work, not purely link cost.
        if r['kind']==11 and r.get('aux') in (1,3,10) and r['value']>=40000:
            windows.append(dict(start_us=r['device_us']-r['value'],end_us=r['device_us'],
                category='link-backpressure',evidence='kind-11 SlowCall',part=r['aux'],duration_us=r['value']))
    samples=sorted((r.get('perf',r) for r in snapshots),key=lambda r:r['device_us'])
    for a,b in zip(samples,samples[1:]):
        if a.get('generation') is None or a.get('generation')!=b.get('generation'):continue
        if a.get('pc_id')!=b.get('pc_id'):continue
        evidence={}
        for name,group in (('socket_send_enomem','peer_profile'),('send_cutoff','drop_reasons'),('send_budget_cutoffs',None)):
            x,y=(a.get(group,{}).get(name),b.get(group,{}).get(name)) if group else (a.get(name),b.get(name))
            if isinstance(x,int) and isinstance(y,int) and y>x:evidence[name]=y-x
        if not evidence:continue
        for r in trace:
            # Transport DropReason::SendCutoff=10, flags=1; value is the reason.
            if (r['kind']==7 and r.get('flags')==1 and r.get('value')==10
                    and a['device_us']<=r['device_us']<=b['device_us']
                    and r.get('reserved')==a['generation']%65536):
                windows.append(dict(start_us=r['device_us']-250000,end_us=r['device_us']+250000,
                    category='link-backpressure',evidence=evidence,
                    record='kind-7 send_cutoff',device_us=r['device_us'],pad_us=250000))
    return windows


def overlaps(start,end,windows):
    return any(start<=w['end_us'] and end>=w['start_us'] for w in windows)


def span_seconds(intervals):
    """Union duration, so overlapping records cannot inflate excused time."""
    end=None;total=0
    for first,last in sorted(intervals):
        total+=max(0,last-max(first,end if end is not None else first))
        end=max(last,end if end is not None else last)
    return total/1e6


def device_send_cadence(trace,start_us,end_us,excluded_windows=(),console_reads=(),target_p99_ms=45,*,snapshots=()):
    instrumentation=[dict(start_us=t-120000,end_us=t+120000,category='console') for t in console_reads]
    windows=list(excluded_windows)+instrumentation+link_backpressure_windows(trace,snapshots)
    informational=[w for w in windows if w.get('category')=='console']
    declared=[w for w in windows if w.get('category')!='console']
    streams=defaultdict(list)
    for r in trace:
        if r['kind']==12 and start_us<=r['device_us']<=end_us:
            streams[(r['reserved'],r['ssrc'])].append(r)
    eligible=[];excluded=[];backpressure=[];console=[];idle=[]
    for stream in streams.values():
        ordered=sorted(stream,key=lambda r:r['device_us'])
        for a,b in zip(ordered,ordered[1:]):
            item=dict(before_us=a['device_us'],after_us=b['device_us'],gap_ms=(b['device_us']-a['device_us'])/1000)
            item['raw_gap_ms']=item['gap_ms']
            if 'seq' in a and 'seq' in b:item['seq_step']=(b['seq']-a['seq'])%65536
            if 'timestamp' in a and 'timestamp' in b:item['timestamp_step']=(b['timestamp']-a['timestamp'])%2**32
            if overlaps(a['device_us'],b['device_us'],informational):console.append(item)
            matching=[]
            for w in declared:
                if not overlaps(a['device_us'],b['device_us'],[w]):continue
                if w['category']=='idle':
                    contained,gap_s=idle_interval(a['device_us']/1e6,b['device_us']/1e6,
                        w['start_us']/1e6,w['end_us']/1e6,seq_step=item.get('seq_step'),
                        timestamp_step=item.get('timestamp_step'))
                    if not contained:
                        item['gap_ms']=gap_s*1000
                        continue
                matching.append(w)
            if matching:
                item['gap_ms']=item['raw_gap_ms']
                item['categories']=sorted({w['category'] for w in matching});excluded.append(item)
                if 'link-backpressure' in item['categories']:backpressure.append(item)
                if 'idle' in item['categories']:idle.append(item)
            else:eligible.append(item)
    raw=eligible+excluded;healthy=quantiles(r['gap_ms'] for r in eligible)
    return dict(gaps_ms=healthy,raw_gaps_ms=quantiles(r['raw_gap_ms'] for r in raw),
        raw_over60ms=sum(r['raw_gap_ms']>60 for r in raw),declared_windows=declared,
        instrumentation_windows=informational,console_informational=dict(count=len(console),gaps_ms=quantiles(r['gap_ms'] for r in console)),
        link_backpressure=dict(count=len(backpressure),excused_span_s=span_seconds((r['before_us'],r['after_us']) for r in backpressure),gaps_ms=quantiles(r['gap_ms'] for r in backpressure),intervals=backpressure),
        idle=dict(count=len(idle),excused_span_s=span_seconds((r['before_us'],r['after_us']) for r in idle),gaps_ms=quantiles(r['gap_ms'] for r in idle)),
        target_p99_ms=target_p99_ms,
        over40ms=sum(r['gap_ms']>40 for r in eligible),over60ms=sum(r['gap_ms']>60 for r in eligible),
        target_passed=healthy['p99']<=target_p99_ms if eligible else None,
        hard_ceiling_passed=all(r['gap_ms']<=60 for r in eligible) if eligible else None,
        excluded=excluded,basis='Raw successful RTP sends primary. Device-evidenced link-backpressure and declared fault/idle/reconnect separate; idle requires containment within 100 ms of acknowledgements and seq_step 1 when recorded; boundary excess remains eligible; contained loss uses max((seq_step-1)*20, timestamp_step/48) ms (sequence estimate alone without timestamps); stats/wifi-stats windows informational only.')


def owner_gate(slow,calls,*,sustained=False,covered=True,trace=(),snapshots=(),excluded_windows=()):
    windows=list(excluded_windows)+link_backpressure_windows(trace,snapshots)
    ordinary=[];excluded=[];backpressure=[]
    for r in slow:
        end=r.get('device_us',0);start=end-r['value']
        categories=sorted({w['category'] for w in windows if w.get('category')!='console' and overlaps(start,end,[w])})
        if categories:
            item=dict(r,categories=categories);excluded.append(item)
            if 'link-backpressure' in categories:backpressure.append(item)
        else:ordinary.append(r)
    over40=sum(r['value']>40000 for r in ordinary);over60=sum(r['value']>60000 for r in ordinary)
    rate=over40/calls if calls else None
    return dict(calls=calls,over40=over40,over60=over60,rate_over40=rate,
        maximum_ms=max((r['value']/1000 for r in ordinary),default=None),
        raw_over40=sum(r['value']>40000 for r in slow),raw_over60=sum(r['value']>60000 for r in slow),
        raw_maximum_ms=max((r['value']/1000 for r in slow),default=0),excluded=excluded,
        link_backpressure=dict(count=len(backpressure),excused_span_s=span_seconds((r['device_us']-r['value'],r['device_us']) for r in backpressure),maximum_ms=max((r['value']/1000 for r in backpressure),default=0),intervals=backpressure),
        maximum_basis='SlowLoop records only; with no ordinary record the maximum is bounded by 40 ms, not measured as zero',declared_windows=windows,rate_limit=.001 if sustained else 0,
        passed=(rate<=.001 and over60==0 if sustained else over40==0) if covered and calls else None)


def lcd_memory_gate(snapshots,*,reconnects=None):
    limits=dict(session_window_internal=96*1024,boot_minimum_internal=80*1024,
                largest_internal=32*1024)
    snapshots=[s.get('perf',s) for s in snapshots]
    coverage=True;previous_end=None
    for s in snapshots:
        w=s.get('heap_window',{})
        first,last=w.get('start_us'),w.get('end_us')
        if w.get('schema')!=1 or not isinstance(first,int) or not isinstance(last,int) or last<first:
            coverage=False;continue
        if previous_end is not None and last>=previous_end and first!=previous_end:coverage=False
        previous_end=last
    def minimum(cap,key):
        values=[]
        for s in snapshots:
            window=s.get('heap_window',{})
            row=s.get('heap',{}).get(cap,{})
            if key=='window_session_minimum_free':
                if 'session_samples' in window and window['session_samples']==0:continue
                value=row.get(key,row.get('window_minimum_free'))
            else:
                if key=='sampled_largest_minimum' and row.get(key) is None and window.get('samples')==0:continue
                value=row.get(key)
            values.append(value)
        return min(values) if values and all(isinstance(v,(int,float)) for v in values) else None
    measured={cap:dict(window_minimum_free=minimum(cap,'window_session_minimum_free'),
        minimum_free=minimum(cap,'minimum_free'),largest=minimum(cap,'sampled_largest_minimum'))
        for cap in ('internal','dma')}
    failures=[s.get('failed_allocations',{}).get('internal') for s in snapshots]
    failed=max(failures) if failures and all(isinstance(v,int) for v in failures) else None
    m=measured['internal']
    checks=dict(session_window_internal=(m['window_minimum_free']>=limits['session_window_internal']) if m['window_minimum_free'] is not None and coverage else None,
        boot_minimum_internal=(m['minimum_free']>=limits['boot_minimum_internal']) if m['minimum_free'] is not None and reconnects else None,
        largest_internal=(m['largest']>=limits['largest_internal']) if m['largest'] is not None else None,
        failed_internal_allocations=(failed==0) if failed is not None else None)
    legacy=sum('window_session_minimum_free' not in s.get('heap',{}).get('internal',{}) for s in snapshots)
    return dict(session_basis=dict(active_media_snapshots=len(snapshots)-legacy,setup_inclusive_fallback_snapshots=legacy),limits_bytes=limits,measured=measured,window_coverage_complete=coverage,failed_internal_allocations=failed,
        reconnects=reconnects,checks=checks,
        passed=False if False in checks.values() else None if None in checks.values() else True,
        basis='LCD budget: owner-loop session windows, boot-cumulative trough after forced reconnect, owner-sampled largest block and boot allocation-failure counts. KB denotes KiB. DMA reported alongside.')


def console_windows(path):
    import re
    # Older untagged windows include dumps and cannot establish a small read.
    return [dict(start_us=int(a)-120000,end_us=int(b)+120000,category='console',command=cmd)
            for cmd,a,b in re.findall(r'CONSOLE_WINDOW command=(stats|wifi-stats) start_us=(\d+) end_us=(\d+)',Path(path).read_text())]


def voice_device_cadence(path,*,target_p99_ms=45):
    path=Path(path);result=json.loads((path/'result.json').read_text())
    traces=[json.loads(l) for p in path.rglob('*arrivals.jsonl') for l in p.read_text().splitlines()]
    traces=list({tuple(sorted(r.items())):r for r in traces}.values())
    turns=result.get('turns',[])
    if not traces or not turns:return dict(target_passed=None,hard_ceiling_passed=None,reason='No complete voice trace/turn population')
    before=turns[0]['before_perf']['device_us'];after=turns[-1]['after_perf']['device_us']
    # Only explicit reconnect intervals are excluded; in-session air tails stay.
    windows=console_windows(path/'serial.log')
    from uplink_continuity import recorded_audio_idle_windows
    windows+=recorded_audio_idle_windows(path/'serial.log',clock='device_us',end=after)
    for first,second in zip(turns,turns[1:]):
        a,b=first['after_perf'],second['before_perf']
        if a.get('generation')!=b.get('generation'):
            windows.append(dict(start_us=a['device_us'],end_us=b['device_us'],category='reconnect'))
    return device_send_cadence(traces,before,after,windows,target_p99_ms=target_p99_ms,snapshots=perf_history(path/'serial.log'))



def voice_owner_gate(path):
    path=Path(path);result=json.loads((path/'result.json').read_text());turns=result.get('turns',[])
    if not turns:return dict(passed=None,reason='No voice turns')
    trace=[json.loads(l) for p in path.rglob('*arrivals.jsonl') for l in p.read_text().splitlines()]
    trace=list({tuple(sorted(r.items())):r for r in trace}.values())
    start,end=turns[0]['before_perf']['device_us'],turns[-1]['after_perf']['device_us']
    samples=perf_history(path/'serial.log');selected=[s for s in samples if start<=s['device_us']<=end]
    calls=sum(b['peer_profile']['loop_iteration']['calls']-a['peer_profile']['loop_iteration']['calls']
        for a,b in zip(selected,selected[1:]) if a['generation']==b['generation'])
    windows=[dict(start_us=a['after_perf']['device_us'],end_us=b['before_perf']['device_us'],category='reconnect')
        for a,b in zip(turns,turns[1:]) if a['after_perf']['generation']!=b['before_perf']['generation']]
    overwritten=max((s['peer_profile']['trace']['overwritten'] for s in selected),default=1)
    sends=[r['device_us'] for r in trace if r['kind']==12 and start<=r['device_us']<=end]
    covered=bool(sends) and min(sends)-start<=1000000 and end-max(sends)<=1000000 and not overwritten
    slow=[r for r in trace if r['kind']==8 and start<=r['device_us']<=end]
    gate=owner_gate(slow,calls,covered=covered,trace=trace,snapshots=samples,excluded_windows=windows)
    gate.update(trace_overwritten=overwritten,trace_covered=covered)
    return gate

def perf_history(path):
    result=[]
    for line in Path(path).read_text().splitlines():
        if 'PERF {' in line:
            try:
                sample=json.loads(line.split('PERF ',1)[1])
                if 'heap_window' in sample:result.append(sample)
            except json.JSONDecodeError:pass
    return result
