"""Offline device-send/host-arrival comparison; no absolute one-way latency."""
import json
from pathlib import Path

from uplink_continuity import link_tail


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def identity(row):
    return row['ssrc'], row['seq'], row['timestamp']


def joined_intervals(packets, trace, start, end):
    """Compare the SAME adjacent RTP pair at both boundaries; clocks may differ."""
    if len({row['pc_id'] for row in packets}) > 1:
        raise ValueError('Filter one peer session before joining device records')
    sends = {}
    send_streams = {}
    for row in trace:
        if row['kind'] == 12:
            sends.setdefault(identity(row), row)
            send_streams.setdefault(row['ssrc'], []).append(row)
    send_positions = {}
    for ssrc, stream in send_streams.items():
        stream.sort(key=lambda row: row['device_us'])
        for index, row in enumerate(stream):send_positions.setdefault(identity(row), index)
    streams = {}
    for row in packets:
        streams.setdefault((row['pc_id'], row['ssrc']), []).append(row)
    joined = []
    for stream in streams.values():
        stream = sorted(stream, key=lambda row: row['observed_ms'])
        origin = stream[0]['observed_ms']/1000
        for a, b in zip(stream, stream[1:]):
            left, right = a['observed_ms']/1000, b['observed_ms']/1000
            if left > end or right < start:
                continue
            sa, sb = sends.get(identity(a)), sends.get(identity(b))
            device_gap = sb['device_us']-sa['device_us'] if sa and sb else None
            host_gap = b['observed_ms']-a['observed_ms']
            source_max = None; intervening = None
            if sa and sb:
                ia, ib = send_positions[identity(a)], send_positions[identity(b)]
                source = send_streams[a['ssrc']][min(ia, ib):max(ia, ib)+1]
                source_max = max((y['device_us']-x['device_us'] for x, y in zip(source, source[1:])), default=0)
                intervening = max(0, abs(ib-ia)-1)
            joined.append(dict(start_s=left, end_s=right, host_gap_ms=host_gap,
                category='startup' if left < origin+5 else 'eligible',
                before_identity=list(identity(a)), after_identity=list(identity(b)),
                device_before_us=sa['device_us'] if sa else None,
                device_after_us=sb['device_us'] if sb else None,
                device_gap_us=device_gap,
                device_max_adjacent_gap_us=source_max,
                device_intervening_sends=intervening,
                spacing_change_ms=host_gap-device_gap/1000 if device_gap is not None else None,
                attribution=('unmatched' if device_gap is None else
                    'after ESP socket send-return (receive reordering)' if device_gap < 0 else
                    'before/at ESP socket send-return' if source_max > 60000 else
                    'after ESP socket send-return (intervening sends)' if intervening else
                    'after ESP socket send-return') if host_gap > 60 else None))
    return joined


def summarize_joined(joined):
    eligible = [row for row in joined if row['category'] == 'eligible']
    matched = [row for row in eligible if row['device_gap_us'] is not None]
    tails = [row for row in eligible if row['host_gap_ms'] > 60]
    complete = bool(eligible) and len(matched) == len(eligible)
    return dict(intervals=len(eligible), matched=len(matched), complete=complete,
        host_over60=len(tails), host_over150=sum(row['host_gap_ms'] > 150 for row in eligible),
        host_max_ms=max((row['host_gap_ms'] for row in eligible), default=None),
        host_tail_rate=len(tails)/len(eligible) if eligible else None,
        receive_reordered_pairs=sum(row['device_gap_us'] < 0 for row in matched),
        device_over60=sum(row['device_max_adjacent_gap_us'] > 60000 for row in matched) if complete else None,
        device_max_us=max((row['device_max_adjacent_gap_us'] for row in matched), default=None),
        device_tail_rate=sum(row['device_max_adjacent_gap_us'] > 60000 for row in matched)/len(matched) if complete else None,
        device_rate_basis='Matched receive intervals containing an adjacent source send gap >60 ms; source packet cadence separately retained',
        host_tails_attributed={label: sum(row['attribution'] == label for row in tails)
            for label in sorted({row['attribution'] for row in tails})})


