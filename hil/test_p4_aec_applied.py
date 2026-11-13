"""Historical replay, applied image/readback gates and diagnostic-only repeats."""
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import p4_aec_applied as applied
import p4_aec_product as product
import p4_aec_image_measure as image_measure
import p4_aec_measure as measure
import p4_aec_policy as policy
from test_p4_aec_measure import held_rig
from test_p4_aec_reselect import prompt_events
from make_bundle import digest

RECOMMENDATION=Path('/home/khkramer/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json')
SELECTED=dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024)


def manifest():
    return dict(elf_sha256='b'*64,boot_ref_gain_db=30,aec_enabled=True,
        p4_aec_calibration=dict(SELECTED,status='APPLIED',recommendation_sha256=digest(RECOMMENDATION),measured=False,
            calibration_status=copy.deepcopy(applied.CALIBRATION_STATUS),gain_selection=copy.deepcopy(applied.GAIN_SELECTION),
            guard_validation=copy.deepcopy(applied.GUARD_VALIDATION),delay_provenance=json.loads(RECOMMENDATION.read_text())['delay_provenance']))


def boot_lines():
    return ['I (1) dg-esp32: esp32p4 audio elf_sha256='+('b'*64),
        'MIC3_GAIN=0x1a analog_db=30',
        'UNQUALIFIED AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000',
        'READY transport-console']


def test_real_recommendation_replays_from_immutable_git_after_worktree_removal():
    before=digest(RECOMMENDATION)
    row=applied.checked_recommendation(RECOMMENDATION)
    assert row['selected']==SELECTED and row['applied'] is False
    assert row['delay_provenance']['kind']=='decision-backed' and row['delay_provenance']['measured_pass'] is False
    assert digest(RECOMMENDATION)==before


def test_altered_recommendation_is_rejected_before_any_replay(tmp_path,monkeypatch):
    path=tmp_path/'forged.json';path.write_text('{}')
    run=Mock();monkeypatch.setattr(applied.subprocess,'run',run)
    with pytest.raises(ValueError,match='hash differs'):applied.checked_recommendation(path)
    assert not run.called


def test_applied_image_cannot_use_the_no_recommendation_baseline_path():
    with pytest.raises(ValueError,match='requires its recommendation'):product.applied_gate(manifest(),None)


def test_applied_pair_cannot_drop_its_recommendation_evidence(tmp_path):
    from test_p4_aec_product import pair_fixture
    left,right=pair_fixture(tmp_path)
    path=right/'image.json';image=json.loads(path.read_text())
    image['p4_aec_calibration']['status']='APPLIED';path.write_text(json.dumps(image))
    with pytest.raises(ValueError,match='lacks recommendation'):product.pair(left,right,product.smoke.BOUNDS_FILE)


def test_applied_boot_readback_requires_fresh_hash_and_all_values():
    row=applied.readback(boot_lines(),manifest(),SELECTED)
    assert row==dict(passed=True,elf_sha256='b'*64,**SELECTED)


@pytest.mark.parametrize('change',['elf','register','gain','guard','rearm','delay','missing-config'])
def test_wrong_or_missing_applied_readback_fails(change):
    lines=boot_lines()
    if change=='elf':lines[0]=lines[0].replace('b'*64,'a'*64)
    elif change=='register':lines[1]=lines[1].replace('0x1a','0x19')
    elif change=='gain':lines[1]=lines[1].replace('analog_db=30','analog_db=15')
    elif change=='guard':lines[2]=lines[2].replace('guard_ms=400','guard_ms=600')
    elif change=='rearm':lines[2]=lines[2].replace('rearm_ms=2000','rearm_ms=3000')
    elif change=='delay':lines[2]=lines[2].replace('samples=1024','samples=512')
    else:lines.pop(2)
    with pytest.raises(ValueError):applied.readback(lines,manifest(),SELECTED)


