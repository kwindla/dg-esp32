#!/usr/bin/env python3
"""Apply the predeclared natural-rate power and higher clean lower-clock rule."""
import json,os,subprocess,time
from pathlib import Path
from esp_serial import ROOT
from step9a_r2_diagnose import save
from step9a_r3b_analyze import analyze
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r3-report.md'

def args_for(row):
    kind=row['kind'];p=row['power'];reps=p.get('repeat_factor',1)
    if kind=='A':args=['--cycles',str(p.get('required_cycles',60)),'--min-stripes','100000','--min-padding-stripes',str(p.get('required_padding_stripes',0))]
    elif kind=='B':args=['--dumps',str(p.get('required_dumps',1050)),'--burst-calls',str(p.get('required_calls',1))]
    elif kind=='C':args=['--cycles',str(p.get('required_cycles',20))]
    else:args=['--min-stripes',str(p.get('required_stripes',100000))]
    return args,reps

def run_cell(name,kind,hz,args):
    save(OUT/'r3b-lower-progress.json',{'active_cell':name,'hz':hz,'kind':kind,'args':args});print('START '+name,flush=True)
    with (OUT/(name+'-run.log')).open('w') as log:proc=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r3b_natural.py'),'--name',name,'--kind',kind,'--hz',str(hz)]+args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    path=OUT/name;r=json.loads((path/'result.json').read_text())
    a=analyze(path) if r['completed'] and kind!='E' else None
    with REPORT.open('a') as f:
        f.write('\n### Lower-clock cell '+name+'\n\n')
        if r.get('exposure'):
            e=r['exposure'];f.write(f"Requested{hz}Hz, actual{r['after']['actual_khz']}kHz; errors{e['dma_errors']}/{e['completed']:,} stripes, one-sided95% Poisson upper{e['poisson_one_sided_95_upper_per_stripe']:.8g}; ratevalid={r['rate_valid']}, clean={r['clean']}. Declared dose arguments:{args}. Complete error context, phase/API overlap, CPU/bytes/DMA/scheduling exposure and cleanup remain in `{name}/result.json` and analysis. No unlike-cell pooling or retake.\n")
        else:f.write(f"Incomplete diagnostic retained:{r.get('error')}; no passing result.\n")
    print(json.dumps({'cell':name,'returncode':proc.returncode,'rate_valid':r['rate_valid'],'clean':r['clean'],'hardware_errors':r.get('cleanup_info',{}).get('dma_errors'),'exposure':r.get('exposure')}),flush=True)
    if not r['rate_valid']:raise RuntimeError(name+': invalid '+str(r['invalid_reasons']))
    return {'name':name,'kind':kind,'hz':hz,'clean':r['clean'],'analysis':a,'exposure':r['exposure'],'actual_khz':r['after']['actual_khz']}

def main():
    os.environ['STEP9A_BUDGET_PATH']='/home/khkramer/src/evidence/step9a/r2/arena-budget.json'
    while not (OUT/'r3b-40-summary.json').exists():
        if (OUT/'r3b-item4-blocked.json').exists():raise RuntimeError('Item4 invalid;40MHz campaign cannot seal')
        time.sleep(1)
    control=json.loads((OUT/'r3b-40-summary.json').read_text());rows=control['cells'];positives=[r for r in rows if r['whole_boot_hardware_errors']]
    if not positives:
        save(OUT/'r3b-decision.json',{'blocked':'no natural40MHz reproducer','selected_hz':None});return 2
    plans=[]
    for hz in (26666667,20000000):
        for row in rows:
            if row['kind']=='D' and not row['whole_boot_hardware_errors']:continue
            args,reps=args_for(row)
            for rep in range(reps):plans.append({'hz':hz,'kind':row['kind'],'name':f"r3b-{row['kind']}-{hz}"+(f'-rep{rep+1}' if reps>1 else ''),'args':args,'from40':row['power']})
    save(OUT/'r3b-lower-dose-plan.json',{'frozen_rule':'predeclared-r3b.json','plans':plans,'both_clocks_required':True,'no_threshold_change':True})
    completed=[]
    try:
        for plan in plans:
            completed.append(run_cell(plan['name'],plan['kind'],plan['hz'],plan['args']));save(OUT/'r3b-lower-summary.json',{'completed':completed})
        qualifying=[hz for hz in (26666667,20000000) if all(r['clean'] for r in completed if r['hz']==hz)]
        if not qualifying:save(OUT/'r3b-decision.json',{'blocked':'neitherlowerclock qualifies; needsinternalstaging ruling/freshcensus','selected_hz':None,'completed':completed});return 2
        confirmation=[];selected=None
        for hz in qualifying:
            r=run_cell('r3b-confirm-'+str(hz),'E',hz,['--min-stripes','30000']);confirmation.append(r)
            if r['clean']:selected=hz;break
        if selected is None:save(OUT/'r3b-decision.json',{'blocked':'freshconfirmation fails for everyeligibleclock','selected_hz':None,'confirmation':confirmation});return 2
        save(OUT/'r3b-decision.json',{'selected_hz':selected,'lower_cells':completed,'confirmation':confirmation,'performance_pending':True})
        with (OUT/'r3b-performance-run.log').open('w') as log:proc=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r3b_performance.py'),'--hz',str(selected)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        perf=OUT/('r3b-performance-'+str(selected))/'summary.json'
        decision=json.loads((OUT/'r3b-decision.json').read_text());decision['performance_pending']=False;decision['performance_passed']=proc.returncode==0 and perf.exists() and json.loads(perf.read_text())['passed'];decision['performance_summary_path']=str(perf);save(OUT/'r3b-decision.json',decision)
        return 0 if decision['performance_passed'] else 2
    except Exception as exc:save(OUT/'r3b-lower-blocked.json',{'reason':str(exc),'completed':completed});raise
if __name__=='__main__':raise SystemExit(main())
