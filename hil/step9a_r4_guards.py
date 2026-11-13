#!/usr/bin/env python3
import json,os,subprocess
from pathlib import Path
from esp_serial import ROOT
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r4');REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r4-report.md'
def cell(name,guard):
    os.environ['STEP9A_R4_GUARD']=str(guard)
    with (OUT/(name+'-run.log')).open('x') as f:proc=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r4_hash.py'),'--name',name,'--core','1','--mode','0'],stdout=f,stderr=subprocess.STDOUT,cwd=ROOT)
    r=json.loads((OUT/name/'result.json').read_text());print(json.dumps({'name':name,'returncode':proc.returncode,'clean':r['clean'],'errors':r.get('cleanup_info',{}).get('dma_errors'),'fps':r.get('exposure',{}).get('fps')}),flush=True);return r

def main():
    os.environ['STEP9A_BUDGET_PATH']=str(OUT/'arena-budget.json');inventory=json.loads((OUT/'inventory-summary.json').read_text());assert inventory['complete'] and inventory['nonasset_psram_crypto']>0
    rows=[];selected=None
    try:
        rows.append(cell('guard-positive',0));positive=rows[-1]
        assert positive.get('cleanup_info',{}).get('dma_errors',0)>0,'Ungated positive did not fail; no guardexperimentauthorized'
        assert not positive.get('cleanup_errors'),'Positive cleanup failed'
        rows.append(cell('guard-priority',1));priority=rows[-1]
        if priority['clean'] and priority.get('exposure',{}).get('completed',0)>=10000 and priority['exposure']['fps']>=28:selected=1
        else:
            assert not priority.get('cleanup_errors'),'Priority cleanup failed'
            rows.append(cell('guard-gate',2));gate=rows[-1]
            if gate['clean'] and gate.get('exposure',{}).get('completed',0)>=10000:selected=2
        save(OUT/'guard-summary.json',{'complete':True,'selected':selected,'cells':rows})
        return 0 if selected else 2
    finally:
        with REPORT.open('a') as f:
            f.write('\n## Step 3 — conditional general guard measurements\n\n')
            for r in rows:f.write(f"`{r['name']}`: guard{r['guard']}; {r.get('exposure')}; clean={r['clean']}, ratevalid={r['rate_valid']}; full hashes {r.get('hash_after',{}).get('hashes')}; crypto status {r.get('cleanup_crypto')}; invalid reasons {r.get('invalid_reasons')}. Raw/error contexts and cleanup retained; no retakes.\n\n")
            f.write(f'Progress note after numbered step3: measured guards; selected={selected}. Priority is preferred only if clean>=10000 stripes andcat>=28fps; otherwise gate requires clean>=10000. The positive is expected to fail; its failed monitor is never counted as qualification. No SDK edit, commit or push.\n')
if __name__=='__main__':raise SystemExit(main())
