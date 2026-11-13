#!/usr/bin/env python3
"""Export per-turn pause measurements and auditable aggregates without rescoring."""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from step8d3_pause import decision

FIELDS=('spl','bound','fixture','round','correct_audible_reply','time_to_correct_reply_ms',
    'first_fragment_started_generation','cancelled_before_first_tts_byte','continuation_dropped_by_mute',
    'continuation_detected_after_first_tts_byte','continuation_gate_or_mute_exposed','vad_missed_continuation_onset')


def export(directory):
    raw=json.loads((directory/'result.json').read_text());turns=raw['turns']
    with (directory/'per-turn.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader()
        for turn in turns:
            row={**turn,**turn['pause']}
            writer.writerow({key:row.get(key) if row.get(key) is not None else 'unknown' for key in FIELDS})
    summary=dict(turns=len(turns),completed=raw['completed'],error=raw.get('error'),arms={})
    for bound in (3,1):
        rows=[t for t in turns if t['bound']==bound]
        latency=[r['pause']['time_to_correct_reply_ms'] for r in rows if r['pause']['correct_audible_reply']
                 and r['pause']['time_to_correct_reply_ms'] is not None]
        model=[e for t in rows for e in t['events'] if e['event']=='smartturn-inference' and e.get('kind')=='model']
        summary['arms'][str(bound)]=dict(n=len(rows),correct=sum(t['pause']['correct_audible_reply'] for t in rows),
            latency_n=len(latency),latency_p50_ms=float(np.median(latency)) if latency else None,
            first_fragment_generations=sum(t['pause']['first_fragment_started_generation'] for t in rows),
            cancelled_before_first_tts=sum(t['pause']['cancelled_before_first_tts_byte'] for t in rows),
            muted_continuations=sum(t['pause']['continuation_dropped_by_mute'] is True for t in rows),
            unknown_mute_exposure=sum(t['pause']['continuation_dropped_by_mute'] is None for t in rows),
            vad_missed_continuations=sum(t['pause']['vad_missed_continuation_onset'] is True for t in rows),
            model_calls=len(model),model_verdicts={v:sum(e.get('verdict')==v for e in model) for v in ('COMPLETE','INCOMPLETE')},
            stop_secs_timeouts=sum(e['event']=='smartturn-inference' and e.get('kind')=='stop_secs_timeout' for t in rows for e in t['events']))
    summary['decision']=decision(turns,raw['repetitions'])
    (directory/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args();print(json.dumps(export(a.directory)))
