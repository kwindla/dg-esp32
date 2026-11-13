"""Pre-acquisition boundaries, negatives and the unchanged independent/scoring methods."""
import copy
import json
import numpy as np
import pytest

import p4_aec_policy as policy
from wire_contract import ContractError
from test_p4_aec_schema import perf_record


def points():
    return [dict(gain_db=g,clips_passed=True,echoes=[dict(available=True,median_erle_db=g/3,false_user_turns=0) for _ in range(10)],
        prompts=[dict(id=name,passed=True,accuracy=1.,false_user_turns=0) for name in ('france','italy','speech')]) for g in policy.GAINS]


def test_complete_gain_ladder_and_lower_gain_within_one_db_of_global_best():
    result=policy.select_gain(points())
    assert result['selected_gain_db']==30 and result['median_erle_db']==10
    assert not result['campaign_erle_target_passed'] and not result['qualification']
    rows=points()
    for p in rows:p['clips_passed']=p['gain_db']<=6
    for p,score in zip(rows,(10,10.9,11.8)):
        for r in p['echoes']:r['median_erle_db']=score
    assert policy.select_gain(rows)['selected_gain_db']==3  # No transitive tie chaining.


@pytest.mark.parametrize('distance,selected',[(.999,0),(1.,0),(1.001,3)])
def test_erle_tie_boundary(distance,selected):
    rows=points()
    for p in rows:p['clips_passed']=p['gain_db']<=3
    for r in rows[0]['echoes']:r['median_erle_db']=20
    for r in rows[1]['echoes']:r['median_erle_db']=20+distance
    assert policy.select_gain(rows)['selected_gain_db']==selected


@pytest.mark.parametrize('accuracy,passed',[(.899999,False),(.9,True),(1.,True),(float('nan'),False),(float('inf'),False),(True,False)])
def test_accuracy_boundary_and_invalid_values(accuracy,passed):
    rows=points()
    for p in rows:p['clips_passed']=p['gain_db']==0
    rows[0]['prompts'][0]['accuracy']=accuracy
    result=policy.select_gain(rows)
    assert result['passed'] is passed
    assert result['selected_gain_db']==(0 if passed else None)


@pytest.mark.parametrize('mutation',['clip','false-echo-turn','false-prompt-turn','prompt-fail','missing-erle','nan-erle'])
def test_none_eligible_stops(mutation):
    rows=points()
    for p in rows:
        if mutation=='clip':p['clips_passed']=False
        elif mutation=='false-echo-turn':p['echoes'][0]['false_user_turns']=1
        elif mutation=='false-prompt-turn':p['prompts'][0]['false_user_turns']=1
        elif mutation=='prompt-fail':p['prompts'][0]['passed']=False
        elif mutation=='missing-erle':p['echoes'][0]['available']=False
        else:p['echoes'][0]['median_erle_db']=float('nan')
    result=policy.select_gain(rows)
    assert not result['passed'] and result['selected_gain_db'] is None and result['reason']=='none eligible; stop'


@pytest.mark.parametrize('mutation',['gain-missing','gain-order','replies','prompt-missing','prompt-order'])
def test_missing_or_reordered_sweep_cannot_select(mutation):
    rows=points()
    if mutation=='gain-missing':rows.pop()
    elif mutation=='gain-order':rows[0],rows[1]=rows[1],rows[0]
    elif mutation=='replies':rows[0]['echoes'].pop()
    elif mutation=='prompt-missing':rows[0]['prompts'].pop()
    else:rows[0]['prompts'].reverse()
    with pytest.raises(ContractError):policy.select_gain(rows)


def signals():
    rng=np.random.default_rng(8513);source=rng.integers(-3500,3500,8000).astype(float)
    near=np.zeros((32000,5));near[:,0]=rng.normal(0,2,32000);near[4800:12800,0]+=source
    near[1024:,4]=near[:-1024,0]*.8
    dac=np.zeros((48000,5));probe=policy.alignment.probe_pcm().astype(float)
    dac[4000:4000+len(probe),2]=probe*.2;dac[4019:4019+len(probe),0]=probe*.3
    return dac,near,source


def test_real_independent_method_measures_1024_without_erle_search():
    dac,near,source=signals();result=policy.delay_score(dac,near,source)
    assert result['available'] and result['measured_delay_samples']==1024
    assert result['correlation']>=.6 and result['separation']>=.05
    assert result['analysis']['erle'] is None


