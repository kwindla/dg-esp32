"""v8.5 must check the active NM UUID, not a different saved profile's lock."""
import copy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import echoear_aec_host_gate as gate
from freeze_aec_link_soak import launch_script
from test_aec_host_wifi_diagnostics import raw
from test_echoear_aec_host_gate import good


def witness():
    identity=gate.V85_POLICY['connection']+'\n'+gate.V85_POLICY['connection_uuid']
    return dict(before=raw(identity),after=raw(identity),profile=raw(identity+'\n'+gate.BSSID.upper()))


@pytest.mark.parametrize('fault',[None,'duplicate','uuid','profile-id','profile-uuid','unlocked',
                                  'switch','missing','query-error','stderr','extra-line'])
def test_active_identity_and_its_own_lock_fail_closed(fault):
    value,lock=good();w=witness()
    if fault=='duplicate':w['before']=w['after']=raw('daily-633 1\nother-uuid')
    if fault=='uuid':w['before']=w['after']=raw('daily-633\nother-uuid')
    if fault=='profile-id':w['profile']['stdout']=w['profile']['stdout'].replace('daily-633','daily-633 1')
    if fault=='profile-uuid':w['profile']['stdout']=w['profile']['stdout'].replace(gate.V85_POLICY['connection_uuid'],'other')
    if fault=='unlocked':w['profile']['stdout']='daily-633\n'+gate.V85_POLICY['connection_uuid']+'\n'
    if fault=='switch':w['after']=raw('daily-633 1\nother-uuid')
    if fault=='missing':w=None
    if fault=='query-error':w['before']['available']=False
    if fault=='stderr':w['profile']['stderr']='query failed'
    if fault=='extra-line':w['after']['stdout']+='\nunexpected'
    result=gate.boundary_verdict(value,lock,gate.V85_POLICY,w)
    assert result['passed']==(fault is None)
    assert result['active_profile']==w
    # Saved profile / association checks alone would accept the duplicate.
    assert gate.boundary_verdict(value,lock,gate.POLICY)['passed']


def test_read_only_queries_address_uuid_and_recheck_active(monkeypatch):
    calls=[];w=witness()
    def query(argv):
        calls.append(argv)
        return [w['before'],w['profile'],w['after']][len(calls)-1]
    monkeypatch.setattr(gate.wifi,'query',query)
    assert gate.active_profile_witness(gate.V85_POLICY)==w
    assert calls[0]==calls[2]==['nmcli','--escape','no','-g','GENERAL.CONNECTION,GENERAL.CON-UUID','device','show','wlp8s0']
    assert calls[1][-4:]==['connection','show','uuid',gate.V85_POLICY['connection_uuid']]
    assert all('up' not in c and 'modify' not in c for c in calls)


def test_boundary_retains_and_audits_active_profile(tmp_path,monkeypatch):
    h=SimpleNamespace(out=tmp_path,p={'host_environment':gate.V85_POLICY},result={},save=lambda:None)
    g=gate.HostGate(h);w=witness()
    def query(argv,**kw):
        if argv[0]=='iw':return raw('Connected to '+gate.BSSID+'\n freq: 5260\n signal: -50 dBm')
        if argv[0]=='nmcli':
            if 'GENERAL.CONNECTION,GENERAL.CON-UUID' in argv:return w['before']
            if 'connection.id,connection.uuid,802-11-wireless.bssid' in argv:return w['profile']
            return raw(gate.BSSID)
        return raw('')
    monkeypatch.setattr(gate.wifi,'query',query)
    for label in ('hold-start','connected','disconnected','hold-end'):g.boundary(tmp_path,label)
    assert gate.audit(tmp_path,gate.V85_POLICY)['passed']
    path=tmp_path/'connected-host-gate.json';saved=json.loads(path.read_text())
    assert saved['active_profile']==w
    saved['active_profile']['after']=raw('daily-633 1\nother-uuid');path.write_text(json.dumps(saved))
    assert not gate.audit(tmp_path,gate.V85_POLICY)['passed']


@pytest.mark.parametrize('mode',['good','duplicate','unlocked','switch','query-error'])
def test_generated_launch_refuses_before_runner_exec(tmp_path,mode):
    # Entire executable PATH is fake. No live nmcli, iw, uv, serial or bot.
    import freeze_aec_link_soak as freeze
    old=freeze.ROOT;freeze.ROOT=tmp_path
    try:script=launch_script('a'*64,tmp_path/'declaration.json',tmp_path/'absent',gate.V85_POLICY)
    finally:freeze.ROOT=old
    (tmp_path/'server').mkdir();bin=tmp_path/'bin';bin.mkdir()
    shim='''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
p=Path(os.environ['CALLS']);calls=json.loads(p.read_text()) if p.exists() else []
calls.append([name]+sys.argv[1:]);p.write_text(json.dumps(calls))
mode=os.environ['MODE'];uid=os.environ['UUID']
if name=='nmcli':
 if mode=='query-error':sys.exit(1)
 if 'GENERAL.CONNECTION,GENERAL.CON-UUID' in sys.argv:
  n=sum('GENERAL.CONNECTION,GENERAL.CON-UUID' in c for c in calls)
  print('daily-633 1\\nother' if mode=='duplicate' or (mode=='switch' and n==2) else 'daily-633\\n'+uid)
 else:print('' if mode=='unlocked' else 'B4:20:46:F4:EA:E5')
if name=='iw':print('Connected to b4:20:46:f4:ea:e5')
'''
    for name in ['nmcli','iw','uv','sha256sum']:
        f=bin/name;f.write_text(shim);f.chmod(0o755)
    env=dict(os.environ,PATH=str(bin)+os.pathsep+os.environ['PATH'],MODE=mode,UUID=gate.V85_POLICY['connection_uuid'],CALLS=str(tmp_path/'calls.json'))
    result=subprocess.run(['bash'],input=script,text=True,env=env,capture_output=True)
    calls=json.loads((tmp_path/'calls.json').read_text())
    assert (result.returncode==0)==(mode=='good'),result.stderr
    assert any(c[0]=='uv' for c in calls)==(mode=='good')
    if mode=='good':assert any(c[-4:]==['connection','show','uuid',gate.V85_POLICY['connection_uuid']] for c in calls)
    assert not (tmp_path/'absent').exists()
