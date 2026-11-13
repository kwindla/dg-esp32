import json
from datetime import datetime
import pytest
from echoear_aec_diag_analysis import arrival_diagnostics,phase_histograms,parse_ping,wall_to_monotonic,correlate
from echoear_aec_diag_probes import deadline,ZONE


def row(kind,at,**kw):
    return dict(device_us=at,timestamp=960,ssrc=7,seq=42,value=1,aux=1,
                bytes=320,kind=kind,flags=0,reserved=1,**kw)


def fixture():
    render=row(14,100000);render['flags']=1
    return [render,row(1,110000),row(2,111000),row(13,112000)]


def analyze(rows,**kw):
    return arrival_diagnostics(rows,[dict(pc_id='pc',ssrc=7,seq=42,timestamp=960,send_ms=50)],
                               pc_id='pc',generation=1,lo_us=90000,hi_us=120000,
                               coverage_complete=kw.pop('coverage_complete',True),expected_missing=kw.pop('expected_missing',1),**kw)


def test_arrival_after_completion_is_upstream_but_before_is_unknown():
    r=analyze(fixture());assert r['coverage_complete']
    assert r['late_slots'][0]['attribution'].startswith('upstream of socket')
    rows=fixture()
    for r in rows[1:]:r['device_us']-=20000
    r=analyze(rows)['late_slots'][0]
    assert r['attribution'].startswith('unknown: ingress before completion') and r['exact_deadline_us'] is None


def test_cache_boundary_and_absent_packet():
    rows=fixture();rows[1]['device_us']=80000;rows[2]['device_us']=81000
    assert 'authoritative ingress' in analyze(rows)['late_slots'][0]['attribution']
    assert 'absent at ingress' in analyze(rows[:-1])['late_slots'][0]['attribution']


def test_overflow_counter_mismatch_tick_gap_and_wrong_session_fail_closed():
    assert not analyze(fixture(),coverage_complete=False)['coverage_complete']
    assert not analyze(fixture(),expected_missing=2)['coverage_complete']
    rows=fixture();extra=dict(rows[0],device_us=119000,value=3,flags=0);rows.append(extra)
    assert not analyze(rows)['coverage_complete']
    rows=fixture()
    for r in rows:r['reserved']=2
    assert not analyze(rows)['coverage_complete']


def test_identity_collision_not_retransmit_and_duplicate_keeps_first():
    rows=fixture();rows.append(dict(rows[-1],device_us=115000))
    assert analyze(rows)['late_slots'][0]['ingress_us']==112000
    rows.append(dict(rows[-1],seq=43))
    assert 'ambiguous RTP identity' in analyze(rows)['late_slots'][0]['attribution']


def test_relative_delay_deduplicates_retransmissions():
    rows=fixture();rows.append(dict(rows[1],device_us=118000))
    wire=next(d for d in analyze(rows)['arrival_distributions'] if d['kind']==1)
    assert wire['relative_delay_ms']['n']==1 and wire['relative_delay_ms']['max']==0


def snap(t,n,hist):
    return dict(device_us=t,generation=1,pc_id='pc',received=1000*t,lost=n,playout_dropped=n,
                playout=dict(missing_run_ended=hist))


def test_phase_run_crosses_boundary_without_becoming_two_runs():
    a=snap(1,0,[0]*6);b=snap(2,2,[0]*6);c=snap(3,3,[0,0,1,0,0,0])
    r=phase_histograms(a,b,c)['phases']
    assert r[0]['transport']['missing_run_ended_delta']==[0]*6
    assert r[1]['transport']['delta']['lost']==1
    assert r[1]['transport']['missing_run_ended_delta']==[0,0,1,0,0,0]
    c['generation']=2
    assert not phase_histograms(a,b,c)['phases'][1]['available']
    b['playout']['missing_run_ended']=[0]
    assert not phase_histograms(a,b,a)['phases'][0]['available']


