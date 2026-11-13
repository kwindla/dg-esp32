"""Device arrival v1 records and conservative loss/stall attribution.

Socket-read timestamps follow WiFi/lwIP buffering. They cannot distinguish AP
stalls from driver/lwIP stalls without independent radio capture.
"""
import base64
import json
import re
import struct
from collections import Counter
from pathlib import Path

RECORD = struct.Struct('<qIIIIHHBBH')
KINDS = {1:'wire',2:'authenticated',3:'callback',4:'plc',5:'rebuffer',
         6:'drift',7:'drop',8:'slow_loop',9:'wifi',10:'rtp_gap',11:'slow_call',12:'uplink_wire',13:'ingress',14:'render',15:'latency_trim',16:'reset',17:'device_epoch',18:'native_terminal',19:'dtls_application'}
DROP_REASONS=('invalid_audio','received_queue','invalid_data','message_queue',
    'encoded_queue','packet_pool','message_parse','event_queue','invalid_sdp',
    'offer_queue','worker_exception','outbound_queue','uplink_gap','send_cutoff')

def decode_line(encoded):
    raw=base64.b64decode(encoded,validate=True)
    if len(raw)%RECORD.size:raise ValueError('Partial arrival record')
    return [dict(zip(('device_us','timestamp','ssrc','value','aux','seq','bytes','kind','flags','reserved'),r))
            for r in RECORD.iter_unpack(raw)]

def drain(board,path):
    cursor=board.cursor;board.command('dump-arrivals',timeout=40)
    rows=[]
    for _,_,line in board.lines_since(cursor):
        match=re.fullmatch(r'ARRIVAL ([A-Za-z0-9+/=]+)',line.strip())
        if match:rows.extend(decode_line(match[1]))
    with Path(path).open('a') as stream:
        for row in rows:stream.write(json.dumps(row)+'\n')
    return rows

def generation_sessions(rows,server_packets,observed):
    """Map retained generation tags without assuming connection creation order.

    PERF maps observed sessions directly. For a retry between observations,
    only a unique authenticated downlink SSRC/server-session match can establish
    its identity. A stale packet at the socket boundary cannot establish it.
    """
    mapped={};evidence={};sources={}
    for pc,generation in observed.items():
        tag=generation%65536
        if tag in mapped and mapped[tag]!=pc:raise ValueError('Generation tag reused across observed sessions')
        mapped[tag]=pc;evidence[tag]='PERF session/generation'
    for packet in server_packets:sources.setdefault(packet['ssrc'],set()).add(packet['pc_id'])
    tags={r['reserved'] for r in rows}
    for tag in sorted(tags-mapped.keys()):
        packets=[r for r in rows if r['reserved']==tag and r['kind']==2]
        candidates=set().union(*(sources.get(r['ssrc'],set()) for r in packets)) if packets else set()
        if len(candidates)==1:
            mapped[tag]=next(iter(candidates));evidence[tag]='Unique authenticated RTP SSRC/server session match'
        elif any(r['reserved']==tag and r['kind'] in (4,5,6) for r in rows):
            raise ValueError('Unmapped or ambiguous generation contains playout events')
        else:evidence[tag]='Unmapped; no playout events'
    if len(set(mapped.values()))!=len(mapped):raise ValueError('Multiple generations map to the same server session')
    return mapped,evidence

def sequence_gaps(rows):
    """Extend 16-bit seq against newest; duplicates and reordering aren't loss."""
    groups={}
    for r in rows:groups.setdefault(r['ssrc'],[]).append(r)
    gaps=[]
    for ssrc,packets in groups.items():
        if not packets:continue
        highest=packets[0]['seq'];seen={}
        for r in packets:
            seq=highest+(r['seq']-highest%65536+32768)%65536-32768
            seen.setdefault(seq,r);highest=max(seq,highest)
        for seq in range(min(seen),max(seen)):
            if seq+1 not in seen:
                before=seen.get(seq)
                if before:
                    after=next((seen[x] for x in range(seq+2,max(seen)+1) if x in seen),None)
                    gaps.append(dict(ssrc=ssrc,first_missing=(seq+1)%65536,before=before,after=after))
    return gaps

