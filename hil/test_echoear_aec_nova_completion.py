"""Pure file/mocked regressions. No acquisition or vendor calls."""
import copy
from pathlib import Path

import pytest
import echoear_aec_nova_completion as c
import echoear_aec_nova_prestep as n
import echoear_aec_nova_preflight as a
from test_echoear_aec_nova_prestep import turn_fixture


@pytest.fixture(scope='module')
def retained():
    return c.partial()


@pytest.fixture
def plan(tmp_path,retained):
    analysis=tmp_path/'partial.json'; n.write(analysis,retained)
    p=c.plan_from(analysis)
    inputs=(n.SOURCE,n.COORDINATOR,c.PRIOR_PLAN,analysis,n.JOBS/'step-9-nova-prestep-02-request.md')
    p.update(inputs_sha256={str(f):n.sha(f) for f in inputs},prior_sources_sha256=c.manifest(c.PRIOR),
             runtime_sha256={},installed_packages_sha256={},prepared_from_commit='test')
    return p


def test_retained_real_abort_is_partial_not_whole_hold_pass(retained):
    assert all(retained['shared_retention_checks'].values())
    assert not retained['whole_hold_completed'] and not retained['complete_measurements']
    assert not retained['final_freeze_ready'] and not retained['qualification']
    assert retained['summary']['retained_acquisitions']==4
    assert retained['summary']['strict_complete_captures']==3
    assert retained['summary']['final_segment_delay_s']['count']==1
    assert retained['summary']['real_reply_pcm_s']['count']==2
    assert retained['summary']['real_reply_pcm_s']['maximum']==8.72
    stop=retained['measurements'][2]
    assert stop['retained_acquisition_usable'] and stop['complete_reply']
    assert stop['turn']['structure_complete'] and not stop['turn']['complete']
    assert stop['turn']['accepted_turns'][0]['final_segment_delay_s'] is None
    assert stop['turn']['accepted_turns'][0]['turn_completion_delay_s'] is None
    assert stop['turn']['accepted_turns'][0]['final_to_turn_delay_s']==pytest.approx(1.002002633)


def test_failed_normal_all_segments_visible_but_excluded(retained):
    failed=retained['measurements'][4]
    assert failed['use']=='excluded-transport-failure-report-only'
    assert not failed['retained_acquisition_usable'] and not failed['accepted_by_runner']
    assert failed['complete_reply'] and failed['quiet_tail_s']>2.25
    assert failed['turn']['accepted_turns'][0]['text']=='Please change the subject to cook cooking.'
    assert len(failed['turn']['accepted_turns'])==len(failed['turn']['llm_requests'])==1
    finals=[e['text'] for e in failed['turn']['segments'] if e['frame']=='TranscriptionFrame']
    assert finals==['Please change the subject to cook','cooking.']
    transport=failed['acquisition_audit']['checks']['transport']
    assert not transport['passed'] and transport['ge6_ended']
    assert transport['silence_delta']['post_plc_zeros']==4
    assert not failed['turn']['complete']
    assert retained['measurements'][5]['use']=='not-run'


def test_missing_vad_does_not_invent_delays_or_relax_old_complete():
    cell,events,recorded=turn_fixture()
    events=[e for e in events if not e.get('frame','').startswith('VADUser')]
    result=a.turn_measurements(cell,events,recorded)
    assert result['structure_complete'] and not result['complete'] and not result['vad_timing_available']
    assert result['accepted_turns'][0]['turn_completion_delay_s'] is None
    assert result['accepted_turns'][0]['final_to_turn_delay_s']==pytest.approx(.3)


def test_completion_exact_scope_and_unchanged_safety(plan):
    old=a.read(c.PRIOR_PLAN)
    assert [x['id'] for x in plan['cases']]==[c.WARMUP,*[x['id'] for x in old['cases'][4:]]]
    assert plan['cases'][1:]==old['cases'][4:]
    assert plan['cases'][0]['aec_state']=='cold-boot' and plan['cases'][0]['measurement_role']=='conditioning-only'
    for key in ('budget','image','gates','noise_gate','host_environment','main_session','vendor','profile',
                'measurement_policy','attempts_per_cell','reconnects','spares','bot_config','bot_port','rf_pre_gate_s'):
        assert plan[key]==old[key]
    assert plan['output']==str(c.OUTPUT)
    assert plan['cases'][-1]['initially_silent'] and plan['cases'][-1]['real_bot_reply']


