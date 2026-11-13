"""Diagnostic-only rejudge of a retained AEC reconnect part (smoke-01 part 2) under the current liveness rule.

Host-only; reads retained evidence and writes ONE new file beside the run folder. It never writes into the
run folder, never overwrites, and never rescores: the run's outcome of record is copied into the output
unchanged. smoke-01 predates the runner's wall-clock stamps, so the epochs' monotonic connect/disconnect
times are mapped to loguru's wall clock through anchor pairs: recorder rows and the bot.log lines pipecat
writes for the same aggregator events in the same coroutine. The mapping's spread (plus loguru's 1 ms
truncation) widens the clock slack.
"""
import argparse
import glob
import hashlib
import json
import re
import statistics
from pathlib import Path

import echoear_mute_gate as g

ROOT = Path(__file__).resolve().parents[1]
RECONNECT_PLAN = ROOT/'hil/plans/echoear-aec-reconnect-v1.json'
ANCHORS = {'aggregator-user-turn-started': 'User started speaking',
           'aggregator-user-turn-stopped': 'User stopped speaking',
           'aggregator-assistant-turn-stopped': 'Transcript: assistant'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clock_map(rows, bot_log):
    """wall - monotonic offsets from paired anchors; pairs by kind in order, counts must match."""
    lines = [m for m in (g.LOGURU_LINE.match(x) for x in bot_log.splitlines()) if m]
    offsets, pairs = [], {}
    for kind, text in ANCHORS.items():
        mono = sorted(r['monotonic_ns']/1e9 for r in rows if r.get('kind') == kind)
        wall = [g.loguru_wall(m[1]) for m in lines if text in m[4]]
        if len(mono) != len(wall) or not mono:
            raise ValueError(f'anchor {kind}: {len(mono)} recorder rows vs {len(wall)} bot.log lines')
        pairs[kind] = len(mono)
        offsets += [w-t for w, t in zip(wall, mono)]
    spread = max(offsets)-min(offsets)
    return dict(offset_s=statistics.median(offsets), spread_s=spread, anchors=pairs, n=len(offsets))


def mapped_inputs(epochs, m, plan):
    """Monotonic epoch stamps -> wall-clock peers; the clock slack grows by the mapping spread + 1 ms."""
    off = m['offset_s']
    peers = []
    for e in epochs:
        d = e.get('disconnect') or {}
        peers.append(dict(id=e['id'], pc_id=(e.get('first') or {}).get('pc_id'), live_from_wall=e['pre_connect_ns']/1e9+off,
                          disconnect=dict(lower_wall=d['send_lower_s']+off, sent_wall=d['sent_s']+off, ok=d.get('ok')) if d else None))
    slack = plan['liveness_logs']['clock_slack_s']+m['spread_s']+0.001
    return peers, dict(plan, liveness_logs=dict(plan['liveness_logs'], clock_slack_s=slack)), slack


def rejudge(folder, plan_path=RECONNECT_PLAN):
    folder = Path(folder)
    gate = json.loads((folder/'gate.json').read_text())
    bot_log = (folder/'bot.log').read_text(errors='replace')
    device = (folder/'aec-serial.log').read_bytes().decode(errors='replace').splitlines()
    rows = [json.loads(line) for f in sorted(glob.glob(str(folder/'sessions/session-*/events.jsonl')))
            for line in open(f) if line.strip()]
    m = clock_map(rows, bot_log)
    plan = json.loads(Path(plan_path).read_text())
    peers, plan, slack = mapped_inputs(gate['epochs'], m, plan)
    result = g.liveness_logs_attributed(bot_log, device, peers, plan)
    parts = {k: v for k, v in gate['parts'].items()}
    parts['liveness-logs'] = result
    hypothetical = g.gate_verdict(parts)
    return dict(diagnostic=True, rescore=False,
                note='DIAGNOSTIC ONLY. The run stays at its outcome of record; this file does not rescore it '
                     '(decision 112: rule changed after the data were seen; a fresh run is required). The r8 rule '
                     '(decision 113) attributes alarms only by the pc_id tag on the alarm line, which the bot writes '
                     'since r8; a pre-113 run such as smoke-01 has untagged alarms, which FAIL closed under it.',
                of_record=dict(gate_outcome=gate['verdict']['outcome'], liveness_logs=gate['parts'].get('liveness-logs')),
                inputs=dict(folder=str(folder), gate_sha256=sha(folder/'gate.json'), bot_log_sha256=sha(folder/'bot.log'),
                            device_log_sha256=sha(folder/'aec-serial.log'), plan_sha256=sha(plan_path)),
                clock=dict(m, clock_slack_s=slack), peers=peers, liveness_logs=result,
                hypothetical_gate_outcome_if_only_liveness_logs_replaced=hypothetical['outcome'])


def part1_disconnect_bounds(hold, m):
    """Evidence-derived wall bracket of part 1's final 'disconnect' console send, for runs that predate the
    promotion wrapper's stamp (smoke-02). Lower: the device's 'audio 0' record (candidate-serial.audio.jsonl,
    host monotonic receipt) mapped to wall time - the hold sends 'audio 0', waits for its OK, then sends
    'disconnect' - minus the mapping spread and 1 ms. Upper: the mtime of disconnected.json, the snapshot the
    hold writes only after the disconnect's OK (a hard upper bound on the send, on the same wall clock)."""
    import os
    hold = Path(hold)
    upper = os.stat(hold/'disconnected.json').st_mtime
    rows = [json.loads(x) for x in (hold/'candidate-serial.audio.jsonl').read_text().splitlines() if x.strip()]
    offs = [r['host_s']+m['offset_s'] for r in rows if r.get('enabled') == 0 and r['host_s']+m['offset_s'] < upper]
    if not offs:
        raise ValueError('no audio-0 record before the disconnected snapshot')
    return dict(command='disconnect', ok=True, lower_wall=max(offs)-m['spread_s']-0.001, sent_wall=upper,
                sent_mono=upper-m['offset_s'],
                basis='lower: last audio-0 record mapped to wall (anchor offset, minus spread + 1 ms); upper: '
                      'disconnected.json mtime (written after the disconnect OK)')


def rejudge_part1(run):
    """Smoke part 1 under the decision-114 rule (promotion_smoke.part1_liveness_rule, current spec)."""
    import echoear_aec_promotion_smoke as s
    run = Path(run); hold = run/'part-1'
    bot_log = '\n'.join(p.read_text(errors='replace') for p in sorted((hold/'bot').glob('server-*.log')))
    rows = [json.loads(line) for f in sorted(glob.glob(str(hold/'sessions/session-*/events.jsonl')))
            for line in open(f) if line.strip()]
    m = clock_map(rows, bot_log)
    stamp = part1_disconnect_bounds(hold, m)
    spec, spec_sha = s.load_spec()
    plan2, plan2_sha = s.reconnect_plan(spec)
    lines = s.serial_lines(hold)
    pc = (s.perf(hold/'connected.json') or {}).get('pc_id')
    path = hold/'evaluation/events.jsonl'
    events = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    result = s.part1_liveness_rule(plan2, pc, [stamp], bot_log, lines, events)
    verdict = json.loads((run/'promotion-verdict.json').read_text())
    counters = verdict['part1']['checks']['counters']
    others = {k: c['passed'] for k, c in verdict['part1']['checks'].items() if k != 'counters'}
    counters_now = bool(counters.get('part_audit_clean')) and counters.get('aec_counters_zero') is True and \
        (counters.get('liveness') or {}).get('timeouts') == 0 and (counters.get('liveness') or {}).get('pongs', 0) > 0 and result['passed']
    return dict(diagnostic=True, rescore=False,
                note='DIAGNOSTIC ONLY. The run stays at its outcome of record; this file does not rescore it (decision 114: '
                     'the part-1 rule changed after the data were seen; a fresh smoke-03 is required). The disconnect send '
                     'is bracketed from retained evidence because smoke-02 predates the promotion wrapper stamp. Since '
                     'r11 (decision 115) the rule also needs silent_s on the alarm line, which pre-115 runs lack, so '
                     'their alarms FAIL closed under the current rule.',
                of_record=dict(outcome=verdict['outcome'], part1=verdict['part1']['outcome'], part2=verdict['part2']['outcome'],
                               counters_passed=counters['passed']),
                inputs=dict(run=str(run), promotion_verdict_sha256=sha(run/'promotion-verdict.json'),
                            bot_logs_sha256={p.name: sha(p) for p in sorted((hold/'bot').glob('server-*.log'))},
                            spec_sha256=spec_sha, reconnect_plan_sha256=plan2_sha),
                clock=m, disconnect=stamp, part1_bot_liveness=result,
                hypothetical_part1_if_only_the_bot_clause_replaced='PASS' if counters_now and all(others.values()) else 'FAIL')


def rejudge_teardown(run):
    """Promotion smoke part 2 under the decision-117 teardown principle: re-score each echo cell from the retained
    recorder rows, serial lines and room recording with the deliberately disconnected peers' post-send rows split
    off, and recorder integrity likewise. The live cell's start_ns (taken just before the recorder spawn) is not
    retained, so the window starts 10 ms before the recorder's popen_s and ends at the recorder stop; the
    recomputation WITHOUT the principle is reported next to the outcome of record as a check of that window."""
    import numpy as np
    import echoear_mute_gate as g
    run = Path(run); part = run/'part-2'
    gate = json.loads((part/'gate.json').read_text())
    plan = json.loads((ROOT/'hil/plans/echoear-aec-reconnect-v1.json').read_text())
    rows = [json.loads(line) for f in sorted(glob.glob(str(part/'sessions/session-*/events.jsonl')))
            for line in open(f) if line.strip()]
    rows = [r for r in rows if r.get('kind') != 'audio']
    retired = g.retired_peers(gate['epochs'], plan['teardown_bracket_max_s'])     # decision 120: OK-receipt stamp
    cells = {}
    for name, results in gate['cells'].items():
        for c in results:
            h = c.get('recording_health') or {}
            start = int((h['popen_s']-0.01)*1e9); end_s = h['diagnostic']['send_s']
            window = [r for r in rows if start <= r['monotonic_ns'] <= int(end_s*1e9)]
            raw = np.fromfile(part/(c['label']+'-room.raw'), dtype='<i2')
            rec = dict(samples=raw, rate=16000, start_interval_s=h['start_interval_s'], healthy=h['healthy'], health=h)
            lines = json.loads((part/(c['label']+'-serial.json')).read_text())
            # the live cell saw only the peers retired before it ran
            before = {pc: ns for pc, ns in retired.items() if ns < start}
            new = g.score_echo_cell(window, lines, rec, plan, c['prompt'], c['prompt_start_s'], c['prompt_end_s'], end_s, retired=before)
            old = g.score_echo_cell(window, lines, rec, plan, c['prompt'], c['prompt_start_s'], c['prompt_end_s'], end_s)
            cells[c['label']] = dict(of_record=dict(status=c['status'], cause=c.get('cause')), recomputed_without_principle=dict(
                status=old['status'], failures=old['failures']), under_principle=dict(status=new['status'], failures=new['failures'],
                missing=new['missing'], teardown_report_only=new['teardown_report_only']))
    ri = g.recorder_integrity(rows, retired)
    parts = dict(gate['verdict']['parts'])
    parts.update({'reply-'+k[0]: v['under_principle']['status'] for k, v in cells.items()},
                 **{'recorder-integrity': 'PASS' if ri['passed'] else 'FAIL'})
    statuses = {k: dict(status=v) for k, v in parts.items()}
    return dict(diagnostic=True, rescore=False,
                note='DIAGNOSTIC ONLY. smoke-03 stays FAIL of record; this file does not rescore it (decision 117: the '
                     'teardown principle was declared after the data were seen; a fresh smoke-04 is required). Since '
                     'decision 120 retirement starts at the acknowledged disconnect (OK receipt) and needs a bracket no '
                     'wider than teardown_bracket_max_s.',
                retirement=dict(rule='decision 120: OK-receipt stamp, bracket <= teardown_bracket_max_s',
                                max_bracket_s=plan['teardown_bracket_max_s'],
                                brackets_s={e['id']: (e.get('disconnect') or {}).get('done_s', 0)-(e.get('disconnect') or {}).get('send_lower_s', 0)
                                            for e in gate['epochs']},
                                teardown_rows_before_upper=sum(1 for r in rows for e in gate['epochs']
                                    if r.get('pc_id') == (e.get('first') or {}).get('pc_id') and (e.get('disconnect') or {}).get('send_lower_s')
                                    and e['disconnect']['send_lower_s']*1e9 <= r['monotonic_ns'] < e['disconnect']['done_s']*1e9)),
                of_record=dict(outcome=json.loads((run/'promotion-verdict.json').read_text())['outcome'], part2_gate=gate['verdict']),
                inputs=dict(run=str(run), gate_sha256=sha(part/'gate.json')), retired_ok_receipt_ns=retired,
                cells=cells, recorder_integrity_under_principle=ri,
                hypothetical_part2_gate_if_only_reply_and_integrity_replaced=g.gate_verdict(statuses))


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument('folder', help='retained part-2 folder, or with --part1 the retained run folder (read-only)')
    a.add_argument('out', help='new JSON file, outside the run folder')
    a.add_argument('--part1', action='store_true', help='promotion smoke part 1 under the decision-114 rule')
    a.add_argument('--part2-teardown', action='store_true', help='promotion smoke part 2 under the decision-117 teardown principle')
    a = a.parse_args(argv)
    folder, out = Path(a.folder).resolve(), Path(a.out).resolve()
    run_root = folder.parent if folder.name.startswith('part-') else folder
    if run_root == out.parent or run_root in out.parents:
        raise SystemExit('refusing to write inside the retained run folder')
    if a.part2_teardown:
        result = rejudge_teardown(folder)
        with out.open('x') as stream:
            json.dump(result, stream, indent=1)
        print(json.dumps(dict(out=str(out), hypothetical=result['hypothetical_part2_gate_if_only_reply_and_integrity_replaced']['outcome'],
                              of_record=result['of_record']['outcome'])))
        return
    if a.part1:
        result = rejudge_part1(folder)
        with out.open('x') as stream:
            json.dump(result, stream, indent=1)
        print(json.dumps(dict(out=str(out), part1_bot_liveness=result['part1_bot_liveness']['passed'],
                              of_record=result['of_record']['outcome'],
                              hypothetical_part1=result['hypothetical_part1_if_only_the_bot_clause_replaced'])))
        return
    result = rejudge(folder)
    with out.open('x') as stream:
        json.dump(result, stream, indent=1)
    print(json.dumps(dict(out=str(out), liveness_logs=result['liveness_logs']['passed'],
                          of_record=result['of_record']['gate_outcome'],
                          hypothetical=result['hypothetical_gate_outcome_if_only_liveness_logs_replaced'])))


if __name__ == '__main__':
    main()
