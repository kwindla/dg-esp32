import copy
import json
import signal
from types import SimpleNamespace

import numpy as np
import pytest

import echoear_aec_echo_dropout as d


def source(tmp_path):
    p = dict(image='/candidate', elf_sha256=d.CANDIDATE, gain_db=15, boot_ref_gain_db=15,
             bot_config={}, noise_gate={}, preflight_contract={}, host_environment={},
             first_hold_periodicity={}, gates={}, v85=True, fixture_policy={}, failure_policy={})
    path = tmp_path/'source.json'
    path.write_text(json.dumps(p))
    return path


def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'runtime_files', lambda: ['hil/echoear_aec_metrics.py'])
    monkeypatch.setattr(d, 'verify_bundle', lambda *a: None)
    return d.prepare(source(tmp_path), tmp_path/'prep')


def test_fixed_schedule_has_eight_bakers_two_distinct_controls_no_jabra(tmp_path):
    p = d.plan_from(source(tmp_path))
    assert len(p['cases']) == 10
    assert [c['role'] for c in p['cases']] == ['target']*4+['control']+['target']*4+['control']
    assert sum(c['text'] == d.TARGET for c in p['cases']) == 8
    assert {c['text'] for c in p['cases'] if c['role']=='control'} == set(d.CONTROLS)
    assert all(c['volume_db']==-6 and c['kind']=='echo' and c['stop_offset_s'] is None
               and 'fixture' not in c for c in p['cases'])
    assert p['cases'][0]['aec_state']=='cold-boot'
    assert all(c['aec_state']=='warm' for c in p['cases'][1:])
    assert p['attempts_per_cell']==1 and p['reconnects']==p['spares']==[]
    assert p['budget']['active_s']+p['budget']['restore_reserve_s']==1200


def test_prepare_validate_are_file_only_and_refuse_overwrite(tmp_path, monkeypatch):
    import echoear_aec_campaign as campaign
    monkeypatch.setattr(campaign, 'bench_lock', lambda: pytest.fail('prep took lock'))
    monkeypatch.setattr(campaign, 'flash', lambda *a: pytest.fail('prep flashed'))
    monkeypatch.setattr(campaign, 'resolve_jabra', lambda: pytest.fail('prep touched mixer'))
    p=prepared(tmp_path,monkeypatch)
    assert d.validate(p)
    assert (tmp_path/'prep/plan.json').exists()
    with pytest.raises(FileExistsError): d.prepare(tmp_path/'source.json', tmp_path/'prep')


@pytest.mark.parametrize('field,value', [('elf_sha256','bad'),('gain_db',24),('boot_ref_gain_db',0),
    ('attempts_per_cell',3),('qualification',True),('budget',{}),('restore',{})])
def test_tampered_plan_refused(tmp_path,monkeypatch,field,value):
    p=prepared(tmp_path,monkeypatch);p[field]=value
    with pytest.raises(ValueError):d.validate(p)


def test_changed_source_runtime_and_schedule_refused(tmp_path,monkeypatch):
    p=prepared(tmp_path,monkeypatch)
    bad=copy.deepcopy(p);bad['cases'][0]['stop_offset_s']=1
    with pytest.raises(ValueError):d.validate(bad)
    bad=copy.deepcopy(p);bad['runtime_sha256']['hil/echoear_aec_metrics.py']='bad'
    with pytest.raises(ValueError):d.validate(bad)
    (tmp_path/'source.json').write_text((tmp_path/'source.json').read_text()+'\n')
    with pytest.raises(ValueError):d.validate(p)


def signals(frames=20):
    n=frames*d.FRAME+d.AFE_DELAY
    raw=np.full(n,1000.);ref=np.zeros(n);ref[:frames*d.FRAME]=100
    afe=np.zeros(n);afe[d.AFE_DELAY:]=10
    # A five-frame, exactly 100 ms residual burst. Its active source is identical.
    afe[d.AFE_DELAY+5*d.FRAME:d.AFE_DELAY+10*d.FRAME]=500
    return raw,afe,ref,np.ones(3200)


def test_frame_metric_known_burst_and_fixed_alignment():
    raw,afe,ref,quiet=signals()
    v=d.frame_analysis(raw,afe,ref,quiet)
    expected=5*500**2/(5*500**2+15*10**2)
    assert v['worst_five_frames']==list(range(5,10))
    assert v['worst_five_share']==pytest.approx(expected)
    assert v['whole']['active_frames']==20
    assert v['whole']['integrated_reduction_db']==pytest.approx(10*np.log10(1000**2/((5*500**2+15*10**2)/20)))
    assert v['without_worst_five_report_only']['integrated_reduction_db']==pytest.approx(40)
    assert v['frames'][5]['afe_start_sample']==5*320+1024
    assert v['frames'][5]['residual_energy']==500**2*320
    assert v['frames'][5]['reduction_db']==pytest.approx(20*np.log10(2))


