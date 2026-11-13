"""Prospective F1b evidence; never infer silence from missing observations."""


def zero_witness(before, after, playout, recorded=(), arrivals=(), window=None):
    pc=before.get('pc_id');lo,hi=before.get('device_us'),after.get('device_us')
    same=type(lo) is int and type(hi) is int and hi>lo and pc is not None and pc==after.get('pc_id') and type(before.get('generation')) is int and before['generation']==after.get('generation')
    # Conservative no-speech witness: a complete recorder epoch in which NO TTS
    # PCM has ever been written. Do not guess when earlier queued PCM drained or
    # call a low-power TTS packet silence. Live caller supplies records up to now.
    own=[r for r in recorded if r.get('pc_id')==pc]
    starts=[r for r in own if r.get('kind')=='session-start' and r.get('transport_write_witness') is True]
    covered=bool(window and len(starts)==1 and starts[0].get('monotonic_ns',float('inf'))/1e9<=window[0] and
        any(r.get('kind')=='audio' and r.get('stream')=='uplink' and window[1]-.1<=r.get('monotonic_ns',0)/1e9<=window[1] for r in own))
    no_speech=bool(same and covered and len(starts)==1 and any(r.get('kind')=='audio' and r.get('stream')=='uplink' for r in own)
        and not any(r.get('kind') in ('tts-transport-write','gap','session-end') or
                    r.get('kind')=='audio' and r.get('stream')=='tts' for r in own))
    states=[v.get('peer_profile',{}).get('trace',{}).get('enabled',False) for v in (before,after)]
    trace_enabled=any(states)
    trace=None;wholly_late=False
    if trace_enabled:
        from echoear_aec_link_soak import trace_bursts
        try:trace=trace_bursts(arrivals,before,after)
        except (KeyError,TypeError,ValueError):trace=dict(valid=False,reason='Incomplete trace/counter metadata')
        gen=before.get('generation',0)%65536
        missing={tuple(sorted(r.items())):r for r in arrivals if same and r['reserved']==gen and r['kind']==14 and r['flags'] in (1,2) and lo<r['device_us']<=hi}
        ingress=[r for r in arrivals if r['reserved']==gen and r['kind']==13]
        witnessed=sum(any(i['timestamp']==r['timestamp'] and i['device_us']>r['device_us'] for i in ingress) for r in missing.values())
        wholly_late=bool(same and all(states) and trace['valid'] and missing and witnessed==len(missing))
        trace.update(missing_slots=len(missing),ingress_witnessed_slots=witnessed)
    else:
        wholly_late=bool(same and type(playout.get('missing_slots')) is int and playout['missing_slots']>0 and
                         playout.get('late')==playout['missing_slots'])
    zeros=playout.get('post_plc_zeros')
    consistent=type(zeros) is int and type(playout.get('missing_slots')) is int and 0<=zeros<=playout['missing_slots']
    return dict(passed=same and consistent and (zeros==0 or no_speech or wholly_late),
                post_plc_zeros=zeros,no_speech_packet_due=no_speech,wholly_late=wholly_late,
                basis='Ingress for every missing Render slot' if trace_enabled else 'counter late == missing',trace=trace,
                no_speech_basis='recorder start before attempt with transport-write witness, uplink within 100ms of end, no TTS PCM/write/gap/end anywhere in epoch',
                raw_recorded=own,raw_arrivals=list(arrivals))
