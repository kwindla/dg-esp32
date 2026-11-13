import copy
import json
import os
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace
import wave
import numpy as np
import pytest

import echoear_aec_nova_prestep as n
import echoear_aec_pause_fixture as fixture
import echoear_aec_nova_preflight as a


@pytest.fixture
def plan(tmp_path):
    fixture.generate(n.FIXTURE, tmp_path/'pause.wav', tmp_path/'fixture.json')
    return n.plan_from(n.SOURCE, tmp_path/'fixture.json', n.OUTPUT)


def test_lossless_reproducible_gap_and_margin(tmp_path):
    before = fixture.sha(n.FIXTURE)
    one = fixture.generate(n.FIXTURE, tmp_path/'one.wav', tmp_path/'one.json')
    two = fixture.generate(n.FIXTURE, tmp_path/'two.wav', tmp_path/'two.json')
    assert one['output_sha256']==two['output_sha256']
    assert fixture.verify(tmp_path/'one.json')==one
    with wave.open(str(n.FIXTURE)) as w: source = w.readframes(w.getnframes())
    with wave.open(str(tmp_path/'one.wav')) as w: output = w.readframes(w.getnframes())
    i, length = one['gap_index'], one['gap_samples']
    assert 28800<=i<=len(source)//2-28800 and length==33600
    assert output[:i*2]==source[:i*2] and output[(i+length)*2:]==source[i*2:]
    assert output[i*2:(i+length)*2]==bytes(length*2)
    assert fixture.sha(n.FIXTURE)==before
    with pytest.raises(FileExistsError): fixture.generate(n.FIXTURE,tmp_path/'one.wav',tmp_path/'other.json')
    bad = tmp_path/'bad.wav'; bad.write_bytes(source)
    with pytest.raises(ValueError,match='calibrated'): fixture.derive(bad)
    value = json.loads((tmp_path/'one.json').read_text()); value['gap_index']+=1
    (tmp_path/'one.json').write_text(json.dumps(value))
    with pytest.raises(ValueError,match='manifest'): fixture.verify(tmp_path/'one.json')


def test_exact_six_real_reply_schedule_and_unchanged_main_groups(plan):
    p = plan; cells=p['cases']
    assert len(cells)==6 and p['attempts_per_cell']==1 and not p['spares'] and not p['reconnects']
    assert [c['volume_db'] for c in cells]==[-6,0,-6,-6,-6,-6]
    assert cells[0]['text']==cells[1]['text']==n.diagnostic.CONTROLS[0]
    assert [c['stop_offset_s'] for c in cells]==[None,None,.9,1.2,1.5,None]
    assert [c.get('fixture_id') for c in cells[2:]]==['stop-calibrated','sentence-01-calibrated','sentence-04-normal','sentence-04-pause-700ms']
    assert all(c['real_bot_reply'] for c in cells[2:])
    assert cells[-1]['kind']=='soak-near' and cells[-1]['initially_silent']
    assert p['bot_port']==7861 and p['bot_config']['ECHOEAR_STT_PROFILE']=='dev'
    assert not p['qualification'] and p['rf_pre_gate_s']==5
    original=json.loads(n.SOURCE.read_text())
    assert p['main_session']['gates']==original['gates']
    assert p['main_session']['failure_policy']==original['failure_policy']
    assert [(g['eligible'],g['minimum_successes'],g['spares']) for g in p['main_session']['group_denominators']]==[(10,9,7),(10,9,6),(10,9,6)]
    assert p['main_session']['budget']==n.MAIN_BUDGET and n.MAIN_BUDGET['total_s']==3600
    from echoear_aec_campaign import protocol
    proto=protocol(cells)
    assert cells[-1]['id']+'-a1' in proto['listen_ids']
    assert all(c['id']+'-a1' in proto['real_bot_ids'] for c in cells[2:])


@pytest.mark.parametrize('fault',['case','budget','port','profile','main-denominator','fixture'])
def test_mutation_rejected_before_hardware(plan,monkeypatch,fault):
    if fault=='case':plan['cases'][2]['stop_offset_s']=.5
    if fault=='budget':plan['budget']['total_s']=4000
    if fault=='port':plan['bot_port']=7860
    if fault=='profile':plan['bot_config']['ECHOEAR_STT_PROFILE']='demo'
    if fault=='main-denominator':plan['main_session']['group_denominators'][0]['eligible']=9
    if fault=='fixture':plan['cases'][-1]['fixture_sha256']='wrong'
    monkeypatch.setattr(n,'verify_bundle',lambda *a:pytest.fail('reached images'))
    with pytest.raises(ValueError,match='schedule'):n.validate(plan)


