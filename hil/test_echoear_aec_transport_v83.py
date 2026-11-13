import copy
import json
from pathlib import Path
import pytest
from echoear_aec_campaign_analysis import transport,campaign_verdict
from echoear_aec_transport_environment import piecewise_transport
from echoear_aec_transport_policy import RULE,LIMITS,validate_policy,burst_summary
from test_echoear_aec_histogram import bounds
from test_aec_link_soak_launch import launch
from freeze_aec_campaign_v8_3 import launch_script
from echoear_aec_host_gate import POLICY

BASE=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')


def window(missing=5,buckets=None):
    a,b=bounds(missing,buckets or [0,0,0,0,1,0])
    for p in (a,b):p['playout'].update(post_plc_zeros=0,starvation=0)
    return a,b


def test_retained_c06_transport_and_unchanged_v82():
    root=BASE/'campaign-06/qualification-01/qualification-01-stop-in-window-a1'
    if not root.exists():pytest.skip('retained evidence unavailable')
    a,b=[json.loads((root/(name+'.json')).read_text()) for name in ('before','after')]
    old=transport(a,b);new=transport(a,b,RULE)
    saved=json.loads((root/'result.json').read_text())['checks']['transport']
    assert old==saved # exact historical result, not merely the verdict
    assert not old['passed'] and new['passed']
    assert new['loss_fraction']==38/3470 and new['max_burst_upper']==5
    assert new['missing_run_ended_delta']==[10,2,0,0,1,0]
    assert new['open_missing_slots_upper']==0 and new['burst_report_only']


@pytest.mark.parametrize('missing,passed',[(40,True),(42,False)])
def test_loss_boundary_and_21_per_thousand(missing,passed):
    a,b=window(missing,[missing,0,0,0,0,0])
    r=transport(a,b,RULE)
    assert r['loss_fraction']==2*missing/4000 and r['passed']==passed


@pytest.mark.parametrize('fault',['ge6','zeros','starvation','absent','one_absent','short',
    'negative','decreasing','boolean','peer','generation','time','missing_silence','mirror'])
def test_hard_failures(fault):
    a,b=window()
    if fault=='ge6':b['playout']['missing_run_ended'][5]+=1
    if fault=='zeros':b['playout']['post_plc_zeros']=1
    if fault=='starvation':b['playout']['starvation']=1
    if fault in ('absent','one_absent'):a['playout'].pop('missing_run_ended')
    if fault=='absent':b['playout'].pop('missing_run_ended')
    if fault=='short':b['playout']['missing_run_ended'].pop()
    if fault=='negative':b['playout']['missing_run_ended'][0]=-1
    if fault=='decreasing':b['playout']['missing_run_ended'][0]=0
    if fault=='boolean':b['playout']['missing_run_ended'][0]=True
    if fault=='peer':b['pc_id']='q'
    if fault=='generation':b['generation']=2
    if fault=='time':b['device_us']=a['device_us']
    if fault=='missing_silence':b['playout'].pop('starvation')
    if fault=='mirror':b['post_plc_zeros']=1
    assert not transport(a,b,RULE)['passed']


def test_open_remainder_is_report_only_and_counters_are_deltas():
    a,b=window(9,[0]*6)
    for p in (a,b):p['playout'].update(post_plc_zeros=7,starvation=8)
    r=transport(a,b,RULE)
    assert r['passed'] and r['open_missing_slots_upper']==r['max_burst_upper']==9
    assert r['silence_delta']==dict(post_plc_zeros=0,starvation=0)
    assert not transport(a,b)['passed']


def test_piecewise_policy_and_probe_rule_remain_separate():
    a,b=window()
    assert not piecewise_transport([a,b],[])['passed']
    value=piecewise_transport([a,b],[],policy=RULE)
    assert value['passed'] and value['intervals'][0]['max_burst_upper']==5
    probe=dict(pc_id='p',generation=1,before_device_us=100,after_device_us=200,verdict=dict(passed=False))
    value=piecewise_transport([a,b],[],probe,RULE)
    assert not value['passed'] and value['intervals'][0]['rule']=='probe-own-rule-v1'
    assert value['intervals'][0]['per_cell_diagnostic']['passed']


def test_summary_keeps_buckets_burst_open_and_failed_attempts():
    r=transport(*window(),RULE)
    h=dict(id='hold',cases=[dict(id='cell',clean=True,transport=r)],
           harness_failures=[dict(id='retry',transport=r,witnesses=dict(raw=dict(checks=dict(transport=r))))],
           epoch_checks=[dict(pc_id='p',generation=1,checks=dict(transport=dict(intervals=[r],raw_first_to_last=r)))])
    summary=burst_summary([h])
    assert summary['report_only'] and len(summary['windows'])==5
    assert all(w['transport']['missing_run_ended_delta']==[0,0,0,0,1,0] and
               w['transport']['max_burst_upper']==5 and w['transport']['open_missing_slots_upper']==0
               for w in summary['windows'])
    from test_aec_campaign import plan,passing_holds
    p=plan();holds=passing_holds(p)
    p['gates']['transport']=dict(LIMITS)
    holds[0]['cases'][0]['transport']=r
    assert campaign_verdict(p,holds)['transport_burst_summary']['windows'][0]['transport']==r


def test_frozen_contract_keeps_every_other_gate():
    prior=BASE/'freeze-v8.2/declaration-v8.2.json'
    if not prior.exists():pytest.skip('retained declaration unavailable')
    old=json.loads(prior.read_text())
    # Historical transport transition; current v8.4 contract is checked separately.
    import subprocess
    new=json.loads(subprocess.check_output(['git','show','2f674e8:hil/plans/echoear-aec-accept-v1.json'],cwd=Path(__file__).resolve().parents[1],text=True))
    for k in ('noise_gate','cases','controls','reconnects','sessions','bot_config',
              'open_near_end_level','fixture_policy','rf','diagnostics',
              'link_probe',):
        assert old[k]==new[k],k
    assert {k:v for k,v in old['gates'].items() if k not in ('transport','missing_playback')}=={k:v for k,v in new['gates'].items() if k not in ('transport','missing_playback')}
    for k in ('firmware_commit','elf_sha256','image_bundle_sha256','review_build_record'):
        assert old['candidate'][k]==new['candidate'][k]
    validate_policy(new['gates']['transport'])
    for k in LIMITS:
        changed=dict(new['gates']['transport']);changed[k]='bad'
        with pytest.raises(ValueError):validate_policy(changed)
    with pytest.raises(ValueError):transport(*window(),policy='unknown')


@pytest.mark.parametrize('signal,success',[(-54,True),(-61,False)])
def test_v83_launch_guard(launch,signal,success):
    import hashlib
    policy=dict(POLICY,connection='test-profile')
    launch['declaration'].write_text(json.dumps(dict(host_environment=policy)))
    digest=hashlib.sha256(launch['declaration'].read_bytes()).hexdigest()
    launch['path'].write_text(launch_script(digest,launch['declaration'],launch['output'],policy))
    result,calls=launch['run'](FAKE_IW=f'Connected to {POLICY["bssid"]} (on wlp8s0)\n freq: 5180\n signal: {signal} dBm\n')
    assert (result.returncode==0)==success,result.stderr
    assert len([x for x in calls if x['name']=='uv'])==int(success)
    assert not launch['output'].exists()
