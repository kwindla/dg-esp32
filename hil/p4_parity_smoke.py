#!/usr/bin/env python3
"""Plan-bound entrypoint for the existing full product smokes; no bot lifecycle."""
import argparse
from pathlib import Path
import shutil

from p4_parity import checked_plan,pinned_bundle,load,require,output_path


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',type=Path,required=True);p.add_argument('--board',choices=('echoear','p4xc'),required=True)
    p.add_argument('--variant',choices=('normal','demo','aec-demo'),required=True)
    for name in ('out','bot-dir','bot-identity'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    p.add_argument('--erle-report',type=Path)
    a=p.parse_args(argv);plan=checked_plan(a.plan)
    a.out=output_path(plan,a.out)
    require(load(a.bot_identity)==plan['product_identity'],'all product pairs require the one planned bot process')
    preflight(a,plan)
    from p4_parity_campaign import attempt
    with attempt(a.plan,'smoke',a.board,a.variant,a.out):return execute(a,plan)


def preflight(a,plan):
    bundle=Path(plan['images'][a.board+'/'+a.variant]['bundle']);manifest=pinned_bundle(bundle,a.board,a.variant)
    if a.board=='p4xc' and a.variant=='aec-demo':
        require(a.erle_report is not None,'--erle-report required for the exact P4 AEC demo ELF')
        from p4_aec_close import adapters
        from p4_aec_product import applied_gate
        from p4_aec_image_measure import checked_report
        with adapters():
            applied_gate(manifest,Path.home()/'src/evidence/echoear/p4/step-15b-fix-5/recommendation.json')
            report=checked_report(a.erle_report)
        require(report['elf_sha256']==manifest['elf_sha256'] and report['profile']==manifest['profile'] and
            report['gain_db']==manifest['boot_ref_gain_db'],'ERLE report image/gain differs')
    from p4_parity_preflight import host_checks
    a.bundle=bundle
    host_checks(a,plan,manifest,load(a.bot_identity))


def execute(a,plan):
    image=plan['images'][a.board+'/'+a.variant];bundle=Path(image['bundle']);pinned_bundle(bundle,a.board,a.variant)
    common=['--board',a.board,'--bundle',str(bundle),'--out',str(a.out),
        '--bot-dir',str(a.bot_dir),'--bot-identity',str(a.bot_identity),'--bot-repo',str(a.bot_repo),
        '--pair-id','step16-'+a.variant]
    restore=['--restore-record',plan['restore_record'],'--restore-record-sha256',plan['restore_record_sha256']]
    if a.variant=='aec-demo':
        import p4_aec_close
        extra=[]
        if a.board=='p4xc':
            require(a.erle_report is not None,'--erle-report required for the exact P4 AEC demo ELF')
            extra=['--erle-report',str(a.erle_report),'--applied',str(Path.home()/'src/evidence/echoear/p4/step-15b-fix-5/recommendation.json')]
        code=p4_aec_close.main(['product','smoke',*common,*restore,'--llm-content','report-only',*extra])
    else:
        import board_smoke as smoke
        import p4_step14
        old_boot=smoke.LiveIO.boot
        def boot(io,board):
            at=old_boot(io,board)
            path=io.out/'boot.log'
            if not path.exists():path.write_text('\n'.join(line for _,_,line in board.lines_since(at))+'\n')
            return at
        smoke.LiveIO.boot=boot
        try:code=p4_step14.main(['run',*restore,'smoke',*common])
        finally:smoke.LiveIO.boot=old_boot
        if a.out.is_dir() and not (a.out/'image.json').exists():shutil.copyfile(bundle/'manifest.json',a.out/'image.json')
    if (a.out/'serial.log').exists():
        from p4_parity_serial import candidate_serial
        from board_smoke import write
        try:candidate_serial(a.out)
        except ValueError as exc:
            result=load(a.out/'result.json')
            result.update(acquisition_passed=False,passed=False,error=type(exc).__name__+': '+str(exc))
            write(a.out/'result.json',result);return 1
    return code


if __name__=='__main__':raise SystemExit(main())
