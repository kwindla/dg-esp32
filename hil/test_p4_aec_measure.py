"""Fake-device R7 lifecycle, immutable plan/provenance and unscored-only retries."""
from contextlib import contextmanager
import json
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import p4_aec_measure as measure
import p4_aec_policy as policy
from wire_contract import ContractError
from test_wire_contract import IDENTITY
from test_p4_aec_schema import perf_record


@pytest.fixture
def held_rig(tmp_path,monkeypatch):
    import alsa,boards,p4_step14,p4_product
    state=SimpleNamespace(calls=[],noise=False,fail=None,clean=True,action='p4-product-of-record')
    bundle=tmp_path/'bundle';bundle.mkdir()
    plan=tmp_path/'plan.json';plan.write_text('{}')
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    args=SimpleNamespace(action='delay',plan=plan,bundle=bundle,out=tmp_path/'delay/attempt-01',
        restore_record=tmp_path/'restore.json',restore_record_sha256='f'*64,bot_identity=identity)
    monkeypatch.setattr(boards,'require_bench',lambda:state.calls.append('bench'))
    monkeypatch.setattr(measure,'validate_plan',lambda plan:None)
    def record(*args):
        state.calls.append('record-check')
        if state.fail=='pin':raise ValueError('wrong pin')
    monkeypatch.setattr(p4_step14,'restore_record',record)
    def room(*args):
        state.calls.append('room')
        if state.fail=='room':raise RuntimeError('room recorder failed')
        return dict(noise_contaminated=state.noise)
    monkeypatch.setattr(alsa,'record_room_baseline',room)
    monkeypatch.setattr(alsa,'resolve_jabra',lambda:('card','device'))
    monkeypatch.setattr(alsa,'set_volumes',lambda *args:None)
    monkeypatch.setattr(measure,'verify_bundle',lambda *args:(dict(elf_sha256='a'*64),{}))
    @contextmanager
    def restoring(path,sha):
        assert path==args.restore_record and sha==args.restore_record_sha256
        state.calls.append('R7-enter')
        try:yield
        finally:state.calls.append('R7-exit')
    @contextmanager
    def storage(board,bundle,out):
        assert board=='p4xc' and bundle==args.bundle and out==args.out
        state.calls.append('storage-enter')
        try:yield
        finally:state.calls.append('storage-exit')
    monkeypatch.setattr(p4_step14,'restoring',restoring);monkeypatch.setattr(p4_product,'candidate_storage',storage)
    class Guard:
        success=False
        def touch(self):state.calls.append('touch')
        def __enter__(self):state.calls.append('hold-enter');return self
        def __exit__(self,*exc):
            state.calls.append('restore')
            if getattr(state,'restore_observer',None):state.restore_observer()
            self.record=dict(clean=state.clean,action=state.action)
        def release_signals(self):state.calls.append('signals-released')
    class IO:
        def __init__(self,*args):
            state.calls.append('backend')
            if state.fail=='bot':raise RuntimeError('bot identity differs')
        def preflight_tools(self):state.calls.append('tools')
        def preflight_restore(self):state.calls.append('preflight-restore')
        def hold(self):return Guard()
        def flash(self,*args):
            state.calls.append('flash')
            if state.fail=='flash':raise RuntimeError('flash failed')
        @contextmanager
        def open(self):state.calls.append('open');yield object()
        def boot(self,board):state.calls.append('boot')
        def prepare_manual(self,board):state.calls.append('prepare')
        def finish(self,board,label):
            state.calls.append('finish')
            if state.fail=='finish':raise RuntimeError('finish failed')
        def cleanup(self,board):
            state.calls.append('cleanup')
            if getattr(state,'cleanup_observer',None):state.cleanup_observer()
            if state.fail in ('cleanup','both'):raise RuntimeError('cleanup failed')
    return args,dict(elf_sha256='a'*64),state,IO


def test_all_touched_success_paths_restore_the_pinned_normal_and_release_last(held_rig):
    args,plan,state,IO=held_rig
    result=measure.held(args,plan,lambda *args:dict(score=dict(available=True)),io_factory=IO)
    assert result['acquisition_passed'] and result['restore']['action']=='p4-product-of-record'
    assert state.calls.index('record-check')<state.calls.index('room')<state.calls.index('backend')
    assert state.calls.index('R7-enter')<state.calls.index('touch')<state.calls.index('flash')<state.calls.index('open')
    assert state.calls.index('cleanup')<state.calls.index('restore')<state.calls.index('storage-exit')<state.calls.index('R7-exit')
    assert state.calls[-1]=='signals-released'
    assert json.loads((args.out/'result.json').read_text())==result


@pytest.mark.parametrize('failure',['body','abort','flash','cleanup','unclean','wrong-action'])
def test_failure_and_abort_restore_before_releasing_signal_protection(held_rig,failure):
    args,plan,state,IO=held_rig;state.fail=failure
    if failure=='unclean':state.clean=False
    if failure=='wrong-action':state.action='keep-tested'
    def body(*args):
        if failure=='body':raise RuntimeError('measurement failure')
        if failure=='abort':raise KeyboardInterrupt()
        return {}
    result=measure.held(args,plan,body,io_factory=IO)
    assert not result['acquisition_passed'] and result['status']=='acquisition-error'
    assert 'restore' in state.calls and state.calls[-1]=='signals-released'


