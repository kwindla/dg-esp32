#!/usr/bin/env python3
"""Same-image applied AEC reports and report-only double talk; no gain selection."""
import argparse
import json
from pathlib import Path

import board_smoke as smoke
import p4_aec_measure as measure
import p4_aec_policy as policy
import p4_aec_product as product
from p4_aec_applied import checked_recommendation
from p4_aec_reselect import direction_prompt_score
from make_bundle import digest
from wire_contract import require


def make_plan(bundle,recommendation):
    bundle=Path(bundle).resolve();recommendation=Path(recommendation).resolve()
    manifest=product.aec_manifest(bundle,'p4xc');product.applied_gate(manifest,recommendation)
    plan=policy.make_plan(bundle)
    return dict(plan,kind='p4-aec-applied-image-plan',applied_recommendation=str(recommendation),
        recommendation_sha256=digest(recommendation),adapter_sha256=digest(Path(__file__)),
        replay_adapter_sha256=digest(Path(__file__).with_name('p4_aec_applied.py')))


def validate_plan(plan):
    require(plan==make_plan(Path(plan['bundle']),Path(plan['applied_recommendation'])),'applied image plan changed')
    return plan


def image_delay(path,plan):
    path=Path(path).resolve()
    require(str(path)==plan['applied_recommendation'] and digest(path)==plan['recommendation_sha256'],
        'same authenticated applied recommendation required')
    row=checked_recommendation(path);original=row['delay_provenance']
    return dict(original,record_file=str(path),record_sha256=digest(path),
        original_delay_record_file=original['record_file'],original_delay_record_sha256=original['record_sha256'],
        source_elf_sha256=row['source_elf_sha256'],applied_elf_sha256=plan['elf_sha256'],
        application='unchanged AFE implementation delay explicitly adopted through the approved recommendation')


def gain_report(plan_path,root):
    plan_path=Path(plan_path);plan=json.loads(plan_path.read_text())
    row=measure.gain_selection(plan,plan_path,Path(plan['applied_recommendation']),Path(root),30,
        plan_validator=validate_plan,delay_reader=image_delay,prompt_scorer=direction_prompt_score,
        acquisition_prompt_scorer=direction_prompt_score)
    return dict(row,kind='p4-aec-applied-image-report',qualification=False,report_only=True)


def checked_report(path):
    row=json.loads(Path(path).read_text())
    require(row.get('kind')=='p4-aec-applied-image-report' and row==gain_report(Path(row['plan_file']),Path(row['root'])) and
        row['available'],'authentic available same-image applied ERLE report required')
    return row


def double_talk(args,plan):
    provenance=image_delay(Path(plan['applied_recommendation']),plan)
    spec=next(s for s in plan['fixtures'] if s['id']==args.prompt)
    require(args.prompt in ('france','speech') and plan['profile']=='aec-normal','normal-image France/speech repeat only')
    def body(io,board):
        io.configure(board,30,400,2000);quiet=io.quiet(board,args.out/'quiet.npz');io.begin(board,'manual');trials=[]
        for index in range(5):
            original=io.reply(board,args.out/f'trial-{index+1:02d}',plan['fixtures'][0],spec)
            pcm=measure.load_capture(original['capture'])
            policy.reference_gain(original['before'],original['after'],30)
            prompt=direction_prompt_score(spec,original['events'],original['near_playback'] or dict(request_monotonic_s=0),
                original['bot_started_ns'] or 0,original['bot_stopped_ns'] or 0)
            try:erle=policy.echo_score(pcm,measure.load_capture(quiet),1024,original['events'],original['complete'])
            except ValueError as exc:erle=dict(available=False,error=str(exc),median_erle_db=None)
            request=(original['near_playback'] or {}).get('request_monotonic_s',0)*1e9
            interrupted=any(e.get('frame')=='InterruptionFrame' and e['monotonic_ns']>=request
                for e in smoke.downstream_frames(original['events']))
            trials.append(dict(index=index+1,original=original,transcript=prompt['received'],accuracy=prompt['accuracy'],
                interruption=interrupted,prompt=prompt,erle=erle,
                median_erle_db=erle.get('median_erle_db'),clips=policy.clip_evidence(original['before'],original['after'],pcm)))
        return dict(prompt_id=args.prompt,report_only=True,qualification=False,quiet=quiet,trials=trials,
            delay_provenance=provenance,scope='double-talk diagnostics only; no re-selection or performance acceptance gate')
    return measure.held(args,plan,body,plan_validator=validate_plan)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    plan=sub.add_parser('plan')
    for name in ('bundle','recommendation','out'):plan.add_argument('--'+name,type=Path,required=True)
    report=sub.add_parser('report-gain')
    for name in ('plan','root','out'):report.add_argument('--'+name,type=Path,required=True)
    for action in ('gain','double-talk'):
        live=sub.add_parser(action)
        for name in ('plan','out','bot-dir','bot-identity','restore-record'):live.add_argument('--'+name,type=Path,required=True)
        live.add_argument('--restore-record-sha256',required=True)
        live.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
        live.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=smoke.FACTORY_BACKUP)
        if action=='gain':live.add_argument('--part',type=int,choices=range(4),required=True)
        else:live.add_argument('--prompt',choices=('france','speech'),required=True)
    args=parser.parse_args(argv)
    if args.action=='plan':policy.write_new(args.out,make_plan(args.bundle,args.recommendation));return 0
    if args.action=='report-gain':
        row=gain_report(args.plan,args.root);policy.write_new(args.out,row);return 0 if row['available'] else 1
    plan=json.loads(args.plan.read_text());validate_plan(plan)
    args.bundle=Path(plan['bundle']);args.board='p4xc';args.reference_only=False;args.calibration=True;args.headless=True
    args.p4_restore_bundle=None;args.p4_restore_elf=None
    if args.action=='gain':
        args.gain_db=30;args.delay=Path(plan['applied_recommendation'])
        result=measure.acquire_gain(args,plan,plan_validator=validate_plan,delay_reader=image_delay,prompt_scorer=direction_prompt_score)
    else:result=double_talk(args,plan)
    if result.get('status')=='preflight-noisy':return 2
    if result.get('status')=='preflight-error':return measure.PREFLIGHT_ERROR_EXIT
    return 0 if result['acquisition_passed'] else 1


if __name__=='__main__':raise SystemExit(main())