@pytest.mark.parametrize('change',['silent','reference-hot','weak-excitation','clipped','incomplete'])
def test_ambiguous_or_invalid_independent_delay_is_unavailable(change):
    dac,near,source=signals()
    if change=='silent':near[:]=0
    elif change=='reference-hot':near[:,2]=near[:,0]/10
    elif change=='weak-excitation':
        near[:,0]=near[:,0]/10000+np.random.default_rng(9).normal(0,2,len(near));near[:,4]=near[:,0]
    elif change=='clipped':near[0,0]=32767
    else:near=near[:100]
    result=policy.delay_score(dac,near,source)
    assert not result['available'] and result['measured_delay_samples'] is None


@pytest.mark.parametrize('correlation,secondary,lag,passed',[(.6,.4,1024,True),(.599999,.4,1024,False),
    (.8,.75,1024,True),(.8,.750001,1024,False),(.9,.1,0,False),(.9,.1,4096,False),(.9,.1,True,False)])
def test_delay_confidence_and_search_boundaries(monkeypatch,correlation,secondary,lag,passed):
    row=dict(independent_nearend_afe_delay=dict(available=True,signed_correlation=correlation,secondary_peak=secondary,lag_samples=lag),
        known_source_to_reference=dict(available=True),known_nearend_source_to_mic=dict(available=True),
        nearend_excitation_verified=True,reference_silent_for_independent_afe=True,reference_dac_vs_nearend_db=20)
    monkeypatch.setattr(policy.alignment,'analyze',lambda *args:row)
    pcm=np.zeros((8192,5))
    assert policy.delay_score(pcm,pcm,np.zeros(8192))['available'] is passed


def test_prompt_must_be_during_playback_and_use_a_single_actual_transcript():
    spec=dict(expected='one two three four five six seven eight nine ten')
    events=[dict(kind='aggregator-user-turn-stopped',monotonic_ns=2200000000,text='one two three four five six seven eight nine')]
    events += [dict(kind='frame',frame='UserStartedSpeakingFrame',monotonic_ns=stamp,bot_active=active)
               for stamp,active in ((500000000,False),(2100000000,True))]
    play=dict(request_monotonic_s=2)
    result=policy.prompt_score(spec,events,play,1000000000,3000000000)
    assert result['passed'] and result['accuracy']==.9
    assert not policy.prompt_score(spec,events,play,1000000000,2000000000)['passed']
    assert not policy.prompt_score(spec,events+events,play,1000000000,3000000000)['passed']
    assert not policy.prompt_score(spec,[],play,1000000000,3000000000)['passed']
    extra=dict(kind='frame',frame='UserStartedSpeakingFrame',monotonic_ns=2500000000,bot_active=True)
    assert not policy.prompt_score(spec,events+[extra],play,1000000000,3000000000)['passed']


def test_native_and_aec_clips_and_counter_resets_fail():
    before=perf_record('p4xc');after=copy.deepcopy(before)
    pcm=np.zeros((8192,5));assert policy.clip_evidence(before,after,pcm)['passed']
    pcm[5,0]=-32768;assert not policy.clip_evidence(before,after,pcm)['passed'];pcm[:]=0
    after['aec']['reference_clips']=1;assert not policy.clip_evidence(before,after,pcm)['passed']
    before['aec']['reference_clips']=2
    with pytest.raises(ContractError,match='reset'):policy.clip_evidence(before,after,pcm)


def guard_input():
    turns=[dict(passed=True,user_turn_starts=1,user_starts_during_bot=0,interruptions_during_bot=0) for _ in range(10)]
    lines=['AEC_GUARD onset_upper_us=3000000 source_deadline_us=3400000 guard_ms=400 rearm_ms=2000 previous_end_us=100000']
    return turns,lines,dict(inside_guard=True,clips=dict(passed=True)),dict(detected=True,after_rearm=True,clips=dict(passed=True))


def test_guard_starts_at_s3_values_and_records_real_onset_rearm():
    result=policy.guard_score(0,*guard_input())
    assert result['passed'] and (result['guard_ms'],result['rearm_ms'])==(400,2000)
    assert policy.select_guard([result])['candidate_index']==0
    assert all(0<=g<=1000 and 500<=r<=10000 for g,r in policy.GUARDS)


