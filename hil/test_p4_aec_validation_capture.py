"""Offline provenance, consumed-trial resume and inherited R7 safety tests."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

import p4_aec_validation_capture as c
import p4_aec_validation as v
from test_p4_aec_measure import held_rig
from test_p4_aec_validation import prompt_rig
from test_wire_contract import IDENTITY


@pytest.fixture
def rig(held_rig,monkeypatch):
    args,base,state,BaseIO=held_rig
    import alsa,wave
    previous_room=alsa.record_room_baseline
    def room(folder,arm):
        result=previous_room(folder,arm);folder.mkdir()
        wav=folder/'room.wav'
        with wave.open(str(wav),'wb') as stream:
            stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(bytes(96000))
        result.update(baseline_dbfs=-120,limit_dbfs=-50,recording_seconds=3,scored_start_seconds=.2,wav=str(wav))
        c.policy.write_new(folder/'result.json',result);(folder/'mixer.log').write_text('fake mixer readback')
        return result
    monkeypatch.setattr(alsa,'record_room_baseline',room)
    args.restore_record_sha256=c.RESTORE_SHA
    root=args.out.parents[1]/'validation';root.mkdir()
    plan=dict(root=str(root),bundle=str(args.bundle),elf_sha256=c.ELFS['p4xc'],schedule=v.schedule(),
        fixtures=[dict(id='long',path='long.wav'),dict(id='france',path='france.wav'),dict(id='speech',path='speech.wav')],
        identity=IDENTITY,restore_record=str(args.restore_record),restore_record_sha256=c.RESTORE_SHA)
    args.plan=root/'plan.json';args.plan.write_text(json.dumps(plan))
    args.part='delay-0';args.out=root/args.part/'attempt-01'
    monkeypatch.setattr(c,'validate_plan',lambda plan:plan)
    class IO(BaseIO):
        def quiet(self,board,path):
            state.calls.append('quiet')
            assert (path.parent/'trial-start.json').is_file()
            if state.fail=='quiet':raise RuntimeError('quiet failed')
            return dict(path=str(path))
        def begin(self,*args):
            state.calls.append('begin')
            if state.fail=='begin':raise RuntimeError('begin failed')
        def original(self,board,folder,*unused):
            state.calls.append('stimulus')
            assert (folder.parent/'trial-start.json').is_file()
            if state.fail=='body':raise RuntimeError('capture failed')
            if state.fail=='abort':raise KeyboardInterrupt()
            folder.mkdir();original=dict(fake=True)
            c.policy.write_new(folder/'original.json',original)
            return original
        delay=reply=original
    return args,plan,state,IO


def next_part(args,plan,part,attempt=1):
    args.part=part;args.out=Path(plan['root'])/part/f'attempt-{attempt:02d}'


def start_double(args,plan,IO,monkeypatch):
    for part in c.PARTS[:3]:
        next_part(args,plan,part);assert c.acquire(args,plan,io_factory=IO)['acquisition_passed']
    monkeypatch.setattr(c,'observations',lambda plan,part:[dict(available=True,passed=True,
        measured_delay_samples=1024,correlation=.9,separation=.1)])
    next_part(args,plan,'double-talk-0')


def test_delay_restores_and_seals_consumed_original(rig):
    args,plan,state,IO=rig;r=c.acquire(args,plan,io_factory=IO)
    assert r['acquisition_passed'] and r['restore']['action']=='p4-product-of-record'
    assert state.calls.index('cleanup')<state.calls.index('restore')<state.calls.index('R7-exit')
    assert state.calls[-1]=='signals-released'
    assert len(c.consumed(plan,'delay-0'))==1
    with pytest.raises(ValueError,match='fully consumed'):c.acquire(args,plan,io_factory=IO)


@pytest.mark.parametrize('failure',['body','abort'])
def test_started_delay_failure_consumes_repetition_even_without_payload(rig,failure):
    args,plan,state,IO=rig;state.fail=failure;r=c.acquire(args,plan,io_factory=IO)
    assert not r['acquisition_passed'] and state.calls[-1]=='signals-released'
    folder=next(iter(c.consumed(plan,'delay-0').values()))
    assert json.loads((folder/'trial.json').read_text())['status']=='unavailable'
    next_part(args,plan,'delay-0',2)
    with pytest.raises(ValueError,match='fully consumed'):c.acquire(args,plan,io_factory=IO)


@pytest.mark.parametrize('failure',['flash','pin','room','bot'])
def test_pre_stimulus_failure_can_retry_same_unstarted_repetition(rig,failure):
    args,plan,state,IO=rig;state.fail=failure
    assert not c.acquire(args,plan,io_factory=IO)['acquisition_passed']
    assert not c.consumed(plan,'delay-0')
    state.fail=None;next_part(args,plan,'delay-0',2)
    assert c.acquire(args,plan,io_factory=IO)['acquisition_passed']


def test_noisy_preflight_does_not_flash_or_consume(rig):
    args,plan,state,IO=rig;state.noise=True;r=c.acquire(args,plan,io_factory=IO)
    assert r['status']=='preflight-noisy' and r['device_untouched']
    assert 'flash' not in state.calls and not c.consumed(plan,'delay-0')


def test_earlier_parts_required_before_touch(rig):
    args,plan,state,IO=rig;next_part(args,plan,'delay-1')
    with pytest.raises(ValueError,match='earlier parts'):c.acquire(args,plan,io_factory=IO)
    assert not state.calls


def test_wrong_output_and_canonical_plan_refused_before_device(rig):
    args,plan,state,IO=rig;args.out=args.out.parent.parent/'foreign/attempt-01'
    with pytest.raises(ValueError,match='output'):c.acquire(args,plan,io_factory=IO)
    next_part(args,plan,'delay-0');args.plan=args.plan.with_name('foreign.json')
    with pytest.raises(ValueError,match='canonical'):c.acquire(args,plan,io_factory=IO)
    assert not state.calls


def test_duplicate_consumed_journal_is_rejected(rig):
    args,plan,state,IO=rig;c.acquire(args,plan,io_factory=IO)
    original=next(iter(c.consumed(plan,'delay-0').values()))
    duplicate=original.parent.parent/'attempt-02'/original.name;duplicate.mkdir(parents=True)
    (duplicate/'trial-start.json').write_bytes((original/'trial-start.json').read_bytes())
    with pytest.raises(ValueError,match='duplicate'):c.consumed(plan,'delay-0')


def test_foreign_or_reordered_journal_rejected(rig):
    args,plan,state,IO=rig
    spec=next(s for s in plan['schedule'] if s['part']=='double-talk-0' and s['id'].endswith('speech'))
    folder=Path(plan['root'])/'double-talk-0/attempt-01'/spec['id'];folder.mkdir(parents=True)
    c.policy.write_new(folder/'trial-start.json',dict(spec=spec,plan_digest=c.plan_digest(plan)))
    with pytest.raises(ValueError,match='prefix'):c.consumed(plan,'double-talk-0')


def test_three_interleaved_double_talk_sets_exactly_five_per_prompt(rig,monkeypatch):
    args,plan,state,IO=rig;start_double(args,plan,IO,monkeypatch)
    for part in c.PARTS[3:]:
        next_part(args,plan,part);r=c.acquire(args,plan,io_factory=IO)
        assert r['acquisition_passed'] and len(c.consumed(plan,part))==10
    assert state.calls.count('begin')==30 and state.calls.count('quiet')==30


@pytest.mark.parametrize('failure',['quiet','begin','body','finish'])
def test_double_talk_failure_only_resumes_unstarted_suffix(rig,monkeypatch,failure):
    args,plan,state,IO=rig;start_double(args,plan,IO,monkeypatch)
    state.fail=failure;r=c.acquire(args,plan,io_factory=IO)
    assert not r['acquisition_passed'] and len(c.consumed(plan,'double-talk-0'))==1
    state.fail=None;next_part(args,plan,'double-talk-0',2)
    assert c.acquire(args,plan,io_factory=IO)['acquisition_passed']
    assert len(c.consumed(plan,'double-talk-0'))==10
    assert not (args.out/'set-1-1-france').exists()


@pytest.mark.parametrize('failure',['cleanup','unclean','wrong-action'])
def test_post_body_failures_never_allow_replacement(rig,failure):
    args,plan,state,IO=rig;state.fail=failure
    if failure=='unclean':state.clean=False
    if failure=='wrong-action':state.action='factory'
    assert not c.acquire(args,plan,io_factory=IO)['acquisition_passed']
    assert len(c.consumed(plan,'delay-0'))==1 and state.calls[-1]=='signals-released'
    next_part(args,plan,'delay-0',2)
    with pytest.raises(ValueError,match='fully consumed'):c.acquire(args,plan,io_factory=IO)


def test_different_delay_blocks_double_talk_before_device(rig,monkeypatch):
    args,plan,state,IO=rig;start_double(args,plan,IO,monkeypatch);state.calls.clear()
    monkeypatch.setattr(c,'observations',lambda plan,part:[dict(available=True,passed=True,
        measured_delay_samples=1400,correlation=.8,separation=.2)])
    with pytest.raises(ValueError,match='measured-different'):c.acquire(args,plan,io_factory=IO)
    assert not state.calls


def test_seal_tampering_is_not_silently_reclassified_as_missing(rig):
    args,plan,state,IO=rig;state.fail='body';c.acquire(args,plan,io_factory=IO)
    folder=next(iter(c.consumed(plan,'delay-0').values()));(folder/'trial.json').write_text('{}')
    with pytest.raises(ValueError,match='sealed'):c.observations(plan,'delay-0')


def test_unsealed_crash_remains_consumed_unavailable(rig):
    args,plan,state,IO=rig;state.fail='body';c.acquire(args,plan,io_factory=IO)
    folder=next(iter(c.consumed(plan,'delay-0').values()));(folder/'trial-sealed.json').unlink()
    r=c.observations(plan,'delay-0')[0]
    assert r['available'] is False and 'consumed' in r['reason']


@pytest.fixture
def plan_rig(tmp_path,monkeypatch):
    import p4_step14
    bundle=tmp_path/'bundle';bundle.mkdir();(bundle/'manifest.json').write_text('{}')
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    restore=tmp_path/'restore.json';restore.write_text('{}')
    manifest=dict(profile='aec-normal',aec_enabled=True,autostart=False,elf_sha256=c.ELFS['p4xc'],boot_ref_gain_db=30,
        p4_aec_calibration=dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024))
    monkeypatch.setattr(c,'verify_bundle',lambda *args:(manifest,{}))
    monkeypatch.setattr(p4_step14,'restore_record',lambda path,sha:None if sha==c.RESTORE_SHA else 1/0)
    monkeypatch.setattr(c.policy,'fixtures',lambda:[])
    return bundle,tmp_path/'campaign',identity,restore,manifest


def test_plan_authenticates_image_restore_identity_sources_and_rules(plan_rig):
    *args,manifest=plan_rig;plan=c.make_plan(*args)
    assert c.validate_plan(plan)==plan and len(plan['schedule'])==33
    assert 'hil/p4_aec_measure.py' in plan['source_sha256']
    assert 'hil/p4_aec_validation_capture.py' in plan['source_sha256']
    assert 'hil/make_bundle.py' in plan['source_sha256']
    plan['rules']=copy.deepcopy(plan['rules']);plan['rules']['delay']['minimum_correlation']=.1
    with pytest.raises(ValueError,match='changed'):c.validate_plan(plan)


@pytest.mark.parametrize('field,value',[('profile','aec-demo'),('aec_enabled',False),('autostart',True),
    ('elf_sha256','0'*64),('boot_ref_gain_db',0)])
def test_plan_refuses_other_images(plan_rig,field,value):
    *args,manifest=plan_rig;manifest[field]=value
    with pytest.raises(ValueError,match='exact fix-2'):c.make_plan(*args)


def test_evidence_inside_worktree_rejected(plan_rig):
    bundle,root,identity,restore,manifest=plan_rig
    with pytest.raises(ValueError,match='outside'):c.make_plan(bundle,c.ROOT/'evidence',identity,restore)


def test_boot_settings_cannot_be_silently_tuned(plan_rig):
    *args,manifest=plan_rig;manifest['p4_aec_calibration']['guard_ms']=600
    with pytest.raises(ValueError,match='unchanged'):c.make_plan(*args)


def test_empty_report_discloses_all_missing_and_guard_partially_available_not_run(plan_rig):
    *args,manifest=plan_rig;plan=c.make_plan(*args);r=c.report(plan)
    assert r['delay']['outcome']=='unavailable' and r['complete'] is False
    assert r['guard']['outcome']=='partially-available-not-run'
    assert r['guard']==plan['guard']
    assert 'partially available but not run' in r['completion_reason']
    assert r['double_talk']['france']['unavailable']==15
    assert all(r['sets'][str(group)]['speech']['planned']==5 for group in (1,2,3))


def test_delay_waits_for_done_and_keeps_both_margins(monkeypatch,tmp_path):
    from types import SimpleNamespace
    calls=[]
    board=SimpleNamespace(cursor=0,command=lambda *args:calls.append(('command',args)),
        wait_for=lambda *args,**kwargs:calls.append(('done',args)),lines_since=lambda cursor:[])
    io=object.__new__(c.ValidationIO)
    io.start_capture=lambda board:calls.append(('start',))
    io.pause=lambda board,seconds:calls.append(('pause',seconds))
    io.stop_capture=lambda board,path:dict(path=str(path))
    io.play=lambda *args:dict(duration_valid=True)
    io.delay(board,tmp_path/'audio',dict(path='speech.wav'))
    assert calls.index(('pause',.75))<next(i for i,row in enumerate(calls) if row[0]=='command')
    assert next(i for i,row in enumerate(calls) if row[0]=='done')<calls.index(('pause',1.25))
    assert calls.count(('pause',1.25))==2
    assert all('aec-ref-gain ' not in str(row) and 'aec-guard-ms ' not in str(row) for row in calls)


def test_delay_replay_reads_checked_captures_instead_of_a_cached_score(rig,monkeypatch):
    import echoear_aec_alignment
    args,plan,state,IO=rig
    class RecordedIO(IO):
        def delay(self,board,folder,spec):
            folder.mkdir();row=dict(dac=dict(name='dac'),near=dict(name='near'))
            c.policy.write_new(folder/'original.json',row);return row
    c.acquire(args,plan,io_factory=RecordedIO)
    loaded=[]
    def load(record):loaded.append(record['name']);return np.zeros((64000,5),dtype='<i2')
    monkeypatch.setattr(c.measure,'load_capture',load)
    monkeypatch.setattr(echoear_aec_alignment,'load_nearend_source',lambda path:np.zeros(10000))
    monkeypatch.setattr(c.validation,'delay_trial',lambda *args:dict(available=True,passed=True,
        measured_delay_samples=1024,correlation=.9,separation=.2))
    row=c.observations(plan,'delay-0')[0]
    assert loaded==['dac','near'] and row['available'] is True and row['started'] is True
    assert 'reason' not in row


def test_original_tampering_is_rejected_even_if_capture_replay_is_mocked(rig,monkeypatch):
    args,plan,state,IO=rig;c.acquire(args,plan,io_factory=IO)
    folder=next(iter(c.consumed(plan,'delay-0').values()))
    (folder/'audio/original.json').write_text('{"changed": true}')
    with pytest.raises(ValueError,match='original changed'):c.observations(plan,'delay-0')


def test_gap_in_capture_replay_is_retained_unavailable_and_not_replaced(rig,monkeypatch):
    import echoear_aec_alignment
    args,plan,state,IO=rig
    class RecordedIO(IO):
        def delay(self,board,folder,spec):
            folder.mkdir();row=dict(dac={},near={});c.policy.write_new(folder/'original.json',row);return row
    c.acquire(args,plan,io_factory=RecordedIO)
    def bad_capture(row):raise ValueError('capture timing gap')
    monkeypatch.setattr(c.measure,'load_capture',bad_capture)
    monkeypatch.setattr(echoear_aec_alignment,'load_nearend_source',lambda path:np.zeros(10000))
    row=c.observations(plan,'delay-0')[0]
    assert not row['available'] and row['started'] and 'capture timing gap' in row['reason']
    next_part(args,plan,'delay-0',2)
    with pytest.raises(ValueError,match='fully consumed'):c.acquire(args,plan,io_factory=RecordedIO)


def test_live_cli_writes_different_delay_summary_before_stopping(plan_rig,monkeypatch,capsys):
    *args,manifest=plan_rig;plan=c.make_plan(*args);root=Path(plan['root']);root.mkdir()
    c.policy.write_new(root/'plan.json',plan)
    def fake_acquire(args,plan):args.out.mkdir(parents=True);return dict(acquisition_passed=True)
    monkeypatch.setattr(c,'acquire',fake_acquire)
    monkeypatch.setattr(c,'observations',lambda plan,part:[dict(available=True,passed=True,started=True,
        measured_delay_samples=1400,correlation=.8,separation=.2)])
    code=c.main(['capture','--plan',str(root/'plan.json'),'--part','delay-2','--attempt','1','--bot-dir','unused'])
    assert code==4
    assert json.loads((root/'delay-2/attempt-01/delay-summary.json').read_text())['outcome']=='measured-different'
    assert 'measured-different' in capsys.readouterr().out


@pytest.fixture
def numerical_delay_rig(rig,monkeypatch):
    """Exercise real delay math behind fake-device capture and R7 lifecycle."""
    import echoear_aec_alignment
    from test_p4_aec_validation import signals
    args,plan,state,IO=rig;dac,near,source=signals()
    monkeypatch.setattr(echoear_aec_alignment,'load_nearend_source',lambda path:source)
    monkeypatch.setattr(c.measure,'load_capture',lambda record:dac if record['name']=='dac' else near)
    count=[];original_score=c.validation.delay_trial
    def score(*args):count.append('score');return original_score(*args)
    monkeypatch.setattr(c.validation,'delay_trial',score)
    class NumericalIO(IO):
        def delay(self,board,folder,spec):
            folder.mkdir();row=dict(dac=dict(name='dac'),near=dict(name='near'))
            c.policy.write_new(folder/'original.json',row);return row
    return args,plan,state,NumericalIO,count


@pytest.mark.parametrize('failure',['none','cleanup','unclean','factory','missing-result'])
def test_v1_reviewer_four_failure_cases_cannot_confirm_delay(numerical_delay_rig,failure):
    args,plan,state,IO,count=numerical_delay_rig
    if failure=='cleanup':state.fail='cleanup'
    if failure=='unclean':state.clean=False
    if failure=='factory':state.action='factory'
    rows=[]
    for part in c.PARTS[:3]:
        next_part(args,plan,part);c.acquire(args,plan,io_factory=IO)
        if failure=='missing-result':(args.out/'result.json').unlink()
    result=c.report(plan)['delay'];rows=result['trials']
    assert result['consumed_repetitions']==3
    if failure=='none':
        assert result['outcome']=='measured-confirmed' and len(count)==3
        assert all(row['eligibility']['eligible'] for row in rows)
    else:
        assert result['outcome']=='unavailable' and result['passing_repetitions']==0 and count==[]
        assert all(not row['eligibility']['eligible'] and not row['available'] for row in rows)


def test_v3_finish_failure_after_original_is_ineligible_before_scoring(rig,monkeypatch):
    args,plan,state,IO=rig
    # Consume delay journals for part admission, then restore the real observation gate.
    real_observations=c.observations;start_double(args,plan,IO,monkeypatch)
    state.fail='finish';c.acquire(args,plan,io_factory=IO)
    monkeypatch.setattr(c,'observations',real_observations)
    def forbidden(*args):pytest.fail('ineligible failed trial reached numerical scorer')
    monkeypatch.setattr(c.validation,'double_talk_trial',forbidden)
    row=c.observations(plan,'double-talk-0')[0]
    assert row['status']=='unavailable' and 'finish failed' in row['error']
    assert 'original' in row and not row['observed'] and not row['available']
    assert not row['eligibility']['eligible']
    assert v.double_talk_summary(c.observations(plan,'double-talk-0'),10)['interruption']['total']==0


def test_successful_prefix_of_later_failed_part_stays_consumed_but_ineligible(rig,monkeypatch):
    args,plan,state,IO=rig;real_observations=c.observations;start_double(args,plan,IO,monkeypatch)
    class LaterFailure(IO):
        begun=0
        def begin(self,*args):
            self.begun+=1
            if self.begun==2:raise RuntimeError('second trial connect failed')
            return super().begin(*args)
    c.acquire(args,plan,io_factory=LaterFailure)
    monkeypatch.setattr(c,'observations',real_observations)
    rows=c.observations(plan,'double-talk-0')
    assert rows[0]['status']=='retained' and not rows[0]['eligibility']['eligible'] and not rows[0]['observed']
    assert rows[1]['status']=='unavailable' and not rows[1]['eligibility']['eligible']
    assert len(c.consumed(plan,'double-talk-0'))==2
    next_part(args,plan,'double-talk-0',2)
    assert c.acquire(args,plan,io_factory=IO)['acquisition_passed']
    assert len(c.consumed(plan,'double-talk-0'))==10
    assert c.observations(plan,'double-talk-0')[0]['eligibility']['eligible'] is False


@pytest.mark.parametrize('file',['part-finalized.json','scored-original.json','post-body-payload.json',
    'room/result.json','room/room.wav','room/mixer.log'])
def test_missing_enclosing_or_room_evidence_stops_scoring(numerical_delay_rig,file):
    args,plan,state,IO,count=numerical_delay_rig;c.acquire(args,plan,io_factory=IO)
    (args.out/file).unlink();row=c.observations(plan,'delay-0')[0]
    assert not row['available'] and not row['eligibility']['eligible'] and count==[]


@pytest.mark.parametrize('field,value',[('board','echoear'),('elf_sha256','0'*64),('part','delay-1'),
    ('plan_sha256','f'*64),('identity',{})])
def test_rebound_result_is_ineligible_even_with_consistent_file_receipts(numerical_delay_rig,field,value):
    args,plan,state,IO,count=numerical_delay_rig;c.acquire(args,plan,io_factory=IO)
    path=args.out/'result.json';row=json.loads(path.read_text());row[field]=value
    path.write_text(json.dumps(row));(args.out/'scored-original.json').write_text(json.dumps(row))
    (args.out/'part-finalized.json').unlink();c.finalize_part(plan,args.part,args.out)
    assert not c.observations(plan,'delay-0')[0]['available'] and count==[]


def test_room_failure_cannot_be_rebound_into_an_eligible_result(numerical_delay_rig):
    args,plan,state,IO,count=numerical_delay_rig;c.acquire(args,plan,io_factory=IO)
    path=args.out/'result.json';result=json.loads(path.read_text());result['room']['noise_contaminated']=True
    path.write_text(json.dumps(result));(args.out/'scored-original.json').write_text(json.dumps(result))
    (args.out/'room/result.json').write_text(json.dumps(result['room']))
    (args.out/'part-finalized.json').unlink();c.finalize_part(plan,args.part,args.out)
    assert not c.observations(plan,'delay-0')[0]['available'] and count==[]


def test_runtime_source_set_excludes_unrelated_helpers_and_tests(plan_rig,tmp_path,monkeypatch):
    *args,manifest=plan_rig
    # No campaign plan is written. Hash checks are entirely in memory.
    plan=c.make_plan(*args)
    assert len(plan['source_sha256'])==len(c.SOURCES)<60
    assert 'hil/test_p4_aec_validation.py' not in plan['source_sha256']
    assert 'hil/p4_aec_image_measure.py' not in plan['source_sha256']
    original_digest=c.digest
    def changed_test(path):
        return 'f'*64 if Path(path).name.startswith('test_') else original_digest(path)
    monkeypatch.setattr(c,'digest',changed_test)
    assert c.validate_plan(plan)==plan
    def changed_runtime(path):
        return 'f'*64 if Path(path).name=='p4_aec_measure.py' else original_digest(path)
    monkeypatch.setattr(c,'digest',changed_runtime)
    with pytest.raises(ValueError,match='changed'):c.validate_plan(plan)


@pytest.mark.parametrize('change',['status','error'])
def test_failed_trial_is_rejected_inside_otherwise_successful_part(numerical_delay_rig,change):
    args,plan,state,IO,count=numerical_delay_rig;c.acquire(args,plan,io_factory=IO)
    folder=next(iter(c.consumed(plan,'delay-0').values()));path=folder/'trial.json'
    trial=json.loads(path.read_text())
    if change=='status':trial['status']='unavailable'
    else:trial['error']='trial finalization failed'
    path.write_text(json.dumps(trial))
    (folder/'trial-sealed.json').write_text(json.dumps(dict(trial_sha256=c.digest(path),start_sha256=c.digest(folder/'trial-start.json'))))
    (args.out/'part-finalized.json').unlink();c.finalize_part(plan,args.part,args.out)
    assert not c.observations(plan,'delay-0')[0]['available'] and count==[]


def test_invalid_restore_pin_blocks_replay_before_numerical_scoring(numerical_delay_rig,monkeypatch):
    import p4_step14
    args,plan,state,IO,count=numerical_delay_rig;c.acquire(args,plan,io_factory=IO)
    def bad_pin(*args):raise ValueError('R7 pin changed')
    monkeypatch.setattr(p4_step14,'restore_record',bad_pin)
    row=c.observations(plan,'delay-0')[0]
    assert not row['available'] and 'R7 pin changed' in row['reason'] and count==[]


def test_v3_successful_finalized_trials_are_admitted_and_scored(rig,prompt_rig,monkeypatch):
    args,plan,state,IO=rig;real_observations=c.observations
    for spec in plan['fixtures']:spec['expected']='hello'
    args.plan.write_text(json.dumps(plan))
    start_double(args,plan,IO,monkeypatch)
    class RecordedIO(IO):
        def reply(self,board,folder,*args):
            folder.mkdir();row=copy.deepcopy(prompt_rig[1]);row['capture']={}
            c.policy.write_new(folder/'original.json',row);return row
    monkeypatch.setattr(c.measure,'load_capture',lambda *args:np.zeros((8192,5),dtype='<i2'))
    assert c.acquire(args,plan,io_factory=RecordedIO)['acquisition_passed']
    monkeypatch.setattr(c,'observations',real_observations)
    rows=c.observations(plan,'double-talk-0')
    assert all(row['eligibility']['eligible'] and row['observed'] and row['median_erle_db']==23 for row in rows)
    summary=v.double_talk_summary(rows,10)
    assert summary['interruption']['hits']==10 and summary['stt_accuracy_ge_90_percent']['hits']==10
    assert summary['missed_injections']==0
