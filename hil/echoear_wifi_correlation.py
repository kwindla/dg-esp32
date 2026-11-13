"""Descriptive same-identity WiFi/receive-tail joins; no qualification score."""
from collections import Counter

from echoear_uplink_analysis import joined_intervals
from uplink_continuity import link_tail


def correlate(observation, windows, packets, traces, callback_packets=()):
    """Use device send-return identities to avoid inventing a clock offset.

    A sample's completion deltas cover(counter_begin_us,sample_end_us]. RSSI,
    PS and channel are discrete API reads within sample_begin/end, not values
    proven constant over that preceding counter interval. Right-send membership
    is a descriptive bin assignment, never a per-RTP completion/retry relation.
    """
    callback_index={}
    for p in callback_packets:
        callback_index.setdefault((p['pc_id'],p['ssrc'],p['seq'],p['timestamp']),p['observed_ms'])
    rows=observation['rows'];phases=[];bins={r['sequence']:dict(sample=r,receive_intervals=0,
        receive_tails=0) for r in rows}
    for window in windows:
        index=window['index'];pc=window['pc_id'];generation=window['generation']%65536
        trace=traces.get(index,[])
        own=[r for r in trace if r.get('reserved')==generation]
        joined=joined_intervals([p for p in packets if p['pc_id']==pc],own,
                                window['start_s'],window['end_s'])
        receive=link_tail([p for p in packets if p['pc_id']==pc],window['start_s'],window['end_s'])
        status=window.get('receive_capture_status',{})
        receive_valid=(receive['covered'] is True and status.get('post_window_reports',0)>0
                       and status.get('truncated') is False)
        eligible=[r for r in joined if r['category']=='eligible'];tails=[];matched=0;wifi_matched=0
        for interval in eligible:
            end=interval['device_after_us'];begin=interval['device_before_us']
            selected=[r for r in rows if end is not None and r['counter_begin_us']<end<=r['sample_end_us']]
            if begin is not None and end is not None:matched+=1
            if len(selected)==1:
                wifi_matched+=1;bucket=bins[selected[0]['sequence']]
                bucket['receive_intervals']+=1
                bucket['receive_tails']+=interval['host_gap_ms']>60
            if interval['host_gap_ms']>60:
                overlap=[r['sequence'] for r in rows if begin is not None and end is not None and
                         r['counter_begin_us']<max(begin,end) and r['sample_end_us']>=min(begin,end)]
                left_callback=callback_index.get((pc,*interval['before_identity']))
                right_callback=callback_index.get((pc,*interval['after_identity']))
                callback_gap=(right_callback-left_callback) if left_callback is not None and right_callback is not None else None
                tails.append(dict(interval,udp_callback_gap_ms=callback_gap,
                    authenticated_vs_callback_spacing_change_ms=interval['host_gap_ms']-callback_gap if callback_gap is not None else None,
                    right_send_sample=selected[0] if len(selected)==1 else None,
                    overlapping_counter_bins=overlap,metric_read_is_instantaneous=True,
                    tx_retries=None,send_to_air_us=None,channel_occupancy=None))
        sendrows=sorted([r for r in own if r['kind']==12],key=lambda r:r['device_us'])
        source_gaps=[b['device_us']-a['device_us'] for a,b in zip(sendrows,sendrows[1:]) if a['ssrc']==b['ssrc']]
        phases.append(dict(index=index,pc_id=pc,generation=window['generation'],
            start_s=window['start_s'],end_s=window['end_s'],receive_intervals=len(eligible),
            over60=len(tails),tail_rate=len(tails)/len(eligible) if eligible else None,
            receive_max_ms=max((r['host_gap_ms'] for r in eligible),default=None),
            exact_send_pairs_matched=matched,right_send_wifi_bins_matched=wifi_matched,
            all_retained_pairs_matched=bool(eligible) and matched==len(eligible),
            receive_endpoint_coverage=receive['endpoint_coverage'],receive_evidence_available=receive_valid,
            receive_capture_status=status,
            all_right_send_bins_matched=bool(eligible) and wifi_matched==len(eligible),
            foreign_generation_send_records=sum(r['kind']==12 and r.get('reserved')!=generation for r in trace),
            source_adjacent_gaps=dict(intervals=len(source_gaps),over60=sum(x>60000 for x in source_gaps),max_us=max(source_gaps,default=None)),
            tail_pairs=tails,mid_window_commands=window.get('mid_window_commands',[]),
            trace_overwritten=window.get('trace_overwritten')))
    for p in phases:
        p['correlation_available']=bool(p['receive_evidence_available'] and p['all_retained_pairs_matched']
            and p['all_right_send_bins_matched'] and p['trace_overwritten']==0
            and not p['mid_window_commands'] and observation['available']
            and observation['retained_history_not_overwritten'])
    available=len(phases)==3 and all(p['correlation_available'] for p in phases)
    used=[b for b in bins.values() if b['receive_intervals']]
    rssi=[b['sample']['rssi_dbm'] for b in used if b['sample']['ap_error']==0]
    return dict(qualification=False,scoring_performed=False,causal_attribution=False,
        correlation_available=available,status='DESCRIPTIVE_OBSERVATION' if available else 'INCONCLUSIVE_EVIDENCE',
        phases=phases,bins=used,availability=dict(
            tx_retries=None,send_to_air_us=None,channel_occupancy=None,
            callback_activity_observed=observation['callback_activity_observed'],
            retained_history_not_overwritten=observation['retained_history_not_overwritten']),
        observed_states=dict(rssi_min=min(rssi,default=None),rssi_max=max(rssi,default=None),
            ps_modes=dict(Counter(str(b['sample']['ps_mode']) for b in used)),
            channels=dict(Counter(str(b['sample']['primary_channel']) for b in used)),
            ap_read_errors=sum(b['sample']['ap_error']!=0 for b in used),
            ps_read_errors=sum(b['sample']['ps_error']!=0 for b in used)),
        limitations=['No observer-off control; observer cost and unconditional reliability unqualified',
            'RSSI/PS/channel are discrete reads; completion deltas cover coarse intervals and all STA traffic',
            'Native completion statuses are not retries, air/ACK timing, per-RTP statuses or channel occupancy',
            'Identity joins relate send-return boundaries; no absolute host/device clock offset or one-way latency is inferred',
            'Missing/overwritten samples or send identities remain explicit, without last-value filling or fitted windows'])