@pytest.mark.parametrize('change',['nine','self-start','interruption','no-onset','wrong-setting','wrong-deadline','no-rearm','after-undetected','after-not-rearmed'])
def test_guard_negatives(change):
    turns,lines,inside,after=guard_input()
    if change=='nine':
        with pytest.raises(ContractError):policy.guard_score(0,turns[:-1],lines,inside,after)
        return
    if change=='self-start':turns[0]['user_starts_during_bot']=1
    elif change=='interruption':turns[0]['interruptions_during_bot']=1
    elif change=='no-onset':lines=[]
    elif change=='wrong-setting':lines=[lines[0].replace('guard_ms=400','guard_ms=600')]
    elif change=='wrong-deadline':lines=[lines[0].replace('3400000','3400001')]
    elif change=='no-rearm':lines=[lines[0].replace('previous_end_us=100000','previous_end_us=-1')]
    elif change=='after-undetected':after['detected']=False
    elif change=='after-not-rearmed':after['after_rearm']=False
    assert not policy.guard_score(0,turns,lines,inside,after)['passed']


def test_guard_changes_only_after_failure_and_never_skips_or_continues_after_pass():
    first=policy.guard_score(0,*guard_input());second=dict(first,guard_ms=600)
    with pytest.raises(ContractError):policy.select_guard([second])
    with pytest.raises(ContractError):policy.select_guard([first,second])
    first['passed']=False
    assert policy.select_guard([first,second])['guard_ms']==600


def test_real_frozen_frame_erle_and_residual_use_explicit_independent_delay():
    from test_board_smoke import events
    rng=np.random.default_rng(4);pcm=np.zeros((20000,5))
    pcm[:,0]=rng.normal(0,300,len(pcm));pcm[:,2]=rng.normal(0,10,len(pcm))
    pcm[1024:,4]=pcm[:-1024,0]/20
    quiet=np.ones((8192,5))
    result=policy.echo_score(pcm,quiet,1024,events('long',True),True)
    assert result['available'] and result['false_user_turns']==0
    assert result['erle']['delay_samples']==1024 and result['erle']['method']=='independent-quiet-v2'
    assert abs(result['median_erle_db']-26.0205999)<.00001
    assert abs(result['residual']['median_frame_reduction_db']-result['median_erle_db'])<.00001
    assert result['residual']['residual_dbfs']<result['residual']['raw_dbfs']
    assert not policy.echo_score(pcm,quiet*20,1024,events('long',True),True)['available']
    with pytest.raises(ContractError,match='incomplete'):policy.echo_score(pcm,quiet,1024,events('long',True),False)
    corrupted=events('long',True)+[dict(kind='frame',frame='UserStartedSpeakingFrame',direction='DOWNSTREAM',bot_active=True)]
    failed=policy.echo_score(pcm,quiet,1024,corrupted,True)
    assert not failed['available'] and failed['false_user_turns']==1


def test_inside_guard_timing_and_guard_clip_diagnostics_are_report_only():
    turns,lines,inside,after=guard_input()
    inside.update(inside_guard=False,reason='late acoustic onset',clips=dict(passed=False))
    after['clips']['passed']=False
    result=policy.guard_score(0,turns,lines,inside,after)
    assert result['passed'] and result['inside_timing_report_only'] and result['inside_probe']['inside_guard'] is False


def test_bot_content_is_report_only_but_false_turns_remain_hard():
    from test_board_smoke import events
    rng=np.random.default_rng(8);pcm=np.zeros((16000,5));pcm[:,0]=rng.normal(0,200,len(pcm));pcm[:,2]=20
    pcm[1024:,4]=pcm[:-1024,0]/20
    original=events('long',True)
    for event in original:
        if event.get('kind')=='aggregator-assistant-turn-stopped':event['text']='I would rather tell you about penguins.'
    score=policy.echo_score(pcm,np.ones((8192,5)),1024,original,True)
    assert score['available'] and score['turn']['bot_content_findings']
    turns,lines,inside,after=guard_input();turns=[score['turn']]*10
    guard=policy.guard_score(0,turns,lines,inside,after)
    assert guard['passed'] and len(guard['bot_content_findings'])==10
    original.append(dict(kind='frame',direction='DOWNSTREAM',frame='InterruptionFrame',bot_active=True))
    assert not policy.echo_score(pcm,np.ones((8192,5)),1024,original,True)['available']
