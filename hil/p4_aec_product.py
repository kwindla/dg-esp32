#!/usr/bin/env python3
"""AEC-enabled product/contention adapters over frozen P4 restore and HIL code."""
import argparse
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path

import board_smoke as smoke
import p4_contention as contention
import p4_audio_fix4 as fix4
from make_bundle import digest,verify_bundle
from wire_contract import require,compare,parse_capture
from p4_aec_policy import write_new


def record_untouched(filename):
    """Retain host preflight failures without consuming a hardware original."""
    def decorate(operation):
        @wraps(operation)
        def run(args):
            require(not args.out.exists(),'fresh live output directory required')
            args._device_touched=False;args._preflight_room=None
            try:return operation(args)
            except BaseException as exc:
                if args._device_touched:raise
                noisy=bool(args._preflight_room and args._preflight_room.get('noise_contaminated'))
                row=dict(acquisition_passed=False,qualification=False,board=args.board,device_untouched=True,
                    score_produced=False,retryable=True,status='preflight-noisy' if noisy else 'preflight-error',
                    error=type(exc).__name__+': '+str(exc),room=args._preflight_room)
                args.out.mkdir(parents=True,exist_ok=True);write_new(args.out/filename,row)
                return 2 if noisy else 3
        return run
    return decorate


class ProductIO(fix4.VoiceIO):
    def flash(self,bundle,manifest,label):
        if label=='candidate':self.args._device_touched=True
        return super().flash(bundle,manifest,label)

    def boot(self,board):
        at=super().boot(board)
        lines=[line for _,_,line in board.lines_since(at)]
        (self.out/'boot.log').write_text('\n'.join(lines)+'\n')
        if getattr(getattr(self,'args',None),'_applied_calibration',None):
            from p4_aec_applied import readback
            proof=readback(lines,self.manifest,self.args._applied_calibration['selected'])
            self.applied_readback=proof
        return at

    def finish(self,board,label,*,hold=False):
        lines,capability=smoke.LiveIO.finish(self,board,label,hold=hold)
        if label=='failed':return lines,capability
        capture,_,_=fix4.product_capture(lines,capability,manifest=self.manifest,board=self.args.board,
            calibration=False,bounds=smoke.bounds_for(self.args.bounds,self.manifest['profile']))
        image_audio(capture,self.manifest,self.args.board)
        generation=capture.intervals[0]['gen']
        samples=[r for r in getattr(self,'stack_samples',[]) if r.get('generation')==generation]
        require(len(samples)>=2,'before/after stack snapshots from the measured generation required')
        report=dict(label=label,generation=generation,**fix4.stack_report(samples,board=self.args.board))
        if not hasattr(self,'task_stack_reports'):self.task_stack_reports=[]
        self.task_stack_reports.append(report)
        require(report['stack_checks_passed'],'; '.join(report['stack_findings']))
        self.product_evidence_verified=True
        return lines,capability


def image_audio(capture,manifest,board):
    audio=capture.records['start']['body']['audio']
    require(audio['aec'] is True and audio['ref_gain_db']==manifest.get('boot_ref_gain_db'),'wire AEC/gain differs from boot image')
    if board=='p4xc':
        calibration=manifest.get('p4_aec_calibration',{})
        require(audio['ref_gain_db']==calibration.get('ref_gain_db') and audio['guard_ms']==calibration.get('guard_ms'),
            'wire gain/guard differs from P4 image settings')


def aec_manifest(bundle,board,normal=False,*,llm_content='off'):
    manifest,_=verify_bundle(bundle,board)
    if llm_content=='report-only':
        require(manifest.get('aec_enabled') is True and manifest.get('profile') in ('aec-normal','aec-demo'),
            '--llm-content report-only requires an AEC-enabled product bundle')
    allowed=('aec-normal',) if normal else ('aec-normal','aec-demo')
    require(manifest['aec_enabled'] is True and manifest['profile'] in allowed,'AEC bundle required')
    return manifest


