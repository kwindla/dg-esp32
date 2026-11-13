"""AEC adapters run through frozen validators and R7; all IO is fake."""
from contextlib import contextmanager
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import p4_aec_product as product
import p4_aec_measure as measure
from make_bundle import digest
from test_wire_contract import capture,serialize,IDENTITY
from test_p4_contention import valid_run,rewrite_wire
from test_p4_audio_fix4 import cost_fixture
from wire_contract import ContractError


def aec_run(tmp_path,monkeypatch):
    row=valid_run(tmp_path,monkeypatch);row.update(aec_enabled=True,variant='aec-normal')
    stacks=cost_fixture(tmp_path/'cost')
    for session in row['sessions']:
        rewrite_wire(tmp_path,session)
        path=tmp_path/session['wire_file'];lines=path.read_text().replace('"aec":false','"aec":true')
        path.write_text(lines);session['wire_sha256']=digest(path)
        for perf in session['samples']:
            perf.update(aec=dict(enabled=True,errors=0,reference_clips=0,output_clips=0))
            for key in ('tx_stack','decode_stack','owner_stack','rx_stack','rx_stack_bytes','tasks'):perf[key]=copy.deepcopy(stacks[key])
    return row


def test_aec_13b_runs_unchanged_timing_resources_restart_and_stack_gates(tmp_path,monkeypatch):
    row=aec_run(tmp_path,monkeypatch)
    result=product.score_contention(tmp_path,tmp_path/'bounds.json',row)
    assert result['checks_passed'] and result['aec_enabled'] and len(result['task_stack_reports'])==2
    assert not result['qualification']


@pytest.mark.parametrize('change',['aec-off','wire-off','clip','missing-counter','stack','duration','heap','restart','limits','wire-hash'])
def test_aec_contention_cannot_bypass_old_or_additive_gates(tmp_path,monkeypatch,change):
    row=aec_run(tmp_path,monkeypatch);perf=row['sessions'][0]['samples'][0]
    if change=='aec-off':row['aec_enabled']=False
    elif change=='wire-off':
        session=row['sessions'][0];path=tmp_path/session['wire_file'];path.write_text(path.read_text().replace('"aec":true','"aec":false'));session['wire_sha256']=digest(path)
    elif change=='clip':perf['aec']['output_clips']=1
    elif change=='missing-counter':perf['aec'].pop('errors')
    elif change=='stack':perf['tx_stack']=4095
    elif change=='duration':row['elapsed_s']=299
    elif change=='heap':perf['heap']['internal']['minimum_free']=81919
    elif change=='restart':row['restart']=[]
    elif change=='limits':row['limits']=dict(row['limits'],stall_us=68412)
    elif change=='wire-hash':row['sessions'][0]['wire_sha256']='b'*64
    with pytest.raises(ValueError):product.score_contention(tmp_path,tmp_path/'bounds.json',row)


def test_scoped_legacy_adapter_restores_validator_on_any_exit():
    import wire_contract
    original=wire_contract.validate_capture
    with pytest.raises(RuntimeError):
        with product.aec_contention_validation():
            with pytest.raises(ContractError,match='unexpected'):wire_contract.validate_capture(None,profile='product',intended_aec=False)
            raise RuntimeError()
    assert wire_contract.validate_capture is original


