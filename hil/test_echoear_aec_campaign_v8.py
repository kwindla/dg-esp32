import copy
import json
from types import SimpleNamespace
import numpy as np
import pytest
import echoear_aec_campaign as runner
from echoear_aec_bench import write,EVIDENCE
from echoear_aec_campaign_analysis import callback_witness,source_write_witness,generation_audit
from test_aec_campaign import evidence,plan
from test_aec_link_soak_launch import launch
from freeze_aec_campaign_v8 import launch_script
from freeze_aec_campaign_v8_1 import launch_script as launch_script_v8_1
from freeze_aec_campaign_v8_2 import launch_script as launch_script_v8_2
from echoear_aec_host_gate import POLICY


def test_retry_window_ends_before_stats_and_excludes_late_ping(tmp_path,monkeypatch):
    tick=[10_000_000_000];order=[]
    class Guard:
        def boundary(self,*a):order.append('host')
        def periodicity(self):order.append('periodicity');tick[0]+=700_000_000
    board=SimpleNamespace(cursor=0,campaign_host_gate=Guard(),lines_since=lambda c:[])
    monkeypatch.setattr(runner.time,'monotonic_ns',lambda:tick[0])
    monkeypatch.setattr(runner,'command',lambda *a:order.append('wifi'))
    def stats(*a):
        order.append('stats');tick[0]+=100_000_000;return {},[]
    monkeypatch.setattr(runner,'admission_boundary',stats)
    window=dict(start_ns=9_000_000_000)
    runner.boundary(board,tmp_path,'retry-check',admission_window=window)
    assert order==['host','wifi','stats','periodicity']
    assert window['stats_before_send_ns']==10_000_000_000 and window['end_ns']==9_800_000_000
    before=dict(generation=1,pc_id='p',data_admission=dict(received=10,dispatched=10),drop_reasons=dict(admission_budget=0))
    after=dict(before,data_admission=dict(received=11,dispatched=11))
    records=[dict(kind='rtvi-output',source='RTVIProcessor',direction='DOWNSTREAM',monotonic_ns=t) for t in [9_500_000_000,10_500_000_000]]
    assert runner.retry_admission(before,after,records,window)['passed']
    assert not runner.retry_admission(before,after,records,dict(window,end_ns=tick[0]))['passed']
    assert not runner.retry_admission(before,before,records,window)['passed'] # real missing delivery still fails
    assert json.loads((tmp_path/'retry-check-recorder-window.json').read_text())==window


@pytest.mark.parametrize("failure_at",["analysis","write"])
def test_json_numpy_and_original_failure_survive(tmp_path,monkeypatch,failure_at):
    write(tmp_path/'ok.json',dict(count=np.int64(0),flag=np.bool_(True)))
    assert json.loads((tmp_path/'ok.json').read_text())==dict(count=0,flag=True)
    for value in [float('nan'),np.float64('nan'),object()]:
        with pytest.raises((ValueError,TypeError)):write(tmp_path/'bad.json',value)
    import echoear_aec_campaign_analysis as a
    def fail(*args):raise TypeError('serialization broke')
    if failure_at=='analysis':monkeypatch.setattr(a,'analyze_campaign',fail)
    else:
        monkeypatch.setattr(a,'analyze_campaign',lambda *_:dict(passed=False))
        def failing_write(path,data):
            if path.name=='analysis.json':raise TypeError('serialization broke')
            write(path,data)
        monkeypatch.setattr(runner,'write',failing_write)
    original=RuntimeError('acquisition failed')
    with pytest.raises(RuntimeError,match='acquisition failed') as seen:
        try:raise original
        finally:runner.finalize_analysis(tmp_path,original)
    assert seen.value is original
    record=json.loads((tmp_path/'analysis-failure.json').read_text())
    assert record['original_error']=='RuntimeError: acquisition failed' and 'serialization broke' in record['analysis_error']
    with pytest.raises(TypeError):runner.finalize_analysis(tmp_path)


def test_real_reply_llm_ttfb_is_not_a_second_tts_metric():
    events,recorded=evidence()
    llm=copy.deepcopy(next(e for e in events if e.get('metric_type')=='TTFBMetricsData'))
    llm['data'].update(processor='OpenAILLMService#0',model='gb-phone',value=.4);events.append(llm)
    assert generation_audit(events,recorded)['passed']
    duplicate=copy.deepcopy(next(e for e in events if e.get('metric_type')=='TTFBMetricsData'))
    assert not generation_audit(events+[duplicate],recorded)['passed']
    events=[e for e in events if not(e.get('metric_type')=='TTFBMetricsData' and e.get('context_id')=='c0' and e['data']['processor'].startswith('Proactive'))]
    assert not generation_audit(events,recorded)['passed']


@pytest.mark.parametrize('error,passes',[(-.10001,False),(-.1,True),(.25,True),(.25001,False)])
def test_duration_bounds_retained_even_on_rejection(tmp_path,error,passes):
    c=dict(run_id='cell-a1',jabra=dict(expected_seconds=0.,elapsed_seconds=error))
    if passes:runner.check_jabra_duration(tmp_path,c,plan()['fixture_policy'])
    else:
        with pytest.raises(runner.HarnessFailure):runner.check_jabra_duration(tmp_path,c,plan()['fixture_policy'])
    result=json.loads((tmp_path/'jabra-outcome-cell-a1.json').read_text())
    assert result['passed']==passes and result['error_s']==error


