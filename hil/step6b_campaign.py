#!/usr/bin/env python3
"""Interleave saved old/new images, using phase-1 capture and summary tooling.

Every failed sample remains in its boot directory. Repeat each failed boot once
in an outliers directory; never substitute the repeat into the formal cohort.
"""
import argparse
import json
import subprocess
from pathlib import Path
from esp_serial import ROOT, port_lock
from phase1_campaign import run, PYTHON
from phase1_summary import summarize
from phase1_compare import compare_step6b

def flash(image,out):
    out.parent.mkdir(parents=True,exist_ok=True)
    with port_lock(),out.open('w') as log:
        subprocess.run(['bash','-c','. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null; exec python -m esptool "$@"',
                        'flash-saved','--chip','esp32s3','--port','/dev/ttyACM0','--baud','460800','write-flash',
                        '0x10000',str(image/'dg-esp32.bin')],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)

def boot(image,out):
    if out.exists(): raise RuntimeError(f'Refusing to overwrite {out}')
    flash(image,out.parent/f'{out.name}-flash.log')
    code=run([PYTHON,ROOT/'hil/transport_test.py','--campaign','--sessions','3','--elf',image/'dg-esp32.elf','--out',out],out.parent/f'{out.name}.log')
    print(f'{out.parent.name}/{out.name}: exit={code}',flush=True)
    return code

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--old',type=Path,required=True);p.add_argument('--new',type=Path,required=True);p.add_argument('--boots',type=int,default=5);a=p.parse_args()
    old=a.out/'old-interleaved';new=a.out/'candidate';old.mkdir(exist_ok=True);new.mkdir(exist_ok=True)
    for i in range(1,a.boots+1):
        for label,image,cohort in [('old',a.old,old),('new',a.new,new)]:
            out=cohort/f'boot{i}'
            if out.exists():
                result=json.loads((out/'result.json').read_text());code=0 if result.get('passed') else 1
                print(f'Keeping previously captured {label} boot {i}: exit={code}',flush=True)
            else:code=boot(image,out)
            if code:
                repeat=a.out/f'{label}-outliers'/f'boot{i}'
                repeat.parent.mkdir(exist_ok=True)
                if not repeat.exists():boot(image,repeat)
    for cohort in [old,new,a.out/'old-outliers',a.out/'new-outliers']:
        if cohort.exists(): (cohort/'summary.json').write_text(json.dumps(summarize(cohort),indent=2)+'\n')
    base=summarize(old);candidate=summarize(new)
    for name,baseline in [('interleaved',base),('committed',json.loads((ROOT/'proj-2026-10-08-0949/evidence/step6a/baseline-qualified/summary.json').read_text()))]:
        comparison=compare_step6b(baseline,candidate,3*a.boots)
        (a.out/f'compare-{name}.json').write_text(json.dumps(comparison,indent=2)+'\n')
    flash(a.new,a.out/'restore-new.log')
if __name__=='__main__':main()
