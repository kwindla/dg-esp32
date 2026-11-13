"""Decision 138a: device-side uplink send cadence (S1) and the PERF-counter stand-in (S2).

S1 (item2 strict_voice_cat / quiet_offline / quiet_live). The firmware's per-packet trace (`peer-option arrival-trace
1`; kind 12 'uplink_wire' rows = one row per successful uplink RTP send, device clock) is enabled for every session of
those stages and drained over the console ONLY between scored windows: after the pre-prompt PERF and after the
reply-end PERF (e2e_common source-trace mode). The scored window of an original is prompt start -> reply end (the
same span the network-arrival link_tail used), mapped to the device clock from the bracketing PERF snapshots. The
gate is the 2026-10-15 diagnosis measure: adjacent kind-12 send intervals within the session's stream, console
read windows (`CONSOLE_WINDOW` stats/wifi-stats, +/-120 ms) excluded and reported (harness instrumentation),
acknowledged audio-idle windows excused by containment; every other interval is eligible and none may exceed SOURCE_GAP_LIMIT_MS. No link-backpressure
excusal (decision 139b, plan field excuse_firmware_backpressure=false): a send held back or skipped under Wi-Fi
back-pressure is a late send.
Completeness (fail closed, labelled harness_failure, never a pass): the trace is enabled at both PERF endpoints, the
same session (pc_id, generation) brackets the window, no record was overwritten between the pre-prompt PERF and the
post-reply drain, the stream has a send at or before the window start and at or after its end, and every RTP
sequence step inside the window is 1 (a missing record cannot hide a gap).
S1 adds the trace (one 32-byte ring record per traced event) and its console readout between windows to the
configuration being qualified (three drains per original: pre-prompt, post-reply, post-turn; their cost is recorded
per original in source_cadence.trace_cost); record_s2_window() keeps the per-window counters a trace-on vs trace-off
comparison needs.

S2 (item1 gate; item2 report-only). Session PERF counters: every uplink drop/error counter zero, frames encoded but
not yet sent at the snapshot <= encoded queue depth + IN_FLIGHT, and the session's worst encode->send wait
(enqueue_send_max_us) <= ENQUEUE_SEND_BOUND_US. These counters CANNOT see a stall before encoding (PCM frames
waiting in the framer / TX task); they show only that no encoded frame was dropped or held back after encoding.
"""
from collections import defaultdict

SOURCE_GAP_LIMIT_MS = 60
PRE_PROMPT_PENDING_MAX = 512   # mirrors e2e_common.PRE_PROMPT_PENDING_MAX
ENQUEUE_SEND_BOUND_US = 40000
IN_FLIGHT = 2
S2_ZERO = ('send_errors', 'send_budget_cutoffs', 'stale_audio_skipped', 'pcm_queue_drops')
S2_DROP_REASONS = ('encoded_queue', 'uplink_gap', 'send_cutoff')
DOWNLINK = ('lost', 'concealed', 'plc_frames', 'playout_dropped')
S2_LIMIT_TEXT = ('S2 counters cannot see a stall before encoding (PCM frames waiting in the framer or a stalled TX task); '
                 'they show only that no encoded frame was dropped, refused or held back more than the bound after encoding.')


def s2_counters(sample, *, bound_us=ENQUEUE_SEND_BOUND_US, pcm_queue_drops_max=0):
    """S2 on one PERF snapshot (cumulative since the session started).

    pcm_queue_drops_max is the declared plan field decision_138.item1_s2.pcm_queue_drops_max (decision 139c: 1 per
    item1 session); every other counter stays 0. item2's report-only S2 uses 0."""
    sample = sample or {}
    drops = sample.get('drop_reasons') or {}
    values = {k: sample.get(k) for k in ('sent', 'encode_frames', 'encoded_queue', 'enqueue_send_max_us', *S2_ZERO)}
    values.update({'drop_reasons.'+k: drops.get(k) for k in S2_DROP_REASONS})
    ints = all(type(v) is int and v >= 0 for v in values.values())
    in_flight = values['encode_frames']-values['sent']-values['encoded_queue'] if ints else None
    checks = dict(available=ints,
                  zero_counters=ints and all(values[k] == 0 for k in S2_ZERO if k != 'pcm_queue_drops') and
                  values['pcm_queue_drops'] <= pcm_queue_drops_max and all(values['drop_reasons.'+k] == 0 for k in S2_DROP_REASONS),
                  sent_matches_encoded=ints and 0 <= in_flight <= IN_FLIGHT,
                  enqueue_send_bound=ints and values['enqueue_send_max_us'] <= bound_us)
    return dict(passed=all(checks.values()), checks=checks, values=values, in_flight=in_flight, bound_us=bound_us,
                in_flight_limit=IN_FLIGHT, pcm_queue_drops_max=pcm_queue_drops_max, limit=S2_LIMIT_TEXT)


