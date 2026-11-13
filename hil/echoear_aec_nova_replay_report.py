"""Score retained-uplink Nova replay; never awards live barge-in credit."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from echoear_aec_accept_analysis import wer


def read(path):return json.loads(path.read_text())
def write(path,value):path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')


def summarize(root):
    root=Path(root);clips=read(root/'clips.json')['clips'];out=[];integrity=[];models={};configs=set()
    for clip in clips:
        folder=root/'runs'/(clip['id']+'--dev')
        if not (folder/'result.json').exists():
            integrity.append(dict(clip=clip['id'],completed=False,reason='missing replay'));continue
        result=read(folder/'result.json');frames=read(folder/'frames.json');turns=read(folder/'turns.json')
        vendor=read(folder/'vendor-events.json');cfg=read(folder/'config.json')
        configs.add(cfg['profile_manifest']['config_sha256'])
        coverage=max((m['message'].get('start',0)+m['message'].get('duration',0) for m in vendor if isinstance(m['message'],dict) and m['message'].get('type')=='Results'),default=0)
        connections={m['message']['metadata'].get('request_id') for m in vendor if isinstance(m['message'],dict) and isinstance(m['message'].get('metadata'),dict)}-{None}
        for m in vendor:
            msg=m['message']
            if isinstance(msg,dict):
                metadata=msg.get('metadata',msg)
                info=metadata.get('model_info',{})
                if info.get('name'):
                    models[metadata.get('model_uuid',info['name'])]=info
                else:
                    models.update(info)
        same_file=hashlib.sha256(Path(clip['path']).read_bytes()).hexdigest()==clip['sha256']
        integrity.append(dict(clip=clip['id'],completed=result['completed'],vendor_coverage_s=coverage,
            capture_s=clip['duration_s'],vendor_covers_capture=coverage>=clip['pcm_duration_s'],
            one_connection=len(connections)==1 and len(result['requests'])==1,input_unchanged=same_file,
            sample_counts_equal=result['planned_samples']==result['sent_samples']==result['received']['aggregator_input']==result['received']['stt'],
            max_feed_lateness_s=result['max_feed_lateness_s'],p99_feed_lateness_s=result['p99_feed_lateness_s']))
        logical=lambda name:[f for f in frames if f['frame']==name and f['direction']=='DOWNSTREAM']
        stops=[t for t in turns if t['event']=='stopped'];starts=[t for t in turns if t['event']=='started']
        transcript=result['transcript'];positive=clip['kind']!='echo'
        for c in clip['cases']:
            expected=c.get('expected');words=re.findall(r'[a-z0-9]+',transcript.lower())
            expected_words=re.findall(r'[a-z0-9]+',(expected or '').lower())
            recognition=('stop' in words) if expected_words==['stop'] else wer(expected,transcript)<=.2 if expected else None
            out.append(dict(id=c['id'],clip=clip['id'],kind=c['kind'],voice_class=c.get('voice_class','firm'),
                clean=c['flux']['clean'],opportunity=c['flux']['opportunity'],
                transcript=transcript,recognized=recognition,wer=wer(expected,transcript) if expected else None,
                expected=expected,turn_starts=starts,turn_stops=stops,split_turn=positive and (len(stops)>1 or len(starts)>1),
                vad_starts=logical('VADUserStartedSpeakingFrame'),interruptions=logical('InterruptionFrame'),
                smart_turn=read(folder/'smart-turn.json'),live_barge_in_credit=False))
    firm=[r for r in out if r['kind']=='stop' and r['voice_class']=='firm' and r['clean'] and r['opportunity'] in ('eligible','ongoing-reply-lull')]
    echoes=[c for c in clips if c['kind']=='echo']
    unique_echo=[next(r for r in out if r['clip']==c['id']) for c in echoes if any(r['clip']==c['id'] for r in out)]
    intact=len(integrity)==len(clips) and all(all(r.get(k) for k in ('completed','vendor_covers_capture','one_connection','input_unchanged','sample_counts_equal')) for r in integrity)
    summary=dict(expected_captures=len(clips),completed=sum(r['completed'] for r in integrity),integrity_passed=intact,
        profile_config_sha256=sorted(configs),one_config=len(configs)==1,vendor_models=models,
        firm_eligible_stop_count=len(firm),firm_eligible_stop_recognized=sum(r['recognized'] for r in firm),
        split_turn_cells=[r['id'] for r in out if r['split_turn']],echo_captures=len(echoes),echo_reply_rows=sum(len(c['cases']) for c in echoes),
        echo_capture_s=sum(c['duration_s'] for c in echoes),echo_false_vad_starts=sum(len(r['vad_starts']) for r in unique_echo),
        echo_false_accepted_turns=sum(len(r['turn_starts']) for r in unique_echo),echo_interruptions=sum(len(r['interruptions']) for r in unique_echo),
        live_barge_in_credit=False,qualification=False,
        limitation='Historical bot state/downlink is fixed; no counterfactual flush, MIC3 or next-reply result is inferred.')
    summary['requested_validation_passed']=bool(intact and len(configs)==1 and len(firm)==10 and all(r['recognized'] for r in firm) and not summary['split_turn_cells'] and not summary['echo_false_accepted_turns'] and not summary['echo_interruptions'] and not summary['echo_false_vad_starts'])
    summary['groups']={f'{voice}:{kind}':dict(n=len(g),recognized=sum(bool(r['recognized']) for r in g),split_turns=sum(r['split_turn'] for r in g)) for voice,kind in {(r['voice_class'],r['kind']) for r in out} if (g:=[r for r in out if (r['voice_class'],r['kind'])==(voice,kind)])}
    write(root/'validation.json',dict(integrity=integrity,summary=summary));write(root/'per-cell.json',out)
    print(json.dumps(summary,indent=2))
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');a=p.parse_args();summarize(a.root)
