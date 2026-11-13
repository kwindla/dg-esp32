"""Step-8b retained TTS A/B analysis; never opens a board or service."""
import json,re
from pathlib import Path
import numpy as np
from echoear_aec_bench import load_capture,analyze,write,sha
from echoear_aec_metrics import frame_power,activity
from echoear_aec_timing import sample_interval
from echoear_aec_accept_analysis import gate_at_near,match,check_render_ledger


def distribution(values):
    x=np.array([v for v in values if v is not None],float)
    return dict(n=len(x),min=float(x.min()) if len(x) else None,
                median=float(np.median(x)) if len(x) else None,
                p95=float(np.percentile(x,95)) if len(x) else None,
                max=float(x.max()) if len(x) else None)


def summarize(cases,completed):
    arms={}
    for arm in ('A','B'):
        group=[c for c in cases if c['arm']==arm]
        clean=[c for c in group if c['clean']]
        warm=[c for c in clean if not c['cold']]
        arms[arm]=dict(acquired=len(group),clean=len(clean),cold=sum(c['cold'] for c in clean),
            false_turns=sum(c['false_turns'] for c in group),
            guarded=sum(c['guard_on_real_speech'] for c in clean),
            bracketed=sum(c['bracketed_playback'] for c in clean),
            raw_leading_s=distribution([c['raw_leading_s'] for c in clean]),
            output_leading_s=distribution([c['output_leading_s'] for c in clean]),
            written_leading_s=distribution([c['written_leading_s'] for c in clean]),
            request_to_non_silent_write_s=distribution([c['request_to_non_silent_write_s'] for c in clean]),
            warm_request_to_non_silent_write_s=distribution([c['request_to_non_silent_write_s'] for c in warm]),
            server_ttfa_s=distribution([c['server_ttfa_s'] for c in clean]),
            server_leading_s=distribution([c['server_leading_s'] for c in clean]))
    a,b=arms['A'],arms['B'];aw,bw=(x['warm_request_to_non_silent_write_s'] for x in (a,b))
    enough=all(x['clean']>=10 and x['guarded']==x['clean'] and x['bracketed']==x['clean'] and x['false_turns']==0 for x in (a,b))
    complete_metrics=all(x['written_leading_s']['n']==x['clean'] and x['server_ttfa_s']['n']==x['clean'] and x['request_to_non_silent_write_s']['n']==x['clean'] for x in (a,b))
    latency=bool(aw['n'] and bw['n'] and b['written_leading_s']['n'] and
        b['written_leading_s']['median']<=.08 and aw['median']-bw['median']>=.5 and bw['p95']<=aw['p95']+.1)
    select=bool(completed and enough and complete_metrics and latency and all(c['hardware_resources_passed'] for c in cases))
    return dict(arms=arms,selected='B' if select else None,retained_default='B' if select else 'A',select_trim=select,
                acquisition_completed=completed,enough_clean_guarded_replies=enough,
                complete_metrics=complete_metrics,latency_gate=latency,
                qualification=False,cold_limitation='Only the first A reply is cold; warm results do not qualify cold-arm equivalence.')


def speech_onset(ref):
    """First 20 ms MIC3 frame within 20 dB of peak OR above -55 dBFS.

    Reject a wholly quiet trace: a relative threshold alone has no speech witness.
    """
    power=frame_power(ref)
    absolute=32768**2 * 10**(-55/10)
    if not len(power) or power.max() <= absolute:
        return None
    active=np.flatnonzero((power >= power.max()/100) | (power > absolute))
    return int(active[0])*320 if len(active) else None


def streaming_findings(metrics,ttfb):
    complete=len(metrics)==len(ttfb) and bool(metrics)
    residuals=[a-b-lead for (a,lead),b in zip(metrics,ttfb)]
    real_time=complete and all(abs(x)<=.03 for x in residuals)
    return dict(ttfb_count_matches=complete,ttfa_minus_ttfb_minus_lead_s=residuals,
        silence_streams_at_real_time=real_time,
        long_state_generations=sum(lead>=.8 for _,lead in metrics),
        hold_entered_long_state=any(lead>=.8 for _,lead in metrics),
        conclusion=('Trimming cannot shorten first-speech arrival: TTFA equals TTFB plus lead '
                    'in every generation (within 30 ms); silence streams at real time. '
                    'The arm-B latency gate is structurally unreachable.') if real_time else 'Insufficient streaming evidence')


