#!/usr/bin/env python3
"""Interleave exact saved lean-control firmware and final app on today's bench."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from esp_serial import ROOT
from phase2_campaign import flash

def main():
    p=argparse.ArgumentParser();p.add_argument('--control',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);control=a.control.resolve();final=out/'final-image';final.mkdir()
    for suffix in ['bin','elf','map']:shutil.copyfile(ROOT/f'build/dg-esp32.{suffix}',final/f'dg-esp32.{suffix}')
    shutil.copyfile(ROOT/'build/bootloader/bootloader.bin',final/'bootloader.bin')
    shutil.copyfile(ROOT/'build/partition_table/partition-table.bin',final/'partition-table.bin')
    identities={name:{suffix:hashlib.sha256((image/f'dg-esp32.{suffix}').read_bytes()).hexdigest() for suffix in ['elf','bin']} for name,image in [('control',control),('candidate',final)]}
    (out/'identities.json').write_text(json.dumps(identities,indent=2)+'\n');codes=[]
    try:
        for boot in range(1,4):
            for name,image in [('control',control),('candidate',final)]:
                dest=out/name/f'boot{boot}';flash(image,out/f'{name}-boot{boot}-flash.log')
                argv=[str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/transport_test.py'),'--sessions','3','--campaign','--rerun-failed','--elf',str(image/'dg-esp32.elf'),'--out',str(dest)]
                with (out/f'{name}-boot{boot}.log').open('w') as log:code=subprocess.run(argv,cwd=ROOT,env={**os.environ,'BOT_OPUS':'lean'},stdout=log,stderr=subprocess.STDOUT).returncode
                codes.append({'boot':boot,'image':name,'exit':code});print(codes[-1],flush=True)
                (out/'campaign.json').write_text(json.dumps({'identities':identities,'captures':codes},indent=2)+'\n')
    finally:
        flash(final,out/'restore-final-flash.log')
    return int(any(row['exit'] for row in codes))
if __name__=='__main__':raise SystemExit(main())
