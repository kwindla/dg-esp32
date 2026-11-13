import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from echoear_jabra_rebaseline import derive,load,NORMAL,OLD


def test_offset_uses_old_only_and_does_not_change_width_or_minimum_rise():
    p=derive(-23.908268276698852,-31.754075142517966,36.30147520962353,37.995434611387736)
    assert p['geometry_attenuation_db']==pytest.approx(7.845806865819114)
    assert p['target_dbfs']==pytest.approx(-29.845806865819114)
    assert p['tolerance_db']==3 and p['minimum_rise_db']==10 and p['volume_relative_db']==0
    assert p['expected_long_reply_rise_db']==pytest.approx(30.149627745568623)


def policy_files(tmp_path):
    plan=tmp_path/'old-plan.json';acq=tmp_path/'old-acq.json';long=tmp_path/'long-plan.json'
    historical=dict(rms_dbfs=-23.908268276698852,alignment=dict(rms_rise_db=36.30147520962353))
    plan.write_text(json.dumps(dict(old_elf_sha256=OLD,reference=dict(sha256='same'),analysis=dict(historical_replayed=dict(jabra=historical)))))
    acq.write_text(json.dumps(dict(arms=[dict(role='A-old',elf_sha256=OLD,reference_sha256='same',measurements=dict(jabra=dict(rms_dbfs=-31.754075142517966)))])))
    long.write_text(json.dumps(dict(analysis=dict(historical_replayed=dict(jabra=dict(alignment=dict(rms_rise_db=37.995434611387736)))))))
    p=dict(authorization='explicit-coordinator-geometry-decision',normal_elf_sha256=NORMAL,old_reference_plan=str(plan),old_acquisition=str(acq),long_reference_plan=str(long),
        source_sha256={str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in (plan,acq,long)},
        calibration=derive(historical['rms_dbfs'],-31.754075142517966,historical['alignment']['rms_rise_db'],37.995434611387736),prior_failures_unchanged=True)
    path=tmp_path/'policy.json';path.write_text(json.dumps(p));return path,p


@pytest.mark.parametrize('change',['none','target','width','rise','source','unfrozen'])
def test_policy_binding_rejects_fit_threshold_changes_or_modified_sources(tmp_path,change):
    path,p=policy_files(tmp_path)
    if change=='none':assert load(path)['target_dbfs']==p['calibration']['target_dbfs'];return
    if change=='source':Path(p['old_acquisition']).write_text('{}')
    elif change=='unfrozen':p['source_sha256'].pop(p['old_acquisition'])
    else:p['calibration'][dict(target='target_dbfs',width='tolerance_db',rise='minimum_rise_db')[change]]+=1
    path.write_text(json.dumps(p))
    with pytest.raises(ValueError):load(path)


def test_runtime_uses_explicit_target_and_retains_rise_and_volume_gates():
    from e2e_common import reply_level
    p=derive(-23.908268276698852,-31.754075142517966,36.30147520962353,37.995434611387736)
    samples=np.ones(16000)*32768*10**(p['target_dbfs']/20)
    with patch.dict('os.environ',dict(DG_BOARD='echoear')):
        assert reply_level(samples,30,{},dict(output_volume_db=0),p)['passed']
        assert not reply_level(samples,30,{},dict(output_volume_db=0))['passed']
        assert not reply_level(samples,9.99,{},dict(output_volume_db=0),p)['passed']
        assert not reply_level(samples,30,{},dict(output_volume_db=-6),p)['passed']


def test_all_originals_rehearsal_has_three_calls_no_retakes_and_retains_failure(tmp_path):
    from echoear_requalification import load_plan
    from echoear_requalification_dry_run import rehearse
    r=rehearse(load_plan(),tmp_path,all_originals=True,jabra_policy=dict(target_dbfs=-29.845806865819114))
    assert r['passed']
    failed=next(x for x in r['cohort_calls'] if x['scenario']=='synthetic-failed-original')
    assert failed['runs']==3 and failed['reruns']==0 and failed['code']==1
    fatal=next(x for x in r['cohort_calls'] if x['scenario']=='synthetic-monitor-failure')
    assert fatal['runs']==1 and fatal['reruns']==0 and fatal['code']==1


def test_only_hash_bound_authorized_old_failure_can_precede_new_attempt(tmp_path):
    from echoear_item2_campaign import refuse_replayed_population
    directory=tmp_path/'attempt-1';directory.mkdir();path=directory/'result.json';path.write_text(json.dumps(dict(passed=False,runs=[dict(passed=False)])))
    authorized={str(path):hashlib.sha256(path.read_bytes()).hexdigest()}
    refuse_replayed_population(tmp_path,authorized)
    path.write_text(json.dumps(dict(passed=True,runs=[dict(passed=True)])))
    with pytest.raises(RuntimeError,match='earlier FAIL changed'):refuse_replayed_population(tmp_path,authorized)