def test_noise_preflight_is_unscored_without_backend_flash_or_hold(held_rig):
    args,plan,state,IO=held_rig;state.noise=True
    result=measure.held(args,plan,Mock(),io_factory=IO)
    assert result['status']=='preflight-noisy' and result['device_untouched'] and not result['acquisition_passed']
    assert not {'backend','flash','touch','restore'}&set(state.calls)


def test_bad_restore_record_stops_before_any_acoustic_or_device_action(held_rig):
    args,plan,state,IO=held_rig;state.fail='pin'
    result=measure.held(args,plan,Mock(),io_factory=IO)
    assert result['status']=='preflight-error' and result['device_untouched'] and result['retryable']
    assert state.calls==['bench','record-check'] and (args.out/'result.json').exists()


@pytest.mark.parametrize('prior_status',['acquisition-error','missing-result','incomplete-result','preflight-error'])
def test_unscored_errors_and_missing_results_can_be_retried_and_are_marked(held_rig,prior_status):
    args,plan,state,IO=held_rig;prior=args.out.parent/'attempt-00';prior.mkdir(parents=True)
    if prior_status=='incomplete-result':(prior/'result.json').write_text('{')
    elif prior_status!='missing-result':(prior/'result.json').write_text(json.dumps(dict(status=prior_status,device_untouched=False,score_produced=False)))
    result=measure.held(args,plan,lambda *args:{},io_factory=IO)
    assert result['acquisition_passed'] and (prior/'unscored-attempt.json').exists()
    assert json.loads((prior/'unscored-attempt.json').read_text())['score_produced'] is False
    assert measure.original_part(args.out.parent)[0]==args.out/'result.json'


def test_acquired_scored_original_cannot_be_retried_even_if_result_is_lost(held_rig):
    args,plan,state,IO=held_rig
    result=measure.held(args,plan,lambda *args:{},io_factory=IO)
    (args.out/'result.json').unlink();args.out=args.out.parent/'attempt-02'
    with pytest.raises(ContractError,match='scored originals'):measure.held(args,plan,Mock(),io_factory=IO)
    source,row=measure.original_part(args.out.parent)
    assert source.name=='scored-original.json' and row==result


def test_only_an_untouched_noisy_attempt_can_precede_an_original(held_rig):
    args,plan,state,IO=held_rig;prior=args.out.parent/'attempt-00';prior.mkdir(parents=True)
    (prior/'result.json').write_text(json.dumps(dict(status='preflight-noisy',device_untouched=True)))
    result=measure.held(args,plan,lambda *args:{},io_factory=IO)
    assert result['acquisition_passed']
    path,row=measure.original_part(args.out.parent);assert path==args.out/'result.json'


def test_original_part_requires_a_clean_completed_original_and_forbids_scored_retakes(tmp_path):
    for name in ('attempt-01','attempt-02'):
        p=tmp_path/name;p.mkdir();(p/'result.json').write_text(json.dumps(dict(status='acquired',acquisition_passed=True,restore=dict(clean=True))))
    with pytest.raises(ContractError,match='one acquired scored'):measure.original_part(tmp_path)


def test_capture_missing_timing_wrong_order_gaps_and_hash_are_rejected(tmp_path):
    path=tmp_path/'capture.npz'
    def save(**changes):
        values=dict(pcm=np.zeros((8192,5),dtype='<i2'),schema=2,rate=16000,
            channels=['mic1','mic2','mic3','mic4','processed'],timing=np.array([[0,8192,1,2000000,0]]))
        values.update(changes);np.savez(path,**values);return measure.digest(path)
    good=save();pcm,timing=policy.capture(path,good);assert pcm.shape==(8192,5)
    for changes in (dict(schema=1),dict(channels=['a']*5),dict(timing=np.array([[0,8192,1,2000000,1]])),dict(timing=np.empty((0,5)))):
        with pytest.raises(ContractError):policy.capture(path,save(**changes))
    with pytest.raises(ContractError,match='hash'):policy.capture(path,'f'*64)


def test_runtime_gain_requires_real_matching_register_readback():
    io=object.__new__(measure.MeasurementIO)
    class Board:
        cursor=0;commands=[]
        def command(self,text):self.commands.append(text)
        def wait_for(self,*args,**kwargs):return 'AEC_REF_GAIN db=15 reg45=0x15'
    board=Board();assert io.configure(board,15)['gain_db']==15
    assert board.commands[0]=='audio 0' and 'aec-ref-gain 15' in board.commands
    with pytest.raises(ContractError,match='readback'):io.configure(board,12)


def test_delay_proof_must_be_measured_available_and_bound_to_same_elf(tmp_path,monkeypatch):
    path=tmp_path/'delay.json';plan=dict(elf_sha256='a'*64,fixtures=[dict(path='known.wav')])
    row=dict(kind='p4-aec-delay',acquisition_passed=True,restore=dict(clean=True),elf_sha256='a'*64,
        policy=policy.POLICY,dac={},near={},score=dict(available=True,measured_delay_samples=1024))
    monkeypatch.setattr(measure,'load_capture',lambda row:np.zeros((8192,5)))
    monkeypatch.setattr(measure,'delay_score',lambda *args:dict(available=True,measured_delay_samples=1024))
    monkeypatch.setattr(policy.alignment,'load_nearend_source',lambda *args:np.zeros(8192))
    path.write_text(json.dumps(row));assert measure.delay_proof(path,plan)==1024
    row['elf_sha256']='b'*64;path.write_text(json.dumps(row))
    with pytest.raises(ContractError,match='same-ELF'):measure.delay_proof(path,plan)
    row['elf_sha256']='a'*64;row['score']['measured_delay_samples']=999;path.write_text(json.dumps(row))
    with pytest.raises(ContractError,match='altered'):measure.delay_proof(path,plan)


