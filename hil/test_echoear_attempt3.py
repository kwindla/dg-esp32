"""Collector/handoff controller tests with explicit hardware tripwires."""
import copy
from contextlib import contextmanager
import json
from unittest.mock import Mock, patch

import pytest

import echoear_attempt3 as runner


def acquired(eligible=True):
    return dict(eligible=eligible,fatal=False,health=dict(passed=True),
                rf=dict(eligible=eligible,reasons=[] if eligible else ['tail'],link_tail=dict(tail_rate=0 if eligible else .02)),
                nic=dict(passed=True))


@pytest.fixture(autouse=True)
def host_only():
    with patch('subprocess.Popen',side_effect=AssertionError('No process/hardware in host tests')):
        yield


def test_admission_consumption_is_durable_before_single_launch(tmp_path):
    def launch():
        assert (tmp_path/'campaign-consumed.json').exists()
        assert len(runner.read_rows(tmp_path/'probes.jsonl'))==1
        return dict(passed=False,runs=[1,2,3])
    launcher=Mock(side_effect=launch)
    outcome=runner.transaction(tmp_path,1,lambda _:acquired(),launcher)
    assert outcome['action']=='campaign-complete' and not outcome['result']['passed']
    launcher.assert_called_once()
    with pytest.raises(FileExistsError):runner.transaction(tmp_path,1,lambda _:acquired(),launcher)
    launcher.assert_called_once()


@pytest.mark.parametrize('bad',['tail','nic','health','fatal','unknown_health','string_eligible'])
def test_admission_conjoins_health_nic_rf_and_exact_verdicts(tmp_path,bad):
    result=acquired()
    if bad=='tail':result['rf']['eligible']=False
    if bad=='nic':result['nic']['passed']=False
    if bad=='health':result['health']['passed']=False
    if bad=='fatal':result['fatal']=True
    if bad=='unknown_health':result['health']={}
    if bad=='string_eligible':result['eligible']='true'
    launch=Mock(side_effect=AssertionError('Unhealthy probe cannot launch'))
    outcome=runner.transaction(tmp_path,1,lambda _:result,launch,clock=lambda:100)
    assert outcome['action'] in ('deferred','fatal')
    assert not (tmp_path/'campaign-consumed.json').exists()
    row=runner.read_rows(tmp_path/'deferrals.jsonl')[0]
    assert row['scored'] is False and row['release_bench']
    if bad in ('health','fatal','unknown_health'):assert row['fatal'] and row['next_probe_utc_s'] is None
    else:assert row['next_probe_utc_s']==1900
    launch.assert_not_called()


def test_crash_retains_incomplete_intent_without_scored_launch(tmp_path):
    def crash(_):raise RuntimeError('capture failed')
    with pytest.raises(RuntimeError):runner.transaction(tmp_path,1,crash,Mock())
    assert (tmp_path/'probe-1/started.json').exists()
    assert not (tmp_path/'probes.jsonl').exists()


def wiring(tmp_path, result):
    plan=copy.deepcopy(runner.load_plan());plan['campaign']['output']=str(tmp_path/'attempt-3')
    binding=dict(normal_elf_sha256='9936da45',budget='unused',rf_execution=dict(root=str(tmp_path.resolve())))
    state=dict(held=False,holds=0,launches=0)
    @contextmanager
    def bench():
        assert not state['held'];state.update(held=True,holds=state['holds']+1)
        try:yield
        finally:state['held']=False
    @contextmanager
    def assets():
        assert state['held'];yield
    def collect(*_):assert state['held'];return result
    def launch(*_):
        assert state['held'] and (tmp_path/'campaign-consumed.json').exists()
        state['launches']+=1;return dict(passed=True)
    patches=[patch.object(runner,'load_plan',return_value=plan),
        patch('echoear_item2_campaign.verified_binding',return_value=(binding,tmp_path)),
        patch('echoear_item2_campaign.flash_image',return_value='fake-by-id'),
        patch('step9a_echoear.bench',bench),patch('step9a_echoear.check_esptool'),
        patch('step9a_assets.asset_server',assets),patch.object(runner,'collect_probe',side_effect=collect),
        patch.object(runner,'launch_campaign',side_effect=launch)]
    return patches,state


@contextmanager
def patched_all(patches):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:stack.enter_context(p)
        yield


def test_actual_run_next_keeps_one_hold_across_probe_and_campaign_then_stops(tmp_path):
    patches,state=wiring(tmp_path,acquired())
    with patched_all(patches):
        result=runner.run_next(tmp_path/'binding.json',tmp_path)
        assert result['action']=='campaign-complete'
        assert state==dict(held=False,holds=1,launches=1)
        assert runner.run_next(tmp_path/'binding.json',tmp_path)['action']=='stop'
        assert state['launches']==1 and state['holds']==1


def test_actual_run_next_releases_hold_during_deferral_without_early_probe(tmp_path):
    patches,state=wiring(tmp_path,acquired(False))
    with patched_all(patches):
        assert runner.run_next(tmp_path/'binding.json',tmp_path)['action']=='deferred'
        assert state==dict(held=False,holds=1,launches=0)
        decision=runner.run_next(tmp_path/'binding.json',tmp_path)
        assert decision['action']=='wait' and decision['release_bench']
        assert state['holds']==1


def test_ended_hold_admission_cannot_launch_after_restart(tmp_path):
    runner.append(tmp_path/'probes.jsonl',dict(eligible=True))
    patches,state=wiring(tmp_path,acquired())
    with patched_all(patches):
        with pytest.raises(RuntimeError,match='ended hold'):runner.run_next(tmp_path/'binding.json',tmp_path)
        assert state['holds']==0 and state['launches']==0


def test_incomplete_probe_or_existing_campaign_cannot_replay(tmp_path):
    (tmp_path/'probe-1').mkdir();patches,state=wiring(tmp_path,acquired())
    with patched_all(patches):
        with pytest.raises(RuntimeError,match='Incomplete probe'):runner.run_next(tmp_path/'binding.json',tmp_path)
        (tmp_path/'probe-1').rmdir();(tmp_path/'attempt-3').mkdir()
        with pytest.raises(RuntimeError,match='already exists'):runner.run_next(tmp_path/'binding.json',tmp_path)
        assert state['holds']==0 and state['launches']==0