def record_s2_window(before, after):
    """Report-only per-original record (item2) for the trace-on vs trace-off comparison."""
    before, after = before or {}, after or {}
    same = before.get('pc_id') == after.get('pc_id') and before.get('generation') == after.get('generation')
    def delta(k):
        a, b = before.get(k), after.get(k)
        return b-a if same and type(a) is int and type(b) is int else None
    trace = (after.get('peer_profile') or {}).get('trace') or {}
    return dict(s2=s2_counters(after), s2_window_zero=all(delta(k) == 0 for k in S2_ZERO), same_session=same,
                trace_enabled=trace.get('enabled'),
                enqueue_send_max_us=after.get('enqueue_send_max_us'), enqueue_send_mean_us=after.get('enqueue_send_mean_us'),
                loop_max_us=after.get('loop_max_us'), encode_max_us=after.get('encode_max_us'),
                deltas={k: delta(k) for k in ('sent', 'encode_frames', *S2_ZERO, *DOWNLINK)},
                internal_minimum_free=((after.get('heap') or {}).get('internal') or {}).get('minimum_free'))


COMPARE_COUNTERS = ('send_errors', 'send_budget_cutoffs', 'stale_audio_skipped', 'pcm_queue_drops', *DOWNLINK)


def _comparable(row):
    d = row.get('deltas') or {}
    return (row.get('same_session') is True and isinstance(row.get('stage'), str) and
            all(type(d.get(k)) is int for k in COMPARE_COUNTERS) and type(row.get('enqueue_send_max_us')) is int and
            type(row.get('internal_minimum_free')) is int)


def trace_configuration_comparison(trace_on, trace_off):
    """Answerable by the r16 smokes: is a change of the qualified configuration DETECTED when S1's trace is on?

    trace_on / trace_off are record_s2_window() rows (one per scored original). Rows are compared per stage, only on
    stages present in both sets, and every row must be complete (same session, stage, per-window deltas of every
    counter, enqueue_send_max_us, internal heap minimum); otherwise the result is insufficient_evidence.
    A change is detected if, in any compared stage, ANY of:
      (a) for any single counter (send_errors, send_budget_cutoffs, stale_audio_skipped, pcm_queue_drops, lost,
          concealed, plc_frames, playout_dropped) the fraction of trace-on windows whose per-window delta is > 0, or
          the mean per-window delta, exceeds the trace-off value;
      (b) the trace-on maximum enqueue_send_max_us exceeds the trace-off maximum by more than 10 ms, or exceeds the
          S2 bound (40 ms);
      (c) the trace-on internal-heap minimum is more than 8 KiB below the trace-off minimum.
    A negative result is reported as 'not detected in these samples': old trace-off and new trace-on samples differ in
    RF conditions and session history, so the comparison cannot establish causality or absence of an effect."""
    on = [r for r in trace_on]; off = [r for r in trace_off]
    bad = [i for i, r in enumerate(on+off) if not _comparable(r)]
    stages = sorted({r['stage'] for r in on if _comparable(r)} & {r['stage'] for r in off if _comparable(r)})
    if bad or not on or not off or not stages:
        return dict(result='insufficient_evidence', changes_detected=None, incomplete_rows=len(bad), n_on=len(on), n_off=len(off),
                    compared_stages=stages, rule=trace_configuration_comparison.__doc__.split('\n\n')[1].strip())
    per_stage = {}
    for stage in stages:
        a = [r for r in on if r['stage'] == stage]; b = [r for r in off if r['stage'] == stage]
        frac = lambda rows, k: sum(r['deltas'][k] > 0 for r in rows)/len(rows)
        mean = lambda rows, k: sum(r['deltas'][k] for r in rows)/len(rows)
        counters = {k: dict(on=frac(a, k), off=frac(b, k), on_mean=mean(a, k), off_mean=mean(b, k),
                            detected=frac(a, k) > frac(b, k) or mean(a, k) > mean(b, k)) for k in COMPARE_COUNTERS}
        on_max, off_max = max(r['enqueue_send_max_us'] for r in a), max(r['enqueue_send_max_us'] for r in b)
        on_heap, off_heap = min(r['internal_minimum_free'] for r in a), min(r['internal_minimum_free'] for r in b)
        per_stage[stage] = dict(n_on=len(a), n_off=len(b), counters=counters,
                                enqueue_send_max_us=dict(on=on_max, off=off_max, detected=on_max > off_max+10000 or on_max > ENQUEUE_SEND_BOUND_US),
                                internal_minimum_free=dict(on=on_heap, off=off_heap, detected=on_heap < off_heap-8192))
        per_stage[stage]['detected'] = (any(c['detected'] for c in counters.values()) or per_stage[stage]['enqueue_send_max_us']['detected']
                                        or per_stage[stage]['internal_minimum_free']['detected'])
    detected = any(v['detected'] for v in per_stage.values())
    # r17 (Fable r16 N5, declared before data): a downlink-counter detection is "detected, not attributable" (no
    # same-night trace-off control: RF also moves it); only device-side signals (send errors, cutoffs, stale skips, PCM
    # drops, enqueue wait, heap) can raise the S1-with-trace vs S2-only question.
    device = any(c['detected'] for v in per_stage.values() for k, c in v['counters'].items() if k not in DOWNLINK) or \
        any(v['enqueue_send_max_us']['detected'] or v['internal_minimum_free']['detected'] for v in per_stage.values())
    downlink = any(c['detected'] for v in per_stage.values() for k, c in v['counters'].items() if k in DOWNLINK)
    return dict(result='change detected' if detected else 'not detected in these samples', changes_detected=detected,
                device_side_detected=device, raises_trace_question=device,
                downlink_detected='detected, not attributable (no same-night trace-off control)' if downlink else None,
                enqueue_note='the relative +10 ms criterion is inert where the baseline maximum already exceeds 30 ms '
                             '(quiet_offline: 60.3 ms); the absolute 40 ms bound carries it',
                compared_stages=stages, per_stage=per_stage, n_on=len(on), n_off=len(off),
                rule=trace_configuration_comparison.__doc__.split('\n\n')[1].strip())