@pytest.fixture(params=['p4xc','echoear'])
def scoring_grid(tmp_path,monkeypatch,request):
    plan_path=tmp_path/'plan.json';plan_path.write_text('{}')
    delay_path=tmp_path/'delay.json';delay_path.write_text('{}')
    root=tmp_path/'grid';plan=dict(elf_sha256='a'*64,fixtures=[dict(id=x,path='known.wav') for x in ('long','france','italy','speech')],s3_reference=policy.S3_REFERENCE)
    monkeypatch.setattr(measure,'validate_plan',lambda *args:None)
    monkeypatch.setattr(measure,'delay_input',lambda path,plan:dict(kind='measured',delay_samples=1024,measured_pass=True,
        record_file=str(Path(path).resolve()),record_sha256=measure.digest(path),qualification=False))
    monkeypatch.setattr(measure,'load_capture',lambda row:np.zeros((8192,5),dtype='<i2'))
    monkeypatch.setattr(measure,'clip_evidence',lambda *args:dict(passed=True))
    def echo(*args):return dict(available=True,median_erle_db=24.,false_user_turns=0,residual=dict(residual_dbfs=-55.))
    def prompt(*args):return dict(passed=True,accuracy=.9,false_user_turns=0)
    monkeypatch.setattr(measure,'echo_score',echo);monkeypatch.setattr(measure,'prompt_score',prompt)
    for gain in policy.GAINS:
        for part,indices in enumerate(policy.GAIN_PARTS):
            directory=root/f'gain-{gain:02d}'/f'part-{part}'/'attempt-01';directory.mkdir(parents=True)
            cases=[]
            for index in indices:
                before=perf_record(request.param);before['aec']['reference_health']['gain_db']=gain
                original=dict(capture={},events=[],complete=True,near_playback={},bot_started_ns=0,bot_stopped_ns=1,
                    before=before,after=copy.deepcopy(before))
                case=dict(id=f'echo-{index+1:02d}' if index<10 else 'prompt-'+plan['fixtures'][index-9]['id'],
                    original=original,clips=dict(passed=True),score=echo() if index<10 else prompt())
                cases.append(case)
            row=dict(status='acquired',acquisition_passed=True,restore=dict(clean=True),gain_db=gain,part=part,settings=dict(gain_db=gain),
                elf_sha256=plan['elf_sha256'],policy=policy.POLICY,plan_sha256=measure.digest(plan_path),delay_sha256=measure.digest(delay_path),
                identity=IDENTITY,quiet={},cases=cases,delay_file=str(delay_path),delay_provenance=measure.delay_input(delay_path,plan))
            (directory/'result.json').write_text(json.dumps(row))
    return plan,plan_path,delay_path,root


def test_complete_grid_is_rederived_and_report_gain_never_selects(scoring_grid,monkeypatch):
    plan,path,delay,root=scoring_grid
    selected=measure.gain_selection(plan,path,delay,root)
    assert selected['passed'] and selected['selected_gain_db']==0 and len(selected['sources'])==48
    monkeypatch.setattr(measure,'select_gain',Mock(side_effect=AssertionError('report must not select')))
    report=measure.gain_selection(plan,path,delay,root,12)
    assert report['report_only'] and report['median_erle_db']==24 and len(report['residuals'])==10 and len(report['sources'])==4
    report_path=path.parent/'report.json';report_path.write_text(json.dumps(report))
    monkeypatch.setattr(measure,'gain_selection',lambda *args:report)
    assert measure.checked_report(report_path)['available']
    changed=dict(report,median_erle_db=99);report_path.write_text(json.dumps(changed))
    with pytest.raises(ContractError,match='authentic'):measure.checked_report(report_path)


@pytest.mark.parametrize('change',['missing','reorder','score','gain','elf','delay','policy','plan','identity','settings','clips','perf'])
def test_grid_cannot_select_from_incomplete_reordered_or_changed_originals(scoring_grid,change):
    plan,path,delay,root=scoring_grid
    source=root/'gain-03/part-0/attempt-01/result.json';row=json.loads(source.read_text())
    if change=='missing':source.unlink()
    else:
        if change=='reorder':row['cases'].reverse()
        elif change=='score':row['cases'][0]['score']['median_erle_db']=99
        elif change=='gain':row['gain_db']=0
        elif change=='elf':row['elf_sha256']='b'*64
        elif change=='delay':row['delay_sha256']='b'*64
        elif change=='policy':row['policy']={}
        elif change=='plan':row['plan_sha256']='b'*64
        elif change=='identity':row['identity']={}
        elif change=='settings':row['settings']['gain_db']=0
        elif change=='clips':row['cases'][0]['clips']['passed']=False
        elif change=='perf':row['cases'][0]['original']['before']['aec']['reference_health']['gain_db']=0
        source.write_text(json.dumps(row))
    with pytest.raises(ContractError):measure.gain_selection(plan,path,delay,root)


