"""File-only Nova pre-step measurements; missing evidence never becomes a pass."""
import json
from pathlib import Path
import re
import statistics
import numpy as np
from echoear_aec_bench import sha, NORMAL_HASH
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore
from echoear_aec_source_preflight import measure


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).is_file() else default


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()] if Path(path).is_file() else []


def stats(values):
    values = [v for v in values if isinstance(v, (int,float)) and np.isfinite(v)]
    return dict(count=len(values), median=statistics.median(values) if values else None,
                maximum=max(values) if values else None, population_p99=False)


def normalize(text):
    return re.findall(r"[a-z0-9]+", (text or '').lower())


def turn_measurements(cell, events, recorded):
    turns = [e for e in events if e.get('event')=='user-turn-stopped']
    starts = [e for e in events if e.get('event')=='user-turn-started']
    requests = [e for e in events if e.get('event')=='real-bot-llm-request']
    transcripts = [e for e in recorded if e.get('kind') in ('stt-frame','flux-frame') and e.get('frame') in ('TranscriptionFrame','InterimTranscriptionFrame')]
    finals = [e for e in transcripts if e['frame']=='TranscriptionFrame']
    boundaries = [e for e in events if e.get('event')=='nova-transcript-boundary']
    stop_times = [e['t'] for e in events if e.get('event')=='frame' and e.get('frame')=='VADUserStoppedSpeakingFrame']
    vad_starts = [e for e in events if e.get('event')=='frame' and e.get('frame')=='VADUserStartedSpeakingFrame']
    # Each accepted turn remains a separate row. Never concatenate split turns.
    per_turn = []
    for i, turn in enumerate(turns):
        previous = turns[i-1]['t'] if i else float('-inf')
        ends = [t for t in stop_times if previous < t <= turn['t']]
        own = [e for e in finals if previous < e['monotonic_ns']/1e9 <= turn['t']]
        per_turn.append(dict(text=turn.get('text'), strategy=turn.get('strategy'), pc_id=turn.get('pc_id'),
            accepted_at_s=turn['t'], final_segment_at_s=own[-1]['monotonic_ns']/1e9 if own else None,
            final_to_turn_delay_s=turn['t']-own[-1]['monotonic_ns']/1e9 if own else None,
            turn_completion_delay_s=turn['t']-ends[-1] if ends else None,
            final_segment_delay_s=own[-1]['monotonic_ns']/1e9-ends[-1] if own and ends else None))
    expected = cell.get('expected_text')
    if expected:
        structure_complete = (len(turns)==len(starts)==len(requests)==1 and normalize(turns[0].get('text'))==normalize(expected)
            and bool(finals) and all(t.get('strategy') for t in turns+starts)
            and {e['frame_id'] for e in finals} <= {e.get('frame_id') for e in boundaries}
            and all((e.get('vendor_result') or {}).get('channel',{}).get('alternatives') for e in transcripts)
            and all((e.get('vendor_result') or {}).get('channel',{}).get('alternatives',[{}])[0].get('words') for e in finals)
            and all(e['monotonic_ns']/1e9 <= requests[0]['t'] for e in finals))
        complete = structure_complete and bool(vad_starts and stop_times)
    else:
        complete = not (turns or starts or requests or any(e.get('frame')=='InterruptionFrame' for e in events))
        structure_complete = complete
    return dict(complete=bool(complete), structure_complete=bool(structure_complete),
        vad_timing_available=bool(vad_starts and stop_times), expected_text=expected, accepted_turns=per_turn,
        start_events=starts, llm_requests=requests, segments=transcripts, vad_starts=vad_starts,
        vad_stop_times_s=stop_times, boundary_events=boundaries,
        transcript_delay_definition='host receipt of last segment minus last preceding VAD stop; may be negative; not provider p99')


