#!/usr/bin/env python3
"""Analyze retained 8e0 captures, including conservative RTP boundary evidence."""
import argparse
from bisect import bisect_left
from collections import Counter
import json
from pathlib import Path
import re

import arrival_trace
import netprobe


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def unique_rtp_sends(records):
    """A retransmission is another attempt, not another missing audio packet.
    Keep the first-send time and use timestamp/SSRC to disambiguate seq wrap.
    """
    first={}
    for record in records:
        first.setdefault((record['ssrc'],record['seq'],record['timestamp']),record)
    return list(first.values())


def trace_rows(path):
    rows=[]
    for line in Path(path).read_text().splitlines():
        if line.startswith('ARRIVAL '):
            rows.extend(arrival_trace.decode_line(line.split()[1]))
    return rows


def near(times, stamp, tolerance=100_000_000):
    at=bisect_left(times,stamp)
    return any(abs(times[i]-stamp)<=tolerance for i in (at-1,at) if 0<=i<len(times))


def lost_times(direction, origin, pps=50):
    times=[]
    for event in direction.get('loss_events',[]):
        beginning=event['time_ns']
        if beginning is not None:
            times.extend(beginning+i*1e9/pps for i in range(event['packets']))
    return sorted(times)


def stack_delta(samples, stamp, offset):
    """Counter association bracket, not a causal claim. Snapshot interval is
    retained so one-second resolution is never presented as +/-100 ms proof.
    """
    ordered=sorted(samples,key=lambda r:r['stats']['device_us'])
    target=(stamp-offset)/1000
    at=bisect_left([r['stats']['device_us'] for r in ordered],target)
    if at==0 or at>=len(ordered):
        return dict(available=False)
    before,after=ordered[at-1]['stats'],ordered[at]['stats']
    bits=after.get('lwip_counter_bits',16);mod=1 << bits
    counters=('udp_drop','udp_memerr','ip_drop','ip_memerr','udp_recvmbox_full')
    deltas={k:(after[k]-before[k]) % (mod if k!='udp_recvmbox_full' else 1<<64) for k in counters}
    previous={row['local_port']:row['full'] for row in before.get('udp_recvmbox_ports',[])}
    per_port={str(row['local_port']):row['full']-previous.get(row['local_port'],0) for row in after.get('udp_recvmbox_ports',[])}
    return dict(available=True,interval_us=[before['device_us'],after['device_us']],deltas=deltas,per_port_deltas=per_port)


