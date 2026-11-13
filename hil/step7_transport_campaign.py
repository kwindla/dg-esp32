#!/usr/bin/env python3
"""Three fresh boots of the final image, three transport sessions per boot."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from esp_serial import ROOT

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    identity={kind:hashlib.sha256((ROOT/f'build/dg-esp32.{kind}').read_bytes()).hexdigest() for kind in ('elf','bin')}
    (out/'identity.json').write_text(json.dumps(identity,indent=2)+'\n');codes=[]
    for boot in range(1,4):
        command=[str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/transport_test.py'),'--sessions','3','--campaign','--rerun-failed','--out',str(out/f'boot{boot}')]
        with (out/f'boot{boot}.log').open('w') as log:
            code=subprocess.run(command,cwd=ROOT,env={**os.environ,'BOT_OPUS':'lean'},stdout=log,stderr=subprocess.STDOUT).returncode
        codes.append(code);print(f'boot {boot}: exit {code}',flush=True)
        assert hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest()==identity['elf'],'Image changed during campaign'
    (out/'campaign.json').write_text(json.dumps({'identity':identity,'boot_exit_codes':codes},indent=2)+'\n')
    return int(any(codes))
if __name__=='__main__':raise SystemExit(main())
