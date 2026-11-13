"""Whole-boot CPU statistics, exact permutations and heap for step 8c3."""
import itertools
import json
from collections import defaultdict
from pathlib import Path
import numpy as np


def cpu_statistics(crossover):
    if not crossover['completed']:raise ValueError('Incomplete CPU population')
    boots={};census=None;identities=[]
    for run in crossover['runs']:
        windows=run['summary']['cpu_windows']
        if len(windows)!=4 or not run['summary']['stimulus_complete']:raise ValueError('Need four complete windows per boot')
        values=defaultdict(list);worker_ids=None
        for window in windows:
            workers=sorted((t for t in window['tasks'] if t['name'].startswith('pipecat#')),key=lambda t:int(t['name'].split('#')[1]))
            if len(workers)!=2 or any((t['core'],t['priority'])!=(1,5) for t in workers):raise ValueError('Unexpected worker census')
            names=[t['name'] for t in workers]
            if worker_ids is not None and worker_ids!=names:raise ValueError('Worker identity changed within boot')
            worker_ids=names;mapping=dict(zip(names,('pipecat-event-loop','pipecat-request-timer')))
            for task in window['tasks']:values[mapping.get(task['name'],task['name'])].append(task['cpu'])
        if any(len(v)!=4 for v in values.values()):raise ValueError('Incomplete task census')
        if census is not None and set(values)!=census:raise ValueError('Task census changed across boots')
        census=set(values);key=(run['pair'],run['arm'])
        if key in boots:raise ValueError('Duplicate boot')
        boots[key]=values
        identities.append(dict(pair=run['pair'],arm=run['arm'],raw_to_role=dict(zip(worker_ids,('pipecat-event-loop','pipecat-request-timer')))))
    pairs=sorted({i for i,_ in boots})
    if len(pairs)<6 or len(boots)!=2*len(pairs) or any((i,a) not in boots for i in pairs for a in ('A','B')):raise ValueError('Need >=6 paired boots per arm')
    result={}
    for name in sorted(census):
        a=np.array([np.median(boots[(i,'A')][name]) for i in pairs]);b=np.array([np.median(boots[(i,'B')][name]) for i in pairs])
        delta=float(np.median(b)-np.median(a))
        paired_mean=float(np.mean(b-a))
        # Exact blocked relabeling preserves each adjacent A/B boot pair.
        blocked=[]
        for swap in itertools.product((False,True),repeat=len(pairs)):
            aa=np.where(swap,b,a);bb=np.where(swap,a,b)
            blocked.append(float(np.mean(bb-aa)))
        pooled=np.concatenate([a,b]);unblocked=[]
        for indices in itertools.combinations(range(len(pooled)),len(a)):
            left=pooled[list(indices)];right=np.delete(pooled,indices)
            unblocked.append(float(np.median(right)-np.median(left)))
        paired=(b-a).tolist();idle=name.startswith('IDLE');q=10 if idle else 90
        aw=sum((boots[(i,'A')][name] for i in pairs),[]);bw=sum((boots[(i,'B')][name] for i in pairs),[])
        tail=float(np.percentile(bw,q,method='lower')-np.percentile(aw,q,method='lower'))
        allowance=5 if idle else 2;direction=-1 if idle else 1
        result[name]=dict(boots_per_arm=len(pairs),windows_per_arm=4*len(pairs),
            boot_medians_A=a.tolist(),boot_medians_B=b.tolist(),median_A=float(np.median(a)),median_B=float(np.median(b)),
            delta_pp=delta,paired_mean_delta_pp=paired_mean,pair_deltas_pp=paired,pair_signs=['+' if d>0 else '-' if d<0 else '0' for d in paired],
            paired_permutation_p=sum(abs(d)>=abs(paired_mean)-1e-12 for d in blocked)/len(blocked),paired_permutations=len(blocked),
            unpaired_permutation_p=sum(abs(d)>=abs(delta)-1e-12 for d in unblocked)/len(unblocked),unpaired_permutations=len(unblocked),
            tail_percentile=q,tail_delta_pp=tail,allowance_pp=allowance,
            material_regression=delta*direction>allowance or tail*direction>allowance)
    return dict(tasks=result,worker_identities=identities,
        passed=not any(r['material_regression'] for r in result.values()),
        statistic='Primary mean of paired whole-boot median deltas with exact blocked sign relabeling; arm medians remain effect sizes. Supplemental unpaired median relabeling. Four windows within each boot are never independent permutation units.',
        limitation='Six pairs give 64 blocked permutations and minimum two-sided p=0.03125; per-pair signs and effect sizes remain essential. Unpaired permutations ignore temporal blocks.')