def test_launcher_gates_hash_before_uv_and_exports_interpreter(tmp_path,monkeypatch):
    plan=tmp_path/'plan with spaces.json';plan.write_text('{}')
    script=tmp_path/'launch.sh';script.write_text(n.launcher(plan,tmp_path/'absent run'))
    fake=tmp_path/'bin';fake.mkdir();uv=fake/'uv'
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$ESPTOOL_PYTHON" "$@" > "$RECORD"\n');uv.chmod(0o755)
    record=tmp_path/'argv'
    env=dict(os.environ,PATH=str(fake)+':'+os.environ['PATH'],RECORD=str(record))
    subprocess.run(['bash','-n',str(script)],check=True)
    for digest in (None,'bad'):
        if digest:env[n.REVIEW_ENV]=digest
        result=subprocess.run(['bash',str(script)],env=env,capture_output=True)
        assert result.returncode!=0 and not record.exists()
    env[n.REVIEW_ENV]=n.sha(plan)
    subprocess.run(['bash',str(script)],env=env,check=True)
    args=record.read_text().splitlines()
    assert args[0]==n.diagnostic.ESPTOOL_PYTHON
    assert args[args.index('--out')+1]==str(tmp_path/'absent run')
    assert 'flock' not in script.read_text() and '7860' not in script.read_text()
    record.unlink();plan.write_text('{ }')
    result=subprocess.run(['bash',str(script)],env=env,capture_output=True)
    assert result.returncode!=0 and not record.exists()


def test_python_hash_gate_precedes_everything(tmp_path,monkeypatch):
    path=tmp_path/'plan';path.write_text('{}')
    monkeypatch.setattr(n,'validate',lambda *a:pytest.fail('validation reached'))
    with pytest.raises(ValueError,match='reviewed'):n.run(path,tmp_path/'out','bad')
    assert not (tmp_path/'out').exists()


def test_esptool_guard_precedes_nic_alarm_lock(tmp_path,monkeypatch):
    import echoear_aec_accept as accept
    import echoear_aec_host_gate as host
    path=tmp_path/'plan';out=tmp_path/'out';path.write_text(json.dumps(dict(output=str(out))))
    monkeypatch.setattr(n,'validate',lambda *a:True)
    def fail():raise RuntimeError('esptool unavailable')
    monkeypatch.setattr(accept,'check_esptool',fail)
    monkeypatch.setattr(host,'prelaunch',lambda *a:pytest.fail('NIC query'))
    monkeypatch.setattr(n.Budget,'__enter__',lambda *a:pytest.fail('alarm'))
    monkeypatch.setattr(n,'hold_type',lambda *a:pytest.fail('hold/lock'))
    with pytest.raises(RuntimeError,match='esptool'):n.run(path,out,n.sha(path))
    assert not out.exists()


@pytest.mark.parametrize('existing',['directory','symlink'])
def test_existing_output_never_starts(plan,tmp_path,monkeypatch,existing):
    out=tmp_path/'out'
    if existing=='directory':out.mkdir()
    else:out.symlink_to(tmp_path/'nonexistent')
    plan['output']=str(out);path=tmp_path/'plan';path.write_text(json.dumps(plan))
    monkeypatch.setattr(n,'validate',lambda *a:True)
    import echoear_aec_accept as accept
    monkeypatch.setattr(accept,'check_esptool',lambda:pytest.fail('live preflight'))
    with pytest.raises(ValueError,match='Fresh'):n.run(path,out,n.sha(path))


def event_binding(p):
    return [dict(event='stt-profile',**p['profile']),dict(event='stt-model-observed',models=[p['vendor']['model_uuid']],
        model_info={p['vendor']['model_uuid']:dict(name=p['vendor']['name'],version=p['vendor']['version'])})]