def applied_gate(manifest,path):
    """Only a follow-up build can apply calibration; runtime staging is separate."""
    if path is None:
        require(manifest.get('p4_aec_calibration',{}).get('status')!='APPLIED','APPLIED image requires its recommendation')
        return dict(status='baseline; unmeasured settings; no applied-calibration claim')
    row=json.loads(Path(path).read_text())
    if row.get('kind')=='p4-aec-event-guard-recommendation':
        from p4_aec_applied import checked_recommendation,calibration_labels
        expected=checked_recommendation(path)
        require(manifest.get('p4_aec_calibration',{}).get('status')=='APPLIED' and
            manifest['p4_aec_calibration'].get('recommendation_sha256')==digest(path),'applied image recommendation stamp differs')
        calibration_labels(manifest['p4_aec_calibration'],row['delay_provenance'])
    else:
        from p4_aec_reselect import recommendation_for_record as recommendation
        expected=recommendation(Path(row['plan_file']),Path(row['gain_file']),[Path(p) for p in row['guard_files']])
    require(row==expected and row['passed'],'authentic calibration recommendation required')
    current=manifest.get('p4_aec_calibration',{})
    wanted=row['selected']
    require(all(type(current.get(k)) is int and current[k]==wanted[k] for k in ('ref_gain_db','guard_ms','rearm_ms','afe_delay_samples')),
        'selected values are not applied: follow-up Kconfig/timing rebuild, image/headroom checks and fresh bundle required')
    require(manifest['elf_sha256']!=row['source_elf_sha256'],'fresh follow-up image required before applied-calibration smoke')
    return dict(status='selected values match rebuilt image',recommendation_file=str(Path(path).resolve()),
        recommendation_sha256=digest(path),selected=wanted,delay_provenance=row.get('delay_provenance'))


@record_untouched('result.json')
def smoke_live(args):
    import boards
    from alsa import record_room_baseline
    from p4_step14 import restoring,adapters,restore_record
    from p4_product import candidate_storage
    boards.require_bench();manifest=aec_manifest(args.bundle,args.board,llm_content=getattr(args,'llm_content','off'))
    restore_record(args.restore_record,args.restore_record_sha256)
    applied=applied_gate(manifest,args.applied) if args.board=='p4xc' else None
    args._applied_calibration=applied
    erle=None
    if args.board=='p4xc':
        require(args.erle_report is not None,'P4 smoke requires an authenticated normal-image ERLE/residual report')
        if args.erle_report.is_file() and json.loads(args.erle_report.read_text()).get('kind')=='p4-aec-applied-image-report':
            from p4_aec_image_measure import checked_report
        else:
            from p4_aec_measure import checked_report
        erle=checked_report(args.erle_report)
        require(erle['gain_db']==manifest['boot_ref_gain_db'],'ERLE report gain differs from actual smoke image')
        require(erle['elf_sha256']==manifest['elf_sha256'] and erle['profile']==manifest['profile'],'ERLE report image differs')
    # The room capture has its own fresh sibling path, so a noisy preflight
    # never creates a scored smoke directory or flashes a device.
    room=record_room_baseline(args.out.with_name(args.out.name+'-room'),'AEC product smoke')
    args._preflight_room=room
    require(not room['noise_contaminated'],'room preflight failed; retry later in a new unscored path')
    identity=json.loads(args.bot_identity.read_text())
    with restoring(args.restore_record,args.restore_record_sha256),adapters(),candidate_storage(args.board,args.bundle,args.out):
        io=ProductIO(args,args.out,manifest,identity)
        from p4_smoke_rescore import content_mode,finalize_live
        with content_mode(getattr(args,'llm_content','off')):
            result=smoke.execute(args,backend=io)
        if getattr(args,'llm_content','off')=='report-only':finalize_live(result)
    write_new(args.out/'image.json',manifest)
    # Product profile/stack validation stays inside VoiceIO.finish, before
    # the unchanged executor can declare acquisition successful.
    require(not result['acquisition_passed'] or io.product_evidence_verified,'missing product wire validation')
    result.update(room=room,applied_calibration=applied,erle_report=erle,
        erle_scope='report-only; separate controlled acquisition on this exact image and gain',
        task_stack_reports=getattr(io,'task_stack_reports',[]),
        applied_readback=getattr(io,'applied_readback',None),
        helper_sha256=digest(Path(__file__)),qualification=False,device_untouched=not args._device_touched)
    if result.get('error') and not args._device_touched:
        result.update(status='preflight-error',retryable=True,score_produced=False)
    smoke.write(args.out/'result.json',result)
    if result.get('status')=='preflight-error':return 3
    return 0 if result['acquisition_passed'] and not result.get('error') else 1


