import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from echoear_aec_link_soak import trace_bursts,decision,prompts,validate,stimulus_worker


def snapshots():
    def snap(t,missing,zeros):
        return dict(device_us=t,pc_id='pc',generation=1,
            peer_profile={'trace':dict(enabled=True,allocated=True,overwritten=0)},
            playout=dict(missing_slots=missing,post_plc_zeros=zeros,starvation=0))
    return snap(0,0,0),snap(200000,7,2)


def records():
    rows=[]
    for i in range(1,11):
        flag=1 if 2<=i<=6 else 2 if 7<=i<=8 else 0
        rows.append(dict(device_us=i*20000,timestamp=i*960,ssrc=0,kind=14,flags=flag,value=i,aux=1,reserved=1))
        rows.append(dict(device_us=i*20000+(150000 if flag else -10000),timestamp=i*960,ssrc=5,kind=13,flags=0,value=0,aux=0,reserved=1))
    return rows


def test_actual_seven_slot_burst_not_seven_events_and_late_witness():
    a,b=snapshots();r=trace_bursts(records(),a,b)
    assert r['valid'] and r['late_bursts']==1
    assert r['bursts'][0]['slots']==7 and r['bursts'][0]['at_us']==100000
    assert r['render_zeros']==2
    # Duplicate drain records do not double count a run.
    assert trace_bursts(records()*2,a,b)==r


@pytest.mark.parametrize('fault',['overwritten','missing-tick','counter','generation','stream-reset'])
def test_trace_coverage_gaps_cannot_exonerate(fault):
    a,b=snapshots();rows=records()
    if fault=='overwritten':b['peer_profile']['trace']['overwritten']=1
    elif fault=='missing-tick':rows=[r for r in rows if not(r['kind']==14 and r['value']==5)]
    elif fault=='counter':b['playout']['missing_slots']=8
    elif fault=='generation':b['generation']=2
    elif fault=='stream-reset':
        for r in rows:
            if r['kind']==14 and r['value']>5:r['aux']=2
    assert not trace_bursts(rows,a,b)['valid']


def test_late_unwitnessed_counts_loss_without_invalidating_coverage():
    a,b=snapshots();r=trace_bursts([r for r in records() if r['kind']!=13],a,b)
    assert r['valid'] and r['coverage_valid']
    assert (r['missing_bursts'],r['late_bursts'],r['unresolved_bursts'])==(1,0,1)
    sample=blocks([0,0,0,0,0,0]);sample[1]['device_bursts']=r
    result=decision(sample)
    assert result['valid'] and result['pooled_events']==1
    assert result['arms']['dump']['unresolved_bursts']==1


def test_cross_boundary_run_counted_once_at_fourth_slot():
    a,b=snapshots();mid=copy.deepcopy(a);mid['device_us']=80000;mid['playout']['missing_slots']=3
    first=trace_bursts(records(),a,mid);second=trace_bursts(records(),mid,b)
    assert first['valid'] and first['late_bursts']==0
    assert second['valid'] and second['late_bursts']==1


def blocks(counts):
    return [dict(mode='no-dump' if i%2==0 else 'dump',duration_s=300,valid=True,
                 device_bursts={'missing_bursts':n,'late_bursts':n,'unresolved_bursts':0}) for i,n in enumerate(counts)]


def test_decision_frozen_threshold_and_equal_exposure_normalization():
    assert decision(blocks([1,2,0,2,0,1]))['verdict']=='export implicated'
    assert decision(blocks([1,1,1,1,1,1]))['verdict']=='export exonerated under declared rule'
    assert decision(blocks([0,1,0,1,0,1]))['verdict']=='export exonerated under declared rule'
    assert decision(blocks([0,2,0,2,0,1]))['verdict']=='export implicated'
    assert not decision(blocks([1,2]))['valid']
    b=blocks([1,2,1,2,1,2]);b[0]['valid']=False
    assert not decision(b)['valid']
    b=blocks([1,2,1,2,1,2])
    for row in b:
        if row['mode']=='dump':row['duration_s']=600
    assert decision(b)['verdict']=='export exonerated under declared rule'


def test_exact_six_blocks_five_short_prompts_each():
    p=prompts();assert len(p)==30 and len({x['id'] for x in p})==30
    for b in range(1,7):assert [x['offset_s'] for x in p if x['block']==b]==[0,60,120,180,240]


