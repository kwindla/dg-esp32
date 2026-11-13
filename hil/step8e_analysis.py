"""Compare successful device UDP sends and bot RTP delivery boundaries.

Legacy rtp.jsonl is after ICE queueing / SRTP processing, not raw socket receipt.
socket-rtp.jsonl is the separate asyncio UDP callback trace when available.
"""
def authenticated_socket_packets(socket_packets, delivered_packets):
    """Match public UDP header identities to subsequently authenticated RTP."""
    def key(row): return row['pc_id'], row['ssrc'], row['seq'], row['timestamp']
    delivered = {key(row) for row in delivered_packets}
    return [row for row in socket_packets if key(row) in delivered]


def cyclic_fixture_windows(decoded, source):
    """Score every complete block of a continuously repeated fixture.

    A later clean linear match cannot supply an initial phase modulo the
    source period: playout drops/idle reads may change phase in between.
    Use nonoverlapping source-length blocks, retain every block, and allow
    circular source phase within each block. This is repeated-content
    evidence, not a source-time alignment or a wall-clock continuity proof.
    """
    import numpy as np
    from scipy import signal
    decoded=np.asarray(decoded,dtype=float)
    source=np.asarray(source,dtype=float)
    size=len(source)
    if not size:
        return dict(windows=[],p50=None,minimum=None,clean_fraction=None,
                    trailing_samples=len(decoded),basis='Empty repeated fixture')
    if not len(decoded):
        return dict(windows=[],p50=None,minimum=None,clean_fraction=None,
                    trailing_samples=0,basis='Missing returned PCM; no content score available')
    sos=signal.butter(4,[500,1500],btype='bandpass',fs=16000,output='sos')
    # Filter a periodic extension so the cyclic source seam has the same
    # history as continuous fixture playback. Capture startup is retained.
    reference=signal.sosfilt(sos,np.tile(source,3))[size:2*size]
    observed=signal.sosfilt(sos,decoded)
    spectrum=np.fft.rfft(reference)
    reference_energy=float(np.dot(reference,reference))
    windows=[]
    for begin in range(0,len(observed)-size+1,size):
        block=observed[begin:begin+size]
        correlations=np.fft.irfft(np.fft.rfft(block)*np.conj(spectrum),n=size)
        at=int(np.argmax(correlations))
        energy=float(np.dot(block,block))
        score=float(correlations[at]/np.sqrt(max(energy,1)*max(reference_energy,1)))
        windows.append(dict(capture_offset_samples=begin,samples=size,
            source_phase_samples=at,correlation=score,passed=score>=.95))
    scores=[x['correlation'] for x in windows]
    return dict(windows=windows,p50=float(np.median(scores)) if scores else None,
        minimum=min(scores) if scores else None,
        clean_fraction=sum(x['passed'] for x in windows)/len(windows) if windows else None,
        trailing_samples=len(decoded)%size,
        basis='Every nonoverlapping source-length returned-PCM block; circular repeated-source phase, fixed 500–1500 Hz normalized matcher and 0.95 threshold; no block selection or wall-clock continuity claim')


def uplink_packet_summary(packets):
    groups={}
    for row in packets:groups.setdefault((row['pc_id'],row['ssrc']),[]).append(row)
    sessions=[]
    for (pc_id,ssrc),rows in groups.items():
        highest=rows[0]['seq'];seen=set()
        for packet in rows:
            seq=highest+(packet['seq']-highest%65536+32768)%65536-32768
            seen.add(seq);highest=max(highest,seq)
        sessions.append(dict(pc_id=pc_id,ssrc=ssrc,packets=len(rows),duplicates=len(rows)-len(seen),
            missing_lower_bound=max(seen)-min(seen)+1-len(seen)))
    return dict(packets=len(packets),sessions=sessions,
        missing_lower_bound=sum(r['missing_lower_bound'] for r in sessions) if sessions else None,
        max_gap_ms=max((b['observed_ms']-a['observed_ms'] for a,b in zip(packets,packets[1:])),default=None))


def device_cadence(device_records,packets,start,end,threshold_ms=60):
    """Include device sends lost in transit; bracket by matched RTP identities."""
    sends=sorted((r for r in device_records if r['kind']==12),key=lambda r:r['device_us'])
    indexed={(r['ssrc'],r['seq'],r['timestamp']):r for r in sends}
    prior=[p for p in packets if p['observed_ms']<=start*1000]
    later=[p for p in packets if p['observed_ms']>=end*1000]
    a=indexed.get((prior[-1]['ssrc'],prior[-1]['seq'],prior[-1]['timestamp'])) if prior else None
    b=indexed.get((later[0]['ssrc'],later[0]['seq'],later[0]['timestamp'])) if later else None
    covered=a is not None and b is not None
    pairs=[(x,y) for x,y in zip(sends,sends[1:]) if covered and
        x['device_us']<=b['device_us'] and y['device_us']>=a['device_us']]
    maximum=max(((y['device_us']-x['device_us'])/1000 for x,y in pairs),default=None)
    return dict(covered=covered,max_device_send_gap_ms=maximum,
        passed=covered and maximum is not None and maximum<=threshold_ms,
        basis='Successful lwIP sends, including packets absent at bot; scoring bracket mapped by RTP identities')


