#!/usr/bin/env python3
"""Execute frozen item5 cells and conditional candidate measurements once."""
import json,os,subprocess
from pathlib import Path
from esp_serial import ROOT
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r3-report.md'

def cell(row):
    name=row['name'];log=OUT/('r3b-item5-'+name+'-run.log');print('START '+name,flush=True)
    save(OUT/'r3b-item5-progress.json',{'active':row})
    with log.open('x') as f:p=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r3b_hash.py'),'--name',name,'--core',str(row['core']),'--mode',str(row['mode'])],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
    r=json.loads((OUT/('r3b-item5-'+name)/'result.json').read_text())
    with REPORT.open('a') as f:
        f.write('\n### Item 5 cell: '+name+'\n\n')
        if r.get('exposure'):
            e=r['exposure'];h=r['hash_after'];f.write(f"Fresh diagnostic boot, core {row['core']}, mode {row['mode']}, actual {r['after']['actual_khz']} kHz. {e['dma_errors']} errors / {e['completed']:,} completed stripes; one-sided 95% Poisson upper {e['poisson_one_sided_95_upper_per_stripe']:.8g}/stripe. Rendered {e['rendered']:,} frames / {e['fps']:.4f} fps; completed {e['completed_bytes']:,} bytes; DMA pre/post exposure {e['dma_active_us']/1e6:.6f}s; scheduling maximum {e['max_scheduling_gap_us']}us. Verified {h['hashes']:,} full hashes / {h['hashed_bytes']:,} bytes; SHA GDMA starts {h['gdma_starts']:,}, PSRAM descriptor bytes {h['psram_bytes']:,}, internal descriptor bytes {h['internal_bytes']:,}, SHA API/LCD overlapping stripes {h['sha_overlap_stripes']:,}. Rates {r.get('hash_rates')}; gate wait/hold maxima {h['gate_wait_max_us']}/{h['gate_hold_max_us']}us; digest mismatches {h['mismatches']}. Rate validity {r['rate_valid']}, clean {r['clean']}; invalid reasons {r['invalid_reasons']}. Every error context and original raw/cleanup remain in `r3b-item5-{name}/`. No retake or production adoption.\n")
        else:f.write('Incomplete/failed diagnostic retained: '+str(r.get('error',r))[:1400]+'. No retake or production adoption.\n')
    print(json.dumps({'cell':name,'returncode':p.returncode,'rate_valid':r.get('rate_valid'),'clean':r.get('clean'),'hardware_errors':r.get('cleanup_info',{}).get('dma_errors')}),flush=True)
    if not r.get('cleanup_info') or r.get('cleanup_errors'):raise RuntimeError('Unsafe/incomplete cleanup: '+name)
    return r

def main():
    os.environ['STEP9A_BUDGET_PATH']='/home/khkramer/src/evidence/step9a/r2/arena-budget.json'
    plan=json.loads((OUT/'predeclared-r3b-item5.json').read_text());rows=[]
    try:
        for p in plan['cells']:rows.append(cell(p));save(OUT/'r3b-item5-summary.json',{'complete':False,'cells':rows})
        positive=[r for r in rows if r['mode']==0 and r['cleanup_info']['dma_errors']>0]
        if positive:
            for p in plan['optional_candidates_if_ungated_hardware_errors']:rows.append(cell(p));save(OUT/'r3b-item5-summary.json',{'complete':False,'cells':rows})
        summary={'complete':True,'cells':rows,'ungated_physical_positive':[r['name'] for r in positive],'candidate_fixes_measured_not_adopted':bool(positive),'lower_clock_plan_unchanged':True,'no_clock_selected':True,'original_lower_gate_blockers_retained':True}
        save(OUT/'r3b-item5-summary.json',summary);return 0
    except Exception as exc:save(OUT/'r3b-item5-blocked.json',{'reason':str(exc),'completed':rows});raise
if __name__=='__main__':raise SystemExit(main())