def audit_uplink(records, start, end, analysis):
    """Sequence-span audit: endpoint losses require an independent TX ledger.

    Infer gap times between received neighbors, not from the first packet's
    clock plus 20 ms: microphone suppression can legitimately pause RTP.
    """
    groups={}
    for row in records:
        # aiortc arrival_ms is its NTP-era clock. observed_ms is retained on
        # the same monotonic host clock as the probe and downlink ledger.
        row=dict(row,monotonic_ms=row.get('observed_ms',row['arrival_ms']))
        groups.setdefault((row['pc_id'],row['ssrc']),[]).append(row)
    streams=[];all_events=[]
    times={name:lost_times(dict(loss_events=record.get('uplink',{}).get('wire_loss_events',record.get('uplink',{}).get('loss_events',[])) or []),start) for name,record in analysis.items()}
    local_failures=sorted(int(f['arrival_us']*1000+record['clock_mapping']['offset_ns'])
        for name,record in analysis.items() if name.startswith('esp-') and record.get('clock_mapping')
        for f in record.get('uplink',{}).get('local_send_failures',[]))
    for (pc_id,ssrc),group in groups.items():
        extended=netprobe.sequence_extend(group,16)
        seen={}
        for row in extended:seen.setdefault(row['extended_seq'],row)
        ordered=sorted(seen)
        events=[]
        for a,b in zip(ordered,ordered[1:]):
            if b-a<=1:continue
            left=seen[a]['monotonic_ms']*1_000_000;right=seen[b]['monotonic_ms']*1_000_000
            if not (start<=left<=end and start<=right<=end):continue
            stamp=int(left+(right-left)/(b-a))
            if not start<=stamp<=end:continue
            matches={name:near(values,stamp) for name,values in times.items()}
            esp=matches.get('esp-0') or matches.get('esp-1')
            spark=matches.get('spark-0') or matches.get('spark-1')
            local=near(local_failures,stamp)
            category='TX side' if local else 'ESP hop (radio or TX queue)' if esp else 'unexplained'
            events.append(dict(first_seq=(a+1)%65536,last_seq=(b-1)%65536,packets=b-a-1,
                time_ns=stamp,arrival_bracket_ns=[left,right],category=category,probe_matches=matches,
                local_probe_send_failure_nearby=local,reference_client_loss_nearby=bool(spark)))
        active=[row for row in group if start<=row['monotonic_ms']*1_000_000<=end]
        active_extended=netprobe.sequence_extend(active,16)
        seqs={r['extended_seq'] for r in active_extended}
        expected=max(seqs)-min(seqs)+1 if seqs else 0
        spacing=[];variation=[];rfc_jitter=0
        for a,b in zip(active,active[1:]):
            gap=b['arrival_ms']-a['arrival_ms'];spacing.append(gap)
            media=((b['timestamp']-a['timestamp']+2**31)%2**32-2**31)/48
            delta=abs(gap-media);variation.append(delta);rfc_jitter+=(delta-rfc_jitter)/16
        histogram=dict.fromkeys(netprobe.BUCKETS,0)
        for event in events:histogram[netprobe.BUCKETS[netprobe.burst_bucket(event['packets'])]]+=1
        streams.append(dict(pc_id=pc_id,ssrc=ssrc,expected_sequence_span=expected,received_unique=len(seqs),
            lost_in_span=expected-len(seqs),loss_pct=100*(expected-len(seqs))/expected if expected else None,
            duplicate_records=len(active)-len(seqs),max_gap_ms=max(spacing,default=0),
            jitter_abs_transit_difference_ms=netprobe.quantiles(variation),rfc3550_final_jitter_ms=rfc_jitter,
            burst_histogram=histogram,events=events))
        all_events.extend(events)
    return dict(streams=streams,loss_bursts_by_category=dict(Counter(e['category'] for e in all_events)),
        missing_packets_by_category=dict(Counter({c:sum(e['packets'] for e in all_events if e['category']==c) for c in {e['category'] for e in all_events}})),
        limitation='Expected is the received RTP sequence span, not total ESP send attempts; leading/trailing losses are unobservable in this audit. Gap time is interpolated within the retained arrival bracket. ESP-only coincidence identifies a common ESP TX/link path, not proof of TX queue drops. Spark uses a different band/BSSID; shared loss is an infrastructure association, not proof of common 2.4 GHz airtime.')


def late_frame_summary(pipeline, rows, perf):
    frames=[]
    slow=[r for r in rows if r['kind']==8]
    for event in pipeline:
        if event['boundary']!='native jitter cache':continue
        auth=event['authenticated_us'];decision=event['device_us'];callback=event['callback_us']
        frames.append(dict(pts=event['value'],authenticated_us=auth,plc_us=decision,callback_us=callback,
            auth_to_plc_ms=(decision-auth)/1000,
            auth_to_callback_ms=(callback-auth)/1000 if callback is not None else None,
            callback_late_after_plc_ms=(callback-decision)/1000 if callback is not None else None,
            overlapping_slow_loop_us=[r['value'] for r in slow if r['device_us']-r['value']<= (callback or decision) and r['device_us']>=auth]))
    return dict(frames=len(frames),callback_observed=sum(f['callback_us'] is not None for f in frames),
        auth_to_plc_ms=netprobe.quantiles([f['auth_to_plc_ms'] for f in frames]),
        auth_to_callback_ms=netprobe.quantiles([f['auth_to_callback_ms'] for f in frames if f['callback_us'] is not None]),
        callback_late_after_plc_ms=netprobe.quantiles([f['callback_late_after_plc_ms'] for f in frames if f['callback_us'] is not None]),
        slow_loop_overlap_frames=sum(bool(f['overlapping_slow_loop_us']) for f in frames),events=frames,
        tasks=[dict(name=t['name'],core=t['core'],priority=t['priority']) for t in (perf[0].get('tasks',[]) if perf else [])],
        boundary_tasks=dict(authenticated='peer-loop on core 0',callback='peer-loop on core 0',plc='audio-playback on core 1'),
        limitation='Task identities at these boundaries follow the pinned transport code and PERF task/core inventory. There is no scheduler-switch trace; which task executed during the intervening delay cannot be reconstructed. SlowLoop is inclusive wall time and does not prove continuous peer-loop CPU execution.')


