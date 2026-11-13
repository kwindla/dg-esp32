import copy
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
import echoear_aec_campaign as runner
from echoear_aec_source_preflight import measure,preflight,validate_source
from echoear_aec_campaign_resume import resume_state,resolve_host_interruption
from test_aec_campaign import plan,bind_prior


def pcm(active):return np.repeat(np.array(active,dtype=np.int16)*1000,320)


def test_continuity_bridges_100ms_but_not_120ms_and_excludes_edges():
    row=measure(pcm([0]*10+[1]*100+[0]*5+[1]*100+[0]*20))
    assert row['passed'] and row['speech_onset_s']==.2
    assert row['continuous_active_seconds']==4.1 and row['max_internal_silent_run_ms']==100
    assert row['max_silent_run_ms']==400
    row=measure(pcm([1]*100+[0]*6+[1]*250))
    assert not row['passed'] and row['continuous_active_seconds']==2 and row['longest_span_seconds']==5
    assert row['max_internal_silent_run_ms']==120
    assert not measure(pcm([0]*300))['passed']
    assert measure(pcm([1]*200))['passed']
    assert not measure(pcm([1]*199))['passed']


def test_preflight_binds_pcm_text_declaration_and_recomputes_metric(tmp_path):
    p=plan();source=tmp_path/'source.json';source.write_text(json.dumps(dict(text=p['text'])))
    audio=tmp_path/'source.pcm';audio.write_bytes(pcm([1]*210).astype('<i2').tobytes())
    value=preflight(audio,source,p);out=tmp_path/'preflight.json';runner.write(out,value)
    witness=dict(path=str(out),sha256=runner.sha(out),continuous_active_seconds=value['continuous_active_seconds'])
    assert validate_source(p,witness)['passed']
    bad=dict(value,continuous_active_seconds=9);runner.write(out,bad);witness['sha256']=runner.sha(out)
    with pytest.raises(ValueError,match='metric'):validate_source(p,witness)
    runner.write(out,value);witness['sha256']=runner.sha(out);audio.write_bytes(pcm([1]*211).astype('<i2').tobytes())
    with pytest.raises(ValueError,match='metric'):validate_source(p,witness)
    source.write_text(json.dumps(dict(text='changed')))
    with pytest.raises(ValueError,match='text differs'):preflight(audio,source,p)


@pytest.mark.parametrize('error,kind',[(OSError('USB read'),'serial-or-host'),(KeyboardInterrupt(),'serial-or-host'),(TimeoutError('Serial timeout waiting for PERF'),'serial-or-host'),(RuntimeError('I2S fault'),'device-fault'),(RuntimeError('Observer health failure'),'device-fault'),(TimeoutError('Ready missed'),'device-fault'),(runner.EnvironmentalPause('noise'),'environmental-pause')])
def test_any_exception_attempts_finish_and_records_error_class(tmp_path,error,kind):
    calls=[]
    h=SimpleNamespace(out=tmp_path,result={},save=lambda:None)
    def finish():calls.append('finish');h.result['completed']=True
    h.finish=finish
    runner.finish_after_exception(h,error)
    assert calls==['finish'] and h.result['error_class']==kind and h.result['completed']


def test_failed_audit_cannot_make_host_error_resumable(tmp_path):
    h=SimpleNamespace(out=tmp_path,result={},save=lambda:None)
    def finish():raise RuntimeError('Resource floor violation')
    h.finish=finish;runner.finish_after_exception(h,OSError('USB'))
    assert not h.result['completed'] and h.result['error_class']=='device-fault'
    assert 'Resource floor' in h.result['finish_error']


def test_host_resume_requires_integrity_and_restore_and_device_fault_refused(tmp_path):
    p=plan();root=tmp_path/'prior';hold=root/'qualification-01';hold.mkdir(parents=True)
    value=dict(completed=True,lock_released=True,mixer_equal=True,cleanup_errors=[],restored_elf_sha256=runner.NORMAL_HASH,error='SerialException',error_class='serial-or-host')
    runner.write(hold/'result.json',value);p['resume_from']=bind_prior(root,plan())
    assert 'qualification-01' in resume_state(p)
    for changes in (dict(completed=False),dict(restored_elf_sha256='bad'),dict(error_class='device-fault'),dict(error_class='unknown')):
        runner.write(hold/'result.json',dict(value,**changes));p['resume_from']=bind_prior(root,plan())
        with pytest.raises(ValueError):resume_state(p)


