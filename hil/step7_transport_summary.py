#!/usr/bin/env python3
"""Compare nine final sessions with the preserved 15-session step-6c lean cohort."""
import argparse
import json
from pathlib import Path
from phase2_campaign import compare,write_json
from step6c_summary import population

def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path)
    p.add_argument('--expected-baseline-sessions',type=int,default=15)
    p.add_argument('--expected-candidate-sessions',type=int,default=9)
    p.add_argument('--expected-boots',type=int,default=3)
    p.add_argument('--baseline',type=Path,default=Path('../evidence/step6c/ab/lean/summary.json'));a=p.parse_args()
    baseline=json.loads(a.baseline.read_text());candidate=population(a.directory)
    result=compare(baseline,candidate,expected=a.expected_candidate_sessions)
    # Historical control has 15 formal samples; this brief requires 3x3 new
    # samples. Retain both actual population sizes and check them explicitly.
    result['checks']['complete']=(baseline['media_sessions']==a.expected_baseline_sessions and candidate['media_sessions']==a.expected_candidate_sessions and
        candidate['boots']==a.expected_boots and not baseline['boot_errors'] and not candidate['boot_errors'])
    # Step-6a's memory-gain adoption criterion does not apply to this app step.
    del result['checks']['ready_gain']
    for key in (k for k in baseline['metrics'] if k.startswith('cpu_')):
        bound=baseline['metrics'][key]['p50']*1.1
        result['metrics'][key]['p50_bound']=bound
        result['checks'][key]=candidate['metrics'][key]['p50']<=bound
    result['checks']['wire_rate_le_36_kbps']=candidate['wire_rate_le_36_kbps']
    result['baseline']=str(a.baseline.resolve())
    result['passed']=all(result['checks'].values())
    write_json(a.directory/'comparison.json',result)
    print(json.dumps({'passed':result['passed'],'failed':[k for k,v in result['checks'].items() if not v]},indent=2))
    return int(not result['passed'])
if __name__=='__main__':raise SystemExit(main())