def slow_call_summary(rows):
    names=('main_loop','send_audio','send_data','control','wait','socket_recv','socket_send',
           'srtp_decrypt','srtp_encrypt','dtls_read','dtls_write','socket_select','lwip_select_nested','loop_iteration')
    calls=[r for r in rows if r['kind']==11]
    groups={}
    for row in calls:
        name=names[row['aux']] if row['aux']<len(names) else 'unknown'
        groups.setdefault(name,[]).append(row)
    return dict(parts={name:dict(calls_ge_40ms=len(group),calls_ge_120ms=sum(r['value']>=120000 for r in group),
                    wall_ms=netprobe.quantiles([r['value']/1000 for r in group])) for name,group in groups.items()},
                events=calls,limitation='Inclusive nested wall intervals; overlapping calls must not be added as exclusive CPU.')


def playout_trace_summary(rows, start, end, offset):
    active=[r for r in rows if start<=r['device_us']*1000+offset<=end]
    return dict(rebuffers=sum(r['kind']==5 for r in active),
                drift_markers=sum(r['kind']==6 for r in active),
                explicit_playout_drop_records=sum(r['kind']==7 and r['flags']==0 for r in active))


def callback_delivery_summary(auth_pts, callback_pts, rows, start, end, offset, calibrated):
    """Audit all authenticated frames, including ones without a PLC record.

    Authentication uses the decision window; callbacks may occur anywhere
    in the retained session. Require a tail covering the largest cache
    timeout supported by this campaign before calling a callback absent.
    """
    active={pts:row for pts,row in auth_pts.items()
            if start<=row['device_us']*1000+offset<=end}
    unmatched=sum(pts not in callback_pts for pts in active)
    tail_ms=(max((row['device_us'] for row in rows),default=0)*1000+offset-end)/1e6
    observable=calibrated and bool(active) and tail_ms>=300
    return dict(authenticated=len(active),without_callback=unmatched if observable else None,
                unmatched_in_retained_trace=unmatched,observable=observable,
                tail_after_window_ms=tail_ms,required_tail_ms=300,
                basis='All authentication records in the RTP decision window; callbacks throughout the retained session')


