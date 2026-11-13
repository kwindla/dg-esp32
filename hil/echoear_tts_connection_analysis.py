"""Server-only step-8d metrics; all declared sessions remain in the denominator."""
import json,re
from pathlib import Path
from echoear_tts_ab_analysis import distribution
from echoear_aec_bench import write,sha
from echoear_aec_accept import rows


def parse_metrics(log):
    ttfb=[float(x) for x in re.findall(r'(?:DeepgramFluxTTSService|ConnectionProbeTTS)#\d+ TTFB: ([\d.]+)s',log)]
    ttfa=[(float(a),float(b)) for a,b in re.findall(r'(?:DeepgramFluxTTSService|ConnectionProbeTTS)#\d+ TTFA: ([\d.]+)s \(([\d.]+)s leading silence\)',log)]
    if len(ttfb)!=len(ttfa):raise ValueError('TTFB/TTFA metric counts disagree')
    return [dict(ttfb_s=b,ttfa_s=a,lead_s=lead) for b,(a,lead) in zip(ttfb,ttfa)]


def summarize(sessions,plan,completed):
    arms={}
    for arm in ('A','B'):
        group=[s for s in sessions if s['arm']==arm]
        gens=[g for s in group for g in s['metrics']]
        arms[arm]=dict(sessions=len(group),complete_sessions=sum(s['complete'] for s in group),
            clean_sessions=sum(s.get('whole_session_clean',s['complete']) for s in group),
            long_sessions=sum(any(g['lead_s']>=plan['long_state_s'] for g in s['metrics']) for s in group),
            generations=len(gens),**{k:distribution([g.get(k) for g in gens]) for k in ('ttfb_s','ttfa_s','lead_s','reconnect_s','request_to_first_write_s','request_to_non_silent_write_s','ttfb_plus_reconnect_s','ttfa_plus_reconnect_s')})
    a,b=arms['A'],arms['B'];full=completed and all(x['complete_sessions']>=8 and x['generations']>=32 for x in (a,b)) and all(s['complete'] for s in sessions)
    comparable=bool(a['ttfb_s']['n'] and b['ttfb_s']['n'])
    unchanged=comparable and all(b['ttfb_s'][k]<=a['ttfb_s'][k]+plan['ttfb_tolerance_s'][k] for k in ('median','p95'))
    # Service metrics start after preparation; expose connection cost independently.
    inclusive=comparable and all(b['ttfb_plus_reconnect_s'][k]<=a['ttfb_plus_reconnect_s'][k]+plan['ttfb_tolerance_s'][k] for k in ('median','p95'))
    passed=bool(full and b['long_sessions']==0 and unchanged)
    return dict(arms=arms,complete_denominator=full,server_ttfb_unchanged=bool(unchanged),
        reconnect_inclusive_ttfb_unchanged=bool(inclusive),passed=passed,
        a_long_state_observed=a['long_sessions']>0,
        whole_session_clean=all(s.get('whole_session_clean',s['complete']) for s in sessions),
        default_changed=False,qualification=False,
        interpretation='Server-metric pass requires complete denominator, zero B long sessions and unchanged TTFB. Reconnect-inclusive cost is separately reported; this experiment does not automatically change the default.')


def classify_failures(events):
    failures=[e for e in events if e.get('frame') in ('InterruptionFrame','ErrorFrame','FatalErrorFrame') or e['event']=='evaluation-error']
    seals=[e['t'] for e in events if e['event']=='case-sealed']
    last=max(seals) if seals else float('inf')
    # Keep teardown failures in the report, but they cannot invalidate already
    # completed server TTFA/TTFB measurements. No acoustic/transport acceptance
    # is claimed here. An error before the last seal still fails the metric set.
    return failures,[e for e in failures if e['t']<=last],[e for e in failures if e['t']>last]