def analyze(p, output, *, include_rejected=False):
    from echoear_aec_nova_prestep import profile_witness
    from echoear_aec_campaign_analysis import generation_audit
    from echoear_aec_host_gate import audit as host_audit
    out = Path(output)
    # Hash before and after; derived analyses are not acquisition inputs.
    files = sorted(f for f in out.rglob('*') if f.is_file() and f.name not in
                   ('nova-preflight.json','combined-nova-preflight.json'))
    before = {str(f):sha(f) for f in files}
    events = rows(out/'evaluation/events.jsonl')
    recorded = sorted([e for path in sorted((out/'sessions').glob('*/events.jsonl')) for e in rows(path)], key=lambda e:e['monotonic_ns'])
    saved = read(out/'result.json', {})
    accepted = {c['run_id']:c for c in saved.get('cases', [])}
    binding = profile_witness(p, events)
    generations = generation_audit(events, recorded)
    metrics = {(s['pc_id'],g['context_id']):g for s in generations['sessions'] for g in s['generations']}
    measurements = []
    for cell in p['cases']:
        cid = cell['id']+'-a1'; folder = out/cid
        c = accepted.get(cid, {})
        if not c and include_rejected:
            # Descriptive evidence only; the separate partial-run audit must
            # still enforce every acquisition gate before assigning any use.
            c = next((r for r in read(folder/'result.json',{}).get('cases',[]) if r.get('run_id')==cid), {})
        own = [e for e in events if e.get('case_id')==cid]
        case_pcs = {e.get('pc_id') for e in own if e.get('pc_id')}
        window = read(folder/'attempt-window.json', {}).get('window_s', [None,None])
        lower = c.get('start_ns',0)/1e9
        upper = window[1] if window[1] is not None else c.get('end_ns',0)/1e9
        rec = [e for e in recorded if e.get('pc_id') in case_pcs and lower <= e['monotonic_ns']/1e9 <= upper] if c else []
        turn = turn_measurements(cell, own, rec)
        contexts = []
        for event in [e for e in own if e.get('event')=='tts-context-pcm']:
            context = event['context_id']; pcm_path = out/'evaluation'/event['path']
            raw = pcm_path.read_bytes() if pcm_path.is_file() else b''
            values = np.frombuffer(raw, '<i2') if len(raw)%2==0 else np.array([])
            pcm_events = [e for e in own if e.get('event')=='tts-pcm' and e.get('context_id')==context]
            writes = [e for e in rec if e.get('kind')=='tts-transport-write' and e.get('context_id')==context]
            active = [e for e in writes if e.get('non_silent')]
            normal_close = [e for e in own if e.get('event')=='tts-generation-stopped' and e.get('context_id')==context]
            interrupted = [e for e in own if e.get('event')=='tts-interruption-stopped' and e.get('context_id')==context]
            metric = metrics.get((event.get('pc_id'), context), {})
            complete = bool(len(values)>=320 and len(raw)==sum(e.get('bytes',0) for e in pcm_events) and
                len(normal_close)==1 and not interrupted and metric.get('complete') and active)
            source = measure(values) if len(values)>=320 else dict(passed=False, samples=len(values), reason='Missing/truncated context PCM')
            contexts.append(dict(context_id=context, pc_id=event.get('pc_id'), path=str(pcm_path),
                sha256=sha(pcm_path) if pcm_path.is_file() else None,
                complete=complete, interrupted=bool(interrupted), source_continuity=source,
                source_qualification=bool(complete and source['passed']),
                continuity_censored=bool(interrupted), pcm_seconds=len(values)/16000,
                first_non_silent_write_s=active[0]['monotonic_ns']/1e9 if active else None,
                last_write_end_s=writes[-1]['monotonic_ns']/1e9+writes[-1]['samples']/16000 if writes else None,
                metrics=metric, closures=normal_close+interrupted))
        real = [r for r in contexts if r['metrics'].get('caller_anchor')=='real-bot-llm-request']
        expected_replies = real if cell.get('real_bot_reply') else contexts
        stops = [e['t'] for e in own if e.get('frame')=='BotStoppedSpeakingFrame']
        starts = [e['t'] for e in own if e.get('frame')=='BotStartedSpeakingFrame']
        last_write = max((r['last_write_end_s'] for r in contexts if r['last_write_end_s'] is not None), default=None)
        end = c.get('end_ns',0)/1e9
        complete_reply = bool(expected_replies and all(r['complete'] for r in expected_replies) and
            stops and starts and stops[-1]>starts[-1] and last_write is not None and
            end-max(stops[-1],last_write) >= 2.25 and c.get('quiet_windows_valid'))
        llm_end = [e for e in rec if e.get('kind')=='frame' and e.get('frame')=='LLMFullResponseEndFrame']
        if cell.get('real_bot_reply'):
            complete_reply &= bool(len(llm_end)==1 and any(e.get('kind')=='aggregator-assistant-turn-stopped' for e in rec))
        duration = read(folder/'capture-duration.json', {})
        export = read(folder/'export-duration.json', {})
        rf = read(folder/'rf-pre-gate.json', {})
        timing_complete = bool(duration.get('elapsed_s',float('inf'))<=28 and export and rf.get('passed') and all(x is not None for x in window))
        complete = bool(c and c.get('noise_gate_passed') and turn['complete'] and complete_reply and timing_complete and len(case_pcs)==1)
        measurements.append(dict(id=cid, accepted_by_runner=cid in accepted, complete=complete, turn=turn, contexts=contexts,
            complete_reply=complete_reply, capture=duration, export=export, rf_pre_gate=rf,
            elapsed_with_pre_gate_s=window[1]-window[0] if all(x is not None for x in window) else None,
            quiet_tail_s=end-max(stops[-1],last_write) if stops and last_write is not None else None,
            llm_response_end_count=len(llm_end)))
    host = host_audit(out,p['host_environment']) if (out/'result.json').is_file() else dict(passed=False)
    elapsed = read(out/'elapsed.json', {})
    cleanup = bool(saved.get('restored_elf_sha256')==NORMAL_HASH and saved.get('mixer_equal') is True and saved.get('lock_released') is True and not saved.get('cleanup_errors'))
    valid_hold = bool(saved.get('completed') and cleanup and binding['passed'] and generations['passed']
        and host.get('passed') and elapsed.get('completed') and elapsed.get('seconds',float('inf'))<=p['budget']['total_s'])
    good = [m for m in measurements if m['complete'] and m['accepted_by_runner']] if valid_hold else []
    complete = len(good)==len(p['cases'])
    result = dict(schema='nova-preflight-measurements-v1', qualification=False, launch_authorized=False,
        final_freeze_ready=False, coordinator_review_required=True, complete_measurements=complete,
        profile=p['profile'], vendor_binding=binding, installed_packages_sha256=p['installed_packages_sha256'],
        runtime_sha256=p['runtime_sha256'], inputs_sha256=before, measurements=measurements,
        generation_audit=generations, host_audit=host, cleanup_complete=cleanup,
        summary=dict(complete_captures=len(good), expected_captures=len(p['cases']),
            capture_s=stats(m['capture'].get('elapsed_s') for m in good),
            export_s=stats(m['export'].get('elapsed_s') for m in good),
            end_to_end_s=stats(m['elapsed_with_pre_gate_s'] for m in good),
            final_segment_delay_s=stats(t['final_segment_delay_s'] for m in good for t in m['turn']['accepted_turns']),
            turn_completion_delay_s=stats(t['turn_completion_delay_s'] for m in good for t in m['turn']['accepted_turns']),
            real_reply_pcm_s=stats(r['pcm_seconds'] for m in good for r in m['contexts'] if r['complete'] and r['metrics'].get('caller_anchor')=='real-bot-llm-request'),
            real_reply_lead_s=stats(r['metrics'].get('lead_s') for m in good for r in m['contexts'] if r['complete'] and r['metrics'].get('caller_anchor')=='real-bot-llm-request')),
        source_metric_note='Interrupted source contexts are censored; a short captured prefix cannot prove a complete >=4 s source. No old Flux timing substitution. Review per-context unchanged metric before final freeze.')
    if before != {str(f):sha(f) for f in files}: raise RuntimeError('Measurement inputs changed during analysis')
    return result
