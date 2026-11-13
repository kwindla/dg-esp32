from copy import deepcopy
from echoear_acoustic_qualification import gate,PROFILE
from playback_quality import TRANSPORT_COUNTERS


def sample():
    perf=dict.fromkeys(TRANSPORT_COUNTERS,0);perf.update(playout_schema=2,generation=1,pc_id='a')
    run=dict(passed=True,room_baseline=dict(noise_contaminated=False),
        gain_readback=dict(protection=PROFILE,analog_db=30,digital_percent=125,register=0x1a),
        mic_headroom=dict(passed=True,unprotected_headroom_db=4),mic_clip_gate=dict(passed=True),qualification_volume_relative_db=0,
        perf_acquisition_gate=dict(passed=True),before_perf=perf,after_perf=deepcopy(perf))
    return dict(runs=[deepcopy(run) for _ in range(5)],reruns=[],link_tail=dict(passed=True),display_monitor=dict(passed=True))


def test_fresh_five_turn_population_passes():
    assert gate(sample(),['a'])['passed']


def test_partial_retaken_noisy_or_incomplete_population_cannot_qualify():
    for case in ('four','retake','noise','gain','protection','headroom','clips','volume','queries','loss','drift','uplink','display'):
        r=sample();turn=r['runs'][0]
        if case=='four':r['runs'].pop()
        if case=='retake':r['reruns']=[{}]
        if case=='noise':turn['room_baseline']['noise_contaminated']=True
        if case=='gain':turn['gain_readback']['digital_percent']=200
        if case=='protection':turn['gain_readback']['protection']=None
        if case=='headroom':turn['mic_headroom']['unprotected_headroom_db']=2.9
        if case=='clips':turn['mic_clip_gate']['passed']=False
        if case=='volume':turn['qualification_volume_relative_db']=-6
        if case=='queries':turn['perf_acquisition_gate']['passed']=False
        if case=='loss':turn['after_perf']['lost']=1
        if case=='drift':turn['after_perf']['drift_inserts']=1
        if case=='uplink':r['link_tail']={}
        if case=='display':r['display_monitor']={}
        assert not gate(r,['a'])['passed'],case
    assert not gate(sample(),[])['passed']
    assert not gate(sample(),['a','b'])['passed']


def test_acoustic_quiet_policy_does_not_require_live_display_protocol(monkeypatch):
    from e2e_common import quiet_acquisition
    monkeypatch.delenv('ECHOEAR_STRICT_MERGE',raising=False)
    monkeypatch.setenv('ECHOEAR_ACOUSTIC_QUALIFICATION','1')
    assert quiet_acquisition()
    monkeypatch.delenv('ECHOEAR_ACOUSTIC_QUALIFICATION')
    assert not quiet_acquisition()
    monkeypatch.setenv('ECHOEAR_STRICT_MERGE','1')
    assert quiet_acquisition()


def test_plan_requires_protected_image_and_preserves_frozen_identity(tmp_path,monkeypatch):
    import json,pytest
    import echoear_acoustic_qualification as q
    monkeypatch.setattr(q,'ROOT',tmp_path)
    (tmp_path/'build/config').mkdir(parents=True)
    elf=tmp_path/'build/dg-esp32.elf';elf.write_bytes(b'MIC_PROTECTION schema=')
    config=tmp_path/'build/config/sdkconfig.json'
    config.write_text(json.dumps(dict(DG_BOARD_ECHOEAR=True,DG_MIC_GAIN_PERCENT=200)))
    budget=tmp_path/'budget.json';budget.write_text('{}')
    plan=tmp_path/'plan.json'
    with pytest.raises(ValueError):q.prepare(plan,budget)
    config.write_text(json.dumps(dict(DG_BOARD_ECHOEAR=True,DG_MIC_GAIN_PERCENT=125)))
    for path in ('hil/fixtures/prompt_speech.wav','server/fixtures/greeting.wav','server/fixtures/reply_pineapple.wav','server/fixtures/prompt_france.wav','server/fixtures/manifest.json'):
        p=tmp_path/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
    frozen=q.prepare(plan,budget)
    assert frozen['runs_each']==5 and not frozen['prior_acoustic_runs_counted']
    assert frozen['schema']==2
    with pytest.raises(FileExistsError):q.prepare(plan,budget)
    shared=tmp_path/'shared.elf';shared.write_bytes(elf.read_bytes())
    fixture=tmp_path/'extra.wav';fixture.write_bytes(b'original')
    shared_plan=tmp_path/'shared-plan.json'
    q.prepare(shared_plan,budget,shared,[fixture])
    assert q.verify_plan(shared_plan)[1]==shared
    fixture.write_bytes(b'changed')
    with pytest.raises(AssertionError,match='Reference changed'):q.verify_plan(shared_plan)
    monkeypatch.setenv('DG_BENCH_LOCK_FD','123')
    elf.write_bytes(b'changed')
    with pytest.raises(AssertionError,match='ELF changed'):q.execute(plan,tmp_path/'out','offline')
