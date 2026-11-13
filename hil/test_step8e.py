import json
import pytest
from netem import commands
from failure_snapshot import capture_failure
from uplink_continuity import score_uplink

@pytest.mark.parametrize('retry',[False,True])
def test_physical_rejoin_allows_only_explained_serial_retries(retry):
    from step8e_rejoin import rejoin_sequence
    lines=['STATE WifiConnecting gen=2 desired=1 t=1000000','WIFI READY',
           'STATE Starting gen=3 desired=1 t=2000000']
    if retry:
        lines+=['SESSION FAIL gen=3 reason=HTTP open failed retry_ms=2000',
                'STATE Disconnected gen=4 desired=1 t=2500000',
                'STATE Starting gen=5 desired=1 t=4505000']
    lines+=['STATE Ready gen='+('5' if retry else '3')+' desired=1 t=6000000']
    assert rejoin_sequence(lines)['passed']

@pytest.mark.parametrize('fault',['no_failure','early_retry','second_wifi'])
def test_physical_rejoin_still_rejects_spurious_restart_and_broken_backoff(fault):
    from step8e_rejoin import rejoin_sequence
    lines=['STATE WifiConnecting gen=2 desired=1 t=1000000','WIFI READY',
           'STATE Starting gen=3 desired=1 t=2000000']
    if fault!='no_failure':lines+=['SESSION FAIL gen=3 reason=HTTP open failed retry_ms=2000']
    lines+=['STATE Disconnected gen=4 desired=1 t=2500000']
    if fault=='second_wifi':lines+=['STATE WifiConnecting gen=6 desired=1 t=3000000','WIFI READY']
    lines+=['STATE Starting gen=7 desired=1 t='+('2510000' if fault=='early_retry' else '4505000'),
            'STATE Ready gen=7 desired=1 t=6000000']
    assert not rejoin_sequence(lines)['passed']

def test_udp_callback_trace_matches_authenticated_identity_and_generation():
    from step8e_analysis import authenticated_socket_packets
    delivered=[dict(pc_id='one',ssrc=7,seq=2,timestamp=960,observed_ms=1120)]
    socket=[dict(delivered[0],observed_ms=1000),
            dict(delivered[0],pc_id='other',observed_ms=1000),
            dict(delivered[0],timestamp=1920,observed_ms=1000)]
    assert authenticated_socket_packets(socket,delivered)==[socket[0]]

@pytest.mark.parametrize('state,expected',[('Starting',True),('Disconnected',True),('Ready',False)])
def test_periodic_perf_unavailable_during_reconnect_is_retained_not_a_hang(monkeypatch,state,expected):
    import step8e_matrix
    from types import SimpleNamespace
    def unavailable(*args,**kwargs):raise RuntimeError('ERR #9 perf')
    monkeypatch.setattr(step8e_matrix,'snapshot',unavailable)
    board=SimpleNamespace(lines_since=lambda cursor:[(1,1.0,'I dg-app: STATE '+state)])
    result={}
    if expected:
        assert step8e_matrix.periodic_snapshot(board,result) is None
        assert result['transitional_snapshot_errors'][0]['state']==state
    else:
        with pytest.raises(RuntimeError):step8e_matrix.periodic_snapshot(board,result)

def test_reconnecting_console_timeout_is_still_a_failure(monkeypatch):
    import step8e_matrix
    from types import SimpleNamespace
    def timeout(*args,**kwargs):raise TimeoutError('console ack')
    monkeypatch.setattr(step8e_matrix,'snapshot',timeout)
    board=SimpleNamespace(lines_since=lambda cursor:[(1,1.0,'I dg-app: STATE Starting')])
    with pytest.raises(TimeoutError):step8e_matrix.periodic_snapshot(board,{})


@pytest.mark.parametrize('direction',['uplink','downlink','both'])
def test_netem_scopes_every_classifier_and_bypasses_unmatched_traffic(direction):
    generated=commands('192.168.4.29',['loss','3%'],direction)
    filters=[c for c in generated if c[0]=='filter']
    assert filters
    assert all('192.168.4.29/32' in c for c in filters)
    assert all('u32' in c for c in filters)
    roots=[c for c in generated if 'root' in c]
    assert all(c[c.index('priomap')+1:]==['0']*16 for c in roots)
    assert all('8e01:3' in c for c in generated if 'netem' in c)
    assert any('mirred' in c for c in generated)==(direction!='downlink')
    assert any('dst' in c for c in filters)==(direction!='uplink')