def test_plan_refuses_changed_bundle_policy_helpers_and_fixtures(tmp_path,monkeypatch):
    image=dict(profile='aec-normal',aec_enabled=True,elf_sha256='a'*64)
    bundle=tmp_path/'bundle';bundle.mkdir();(bundle/'manifest.json').write_text('{}')
    monkeypatch.setattr(policy,'verify_bundle',lambda *args:(image,{}))
    monkeypatch.setattr(policy,'fixtures',lambda:[dict(id='frozen',sha256='b'*64)])
    plan=policy.make_plan(bundle);assert policy.validate_plan(plan)==plan
    assert 'echoear_aec_scoring.py' in plan['source_sha256']
    for key in ('policy','source_sha256','fixtures','manifest_sha256','elf_sha256'):
        mutant=json.loads(json.dumps(plan));mutant[key]={}
        with pytest.raises(ContractError):policy.validate_plan(mutant)
    (bundle/'manifest.json').write_text('{"changed":true}')
    with pytest.raises(ContractError):policy.validate_plan(plan)


@pytest.mark.parametrize('bounds,inside,after',[( (1100000,1120000),True,False),((1390000,1400000),False,False),
    ((1400000,1420000),False,True),((900000,950000),False,False),((1900000,2100000),False,False)])
def test_probe_uses_source_active_onset_and_conservative_clock_intervals(tmp_path,monkeypatch,bounds,inside,after):
    import echoear_aec_timing as timing
    source=np.r_[np.zeros(640),np.ones(4096)]
    pcm=np.zeros((8192,5),dtype='<i2');blocks=np.array([[0,8192,*bounds,0]])
    monkeypatch.setattr(policy.alignment,'load_nearend_source',lambda *args:source)
    monkeypatch.setattr(policy.alignment,'source_match',lambda *args:dict(available=True,lag_samples=100))
    monkeypatch.setattr(measure,'capture',lambda *args:(pcm,blocks))
    monkeypatch.setattr(measure,'load_capture',lambda *args:pcm)
    monkeypatch.setattr(measure,'clip_evidence',lambda *args:dict(passed=True))
    monkeypatch.setattr(measure,'prompt_score',lambda *args:dict(passed=True,available=True,intentional_user_turn_witnessed=True,playback_overlap=True,false_user_turns=0))
    anchors=[dict(coordinate=v,lower_s=v/1e6,upper_s=v/1e6) for v in (900000,950000,1000000,1100000,1120000,1390000,1400000,1420000,1900000,2000000)]
    path=tmp_path/'anchors.json';path.write_text(json.dumps(anchors))
    original=dict(capture=dict(path='capture.npz',sha256='a'*64,anchors=str(path)),
        guard_lines=['AEC_GUARD onset_upper_us=1000000 source_deadline_us=1400000 guard_ms=400 rearm_ms=500 previous_end_us=1'],
        events=[],near_playback={},bot_started_ns=0,bot_stopped_ns=1,before={},after={})
    observed=[];real=timing.sample_interval
    def sample(at,*args):observed.append(at);return real(at,*args)
    monkeypatch.setattr(timing,'sample_interval',sample)
    result=measure.probe_score(original,dict(path='known.wav'),True)
    assert observed==[740] and result['source_active_offset_samples']==640
    assert result['inside_guard'] is inside and result['after_rearm'] is after


def test_guard_rederives_turn_events_instead_of_trusting_cached_pass(monkeypatch):
    from test_p4_aec_policy import guard_input
    turns,lines,inside,after=guard_input()
    monkeypatch.setattr(measure,'probe_score',lambda original,*args:original)
    def actual(events,*args,**kwargs):
        assert kwargs['calibration'] is True
        raise ContractError('original contains self-interruption')
    monkeypatch.setattr(measure.smoke,'turn_checks',actual)
    for turn in turns:turn['events']=[]
    row=dict(candidate_index=0,long_turns=turns,onsets=lines,inside_original=inside,after_original=after)
    assert not measure.score_guard_row(row,dict(fixtures=[{},{}]))['passed']
    row['long_turns'][0].pop('events')
    with pytest.raises(ContractError):measure.score_guard_row(row,dict(fixtures=[{},{}]))


