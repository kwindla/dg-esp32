#!/usr/bin/env python3
"""Analyze retained observations with explicit, audited checker corrections.

The initial qualification checker coupled reply count to VAD detection. A
missing VAD edge is still a failed VAD check, but does not mean one observed
LLM generation is zero replies. Original result.json files remain immutable.
"""
import argparse
import copy
import json
from pathlib import Path
import re
from agent_conversation_test import one_reply_matches, failure_cause, summarize, CHECKS, answer_matches
from agent_prompts import EXTRA


def analyze(raw):
    turns=[];corrections=[];previous={}
    for index, original in enumerate(raw['turns'], 1):
        row=copy.deepcopy(original)
        if row['prompt']=='long' and row['checks'].get('acoustic_content') is False \
                and answer_matches(row.get('acoustic_text',''), EXTRA[0]):
            corrections.append(dict(index=index,prompt='long',check='acoustic_content',
                original=False,corrected=True,
                reason='Normalize the exact complete smart-formatted 1..20 sequence into number tokens'))
            row['checks']['acoustic_content']=True
            row['failures']=[f for f in row['failures'] if f['check']!='acoustic_content']
        if row['checks'].get('playback') is False and row.get('latency_ms',{}).get('audible_reply') is not None:
            corrections.append(dict(index=index,prompt=row['prompt'],check='audible_reply_timestamp',
                original=row['latency_ms']['audible_reply'],corrected=None,
                reason='An unqualified acoustic candidate cannot supply a verified audible onset'))
            row['latency_ms']['audible_reply']=None
            for stage in row.get('stage_latency_ms',{}):
                if 'audible_reply' in stage:row['stage_latency_ms'][stage]=None
        if 'one_reply' in row['checks']:
            counted=one_reply_matches(row.get('events', []), row.get('tts_segments'))
            if row['checks']['one_reply'] != counted:
                corrections.append(dict(index=index, prompt=row['prompt'], check='one_reply',
                    original=row['checks']['one_reply'], corrected=counted,
                    reason='Count complete LLM generations separately from the required VAD edge'))
                row['checks']['one_reply']=counted
                row['failures']=[f for f in row['failures'] if f['check']!='one_reply']
                if not counted:
                    row['failures'].append(dict(check='one_reply',cause='reply count',detail='Not exactly one complete generation'))
        for failure in row['failures']:
            if failure['check'] in CHECKS:
                failure['cause']=failure_cause(failure['check'],row['checks'])
                no_uplink = all(row.get(boundary, {}).get(counter) == 0
                    for boundary in ('before_perf', 'after_perf') for counter in ('sent', 'encode_frames'))
                if failure['check'] in ('playback', 'gate', 'one_reply') \
                        and row['checks'].get('stt') is False and not row.get('stt_text') \
                        and not any(e['event']=='llm-start' for e in row.get('events', [])):
                    failure['cause']='STT miss (upstream; no generated reply)'
                    failure['cause_basis']='No final transcript or LLM generation; downstream reply evidence unavailable'
                if failure['check'] != 'session' and no_uplink and not row.get('stt_text'):
                    failure['cause']='capture/uplink inactive'
                    failure['cause_basis']='Both device boundaries have zero encoded and sent frames'
                if row['prompt']=='italy' and failure['check'] in ('llm_content','acoustic_content') \
                        and row['checks'].get('llm_content') is False \
                        and row.get('text', 'And of Italy?') == 'And of Italy?' \
                        and re.search(r'\bend\s+of\s+italy\b', row.get('stt_text','').casefold()):
                    failure['cause']='fixture defect (And rendered as End)'
                    failure['cause_basis']='The clean original fixture was independently transcribed as End of Italy; see step8d prompt-stt-validation.json'
        dependency={'italy':'france','recall':'name'}.get(row['prompt'])
        parent=previous.get((row.get('conversation'),dependency)) if dependency else None
        if parent and parent['checks'].get('stt') is False and row['checks'].get('llm_content') is False:
            for failure in row['failures']:
                if failure['check'] in ('llm_content','acoustic_content'):
                    failure['cause']='STT miss (prior '+dependency+')'
                    failure['upstream_prompt']=dependency
        previous[(row.get('conversation'),row['prompt'])]=row
        row['passed']=len(row['checks'])==len(CHECKS) and all(row['checks'].values()) and not row['failures']
        turns.append(row)
    groups={}
    for row in turns:
        if 'conversation' in row:groups.setdefault(str(row['conversation']),[]).append(row)
    return dict(summary=summarize(turns),checker_corrections=corrections,
        original_summary=raw['summary'],
        conversation_passes=sum(len(rows)>=6 and all(r['passed'] for r in rows) for rows in groups.values()),
        conversation_count=len(groups),
        by_temperature={key:summarize([r for r in turns if r.get('temperature')==key]) for key in ('cold','warm')},
        failure_causes={cause:sum(any(f['cause']==cause for f in r['failures']) for r in turns)
                        for cause in sorted({f['cause'] for r in turns for f in r['failures']})})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    result=analyze(json.loads((a.directory/'result.json').read_text()))
    (a.directory/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(passes=result['summary']['passes'],turns=result['summary']['turns'],
        corrections=len(result['checker_corrections']),conversations=result['conversation_passes'])))
