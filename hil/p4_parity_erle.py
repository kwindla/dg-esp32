#!/usr/bin/env python3
"""Register each bounded A0 ERLE acquisition before delegating to frozen tools."""
import argparse
from pathlib import Path
from p4_parity import checked_plan,load,require


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('plan','image-plan','out','bot-dir','bot-identity'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--part',type=int,choices=range(4),required=True)
    p.add_argument('--bot-repo',type=Path,default=Path.home()/'src/dg-esp32')
    a=p.parse_args(argv);plan=checked_plan(a.plan)
    require(load(a.bot_identity)==plan['product_identity'],'planned product bot required')
    require(load(a.image_plan)['elf_sha256']==plan['images']['p4xc/aec-demo']['elf_sha256'],'same-image A0 plan required')
    from p4_parity_campaign import attempt
    import p4_aec_close
    with attempt(a.plan,'erle','p4xc','aec-demo',a.out,a.part):
        return p4_aec_close.main(['image','gain','--plan',str(a.image_plan),'--part',str(a.part),'--out',str(a.out),
            '--bot-dir',str(a.bot_dir),'--bot-identity',str(a.bot_identity),'--bot-repo',str(a.bot_repo),
            '--restore-record',plan['restore_record'],'--restore-record-sha256',plan['restore_record_sha256']])


if __name__=='__main__':raise SystemExit(main())
