#!/usr/bin/env python3
"""Summarize retained R3 maximum controls without pooling unlike cells."""
import json
from pathlib import Path
from step9a_r2_checks import audit
from step9a_r2_diagnose import save
OUT=Path('/home/khkramer/src/evidence/step9a/r3')

def main():
    rows=[]
    for name in ('maximum40-cat','maximum40-full'):
        cell=OUT/name;r=json.loads((cell/'result.json').read_text());raw=(cell/'serial.log').read_bytes()
        independent=audit(raw,diagnostic=True)
        assert independent['errors']==r['monitor']['errors'] and independent['animation_snapshots']==r['monitor']['animation_snapshots'],name+' raw/live health differs'
        assert independent['raw_bytes']>=r['monitor']['raw_bytes']
        closing_tail=independent['raw_bytes']-r['monitor']['raw_bytes']
        assert r['valid'] and r['clean'] and r['exposure']['completed']>=100000
        assert r['exposure']['dma_errors']==0 and r['stress_measured']['overlap_stripes']>0
        assert not r['cleanup_stress']['running'] and not r['cleanup_stress']['allocations_live']
        assert r['cleanup_status']['bench'][0]==0 and r['cleanup_status']['flash']['count']==16 and r['cleanup_status']['flash']['temp_bytes']==0
        rows.append({'cell':name,'requested_hz':r['requested_hz'],'actual_khz':r['after']['actual_khz'],'fresh_boot_repetitions_this_shape':1,'valid':r['valid'],'clean':r['clean'],'exposure':r['exposure'],'stressor':r['stress_measured'],'hardware_error_records':r['dma_trace']['total'],'clustering':'No error events; clustering not estimable','monitor':{k:v for k,v in independent.items() if k!='errors'},'cleanup_max_gap_us':r['cleanup_info']['scheduling_gap_max_us'],'live_summary_bytes':r['monitor']['raw_bytes'],'closing_tail_bytes_audited':closing_tail})
    result={'diagnostic_only':True,'positive_control_success':False,'matrix_informative':False,'blocked_reason':'Maximum predeclared core1 PSRAM-copy stress produced no40MHz underflows in either100000-stripe shape. Brief requires stopping an uninformative matrix.','D40':None,'acceleration_factor':None,'selected_clock':None,'cells':rows,'cells_not_run':['40MHz natural no-stressor100000 stripes/1050stats+dump-arrivals/30connect-disconnect cycles','D40 tier/burst/period/phase sweep and fresh repeat','26.67MHz production and stress cells','20MHz production and stress cells','chosen-clock30000-stripe fresh confirmation','chosen-clock persist/preload/Idle/full performance'],'step3_not_started':True,'poisson_bounds':'One-sided95% nominalPoisson upper bounds per completedstripe; descriptive model assumes independence, not established for clustered memory-contention faults. Separate cells are not pooled.','scope':'Finite20ms/21ms memcpy control only; no universal traffic safety claim; retained r2 production40MHz failure remains binding'}
    save(OUT/'step2-summary.json',result);print(json.dumps(result,indent=2))
if __name__=='__main__':main()
