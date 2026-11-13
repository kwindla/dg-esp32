#!/usr/bin/env python3
"""Host-only RF eligibility arithmetic and bounded scheduling; no device IO.

This pre-gate never qualifies firmware or changes scored uplink limits. The
acquisition protocol and immutable inputs are declared in the matching plan.
"""
import json
import math
from pathlib import Path

from uplink_continuity import audio_idle_windows, link_tail

PLAN = Path(__file__).parent/'plans/echoear-attempt3-rf-precheck.json'


def load_plan(path=PLAN):
    plan = json.loads(Path(path).read_text())
    assert plan['schema'] == 1 and plan['declaration_only'] is True
    assert plan['campaign']['runs'] == 3 and plan['campaign']['retakes'] == 0
    assert plan['probe']['duration_s'] == 60 and plan['probe']['settle_s'] == 6
    assert plan['probe']['tail_rate_exclusive'] == .005
    assert plan['retry'] == dict(initial_probes=1, maximum_retries=4,
                                 maximum_total_probes=5, wait_seconds=1800,
                                 release_bench=True)
    return plan


def evaluate_probe(packets, events, audio_states, *, pc_id, start, end,
                   truncated=False, plan=None):
    """Replay the complete predeclared window; no gap-fitted window/exclusions.

    Idle describes a quiet room here, not disabled device audio. No startup or
    audio-idle gap is excluded from this measurement. Missing evidence defers.
    Raw errors/resource/cleanup failures are fatal in the acquisition protocol.
    """
    plan = plan or load_plan()
    result = dict(eligible=False, qualification=False, scored=False,
                  reasons=[], start_s=start, end_s=end, pc_id=pc_id)
    if not (type(start) in (int, float) and type(end) in (int, float)
            and math.isfinite(start) and math.isfinite(end)
            and math.isclose(end-start, plan['probe']['duration_s'], abs_tol=.001)):
        result['reasons'].append('Missing or short declared 60-second window')
        return result
    try:
        streams = {(p['pc_id'], p['ssrc']) for p in packets
                   if start-1 <= p['observed_ms']/1000 <= end+1}
        if len(streams) != 1 or next(iter(streams))[0] != pc_id:
            result['reasons'].append('One continuous expected peer/SSRC required')
        rows = [p for p in packets if p['pc_id'] == pc_id
                and (p['pc_id'], p['ssrc']) in streams]
        if not rows or min(p['observed_ms'] for p in rows)/1000 > start-plan['probe']['settle_s']:
            result['reasons'].append('Six-second connected uplink settling evidence missing')
        # Preserve link_tail's adjacency/coverage semantics. Settle is measured
        # separately; no startup, fault, console or idle exclusions are passed.
        tail = link_tail(rows, start, end, startup_seconds=0)
        result['link_tail'] = tail
        if tail['covered'] is not True:
            result['reasons'].append('RTP endpoint coverage missing')
        if tail['intervals'] < plan['probe']['minimum_intervals']:
            result['reasons'].append('Insufficient continuous 20ms RTP intervals')
        # Integer comparison makes exactly0.5% fail without rounding.
        if not tail['intervals'] or tail['over_threshold']*200 >= tail['intervals']:
            result['reasons'].append('Uplink >60ms tail must be strictly below0.5%')
        if any(b['host_s'] < a['host_s'] for a, b in zip(audio_states, audio_states[1:])):
            result['reasons'].append('Audio acknowledgement clock regressed')
        prior = [r for r in audio_states if r['host_s'] <= start]
        enabled = bool(prior) and prior[-1]['enabled'] == 1
        idle = audio_idle_windows(audio_states, end=end)
        if not enabled or any(w['start_s'] < end and w['end_s'] > start for w in idle):
            result['reasons'].append('Audio must remain enabled throughout probe')
        activity = []
        for e in events:
            if not start <= e['t'] <= end:
                continue
            if e.get('pc_id') != pc_id:
                result['reasons'].append('Unexpected peer event during probe')
            if e['event'] in plan['probe']['disallowed_events']:
                activity.append(e)
            if e['event'] == 'rtvi-out' and e.get('message', {}).get('type') in (
                    'bot-started-speaking', 'user-started-speaking'):
                activity.append(e)
        result['activity'] = activity
        if activity:
            result['reasons'].append('Speech/playback/session activity invalidates idle probe')
        if not any(e.get('pc_id') == pc_id and e['event'] == 'input-level'
                   and start <= e['t'] <= end for e in events):
            result['reasons'].append('Live input pipeline evidence missing')
        if truncated is not False:
            result['reasons'].append('RTP capture truncated or status unavailable')
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        result['reasons'].append('Unavailable/malformed probe evidence: '+str(exc))
    result['eligible'] = not result['reasons']
    return result


def next_action(history, now_utc_s, *, campaign_started=False, plan=None):
    """An append-only probe ledger controls retries, never scored turn retakes.

    UTC deadlines survive launcher restarts. The collector also records host
    monotonic timestamps; backwards/inconsistent clocks fail closed.
    """
    plan = plan or load_plan()
    finite = lambda value: type(value) in (int, float) and math.isfinite(value)
    if not finite(now_utc_s) or type(campaign_started) is not bool:
        raise ValueError('Finite current UTC and explicit campaign disposition required')
    if campaign_started:
        return dict(action='stop', reason='The single attempt3 population is consumed')
    if not history:
        return dict(action='probe', probe=1)
    for index, row in enumerate(history, 1):
        if (type(row.get('probe')) is not int or row['probe'] != index
                or row.get('scored') is not False or row.get('completed') is not True
                or type(row.get('eligible')) is not bool
                or type(row.get('fatal')) is not bool
                or (row['eligible'] and row['fatal'])
                or not finite(row.get('started_utc_s'))
                or not finite(row.get('ended_utc_s'))):
            raise ValueError('Complete contiguous unscored probe ledger with explicit boolean verdicts required')
        if index < len(history) and (row.get('eligible') or row.get('fatal')):
            raise ValueError('No probe may follow admission or a fatal failure')
        if index > 1 and row['started_utc_s'] < history[index-2]['ended_utc_s']+1800:
            raise ValueError('Retry started before30-minute deadline')
        if not row['started_utc_s'] <= row['ended_utc_s'] <= now_utc_s:
            raise ValueError('Invalid/unfinished UTC probe interval')
    if len(history) > plan['retry']['maximum_total_probes']:
        raise ValueError('More than five probe opportunities')
    last = history[-1]
    if last.get('fatal'):
        return dict(action='stop', reason='Raw/device/resource/cleanup failure is fatal')
    if last.get('eligible'):
        return dict(action='campaign', probe=last['probe'], runs=3, retakes=0)
    if len(history) >= plan['retry']['maximum_total_probes']:
        return dict(action='stop', reason='All five RF eligibility opportunities exhausted; campaign unstarted')
    deadline = last['ended_utc_s']+plan['retry']['wait_seconds']
    if now_utc_s < deadline:
        return dict(action='wait', until_utc_s=deadline, release_bench=True)
    return dict(action='probe', probe=len(history)+1)
