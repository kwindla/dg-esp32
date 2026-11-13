import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import echoear_aec_nova_live as live


@pytest.fixture
def plan():return live.plan_from()


@pytest.fixture
def plan87():return live.plan_from(live.V87)


EXTRAS={'inputs_sha256','runtime_sha256','prepared_from_commit','installed_packages_sha256'}


def good_hold(p):
    cases=[]
    for c in p['cases']+p['controls']:
        cases.append(dict(c,id=c['id']+'-a1',cell_id=c['id'],clean=True,
            opportunity='eligible',barge_success=True,acoustic_passed=True,
            accepted_false_turns=0,turn_events={'interruptions':0}))
    return dict(id='qualification-01',passed=True,acceptance_credit=True,
        nova_profile={'passed':True},cases=cases)


def test_fixed_contract_and_all_original_gates(plan):
    old=json.loads(live.SOURCE.read_text())
    assert plan['gates']==old['gates'] and plan['noise_gate']==old['noise_gate']
    assert plan['cases']==old['cases'] and plan['spares']==old['spares']
    assert plan['controls'][:-1]==old['controls']
    assert plan['budget']==dict(active_s=3240,restore_reserve_s=360,total_s=3600,minimum_cell_remaining_s=210)
    assert len(plan['cases'])+len(plan['controls'])==39
    assert plan['time_estimate']['v85_budget']['rtp_evidence_max_packets']>=3600*100
    assert not plan['nova_live']['sentence_split_regression']['gate']
    assert not plan['qualification'] and not plan['nova_live']['preservation']['gate']
    assert plan['preflight_contract']['real_reply']['max_s']==9.36
    assert not any('user-decisions' in f for f in live.input_manifest(plan))
    from demo_policy import profile_manifest
    assert plan['profile']==profile_manifest('dev')
    assert plan['nova_live']['measured_bot_preflight']['strict_turn_timing_summary']['final_segment_delay_s']['count']==2


@pytest.mark.parametrize('mutation',['normal-gate','spare','pause','budget','vendor','profile','host'])
def test_contract_mutations_refused(plan,mutation):
    if mutation=='normal-gate':next(c for c in plan['cases'] if c['kind']=='sentence' and c['voice_class']=='normal')['report_only']=True
    elif mutation=='spare':plan['spares'].pop()
    elif mutation=='pause':plan['controls'][-1]['report_only']=False
    elif mutation=='budget':plan['budget']['total_s']=4000
    elif mutation=='vendor':plan['vendor']['version']='new'
    elif mutation=='profile':plan['bot_config']['ECHOEAR_STT_PROFILE']='demo'
    else:plan['controls'][0]['timing_report_only']=False
    with pytest.raises(ValueError,match='contract'):live.validate(plan)


def test_three_separate_gates_no_pooling_and_pause_never_fills(plan):
    h=good_hold(plan)
    # Real classifier uses the strict opportunity name.
    for r in h['cases']:r['opportunity']='eligible'
    assert live.verdict(plan,[h])['passed']
    normal=[r for r in h['cases'] if r['kind']=='sentence' and r['voice_class']=='normal']
    normal[0]['barge_success']=False
    assert live.verdict(plan,[h])['passed']
    normal[1]['barge_success']=False
    assert not live.verdict(plan,[h])['passed']
    h=good_hold(plan)
    for r in h['cases']:r['opportunity']='eligible'
    h['cases']=[r for r in h['cases'] if r['cell_id']!=normal[0]['cell_id']]
    assert not live.verdict(plan,[h])['passed']


def test_report_only_findings_and_echo_both_volumes(plan):
    h=good_hold(plan)
    for r in h['cases']:r['opportunity']='eligible'
    for r in h['cases']:
        if r.get('report_only'):r.update(acoustic_passed=False,barge_success=False)
    assert live.verdict(plan,[h])['passed']
    h['cases']=[r for r in h['cases'] if r['cell_id']!=live.PAUSED]
    assert live.verdict(plan,[h])['passed']
    next(r for r in h['cases'] if r['kind']=='echo' and r['volume_db']==0)['turn_events']['interruptions']=1
    assert not live.verdict(plan,[h])['passed']


