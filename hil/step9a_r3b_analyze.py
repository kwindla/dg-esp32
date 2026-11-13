#!/usr/bin/env python3
"""Per-cell R3b context/cycle attribution and frozen expected-count dose."""
import argparse,json,math
from collections import Counter
from pathlib import Path
from scipy.stats import chi2
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r3')
KINDS={1:'cat patch',2:'full repaint',3:'CRC diagnostic',4:'measure',5:'full benchmark'}
PHASES=['session start','teardown','HTTP/validation','PERF build/print','persistence/validation','console']

def names(mask,labels):return [label for i,label in enumerate(labels) if mask&(1<<i)]
def bound(k,n):return float(chi2.ppf(.95,2*(k+1))/2/n) if n else None

def analyze(path):
    r=json.loads((path/'result.json').read_text());records=r['context_records'];e=r['exposure'];kind=r['kind'];scopes=[];assigned=set();cycles=[]
    if kind in ('A','C'):
        rows=r['rows']
        for i,row in enumerate(rows):
            # A's final stats belong to that cycle's observed cleanup. The next
            # before endpoint (or trigger_after) includes them without repairing data.
            end=(rows[i+1]['before'] if i+1<len(rows) else r.get('trigger_after',row['after'])) if kind=='A' else row['after']
            before=row['before'];ids=list(range(before['dma_errors'],end['dma_errors']));assigned.update(ids)
            cycles.append({'cycle':row['cycle'],'before':before,'after':end,'error_indices':ids,'hardware_errors':len(ids),'completed_stripes':end['completed']-before['completed'],'affected':bool(ids)})
            scopes.append({'name':'cycle-'+str(row['cycle']),'indices':ids})
    elif kind=='B':
        journal=[json.loads(line) for line in (path/'durable-events.jsonl').read_text().splitlines()]
        error_journal=[event for event in journal if event['marker']=='DISPLAY_DMA_ERROR']
        for pair in r['pairs']:
            ids=[]
            # Serial interval attribution is bounded, not a CPU execution trace.
            # Context's durable receive cursor is recovered from the retained journal.
            for event in error_journal:
                if pair['cursor_before']<event['cursor']<=pair['cursor_after']:ids.append(event['value']['index'])
            if ids:scopes.append({'name':f"call-{pair['call']}-pair-{pair['dump']}",'indices':ids});assigned.update(ids)
    in_cell=set(range(r['before']['dma_errors'],r['after']['dma_errors']));outside=in_cell-assigned
    matched={}
    for event in r.get('spi_error_records',[]):matched.setdefault(event['dma_index'],[]).append(event)
    details=[]
    for event in records:
        row=event['record'];idx=event['index'];scope=next((s['name'] for s in scopes if idx in s['indices']),None)
        if scope is None:
            if kind=='A' and r.get('padding_before',{}).get('dma_errors',0)<=idx<r.get('padding_after',{}).get('dma_errors',0):scope='A-padding-cat-1Hzstats'
            elif idx not in in_cell:scope='setup/warmup/final-cleanup (outside cell endpoints)'
            else:scope='in-cell boundary/other'
        details.append({'index':idx,'scope':scope,'frame':row[8],'frame_kind':KINDS.get(row[9],str(row[9])),'stripe':row[10],'rect':row[11],'bytes':row[12],'stripe_start_us':row[1],'callback_us':row[2],'recycle_us':row[3],'dma_pre_us':event['dma_pre_us'],'dma_post_us':event['dma_post_us'],'flags':row[4],'phase_mask':row[13],'phases':names(row[13],PHASES),'LittleFS_overlap':row[5:8],'SPI1_mask':row[14],'SPI1_ops':names(row[14],['read','write','erase-region']),'matched_SPI1_windows':matched.get(idx,[]),'spi_api_total':event['spi_api_total'],'spi_coverage_errors':event['spi_coverage_errors']})
    result={'cell':r['name'],'kind':kind,'rate_valid':r['rate_valid'],'clean':r['clean'],'exposure':e,'fresh_boot':r['fresh_boot'],'continued_preflight_boot':r.get('continued_preflight_boot',False),'whole_boot_hardware_errors':r['cleanup_info']['dma_errors'],'context_count':len(records),'all_contexts_captured':r['every_dma_error_context_captured'],'cycles':cycles,'cycle_hardware_errors':sum(c['hardware_errors'] for c in cycles),'affected_cycles':sum(c['affected'] for c in cycles),'cycle_count':len(cycles),'affected_pairs':len(scopes) if kind=='B' else None,'unassigned_in_cell_indices':sorted(outside),'error_details':details,'phase_clusters':dict(Counter(d['phase_mask'] for d in details)),'frame_clusters':dict(Counter(d['frame'] for d in details)),'SPI1_overlap_errors':sum(bool(d['SPI1_mask'] or d['matched_SPI1_windows']) for d in details),'poisson_upper_events_per_stripe':bound(e['dma_errors'],e['completed']),'model':'Nominal Poisson/Bernoulli descriptions; independence not established. No unlike-cell pooling.'}
    if cycles:
        k=result['cycle_hardware_errors'];n=result['cycle_count'];result['poisson_upper_events_per_cycle']=bound(k,n);result['affected_cycle_rate']=result['affected_cycles']/n
    power={'kind':kind,'positive':r['cleanup_info']['dma_errors']>0,'original_floor':{'A':60,'B':1050,'C':20,'D':100000}[kind]}
    if cycles and result['affected_cycles']:
        q=result['affected_cycle_rate'];power.update(unit='affected-cycle',rate40=q,required_cycles=max(power['original_floor'],math.ceil(6/q)),expected_count_at_floor=q*power['original_floor'])
    elif kind=='B' and result['affected_pairs']:
        n=len(r['pairs']);q=result['affected_pairs']/n;power.update(unit='affected-pair-by-serial-interval',rate40=q,required_dumps=max(1050,math.ceil(6/q)),required_calls=1,expected_count_at_floor=q*1050)
    elif kind=='A' and r.get('padding_after',{}).get('dma_errors',0)>r.get('padding_before',{}).get('dma_errors',0):
        a,b=r['padding_before'],r['padding_after'];k=b['dma_errors']-a['dma_errors'];n=b['completed']-a['completed'];power.update(unit='A-padding-stripe',rate40=k/n,required_cycles=60,required_padding_stripes=math.ceil(6*n/k),expected_count_at_original_padding=k)
    elif kind=='D' and e['dma_errors']:
        power.update(unit='D-stripe',rate40=e['dma_errors']/e['completed'],required_stripes=max(100000,math.ceil(6*e['completed']/e['dma_errors'])),expected_count_at_floor=100000*e['dma_errors']/e['completed'])
    elif power['positive']:
        k=r['cleanup_info']['dma_errors'];factor=max(1,math.ceil(6/k));power.update(unit='whole-reproducer-boundary-fallback',repeat_factor=factor,minimum_expected_count=k*factor,reason='No homogeneous cycle/pair/padding assignment; repeat original trigger density and fresh-boot cell, never omit boundary error')
    else:power['unit']='no positive errors'
    result['power']=power;save(path/'analysis.json',result)
    lines=['# '+r['name']+' — every hardware error context','','| Index | Scope | Frame/kind/stripe | Rect/bytes | DMA pre→post µs | Phase mask / labels | SPI1 / API windows |','| --- | --- | --- | --- | --- | --- | --- |']
    for d in details:lines.append(f"| {d['index']} | {d['scope']} | {d['frame']}/{d['frame_kind']}/{d['stripe']} | {d['rect']} / {d['bytes']} | {d['dma_pre_us']}→{d['dma_post_us']} | {d['phase_mask']} / {', '.join(d['phases'])} | {d['SPI1_mask']} / {len(d['matched_SPI1_windows'])} |")
    (path/'error-contexts.md').write_text('\n'.join(lines)+'\n');return result

def main():
    p=argparse.ArgumentParser();p.add_argument('cell');a=p.parse_args();r=analyze(OUT/a.cell);print(json.dumps({k:r[k] for k in ('cell','rate_valid','clean','whole_boot_hardware_errors','context_count','cycle_count','cycle_hardware_errors','affected_cycles','SPI1_overlap_errors','power')}))
if __name__=='__main__':main()