def pair_fixture(tmp_path,variant='aec-normal'):
    directories=[]
    for board,target,chip in (('echoear','esp32s3','ESP32-S3 step 7'),('p4xc','esp32p4','esp32p4')):
        directory=tmp_path/board;directory.mkdir();directories.append(directory)
        labels=['main','resume','cold'] if variant.endswith('demo') else ['main']
        row=dict(board=board,variant=variant,acquisition_passed=True,restore=dict(clean=True,action='p4-product-of-record'),
            pair_id='fixed-pair',elf_sha256='a'*64,identity=IDENTITY,sessions=[dict(label=l) for l in labels])
        (directory/'result.json').write_text(json.dumps(row));(directory/'identity.json').write_text(json.dumps(IDENTITY))
        (directory/'image.json').write_text(json.dumps(dict(profile=variant,aec_enabled=True,elf_sha256='a'*64,boot_ref_gain_db=15,
            p4_aec_calibration=dict(ref_gain_db=15,guard_ms=400))))
        (directory/'boot.log').write_text(f'I (1) dg-esp32: {chip} audio elf_sha256='+('a'*64)+'\nMIC3_GAIN=0x15 analog_db=15\nREADY transport-console\n')
        for label in labels:
            lines=serialize(capture(target,True))
            (directory/(label+'-serial.log')).write_text('\n'.join(l for l in lines if not l.startswith('DEMO_CAPABILITY'))+'\nPHASE closed\n')
            (directory/(label+'-bot.log')).write_text('\n'.join(l for l in lines if l.startswith('DEMO_CAPABILITY'))+'\n')
    return directories


@pytest.mark.parametrize('variant',['aec-normal','aec-demo'])
def test_product_aec_pair_validates_every_session_with_fixed_variant_bounds(tmp_path,variant):
    left,right=pair_fixture(tmp_path,variant)
    result=product.pair(left,right,product.smoke.BOUNDS_FILE)
    assert result['checks_passed'] and len(result['measured'])==(3 if variant.endswith('demo') else 1)
    assert all(len(sources)>=6 for sources in result['sources'])


@pytest.mark.parametrize('change',['variant','pair-id','identity','restore','action','elf','sessions','wire-off','capability','close','target','boot'])
def test_product_pair_rejects_mismatch_or_missing_original_evidence(tmp_path,change):
    left,right=pair_fixture(tmp_path);path=right/'result.json';row=json.loads(path.read_text())
    if change=='variant':row['variant']='normal'
    elif change=='pair-id':row['pair_id']='other'
    elif change=='identity':row['identity']={}
    elif change=='restore':row['restore']['clean']=False
    elif change=='action':row['restore']['action']='keep-tested'
    elif change=='elf':row['elf_sha256']='b'*64
    elif change=='sessions':row['sessions']=[]
    elif change=='wire-off':
        p=right/'main-serial.log';p.write_text(p.read_text().replace('"aec":true','"aec":false'))
    elif change=='capability':(right/'main-bot.log').write_text('')
    elif change=='close':
        p=right/'main-serial.log';p.write_text(p.read_text().replace('PHASE closed',''))
    elif change=='target':
        p=right/'main-serial.log';p.write_text(p.read_text().replace('esp32p4','esp32s3'))
    elif change=='boot':(right/'boot.log').write_text('READY transport-console\n')
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError):product.pair(left,right,product.smoke.BOUNDS_FILE)


def test_applied_gate_demands_rederived_measurements_values_and_fresh_image(tmp_path,monkeypatch):
    selected=dict(ref_gain_db=12,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024)
    row=dict(plan_file='plan',gain_file='gain',guard_files=['guard'],passed=True,selected=selected,source_elf_sha256='a'*64)
    path=tmp_path/'recommendation.json';path.write_text(json.dumps(row))
    monkeypatch.setattr(measure,'recommendation',lambda *args:copy.deepcopy(row))
    image=dict(elf_sha256='b'*64,p4_aec_calibration=selected)
    assert product.applied_gate(image,path)['selected']==selected
    assert 'unmeasured' in product.applied_gate(image,None)['status']
    for mutant in (dict(image,elf_sha256='a'*64),dict(image,p4_aec_calibration=dict(selected,ref_gain_db=15))):
        with pytest.raises(ContractError):product.applied_gate(mutant,path)
    forged=dict(row,passed=False);path.write_text(json.dumps(forged))
    with pytest.raises(ContractError):product.applied_gate(image,path)


