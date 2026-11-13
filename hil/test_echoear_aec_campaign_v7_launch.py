import hashlib
import json
import re
import subprocess
import pytest
from test_aec_link_soak_launch import launch
from freeze_aec_campaign_v7 import launch_script
from freeze_aec_link_soak import runner_pattern
from echoear_aec_host_gate import POLICY


def setup(launch):
    policy=dict(POLICY,connection='test-profile')
    launch['declaration'].write_text(json.dumps(dict(host_environment=policy)))
    digest=hashlib.sha256(launch['declaration'].read_bytes()).hexdigest()
    script=launch_script(digest,launch['declaration'],launch['output'],policy)
    launch['path'].write_text(script)
    return digest,script


@pytest.mark.parametrize('signal,success',[(-60,True),(-59,True),(-60.1,False),(None,False)])
def test_campaign_launch_full_host_guard_before_exec(launch,signal,success):
    digest,script=setup(launch)
    link='Connected to '+POLICY['bssid']+' (on wlp8s0)\n freq: 5260\n'
    if signal is not None:link+=f' signal: {signal} dBm\n'
    result,calls=launch['run'](FAKE_IW=link)
    assert (result.returncode==0)==success,result.stderr
    uv=[c for c in calls if c['name']=='uv']
    assert bool(uv)==success
    if success:
        assert '--link-soak' not in uv[0]['args']
        assert uv[0]['args'][-4:]==['--declaration-sha256',digest,str(launch['declaration']),str(launch['output'])]
        assert uv[0]['env']['PYTHONUNBUFFERED']=='1'
    assert not launch['output'].exists()


@pytest.mark.parametrize('fault',['hash','existing','dangling'])
def test_campaign_refuses_output_and_hash_before_host_queries(launch,fault):
    setup(launch)
    if fault=='hash':launch['declaration'].write_text('{}')
    elif fault=='existing':launch['output'].mkdir()
    else:launch['output'].symlink_to(launch['output'].with_name('missing'))
    result,calls=launch['run']()
    assert result.returncode and not calls


def test_stop_pattern_matches_only_frozen_python_child(launch):
    digest,script=setup(launch)
    pattern=runner_pattern(digest,launch['declaration'],launch['output']).replace('--link-soak ','')
    argv='python ../hil/echoear_aec_campaign.py --declaration-sha256 '+digest+' '+str(launch['declaration'])+' '+str(launch['output'])
    for text,match in [(argv,True),('/venv/bin/'+argv,True),('uv run '+argv,False),(argv+' extra',False),(argv.replace(digest,'0'*64),False)]:
        p=subprocess.run(['grep','-E',pattern],input=text+'\n',text=True,capture_output=True)
        assert (p.returncode==0)==match
