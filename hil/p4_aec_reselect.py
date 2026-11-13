#!/usr/bin/env python3
"""Additive, decision-bound AEC re-selection. Originals are read-only.

Offline selection is deliberately separate from the running acquisition tree.
The guard command is coordinator-only and retains the existing bench/R7 path.
"""
import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import subprocess

import board_smoke as smoke
import p4_aec_measure as measure
import p4_aec_policy as policy
from make_bundle import digest,verify_bundle
from wire_contract import require

ROOT=Path(__file__).resolve().parents[1]
ACQUISITION_ROOT=Path('/home/khkramer/src/dg-esp32-p4')
FROZEN_COMMIT='b11f7e30b01b800a1e884d0f6d248b20060c9135'
JOBS=Path('/home/khkramer/src/dg-esp32/proj-2026-10-30-2143/codex-jobs')
DECISIONS={
    'decision-2026-11-02-p4-aec-prompt-dedup':
        '60ce36f9cb14549937566dafff7282f85ba0a21456681747ac79a9495cd9f8e7',
    'decision-2026-11-02-p4-aec-sweep-predeclared':
        '0e16fe3e1423201140a7e9dc712cb0eb823b65a08caa73d37e63e2b453cbe710',
}
HELPERS=('p4_aec_policy.py','p4_aec_measure.py','echoear_aec_alignment.py','echoear_aec_gain_sweep.py',
    'echoear_aec_scoring.py','echoear_aec_metrics.py','echoear_aec_capture.py','p4_aec_product.py',
    'board_smoke.py','echoear_aec_timing.py','dump_verify.py','playback_quality.py','echoear_hot_stt.py',
    'p4_step14.py','p4_product.py','p4_audio_fix4.py','alsa.py')


def decision_bindings():
    rows=[]
    for name,expected in DECISIONS.items():
        path=JOBS/(name+'.md')
        require(digest(path)==expected,'re-selection decision changed: '+name)
        rows.append(dict(id=name,path=str(path),sha256=expected))
    return rows


@lru_cache(maxsize=1)
def frozen_source_hashes():
    # Read immutable Git blobs from this checkout, never execute another
    # worktree's modules or rebind an acquisition to the edited source tree.
    return {name:hashlib.sha256(subprocess.check_output(
        ['git','show',FROZEN_COMMIT+':hil/'+name],cwd=ROOT)).hexdigest() for name in HELPERS}


def validate_frozen_plan(plan):
    decision_bindings()
    manifest,_=verify_bundle(Path(plan['bundle']),'p4xc')
    fixtures=[]
    for spec in policy.fixtures():
        relative=Path(spec['path']).relative_to(ROOT)
        path=ACQUISITION_ROOT/relative
        require(digest(path)==spec['sha256'],'acquisition fixture changed')
        fixtures.append(dict(spec,path=str(path)))
    expected=dict(schema=1,kind='p4-aec-calibration-plan',qualification=False,policy=policy.POLICY,
        bundle=str(Path(plan['bundle']).resolve()),profile=manifest['profile'],
        manifest_sha256=digest(Path(plan['bundle'])/'manifest.json'),elf_sha256=manifest['elf_sha256'],
        fixtures=fixtures,gain_parts=[list(p) for p in policy.GAIN_PARTS],s3_reference=policy.S3_REFERENCE,
        source_sha256=frozen_source_hashes())
    require(manifest['aec_enabled'] is True and manifest['profile'] in ('aec-normal','aec-demo'),
        'frozen P4 AEC image required')
    require(plan==expected,'original plan/bundle/fixtures/frozen Git-source hashes differ')
    # Only the explicitly adapted files may differ in the scoring checkout.
    for name,sha in plan['source_sha256'].items():
        if name not in ('p4_aec_measure.py','p4_aec_product.py'):
            require(digest(ROOT/'hil'/name)==sha,'unreviewed helper change: '+name)
    return plan