@pytest.mark.parametrize('fault',['extra-cell','offset','budget','port','gates','warmup-credit','output','decision-input'])
def test_completion_mutation_rejected_before_hardware(plan,monkeypatch,fault):
    if fault=='extra-cell':plan['cases'].append(copy.deepcopy(plan['cases'][1]))
    if fault=='offset':plan['cases'][1]['stop_offset_s']=1.2
    if fault=='budget':plan['budget']['total_s']=1300
    if fault=='port':plan['bot_port']=7860
    if fault=='gates':plan['gates']={}
    if fault=='warmup-credit':plan['combination_policy']['current_ids'].append(c.WARMUP+'-a1')
    if fault=='output':plan['output']+='/other'
    if fault=='decision-input':plan['inputs_sha256'][str(n.JOBS/'step-9-user-decisions-2026-10-18-d.md')]='unused'
    monkeypatch.setattr(n,'validate_bindings',lambda *args:pytest.fail('reached runtime/image validation'))
    with pytest.raises(ValueError):n.validate(plan)


def test_live_decision_notes_are_not_dependencies(plan,monkeypatch):
    # Simulate later decision-file changes without touching either real file.
    original=Path.read_text
    def read(path,*args,**kwargs):
        if path.name in ('step-9-user-decisions-2026-10-18-c.md','step-9-user-decisions-2026-10-18-d.md'):
            pytest.fail('Opened mutable decision log')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    original_bytes=Path.read_bytes
    def read_bytes(path):
        if path.name in ('step-9-user-decisions-2026-10-18-c.md','step-9-user-decisions-2026-10-18-d.md'):
            pytest.fail('Hashed mutable decision log')
        return original_bytes(path)
    monkeypatch.setattr(Path,'read_bytes',read_bytes)
    def bindings(p):
        for name,digest in p['inputs_sha256'].items():assert n.sha(name)==digest
        return True
    monkeypatch.setattr(n,'validate_bindings',bindings)
    assert n.validate(plan)
    assert all('user-decisions' not in f for f in plan['inputs_sha256'])


def test_retained_manifest_rejects_mutation_before_launch(plan,monkeypatch):
    plan['prior_sources_sha256'][str(c.PRIOR/'result.json')]='changed'
    monkeypatch.setattr(n,'validate_bindings',lambda *a:pytest.fail('late validation'))
    with pytest.raises(ValueError,match='Retained source'):n.validate(plan)


def test_missing_gate_cannot_retain_cell(monkeypatch):
    p=a.read(c.PRIOR_PLAN)
    original=a.read
    def read(path,default=None):
        value=original(path,default)
        if Path(path).name=='capture-end.json':return {}
        return value
    monkeypatch.setattr(a,'read',read)
    value=c.acquisition_audit(p,c.PRIOR,p['cases'][0],[],-65,[])
    assert not value['passed']


@pytest.mark.parametrize('complete',[False,True])
def test_combined_fixed_slots_keep_failed_normal_and_warmup_out(plan,retained,monkeypatch,tmp_path,complete):
    new=[]
    for cell in plan['cases']:
        m=copy.deepcopy(retained['measurements'][3])
        m.update(id=cell['id']+'-a1',complete=True,accepted_by_runner=True)
        new.append(m)
    # A false turn in the warm-up and a paused split are measurement findings.
    new[0]['complete']=new[2]['complete']=False
    monkeypatch.setattr(a,'analyze',lambda *args,**kwargs:dict(measurements=new,complete_measurements=False))
    monkeypatch.setattr(c,'current_acquisition',lambda *args:dict(passed=complete,
        captures={m['id']:dict(passed=True) for m in new}))
    out=tmp_path/'future-run'
    value=c.combine(plan,out)
    assert not out.exists()
    assert len(value['measurements'])==6
    assert all(m['source_run']==str(c.PRIOR) for m in value['measurements'][:4])
    assert all(m['source_run']==str(out) for m in value['measurements'][4:])
    assert value['excluded_prior_capture']['id']==plan['combination_policy']['excluded_prior_id']
    assert value['conditioning_only']['id']==c.WARMUP+'-a1'
    assert all(m['id']!=c.WARMUP+'-a1' for m in value['measurements'])
    assert value['summary']['retained_acquisitions']==(6 if complete else 4)
    assert not value['complete_measurements']  # Stop VAD timing remains unavailable.
    assert not value['final_freeze_ready'] and len(value['source_runs'])==2
    assert value['measurements'][4]['strict_measurement_complete']==complete
    assert not value['measurements'][5]['strict_measurement_complete']


def test_generic_analyzer_tracks_three_expected_cells(plan,tmp_path):
    value=a.analyze(plan,tmp_path/'absent')
    assert len(value['measurements'])==value['summary']['expected_captures']==3
    assert not value['complete_measurements'] and not (tmp_path/'absent').exists()


