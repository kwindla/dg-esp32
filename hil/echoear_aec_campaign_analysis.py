"""Fail-closed minimal campaign verdict, derived only from retained witnesses."""
import json
import re
from pathlib import Path
import numpy as np
from echoear_aec_accept_analysis import main as block_analysis, match, check_render_ledger
from echoear_aec_bench import load_capture, write, sha, NORMAL_HASH
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_metrics import activity, frame_power, dbfs, reduction, near_metrics, interval_latency
from echoear_aec_timing import sample_interval, map_interval
from echoear_tts_proactive_analysis import connection_windows
from echoear_aec_accept import rows
from echoear_aec_noise_gate import highpass100, V3_FILTER


def read(path,default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def bound(value,limit):
    return isinstance(value,dict) and value.get('scorable') is True and value.get('upper_s',float('inf'))<=limit


def upper(value):
    # interval_latency uses lower_s/upper_s, and fails explicitly when unmapped.
    return value.get('upper_s') if isinstance(value,dict) else None


def echo_verdict(whole,first,source_dbfs,post):
    strong=source_dbfs is not None and source_dbfs>-30
    return dict(passed=bool(whole.get('scorable') and whole['integrated_reduction_db']>=20 and
                           source_dbfs is not None and (not strong or first.get('scorable') and first['integrated_reduction_db']>=10) and
                           post.get('scorable') and post['residual_dbfs']<=-50),
                first_500ms_gated=strong,source_first_500ms_dbfs=source_dbfs,
                whole=whole,first_500ms=first,post_guard=post)


def generation_audit(events,recorded,disconnects=(),active_ends=None,excluded_generations=()):
    """Context metrics, writes, request/wait and prohibited lifecycle overlaps."""
    sessions=[]
    for pc in sorted({e.get('pc_id') for e in events if e.get('pc_id')}):
        own=[e for e in events if e.get('pc_id')==pc]
        starts=[e for e in own if e['event']=='tts-generation-metric-start']
        generations=[];problems=[]
        ids=[e.get('context_id') for e in starts]
        idle=not starts and not any(e['event'] in ('script-request','real-bot-llm-request','tts-context-request') for e in own) and not any(e.get('pc_id')==pc and e.get('kind')=='tts-transport-write' for e in recorded)
        if (not starts and not idle) or None in ids or len(ids)!=len(set(ids)):problems.append('missing/duplicate metric context')
        for start in starts:
            cid=start.get('context_id');metrics=[e for e in own if e['event']=='tts-context-metric' and e.get('context_id')==cid]
            tts=[e for e in metrics if re.fullmatch(r'ProactiveConnectionTTS(?:#\d+)?',e.get('data',{}).get('processor',''))]
            a=[e for e in tts if e['metric_type']=='TTFAMetricsData'];b=[e for e in tts if e['metric_type']=='TTFBMetricsData']
            writes=sorted([e for e in recorded if e.get('pc_id')==pc and e.get('kind')=='tts-transport-write' and e.get('context_id')==cid],key=lambda e:e['monotonic_ns'])
            requests=[e for e in own if e['event']=='tts-context-request' and e.get('context_id')==cid]
            active=[e for e in writes if e.get('non_silent')]
            complete=len(a)==len(b)==len(requests)==1 and bool(writes and active)
            data=a[0]['data'] if len(a)==1 else {}
            if complete:complete=(data.get('processor'),data.get('model'))==(b[0]['data'].get('processor'),b[0]['data'].get('model')) and abs(data.get('ttfb',-100)-b[0]['data'].get('value',-200))<.001 and requests[0]['t']<=start['t']
            request=requests[0]['t'] if len(requests)==1 else None
            previous=[e for e in own if e['event']=='tts-interruption-local-cleanup' and e['t']<start['t'] and e.get('case_id')==start.get('case_id')]
            callers=[e for e in own if e.get('case_id')==start.get('case_id') and e['event'] in ('script-request','real-bot-llm-request','host-next-reply-queued') and e['t']<=start['t']]
            waits=[e for e in own if request is not None and request<=e['t']<=start['t'] and e['event']=='tts-reconnect-wait-end']
            generations.append(dict(context_id=cid,case_id=start.get('case_id'),complete=bool(complete),
                ttfb_s=data.get('ttfb'),ttfa_s=data.get('ttfa'),lead_s=data.get('leading_silence'),
                request_to_first_write_s=writes[0]['monotonic_ns']/1e9-request if writes and request is not None else None,
                request_to_metric_start_s=start['t']-request if request is not None else None,
                caller_anchor=callers[-1]['event'] if callers else None,
                caller_request_to_first_write_s=writes[0]['monotonic_ns']/1e9-callers[-1]['t'] if callers and writes else None,
                stop_to_first_write_s=writes[0]['monotonic_ns']/1e9-previous[-1]['t'] if previous and writes else None,
                inflight_wait_s=sum(e.get('elapsed_s',0) for e in waits)))
        written={e.get('context_id') for e in recorded if e.get('pc_id')==pc and e.get('kind')=='tts-transport-write'}
        if written!=set(ids):problems.append('transport/metric context set mismatch')
        windows=connection_windows(own)
        if not windows['passed']:problems.extend(windows['problems'])
        for a,b in windows['reconnects']:
            if not starts or a<starts[0]['t']:problems.append('reconnect before first generation')
            if any(a<hi and b>lo for lo,hi in disconnects):problems.append('reconnect across device disconnect/Ready')
        seals=[e['t'] for e in own if e['event']=='case-sealed'];last=(active_ends or {}).get(pc,max(seals,default=float('inf')))
        failures=[e for e in own if e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','tts-reconnect-refused','tts-reconnect-failed','liveness-alarm')]
        active=[e for e in failures if e['t']<=last];teardown=[e for e in failures if e['t']>last]
        if excluded_generations:
            for g in generations:g['long_tts_excluded']=(g['case_id'],g['context_id']) in excluded_generations
        sessions.append(dict(pc_id=pc,generations=generations,connection_windows=windows,problems=problems,
            active_errors=active,post_seal_errors=teardown,whole_session_clean=not failures,
            idle_no_generation_requested=idle,passed=(bool(generations) or idle) and not problems and not active and all(g['complete'] and g['lead_s'] is not None and (g['lead_s']<.8 or g.get('long_tts_excluded',False)) for g in generations)))
    return dict(passed=bool(sessions) and all(s['passed'] for s in sessions),sessions=sessions)


def context_playback(source,ref,blocks,anchors,write_s,highpass_hz=None):
    """Source prefix identifies THIS generation, not any residual old playback."""
    if len(source)<4000:return dict(passed=False,reason='Short/missing context PCM')
    if highpass_hz is not None:
        from echoear_aec_playback_template import filter_source
        source=filter_source(source,highpass_hz)
    pp=frame_power(source);active=np.flatnonzero(pp>1024)
    if not len(active):return dict(passed=False,reason='No speech-level source')
    first=int(active[0])*320;template=source[first:first+4000]
    candidates=[int(b[0]) for b in blocks if (lambda t:t and t[1]>=write_s-.2 and t[0]<=write_s+1.5)(sample_interval(int(b[0]),blocks,anchors))]
    if len(template)!=4000 or not candidates:return dict(passed=False,reason='Missing prefix/onset coverage')
    lo=min(candidates);hi=min(len(ref),max(candidates)+320)
    witness=match(ref[lo:hi],template)
    if not witness['valid']:return dict(passed=False,reason='Unscorable source/MIC3 match',match=witness)
    onset=lo+witness['at'];start=onset-first;end=start+len(source)
    interval=sample_interval(onset,blocks,anchors);latency=interval_latency([write_s,write_s],interval)
    return dict(passed=interval is not None and interval[0]>=write_s-.1 and interval[1]-write_s<=1,
                latency=latency,source_extent=[start,end],speech_onset_sample=onset,
                source_first_speech_sample=first,match=witness,prefix_loss_ms_diagnostic=None,
                prefix_loss_note='Signed first250ms match retained; exact lost-prefix bound unresolved, report-only')


def source_gate(serial,blocks,frames):
    changes=[(int(s),v=='closed') for v,t,s,ok in re.findall(r'GATE (closed|open) t=(\d+) source_lower_us=(-?\d+) source_valid=(\d)',serial) if ok=='1']
    state=[]
    for i in range(frames):
        b=next((b for b in blocks if b[0]<=i*320<b[1]),None)
        prior=[closed for t,closed in changes if b is not None and t<=b[2]]
        state.append(prior[-1] if prior else None)
    return state


def echo_measure(raw,afe,ref,qref,source,playback,serial,blocks):
    if not playback.get('passed'):return dict(passed=False,reason='Unscorable playback extent')
    start,end=playback['source_extent'];start=max(0,start)
    if end+1024>len(ref) or end<=start:return dict(passed=False,reason='Incomplete source/delayed tail')
    mask=activity(ref,qref,[(start,end)])
    ids=np.flatnonzero(mask)
    if not len(ids):return dict(passed=False,reason='No reference-active frames')
    first=mask.copy();first[int(ids[0])+25:]=False
    gate=source_gate(serial,blocks,len(mask));post=mask & np.array([x is False for x in gate])
    source_onset=playback['source_first_speech_sample'];piece=source[source_onset:source_onset+8000]
    source_dbfs=dbfs(piece) if len(piece)==8000 else None
    result=echo_verdict(reduction(raw,afe,mask),reduction(raw,afe,first),source_dbfs,reduction(raw,afe,post))
    # Filter the contiguous capture once, then select the SAME delayed samples
    # and independently derived masks as the frozen full-band metrics. No gate
    # or activity decision depends on this diagnostic.
    filtered_afe=highpass100(afe)
    result['residual_diagnostic_filter']=dict(V3_FILTER, scope='entire AFE capture before mask/delay selection',gate=False)
    for name, selected in [('whole',mask),('first_500ms',first),('post_guard',post)]:
        diagnostic=reduction(raw,filtered_afe,selected)
        result[name]['highpass100_residual_dbfs']=diagnostic.get('residual_dbfs')
    speech=frame_power(ref)>=max(float(frame_power(ref)[mask].max())*.01,4)
    speech_ids=np.flatnonzero(speech & mask);onset=int(speech_ids[0]) if len(speech_ids) else None
    # Require a closed source gate at speech after a >=2s gap; quick pairs
    # retain measured gap below for the no-rearm case.
    before=np.flatnonzero(activity(ref,qref,[(0,max(320,start))]))
    gap=(onset-int(before[-1])-1)*.02 if onset is not None and len(before) else None
    guard_ok=onset is not None and (gap is not None and gap<2 or gate[onset] is True)
    result.update(guard_at_speech=gate[onset] if onset is not None else None,preceding_silence_s=gap,guard_passed=guard_ok,
                  muted_fraction=float(sum(bool(gate[i]) for i in ids)/len(ids)),
                  gate_unknown_active_frames=sum(gate[i] is None for i in ids),reonsets=[])
    starts=np.flatnonzero(mask & ~np.r_[False,mask[:-1]])
    for at in starts:
        stop=int(at)+1
        while stop<len(mask) and mask[stop]:stop+=1
        piece=afe[int(at)*320+1024:stop*320+1024]
        result['reonsets'].append(dict(source_sample=int(at)*320,guard_closed=gate[int(at)],
            first_400ms_residual_dbfs=dbfs(piece[:6400]),max_20ms_residual_dbfs=float(10*np.log10(max(float(frame_power(piece).max()),1e-20)/32768**2))))
    result['passed'] &= guard_ok and not result['gate_unknown_active_frames']
    return result


def overlap_bounds(near_start,near_end,far_end):
    if near_start is None or near_end is None or far_end is None:
        return dict(scorable=False,reason='Missing independent overlap endpoint')
    end=[min(near_end[0],far_end[0]),min(near_end[1],far_end[1])]
    value=interval_latency(near_start,end)
    value['lower_s']=max(0,value['lower_s']);value['upper_s']=max(0,value['upper_s'])
    return value


def near_end_interval(c,row,blocks,anchors):
    import wave
    from scipy.signal import resample_poly
    if not c.get('fixture'):return None  # scripted host-interrupt cells have no near-end source
    with wave.open(c['fixture']) as w:
        source=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float);rate=w.getframerate()
    if rate!=16000:source=resample_poly(source,16000,rate)
    power=frame_power(source);active=np.flatnonzero(power>max(1,float(power.max())*.001))
    onset=row.get('near_onset',{}).get('raw_onset_sample')
    if not len(active) or onset is None:return None
    end=onset+(int(active[-1])-int(active[0])+1)*320
    left=sample_interval(max(0,end-320),blocks,anchors);right=sample_interval(end+320,blocks,anchors)
    return [left[0],right[1]] if left and right else None


def flush_measure(c,row,ref,qref,blocks,anchors,serial,playbacks,policy=None):
    starts=[e['t'] for e in c['events'] if e.get('frame')=='InterruptionFrame' and e['t']>=c.get('host_interrupt_timer',{}).get('command',{}).get('request_monotonic_s',c.get('jabra',{}).get('request_monotonic_s',float('inf')))]
    if not starts:return dict(passed=False,reason='No accepted interruption')
    event=min(starts);following=min((v['write_s'] for v in playbacks[1:]),default=anchors[-1]['upper_s'])
    callbacks=[]
    for a,b,g,v in re.findall(r'AUDIO_CANCEL callback_us=(\d+) device_us=(\d+) gen=(\d+) applied=(\d)',serial):
        mapped=map_interval(int(a),anchors)
        if mapped and mapped[1]>=event and mapped[0]<following:callbacks.append(dict(callback_us=int(a),control_us=int(b),generation=int(g),applied=bool(int(v)),host_interval=mapped))
    if len(callbacks)!=1:return dict(passed=False,reason='Missing/ambiguous callback',callbacks=callbacks)
    cb=callbacks[0];mask=activity(ref,qref,[(0,len(ref))]);active=np.flatnonzero(mask)
    original=[int(i) for i in active if (lambda t:t and t[1]<following)(sample_interval(int(i)*320,blocks,anchors))]
    at=[int(b[0])//320 for b in blocks if b[2]<=cb['callback_us']<=b[3] and not b[4]]
    if not original:return dict(passed=False,reason='Missing old reference')
    last=original[-1]*320+319;device=next(b for b in blocks if b[0]<=last<b[1]);host=sample_interval(last,blocks,anchors)
    direct=[(float(x)-cb['callback_us'])/1e6 for x in device[2:4]]
    server=interval_latency([event,event],host)
    stale=int(sum(b[2]>cb['control_us']+150000 and int(b[0])//320 in original for b in blocks))
    certain=bool(at) and all(mask[i] for i in at)
    rule=(policy or {}).get('callback_witness')
    window_us=policy['pre_callback_activity_window_ms']*1000 if rule=='pre-callback-active-written-tail-v2' else None
    witness=callback_witness(blocks,mask,cb,last,playbacks[0],window_us=window_us) if rule in ('pre-callback-active-written-tail-v1','pre-callback-active-written-tail-v2') else dict(passed=certain,rule='all-candidate-blocks-active-v7')
    diff=np.abs(np.diff(ref));cut=float(diff[max(0,last-320):min(len(diff),last+320)].max());prior=float(diff[max(0,last-3200):max(1,last-320)].max())
    return dict(passed=bool(witness['passed'] and direct[1]<=.1 and upper(server) is not None and upper(server)<=.25 and stale==0),callbacks=callbacks,
        callback_to_flush_call_s=(cb['control_us']-cb['callback_us'])/1e6,
        last_mic3_host_interval=host,callback_to_last_mic3_s=direct,server_to_last_mic3=server,server_to_callback=interval_latency([event,event],cb['host_interval']),
        mic3_certainly_active_at_callback=certain,callback_witness=witness,stale_frames_after_mute=stale,
        near_onset_to_last_old_mic3=interval_latency(row.get('near_onset',{}).get('host_interval_with_onset_uncertainty'),host),
        cutoff_step_diagnostic=dict(max_lsb=cut,prior_lsb=prior,proposed_limit_lsb=max(32,2*prior)),
        overlap_duration_s=overlap_bounds(row.get('near_onset',{}).get('host_interval_with_onset_uncertainty'),near_end_interval(c,row,blocks,anchors),host))


def source_write_witness(play,source,recorded,pc_id,context_id):
    writes=sorted((e for e in recorded if e.get('kind')=='tts-transport-write' and e.get('pc_id')==pc_id and e.get('context_id')==context_id),key=lambda e:e['monotonic_ns'])
    ranges=[];cursor=0
    for e in writes:
        n=e.get('samples')
        if e.get('drop') or type(n) is not int or n<=0:return dict(valid=False,reason='Missing/dropped source write coverage')
        ranges.append(dict(start=cursor,end=cursor+n,host_s=e['monotonic_ns']/1e9,non_silent=e.get('non_silent') is True));cursor+=n
    return dict(valid=bool(ranges) and cursor<=len(source),ranges=ranges,
        active_frames=[int(i) for i in np.flatnonzero(frame_power(source)>1024)] if len(source)>=320 else [])


def callback_witness(blocks,mask,callback,last,old_playback,*,window_us=None):
    preceding=[b for b in blocks if b[3]<callback['callback_us']]
    if window_us is not None:
        preceding=[b for b in preceding if b[3]>=callback['callback_us']-window_us and not b[4] and mask[int(b[0])//320]]
    prior=max(preceding,key=lambda b:b[0]) if preceding else None
    active=bool(prior is not None and not prior[4] and mask[int(prior[0])//320])
    source=old_playback.get('source_write_witness',{})
    extent=old_playback.get('source_extent')
    cut=last+1-extent[0] if extent else None
    tail=[]
    if source.get('valid') and cut is not None and cut>=0:
        for i in source['active_frames']:
            start=i*320;end=start+320
            if start<cut:continue
            # A complete active source frame beyond the physical cut must have
            # reached transport by the callback bracket's upper endpoint.
            coverage=[w for w in source['ranges'] if w['start']<end and w['end']>start]
            if coverage and coverage[0]['start']<=start and coverage[-1]['end']>=end and all(w['non_silent'] and w['host_s']<=callback['host_interval'][1] for w in coverage):
                tail.append(dict(source_samples=[start,end],writes=coverage));break
    return dict(rule='pre-callback-active-written-tail-v2' if window_us is not None else 'pre-callback-active-written-tail-v1',passed=active and bool(tail),
        pre_callback_activity_window_us=window_us,
        pre_callback_upper_age_us=int(callback['callback_us']-prior[3]) if prior is not None else None,
        pre_callback_block=prior.tolist() if prior is not None else None,pre_callback_active=active,
        source_cut_sample=cut,written_active_tail=tail)


def interruption_copies(events,sctp):
    import hashlib
    messages={hashlib.sha256(json.dumps(e['message']).encode()).hexdigest():e['message'] for e in events if e.get('kind')=='rtvi-output' and e.get('source')=='RTVIProcessor' and e.get('direction')=='DOWNSTREAM'}
    groups={}
    for e in sctp:
        if e['event']=='send-chunk' and e.get('chunk_type')=='DataChunk' and e.get('protocol')==51:
            groups.setdefault((e['pc_id'],e['tsn']),[]).append(e)
    result=[]
    for (pc,tsn),sends in groups.items():
        first=min(sends,key=lambda e:e['monotonic_ns']);message=messages.get(first['user_data_sha256'],{})
        if message.get('type')!='bot-interrupted':continue
        def acked(sack):
            d=(tsn-sack['cumulative_tsn'])%2**32
            return d==0 or d>=2**31 or any(a<=d<=b for a,b in sack.get('gap_ack_blocks',[]))
        sacks=[e for e in sctp if e['pc_id']==pc and e['event']=='receive-chunk' and e.get('chunk_type')=='SackChunk' and e['monotonic_ns']>first['monotonic_ns'] and acked(e)]
        ack=min(sacks,key=lambda e:e['monotonic_ns']) if sacks else None
        result.append(dict(pc_id=pc,tsn=tsn,interrupt_seq=message['data']['interrupt_seq'],first_send_ns=first['monotonic_ns'],transmissions=len(sends),first_ack_ms=(ack['monotonic_ns']-first['monotonic_ns'])/1e6 if ack else None,first_send_acked=bool(ack and (len(sends)==1 or ack['monotonic_ns']<sorted(e['monotonic_ns'] for e in sends)[1]))))
    return sorted(result,key=lambda r:r['first_send_ns'])


def transport(before,after,policy=None):
    from echoear_aec_transport_policy import RULE
    if policy not in (None,RULE):raise ValueError("Unknown transport policy")
    keys=('received','lost','playout_dropped')
    valid=all(type(p.get(k)) is int and p[k]>=0 for p in (before,after) for k in keys)
    deltas={k:after[k]-before[k] if valid else None for k in keys}
    # Epoch identity and ordered snapshots, when available, also constrain the
    # legacy fallback. A lifetime maximum is never a per-window witness.
    for k in ('generation','pc_id'):
        if k in before or k in after:valid &= before.get(k) is not None and before.get(k)==after.get(k)
    if 'device_us' in before or 'device_us' in after:
        valid &= all(type(p.get('device_us')) is int for p in (before,after)) and after.get('device_us',0)>before.get('device_us',0)
    valid &= all(v is not None and v>=0 for v in deltas.values())
    ratio=(deltas['lost']+deltas['playout_dropped'])/max(1,deltas['received']) if valid else None
    burst=deltas['playout_dropped'] if valid else None
    basis='conservative total-drop upper bound; not a measured run length'
    detail={}
    hist=[p.get('playout',{}).get('missing_run_ended') for p in (before,after)]
    present=any('missing_run_ended' in p.get('playout',{}) for p in (before,after))
    if present:
        hist_valid=valid and all(isinstance(v,list) and len(v)==6 and all(type(x) is int and x>=0 for x in v) for v in hist)
        hist_valid &= all(type(p.get('generation')) is int and isinstance(p.get('pc_id'),str) and bool(p['pc_id']) and type(p.get('device_us')) is int for p in (before,after))
        delta=[b-a for a,b in zip(*hist)] if hist_valid else None
        hist_valid &= delta is not None and all(x>=0 for x in delta)
        if hist_valid:
            # Bucket 6 is a lower bound, never an exact count; it always fails.
            finished=sum(k*n for k,n in enumerate(delta,1))
            opened=max(0,deltas['lost']-finished)
            largest=max((k for k,n in enumerate(delta,1) if n),default=0)
            burst=max(largest,opened)
            detail=dict(missing_run_ended_delta=delta,finished_missing_slots_lower_bound=finished,
                open_missing_slots_upper=opened,ge6_ended=delta[5]>0)
        else:burst=None
        valid=bool(hist_valid)
        basis='ended missing-run histogram delta plus conservative open-run remainder'
    if policy==RULE:
        # Explicit echo-acceptance policy; the historical path below is unchanged.
        faults={}
        for key in ('post_plc_zeros','starvation'):
            counters=[p.get('playout',{}).get(key) for p in (before,after)]
            ok=all(type(v) is int and v>=0 for v in counters)
            faults[key]=counters[1]-counters[0] if ok else None
        # PERF also exports a top-level zeros mirror: a disagreement is not a waiver.
        mirror_valid=all('post_plc_zeros' not in p or
            (type(p['post_plc_zeros']) is int and p['post_plc_zeros']==p.get('playout',{}).get('post_plc_zeros'))
            for p in (before,after))
        passed=bool(valid and present and ratio is not None and ratio<=.02 and
                    detail.get('ge6_ended') is False and mirror_valid and
                    all(v==0 for v in faults.values()))
        return dict(passed=passed,rule=RULE,delta=deltas,loss_fraction=ratio,
            loss_fraction_max=.02,unique_unusable_fraction=(deltas['lost']/max(1,deltas['received']) if valid else None),
            max_burst_upper=burst if present else None,burst_basis=basis,
            burst_report_only=True,histogram_present=present,histogram_valid=bool(valid and present),
            silence_delta=faults,silence_mirror_valid=mirror_valid,
            **{k:detail.get(k) for k in ('missing_run_ended_delta','finished_missing_slots_lower_bound','open_missing_slots_upper','ge6_ended')})
    return dict(passed=bool(valid and ratio is not None and ratio<=.005 and type(burst)is int and burst<=3),delta=deltas,loss_fraction=ratio,unique_unusable_fraction=(deltas['lost']/max(1,deltas['received']) if valid else None),max_burst_upper=burst,burst_basis=basis,**detail)


def probe_measure(ref,qref,volume,gain):
    state=0x13579bdf;source=[]
    for i in range(38400):
        if i<3200 or i>=35200:source.append(0);continue
        state^=(state<<13)&0xffffffff;state^=state>>17;state^=(state<<5)&0xffffffff
        value=state>>16;value=value if value<32768 else value-65536
        source.append(int(int(value/10)*min(160,i-3200,35200-i)/160))
    ids=np.flatnonzero(activity(ref,qref,[(0,len(ref))]))
    if not len(ids):return dict(passed=False,reason='Missing canonical probe')
    at=int(ids[0])*320;central=ref[at+3200:at+28800]
    accepted=np.trunc(np.array(source[6400:32000])*int(np.floor(32768*10**(volume/20)+.5))/32768)
    normalized=float(np.mean(central**2)/np.mean(accepted**2)/10**(gain/10)*1e6) if len(central)==25600 else None
    return dict(passed=normalized is not None and normalized>=1 and not np.any(np.abs(ref)>=32767),normalized_0db_ratio_ppm=normalized,margin_db=10*np.log10(normalized) if normalized and normalized>0 else None)


def delivery_counters(before,after):
    paths=[('send_errors',),('send_budget_cutoffs',),('aec','capture','rx_overflows')]
    paths += [('drop_reasons',k) for k in sorted(set(before.get('drop_reasons',{}))|set(after.get('drop_reasons',{})))]
    def get(p,path):
        for key in path:p=p.get(key) if isinstance(p,dict) else None
        return p
    delta={'.'.join(path):(get(after,path)-get(before,path) if type(get(after,path)) is int and type(get(before,path)) is int else None) for path in paths}
    return dict(passed=bool(before.get('drop_reasons')) and all(v==0 for v in delta.values()),delta=delta)


def near_preservation(c,raw,afe,ref,qref):
    import wave
    from scipy.signal import resample_poly
    with wave.open(c['fixture']) as w:
        source=np.frombuffer(w.readframes(w.getnframes()),'<i2').astype(float);rate=w.getframerate()
    if rate!=16000:source=resample_poly(source,16000,rate)
    power=frame_power(source);active=np.flatnonzero(power>max(1,float(power.max())*.001))
    if not len(active):return dict(passed=False,reason='Silent source')
    first=int(active[0])*320;last=(int(active[-1])+1)*320
    witness=match(raw,source[first:min(last,first+8000)])
    if not witness['valid']:return dict(passed=False,reason='No independent raw/fixture onset',match=witness)
    onset=witness['at'];end=onset+last-first
    if end+1024>len(afe):return dict(passed=False,reason='Incomplete near source/delayed output')
    metric=near_metrics(raw,afe,[(onset,end)])
    silent=not bool(activity(ref,qref,[(0,len(ref))]).any())
    return dict(passed=bool(silent and metric.get('within_preservation_bound')),metric=metric,board_silent=silent,match=witness,known_extent=[onset,end])


def analyze_hold(root,p):
    from echoear_aec_lull import declared_rule, eligible
    lull_policy=declared_rule(p)
    from echoear_aec_playback_template import declared_corner
    highpass_hz=declared_corner(p)
    saved=read(root/'result.json',{});events=rows(root/'evaluation/events.jsonl');recorded=[e for path in (root/'sessions').glob('*/events.jsonl') for e in rows(path)]
    if not saved:return dict(passed=False,reason='Missing hold',cases=[])
    copies=interruption_copies(recorded,rows(root/'rtp/sctp.jsonl'))
    # Analysis of a hash-bound prior run must not rewrite its artifacts.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        block=block_analysis(root,Path(temp)/'block-analysis.json',lull_policy=lull_policy)
    indexed={r['id']:r for r in block['cases']}
    serial=(root/'candidate-serial.log').read_text();qraw,qafe,qref,_=load_capture(root/'quiet.npz');result=[]
    for c in saved['cases']:
        folder=root/c.get('capture_dir',c['run_id']);raw,afe,ref,blocks=load_capture(folder/'capture.npz');anchors=read(folder/'anchors.json');row=indexed[c['run_id']]
        own=c['events'];contexts=[e for e in own if e['event']=='tts-context-pcm'];playbacks=[]
        for context in contexts:
            writes=[e for e in recorded if e.get('kind')=='tts-transport-write' and e.get('context_id')==context['context_id'] and e.get('pc_id')==context['pc_id'] and e.get('non_silent')]
            source=np.fromfile(root/'evaluation'/context['path'],dtype='<i2').astype(float)
            play=context_playback(source,ref,blocks,anchors,min(e['monotonic_ns'] for e in writes)/1e9,highpass_hz) if writes else dict(passed=False,reason='No non-silent write')
            play['source_write_witness']=source_write_witness(play,source,recorded,context['pc_id'],context['context_id'])
            play.update(context_id=context['context_id'],write_s=min(e['monotonic_ns'] for e in writes)/1e9 if writes else None)
            if c['kind'] in ('echo','soak-echo'):play['echo']=echo_measure(raw,afe,ref,qref,source,play,serial,blocks)
            playbacks.append(play)
        row.update(**(dict(report_only=c.get('report_only',False)) if p.get('v85') else {}),voice_class=c.get('voice_class','firm'),playbacks=playbacks,clean=c['noise_gate_passed'] and c['quiet_windows_valid'],expected_class=c.get('expected_class'))
        before,after=read(folder/'before.json'),read(folder/'after.json')
        scored_after=read(folder/'capture-end.json') if p.get('v85') else after
        if scored_after is None:
            row['transport']=dict(passed=False,reason='Missing capture-end PERF')
            row['delivery_counters']=dict(passed=False,reason='Missing capture-end PERF')
        else:
            row['transport']=transport(before,scored_after,p.get('gates',{}).get('transport',{}).get('rule'))
            row['delivery_counters']=delivery_counters(before,scored_after)
        if p.get('v85') and scored_after:
            row['export_event']=dict(report_only=True,transport=transport(scored_after,after,p['gates']['transport']['rule']),delivery=delivery_counters(scored_after,after))
        row['ledger']=check_render_ledger([read(f) for f in sorted(folder.glob('*-boundary-*.json'))])
        row['capture_cap']=read(folder/'capture-duration.json',{}).get('elapsed_s',100)>0 and read(folder/'capture-duration.json',{}).get('elapsed_s',100)<=28
        row['rf']=read(folder/'rf.json',{}).get('passed',False)
        row['reference_health']=after.get('aec',{}).get('reference_health',{}).get('low_windows',-1)==before.get('aec',{}).get('reference_health',{}).get('low_windows',-2)
        row['playback_passed']=bool(playbacks) and all(x['passed'] for x in playbacks)
        if c['kind'] in ('near','probe'):row['playback_passed']=True
        row['acoustic_passed']=True
        if c['kind'] in ('echo','soak-echo'):
            row['accepted_false_turns']=sum(e.get('frame')=='UserStartedSpeakingFrame' for e in own)
            row['turn_events']=turn_events(own)
            row['acoustic_passed']=bool(playbacks) and all(x['echo']['passed'] for x in playbacks) and row['accepted_false_turns']==0 and row['turn_events']['interruptions']==0
            if c.get('quick_gap_s') is not None:
                gap=playbacks[0].get('echo',{}).get('preceding_silence_s') if playbacks else None
                row['quick_gap']=quick_gap(gap)
        if c['kind']=='near':
            row['near']=near_preservation(c,raw,afe,ref,qref);row['acoustic_passed']=row['near']['passed']
        if c['kind']=='probe':
            row['probe']=dict(reference_active=bool(activity(ref,qref,[(0,len(ref))]).any()),raw_dbfs=dbfs(raw),ref_dbfs=dbfs(ref))
            row['acoustic_passed']=row['probe']['reference_active']
        if c.get('expected_class')=='eligible' and eligible(row.get('opportunity'),lull_policy):
            row['flush']=flush_measure(c,row,ref,qref,blocks,anchors,serial,playbacks,p['gates']['flush'])
            row['interrupt_copies']=[x for x in copies if c['start_ns']<=x['first_send_ns']<=c['end_ns']]
            row['flush']['copies_observed']=len(row['interrupt_copies'])==2
            row['flush']['passed'] &= row['flush']['copies_observed']
            latency=upper(row.get('near_onset_to_interruption'));limit=p['gates']['barge_in']['near_onset_to_accepted_interruption_upper_max_s_by_kind'][c['kind']]
            row['barge_success']=bool(row.get('credited_barge_in') and latency is not None and latency<=limit and row['flush']['passed'])
            if c['volume_db']==-6 and row.get('interruption_after_stimulus'):
                row['acoustic_passed'] &= row['flush']['passed'] and latency is not None and latency<=limit
            row['sentence_overlap_duration']=row['flush'].get('overlap_duration_s') if c['kind']=='sentence' else None
            # A genuine recognition/interruption miss stays in the denominator;
            # it is not a missing next-output generation when no turn was accepted.
            if row.get('interruption_after_stimulus'):row['playback_passed'] &= len(playbacks)>=2
        if c.get('expected_class')=='report-actual-guard-overlap':
            row['acoustic_passed']=bool(row.get('near_onset',{}).get('valid') and row.get('in_window') is not None)
        if c['kind']=='host-interrupt':
            row['host_interrupt']=host_interrupt_measure(c,row,ref,qref,blocks,anchors,serial,playbacks,p['gates']['flush'])
            row['host_interrupt']['copies_observed']=len([x for x in copies if c['start_ns']<=x['first_send_ns']<=c['end_ns']])==2
            row['host_interrupt']['passed'] &= row['host_interrupt']['copies_observed']
            row['acoustic_passed']=row['host_interrupt']['passed']
        if c['kind']=='silent-cancel':
            callbacks=[a for a,b,g,v in re.findall(r'AUDIO_CANCEL callback_us=(\d+) device_us=(\d+) gen=(\d+) applied=(\d)',serial) if map_interval(int(a),anchors)]
            row['silent_cancel']=dict(callbacks=len(callbacks),ledger=row['ledger']['passed'])
            row['acoustic_passed']=len(callbacks)==1 and row['ledger']['passed']
        row['integrity_passed']=all([row['transport']['passed'],row['delivery_counters']['passed'],row['ledger']['passed'],row['capture_cap'],row['rf'],row['reference_health'],row['resources']['all_passed'],row['admission']['passed']])
        if c.get('fixture'):
            from echoear_aec_stimulus_levels import stimulus_levels
            row['stimulus_levels']=stimulus_levels(c,row,raw,afe,qraw,qafe)
            from echoear_aec_preservation import preservation
            row['firmware_near_preservation']=preservation(c,row,raw,afe,blocks=blocks,anchors=anchors)
        if p.get('schema')=='nova-live-v8.6-final-1' and c.get('id')=='nova-sentence-split-01':
            from echoear_aec_nova_live import regression
            rec=[e for e in recorded if c['start_ns']<=e.get('monotonic_ns',0)<=c['end_ns']]
            row['nova_regression']=regression(own,rec)
        result.append(row)
    disconnects=[r['disconnect_ready_interval_s'] for r in saved.get('reconnects',[]) if r.get('disconnect_ready_interval_s')]
    active_ends={}
    for reconnect in saved.get('reconnects',[]):
        old=read(root/reconnect['id']/'old-final.json',{})
        if old.get('pc_id') and reconnect.get('disconnect_ready_interval_s'):active_ends[old['pc_id']]=reconnect['disconnect_ready_interval_s'][0]
    for e in events:
        if e.get('pc_id') not in active_ends:active_ends[e.get('pc_id')]=saved.get('active_end_ns',float('inf'))/1e9
    excluded=()
    if p.get('v85'):
        from echoear_aec_v85_tts import exclusions
        excluded=exclusions(saved,events,recorded,root)
    proactive=generation_audit(events,recorded,disconnects,active_ends,excluded)
    generation_passed=proactive['passed']
    nova_profile=None
    if p.get('schema')=='nova-live-v8.6-final-1':
        from echoear_aec_nova_live import generation_gate
        from echoear_aec_nova_prestep import profile_witness
        proactive['nova_gated_audit']=generation_gate(events,recorded,disconnects,active_ends,excluded)
        generation_passed=proactive['nova_gated_audit']['passed']
        nova_profile=profile_witness(p,events)
    from echoear_aec_resources import liveness_reply
    epochs={}
    for path in root.rglob('*.json'):
        value=read(path)
        if isinstance(value,dict) and isinstance(value.get('tasks'),list) and value.get('generation',0)>0 and value.get('pc_id') and (not value.get('tasks_exited') or p.get('v85') and path.name=='old-final.json'):
            epochs.setdefault((value['pc_id'],value['generation']),[]).append(value)
    epoch_checks=[]
    for key,values in epochs.items():
        values.sort(key=lambda x:x['device_us']);first,last=values[0],values[-1]
        from echoear_aec_transport_environment import piecewise_transport,probe_epoch_scope
        probe=None
        if p.get('link_probe',{}).get('epoch_transport_rule')=='probe-own-rule-v1':
            scope=probe_epoch_scope(root)
            if (scope['pc_id'],scope['generation'])==key:probe=scope
        transport_check=piecewise_transport(values,saved.get('harness_failures',[]),probe,p.get('gates',{}).get('transport',{}).get('rule')) if p.get('failure_policy',{}).get('transport_environmental') else transport(first,last,p.get('gates',{}).get('transport',{}).get('rule'))
        delivery_check=delivery_counters(first,last)
        if p.get('v85'):
            from echoear_aec_v85_transport import scopes,audit
            spans,missing=scopes(root)
            transport_check=audit(values,spans,probe)
            transport_check['missing_capture_endpoints']=missing
            transport_check['passed'] &= not missing
            delivery_check=dict(passed=all(r['delivery']['passed'] for r in transport_check['intervals'] if r['scope']!='export'),raw_report_only=delivery_check)
        checks=dict(transport=transport_check,delivery=delivery_check,liveness=liveness_reply(first,last),
            render_ledger=check_render_ledger([v for v in values if v.get('playout',{}).get('clock',{}).get('completed_samples',0)>0]))
        epoch_checks.append(dict(pc_id=key[0],generation=key[1],checks=checks,passed=all(v['passed'] for v in checks.values())))
    wire_metrics=[e for e in recorded if e.get('kind')=='rtvi-output' and e.get('direction')=='DOWNSTREAM' and e.get('message',{}).get('type')=='metrics']
    clean=[r for r in result if r['clean']]
    # Noise-invalid rows retain hardware/transport failures; acoustic/recognition
    # scores from them never qualify or consume an opportunity spare.
    passed=bool(saved.get('completed') and saved.get('rf_active',{}).get('passed') and bool(epoch_checks) and all(e['passed'] for e in epoch_checks) and not wire_metrics and generation_passed and all(r['integrity_passed'] for r in result) and all(f['retry_safe'] for f in saved.get('harness_failures',[])) and
                all(r['playback_passed'] and r['acoustic_passed'] for r in clean if not r.get('report_only')) and
                saved.get('resources',{}).get('all_passed') and saved.get('sctp_recovery',{}).get('passed') and
                saved.get('restored_elf_sha256')==NORMAL_HASH and saved.get('mixer_equal') and saved.get('lock_released') and not saved.get('cleanup_errors'))
    from echoear_aec_transport_environment import environmental_summary
    environment=environmental_summary(root,saved)
    host_audit=None
    if p.get('host_environment'):
        from echoear_aec_host_gate import audit
        host_audit=audit(root,p['host_environment'])
        passed &= host_audit['passed']
        passed &= saved.get('host_environment',{}).get('passed') is True and not saved.get('error_class')
        if root.name==p['first_hold_periodicity']['hold']:passed &= saved.get('periodicity',{}).get('passed') is True
    if p.get('link_probe'):passed &= saved.get('link_probe',{}).get('passed',False)
    if nova_profile is not None:passed &= nova_profile['passed']
    return dict(nova_profile=nova_profile,stt_profiles=[e for e in events if e['event'] in ('stt-profile','stt-model-observed')],passed=passed,**(dict(host_environment_audit=host_audit,acceptance_credit=host_audit['passed'] and saved.get('host_environment',{}).get('passed') is True and not saved.get('error_class')) if p.get('host_environment') else {}),environmental=environment,cases=result,harness_failures=saved.get('harness_failures',[]),epoch_checks=epoch_checks,wire_metrics=len(wire_metrics),proactive=proactive,reconnects=saved.get('reconnects',[]),schedule=read(root/'schedule.json',{}),soak=read(root/'soak.json'),probes=saved.get('probes',[]),cleanup=saved)


def quick_gap(gap):
    witnessed=gap is not None and 0<gap<2
    return dict(actual_s=gap,classification='quick-gap witnessed' if witnessed else 'not quick',
                request_anchor='BotStoppedSpeaking +1s; independent MIC3 gap',witnessed=witnessed)


def campaign_verdict(p,holds, *, lull_policy=None):
    if p.get('schema')=='nova-live-v8.6-final-1':
        from echoear_aec_nova_live import verdict
        return verdict(p,holds)
    if p.get('v85'):
        from echoear_aec_v85_verdict import verdict
        return verdict(p,holds)
    from echoear_aec_lull import eligible, stratum_summary, declared_rule, cancelled_check
    report_only=lull_policy is not None
    lull_policy=lull_policy or declared_rule(p)
    eligible(None,lull_policy)  # Validate even an empty report.
    cases=[c for h in holds if h.get('acceptance_credit',True) for c in h.get('cases',[])];clean=[c for c in cases if c.get('clean')]
    required={c['id'] for c in p['cases']+p['controls'] if c['kind']!='probe'}
    seen={c.get('cell_id',c.get('id')) for c in cases}
    clean_ids=[c.get('cell_id',c.get('id')) for c in clean]
    missing=sorted(required-seen)
    score={};quick_pairs={};complete=not missing and len(clean_ids)==len(set(clean_ids))
    for volume,n in ((-6,10),(0,5)):
        voices=p['gates']['barge_in'].get('gated_voice_classes',['firm']) if volume==-6 else ['firm']
        for voice in voices:
            for kind in ('stop','sentence'):
                group=[r for r in clean if r['volume_db']==volume and r.get('voice_class','firm')==voice and r['kind']==kind and r.get('expected_class')=='eligible' and eligible(r.get('opportunity'),lull_policy)]
                success=sum(r.get('barge_success',False) for r in group)
                key=f'{volume}:{voice}:{kind}'
                score[key]=dict(voice_class=voice,eligible=len(group),successes=success,required_eligible=n,gated=volume==-6,passed=len(group)==n and (volume==0 or success>=9))
                if lull_policy is not None:score[key]['strata']=stratum_summary([r for r in clean if r['volume_db']==volume and r.get('voice_class','firm')==voice and r['kind']==kind and r.get('expected_class')=='eligible'])
                complete &= score[key]['passed']
        echo=[c for c in clean if c['volume_db']==volume and c['kind']=='echo']
        quick_pairs[str(volume)]=sum(c.get('quick_gap',{}).get('witnessed',False) for c in echo)
        complete &= len(echo)==10 and quick_pairs[str(volume)]>=1
    for kind,n in [('near',4),('silent-cancel',2),('soak-echo',10)]:complete &= sum(c['kind']==kind for c in clean)==n
    complete &= sum(c.get('expected_class')=='report-actual-guard-overlap' for c in clean)==2
    probes=[r for h in holds if h.get('acceptance_credit',True) for r in h.get('probes',[]) if r['noise']]
    complete &= len(probes)==2 and all(r['passed'] for r in probes)
    reconnects=[r for h in holds if h.get('acceptance_credit',True) for r in h.get('reconnects',[]) if r.get('noise_passed',True)]
    complete &= len(reconnects)==3 and all(r['passed'] for r in reconnects)
    soak=[h['soak'] for h in holds if h.get('acceptance_credit',True) and h.get('soak')];complete &= len(soak)==1 and soak[0]['passed']
    passed=bool({h.get('id') for h in holds}=={s['id'] for s in p['sessions']} and complete and all(h.get('passed') for h in holds))
    environmental_attempts=sum(h.get('environmental',{}).get('environmental_attempts',0) for h in holds)
    capture_attempts=sum(h.get('environmental',{}).get('capture_attempts',0) for h in holds)
    from echoear_aec_transport_environment import export_load_hypothesis
    export_signal=export_load_hypothesis([f for h in holds for f in h.get('environmental',{}).get('attempts',[])])
    diagnostic={}
    if lull_policy is not None:
        guard=cancelled_check([c for h in holds for c in h.get('cases',[])])
        diagnostic['cancelled_lull_check']=guard
        passed &= guard['passed']
    if report_only:
        # Draft denominator simulation is useful; adoption/qualification is
        # explicitly impossible even with a historical qualification=True.
        diagnostic.update(report_only=True,acceptance_credit=False,lull_policy=lull_policy,
                          hypothetical_passed=passed)
        passed=False
    if p.get('gates',{}).get('transport',{}).get('rule'):
        from echoear_aec_transport_policy import burst_summary
        diagnostic['transport_burst_summary']=burst_summary(holds)
    return dict(**diagnostic,export_load_hypothesis=export_signal,environmental_attempts=environmental_attempts,capture_attempts=capture_attempts,environmental_fault_rate=environmental_attempts/capture_attempts if capture_attempts else None,environmental_holds=[dict(id=h.get('id'),run_root=h.get('run_root'),**h['environmental']) for h in holds if 'environmental' in h],passed=passed,qualification=passed and p.get('qualification') is True,qualified_volumes_db=[-6] if passed else [],barge_in=score,stimulus_levels_by_class={f'{volume}:{voice}:{kind}':[dict(id=c['id'],clean=c.get('clean'),**c['stimulus_levels']) for c in cases if c['volume_db']==volume and c.get('voice_class','firm')==voice and c['kind']==kind and 'stimulus_levels' in c] for volume,voice,kind in {(c['volume_db'],c.get('voice_class','firm'),c['kind']) for c in cases if 'stimulus_levels' in c}},quick_pairs_witnessed=quick_pairs,complete_denominators=bool(complete),missing_declared_ids=missing,default_changed=False,
                spares_used=list({(r['original'],r['spare']):r for h in holds for j in h.get('schedule',{}).get('journal',[]) for r in j.get('replacements',[])}.values()))


def analyze_campaign(root):
    root=Path(root);p=read(root/'declaration.json');holds=[]
    from echoear_aec_campaign_resume import run_chain
    for run_root in run_chain(p)+[root]:
        for s in p['sessions']:
            if not (run_root/s['id']).exists():continue
            try:h=analyze_hold(run_root/s['id'],p)
            except Exception as exc:h=dict(passed=False,cases=[],analysis_error=f'{type(exc).__name__}: {exc}')
            holds.append(dict(id=s['id'],run_root=str(run_root),**h))
    summary=campaign_verdict(p,holds)
    if p.get('schema')=='nova-live-v8.6-final-1':
        elapsed=read(root/'elapsed.json',{})
        summary['budget_passed']=bool(elapsed.get('completed') and elapsed.get('seconds',float('inf'))<=3600)
        summary['passed'] &= summary['budget_passed']
    return dict(summary=summary,holds=holds,analyzer_sha256=sha(__file__))



def turn_events(events):
    return dict(vad_starts=sum(e.get('frame')=='VADUserStartedSpeakingFrame' for e in events),
                transcription_starts=sum(e.get('event')=='user-turn-started' and e.get('transcription_start') is True for e in events),
                accepted_turns=sum(e.get('frame')=='UserStartedSpeakingFrame' for e in events),
                interruptions=sum(e.get('frame')=='InterruptionFrame' for e in events),
                bot_state=[dict(t=e['t'],active=e['frame']=='BotStartedSpeakingFrame') for e in events if e.get('frame') in ('BotStartedSpeakingFrame','BotStoppedSpeakingFrame')])


def host_interrupt_measure(c,row,ref,qref,blocks,anchors,serial,playbacks,policy):
    timer=c.get('host_interrupt_timer',{})
    command=timer.get('command') or {}
    issued=command.get('request_monotonic_s')
    commands=[e for e in c['events'] if e.get('event')=='script-interrupt']
    accepted=[e for e in c['events'] if e.get('frame')=='InterruptionFrame']
    user_turns=[e for e in c['events'] if e.get('frame')=='UserStartedSpeakingFrame']
    queued=[e for e in c['events'] if e.get('event')=='host-next-reply-queued']
    if (issued is None or timer.get('skipped') or timer.get('harness_failure') or
        len(commands)!=1 or len(accepted)!=1 or user_turns or len(queued)!=1 or
        timer.get('timing_passed') is not True or timer.get('active_tts_witness',{}).get('active') is not True or
        timer.get('requested_offset_s')!=c.get('host_interrupt_offset_s') or
        commands[0].get('host_issued_s')!=issued or commands[0].get('number')!=command.get('number') or
        commands[0].get('frame_id') is None or commands[0]['frame_id']!=accepted[0].get('frame_id') or
        not issued<=commands[0]['t']<=accepted[0]['t']<=queued[0]['t']):
        return dict(passed=False,reason='Missing/ambiguous host interruption, timing failure or STT interference')
    flush=flush_measure(c,row,ref,qref,blocks,anchors,serial,playbacks,policy)
    next_play=playbacks[1] if len(playbacks)==2 else None
    next_interval=sample_interval(next_play['speech_onset_sample'],blocks,anchors) if next_play and next_play.get('speech_onset_sample') is not None else None
    callback=flush.get('callbacks',[{}])[0] if flush.get('callbacks') else {}
    # Transport-to-MIC3 initial playback bounds remain inside each playback result.
    return dict(passed=bool(flush['passed'] and callback.get('applied') and next_play and next_play['passed'] and next_interval),
        stt_independent=True,recognition_credit=False,next_reply_role='declared scripted recovery probe',
        callback_to_next_mic3=interval_latency(callback.get('host_interval'),next_interval),
        last_old_mic3_to_next_mic3=interval_latency(flush.get('last_mic3_host_interval'),next_interval),
        issued_s=issued,host_to_server_s=commands[0]['t']-issued,
        host_to_interruption_s=accepted[0]['t']-issued,flush=flush,
        next_reply=next_play,next_request_s=queued[0]['t'],
        callback_to_next_write_s=interval_latency(flush.get('callbacks',[{}])[0].get('host_interval') if flush.get('callbacks') else None,
            [next_play['write_s'],next_play['write_s']] if next_play else None))

if __name__=='__main__':
    import sys
    root=Path(sys.argv[1]);write(root/'analysis.json',analyze_campaign(root))
