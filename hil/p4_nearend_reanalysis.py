#!/usr/bin/env python3
"""Versioned offline 15d re-analysis. Run only after acquisition and 'bench free'.

First produce the unchanged original report with p4_nearend_capture.py report.
Then, under the usual frozen server environment (PYTHONDONTWRITEBYTECODE=1):
  python hil/p4_nearend_reanalysis.py --plan PLAN --root RUN \
      --original-report ORIGINAL_REPORT --out NEW_REPORT

This module never writes acquisition evidence or changes imported modules. Its
one permitted change to original evaluation is the decision-backed B alignment
fallback. Spectra and method sensitivity are additional report-only diagnostics.
"""
import argparse
import copy
import json
from pathlib import Path
import re
from types import FunctionType, SimpleNamespace

import numpy as np
from scipy import signal

import p4_nearend_analysis as analysis
import p4_nearend_capture as original
from echoear_aec_alignment import load_nearend_source
from make_bundle import digest
from wire_contract import require

ROOT=Path(__file__).resolve().parents[1]
DECISION=Path('/home/khkramer/src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-02-15d-armB-alignment-fallback.md')
DECISION_SHA256='a97413b9cf70317dd3d6a90e980012daa5770136a0fddb8566ccd41c06feab93'
METHODS=('raw','aec-fallback')
B_SELECTION_CAUTION='S3 fallback trials are selected on surviving near speech; cross-board arm-B deficits are selection-biased upward'
INSUFFICIENT_METHOD_PAIRS='method comparison unavailable (insufficient same-method pairs)'
SPECTRAL_RULES=dict(report_only=True,enters_attribution=False,arm='A',stage='raw MIC1',
    band_hz=[300.,3500.],centers='1000 * 2**(k/3), integer k; edges center * 2**(+/-1/6), clipped to 300..3500 Hz',
    filter='fourth-order causal Butterworth SOS per band; source-defined original masks; no band-specific re-alignment',
    ratio='median(P4 level dBFS) minus median(S3 level dBFS); independent-board bootstrap of median difference',
    interpretation='Acoustic/analog/placement spectral shape; not a deconvolved transfer function or a mic-versus-placement cause')


def fallback_levels(source,pcm,uplink,arm):
    """Fallback only for a rejected raw match, with identical thresholds on both boards."""
    pcm=np.asarray(pcm)
    analysis.check(pcm.ndim==2 and pcm.shape[1]==5 and np.isfinite(pcm).all(),
        'five synchronized finite capture channels required')
    try:
        analysis.locate(source,pcm[:,0],analysis.RULES['raw_match_min'])
    except ValueError as raw_error:
        if arm!='B':raise
        try:match=analysis.locate(source,pcm[:,4],analysis.RULES['raw_match_min'])
        except ValueError as aec_error:
            raise ValueError('raw match failed: '+str(raw_error)+'; AEC fallback failed: '+str(aec_error)) from aec_error
        processed=match['offset_samples'];onset=processed-analysis.RULES['afe_delay_samples']
        analysis.check(onset>=0 and onset+len(source)<=len(pcm),'AEC fallback implies incomplete/out-of-range raw window')
        masks=analysis.segments(source)
        matches=dict(raw=dict(offset_samples=onset,method='aec-fallback',raw_match_failure=str(raw_error),
            derived_from='matched AEC offset minus adopted delay',measured_pass=False),
            aec=dict(match,method='aec-fallback',kind=analysis.RULES['delay_kind'],measured_pass=False))
        stages={'raw':(pcm[:,0],onset),'aec':(pcm[:,4],processed)};errors={}
        if uplink is not None:
            try:
                received=analysis.locate(pcm[processed:processed+len(source),4],uplink,analysis.RULES['uplink_match_min'])
                stages['uplink']=(uplink,received['offset_samples']);matches['uplink']=received
            except ValueError as exc:errors['uplink']=str(exc)
        levels={};validity={name:False for name in ('raw','aec','uplink')}
        for name,(samples,offset) in stages.items():
            try:
                analysis.check(offset+len(source)<=len(samples),'capture ends before complete delayed final word')
                filtered=analysis.band(samples)
                measured={segment:analysis.level(filtered,offset+mask) for segment,mask in masks.items()}
                levels[name]=measured;validity[name]=True
            except ValueError as exc:errors[name]=str(exc)
        return dict(levels=levels,alignment=matches,stage_validity=validity,unavailable=errors,
            alignment_method='aec-fallback')
    # A raw success can never be replaced merely because a later stage fails.
    result=analysis.stage_levels(source,pcm,uplink)
    result['alignment']['raw']['method']='raw';result['alignment_method']='raw'
    return result


