#!/usr/bin/env python3
import json,os,subprocess
from pathlib import Path
from esp_serial import ROOT
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r4');REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r4-report.md'
def main():
    rows=[json.loads((OUT/'inventory-handshakes/result.json').read_text())];os.environ['STEP9A_BUDGET_PATH']=str(OUT/'arena-budget.json')
    if not rows[0]['measurement_valid']:raise RuntimeError('Handshake inventory invalid; no retake')
    try:
        for kind in ('flood','fetch'):
            name='inventory-'+kind
            with (OUT/(name+'-run.log')).open('x') as f:
                proc=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r4_inventory.py'),'--name',name,'--kind',kind],stdout=f,stderr=subprocess.STDOUT,cwd=ROOT)
            row=json.loads((OUT/name/'result.json').read_text());rows.append(row);save(OUT/'inventory-summary.json',{'complete':False,'cells':rows})
            print(json.dumps({'name':name,'measurement_valid':row['measurement_valid'],'clean':row['clean'],'returncode':proc.returncode}),flush=True)
            if not row['measurement_valid']:raise RuntimeError('Invalid '+name+'; no retake')
        summary={'complete':True,'cells':rows,'nonasset_psram_crypto':sum(p['input_psram'] for r in rows for p in r.get('crypto_delta',[]) if not p['asset'])};save(OUT/'inventory-summary.json',summary)
        return 0
    finally:
        with REPORT.open('a') as f:
            f.write('\n## Step 2 — crypto/GDMA inventory\n\n')
            for r in rows:
                f.write(f"`{r['name']}` completed={r['completed']}, valid={r['measurement_valid']}, clean={r['clean']}; {r.get('exposure')}; requested-input/RX-capacity deltas {r.get('crypto_delta')}; flooddelivery {r.get('flood_delivery')}; error {r.get('error')}. Complete raw/cleanup/counter records retained under r4.\n\n")
            f.write('Progress note after numbered step2: '+('all three inventory cells complete; residual PSRAM crypto requires conditional general guard tests.' if len(rows)==3 and all(r['measurement_valid'] for r in rows) else 'inventory incomplete/invalid; every failed observation retained and no retake.')+' No commit/push.\n')
if __name__=='__main__':raise SystemExit(main())