@pytest.fixture
def live_rig(tmp_path,monkeypatch):
    import boards,alsa,p4_step14,p4_product,p4_audio
    state=SimpleNamespace(calls=[],noise=False,fail=None,clean=True,action='p4-product-of-record')
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    args=SimpleNamespace(board='p4xc',bundle=tmp_path/'bundle',out=tmp_path/'run',bot_identity=identity,
        restore_record=tmp_path/'restore',restore_record_sha256='f'*64,applied=None,erle_report=tmp_path/'erle',bounds=product.smoke.BOUNDS_FILE)
    manifest=dict(aec_enabled=True,profile='aec-normal',elf_sha256='a'*64,boot_ref_gain_db=0)
    monkeypatch.setattr(boards,'require_bench',lambda:state.calls.append('bench'))
    monkeypatch.setattr(product,'aec_manifest',lambda *args,**kwargs:manifest)
    monkeypatch.setattr(measure,'checked_report',lambda *args:dict(elf_sha256='a'*64,gain_db=0,profile='aec-normal'))
    monkeypatch.setattr(p4_step14,'restore_record',lambda *args:state.calls.append('pin'))
    monkeypatch.setattr(alsa,'record_room_baseline',lambda *args:dict(noise_contaminated=state.noise))
    monkeypatch.setattr(product.contention,'normal_references',lambda *args:None)
    monkeypatch.setattr(product.contention,'bot_identity',lambda *args:None)
    @contextmanager
    def restoring(*args):
        state.calls.append('R7-enter')
        try:yield
        finally:state.calls.append('R7-exit')
    @contextmanager
    def storage(*args):
        state.calls.append('storage-enter')
        try:yield
        finally:state.calls.append('storage-exit')
    @contextmanager
    def adapters():
        state.calls.append('adapters-enter')
        try:yield
        finally:state.calls.append('adapters-exit')
    monkeypatch.setattr(p4_step14,'restoring',restoring);monkeypatch.setattr(p4_step14,'adapters',adapters)
    monkeypatch.setattr(p4_product,'candidate_storage',storage)
    class Guard:
        success=False
        def __enter__(self):state.calls.append('hold');return self
        def touch(self):state.calls.append('touch');args._device_touched=True
        def __exit__(self,*exc):state.calls.append('restore');self.record=dict(clean=state.clean,action=state.action)
        def release_signals(self):state.calls.append('release')
    guard=Guard()
    class IO:
        product_evidence_verified=True
        def __init__(self,*args):state.calls.append('backend')
        def preflight_tools(self):state.calls.append('tools')
        def preflight_restore(self):state.calls.append('restore-preflight')
        def hold(self):return guard
    monkeypatch.setattr(p4_audio,'HardwareIO',IO);monkeypatch.setattr(product,'ProductIO',IO)
    def acquire(*args):
        state.calls.append('acquire')
        if state.fail=='abort':raise KeyboardInterrupt()
        if state.fail=='acquire':raise RuntimeError('failure')
        return dict(acquisition_passed=True)
    def score(*args):
        state.calls.append('score')
        if state.fail=='score':raise ValueError('gate failed')
        return dict(checks_passed=True)
    monkeypatch.setattr(product.contention,'acquire',acquire);monkeypatch.setattr(product,'score_contention',score)
    def execute(args,*,backend):
        args.out.mkdir()
        with backend.hold() as hold:
            hold.touch();result=acquire()
        result['restore']=guard.record;guard.release_signals()
        return result
    monkeypatch.setattr(product.smoke,'execute',execute)
    return args,state,guard


@pytest.mark.parametrize('failure',[None,'abort','acquire','score','unclean','wrong-action'])
def test_contention_restores_on_success_every_failure_and_abort(live_rig,failure):
    args,state,guard=live_rig;state.fail=failure
    if failure=='unclean':state.clean=False
    if failure=='wrong-action':state.action='keep-tested'
    code=product.contention_live(args)
    assert code==(0 if failure is None else 1)
    assert state.calls.index('pin')<state.calls.index('backend')<state.calls.index('touch')<state.calls.index('acquire')
    assert state.calls.index('restore')<state.calls.index('storage-exit')<state.calls.index('R7-exit')
    assert state.calls[-1]=='release'
    assert json.loads((args.out/'acquisition.json').read_text())['acquisition_passed'] is (failure is None)