@pytest.mark.parametrize('change',[None,'old-elf','gain','guard','rearm','delay','stamp','status','float'])
def test_event_applied_gate_replays_and_checks_manifest_values(monkeypatch,change):
    row=json.loads(RECOMMENDATION.read_text());image=manifest()
    replay=Mock(return_value=row);monkeypatch.setattr(applied,'checked_recommendation',replay)
    if change=='old-elf':image['elf_sha256']=row['source_elf_sha256']
    elif change in ('gain','guard','rearm','delay'):
        key={'gain':'ref_gain_db','guard':'guard_ms','rearm':'rearm_ms','delay':'afe_delay_samples'}[change]
        image['p4_aec_calibration'][key]+=3
    elif change=='stamp':image['p4_aec_calibration']['recommendation_sha256']='c'*64
    elif change=='status':image['p4_aec_calibration']['status']='UNMEASURED'
    elif change=='float':image['p4_aec_calibration']['guard_ms']=400.
    if change:
        with pytest.raises(ValueError):product.applied_gate(image,RECOMMENDATION)
    else:
        result=product.applied_gate(image,RECOMMENDATION)
        assert result['selected']==SELECTED and result['delay_provenance']['measured_pass'] is False
    replay.assert_called_once_with(RECOMMENDATION)


@pytest.mark.parametrize('change',['missing-status','gain-measured','guard-measured','delay-measured','aggregate-measured',
    'old-aggregate-claim','repeated-trials','boolean-trials','physical-validated','integer-physical-status','missing-15d','delay-promoted'])
def test_applied_gate_requires_honest_per_field_labels(monkeypatch,change):
    row=json.loads(RECOMMENDATION.read_text());image=manifest();cal=image['p4_aec_calibration']
    monkeypatch.setattr(applied,'checked_recommendation',lambda *args:row)
    if change=='missing-status':cal.pop('calibration_status')
    elif change in ('gain-measured','guard-measured','delay-measured'):
        cal['calibration_status'][change.split('-')[0]]='measured'
    elif change=='aggregate-measured':cal['measured']=True
    elif change=='old-aggregate-claim':cal['gain_guard_measured']=True
    elif change=='repeated-trials':cal['gain_selection']['prompt_trials_per_gain']=5
    elif change=='boolean-trials':cal['gain_selection']['prompt_trials_per_gain']=True
    elif change=='physical-validated':cal['guard_validation']['physically_validated_in_window']=True
    elif change=='integer-physical-status':cal['guard_validation']['physically_validated_in_window']=0
    elif change=='missing-15d':cal['guard_validation'].pop('validation_owed')
    else:cal['delay_provenance']['measured_pass']=True
    with pytest.raises(ValueError):product.applied_gate(image,RECOMMENDATION)


def test_product_boot_performs_readback_before_acceptance(tmp_path,monkeypatch):
    monkeypatch.setattr(product.fix4.VoiceIO,'boot',lambda *args:0)
    io=product.ProductIO.__new__(product.ProductIO)
    io.out=tmp_path;io.manifest=manifest();io.args=SimpleNamespace(_applied_calibration=dict(selected=SELECTED))
    board=SimpleNamespace(lines_since=lambda at:[(i,0,line) for i,line in enumerate(boot_lines())])
    assert io.boot(board)==0 and io.applied_readback['ref_gain_db']==30
    assert (tmp_path/'boot.log').is_file()


@pytest.mark.parametrize('prompt',['france','speech'])
def test_double_talk_keeps_five_bad_trials_as_report_only(tmp_path,monkeypatch,prompt):
    import numpy as np
    spec=dict(id=prompt,expected='What is the capital of France?')
    plan=dict(profile='aec-normal',applied_recommendation='recommendation',fixtures=[dict(id='long'),spec])
    args=SimpleNamespace(prompt=prompt,out=tmp_path)
    original=dict(events=prompt_events('wrong transcript'),near_playback=dict(request_monotonic_s=2),
        bot_started_ns=1000000000,bot_stopped_ns=3000000000,capture={},before={},after={},complete=True)
    io=SimpleNamespace(configure=lambda *args:None,quiet=lambda *args:{},begin=lambda *args:None,
        reply=Mock(return_value=original))
    monkeypatch.setattr(image_measure,'image_delay',lambda *args:dict(kind='decision-backed',measured_pass=False))
    monkeypatch.setattr(measure,'held',lambda args,plan,body,**kwargs:body(io,None))
    monkeypatch.setattr(measure,'load_capture',lambda *args:np.zeros((8192,5),dtype='<i2'))
    monkeypatch.setattr(policy,'reference_gain',lambda *args:30)
    monkeypatch.setattr(policy,'clip_evidence',lambda *args:dict(passed=False))
    monkeypatch.setattr(policy,'echo_score',lambda *args:dict(available=False,median_erle_db=-5))
    monkeypatch.setattr(policy,'select_gain',Mock(side_effect=AssertionError('must never select')))
    row=image_measure.double_talk(args,plan)
    assert row['report_only'] and not row['qualification'] and len(row['trials'])==5
    assert io.reply.call_count==5 and all(t['accuracy']<.9 and t['median_erle_db']==-5 for t in row['trials'])
    assert all('transcript' in t and 'interruption' in t for t in row['trials'])


