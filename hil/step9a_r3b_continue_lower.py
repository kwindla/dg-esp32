#!/usr/bin/env python3
"""Continue the frozen lower-clock plan without rerunning retained failed cells."""
import json,os,subprocess
from step9a_r3b_lower_campaign import OUT,ROOT,REPORT,run_cell
from step9a_r2_diagnose import save

def main():
    os.environ['STEP9A_BUDGET_PATH']='/home/khkramer/src/evidence/step9a/r2/arena-budget.json'
    plan=json.loads((OUT/'r3b-lower-dose-plan.json').read_text())['plans']
    rows=[]
    for p in plan:
        path=OUT/p['name']/'result.json'
        if path.exists():
            r=json.loads(path.read_text())
            row={'name':p['name'],'kind':p['kind'],'hz':p['hz'],'clean':r['clean'],'rate_valid':r['rate_valid'],'exposure':r.get('exposure'),'retained_existing_cell':True,'invalid_reasons':r['invalid_reasons'],'hardware_errors':r.get('cleanup_info',{}).get('dma_errors')}
            print('RETAIN '+p['name'],flush=True)
        else:
            try:row=run_cell(p['name'],p['kind'],p['hz'],p['args'])
            except Exception as exc:
                if not path.exists():raise
                r=json.loads(path.read_text());row={'name':p['name'],'kind':p['kind'],'hz':p['hz'],'clean':False,'rate_valid':r['rate_valid'],'exposure':r.get('exposure'),'invalid_reasons':r['invalid_reasons'],'hardware_errors':r.get('cleanup_info',{}).get('dma_errors'),'driver_error':str(exc)}
                print('RETAIN FAILED '+p['name'],flush=True)
        rows.append(row);save(OUT/'r3b-lower-continuation-summary.json',{'cells':rows})
    eligible=[hz for hz in (26666667,20000000) if all(r['clean'] for r in rows if r['hz']==hz)]
    confirmations=[];selected=None
    for hz in eligible:
        try:r=run_cell('r3b-confirm-'+str(hz),'E',hz,['--min-stripes','30000'])
        except Exception as exc:r={'hz':hz,'clean':False,'driver_error':str(exc)}
        confirmations.append(r)
        if r['clean']:selected=hz;break
    decision={'selected_hz':selected,'lower_cells':rows,'confirmation':confirmations,'performance_pending':selected is not None}
    if selected is None:
        decision['blocked']='Neither lower clock qualifies; internal staging needs user ruling and fresh census'
        save(OUT/'r3b-decision.json',decision);return 2
    save(OUT/'r3b-decision.json',decision)
    with (OUT/'r3b-performance-run.log').open('x') as log:proc=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r3b_performance.py'),'--hz',str(selected)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    perf=OUT/('r3b-performance-'+str(selected))/'summary.json'
    decision['performance_pending']=False;decision['performance_passed']=proc.returncode==0 and perf.exists() and json.loads(perf.read_text())['passed'];decision['performance_summary_path']=str(perf)
    save(OUT/'r3b-decision.json',decision)
    return 0 if decision['performance_passed'] else 2
if __name__=='__main__':raise SystemExit(main())
