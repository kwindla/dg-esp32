#!/usr/bin/env python3
"""Continue sealed R3b40MHz cells and append each completed numbered item."""
import json,os,socket,subprocess,time
from pathlib import Path
from esp_serial import ROOT
from step9a_r3b_analyze import analyze
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
REPORT=ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r3-report.md'

def listeners_closed():
    for port in (7861,7874):
        with socket.socket() as sock:
            sock.settimeout(.2)
            if sock.connect_ex(('127.0.0.1',port))==0:return False
    return True

def finish(number,name):
    path=OUT/name;r=json.loads((path/'result.json').read_text())
    if not r['completed']:
        with REPORT.open('a') as f:f.write(f"\n### R3b item {number} — {name}\n\n**Item {number} progress:** Incomplete diagnostic retained; {r.get('error',r.get('invalid_reasons'))}. No passing result or retake is claimed.\n")
        raise RuntimeError(name+': incomplete '+str(r.get('error')))
    a=analyze(path);e=a['exposure'];k=e['dma_errors'];n=e['completed'];cycles=a['cycle_count'];affected=a['affected_cycles']
    lines=[f'\n### R3b item {number} — {name}\n',f"Diagnostic ELF `{r['elf_sha256']}`, requested{r['requested_hz']}Hz / actual driver{r['after']['actual_khz']}kHz. {'Continues the same verified preflight boot; no reset.' if r.get('continued_preflight_boot') else 'One fresh boot.'} Cell{e['elapsed_us']/1e6:.6f}s, rendered{e['rendered']:,} /{e['fps']:.4f}fps, attempted/queued/completed stripes{e['attempted']:,}/{e['queued']:,}/{n:,}; requested/completed payload{e['attempted_bytes']:,}/{e['completed_bytes']:,}B; DMA pre→post{e['dma_active_us']/1e6:.6f}s. Errors**{k}/{n:,}**, one-sided95% Poisson upper**{a['poisson_upper_events_per_stripe']:.8g}/stripe**. Separate cell, no pooling.\n"]
    if cycles:lines.append(f"Completed cycles{cycles}, hardware events attributed through cycle cleanup{a['cycle_hardware_errors']}, affected cycles{affected}/{cycles}; affected-cycle rate{a['affected_cycle_rate']:.6f}, nominal Poisson upper{a['poisson_upper_events_per_cycle']:.8g}events/cycle. Multiple flags in one cycle are not treated as independent power trials.\n")
    if r['kind']=='B':lines.append(f"Stats+dump-arrivals pairs{len(r['pairs'])}, calls{len(r['calls'])}, affected pair intervals{a['affected_pairs']} (serial cursor attribution is not a CPU trace); no fetch in the call.\n")
    if r['kind']=='A':
        b,c=r['padding_before'],r['padding_after'];lines.append(f"Post-trigger cat/1Hzstats padding is separate:{c['completed']-b['completed']:,} stripes/{c['dma_errors']-b['dma_errors']} events. The lifetime scheduling maximum{e['max_scheduling_gap_us']/1e6:.6f}s includes the inactive preflight continuation interval and is not an active-render or gate-hold bound.\n")
    if r['kind']=='D':
        s=r['stress_after'];lines.append(f"Core{s['core']}/priority{s['priority']}, fixed{s['buffer_bytes_each']}B buffers ×2, burst{s['burst_us']}us/period{s['period_us']}us; copy{s['copied_bytes']*1e6/s['elapsed_us']/1e6:.5f}MB/s, achieved burst wall duty{100*s['busy_us']/s['elapsed_us']:.5f}%, DMA overlaps{s['dma_overlap_stripes']-r['stress_before']['dma_overlap_stripes']:,}; watchdog/idle unchanged.\n")
    lines.append(f"Every lifetime error context captured:{a['context_count']}/{a['whole_boot_hardware_errors']}; SPI1-overlap errors{a['SPI1_overlap_errors']}; phase clusters{a['phase_clusters']}. Every frame/kind/stripe/rect/bytes/phase/pre/post/API match is retained in `{name}/analysis.json` and `{name}/error-contexts.md`. No error events implies clustering cannot be estimated. Poisson/zero-tail models do not prove independence.\n")
    lines.append(f"Monitor raw bytes{r['monitor']['raw_bytes']:,}, advancing snapshots{r['monitor']['animation_snapshots']}; monitor failure retained={not r['monitor']['passed']}, rate validity={r['rate_valid']}, clean={r['clean']}. Failed stats remain failures; complete primary endpoints and context indices establish positive-control rates without repairing JSON. Invalid reasons:{r['invalid_reasons']}.\n")
    lines.append(f"**Item {number} progress:** Declared item exposure completed and retained. {'A real40MHz positive control is established; use the frozen dose rule for lower clocks.' if a['whole_boot_hardware_errors'] else 'No hardware error reproduced in this item; other declared40MHz items/controls still govern.'} No retake, threshold change or step3 work.\n")
    with REPORT.open('a') as f:f.write('\n'.join(lines))
    save(path/'report-appended.json',{'number':number,'completed':True});print(json.dumps({'item':number,'cell':name,'rate_valid':r['rate_valid'],'hardware_errors':a['whole_boot_hardware_errors'],'power':a['power']}),flush=True)
    if not r['rate_valid']:raise RuntimeError(name+': invalid rate '+str(r['invalid_reasons']))
    return a

def main():
    os.environ['STEP9A_BUDGET_PATH']='/home/khkramer/src/evidence/step9a/r2/arena-budget.json'
    first=OUT/'r3b-A40-continued/result.json'
    while not first.exists() or not listeners_closed():time.sleep(1)
    rows=[]
    try:
        rows.append(finish(1,'r3b-A40-continued'))
        for number,name,kind,args in [(2,'r3b-B40','B',['--dumps','1050']),(3,'r3b-C40','C',['--cycles','20']),(4,'r3b-D40','D',['--min-stripes','100000'])]:
            save(OUT/'r3b-campaign-progress.json',{'active_item':number,'active_cell':name,'completed':[r['cell'] for r in rows]});print('START '+name,flush=True)
            with (OUT/(name+'-run.log')).open('w') as log:completed=subprocess.run([str(ROOT/'hil/.venv/bin/python'),str(ROOT/'hil/step9a_r3b_natural.py'),'--name',name,'--kind',kind]+args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            rows.append(finish(number,name,));save(OUT/'r3b-40-progress.json',{'completed':rows})
            if completed.returncode:raise RuntimeError(name+': process exit'+str(completed.returncode))
        save(OUT/'r3b-40-summary.json',{'completed':True,'cells':rows,'positive_controls':[r['cell'] for r in rows if r['whole_boot_hardware_errors']]});save(OUT/'r3b-campaign-progress.json',{'completed':True,'cells':[r['cell'] for r in rows]})
    except Exception as exc:save(OUT/'r3b-40-blocked.json',{'reason':str(exc),'retained':[r['cell'] for r in rows]});raise
if __name__=='__main__':main()
