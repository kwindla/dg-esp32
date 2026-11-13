"""Single-buffer receive evidence: full RTP identities and device render ticks.

Native callback latency is retained as a shadow measurement. It is never used
as the arrival boundary of the new Playout. Software completion times bound
render deadlines; they are not DAC timestamps or one-way network latency.
"""
import argparse
from bisect import bisect_left,bisect_right
from collections import Counter,defaultdict
import json
from pathlib import Path


def records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def quantiles(values):
    values=sorted(values)
    return dict(n=len(values),p50=values[(len(values)-1)//2],
                p99=values[int((len(values)-1)*.99)],maximum=values[-1]) if values else dict(n=0,p50=None,p99=None,maximum=None)


def receive_audit(rows,sent,before_us=None,after_us=None,tc_model=None):
    key=lambda r:(r.get('reserved',0),r['ssrc'],r['seq'],r['timestamp'])
    boundaries={}
    for kind in (1,2,13):
        indexed={}
        for r in rows:
            if r['kind']==kind:indexed.setdefault(key(r),r)
        boundaries[kind]=indexed
    auth=boundaries[2];ingress=boundaries[13]
    hook_delay=[(r['device_us']-auth[k]['device_us'])/1000 for k,r in ingress.items() if k in auth]
    # Pair native PTS using the full source and both modulo/extended RTP models.
    candidates=defaultdict(list);high={}
    for r in auth.values():
        source=(r['reserved'],r['ssrc']);h=high.get(source,r['timestamp'])
        extended=h+(r['timestamp']-h%2**32+2**31)%2**32-2**31;high[source]=max(h,extended)
        for pts in {r['timestamp']//48,(extended//48)%2**32}:
            candidates[(r['reserved'],pts)].append(r)
    shadow=[];ambiguous=0
    for r in rows:
        if r['kind']!=3:continue
        matches=[a for a in candidates[(r['reserved'],r['value'])] if a['device_us']<=r['device_us']]
        # Retransmission duplicates retain the first authenticated observation.
        identities={(a['ssrc'],a['seq'],a['timestamp']) for a in matches}
        if len(identities)==1:shadow.append((r['device_us']-min(a['device_us'] for a in matches))/1000)
        else:ambiguous+=1
    arrived=defaultdict(list)
    for r in ingress.values():arrived[(r['reserved'],r['ssrc'],r['timestamp'])].append(r)
    sources={(r['ssrc'],r['timestamp']):r for r in sent}
    renders=[r for r in rows if r['kind']==14 and
             (before_us is None or r['device_us']>=before_us) and
             (after_us is None or r['device_us']<=after_us)]
    resets=[r for r in rows if r['kind']==16]
    # Zero during fixed prefill does not advance the watermark. Require the
    # recorded reset plus the repeated watermark, rather than calling every
    # zero output a missing-media slot (notably after starvation resumption).
    legacy_prefill={i for i,r in enumerate(renders[:-1]) if r['flags']==2 and
        key(r)==key(renders[i+1]) and r['aux']==renders[i+1]['aux'] and
        any(key(r)==key(s) and 0<=r['device_us']-s['device_us']<100000 for s in resets)}
    # Kind 7 is explicit in new captures. The fallback is only for archived
    # schema-2 images, whose kind 2 conflated prefill and missing output.
    prefill={i for i,r in enumerate(renders) if r['flags']==7}
    explicit=any(r['kind']==17 for r in rows) or bool(prefill)
    if not explicit:prefill=legacy_prefill
    epochs={};epoch_for={}
    for r in rows:
        session=r.get('reserved',0)
        if r['kind']==17:epochs[session]=(r['value']<<32)|r['timestamp']
        elif r['kind']==14:epoch_for[id(r)]=epochs.get(session)
    latency=[];isolated=[];missing=Counter();ticks=defaultdict(list)
    for i,r in enumerate(renders):
        ticks[(r['reserved'],r['aux'],epoch_for.get(id(r)))].append(r['value'])
        source=(r['reserved'],r['ssrc'],r['timestamp']);packets=arrived[source]
        if r['flags']==0 and packets:
            latency.append((r['device_us']-min(p['device_us'] for p in packets))/1000)
        if r['flags'] not in (1,2) or i in prefill:continue
        if not packets:label='absent at authoritative ingress'
        elif min(p['device_us'] for p in packets)>r['device_us']:label='late at authoritative ingress'
        else:label='arrived before completion; admission/deadline requires separate inspection'
        missing[label]+=1
        target=sources.get((r['ssrc'],r['timestamp']))
        if not target or packets or r['flags']!=1 or not (0<i<len(renders)-1):continue
        neighbours=[renders[i-1],renders[i+1]]
        if any(n['flags']!=0 or n['ssrc']!=r['ssrc'] or n['aux']!=r['aux'] for n in neighbours):continue
        if [n['timestamp'] for n in neighbours]!=[(r['timestamp']-960)%2**32,(r['timestamp']+960)%2**32]:continue
        ontime=True
        for n in neighbours:
            a=arrived[(n['reserved'],n['ssrc'],n['timestamp'])]
            ontime &= bool(a) and min(p['device_us'] for p in a)<n['device_us']-5000
        if ontime:isolated.append(dict(timestamp=r['timestamp'],ssrc=r['ssrc'],seq=target['seq'],
            device_us=r['device_us'],missing_slots=1,plc_calls=1,post_plc_zeros=0,
            adjacent_survivors_decoded=True))
    bursts=[];i=1
    while i<len(renders)-1:
        if renders[i]['flags'] not in (1,2) or i in prefill:i+=1;continue
        begin=i
        while i+1<len(renders) and i+1 not in prefill and renders[i+1]['flags'] in (1,2) and             renders[i+1]['timestamp']==(renders[i]['timestamp']+960)%2**32 and             (renders[i+1]['reserved'],renders[i+1]['ssrc'],renders[i+1]['aux'])==            (renders[i]['reserved'],renders[i]['ssrc'],renders[i]['aux']):i+=1
        end=i;i+=1;run=renders[begin:end+1]
        if len(run)>12 or end+1>=len(renders):continue
        neighbours=[renders[begin-1],renders[end+1]]
        if any(n['flags']!=0 or n['ssrc']!=run[0]['ssrc'] or n['aux']!=run[0]['aux'] for n in neighbours):continue
        if neighbours[0]['timestamp']!=(run[0]['timestamp']-960)%2**32 or             neighbours[1]['timestamp']!=(run[-1]['timestamp']+960)%2**32:continue
        if any(arrived[(r['reserved'],r['ssrc'],r['timestamp'])] or             (r['ssrc'],r['timestamp']) not in sources for r in run):continue
        if any(not arrived[(n['reserved'],n['ssrc'],n['timestamp'])] or             min(a['device_us'] for a in arrived[(n['reserved'],n['ssrc'],n['timestamp'])])>=n['device_us']-5000             for n in neighbours):continue
        plc=sum(r['flags']==1 for r in run);zeros=sum(r['flags']==2 for r in run)
        bursts.append(dict(first_timestamp=run[0]['timestamp'],ssrc=run[0]['ssrc'],slots=len(run),
            plc_calls=plc,post_plc_zeros=zeros,passed=plc==min(len(run),5) and zeros==max(len(run)-5,0)))
    tick_duplicates=sum(len(v)-len(set(v)) for v in ticks.values())
    wire=sorted(boundaries[1].values(),key=lambda r:r['device_us'])
    first_sends={}
    for r in sorted(sent,key=lambda r:r['send_ms']):
        first_sends.setdefault((r['pc_id'],r['ssrc'],r['seq'],r['timestamp']),r)
    sendkey=defaultdict(list);sendtimes=defaultdict(list)
    for r in first_sends.values():
        sendkey[(r['ssrc'],r['seq'],r['timestamp'])].append(r)
        sendtimes[(r['pc_id'],r['ssrc'])].append(r['send_ms'])
    stalls=[]
    for a,b in zip(wire,wire[1:]):
        gap=(b['device_us']-a['device_us'])/1000
        if gap<=100:continue
        left=sendkey[(a['ssrc'],a['seq'],a['timestamp'])];right=sendkey[(b['ssrc'],b['seq'],b['timestamp'])]
        sender=maximum=None
        if len(left)==len(right)==1 and left[0]['pc_id']==right[0]['pc_id'] and a['ssrc']==b['ssrc']:
            sa,sb=left[0],right[0];sender=sb['send_ms']-sa['send_ms']
            times=sendtimes[(sa['pc_id'],sa['ssrc'])]
            window=times[bisect_left(times,sa['send_ms']):bisect_right(times,sb['send_ms'])]
            maximum=max((y-x for x,y in zip(window,window[1:])),default=None)
        intermediate=[];present=None;crosscheck='unavailable'
        if sender is not None:
            intermediate=[r for r in first_sends.values() if r['pc_id']==sa['pc_id'] and
                r['ssrc']==sa['ssrc'] and sa['send_ms']<r['send_ms']<sb['send_ms']]
            present=sum((a['reserved'],r['ssrc'],r['seq'],r['timestamp']) in boundaries[1]
                        for r in intermediate)
        if maximum is not None and maximum>=gap-20:basis='server pacing'
        elif intermediate and present==0:basis='loss burst (sent, never at wire)'
        elif sender is not None and (not intermediate or present==len(intermediate)):
            basis='delay stall (arrived late)'
        elif intermediate:basis='mixed loss and delay stall'
        else:basis='receiver-path gap; insufficient identity coverage'
        if tc_model is not None:
            model=' '.join(tc_model)
            if basis.startswith('loss burst'):
                crosscheck='consistent with loss model' if 'loss' in tc_model else 'loss observed without imposed loss'
            elif basis.startswith('delay stall'):
                crosscheck='consistent with delay/reorder model' if any(x in tc_model for x in ('delay','reorder')) else 'delay observed without imposed delay'
            else:crosscheck='not classified against model'
        stalls.append(dict(before_us=a['device_us'],after_us=b['device_us'],gap_ms=gap,
            sender_gap_ms=sender,sender_max_adjacent_gap_ms=maximum,basis=basis,
            intermediate_sent=len(intermediate),intermediate_at_wire=present,
            tc_model=tc_model,tc_crosscheck=crosscheck))
    return dict(records=len(rows),kinds=dict(Counter(r['kind'] for r in rows)),
        auth_to_ingress_ms=quantiles(hook_delay),ingress_unmatched_authenticated=len(set(ingress)-set(auth)),
        native_shadow_hold_ms=quantiles(shadow),native_callback_ambiguous_or_unmatched=ambiguous,
        ingress_to_render_completion_ms=quantiles(latency),render_kinds=dict(Counter(r['flags'] for r in renders)),
        duplicate_render_ticks=tick_duplicates,prefill_zero_decisions=len(prefill),
        prefill_basis="explicit kind" if explicit else "legacy repeated watermark heuristic",
        device_epochs=sorted({v for v in epoch_for.values() if v is not None}),
        renders_without_device_epoch=sum(epoch_for.get(id(r)) is None for r in renders),missing_attribution=dict(missing),isolated_loss_cases=isolated,
        isolated_loss_count=len(isolated),short_burst_cases=bursts,
        short_burst_cases_passed=all(r['passed'] for r in bursts),stalls_over_reserve=stalls,
        limits='Isolated census requires absent ingress and two on-time decoded neighbours; excludes stall releases and incomplete endpoints. Render record follows decode, not DAC. No absolute network one-way timing inferred.')


def analyze(directory):
    directory=Path(directory);raw=json.loads((directory/'result.json').read_text())
    before=raw.get('before',{}).get('perf',{});after=raw.get('after',{}).get('perf',{})
    audit=receive_audit(records(directory/'arrivals.jsonl'),records(directory/'rtp/downlink.jsonl'),
                        before.get('device_us'),after.get('device_us'),raw.get('netem'))
    audit['schema_available']=before.get('playout_schema')==after.get('playout_schema')==2
    audit['transport_deltas']=raw.get('transport_deltas')
    nacks=records(directory/'rtp/downlink-nack.jsonl');sent=records(directory/'rtp/downlink.jsonl')
    sends=Counter((r['pc_id'],r['ssrc'],r['seq'],r['timestamp']) for r in sent)
    audit['authenticated_nack']=dict(messages=len(nacks),requested_sequences=sum(len(r['lost']) for r in nacks),
        repeated_send_identities=sum(v-1 for v in sends.values()), # legacy observation-count alias
        unique_retransmitted_identities=sum(v>1 for v in sends.values()),
        successful_resend_observations=sum(v-1 for v in sends.values()),
        basis='aiortc authenticated handler and successful send observer; host pcap unavailable')
    if audit['schema_available']:
        a=before['playout'];b=after['playout']
        audit['source_conservation']=dict(media_advance=b['media_advance']-a['media_advance'],
            decoded=b['decoded_packets']-a['decoded_packets'],missing=b['missing_slots']-a['missing_slots'],
            drift_drop=b['drift_drop']-a['drift_drop'],trim=b['latency_trim']-a['latency_trim'],
            device_silence=b['device_silence_blocks']-a['device_silence_blocks'],
            decode_errors=b['decode_errors']-a['decode_errors'],
            endpoint_limit='PERF and decoder completion can differ by one in-flight READING lease')
        same_epoch=a['clock'].get('device_generation') is not None and a['clock'].get('device_generation')==b['clock'].get('device_generation')
        audit['device_conservation']=dict(available=same_epoch,
            device_generation_before=a['clock'].get('device_generation'),device_generation_after=b['clock'].get('device_generation'),
            completed_delta=b['clock']['completed_samples']-a['clock']['completed_samples'] if same_epoch else None,
            submitted_delta=b['clock']['submitted_samples']-a['clock']['submitted_samples'] if same_epoch else None,
            rendered_delta=b['render_decisions']-a['render_decisions'],
            silence_samples_delta=b['clock']['device_silence_samples']-a['clock']['device_silence_samples'] if same_epoch else None,
            partial_remainder_before=a['clock']['partial_remainder'],partial_remainder_after=b['clock']['partial_remainder'])
    return audit


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
    (a.directory/'single-buffer-analysis.json').write_text(json.dumps(analyze(a.directory),indent=2)+'\n')


def cpu_decision(aa_path,ab_path):
    """Whole-boot pairing avoids treating adjacent CPU windows as new boots."""
    import numpy as np
    def collect(path):
        data=json.loads(Path(path).read_text())
        if not data['completed']:raise ValueError('Incomplete CPU crossover')
        groups={'A':defaultdict(list),'B':defaultdict(list)};boots={'A':defaultdict(list),'B':defaultdict(list)};paired={};identities=[]
        for run in data['runs']:
            summary=run['summary']
            if not summary['stimulus_complete']:raise ValueError('Incomplete CPU stimulus')
            values=defaultdict(list)
            workers=None
            for window in summary['cpu_windows']:
                duplicate_workers=[t for t in window['tasks'] if t['name'].startswith('pipecat#')]
                mapping={}
                if duplicate_workers:
                    if len(duplicate_workers)!=2 or any((t.get('core'),t.get('priority'))!=(1,5) for t in duplicate_workers):
                        raise ValueError('Unexpected Pipecat worker census')
                    ordered=sorted(duplicate_workers,key=lambda t:int(t['name'].split('#')[1]))
                    names=[t['name'] for t in ordered]
                    if workers is not None and workers!=names:raise ValueError('Pipecat workers changed within CPU boot')
                    workers=names
                    # Client's EventLoop member constructs its thread before
                    # Impl's constructor starts _request_timer. Task numbers
                    # increase with creation; retries shift both IDs, not roles.
                    mapping=dict(zip(names,('pipecat-event-loop','pipecat-request-timer')))
                for task in window['tasks']:values[mapping.get(task['name'],task['name'])].append(task['cpu'])
            if workers:identities.append(dict(pair=run['pair'],arm=run['arm'],raw_to_role=dict(zip(workers,('pipecat-event-loop','pipecat-request-timer')))))
            paired[(run['pair'],run['arm'])]={name:float(np.median(v)) for name,v in values.items()}
            for name,v in values.items():
                groups[run['arm']][name]+=v;boots[run['arm']][name].append(float(np.median(v)))
        if set(groups['A'])!=set(groups['B']):raise ValueError('CPU task census differs')
        return data,groups,boots,paired,identities
    aa,ag,abo,ap,ai=collect(aa_path);ab,bg,bbo,bp,bi=collect(ab_path)
    if set(ag['A'])!=set(bg['A']):raise ValueError('A/A and A/B task census differs')
    rows={}
    q=lambda x,p:float(np.percentile(x,p,method='lower'))
    for name in ag['A']:
        if min(len(ag[a][name]) for a in ag)<8 or min(len(bg[a][name]) for a in bg)<8:
            raise ValueError('Fewer than eight CPU windows per arm')
        if min(len(abo[a][name]) for a in abo)<4 or min(len(bbo[a][name]) for a in bbo)<4:
            raise ValueError('Fewer than four fresh boots per arm')
        tail=10 if name.startswith('IDLE') else 90
        paired_noise=[abs(ap[(i,'B')][name]-ap[(i,'A')][name]) for i in sorted({r['pair'] for r in aa['runs']})]
        noise=max(abs(float(np.median(abo['B'][name])-np.median(abo['A'][name]))),float(np.median(paired_noise)))
        tail_noise=abs(q(ag['B'][name],tail)-q(ag['A'][name],tail))
        threshold=max(noise,5.0 if tail==10 else 2.0);tail_threshold=max(tail_noise,threshold)
        delta=float(np.median(bbo['B'][name])-np.median(bbo['A'][name]))
        tail_delta=q(bg['B'][name],tail)-q(bg['A'][name],tail)
        direction=-1 if tail==10 else 1
        rows[name]=dict(boots_per_arm={a:len(bbo[a][name]) for a in bbo},
            windows_per_arm={a:len(bg[a][name]) for a in bg},
            boot_median_A=float(np.median(bbo['A'][name])),boot_median_B=float(np.median(bbo['B'][name])),
            delta_pp=delta,aa_noise_pp=noise,threshold_pp=threshold,
            A_p90=q(bg['A'][name],90),B_p90=q(bg['B'][name],90),
            tail_percentile=tail,tail_delta_pp=tail_delta,aa_tail_noise_pp=tail_noise,tail_threshold_pp=tail_threshold,
            passed=delta*direction<=threshold and tail_delta*direction<=tail_threshold)
    return dict(passed=all(r['passed'] for r in rows.values()),tasks=rows,worker_identities=dict(aa=ai,ab=bi),
        basis='Four fresh boots per arm, alternating paired order; median of whole-boot medians and window upper/lower tails. Pipecat event-loop/request-timer roles remain distinct by verified constructor order; raw IDs retained.')


def onset_audit(directory):
    directory=Path(directory);raw=json.loads((directory/'result.json').read_text())
    sent=records(directory/'bot/rtp/downlink.jsonl');unique={}
    for r in sent:unique.setdefault((r['pc_id'],r['ssrc'],r['seq'],r['timestamp']),r)
    ordered=sorted(unique.values(),key=lambda r:r['send_ms']);rows=[]
    for turn in raw.get('turns',[]):
        starts=[e for e in turn.get('events',[]) if e['event']=='first-tts-byte']
        for e in starts:
            intervals=[dict(before=a['send_ms'],after=b['send_ms'],gap_ms=b['send_ms']-a['send_ms'],
                            timestamp_advance=(b['timestamp']-a['timestamp'])%2**32,
                            sequence_advance=(b['seq']-a['seq'])%65536)
                for a,b in zip(ordered,ordered[1:]) if a['pc_id']==b['pc_id']==e['pc_id'] and
                    a['ssrc']==b['ssrc'] and e['t']*1000-200<=b['send_ms']<=e['t']*1000+2000]
            gaps=[r['gap_ms'] for r in intervals]
            rows.append(dict(prompt=turn['prompt'],response_id=e.get('response_id'),first_byte_ms=e['t']*1000,
                send_gaps_ms=quantiles(gaps),gaps_over_reserve=[r for r in intervals if r['gap_ms']>100],
                board_stt=turn.get('board_stt'),strict_passed=turn.get('passed')))
    return dict(turns=len(raw.get('turns',[])),onsets=rows,
        auto_silence=[e for e in records(directory/'bot/events.jsonl') if e['event']=='audio-output-config'],
        limits='First successful send per full RTP identity; excludes retransmission duplicates. Onset window is first TTS byte minus 200 ms through plus 2 s. No sender or target change.')