# Declared before any r16 data (r16, Astra r15 Q2 / Fable N1): the trace-off baseline is every scored original of the
# requal-n1 normal image in these retained stage records (strict_voice_cat and quiet_offline; quiet_live has none).
TRACE_OFF_BASELINE = (
    'smoke/strict_voice_cat/attempt-1', 'smoke/quiet_offline/attempt-1',
    'smoke-r7/strict_voice_cat/attempt-1', 'smoke-r7/quiet_offline/attempt-1',
    'smoke-r9/strict_voice_cat/attempt-1', 'smoke-r9/quiet_offline/attempt-1',
    'smoke-r15/quiet_offline/attempt-2')


def rows_from_records(paths):
    """record_s2_window() rows for every scored original of the given stage result directories."""
    import json
    from pathlib import Path
    rows = []
    for path in paths:
        record = json.loads((Path(path)/'result.json').read_text())
        stage = Path(path).parent.name
        for run in record.get('runs') or []:
            if run.get('scored') is not True:continue
            row = run.get('s2') or record_s2_window(run.get('before_perf'), (run.get('reply_window_transport') or {}).get('after'))
            rows.append(dict(row, stage=stage, run=run.get('run'), source=str(path)))
    return rows


def _window(sample):
    """Device-clock scored window [prompt start, reply end] from the bracketing PERF snapshots."""
    before = sample.get('before_perf') or {}
    after = (sample.get('reply_window_transport') or {}).get('after') or {}
    if not all(isinstance(x.get('device_us'), int) for x in (before, after)):return None
    lead = sample.get('prompt_start', 0)-sample.get('before_perf_completed_at', 0)
    if not sample.get('prompt_start') or not sample.get('before_perf_completed_at') or lead < 0:return None
    # The pre-prompt PERF's device_us precedes its host completion, so the mapped start is at or before the prompt.
    return before, after, before['device_us']+int(lead*1e6), after['device_us']


def trace_cost(sample, rows, start, end):
    """r16 (Astra r15 Q1/Q2, Fable N2): what the trace actually cost this original: the record rate over the scored
    window (all kinds), the largest pending count seen at any snapshot, overwrite deltas, and every drain (three per
    original: pre-prompt, post-reply, post-turn) with its host duration and rows."""
    snaps = [((sample.get('before_perf') or {}).get('peer_profile') or {}).get('trace') or {},
             (((sample.get('reply_window_transport') or {}).get('after') or {}).get('peer_profile') or {}).get('trace') or {},
             ((sample.get('after_perf') or {}).get('peer_profile') or {}).get('trace') or {}]
    drains = list(sample.get('source_trace_drains') or [])
    snaps += [d.get('export') or {} for d in drains]
    inside = sum(1 for r in rows if start <= r.get('device_us', -1) <= end)
    ow = [x.get('overwritten') for x in snaps if type(x.get('overwritten')) is int]
    return dict(window_s=(end-start)/1e6, rows_in_window=inside, rows_per_s=inside/((end-start)/1e6) if end > start else None,
                max_pending=max((x.get('pending') for x in snaps if type(x.get('pending')) is int), default=None),
                capacity=next((x.get('capacity') for x in snaps if x.get('capacity')), None),
                overwritten_delta=(max(ow)-min(ow)) if ow else None,
                drains=[dict(phase=d.get('phase'), duration_s=d.get('duration_s'), rows=d.get('rows'),
                             pending_after=(d.get('export') or {}).get('pending'), overwritten=(d.get('export') or {}).get('overwritten'))
                        for d in drains], drain_count=len(drains))