def attribute_rtp(directory, analysis):
    rows=trace_rows(directory/'trace-serial.log')
    sent=read_jsonl(directory/'rtp/downlink.jsonl')
    current_ssrc={r['ssrc'] for r in sent}
    selected_pairs=read_jsonl(directory/'rtp/ice-pairs.jsonl')
    # A remote host candidate exposes the ESP local port. A NAT srflx port
    # need not equal that local port and must not be guessed.
    known_voice_ports={str(pair['remote'][1]) for record in selected_pairs for pair in record['pairs'] if pair.get('remote_type')=='host'}
    current_tags={r['reserved'] for r in rows if r['kind']==2 and r['ssrc'] in current_ssrc}
    rows=[r for r in rows if r['reserved'] in current_tags]
    host=read_jsonl(directory/'host.jsonl')
    send_rows=[r for r in host if r['kind']=='send' and r['path'].startswith('esp-')]
    start=min(r['send_ns'] for r in send_rows);end=max(r['send_ns'] for r in send_rows)
    offset=analysis['esp-1']['clock_mapping']['offset_ns']
    samples=json.loads((directory/'device-samples.json').read_text())
    samples=[dict(stats=json.loads((directory/'device-before.json').read_text()))]+samples+[dict(stats=json.loads((directory/'device-after.json').read_text()))]
    attempts=[r for r in sent if start<=r['send_ms']*1_000_000<=end]
    sent=[r for r in unique_rtp_sends(sent) if start<=r['send_ms']*1_000_000<=end]
    key=lambda r:(r['ssrc'],r['seq'],r['timestamp'])
    wire={};auth={}
    for row in rows:
        if row['kind']==1:wire.setdefault(key(row),row)
        elif row['kind']==2:auth.setdefault(key(row),row)
    callbacks=[r for r in rows if r['kind']==3]
    models=[(extended,arrival_trace.pts_index(list(auth.values()),extended)) for extended in (True,False)]
    extended,auth_pts=max(models,key=lambda item:sum(r['value'] in item[1] for r in callbacks))
    calibration_matches=sum(r['value'] in auth_pts for r in callbacks)
    calibrated=bool(callbacks) and calibration_matches==len(callbacks)
    wire_pts=arrival_trace.pts_index(list(wire.values()),extended)
    callback_pts={}
    for row in callbacks:callback_pts.setdefault(row['value'],row)
    callback_delays=[(row['device_us']-auth_pts[pts]['device_us'])/1000 for pts,row in callback_pts.items() if calibrated and pts in auth_pts]
    probe_times={name:lost_times(record.get('downlink',{}),start) for name,record in analysis.items()}
    missing=[r for r in sent if key(r) not in wire]
    events=[]
    for packet in missing:
        if events and packet['ssrc']==events[-1]['ssrc'] and (packet['seq']-events[-1]['last_seq'])%65536==1:
            events[-1]['last_seq']=packet['seq'];events[-1]['packets']+=1
        else:
            events.append(dict(ssrc=packet['ssrc'],first_seq=packet['seq'],last_seq=packet['seq'],packets=1,
                               time_ns=packet['send_ms']*1_000_000))
    for event in events:
        stamp=event['time_ns'];window=[stamp,stamp+(event['packets']-1)*20_000_000]
        observed=next((row for row in rows if row['kind']==10 and row['ssrc']==event['ssrc'] and (row['aux']+1)%65536==event['first_seq']),None)
        event['observed_gap_device_us']=observed['device_us'] if observed else None
        event['observed_gap_host_ns']=int(observed['device_us']*1000+offset) if observed else None
        matches={name:any(near(times,t) for t in range(window[0],window[1]+1,20_000_000)) for name,times in probe_times.items()}
        stack=stack_delta(samples,event['observed_gap_host_ns'] or stamp,offset)
        if matches.get('esp-0') or matches.get('esp-1'):
            label='probe-associated receive loss'
            if matches.get('esp-0') and matches.get('esp-1'):
                location='common ESP receive path (radio or UDP mailbox); probe is not an RF witness'
            elif matches.get('esp-0'):
                location='two-hop path / boule WiFi association'
            else:
                location='one-hop-only association; ESP/AP/NAT unresolved'
            if matches.get('spark-0') or matches.get('spark-1'):
                location+='; reference-client loss also present (shared infrastructure; verify band/BSSID before calling this channel-wide)'
            else:
                location+='; ESP-only in this window'
        elif stack.get('available') and any(stack['deltas'].values()):
            label='counter-associated receive loss';location='Counter association in recorded snapshot interval'
        else:
            label='unexplained';location='No simultaneous probe loss or measured stack counter increment'
        # A missing burst follows a long socket-drain gap, then a tight burst
        # that fills a six-slot mailbox. Keep the timing evidence per event.
        ordered=sorted((r for r in wire.values() if r['ssrc']==event['ssrc']),key=lambda r:r['device_us'])
        limit=event['observed_gap_device_us']
        # The review's "97 ms" is rounded to integer milliseconds: the two
        # shortest qualifying gaps are 96.598 and 96.888 ms. Retain exact gaps
        # and this rounding threshold; the headline stall metric remains 100 ms.
        preceding=[dict(start_us=a['device_us'],end_us=b['device_us'],gap_ms=(b['device_us']-a['device_us'])/1000)
                   for a,b in zip(ordered,ordered[1:]) if limit is not None and 0<=limit-b['device_us']<=300000 and b['device_us']-a['device_us']>=96500]
        by_sequence={r['seq']:r for r in ordered}
        tail=[]
        for distance in range(1,7):
            packet=by_sequence.get((event['first_seq']-distance)%65536)
            if packet is None:break
            if tail and not 0<=tail[0]['device_us']-packet['device_us']<=25000:break
            tail.append(packet)
        contiguous_count=len(tail)
        left=by_sequence.get((event['first_seq']-1)%65536)
        if left:
            # Immediately preceding socket reads may straddle another missing
            # sequence in the same burst. Count reads as well as sequence runs.
            tail=[r for r in ordered if 0<=left['device_us']-r['device_us']<=25000][-6:][::-1]
        if preceding:
            label='stall/burst receive loss'
            location='socket drain stalled before missing burst; per-port counters distinguish mailbox overflow from other loss'
        socket_overflows={port:count for port,count in stack.get('per_port_deltas',{}).items() if port in known_voice_ports and count>0}
        if socket_overflows:
            label='UDP receive mailbox overflow'
            location='voice local port overflow in retained counter bracket; temporal association, not packet-by-packet drop identity'
        event.update(category=label,location=location,probe_matches=matches,stack=stack,
                     preceding_socket_stalls=preceding,preceding_packets_within_25ms=len(tail),
                     preceding_contiguous_sequences_within_25ms=contiguous_count,
                     preceding_burst_span_ms=(tail[0]['device_us']-tail[-1]['device_us'])/1000 if tail else None,
                     voice_port_overflow_bracket=socket_overflows)
    # Pipeline events are a separate unit from actual missing RTP packets.
    # Count one explicit drop, or one PLC decision with the frame already seen,
    # and never double-count the associated Drift marker as another drop.
    pipeline=[];plc=[];unknown_plc=[]
    for row in rows:
        stamp=int(row['device_us']*1000+offset)
        if not start<=stamp<=end or row['kind'] not in (4,7):
            continue
        if row['kind']==7:
            pts=row['aux'] if row['flags']==1 and row['value'] in (1,5) else row['value'] if row['flags']==0 else None
            packet=auth_pts.get(pts) if calibrated and pts is not None else None
            callback=callback_pts.get(pts) if pts is not None else None
            if packet and packet['device_us']<=row['device_us']:
                pipeline.append(dict(row,time_ns=stamp,category='our pipeline',boundary='explicit queue/playout drop after authenticated arrival',
                                     authenticated_us=packet['device_us'],callback_us=callback['device_us'] if callback else None))
        else:
            pts=row['value'];packet=auth_pts.get(pts) if calibrated else None;callback=callback_pts.get(pts)
            boundary='unknown'
            if packet and packet['device_us']<=row['device_us']:
                boundary='playout' if callback and callback['device_us']<=row['device_us'] else 'native jitter cache'
                pipeline.append(dict(row,time_ns=stamp,category='our pipeline',boundary=boundary,authenticated_us=packet['device_us'],
                                     callback_us=callback['device_us'] if callback else None))
            elif wire_pts.get(pts):
                boundary='late socket arrival' if wire_pts[pts]['device_us']>row['device_us'] else 'authentication/native receive'
                unknown_plc.append(dict(row,time_ns=stamp,boundary=boundary))
            else:
                boundary='absent at socket'
            plc.append(dict(row,time_ns=stamp,boundary=boundary))
    counts=Counter(e['category'] for e in events)
    mailbox_events=[];previous=samples[0]['stats']['udp_recvmbox_full']
    for sample in samples[1:]:
        snapshot=sample['stats']
        if snapshot['udp_recvmbox_full']>previous:
            mailbox_events.append(dict(device_us=snapshot['udp_recvmbox_last_us'],
                count_delta=snapshot['udp_recvmbox_full']-previous,snapshot_device_us=snapshot['device_us']))
        previous=snapshot['udp_recvmbox_full']
    gap_notifications=[r for r in rows if r['kind']==10 and start<=r['device_us']*1000+offset<=end]
    before_after=[]
    for file in ('device-before.json','device-after.json'):
        before_after.append(json.loads((directory/file).read_text()))
    max_overwritten=0
    serial=(directory.parent/'serial.log').read_text(errors='replace')
    # PERF trace.written/overwritten is the ring integrity evidence; keep all
    # snapshots with this session's pc_id rather than estimating from row count.
    pc_ids={r['pc_id'] for r in sent}
    perf=[]
    for line in serial.splitlines():
        if line.startswith('PERF '):
            try:
                record=json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if record.get('pc_id') in pc_ids:
                perf.append(record)
                max_overwritten=max(max_overwritten,record.get('peer_profile',{}).get('trace',{}).get('overwritten',0))
    uplink=audit_uplink(read_jsonl(directory/'rtp/rtp.jsonl'),start,end,analysis)
    lateness=late_frame_summary(pipeline,rows,perf)
    active_rows=[row for row in rows if start<=row['device_us']*1000+offset<=end]
    result=dict(uplink=uplink,late_frames=lateness,slow_calls=slow_call_summary(active_rows),
        callback_delivery=callback_delivery_summary(auth_pts,callback_pts,rows,start,end,offset,calibrated),
        playout_trace=playout_trace_summary(rows,start,end,offset),
        auth_to_callback_ms=netprobe.quantiles(callback_delays),auth_to_callback_samples=len(callback_delays),
        path_stalls={name:{direction:record.get(direction,{}).get('stalls') for direction in ('downlink','uplink')} for name,record in analysis.items()},
        window_host_ns=[start,end],duration_minutes=(end-start)/60e9,rtp_sent=len(sent),
        rtp_send_attempts=len(attempts),rtp_repeat_attempts=len(attempts)-len({key(r) for r in attempts}),
        rtp_absent_wire=len(missing),rtp_loss_bursts=len(events),rtp_loss_bursts_by_category=dict(counts),
        rtp_missing_packets_by_category=dict(Counter({label:sum(e['packets'] for e in events if e['category']==label) for label in counts})),
        rtp_loss_examples=events[:30],rtp_loss_events=events,wire_gap_notifications=len(gap_notifications),
        preceding_stall_threshold_us=96500,
        reordered_gap_notifications_may_heal=True,pipeline_events=len(pipeline),pipeline_boundaries=dict(Counter(e['boundary'] for e in pipeline)),
        pipeline_examples=pipeline[:30],plc_frames=len(plc),plc_boundaries=dict(Counter(e['boundary'] for e in plc)),
        unexplained_plc_examples=unknown_plc[:30],trace_records=len(rows),native_pts_calibration=dict(exact=calibrated,callbacks=len(callbacks),matches=calibration_matches,extended=extended),
        trace_overwritten=max_overwritten,trace_integrity_verified=bool(perf) and max_overwritten==0,udp_mailbox_events=mailbox_events,perf_snapshots=perf,selected_pairs=selected_pairs,
        limitation='Probe loss is temporal association, not an RF witness: probes and RTP can overflow separate UDP mailboxes after the same stall. Old captures have only global stack counters sampled about once a second. Authenticated/no-callback timing locates damage at the native cache boundary; overlap with an inclusive slow loop does not prove scheduler starvation. Socket timestamps measure drain, not air arrival. PHY rate is unavailable through the public station API.')


    if any(r['kind']==14 for r in rows):
        from step8e2a_analysis import receive_audit
        result['authoritative_playout']=receive_audit(rows,sent)
        result['native_callback_role']='Shadow only; legacy native-cache boundary classifications are not authoritative playback attribution.'
    return result