def test_no_hardware_for_invalid_declaration(tmp_path,monkeypatch):
    import echoear_aec_campaign as campaign
    import echoear_aec_link_soak as soak
    monkeypatch.setattr(campaign,'BenchHold',lambda *a:pytest.fail('bench touched'))
    p=tmp_path/'bad.json';p.write_text('{}');out=tmp_path/'not-created'
    with pytest.raises(ValueError,match='Separate'):soak.run(p,out)
    assert not out.exists()


def test_tts_worker_keeps_absolute_cadence_and_seals(tmp_path,monkeypatch):
    import echoear_aec_link_soak as soak
    now=[0.];monkeypatch.setattr(soak.time,'monotonic',lambda:now[0])
    class Cancel:
        def wait(self,n):now[0]+=n;return False
    calls=[];active=[None]
    def send(op,cid):
        calls.append((op,cid,now[0]));active[0]=cid
    h=SimpleNamespace(send=send,events=lambda:[dict(case_id=active[0],event='frame',frame='BotStoppedSpeakingFrame'),dict(case_id=active[0],event='tts-generation-metric-start',context_id='ctx')],recorded=lambda:[dict(kind='tts-transport-write',context_id='ctx',non_silent=True)])
    result=stimulus_worker(h,prompts()[:5],0,Cancel(),tmp_path)
    assert len(result)==5
    assert [round(t) for op,cid,t in calls if op=='speak']==[0,60,120,180,240]
    assert sum(op=='seal' for op,cid,t in calls)==5
    now[0]=1
    result=stimulus_worker(h,prompts()[:2],0,Cancel(),tmp_path)
    assert [r['request_s'] for r in result]==[1,60]
    assert [r['lateness_s'] for r in result]==[1,0]
    now[0]=1.01
    with pytest.raises(RuntimeError,match='deadline'):stimulus_worker(h,prompts()[:1],0,Cancel(),tmp_path)
    assert json.loads((tmp_path/'prompts.json').read_text())[0]['lateness_s']==1.01


def test_launch_digest_checked_before_hardware(tmp_path,monkeypatch):
    import echoear_aec_link_soak as soak
    import echoear_aec_accept as accept
    monkeypatch.setattr(soak,'validate',lambda *a: {'passed':True})
    monkeypatch.setattr(accept,'check_esptool',lambda:pytest.fail('hardware preflight reached'))
    plan=tmp_path/'decl.json';plan.write_text('{}')
    with pytest.raises(ValueError,match='SHA256'):soak.run(plan,tmp_path/'absent',expected_sha256='wrong')
    assert not (tmp_path/'absent').exists()


def test_exception_unwinds_hold_without_leaving_output_owner(tmp_path,monkeypatch):
    import echoear_aec_link_soak as soak
    import echoear_aec_campaign as campaign
    import echoear_aec_accept as accept
    monkeypatch.setattr(soak,'validate',lambda *a: {'passed':True})
    monkeypatch.setattr(accept,'check_esptool',lambda:None)
    monkeypatch.setattr(campaign,'command',lambda *a:None)
    actions=[]
    import host_wifi_diagnostics
    class Diagnostics:
        def __init__(self,*a):pass
        def start(self):actions.append('diagnostics start')
        def stop(self):actions.append('diagnostics stop')
    monkeypatch.setattr(host_wifi_diagnostics,'HostWifiDiagnostics',Diagnostics)
    class Hold:
        def __init__(self,*a):
            assert a[1]['id']=='link-soak-02'
            self.board=object()
        def __enter__(self):actions.append('enter lock');return self
        def connect(self,*a):raise RuntimeError('connect failed')
        def __exit__(self,typ,exc,tb):actions.extend(['restore','verify normal','mixer','release']);return False
    plan=tmp_path/'decl.json';plan.write_text(json.dumps({'prompts':prompts(),'run_id':'link-soak-02','host_wifi_diagnostics':True}))
    with pytest.raises(RuntimeError,match='connect failed'):
        soak.run(plan,tmp_path/'absent',expected_sha256=soak.sha(plan),hold_factory=Hold)
    assert actions==['diagnostics start','enter lock','restore','verify normal','mixer','release','diagnostics stop']


def test_stop_pattern_matches_python_only_and_exact_frozen_arguments():
    import subprocess
    from freeze_aec_link_soak import runner_pattern
    pattern=runner_pattern('abc123','/freeze/declaration.json','/runs/link-soak-01')
    args='../hil/echoear_aec_campaign.py --link-soak --declaration-sha256 abc123 /freeze/declaration.json /runs/link-soak-01'
    for interpreter in ('python','python3','/venv/bin/python3.12'):
        line=interpreter+' '+args
        assert subprocess.run(['grep','-E',pattern],input=line,text=True,capture_output=True).returncode==0
    for line in ('uv run python '+args,'nohup python '+args,'python '+args+' extra',
                 'python '+args.replace('abc123','wrong'),'python '+args.replace('declaration.json','declarationXjson')):
        assert subprocess.run(['grep','-E',pattern],input=line,text=True,capture_output=True).returncode==1


