"""v8.5 disjoint cell/export/piecewise epoch windows; no hardware access."""
import json
from pathlib import Path

POLICY=dict(rule='capture-export-v8.5',piecewise_received_floor=1500,
    cell_ratio_max=.02,export_transport_delivery='report-only',
    terminal_short='merge-backward-or-report-if-entire-piecewise-epoch-short',
    liveness_peer_close='hard-stop-anywhere',
    interpretation='coordinator interpretation of user decisions 3–5, 2026-10-18')


def span(before,after,kind,identity):
    if (before.get('pc_id'),before.get('generation'))!=(after.get('pc_id'),after.get('generation')):
        raise ValueError('Window crosses peer epoch')
    return dict(pc_id=before['pc_id'],generation=before['generation'],
        lo=before['device_us'],hi=after['device_us'],kind=kind,id=identity)


def scopes(root):
    """Use actual retained endpoints; never interpolate an absent capture PERF."""
    from echoear_aec_campaign_analysis import read
    spans=[];missing=[]
    for folder in sorted({p.parent for p in Path(root).rglob('capture-duration.json')}):
        before=read(folder/'before.json');end=read(folder/'capture-end.json');after=read(folder/'after.json')
        if before is None:continue # baseline/canonical probe, no transport cell
        if end is None:
            missing.append(str(folder.relative_to(root))+'/capture-end.json')
            if after and (before.get('pc_id'),before.get('generation'))==(after.get('pc_id'),after.get('generation')):
                spans.append(span(before,after,'unavailable',folder.name))
            continue
        old=read(folder/'old-final.json')
        if old:
            spans.append(span(before,old,'cell',folder.name+'-old'))
            # New-epoch absolute zero loss/concealment checks are performed by
            # reconnect's fresh epoch gate; the first available snapshot is end.
        else:spans.append(span(before,end,'cell',folder.name))
        if after:spans.append(span(end,after,'export',folder.name))
        else:missing.append(str(folder.relative_to(root))+'/after.json')
    return spans,missing


def audit(values, spans, probe=None):
    from echoear_aec_campaign_analysis import transport,delivery_counters
    from echoear_aec_transport_policy import RULE
    values=sorted({v['device_us']:v for v in values}.values(),key=lambda v:v['device_us'])
    if len(values)<2:return dict(passed=False,reason='Insufficient epoch endpoints',intervals=[],aggregates=[])
    own=[s for s in spans if (s['pc_id'],s['generation'])==(values[0]['pc_id'],values[0]['generation'])]
    cells=[];missing=[];index={v['device_us']:v for v in values}
    for s in own:
        if s['lo'] not in index or s['hi'] not in index:missing.append(s);continue
        if s['kind']=='cell':
            t=transport(index[s['lo']],index[s['hi']],RULE);d=delivery_counters(index[s['lo']],index[s['hi']])
            cells.append(dict(**s,transport=t,delivery=d,passed=t['passed'] and d['passed']))
    intervals=[];piece=[]
    for a,b in zip(values,values[1:]):
        covering=[s for s in own if s['lo']<=a['device_us'] and b['device_us']<=s['hi']]
        kinds={s['kind'] for s in covering}
        if len(kinds)>1:raise ValueError('Overlapping scored/export windows')
        kind=next(iter(kinds),'piecewise')
        if probe and probe['pc_id']==a['pc_id'] and probe['generation']==a['generation'] and probe['before_device_us']<=a['device_us']<b['device_us']<=probe['after_device_us']:kind='probe'
        t=transport(a,b,RULE);d=delivery_counters(a,b)
        hard=bool(t['histogram_valid'] and t['silence_mirror_valid'] and t['ge6_ended'] is False and all(v==0 for v in t['silence_delta'].values()))
        row=dict(before_device_us=a['device_us'],after_device_us=b['device_us'],scope=kind,transport=t,delivery=d,
                 hard_passed=hard,passed=True if kind=='export' else hard and d['passed'])
        if kind=='probe':row['passed'] &= probe['verdict']['passed']
        if kind=='unavailable':row['passed']=False
        intervals.append(row)
        if kind=='piecewise':piece.append(row)
    aggregates=[];pending=[];received=0
    for row in piece:
        pending.append(row);received+=row['transport']['delta']['received']
        if received>=1500:aggregates.append(pending);pending=[];received=0
    if pending:
        if aggregates:aggregates[-1]+=pending
        else:aggregates.append(pending)
    totals=[]
    for group in aggregates:
        delta={k:sum(r['transport']['delta'][k] for r in group) for k in ('received','lost','playout_dropped')}
        gated=delta['received']>=1500
        ratio=(delta['lost']+delta['playout_dropped'])/max(1,delta['received'])
        totals.append(dict(intervals=[[r['before_device_us'],r['after_device_us']] for r in group],delta=delta,
            ratio=ratio,ratio_gated=gated,passed=all(v>=0 for v in delta.values()) and (not gated or ratio<=.02)))
    return dict(passed=not missing and all(c['passed'] for c in cells) and all(r['passed'] for r in intervals) and all(r['passed'] for r in totals),
                intervals=intervals,aggregates=totals,cells=cells,missing_endpoints=missing,
                raw_first_to_last=transport(values[0],values[-1],RULE),
                raw_delivery=delivery_counters(values[0],values[-1]),export_report_only=True)