@pytest.mark.parametrize('fault',[None,'missing-profile','missing-vendor','changed-mid-session','uuid','config'])
def test_live_vendor_and_profile_fail_closed(plan,fault):
    events=event_binding(plan)
    if fault=='missing-profile':events=events[1:]
    if fault=='missing-vendor':events=events[:1]
    if fault=='changed-mid-session':
        events.append(copy.deepcopy(events[-1]));events[-1]['model_info'][plan['vendor']['model_uuid']]['version']='new'
    if fault=='uuid':events[-1]['models']=['other']
    if fault=='config':events[0]['config_sha256']='other'
    assert n.profile_witness(plan,events)['passed']==(fault is None)


@pytest.mark.parametrize('contract',[n.BUDGET,n.MAIN_BUDGET])
def test_active_and_total_alarms_reserve_restore(monkeypatch,contract):
    handlers={};armed=[]
    monkeypatch.setattr(n.signal,'getitimer',lambda *a:(0,0))
    monkeypatch.setattr(n.signal,'getsignal',lambda *a:signal.SIG_DFL)
    monkeypatch.setattr(n.signal,'signal',lambda s,h:handlers.update({s:h}))
    monkeypatch.setattr(n.signal,'setitimer',lambda s,t:armed.append(t))
    now=[100.];monkeypatch.setattr(n.time,'monotonic',lambda:now[0])
    with n.Budget(contract) as budget:
        assert armed[-1]==contract['active_s']
        with pytest.raises(n.diagnostic.BudgetExpired):handlers[signal.SIGALRM](signal.SIGALRM,None)
        now[0]+=contract['active_s'];budget.cleanup()
        assert armed[-1]==contract['restore_reserve_s']
        assert handlers[signal.SIGTERM]==handlers[signal.SIGINT]==signal.SIG_IGN
        handlers[signal.SIGALRM](signal.SIGALRM,None)
        assert budget.total_expired
    assert armed[-1]==0 and handlers[signal.SIGTERM]==signal.SIG_DFL


def test_expired_export_yields_to_restore(tmp_path,monkeypatch):
    budget=SimpleNamespace(remaining=lambda:0,total_expired=False,cleanup=lambda:None)
    cls=n.hold_type(budget);hold=cls({}, {}, tmp_path)
    import echoear_aec_campaign as campaign
    monkeypatch.setattr(campaign,'dump_capture',lambda *a,**k:pytest.fail('export after deadline'))
    with pytest.raises(n.diagnostic.BudgetExpired):hold.export_capture(tmp_path/'capture.npz')
    assert json.loads((tmp_path/'export-duration.json').read_text())['active_remaining_s']==0


@pytest.mark.parametrize('failure',['noise','capture','budget'])
def test_no_retry_on_failed_capture(plan,tmp_path,failure):
    calls=[]
    class Hold:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):calls.append('cleanup')
        def connect(self,*a):calls.append('connect')
        def capture(self,group,attempt):
            calls.append(('capture',group['run_id'],attempt))
            if failure=='capture':raise RuntimeError('capture failure')
            return False,[]
        def finish(self):pytest.fail('finished failed acquisition')
    budget=SimpleNamespace(remaining=lambda:0 if failure=='budget' else 800)
    with pytest.raises((RuntimeError,n.diagnostic.BudgetExpired)):
        n.diagnostic.acquire(plan,tmp_path,budget,Hold)
    assert calls[-1]=='cleanup'
    assert len([c for c in calls if isinstance(c,tuple)])==(0 if failure=='budget' else 1)


def test_cleanup_requires_mixer_and_does_not_release_bad_restore(tmp_path,monkeypatch):
    import echoear_aec_campaign as campaign
    budget=SimpleNamespace(cleanup=lambda:None,total_expired=False)
    cls=n.hold_type(budget);h=cls({}, {}, tmp_path);h.touched=True
    h.lock=SimpleNamespace(__exit__=lambda *a:pytest.fail('released failed cleanup'))
    monkeypatch.setattr(campaign,'flash',lambda *a:None)
    monkeypatch.setattr(campaign,'boot',lambda *a:SimpleNamespace(close=lambda:None))
    (tmp_path/'mixer-before.state').write_text('before')
    def mixer(cmd,**kw):
        if cmd[-2]=='store':(tmp_path/'mixer-after.state').write_text('wrong')
    monkeypatch.setattr(campaign.subprocess,'run',mixer)
    class Parked(BaseException):pass
    monkeypatch.setattr(campaign.time,'sleep',lambda *a:(_ for _ in ()).throw(Parked()))
    with pytest.raises(Parked):h.__exit__(None,None,None)
    result=json.loads((tmp_path/'result.json').read_text())
    assert result['restored_elf_sha256']==n.NORMAL_HASH and result['parked_with_lock'] and not result['completed']
    assert not result.get('lock_released')


