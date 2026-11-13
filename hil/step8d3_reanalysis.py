#!/usr/bin/env python3
"""Read-only historical reanalysis; write derived outputs to a new destination."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from soak_memory import transient_memory,heap_trends,growth_verdict
from step8d2_campaign_summary import details
from esp_serial import ROOT
from agent_conversation_test import write_json


def reanalyze(out):
    out.mkdir(parents=True,exist_ok=False);results={}
    for path in [ROOT.parent/'evidence/step8d2/final/soak20/result.json',
                 ROOT.parent/'evidence/step8d/soak-final-v3/result.json']:
        raw=json.loads(path.read_text());memory=transient_memory(raw['observations'],raw['closed'],raw.get('faults',[]))
        trends=heap_trends(raw['observations'])
        results[str(path)]=dict(raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),memory=memory,
                               trends=trends,growth_verdict=growth_verdict(trends))
    path=ROOT.parent/'evidence/step8d2/final/qualification/result.json';raw=json.loads(path.read_text())
    failed=[(i,r) for i,r in enumerate(raw['turns'],1) if r.get('prompt')!='pause' and not r['passed']]
    transport=lambda r:r.get('reply_window_transport_damaged',any(v for v in r.get('reply_window_transport',{}).get('deltas',{}).values() if v is not None))
    results['qualification']=dict(raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        transport_associated=[i for i,r in failed if transport(r)],zero_reply_window_damage=[i for i,r in failed if not transport(r)],
        basis='reply_window_transport_damaged; whole-turn counters are context only')
    results['qualification_model_verdicts']=details([r for r in raw['turns'] if r['prompt']!='pause'])['smartturn']
    write_json(out/'result.json',results)
    return results

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();reanalyze(a.out.resolve())