def test_double_talk_finalization_keeps_scored_payload_and_restores(held_rig):
    args,plan,state,IO=held_rig;args.action='double-talk';state.fail='finish'
    row=measure.held(args,plan,lambda *args:dict(report_only=True,trials=[dict(accuracy=0)]),io_factory=IO)
    assert row['score_produced'] and row['retryable'] is False and row['restore']['clean']
    assert 'finish' in state.calls and state.calls.index('restore')<state.calls.index('signals-released')


def test_image_delay_is_explicitly_bound_to_new_image_without_promoting_measurement(monkeypatch):
    row=json.loads(RECOMMENDATION.read_text())
    monkeypatch.setattr(image_measure,'checked_recommendation',lambda *args:row)
    plan=dict(applied_recommendation=str(RECOMMENDATION),recommendation_sha256=digest(RECOMMENDATION),elf_sha256='b'*64)
    proof=image_measure.image_delay(RECOMMENDATION,plan)
    assert proof['kind']=='decision-backed' and proof['measured_pass'] is False and proof['delay_samples']==1024
    assert proof['source_elf_sha256']==row['source_elf_sha256'] and proof['applied_elf_sha256']=='b'*64
    assert proof['original_delay_record_sha256']==row['delay_provenance']['record_sha256']


@pytest.mark.parametrize('change',[None,'perf-gain','missing-perf-gain','wire-gain','wire-guard','boot-guard','missing-recommendation'])
def test_applied_contention_checks_original_wire_boot_and_perf(tmp_path,monkeypatch,change):
    from test_p4_aec_product import aec_run
    row=aec_run(tmp_path,monkeypatch)
    proof=dict(recommendation_file=str(RECOMMENDATION),selected=SELECTED)
    row['applied_calibration']=proof
    if change=='missing-recommendation':row.pop('applied_calibration')
    monkeypatch.setattr(product,'applied_gate',lambda *args:proof)
    (tmp_path/'image.json').write_text(json.dumps(manifest()))
    lines=boot_lines()
    if change=='boot-guard':lines[2]=lines[2].replace('guard_ms=400','guard_ms=600')
    (tmp_path/'serial.log').write_text('\n'.join(lines)+'\n')
    for session in row['sessions']:
        path=tmp_path/session['wire_file']
        text=re.sub(r'("ref_gain_db":)\d+',r'\g<1>30',path.read_text())
        text=re.sub(r'("guard_ms":)\d+',r'\g<1>400',text)
        if change=='wire-gain':text=text.replace('"ref_gain_db":30','"ref_gain_db":15')
        elif change=='wire-guard':text=text.replace('"guard_ms":400','"guard_ms":0')
        path.write_text(text);session['wire_sha256']=digest(path)
        for sample in session['samples']:sample['aec']['reference_health']=dict(gain_db=30)
    if change=='perf-gain':row['sessions'][0]['samples'][0]['aec']['reference_health']['gain_db']=15
    elif change=='missing-perf-gain':row['sessions'][0]['samples'][0]['aec'].pop('reference_health')
    if change:
        with pytest.raises(ValueError):product.score_contention(tmp_path,tmp_path/'bounds.json',row)
    else:
        result=product.score_contention(tmp_path,tmp_path/'bounds.json',row)
        assert result['checks_passed'] and result['applied_readback']['ref_gain_db']==30


def test_applied_same_image_report_uses_fixed_scorer_and_never_selects(monkeypatch,tmp_path):
    plan=dict(applied_recommendation='rec');path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    run=Mock(return_value=dict(available=True,report_only=True));monkeypatch.setattr(measure,'gain_selection',run)
    row=image_measure.gain_report(path,tmp_path/'grid')
    assert row['kind']=='p4-aec-applied-image-report' and row['report_only']
    assert run.call_args.args[-1]==30
    assert run.call_args.kwargs==dict(plan_validator=image_measure.validate_plan,delay_reader=image_measure.image_delay,
        prompt_scorer=image_measure.direction_prompt_score,acquisition_prompt_scorer=image_measure.direction_prompt_score)
