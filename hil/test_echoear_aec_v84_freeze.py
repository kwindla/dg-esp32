"""Draft packaging contracts and fake-only adoption/launch checks; no bench."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from echoear_aec_bench import ROOT
from echoear_aec_host_gate import POLICY as HOST_POLICY
from echoear_aec_lull import POLICY,RULE,declared_rule
from freeze_aec_campaign_v8_4 import launch_script,host_guard_script
from test_aec_campaign import plan
from test_aec_link_soak_launch import launch


def test_only_v84_packaging_contract_fields_changed():
    old=json.loads(subprocess.check_output(['git','show','2f674e8:hil/plans/echoear-aec-accept-v1.json'],cwd=ROOT,text=True))
    new=plan()
    assert new['ongoing_reply_lull']==POLICY and declared_rule(new)==RULE
    assert new['status']=='draft' and new['qualification'] is False
    allowed={'implementation_status','pending_before_freeze','opportunity','gates',
             'report_columns','declaration_policy','review_of_record','ongoing_reply_lull'}
    assert set(new)==set(old)|{'ongoing_reply_lull'}
    for k in set(new)-allowed:assert new[k]==old[k],k
    a,b=copy.deepcopy(new['gates']),copy.deepcopy(old['gates'])
    for k in ('credited','voice_class_rule'):
        assert a['barge_in'].pop(k)!=b['barge_in'].pop(k)
    assert a==b # every numerical gate and every other gate field unchanged
    a,b=copy.deepcopy(new['opportunity']),copy.deepcopy(old['opportunity'])
    a.pop('ongoing_reply_lull')
    for k in ('allowed_unscorable','denominator_rule'):assert a.pop(k)!=b.pop(k)
    assert a==b # strict definition, denominators, successes, expected counts unchanged
    assert new['report_columns'][:-2]==old['report_columns']
    a,b=copy.deepcopy(new['review_of_record']),copy.deepcopy(old['review_of_record'])
    extra=a['supporting_reviews_and_decisions'][-3:]
    assert [Path(x['path']).name for x in extra]==['fable-step-9-v8.4-spec-clarification.md','fable-step-9-v8.4-lull-prep-review.md','step-9-v8.4-freeze-draft-request.md']
    for x in extra:assert hashlib.sha256(Path(x['path']).read_bytes()).hexdigest()==x['sha256']
    a['supporting_reviews_and_decisions']=a['supporting_reviews_and_decisions'][:-3]
    assert a==b


def prepare(launch,host_guard=False):
    policy=dict(HOST_POLICY,connection='test-profile')
    launch['declaration'].write_text(json.dumps(dict(host_environment=policy)))
    digest=hashlib.sha256(launch['declaration'].read_bytes()).hexdigest()
    script=launch_script(digest,launch['declaration'],launch['output'],policy)
    launch['path'].write_text(host_guard_script(script) if host_guard else script)
    subprocess.run(['bash','-n',str(launch['path'])],check=True)
    return launch['declaration'].parent/'USER-ADOPTED',digest,script


@pytest.mark.parametrize('guard',[False,True])
@pytest.mark.parametrize('marker_kind',['absent','directory','symlink','dangling-symlink'])
def test_draft_refuses_before_all_environment_and_runner_commands(launch,guard,marker_kind):
    marker,_,script=prepare(launch,guard)
    if marker_kind=='directory':marker.mkdir()
    elif marker_kind=='symlink':marker.symlink_to(launch['declaration'])
    elif marker_kind=='dangling-symlink':marker.symlink_to(marker.with_name('absent'))
    result,calls=launch['run']()
    assert result.returncode==2 and 'USER-ADOPTED' in result.stderr
    assert calls==[] and not launch['output'].exists()
    assert script.index('readonly adoption_marker=')<script.index('cd ')<script.index('nmcli ')


def test_adopted_fixture_runs_exact_campaign08_command_through_existing_guards(launch):
    marker,digest,_=prepare(launch);marker.write_text('Synthetic test marker only')
    r,calls=launch['run'](FAKE_IW=f'Connected to {HOST_POLICY["bssid"]} (on wlp8s0)\n freq: 5180\n signal: -53 dBm\n')
    assert r.returncode==0,r.stderr
    uv=[c for c in calls if c['name']=='uv'];assert len(uv)==1
    from freeze_aec_link_soak import runner_command
    expected=runner_command(digest,launch['declaration'],launch['output'])[1:];expected.remove('--link-soak')
    assert uv[0]['args']==expected and not launch['output'].exists()


@pytest.mark.parametrize('failure',['hash','output','bssid','signal'])
def test_marker_does_not_bypass_existing_guards(launch,failure):
    marker,_,_=prepare(launch);marker.write_text('Synthetic test only')
    env=dict(FAKE_IW=f'Connected to {HOST_POLICY["bssid"]} (on wlp8s0)\n freq: 5180\n signal: -53 dBm\n')
    if failure=='hash':launch['declaration'].write_text('{}')
    if failure=='output':launch['output'].mkdir()
    if failure=='bssid':env['FAKE_NM']='00:00:00:00:00:01'
    if failure=='signal':env['FAKE_IW']=env['FAKE_IW'].replace('-53','-61')
    r,calls=launch['run'](**env)
    assert r.returncode!=0 and not any(c['name']=='uv' for c in calls)


def test_host_guard_never_executes_runner_even_with_synthetic_marker(launch):
    marker,_,_=prepare(launch,True);marker.write_text('Synthetic test only')
    r,calls=launch['run'](FAKE_IW=f'Connected to {HOST_POLICY["bssid"]} (on wlp8s0)\n freq: 5180\n signal: -53 dBm\n')
    assert r.returncode==0 and 'runner NOT EXECUTED' in r.stdout
    assert not any(c['name']=='uv' for c in calls) and not launch['output'].exists()


def test_retained_playback_config_reuses_bound_public_evidence():
    from freeze_aec_campaign_v8_4 import retained_playback_config,PRIOR_DECLARATION_SHA256
    config,source=retained_playback_config(plan())
    assert config==plan()['candidate']['playback_config']
    assert source['declaration_sha256']==PRIOR_DECLARATION_SHA256
    assert not source['local_build_read'] and not source['candidate_rebuild_required']
    p=plan();p['candidate']['playback_config']['config']['CONFIG_DG_PLAYBACK_HIGHPASS_HZ']=400
    with pytest.raises(ValueError,match='config changed'):retained_playback_config(p)
    p=plan();p['elf_sha256']='wrong'
    with pytest.raises(ValueError,match='ELF mismatch'):retained_playback_config(p)


def test_retained_playback_declaration_cannot_be_substituted(tmp_path,monkeypatch):
    import freeze_aec_campaign_v8_4 as freeze
    source=tmp_path/'freeze-v8.3.1';source.mkdir()
    (source/'declaration-v8.3.1.json').write_text('{}')
    monkeypatch.setattr(freeze,'BASE',tmp_path)
    with pytest.raises(ValueError,match='declaration changed'):freeze.retained_playback_config(plan())
