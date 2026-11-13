"""Step-8e server metrics with explicit context and lifecycle attribution."""
import json,re
from pathlib import Path
from echoear_aec_accept import rows
from echoear_aec_bench import sha,write
from echoear_tts_ab_analysis import distribution
from echoear_tts_connection_analysis import parse_metrics


def connection_windows(events):
    pending=None;intervals=[];windows=[];problems=[]
    for e in events:
        if e['event']=='tts-reconnect-start':
            if pending is not None:problems.append('overlapping reconnects')
            pending=e['t']
        elif e['event'] in ('tts-reconnect-ready','tts-reconnect-failed','tts-reconnect-aborted'):
            if pending is None:problems.append('reconnect endpoint without start')
            else:intervals.append([pending,e['t']]);pending=None
    if pending is not None:problems.append('unfinished reconnect')
    for start in [e for e in events if e['event']=='tts-generation-metric-start']:
        stops=[e for e in events if e['event'] in ('tts-generation-stopped','tts-interruption-stopped') and e.get('context_id')==start.get('context_id') and e['t']>=start['t']]
        if len(stops)!=1:problems.append('missing/duplicate context stop');continue
        windows.append(dict(context_id=start['context_id'],start=start['t'],stop=stops[0]['t']))
    for a,b in intervals:
        for w in windows:
            if a<w['stop'] and b>w['start']:problems.append('reconnect inside metric window')
    return dict(passed=bool(windows) and not problems,windows=windows,reconnects=intervals,problems=problems)


def analyze(root):
    root=Path(root);plan=json.loads((root/'declaration.json').read_text());result=json.loads((root/'result.json').read_text());sessions=[]
    for spec in plan['sessions']:
        folder=root/spec['id'];events=rows(folder/'evaluation/events.jsonl')
        recorded=[e for p in (folder/'sessions').glob('*/events.jsonl') for e in rows(p)]
        log=folder/'bot/server-1.log';error=None
        try:metrics=parse_metrics(log.read_text().replace('ProactiveConnectionTTS','ConnectionProbeTTS')) if log.exists() else []
        except ValueError as exc:metrics=[];error=str(exc)
        actual=next((s for s in result['sessions'] if s['id']==spec['id']),{})
        windows=connection_windows(events)
        seals=[e['t'] for e in events if e['event']=='case-sealed'];last=max(seals) if seals else float('inf')
        failures=[e for e in events if e.get('frame') in ('ErrorFrame','FatalErrorFrame') or e['event'] in ('evaluation-error','tts-reconnect-refused','tts-reconnect-failed','tts-interruption-stop-timeout')]
        generation_errors=[e for e in failures if e['t']<=last]
        teardown=[e for e in failures if e['t']>last]
        for i,m in enumerate(metrics):
            cid=f'g{i+1}';own=[e for e in events if e.get('case_id')==cid]
            requests=[e for e in own if e['event']=='script-request']
            starts=[e for e in own if e['event']=='tts-generation-metric-start']
            m.update(id=cid,context_id=starts[0].get('context_id') if len(starts)==1 else None,request_to_first_write_s=None,request_to_non_silent_write_s=None)
            if len(requests)==1 and m['context_id']:
                writes=sorted([e for e in recorded if e['kind']=='tts-transport-write' and e.get('context_id')==m['context_id']],key=lambda e:e['monotonic_ns'])
                if writes:m['request_to_first_write_s']=writes[0]['monotonic_ns']/1e9-requests[0]['t']
                active=next((e for e in writes if e.get('non_silent')),None)
                if active:m['request_to_non_silent_write_s']=active['monotonic_ns']/1e9-requests[0]['t']
        fast=[]
        for cid in plan['fast_followup_ids']:
            prior=f'g{int(cid[1:])-1}'
            request=[e for e in events if e['event']=='script-request' and e.get('case_id')==cid]
            stop=[e for e in events if e['event'] in ('tts-generation-stopped','tts-interruption-stopped') and e.get('case_id')==prior]
            lag=request[0]['t']-stop[0]['t'] if len(request)==len(stop)==1 else None
            fast.append(dict(id=cid,delay_s=lag,passed=lag is not None and 0<=lag<=.2))
        request_times=[e['t'] for e in events if e['event']=='script-request']
        unassigned=[e for e in recorded if e['kind']=='tts-transport-write' and not e.get('context_id') and request_times and min(request_times)<=e['monotonic_ns']/1e9<=last]
        scripted=[e for e in events if e['event']=='script-interrupt']
        interruptions=[e for e in events if e.get('frame')=='InterruptionFrame']
        stops=[e for e in events if e['event']=='tts-interruption-stopped']
        interruption_ok=len(scripted)==len(interruptions)==len(stops)==1 and scripted[0].get('case_id')==plan['interrupt_id'] and stops[0]['t']>=scripted[0]['t']
        reconnects=[e for e in events if e['event']=='tts-reconnect-ready']
        count_ok=len(metrics)==len(spec['texts'])==len(actual.get('generations',[]))
        complete=bool(actual.get('completed') and count_ok and not generation_errors and not unassigned and windows['passed'] and all(f['passed'] for f in fast) and interruption_ok and all(m['request_to_first_write_s'] is not None and m['request_to_first_write_s']>=0 for m in metrics))
        if spec['arm']=='A' and reconnects:complete=False
        if spec['arm']=='B' and len(reconnects)!=len(spec['texts']):complete=False
        sessions.append(dict(id=spec['id'],arm=spec['arm'],complete=complete,unassigned_writes=unassigned,metric_error=error,metrics=metrics,connection_windows=windows,fast_requests=fast,scripted_interruptions=scripted,observed_interruptions=interruptions,interruption_stops=stops,interruption_passed=interruption_ok,generation_errors=generation_errors,post_seal_errors=teardown,reconnects=reconnects,inflight_waits=[e for e in events if e['event']=='tts-reconnect-wait-start']))
    arms={}
    for arm in 'AB':
        group=[s for s in sessions if s['arm']==arm];generations=[g for s in group for g in s['metrics']]
        arms[arm]=dict(sessions=len(group),complete_sessions=sum(s['complete'] for s in group),generations=len(generations),long_sessions=sum(any(g['lead_s']>=.8 for g in s['metrics']) for s in group),long_generations=sum(g['lead_s']>=.8 for g in generations),inflight_waits=sum(len(s['inflight_waits']) for s in group),post_seal_errors=sum(len(s['post_seal_errors']) for s in group),**{k:distribution([g.get(k) for g in generations]) for k in ('ttfb_s','ttfa_s','lead_s','request_to_first_write_s','request_to_non_silent_write_s')},reconnect_s=distribution([e['elapsed_s'] for s in group for e in s['reconnects']]))
    a,b=arms['A'],arms['B'];full=result['completed'] and all(x['complete_sessions']>=8 and x['generations']>=32 for x in (a,b))
    latency=bool(a['request_to_first_write_s']['n'] and b['request_to_first_write_s']['n'] and b['request_to_first_write_s']['median']<=a['request_to_first_write_s']['median']+plan['first_write_median_tolerance_s'])
    summary=dict(arms=arms,complete_denominator=full,first_write_latency_passed=latency,inflight_serialization_exercised=b['inflight_waits']>0,passed=bool(full and latency and b['long_sessions']==0 and b['inflight_waits']>0),default_changed=False,qualification=False)
    output=dict(summary=summary,sessions=sessions,analyzer_sha256=sha(__file__));write(root/'analysis.json',output);print(json.dumps(summary,indent=2));return output


if __name__=='__main__':
    import sys
    analyze(sys.argv[1])