def test_checked_dump_pcm_and_clock_anchors_are_rederived_not_just_individually_hashed(tmp_path):
    import base64,hashlib
    pcm=np.zeros((8192,5),dtype='<i2');pcm[:,0]=np.arange(8192,dtype='<i2')
    data=pcm.tobytes();records=[f'AEC_PCM {at//2} '+base64.b64encode(data[at:at+384]).decode() for at in range(0,len(data),384)]
    lines=['AEC_DUMP BEGIN schema=2 frames=8192 channels=5 rate=16000 order=mic1,mic2,mic3,mic4,processed adapter_delay=512 measured_delay=unknown truncated=0 ns=0 agc=0',
        'AEC_TIME start=0 end=8192 lower_us=100 upper_us=600000 gap=0',*records,
        f'AEC_DUMP CHECK lines={len(records)} sha256='+hashlib.sha256('\n'.join(records).encode()).hexdigest(),
        'AEC_DUMP END frames=8192 timing_blocks=1']
    dump=tmp_path/'capture.dump.log';dump.write_text('\n'.join(lines)+'\n')
    anchors=tmp_path/'anchors.json';anchors.write_text(json.dumps([dict(coordinate=v,lower_s=v/1e6,upper_s=v/1e6) for v in (0,700000)]))
    path=tmp_path/'capture.npz';np.savez(path,pcm=pcm,schema=2,rate=16000,
        channels=['mic1','mic2','mic3','mic4','processed'],timing=np.array([[0,8192,100,600000,0]]))
    row=dict(path=str(path),sha256=measure.digest(path),dump=str(dump),dump_sha256=measure.digest(dump),
        anchors=str(anchors),anchors_sha256=measure.digest(anchors))
    assert np.array_equal(measure.load_capture(row),pcm)
    with np.load(path,allow_pickle=False) as loaded:changed={key:loaded[key].copy() for key in loaded.files}
    changed['pcm'][0,0]=1234;np.savez(path,**changed);row['sha256']=measure.digest(path)
    with pytest.raises(ContractError,match='differs from original'):measure.load_capture(row)
    changed['pcm']=pcm;np.savez(path,**changed);row['sha256']=measure.digest(path)
    anchors.write_text(json.dumps([dict(coordinate=v,lower_s=v/1e6,upper_s=v/1e6) for v in (200,700000)]));row['anchors_sha256']=measure.digest(anchors)
    with pytest.raises(ValueError,match='precedes first'):measure.load_capture(row)
    row['anchors_sha256']='b'*64
    with pytest.raises(ContractError,match='anchors changed'):measure.load_capture(row)


@pytest.mark.parametrize('autostart',[False,True])
def test_demo_image_measurement_holds_autostart_before_controlled_manual_session(monkeypatch,autostart):
    calls=[]
    monkeypatch.setattr(measure.VoiceIO,'prepare_manual',lambda self,board:calls.append('prepare'))
    io=object.__new__(measure.MeasurementIO);io.manifest=dict(autostart=autostart)
    board=SimpleNamespace(command=lambda *args:calls.append(args))
    io.prepare_manual(board)
    assert calls==([('demo hold',45),'prepare'] if autostart else ['prepare'])


@pytest.mark.parametrize('failure',['room','bot','jabra','identity-file'])
def test_untouched_host_errors_are_recorded_unscored_and_retryable(held_rig,monkeypatch,failure):
    import alsa
    args,plan,state,IO=held_rig;state.fail=failure
    if failure=='jabra':monkeypatch.setattr(alsa,'resolve_jabra',Mock(side_effect=RuntimeError('Jabra missing')))
    if failure=='identity-file':args.bot_identity.unlink()
    result=measure.held(args,plan,Mock(),io_factory=IO)
    assert result['status']=='preflight-error' and result['device_untouched'] and result['retryable']
    assert not result['score_produced'] and not result['acquisition_passed']
    assert not {'touch','flash','restore'}&set(state.calls)
    assert json.loads((args.out/'result.json').read_text())==result
    assert not measure.attempt_rows(args.out.parent,mark_unscored=True)[0][2]


def test_body_failure_is_primary_and_cleanup_error_is_also_retained(held_rig):
    args,plan,state,IO=held_rig;state.fail='both'
    def body(*args):raise ValueError('original failure')
    result=measure.held(args,plan,body,io_factory=IO)
    assert result['error']=='ValueError: original failure'
    assert result['cleanup_errors']==['RuntimeError: cleanup failed']
    assert result['status']=='acquisition-error' and not result['score_produced']
    assert 'restore' in state.calls and state.calls[-1]=='signals-released'


@pytest.mark.parametrize('name',['run','attempt-1','attempt-001','attempt-xx','attempt-100'])
def test_attempt_name_must_have_exactly_two_digits_before_any_device_action(held_rig,name):
    args,plan,state,IO=held_rig;args.out=args.out.with_name(name)
    with pytest.raises(ContractError,match='attempt-NN'):measure.held(args,plan,Mock(),io_factory=IO)
    assert 'room' not in state.calls and 'touch' not in state.calls and not args.out.exists()


def test_result_or_marker_with_published_score_cannot_be_retried(held_rig):
    args,plan,state,IO=held_rig;prior=args.out.parent/'attempt-00';prior.mkdir(parents=True)
    (prior/'result.json').write_text(json.dumps(dict(status='acquisition-error',score=dict(passed=True))))
    with pytest.raises(ContractError,match='scored originals'):measure.held(args,plan,Mock(),io_factory=IO)


def test_incomplete_marker_without_any_scored_result_is_unscored(held_rig):
    args,plan,state,IO=held_rig;prior=args.out.parent/'attempt-00';prior.mkdir(parents=True)
    (prior/'scored-original.json').write_text('{')
    result=measure.held(args,plan,lambda *args:{},io_factory=IO)
    assert result['acquisition_passed'] and (prior/'unscored-attempt.json').exists()


@pytest.mark.parametrize('status,available,expected',[('preflight-error',False,3),('preflight-noisy',False,2),
    ('acquisition-error',False,1),('acquired',False,1),('acquired',True,0)])