def evaluate(folder,plan):
    """Use the exact original evaluator code with a private analysis facade.

    FunctionType gives this call its own globals dictionary; no monkeypatch,
    module assignment or acquisition-process mutation is involved. All original
    controls, STT, residual estimation and stage-evidence checks still execute.
    """
    start=json.loads((Path(folder)/'trial-start.json').read_text());state={}
    def aligned(source,pcm,uplink=None):
        state['attempted']=True
        try:
            result=fallback_levels(source,pcm,uplink,start['spec']['arm'])
            state['method']=result['alignment_method'];return result
        except ValueError as exc:
            state['error']=str(exc);raise
    facade=SimpleNamespace(**dict(vars(analysis),stage_levels=aligned))
    bindings=dict(original.evaluate.__globals__,analysis=facade)
    evaluator=FunctionType(original.evaluate.__code__,bindings,original.evaluate.__name__,original.evaluate.__defaults__)
    row=evaluator(folder,plan)
    row['alignment_method']=state.get('method','unavailable')
    row['alignment_flag']='aec-fallback' if row['alignment_method']=='aec-fallback' else row['alignment_method']
    if 'error' in state:row['alignment_failure']=state['error']
    return row


def third_octave_bands():
    bands=[]
    for k in range(-12,13):
        center=1000.*2**(k/3);low=max(300.,center*2**(-1/6));high=min(3500.,center*2**(1/6))
        if low<high:bands.append(dict(id=str(k),center_hz=center,low_hz=low,high_hz=high))
    return bands


def spectral_levels(raw,onset,masks):
    raw=analysis.mono(raw);result={segment:{} for segment in masks}
    for band in third_octave_bands():
        filtered=signal.sosfilt(signal.butter(4,[band['low_hz'],band['high_hz']],fs=analysis.RATE,
            btype='bandpass',output='sos'),raw)
        for segment,mask in masks.items():result[segment][band['id']]=analysis.level(filtered,onset+mask)
    return result


def ratio_ci(p4,s3):
    p4=np.asarray(p4,dtype=float);s3=np.asarray(s3,dtype=float)
    require(p4.ndim==s3.ndim==1 and np.isfinite(p4).all() and np.isfinite(s3).all(),'finite spectral level vectors required')
    if not len(p4) or not len(s3):return dict(p4_n=len(p4),s3_n=len(s3),median=None,ci95=None)
    rng=np.random.default_rng(analysis.RULES['bootstrap_seed']);count=analysis.RULES['bootstrap_resamples']
    p=rng.integers(0,len(p4),(count,len(p4)));s=rng.integers(0,len(s3),(count,len(s3)))
    differences=np.median(p4[p],axis=1)-np.median(s3[s],axis=1)
    return dict(p4_n=len(p4),s3_n=len(s3),median=float(np.median(p4)-np.median(s3)),
        ci95=np.quantile(differences,[.025,.975]).tolist())


def spectral_summary(rows):
    bands=third_octave_bands();groups={};ratios={}
    for segment in ('phrase','word'):
        groups[segment]={board:{} for board in original.ELFS};ratios[segment]={}
        for band in bands:
            values={board:[r['raw_spectrum'][segment][band['id']] for r in rows if r['board']==board and
                r['arm']=='A' and analysis.valid_stage(r,'raw') and segment in r.get('raw_spectrum',{})]
                for board in original.ELFS}
            for board in values:groups[segment][board][band['id']]=analysis.median_ci(values[board])
            ratios[segment][band['id']]=ratio_ci(values['p4xc'],values['echoear'])
    return dict(rules=SPECTRAL_RULES,bands=bands,levels=groups,p4_minus_s3_db=ratios,
        unavailable=[dict(id=r['id'],reason=r.get('spectral_unavailable','raw/control unavailable')) for r in rows
            if r['arm']=='A' and 'raw_spectrum' not in r])


def b_key(key):
    return key.startswith(('double_talk/','double_talk_output/','double_talk_output_corrected/')) or '/B/' in key


def fallback_counts(rows):
    result={}
    for board in original.ELFS:
        records=[r for r in rows if r['board']==board and r['arm']=='B']
        result[board]=dict(originals=len(records),raw=sum(r.get('alignment_method')=='raw' for r in records),
            aec_fallback=sum(r.get('alignment_method')=='aec-fallback' for r in records),
            unavailable=sum(r.get('alignment_method') not in METHODS for r in records),
            fallback_control_valid=sum(r.get('alignment_method')=='aec-fallback' and r.get('control_valid') is True for r in records),
            fallback_stage_valid={stage:sum(r.get('alignment_method')=='aec-fallback' and analysis.valid_stage(r,stage)
                for r in records) for stage in ('raw','aec','uplink')})
    return result


