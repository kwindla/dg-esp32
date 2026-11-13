import copy
import json
from types import SimpleNamespace
import pytest
import echoear_aec_host_gate as gate
import echoear_aec_campaign as campaign
from test_aec_host_wifi_diagnostics import raw


def good():
    value=dict(link=dict(available=True,connected=True,bssid=gate.BSSID,frequency_mhz=5260,signal_dbm=-60),journal_available=True,supplicant_state_changes=0)
    return value,raw(gate.BSSID)


@pytest.mark.parametrize('fault',[None,'escaped-lock','no-lock','wrong-lock','lock-error','bssid','disconnected','signal','signal-missing','freq','journal','journal-count'])
def test_boundaries_fail_closed(fault):
    v,k=good()
    if fault=='escaped-lock':k['stdout']=gate.BSSID.replace(':',r'\:')
    if fault=='no-lock':k['stdout']=''
    if fault=='wrong-lock':k['stdout']='aa:bb:cc:dd:ee:ff'
    if fault=='lock-error':k['available']=False
    if fault=='bssid':v['link']['bssid']='aa:bb:cc:dd:ee:ff'
    if fault=='disconnected':v['link']['connected']=False
    if fault=='signal':v['link']['signal_dbm']=-60.01
    if fault=='signal-missing':v['link']['signal_dbm']=None
    if fault=='freq':v['link']['frequency_mhz']=None
    if fault=='journal':v['journal_available']=False
    if fault=='journal-count':v['supplicant_state_changes']=1
    r=gate.boundary_verdict(v,k,gate.POLICY)
    assert r['passed']==(fault is None) and not r['credit'] and r['failure_class']=='harness/environment'


def events(cycles,offset=0):
    p=gate.CADENCE
    return [dict(kind='qualifying-uplink',window_s=[p['phase_s']+n*p['period_s']+offset]*2) for n in cycles]


@pytest.mark.parametrize('cycles,offset,passed',[([1],0,True),([1,1],0,True),([1,3],0,True),([1,2],0,False),([1,2],-5,False),([1,2],20,False),([1,2],-6,True),([1,2],21,True)])
def test_fixed_lattice_two_distinct_adjacent_cycles(cycles,offset,passed):
    assert gate.lattice(events(cycles,offset),gate.CADENCE,gate.CADENCE['host_boot_id'])['passed']==passed


def test_boot_change_and_ambiguous_interval_cannot_fabricate_cycles():
    assert not gate.lattice([],gate.CADENCE,'different-boot')['passed']
    e=events([1,2]);span=dict(kind='counter-possible-late-run',window_s=[e[0]['window_s'][0],e[1]['window_s'][1]])
    assert gate.lattice([span],gate.CADENCE,gate.CADENCE['host_boot_id'])['passed']
    e[1].update(kind='counter-possible-late-run',late=4,missing=4)
    assert gate.lattice(e,gate.CADENCE,gate.CADENCE['host_boot_id'])['passed']
    assert not gate.lattice(e,gate.LEGACY_CADENCE,gate.LEGACY_CADENCE['host_boot_id'])['passed']


def test_guard_is_sticky_and_uses_unescaped_nmcli(tmp_path,monkeypatch):
    hold=SimpleNamespace(out=tmp_path,p={'host_environment':dict(gate.POLICY,connection='test-profile')},result={},save=lambda:None)
    g=gate.HostGate(hold);calls=[];value,lock=good()
    monkeypatch.setattr(gate.wifi,'query',lambda argv:(calls.append(argv) or lock))
    monkeypatch.setattr(g.diag,'boundary',lambda folder,label:(folder/f'{label}-host-wifi.json').write_text(json.dumps(value)))
    g.boundary(tmp_path,'before')
    value['link']['signal_dbm']=-61
    with pytest.raises(campaign.EnvironmentalPause):g.boundary(tmp_path,'after')
    value['link']['signal_dbm']=-50
    g.boundary(tmp_path,'hold-end',enforce=False)
    assert g.failed and not hold.result['host_environment']['passed']
    assert all(c[:3]==['nmcli','--escape','no'] for c in calls)
    assert campaign.exception_class(campaign.EnvironmentalPause())=='environmental-pause'


