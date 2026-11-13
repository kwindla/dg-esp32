"""Execute generated shell guards against fake NM/iw/uv only; no bench access."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from freeze_aec_link_soak import ESPTOOL_PYTHON,LOCKED_BSSID,launch_script,validate_launch_script,runner_command


@pytest.fixture
def launch(tmp_path):
    declaration=tmp_path/'declaration.json';declaration.write_text('{}\n')
    digest=hashlib.sha256(declaration.read_bytes()).hexdigest()
    output=tmp_path/'absent-output'
    precondition=dict(connection='test-profile',interface='wlp8s0',bssid=LOCKED_BSSID)
    script=launch_script(digest,declaration,output,precondition)
    path=tmp_path/'launch.sh';path.write_text(script)
    commands=tmp_path/'commands';commands.mkdir();log=tmp_path/'calls.jsonl'
    fake='''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
with open(os.environ['FAKE_LOG'],'a') as f:
 f.write(json.dumps(dict(name=name,args=sys.argv[1:],env={k:os.environ.get(k) for k in ('ESPTOOL_PYTHON','PYTHONUNBUFFERED','PYTHONPATH')}))+'\\n')
if name=='nmcli':
 value=os.environ['FAKE_NM']
 if sys.argv[1:3]!=['--escape','no']:value=value.replace(':',chr(92)+':')
 print(value);sys.exit(int(os.environ.get('FAKE_NM_EXIT','0')))
elif name=='iw':
 print(os.environ['FAKE_IW']);sys.exit(int(os.environ.get('FAKE_IW_EXIT','0')))
elif name!='uv':raise RuntimeError(name)
'''
    for name in ('nmcli','iw','uv'):
        cmd=commands/name;cmd.write_text(fake);cmd.chmod(0o755)
    env=dict(os.environ,PATH=str(commands)+os.pathsep+os.environ['PATH'],FAKE_LOG=str(log),
             FAKE_NM=LOCKED_BSSID.upper(),FAKE_IW='Connected to '+LOCKED_BSSID+' (on wlp8s0)\n\tfreq: 5260')
    def run(**changes):
        result=subprocess.run(['bash',str(path)],env=dict(env,**changes),capture_output=True,text=True)
        calls=[json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return result,calls
    return dict(run=run,path=path,script=script,declaration=declaration,digest=digest,output=output,precondition=precondition)


def test_required_esptool_export_cannot_be_omitted(launch):
    def check(script):return validate_launch_script(script,launch['digest'],launch['declaration'],launch['output'],launch['precondition'])
    assert check(launch['script'])['passed']
    broken=launch['script'].replace('export ESPTOOL_PYTHON='+ESPTOOL_PYTHON+'\n','')
    with pytest.raises(ValueError,match='ESPTOOL_PYTHON'):check(broken)
    with pytest.raises(ValueError,match='frozen guarded command'):
        check(launch['script'].replace('set -euo pipefail','set -eu'))


def test_guarded_launch_exports_env_and_executes_exact_runner(launch):
    result,calls=launch['run']();assert result.returncode==0,result.stderr
    assert [c['name'] for c in calls]==['nmcli','iw','uv']
    assert calls[0]['args']==['--escape','no','-g','802-11-wireless.bssid','connection','show','test-profile']
    assert calls[1]['args']==['dev','wlp8s0','link']
    assert calls[2]['env']==dict(ESPTOOL_PYTHON=ESPTOOL_PYTHON,PYTHONUNBUFFERED='1',PYTHONPATH='../hil:.')
    assert calls[2]['args']==runner_command(launch['digest'],launch['declaration'],launch['output'])[1:]
    assert not launch['output'].exists()


@pytest.mark.parametrize('changes',[
    {'FAKE_NM':''},{'FAKE_NM':'00:00:00:00:00:01'},{'FAKE_NM_EXIT':'1'},
    {'FAKE_IW':'Not connected.'},{'FAKE_IW':'Connected to 00:00:00:00:00:01 (on wlp8s0)'},
    {'FAKE_IW_EXIT':'1'},
])
def test_mismatch_or_query_error_refuses_before_uv(launch,changes):
    result,calls=launch['run'](**changes)
    assert result.returncode!=0 and not any(c['name']=='uv' for c in calls)
    assert not launch['output'].exists()


@pytest.mark.parametrize('obstacle',['existing-output','dangling-output','changed-declaration'])
def test_output_or_digest_failure_precedes_environment_queries(launch,obstacle):
    if obstacle=='existing-output':launch['output'].mkdir()
    elif obstacle=='dangling-output':launch['output'].symlink_to(launch['output'].with_name('nonexistent'))
    else:launch['declaration'].write_text('{"changed":true}')
    result,calls=launch['run']()
    assert result.returncode!=0 and calls==[]


def test_default_nmcli_escaping_reproduces_the_live_refusal(launch):
    launch['path'].write_text(launch['script'].replace('nmcli --escape no -g','nmcli -g'))
    result,calls=launch['run']()
    assert result.returncode==2
    assert 'BSSID lock missing or wrong' in result.stderr
    assert [c['name'] for c in calls]==['nmcli']
