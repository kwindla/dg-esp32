"""Loss estimates use send counts, not receive high water; timing domains matter."""
import base64
import struct
import netprobe


def test_stall_episodes_preserve_counts_spans_and_periods():
    times=[0,20,140,160,280,300,420]+list(range(440,304001,20))+[304020,304140,304160,304280,304300,304420]
    result=netprobe.stall_episodes([dict(arrival_ns=t*1_000_000) for t in times])
    assert result['stall_count']==6
    assert result['clustered_episode_count']==2
    assert result['episodes'][0]['stalls']==3
    assert result['episodes'][0]['span_ms']==400
    assert result['clustered_period_seconds']==[304]


def test_echo_off_keeps_downlink_loss_and_device_stall_timing(tmp_path):
    import json
    rows=[dict(kind='start',started_ns=9_000_000_000,pps=50),
          dict(kind='end',send_counts={'esp-0':3})]
    rows += [dict(kind='send',path='esp-0',seq=s,send_ns=9_000_000_000+s*20_000_000,lateness_ns=0,error=None) for s in range(3)]
    path=tmp_path/'host';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=netprobe.analyze_streams(path,device_rows=[dict(path=0,seq=s,arrival_us=t,send_errno=0) for s,t in [(0,1000),(2,121000)]],
        device_stats=dict(paths=[dict(sent=3,began_us=0,overwritten=0)]))
    down=result['esp-0']['downlink']
    assert result['esp-0']['clock_mapping'] is None
    assert down['loss_packets']==1 and down['unique_received']==2
    assert down['longest_gap_ms']==120
    assert down['stalls']['stall_count']==1


def test_leading_trailing_loss_and_burst_bins():
    rows=[dict(seq=s,arrival_ns=s*20_000_000) for s in [2,3,5,6]]
    result=netprobe.summarize_direction(rows,10,50,start_ns=0)
    assert result['loss_packets']==6
    assert result['burst_histogram']=={'1':1,'2-3':2,'4-10':0,'11-50':0,'>50':0}
    assert result['loss_events_per_minute']=={'0':3}
    assert result['jitter_ms']['max']==0


def test_loss_minute_buckets_follow_actual_host_send_time():
    result=netprobe.summarize_direction([],2,50,start_ns=0,send_times={0:0,1:61_000_000_000})
    assert result['loss_packets_per_minute']=={'0':1,'1':1}
    assert result['loss_events_per_minute']=={'0':1}


def test_sequence_wrap_reorder_and_duplicate():
    rows=[dict(seq=s) for s in [65534,0,65535,1,1]]
    assert [r['extended_seq'] for r in netprobe.sequence_extend(rows,16)]==[65534,65536,65535,65537,65537]


def test_clock_offset_uses_echo_midpoints_and_fastest_samples():
    rows=[dict(host_send_ns=100_000,arrival_ns=104_000,dev_rx_us=52,dev_tx_us=52,rtt_ns=4000),
          dict(host_send_ns=200_000,arrival_ns=240_000,dev_rx_us=152,dev_tx_us=152,rtt_ns=40000)]
    assert netprobe.clock_offset(rows)==dict(offset_ns=50000.0,uncertainty_ns=2000.0,samples=1)
    assert netprobe.clock_offset([]) is None


def test_binary_dump_preserves_send_failure_and_path():
    raw=struct.pack('<qIHH',100,7,1,0)+struct.pack('<qIHH',200,8,0,11)
    decoded=netprobe.decode_dump('NETPROBE_ROW '+base64.b64encode(raw).decode())
    assert decoded==[dict(path=1,seq=7,arrival_us=100,send_errno=0),dict(path=0,seq=8,arrival_us=200,send_errno=11)]


def test_udp_bootstrap_uses_observed_source_and_independent_streams():
    import selectors
    selector=selectors.DefaultSelector();rows=[]
    host=netprobe.Endpoint('probe-0,127.0.0.1,0',50,130,False,None,selector)
    port=host.sock.getsockname()[1]
    client=netprobe.Endpoint(f'probe-0,127.0.0.1,{port}',50,130,True,None,selector)
    try:
        host.send(netprobe.time.perf_counter_ns(),rows.append)
        assert not rows  # No ESP address is guessed or sent to before mapping.
        client.send(netprobe.time.perf_counter_ns(),rows.append)
        assert selector.select(1)
        host.receive(rows.append)
        assert host.remote==('127.0.0.1',client.sock.getsockname()[1])
        host.send(netprobe.time.perf_counter_ns(),rows.append)
        assert selector.select(1)
        client.receive(rows.append)
        assert selector.select(1)
        host.receive(rows.append)
        assert [r['kind'] for r in rows].count('echo')==1
        assert sum(r['kind']=='receive' for r in rows)==2
        assert host.seq==client.seq==1
    finally:
        host.sock.close();client.sock.close();selector.close()


def test_reference_and_device_denominators_are_independent(tmp_path):
    import json
    host_rows=[dict(kind='start',started_ns=0,pps=50),dict(kind='end',send_counts={'esp-0':1,'spark-0':1})]
    for path in ('esp-0','spark-0'):
        host_rows += [dict(kind='send',path=path,seq=0,send_ns=10000,lateness_ns=0,error=None),
                      dict(kind='receive',path=path,seq=0,arrival_ns=20000,send_clock=15),
                      dict(kind='echo',path=path,seq=0,host_send_ns=10000,arrival_ns=20000,rtt_ns=10000,dev_rx_us=15,dev_tx_us=15)]
    reference=[dict(kind='send',path='spark-0',seq=s,send_ns=s*20000000,lateness_ns=0,error=None) for s in range(3)]
    reference += [dict(kind='receive',path='spark-0',seq=0,arrival_ns=20000),dict(kind='end',send_counts={'spark-0':3})]
    def write(name,rows):
        path=tmp_path/name;path.write_text(''.join(json.dumps(r)+'\n' for r in rows));return path
    result=netprobe.analyze_streams(write('host',host_rows),device_rows=[dict(path=0,seq=0,arrival_us=20,send_errno=0)],
        device_stats=dict(paths=[dict(sent=2,began_us=0,overwritten=0)]),reference_path=write('client',reference))
    assert result['esp-0']['uplink']['expected']==2
    assert result['spark-0']['uplink']['expected']==3
    assert result['spark-0']['uplink']['loss_packets']==2
    assert result['esp-0']['downlink']['loss_packets']==0


