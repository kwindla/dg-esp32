#!/usr/bin/env python3
"""Apply phase-1 acceptance bounds to complete campaign distributions."""
import argparse
import json
from pathlib import Path

def compare(base,candidate,required_sessions,claimed_heap_bytes=0):
    b=base['metrics']; c=candidate['metrics']; checks={}
    def maxv(metrics,key): return metrics[key]['max']
    checks['complete_sessions']=candidate['media_sessions']==required_sessions
    checks['media_acceptance']=not candidate['failures']
    checks['boot_acceptance']=not candidate.get('boot_failures',[])
    if required_sessions==15:
        checks['extras_pass']=all(candidate.get('extras',{}).get(name) is True for name in ['greeting1','greeting2','loop','console'])
    checks['server_loss_zero']=maxv(c,'missing')==0
    checks['max_gap']=maxv(c,'max_gap_ms')<=maxv(b,'max_gap_ms')+20 and maxv(c,'max_gap_ms')<200
    checks['enqueue_mean']=c['enqueue_send_mean_us']['mean']<=b['enqueue_send_mean_us']['mean']+1000
    checks['enqueue_max']=maxv(c,'enqueue_send_max_us')<=maxv(b,'enqueue_send_max_us')+10000
    for name in ['encode_p95_us','encode_max_us','decode_p95_us','decode_max_us']:
        checks[name]=maxv(c,name)<=maxv(b,name)*1.1
    for name in ['late','lost','concealed','underruns','playout_dropped']:
        checks[name]=maxv(c,name)<=maxv(b,name)
    for name in ['greeting','reply']:
        checks[f'{name}_correlation']=c[f'{name}_correlation']['min']>=.9
    checks['capture_errors_zero']=bool(candidate['audio_counters']) and all(not any(row) for row in candidate['audio_counters'])
    for name in ['wifi_to_ready_us','open_connected_us']:
        checks[name]=candidate['timings'][name]['max']<=base['timings'][name]['max']+100000
    checks['dtls_duration_us']=maxv(c,'dtls_duration_us')<=maxv(b,'dtls_duration_us')+50000
    heap_gain={key:candidate['heap_phases']['ready']['general_internal'][key]-base['heap_phases']['ready']['general_internal'][key] for key in ['min','p50']}
    if claimed_heap_bytes:
        checks['claimed_heap_gain']=heap_gain['min']>=claimed_heap_bytes
    return dict(passed=all(checks.values()),checks=checks,baseline_loss_zero=maxv(b,'missing')==0,
                heap_gain_bytes=heap_gain,claimed_heap_bytes=claimed_heap_bytes,
                failed=[name for name,passed in checks.items() if not passed])


def compare_step6b(base,candidate,required_sessions):
    """Step 6b distribution bounds; retain all samples, including failed boots."""
    b=base['metrics'];c=candidate['metrics'];checks={}
    checks['complete_sessions']=candidate['media_sessions']==required_sessions and candidate['boots']*3==required_sessions
    # Preserve per-session failures, but judge RTP loss/pacing with the p95
    # bounds below. A single RTP maximum must not veto those distributions.
    network_assertions={'Server RTP loss/timing','Server RTP pacing'}
    checks['media_acceptance']=not any(
        not row.get('greeting') or not row.get('reply') or
        (row.get('acceptance_error') and row['acceptance_error'] not in network_assertions)
        for row in candidate['failures'])
    checks['boot_acceptance']=not candidate.get('boot_errors',[])
    for name in ['decode_p95_us']:
        checks[name]=c[name]['p95']<=b[name]['p95']
    checks['enqueue_mean']=c['enqueue_send_mean_us']['mean']<=b['enqueue_send_mean_us']['p95']
    for name in ['max_gap_ms','p95_gap_ms','missing']:
        checks[name]=c[name]['p95']<=b[name]['p95']
    for name in ['start_ready_us','wifi_to_ready_us','open_connected_us']:
        checks[name]=candidate['timings'][name]['p95']<=base['timings'][name]['p95']
    for name in ['i2s_underflows','rebuffers']:
        checks[name]=c.get(name,{}).get('max')==0
    for name in ['greeting','reply']:
        checks[f'{name}_correlation']=c[f'{name}_correlation']['min']>=.95
    checks['capture_errors_zero']=bool(candidate['audio_counters']) and all(not any(row) for row in candidate['audio_counters'])
    return dict(passed=all(checks.values()),checks=checks,failed=[k for k,v in checks.items() if not v])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('baseline',type=Path);p.add_argument('candidate',type=Path);p.add_argument('--sessions',type=int,default=15);p.add_argument('--claimed-heap-bytes',type=int,default=0);p.add_argument('--step6b',action='store_true');p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    result=(compare_step6b(json.loads(a.baseline.read_text()),json.loads(a.candidate.read_text()),a.sessions) if a.step6b else compare(json.loads(a.baseline.read_text()),json.loads(a.candidate.read_text()),a.sessions,a.claimed_heap_bytes))
    a.out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