def test_cleanup_after_environment_pause_restores_and_releases(tmp_path,monkeypatch):
    h=campaign.BenchHold({},dict(id='qualification-01'),tmp_path);calls=[]
    h.touched=True;h.lock=SimpleNamespace(__exit__=lambda *a:calls.append('release'))
    h.host_guard=SimpleNamespace(failed=True,boundary=lambda *a,**k:calls.append('host-end'),stop=lambda:calls.append('sampler-stop'))
    monkeypatch.setattr(campaign,'finish_after_exception',lambda *a:None)
    monkeypatch.setattr(campaign,'flash',lambda *a:calls.append('restore'))
    monkeypatch.setattr(campaign,'boot',lambda *a:SimpleNamespace(close=lambda:calls.append('hash-verified')))
    h.__exit__(campaign.EnvironmentalPause,campaign.EnvironmentalPause('host gate'),None)
    assert calls==['restore','hash-verified','host-end','sampler-stop','release']
    assert h.result['restored_elf_sha256']==campaign.NORMAL_HASH and h.result['lock_released']
    assert not h.result['completed'] and h.result['error_class']=='environmental-pause'


def test_final_host_failure_stops_next_hold_after_cleanup(tmp_path,monkeypatch):
    h=campaign.BenchHold({},dict(id='qualification-01'),tmp_path);h.lock=SimpleNamespace(__exit__=lambda *a:None)
    h.host_guard=SimpleNamespace(failed=True,boundary=lambda *a,**k:None,stop=lambda:None)
    with pytest.raises(campaign.EnvironmentalPause):h.__exit__(None,None,None)
    assert h.result['lock_released'] and not h.result['completed']


def test_per_hold_guard_precedes_serial_or_mixer(tmp_path,monkeypatch):
    out=tmp_path/'hold';calls=[]
    lock=SimpleNamespace(__enter__=lambda:calls.append('lock'),__exit__=lambda *a:calls.append('unlock'))
    monkeypatch.setattr(campaign,'bench_lock',lambda:lock)
    class Fail:
        failed=True
        def __init__(self,h):pass
        def start(self):calls.append('host-guard');raise campaign.EnvironmentalPause('wrong BSSID')
        def boundary(self,*a,**k):pass
        def stop(self):pass
    monkeypatch.setattr(gate,'HostGate',Fail)
    monkeypatch.setattr(campaign,'resolve_jabra',lambda:pytest.fail('must not touch Jabra'))
    monkeypatch.setattr(campaign,'flash',lambda *a:pytest.fail('must not flash'))
    h=campaign.BenchHold(dict(host_environment=gate.POLICY),dict(id='qualification-01'),out)
    with pytest.raises(campaign.EnvironmentalPause):h.__enter__()
    assert calls==['lock','host-guard','unlock']
    assert h.result['error_class']=='environmental-pause' and h.result['lock_released']


def test_analysis_rechecks_raw_host_inputs_and_all_boundaries(tmp_path,monkeypatch):
    p=dict(gate.POLICY,connection='test-profile')
    hold=SimpleNamespace(out=tmp_path,p={'host_environment':p},result={},save=lambda:None)
    g=gate.HostGate(hold)
    def query(argv,timeout=2):
        if argv[0]=='iw':return raw('Connected to '+gate.BSSID+'\n freq: 5260\n signal: -50 dBm')
        if argv[0]=='nmcli':return raw(gate.BSSID)
        return raw('')
    monkeypatch.setattr(gate.wifi,'query',query)
    for label in ('hold-start','connected','disconnected','hold-end'):g.boundary(tmp_path,label)
    assert gate.audit(tmp_path,p)['passed']
    path=tmp_path/'connected-host-wifi.json';v=json.loads(path.read_text());v['link_command']['stdout']=v['link_command']['stdout'].replace('-50','-61');path.write_text(json.dumps(v))
    assert not gate.audit(tmp_path,p)['passed']
    path.unlink()
    assert not gate.audit(tmp_path,p)['passed']


