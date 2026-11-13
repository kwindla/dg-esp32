#!/usr/bin/env python3
"""Raw UDP probe and reference client; standard library only on spark.

Wire v1: little endian <4sIQ>, padded to --bytes. NPD1 host downlink,
NPU1 independent device uplink, NPE1 downlink echo with <QQ> RX/TX us
appended. Host learns each peer from uplink before sending (NAT safe).
Every attempted send and receive is retained as JSONL, including failures.
"""
import argparse
import base64
import json
import math
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import time
import zlib

HEADER = struct.Struct('<4sIQ')
RECORD = struct.Struct('<qIHH')
BUCKETS = ('1', '2-3', '4-10', '11-50', '>50')


def quantiles(values):
    values = sorted(values)
    if not values:
        return dict(p50=None, p99=None, max=None)
    def q(p):
        return values[min(len(values)-1, max(0, math.ceil(len(values)*p)-1))]
    return dict(p50=q(.5), p99=q(.99), max=values[-1])


def burst_bucket(length):
    return 0 if length == 1 else 1 if length <= 3 else 2 if length <= 10 else 3 if length <= 50 else 4


def sequence_extend(records, bits=32):
    """Unwrap against high water; duplicates/reordering never advance it."""
    if not records:
        return []
    modulus = 1 << bits
    highest = records[0]['seq']
    result = []
    for row in records:
        seq = highest + (row['seq'] - highest + modulus//2) % modulus - modulus//2
        highest = max(highest, seq)
        result.append(dict(row, extended_seq=seq))
    return result


def stall_episodes(records, *, time_key='arrival_ns', threshold_ms=100, join_ms=1000, prominent_min_stalls=10):
    """Socket-drain gaps, not on-air arrival times. Group nearby stalls;
    report periodicity of groups with >=3 stalls separately from isolated gaps.
    No host/device clock mapping is needed for durations or periods.
    """
    stalls=[]
    for left,right in zip(records,records[1:]):
        gap=(right[time_key]-left[time_key])/1e6
        if gap>=threshold_ms:
            stalls.append(dict(start_ns=left[time_key],end_ns=right[time_key],gap_ms=gap))
    groups=[]
    for stall in stalls:
        if groups and stall['start_ns']-groups[-1]['end_ns']<=join_ms*1e6:
            group=groups[-1];group['end_ns']=stall['end_ns'];group['stalls']+=1
            group['max_gap_ms']=max(group['max_gap_ms'],stall['gap_ms'])
        else:
            groups.append(dict(start_ns=stall['start_ns'],end_ns=stall['end_ns'],stalls=1,max_gap_ms=stall['gap_ms']))
    for group in groups:group['span_ms']=(group['end_ns']-group['start_ns'])/1e6
    clustered=[group for group in groups if group['stalls']>=3]
    prominent=[group for group in groups if group['stalls']>=prominent_min_stalls]
    periods=[(b['start_ns']-a['start_ns'])/1e9 for a,b in zip(clustered,clustered[1:])]
    return dict(threshold_ms=threshold_ms,join_ms=join_ms,stall_count=len(stalls),
                episode_count=len(groups),clustered_episode_count=len(clustered),
                clustered_period_seconds=periods,episodes=groups,
                prominent_min_stalls=prominent_min_stalls,prominent_episode_count=len(prominent),
                prominent_period_seconds=[(b['start_ns']-a['start_ns'])/1e9 for a,b in zip(prominent,prominent[1:])],
                timestamp_boundary='socket drain')


def summarize_direction(records, expected, pps, *, time_key='arrival_ns', start_ns=None, send_times=None, excluded_sequences=(), timing_window_ns=None, timestamps_share_clock=True):
    """Known transmitted range includes leading and trailing loss. Never infer
    expected count from received maximum (which hides terminal blackouts).
    """
    excluded={s for s in excluded_sequences if 0<=s<expected}
    received = {r['seq'] for r in records if 0 <= r['seq'] < expected and r['seq'] not in excluded}
    missing = [s for s in range(expected) if s not in received and s not in excluded]
    hist = dict.fromkeys(BUCKETS, 0)
    events = []
    for seq in missing:
        if events and seq == events[-1]['last_seq']+1:
            events[-1]['last_seq'] = seq
            events[-1]['packets'] += 1
        else:
            events.append(dict(first_seq=seq, last_seq=seq, packets=1))
    minute_packets, minute_events = {}, {}
    for event in events:
        hist[BUCKETS[burst_bucket(event['packets'])]] += 1
        seq = event['first_seq']
        stamp = (send_times or {}).get(seq)
        if stamp is None and start_ns is not None:
            stamp = start_ns + seq * 1_000_000_000 // pps
        event['time_ns'] = stamp
        if stamp is not None and start_ns is not None:
            minute = str(max(0, int((stamp-start_ns)//60_000_000_000)))
            minute_events[minute] = minute_events.get(minute, 0)+1
        for missing_seq in range(event['first_seq'], event['last_seq']+1):
            loss_stamp = (send_times or {}).get(missing_seq, stamp)
            if loss_stamp is not None and start_ns is not None:
                minute = str(max(0, int((loss_stamp-start_ns)//60_000_000_000)))
                minute_packets[minute] = minute_packets.get(minute, 0)+1
    timing_records=[r for r in records if timing_window_ns is None or timing_window_ns[0]<=r[time_key]<=timing_window_ns[1]]
    gaps, jitter = [], []
    reorders = 0
    for a, b in zip(timing_records, timing_records[1:]):
        gap = (b[time_key]-a[time_key])/1e6
        gaps.append(gap)
        delta = b['seq']-a['seq']
        reorders += delta < 0
        if delta > 0:
            if send_times and a['seq'] in send_times and b['seq'] in send_times:
                interval = (send_times[b['seq']]-send_times[a['seq']])/1e6
            else:
                interval = delta*1000/pps
            jitter.append(abs(gap-interval))
    # Account for edge gaps when sender timestamps share this clock domain.
    if records and send_times and timing_window_ns is None and timestamps_share_clock:
        ordered = sorted(records, key=lambda r:r[time_key])
        if 0 in send_times:
            gaps.append(max(0, (ordered[0][time_key]-send_times[0])/1e6))
        if expected-1 in send_times:
            gaps.append(max(0, (send_times[expected-1]-ordered[-1][time_key])/1e6))
    denominator=expected-len(excluded)
    return dict(expected=denominator, attempted=expected, excluded_local_failures=len(excluded), unique_received=len(received), loss_packets=len(missing),
                loss_pct=100*len(missing)/denominator if denominator else None,
                duplicates=len(records)-len({r['seq'] for r in records}), reorders=reorders,
                burst_histogram=hist, longest_gap_ms=max(gaps, default=None),
                jitter_ms=quantiles(jitter), timing_window_ns=timing_window_ns, jitter_definition='absolute inter-arrival minus actual send interval (nominal sequence interval if unavailable)',
                loss_events=events, loss_events_per_minute=minute_events, loss_packets_per_minute=minute_packets,
                stalls=stall_episodes(timing_records,time_key=time_key))


def decode_dump(serial_text):
    result = []
    for line in serial_text.splitlines():
        if line.startswith('NETPROBE_ROW '):
            raw = base64.b64decode(line.split()[1], validate=True)
            if len(raw) % RECORD.size:
                raise ValueError('Incomplete probe record')
            for arrival, seq, path, reserved in RECORD.iter_unpack(raw):
                result.append(dict(path=path, seq=seq, arrival_us=arrival, send_errno=reserved))
    return result


def clock_offset(echo_rows):
    """NTP midpoint estimate from the fastest decile of echoes. Device time is
    never compared directly to host monotonic time. Return uncertainty too.
    """
    if not echo_rows:
        return None
    fastest = sorted(echo_rows, key=lambda r:r['rtt_ns'])[:max(1, len(echo_rows)//10)]
    offsets = [(r['host_send_ns']+r['arrival_ns'])/2 - (r['dev_rx_us']+r['dev_tx_us'])*500 for r in fastest]
    return dict(offset_ns=sorted(offsets)[len(offsets)//2], uncertainty_ns=max(r['rtt_ns'] for r in fastest)/2,
                samples=len(fastest))


def wifi_snapshot(interface):
    result = {}
    for name, command in [('link',['iw','dev',interface,'link']), ('station',['iw','dev',interface,'station','dump'])]:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=10)
            result[name] = dict(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[name] = dict(error=str(exc))
    return result


class Endpoint:
    def __init__(self, spec, pps, size, client, interface, selector):
        self.name, address, port = spec.split(',')
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if interface:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode()+b'\0')
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.setblocking(False)
        if client:
            self.sock.bind(('0.0.0.0',0));self.remote=(address,int(port))
        else:
            self.sock.bind((address,int(port)));self.remote=None
        self.client=client;self.seq=0;self.deadline=None;self.pps=pps;self.size=size
        self.period=1_000_000_000//pps
        selector.register(self.sock,selectors.EVENT_READ,self)

    def send(self, now, log):
        if self.remote is None:
            return
        if self.deadline is None:
            self.deadline=now
        if now < self.deadline:
            return
        stamp=time.perf_counter_ns()
        wire=HEADER.pack(b'NPU1' if self.client else b'NPD1', self.seq, stamp//1000 if self.client else stamp)
        error=None
        try:
            self.sock.sendto(wire.ljust(self.size,b'\0'),self.remote)
        except OSError as exc:
            error=str(exc)
        log(dict(kind='send',path=self.name,seq=self.seq,send_ns=stamp,lateness_ns=stamp-self.deadline,error=error))
        self.seq+=1;self.deadline+=self.period
        if self.deadline<=stamp:
            # Avoid artificial catch-up bursts; preserve overruns in evidence.
            self.deadline=stamp+self.period

    def receive(self, log):
        for _ in range(64):
            try:
                data, source=self.sock.recvfrom(2048)
                arrival=time.perf_counter_ns()
            except BlockingIOError:
                break
            if len(data)<HEADER.size:
                continue
            magic,seq,stamp=HEADER.unpack_from(data)
            expected=b'NPD1' if self.client else b'NPU1'
            if magic==expected and len(data)==self.size:
                if self.remote is None:
                    self.remote=source
                    log(dict(kind='peer',path=self.name,source=list(source),arrival_ns=arrival))
                if source!=self.remote:
                    log(dict(kind='unexpected_source',path=self.name,source=list(source),arrival_ns=arrival));continue
                log(dict(kind='receive',path=self.name,seq=seq,arrival_ns=arrival,send_clock=stamp))
                if self.client:
                    echo=bytearray(data);echo[:4]=b'NPE1';echo.extend(struct.pack('<QQ',arrival//1000,time.perf_counter_ns()//1000))
                    self.sock.sendto(echo,source)
            elif magic==b'NPE1' and not self.client and source==self.remote and len(data)==self.size+16:
                rx,tx=struct.unpack_from('<QQ',data,self.size)
                log(dict(kind='echo',path=self.name,seq=seq,arrival_ns=arrival,host_send_ns=stamp,
                         dev_rx_us=rx,dev_tx_us=tx,rtt_ns=arrival-stamp))


def run_streams(specs, duration, pps, size, out, *, client=False, interface=None, ready=None, finish=None):
    selector=selectors.DefaultSelector();endpoints=[];out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter_ns()
    try:
        with out.open('w', buffering=256*1024) as stream:
            def log(row):
                stream.write(json.dumps(row,separators=(',',':'))+'\n')
            for spec in specs:
                endpoints.append(Endpoint(spec,pps,size,client,interface,selector))
            log(dict(kind='start',clock='perf_counter_ns',started_ns=started,duration=duration,pps=pps,bytes=size,
                     role='client' if client else 'host',interface=interface))
            if ready:
                ready()
            started=time.perf_counter_ns();deadline=started+int(duration*1e9)
            while time.perf_counter_ns()<deadline:
                now=time.perf_counter_ns()
                for endpoint in endpoints:
                    endpoint.send(now,log)
                next_send=min([e.deadline for e in endpoints if e.deadline is not None]+[now+5_000_000])
                timeout=max(0,min(.005,(next_send-time.perf_counter_ns())/1e9))
                for key,_ in selector.select(timeout):
                    key.data.receive(log)
            # Let delayed downlink reach the device, then stop its transmitter
            # while host sockets remain open to collect every attempted send.
            grace=time.perf_counter_ns()+500_000_000
            while time.perf_counter_ns()<grace:
                for key,_ in selector.select(.005):
                    key.data.receive(log)
            if finish:
                finish()
            # Tail grace: no new sends, collect delayed packets and echoes.
            tail=time.perf_counter_ns()+1_500_000_000
            while time.perf_counter_ns()<tail:
                for key,_ in selector.select(.005):
                    key.data.receive(log)
            log(dict(kind='end',ended_ns=time.perf_counter_ns(),send_counts={e.name:e.seq for e in endpoints}))
    finally:
        for endpoint in endpoints:
            endpoint.sock.close()
        selector.close()


def analyze_streams(path, *, device_rows=None, device_stats=None, reference_path=None):
    rows=[json.loads(line) for line in Path(path).read_text().splitlines()]
    start=next(r for r in rows if r['kind']=='start');end=next(r for r in rows if r['kind']=='end')
    reference=[json.loads(line) for line in Path(reference_path).read_text().splitlines()] if reference_path else []
    ref_end=next((r for r in reference if r['kind']=='end'),None)
    # Both ESP sockets use the same timer. Sparse echo on just one socket is
    # sufficient for alignment; sequence loss remains valid with no echo at all.
    device_mapping=clock_offset([r for r in rows if r.get('kind')=='echo' and r.get('path','').startswith('esp-')])
    result={}
    for name, expected in end['send_counts'].items():
        group=[r for r in rows if r.get('path')==name]
        sent=[r for r in group if r['kind']=='send'];up=[r for r in group if r['kind']=='receive'];echo=[r for r in group if r['kind']=='echo']
        send_times={r['seq']:r['send_ns'] for r in sent};origin=min(send_times.values(),default=start['started_ns'])
        index=int(name.rsplit('-',1)[-1]);is_device=name.startswith('esp-')
        mapping=clock_offset(echo)
        if is_device and mapping is None:mapping=device_mapping
        active_end=max(send_times.values(),default=origin)
        active_echo=[r for r in echo if r['arrival_ns']<=active_end+500_000_000]
        result[name]=dict(sender_lateness_ms=quantiles([r['lateness_ns']/1e6 for r in sent]),
            send_errors=sum(r['error'] is not None for r in sent),rtt_ms=quantiles([r['rtt_ns']/1e6 for r in active_echo]),
            rtt_tail_samples_excluded=len(echo)-len(active_echo),
            clock_mapping=mapping,peer=next((r['source'] for r in group if r['kind']=='peer'),None))
        if mapping:
            # Per-minute estimates expose clock drift/asymmetry; the event
            # classifier must include this mapping uncertainty in its limits.
            bins={}
            for row in echo:
                minute=int((row['host_send_ns']-origin)//60_000_000_000)
                bins.setdefault(minute,[]).append(row)
            result[name]['clock_mapping_per_minute']={str(k):clock_offset(v) for k,v in bins.items()}
        if is_device and device_stats:
            up_expected=device_stats['paths'][index]['sent']
            failures=[r for r in (device_rows or []) if r['path']==index and r.get('send_errno')]
            up_origin=(device_stats['paths'][index]['began_us']*1000+mapping['offset_ns']) if mapping else origin
            tx_times={r['seq']:int(r['send_clock']*1000+mapping['offset_ns']) for r in up} if mapping else None
            timing_window=(origin,max(send_times.values(),default=origin))
            up_result=summarize_direction(up,up_expected,start['pps'],start_ns=up_origin,send_times=tx_times,timing_window_ns=timing_window)
            up_result['local_send_failures']=failures
            wire_up=summarize_direction(up,up_expected,start['pps'],start_ns=up_origin,send_times=tx_times,
                excluded_sequences=[r['seq'] for r in failures],timing_window_ns=timing_window)
            up_result['wire_loss_packets']=wire_up['loss_packets']
            up_result['wire_loss_pct']=wire_up['loss_pct']
            up_result['wire_burst_histogram']=wire_up['burst_histogram']
            up_result['wire_loss_events']=wire_up['loss_events']
            up_result['wire_loss_events_per_minute']=wire_up['loss_events_per_minute']
            up_result['wire_loss_packets_per_minute']=wire_up['loss_packets_per_minute']
            up_result['local_send_failure_capture_complete']=len(failures)==device_stats['paths'][index].get('send_errors',len(failures))
            if not up_result['local_send_failure_capture_complete']:
                # Attempt loss still has a known denominator. Post-success
                # loss/bursts cannot exclude unretained local failure sequences.
                for key in ('wire_loss_packets','wire_loss_pct','wire_burst_histogram','wire_loss_events','wire_loss_events_per_minute','wire_loss_packets_per_minute'):
                    up_result[key]=None
            result[name]['uplink']=up_result
            down=[dict(r,arrival_ns=int(r['arrival_us']*1000+(mapping['offset_ns'] if mapping else 0))) for r in (device_rows or []) if r['path']==index and not r.get('send_errno')]
            result[name]['downlink']=summarize_direction(down,expected,start['pps'],start_ns=origin,send_times=send_times,timestamps_share_clock=bool(mapping))
            result[name]['downlink']['arrival_clock']='host-mapped' if mapping else 'device timer'
            complete=device_stats['paths'][index]['overwritten']==0 and device_stats['paths'][index].get('outside',0)==0
            result[name]['downlink']['capture_complete']=complete
            if not complete:
                result[name]['downlink'].update(loss_pct=None,loss_packets=None,status='Incomplete device ring/bitmap; no valid loss estimate')
        elif not is_device and ref_end:
            ref=[r for r in reference if r.get('path')==name]
            ref_tx=[r for r in ref if r['kind']=='send']
            tx_times={r['seq']:int(r['send_ns']+mapping['offset_ns']) for r in ref_tx} if mapping else None
            up_origin=min(tx_times.values(),default=origin) if tx_times else origin
            result[name]['uplink']=summarize_direction(up,ref_end['send_counts'][name],start['pps'],start_ns=up_origin,send_times=tx_times,
                timing_window_ns=(origin,max(send_times.values(),default=origin)))
            covered=(max(tx_times.values())<=end['ended_ns']+mapping['uncertainty_ns']) if tx_times and 'ended_ns' in end else None
            result[name]['uplink']['reference_capture_covered']=covered
            if covered is False:
                result[name]['uplink'].update(loss_pct=None,loss_packets=None,loss_events=[],
                    status='Reference transmitter outlived host collector; missing tail is not network loss')
            result[name]['reference_sender_lateness_ms']=quantiles([r['lateness_ns']/1e6 for r in ref_tx])
            result[name]['reference_send_errors']=sum(r['error'] is not None for r in ref_tx)
            down=[dict(r,arrival_ns=int(r['arrival_ns']+(mapping['offset_ns'] if mapping else 0))) for r in ref if r['kind']=='receive']
            result[name]['downlink']=summarize_direction(down,expected,start['pps'],start_ns=origin,send_times=send_times,timestamps_share_clock=bool(mapping))
            result[name]['downlink']['capture_complete']=True
        else:
            result[name]['uplink']=dict(status='Expected send count needed from client; no loss estimate from receive maximum')
    return result


def stun_binding_response(data, source):
    """RFC 5389 discovery only, no relay or ICE credential handling."""
    if len(data)<20:
        return None
    kind,length,cookie,transaction=struct.unpack_from('!HHI12s',data)
    if kind!=1 or cookie!=0x2112A442 or length%4 or len(data)!=20+length:
        return None
    address=struct.unpack('!I',socket.inet_aton(source[0]))[0]
    xor=struct.pack('!HHBBHI',0x0020,8,0,1,source[1]^(cookie>>16),address^cookie)
    mapped=struct.pack('!HHBBHI',0x0001,8,0,1,source[1],address)
    response=struct.pack('!HHI12s',0x0101,len(xor)+len(mapped)+8,cookie,transaction)+xor+mapped
    fingerprint=zlib.crc32(response)^0x5354554e
    return response+struct.pack('!HHI',0x8028,4,fingerprint)


def run_stun(bind, port, duration, out):
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock, Path(out).open('w') as log:
        sock.bind((bind,port));sock.settimeout(.5)
        deadline=time.monotonic()+duration
        log.write(json.dumps(dict(kind='stun-start',bind=bind,port=port))+'\n');log.flush()
        while time.monotonic()<deadline:
            try:
                data,source=sock.recvfrom(2048)
            except socket.timeout:
                continue
            response=stun_binding_response(data,source)
            if response:
                sock.sendto(response,source)
                log.write(json.dumps(dict(kind='stun-binding',observed_ns=time.perf_counter_ns(),source=list(source)))+'\n');log.flush()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint',action='append',help='name,IP,port; host binds, client sends first')
    parser.add_argument('--stun-bind',help='Optional temporary LAN STUN discovery responder')
    parser.add_argument('--stun-port',type=int,default=3478)
    parser.add_argument('--client',action='store_true');parser.add_argument('--interface')
    parser.add_argument('--duration',type=float,default=600);parser.add_argument('--pps',type=int,default=50)
    parser.add_argument('--bytes',type=int,default=130);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.pps<=1000 or not 16<=args.bytes<=1200 or args.duration<=0:
        parser.error('pps 1..1000, bytes 16..1200, positive duration required')
    if args.stun_bind:
        run_stun(args.stun_bind,args.stun_port,args.duration,args.out);return
    if not args.endpoint:
        parser.error('--endpoint or --stun-bind required')
    run_streams(args.endpoint,args.duration,args.pps,args.bytes,args.out,client=args.client,interface=args.interface)

if __name__=='__main__':
    main()
