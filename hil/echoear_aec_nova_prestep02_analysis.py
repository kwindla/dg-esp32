"""Read-only reconstruction of retained Nova pre-step 02 turn observations.

No model inference, service, board or profile changes. Output belongs outside
both run directories; original runner analyses are immutable evidence.
"""
import argparse
import csv
import json
from pathlib import Path
import re
import subprocess
import wave

import echoear_aec_nova_completion as c
import echoear_aec_nova_preflight as a
from echoear_aec_bench import ROOT, sha, write

PLAN = c.n.BASE/'freeze-v8.6-nova-prestep-02/plan.json'


def indexed(path):
    return [dict(source=str(path),line=i,record=json.loads(line))
            for i,line in enumerate(Path(path).read_text().splitlines(),1) if line.strip()]


def timeline(output=c.OUTPUT):
    out=Path(output)
    evaluation=indexed(out/'evaluation/events.jsonl')
    recorded=[r for path in sorted((out/'sessions').glob('*/events.jsonl')) for r in indexed(path)]
    cases=[]
    for cell in a.read(out/'result.json')['cases']:
        cid=cell['run_id']
        own=[r for r in evaluation if r['record'].get('case_id')==cid]
        end=max(r['record']['t'] for r in own)
        pcs={r['record']['pc_id'] for r in own if r['record'].get('pc_id')}
        rec=[r for r in recorded if r['record'].get('pc_id') in pcs and
             cell['start_ns']<=r['record']['monotonic_ns']<=end*1e9]
        events=[]
        for row in own:
            r=row['record']; frame=r.get('frame')
            if r['event'] in ('user-turn-started','user-turn-stopped','nova-transcript-boundary',
                'real-bot-llm-request','tts-context-request','tts-generation-metric-start',
                'tts-generation-stopped','tts-interruption-stopped','case-sealed') or (
                frame in ('VADUserStartedSpeakingFrame','VADUserStoppedSpeakingFrame','InterruptionFrame',
                          'BotStartedSpeakingFrame','BotStoppedSpeakingFrame') and r.get('direction')=='DOWNSTREAM'):
                events.append(dict(t=r['t'],kind=frame or r['event'],**row))
        for row in rec:
            r=row['record']
            if (r['kind']=='stt-frame' and r.get('frame') in ('InterimTranscriptionFrame','TranscriptionFrame')) or (
                r['kind']=='frame' and r.get('frame') in ('LLMFullResponseStartFrame','LLMTextFrame','LLMFullResponseEndFrame')) or (
                r['kind']=='aggregator-assistant-turn-stopped'):
                events.append(dict(t=r['monotonic_ns']/1e9,kind=r.get('frame',r['kind']),**row))
        first_vad=next((e['t'] for e in events if e['kind']=='VADUserStartedSpeakingFrame'),None)
        for e in events:e['relative_first_vad_s']=e['t']-first_vad if first_vad else None
        contexts=[]
        for row in own:
            r=row['record']
            if r['event']!='tts-context-request':continue
            cid_context=r['context_id']
            writes=[x for x in rec if x['record']['kind']=='tts-transport-write' and x['record'].get('context_id')==cid_context]
            active=[x for x in writes if x['record']['non_silent']]
            contexts.append(dict(context_id=cid_context,request=row,transport_write_count=len(writes),
                first_write=writes[0] if writes else None,last_write=writes[-1] if writes else None,
                first_non_silent_write=active[0] if active else None,
                context_pcm=[x for x in own if x['record']['event']=='tts-context-pcm' and x['record'].get('context_id')==cid_context],
                closures=[x for x in own if x['record']['event'] in ('tts-generation-stopped','tts-interruption-stopped') and x['record'].get('context_id')==cid_context]))
        cases.append(dict(id=cid,pc_ids=sorted(pcs),capture_start_s=cell['start_ns']/1e9,
            jabra=cell.get('jabra'),first_vad_s=first_vad,events=sorted(events,key=lambda e:e['t']),contexts=contexts))
    log=out/'bot/server-1.log'
    smart=[dict(source=str(log),line=i,wall_clock=line[:23],state=match.group(1),
                call_entry_monotonic_s=None,completion_monotonic_s=None,probability=None,raw=line)
           for i,line in enumerate(log.read_text().splitlines(),1)
           if (match:=re.search(r'End of Turn result: EndOfTurnState\.(\w+)',line))]
    audio=[]
    for path in sorted((out/'sessions').glob('*/uplink.wav')):
        with wave.open(str(path)) as wav:
            rows=[r['record'] for r in recorded if r['source']==str(path.with_name('events.jsonl')) and
                  r['record']['kind']=='audio' and r['record'].get('stream')=='uplink']
            audio.append(dict(path=str(path),sha256=sha(path),rate=wav.getframerate(),channels=wav.getnchannels(),
                sample_width=wav.getsampwidth(),samples=wav.getnframes(),audio_records=len(rows),
                last_sample_end=rows[-1]['sample_end'] if rows else None,
                sample_offsets_contiguous=bool(rows) and rows[0]['sample_start']==0 and
                    all(x['sample_end']==y['sample_start'] for x,y in zip(rows,rows[1:])),
                gap_suspected_count=sum(r.get('gap_suspected',False) for r in rows),
                note='Storage offsets are not an acoustic clock; replay recorded frame sizes and arrival times, not WAV contiguity alone.'))
    return dict(cases=cases,smart_turn_log_observations=smart,uplink=audio,
        probability_status='Not recorded: session EVENTS omit MetricsFrame, evaluation captures TTS metrics only; server DEBUG log retains COMPLETE, not TRACE probabilities. No inferred probability or exact call-entry timestamp.',
        wall_clock_note='Server log wall timestamps retained at millisecond precision; no invented conversion to exact monotonic time.')


def generate(destination):
    destination=Path(destination).resolve()
    if destination.exists() or any(destination==run or run in destination.parents for run in (c.PRIOR,c.OUTPUT)):
        raise ValueError('New analysis directory outside retained runs required')
    before={**c.manifest(c.PRIOR),**c.manifest(c.OUTPUT)}
    plan=a.read(PLAN)
    combined=c.combine(plan,c.OUTPUT)
    observed=timeline()
    provenance=dict(retained_inputs_sha256=before,plan=dict(path=str(PLAN),sha256=sha(PLAN)),
        analysis_runtime_sha256={name:sha(ROOT/name) for name in c.n.runtime_files()},
        analysis_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        note='Acquisition runtime remains the frozen plan; analysis runtime is separately bound. Original runner files preserved.')
    c.check_manifest(before)
    if before!={**c.manifest(c.PRIOR),**c.manifest(c.OUTPUT)}:raise ValueError('Retained file set changed')
    destination.mkdir(parents=True)
    write(destination/'combined-nova-preflight.rejudged.json',dict(**combined,analysis_provenance=provenance))
    write(destination/'turn-timeline.json',dict(**observed,analysis_provenance=provenance))
    with (destination/'turn-timeline.csv').open('w') as f:
        fields=('case','monotonic_s','relative_first_vad_s','kind','text','strategy','context_id','source','line')
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for cell in observed['cases']:
            for e in cell['events']:
                writer.writerow(dict(case=cell['id'],monotonic_s=f"{e['t']:.9f}",relative_first_vad_s=e['relative_first_vad_s'],
                    kind=e['kind'],**{k:e['record'].get(k) for k in ('text','strategy','context_id')},source=e['source'],line=e['line']))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--destination',required=True)
    generate(parser.parse_args().destination)