def recovery_bounds(records,uplink,downlink,release_begin,release_end,generation_mapping=None):
    """One-way transit is nonnegative; use matched RTP to bound clock offset."""
    mapping={int(tag):pc for tag,pc in (generation_mapping or {}).items()}
    def identity(r):return r['ssrc'],r['seq'],r['timestamp']
    def trace_key(r):
        return (mapping.get(r.get('reserved')),)+identity(r) if mapping else identity(r)
    def packet_key(r):return (r.get('pc_id'),)+identity(r) if mapping else identity(r)
    def unique_index(kind):
        groups={}
        for row in records:
            if row['kind']==kind:
                groups.setdefault(trace_key(row),{})[row['device_us']]=row
        return {key:next(iter(values.values())) for key,values in groups.items() if len(values)==1},sum(len(v)>1 for v in groups.values())
    send_index,ambiguous_up=unique_index(12)
    receive_index,ambiguous_down=unique_index(1)
    before=[p['send_ms']/1000-receive_index[packet_key(p)]['device_us']/1e6 for p in downlink
        if packet_key(p) in receive_index and abs(p['send_ms']/1000-release_end)<=10]
    after=[p['observed_ms']/1000-send_index[packet_key(p)]['device_us']/1e6 for p in uplink
        if packet_key(p) in send_index and abs(p['observed_ms']/1000-release_end)<=10]
    # Host packet clocks are integer milliseconds. Include that quantization
    # explicitly; bounds presume negligible device clock drift over 20 s.
    lower=max(before)-.001 if before else None
    upper=min(after)+.001 if after else None
    result=dict(clock_offset_lower_s=lower,clock_offset_upper_s=upper,
        downlink_matches=len(before),uplink_matches=len(after),
        generation_mapping=mapping,ambiguous_uplink_identities_excluded=ambiguous_up,
        ambiguous_downlink_identities_excluded=ambiguous_down,
        clock_basis='Matched host send/device receive and device send/host receive; nonnegative transit, 1 ms quantization, negligible clock drift over local 20 s window')
    resume=next((p for p in uplink if p['observed_ms']/1000>=release_begin),None)
    result['uplink_recovery_ms_bounds']=[max(0,resume['observed_ms']-release_end*1000),
        max(0,resume['observed_ms']-release_begin*1000)] if resume else None
    result['clock_bounds_valid']=lower is not None and upper is not None and lower<=upper
    if result['clock_bounds_valid']:
        wire=sorted((r for r in records if r['kind']==1),key=lambda r:r['device_us'])
        possible=next((r for r in wire if r['device_us']/1e6+upper>=release_begin),None)
        certain=next((r for r in wire if r['device_us']/1e6+lower>=release_begin),None)
        result['first_downlink_packet_unambiguous']=possible==certain and possible is not None
        if possible:
            result['downlink_recovery_ms_bounds']=[max(0,1000*(possible['device_us']/1e6+lower-release_end)),
                max(0,1000*((certain or possible)['device_us']/1e6+upper-release_begin))]
    return result


def uplink_gap_attribution(device_records,packets,start,end,threshold_ms=60):
    sent={(r['ssrc'],r['seq'],r['timestamp']):r for r in device_records if r['kind']==12}
    result=[]
    for a,b in zip(packets,packets[1:]):
        gap=b['observed_ms']-a['observed_ms']
        if gap<=threshold_ms or a['observed_ms']/1000>end or b['observed_ms']/1000<start:continue
        before=sent.get((a['ssrc'],a['seq'],a['timestamp']))
        after=sent.get((b['ssrc'],b['seq'],b['timestamp']))
        row=dict(bot_gap_ms=gap,start_s=a['observed_ms']/1000,end_s=b['observed_ms']/1000,
            seq_step=(b['seq']-a['seq'])%65536,device_send_gap_ms=None,
            attribution='UNKNOWN: missing device boundary',slow_calls=[])
        if before and after:
            device_gap=(after['device_us']-before['device_us'])/1000
            # Missing bot packets can span many successful sends. Their
            # endpoint interval alone does not establish an owner stall.
            observed=sorted((r for r in device_records if r['kind']==12 and
                before['device_us']<=r['device_us']<=after['device_us']),key=lambda r:r['device_us'])
            cadence=max(((y['device_us']-x['device_us'])/1000 for x,y in zip(observed,observed[1:])),default=None)
            slow=[r for r in device_records if r['kind'] in (8,11)
                and r['device_us']>=before['device_us'] and r['device_us']-r['value']<=after['device_us']]
            row.update(device_send_gap_ms=device_gap,device_max_send_gap_ms=cadence,slow_calls=slow,
                attribution=('After successful device lwIP send; network/driver or server ICE/SRTP processing unresolved'
                    if cadence is not None and cadence<=threshold_ms else
                    'Device send cadence stalled; overlapping owner timing retained' if slow else
                    'Device send cadence stalled; no owner interval over 40 ms'))
        result.append(row)
    return result
def session_state_audit(result):
    """A retained closed PERF identity is not proof of a live session."""
    import re
    start = result['started_s']
    end = start + result['elapsed_s']
    states = [entry for entry in result.get('state_history', [])
              if start <= entry['host_s'] <= end and
              re.search(r'dg-app: (SESSION FAIL |STATE (?:Disconnected|WifiConnecting)\b)', entry['line'])]
    observations = [result['before']] + result.get('samples', []) + [result['after']]
    closed = [sample['observed_monotonic_s'] for sample in observations
              if sample['perf'].get('tasks_exited') is True]
    replacements = result.get('reconnections', [])
    same_identity = result['initial_pc_id'] == result['after']['perf'].get('pc_id')
    ended = bool(states or closed or replacements or not same_identity)
    return dict(session_survived=not ended, terminal_states=states,
                sampled_closed_perf_times=closed,
                replacements_ready=len(replacements),
                retired_counter_tail_unknown=ended,
                original_session_survived=result.get('session_survived'),
                basis='State transitions and sampled closed-task flag override retained closed PERF identity; replacements count only observed Ready/live generations')