def pair(left,right,bounds_path):
    sources=[];identities=[];captures=[];llm_content=[];variant=pair_id=None
    for directory,board in ((Path(left),'echoear'),(Path(right),'p4xc')):
        source=directory/'result.json';row=json.loads(source.read_text())
        require(row.get('board')==board and row.get('acquisition_passed') is True and not row.get('error') and
                not row.get('cleanup_errors') and row.get('restore',{}).get('clean') is True,'clean original S3/P4 AEC acquisitions required')
        require(row['variant'] in ('aec-normal','aec-demo'),'AEC product variant required')
        if variant is None:variant=row['variant']
        require(row['variant']==variant,'matching AEC variants required')
        require(isinstance(row.get('pair_id'),str) and bool(row['pair_id']),'missing pair id')
        if pair_id is None:pair_id=row['pair_id']
        require(row['pair_id']==pair_id,'pair id differs')
        if board=='p4xc':require(row['restore'].get('action')=='p4-product-of-record','pinned normal P4 restore required')
        expected_labels=['main','resume','cold'] if variant.endswith('demo') else ['main']
        require([s['label'] for s in row['sessions']]==expected_labels,'missing/reordered product sessions')
        image_path=directory/'image.json';image=json.loads(image_path.read_text())
        require(image.get('aec_enabled') is True and image.get('profile')==variant and
            image.get('elf_sha256')==row['elf_sha256'],'image/result ELF or AEC profile differs')
        if board=='p4xc' and image.get('p4_aec_calibration',{}).get('status')=='APPLIED':
            require(bool(row.get('applied_calibration')),'APPLIED pair member lacks recommendation evidence')
        smoke.boot_check((directory/'boot.log').read_text().splitlines(),image,board)
        if board=='p4xc' and row.get('applied_calibration'):
            from p4_aec_applied import readback
            applied=applied_gate(image,Path(row['applied_calibration']['recommendation_file']))
            require(applied==row['applied_calibration'],'applied smoke provenance differs')
            require(readback((directory/'boot.log').read_text().splitlines(),image,applied['selected'])==row.get('applied_readback'),
                'applied smoke readback differs')
        identity=json.loads((directory/'identity.json').read_text());require(identity==row['identity'],'identity changed')
        identities.append(identity);local=[];files={str(source):digest(source),str(directory/'identity.json'):digest(directory/'identity.json')}
        for label in expected_labels:
            serial=directory/(label+'-serial.log');bot=directory/(label+'-bot.log')
            require('\x1b[' not in bot.read_text(),'uncoloured bot evidence required')
            lines=serial.read_text().splitlines()
            closed=smoke.slice_sessions(lines);require(len(closed)==1,'one closed original session required')
            lines=closed[0]+[l for l in bot.read_text().splitlines() if 'DEMO_CAPABILITY ' in l]
            capture=parse_capture(lines);image_audio(capture,image,board)
            local.append(capture);files.update({str(serial):digest(serial),str(bot):digest(bot)})
        files[str(directory/'boot.log')]=digest(directory/'boot.log')
        files[str(image_path)]=digest(image_path)
        captures.append(local);sources.append(files)
        llm_content.append(dict(board=board,llm_content_mode=row.get('llm_content_mode','off'),
            finding_count=len(row.get('llm_content_findings',[]))))
    bounds=smoke.bounds_for(bounds_path,variant)
    measured=[compare(a,b,profile='product',left_aec=True,right_aec=True,bounds=bounds,left_target='esp32s3',right_target='esp32p4',
        left_identity=identities[0],right_identity=identities[1]) for a,b in zip(*captures)]
    return dict(checks_passed=True,qualification=False,variant=variant,pair_id=pair_id,sources=sources,
                bounds_sha256=digest(bounds_path),measured=measured,llm_content=llm_content)