def test_contention_noise_preflight_never_constructs_backend_or_touches_board(live_rig):
    args,state,guard=live_rig;state.noise=True
    assert product.contention_live(args)==2 and 'backend' not in state.calls and 'touch' not in state.calls


def test_smoke_wraps_frozen_executor_and_product_finish_with_R7_and_storage(live_rig):
    args,state,guard=live_rig
    assert product.smoke_live(args)==0
    assert state.calls.index('R7-enter')<state.calls.index('adapters-enter')<state.calls.index('backend')<state.calls.index('touch')
    assert state.calls.index('restore')<state.calls.index('storage-exit')<state.calls.index('R7-exit')


@pytest.mark.parametrize('change',['missing','image','gain'])
def test_smoke_rejects_missing_or_wrong_erle_report_before_room_or_device(live_rig,monkeypatch,change):
    args,state,guard=live_rig
    if change=='missing':args.erle_report=None
    else:monkeypatch.setattr(measure,'checked_report',lambda *args:dict(elf_sha256='b'*64 if change=='image' else 'a'*64,gain_db=3 if change=='gain' else 0,profile='aec-normal'))
    assert product.smoke_live(args)==3
    result=json.loads((args.out/'result.json').read_text())
    assert result['device_untouched'] and result['retryable'] and not result['score_produced']
    assert 'backend' not in state.calls


def test_smoke_room_noise_cannot_reach_executor_or_construct_backend(live_rig):
    args,state,guard=live_rig;state.noise=True
    assert product.smoke_live(args)==2
    assert json.loads((args.out/'result.json').read_text())['device_untouched']
    assert 'backend' not in state.calls and 'touch' not in state.calls


def test_product_boot_retains_exact_verified_candidate_lines(tmp_path,monkeypatch):
    def boot(self,board):return 3
    monkeypatch.setattr(product.fix4.VoiceIO,'boot',boot)
    io=object.__new__(product.ProductIO);io.out=tmp_path
    board=SimpleNamespace(lines_since=lambda at:[(at,1.,'candidate boot'),(at+1,2.,'READY transport-console')])
    assert io.boot(board)==3 and (tmp_path/'boot.log').read_text()=='candidate boot\nREADY transport-console\n'


@pytest.mark.parametrize('variant',['aec-normal','aec-demo'])
def test_product_finish_uses_own_variant_bounds_and_checks_image_and_stack_before_success(tmp_path,monkeypatch,variant):
    lines=serialize(capture('esp32p4',True));cap=[line for line in lines if line.startswith('DEMO_CAPABILITY')]
    lines=[line for line in lines if not line.startswith('DEMO_CAPABILITY')]+['PHASE closed']
    monkeypatch.setattr(product.smoke.LiveIO,'finish',lambda *args,**kwargs:(lines,cap))
    wanted=product.smoke.bounds_for(product.smoke.BOUNDS_FILE,variant)
    original=product.fix4.product_capture;observed=[]
    def checked(*args,**kwargs):observed.append(kwargs['bounds']);return original(*args,**kwargs)
    monkeypatch.setattr(product.fix4,'product_capture',checked)
    io=object.__new__(product.ProductIO);io.args=SimpleNamespace(board='p4xc',bounds=product.smoke.BOUNDS_FILE)
    io.manifest=dict(profile=variant,aec_enabled=True,boot_ref_gain_db=15,p4_aec_calibration=dict(ref_gain_db=15,guard_ms=400))
    perf=cost_fixture(tmp_path/'cost');perf['generation']=1;io.stack_samples=[perf,perf]
    assert io.finish(object(),'main')==(lines,cap) and io.product_evidence_verified
    assert observed==[wanted] and io.task_stack_reports[0]['stack_checks_passed']
    io.manifest['p4_aec_calibration']['guard_ms']=600
    with pytest.raises(ContractError,match='image settings'):io.finish(object(),'main')
    io.manifest['p4_aec_calibration']['guard_ms']=400;perf['tx_stack']=4095
    with pytest.raises(ContractError,match='tx_stack'):io.finish(object(),'main')
    assert io.finish(object(),'failed')==(lines,cap) # Failed cleanup is not a new product claim.