def summarize(rows):
    summary=analysis.summarize(rows)
    for board,counts in fallback_counts(rows).items():
        summary['boards'][board]['B'].update(alignment_methods=counts,selection_caution=B_SELECTION_CAUTION,
            selection_caution_scope='Only the within-board-guarded localization is unaffected; its original guards remain required')
    return summary


def method_sensitivity(rows,all_summary):
    """Build each method's strata from independent board-local A/B pairs.

    Pair numbers associate A and B within a board, never observations across
    boards. Measured strata with <15 usable pairs remain visible, but cannot
    support a method-comparison conclusion for the affected metric.
    """
    def conclusion(summary,key):
        comparison=summary['p4_vs_s3'][key]
        return dict(available=comparison['available'],rule_crossed=comparison['responsible'],
            deficit=comparison.get('deficit',False),localized=key in summary['localized_deficits'],
            p4=summary['contributions']['p4xc'][key],s3=summary['contributions']['echoear'][key])
    strata={};keys=[key for key in all_summary['p4_vs_s3'] if b_key(key)]
    full={key:conclusion(all_summary,key) for key in keys}
    minimum=analysis.RULES['residual']['min_pairs']
    for method in METHODS:
        pairs={board:{r['pair'] for r in rows if r['board']==board and r['arm']=='B' and
            r.get('control_valid') is True and r.get('alignment_method')==method} for board in original.ELFS}
        # Keep that board's own A for each eligible B, without a cross-board ID intersection.
        selected=[r for r in rows if r['pair'] in pairs[r['board']] and
            (r['arm']=='A' or r.get('alignment_method')==method)]
        data_available=all(pairs.values());summary=summarize(selected) if any(pairs.values()) else None
        comparisons={}
        for key in keys:
            same=conclusion(summary,key) if summary is not None else None
            n={board:summary['contributions'][board][key]['n'] if summary is not None else 0 for board in original.ELFS}
            measured=bool(same and full[key]['available'] and same['available'])
            sufficient=all(count>=minimum for count in n.values())
            available=measured and sufficient
            changed=available and any(full[key][k]!=same[k] for k in ('rule_crossed','deficit','localized'))
            label=('method-sensitive' if changed else 'same conclusion') if available else (
                INSUFFICIENT_METHOD_PAIRS if data_available and not sufficient else 'method comparison unavailable')
            comparisons[key]=dict(all_valid=full[key],same_method=same,measured=measured,available=available,
                usable_pairs=n,minimum_pairs=minimum,method_sensitive=bool(changed),label=label)
        strata[method]=dict(available=data_available,pairs={board:sorted(ids) for board,ids in pairs.items()},
            paired_blocks={board:len(ids) for board,ids in pairs.items()},summary=summary,comparisons=comparisons)
    comparisons={};sensitive=[]
    for key in keys:
        by_method={method:strata[method]['comparisons'][key] for method in METHODS}
        assessable=[method for method,value in by_method.items() if value['available']]
        changed=any(value['method_sensitive'] for value in by_method.values())
        if changed:sensitive.append(key)
        insufficient=any(value['label']==INSUFFICIENT_METHOD_PAIRS for value in by_method.values())
        label=('method-sensitive' if changed else 'same conclusion') if assessable else (
            INSUFFICIENT_METHOD_PAIRS if insufficient else 'method comparison unavailable')
        comparisons[key]=dict(all_valid=full[key],by_method=by_method,available=bool(assessable),
            assessable_methods=assessable,method_sensitive=changed,label=label)
    return dict(available=any(value['available'] for value in strata.values()),
        comparison_available=any(value['available'] for value in comparisons.values()),by_method=strata,
        comparisons=comparisons,method_sensitive_conclusions=sensitive,
        caution='Strata use independent board-local pairs; measured values remain available below 15 pairs, but method conclusions require >=15 usable pairs per board for that metric')


def read_json(path):
    return json.loads(Path(path).read_text())


def validate_sources(plan):
    require(set(plan['source_sha256'])==set(original.SOURCES),'original acquisition source inventory changed')
    for name,expected in plan['source_sha256'].items():
        require(re.fullmatch(r'[A-Za-z0-9_]+\.py',name),'unsafe acquisition source name')
        require(digest(ROOT/'hil'/name)==expected,'acquisition source hash changed: '+name)
    original.validate_plan(plan)