def test_periodicity_reads_raw_rtp_and_perf_and_pauses_before_credit(tmp_path,monkeypatch):
    import echoear_aec_transport_environment as env
    p=gate.CADENCE;onsets=[p['phase_s']+n*p['period_s'] for n in (1,2)]
    rtp=tmp_path/'rtp';rtp.mkdir()
    packets=[dict(pc_id='pc',observed_ms=t*1000) for t in (onsets[0]-60,onsets[1]+30)]
    (rtp/'socket-rtp.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in packets))
    hold=SimpleNamespace(out=tmp_path,p={'host_environment':dict(gate.POLICY,connection='test-profile'),'first_hold_periodicity':p},s={'id':p['hold']},result={},save=lambda:None,
                         board=SimpleNamespace(lines_since=lambda cursor:[]))
    g=gate.HostGate(hold)
    # Detector thresholds are tested in transport_environment tests. Here verify
    # the live consumer persists a negative verdict and raises before scoring.
    monkeypatch.setattr(env,'uplink_excursion',lambda *a:dict(valid=True,excursions=[dict(start_s=t) for t in onsets]))
    original=gate.Path.read_text
    monkeypatch.setattr(gate.Path,'read_text',lambda self,*a,**kw:p['host_boot_id'] if str(self)=='/proc/sys/kernel/random/boot_id' else original(self,*a,**kw))
    with pytest.raises(campaign.EnvironmentalPause,match='304s recurrence'):g.periodicity()
    assert hold.result['periodicity']['passed'] is False
    retained=json.loads((tmp_path/'periodicity.json').read_text())
    assert retained['checks'][0]['consecutive_cycle_pairs']==[[1,2]]


def test_counter_run_split_across_perf_boundaries_report_only():
    samples=[]
    for event in events([1,2]):
        for late,missing in ((0,2),(1,2),(3,0)):
            samples.append(dict(event,kind='counter-possible-late-run',pc_id='pc',generation=1,late=late,missing=missing))
    result=gate.lattice(samples,gate.CADENCE,gate.CADENCE['host_boot_id'])
    assert result['passed'] and result['consecutive_cycle_pairs']==[]
    assert result['counter_evaluation']['consecutive_cycle_pairs']==[[1,2]]
    assert result['counter_evaluation']['report_only']
    assert not gate.lattice(samples,gate.LEGACY_CADENCE,gate.LEGACY_CADENCE['host_boot_id'])['passed']
    # Three total slots cannot manufacture a >=4-slot run.
    samples=[e for e in samples if e['late']!=1]
    assert gate.lattice(samples,gate.CADENCE,gate.CADENCE['host_boot_id'])['passed']


def test_real_cli_module_uses_same_environment_exception_identity(tmp_path,monkeypatch):
    import argparse
    import importlib.util
    import sys
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('__main__',Path(campaign.__file__))
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,'__main__',module)
    monkeypatch.setitem(sys.modules,'echoear_aec_campaign',campaign) # restore after test
    plan=tmp_path/'plan.json';plan.write_text('{}')
    called=[]
    def parse(parser):
        def fake_run(*args):
            from echoear_aec_campaign import EnvironmentalPause
            assert module.exception_class(EnvironmentalPause('host'))=='environmental-pause'
            assert sys.modules['echoear_aec_campaign'] is module
            called.append(True)
        module.run=fake_run
        return SimpleNamespace(link_soak=False,declaration_sha256=None,plan=plan,output=tmp_path/'absent')
    monkeypatch.setattr(argparse.ArgumentParser,'parse_args',parse)
    spec.loader.exec_module(module)
    assert called==[True] and not (tmp_path/'absent').exists()