def analyze_arm(path, result):
    pc = result.get('whole_before', {}).get('pc_id')
    if not pc or not result.get('windows'):
        return dict(available=False, reason='Arm failed before measurements')
    packets = [r for r in rows(path/'rtp/rtp.jsonl') if r.get('pc_id') == pc]
    socket = [r for r in rows(path/'rtp/socket-rtp.jsonl') if r.get('pc_id') == pc]
    events = rows(path/'bot/events.jsonl')
    commands = rows(path/'commands.jsonl')
    phases = []; pooled = []
    for window in result['windows']:
        if 'end_s' not in window:continue
        trace = rows(path/f'trace{window["index"]}.jsonl')
        start, end = window['start_s'], window['end_s']
        joined = joined_intervals(packets, trace, start, end)
        gate = link_tail(packets, start, end)
        summary = summarize_joined(joined)
        assert summary['intervals'] == gate['intervals']
        assert summary['host_over60'] == gate['over_threshold']
        slow = [r for r in trace if r['kind'] in (8, 11)]
        ordered = sorted([r for r in trace if r['kind'] == 12], key=lambda r: r['device_us'])
        source_gaps = [b['device_us']-a['device_us'] for a, b in zip(ordered, ordered[1:]) if a['ssrc'] == b['ssrc']]
        for interval in joined:
            if interval['host_gap_ms'] <= 60 or interval['device_before_us'] is None or interval['device_after_us'] is None:
                continue
            a, b = interval['device_before_us'], interval['device_after_us']
            interval['overlapping_owner_events'] = [r for r in slow if r['device_us'] >= a and r['device_us']-r['value'] <= b]
            interval['overlapping_host_loop_lags'] = [e for e in events if e['event'] == 'event-loop-lag' and
                e['t'] >= interval['start_s'] and e['t']-e['lag_ms']/1000 <= interval['end_s']]
        baseline = window['before']['peer_profile']['trace']['overwritten']
        trace_clean = (window.get('export_trace', {}).get('overwritten') == baseline == 0 and
                       window['after']['peer_profile']['trace']['overwritten'] == 0)
        mid_perf = [c for c in commands if c['command'] == 'stats' and c['start'] < end and c['end'] > start]
        phases.append(dict(index=window['index'], start_s=start, end_s=end, summary=summary,
            strict_link_gate=gate, socket_callback_gate=link_tail(socket, start, end),
            trace_clean=trace_clean, trace_records=len(trace),
            ordered_send_cadence=dict(intervals=len(source_gaps), over60=sum(x > 60000 for x in source_gaps),
                max_us=max(source_gaps, default=None),
                basis='All consecutive same-SSRC send-return records in the uninterrupted per-window trace, including its boundary margins'),
            mid_window_perf=mid_perf,
            complete_pairs=summary['complete'] and trace_clean and not mid_perf,
            tail_pairs=[r for r in joined if r['host_gap_ms'] > 60],
            owner_events=slow,
            host_event_loop=[e for e in events if e['event'] == 'event-loop-lag' and start <= e['t'] <= end]))
        pooled += joined
    summary = summarize_joined(pooled)
    all_complete = len(phases) == 3 and all(p['complete_pairs'] for p in phases)
    summary['all_three_trace_windows_complete'] = all_complete
    whole_start, whole_end = (phases[0]['start_s'], phases[-1]['end_s']) if phases else (None, None)
    return dict(available=bool(phases), qualification=False, phases=phases, summary=summary,
        whole_arm_including_interwindow_stats_exports=link_tail(packets, whole_start, whole_end) if phases else None,
        same_session=result.get('whole_before', {}).get('pc_id') == result.get('whole_after', {}).get('pc_id') and
                     result.get('whole_before', {}).get('generation') == result.get('whole_after', {}).get('generation'),
        transcripts=[e for e in events if e['event'] in ('final-transcript', 'turn-transcript', 'fixed-reply')],
        limitations='Device timestamps follow successful lwIP send return, not RF transmission; host socket callback follows kernel scheduling. Matched spacing differences locate a boundary, not AP/driver/RF versus host kernel. App enqueue/TX task timestamps unavailable on these frozen images. Enabled trace/call timing is identical but adds instrumentation cost.')