def scoring_context():
    return dict(id='p4-aec-direction-reselection-v1',frame_convention='DOWNSTREAM only, board_smoke.downstream_frames',
        decisions=decision_bindings(),frozen_commit=FROZEN_COMMIT,
        adapter_sources={name:digest(ROOT/'hil'/name) for name in
            HELPERS+('p4_aec_reselect.py','p4_defaults_validate.py','atomic_json.py')})


def direction_prompt_score(spec,events,playback,bot_started_ns,bot_stopped_ns):
    smoke.downstream_frames(events) # Apply the frozen convention and direction validation.
    logical=[e for e in events if e.get('kind')!='frame' or e['direction']=='DOWNSTREAM']
    return policy.prompt_score(spec,logical,playback,bot_started_ns,bot_stopped_ns)


def scored_part(directory):
    attempts=measure.attempt_rows(directory)
    scored=[(path,row) for path,row,has_score in attempts if has_score]
    require(len(scored)==1,'exactly one consumed scored original required')
    path,row=scored[0]
    require(attempts[-1][0]==path,'attempt follows a consumed scored original')
    require(isinstance(row,dict) and isinstance(row.get('cases'),list) and row['cases'],
        'scored result/checkpoint provenance required; a payload alone cannot invent missing context')
    complete=row.get('status')=='acquired' and row.get('acquisition_passed') is True
    if not complete:
        require(row.get('score_produced') is True and row.get('retryable') is False,
            'only a published, consumed scored part can use the finalization-failure ruling')
    payload=path.parent/'post-body-payload.json'
    require(payload.is_file() and digest(payload)==row.get('post_body_payload_sha256'),
        'original scored payload hash required')
    saved=json.loads(payload.read_text())
    require(isinstance(saved,dict) and all(key in row and row[key]==value for key,value in saved.items()),
        'result differs from original scored payload')
    return path,row


def part_failure(path,row):
    clean=row.get('restore',{}).get('clean') is True
    if row.get('acquisition_passed') is True and row.get('status')=='acquired' and clean:return None
    return dict(result_file=str(path.resolve()),result_sha256=digest(path),status=row.get('status'),
        acquisition_passed=row.get('acquisition_passed'),error=row.get('error'),
        cleanup_errors=row.get('cleanup_errors',[]),restore=row.get('restore'),
        r7_recovery_status='clean restore recorded' if clean else 'recovery must be established by coordinator R7 evidence',
        score_treatment='used as scored; consumed; never retaken')


def reselect(plan_path,delay_path,root,original_selection=None):
    plan_path=Path(plan_path);delay_path=Path(delay_path);root=Path(root)
    plan=json.loads(plan_path.read_text());validate_frozen_plan(plan)
    failures=[]
    def reader(directory):
        path,row=scored_part(directory)
        failure=part_failure(path,row)
        if failure is not None:failures.append(failure)
        return path,row
    selected=measure.gain_selection(plan,plan_path,delay_path,root,
        prompt_scorer=direction_prompt_score,plan_validator=validate_frozen_plan,part_reader=reader)
    original=None
    if original_selection is not None:
        path=Path(original_selection)
        record=json.loads(path.read_text());comparison=record.get('rescored_selection',record)
        require(comparison.get('plan_sha256')==digest(plan_path) and
            Path(comparison['root']).resolve()==root.resolve() and comparison.get('elf_sha256')==plan['elf_sha256'],
            'original selection belongs to a different campaign/image')
        original=dict(path=str(path.resolve()),sha256=digest(path),record=record)
    return dict(kind='p4-aec-reselection',qualification=False,plan_file=str(plan_path.resolve()),
        plan_sha256=digest(plan_path),root=str(root.resolve()),delay_file=str(delay_path.resolve()),
        original_selection=original,rescored_selection=selected,scoring_context=scoring_context(),
        finalization_failures=failures,
        recovery_scope='Eligibility uses the pre-declared scored-part rule. This offline output is not R7 recovery proof; recover the board through R7 before continuing.')


