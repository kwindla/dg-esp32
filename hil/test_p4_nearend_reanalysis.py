"""Synthetic re-analysis regressions. PENDING execution until coordinator says bench free."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

import p4_nearend_analysis as analysis
import p4_nearend_capture as capture
import p4_nearend_reanalysis as reanalysis
from test_p4_nearend_capture import evaluation


def stimulus():
    rng=np.random.default_rng(1503);source=np.zeros(21759)
    source[1600:10560]=rng.normal(0,2000,8960)
    source[12800:19840]=rng.normal(0,1700,7040)
    return source


def samples():
    source=stimulus();pcm=np.zeros((40000,5))
    pcm[3000:3000+len(source),0]=source
    pcm[4024:4024+len(source),4]=source*.5
    return source,pcm


def fail_raw(monkeypatch,pcm):
    locate=analysis.locate;calls=[]
    def checked(source,target,minimum):
        calls.append(minimum)
        if np.shares_memory(target,pcm[:,0]):raise ValueError('synthetic raw envelope rejection')
        return locate(source,target,minimum)
    monkeypatch.setattr(analysis,'locate',checked)
    return calls


def test_raw_success_never_uses_fallback_even_if_aec_levels_fail():
    source,pcm=samples();pcm[:,4]=0
    row=reanalysis.fallback_levels(source,pcm,None,'B')
    assert row['alignment_method']=='raw'
    assert row['stage_validity']['raw'] and not row['stage_validity']['aec']


def test_raw_failure_uses_aec_with_original_raw_thresholds(monkeypatch):
    source,pcm=samples();calls=fail_raw(monkeypatch,pcm)
    row=reanalysis.fallback_levels(source,pcm,None,'B')
    assert calls==[analysis.RULES['raw_match_min']]*2
    assert row['alignment_method']=='aec-fallback'
    assert row['alignment']['aec']['offset_samples']==4024
    assert row['alignment']['raw']['offset_samples']==3000
    assert row['stage_validity']['raw'] and row['stage_validity']['aec']
    assert row['levels']['aec']['word']-row['levels']['raw']['word']==pytest.approx(-6.0206,abs=.03)


def test_arm_a_never_uses_aec_fallback(monkeypatch):
    source,pcm=samples();calls=fail_raw(monkeypatch,pcm)
    with pytest.raises(ValueError,match='raw envelope'):reanalysis.fallback_levels(source,pcm,None,'A')
    assert len(calls)==1


def test_failed_aec_match_stays_unavailable(monkeypatch):
    source,pcm=samples();pcm[:,4]=0;fail_raw(monkeypatch,pcm)
    with pytest.raises(ValueError,match='AEC fallback failed'):reanalysis.fallback_levels(source,pcm,None,'B')


def test_aec_offset_cannot_imply_negative_raw_indices(monkeypatch):
    source,pcm=samples();pcm[:,4]=0;pcm[500:500+len(source),4]=source
    fail_raw(monkeypatch,pcm)
    with pytest.raises(ValueError,match='out-of-range raw'):reanalysis.fallback_levels(source,pcm,None,'B')


def arm_b_fixture(evaluation,board):
    folder,plan,trial,pcm,save=evaluation
    trial['spec'].update(board=board,arm='B');trial['original']['bot_started_ns']=7_000_000_000
    (folder/'trial-start.json').write_text(json.dumps(dict(spec=trial['spec'])))
    path=folder/'events.jsonl';events=[json.loads(line) for line in path.read_text().splitlines()]
    for frame,stamp in [('BotStartedSpeakingFrame',7_000_000_000),('BotStoppedSpeakingFrame',12_000_000_000)]:
        events.append(dict(kind='frame',frame=frame,direction='DOWNSTREAM',monotonic_ns=stamp))
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))
    trial['media']['events.jsonl']['sha256']=capture.digest(path);save()
    if board=='echoear':
        path=folder.parent/'result.json';part=json.loads(path.read_text())
        part['restore']=dict(clean=True,restored_elf_sha256=capture.S3_RESTORE);path.write_text(json.dumps(part))
    return folder,plan,pcm


@pytest.mark.parametrize('board',['echoear','p4xc'])
def test_fallback_is_identical_across_boards_and_keeps_controls(evaluation,monkeypatch,board):
    folder,plan,pcm=arm_b_fixture(evaluation,board);fail_raw(monkeypatch,pcm)
    original_function=capture.evaluate;original_analysis=capture.analysis
    row=reanalysis.evaluate(folder,plan)
    assert row['alignment_method']==row['alignment_flag']=='aec-fallback'
    assert row['alignment']['raw']['offset_samples']==3000
    assert row['alignment']['aec']['offset_samples']==4024
    assert row['control_valid'] and row['stt_valid'] and row['hit'] is True
    assert row['stage_validity']==dict(raw=True,aec=True,uplink=True)
    assert capture.evaluate is original_function and capture.analysis is original_analysis


def test_fallback_does_not_bypass_physical_onset_control(evaluation,monkeypatch):
    import echoear_aec_timing
    folder,plan,pcm=arm_b_fixture(evaluation,'p4xc');fail_raw(monkeypatch,pcm)
    monkeypatch.setattr(echoear_aec_timing,'sample_interval',lambda *args:[12.1,12.2])
    row=reanalysis.evaluate(folder,plan)
    assert row['alignment_method']=='aec-fallback' and not row['control_valid']
    assert not any(row['stage_validity'].values())
    assert row['stt_observed'] and row['hit'] is True and not row['stt_valid']
    assert row['stt_exclusion_reason']


def test_failed_aec_fallback_preserves_observed_stt(evaluation,monkeypatch):
    folder,plan,pcm=arm_b_fixture(evaluation,'p4xc');pcm[:,4]=0;fail_raw(monkeypatch,pcm)
    row=reanalysis.evaluate(folder,plan)
    assert row['alignment_method']=='unavailable' and not any(row['stage_validity'].values())
    assert row['hit'] is True and row['stt_observed'] and not row['stt_valid']
    assert 'AEC fallback failed' in row['alignment_failure']


def test_raw_success_keeps_original_stages_controls_and_stt(evaluation):
    folder,plan,trial,pcm,save=evaluation
    before=capture.evaluate(folder,plan);after=reanalysis.evaluate(folder,plan)
    for key in ('levels','stage_validity','hit','stt_observed','stt_valid','control_valid',
                'reference_diagnostics','near_host_bounds_s','word_arrivals'):
        assert after[key]==before[key]
    assert after['alignment_method']=='raw'


def synthetic_rows():
    rows=[]
    for board in capture.ELFS:
        for pair in range(1,21):
            for arm in ('A','B'):
                method='aec-fallback' if board=='p4xc' and arm=='B' and pair>=5 else 'raw'
                value=-36. if method=='aec-fallback' else -30.
                row=dict(id=f'{board}-{pair:02d}-{arm}',board=board,arm=arm,pair=pair,part=(pair-1)//5,
                    alignment_method=method,control_valid=True,stt_observed=True,stt_valid=True,hit=True,
                    stage_validity={s:True for s in ('raw','aec','uplink')},
                    levels={s:dict(phrase=value,word=value) for s in ('raw','aec','uplink')})
                if arm=='B':row['residual_estimates']={s:{part:dict(valid=True,near_dbfs=value)
                    for part in ('phrase','word')} for s in ('aec','uplink')}
                rows.append(row)
    return rows


def set_method(row,method,value):
    row['alignment_method']=method
    row['levels']={stage:dict(phrase=value,word=value) for stage in ('raw','aec','uplink')}
    if row['arm']=='B':row['residual_estimates']={stage:{segment:dict(valid=True,near_dbfs=value)
        for segment in ('phrase','word')} for stage in ('aec','uplink')}


def local_method_rows(s3_raw,p4_raw):
    rows=synthetic_rows()
    for row in rows:
        raw=s3_raw if row['board']=='echoear' else p4_raw
        method='raw' if row['arm']=='A' or row['pair'] in raw else 'aec-fallback'
        value=-36. if row['board']=='p4xc' and row['arm']=='B' else -30.
        set_method(row,method,value)
    return rows


def test_method_sensitive_conclusion_uses_an_assessable_local_stratum():
    rows=local_method_rows(set(range(1,21)),set(range(1,16)))
    for row in rows:
        if row['board']=='p4xc' and row['arm']=='B':
            change=-4. if row['pair']<=7 else -1. if row['pair']<=15 else -6.
            set_method(row,row['alignment_method'],-30.+change)
    summary=reanalysis.summarize(rows);sensitivity=reanalysis.method_sensitivity(rows,summary)
    assert summary['double_talk_near_end_localized']
    raw=sensitivity['by_method']['raw'];comparison=raw['comparisons']['double_talk_output/word']
    assert raw['paired_blocks']==dict(echoear=20,p4xc=15)
    assert comparison['all_valid']['deficit'] and not comparison['same_method']['deficit']
    assert comparison['available'] and comparison['method_sensitive'] and comparison['label']=='method-sensitive'
    assert 'double_talk_output_corrected/word' in sensitivity['method_sensitive_conclusions']
    assert not raw['summary']['double_talk_near_end_localized']


def test_one_pair_crossing_never_claims_same_conclusion():
    rows=local_method_rows(set(range(1,21)),{1})
    result=reanalysis.method_sensitivity(rows,reanalysis.summarize(rows))
    comparison=result['by_method']['raw']['comparisons']['double_talk_output/word']
    assert comparison['all_valid']['deficit'] and comparison['same_method']['deficit']
    assert comparison['usable_pairs']==dict(echoear=20,p4xc=1)
    assert not comparison['available'] and not comparison['method_sensitive']
    assert comparison['label']==reanalysis.INSUFFICIENT_METHOD_PAIRS
    assert result['comparisons']['double_talk_output/word']['label']==reanalysis.INSUFFICIENT_METHOD_PAIRS
    assert result['comparisons']['double_talk_output/word']['label']!='same conclusion'


@pytest.mark.parametrize('count,available',[(14,False),(15,True)])
def test_method_conclusion_minimum_pair_boundary(count,available):
    rows=local_method_rows(set(range(1,21)),set(range(1,count+1)))
    result=reanalysis.method_sensitivity(rows,reanalysis.summarize(rows))
    comparison=result['by_method']['raw']['comparisons']['double_talk_output_corrected/word']
    assert comparison['available'] is available
    assert comparison['label']==('same conclusion' if available else reanalysis.INSUFFICIENT_METHOD_PAIRS)


def test_sixteen_board_local_raw_pairs_localize_without_cross_board_id_intersection():
    rows=local_method_rows(set(range(1,17)),set(range(5,21)))
    full=reanalysis.summarize(rows);result=reanalysis.method_sensitivity(rows,full)
    assert full['double_talk_near_end_localized']
    raw=result['by_method']['raw']
    assert raw['pairs']==dict(echoear=list(range(1,17)),p4xc=list(range(5,21)))
    assert raw['paired_blocks']==dict(echoear=16,p4xc=16)
    claim=raw['summary']['double_talk_near_end']['aec']['word']
    assert claim['localized'] and claim['p4_pairs']==claim['s3_pairs']==16
    assert all(claim['criteria'].values())
    comparison=raw['comparisons']['double_talk_output_corrected/word']
    assert comparison['available'] and comparison['label']=='same conclusion'
    assert not result['method_sensitive_conclusions']


def test_opposite_ten_ten_assignments_retain_both_measured_strata():
    rows=local_method_rows(set(range(1,11)),set(range(11,21)))
    result=reanalysis.method_sensitivity(rows,reanalysis.summarize(rows))
    assert result['available'] and not result['comparison_available']
    for method in reanalysis.METHODS:
        stratum=result['by_method'][method]
        assert stratum['available'] and stratum['paired_blocks']==dict(echoear=10,p4xc=10)
        assert stratum['summary'] is not None
        assert not stratum['summary']['double_talk_near_end_localized']
        metric=stratum['comparisons']['double_talk_output_corrected/word']
        assert metric['same_method']['available'] and metric['same_method']['deficit']
        assert metric['same_method']['p4']['n']==metric['same_method']['s3']['n']==10
        assert not metric['available'] and metric['label']==reanalysis.INSUFFICIENT_METHOD_PAIRS
    assert not result['method_sensitive_conclusions']


def test_pair_floor_is_per_metric_after_original_validity_checks():
    rows=local_method_rows(set(range(1,21)),set(range(1,21)))
    for row in rows:
        if row['board']=='p4xc' and row['arm']=='B' and row['pair']>14:row['residual_estimates']={}
    result=reanalysis.method_sensitivity(rows,reanalysis.summarize(rows))
    comparisons=result['by_method']['raw']['comparisons']
    assert comparisons['double_talk_output/word']['available']
    assert comparisons['double_talk_output_corrected/word']['usable_pairs']['p4xc']==14
    assert not comparisons['double_talk_output_corrected/word']['available']
    assert comparisons['double_talk_output_corrected/word']['label']==reanalysis.INSUFFICIENT_METHOD_PAIRS


def test_no_common_method_is_unassessable_not_a_claim_of_robustness():
    rows=local_method_rows(set(range(1,21)),set())
    result=reanalysis.method_sensitivity(rows,reanalysis.summarize(rows))
    assert not result['available'] and not result['comparison_available']
    assert not result['by_method']['raw']['available'] and not result['by_method']['aec-fallback']['available']
    assert result['comparisons']['double_talk_output/word']['label']=='method comparison unavailable'
    assert not result['method_sensitive_conclusions']


def test_identical_methods_keep_the_same_comparison_and_original_guards():
    rows=local_method_rows(set(range(1,21)),set(range(1,21)))
    full=reanalysis.summarize(rows);result=reanalysis.method_sensitivity(rows,full)
    assert result['by_method']['raw']['paired_blocks']==dict(echoear=20,p4xc=20)
    assert result['by_method']['raw']['summary']==full
    assert result['comparison_available'] and not result['method_sensitive_conclusions']


def test_selection_caution_is_present_in_each_arm_b_summary_and_stratum():
    rows=local_method_rows(set(range(1,17)),set(range(5,21)))
    full=reanalysis.summarize(rows);sensitivity=reanalysis.method_sensitivity(rows,full)
    summaries=[full]+[s['summary'] for s in sensitivity['by_method'].values() if s['summary'] is not None]
    expected='S3 fallback trials are selected on surviving near speech; cross-board arm-B deficits are selection-biased upward'
    for summary in summaries:
        for board in capture.ELFS:
            assert summary['boards'][board]['B']['selection_caution']==expected
            assert 'within-board-guarded localization is unaffected' in summary['boards'][board]['B']['selection_caution_scope']


def test_fallback_counts_include_validity_not_only_successful_levels():
    rows=synthetic_rows();fallback=next(r for r in rows if r['alignment_method']=='aec-fallback')
    fallback['stage_validity']['uplink']=False
    counts=reanalysis.fallback_counts(rows)
    assert counts['echoear']['raw']==20 and counts['echoear']['aec_fallback']==0
    assert counts['p4xc']['raw']==4 and counts['p4xc']['aec_fallback']==16
    assert counts['p4xc']['fallback_stage_valid']['uplink']==15


def test_third_octave_edges_cover_300_3500_without_gaps():
    bands=reanalysis.third_octave_bands()
    assert len(bands)==11 and bands[0]['low_hz']==300 and bands[-1]['high_hz']==3500
    for before,after in zip(bands,bands[1:]):assert before['high_hz']==pytest.approx(after['low_hz'])


def test_third_octave_ratio_matches_known_broadband_gain():
    raw=np.random.default_rng(51).normal(0,1000,64000)
    masks={'phrase':np.arange(12000,20000),'word':np.arange(28000,36000)}
    s3=reanalysis.spectral_levels(raw,0,masks);p4=reanalysis.spectral_levels(raw*.5,0,masks)
    for segment in masks:
        for band in s3[segment]:assert p4[segment][band]-s3[segment][band]==pytest.approx(-6.0205999133,abs=1e-8)


def test_third_octave_ratio_matches_known_lowpass_filter():
    time=np.arange(64000)/16000
    raw=2000*np.sin(2*np.pi*500*time)+2000*np.sin(2*np.pi*2000*time)
    sos=signal.butter(4,1000,fs=16000,output='sos');filtered=signal.sosfilt(sos,raw)
    masks={'phrase':np.arange(12000,20000),'word':np.arange(28000,36000)}
    s3=reanalysis.spectral_levels(raw,0,masks);p4=reanalysis.spectral_levels(filtered,0,masks)
    _,gain=signal.sosfreqz(sos,worN=np.array([500.,2000.]),fs=16000)
    for segment in masks:
        for band,expected in zip(('-3','3'),20*np.log10(np.abs(gain))):
            assert p4[segment][band]-s3[segment][band]==pytest.approx(expected,abs=.05)


def test_spectral_summary_uses_only_valid_arm_a_and_stays_report_only():
    rows=synthetic_rows();bands=reanalysis.third_octave_bands()
    for row in rows:
        value=-36 if row['board']=='p4xc' else -30
        if row['arm']=='B':value=100
        row['raw_spectrum']={segment:{b['id']:value for b in bands} for segment in ('phrase','word')}
    before=analysis.summarize(rows);result=reanalysis.spectral_summary(rows)
    for segment in ('phrase','word'):
        for band in bands:
            assert result['p4_minus_s3_db'][segment][band['id']]==dict(p4_n=20,s3_n=20,median=-6.,ci95=[-6.,-6.])
    assert result['rules']['report_only'] and not result['rules']['enters_attribution']
    assert analysis.summarize(rows)==before


def test_spectral_missing_board_is_unavailable_and_intervals_are_deterministic():
    assert reanalysis.ratio_ci([],[-30])['ci95'] is None
    assert reanalysis.ratio_ci([-36,-35,-34],[-30,-29,-28])==reanalysis.ratio_ci([-36,-35,-34],[-30,-29,-28])
    with pytest.raises(ValueError):reanalysis.ratio_ci([float('nan')],[0])


@pytest.fixture
def retained(tmp_path,monkeypatch):
    root=tmp_path/'run';root.mkdir();sources=tmp_path/'source/hil';sources.mkdir(parents=True)
    (sources/'fixture.py').write_text('original source\n')
    monkeypatch.setattr(reanalysis,'ROOT',sources.parent)
    monkeypatch.setattr(capture,'SOURCES',('fixture.py',))
    monkeypatch.setattr(capture,'validate_plan',lambda plan:plan)
    decision=tmp_path/'decision.md';decision.write_text('approved fallback\n')
    monkeypatch.setattr(reanalysis,'DECISION',decision);monkeypatch.setattr(reanalysis,'DECISION_SHA256',capture.digest(decision))
    plan=dict(parts_per_board=1,source_sha256={'fixture.py':capture.digest(sources/'fixture.py')},
        schedule={b:[dict(id=b+'-01-A',board=b,part=0,pair=1,arm='A',order=0)] for b in capture.ELFS})
    plan_path=root/'plan.json';plan_path.write_text(json.dumps(plan));plan_id=capture.digest_plan(plan)
    expected=dict(kind='step-15d-word-local-report',plan_sha256=plan_id,rows=[])
    prior=root/'original-report.json';prior.write_text(json.dumps(expected))
    monkeypatch.setattr(capture,'report',lambda *args:copy.deepcopy(expected))
    for board in capture.ELFS:
        attempt=root/board/'part-0/attempt-01';folder=attempt/(board+'-01-A');folder.mkdir(parents=True)
        spec=plan['schedule'][board][0]
        (folder/'trial-start.json').write_text(json.dumps(dict(plan_sha256=plan_id,spec=spec)))
        (folder/'trial.json').write_text(json.dumps(dict(spec=spec,status='unavailable')))
        restore=dict(clean=True,action='p4-product-of-record') if board=='p4xc' else dict(clean=True,restored_elf_sha256=capture.S3_RESTORE)
        result=dict(kind='step-15d-part',board=board,part=0,plan_sha256=plan_id,acquisition_passed=True,
            status='acquired',room=dict(noise_contaminated=False),restore=restore,trials=[str(folder)])
        (attempt/'result.json').write_text(json.dumps(result))
    return plan_path,root,prior,sources,plan


def test_plan_part_journal_and_source_hash_binding(retained):
    path,root,prior,sources,plan=retained
    actual,folders,parts,receipts=reanalysis.validate_inputs(path,root,prior)
    assert actual==plan and len(folders)==2 and len(parts)==2
    assert receipts[str(path.resolve())]==capture.digest(path)
    reanalysis.verify_receipts(receipts)


@pytest.mark.parametrize('fault',['source','part-plan','journal','trial','source-stamp','original-replay','unfinished'])
def test_foreign_or_unfinished_acquisition_is_rejected(retained,fault):
    path,root,prior,sources,plan=retained;attempt=root/'p4xc/part-0/attempt-01';folder=attempt/'p4xc-01-A'
    if fault=='source':(sources/'fixture.py').write_text('changed source\n')
    elif fault=='part-plan':
        p=attempt/'result.json';row=json.loads(p.read_text());row['plan_sha256']='f'*64;p.write_text(json.dumps(row))
    elif fault=='journal':
        p=folder/'trial-start.json';row=json.loads(p.read_text());row['spec']['pair']=2;p.write_text(json.dumps(row))
    elif fault=='trial':
        p=folder/'trial.json';row=json.loads(p.read_text());row['spec']['arm']='B';p.write_text(json.dumps(row))
    elif fault=='source-stamp':
        p=attempt/'result.json';row=json.loads(p.read_text());row['source_sha256']={};p.write_text(json.dumps(row))
    elif fault=='original-replay':
        row=json.loads(prior.read_text());row['rows']=[dict(changed=True)];prior.write_text(json.dumps(row))
    else:(attempt/'result.json').unlink()
    with pytest.raises(ValueError):reanalysis.validate_inputs(path,root,prior)


def test_receipts_detect_evidence_changed_during_reanalysis(retained):
    path,root,prior,sources,plan=retained
    _,folders,_,receipts=reanalysis.validate_inputs(path,root,prior)
    (folders[0]/'trial.json').write_text('{}')
    with pytest.raises(ValueError,match='input changed'):reanalysis.verify_receipts(receipts)


def test_noisy_preflight_cannot_supply_trial_journals(retained):
    path,root,prior,sources,plan=retained
    p=root/'p4xc/part-0/attempt-01/result.json';row=json.loads(p.read_text())
    row.update(room=dict(noise_contaminated=True),device_untouched=True,acquisition_passed=False)
    p.write_text(json.dumps(row))
    with pytest.raises(ValueError,match='noisy preflight'):reanalysis.validate_inputs(path,root,prior)


def test_original_report_is_required_and_cannot_be_overwritten(tmp_path):
    path=tmp_path/'original.json';path.write_text('{"original":true}\n');before=path.read_bytes()
    with pytest.raises(FileExistsError):reanalysis.write_report(path,dict(kind='p4-nearend-reanalysis-v1'))
    assert path.read_bytes()==before
    new=tmp_path/'new.json';reanalysis.write_report(new,dict(kind='p4-nearend-reanalysis-v1'))
    assert json.loads(new.read_text())['kind']=='p4-nearend-reanalysis-v1'


@pytest.mark.parametrize('relative',['report.json','part-0/attempt-09','.'])
def test_cli_refuses_output_inside_acquisition_root_before_any_analysis(tmp_path,monkeypatch,relative):
    root=tmp_path/'acquisition';root.mkdir();out=root/relative
    def forbidden(*args):raise AssertionError('must refuse output before reading acquisition')
    monkeypatch.setattr(reanalysis,'build_report',forbidden)
    with pytest.raises(ValueError,match='outside the acquisition root'):
        reanalysis.main(['--plan','unused.json','--root',str(root),'--original-report','unused-report.json','--out',str(out)])
    assert not list(root.iterdir())


def test_cli_refuses_symlinked_output_into_acquisition_root(tmp_path,monkeypatch):
    root=tmp_path/'acquisition';root.mkdir();alias=tmp_path/'alias';alias.symlink_to(root,target_is_directory=True)
    monkeypatch.setattr(reanalysis,'build_report',lambda *args:pytest.fail('must not analyze'))
    with pytest.raises(ValueError,match='outside the acquisition root'):
        reanalysis.main(['--plan','unused.json','--root',str(root),'--original-report','unused-report.json','--out',str(alias/'new.json')])
    assert not list(root.iterdir())


def test_cli_allows_external_output_with_shared_path_prefix(tmp_path,monkeypatch):
    root=tmp_path/'acquisition';root.mkdir();out=tmp_path/'acquisition-report.json'
    monkeypatch.setattr(reanalysis,'build_report',lambda *args:dict(kind='p4-nearend-reanalysis-v1',complete=True))
    assert reanalysis.main(['--plan','unused.json','--root',str(root),'--original-report','unused-report.json','--out',str(out)])==0
    assert json.loads(out.read_text())['kind']=='p4-nearend-reanalysis-v1'
    assert not list(root.iterdir())