def reconnect_windows(events):
    """Use explicit metric starts, or conservative queue-to-stop windows in 8d."""
    windows=[];problems=[];pending=None;intervals=[]
    ids=list(dict.fromkeys(e.get('case_id') for e in events if e['event']=='script-request'))
    for cid in ids:
        own=[e for e in events if e.get('case_id')==cid]
        starts=[e['t'] for e in own if e['event']=='tts-generation-metric-start']
        if not starts:starts=[e['t'] for e in own if e['event']=='script-queued']
        stops=[e['t'] for e in own if e.get('frame')=='TTSStoppedFrame' or e['event']=='tts-generation-stopped']
        if len(starts)!=1 or not stops or min(stops)<starts[0]:
            problems.append('Missing or ambiguous generation window: '+str(cid))
        else:windows.append(dict(case_id=cid,start=starts[0],stop=max(stops)))
    for e in events:
        if e['event']=='tts-reconnect-start':
            if pending is not None:problems.append('Overlapping reconnect starts')
            pending=e['t']
        elif e['event']=='tts-reconnect-ready':
            if pending is None:problems.append('Reconnect ready without start')
            else:intervals.append(dict(start=pending,stop=e['t']));pending=None
    if pending is not None:problems.append('Unfinished reconnect')
    for interval in intervals:
        if interval['stop']<interval['start']:problems.append('Reversed reconnect interval')
        for window in windows:
            if interval['start']<window['stop'] and interval['stop']>window['start']:
                problems.append('Reconnect overlaps generation '+str(window['case_id']))
    return dict(passed=bool(ids) and not problems,windows=windows,reconnects=intervals,problems=problems)


def analyze(root,output=None):
    root=Path(root);plan=json.loads((root/'declaration.json').read_text());saved=json.loads((root/'result.json').read_text());sessions=[]
    for spec in plan['sessions']:
        folder=root/spec['id'];log=folder/'bot/server-1.log';metric_error=None
        try:metrics=parse_metrics(log.read_text()) if log.exists() else []
        except ValueError as exc:metrics=[];metric_error=str(exc)
        events=rows(folder/'evaluation/events.jsonl');recordings=[e for p in (folder/'sessions').glob('*/events.jsonl') for e in rows(p)]
        actual=next((s for s in saved['sessions'] if s['id']==spec['id']),{})
        metric_count_matches=len(metrics)==len(spec['texts'])==len(actual.get('generations',[]))
        failures,generation_failures,post_sequence_failures=classify_failures(events)
        for i,g in enumerate(metrics):
            cid=f'g{i+1}';own=[e for e in events if e.get('case_id')==cid]
            requests=[e for e in own if e['event']=='script-request'];reconnects=[e for e in own if e['event']=='tts-reconnect-ready']
            seals=[e for e in own if e['event']=='case-sealed']
            g['reconnect_s']=sum(e['elapsed_s'] for e in reconnects)
            g['reconnect_witness_valid']=len(reconnects)==(1 if spec['arm']=='B' else 0)
            g['ttfb_plus_reconnect_s']=g['ttfb_s']+g['reconnect_s']
            g['ttfa_plus_reconnect_s']=g['ttfa_s']+g['reconnect_s']
            g['request_to_first_write_s']=g['request_to_non_silent_write_s']=None
            if len(requests)==1 and len(seals)==1:
                writes=sorted((e for e in recordings if e['kind']=='tts-transport-write' and requests[0]['t']<=e['monotonic_ns']/1e9<=seals[0]['t']),key=lambda e:e['monotonic_ns'])
                if writes:g['request_to_first_write_s']=writes[0]['monotonic_ns']/1e9-requests[0]['t']
                audible=next((e for e in writes if e.get('non_silent')),None)
                if audible:g['request_to_non_silent_write_s']=audible['monotonic_ns']/1e9-requests[0]['t']
        connection_windows=reconnect_windows(events)
        complete=bool(connection_windows['passed'] and actual.get('completed') and metric_count_matches and not generation_failures and all(g['reconnect_witness_valid'] and g['request_to_first_write_s'] is not None for g in metrics))
        sessions.append(dict(id=spec['id'],arm=spec['arm'],complete=complete,metric_error=metric_error,connection_windows=connection_windows,metric_count_matches=metric_count_matches,metrics=metrics,failures=failures,generation_failures=generation_failures,post_sequence_failures=post_sequence_failures,whole_session_clean=complete and not failures,lead_sequence_s=[g['lead_s'] for g in metrics]))
    result=dict(summary=summarize(sessions,plan,saved['completed']),sessions=sessions,analyzer_sha256=sha(__file__))
    write(Path(output) if output else root/'analysis.json',result);print(json.dumps(result['summary'],indent=2));return result


if __name__=='__main__':
    import sys
    analyze(sys.argv[1])