def checked_gain(path,plan):
    row=json.loads(Path(path).read_text())
    require(row.get('kind')=='p4-aec-reselection','decision-bound re-selection required')
    require(json.loads(Path(row['plan_file']).read_text())==plan,'re-selection plan differs')
    original=row.get('original_selection')
    expected=reselect(Path(row['plan_file']),Path(row['delay_file']),Path(row['root']),
        Path(original['path']) if original is not None else None)
    require(row==expected and row['rescored_selection']['passed'],'authentic eligible deduplicated re-selection required')
    return row['rescored_selection']['selected_gain_db']


def guard(args,plan):
    context=scoring_context()
    for path in args.previous:
        require(json.loads(Path(path).read_text()).get('scoring_context')==context,'previous guard used a different scorer/decision')
    return measure.acquire_guard(args,plan,gain_checker=checked_gain,plan_validator=validate_frozen_plan,
        prompt_scorer=direction_prompt_score,scoring_metadata=dict(scoring_context=context))


def recommendation(plan_path,gain_path,guard_paths):
    context=scoring_context()
    for path in guard_paths:
        require(json.loads(Path(path).read_text()).get('scoring_context')==context,'guard scorer/decision provenance differs')
    result=measure.recommendation(Path(plan_path),Path(gain_path),[Path(p) for p in guard_paths],
        gain_checker=checked_gain,plan_validator=validate_frozen_plan,prompt_scorer=direction_prompt_score)
    return dict(result,scoring_context=context)


def recommendation_for_record(plan_path,gain_path,guard_paths):
    # Keep previously reviewed non-fix4 records on their original replay path.
    path=Path(gain_path)
    if path.is_file() and json.loads(path.read_text()).get('kind')=='p4-aec-reselection':
        return recommendation(plan_path,gain_path,guard_paths)
    return measure.recommendation(Path(plan_path),path,[Path(p) for p in guard_paths])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    select=sub.add_parser('select-gain')
    for name in ('plan','delay','root','out'):select.add_argument('--'+name,type=Path,required=True)
    select.add_argument('--original-selection',type=Path)
    recommend=sub.add_parser('recommend')
    for name in ('plan','gain-selection','out'):recommend.add_argument('--'+name,type=Path,required=True)
    recommend.add_argument('--guard-results',type=Path,nargs='+',required=True)
    live=sub.add_parser('guard')
    for name in ('plan','gain-selection','out','bot-dir','bot-identity','restore-record'):
        live.add_argument('--'+name,type=Path,required=True)
    live.add_argument('--restore-record-sha256',required=True)
    live.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    live.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=smoke.FACTORY_BACKUP)
    live.add_argument('--candidate-index',type=int,choices=range(len(policy.GUARDS)),required=True)
    live.add_argument('--previous',type=Path,nargs='*',default=[])
    args=parser.parse_args(argv)
    if args.action=='select-gain':
        require(not args.out.exists(),'fresh re-selection output required')
        require(not args.out.resolve().is_relative_to(args.root.resolve()),'new selection output must be outside original grid')
        original=args.original_selection
        if original is None and (args.root.parent/'gain-selection.json').is_file():original=args.root.parent/'gain-selection.json'
        result=reselect(args.plan,args.delay,args.root,original);policy.write_new(args.out,result)
        return 0 if result['rescored_selection']['passed'] else 1
    if args.action=='recommend':
        policy.write_new(args.out,recommendation(args.plan,args.gain_selection,args.guard_results));return 0
    plan=json.loads(args.plan.read_text());validate_frozen_plan(plan)
    args.bundle=Path(plan['bundle']);args.board='p4xc';args.reference_only=False;args.calibration=True;args.headless=True
    args.p4_restore_bundle=None;args.p4_restore_elf=None
    result=guard(args,plan)
    if result.get('status')=='preflight-noisy':return 2
    if result.get('status')=='preflight-error':return measure.PREFLIGHT_ERROR_EXIT
    return 0 if result['acquisition_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