def wifi_dump_blocks(serial):
    blocks=[];active=None
    for line in serial.splitlines():
        if 'wifi: BUFFER:' in line or (active is None and 'wifi: t ' in line and 'total_alloc' in line):
            active=[]
        if active is not None:
            active.append(line)
            if re.search(r'^OK #\d+ wifi-stats',line):
                blocks.append('\n'.join(active)+'\n');active=None
    return blocks


def parse_wifi_dump(text):
    buffers={};lmac=[];mode=None
    for line in text.splitlines():
        if 'BUFFER:' in line or ('total_alloc' in line and 'fail_fc' in line):mode='buffer'
        elif 'wifi: LMAC TX:' in line:mode='lmac'
        elif 'wifi: LMAC RX:' in line:mode=None
        match=re.search(r'wifi: (.*)',line)
        if not match:continue
        fields=match[1].split()
        if mode=='buffer' and len(fields)==7 and all(v.isdecimal() for v in fields):
            buffers[fields[0]]=dict(zip(('max','alloc','total_alloc','total_free','fail_fc','fail_oom'),map(int,fields[1:])))
        elif mode=='lmac' and len(fields)==5 and all(v.isdecimal() for v in fields):
            lmac.append(dict(zip(('tx_all','lrc','src','age','to'),map(int,fields))))
    return dict(buffers=buffers,lmac_tx=lmac)