def test_spares_never_cross_voice_and_pause_does_not_replenish(plan):
    from echoear_aec_campaign import Schedule
    from echoear_aec_lull import declared_rule
    pause=plan['controls'][-1]
    normal=next(c for c in plan['cases'] if c['kind']=='sentence' and c['voice_class']=='normal')
    s=Schedule([pause,normal],plan['spares'],plan['noise_gate'],lull_policy=declared_rule(plan))
    item=s.next();s.finish(*item,True,{pause['id']:'unscorable-lull-cancelled'})
    assert not s.journal[-1]['replacements'] and not s.unresolved
    item=s.next();s.finish(*item,True,{normal['id']:'no-opportunity'})
    rep=s.journal[-1]['replacements'][0]
    assert next(c for c in plan['spares'] if c['id']==rep['spare'])['voice_class']=='normal'


def test_retained_pause_two_requests_survive_report_and_do_not_poison_generation_gate():
    from echoear_aec_accept import rows
    from echoear_aec_campaign_analysis import generation_audit
    out=live.BASE/'nova-prestep-02'
    events=rows(out/'evaluation/events.jsonl')
    recorded=[e for f in (out/'sessions').glob('*/events.jsonl') for e in rows(f)]
    own=[e for e in events if e.get('case_id')=='nova-prestep-paused-sentence04-a1']
    assert len(live.regression(own,recorded)['turns']['accepted_turns'])==2
    assert len(live.regression(own,recorded)['requests'])==2
    for e in own:e['case_id']=live.PAUSED+'-a1'
    assert not generation_audit(events,recorded)['passed']
    assert live.generation_gate(events,recorded,(),None,())['passed']
    events.append(dict(event='evaluation-error',pc_id=own[0]['pc_id'],case_id=live.PAUSED+'-a1',t=0))
    assert not live.generation_gate(events,recorded,(),None,())['passed']


def test_no_user_go_refuses_before_validation_and_output(tmp_path,monkeypatch):
    tmp_path=tmp_path.resolve();p=tmp_path/'plan.json';p.write_text('{}')
    monkeypatch.setitem(live.V87,'destination',tmp_path)
    check=Mock(side_effect=AssertionError('validation must not run'))
    monkeypatch.setattr(live,'validate',check)
    with pytest.raises(ValueError,match='USER-GO'):live.run(p,tmp_path/'run',live.sha(p))
    check.assert_not_called();assert not (tmp_path/'run').exists()


def test_launcher_refuses_without_user_go_before_uv(tmp_path):
    p=tmp_path/'plan.json';p.write_text('{}');digest=live.sha(p)
    script=live.launcher(p,digest,dict(live.V87,destination=tmp_path))
    assert str(live.V87['output']) in script and str(live.V86['output']) not in script
    result=subprocess.run(['/bin/bash'],input=script,text=True,capture_output=True,
        env={live.REVIEW_ENV:digest,'PATH':'/nonexistent'})
    assert result.returncode==2 and 'USER-GO required' in result.stderr
    assert list(tmp_path.iterdir())==[p]


def test_acquire_runs_exact_short_matrix_then_restores(tmp_path,monkeypatch,plan):
    import echoear_aec_campaign as campaign
    calls=[]
    class Hold:
        def __init__(self,p,s,out):self.out=out;self.result=dict(completed=True,restored_elf_sha256=live.NORMAL_HASH,mixer_equal=True,lock_released=True)
        def __enter__(self):calls.append('enter');return self
        def connect(self,cells,reconnects):assert len(cells)==58 and not reconnects;calls.append('connect')
        def link_probe(self):calls.append('probe')
        def finish(self):calls.append('finish')
        def __exit__(self,*exc):calls.append('restore')
    def schedule(h,cases,spares,policy):
        assert len(cases)==39 and len(spares)==19;calls.append('schedule')
    monkeypatch.setattr(campaign,'run_schedule',schedule)
    live.acquire(plan,tmp_path/'run',None,Hold)
    assert calls==['enter','connect','probe','schedule','finish','restore']


def test_budget_stops_before_new_capture(plan):
    factory=live.hold_type(SimpleNamespace(remaining=lambda:209))
    h=factory(plan,plan['sessions'][0],Path('/unused'))
    with pytest.raises(live.n.diagnostic.BudgetExpired):h.capture({},1)