def main(root,output=None):
    root=Path(root);saved=json.loads((root/'result.json').read_text());serial=(root/'candidate-serial.log').read_text()
    events=[json.loads(line) for p in (root/'sessions').glob('*/events.jsonl') for line in p.read_text().splitlines()]
    identity=json.loads((root/'identity.json').read_text());qref=load_capture(root/'quiet.npz')[2]
    metrics=[(float(a),float(b)) for a,b in re.findall(r'DeepgramFluxTTSService#\d+ TTFA: ([\d.]+)s \(([\d.]+)s leading silence\)',(root/'bot/server-1.log').read_text())]
    ttfb=[float(x) for x in re.findall(r'DeepgramFluxTTSService#\d+ TTFB: ([\d.]+)s',(root/'bot/server-1.log').read_text())]
    rows=[]
    for index,c in enumerate(saved['cases']):
        folder=root/c['run_id'];raw,afe,ref,blocks=load_capture(folder/'capture.npz');anchors=json.loads((folder/'anchors.json').read_text())
        selected=[e for e in events if c['start_ns']<=e['monotonic_ns']<=c['end_ns']]
        writes=sorted((e for e in selected if e['kind']=='tts-transport-write'),key=lambda e:e['monotonic_ns'])
        first=next((i for i,e in enumerate(writes) if e['non_silent']),None)
        requests=[e for e in c['events'] if e.get('event')=='script-request' and e.get('case_id')==c['run_id']]
        trims=[e for e in selected if e['kind']=='tts-leading-trim']
        source=np.fromfile(root/'evaluation'/(c['run_id']+'.pcm'),dtype='<i2')
        source_active=np.flatnonzero(frame_power(source)>1024)
        leading=int(source_active[0])*320 if len(source_active) else None
        removed=sum(e['removed_samples'] for e in trims)
        physical=activity(ref,qref,[(0,len(ref)-1024)])
        active=np.flatnonzero(physical);low_onset=int(active[0])*320 if len(active) else None
        onset=speech_onset(ref)
        template=source[leading:leading+4000] if leading is not None else np.array([])
        prefix=match(ref,template)
        # Fixed source-clock lookup, no fitted ERLE/AFE delay. A matched source
        # prefix must lie near independent MIC3 activity, not elsewhere in reply.
        guarded=onset is not None and gate_at_near(serial,blocks,onset,uncertainty=0) is True
        prefix_ok=bool(prefix.get('valid') and low_onset is not None and abs(prefix['at']-low_onset)<=3200)
        live_events=[e for e in events if e['kind']=='session-start' or anchors[0]['lower_s']<=e['monotonic_ns']/1e9<=anchors[-1]['upper_s']]
        playback=analyze('live',folder/'capture.npz',root/'quiet.npz',identity,events=live_events,anchors=anchors)
        counts=playback['non_silent_tts_counts']
        accepted=[e for e in c['events'] if e.get('frame') in ('UserStartedSpeakingFrame','InterruptionFrame') or e.get('event')=='user-turn-stopped']
        # Count accepted false-turn cells, preserving individual observations.
        ledger=check_render_ledger([json.loads(p.read_text()) for p in folder.glob('after-boundary-*.json')])
        row=dict(id=c['run_id'],arm=c['arm'],cold=c['cold'],clean=c['noise_gate_passed'] and c['quiet_windows_valid'],
            raw_leading_s=leading/16000 if leading is not None else None,
            removed_s=removed/16000,output_leading_s=(leading-removed)/16000 if leading is not None else None,
            written_leading_s=sum(e['samples'] for e in writes[:first])/16000 if first is not None else None,
            request_to_non_silent_write_s=writes[first]['monotonic_ns']/1e9-requests[0]['t'] if first is not None and len(requests)==1 else None,
            server_ttfa_s=metrics[index][0] if index<len(metrics) else None,
            server_leading_s=metrics[index][1] if index<len(metrics) else None,
            ttfa_attribution='One TTFA per sequential declared script; requires total metric count equal acquired reply count',
            guard_on_real_speech=guarded,source_prefix_near_low_level_onset=prefix_ok,gate_closed_at_mic3=guarded,source_prefix=prefix,
            diagnostic_2lsb_onset_sample=low_onset,
            diagnostic_2lsb_guard=low_onset is not None and gate_at_near(serial,blocks,low_onset,uncertainty=0) is True,
            server_ttfb_s=ttfb[index] if index<len(ttfb) else None,
            mic3_onset_sample=onset,mic3_onset_host_interval=sample_interval(onset,blocks,anchors) if onset is not None else None,
            bracketed_playback=counts['playback']==1 and sum(counts.values())==1,
            playback=playback,false_turns=int(bool(accepted)),false_turn_observations=accepted,
            hardware_resources_passed=c['resources']['all_passed'] and c['admission']['passed'] and ledger['passed'],ledger=ledger,
            source_hashes={name:sha(folder/name) for name in ('capture.npz','anchors.json','result.json')})
        rows.append(row)
    summary=summarize(rows,saved['completed'])
    summary.update(streaming_findings(metrics,ttfb))
    summary.update(selected=None,retained_default='A',select_trim=False,closed_no_change=True,
        guard_rule='20 ms MIC3 within 20 dB of reply peak OR above -55 dBFS; wholly quiet traces rejected')
    summary['ttfa_count_matches']=len(metrics)==len(rows)
    if not summary['ttfa_count_matches']:summary.update(selected=None,retained_default='A',select_trim=False)
    plan=json.loads((root/'declaration.json').read_text())
    summary['declared_replies']=len(plan['cases'])
    summary['acquired_replies']=len(rows)
    summary['unacquired']=[c['id'] for c in plan['cases'] if not any(r['id']==c['id']+'-a1' for r in rows)]
    summary['contaminated']=[c['id'] for c in rows if not c['clean']]
    result=dict(summary=summary,cases=rows,analyzer_sha256=sha(Path(__file__)))
    write(Path(output) if output else root/'tts-ab-analysis.json',result);print(json.dumps(summary,indent=2));return result


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('--output',type=Path);args=p.parse_args();main(args.root,args.output)