def wifi_counters(directory):
    files=[directory/'wifi-tx-before.log',directory/'wifi-tx-after.log']
    if not all(f.exists() for f in files):
        blocks=wifi_dump_blocks((directory.parent/'serial.log').read_text(errors='replace'))
        # Completed arms in one controller share a serial log, in arm order.
        arms=[d for d in directory.parent.iterdir() if d.is_dir() and (d/'device-after.json').exists()]
        arms.sort(key=lambda d:json.loads((d/'device-before.json').read_text())['device_us'])
        index=arms.index(directory)*2
        if len(blocks)<index+2:return dict(available=False)
        for file,block in zip(files,blocks[index:index+2]):file.write_text(block)
    before,after=[parse_wifi_dump(f.read_text()) for f in files]
    delta={}
    for kind,counters in after['buffers'].items():
        if kind in before['buffers']:
            delta[kind]={key:counters[key]-before['buffers'][kind][key] for key in ('total_alloc','total_free','fail_fc','fail_oom')}
    lmac=[{key:a[key]-b[key] for key in a} for b,a in zip(before['lmac_tx'],after['lmac_tx'])]
    return dict(available=bool(delta),before=before,after=after,buffer_deltas=delta,lmac_tx_deltas=lmac,
        limitation='Vendor dump buffer type numbers are retained verbatim. Type 1 has max/alloc 16, matching this image static TX pool; fail_fc is flow-control pressure, not a one-to-one dropped-packet count. Driver pool failures and socket ENOMEM are distinct and must not be added as independent lost packets.')