def test_partial_analysis_never_queries_bench(monkeypatch):
    import host_nic
    import echoear_aec_host_gate as host
    import echoear_aec_campaign as campaign
    def forbidden(*args,**kwargs):pytest.fail('Host-only analysis attempted live access')
    monkeypatch.setattr(host_nic,'snapshot',forbidden)
    monkeypatch.setattr(host,'prelaunch',forbidden)
    monkeypatch.setattr(campaign,'bench_lock',forbidden)
    monkeypatch.setattr(campaign,'flash',forbidden)
    monkeypatch.setattr(campaign,'boot',forbidden)
    assert c.partial()['summary']['retained_acquisitions']==4


@pytest.mark.parametrize('fault',['other-abort','cleanup','missing-accepted-cell'])
def test_partial_exception_never_salvages_arbitrary_failed_holds(monkeypatch,fault):
    original=a.read
    def read(path,default=None):
        value=original(path,default)
        if Path(path)==c.PRIOR/'result.json':
            if fault=='other-abort':value['error']='RuntimeError: different failure'
            if fault=='cleanup':value['mixer_equal']=False
            if fault=='missing-accepted-cell':value['cases']=value['cases'][:-1]
        return value
    monkeypatch.setattr(a,'read',read)
    value=c.partial()
    assert value['summary']['retained_acquisitions']==0
    assert not any(m['retained_acquisition_usable'] for m in value['measurements'])


def test_completed_run02_retains_split_and_clean_normal_independently():
    p=a.read(n.BASE/'freeze-v8.6-nova-prestep-02/plan.json')
    value=c.combine(p,c.OUTPUT)
    assert value['current_acquisition']['passed']
    assert not value['current_hold_analysis']['complete_measurements']
    assert not value['current_hold_analysis']['generation_audit']['passed']
    assert value['summary']['retained_acquisitions']==6
    normal,paused=value['measurements'][4:]
    assert normal['retained_acquisition_usable'] and normal['strict_measurement_complete']
    assert paused['retained_acquisition_usable'] and not paused['strict_measurement_complete']
    assert [t['text'] for t in paused['turn']['accepted_turns']]==['Please change','the subject to cooking.']
    assert len(paused['turn']['llm_requests'])==2
    assert value['summary']['strict_complete_captures']==4
    assert not value['complete_measurements'] and not value['final_freeze_ready']


@pytest.mark.parametrize('fault',['cleanup','budget','capture','hold-resource','missing-witness'])
def test_run02_acquisition_failures_still_exclude(monkeypatch,fault):
    p=a.read(n.BASE/'freeze-v8.6-nova-prestep-02/plan.json')
    original=a.read
    def read(path,default=None):
        value=original(path,default)
        if Path(path)==c.OUTPUT/'result.json':
            if fault=='cleanup':value['lock_released']=False
            if fault=='hold-resource':value['resources']['all_passed']=False
            if fault=='missing-witness':value.pop('active_end_ns')
        if Path(path)==c.OUTPUT/'elapsed.json' and fault=='budget':value['seconds']=1201
        if Path(path)==c.OUTPUT/'nova-prestep-paused-sentence04-a1/result.json' and fault=='capture':
            value['checks']['transport']['passed']=False
        return value
    monkeypatch.setattr(a,'read',read)
    result=c.combine(p,c.OUTPUT)
    assert not result['current_acquisition']['passed']
    assert not any(m['retained_acquisition_usable'] for m in result['measurements'][4:])


def test_run02_timeline_preserves_interims_and_no_first_reply_audio():
    from echoear_aec_nova_prestep02_analysis import timeline
    result=timeline()
    pause=result['cases'][-1]
    segments=[e for e in pause['events'] if e['kind'] in ('InterimTranscriptionFrame','TranscriptionFrame')]
    assert [e['record']['text'] for e in segments]==['Please treat','Please change','the subject','the subject to cooking.']
    assert [e['kind'] for e in segments]==['InterimTranscriptionFrame','TranscriptionFrame']*2
    assert all(e['line']>0 and e['source'].endswith('events.jsonl') for e in segments)
    assert pause['contexts'][0]['transport_write_count']==0
    assert pause['contexts'][0]['first_non_silent_write'] is None
    assert pause['contexts'][1]['transport_write_count']==232
    assert len(result['smart_turn_log_observations'])==3
    assert all(s['probability'] is None and s['call_entry_monotonic_s'] is None for s in result['smart_turn_log_observations'])
    assert result['uplink'][0]['sample_offsets_contiguous']
    assert result['uplink'][0]['last_sample_end']==result['uplink'][0]['samples']


@pytest.mark.parametrize('run',[c.PRIOR,c.OUTPUT])
def test_analysis_refuses_output_inside_retained_runs(run):
    from echoear_aec_nova_prestep02_analysis import generate
    with pytest.raises(ValueError,match='outside retained'):generate(run/'new-analysis')
