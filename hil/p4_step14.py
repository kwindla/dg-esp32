#!/usr/bin/env python3
"""Step 14 adapters over frozen product/smoke/contention helpers.

P4 live entrypoints require an explicit hash-pinned normal restore record.
Imports and pin/score operations never access hardware or start a bot.
"""
import argparse
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import time

import board_smoke as smoke
from make_bundle import digest, verify_bundle
from wire_contract import require


def pin(bundle, headroom, output):
    """Pin an already-built normal image, never infer a pin from a build path."""
    bundle=Path(bundle).resolve();headroom=Path(headroom).resolve()
    manifest,_=verify_bundle(bundle,'p4xc')
    require(manifest['profile']=='normal' and not manifest['aec_enabled'] and not manifest['autostart'],
            'restore-of-record requires P4 normal AEC-off')
    require(manifest.get('target_dependencies_lock')=='dependencies.p4xc.lock' and
            re.fullmatch(r'[0-9a-f]{64}',manifest.get('target_dependencies_lock_sha256','')) and
            manifest['dependencies_lock_sha256']==manifest['target_dependencies_lock_sha256'],
            'restore bundle needs board-aware lock provenance; regenerate with scripts/p4_bundle.py')
    from p4_product import storage_plan
    plan=storage_plan(bundle)
    report=json.loads(headroom.read_text())
    require(report.get('passed') is True,'P4 headroom gate required')
    require(report.get('elf_sha256')==manifest['elf_sha256'],'headroom evidence must identify the pinned ELF')
    record=dict(schema=1,board='p4xc',profile='normal',bundle=str(bundle),
        elf_sha256=manifest['elf_sha256'],manifest_sha256=digest(bundle/'manifest.json'),
        target_dependencies_lock_sha256=manifest['target_dependencies_lock_sha256'],
        headroom=str(headroom),headroom_sha256=digest(headroom),storage='erase named storage before boot',
        partition_table_sha256=plan['partition_table_sha256'],
        boot_verified=False,qualification=False,scope='R7 restore image pin; live boot proof still required')
    with Path(output).open('x') as stream:stream.write(json.dumps(record,indent=2)+'\n')
    return record


def restore_record(path, expected):
    path=Path(path).resolve()
    require(isinstance(expected,str) and re.fullmatch(r'[0-9a-f]{64}',expected) and digest(path)==expected,
            'restore record hash mismatch')
    record=json.loads(path.read_text());bundle=Path(record['bundle'])
    require(record.get('schema')==1 and record.get('board')=='p4xc' and record.get('profile')=='normal' and
            record.get('storage')=='erase named storage before boot','invalid P4 restore record')
    manifest,_=verify_bundle(bundle,'p4xc')
    require(manifest['profile']=='normal' and manifest['elf_sha256']==record['elf_sha256'] and
            digest(bundle/'manifest.json')==record['manifest_sha256'],'P4 restore pin mismatch')
    require(manifest.get('target_dependencies_lock_sha256')==record['target_dependencies_lock_sha256'],
            'restore target lock changed')
    require(digest(record['headroom'])==record['headroom_sha256'],'restore headroom evidence changed')
    from p4_product import storage_plan
    require(storage_plan(bundle)['partition_table_sha256']==record['partition_table_sha256'],'restore partition table changed')
    return record