def test_wifi_export_at_both_boundaries_of_both_arms(tmp_path,monkeypatch):
    import echoear_aec_campaign as campaign
    from echoear_aec_link_soak import block_boundary
    from test_wifi_observation import WifiObservationTests
    fixture=WifiObservationTests();meta,rows=fixture.records();lines=fixture.lines(meta,rows)
    calls=[]
    board=SimpleNamespace(cursor=0,command=lambda cmd,timeout:calls.append(cmd),
                          lines_since=lambda cursor:[(i,0,line) for i,line in enumerate(lines)])
    monkeypatch.setattr(campaign,'boundary',lambda b,f,label:calls.append(label) or ('perf','rows'))
    for block in range(1,7):
        folder=tmp_path/f'block-{block:02d}';folder.mkdir()
        for label in ('before','after'):
            assert block_boundary(board,folder,label)==('perf','rows')
            assert json.loads((folder/f'{label}-wifi-observe.json').read_text())['rows']==rows
            assert (folder/f'{label}-wifi-observe.log').read_text().splitlines()==lines
            window=json.loads((folder/f'{label}-wifi-observe-window.json').read_text())
            assert window['end_s']>=window['start_s']
    assert calls==['wifi-observe','before','wifi-observe','after']*6


def test_wifi_summary_deduplicates_and_reports_changes_gaps_and_nulls():
    from echoear_aec_link_soak import wifi_summary
    def row(seq,ap,rssi=-40):
        return dict(sequence=seq,sample_begin_us=seq*100-10,sample_end_us=seq*100,
                    bssid=ap,rssi_dbm=rssi,primary_channel=6 if ap else None,ps_mode=0 if ap else None)
    a,b,c,d=row(1,'a'),row(2,'a'),row(4,None,None),row(5,'b',-50)
    observations=[dict(available=True,rows=[a,b]),dict(available=True,rows=[b,c,d])]
    result=wifi_summary(observations,200,500)
    assert result['rows']==[b,c,d] and result['prior_context_included']
    assert result['bssids']==['a','b'] and result['rssi_dbm_range']==[-50,-40]
    assert result['primary_channels']==[6] and result['ps_modes']==[0]
    assert result['uncovered_gaps']==[dict(after_us=200,before_us=390,missing_samples=1)]
    assert result['bssid_changes']==[dict(from_bssid='a',to_bssid='b',after_us=200,observed_by_us=500,unobserved_samples=2)]
    assert wifi_summary(observations)['rows']==[a,b,c,d]
    assert not wifi_summary([])['boundary_exports_available']
    with pytest.raises(ValueError,match='Conflicting'):
        wifi_summary(observations+[dict(rows=[dict(b,bssid='other')])])


def test_final_analysis_missing_inputs_retains_original_exception(tmp_path):
    from echoear_aec_link_soak import finalize_analysis
    (tmp_path/'declaration.json').write_text('{}')
    with pytest.raises(RuntimeError,match='connect failed'):
        try:raise RuntimeError('connect failed')
        finally:
            import sys
            finalize_analysis(tmp_path,sys.exc_info()[1])
    result=json.loads((tmp_path/'analysis.json').read_text())
    assert not result['valid'] and len(result['missing_inputs'])==2


@pytest.mark.parametrize('unwritable',[False,True])
def test_final_analysis_failure_cannot_mask_acquisition_exception(tmp_path,monkeypatch,unwritable):
    import echoear_aec_link_soak as soak
    (tmp_path/'declaration.json').write_text('{}');(tmp_path/'rtp').mkdir()
    (tmp_path/'rtp/socket-rtp.jsonl').touch();(tmp_path/'device-trace.jsonl').touch()
    def broken(*a):raise ValueError('analysis broken')
    monkeypatch.setattr(soak,'analyze',broken)
    if unwritable:monkeypatch.setattr(soak,'write',broken)
    with pytest.raises(RuntimeError,match='original stop'):
        try:raise RuntimeError('original stop')
        finally:
            import sys
            soak.finalize_analysis(tmp_path,sys.exc_info()[1])
    if not unwritable:
        assert json.loads((tmp_path/'analysis-error.json').read_text())['original_error']=='RuntimeError: original stop'
    with pytest.raises(ValueError,match='analysis broken'):soak.finalize_analysis(tmp_path)