@contextmanager
def aec_contention_validation():
    """Only change the AEC intent passed to the unchanged 13b validator.

    Source wire bytes stay original. All normal-13b timing/resource/workload
    gates, reference hashes and restart rules execute unchanged.
    """
    import wire_contract
    previous=wire_contract.validate_capture
    def validate(capture,**kwargs):
        require(kwargs.get('profile')=='bench-display' and kwargs.get('intended_aec') is False,
                'unexpected legacy contention validation call')
        return previous(capture,**dict(kwargs,intended_aec=True))
    wire_contract.validate_capture=validate
    try:yield
    finally:wire_contract.validate_capture=previous


def score_contention(directory,bounds_path,acquisition=None):
    directory=Path(directory)
    row=json.loads((directory/'acquisition.json').read_text()) if acquisition is None else acquisition
    require(row.get('aec_enabled') is True and row.get('variant')=='aec-normal','AEC-normal contention acquisition required')
    with aec_contention_validation():result=contention.score(directory,bounds_path,acquisition=row)
    reports=[]
    applied_readback=None
    if (directory/'image.json').is_file():
        image=json.loads((directory/'image.json').read_text())
        if image.get('p4_aec_calibration',{}).get('status')=='APPLIED':
            require(bool(row.get('applied_calibration')),'APPLIED contention lacks recommendation evidence')
    if row.get('applied_calibration'):
        from p4_aec_applied import readback
        manifest=json.loads((directory/'image.json').read_text())
        applied=applied_gate(manifest,Path(row['applied_calibration']['recommendation_file']))
        require(applied==row['applied_calibration'],'applied contention provenance differs')
        applied_readback=readback((directory/'serial.log').read_text().splitlines(),manifest,applied['selected'])
    for session in row['sessions']:
        samples=session['samples']
        require(all(s.get('aec',{}).get('enabled') is True and all(type(s['aec'].get(k)) is int and s['aec'][k]==0
                    for k in ('errors','reference_clips','output_clips')) for s in samples),'AEC contention error/clip evidence unavailable or nonzero')
        stack=fix4.stack_report(samples,board=row['board']);require(stack['stack_checks_passed'],'AEC contention stack floor')
        reports.append(stack)
        if applied_readback is not None:
            image_audio(parse_capture((directory/session['wire_file']).read_text().splitlines()),manifest,row['board'])
            require(all(type(s['aec'].get('reference_health',{}).get('gain_db')) is int and
                s['aec']['reference_health']['gain_db']==applied['selected']['ref_gain_db'] for s in samples),
                'applied contention PERF reference gain differs')
    return dict(result,aec_enabled=True,task_stack_reports=reports,applied_readback=applied_readback)