def test_overwritten_ring_does_not_become_network_loss(tmp_path):
    import json
    rows=[dict(kind='start',started_ns=0,pps=50),dict(kind='end',send_counts={'esp-0':1}),
          dict(kind='send',path='esp-0',seq=0,send_ns=10000,lateness_ns=0,error=None),
          dict(kind='echo',path='esp-0',seq=0,host_send_ns=10000,arrival_ns=20000,rtt_ns=10000,dev_rx_us=15,dev_tx_us=15)]
    path=tmp_path/'host';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=netprobe.analyze_streams(path,device_rows=[],device_stats=dict(paths=[dict(sent=0,began_us=0,overwritten=1)]))
    assert result['esp-0']['downlink']['loss_pct'] is None
    assert not result['esp-0']['downlink']['capture_complete']


def test_failed_send_sequences_are_excluded_from_wire_loss():
    rows=[dict(seq=s,arrival_ns=s*20_000_000) for s in [0,1,5,7]]
    result=netprobe.summarize_direction(rows,8,50,excluded_sequences=[2,3,4])
    assert result['expected']==5 and result['attempted']==8
    assert result['loss_packets']==1 and result['loss_pct']==20
    assert result['burst_histogram']['1']==1


def test_serial_stop_tail_does_not_fake_network_jitter():
    rows=[dict(seq=s,arrival_ns=t) for s,t in enumerate([0,20_000_000,40_000_000,2_000_000_000])]
    result=netprobe.summarize_direction(rows,4,50,timing_window_ns=(0,50_000_000))
    assert result['loss_packets']==0
    assert result['longest_gap_ms']==20
    assert result['jitter_ms']['max']==0


def test_lan_stun_discovery_has_valid_xor_address_and_fingerprint():
    import socket
    import zlib
    request=struct.pack('!HHI12s',1,0,0x2112A442,b'123456789012')
    response=netprobe.stun_binding_response(request,('198.51.100.7',45678))
    kind,length,cookie,transaction=struct.unpack_from('!HHI12s',response)
    assert kind==0x0101 and length==len(response)-20 and transaction==b'123456789012'
    attr,alen,_,family,port,address=struct.unpack_from('!HHBBHI',response,20)
    assert attr==0x0020 and alen==8 and family==1
    assert port^(cookie>>16)==45678
    assert socket.inet_ntoa(struct.pack('!I',address^cookie))=='198.51.100.7'
    assert struct.unpack_from('!I',response,len(response)-4)[0]==zlib.crc32(response[:-8])^0x5354554e
    assert netprobe.stun_binding_response(request[:-1],('198.51.100.7',1)) is None
    assert netprobe.stun_binding_response(b'bad',('198.51.100.7',1)) is None


def test_unretained_local_failures_invalidate_only_post_success_loss(tmp_path):
    import json
    rows=[dict(kind='start',started_ns=0,pps=50),dict(kind='end',send_counts={'esp-0':1}),
          dict(kind='send',path='esp-0',seq=0,send_ns=10000,lateness_ns=0,error=None),
          dict(kind='echo',path='esp-0',seq=0,host_send_ns=10000,arrival_ns=20000,rtt_ns=10000,dev_rx_us=15,dev_tx_us=15)]
    path=tmp_path/'host';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=netprobe.analyze_streams(path,device_rows=[],device_stats=dict(paths=[dict(sent=2,began_us=0,overwritten=0,send_errors=1)]))
    up=result['esp-0']['uplink']
    assert up['loss_packets']==2 and up['loss_pct']==100
    assert not up['local_send_failure_capture_complete']
    assert up['wire_loss_pct'] is None and up['wire_loss_events'] is None


def test_reference_outliving_collector_is_not_network_loss(tmp_path):
    import json
    host=[dict(kind='start',started_ns=0,pps=50),dict(kind='end',ended_ns=35_000_000,send_counts={'spark-0':1}),
          dict(kind='send',path='spark-0',seq=0,send_ns=10000,lateness_ns=0,error=None),
          dict(kind='receive',path='spark-0',seq=0,arrival_ns=20000,send_clock=15),
          dict(kind='echo',path='spark-0',seq=0,host_send_ns=10000,arrival_ns=20000,rtt_ns=10000,dev_rx_us=15,dev_tx_us=15)]
    ref=[dict(kind='send',path='spark-0',seq=s,send_ns=s*20_000_000,lateness_ns=0,error=None) for s in range(3)]
    ref += [dict(kind='receive',path='spark-0',seq=0,arrival_ns=20000),dict(kind='end',send_counts={'spark-0':3})]
    def write(name,rows):
        p=tmp_path/name;p.write_text(''.join(json.dumps(r)+'\n' for r in rows));return p
    up=netprobe.analyze_streams(write('host',host),reference_path=write('ref',ref))['spark-0']['uplink']
    assert up['expected']==3
    assert up['reference_capture_covered'] is False
    assert up['loss_pct'] is None and up['loss_packets'] is None
    assert not up['loss_events']
