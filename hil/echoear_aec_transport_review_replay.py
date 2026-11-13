"""Offline review cross-check; original captures and verdicts are never rewritten."""
import json
from pathlib import Path
from echoear_aec_accept import rows
from echoear_aec_bench import sha,write
from echoear_aec_campaign_analysis import transport,delivery_counters
from echoear_aec_transport_environment import classify,uplink_loss


def replay(root, capture, output):
    root=Path(root);folder=root/capture;output=Path(output)
    read=lambda p:json.loads(p.read_text())
    before,after=read(folder/'before.json'),read(folder/'after.json')
    anchors=read(folder/'anchors.json');anchor=anchors[0]
    origin=(anchor['lower_s']+anchor['upper_s'])/2-anchor['coordinate']/1e6
    window=[v['device_us']/1e6+origin for v in (before,after)]
    saved=read(folder/'result.json') if (folder/'result.json').exists() else {}
    cases=saved.get('cases') or [c for c in read(root/'result.json').get('cases',[]) if c.get('capture_dir',c['run_id'])==capture]
    checks=saved.get('checks')
    saved_case_checks=bool(cases and all(k in cases[0] for k in ('resources','admission')))
    if checks is None:
        # Use retained per-case checks for c1 cell7. With no retained case (cell8),
        # true is only an upper-bound assumption; independent hard faults refuse it.
        checks=dict(resources=cases[0]['resources'] if saved_case_checks else {'all_passed':True},
                    admission=cases[0]['admission'] if saved_case_checks else {'passed':True},
                    rf=read(folder/'rf.json'),delivery=delivery_counters(before,after),transport=transport(before,after))
    if not cases:
        # c1 hard-stop capture has no completed result; retain actual request to
        # seal as the scored-cell interval when available, never fabricate credit.
        events=rows(root/'evaluation/events.jsonl');own=[e for e in events if e.get('case_id')==capture]
        starts=[e['t'] for e in own if e['event']=='script-request'];ends=[e['t'] for e in own if e['event']=='case-sealed']
        if starts and ends:cases=[dict(run_id=capture,start_ns=int(min(starts)*1e9),end_ns=int(max(ends)*1e9))]
    timing=(read(folder/'attempt-window.json') if (folder/'attempt-window.json').exists() else
        dict(window_s=window,capture_end_s=anchors[-1]['upper_s'],after_boundary_s=window[1],
             basis='retained replay: capture-end final AEC clock upper bound; after-boundary mapped PERF device_us'))
    # The live classifier used the host attempt bounds, when retained. Mixing
    # mapped PERF bounds with host export bounds silently loses export timing.
    window=timing['window_s']
    socket=rows(root/'rtp/socket-rtp.jsonl')
    witness=classify(before,after,checks,read(folder/'rf-pre-gate.json'),rows(root/'evaluation/events.jsonl'),
        rows(root/'rtp/downlink.jsonl'),rows(root/'rtp/sctp.jsonl'),socket,window,cases,timing,
        device_alive_rule=read(root/'declaration.json').get('failure_policy',{}).get('transport_environmental',{}).get('device_alive_rule'),
        recorded=[e for path in (root/'sessions').glob('*/events.jsonl') for e in rows(path) if e.get('monotonic_ns',0)/1e9<=window[1]],
        arrivals=rows(folder/'arrivals.jsonl'))
    own=[r for r in socket if r['pc_id']==before['pc_id']]
    whole=uplink_loss(own,before['pc_id'],min(r['observed_ms'] for r in own)/1000,max(r['observed_ms'] for r in own)/1000)
    # Diagnostic reproduction of the erroneous loss method: forward gaps ignore
    # later reordered arrivals. Do not use it for the independent loss witness.
    naive=sum(max(0,b['seq']-a['seq']-1) for a,b in zip(own,own[1:]))
    value=dict(root=str(root),capture=capture,window_mapping=('saved host attempt-window.json' if (folder/'attempt-window.json').exists() else 'first AEC clock bracket midpoint + device_us; retained replay only'),
        historical_missing_checks_assumed_true=not bool(saved.get('checks')) and not saved_case_checks,
        saved_case_resource_admission_checks=saved_case_checks,attempt_timing=timing,
        classification=witness,whole_uplink=whole,naive_forward_gap_sum=naive,
        input_sha256={str(f.relative_to(root)):sha(f) for f in [folder/'before.json',folder/'after.json',folder/'anchors.json',folder/'rf.json',folder/'rf-pre-gate.json',root/'evaluation/events.jsonl',root/'rtp/downlink.jsonl',root/'rtp/sctp.jsonl',root/'rtp/socket-rtp.jsonl',root/'result.json']+
            [f for f in (folder/'result.json',folder/'attempt-window.json') if f.exists()]})
    if output.exists():raise FileExistsError(output)
    write(output,value)
    return value


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('capture');p.add_argument('output',type=Path)
    a=p.parse_args();v=replay(a.root,a.capture,a.output)
    print(json.dumps(dict(passed=v['classification']['passed'],conditions=v['classification']['conditions'],
        whole_unique_missing=v['whole_uplink']['missing'],whole_expected=v['whole_uplink']['expected'],
        naive_forward_gap_sum=v['naive_forward_gap_sum']),indent=2))
