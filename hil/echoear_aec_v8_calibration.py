"""Retained Jabra process-duration calibration; host-only and deterministic."""
import json
from pathlib import Path
from echoear_aec_bench import EVIDENCE,sha


def calibration():
    groups={}
    for name,paths in [('step-8',sorted((EVIDENCE/'step-8').glob('dry-*/**/result.json'))+sorted((EVIDENCE/'step-8/aux-1').rglob('result.json'))),('campaign-04',[EVIDENCE/'step-9/campaign-04/qualification-01/result.json'])]:
        timers={}
        def visit(value,path):
            if isinstance(value,dict):
                if 'expected_seconds' in value and 'elapsed_seconds' in value:
                    key=(value['expected_seconds'],value['elapsed_seconds'])
                    row=timers.setdefault(key,dict(expected_s=key[0],elapsed_s=key[1],error_s=key[1]-key[0],sources=[]))
                    source=dict(path=str(path),sha256=sha(path))
                    if source not in row['sources']:row['sources'].append(source)
                for v in value.values():visit(v,path)
            elif isinstance(value,list):
                for v in value:visit(v,path)
        for path in paths:visit(json.loads(path.read_text()),path)
        values=[timers[key] for key in sorted(timers)]
        groups[name]=dict(unique_timers=len(values),over_100ms=sum(r['error_s']>.1 for r in values),min_error_s=min(r['error_s'] for r in values),max_error_s=max(r['error_s'] for r in values),timers=values)
    return dict(basis='Same subprocess.run(aplay) wall timer; launch/drain overhead is positive. Failed campaign-04 numeric timers were not retained; no invented values.',deduplication='Unique (expected_seconds, elapsed_seconds) per evidence group',groups=groups)