def turn_fixture():
    c=dict(expected_text='Please change the subject to cooking.')
    e=[dict(event='user-turn-started',t=1,strategy='VAD',pc_id='pc'),
       dict(event='frame',frame='VADUserStartedSpeakingFrame',t=1),
       dict(event='frame',frame='VADUserStoppedSpeakingFrame',t=3),
       dict(event='nova-transcript-boundary',frame_id=7,t=3.2),
       dict(event='user-turn-stopped',t=3.5,text=c['expected_text'],strategy='SmartTurn',pc_id='pc'),
       dict(event='real-bot-llm-request',t=3.6)]
    r=[dict(kind='stt-frame',frame='TranscriptionFrame',frame_id=7,monotonic_ns=3200000000,
            text=c['expected_text'],vendor_result=dict(channel=dict(alternatives=[dict(words=[dict(word='cooking',start=2.8,end=3)])])))]
    return c,e,r


def test_single_turn_delays_and_no_small_sample_p99():
    c,e,r=turn_fixture();v=a.turn_measurements(c,e,r)
    assert v['complete'] and v['accepted_turns'][0]['pc_id']=='pc'
    assert v['accepted_turns'][0]['turn_completion_delay_s']==.5
    assert v['accepted_turns'][0]['final_segment_delay_s']==pytest.approx(.2)
    assert a.stats([1,2,3])==dict(count=3,median=2,maximum=3,population_p99=False)
    assert a.stats([])['maximum'] is None


@pytest.mark.parametrize('fault',['split','late-final','missing-boundary','missing-words','missing-vad','wrong-text','missing-request'])
def test_missing_or_split_turn_is_never_merged_to_success(fault):
    c,e,r=turn_fixture()
    if fault=='split':
        e[-2]['text']='Please change the subject to'
        e.extend([dict(event='user-turn-stopped',text='cooking.',t=4,strategy='SmartTurn',pc_id='pc'),dict(event='real-bot-llm-request',t=4.1)])
    if fault=='late-final':r[0]['monotonic_ns']=4000000000
    if fault=='missing-boundary':e=[x for x in e if x['event']!='nova-transcript-boundary']
    if fault=='missing-words':r[0]['vendor_result']={}
    if fault=='missing-vad':e=[x for x in e if x.get('frame')!='VADUserStoppedSpeakingFrame']
    if fault=='wrong-text':e[-2]['text']='Wrong sentence.'
    if fault=='missing-request':e.pop()
    value=a.turn_measurements(c,e,r)
    assert not value['complete']
    if fault=='split':assert len(value['accepted_turns'])==2


def test_actual_streaming_flat_metadata_shape(plan):
    events=event_binding(plan)[:1]
    events.append(dict(event='stt-model-observed',model_uuid=plan['vendor']['model_uuid'],
        model_info=dict(name=plan['vendor']['name'],version=plan['vendor']['version'])))
    assert n.profile_witness(plan,events)['passed']
    events[-1].pop('model_uuid')
    assert not n.profile_witness(plan,events)['passed']


def test_missing_evidence_retains_six_incomplete_rows_without_writing(plan,tmp_path):
    plan.update(installed_packages_sha256={},runtime_sha256={})
    out=tmp_path/'absent'
    result=a.analyze(plan,out)
    assert not result['complete_measurements'] and not result['final_freeze_ready']
    assert result['summary']['complete_captures']==0 and len(result['measurements'])==6
    assert not out.exists()


