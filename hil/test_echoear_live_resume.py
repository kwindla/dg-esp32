from copy import deepcopy
import pytest
from echoear_live_resume import original_population,combine


def original():
    return dict(passed=False,reruns=[],runs=[dict(run=1,scored=True,passed=False,pc_id='before-handoff'),dict(run=2,passed=False,error='Unscored setup')])


def test_resume_retains_failed_original_and_excludes_unscored_setup_from_consumed_slots():
    before=original();saved=deepcopy(before)
    segment=dict(runs=[dict(run=i,scored=True,passed=True,pc_id='after-handoff') for i in range(2,6)])
    joined=combine(before,segment)
    assert joined['acquisition_completed'] and not joined['passed'] and not joined['qualification']
    assert [r['run'] for r in joined['runs']]==[1,2,3,4,5]
    assert joined['runs'][0]['passed'] is False and not joined['same_peer']
    assert before==saved


def test_replay_duplicates_and_missing_original_are_rejected():
    for indices in ([1,2,3,4],[2,2,3,4],[3,4,5]):
        with pytest.raises(RuntimeError):combine(original(),dict(runs=[dict(run=i,scored=True) for i in indices]))
    changed=original();changed['runs'][1]['scored']=True
    with pytest.raises(RuntimeError):original_population(changed)


def test_fatal_unscored_stop_cannot_complete_or_qualify_the_cohort():
    joined=combine(original(),dict(runs=[dict(run=2,scored=True,passed=False),dict(run=3,passed=False)]))
    assert not joined['acquisition_completed'] and joined['remaining_originals']==[3,4,5] and not joined['passed']


def test_production_loop_resumes_indices_two_through_five_without_original_one(tmp_path):
    from echoear_requalification import load_plan
    from echoear_requalification_dry_run import rehearse
    result=rehearse(load_plan(),tmp_path,all_originals=True)
    assert result['checks']['resume_only_unscored_slots']
    resumed=next(c for c in result['cohort_calls'] if c['scenario']=='synthetic-priority-resume')
    assert resumed['runs']==4 and resumed['reruns']==0 and resumed['original_calls'][0]['reset']


@pytest.mark.parametrize('failed_gate',['uplink','zero_transport','memory','per_arm_nic'])
def test_segment_cannot_pass_when_production_observation_gate_fails(tmp_path,failed_gate):
    from unittest.mock import patch
    from echoear_live_resume import validate_segment
    (tmp_path/'serial.log').write_text('synthetic raw')
    policy=dict(target_dbfs=-29.845806865819114)
    segment=dict(passed=True,stage_nic=dict(passed=True),runs=[dict(run=i,passed=True,
        jabra_rebaseline_policy=policy,jabra_level=dict(target_dbfs=policy['target_dbfs'])) for i in range(2,6)])
    with patch('echoear_requalification.observation_gate',return_value=dict(observations_passed=False,checks={failed_gate:False})):
        validate_segment({},segment,tmp_path,policy)
    assert not segment['passed']
    assert segment['item2_observations']['checks'][failed_gate] is False
    assert segment['population_scope']['absent_originals_from_this_segment']==[1]
    assert not segment['population_scope']['five_originals_gate_relaxed']


def test_segment_deferral_uses_normal_resource_and_cleanup_validation(tmp_path):
    from unittest.mock import patch
    from echoear_live_resume import validate_segment
    segment=dict(deferred=True,passed=True)
    with patch('echoear_item2_campaign.validate_deferral') as check:
        validate_segment({},segment,tmp_path,{})
    check.assert_called_once_with(tmp_path,segment)
    assert not segment['passed']