def test_netem_rejects_non_address_input():
    with pytest.raises(ValueError):commands('192.168.4.29; unsafe',['loss','3%'])


def test_setup_selectors_never_escape_board_address_scope():
    from step8e_setup import SELECTORS
    for selector in SELECTORS.values():
        filters=[c for c in commands('192.168.4.29',['loss','100%'],selector=selector) if c[0]=='filter']
        assert len(filters)==3
        for classifier in filters:
            assert classifier.index('192.168.4.29/32')<classifier.index('protocol',classifier.index('u32'))
            assert 'at' in classifier


def test_restore_only_deletes_journalled_roots(monkeypatch):
    import netem
    calls=[]
    def command(*args,**kwargs):
        calls.append(args)
        if args[:3]==('-j','qdisc','show'):
            return json.dumps([dict(root=True,handle='8e01:',kind='prio')])
        return '[]'
    monkeypatch.setattr(netem,'command',command)
    netem.restore({},'wlp8s0','ifb0',[['qdisc','add','dev','ifb0','root']])
    mutations=[c for c in calls if c[0]!='-j']
    assert mutations==[('qdisc','del','dev','ifb0','root','handle','8e01:')]


def test_signal_runs_cleanup_before_exit(monkeypatch,tmp_path):
    import signal
    from netem import Netem
    calls=[]
    monkeypatch.setattr(Netem,'__exit__',lambda *args:calls.append('restored'))
    fault=Netem('192.168.4.29',['loss','3%'],state=tmp_path/'state.json')
    with pytest.raises(SystemExit) as exc:fault._signal(signal.SIGTERM,None)
    assert exc.value.code==128+signal.SIGTERM and calls==['restored']
    # A harness finally block clearing loss must not replace SystemExit with
    # a missing-qdisc exception, which a campaign could catch and continue.
    monkeypatch.setattr('netem.command',lambda *args,**kwargs:pytest.fail('tc called after signal cleanup'))
    fault.update(['loss','0%'])
    with pytest.raises(SystemExit) as exc:fault.update(['loss','3%'])
    assert exc.value.code==128+signal.SIGTERM


def test_snapshot_keeps_attempting_before_teardown_even_with_failed_perf(tmp_path):
    calls=[]
    class Board:
        cursor=10
        def command(self,command,timeout):
            calls.append(command)
            if command=='perf':raise TimeoutError('ack')
            return 'OK'
        def lines_since(self,cursor):return [(11,1.0,'alive')]
    result=capture_failure(Board(),tmp_path,'gate failure')
    calls.append('disconnect')
    assert calls==['perf','wifi-stats','netprobe stats','disconnect']
    assert result['commands'][0]['error'].startswith('TimeoutError')
    assert json.loads((tmp_path/'failure-device-snapshot.json').read_text())['reason']=='gate failure'


def test_uplink_counts_delayed_gap_across_turn_start_and_excludes_other_session():
    packets=[dict(pc_id='pc',ssrc=1,observed_ms=t,seq=i,timestamp=960*i)
             for i,t in enumerate((990,1010,1200,1220,1300))]
    events=[dict(pc_id='pc',event='input-clock',t=1.2,gap_s=.19),
            dict(pc_id='pc',event='input-level',t=1.1)]
    score=score_uplink(packets,events,'pc',1.0,1.25)
    assert score['socket_covered']
    assert score['socket_max_gap_ms']==190
    assert score['socket_gaps_over_threshold'][0]['seq_step']==1
    assert not score['passed']
    assert not score_uplink(packets,events,'other',1,1.25)['passed']


def test_uplink_missing_end_coverage_cannot_pass():
    packets=[dict(pc_id='pc',ssrc=1,observed_ms=t,seq=i,timestamp=960*i)
             for i,t in enumerate((990,1010,1030))]
    assert not score_uplink(packets,[], 'pc',1,1.2)['passed']