def test_activity_independent_of_output_and_worst_five_need_not_be_contiguous():
    raw,afe,ref,quiet=signals()
    afe[:]=10000
    v=d.frame_analysis(raw,afe,ref,quiet)
    assert v['whole']['active_frames']==20
    afe[:]=0
    for i in (1,4,7,12,19):afe[d.AFE_DELAY+i*320:d.AFE_DELAY+(i+1)*320]=100
    v=d.frame_analysis(raw,afe,ref,quiet)
    assert v['worst_five_frames']==[1,4,7,12,19] and v['worst_five_share']==1
    assert v['whole']['active_frames']==20


def test_tail_activity_never_dropped_to_fake_delayed_coverage():
    raw,afe,ref,quiet=signals();ref[-640:]=100
    v=d.frame_analysis(raw,afe,ref,quiet)
    assert not v['whole']['scorable'] and v['active_without_delayed_output']>0
    assert v['worst_five_share'] is None


def test_silent_reference_and_zero_residual_are_not_fabricated_shares():
    raw,afe,ref,quiet=signals();ref[:]=0
    assert not d.frame_analysis(raw,afe,ref,quiet)['whole']['scorable']
    ref[:20*320]=100;afe[:]=0
    assert d.frame_analysis(raw,afe,ref,quiet)['worst_five_share'] is None


def test_analyze_three_channel_order_hashes_and_gaps(tmp_path):
    raw,afe,ref,quiet=signals()
    capture=tmp_path/'capture.npz';q=tmp_path/'quiet.npz'
    np.savez(q,pcm=np.stack([quiet,quiet,quiet],axis=1),rate=d.RATE)
    def save(gap):
        np.savez(capture,pcm=np.stack([raw,ref,afe],axis=1),rate=d.RATE,
                 timing=np.array([[0,len(raw),1,2,gap]]))
    save(0);before=d.sha(capture)
    v=d.analyze_capture(capture,q)
    assert v['whole']['active_frames']==20 and d.sha(capture)==before
    assert v['inputs_sha256'][str(capture)]==before
    save(1);v=d.analyze_capture(capture,q)
    assert not v['whole']['scorable'] and v['worst_five_share'] is None
    assert not v['without_worst_five_report_only']['scorable']


def fake_hold(log, fail=None):
    class Hold:
        def __init__(self,p,s,out):
            self.result=dict(completed=True,restored_elf_sha256=d.NORMAL_HASH,mixer_equal=True,
                             lock_released=True,cleanup_errors=[])
        def __enter__(self):log.append('enter');return self
        def connect(self,cells,reconnects):log.append('connect');assert reconnects==[]
        def capture(self,group,attempt):
            log.append(group['run_id']);assert attempt==1
            if fail=='capture':raise RuntimeError('capture failed')
            if fail=='deadline':raise d.BudgetExpired('deadline')
            return fail!='noise',[]
        def finish(self):log.append('finish')
        def __exit__(self,*exc):
            log.append('restore')
            if fail=='mixer':self.result['mixer_equal']=False
            if fail=='restore':self.result.pop('restored_elf_sha256')
            if fail=='lock':self.result['lock_released']=False
    return Hold


def test_acquisition_exact_order_and_restore(tmp_path):
    p=d.plan_from(source(tmp_path));log=[]
    d.acquire(p,tmp_path,SimpleNamespace(remaining=lambda:840),fake_hold(log))
    assert log==['enter','connect']+[c['id'] for c in p['cases']]+['finish','restore']


@pytest.mark.parametrize('failure',['capture','deadline','noise'])
def test_failure_keeps_attempt_and_restores_no_retry(tmp_path,failure):
    p=d.plan_from(source(tmp_path));log=[]
    with pytest.raises((RuntimeError,d.BudgetExpired)):
        d.acquire(p,tmp_path,SimpleNamespace(remaining=lambda:840),fake_hold(log,failure))
    assert log==['enter','connect',p['cases'][0]['id'],'restore']


def test_budget_does_not_start_another_cell(tmp_path):
    p=d.plan_from(source(tmp_path));log=[];remaining=iter([210,209])
    with pytest.raises(d.BudgetExpired):
        d.acquire(p,tmp_path,SimpleNamespace(remaining=lambda:next(remaining)),fake_hold(log))
    assert log==['enter','connect',p['cases'][0]['id'],'restore']