def test_measurement_cli_has_distinct_untouched_error_exit(tmp_path,monkeypatch,status,available,expected):
    plan=tmp_path/'plan.json';plan.write_text(json.dumps(dict(bundle='bundle')))
    monkeypatch.setattr(measure,'validate_plan',lambda *args:None)
    monkeypatch.setattr(measure,'acquire_delay',lambda *args:dict(status=status,score=dict(available=available),acquisition_passed=status=='acquired'))
    argv=['delay','--plan',str(plan),'--out',str(tmp_path/'attempt-01'),'--bot-dir','bot','--bot-identity','identity',
        '--restore-record','restore','--restore-record-sha256','f'*64]
    assert measure.main(argv)==expected


@pytest.fixture
def reply_rig(tmp_path,monkeypatch):
    import echoear_aec_bench
    clock=SimpleNamespace(value=100.,stop=105.6,completion=105.7,interrupted=False,late_guard=False)
    monkeypatch.setattr(measure,'time',SimpleNamespace(monotonic_ns=lambda:int(clock.value*1e9),sleep=lambda s:setattr(clock,'value',clock.value+s)))
    monkeypatch.setattr(echoear_aec_bench,'clock_anchor',lambda board:dict(coordinate=int(clock.value*1e6),lower_s=clock.value,upper_s=clock.value))
    def frame(name,at,active=False):return dict(kind='frame',direction='DOWNSTREAM',frame=name,bot_active=active,monotonic_ns=int(at*1e9))
    def events():
        rows=[frame('UserStartedSpeakingFrame',100.2),dict(kind='aggregator-user-turn-stopped',text='Count to twenty slowly',monotonic_ns=int(100.4e9)),
            frame('BotStartedSpeakingFrame',100.6),frame('BotStoppedSpeakingFrame',clock.stop),
            dict(kind='aggregator-assistant-turn-stopped',text='Some off-script persona response',monotonic_ns=int(clock.completion*1e9))]
        if clock.interrupted:rows += [frame('UserStartedSpeakingFrame',clock.stop-.1,True),frame('InterruptionFrame',clock.stop-.05,True)]
        return sorted([e for e in rows if e['monotonic_ns']<=clock.value*1e9],key=lambda e:e['monotonic_ns'])
    board=SimpleNamespace(cursor=0,lines_since=lambda cursor:[(1,106. if clock.late_guard else 100.6,
        'AEC_GUARD onset_upper_us=100600000 source_deadline_us=101000000 guard_ms=400 rearm_ms=2000 previous_end_us=-1')])
    io=object.__new__(measure.MeasurementIO);io.clock=lambda:clock.value;io.snapshot=lambda b:{}
    def start(b):io.capture_started=clock.value;io.anchors=[]
    io.start_capture=start;io.stop_capture=lambda *args:dict(path='modeled-no-device.npz')
    io.pause=lambda b,s:setattr(clock,'value',clock.value+s)
    io.play=Mock(side_effect=lambda b,path:dict(request_monotonic_s=clock.value))
    io.poll=lambda b:None;io.events=lambda:(None,events())
    io.wait=Mock(side_effect=AssertionError('must not wait for a nonexistent second user turn'))
    return clock,io,board,tmp_path/'reply'


@pytest.mark.parametrize('probe,late_guard',[(False,False),(True,False),(True,True)])
def test_reply_retains_unheard_prompt_and_late_inside_injection_without_impossible_drain(reply_rig,probe,late_guard):
    clock,io,board,folder=reply_rig;clock.late_guard=late_guard
    row=io.reply(board,folder,dict(path='long.wav'),dict(path='near.wav'),probe=probe)
    assert row['complete'] and not io.wait.called
    if late_guard:assert row['near_playback'] is None
    else:assert row['near_playback'] is not None


@pytest.mark.parametrize('kind',['gain','guard-after-rearm'])
@pytest.mark.parametrize('completion',[101.7,104.7])
def test_missed_playback_is_failed_and_consumed_in_both_scored_paths(reply_rig,held_rig,monkeypatch,kind,completion):
    from test_p4_aec_policy import points,guard_input
    clock,io,board,folder=reply_rig;clock.stop=101.6;clock.completion=completion;clock.interrupted=True
    row=io.reply(board,folder,dict(path='long.wav'),dict(path='near.wav'))
    assert row['injection_status']=='missed-bot-playback' and row['near_playback'] is None and row['complete']
    assert io.play.call_count==1 and not io.wait.called # Only the trigger, no injected near-end audio.
    prompt=policy.prompt_score(dict(expected='What is the capital of France?'),row['events'],
        row['near_playback'] or dict(request_monotonic_s=0),row['bot_started_ns'],row['bot_stopped_ns'])
    assert not prompt['passed']
    if kind=='gain':
        grid=points()
        for point in grid:
            point['prompts'][0].update(prompt)
        assert not policy.select_gain(grid)['passed']
        payload=dict(cases=[dict(original=row,score=prompt)])
    else:
        turns,lines,inside,after=guard_input()
        after['detected']=prompt['available'] and prompt['intentional_user_turn_witnessed'] and prompt['playback_overlap']
        score=policy.guard_score(0,turns,lines,inside,after)
        assert not score['passed']
        payload=dict(score=score,after_original=row)
    args,plan,state,IO=held_rig
    result=measure.held(args,plan,lambda *args:payload,io_factory=IO)
    assert result['acquisition_passed'] and result['score_produced'] and not result['retryable']
    assert measure.original_part(args.out.parent)[1]==result
    args.out=args.out.with_name('attempt-02')
    with pytest.raises(ContractError,match='scored originals'):measure.held(args,plan,Mock(),io_factory=IO)