def validate_inputs(plan_path,root,original_report):
    """Bind part/trial journals to the canonical plan and its source hashes.

    Acquisition records do not duplicate the source map. Their plan_sha256
    binds them to the plan, which includes source_sha256; do not claim an
    additional independent per-trial source measurement that was not retained.
    """
    plan_path=Path(plan_path).resolve();root=Path(root).resolve();original_report=Path(original_report).resolve()
    require(digest(DECISION)==DECISION_SHA256,'fallback decision changed')
    plan=read_json(plan_path);validate_sources(plan);plan_id=original.digest_plan(plan)
    receipts={}
    def remember(path,required=True):
        path=Path(path).resolve()
        require(path.is_file() or not required,'missing immutable input: '+str(path))
        receipts[str(path)]=digest(path) if path.is_file() else None
    remember(plan_path);remember(original_report);remember(DECISION);remember(Path(__file__))
    prior=read_json(original_report)
    require(prior.get('kind')=='step-15d-word-local-report' and prior.get('plan_sha256')==plan_id,
        'unchanged original report for this acquisition plan is required first')
    folders=[];acquisitions=[];known_journals=set();known_attempts=set()
    for board in original.ELFS:
        for part in range(plan['parts_per_board']):
            directory=root/board/f'part-{part}'
            originals=original.consumed(directory,plan,board,part)
            planned=[spec for spec in plan['schedule'][board] if spec['part']==part]
            require(set(originals)=={spec['id'] for spec in planned[:len(originals)]},'consumed trials must form the original declared prefix')
            if originals:
                for earlier in range(part):
                    expected=sum(spec['part']==earlier for spec in plan['schedule'][board])
                    require(len(original.consumed(root/board/f'part-{earlier}',plan,board,earlier))==expected,
                        'later same-board part precedes earlier consumed originals')
            consumed_count=0
            for attempt in sorted(directory.glob('attempt-??')):
                require(re.fullmatch(r'attempt-[0-9]{2}',attempt.name),'invalid acquisition attempt name')
                known_attempts.add(attempt.resolve())
                result_path=attempt/'result.json'
                require(result_path.is_file(),'unfinished acquisition: wait for completion and bench free')
                remember(result_path);result=read_json(result_path)
                require(result.get('kind')=='step-15d-part' and result.get('plan_sha256')==plan_id and
                    result.get('board')==board and result.get('part')==part,'foreign acquisition result')
                if 'source_sha256' in result:require(result['source_sha256']==plan['source_sha256'],'acquisition source stamp differs')
                journals=list(attempt.glob('*/trial-start.json'))
                require({p.parent.name for p in journals}=={spec['id'] for spec in planned[consumed_count:consumed_count+len(journals)]},
                    'attempt journals violate original resume order')
                consumed_count+=len(journals)
                if result.get('room',{}).get('noise_contaminated'):
                    require(not journals and not result.get('trials') and result.get('device_untouched') is True,
                        'noisy preflight must not supply scored originals')
                listed={Path(p).resolve() for p in result.get('trials',[])}
                require(listed<={p.parent.resolve() for p in journals},'part references trials without journals')
                if result.get('acquisition_passed') is True:
                    require(original.restore_ok(board,result.get('restore',{})),'successful part lacks clean pinned restore')
                for journal in journals:
                    remember(journal);known_journals.add(journal.resolve());start=read_json(journal)
                    require(start['plan_sha256']==plan_id and start['spec'] in plan['schedule'][board]
                        and start['spec']['part']==part and start['spec']['id']==journal.parent.name,'trial journal differs from plan')
                    trial_path=journal.parent/'trial.json';remember(trial_path,False)
                    if not trial_path.is_file():continue
                    require(journal.parent.resolve() in listed,'finished trial not recorded by its acquisition')
                    trial=read_json(trial_path);require(trial['spec']==start['spec'],'trial result differs from journal')
                    if trial.get('status')!='retained':continue
                    folder=journal.parent;remember(folder/'audio/original.json')
                    capture=trial['original']['capture']
                    for key,name in (('path','capture.npz'),('dump','capture.dump.log'),('anchors','capture.anchors.json')):
                        require(Path(capture[key]).resolve()==(folder/'audio'/name).resolve(),'capture evidence escaped original folder')
                        remember(capture[key],False)
                    for name in ('events.jsonl','uplink.wav','tts.wav'):
                        require(Path(trial['media'][name]['path']).resolve()==(folder/name).resolve(),'media evidence escaped original folder')
                        remember(trial['media'][name]['path'],False)
                acquisitions.append(dict(path=str(result_path),sha256=receipts[str(result_path.resolve())],
                    board=board,part=part,status=result.get('status'),journal_count=len(journals)))
            folders.extend(originals.values())
    require(set(p.resolve() for p in root.glob('*/*/attempt-??'))==known_attempts,
        'unexpected acquisition attempt outside original board/part schedule')
    require(set(p.resolve() for p in root.glob('*/*/attempt-??/*/trial-start.json'))==known_journals,
        'unexpected board/part trial journals outside original schedule')
    # Replay before introducing any fallback, so new output cannot pass as the original.
    require(prior==original.report(plan,root),'original report does not reproduce for this immutable acquisition snapshot')
    return plan,folders,acquisitions,receipts