def source_window(sample, rows, windows=(), *, limit_ms=SOURCE_GAP_LIMIT_MS, excuse_backpressure=False, snapshots=()):
    """S1 for one scored original. rows: decoded trace rows of this original; windows: console/idle windows (device_us).

    excuse_backpressure is the declared plan field decision_138.source_cadence.excuse_firmware_backpressure (decision
    139b: false; every > 60 ms send gap fails whatever the cause). If a plan ever set it true, the analyzer's declared
    device-evidenced link-backpressure windows (from PERF snapshots) would be excused; nothing else changes."""
    from step8e2b_analysis import device_send_cadence
    # Console reads (stats/wifi-stats PERF dumps, +/-120 ms) are harness instrumentation: excluded from the gate and
    # reported (bench-console-dump-send-gaps). Every other interval in the window is eligible.
    windows = [dict(w, category='console-read') if w.get('category') == 'console' else w for w in windows]
    problems = []
    w = _window(sample)
    if w is None:
        return dict(complete=False, passed=None, harness_failure=['scored window endpoints unavailable'], limit_ms=limit_ms)
    before, after, start, end = w
    export = sample.get('source_trace_export') or {}
    tb, ta = (before.get('peer_profile') or {}).get('trace') or {}, (after.get('peer_profile') or {}).get('trace') or {}
    if not (tb.get('enabled') is True and ta.get('enabled') is True):problems.append('trace not enabled at both endpoints')
    if before.get('pc_id') is None or before.get('pc_id') != after.get('pc_id') or before.get('generation') != after.get('generation'):
        problems.append('window not bracketed by one session')
    # r17 (smoke-r17 diagnosis): the overwrite baseline is the snapshot after the LAST pre-prompt drain (the ring is then
    # nearly empty), not the pre-prompt PERF: a full ring between originals overwrites only records older than that
    # drain. That snapshot must precede the window start; records older than it are outside the window (and the
    # sequence-contiguity check below still covers the span from the last send before the start).
    pre = [d for d in sample.get('source_trace_drains') or [] if d.get('phase') == 'pre-prompt']
    baseline = (pre[-1].get('export') or {}) if pre else tb
    ob, oa, oe = baseline.get('overwritten'), ta.get('overwritten'), export.get('overwritten')
    if not all(type(v) is int for v in (ob, oa, oe)) or not ob == oa == oe:
        problems.append('trace overwritten since the last pre-prompt drain or before its readout (%r, %r, %r)' % (ob, oa, oe))
    if pre:
        # r18 (Astra r17 / Fable r17 N2): the drain end must be PROVEN to precede the window, on both clocks.
        if type(baseline.get('device_us')) is not int:problems.append('last pre-prompt drain has no valid device_us')
        elif baseline['device_us'] > start:problems.append('last pre-prompt drain ended after the window start')
        host_end = pre[-1].get('host_end_s')
        if not isinstance(host_end, (int, float)) or not isinstance(sample.get('prompt_start'), (int, float)) or host_end > sample['prompt_start']:
            problems.append('last pre-prompt drain host_end_s not before the prompt start')
    # r18 (Fable r17 N3): flag, do not fail, a readout that did not converge (pending > 512 after the last dump); the
    # overwrite rule stays sound because any resulting overflow inside the window changes the counter and fails closed.
    converged = None if not pre else ((baseline.get('pending') if type(baseline.get('pending')) is int else 1 << 30) <= PRE_PROMPT_PENDING_MAX)
    gen = before.get('generation')
    stream = defaultdict(list)
    rows = list({tuple(sorted(r.items())): r for r in rows}.values())   # a record is exported once; dedupe anyway
    for r in rows:
        if r.get('kind') == 12 and r.get('reserved') == gen:stream[r['ssrc']].append(r)
    if len(stream) != 1:problems.append('expected one uplink stream in the session, found %d' % len(stream))
    sends = sorted(next(iter(stream.values())), key=lambda r: r['device_us']) if len(stream) == 1 else []
    if not (sends and sends[0]['device_us'] <= start and sends[-1]['device_us'] >= end):
        problems.append('trace does not cover the window ends (missing span)')
    inside = [r for r in sends if start <= r['device_us'] <= end]
    first = max((i for i, r in enumerate(sends) if r['device_us'] <= start), default=None)
    last = min((i for i, r in enumerate(sends) if r['device_us'] >= end), default=None)
    span = sends[first:last+1] if first is not None and last is not None else []
    # r19 (Astra r18 BLOCKING): classify every step independently (signed 16-bit). Only positive holes count as missing;
    # a nonforward step (0 = duplicate, < 0 = reorder) is always an incomplete trace and can never cancel a hole.
    steps = [((b['seq']-a['seq']+32768) % 65536)-32768 for a, b in zip(span, span[1:])]
    nonforward = sum(1 for d in steps if d <= 0)
    missing = sum(d-1 for d in steps if d > 1)
    if nonforward:problems.append('nonforward sequence step inside the window (%d duplicate/reordered send rows)' % nonforward)
    # r18 (Fable r17 N4): a device-side failed send advances the RTP sequence without an uplink_wire row. If the
    # window's send_errors increment accounts for every missing sequence number, it is a cadence FAIL (the device did
    # not send on time), not an incomplete trace; otherwise the trace is incomplete (fail closed). r19: only for PURE
    # skips (every step >= 1); any nonforward step keeps it incomplete. Scope: firmware send_errors also counts
    # data-channel and catch-all transport failures, so the label means "skips explained by the device's send_errors
    # counter", not "every skip was an audio sendto failure".
    errors = None
    ra = after.get('send_errors'); rb = before.get('send_errors')
    if type(ra) is int and type(rb) is int and before.get('generation') == after.get('generation'):errors = ra-rb
    device_send_failure = bool(missing) and not nonforward and errors is not None and errors >= missing
    if missing and not device_send_failure:
        problems.append('sequence step != 1 inside the window (missing trace record)')
    # Boundary intervals (last send before the start, first send after the end) are included: conservative.
    snaps = list(snapshots) if excuse_backpressure else ()
    cadence = device_send_cadence(span, span[0]['device_us'], span[-1]['device_us'], list(windows), snapshots=snaps) if span else \
        device_send_cadence([], start, end, list(windows), snapshots=snaps)
    complete = not problems
    assert limit_ms == 60, 'device_send_cadence counts eligible intervals over 60 ms'
    eligible_over = cadence['over60ms']
    result = dict(complete=complete, harness_failure=problems, limit_ms=limit_ms, window_us=[start, end],
                  sends=len(inside), max_gap_ms=cadence['gaps_ms']['maximum'], over_limit=eligible_over,
                  raw_over60ms=cadence['raw_over60ms'],
                  console_excluded=dict(count=sum('console-read' in r.get('categories', ()) for r in cadence['excluded']),
                                        max_gap_ms=max((r['raw_gap_ms'] for r in cadence['excluded'] if 'console-read' in r.get('categories', ())), default=None)),
                  idle_excused=cadence['idle']['count'], basis=cadence['basis'],
                  backpressure_excused=bool(excuse_backpressure), backpressure_intervals=cadence['link_backpressure']['count'],
                  trace_cost=trace_cost(sample, rows, start, end))
    result['pre_prompt_converged'] = converged
    result['device_send_failure'] = dict(missing_sequence=missing, send_errors_delta=errors,
                                         scope='send_errors counts audio, data-channel and catch-all send failures') if device_send_failure else None
    result['passed'] = (eligible_over == 0 and not device_send_failure) if complete else None
    return result


def main(argv=None):
    """r17 (Fable r16 N5b): reproducible readout. Writes inputs and result next to the smoke:
    python hil/echoear_source_cadence.py --on <stage attempt dir> [...] --out <json>"""
    import argparse, json
    from pathlib import Path
    p = argparse.ArgumentParser(description=main.__doc__)
    p.add_argument('--on', type=Path, action='append', required=True, help='trace-on stage attempt directory')
    p.add_argument('--evidence-root', type=Path, default=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-10/requal-n1'))
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args(argv)
    on = rows_from_records(a.on); off = rows_from_records([a.evidence_root/x for x in TRACE_OFF_BASELINE])
    result = trace_configuration_comparison(on, off)
    a.out.write_text(json.dumps(dict(result=result, trace_on=on, trace_off=off, baseline=list(TRACE_OFF_BASELINE)), indent=2)+'\n')
    print(json.dumps({k: result.get(k) for k in ('result', 'device_side_detected', 'downlink_detected', 'compared_stages')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