@record_untouched('acquisition.json')
def contention_live(args):
    import boards
    from p4_audio import HardwareIO
    from p4_step14 import restoring,adapters,restore_record
    from p4_product import candidate_storage
    from alsa import record_room_baseline
    boards.require_bench();manifest=aec_manifest(args.bundle,args.board,True)
    restore_record(args.restore_record,args.restore_record_sha256)
    applied=applied_gate(manifest,args.applied) if args.board=='p4xc' else None
    contention.normal_references(args.bounds) # 13b's exact unchanged 68411/7000/.95 bounds.
    identity=json.loads(args.bot_identity.read_text());contention.bot_identity(args,identity)
    os.environ['STEP9A_FIXTURES']=str(contention.ROOT/'hil/fixtures/echoear360')
    args.out.mkdir(parents=True,exist_ok=False);guard=None
    result=dict(acquisition_passed=False,qualification=False,board=args.board,limits=contention.LIMITS,
        aec_enabled=True,variant='aec-normal',elf_sha256=manifest['elf_sha256'],device_untouched=True,score_produced=False)
    result['applied_calibration']=applied
    try:
        room=record_room_baseline(args.out/'room','AEC contention');result['room']=room
        if room['noise_contaminated']:result.update(status='preflight-noisy',retryable=True);return 2
        write_new(args.out/'identity.json',identity);write_new(args.out/'image.json',manifest)
        with restoring(args.restore_record,args.restore_record_sha256),adapters(),candidate_storage(args.board,args.bundle,args.out):
            io=HardwareIO(args,manifest);io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();args._device_touched=True;result['device_untouched']=False
                result.update(contention.acquire(args,io,manifest,identity))
                assessment=score_contention(args.out,args.bounds,dict(result,restore=dict(clean=True)))
                guard.success=True
            result['restore']=guard.record;require(guard.record.get('clean') is True and
                (args.board!='p4xc' or guard.record.get('action')=='p4-product-of-record'),'unclean or wrong contention restore')
        write_new(args.out/'score.json',assessment)
        result['score_produced']=True
    except BaseException as exc:
        result.update(acquisition_passed=False,error=type(exc).__name__+': '+str(exc),retryable=True,
            status='preflight-error' if not args._device_touched else 'acquisition-error')
        if guard is not None:result['restore']=guard.record
    finally:
        try:write_new(args.out/'acquisition.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return 3 if result.get('status')=='preflight-error' else 0 if result['acquisition_passed'] else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    pair_parser=sub.add_parser('pair')
    pair_parser.add_argument('left',type=Path);pair_parser.add_argument('right',type=Path)
    pair_parser.add_argument('--out',type=Path,required=True);pair_parser.add_argument('--bounds',type=Path,default=smoke.BOUNDS_FILE)
    for action in ('smoke','contention'):
        p=sub.add_parser(action);p.add_argument('--board',choices=('p4xc','echoear'),required=True)
        for name in ('bundle','out','bot-dir','bot-identity','restore-record'):p.add_argument('--'+name,type=Path,required=True)
        p.add_argument('--restore-record-sha256',required=True);p.add_argument('--applied',type=Path)
        p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
        p.add_argument('--bounds',type=Path,default=smoke.BOUNDS_FILE)
        p.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=smoke.FACTORY_BACKUP)
        if action=='smoke':
            p.add_argument('--llm-content',choices=('off','report-only'),default='off',
                help='report-only: retain LLM content misses and continue; STT and every other gate stay strict')
            p.add_argument('--pair-id',required=True);p.add_argument('--erle-report',type=Path,
                help='P4: authenticated same-image report-gain output; required for P4 smoke')
        else:
            p.add_argument('--commands',type=Path,required=True)
            p.add_argument('--server-url',default='http://192.168.7.222:7861')
    args=parser.parse_args(argv)
    if args.action=='pair':write_new(args.out,pair(args.left,args.right,args.bounds));return 0
    args.reference_only=False;args.p4_restore_bundle=None;args.p4_restore_elf=None
    args.headless=False;args.calibration=False;args.coverage=.95;args.coverage_decision=None
    print('Coordinator runner PID='+str(os.getpid()),flush=True)
    return smoke_live(args) if args.action=='smoke' else contention_live(args)


if __name__=='__main__':raise SystemExit(main())
