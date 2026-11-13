"""Blocked Nova freeze is reviewable without authorizing any hardware access."""
import copy
import json
from pathlib import Path
import subprocess
import pytest
import freeze_aec_nova_live as f


def plan():
    folder=Path(__file__).with_name('plans')
    return f.make_plan(json.loads((folder/'echoear-aec-accept-v8.5.json').read_text()),json.loads((folder/'echoear-aec-accept-v1.json').read_text()))


def test_three_separate_denominators_and_original_templates_unchanged():
    p=plan();assert f.validate_draft(p)['preparation_valid']
    assert len(p['cases'])==36 and len(p['spares'])==19 and len(p['controls'])==2
    assert not p['qualification'] and not p['campaign_authorized']
    assert not p['nova_live']['preservation']['gate']
    assert p['nova_live']['sentence_split_regression']['counts_toward_barge_denominator'] is False
    assert all(not c.get('capture_group') for c in p['cases'] if c['kind']=='echo')
    assert len([c for c in p['cases'] if c['aec_state']=='cold'])==1
    assert p['nova_live']['measured_bot_preflight'] is None
    assert 'STALE' in p['preflight_contract']['real_reply']['status']


@pytest.mark.parametrize('mutation',['authorize','omit-blocker','profile','denominator','normal-gate','host-gate','fake-preflight','implementation'])
def test_incomplete_contract_cannot_be_promoted(mutation):
    p=plan()
    if mutation=='authorize':p['campaign_authorized']=True
    elif mutation=='omit-blocker':p['pending_before_freeze'].pop()
    elif mutation=='profile':p['bot_config']['ECHOEAR_STT_PROFILE']='demo'
    elif mutation=='denominator':p['cases']=[c for c in p['cases'] if c['id']!='qualification-normal-sentence-10']
    elif mutation=='normal-gate':next(c for c in p['cases'] if c.get('voice_class')=='normal' and c['kind']=='stop')['report_only']=False
    elif mutation=='host-gate':p['controls'][0]['report_only']=False
    elif mutation=='fake-preflight':p['nova_live']['measured_bot_preflight']={'passed':True}
    elif mutation=='implementation':p['nova_live']['implementation_ready']=True
    with pytest.raises(ValueError):f.validate_draft(p)


def test_launcher_refuses_before_queries_and_without_running_python(tmp_path):
    script=f.blocked_launcher()
    assert 'nmcli' not in script and 'python' not in script and 'uv ' not in script
    result=subprocess.run(['/bin/bash'],input=script,text=True,capture_output=True,cwd=tmp_path,env={'PATH':'/nonexistent'})
    assert result.returncode==2 and 'BLOCKED' in result.stderr
    assert list(tmp_path.iterdir())==[]


def test_existing_acquisition_validation_rejects_draft_before_hardware():
    from echoear_aec_campaign import validate_campaign
    with pytest.raises(ValueError,match='Draft specification'):
        validate_campaign(plan())