def test_ping_clock_jump_missing_bounds_and_correlation():
    p=parse_ping('[100.500] 64 bytes from 1: icmp_seq=2 ttl=64 time=120 ms\n[101.0] 64 bytes from 1: icmp_seq=3 ttl=64 time<1 ms')
    assert len(p)==2 and not p[1]['rtt_exact']
    anchors=[dict(wall_s=100,mono_lower_s=10,mono_upper_s=10.001),dict(wall_s=101,mono_lower_s=11,mono_upper_s=11.001)]
    assert wall_to_monotonic(100.5,anchors)==[10.5,pytest.approx(10.501)]
    assert wall_to_monotonic(99,anchors) is None
    anchors[-1]['mono_upper_s']=12
    assert wall_to_monotonic(100.5,anchors) is None
    a=[dict(coordinate=0,lower_s=10,upper_s=10.01),dict(coordinate=200,lower_s=10.2,upper_s=10.21)]
    r=correlate([dict(device_us=100)],a,[dict(host_interval_s=[10.1,10.11],rtt_ms=120)],[],[])
    assert r['events'][0]['correlation']['gateway']['excursions']==1
    assert r['events'][0]['correlation']['board']['max_rtt_ms'] is None


def test_local_deadline_reserves_restore_and_refuses_night():
    d=lambda h,m:datetime(2026,10,18,h,m,tzinfo=ZONE)
    assert deadline(d(1,30))['acquisition_end_local'].startswith('2026-10-18T02:25:00')
    for h,m in [(1,41),(2,40),(3,30),(4,9)]:
        with pytest.raises(ValueError):deadline(d(h,m))
    assert deadline(d(4,10))['hard_end_local'].startswith('2026-10-19T02:45')


def test_analysis_entrypoint_retains_unavailable_export_and_never_overwrites(tmp_path):
    from echoear_aec_diag_analysis import main
    value=dict(before=snap(90000,0,[0]*6),capture_end=snap(120000,1,[1,0,0,0,0,0]),
               after=snap(150000,1,[1,0,0,0,0,0]),records=fixture(),sends=[],
               coverage=dict(capture=dict(complete=True,overwritten_delta=0,max_drain_gap_s=20),
                             export=dict(complete=False,overwritten_delta=100,max_drain_gap_s=50)),
               device_anchors=[],ping_raw=dict(gateway='',board=''),ping_clock_anchors=[],data_channel_rtt=[])
    inp=tmp_path/'input.json';out=tmp_path/'output.json';inp.write_text(json.dumps(value))
    main([str(inp),str(out)]);result=json.loads(out.read_text())
    assert result['qualification'] is False
    assert result['diagnostics'][0]['coverage_complete']
    assert not result['diagnostics'][1]['coverage_complete']
    with pytest.raises(FileExistsError):main([str(inp),str(out)])


def test_ping_pair_owned_process_cleanup_and_exact_unprivileged_args(tmp_path,monkeypatch):
    from echoear_aec_diag_probes import PingPair
    from unittest.mock import Mock
    children=[];argv=[]
    def spawn(args,**kw):
        argv.append(args);child=Mock();child.poll.return_value=None;children.append(child);return child
    monkeypatch.setattr('echoear_aec_diag_probes.subprocess.Popen',spawn)
    pair=PingPair(tmp_path/'ping').start()
    try:pair.check()
    finally:pair.stop()
    assert argv==[['ping','-n','-i','0.2','-D','192.168.4.1'],['ping','-n','-i','0.2','-D','192.168.4.50']]
    assert all(c.send_signal.call_count==1 and c.wait.call_count==1 for c in children)
    assert len((tmp_path/'ping/clock.jsonl').read_text().splitlines())>=2


def test_ping_pair_cleans_first_child_if_second_start_fails(tmp_path,monkeypatch):
    from echoear_aec_diag_probes import PingPair
    from unittest.mock import Mock
    child=Mock();child.poll.return_value=None;attempts=[]
    def spawn(*args,**kw):
        attempts.append(args)
        if len(attempts)==2:raise OSError('test startup failure')
        return child
    monkeypatch.setattr('echoear_aec_diag_probes.subprocess.Popen',spawn)
    with pytest.raises(OSError):PingPair(tmp_path/'ping').start()
    assert child.send_signal.call_count==1 and child.wait.call_count==1