def boot_proof(path,expected,out,factory_backup):
    """Coordinator-only one-shot proof, with factory as the failure restore.

    Never rely on the unbooted pin to recover its own failed proof. Existing
    signal protection/bench ownership remain in the frozen hold. No repinning.
    """
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    guard=None
    result=dict(passed=False,record_sha256=expected,kind='one-shot-restore-boot-proof',device_untouched=True)
    try:
        import boards
        from types import SimpleNamespace
        from p4_audio import HardwareIO
        from p4_product import candidate_storage
        boards.require_bench();record=restore_record(path,expected)
        result['elf_sha256']=record['elf_sha256']
        bundle=Path(record['bundle']);manifest,_=verify_bundle(bundle,'p4xc')
        args=SimpleNamespace(board='p4xc',bundle=bundle,out=out,reference_only=False,
            p4_restore_bundle=None,p4_restore_elf=None,p4_factory_backup=factory_backup)
        io=HardwareIO(args,manifest);io.preflight_tools();io.preflight_restore();guard=io.hold()
        with guard,candidate_storage('p4xc',bundle,out):
            guard.touch();result['device_untouched']=False;io.flash(bundle,manifest,'candidate')
            with io.open() as board:io.boot(board)
            guard.success=True
        result.update(passed=guard.record.get('clean') is True,restore=guard.record)
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc),
                      restore=guard.record if guard is not None else dict(device_untouched=True))
    finally:
        try:smoke.write(out/'restore-boot-proof.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return result


def settle_check(bundle,path,expected,out,factory_backup):
    """Coordinator-only short held-idle jitter witness before the long run."""
    import boards
    from types import SimpleNamespace
    from p4_audio import HardwareIO
    from p4_product import candidate_storage
    from p4_liveness import settled_heap,SETTLED
    boards.require_bench();manifest,_=verify_bundle(bundle,'p4xc')
    require(manifest['profile']=='normal','settle check uses the normal P4 image')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    args=SimpleNamespace(board='p4xc',bundle=Path(bundle),out=out,reference_only=False,
        p4_restore_bundle=None,p4_restore_elf=None,p4_factory_backup=factory_backup)
    class HeldIO(HardwareIO):
        def pause(self,board,seconds):time.sleep(seconds)
    result=dict(passed=False,board='p4xc',elf_sha256=manifest['elf_sha256'],
        manifest_sha256=digest(Path(bundle)/'manifest.json'),record_sha256=expected,policy=SETTLED,windows=[]);guard=None
    try:
        with restoring(path,expected),candidate_storage('p4xc',bundle,out):
            io=HeldIO(args,manifest);io.snapshot_index=0
            io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();io.flash(bundle,manifest,'candidate')
                with io.open() as board:
                    io.boot(board);board.command('demo hold',45)
                    for index in range(6):
                        started=time.monotonic_ns();sample,evidence=settled_heap(board,io)
                        result['windows'].append(dict(index=index,started_ns=started,ended_ns=time.monotonic_ns(),
                            free=sample['heap']['internal']['free'],**evidence))
                        if index<5:io.pause(board,5)
                guard.success=True
            result['restore']=guard.record
            require(guard.record.get('clean') is True and guard.record.get('action')=='p4-product-of-record',
                    'settle check requires a clean pinned-image restore')
            result['passed']=True
    except BaseException as exc:
        result['error']=type(exc).__name__+': '+str(exc)
        if guard is not None:result['restore']=guard.record
    finally:
        try:smoke.write(out/'settle-check.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return result


@contextmanager
def p4_fixture_sources():
    """Select one product fixture pack even after frozen preflight imports.

    prepare_board already uses disp provision-empty for unavailable storage;
    both its manifest and its HTTP origin must then use the same 360px pack.
    Only P4 preflight enters this scope. Frozen helpers and S3 stay unchanged.
    """
    import step9a_assets,step9a_support
    previous=(step9a_assets.ROOT,step9a_support.FIXTURES,os.environ.get('STEP9A_FIXTURES'))
    fixtures=Path(__file__).resolve().parent/'fixtures/echoear360'
    step9a_assets.ROOT=step9a_support.FIXTURES=fixtures
    os.environ['STEP9A_FIXTURES']=str(fixtures)
    try:yield
    finally:
        step9a_assets.ROOT,step9a_support.FIXTURES=previous[:2]
        if previous[2] is None:os.environ.pop('STEP9A_FIXTURES',None)
        else:os.environ['STEP9A_FIXTURES']=previous[2]


@contextmanager
def restoring(path, expected):
    """Bind all inherited LiveIO holds to R7, including failure/abort restores.

    The existing hold retains its signal mask, retry loop and bench lock. A
    restore storage error propagates into that same loop; factory is explicit
    deep recovery only, never an automatic fallback.
    """
    record=restore_record(path,expected) if path is not None else None
    previous_preflight,previous_flash=smoke.LiveIO.preflight_restore,smoke.LiveIO.flash
    previous_hold=smoke.LiveIO.hold
    import board_serial
    previous_open=board_serial.open_board;restore_io=None;fixtures=ExitStack()
    def preflight(io):
        if io.args.board=='p4xc':
            require(record is not None,'step 14 P4 live runs require --restore-record and --restore-record-sha256')
            restore_record(path,expected)
            # The frozen product preflight skips factory validation. Prove the
            # deep-recovery backup pair before any candidate write as well.
            fixtures.enter_context(p4_fixture_sources())
            io.args.p4_factory_backup=getattr(io.args,'p4_factory_backup',smoke.FACTORY_BACKUP)
            saved=(getattr(io.args,'p4_restore_bundle',None),getattr(io.args,'p4_restore_elf',None))
            io.args.p4_restore_bundle=None;io.args.p4_restore_elf=None
            try:previous_preflight(io)
            finally:io.args.p4_restore_bundle,io.args.p4_restore_elf=saved
            io.args.p4_restore_bundle=Path(record['bundle']);io.args.p4_restore_elf=record['elf_sha256']
        return previous_preflight(io)
    def flash(io,bundle,manifest,label):
        nonlocal restore_io
        if io.args.board=='p4xc' and label=='restore':
            if (io.out/'select-factory').exists():
                io.args.p4_restore_bundle=None;io.args.p4_restore_elf=None
                io.guard.success=False
                print('FACTORY SELECTED BY COORDINATOR: bench lock retained. '
                      'Wait for recovery hold; next SIGUSR1 resumes factory recovery in this runner.',flush=True)
                raise RuntimeError('factory selected by coordinator')
            checked=restore_record(path,expected)
            require(Path(bundle).resolve()==Path(checked['bundle']).resolve() and manifest['elf_sha256']==checked['elf_sha256'],
                    'unexpected P4 restore flash')
            restore_io=io
        result=previous_flash(io,bundle,manifest,label)
        if io.args.board=='p4xc' and label=='restore':
            from p4_product import prepare_storage
            # Retry attempts keep their own complete erase/readback evidence.
            number=1
            while (io.out/f'restore-storage-{number:03d}').exists():number+=1
            out=io.out/f'restore-storage-{number:03d}';out.mkdir()
            prepare_storage(bundle,out)
        return result
    def hold(io):
        guard=previous_hold(io)
        if io.args.board=='p4xc':
            original_restore=guard._restore
            def recorded_restore():
                result=original_restore()
                smoke.write(io.out/'restore.json',result)
                return result
            guard._restore=recorded_restore
        return guard
    @contextmanager
    def open_board(board,*args,**kwargs):
        is_restore=(board=='p4xc' and restore_io is not None and
            Path(kwargs.get('log_path','')).resolve()==(restore_io.out/'restore-serial.log').resolve())
        try:
            with previous_open(board,*args,**kwargs) as link:yield link
        except Exception:
            if is_restore:
                # The frozen hold next waits for SIGUSR1, then re-evaluates
                # restore_decision. Explicitly select factory deep recovery;
                # that signal must never retry an image which failed to boot.
                restore_io.args.p4_restore_bundle=None;restore_io.args.p4_restore_elf=None
                restore_io.guard.success=False
                print('PINNED IMAGE BOOT FAILED: factory deep recovery selected. Do not retry the pinned image. '
                      'Bench lock retained; SIGUSR1 resumes factory recovery in this runner.',flush=True)
            raise
    smoke.LiveIO.preflight_restore,smoke.LiveIO.flash=preflight,flash
    smoke.LiveIO.hold=hold
    board_serial.open_board=open_board
    try:yield
    finally:
        smoke.LiveIO.preflight_restore,smoke.LiveIO.flash=previous_preflight,previous_flash
        smoke.LiveIO.hold=previous_hold
        board_serial.open_board=previous_open
        fixtures.close()


def turn_checks(events,kind,aec,*,calibration=False):
    result=_turn_checks(events,kind,aec,calibration=calibration)
    if not calibration and (kind=='france' or kind=='talkover' and aec):
        city='paris' if kind=='france' else 'rome'
        negated=any(re.search(r"\b(?:not\s+|never\s+|isn't\s+)"+city+r'\b|\b(?:don.t|do not) know\b',
                             text,re.I) for text in result['assistants'])
        require(not negated,'qualification reply is off-script or negates '+city)
    return result


_turn_checks=smoke.turn_checks


def contention_summary(report):
    report=dict(report)
    if 'generations' in report and 'board' in report:
        unmeasured=any(row['resources']['capture_overflows_result']!='measured' for row in report['generations'])
    else:
        unmeasured=any(row['capture_overflows_result']!='measured' for board in report['capture_overflow_evidence']
                       for row in board['generations'])
    report.update(capture_overflows_summary='unmeasured (report-only)' if unmeasured else 'measured',
                  all_checks_measured=not unmeasured)
    return report


@contextmanager
def adapters():
    import p4_contention as contention
    previous_turn,previous_score,previous_pair=smoke.turn_checks,contention.score,contention.pair
    smoke.turn_checks=turn_checks
    contention.score=lambda *args,**kwargs:contention_summary(previous_score(*args,**kwargs))
    contention.pair=lambda *args,**kwargs:contention_summary(previous_pair(*args,**kwargs))
    try:yield
    finally:smoke.turn_checks,contention.score,contention.pair=previous_turn,previous_score,previous_pair


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    frozen=sub.add_parser('pin');frozen.add_argument('--bundle',type=Path,required=True)
    frozen.add_argument('--headroom',type=Path,required=True);frozen.add_argument('--out',type=Path,required=True)
    proof=sub.add_parser('boot-proof');proof.add_argument('--restore-record',type=Path,required=True)
    proof.add_argument('--restore-record-sha256',required=True);proof.add_argument('--out',type=Path,required=True)
    proof.add_argument('--factory-backup',type=Path,default=smoke.FACTORY_BACKUP)
    settle=sub.add_parser('settle-check');settle.add_argument('--bundle',type=Path,required=True)
    settle.add_argument('--restore-record',type=Path,required=True);settle.add_argument('--restore-record-sha256',required=True)
    settle.add_argument('--out',type=Path,required=True);settle.add_argument('--factory-backup',type=Path,default=smoke.FACTORY_BACKUP)
    run=sub.add_parser('run');run.add_argument('--restore-record',type=Path)
    run.add_argument('--restore-record-sha256')
    paths=dict(smoke='board_smoke_bounds',contention='p4_contention',display='p4_display',
               audio='p4_audio',**{'audio-fix4':'p4_audio_fix4'},headless='p4_headless')
    paths.update({'hil/'+name+'.py':name for name in tuple(paths.values())})
    run.add_argument('helper',choices=tuple(paths))
    run.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    if args.action=='pin':
        pin(args.bundle,args.headroom,args.out)
        print(json.dumps(dict(record=str(args.out),sha256=digest(args.out))));return 0
    if args.action=='boot-proof':
        print('Coordinator runner PID='+str(os.getpid()),flush=True)
        result=boot_proof(args.restore_record,args.restore_record_sha256,args.out,args.factory_backup)
        return 0 if result['passed'] else 1
    if args.action=='settle-check':
        print('Coordinator runner PID='+str(os.getpid()),flush=True)
        result=settle_check(args.bundle,args.restore_record,args.restore_record_sha256,args.out,args.factory_backup)
        return 0 if result['passed'] else 1
    import p4_product
    print('Coordinator runner PID='+str(os.getpid()),flush=True)
    with restoring(args.restore_record,args.restore_record_sha256),adapters():
        return p4_product.main(['hil/'+paths[args.helper]+'.py',*args.arguments])


if __name__=='__main__':raise SystemExit(main())