@pytest.mark.parametrize('operation,filename',[(product.smoke_live,'result.json'),(product.contention_live,'acquisition.json')])
def test_product_preflight_host_error_is_untouched_recorded_with_exit_three(live_rig,monkeypatch,operation,filename):
    import alsa
    args,state,guard=live_rig
    monkeypatch.setattr(alsa,'record_room_baseline',Mock(side_effect=RuntimeError('Jabra missing')))
    assert operation(args)==3
    row=json.loads((args.out/filename).read_text())
    assert row['device_untouched'] and row['retryable'] and row['status']=='preflight-error' and not row['score_produced']
    assert 'touch' not in state.calls


@pytest.mark.parametrize('mode',['off','report-only'])
def test_smoke_cli_llm_content_is_additive_and_defaults_off(monkeypatch,tmp_path,mode):
    observed=[]
    monkeypatch.setattr(product,'smoke_live',lambda args:observed.append(args) or 0)
    argv=['smoke','--board','p4xc','--pair-id','fixed',
        '--restore-record-sha256','f'*64]
    for key in ('bundle','out','bot-dir','bot-identity','restore-record'):
        argv.extend(['--'+key,str(tmp_path/key)])
    if mode=='report-only':argv.extend(['--llm-content',mode])
    assert product.main(argv)==0
    assert observed[0].llm_content==mode
    assert observed[0].calibration is False and observed[0].headless is False


@pytest.mark.parametrize('profile,aec',[('normal',False),('demo',False),('aec-normal',False),('normal',True)])
def test_report_only_rejects_non_aec_manifest_before_room_restore_or_backend(live_rig,monkeypatch,profile,aec):
    args,state,guard=live_rig;args.llm_content='report-only'
    manifest=dict(profile=profile,aec_enabled=aec)
    monkeypatch.setattr(product,'aec_manifest',aec_manifest_original)
    monkeypatch.setattr(product,'verify_bundle',lambda *args:(manifest,{}))
    assert product.smoke_live(args)==3
    row=json.loads((args.out/'result.json').read_text())
    assert '--llm-content report-only requires an AEC-enabled product bundle' in row['error']
    assert row['device_untouched'] and not row['acquisition_passed']
    assert state.calls==['bench']


aec_manifest_original=product.aec_manifest


@pytest.mark.parametrize('variant',['aec-normal','aec-demo'])
@pytest.mark.parametrize('modes',[('off','off'),('report-only','report-only'),('off','report-only'),('report-only','off')])
def test_pair_records_both_content_modes_and_counts_without_changing_comparison(tmp_path,variant,modes):
    left,right=pair_fixture(tmp_path,variant)
    original=product.pair(left,right,product.smoke.BOUNDS_FILE)
    for directory,mode,count in ((left,modes[0],2),(right,modes[1],1)):
        path=directory/'result.json';row=json.loads(path.read_text())
        row.update(llm_content_mode=mode,llm_content_findings=[dict(kind='off_script_reply')]*count)
        path.write_text(json.dumps(row))
    result=product.pair(left,right,product.smoke.BOUNDS_FILE)
    assert result['llm_content']==[
        dict(board='echoear',llm_content_mode=modes[0],finding_count=2),
        dict(board='p4xc',llm_content_mode=modes[1],finding_count=1)]
    assert original['llm_content']==[
        dict(board='echoear',llm_content_mode='off',finding_count=0),
        dict(board='p4xc',llm_content_mode='off',finding_count=0)]
    assert result['checks_passed'] is original['checks_passed'] is True
    assert result['measured']==original['measured']
