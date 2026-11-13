"""Diagnostic-only arrival and phase analysis; never grants acceptance credit.

The unchanged candidate records Render *completion*, not RenderTick.deadline_us.
Late-after-completion is conclusive. Before-completion alone is NOT on time.
All missing/ambiguous identities and trace gaps remain explicit unknowns.
"""
from collections import defaultdict
import argparse
import json
from pathlib import Path
import re

from echoear_aec_timing import map_interval


def quantiles(values):
    v=sorted(values)
    return dict(n=len(v), **{k:v[int((len(v)-1)*p)] if v else None
                            for k,p in [('p05',.05),('p50',.5),('p95',.95),('p99',.99),('max',1)]})


def phase_histograms(before, capture_end, after):
    """Runs are assigned where they END; retain open-run count at every boundary."""
    from echoear_aec_campaign_analysis import transport
    values=[]
    for name,a,b in [('capture',before,capture_end),('export',capture_end,after)]:
        same=a.get('pc_id')==b.get('pc_id') and a.get('generation')==b.get('generation')
        delta=transport(a,b) if same else None
        available=bool(same and delta and 'missing_run_ended_delta' in delta)
        values.append(dict(phase=name,available=available,transport=delta,
                           device_window_us=[a['device_us'],b['device_us']]))
    return dict(phases=values,qualification=False,
                boundary_note='Before-to-capture-end includes setup/pre-roll; capture-end-to-after includes export and boundary commands. Histogram counts runs ending in a phase, not runs wholly contained in it. A run crossing capture_end is not split into separate runs.')


def arrival_diagnostics(records, sends, *, pc_id, generation, lo_us, hi_us,
                        coverage_complete, expected_missing=None):
    tag=generation%65536
    rows=sorted((r for r in records if r['reserved']==tag),key=lambda r:r['device_us'])
    stages={k:defaultdict(list) for k in (1,2,13)}
    for r in rows:
        if r['kind'] in stages:
            stages[r['kind']][(r['ssrc'],r['timestamp'])].append(r)
    renders=[r for r in rows if r['kind']==14]
    own=[r for r in renders if lo_us<r['device_us']<=hi_us]
    missing=[r for r in own if r['flags'] in (1,2)]
    ticks_complete=bool(own) and own[0]['device_us']-lo_us<100000 and hi_us-own[-1]['device_us']<100000 and all((b['value']-a['value'])%2**32==1
                                   for a,b in zip(own,own[1:]))
    complete=bool(coverage_complete and ticks_complete and expected_missing is not None
                  and len(missing)==expected_missing)
    first={}
    for s in sorted((s for s in sends if s['pc_id']==pc_id),key=lambda s:s['send_ms']):
        first.setdefault((s['ssrc'],s['seq'],s['timestamp']),s)
    events=[]
    for r in missing:
        key=(r['ssrc'],r['timestamp'])
        candidates={k:stages[k][key] for k in stages}
        identities={p['seq'] for v in candidates.values() for p in v}
        ambiguous=len(identities)>1
        times={k:min((p['device_us'] for p in candidates[k]),default=None) for k in stages}
        # Plc is stamped after codec.render too. Retain it as a consistency
        # witness, never promote it to an earlier deadline measurement.
        prior=max((p['device_us'] for p in renders if p['device_us']<r['device_us']),default=lo_us)
        plc=[p['device_us'] for p in rows if r['flags']==1 and p['kind']==4
             and prior<p['device_us']<=r['device_us']]
        wire,auth,ingress=(times[k] for k in (1,2,13))
        if not complete:label='unknown: incomplete trace/counter coverage'
        elif ambiguous:label='unknown: ambiguous RTP identity'
        elif ingress is None:label='unknown: packet absent at ingress in retained trace'
        elif ingress<=r['device_us']:
            label='unknown: ingress before completion; exact decision deadline not recorded'
        elif wire is not None and wire>r['device_us']:
            label='upstream of socket read: arrival after render completion'
        elif auth is not None and auth>r['device_us']:
            label='upstream of authenticated boundary; socket/receive processing unresolved'
        else:label='upstream of authoritative ingress; authentication/cache/admission timing unresolved'
        events.append(dict(device_us=r['device_us'],ssrc=r['ssrc'],timestamp=r['timestamp'],
                           render_tick=r['value'],stream_generation=r['aux'],render_kind=r['flags'],
                           wire_us=wire,authenticated_us=auth,ingress_us=ingress,
                           plc_completion_us=plc,plc_association='Temporal only: between adjacent Render completions',
                           attribution=label,exact_deadline_us=None))
    # Offset-free relative delay, independently per source and receiver boundary.
    # Never call the raw difference a one-way latency. A long hold can drift.
    distributions=[]
    for kind,index in stages.items():
        by_source=defaultdict(list)
        for packets in index.values():
            seen=set()
            for r in packets:
                key=(r['ssrc'],r['seq'],r['timestamp'])
                if key in seen:continue
                seen.add(key)
                if key in first and lo_us<r['device_us']<=hi_us:
                    by_source[r['ssrc']].append(dict(device_us=r['device_us'],seq=r['seq'],timestamp=r['timestamp'],
                                                   offset_ms=r['device_us']/1000-first[key]['send_ms']))
        for source,matched in by_source.items():
            baseline=quantiles([r['offset_ms'] for r in matched])['p05']
            for r in matched:r['relative_delay_ms']=r.pop('offset_ms')-baseline
            distributions.append(dict(kind=kind,ssrc=source,valid=complete,baseline_offset_ms=baseline,
                                      relative_delay_ms=quantiles([r['relative_delay_ms'] for r in matched]),samples=matched))
    return dict(qualification=False,coverage_complete=complete,render_missing=len(missing),expected_missing=expected_missing,
                late_slots=events,arrival_distributions=distributions,
                limits='Render and Plc are post-decode stamps. Before-completion is indeterminate, not an on-time verdict. Wire follows WiFi/lwIP buffering; air/AP/driver cannot be separated by it. Relative delays subtract the per-source/per-phase 5th percentile; not absolute one-way latency and no drift correction.')