@pytest.mark.parametrize('failure',['mixer','restore','lock'])
def test_missing_cleanup_witness_fails(tmp_path,failure):
    with pytest.raises(RuntimeError,match='witness'):
        d.acquire(d.plan_from(source(tmp_path)),tmp_path,SimpleNamespace(remaining=lambda:840),fake_hold([],failure))


def test_signal_deadline_and_operator_stop_restore_previous_handlers():
    old={s:signal.getsignal(s) for s in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
    with d.ActivityBudget() as b:
        assert 830<b.remaining()<=840
        for s in old:
            with pytest.raises(d.BudgetExpired):signal.getsignal(s)(s,None)
        b.cleanup()
        assert signal.getitimer(signal.ITIMER_REAL)==(0.,0.)
        assert all(signal.getsignal(s)==signal.SIG_IGN for s in old)
    assert {s:signal.getsignal(s) for s in old}==old


def test_real_cleanup_adapter_stops_before_parent_restore_and_disarms(monkeypatch,tmp_path):
    import echoear_aec_campaign as c
    log=[];budget=SimpleNamespace(cleanup=lambda:log.append('disarm'))
    monkeypatch.setattr(c,'capture_stop',lambda b:log.append('anchored stop') or {'stop':1})
    # The factory closes over its imports, so construct again after patching.
    monkeypatch.setattr(c,'command',lambda *a:log.append('audio off'))
    cls=d.diagnostic_hold(budget)
    def parent_exit(self,*exc):log.append('parent restore')
    monkeypatch.setattr(c.BenchHold,'__exit__',parent_exit)
    h=cls({}, {}, tmp_path);h.board=object()
    h.__exit__(d.BudgetExpired,d.BudgetExpired('deadline'),None)
    assert log==['disarm','anchored stop','audio off','parent restore']
    assert (tmp_path/'emergency-stop-anchor.json').exists()
    assert h.result['error_class']=='diagnostic-budget-or-operator-stop'
    assert h.result['completed'] is False


def test_no_live_path_without_reviewed_hash(tmp_path,monkeypatch):
    plan=tmp_path/'plan.json';plan.write_text('{}')
    monkeypatch.setattr(d,'validate',lambda p:pytest.fail('bad hash reached validate'))
    with pytest.raises(ValueError,match='reviewed'):
        d.run(plan,tmp_path/'run','no')


def test_generated_launcher_exports_flash_interpreter(tmp_path, monkeypatch):
    import os
    import subprocess
    # Only a stub uv is executed: it records argv/environment, never Python/HIL.
    fake_bin=tmp_path/'bin';fake_bin.mkdir()
    fake_uv=fake_bin/'uv'
    fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$ESPTOOL_PYTHON" "$@" > "$LAUNCH_RECORD"\n')
    fake_uv.chmod(0o755)
    monkeypatch.setattr(d,'runtime_files',lambda:['hil/echoear_aec_metrics.py'])
    monkeypatch.setattr(d,'verify_bundle',lambda *a:None)
    prep=tmp_path/'prep with spaces';run_out=tmp_path/'echo-dropout-02'
    d.prepare(source(tmp_path),prep,run_out=run_out)
    script=prep/'launch-after-review.sh';record=tmp_path/'argv.txt'
    env=dict(os.environ,PATH=str(fake_bin)+':'+os.environ['PATH'],
             ESPTOOL_PYTHON='/wrong/inherited/python',LAUNCH_RECORD=str(record),
             ECHO_DROPOUT_REVIEWED_PLAN_SHA256=d.sha(prep/'plan.json'))
    subprocess.run(['bash','-n',str(script)],check=True)
    subprocess.run(['bash',str(script)],env=env,check=True)
    rows=record.read_text().splitlines()
    assert rows[0]==d.ESPTOOL_PYTHON
    assert rows[rows.index('--out')+1]==str(run_out)
    assert rows[rows.index('--plan')+1]==str(prep/'plan.json')
    assert rows[-1]==d.sha(prep/'plan.json')
    assert not run_out.exists()


@pytest.mark.parametrize('interpreter',[None,'/selected/python'])
@pytest.mark.parametrize('failure',['import','missing','timeout'])
def test_esptool_preflight_failure_precedes_all_bench_work(tmp_path,monkeypatch,interpreter,failure):
    import subprocess
    import sys
    import step9a_echoear as flash_tools
    import echoear_aec_campaign as campaign
    if interpreter is None:monkeypatch.delenv('ESPTOOL_PYTHON',raising=False)
    else:monkeypatch.setenv('ESPTOOL_PYTHON',interpreter)
    wanted=interpreter or sys.executable
    checked=[]
    def probe(cmd,**kw):
        checked.append(cmd)
        assert cmd==[flash_tools.esptool_python(),'-c','import esptool']
        if failure=='missing':raise FileNotFoundError(wanted)
        if failure=='timeout':raise subprocess.TimeoutExpired(cmd,10)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(flash_tools.subprocess,'run',probe)
    monkeypatch.setattr(d,'validate',lambda p:True)
    monkeypatch.setattr(d,'diagnostic_hold',lambda *a:pytest.fail('constructed bench'))
    monkeypatch.setattr(d.ActivityBudget,'__enter__',lambda *a:pytest.fail('armed deadline'))
    monkeypatch.setattr(campaign,'bench_lock',lambda:pytest.fail('took bench lock'))
    plan=tmp_path/'plan.json';plan.write_text('{}');out=tmp_path/'run'
    with pytest.raises((RuntimeError,FileNotFoundError,subprocess.TimeoutExpired)):
        d.run(plan,out,d.sha(plan))
    assert checked==[[wanted,'-c','import esptool']]
    assert not out.exists()


def test_successful_preflight_precedes_acquisition(tmp_path,monkeypatch):
    import echoear_aec_accept as accept
    log=[]
    monkeypatch.setattr(d,'validate',lambda p:log.append('validate'))
    monkeypatch.setattr(accept,'check_esptool',lambda:log.append('check esptool'))
    monkeypatch.setattr(d,'diagnostic_hold',lambda b:object())
    monkeypatch.setattr(d,'acquire',lambda *a:log.append('acquire'))
    plan=tmp_path/'plan.json';plan.write_text('{}')
    d.run(plan,tmp_path/'run',d.sha(plan))
    assert log==['validate','check esptool','acquire']


@pytest.mark.parametrize('evaluation_created',[False,True])
def test_pre_acquisition_failure_restores_without_evaluation_audit(tmp_path,monkeypatch,evaluation_created):
    import echoear_aec_campaign as c
    log=[]
    cls=d.diagnostic_hold(SimpleNamespace(cleanup=lambda:log.append('disarm')))
    h=cls({}, {}, tmp_path)
    h.touched=True
    if evaluation_created:h.evaluation=tmp_path/'evaluation'
    h.lock=SimpleNamespace(__exit__=lambda *a:log.append('release lock'))
    monkeypatch.setattr(c,'flash',lambda image,digest,out,label:log.append(('flash',digest,label)))
    monkeypatch.setattr(c,'boot',lambda out,label,digest:log.append(('boot',digest,label)) or SimpleNamespace(close=lambda:log.append('close')))
    monkeypatch.setattr(c.BenchHold,'finish',lambda *a:pytest.fail('audited nonexistent live session'))
    (tmp_path/'mixer-before.state').write_text('original mixer')
    def mixer(cmd,**kw):
        assert cmd[0]=='alsactl'
        log.append(('mixer',cmd[-2]))
        if cmd[-2]=='store':(tmp_path/'mixer-after.state').write_text('original mixer')
    monkeypatch.setattr(c.subprocess,'run',mixer)
    error=RuntimeError('original flash/startup failure')
    h.__exit__(type(error),error,None)
    r=json.loads((tmp_path/'result.json').read_text())
    assert r['error']=='RuntimeError: original flash/startup failure'
    assert 'finish_error' not in r and r['completed'] is False
    assert r['final_audit']['status']=='not-started'
    assert r['restored_elf_sha256']==d.NORMAL_HASH and r['mixer_equal'] and r['lock_released']
    assert r['cleanup_errors']==[]
    assert log==['disarm',('flash',d.NORMAL_HASH,'restore'),('boot',d.NORMAL_HASH,'restore'),
                 'close',('mixer','restore'),('mixer','store'),'release lock']


def test_started_evaluation_still_requires_normal_finish_audit(tmp_path,monkeypatch):
    import echoear_aec_campaign as c
    cls=d.diagnostic_hold(SimpleNamespace(cleanup=lambda:None))
    h=cls({}, {}, tmp_path);h.evaluation=tmp_path/'evaluation';h.bot=object()
    def audit(self):raise RuntimeError('live audit failed')
    monkeypatch.setattr(c.BenchHold,'finish',audit)
    with pytest.raises(RuntimeError,match='live audit failed'):h.finish()