def pts_index(packets,extended):
    highest={};indexed={}
    for r in packets:
        stamp=r['timestamp'];ssrc=r['ssrc']
        if extended:
            previous=highest.get(ssrc,stamp)
            stamp=previous+(stamp-previous%2**32+2**31)%2**32-2**31
            highest[ssrc]=max(previous,stamp)
        # Records follow arrival/send order. A late duplicate must not erase
        # evidence that the original packet crossed this boundary earlier.
        indexed.setdefault((stamp//48)%2**32,r)
    return indexed

CURRENT_DROP_REASONS=('invalid_data','message_queue','encoded_queue','message_parse',
    'event_queue','invalid_sdp','offer_queue','worker_exception','outbound_queue','uplink_gap','send_cutoff',
    'admission_budget','admission_shape','admission_heap','admission_overflow')

def attribute(rows,server_packets=()):
    # Device-epoch records identify 8e2a2 sessions with the cleaned drop enum.
    current_sessions={r['reserved'] for r in rows if r['kind']==17}
    def drop_reason(row):
        names=CURRENT_DROP_REASONS if row['reserved'] in current_sessions else DROP_REASONS
        return names[row['value']] if row['value']<len(names) else 'unknown'
    wire=[r for r in rows if r['kind']==1];auth=[r for r in rows if r['kind']==2]
    callbacks=[r for r in rows if r['kind']==3]
    # Opus RTP has a 48 kHz clock. Confirm the native millisecond PTS
    # relationship on this capture before using it to name missing frames.
    # Native implementations can extend the RTP clock before converting to
    # milliseconds. Validate both interpretations instead of misclassifying
    # every frame after a 32-bit RTP timestamp rollover.
    models=[(extended,pts_index(auth,extended)) for extended in (True,False)]
    extended,auth_by_pts=max(models,key=lambda model:sum(r['value'] in model[1] for r in callbacks))
    matches=sum(r['value'] in auth_by_pts for r in callbacks)
    calibrated=bool(callbacks) and matches==len(callbacks)
    wire_by_pts=pts_index(wire,extended)
    callback_by_pts={r['value']:r for r in callbacks}
    server_by_pts=pts_index(server_packets,extended)
    queue_drops=[dict(r,reason=drop_reason(r))
        for r in rows if r['kind']==7 and r['flags']==1]
    for r in queue_drops:
        if r['reason'] in ('admission_budget','admission_overflow') and r['aux']:
            r.update(native_frame_type=r['aux'] >> 24, message_bytes=r['aux'] & 0xffffff,
                     stream_id=r['timestamp'])
    rejected_pts={r['aux']:r for r in queue_drops if r['reason'] in ('received_queue','packet_pool')}
    invalid_pts={r['timestamp']:r for r in queue_drops if r['reason']=='invalid_audio'}
    server={(r['ssrc'],r['seq'],r['timestamp']):r for r in server_packets}
    # Missing wire vs authenticated records is an explicit boundary comparison.
    wire_keys={(r['ssrc'],r['seq'],r['timestamp']) for r in wire}
    auth_keys={(r['ssrc'],r['seq'],r['timestamp']) for r in auth}
    gaps=sequence_gaps(wire);stalls=[]
    for a,b in zip(wire,wire[1:]):
        gap=b['device_us']-a['device_us']
        if gap<60000:continue
        sa=server.get((a['ssrc'],a['seq'],a['timestamp']));sb=server.get((b['ssrc'],b['seq'],b['timestamp']))
        send_gap_ms=sb['send_ms']-sa['send_ms'] if sa and sb else None
        stalls.append(dict(before_us=a['device_us'],after_us=b['device_us'],gap_us=gap,
                           server_send_gap_ms=send_gap_ms,
                           source='server pacing' if send_gap_ms is not None and send_gap_ms*1000>=gap-20000 else 'receiver path stall; AP/WiFi/lwIP/owner unresolved'))
    # Callback PTS origin must be inferred from the first native audio delivery.
    # Native cache may already have retired packets; do not assume its RTP origin.
    events=[]
    for r in rows:
        if r['kind'] not in (4,5,6):continue
        nearby_gaps=[g for g in gaps if g['after'] and g['before']['device_us']-200000<=r['device_us']<=g['after']['device_us']+300000]
        nearby_stalls=[s for s in stalls if s['before_us']-200000<=r['device_us']<=s['after_us']+300000]
        slow=[s for s in rows if s['kind']==8 and r['device_us']-300000<=s['device_us']<=r['device_us']+100000]
        label='sequence gap at socket boundary' if nearby_gaps else 'arrival stall' if nearby_stalls else 'playout policy / native cache; no matching socket gap'
        frame_boundary=None
        if calibrated and r['kind']==4:
            pts=r['value'];w=wire_by_pts.get(pts);a=auth_by_pts.get(pts);c=callback_by_pts.get(pts);s=server_by_pts.get(pts)
            frame_boundary=dict(native_pts_ms=pts,wire_us=w['device_us'] if w else None,
                authenticated_us=a['device_us'] if a else None,callback_us=c['device_us'] if c else None,
                server_send_ms=s['send_ms'] if s else None)
            rejected=rejected_pts.get(pts);invalid=invalid_pts.get(pts)
            if rejected and rejected['device_us']<=r['device_us']:label='device incoming queue / payload pool rejected frame'
            elif invalid and invalid['device_us']<=r['device_us']:label='native callback supplied invalid audio frame'
            elif c and c['device_us']<=r['device_us']:label='playout policy: frame delivered before PLC decision'
            elif a and a['device_us']<=r['device_us']:label='native cache / callback scheduling: authenticated before PLC decision'
            elif w and w['device_us']<=r['device_us']:label='native receive / authentication: socket read before PLC decision'
            elif w:label='late arrival at socket boundary'
            elif s:label='sent by server, absent at socket boundary'
            else:label='no corresponding server packet in capture'
        if r['kind']==6:label='depth drift policy'
        events.append(dict(r,event=KINDS[r['kind']],attribution=label,
            sequence_gaps=nearby_gaps,arrival_stalls=nearby_stalls,slow_loops=slow,
            frame_boundary=frame_boundary))
    result=dict(records=len(rows),kind_counts=dict(Counter(KINDS.get(r['kind'],'unknown') for r in rows)),
        wire_sequence_gaps=gaps,wire_not_authenticated=len(wire_keys-auth_keys),
        authenticated_not_wire=len(auth_keys-wire_keys),arrival_stalls=stalls,events=events,
        queue_drops=queue_drops,
        native_pts_calibration=dict(callbacks=len(callbacks),matches=matches,exact=calibrated,
            rule='native_pts_ms = floor('+('extended ' if extended else '')+'RTP timestamp / 48) modulo 2^32'),
        decision_time_basis='Recorded device time immediately after playout poll, not the ideal scheduled deadline',
        limits='Attribution is temporal correlation, not proof of a specific radio or task cause. Socket observations follow driver/lwIP buffering. Clock offset is not used to infer one-way latency.')

    if any(r['kind']==14 for r in rows):
        from step8e2a_analysis import receive_audit
        result['authoritative_playout']=receive_audit(rows,server_packets)
        result['native_callback_role']='Shadow only; legacy callback-based event attribution does not describe the authoritative Playout.'
        result['decision_time_basis']='Render completion records use full RTP timestamp/source and unique device tick; legacy event labels retained only as shadow comparison.'
    return result


def split_session_archive(rows,generation_to_pc):
    """Re-split mixed drain files using recorded session tags, never filenames."""
    groups={}
    for row in rows:
        pc=generation_to_pc.get(row['reserved'])
        if pc is None:continue # Unmapped setup records remain in the raw archive.
        groups.setdefault(pc,[]).append(row)
    return groups
