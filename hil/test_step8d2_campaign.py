import json
from pathlib import Path
import pytest
import step8d2_campaign as campaign


def test_resume_preserves_failed_scored_observations_and_calibration_attempt(tmp_path,monkeypatch):
    runs=[]
    for spl,rounds in ((65,5),(58,2)):
        for index in range(rounds):
            for name in campaign.ARMS:
                runs.append(dict(arm=name,spl=spl,round=index+1,path=str(tmp_path/f'{spl}-{index}-{name}'),
                    exit_code=1,observation=dict(turns=[dict(prompt='france',passed=False)]*6)))
    failed=dict(arm='A+gain',spl=58,round=1,path=str(tmp_path/'failed-calibration'),
        exit_code=1,observation=dict(turns=[],error='Unscored calibration failed'))
    runs.insert(20,failed)
    raw=dict(arms=campaign.ARMS,mute='AlwaysUserMuteStrategy in every arm',pass_rule=campaign.PASS_RULE,
        runs=runs,completed=False,error=failed['observation']['error'])
    (tmp_path/'result.json').write_text(json.dumps(raw))
    def unexpected(*args,**kwargs):pytest.fail('Completed failed turns must not be replaced')
    monkeypatch.setattr(campaign.subprocess,'run',unexpected)
    assert campaign.run(tmp_path,resume=True)==0
    result=json.loads((tmp_path/'result.json').read_text())
    assert result['completed'] and result['runs']==runs
    assert result['retained_errors']==['Unscored calibration failed']


def test_resume_rejects_pre_mute_comparison(tmp_path):
    (tmp_path/'result.json').write_text(json.dumps(dict(arms=campaign.ARMS,runs=[],completed=False)))
    with pytest.raises(AssertionError,match='speaking-state mute'):
        campaign.run(tmp_path,resume=True)