def test_send_boundary_distinguishes_path_gap_from_owner_gap():
    from step8e_analysis import uplink_gap_attribution
    packets=[dict(observed_ms=t,seq=i,timestamp=960*i,ssrc=7) for i,t in enumerate((1000,1120))]
    device=[dict(device_us=t,seq=i,timestamp=960*i,ssrc=7,kind=12) for i,t in enumerate((500000,520000))]
    result=uplink_gap_attribution(device,packets,1,1.2)
    assert result[0]['device_send_gap_ms']==20
    assert result[0]['attribution'].startswith('After successful')
    device[1]['device_us']=620000
    device.append(dict(kind=8,device_us=619000,value=110000))
    result=uplink_gap_attribution(device,packets,1,1.2)
    assert result[0]['device_send_gap_ms']==120 and result[0]['slow_calls']


def test_reconnect_sequence_reset_is_not_wire_loss():
    from step8e_analysis import uplink_packet_summary
    packets=[dict(pc_id=pc,ssrc=ssrc,seq=seq,observed_ms=t)
        for pc,ssrc,seq,t in [('a',1,65535,0),('a',1,0,20),('b',2,30000,1000),('b',2,30002,1040),('b',2,30002,1041)]]
    score=uplink_packet_summary(packets)
    assert score['missing_lower_bound']==1
    assert score['max_gap_ms']==980
    assert score['sessions'][1]['duplicates']==1

def test_lost_packets_do_not_falsely_attribute_a_send_cadence_stall():
    from step8e_analysis import uplink_gap_attribution
    packets=[dict(observed_ms=t,seq=i,timestamp=960*i,ssrc=7) for i,t in ((0,1000),(6,1120))]
    device=[dict(device_us=500000+i*20000,seq=i,timestamp=960*i,ssrc=7,kind=12) for i in range(7)]
    result=uplink_gap_attribution(device,packets,1,1.2)
    assert result[0]['device_send_gap_ms']==120
    assert result[0]['device_max_send_gap_ms']==20
    assert result[0]['attribution'].startswith('After successful')


def test_device_cadence_includes_packets_lost_between_boundaries():
    from step8e_analysis import device_cadence
    sends=[dict(kind=12,ssrc=7,seq=i,timestamp=960*i,device_us=100000+i*20000) for i in range(7)]
    packets=[dict(ssrc=7,seq=i,timestamp=960*i,observed_ms=1000+i*20) for i in (0,6)]
    score=device_cadence(sends,packets,1.01,1.1)
    assert score['covered'] and score['passed'] and score['max_device_send_gap_ms']==20
    assert not device_cadence([],packets,1.01,1.1)['passed']


def test_matched_packet_clock_bounds_include_transit_and_quantization():
    from step8e_analysis import recovery_bounds
    rows=[dict(kind=12,ssrc=1,seq=0,timestamp=0,device_us=500000),
          dict(kind=1,ssrc=2,seq=0,timestamp=0,device_us=510000)]
    up=[dict(ssrc=1,seq=0,timestamp=0,observed_ms=1020)]
    down=[dict(ssrc=2,seq=0,timestamp=0,send_ms=1000)]
    score=recovery_bounds(rows,up,down,1.0,1.005)
    assert score['clock_bounds_valid']
    assert score['clock_offset_lower_s']==pytest.approx(.489)
    assert score['clock_offset_upper_s']==pytest.approx(.521)
    assert score['uplink_recovery_ms_bounds']==pytest.approx([15,20])


def test_ack_timeout_automatically_snapshots_before_raising(tmp_path):
    from esp_serial import EspSerial
    board=EspSerial.__new__(EspSerial);board._cursor=0;board._seq=0
    import threading
    board._cv=threading.Condition();board.log_path=tmp_path/'serial.log'
    calls=[]
    def send(cmd):
        calls.append(cmd);board._seq+=1;return board._seq
    def wait_for(regex,timeout,since):
        if board._seq==1:raise TimeoutError('original timeout')
        return 'OK'
    board.send=send;board.wait_for=wait_for;board.lines_since=lambda cursor:[]
    with pytest.raises(TimeoutError,match='original timeout'):board.command('play',1)
    assert calls==['play','perf','wifi-stats','netprobe stats']
    assert (tmp_path/'ack-timeout-1/failure-device-snapshot.json').exists()


def test_missing_vad_stop_is_readable(monkeypatch):
    import e2e_common
    monkeypatch.setattr(e2e_common,'wait_until',lambda *args:None)
    with pytest.raises(AssertionError,match='No VAD-stop'):
        e2e_common.request_offline_play(None,lambda:[],0)