def test_serial_interruption_spends_inflight_attempt_and_carries_remaining_ids(tmp_path):
    p=plan();s=p['sessions'][0];cases=[c for c in p['cases']+p['controls'] if c['session_id']==s['id'] and c['kind']!='probe'];spares=[c for c in p['spares'] if c['session_id']==s['id']]
    queue=runner.Schedule(cases,spares,p['noise_gate']);group,attempt=queue.next()
    runner.write(tmp_path/'schedule.json',queue.state())
    h=SimpleNamespace(p=p,s=s,out=tmp_path,result=dict(cases=[],error='USB interrupted',completed=True))
    resolve_host_interruption(h)
    state=json.loads((tmp_path/'schedule.json').read_text());resumed=runner.Schedule(cases,spares,p['noise_gate'],state)
    assert state['inflight'] is None and state['journal'][0]['attempt']==1
    assert [(g['run_id'],a) for g,a in resumed.queue.pending][-1]==(group['run_id'],2)
    assert len(resumed.queue.pending)==len(runner.capture_groups(cases))


def test_serial_failure_after_saved_cell_does_not_reacquire_performance_miss(tmp_path,monkeypatch):
    p=plan();s=p['sessions'][0];cases=[c for c in p['cases']+p['controls'] if c['session_id']==s['id'] and c['kind']!='probe'];spares=[c for c in p['spares'] if c['session_id']==s['id']]
    queue=runner.Schedule(cases,spares,p['noise_gate']);group,attempt=queue.next();runner.write(tmp_path/'schedule.json',queue.state())
    saved=[dict(c,attempt=1,run_id=c['id']+'-a1',noise_gate_passed=True) for c in group['cells']]
    import echoear_aec_accept_analysis
    monkeypatch.setattr(echoear_aec_accept_analysis,'main',lambda root:dict(cases=[dict(id=c['run_id'],cell_id=c['id'],opportunity='echo-only') for c in saved]))
    h=SimpleNamespace(p=p,s=s,out=tmp_path,result=dict(cases=saved,error='USB',completed=True))
    resolve_host_interruption(h)
    state=json.loads((tmp_path/'schedule.json').read_text())
    assert state['journal'][0]['action']=='accepted' and group['run_id'] not in {r['id'] for r in state['pending']}


def test_real_context_exit_finishes_before_restore_and_unlock(tmp_path,monkeypatch):
    calls=[];h=runner.BenchHold(plan(),plan()['sessions'][0],tmp_path)
    def finish():calls.append('audit');h.result['completed']=True
    h.finish=finish;h.touched=True
    h.board=SimpleNamespace(close=lambda:calls.append('close'))
    h.lock=SimpleNamespace(__exit__=lambda *a:calls.append('unlock'))
    (tmp_path/'mixer-before.state').write_text('saved')
    monkeypatch.setattr(runner,'flash',lambda *a:calls.append('restore'))
    monkeypatch.setattr(runner,'boot',lambda *a:SimpleNamespace(close=lambda:calls.append('boot-hash')))
    def alsa(args,**kwargs):
        if args[-2]=='store':Path(args[2]).write_text('saved')
    monkeypatch.setattr(runner.subprocess,'run',alsa)
    h.__exit__(OSError,OSError('USB'),None)
    assert calls==['audit','close','restore','boot-hash','unlock']
    value=json.loads((tmp_path/'result.json').read_text())
    assert value['completed'] and value['error_class']=='serial-or-host'
    assert value['restored_elf_sha256']==runner.NORMAL_HASH and value['mixer_equal'] and value['lock_released']


def test_failed_host_recording_does_not_prevent_cleanup():
    def fail_save():raise OSError('disk full')
    h=SimpleNamespace(result={},save=fail_save,finish=lambda:(_ for _ in ()).throw(OSError('serial')))
    runner.finish_after_exception(h,OSError('host'))
    assert not h.result['completed'] and h.result['cleanup_errors']==['finish-record: disk full']