PING=re.compile(r'^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time([=<])(\d+(?:\.\d+)?) ms')


def parse_ping(text):
    """iputils -D reports wall time; retain bounds for '<1 ms', not fake 1 ms."""
    result=[]
    for line in text.splitlines():
        m=PING.search(line)
        if m:
            stamp,seq,op,rtt=m.groups()
            result.append(dict(wall_s=float(stamp),seq=int(seq),rtt_ms=float(rtt),rtt_exact=op=='='))
    return result


def wall_to_monotonic(wall_s, anchors, *, max_offset_change_s=.01):
    """Require wall bracketing and stable sampled offsets; clock jumps invalidate."""
    anchors=sorted(anchors,key=lambda a:a['wall_s'])
    before=[a for a in anchors if a['wall_s']<=wall_s]
    after=[a for a in anchors if a['wall_s']>=wall_s]
    if not before or not after:return None
    a,b=before[-1],after[0]
    if any(x['mono_lower_s']>x['mono_upper_s'] for x in (a,b)):return None
    lower=min(x['mono_lower_s']-x['wall_s'] for x in (a,b))
    upper=max(x['mono_upper_s']-x['wall_s'] for x in (a,b))
    if upper-lower>max_offset_change_s:return None
    return [wall_s+lower,wall_s+upper]


def correlate(events, anchors, ap, board, rtts, *, radius_s=1, threshold_ms=100):
    """Report observations around bounded host times, never a causal classifier.

    Ping samples must carry a bracketed monotonic interval; RTT events use
    monotonic receive time. Sparse or absent ping coverage is not exoneration.
    """
    result=[]
    for event in events:
        interval=map_interval(event['device_us'],anchors)
        windows={}
        for name,rows in [('gateway',ap),('board',board),('data_channel',rtts)]:
            nearby=[r for r in rows if interval and r.get('host_interval_s')
                    and r['host_interval_s'][1]>=interval[0]-radius_s
                    and r['host_interval_s'][0]<=interval[1]+radius_s]
            windows[name]=dict(samples=len(nearby),excursions=sum(r['rtt_ms']>=threshold_ms for r in nearby),
                               max_rtt_ms=max((r['rtt_ms'] for r in nearby),default=None))
        result.append(dict(event=event,host_interval_s=interval,correlation=windows))
    return dict(events=result,radius_s=radius_s,excursion_threshold_ms=threshold_ms,
                interpretation='AP includes AP CPU and host hop; board adds its hop and lwIP; data channel adds peer processing. Co-occurrence suggests a boundary, does not prove a cause. No replies is unavailable, not zero delay.')


def analyze_cell(value):
    """Portable input fixture, with raw arrays and explicit coverage witnesses.

    This entrypoint does not read hardware or infer trace completeness from a
    filename. The acquisition adapter must supply overflow/gap coverage truth.
    """
    before,end,after=(value[k] for k in ('before','capture_end','after'))
    phases=phase_histograms(before,end,after)
    diagnostics=[]
    for phase,a,b in [('capture',before,end),('export',end,after)]:
        trace=value['coverage'][phase]
        coverage=bool(trace.get('complete') is True and trace.get('overwritten_delta')==0
                      and trace.get('max_drain_gap_s',float('inf'))<=25
                      and a.get('pc_id')==b.get('pc_id') and a.get('generation')==b.get('generation'))
        expected=b['lost']-a['lost'] if b['lost']>=a['lost'] else None
        audit=arrival_diagnostics(value['records'],value['sends'],pc_id=a['pc_id'],generation=a['generation'],
                                  lo_us=a['device_us'],hi_us=b['device_us'],coverage_complete=coverage,
                                  expected_missing=expected)
        audit['phase']=phase
        pings={}
        for name in ('gateway','board'):
            pings[name]=[dict(r,host_interval_s=wall_to_monotonic(r['wall_s'],value['ping_clock_anchors']))
                         for r in parse_ping(value['ping_raw'][name])]
        audit['correlations']=correlate(audit['late_slots'],value['device_anchors'],pings['gateway'],pings['board'],value['data_channel_rtt'])
        diagnostics.append(audit)
    return dict(qualification=False,phase_histograms=phases,diagnostics=diagnostics)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path,help='Portable raw cell fixture JSON; see analyze_cell')
    parser.add_argument('output',type=Path,help='Fresh output, never overwrite retained evidence')
    args=parser.parse_args(argv)
    value=analyze_cell(json.loads(args.input.read_text()))
    with args.output.open('x') as stream:json.dump(value,stream,indent=2,allow_nan=False);stream.write('\n')


if __name__=='__main__':main()
