"""Prepared during bench hold. Run only after the coordinator says bench free."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import board_smoke as smoke
import p4_aec_measure as measure
import p4_aec_policy as policy
import p4_aec_reselect as reselect
from make_bundle import digest
from wire_contract import ContractError
from test_p4_aec_measure import scoring_grid
from test_p4_aec_policy import guard_input

REAL=Path(os.environ.get('DG_P4_AEC_DEDUP_ORIGINAL',Path.home()/
    'src/evidence/echoear/p4/step-15b/coordinator-sweep-1/grid/gain-00/part-3/attempt-01/prompt-italy/original.json'))
PLAN=Path(os.environ.get('DG_P4_AEC_SWEEP_PLAN',Path.home()/
    'src/evidence/echoear/p4/step-15b/coordinator-sweep-1/plan.json'))


def paired_start(at,active):
    return [dict(kind='frame',frame='UserStartedSpeakingFrame',direction=direction,
        monotonic_ns=int(at*1e9),bot_active=active,frame_id=index)
        for index,direction in enumerate(('DOWNSTREAM','UPSTREAM'))]


def prompt_events(text='What about Italy?'):
    return paired_start(.5,False)+paired_start(2.1,True)+[
        dict(kind='aggregator-user-turn-stopped',monotonic_ns=2200000000,text=text)]


def score(events,text='What about Italy?',playback=None):
    return reselect.direction_prompt_score(dict(expected=text),events,
        playback or dict(request_monotonic_s=2),1000000000,3000000000)


def test_only_downstream_turn_frames_change_the_original_prompt_score():
    events=prompt_events();before=copy.deepcopy(events)
    original=policy.prompt_score(dict(expected='What about Italy?'),events,dict(request_monotonic_s=2),1000000000,3000000000)
    fixed=score(events)
    assert original['false_user_turns']==2 and not original['passed']
    assert fixed['passed'] and fixed['intentional_user_turn_witnessed'] and fixed['false_user_turns']==0
    assert fixed['accuracy']==original['accuracy']==1 and events==before
    logical=[e for e in events if e.get('kind')!='frame' or e['direction']=='DOWNSTREAM']
    assert fixed==policy.prompt_score(dict(expected='What about Italy?'),logical,dict(request_monotonic_s=2),1000000000,3000000000)


@pytest.mark.skipif(not REAL.is_file(),reason='retained gain-00 Italy original absent; set DG_P4_AEC_DEDUP_ORIGINAL')
def test_real_italy_control_has_one_intentional_start_and_zero_false_turns():
    before=digest(REAL);row=json.loads(REAL.read_text())
    spec=next(s for s in policy.fixtures() if s['id']=='italy')
    result=reselect.direction_prompt_score(spec,row['events'],row['near_playback'],row['bot_started_ns'],row['bot_stopped_ns'])
    logical=smoke.downstream_frames(row['events'])
    starts=[e for e in logical if e['frame']=='UserStartedSpeakingFrame']
    intentional=[e for e in starts if e['monotonic_ns']>=row['near_playback']['request_monotonic_s']*1e9]
    assert len(starts)==2 and len(intentional)==1 and intentional[0]['bot_active'] is True
    assert result['passed'] and result['accuracy']==1 and result['false_user_turns']==0
    assert digest(REAL)==before


def test_genuine_bot_active_start_before_injection_still_fails():
    events=prompt_events()+paired_start(1.5,True)
    result=score(events)
    assert not result['passed'] and result['false_user_turns']>=1


def test_three_real_starts_still_count_as_an_extra_turn():
    result=score(prompt_events()+paired_start(2.5,True))
    assert not result['passed'] and not result['intentional_user_turn_witnessed']
    assert result['false_user_turns']==1


def test_pre_injection_interruption_is_not_removed_as_a_duplicate():
    events=prompt_events()+[dict(kind='frame',frame='InterruptionFrame',direction=d,
        monotonic_ns=1500000000,bot_active=True) for d in ('DOWNSTREAM','UPSTREAM')]
    assert score(events)['false_user_turns']==1 and not score(events)['passed']


@pytest.mark.parametrize('text,expected',[
    ('one two three four five six seven eight nine',True),
    ('one two three four five six seven eight',False)])
def test_accuracy_threshold_is_unchanged(text,expected):
    result=score(prompt_events(text),'one two three four five six seven eight nine ten')
    assert result['passed'] is expected


def test_missing_direction_and_missing_injection_do_not_get_a_pass():
    events=prompt_events();events[0].pop('direction')
    with pytest.raises(ContractError,match='direction'):score(events)
    assert not score(prompt_events(),playback=dict(request_monotonic_s=0))['passed']


def make_part(parent,name='attempt-01',failed=False,clean=True):
    directory=parent/name;directory.mkdir(parents=True)
    payload=dict(cases=[dict(id='prompt-italy',score=dict(passed=False))])
    policy.write_new(directory/'post-body-payload.json',payload)
    row=dict(payload,status='acquisition-error' if failed else 'acquired',acquisition_passed=not failed,
        score_produced=True,retryable=False,restore=dict(clean=clean,action='p4-product-of-record'),
        post_body_payload_sha256=digest(directory/'post-body-payload.json'),
        error='TimeoutError: disconnect' if failed else None,cleanup_errors=['disconnect failed'] if failed else [])
    policy.write_new(directory/'result.json',row)
    return directory,row


@pytest.mark.parametrize('clean',[False,True])
def test_scored_finalization_failure_is_used_and_failure_remains_visible(tmp_path,clean):
    directory,row=make_part(tmp_path,failed=True,clean=clean)
    before={str(p):digest(p) for p in directory.iterdir()}
    path,actual=reselect.scored_part(tmp_path)
    assert actual==row and actual['acquisition_passed'] is False
    failure=reselect.part_failure(path,row)
    assert failure['error']==row['error'] and failure['cleanup_errors']==row['cleanup_errors']
    assert failure['restore']['clean'] is clean and 'used as scored' in failure['score_treatment']
    if not clean:assert 'coordinator R7 evidence' in failure['r7_recovery_status']
    assert before=={str(p):digest(p) for p in directory.iterdir()}


def test_second_scored_original_or_attempt_after_score_is_refused(tmp_path):
    make_part(tmp_path,'attempt-01');make_part(tmp_path,'attempt-02',failed=True)
    with pytest.raises(ContractError,match='exactly one'):reselect.scored_part(tmp_path)


@pytest.mark.parametrize('change',['missing-payload','payload-hash','inconsistent-payload','unscored'])
def test_missing_or_changed_score_evidence_never_defaults_to_usable(tmp_path,change):
    directory,row=make_part(tmp_path,failed=True)
    if change=='missing-payload':(directory/'post-body-payload.json').unlink()
    elif change=='payload-hash':(directory/'post-body-payload.json').write_text('{}')
    elif change=='inconsistent-payload':row['cases'][0]['score']['passed']=True
    else:row.update(score_produced=False,retryable=True)
    (directory/'result.json').write_text(json.dumps(row))
    with pytest.raises(ContractError):reselect.scored_part(tmp_path)


def prepare_grid(scoring_grid,monkeypatch):
    plan,path,delay,root=scoring_grid
    for spec in plan['fixtures']:spec['expected']='Expected '+spec['id']
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(measure,'prompt_score',policy.prompt_score)
    monkeypatch.setattr(reselect,'validate_frozen_plan',lambda plan:plan)
    monkeypatch.setattr(reselect,'scoring_context',lambda:dict(id='test-context',decisions=list(reselect.DECISIONS)))
    for source in root.rglob('result.json'):
        row=json.loads(source.read_text());row.update(score_produced=True,retryable=False,plan_sha256=digest(path))
        for case in row['cases']:
            if not case['id'].startswith('prompt-'):continue
            spec=next(s for s in plan['fixtures'] if case['id']=='prompt-'+s['id'])
            original=case['original'];original.update(events=prompt_events(spec['expected']),
                near_playback=dict(request_monotonic_s=2),bot_started_ns=1000000000,bot_stopped_ns=3000000000)
            case['score']=policy.prompt_score(spec,original['events'],original['near_playback'],1000000000,3000000000)
        payload={key:row[key] for key in ('gain_db','part','settings','quiet','cases','delay_file','delay_sha256','delay_provenance')}
        policy.write_new(source.parent/'post-body-payload.json',payload)
        row['post_body_payload_sha256']=digest(source.parent/'post-body-payload.json')
        source.write_text(json.dumps(row))
    return plan,path,delay,root


def test_full_reselection_changes_only_prompt_convention_and_preserves_original(scoring_grid,monkeypatch,tmp_path):
    plan,path,delay,root=prepare_grid(scoring_grid,monkeypatch)
    original=measure.gain_selection(plan,path,delay,root)
    assert not original['passed']
    original_path=tmp_path/'original-selection.json';policy.write_new(original_path,original)
    before={str(p):digest(p) for p in root.rglob('*.json')}
    result=reselect.reselect(path,delay,root,original_path)
    assert result['rescored_selection']['passed'] and result['rescored_selection']['selected_gain_db']==0
    assert result['original_selection']['record']==original
    assert result['original_selection']['sha256']==digest(original_path)
    assert result['finalization_failures']==[] and result['qualification'] is False
    assert before=={str(p):digest(p) for p in root.rglob('*.json')}
    selected=tmp_path/'reselected.json';policy.write_new(selected,result)
    assert reselect.checked_gain(selected,plan)==0
    result['rescored_selection']['selected_gain_db']=3;selected.write_text(json.dumps(result))
    with pytest.raises(ContractError):reselect.checked_gain(selected,plan)


def test_failed_scored_part_does_not_disqualify_a_gain_under_predeclared_rule(scoring_grid,monkeypatch):
    plan,path,delay,root=prepare_grid(scoring_grid,monkeypatch)
    source=root/'gain-00/part-3/attempt-01/result.json';row=json.loads(source.read_text())
    row.update(status='acquisition-error',acquisition_passed=False,error='disconnect failed',restore=dict(clean=False))
    source.write_text(json.dumps(row))
    result=reselect.reselect(path,delay,root)
    assert result['rescored_selection']['selected_gain_db']==0
    assert result['finalization_failures'][0]['error']=='disconnect failed'
    assert result['finalization_failures'][0]['restore']['clean'] is False


def test_reselection_uses_supplied_delay_and_keeps_original_scores(scoring_grid,monkeypatch):
    plan,path,delay,root=prepare_grid(scoring_grid,monkeypatch)
    corrected=path.parent/'corrected-delay.json';policy.write_new(corrected,dict(corrected=True))
    original_input=measure.delay_input;seen=[]
    def proof(source,plan):
        row=original_input(source,plan)
        return dict(row,delay_samples=512) if Path(source)==corrected else row
    def echo(pcm,quiet,samples,*args):
        seen.append(samples)
        return dict(available=True,median_erle_db=24. if samples==1024 else 21.,false_user_turns=0,
            residual=dict(residual_dbfs=-55.))
    monkeypatch.setattr(measure,'delay_input',proof);monkeypatch.setattr(measure,'echo_score',echo)
    before={str(p):digest(p) for p in root.rglob('*.json')}
    result=reselect.reselect(path,corrected,root)['rescored_selection']
    assert result['passed'] and result['selected_gain_db']==0 and result['median_erle_db']==21
    assert seen.count(1024)==120 and seen.count(512)==120
    assert result['delay_rescored'] and result['delay_provenance']['delay_samples']==512
    assert {p['delay_samples'] for p in result['acquisition_delays'].values()}=={1024}
    assert before=={str(p):digest(p) for p in root.rglob('*.json')}


def test_cached_original_scores_still_have_to_authenticate(scoring_grid,monkeypatch):
    plan,path,delay,root=prepare_grid(scoring_grid,monkeypatch)
    source=root/'gain-00/part-3/attempt-01/result.json';row=json.loads(source.read_text())
    row['cases'][-1]['score']['accuracy']=.8
    # Even a mutually consistent result/payload cannot forge the re-derived cached score.
    payload={k:v for k,v in json.loads((source.parent/'post-body-payload.json').read_text()).items()}
    payload['cases']=row['cases'];(source.parent/'post-body-payload.json').write_text(json.dumps(payload))
    row['post_body_payload_sha256']=digest(source.parent/'post-body-payload.json');source.write_text(json.dumps(row))
    with pytest.raises(ContractError,match='cached'):reselect.reselect(path,delay,root)


@pytest.mark.skipif(not PLAN.is_file(),reason='retained sweep plan absent; set DG_P4_AEC_SWEEP_PLAN')
def test_real_plan_keeps_its_original_hashes_and_paths_without_rebinding():
    before=digest(PLAN);plan=json.loads(PLAN.read_text())
    assert reselect.validate_frozen_plan(plan)==plan and digest(PLAN)==before
    changed=copy.deepcopy(plan);changed['source_sha256']['board_smoke.py']='f'*64
    with pytest.raises(ContractError,match='frozen Git-source'):reselect.validate_frozen_plan(changed)


def test_decision_file_cannot_change_silently(tmp_path,monkeypatch):
    path=tmp_path/'test.md';path.write_text('declared before selection')
    monkeypatch.setattr(reselect,'JOBS',tmp_path);monkeypatch.setattr(reselect,'DECISIONS',{'test':digest(path)})
    assert reselect.decision_bindings()[0]['id']=='test'
    path.write_text('changed later')
    with pytest.raises(ContractError,match='decision changed'):reselect.decision_bindings()


def test_guard_explicitly_receives_fixed_scorer_and_original_r7_runner(monkeypatch,tmp_path):
    context=dict(id='fixed',decisions=list(reselect.DECISIONS))
    monkeypatch.setattr(reselect,'scoring_context',lambda:context)
    run=Mock(return_value=dict(status='acquired'));monkeypatch.setattr(measure,'acquire_guard',run)
    args=SimpleNamespace(previous=[]);plan={}
    assert reselect.guard(args,plan)['status']=='acquired'
    assert run.call_args.kwargs==dict(gain_checker=reselect.checked_gain,plan_validator=reselect.validate_frozen_plan,
        prompt_scorer=reselect.direction_prompt_score,scoring_metadata=dict(scoring_context=context))


def test_guard_probe_replay_passes_the_fixed_scorer(monkeypatch):
    from test_board_smoke import events
    turns,lines,inside,after=guard_input();seen=[]
    def probe(original,spec,is_inside,*,prompt_scorer=None):
        seen.append(prompt_scorer);return original
    monkeypatch.setattr(measure,'probe_score',probe)
    row=dict(candidate_index=0,long_turns=[dict(events=events('long',True)) for _ in range(10)],
        onsets=lines,inside_original=inside,after_original=after)
    result=measure.score_guard_row(row,dict(fixtures=[{},{}]),prompt_scorer=reselect.direction_prompt_score)
    assert result['passed'] and seen==[reselect.direction_prompt_score]*2


def test_recommendation_labels_decision_delay_and_uses_fixed_probe_scorer(tmp_path,monkeypatch):
    context=dict(id='fixed',decisions=list(reselect.DECISIONS));monkeypatch.setattr(reselect,'scoring_context',lambda:context)
    monkeypatch.setattr(reselect,'validate_frozen_plan',lambda plan:plan)
    monkeypatch.setattr(reselect,'checked_gain',lambda *args:12)
    plan=dict(elf_sha256='a'*64);plan_path=tmp_path/'plan.json';policy.write_new(plan_path,plan)
    delay=tmp_path/'delay.json';policy.write_new(delay,{})
    gain=tmp_path/'gain.json';policy.write_new(gain,dict(kind='p4-aec-reselection',rescored_selection=dict(delay_file=str(delay))))
    verdict=dict(passed=True,guard_ms=400,rearm_ms=2000)
    guard=tmp_path/'guard.json';policy.write_new(guard,dict(acquisition_passed=True,restore=dict(clean=True),
        elf_sha256='a'*64,policy=policy.POLICY,gain_selection_sha256=digest(gain),plan_sha256=digest(plan_path),
        score=verdict,scoring_context=context))
    def scored(row,plan,*,prompt_scorer=None):
        assert prompt_scorer is reselect.direction_prompt_score;return verdict
    monkeypatch.setattr(measure,'score_guard_row',scored)
    provenance=dict(kind='decision-backed',measured_pass=False,delay_samples=1024)
    monkeypatch.setattr(measure,'delay_input',lambda *args:provenance)
    result=reselect.recommendation(plan_path,gain,[guard])
    assert result['delay_provenance']==provenance and result['selected']['afe_delay_samples']==1024
    assert result['scoring_context']==context and result['applied'] is False


def test_existing_output_is_refused_before_offline_replay(tmp_path,monkeypatch):
    out=tmp_path/'old.json';out.write_text('original')
    replay=Mock();monkeypatch.setattr(reselect,'reselect',replay)
    with pytest.raises(ContractError,match='fresh'):
        reselect.main(['select-gain','--plan','plan','--delay','delay','--root','grid','--out',str(out)])
    assert not replay.called and out.read_text()=='original'