def verify_receipts(receipts):
    for filename,expected in receipts.items():
        path=Path(filename)
        require((digest(path) if path.is_file() else None)==expected,'input changed during re-analysis: '+filename)


def build_report(plan_path,root,original_report):
    plan,folders,acquisitions,receipts=validate_inputs(plan_path,root,original_report)
    source=load_nearend_source(Path(next(f['path'] for f in plan['fixtures'] if f['id']=='italy')))
    masks=analysis.segments(source);rows=[]
    for folder in folders:
        row=evaluate(folder,plan)
        if row['arm']=='A' and analysis.valid_stage(row,'raw'):
            try:
                trial=read_json(Path(folder)/'trial.json');pcm=original.measure.load_capture(trial['original']['capture'])
                row['raw_spectrum']=spectral_levels(pcm[:,0],row['alignment']['raw']['offset_samples'],masks)
            except (ValueError,KeyError,OSError,TypeError) as exc:row['spectral_unavailable']=str(exc)
        rows.append(row)
    summary=summarize(rows);counts=fallback_counts(rows);sensitivity=method_sensitivity(rows,summary)
    for key,comparison in sensitivity['comparisons'].items():
        summary['p4_vs_s3'][key]['method_sensitivity']=comparison['label']
    for stage,segments in summary['double_talk_near_end'].items():
        for segment,claim in segments.items():
            suffix=segment if stage=='aec' else stage+'/'+segment
            key='double_talk_output_corrected/'+suffix
            claim['alignment_method_sensitivity']=sensitivity['comparisons'][key]['label']
    if any(k in sensitivity['method_sensitive_conclusions'] for k in summary['localized_deficits']):
        summary['localization']+=' (method-sensitive)'
        summary['double_talk_localization']+=' (method-sensitive)'
    elif any(b_key(k) and not sensitivity['comparisons'][k]['available'] for k in summary['localized_deficits']):
        summary['localization']+=' (alignment-method comparison unavailable)'
        summary['double_talk_localization']+=' (alignment-method comparison unavailable)'
    result=dict(kind='p4-nearend-reanalysis-v1',plan_sha256=original.digest_plan(plan),
        plan_file=str(Path(plan_path).resolve()),plan_file_sha256=digest(plan_path),original_report=str(Path(original_report).resolve()),
        original_report_sha256=digest(original_report),original_rules=copy.deepcopy(analysis.RULES),
        reanalysis_source_sha256=digest(Path(__file__)),decision_file=str(DECISION),decision_sha256=DECISION_SHA256,
        acquisition_source_sha256=plan['source_sha256'],
        source_hash_basis='part/trial plan_sha256 binds the canonical plan, including its acquisition source_sha256 map',
        acquisitions=acquisitions,input_sha256=receipts,rows=rows,fallback_counts=counts,method_sensitivity=sensitivity,
        spectral_diagnostic=spectral_summary(rows),**summary)
    validate_sources(plan);verify_receipts(receipts)
    return result


def write_report(path,result):
    """Exclusive-create, including partial reports; never replace original evidence."""
    payload=json.dumps(result,indent=2,allow_nan=False)+'\n'
    with Path(path).open('x') as stream:stream.write(payload)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('plan','root','original-report','out'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args(argv)
    require(not args.out.resolve().is_relative_to(args.root.resolve()),'--out must be outside the acquisition root')
    require(not args.out.exists(),'exclusive new report path required')
    row=build_report(args.plan,args.root,args.original_report);write_report(args.out,row)
    return 0 if row['complete'] else 1


if __name__=='__main__':raise SystemExit(main())
