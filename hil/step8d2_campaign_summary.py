#!/usr/bin/env python3
"""Summarize the interleaved campaign without replacing raw observations."""
import argparse
import json
from pathlib import Path
import numpy as np
from agent_conversation_test import summarize,write_json,TRANSPORT_COUNTERS
from step8d2_analysis import analyze


def quantiles(values):
    values=[v for v in values if v is not None]
    return dict(n=len(values),p50=float(np.median(values)) if values else None,
        p90=float(np.percentile(values,90)) if values else None,max=max(values) if values else None)


def counter_details(rows,reply_window=False):
    result={}
    for key in TRANSPORT_COUNTERS:
        values=[(row.get('reply_window_transport',{}).get('deltas',{}) if reply_window else row.get('transport_deltas',{})).get(key) for row in rows]
        known=[v for v in values if v is not None]
        result[key]=dict(available=len(known),turns_nonzero=sum(v!=0 for v in known),
            total=sum(known) if known else None,max=max(known) if known else None)
    return result


def details(rows):
    cancelled=[r for row in rows for r in row.get('cancelled_responses',[])]
    inputs=[row['server_input'] for row in rows if row.get('server_input',{}).get('available')]
    smart=[e for row in rows for e in row.get('events',[]) if e['event']=='smartturn-inference']
    timeouts=[e for e in smart if e.get('kind')=='stop_secs_timeout' or (e.get('kind') is None and e.get('model_metrics_available') is False)]
    smart=[e for e in smart if e.get('kind') in (None,'model') and e.get('model_metrics_available') is True]
    smart_metrics=[e for row in rows for e in row.get('events',[]) if e['event']=='smartturn']
    tails=[e['tail_ms'] for row in rows for e in row.get('mute_evidence',{}).get('board_tail_after_server_stop',[])]
    return dict(summary=summarize(rows),
        single_correct_audible=sum(r.get('single_correct_audible_reply',False) for r in rows),
        time_to_correct_reply_ms=quantiles([r.get('time_to_correct_reply_ms') for r in rows]),
        cancelled=dict(total=len(cancelled),under_300ms=sum(r['audible_ms'] is not None and r['audible_ms']<300 for r in cancelled),
                       at_least_300ms=sum(r['audible_ms'] is not None and r['audible_ms']>=300 for r in cancelled),
                       unknown=sum(r['audible_ms'] is None for r in cancelled)),
        transport_turns=sum(r.get('transport_observed',False) for r in rows),
        transport=counter_details(rows),
        reply_window_transport_turns=sum(r.get('reply_window_transport_damaged',False) for r in rows),
        reply_window_transport=counter_details(rows,reply_window=True),
        turn_policy_results={k:sum(r.get('turn_policy_results',{}).get(k,False) for r in rows)
            for k in ('vad','stt','llm_content','one_uncancelled_complete_generation')},
        input=dict(turns=len(inputs),active_rms_dbfs=quantiles([r['active_rms_dbfs'] for r in inputs]),
            pre_speech_noise_dbfs=quantiles([r['pre_speech_noise_dbfs'] for r in inputs]),
            peak_dbfs=quantiles([r['peak_dbfs'] for r in inputs]),clipped_samples=sum(r['clipped_samples'] for r in inputs)),
        vad_onset_delay_ms=quantiles([v for r in rows for v in r.get('vad_onset_delay_ms',[])[:1]]),
        smartturn_timeout_calls=len(timeouts),
        smartturn={verdict:dict(count=sum(e['verdict']==verdict for e in smart),
            probability=quantiles([e.get('probability') for e in smart if e['verdict']==verdict]),
            analyzer_call_ms=quantiles([e['inference_time_ms'] for e in smart if e['verdict']==verdict]),
            model_e2e_ms=quantiles([e.get('e2e_processing_time_ms') for e in smart_metrics if ('COMPLETE' if e.get('is_complete') else 'INCOMPLETE')==verdict]))
            for verdict in sorted({e['verdict'] for e in smart})},
        board_tail_after_server_stop_ms=quantiles(tails),
        user_turns_during_bot_speech=sum(len(r.get('mute_evidence',{}).get('user_turns_during_bot_speech',[])) for r in rows),
        interruptions_during_bot_speech=sum(len(r.get('mute_evidence',{}).get('interruptions_during_bot_speech',[])) for r in rows),
        tail_user_turns=sum(len(r.get('mute_evidence',{}).get('tail_user_turns',[])) for r in rows),
        gate_opens_during_bot_speech=sum(len(r.get('mute_evidence',{}).get('gate_opens_during_bot_speech',[])) for r in rows),
        raw_transcripts_during_bot_speech=sum(len(r.get('mute_evidence',{}).get('raw_transcripts_during_bot_speech',[])) for r in rows),
        raw_transcripts_in_board_tail=sum(len(r.get('mute_evidence',{}).get('raw_transcripts_in_board_tail',[])) for r in rows),
        failure_causes={cause:sum(any(f['cause']==cause for f in r['failures']) for r in rows)
            for cause in sorted({f['cause'] for r in rows for f in r['failures']})})


def campaign(directory):
    raw=json.loads((directory/'result.json').read_text());runs=[]
    for original in raw['runs']:
        path=Path(original['path']);result=analyze(path)
        runs.append({**original,'observation':result})
    grouped={}
    for name in raw['arms']:
        grouped[name]={}
        for spl in (65,58):
            rows=[t for r in runs if r['arm']==name and r['spl']==spl for t in r['observation']['turns'] if t['prompt']!='pause']
            grouped[name][str(spl)]=details(rows)
        pauses=[t for r in runs if r['arm']==name for t in r['observation']['turns'] if t['prompt']=='pause']
        grouped[name]['pause']=details(pauses)
    value=dict(arms=grouped,completed=raw['completed'],pass_rule=raw['pass_rule'],raw_campaign=str(directory/'result.json'))
    write_json(directory/'analysis-8d2.json',value)
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    value=campaign(a.directory.resolve())
    print(json.dumps({k:{s:v['summary']['passes'] for s,v in arm.items()} for k,arm in value['arms'].items()}))