@pytest.mark.parametrize('fault',[None,'vendor','missing-llm-end','missing-final-segment','missing-context-pcm','missing-runner-acceptance'])
def test_preflight_complete_evidence_and_no_missing_credit(plan,tmp_path,monkeypatch,fault):
    import echoear_aec_campaign_analysis as campaign_analysis
    import echoear_aec_host_gate as host
    from echoear_aec_bench import write
    plan.update(installed_packages_sha256={},runtime_sha256={})
    out=tmp_path/'synthetic-run';out.mkdir();(out/'evaluation').mkdir()
    session=out/'sessions/only';session.mkdir(parents=True)
    events=event_binding(plan);recorded=[];accepted=[];metrics=[]
    for i,cell in enumerate(plan['cases']):
        cid=cell['id']+'-a1';context='context-'+str(i);base=i*20+100
        folder=out/cid;folder.mkdir()
        def event(kind,t,**kw):return dict(event=kind,t=base+t,case_id=cid,pc_id='pc',**kw)
        def rec(kind,t,**kw):return dict(kind=kind,monotonic_ns=int((base+t)*1e9),pc_id='pc',**kw)
        events.extend([event('tts-context-pcm',4,context_id=context,path=context+'.pcm'),
            event('tts-pcm',4,context_id=context,bytes=160000),
            event('tts-generation-stopped',9,context_id=context),
            event('frame',4,frame='BotStartedSpeakingFrame'),event('frame',9.1,frame='BotStoppedSpeakingFrame')])
        (out/'evaluation'/ (context+'.pcm')).write_bytes(np.full(80000,1000,dtype='<i2').tobytes())
        recorded.append(rec('tts-transport-write',8.96,context_id=context,non_silent=True,samples=640))
        metrics.append(dict(context_id=context,complete=True,caller_anchor='real-bot-llm-request' if cell.get('real_bot_reply') else 'script-request',lead_s=.1))
        if cell.get('expected_text'):
            events.extend([event('user-turn-started',1,strategy='VAD'),event('user-turn-stopped',3.5,text=cell['expected_text'],strategy='SmartTurn'),
                event('real-bot-llm-request',3.6),event('frame',1,frame='VADUserStartedSpeakingFrame'),
                event('frame',3,frame='VADUserStoppedSpeakingFrame'),event('nova-transcript-boundary',3.2,frame_id=i)])
            recorded.extend([rec('stt-frame',3.2,frame='TranscriptionFrame',frame_id=i,text=cell['expected_text'],vendor_result=dict(channel=dict(alternatives=[dict(words=[dict(word='word',start=1,end=3)])]))),
                rec('frame',4,frame='LLMFullResponseEndFrame'),rec('aggregator-assistant-turn-stopped',9.1,text='Complete answer.')])
        accepted.append(dict(run_id=cid,start_ns=int(base*1e9),end_ns=int((base+12.1)*1e9),noise_gate_passed=True,quiet_windows_valid=True))
        write(folder/'attempt-window.json',dict(window_s=[base-7.3,base+14]))
        write(folder/'capture-duration.json',dict(elapsed_s=14.4,limit_s=28))
        write(folder/'export-duration.json',dict(elapsed_s=1))
        write(folder/'rf-pre-gate.json',dict(passed=True))
    if fault=='vendor':events[1]['models']=['changed']
    if fault=='missing-llm-end':recorded=[e for e in recorded if e.get('frame')!='LLMFullResponseEndFrame']
    if fault=='missing-final-segment':recorded=[e for e in recorded if e.get('frame')!='TranscriptionFrame']
    if fault=='missing-context-pcm':(out/'evaluation/context-5.pcm').unlink()
    if fault=='missing-runner-acceptance':
        rejected=accepted.pop()
        write(out/rejected['run_id']/'result.json',dict(cases=[rejected]))
    (out/'evaluation/events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    (session/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in recorded))
    write(out/'result.json',dict(completed=True,cases=accepted,restored_elf_sha256=n.NORMAL_HASH,mixer_equal=True,lock_released=True,cleanup_errors=[]))
    write(out/'elapsed.json',dict(completed=True,seconds=500))
    monkeypatch.setattr(campaign_analysis,'generation_audit',lambda *a:dict(passed=True,sessions=[dict(pc_id='pc',generations=metrics)]))
    monkeypatch.setattr(host,'audit',lambda *a:dict(passed=True))
    value=a.analyze(plan,out,include_rejected=True)
    assert value['complete_measurements']==(fault is None)
    assert not value['final_freeze_ready'] and not value['launch_authorized']
    if fault is None:
        assert value['summary']['capture_s']['count']==6
        assert value['summary']['real_reply_pcm_s']==dict(count=4,median=5,maximum=5,population_p99=False)
    if fault=='vendor':assert value['summary']['capture_s']['count']==0
    if fault=='missing-runner-acceptance':
        assert value['measurements'][-1]['complete']
        assert not value['measurements'][-1]['accepted_by_runner']
        assert value['summary']['complete_captures']==5