def host_hop(directory):
    before=json.loads((directory/'host-before.json').read_text());after=json.loads((directory/'host-after.json').read_text())
    result={'before':before,'after':after,'counter_deltas':{}}
    for label in ('tx retries','tx failed','rx drop misc','beacon loss'):
        values=[]
        for snap in (before,after):
            match=re.search(r'^\s*'+re.escape(label)+r':\s*(\d+)',snap['station'].get('stdout',''),re.M)
            values.append(int(match[1]) if match else None)
        result['counter_deltas'][label]=values[1]-values[0] if None not in values else None
    ping=(directory/'gateway-ping.txt').read_text()
    match=re.search(r'(\d+) packets transmitted, (\d+) received.*?([\d.]+)% packet loss',ping)
    result['gateway_ping']=dict(transmitted=int(match[1]),received=int(match[2]),loss_pct=float(match[3])) if match else dict(status='No ping summary; raw capture retained')
    result['gateway_ping']['rtt_ms']=netprobe.quantiles([float(s) for s in re.findall(r'time=([\d.]+) ms',ping)])
    result['gateway_ping']['request_interval_ms']=20
    return result


def analyze_run(directory):
    after=json.loads((directory/'device-after.json').read_text())
    device=netprobe.decode_dump((directory/'probe-dump.log').read_text())
    analysis=netprobe.analyze_streams(directory/'host.jsonl',device_rows=device,device_stats=after,reference_path=directory/'spark.jsonl')
    (directory/'analysis.json').write_text(json.dumps(analysis,indent=2)+'\n')
    result=dict(wifi_tx=wifi_counters(directory),metadata=json.loads((directory/'metadata.json').read_text()),paths=analysis,device_after=after,
                device_before=json.loads((directory/'device-before.json').read_text()),host_hop=host_hop(directory))
    if (directory/'rtp/downlink.jsonl').exists():
        result['rtp']=attribute_rtp(directory,analysis)
        (directory/'rtp-attribution.json').write_text(json.dumps(result['rtp'],indent=2)+'\n')
    (directory/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('runs',nargs='+',type=Path)
    a=parser.parse_args()
    for directory in a.runs:
        result=analyze_run(directory)
        print(directory.name,json.dumps({k:dict(down=v.get('downlink',{}).get('loss_pct'),up=v['uplink'].get('loss_pct')) for k,v in result['paths'].items()}),flush=True)
        if 'rtp' in result:
            print('RTP',result['rtp']['rtp_missing_packets_by_category'],'pipeline',result['rtp']['pipeline_boundaries'],flush=True)

if __name__=='__main__':
    main()