@pytest.mark.parametrize('failure',['cleanup','finish','unclean','wrong-action'])
def test_completed_scores_survive_finalization_failure_and_cannot_be_retried(held_rig,failure):
    args,plan,state,IO=held_rig;args.action='gain';state.fail=failure
    if failure=='unclean':state.clean=False
    if failure=='wrong-action':state.action='keep-tested'
    payload=dict(cases=[dict(id='prompt-france',score=dict(passed=False,accuracy=0.))])
    observed=[]
    def checkpoint():
        saved=json.loads((args.out/'result.json').read_text())
        assert saved['cases']==payload['cases'] and saved['score_produced'] and not saved['retryable']
        assert saved['acquisition_passed'] is False and 'signals-released' not in state.calls
        assert json.loads((args.out/'post-body-payload.json').read_text())==payload
        observed.append(saved)
    state.cleanup_observer=checkpoint;state.restore_observer=checkpoint
    result=measure.held(args,plan,lambda *args:payload,io_factory=IO)
    assert len(observed)==2
    assert result['cases']==payload['cases'] and result['score_produced'] and not result['retryable']
    assert not result['acquisition_passed'] and result['status']=='acquisition-error'
    assert result['post_body_payload_sha256']==measure.digest(args.out/'post-body-payload.json')
    if failure=='cleanup':assert result['cleanup_errors']==['RuntimeError: cleanup failed']
    assert state.calls.index('restore')<state.calls.index('signals-released')
    if failure in ('cleanup','finish'):
        assert state.calls.index('finish')<state.calls.index('cleanup')<state.calls.index('restore')
    with pytest.raises(ContractError,match='clean acquired'):measure.original_part(args.out.parent)
    args.out=args.out.with_name('attempt-02')
    with pytest.raises(ContractError,match='scored originals'):measure.held(args,plan,Mock(),io_factory=IO)


def test_payload_alone_consumes_original_if_result_is_lost(held_rig):
    args,plan,state,IO=held_rig;state.fail='cleanup'
    result=measure.held(args,plan,lambda *args:dict(score=dict(passed=False)),io_factory=IO)
    (args.out/'result.json').unlink()
    assert measure.attempt_rows(args.out.parent)[0][2]
    with pytest.raises(ContractError,match='clean acquired'):measure.original_part(args.out.parent)
    args.out=args.out.with_name('attempt-02')
    with pytest.raises(ContractError,match='scored originals'):measure.held(args,plan,Mock(),io_factory=IO)


def test_selection_keeps_hashes_of_earlier_unscored_attempts(scoring_grid):
    plan,path,delay,root=scoring_grid
    parent=root/'gain-00/part-0';prior=parent/'attempt-00';prior.mkdir()
    measure.attempt_rows(parent,mark_unscored=True)
    selected=measure.gain_selection(plan,path,delay,root)
    assert selected['passed'] and str((prior/'unscored-attempt.json').resolve()) in selected['sources']


@pytest.fixture
def delay_decision_rig(tmp_path,monkeypatch):
    decision=tmp_path/'approved-decision.md';decision.write_text('Approved: use 1024; source remains unavailable.')
    source=tmp_path/'source-result.json';source.write_text(json.dumps(dict(kind='p4-aec-delay',board='p4xc',
        acquisition_passed=True,restore=dict(clean=True),elf_sha256='a'*64,policy=policy.POLICY,score=dict(available=False))))
    monkeypatch.setattr(measure,'DELAY_DECISION_PATH',decision.resolve());monkeypatch.setattr(measure,'DELAY_DECISION_SHA256',measure.digest(decision))
    monkeypatch.setattr(measure,'DELAY_SOURCE_PATH',source.resolve());monkeypatch.setattr(measure,'DELAY_SOURCE_SHA256',measure.digest(source))
    path=tmp_path/'delay-decision.json';record=measure.delay_decision(decision,source);path.write_text(json.dumps(record))
    return decision,source,path,record,dict(elf_sha256='a'*64)


def test_explicit_delay_decision_is_hash_bound_and_never_a_measured_pass(delay_decision_rig):
    decision,source,path,record,plan=delay_decision_rig
    proof=measure.delay_input(path,plan)
    assert proof['kind']=='decision-backed' and proof['delay_samples']==1024 and proof['measured_pass'] is False
    assert proof['decision_sha256']==measure.digest(decision) and proof['source_attempt_sha256']==measure.digest(source)
    assert measure.delay_proof(path,plan)==1024 and json.loads(source.read_text())['score']['available'] is False


@pytest.mark.parametrize('change',['decision-file','decision-hash','source-file','source-hash','samples','boolean','float','elf','policy','measured-pass','kind'])
def test_delay_decision_refuses_forged_binding_or_measurement_claim(delay_decision_rig,change):
    decision,source,path,record,plan=delay_decision_rig
    if change=='decision-file':record['decision_file']=str(source)
    elif change=='decision-hash':record['decision_sha256']='b'*64
    elif change=='source-file':record['source_attempt_file']=str(decision)
    elif change=='source-hash':record['source_attempt_sha256']='b'*64
    elif change=='samples':record['delay_samples']=1023
    elif change=='boolean':record['delay_samples']=True
    elif change=='float':record['delay_samples']=1024.
    elif change=='elf':plan['elf_sha256']='b'*64
    elif change=='policy':record['policy']={}
    elif change=='measured-pass':record['measured_pass']=True
    else:record['kind']='p4-aec-delay'
    path.write_text(json.dumps(record))
    with pytest.raises((ContractError,KeyError)):measure.delay_input(path,plan)


