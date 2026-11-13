#!/usr/bin/env python3
"""Recompute the brief's exact distortion gates from retained dual-chain data."""
import argparse
import json
from pathlib import Path
from speaker_dual_sweep_test import correct,AMPLITUDES,FREQUENCIES

def summarize(source):
    r=json.loads(source.read_text())
    for row in r['points']+r['reruns']:correct(row,r['response_dbfs'])
    r['passing_amplitudes']=[amp for amp in AMPLITUDES if all(p['passed'] for p in r['points'] if p['amplitude_percent']==amp) and len([p for p in r['points'] if p['amplitude_percent']==amp])==len(FREQUENCIES)*2 and r['mic_linear_low_level']]
    r['candidate_amplitude']=max(r['passing_amplitudes'],default=None)
    r['h3_disagreements']=[p['name'] for p in r['points'] if (p['h3_disagreement_db'] or 0)>6]
    r['analysis_note']='Exact brief gates: no ADC clipping; THD <=5%; H2/H3 <=-26 dBc. Peak headroom is reported without adding a -1 dBFS acceptance threshold. Original capture/result retained.'
    return r

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args()
    r=summarize(a.source);a.destination.write_text(json.dumps(r,indent=2)+'\n')
    print('Passing amplitudes:',r['passing_amplitudes']);print('Candidate:',r['candidate_amplitude'])