def heap_statistics(out,crossover):
    groups={'A':defaultdict(list),'B':defaultdict(list)}
    for run in crossover['runs']:
        target=Path(out)/'cpu'/f"cpu-ab-{run['arm']}{run['pair']:02}"/'webrtc-ethernet/cpu-snapshots.json'
        snapshots=json.loads(target.read_text())
        for cap in ('internal','dma','psram'):
            groups[run['arm']][cap].append(dict(pair=run['pair'],
                free_median=float(np.median([s['heap'][cap]['free'] for s in snapshots])),
                free_min=min(s['heap'][cap]['free'] for s in snapshots),
                minimum_free=min(s['heap'][cap]['minimum_free'] for s in snapshots),
                largest_min=min(s['heap'][cap]['largest'] for s in snapshots)))
    return {a:{c:dict(boots=v,free_median=float(np.median([b['free_median'] for b in v])),
        minimum_free=min(b['minimum_free'] for b in v),largest_min=min(b['largest_min'] for b in v),
        sampled_free_min=min(b['free_min'] for b in v)) for c,v in caps.items()} for a,caps in groups.items()}


MODE_THRESHOLDS={'A':dict(peer_loop=28.5,idle1=24.0),'B':dict(peer_loop=10.0,idle1=50.0)}


def mode_label(peer,idle,arm):
    cutoff=MODE_THRESHOLDS[arm]
    if peer<=cutoff['peer_loop'] and idle>=cutoff['idle1']:return 'quiet'
    if peer>cutoff['peer_loop'] and idle<cutoff['idle1']:return 'busy'
    return 'ambiguous'


def mode_statistics(crossover):
    """Exploratory arm-relative indicators; never drop a boot from pooled data."""
    rows=[];groups={'A':defaultdict(list),'B':defaultdict(list)}
    for run in crossover['runs']:
        windows=run['summary']['cpu_windows'];values=defaultdict(list)
        for window in windows:
            for t in window['tasks']:
                if t['name'] in ('peer-loop','IDLE1','audio-tx','audio-playback','audio-capture'):
                    values[t['name']].append(t['cpu'])
        medians={name:float(np.median(v)) for name,v in values.items()}
        window_modes=[mode_label(p,i,run['arm']) for p,i in zip(values['peer-loop'],values['IDLE1'])]
        mode=mode_label(medians['peer-loop'],medians['IDLE1'],run['arm'])
        if 'quiet' in window_modes and 'busy' in window_modes:mode='switching'
        row=dict(pair=run['pair'],arm=run['arm'],mode=mode,boot_medians=medians,
            peer_loop_windows=values['peer-loop'],idle1_windows=values['IDLE1'],window_modes=window_modes)
        rows.append(row);groups[run['arm']][mode].append(row)
    strata={}
    for mode in ('quiet','busy','ambiguous','switching'):
        counts={a:len(groups[a][mode]) for a in groups};tasks={}
        if all(counts.values()):
            for name in ('audio-tx','peer-loop','audio-playback','audio-capture','IDLE1'):
                a=[r['boot_medians'][name] for r in groups['A'][mode]]
                b=[r['boot_medians'][name] for r in groups['B'][mode]]
                pooled=np.array(a+b);observed=float(np.median(b)-np.median(a));permuted=[]
                for indices in itertools.combinations(range(len(pooled)),len(a)):
                    left=pooled[list(indices)];right=np.delete(pooled,indices)
                    permuted.append(float(np.median(right)-np.median(left)))
                aw=[t['cpu'] for r in crossover['runs'] if r['arm']=='A' and any(row['pair']==r['pair'] for row in groups['A'][mode]) for w in r['summary']['cpu_windows'] for t in w['tasks'] if t['name']==name]
                bw=[t['cpu'] for r in crossover['runs'] if r['arm']=='B' and any(row['pair']==r['pair'] for row in groups['B'][mode]) for w in r['summary']['cpu_windows'] for t in w['tasks'] if t['name']==name]
                q=10 if name=='IDLE1' else 90
                tasks[name]=dict(median_A=float(np.median(a)),median_B=float(np.median(b)),delta_pp=observed,
                    tail_percentile=q,tail_A=float(np.percentile(aw,q,method='lower')),tail_B=float(np.percentile(bw,q,method='lower')),
                    exploratory_unpaired_permutation_p=sum(abs(v)>=abs(observed)-1e-12 for v in permuted)/len(permuted),permutations=len(permuted))
        strata[mode]=dict(boots_per_arm=counts,tasks=tasks,
            available=all(counts.values()),unavailable_reason=None if all(counts.values()) else 'No like-labelled boot in one or both arms')
    return dict(boots=rows,strata=strata,thresholds=MODE_THRESHOLDS,
        basis='A cutoffs anchored to Fable Diagnosis 4; B cutoffs provisionally bracket the first three observed XIP boots (8.84/9.18 vs 11.29 peer-loop and 51.61/51.18 vs 48.90 IDLE1). Fixed before pairs 5 and 6; no playback/TX values used to assign strata.',
        limitation='Both mode indicators change directly with XIP. Arm-relative labels are an exploratory robustness comparison, not proof of an identical latent system mode or a causal adjustment. Ambiguous/switching boots remain in pooled statistics. Stratum permutations are unpaired and exploratory; small stratum populations have coarse p-values.',
        aa_tail_allowance_used=False)