@pytest.mark.parametrize('changed',['decision','source'])
def test_changed_approved_file_cannot_be_reauthorized_by_rehashing_record(delay_decision_rig,changed):
    decision,source,path,record,plan=delay_decision_rig
    target=decision if changed=='decision' else source;target.write_text(target.read_text()+' ')
    record['decision_sha256' if changed=='decision' else 'source_attempt_sha256']=measure.digest(target)
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError,match='path/hash'):measure.delay_input(path,plan)


def test_delay_decision_cli_needs_no_plan_or_hardware(delay_decision_rig,tmp_path):
    decision,source,path,record,plan=delay_decision_rig;out=tmp_path/'explicit-record.json'
    assert measure.main(['delay-decision','--decision',str(decision),'--source-attempt',str(source),'--out',str(out)])==0
    assert json.loads(out.read_text())==record
    with pytest.raises(FileExistsError):measure.main(['delay-decision','--decision',str(decision),'--source-attempt',str(source),'--out',str(out)])


def test_corrected_delay_rescores_raw_originals_without_retaking_or_changing_cached_scores(scoring_grid,monkeypatch):
    plan,path,delay,root=scoring_grid
    corrected=path.parent/'corrected-delay.json';corrected.write_text('{"corrected":true}')
    original_input=measure.delay_input
    def proof(source,plan):
        result=original_input(source,plan)
        if Path(source)==corrected:result.update(delay_samples=512)
        return result
    monkeypatch.setattr(measure,'delay_input',proof)
    def echo(pcm,quiet,samples,*args):
        return dict(available=True,median_erle_db=24. if samples==1024 else 21.,false_user_turns=0,residual=dict(residual_dbfs=-55.))
    monkeypatch.setattr(measure,'echo_score',echo)
    before={str(p):measure.digest(p) for p in root.rglob('*.json')}
    result=measure.gain_selection(plan,path,corrected,root)
    assert result['passed'] and result['median_erle_db']==21 and result['delay_rescored']
    assert result['delay_provenance']['delay_samples']==512
    assert {p['delay_samples'] for p in result['acquisition_delays'].values()}=={1024}
    assert before=={str(p):measure.digest(p) for p in root.rglob('*.json')}


def test_decision_backed_label_is_in_selection_and_original_delay_must_stay_unchanged(scoring_grid,monkeypatch):
    plan,path,delay,root=scoring_grid
    old=measure.delay_input
    def proof(source,plan):return dict(old(source,plan),kind='decision-backed',measured_pass=False)
    monkeypatch.setattr(measure,'delay_input',proof)
    for source in root.rglob('result.json'):
        row=json.loads(source.read_text());row['delay_provenance']=proof(delay,plan);source.write_text(json.dumps(row))
    result=measure.gain_selection(plan,path,delay,root)
    assert result['delay_provenance']['kind']=='decision-backed' and not result['delay_provenance']['measured_pass']
    assert all(not v['measured_pass'] for v in result['acquisition_delays'].values())
    delay.write_text('{"changed":true}')
    with pytest.raises(ContractError,match='delay record changed'):measure.gain_selection(plan,path,delay,root)


@pytest.mark.parametrize('decision_backed',[False,True])
def test_each_gain_result_carries_explicit_delay_provenance(scoring_grid,monkeypatch,decision_backed):
    plan,path,delay,root=scoring_grid
    original_input=measure.delay_input
    if decision_backed:
        monkeypatch.setattr(measure,'delay_input',lambda source,plan:dict(original_input(source,plan),kind='decision-backed',measured_pass=False))
    source=root/'gain-00/part-0/attempt-01/result.json';row=json.loads(source.read_text());cases=iter(row['cases'])
    io=SimpleNamespace(configure=lambda *args:dict(gain_db=0),quiet=lambda *args:{},begin=lambda *args:None,
        reply=lambda *args:next(cases)['original'])
    monkeypatch.setattr(measure,'held',lambda args,plan,body,**kwargs:body(io,None))
    args=SimpleNamespace(delay=delay,gain_db=0,part=0,out=root/'unused')
    result=measure.acquire_gain(args,plan)
    assert result['delay_provenance']['kind']==('decision-backed' if decision_backed else 'measured')
    assert result['delay_provenance']['measured_pass'] is (not decision_backed)
    assert len(result['cases'])==3



def test_gain_preflight_error_also_labels_its_decision_backed_delay(held_rig,delay_decision_rig):
    args,plan,state,IO=held_rig;decision,source,record_path,record,decision_plan=delay_decision_rig
    args.action='gain';args.delay=record_path;args.gain_db=0;args.part=0;state.fail='room'
    result=measure.acquire_gain(args,plan)
    assert result['status']=='preflight-error' and result['device_untouched'] and not result['score_produced']
    assert result['delay_provenance']['kind']=='decision-backed' and result['delay_provenance']['measured_pass'] is False
    assert json.loads((args.out/'result.json').read_text())==result