@pytest.mark.parametrize("window_us",[None,300000])
def test_callback_written_tail_required_natural_end_and_wrong_context_fail(window_us):
    blocks=np.array([[0,320,0,20,0],[320,640,20,60,0],[640,960,40,80,0]])
    cb=dict(callback_us=30,host_interval=[1.,1.1]);source=np.full(1600,100.)
    play=dict(source_extent=[0,1600])
    records=[dict(kind='tts-transport-write',context_id='old',pc_id='p',samples=1600,non_silent=True,monotonic_ns=1_000_000_000)]
    play['source_write_witness']=source_write_witness(play,source,records,'p','old')
    assert callback_witness(blocks,np.array([True,False,True]),cb,639,play,window_us=window_us)['passed']
    assert not callback_witness(blocks,np.array([False,True,True]),cb,639,play,window_us=window_us)['passed']
    gapped=blocks.copy();gapped[0,4]=1
    assert not callback_witness(gapped,np.array([True]*3),cb,639,play,window_us=window_us)['passed']
    assert not callback_witness(blocks,np.array([True]*3),cb,1599,play,window_us=window_us)['passed'] # natural source end
    for changed in [dict(records[0],context_id='new'),dict(records[0],monotonic_ns=1_200_000_000),dict(records[0],non_silent=False),dict(records[0],drop=True)]:
        play['source_write_witness']=source_write_witness(play,source,[changed],'p','old')
        assert not callback_witness(blocks,np.array([True]*3),cb,639,play,window_us=window_us)['passed']
    source[640:]=0;play['source_write_witness']=source_write_witness(play,source,records,'p','old')
    assert not callback_witness(blocks,np.array([True]*3),cb,639,play,window_us=window_us)['passed'] # written trailing silence


def test_seven_stop_six_sentence_spares_only_in_gated_voice_blocks():
    p=plan();assert runner.validate_spec(p)
    for sid,expected in [('qualification-01',7),('qualification-normal',7),('qualification-02',2)]:
        assert sum(c['session_id']==sid and c['kind']=='stop' for c in p['spares'])==expected
        assert sum(c['session_id']==sid and c['kind']=='sentence' for c in p['spares'])==(6 if sid!='qualification-02' else 2)


@pytest.mark.parametrize("make_launch",[launch_script,launch_script_v8_1,launch_script_v8_2])
def test_v8_launch_guard_and_frozen_exec(launch,make_launch):
    import hashlib
    policy=dict(POLICY,connection='test-profile');launch['declaration'].write_text(json.dumps(dict(host_environment=policy)))
    digest=hashlib.sha256(launch['declaration'].read_bytes()).hexdigest()
    launch['path'].write_text(make_launch(digest,launch['declaration'],launch['output'],policy))
    result,calls=launch['run'](FAKE_IW='Connected to '+POLICY['bssid']+' (on wlp8s0)\n freq: 5180\n signal: -54 dBm\n')
    assert result.returncode==0,result.stderr
    assert len([x for x in calls if x['name']=='uv'])==1
    assert not launch['output'].exists()


def test_retained_campaign04_v8_replay_has_no_acceptance_credit(tmp_path):
    if not (EVIDENCE/'step-9/campaign-04').exists():pytest.skip('retained evidence unavailable')
    from echoear_aec_v8_regression import replay
    result=replay();write(tmp_path/'replay.json',result)
    assert result['report_only'] and not result['acceptance_credit'] and not result['summary']['passed']
    assert len(result['holds'][0]['cases'])==22


def test_callback_window_accepts_stop09_lull_but_expires_at_300ms():
    blocks=np.array([[0,320,900000,959300,0],[320,640,940000,974400,0]])
    cb=dict(callback_us=1000000,host_interval=[1.,1.1]);source=np.full(1600,100.)
    play=dict(source_extent=[0,1600])
    records=[dict(kind='tts-transport-write',context_id='old',pc_id='p',samples=1600,non_silent=True,monotonic_ns=1000000000)]
    play['source_write_witness']=source_write_witness(play,source,records,'p','old')
    mask=np.array([True,False])
    assert not callback_witness(blocks,mask,cb,639,play)['passed'] # preserved v8 rule
    amended=callback_witness(blocks,mask,cb,639,play,window_us=300000)
    assert amended['passed'] and amended['pre_callback_upper_age_us']==40700
    blocks[0,2:4]=[650000,700000]
    assert callback_witness(blocks,mask,cb,639,play,window_us=300000)['passed']
    blocks[0,3]=699999
    assert not callback_witness(blocks,mask,cb,639,play,window_us=300000)['passed']
    blocks[0,3]=1000000 # upper must strictly precede callback
    assert not callback_witness(blocks,mask,cb,639,play,window_us=300000)['passed']
