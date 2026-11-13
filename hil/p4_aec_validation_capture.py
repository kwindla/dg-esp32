#!/usr/bin/env python3
"""Step 15d validation: pinned plan, resumable coordinator parts, offline replay."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import p4_aec_measure as measure
import p4_aec_policy as policy
import p4_aec_validation as validation
from p4_nearend_capture import NearendIO,ELFS
from make_bundle import digest,verify_bundle
from wire_contract import require

ROOT=Path(__file__).resolve().parents[1]
RESTORE_SHA='730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a'
PARTS=tuple(dict.fromkeys(row['part'] for row in validation.schedule()))
# Runtime acquisition/replay dependencies only; unrelated helpers and tests do
# not invalidate an overnight plan. Keep lazy live imports here as well.
SOURCES=tuple('hil/'+name for name in (
    'p4_aec_validation.py','p4_aec_validation_capture.py','p4_aec_measure.py','p4_aec_policy.py',
    'p4_aec_reselect.py','p4_nearend_capture.py','p4_nearend_analysis.py','board_smoke.py',
    'p4_step14.py','p4_product.py','p4_audio.py','p4_audio_fix4.py','p4_defaults_validate.py',
    'alsa.py','boards.py','board_serial.py','board_port.py','esp_serial.py','make_bundle.py',
    'wire_contract.py','atomic_json.py','playback_quality.py','speech_accuracy.py',
    'echoear_aec_alignment.py','echoear_aec_scoring.py','echoear_aec_metrics.py',
    'echoear_aec_capture.py','echoear_aec_timing.py','echoear_aec_bench.py','echoear_aec_tts.py',
    'dump_verify.py','echoear_hot_stt.py','echoear_item2_campaign.py','echoear_restore_verify.py',
    'echoear_rollback.py','demo_session.py','step9a_assets.py','step9a_merge_checks.py',
    'acoustic_response.py','bench_calibration.py','e2e_common.py','echoear_acoustic_qualification.py',
    'mic_headroom.py','p4_seed_derive.py','speaker_dual_sweep_test.py','speaker_sweep_test.py',
    'step6b_checks.py','step7_support.py','transport_test.py','uplink_continuity.py'))
REPLAY_POLICY='whole-attempt-success-only; completed prefixes of failed attempts are unavailable'



def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def make_plan(bundle,root,bot_identity,restore_record):
    from p4_step14 import restore_record as check_restore
    import board_smoke as smoke
    bundle=Path(bundle).resolve();root=Path(root).resolve()
    require(not root.is_relative_to(bundle) and not root.is_relative_to(ROOT),'evidence must be outside bundle and worktree')
    manifest,_=verify_bundle(bundle,'p4xc')
    require(manifest['profile']=='aec-normal' and manifest['aec_enabled'] is True and manifest['autostart'] is False and
        manifest['elf_sha256']==ELFS['p4xc'] and manifest['boot_ref_gain_db']==30,'exact fix-2 P4 AEC normal image required')
    calibration=manifest['p4_aec_calibration']
    require(all(calibration.get(k)==v for k,v in dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024).items()),
        'unchanged applied boot settings required')
    check_restore(Path(restore_record),RESTORE_SHA)
    identity=json.loads(Path(bot_identity).read_text());smoke.identity_schema(identity)
    return dict(schema=1,kind='step-15d-validation-plan',qualification=False,
        bundle=str(bundle),root=str(root),elf_sha256=manifest['elf_sha256'],manifest_sha256=digest(bundle/'manifest.json'),
        profile=manifest['profile'],fixtures=policy.fixtures(),rules=validation.RULES,schedule=validation.schedule(),
        bot_identity=str(Path(bot_identity).resolve()),bot_identity_sha256=digest(bot_identity),identity=identity,
        restore_record=str(Path(restore_record).resolve()),restore_record_sha256=RESTORE_SHA,
        source_sha256={name:digest(ROOT/name) for name in SOURCES},
        state='fresh boot per part; fresh peer per double-talk trial; started trials consumed before stimulus, including crashes',
        replay_policy=REPLAY_POLICY,guard=validation.guard_design())


def validate_plan(plan):
    require(plan==make_plan(plan['bundle'],plan['root'],plan['bot_identity'],plan['restore_record']),
        'plan, bundle, identity, restore, fixture, source or rules changed')
    return plan


class ValidationIO(NearendIO):
    """Read boot settings; never call a gain, guard or delay setter."""
    def delay(self,board,folder,spec):
        folder.mkdir();cursor=board.cursor
        self.start_capture(board);self.pause(board,.75)
        at=board.cursor;board.command('aec-probe',15)
        # The console ACK marks entry, not completion. Keep the complete 2.4 s
        # probe including its trailing silence, then another 1.25 s of margin.
        board.wait_for(r'DONE #\d+ aec-probe',15,since=at)
        self.pause(board,1.25);dac=self.stop_capture(board,folder/'dac.npz')
        self.pause(board,2.2) # Ensure the electrical reference is silent in the independent near run.
        self.start_capture(board);self.pause(board,.75)
        playback=self.play(board,Path(spec['path']));self.pause(board,1.25)
        near=self.stop_capture(board,folder/'near.npz')
        original=dict(dac=dac,near=near,near_playback=playback,
            console=[line for _,_,line in board.lines_since(cursor)])
        policy.write_new(folder/'original.json',original)
        return original


def consumed(plan,part):
    require(part in PARTS,'unknown validation part')
    wanted=[r for r in plan['schedule'] if r['part']==part];rows={}
    for start in sorted((Path(plan['root'])/part).glob('attempt-??/*/trial-start.json')):
        row=json.loads(start.read_text());spec=row.get('spec',{})
        require(row.get('plan_digest')==plan_digest(plan) and spec in wanted and start.parent.name==spec['id'],
            'foreign validation journal')
        require(spec['id'] not in rows,'duplicate consumed validation trial; no retakes')
        rows[spec['id']]=start.parent
    require(set(rows)=={r['id'] for r in wanted[:len(rows)]},'consumed trials must be the declared prefix')
    return rows


def original_record(folder,original):
    path=folder/'audio/original.json'
    require(json.loads(path.read_text())==original,'original differs from retained acquisition JSON')
    return dict(path=str(path.resolve()),sha256=digest(path))


def checked_original(record):
    require(digest(record['path'])==record['sha256'],'original changed')
    return json.loads(Path(record['path']).read_text())


def finalize_part(plan,part,attempt):
    """Seal only after the frozen holder has finalized cleanup and R7 restore.

    A crash before this receipt leaves the entire attempt unavailable. No prefix
    exception: even a retained trial needs a fully successful enclosing attempt.
    """
    files=['result.json','scored-original.json','post-body-payload.json',
        'room/result.json','room/room.wav','room/mixer.log']
    files += [str(p.relative_to(attempt)) for p in sorted(attempt.glob('*/trial-sealed.json'))]
    receipt=dict(kind='step-15d-validation-part-finalized',plan_digest=plan_digest(plan),
        plan_sha256=digest(Path(plan['root'])/'plan.json'),board='p4xc',elf_sha256=plan['elf_sha256'],part=part,
        restore_record=plan['restore_record'],restore_record_sha256=plan['restore_record_sha256'],
        files={name:digest(attempt/name) for name in files if (attempt/name).is_file()})
    policy.write_new(attempt/'part-finalized.json',receipt)


def replay_eligibility(plan,part,folder,trial):
    """Single admission gate used by reports, delay summaries and V3 admission."""
    attempt=folder.parent;path=attempt/'result.json'
    try:
        require(trial.get('status')=='retained' and 'error' not in trial and 'original' in trial,
            'trial finalization failed or status is not retained')
        receipt=json.loads((attempt/'part-finalized.json').read_text())
        expected=dict(kind='step-15d-validation-part-finalized',plan_digest=plan_digest(plan),
            plan_sha256=digest(Path(plan['root'])/'plan.json'),board='p4xc',elf_sha256=plan['elf_sha256'],part=part,
            restore_record=plan['restore_record'],restore_record_sha256=plan['restore_record_sha256'])
        require({k:receipt.get(k) for k in expected}==expected,'part finalization binding differs')
        required=['result.json','scored-original.json','post-body-payload.json',
            'room/result.json','room/room.wav','room/mixer.log',folder.name+'/trial-sealed.json']
        require(all(receipt['files'].get(name)==digest(attempt/name) for name in required),
            'part result, room evidence or trial receipt changed/missing')
        result=json.loads(path.read_text())
        require(result==json.loads((attempt/'scored-original.json').read_text()),'final scored part differs')
        require(result.get('kind')=='p4-aec-capture' and result.get('board')=='p4xc' and
            result.get('elf_sha256')==plan['elf_sha256'] and result.get('part')==part and
            result.get('plan_sha256')==expected['plan_sha256'] and result.get('validation_rules')==validation.RULES and
            result.get('identity')==plan['identity'],'enclosing part provenance differs')
        require(result.get('status')=='acquired' and result.get('acquisition_passed') is True and
            result.get('score_produced') is True and not result.get('error') and result.get('cleanup_errors')==[],
            'enclosing part did not finalize successfully; completed prefixes are ineligible')
        payload=attempt/'post-body-payload.json'
        require(result.get('post_body_payload_sha256')==digest(payload) and
            str(folder.resolve()) in json.loads(payload.read_text())['trials'],'trial absent from finalized part payload')
        room=json.loads((attempt/'room/result.json').read_text())
        require(result.get('room')==room and room.get('noise_contaminated') is False and
            room.get('recording_seconds')==3 and room.get('scored_start_seconds')==.2 and
            room.get('limit_dbfs')==-50 and type(room.get('baseline_dbfs')) in (int,float) and
            room['baseline_dbfs']<=-50 and Path(room['wav']).resolve()==attempt/'room/room.wav',
            'clean room-preflight evidence required')
        require(result.get('restore',{}).get('clean') is True and
            result['restore'].get('action')=='p4-product-of-record','clean pinned product restore required')
        from p4_step14 import restore_record
        require(plan['restore_record_sha256']==RESTORE_SHA,'unchanged R7 pin required')
        restore_record(Path(plan['restore_record']),RESTORE_SHA)
        return dict(eligible=True,policy=REPLAY_POLICY,result_file=str(path.resolve()),result_sha256=digest(path),
            finalization_sha256=digest(attempt/'part-finalized.json'))
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return dict(eligible=False,policy=REPLAY_POLICY,reason='ineligible consumed observation: '+str(exc))


def observations(plan,part):
    consumed_rows=consumed(plan,part);rows=[]
    for spec in [r for r in plan['schedule'] if r['part']==part]:
        folder=consumed_rows.get(spec['id']);row=dict(spec=spec,started=False,observed=False,available=False,reason='not started')
        if folder is not None:
            row.update(started=True,reason='started but no sealed original (crash/abort); consumed',folder=str(folder))
            seal=folder/'trial-sealed.json'
            if seal.exists():
                receipt=json.loads(seal.read_text());path=folder/'trial.json'
                require(receipt==dict(trial_sha256=digest(path),start_sha256=digest(folder/'trial-start.json')),
                    'sealed validation trial changed')
                trial=json.loads(path.read_text());require(trial['spec']==spec,'trial specification changed')
                row.update(trial)
                row['reason']=trial.get('error','sealed trial has no retained original')
                row['eligibility']=replay_eligibility(plan,part,folder,trial)
                if not row['eligibility']['eligible']:
                    row['reason']=row['eligibility']['reason'];rows.append(row);continue
                if 'original' in trial:
                    original=checked_original(trial['original'])
                    if spec['kind']=='double-talk':
                        row['missed_injection']=validation.missed_injection(original)
                    try:
                        if spec['kind']=='delay':
                            from echoear_aec_alignment import load_nearend_source
                            source=load_nearend_source(Path(plan['fixtures'][-1]['path']))
                            score=validation.delay_trial(measure.load_capture(original['dac']),measure.load_capture(original['near']),source)
                        else:
                            prompt=next(p for p in plan['fixtures'] if p['id']==spec['prompt'])
                            score=validation.double_talk_trial(prompt,original,measure.load_capture(original['capture']),
                                measure.load_capture(trial['quiet']))
                        row.pop('reason',None);row.update(score)
                        if spec['kind']=='double-talk':row['available']=row['observed']
                    except (ValueError,OSError,KeyError) as exc:
                        row.update(reason='unavailable replay: '+str(exc),observed=False,available=False)
        rows.append(row)
    return rows


def report(plan):
    validate_plan(plan)
    delay_rows=[row for part in PARTS[:3] for row in observations(plan,part)]
    delay=validation.delay_summary(delay_rows)
    double=[row for part in PARTS[3:] for row in observations(plan,part)]
    attempts={}
    for part in PARTS:
        for path in sorted((Path(plan['root'])/part).glob('attempt-??/result.json')):
            row=json.loads(path.read_text())
            require(row.get('plan_sha256')==digest(Path(plan['root'])/'plan.json'),'part plan file differs')
            attempts[str(path.resolve())]=dict(sha256=digest(path),status=row.get('status'),
                acquisition_passed=row.get('acquisition_passed'),restore=row.get('restore'),error=row.get('error'),
                cleanup_errors=row.get('cleanup_errors'))
    return dict(kind='step-15d-validation-report',qualification=False,report_only=True,
        plan_digest=plan_digest(plan),elf_sha256=plan['elf_sha256'],root=plan['root'],delay=delay,
        guard=validation.guard_design(),double_talk={
            prompt:validation.double_talk_summary([r for r in double if r['spec']['prompt']==prompt],15)
            for prompt in ('france','speech')},sets={str(group):{
                prompt:validation.double_talk_summary([r for r in double if r['spec']['set']==group and r['spec']['prompt']==prompt],5)
                for prompt in ('france','speech')} for group in (1,2,3)},attempts=attempts,
        complete=False,completion_reason='V2 is partially available but not run; the re-arm boundary needs scheduled-gap tooling; no full guard validation claim',
        caveat=validation.RULES['double_talk']['caveat'])


def acquire(args,plan,*,io_factory=ValidationIO):
    validate_plan(plan)
    require(args.part in PARTS and re.fullmatch(r'attempt-[0-9]{2}',args.out.name),'declared part and attempt-NN required')
    require(args.out.parent.resolve()==Path(plan['root'])/args.part,'output must match planned root/part')
    require(Path(args.plan).resolve()==Path(plan['root'])/'plan.json','canonical plan.json path required')
    for part in PARTS[:PARTS.index(args.part)]:
        require(len(consumed(plan,part))==sum(r['part']==part for r in plan['schedule']),
            'earlier parts must be consumed first')
    if args.part.startswith('double-talk'):
        result=validation.delay_summary([row for part in PARTS[:3] for row in observations(plan,part)])
        require(not result['stop_for_user_decision'],'measured-different delay; stop for user decision')
    prior=consumed(plan,args.part)
    specs=[r for r in plan['schedule'] if r['part']==args.part and r['id'] not in prior]
    require(specs,'part fully consumed; started originals cannot be retaken')
    # The frozen holder handles noisy preflight, authenticated R7 storage/restore,
    # exceptions, signals and cleanup. Its action is capture: each session below
    # is already disconnected before body completion.
    args.action='capture'
    def body(io,board):
        trials=[]
        for spec in specs:
            folder=args.out/spec['id'];folder.mkdir()
            policy.write_new(folder/'trial-start.json',dict(plan_digest=plan_digest(plan),spec=spec,started_ns=time.monotonic_ns()))
            trial=dict(spec=spec,status='unavailable')
            try:
                if spec['kind']=='delay':
                    original=io.delay(board,folder/'audio',plan['fixtures'][-1])
                    trial['original']=original_record(folder,original)
                else:
                    # A crash even during quiet acquisition consumes this trial.
                    quiet=io.quiet(board,folder/'quiet.npz');trial['quiet']=quiet
                    io.begin(board,'manual')
                    prompt=next(p for p in plan['fixtures'] if p['id']==spec['prompt'])
                    original=io.reply(board,folder/'audio',plan['fixtures'][0],prompt)
                    trial['original']=original_record(folder,original)
                    io.finish(board,spec['id'])
                trial['status']='retained'
            except BaseException as exc:
                trial['error']=type(exc).__name__+': '+str(exc);raise
            finally:
                policy.write_new(folder/'trial.json',trial)
                policy.write_new(folder/'trial-sealed.json',dict(trial_sha256=digest(folder/'trial.json'),
                    start_sha256=digest(folder/'trial-start.json')))
                trials.append(str(folder.resolve()))
        return dict(trials=trials,part=args.part,validation_rules=validation.RULES)
    result=measure.held(args,plan,body,io_factory=io_factory,plan_validator=validate_plan,
        metadata=dict(part=args.part,validation_rules=validation.RULES))
    finalize_part(plan,args.part,args.out)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('plan')
    for name in ('bundle','root','bot-identity','restore-record'):p.add_argument('--'+name,type=Path,required=True)
    for action in ('report','capture'):
        p=sub.add_parser(action);p.add_argument('--plan',type=Path,required=True)
        if action=='report':p.add_argument('--out',type=Path,required=True)
        else:
            p.add_argument('--part',choices=PARTS,required=True);p.add_argument('--attempt',type=int,required=True)
            p.add_argument('--bot-dir',type=Path,required=True)
            p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
            p.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=measure.smoke.FACTORY_BACKUP)
    args=parser.parse_args(argv)
    if args.action=='plan':
        plan=make_plan(args.bundle,args.root,args.bot_identity,args.restore_record)
        policy.write_new(Path(plan['root'])/'plan.json',plan);return 0
    plan=json.loads(args.plan.read_text());validate_plan(plan)
    require(args.plan.resolve()==Path(plan['root'])/'plan.json','canonical plan.json path required')
    if args.action=='report':
        row=report(plan);policy.write_new(args.out,row)
        if row['delay']['stop_for_user_decision']:return 4
        # V2 has not run; exit 1 is expected even when V1/V3 are complete.
        return 1
    require(1<=args.attempt<=99,'attempt must be 1..99')
    args.out=Path(plan['root'])/args.part/f'attempt-{args.attempt:02d}'
    args.bundle=Path(plan['bundle']);args.board='p4xc';args.reference_only=False;args.calibration=True;args.headless=True
    args.bot_identity=Path(plan['bot_identity']);args.restore_record=Path(plan['restore_record']);args.restore_record_sha256=RESTORE_SHA
    args.p4_restore_bundle=None;args.p4_restore_elf=None
    result=acquire(args,plan)
    if result.get('status')=='preflight-noisy':return 2
    if not result.get('acquisition_passed'):return 3
    if args.part.startswith('delay'):
        score=validation.delay_summary([row for part in PARTS[:3] for row in observations(plan,part)])
        policy.write_new(args.out/'delay-summary.json',score)
        print(json.dumps({key:score[key] for key in ('outcome','delay_samples','passing_repetitions','consumed_repetitions','stop_for_user_decision')}))
        if score['stop_for_user_decision']:return 4
    return 0


if __name__=='__main__':raise SystemExit(main())