def test_profile_and_hold_failure_block_credit(plan):
    h=good_hold(plan)
    for r in h['cases']:r['opportunity']='eligible'
    h['nova_profile']['passed']=False
    assert not live.verdict(plan,[h])['passed']
    h['nova_profile']['passed']=True;h['passed']=False
    assert not live.verdict(plan,[h])['passed']


def test_v86_variant_rederives_original_final_plan():
    old=json.loads((live.V86['destination']/'plan.json').read_text())
    assert {k:v for k,v in old.items() if k not in EXTRAS}==live.plan_from(live.V86)


def test_v87_changes_only_candidate_identity(plan,plan87):
    changed={k for k in set(plan)|set(plan87) if plan.get(k)!=plan87.get(k)}
    assert changed=={'candidate','candidate_change','declaration_id','elf_sha256','freeze','image','output','user_go'}
    old,new=plan['candidate'],plan87['candidate']
    assert {k for k in set(old)|set(new) if old.get(k)!=new.get(k)}=={'firmware_commit','elf_sha256',
        'image_bundle_sha256','review_artifact_manifest','review_build_record','playback_config','playout'}
    assert {k for k in old['playback_config'] if old['playback_config'][k]!=new['playback_config'][k]}=={'elf_sha256','sdkconfig_header_sha256'}
    assert new['resolved_config']==old['resolved_config'] and new['playout']['target_ms']==140
    assert plan87['elf_sha256']==live.V87['candidate']['elf_sha256']!=live.n.diagnostic.CANDIDATE
    assert plan87['output']!=plan['output'] and plan87['schema']==plan['schema']
    assert plan87['candidate_change']['previous']['elf_sha256']==plan['elf_sha256']


@pytest.mark.parametrize('mutation',['elf','playout','gates','old-candidate','variant'])
def test_v87_contract_mutations_refused(plan87,mutation):
    if mutation=='elf':plan87['candidate']['elf_sha256']=plan87['candidate_change']['previous']['elf_sha256']
    elif mutation=='playout':plan87['candidate']['playout']['target_frames']=5
    elif mutation=='gates':plan87['gates']['echo']['integrated_reduction_min_db']=10
    elif mutation=='old-candidate':plan87.pop('freeze')
    else:plan87['freeze']='v9'
    with pytest.raises(ValueError,match='contract|variant'):live.validate(plan87)


def test_v87_review_build_must_show_only_playout_keys(tmp_path,monkeypatch):
    image=Path(live.V87['candidate']['image'])
    for name in ('manifest.json','review-build.json'):(tmp_path/name).write_bytes((image/name).read_bytes())
    monkeypatch.setitem(live.V87,'candidate',dict(live.V87['candidate'],image=tmp_path))
    assert live.plan_from(live.V87)['image']==str(tmp_path)
    review=json.loads((tmp_path/'review-build.json').read_text())
    review['resolved_config_comparison']['sdkconfig_h']['differing']['CONFIG_DG_ECHOEAR_AEC_GUARD_MS']=['400','300']
    (tmp_path/'review-build.json').write_text(json.dumps(review))
    with pytest.raises(ValueError,match='two playout keys'):live.plan_from(live.V87)


def test_v87_binds_decisions_by_bundle_copy_only(tmp_path,monkeypatch):
    monkeypatch.setitem(live.V87,'destination',tmp_path)
    p=live.plan_from(live.V87);copy_=tmp_path/'inputs'/live.DECISIONS.name
    assert p['candidate_change']['decisions_record']['path']==str(copy_)
    with pytest.raises(FileNotFoundError):live.input_manifest(p)
    copy_.parent.mkdir();copy_.write_bytes(live.DECISIONS.read_bytes())
    files=live.input_manifest(p)
    assert files[str(copy_)]==live.DECISIONS_SHA and str(live.DECISIONS) not in files
    copy_.write_text('edited')
    with pytest.raises(ValueError,match='decision copy'):live.input_manifest(p)


def test_run_refuses_plan_outside_declared_bundles(tmp_path):
    p=tmp_path/'plan.json';p.write_text('{}')
    with pytest.raises(ValueError,match='declared freeze bundle'):live.run(p,tmp_path/'run',live.sha(p))
    assert not (tmp_path/'run').exists()
