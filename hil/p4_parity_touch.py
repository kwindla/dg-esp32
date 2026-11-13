#!/usr/bin/env python3
"""Coordinator asks for one real tap per board; absence stays unverified."""
import argparse
import json
from pathlib import Path
import re
import time

from p4_parity import checked_plan,pinned_bundle,require,write_new,output_path


def touch_witness(before,after,lines):
    require(before.get('initialized') is True and before.get('enabled') is True and before.get('phase')==1,
        'touch must start initialized/enabled in Live')
    require(after['driver']['injected']==before['driver']['injected'],'injected tap cannot prove physical touch')
    require(after['driver']['double_taps']==before['driver']['double_taps']+1 and
        after['requests']==before['requests']+1 and after['request_rejected']==before['request_rejected'],
        'one physical double tap and accepted demo request required')
    require(after['generation']>before['generation'] and after['phase']==1 and not after['pending'],
        'tap must complete a new live generation')
    require(any(re.search(r'\bSTATE Ready gen='+str(after['generation'])+r'\b',line) for line in lines),
        'missing Ready witness for physical request')
    return dict(verified=True,physical_double_taps=1,generation_before=before['generation'],generation_after=after['generation'])


def live(args):
    import boards
    import board_smoke as smoke
    from p4_product import candidate_storage
    from p4_step14 import restoring
    boards.require_bench();plan=checked_plan(args.plan);args.out=output_path(plan,args.out)
    bundle=Path(plan['images'][args.board+'/demo']['bundle']);manifest=pinned_bundle(bundle,args.board,'demo')
    identity=json.loads(args.bot_identity.read_text());args.bundle=bundle
    require(identity==plan['product_identity'],'planned product bot required for physical touch')
    args.out.mkdir(parents=True,exist_ok=False);guard=io=None
    write_new(args.out/'identity.json',identity)
    result=dict(board=args.board,variant='demo',elf_sha256=manifest['elf_sha256'],bundle=str(bundle),
        acquisition_passed=False,touch_verified=False,qualification=False,identity=identity)
    def status(board):
        at=board.cursor;board.command('touch-status')
        return json.loads(board.wait_for(r'^DEMO_TOUCH_STATUS \{',5,since=at).split(' ',1)[1])
    try:
        with restoring(plan['restore_record'],plan['restore_record_sha256']),candidate_storage(args.board,bundle,args.out):
            io=smoke.LiveIO(args,args.out,manifest,identity);io.preflight_tools();io.preflight_restore();guard=io.hold()
            with guard:
                guard.touch();io.flash(bundle,manifest,'candidate')
                with io.open() as board:
                    try:
                        io.boot(board);io.provision(board);io.begin(board,'resume')
                        board.command('touch-enable 1');before=status(board);start=board.cursor
                        result['before']=before
                        print('USER ACTION: double-tap the '+args.board+' screen ONCE within 45 seconds.',flush=True)
                        end=time.monotonic()+45;after=before
                        while time.monotonic()<end:
                            after=status(board)
                            if after['driver']['double_taps']>before['driver']['double_taps'] and after['phase']==1 and after['generation']>before['generation']:break
                            time.sleep(.25)
                        lines=[line for _,_,line in board.lines_since(start)]
                        result.update(after=after,serial_witness=lines)
                        try:result.update(touch_verified=True,witness=touch_witness(before,after,lines))
                        except ValueError as exc:result.update(touch_verified=False,unverified_reason=str(exc))
                    finally:
                        io.stopping=True;board.command('touch-enable 0');board.command('demo hold',45);io.active=False
                guard.success=True
            result['restore']=guard.record
            require(guard.record.get('clean') is True,'touch restore failed')
            result['acquisition_passed']=True
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc))
        if guard is not None:result['restore']=guard.record
    finally:
        try:write_new(args.out/'touch.json',result)
        finally:
            if guard is not None:guard.release_signals()
    return 1 if not result['acquisition_passed'] else 0 if result['touch_verified'] else 2


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--board',choices=('echoear','p4xc'),required=True)
    for name in ('plan','out','bot-dir','bot-identity'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    p.add_argument('--factory-backup',dest='p4_factory_backup',type=Path,default=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z')
    p.set_defaults(reference_only=False,calibration=False,headless=False,p4_restore_bundle=None,p4_restore_elf=None)
    a=p.parse_args(argv)
    from p4_parity_campaign import attempt
    with attempt(a.plan,'touch',a.board,'demo',a.out):return live(a)


if __name__=='__main__':raise SystemExit(main())
