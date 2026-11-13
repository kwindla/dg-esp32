#!/usr/bin/env python3
"""Real STT -> LLM -> TTS acoustic conversations on the calibrated Jabra bench.

Default: six turns. --runs 10: five cold boots then five conversations on the
last cold session. --spl 58 --runs 2: quiet qualification. --soak-minutes 45:
135 scripted turns at 20-second intervals, per-minute PERF. --interruptions:
a long generated reply, physical speech over its closed gate, recovery turn.
Every original observation is retained; failures never trigger replacement runs.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time
import numpy as np
import requests
from acoustic_response import calibrate_response, predict
from agent_prompts import BANK, TURNS, EXTRA, credentials, validate
from alsa import resolve_jabra, set_volumes, play_wav, record_wav
from bench_calibration import calibrate, record_prompt
from esp_serial import ROOT, EspSerial
from phase2_endurance import snapshot
from soak_memory import (QUALIFICATION_MEMORY_TAIL_SECONDS, heap_trends, closed_stack_margins, closed_stack_unknown_sizes,
    transient_memory, growth_verdict, unknown_heap_windows, attach_heap_intervals, host_uplink_gap_windows)
from step7_support import BotServer, pcm, write_pcm, acoustic_match, leakage
from transport_test import perf, wait_until, dump
from voice_mute_evidence import mute_evidence
from playback_quality import classify_turn,jabra_level_gate,decoded_match,transport_gate,acoustic_content_verdict
from speaker_dual_sweep_test import dump_mic

CHECKS = ('vad', 'stt', 'llm_content', 'playback', 'acoustic_content', 'gate', 'one_reply', 'session')
from playback_quality import TRANSPORT_COUNTERS
CAUSE = dict(vad='STT miss', stt='STT miss', llm_content='LLM wording',
             playback='playback', acoustic_content='acoustic', gate='gate',
             one_reply='reply count', session='session/device')


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def content_matches(text, groups):
    words = set(re.findall(r'\w+', text.casefold()))
    return all(any(word.casefold() in words for word in group) for group in groups)


def answer_matches(text, definition):
    if definition['id'] == 'long':
        # Deepgram smart_format can join a spoken 1..20 count into one token.
        # Normalize only this exact complete sequence, never digit substrings.
        sequence = ''.join(str(i) for i in range(1, 21))
        text = re.sub(r'\b'+sequence+r'\b', ' '.join(str(i) for i in range(1, 21)), text)
    return content_matches(text, definition['answer'])


def playback_counter_deltas(before, after):
    # These are the actual PERF schema's decode and consumer counters.
    # A missing counter is unavailable evidence, never a fabricated zero.
    return {k: after[k]-before[k] for k in ('received', 'decode_frames', 'pulled_frames')}


def cancelled_response_ids(events):
    cancelled = {e.get('response_id') for e in events if e['event'] == 'llm-cancelled'}
    # Compatibility with early 8d2 recordings: a broadcast can be observed
    # before the service emits End, before the downstream audit sees the signal.
    for start in (e for e in events if e['event']=='llm-start' and e.get('response_id') is not None):
        if start['response_id'] in cancelled or 't' not in start: continue
        end = next((e['t'] for e in events if e['event']=='llm-response'
                    and e.get('response_id')==start['response_id']), float('inf'))
        if any(e['event']=='interruption' and e.get('response_id')==start['response_id']
               and start['t'] <= e['t'] <= end for e in events):
            cancelled.add(start['response_id'])
    return cancelled


def one_reply_matches(events, segments=None):
    generations = [e for e in events if e['event'] == 'llm-start']
    responses = [e for e in events if e['event'] == 'llm-response']
    cancelled = cancelled_response_ids(events)
    finals = [e for e in events if e['event'] == 'final-transcript']
    complete = [e for e in responses if e.get('response_id') not in cancelled]
    if len(complete) != 1 or not finals:
        return False
    ids = [e.get('response_id') for e in generations]
    if len(generations) != len(complete) + len(cancelled) or len(set(ids)) != len(ids):
        return False
    # The fixture defines the user turn. audit.turn counts VAD edges, so an
    # initial transcription followed by late VAD can legitimately change that
    # label inside one utterance. Count audible generations independently.
    if cancelled and segments is None:
        return False  # Cancellation is not proof of <300 ms audible output.
    if segments is not None:
        good = [r for r in segments if r['response_id'] == complete[0].get('response_id')]
        if not good or not all(r.get('complete') and r.get('verified') for r in good):
            return False
        for response_id in cancelled:
            parts = [r for r in segments if r['response_id'] == response_id]
            if not parts and any(e['event']=='first-tts-byte' and e.get('response_id')==response_id for e in events):
                return False
            if any(r.get('audible_ms') is None for r in parts): return False
            if sum(r['audible_ms'] for r in parts) >= 300: return False
        if any(r['response_id'] not in cancelled | {complete[0].get('response_id')} for r in segments):
            return False
    return True


def cancelled_audio_duration(events, segments, response_id):
    parts = [r for r in segments if r['response_id']==response_id]
    if not parts and any(e['event']=='first-tts-byte' and e.get('response_id')==response_id for e in events):
        return None
    if any(r.get('audible_ms') is None for r in parts): return None
    return sum(r['audible_ms'] for r in parts)


def final_before_inference(events):
    starts = [e['t'] for e in events if e['event'] == 'llm-start']
    finals = [e for e in events if e['event'] == 'final-transcript']
    boundary = min(starts) if starts else None
    before = [e for e in finals if boundary is not None and e['t'] <= boundary]
    return (before[-1]['t'] if before else None,
            [e for e in finals if boundary is not None and e['t'] > boundary])


def transport_counter_deltas(before, after):
    return transport_gate(before,after)['deltas']


def cancelled_segment(recording, reference, sent_at, ready_at, prompt_start, model, events, chain="jabra"):
    """Measure a cancelled prefix; unknown acoustic duration cannot pass the rule.

    Grow one common source prefix in 20 ms steps. The last qualifying prefix
    bounds audible duration to one frame; include that frame in the gate score.
    """
    source = pcm(reference)
    from bench_calibration import active_mask
    active = np.flatnonzero(active_mask(source))
    if not len(active):
        return dict(verified=False, complete=False, audible_ms=None, error='Inactive TTS reference')
    begin = active[0]
    best = None
    for length in range(begin+1600, len(source)+321, 320):
        prefix = source[:min(length, len(source))]
        try:
            match = acoustic_match(recording, reference, max(0, sent_at-ready_at), ready_at,
                prompt_start-ready_at-.1, reference_pcm=predict(prefix, model, chain), server_send_at=sent_at)
        except (AssertionError, ValueError):
            break
        if match['correlation'] < .6: break
        best = match
        best['end'] = ready_at+match['offset_s']+len(prefix)/16000
        best['audible_ms'] = 1000*(len(prefix)-begin)/16000+20
    return dict(best, verified=True, complete=False) if best else dict(
        verified=False, complete=False, audible_ms=None, error='Cancelled prefix timing unavailable')


def reply_gate_window(gates):
    closed = [t for t, state in gates if state == 'closed']
    if not closed:
        return None, None
    opened = [t for t, state in gates if state == 'open' and t > closed[-1]]
    return closed[0], opened[-1] if opened else None


def failure_cause(name, checks):
    if name == 'vad':
        return 'VAD miss'
    if name == 'llm_content' and checks.get('stt') is False:
        return 'STT miss (upstream)'
    if name == 'acoustic_content' and checks.get('llm_content') is False:
        return 'STT miss (upstream)' if checks.get('stt') is False else 'LLM wording (upstream)'
    return CAUSE[name]


def latency_summary(turns, field='latency_ms'):
    keys = sorted({k for r in turns for k in r.get(field, {})})
    result = {}
    for key in keys:
        values = [r[field][key] for r in turns if r.get(field, {}).get(key) is not None]
        result[key] = dict(n=len(values), p50=float(np.percentile(values, 50, method='lower')) if values else None,
            p90=float(np.percentile(values, 90, method='lower')) if values else None,
            max=max(values) if values else None)
    return result


def summarize(turns):
    eligible=[r for r in turns if classify_turn(r)['passed']]
    return dict(acoustic_scored_turns=len(eligible),transport_damaged_turns=sum(classify_turn(r)['classification']=='transport-damaged' for r in turns),
        acoustic_content_passed=sum(r['checks'].get('acoustic_content') is True for r in eligible),
        turns=len(turns), passes=sum(r['passed'] for r in turns),
        checks={k: dict(passed=sum(r['checks'].get(k) is True for r in (eligible if k in ('playback','acoustic_content','gate') else turns)),
            unavailable=sum(r['checks'].get(k) is None for r in (eligible if k in ('playback','acoustic_content','gate') else turns)),
            total=len(eligible if k in ('playback','acoustic_content','gate') else turns)) for k in CHECKS},
        failures=[dict(index=i+1, prompt=r['prompt'], failures=r['failures'])
                  for i, r in enumerate(turns) if not r['passed']], latency_ms=latency_summary(turns),
        stage_latency_ms=latency_summary(turns, 'stage_latency_ms'),clean_latency_ms=latency_summary(eligible))


def boot(board, elf):
    cur = board.cursor
    board.reset(); board.wait_for('READY transport-console', 60, since=cur)
    identity = board.wait_for(r'elf_sha256=([0-9a-f]{64})', 5, since=cur)
    board.command('mic-gain '+str(getattr(board, 'agent_mic_gain', 1600)))
    if getattr(board,'agent_playback_target',None) is not None:
        board.command(f'playback-loudness {board.agent_playback_target} 26')
    actual = re.search(r'elf_sha256=([0-9a-f]{64})', identity)[1]
    assert actual == hashlib.sha256(elf.read_bytes()).hexdigest(), 'Flashed ELF differs'
    import os
    board.agent_trace=os.getenv('STEP8E_TRACE','0')=='1'
    if board.agent_trace:
        board.command('peer-option arrival-trace 1');board.command('peer-option call-timing 1')
    if os.getenv('BOT_BENCH_SERVER_URL'): board.command('server-url '+os.environ['BOT_BENCH_SERVER_URL'])
    return actual


def connect(board):
    cur = board.cursor
    board.command('connect', 90); entry = board.wait_for_entry('STATE Ready', 5, since=cur)
    board.agent_ready_at = entry[1]
    board.command('audio 1'); board.wait_for('GATE open', 20, since=cur)
    board.command('gate auto'); board.command('quiet 0')
    time.sleep(1)
    pc_id=perf(board)['pc_id']
    if getattr(board,'agent_session_start',None):board.agent_session_start(pc_id)
    return pc_id


def resume_after_restart(board, ready_entry):
    # STATE Ready is printed before App publishes readiness. The command waits
    # for published Ready and the worker completion, then capture can be enabled.
    board.command('connect', 90)
    board.agent_ready_at = ready_entry[1]
    board.command('audio 1')
    board.wait_for('GATE open', 20, since=ready_entry[0])
    board.command('gate auto'); board.command('quiet 0')
    before = perf(board)
    after = wait_until(lambda: (p if (p := perf(board))['sent'] > before['sent'] else None), 8)
    assert after['pc_id'] == before['pc_id'], 'Recovery session changed while checking uplink'
    return after['pc_id']


def restart_server(board, bot):
    cur = board.cursor
    bot.stop(kill=True); board.wait_for('STATE Disconnected', 60, since=cur)
    bot.start()
    return resume_after_restart(board, board.wait_for_entry('STATE Ready', 90, since=cur))


def calibration_definitions(*, interruptions=False, pause_case=False):
    return TURNS + ([EXTRA[0]] if interruptions else []) + ([EXTRA[1]] if pause_case else [])


def calibrate_bank(board, device, out, spl, definitions=None, bank=BANK):
    """Measure each immutable prompt at the native mic; require +/-1 dB."""
    out.mkdir(parents=True)
    base = calibrate(board, device, out/'bench', spl)
    prompts = {}
    for turn in TURNS + EXTRA if definitions is None else definitions:
        source = bank / (turn['id'] + '.wav')
        directory = out / turn['id']; directory.mkdir()
        scale = .1
        observations = []
        for attempt in range(5):
            row = record_prompt(board, device, directory, source, scale, f'attempt{attempt}')
            observations.append(row)
            if abs(row['estimated_spl'] - spl) <= 1:
                break
            scale *= 10**((spl-row['estimated_spl'])/20)
        prompts[turn['id']] = dict(row, target_spl=spl, attempts=observations,
            passed=abs(row['estimated_spl']-spl) <= 1,
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        write_json(out/'prompts.json', prompts)
        assert prompts[turn['id']]['passed'], 'Prompt calibration outside tolerance: ' + turn['id']
        print('Calibrated', turn['id'], round(row['estimated_spl'], 2), flush=True)
    return dict(bench=base, prompts=prompts)


def reference_audio(recorded,parts,chain):
    """Keep the complete matched utterance, including low-energy consonants."""
    audio=[]
    for part in parts:
        match=part['board'] if chain=='board' else part
        at=round(match['offset_s']*16000)
        count=len(pcm(Path(part['source_path'])))
        assert at>=0 and at+count<=len(recorded),'Truncated full-reference recording'
        audio.append(recorded[at:at+count])
    return np.concatenate(audio) if audio else np.array([],dtype=float)


def transcribe_acoustic(path):
    key = credentials()['DEEPGRAM_API_KEY']
    response = requests.post('https://api.deepgram.com/v1/listen',
        params=dict(model='nova-3', smart_format='true'),
        headers={'Authorization': 'Token ' + key, 'Content-Type': 'audio/wav'},
        data=path.read_bytes(), timeout=45)
    if response.status_code != 200:
        raise RuntimeError(f'Acoustic STT HTTP {response.status_code}')
    return response.json()['results']['channels'][0]['alternatives'][0]['transcript']


def transcribe_board_responses(out,recorded,segments,definition):
    """Score raw matched board audio independently of transport and Jabra gates."""
    response_ids=sorted({r['response_id'] for r in segments if not r['cancelled'] and 'board' in r})
    responses=[]
    for response_id in response_ids:
        parts=[r for r in segments if r['response_id']==response_id and not r['cancelled'] and 'board' in r]
        segment=reference_audio(recorded,parts,'board')
        path=out/'reply-board-segment.wav' if len(response_ids)==1 else out/f'reply-board-{response_id}.wav'
        write_pcm(path,segment);text=transcribe_acoustic(path)
        responses.append(dict(response_id=response_id,text=text,correct=answer_matches(text,definition),
            onset=min(r['onset'] for r in parts),end=max(r['end'] for r in parts),path=str(path)))
    return dict(available=bool(responses),passed=bool(responses) and all(r['correct'] for r in responses),responses=responses)


def run_turn(board, bot, device, out, definition, calibrated, response_model, pc_id,
              recording_seconds=13, overlap=None):
    out.mkdir(parents=True)
    if getattr(board,'agent_trace',False):
        from arrival_trace import drain
        drain(board,out/'arrivals.jsonl')
    row = dict(prompt=definition['id'], text=definition['text'], checks={}, failures=[], passed=False)
    cur = board.cursor
    board.command('rx-clear')
    before = perf(board); row['before_perf'] = before
    before_at = time.monotonic()
    def check(name, passed, detail):
        row['checks'][name] = None if passed is None else bool(passed)
        if passed is not None and not passed:
            row['failures'].append(dict(check=name, cause=failure_cause(name, row['checks']), detail=detail))
    try:
        card, actual = resolve_jabra(); assert actual == device
        set_volumes(card, out/'mixer.log')
        with record_wav(out/'turn.wav', device, recording_seconds, 16000) as rec:
            time.sleep(.3)
            board.command('mic-record 1');mic_started=time.monotonic();time.sleep(.4)
            start = time.monotonic(); play_wav(calibrated['path'], device); end = time.monotonic()
            row.update(prompt_start=start, prompt_end=end, recording_ready_at=rec.ready_at,
                       microphone_recording_started=mic_started)
            def events():
                return [e for e in bot.events() if e['pc_id'] == pc_id and e['t'] >= start]
            # Prefer a post-prompt snapshot before the first TTS byte. A split
            # case can already generate while the prompt plays, so retain the
            # earlier pre-prompt baseline when that would miss initial output.
            post_prompt_perf = perf(board); post_prompt_at = time.monotonic()
            reply_window_end = None
            try:
                board.wait_for('GATE closed', 18, since=cur)
                if overlap:
                    time.sleep(.8)
                    overlap_start = time.monotonic(); play_wav(overlap['path'], device)
                    row['overlap'] = dict(start=overlap_start, end=time.monotonic(), prompt='addition')
                board.wait_for('GATE open', 35 if overlap else 18, since=cur)
                time.sleep(2)  # Observe duplicates/noise after gate reopen.
                reply_window_end_perf = perf(board)
                reply_window_end = dict(t=time.monotonic(),perf=reply_window_end_perf)
            except (TimeoutError, AssertionError) as exc:
                row['response_wait_error'] = str(exc)
            board.command('mic-record 0')
            rec.wait()
        dump_mic(board,out/'turn-board.wav')
        decoded=dump(board,out)
        ev = events(); row['events'] = ev
        starts = [e for e in ev if e['event'] == 'vad-start']
        stops = [e for e in ev if e['event'] == 'vad-stop']
        finals = [e for e in ev if e['event'] == 'final-transcript']
        cancelled = cancelled_response_ids(ev)
        responses = [e for e in ev if e['event'] == 'llm-response' and e.get('response_id') not in cancelled]
        first_tokens = [e for e in ev if e['event'] == 'llm-first-token']
        first_bytes = [e for e in ev if e['event'] == 'first-tts-byte']
        references = [e for e in ev if e['event'] == 'tts-reference']
        row['stt_text'] = ' '.join(e['text'] for e in finals)
        row['llm_text'] = ' '.join(e['text'] for e in responses)
        check('vad', bool(starts and stops and stops[0]['turn'] == starts[0]['turn']), 'Missing matched VAD start/stop')
        check('stt', content_matches(row['stt_text'], definition['stt']), row['stt_text'])
        check('llm_content', answer_matches(row['llm_text'], definition), row['llm_text'])
        generations = [e for e in ev if e['event'] == 'llm-start']
        lines = board.lines_since(cur)
        text = '\n'.join(l for _, _, l in lines)
        row['after_perf'] = after = perf(board)
        row['transport_deltas'] = transport_counter_deltas(before, after)
        first_sent = min((e['t'] for e in first_bytes), default=None)
        window_before = post_prompt_perf if first_sent is not None and post_prompt_at < first_sent else before
        window_start = post_prompt_at if window_before is post_prompt_perf else before_at
        window_after = reply_window_end['perf'] if reply_window_end else after
        window_end = reply_window_end['t'] if reply_window_end else time.monotonic()
        row['reply_window_transport'] = dict(before=window_before,after=window_after,
            start=window_start,end=window_end,
            deltas=transport_counter_deltas(window_before,window_after),
            basis='Last completed pre-TTS snapshot (pre-prompt fallback for early split replies) through gate reopen + 2 s observation; whole-turn counters retained separately')
        check('session', before['pc_id'] == after['pc_id'] == pc_id
              and before['generation'] == after['generation']
              and not re.search(r'Guru Meditation|assert failed|abort\(\)|Task watchdog|SESSION FAIL', text),
              'Device session changed or crashed')
        acoustic = None
        segments = []
        cancelled = cancelled_response_ids(ev)
        try:
            assert references and first_bytes, 'Missing live TTS PCM/send evidence'
            for reference_event in references:
                reference = Path(reference_event['path'])
                index = reference_event['index']
                sent = next(e for e in first_bytes if e['index'] == index)
                response_id = reference_event.get('response_id')
                segment = dict(index=index, response_id=response_id, verified=False,
                               complete=False, audible_ms=None, cancelled=response_id in cancelled,
                               source_path=str(reference))
                segments.append(segment)
                if segment['cancelled']:
                    segment.update(cancelled_segment(out/'turn-board.wav', reference, sent['t'], mic_started,
                                                    start, response_model, ev, chain='board'))
                    continue
                match = acoustic_match(out/'turn.wav', reference, max(0, sent['t']-rec.ready_at),
                    rec.ready_at, start-rec.ready_at-.1,
                    reference_pcm=predict(pcm(reference), response_model, 'jabra'), server_send_at=sent['t'])
                board_match = acoustic_match(out/'turn-board.wav',reference,max(0,sent['t']-mic_started),
                    mic_started,start-mic_started,reference_pcm=predict(pcm(reference),response_model,'board'),
                    server_send_at=sent['t'])
                at=round(match['offset_s']*16000)
                level=jabra_level_gate(pcm(out/'turn.wav')[at:at+len(pcm(reference))],match['rms_rise_db'] if match['acoustic_timing_available'] else None,response_model,target_dbfs=getattr(board,'agent_playback_target',None))
                decode=decoded_match(decoded,pcm(reference))
                if not match['acoustic_timing_available']:
                    # Content and calibrated level stay gated; baseline rise/timing are unavailable.
                    level=jabra_level_gate(pcm(out/'turn.wav')[at:at+len(pcm(reference))],10,response_model,target_dbfs=getattr(board,'agent_playback_target',None))
                    level['rms_rise_db']=None
                    level['baseline_rise_available']=False
                verified = match['correlation'] >= .6 and board_match['correlation'] >= .6 and level['passed'] and decode['passed']
                segment.update(match, board=board_match, jabra_level=level, decoded_content=decode, verified=verified, complete=verified,
                               audible_ms=1000*(match['end']-match['onset']))
            row['tts_segments'] = segments
            complete = [r for r in segments if not r['cancelled']]
            if complete:
                acoustic = complete[0]; row['reply'] = acoustic
                row['reply_board'] = acoustic['board']
            deltas = playback_counter_deltas(before, after)
            check('playback', bool(complete) and all(r['verified'] for r in complete)
                  and all(v > 0 for v in deltas.values()), dict(segments=segments, counters=deltas))
        except Exception as exc:
            row['tts_segments'] = segments
            check('playback', False, f'{type(exc).__name__}: {exc}')
        check('one_reply', one_reply_matches(ev, segments),
              dict(generations=len(generations), responses=len(responses), cancelled=len(cancelled), segments=segments))
        gates = [(t, 'closed' if 'GATE closed' in l else 'open') for _, t, l in lines
                 if re.search(r'GATE (closed|open) t=', l)]
        row['gate_events'] = gates
        if any(t>row['reply_window_transport']['end'] for t,state in gates) or any(e['t']>row['reply_window_transport']['end'] for e in first_bytes):
            row['reply_window_transport'].update(after=after,end=time.monotonic(),
                deltas=row['transport_deltas'],
                basis='Later reply/gate event outlasted early gate-reopen snapshot; retained whole-turn bracket covers delayed output')
        classify_turn(row)
        row['mute_evidence'] = mute_evidence(ev,gates)
        closed, opened = reply_gate_window(gates)
        verified_acoustic = acoustic if row['checks'].get('playback') else None
        timing_acoustic=verified_acoustic if verified_acoustic and verified_acoustic.get('acoustic_timing_available',True) else None
        try:
            assert closed and opened, 'No closed/reopened gate'
            row['leakage'] = leakage(lines, timing_acoustic['onset'] if timing_acoustic else None,
                                     max(r['end'] for r in segments if not r['cancelled']) if timing_acoustic else None)
            assert row['acoustic_scoring_eligible'], 'Transport gate: '+str(row['reply_transport_gate'])
            if verified_acoustic and not verified_acoustic.get('acoustic_timing_available',True):
                row['acoustic_timing_available']=False
                check('gate', None, verified_acoustic.get('acoustic_timing_reason'))
            assert verified_acoustic, 'Acoustic timing unavailable; closed-gate PCM silence remains verified separately'
            assert not verified_acoustic.get('acoustic_timing_available',True) or (closed <= verified_acoustic['onset']+.1 and opened >= max(r['end'] for r in segments if not r['cancelled'])-.1), 'Gate does not cover audible reply'
            if overlap:
                assert closed <= row['overlap']['start'] < row['overlap']['end'] <= opened, 'Overlap not wholly within closed gate'
                assert not any(e['t'] >= row['overlap']['start'] for e in starts + finals), 'Overlap produced additional VAD/STT'
                check('gate', True if verified_acoustic.get('acoustic_timing_available',True) else None, '')
            else:
                check('gate', True if verified_acoustic.get('acoustic_timing_available',True) else None, '')
        except Exception as exc:
            check('gate', False, f'{type(exc).__name__}: {exc}')
        try:
            assert segments, 'No retained reply window'
            recorded = pcm(out/'turn-board.wav')
            # Keep complete matched TTS references. Activity bounds locate
            # timing but can truncate quiet consonants; prompt audio is excluded.
            row['board_stt']=transcribe_board_responses(out,recorded,segments,definition)
            acoustic_responses=row['board_stt']['responses']
            row['acoustic_responses'] = acoustic_responses
            row['acoustic_text'] = ' '.join(r['text'] for r in acoustic_responses)
            row['acoustic_content_basis']='Raw board microphone reply STT; mismatch informational when decoded and board correlations pass; Jabra STT informational'
            try:
                jabra=pcm(out/'turn.wav')
                jabra_parts=[r for r in segments if not r['cancelled'] and r['verified']]
                write_pcm(out/'reply-jabra-informational.wav',reference_audio(jabra,jabra_parts,'jabra'))
                row['jabra_transcript_informational']=transcribe_acoustic(out/'reply-jabra-informational.wav')
            except Exception as exc:row['jabra_transcript_informational']=f'Unavailable: {type(exc).__name__}: {exc}'
            row['acoustic_stt_passed']=bool(acoustic_responses) and all(r['correct'] for r in acoustic_responses)
            check('acoustic_content', acoustic_content_verdict(row['acoustic_stt_passed'],segments),
                  acoustic_responses)
        except Exception as exc:
            row['board_stt']=dict(available=False,passed=None,reason=f'{type(exc).__name__}: {exc}')
            check('acoustic_content', acoustic_content_verdict(False,segments), f'{type(exc).__name__}: {exc}')
        if row['acoustic_scoring_eligible'] and row['checks'].get('acoustic_content') is False and references:
            controls = []
            for reference in references:
                if reference.get('response_id') in cancelled: continue
                controls.append(dict(path=reference['path'], text=transcribe_acoustic(Path(reference['path']))))
            row['tts_control_transcripts'] = controls
            control_text = ' '.join(r['text'] for r in controls)
            row['tts_control_passed'] = answer_matches(control_text, definition)
            for failure in row['failures']:
                if failure['check'] == 'acoustic_content' and row['checks'].get('llm_content'):
                    failure['cause'] = 'acoustic/transport' if row['tts_control_passed'] else 'TTS pronunciation or ASR bias'
        from playback_quality import counter_damage
        row['transport_damaged']=counter_damage({k:row['transport_deltas'].get(k) for k in TRANSPORT_COUNTERS})
        from uplink_continuity import score_uplink
        evidence_path=Path(__import__('os').environ.get('RTP_EVIDENCE_DIR',str(bot.out/'rtp')))/'rtp.jsonl'
        score_end=time.monotonic()-.1
        row['uplink_window']=dict(start_s=start,end_s=score_end)
        packets=[json.loads(line) for line in evidence_path.read_text().splitlines()] if evidence_path.exists() else []
        all_events=bot.events()
        row['uplink_continuity']=score_uplink(packets,all_events,pc_id,start,score_end)
        row['llm_first_token_uplink']=[score_uplink(packets,all_events,pc_id,e['t']-.1,e['t']+1.5) for e in first_tokens]
        row['uplink_receive_boundary']='aiortc RTP handler, after ICE queueing and SRTP processing'
        socket_path=evidence_path.with_name('socket-rtp.jsonl')
        if socket_path.exists():
            from step8e_analysis import authenticated_socket_packets
            socket_packets=authenticated_socket_packets(
                [json.loads(line) for line in socket_path.read_text().splitlines()], packets)
            row['uplink_udp_callback']=score_uplink(socket_packets,all_events,pc_id,start,score_end)
            row['llm_first_token_udp_callback']=[score_uplink(socket_packets,all_events,pc_id,e['t']-.1,e['t']+1.5) for e in first_tokens]
            row['udp_callback_boundary']='asyncio UDP callback, before ICE queueing; matched authenticated RTP headers'
        row['uplink_transport_damaged']=(None if row['uplink_continuity']['passed'] is None else not row['uplink_continuity']['passed'])
        row['uplink_at_token_passed']=bool(row['llm_first_token_uplink']) and all(r['passed'] for r in row['llm_first_token_uplink'])
        row['reply_window_transport_damaged']=counter_damage({k:row['reply_window_transport']['deltas'].get(k) for k in TRANSPORT_COUNTERS})
        row['turn_policy_results'] = {k:row['checks'].get(k) for k in ('vad','stt','llm_content')}
        row['turn_policy_results']['one_uncancelled_complete_generation'] = len(responses)==1 and len(generations)==1+len(cancelled)
        if row['reply_window_transport_damaged']:
            for failure in row['failures']:
                if failure['check'] in ('playback','gate','acoustic_content') or (
                        failure['check']=='one_reply' and not cancelled and row['turn_policy_results']['one_uncancelled_complete_generation']):
                    failure['underlying_cause'] = failure['cause']
                    failure['cause'] = 'transport'
        row['cancelled_responses'] = [dict(response_id=i,
            audible_ms=cancelled_audio_duration(ev,segments,i))
            for i in cancelled]
        row['input_levels'] = [e for e in ev if e['event']=='input-level']
        correct_onsets = [r['onset'] for r in row.get('acoustic_responses',[]) if r['correct']] if timing_acoustic else []
        row['time_to_correct_reply_ms'] = 1000*(min(correct_onsets)-end) if correct_onsets else None
        times = dict(prompt_end=end, vad_stop=stops[0]['t'] if stops else None,
            stt_final=final_before_inference(ev)[0],
            llm_start=generations[0]['t'] if generations else None,
            llm_first_token=first_tokens[0]['t'] if first_tokens else None,
            tts_first_byte=first_bytes[0]['t'] if first_bytes else None,
            audible_reply=timing_acoustic['onset'] if timing_acoustic else None)
        row['late_finals'] = final_before_inference(ev)[1]
        row['timestamps'] = times
        row['latency_ms'] = {k: 1000*(v-end) if v is not None else None for k, v in times.items() if k != 'prompt_end'}
        sequence = list(times)
        row['stage_latency_ms'] = {sequence[i-1]+' -> '+sequence[i]:
            1000*(times[sequence[i]]-times[sequence[i-1]])
            if times[sequence[i]] is not None and times[sequence[i-1]] is not None else None
            for i in range(1, len(sequence))}
        row['stage_latency_ms']['stt_final -> llm_first_token'] = (
            1000*(times['llm_first_token']-times['stt_final'])
            if times['llm_first_token'] is not None and times['stt_final'] is not None else None)
        row['timing_basis'] = 'Host monotonic ALSA completion; Jabra AEC suppresses its prompt. VAD can stop before ALSA completion. Acoustic onset from independent-response TTS correlation.'
    except Exception as exc:
        row['failures'].append(dict(check='harness', cause='harness', detail=f'{type(exc).__name__}: {exc}'))
    if not row.get('board_stt',{}).get('available') and (out/'turn-board.wav').exists() and row.get('prompt_end') is not None:
        try:
            recorded=pcm(out/'turn-board.wav')
            begin=max(0,round((row['prompt_end']-row['microphone_recording_started']+.1)*16000))
            path=out/'reply-board-unverified-window.wav';write_pcm(path,recorded[begin:])
            text=transcribe_acoustic(path)
            row['board_stt']=dict(available=True,passed=answer_matches(text,definition),text=text,
                basis='Post-prompt board recording; reply timing unverified; independent of strict transport verdict')
        except Exception as exc:
            row['board_stt']=dict(available=False,passed=False,error=f'{type(exc).__name__}: {exc}')
    if getattr(board,'agent_trace',False):
        try:
            from arrival_trace import drain
            drain(board,out/'arrivals.jsonl')
            from step8e_analysis import uplink_gap_attribution
            trace_records=[json.loads(s) for s in (out/'arrivals.jsonl').read_text().splitlines()]
            row['llm_first_token_gap_attribution']=[uplink_gap_attribution(trace_records,
                [r for r in packets if r['pc_id']==pc_id],e['t']-.1,e['t']+1.5) for e in first_tokens]
        except Exception as exc:
            row['failures'].append(dict(check='trace',cause='harness',detail=f'{type(exc).__name__}: {exc}'))
    row['passed'] = len(row['checks']) == len(CHECKS) and all(v is not False for v in row['checks'].values()) and not row['failures']
    if not row['passed'] or not row.get('uplink_at_token_passed',False):
        from failure_snapshot import capture_failure
        row['failure_snapshot']=capture_failure(board,out,row['failures'] or 'Uplink continuity gate failed')
    write_json(out/'result.json', row)
    print(definition['id'], 'PASS' if row['passed'] else json.dumps(row['failures']), flush=True)
    return row


def soak_checks(result, minutes):
    obs = result['observations']; endpoints = [r['perf'] for r in obs] + [result['closed']]
    trends = heap_trends(obs)
    memory = transient_memory(obs, result['closed'], result.get('faults', []))
    result.update(memory)
    margins = [t.get('stack_margin_percent') for r in obs for t in r['perf'].get('tasks', [])]
    closed = closed_stack_margins(result['closed']); margins.extend(closed.values())
    result.update(heap_trends=trends, closed_stack_margins=closed,
        unknown_stack_sizes=closed_stack_unknown_sizes(result['closed']) +
            [t['name'] for r in obs for t in r['perf'].get('tasks', []) if t.get('stack_margin_percent') is None],
        unknown_heap_windows=unknown_heap_windows(trends),
        heap_growth_limit_bytes_per_minute=64)
    return dict(duration=result['elapsed_s'] >= minutes*60,
        heap_transient_depth=memory['heap_transient_depth_passed'],
        reconnect_transient_budget=memory['reconnect_transient_passed'],
        heap_growth_rate=growth_verdict(trends),
        stack_margins=bool(margins) and not result['unknown_stack_sizes'] and all(v is not None and v >= 25 for v in margins),
        decode_errors=all(p['decode_errors'] == 0 for p in endpoints),
        i2s_underflows=all(p['i2s_underflows'] == 0 for p in endpoints),
        faults=len(result.get('faults', [])) == result.get('expected_faults',0 if result.get('fault_free') else 3),
        turns=len(result['turns']) == minutes*3,
        conversation=all(r['passed'] for r in result['turns']))


FORCED_RECONNECT_SECONDS=360

def soak_schedule(minutes,forced_reconnect,tail_seconds=QUALIFICATION_MEMORY_TAIL_SECONDS,*,fault_free=True):
    from soak_memory import SETTLE_MINUTES,MINIMUM_FIT_SPAN_MINUTES
    assert tail_seconds>=0, 'Negative memory tail'
    duration=minutes*60+tail_seconds
    faults=((FORCED_RECONNECT_SECONDS,'reconnect'),) if forced_reconnect else () if fault_free else ((120,'reconnect'),(300,'server-restart'),(480,'reconnect'))
    boundaries=[0]+[due for due,_ in faults]+[duration]
    assert all(0<due<duration for due,_ in faults), 'Reconnect outside soak'
    assert any(b-a>=SETTLE_MINUTES*60+60 for a,b in zip(boundaries,boundaries[1:])), 'No generation can settle and provide a complete window'
    if forced_reconnect:assert faults[0][0]>=SETTLE_MINUTES*60+60, 'Reconnect precedes first settled window'
    pooled=duration-10*len(faults)-(len(faults)+1)*SETTLE_MINUTES*60
    # Three-fault transport soaks report growth as unknown and claim no fit.
    if forced_reconnect or fault_free:
        assert pooled>=MINIMUM_FIT_SPAN_MINUTES*60+120, 'Pooled heap growth fit infeasible (including 120 s sampling margin)'
    return faults


def run(a):
    credentials(); manifest = validate()
    turn_bank=getattr(a,'turn_definitions',TURNS)
    out = a.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    result = dict(mode='voice', port=7861, spl=a.spl, fixture_manifest=manifest, fault_free=getattr(a, 'fault_free', False),
                  passed=False, turns=[], conversations=[], observations=[], faults=[])
    memory_tail=getattr(a,'memory_tail_seconds',QUALIFICATION_MEMORY_TAIL_SECONDS)
    forced_schedule=soak_schedule(a.soak_minutes,getattr(a,'forced_reconnect',False),memory_tail,
        fault_free=result['fault_free']) if a.soak_minutes else ()
    result['memory_tail_seconds']=memory_tail if a.soak_minutes else 0
    result['forced_reconnect_schedule_s']=[due for due,_ in forced_schedule]
    result['expected_faults']=1 if getattr(a,'forced_reconnect',False) else 0 if result['fault_free'] else 3
    result['scripted_turn_ids']=[d['id'] for d in turn_bank]
    source_dir = out/'source'; source_dir.mkdir()
    source_files = ('hil/agent_conversation_test.py', 'hil/agent_prompts.py',
                    'hil/alsa.py', 'hil/playback_quality.py', 'hil/step8e2b_analysis.py',
                    'hil/netem.py', 'hil/ethernet_bench.py', 'hil/step8e_soak.py',
                    'server/bot.py', 'server/voice_audit.py', 'server/settings.py', 'hil/soak_memory.py',
                    'hil/voice_mute_evidence.py')
    result['source_sha256'] = {}
    for name in source_files:
        data = (ROOT/name).read_bytes()
        (source_dir/Path(name).name).write_bytes(data)
        result['source_sha256'][name] = hashlib.sha256(data).hexdigest()
    def save():
        result['summary'] = summarize(result['turns'])
        write_json(out/'result.json', result)
    import os
    evidence_env={'RTP_EVIDENCE_DIR':str(out/'bot/rtp'),
                  'RTP_EVIDENCE_PAYLOAD_BYTES':'1',
                  'RTP_EVIDENCE_MAX_PACKETS':str(max(100000,math.ceil(((a.soak_minutes or 0)*60+result['memory_tail_seconds'])*60))),
                  'AUDIO_OUT_AUTO_SILENCE':'1'}
    previous_evidence_env={k:os.environ.get(k) for k in evidence_env}
    os.environ.update(evidence_env)
    result['rtp_evidence_packet_cap']=int(evidence_env['RTP_EVIDENCE_MAX_PACKETS'])
    try:
        from alsa import record_room_baseline
        result['room_baseline'] = record_room_baseline(out/'room-baseline', out.name)
        save()
        if result['room_baseline']['noise_contaminated']:
            result['acoustic_hold'] = 'noise'
            raise RuntimeError('Noise-contaminated room baseline; acoustic arm stopped before playback')
        card, device = resolve_jabra(); set_volumes(card, out/'mixer.log')
        with BotServer(out/'bot', 'voice') as bot, EspSerial(a.port, log_path=out/'serial.log',display_checks=bool(getattr(a,'display',None))) as board:
            try:
                board.agent_mic_gain = getattr(a, 'mic_gain', 1600)
                board.agent_session_start=getattr(a,'session_start',None)
                result['mic_gain_percent'] = board.agent_mic_gain
                board.agent_playback_target=getattr(a,'playback_target',None)

                result['voice_policy'] = __import__('os').getenv('BOT_VOICE_POLICY', 'smartturn')
                result['elf_sha256'] = boot(board, a.elf)
                if getattr(a,'display',None):
                    from step9a_support import setup
                    result['display_setup']=setup(board,mode=a.display)
                    if getattr(a,'display_concurrent_persist',False):
                        from step9a_support import FIXTURES,request,terminal,host_address
                        image=json.loads((FIXTURES/'fetch-fixture.json').read_text())
                        board.command('disp defer-persistence 1');board.command('disp call-persistence 1')
                        request(board,'delete',{'seq':1000000,'hash':image['hash']},'soak-persist-remove');assert terminal(board,'soak-persist-remove')['error']=='none'
                        request(board,'load',{'seq':2000000,'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},'soak-persist-ram')
                        assert terminal(board,'soak-persist-ram')['error']=='none'
                        result['display_persist_fixture']=image
                board.command('wifi-log 1')
                if board.agent_playback_target is None:
                    board.command('stats')
                    observed=board.latest_entry(r'LOUDNESS target_dbfs=')
                    if not observed:raise RuntimeError('Playback target observation unavailable')
                    board.agent_playback_target=int(re.search(r'target_dbfs=(-?\d+)',observed[2])[1])
                result['playback_target_dbfs']=board.agent_playback_target
                definitions = calibration_definitions(interruptions=a.interruptions,
                    pause_case=getattr(a,'pause_case',False))
                for definition in turn_bank:
                    if not any(d['id']==definition['id'] for d in definitions):definitions.append(definition)
                if a.calibration_from:
                    prior = json.loads((a.calibration_from.resolve()/'result.json').read_text())
                    assert prior['spl'] == a.spl and prior['elf_sha256'] == result['elf_sha256']
                    calibration = prior['calibration']
                    missing = [d for d in definitions if d['id'] not in calibration['prompts']]
                    if missing:
                        extra = calibrate_bank(board,device,out/'extra-calibration',a.spl,missing)
                        calibration['prompts'].update(extra['prompts'])
                        calibration['extra_reference_bench'] = extra['bench']
                    for definition in definitions:
                        row = calibration['prompts'][definition['id']]
                        assert row['passed'] and abs(row['estimated_spl']-a.spl) <= 1
                        assert row['source_sha256'] == hashlib.sha256((BANK/(definition['id']+'.wav')).read_bytes()).hexdigest()
                        assert Path(row['path']).is_file()
                    result['calibration'] = calibration
                    result['calibration_reused_from'] = str(a.calibration_from.resolve())
                else:
                    result['calibration'] = calibrate_bank(board, device, out/'calibration', a.spl,definitions)
                pc_id = connect(board)
                model = calibrate_response(board, device, out/'response-calibration')
                result['response_calibration'] = model
                if getattr(board,'agent_trace',False):
                    from arrival_trace import drain
                    drain(board,out/'calibration-arrivals.jsonl')
                if getattr(a,'after_calibration',None):
                    a.after_calibration()
                board.command('audio 1'); board.command('quiet 0')
                prompts = result['calibration']['prompts']
                save()
                if a.soak_minutes:
                    # A real warmup turn precedes timed snapshot baseline.
                    result['warmup_turn'] = run_turn(board, bot, device, out/'warmup', TURNS[0], prompts['france'], model, pc_id)
                    if getattr(a,'display',None):
                        from step9a_support import read
                        result['display_timed_before']=read(board,'status','DISPLAY')
                    started = time.monotonic(); next_sample = 0; done_faults = set();persist_posted=False
                    def observe():
                        snap = snapshot(board, active=True, allow_incomplete=True)
                        snap['elapsed_s'] = time.monotonic()-started
                        snap['ready_elapsed_s'] = board.agent_ready_at-started
                        result['observations'].append(snap)
                        board.command('wifi-stats',10)
                        print(f'PERF t={snap["elapsed_s"]:.1f}s', flush=True); save()
                    for index in range(a.soak_minutes*3):
                        time.sleep(max(0, started+index*20-time.monotonic()))
                        if getattr(a,'display_concurrent_persist',False) and not persist_posted and time.monotonic()-started>=120:
                            from step9a_support import request
                            result['display_persist_posted_device_cursor']=board.cursor
                            request(board,'persist',{'seq':1000000,'hash':result['display_persist_fixture']['hash']},'soak-concurrent-persist')
                            result['display_persist_posted_s']=time.monotonic()-started;persist_posted=True
                        if time.monotonic()-started >= next_sample:
                            observe(); next_sample += 60
                        for due, kind in forced_schedule:
                            if time.monotonic()-started < due or due in done_faults:
                                continue
                            observe(); began = time.monotonic(); cur = board.cursor; previous_pc = pc_id
                            if kind == 'reconnect':
                                board.command('audio 0'); board.command('disconnect', 40)
                                pc_id = connect(board)
                            else:
                                pc_id = restart_server(board, bot)
                            result['faults'].append(dict(kind=kind, scheduled_s=due,
                                started_s=began-started, recovered_s=time.monotonic()-started,
                                duration_s=time.monotonic()-began, previous_pc_id=previous_pc, pc_id=pc_id))
                            done_faults.add(due); observe()
                        definition = TURNS[index % len(TURNS)]
                        row = run_turn(board, bot, device, out/f'turn{index+1:03}', definition, prompts[definition['id']], model, pc_id)
                        row.update(scheduled_s=index*20, started_s=row.get('prompt_start', time.monotonic())-started)
                        result['turns'].append(row); save()
                    if getattr(a,'display_concurrent_persist',False):
                        events=[json.loads(line.split('DISPLAY_WORK_RESULT ',1)[1]) for line in (out/'serial.log').read_text().splitlines() if 'DISPLAY_WORK_RESULT ' in line]
                        matches=[e for e in events if e['id']=='soak-concurrent-persist'];assert len(matches)==1 and matches[0]['error']=='none' and matches[0]['persisted']
                        result['display_concurrent_persist']=matches[0]
                    time.sleep(max(0, started+a.soak_minutes*60-time.monotonic()))
                    observe()
                    # Keep the connection alive and sample without more voice
                    # prompts; this supplies the predeclared pooled growth span.
                    tail_started=time.monotonic();display_idle_started=False;display_idle_finished=False
                    tail_trace=bool(getattr(a,'display',None) and getattr(board,'agent_trace',False));next_tail_sample=30
                    if tail_trace:
                        from arrival_trace import drain
                        drain(board,out/'tail-arrivals.jsonl')
                    while time.monotonic()-tail_started<memory_tail:
                        time.sleep(min(10 if tail_trace else 30,max(0,memory_tail-(time.monotonic()-tail_started))))
                        elapsed=time.monotonic()-tail_started
                        if getattr(a,'display_idle_probe',False):
                            if elapsed>=30 and not display_idle_started:board.command('audio 0');display_idle_started=True;result['display_idle_probe_start_s']=time.monotonic()-started
                            elif elapsed>=60 and not display_idle_finished:board.command('audio 1');display_idle_finished=True;result['display_idle_probe_end_s']=time.monotonic()-started
                        if tail_trace:drain(board,out/'tail-arrivals.jsonl')
                        if not tail_trace or elapsed>=next_tail_sample or elapsed>=memory_tail:
                            observe();next_tail_sample+=30
                    if getattr(a,'display',None):
                        from step9a_support import read
                        result['display_final']=read(board,'status','DISPLAY');board.command('disp bench off')
                    result['elapsed_s'] = time.monotonic()-started
                    from failure_snapshot import capture_failure
                    result['pre_teardown_snapshot']=capture_failure(board,out/'pre-teardown',
                        'Soak resource verdict requires closed-state counters; capture active device first')
                    board.command('audio 0'); board.command('disconnect', 40)
                    result['closed'] = perf(board)
                    windows=[]
                    # Long captures outlive the bounded in-memory serial ring;
                    # the durable serial log retains every intermediate reset.
                    for line in (out/'serial.log').read_text().splitlines():
                        if 'PERF {' in line:
                            try:windows.append(json.loads(line.split('PERF ',1)[1]))
                            except json.JSONDecodeError:
                                if getattr(a,'display',None):raise
                                continue
                    write_json(out/'heap-windows.json',windows)
                    merged=attach_heap_intervals(result['observations']+[dict(perf=result['closed'])],windows)
                    result['observations']=merged[:-1];result['closed']['heap_interval']=merged[-1].get('heap_interval')
                    packet_path=out/'bot/rtp/rtp.jsonl'
                    packets=[json.loads(line) for line in packet_path.read_text().splitlines()] if packet_path.exists() else []
                    result['observations']=host_uplink_gap_windows(result['observations'],packets)
                    result['strict_transport_column']=dict(scored=len(result['turns']),
                        clean=sum(r.get('reply_transport_gate',{}).get('passed') is True for r in result['turns']))
                    result['soak_checks'] = soak_checks(result, a.soak_minutes)
                    result['passed'] = all(result['soak_checks'].values())
                elif a.restart_regression:
                    first = run_turn(board, bot, device, out/'before-restart', TURNS[0], prompts['france'], model, pc_id)
                    result['turns'].append(first); save()
                    began = time.monotonic(); pc_id = restart_server(board, bot)
                    result['restart_recovery_s'] = time.monotonic()-began
                    for definition in TURNS[:2]:
                        result['turns'].append(run_turn(board, bot, device, out/('after-'+definition['id']),
                            definition, prompts[definition['id']], model, pc_id)); save()
                    result['passed'] = all(r['passed'] for r in result['turns'])
                elif a.interruptions:
                    row = run_turn(board, bot, device, out/'overlap', EXTRA[0], prompts['long'], model, pc_id,
                                    recording_seconds=35, overlap=prompts['addition'])
                    result['turns'].append(row); save()
                    recovery = run_turn(board, bot, device, out/'recovery', TURNS[0], prompts['france'], model, pc_id)
                    result['turns'].append(recovery)
                    result['experience'] = 'Speech wholly inside speaker playback is ignored. Repeat the question after the gate reopens.'
                    result['passed'] = row['passed'] and recovery['passed']
                else:
                    cold_count = max(1, a.runs//2)
                    for run_index in range(a.runs):
                        temperature = 'cold' if run_index < cold_count else 'warm'
                        if 0 < run_index < cold_count:
                            board.command('audio 0'); board.command('disconnect', 40)
                            boot(board, a.elf); pc_id = connect(board)
                        conversation = dict(index=run_index+1, temperature=temperature, pc_id=pc_id, turns=[])
                        for turn_index, definition in enumerate(turn_bank):
                            if getattr(a,'turn_limit',None) and len(result['turns'])>=a.turn_limit:break
                            row = run_turn(board, bot, device, out/f'run{run_index+1:02}'/f'turn{turn_index+1}',
                                            definition, prompts[definition['id']], model, pc_id,
                                            recording_seconds=getattr(a,'recording_seconds',13))
                            row.update(conversation=run_index+1, temperature=temperature)
                            conversation['turns'].append(row); result['turns'].append(row); save()
                        conversation['passed'] = all(r['passed'] for r in conversation['turns'])
                        result['conversations'].append(conversation); save()
                        if getattr(a,'turn_limit',None) and len(result['turns'])>=a.turn_limit:break
                    if getattr(a, 'pause_case', False):
                        definition = EXTRA[1]
                        result['turns'].append(run_turn(board, bot, device, out/'pause-case', definition,
                            prompts['pause'], model, pc_id, recording_seconds=18)); save()
                    result['passed'] = all(c['passed'] for c in result['conversations']) and all(r['passed'] for r in result['turns'])
            finally:
                board.display_cleanup=True
                for cmd in ('audio 0','gate auto','quiet 0','wifi-log 0','disconnect'):
                    try:board.command(cmd,40)
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                if getattr(board,'agent_trace',False):
                    # The transport-owned ring survives Session teardown. Keep
                    # the final idle/release tail instead of ending at the last
                    # turn's drain; these records retain their generation tags.
                    try:
                        from arrival_trace import drain
                        drain(board,out/'cleanup-arrivals.jsonl')
                    except Exception as exc:
                        result.setdefault('cleanup_errors',[]).append('Final trace drain: '+str(exc))
                if 'display_persist_fixture' in result:
                    try:
                        from step9a_support import request,terminal
                        request(board,'delete',{'seq':3000000,'hash':result['display_persist_fixture']['hash']},'soak-persist-cleanup')
                        assert terminal(board,'soak-persist-cleanup')['error']=='none'
                        board.command('disp call-persistence 0');board.command('disp defer-persistence 0')
                    except Exception as exc:result.setdefault('cleanup_errors',[]).append(str(exc))
                if getattr(a,'display',None):
                    result['display_monitor']=board.display_monitor.result();board.display_monitor.check()
        if getattr(a,'display',None):
            from step9a_r2_checks import audit
            result['raw_display_audit']=audit((out/'serial.log').read_bytes())
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}';result['passed']=False
    finally:
        for k,v in previous_evidence_env.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        save()
    print(json.dumps(dict(passed=result['passed'], turns=result['summary']['turns'],
                         passes=result['summary']['passes'], error=result.get('error'))), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True); p.add_argument('--port', default='/dev/ttyACM0')
    p.add_argument('--runs', type=int, default=1); p.add_argument('--spl', type=float, choices=(65, 58), default=65)
    p.add_argument('--elf', type=Path, default=ROOT/'build/dg-esp32.elf')
    p.add_argument('--turn-limit',type=int,help='Stop after this many original conversation turns')
    p.add_argument('--mic-gain', type=int, default=1600)
    p.add_argument('--fault-free', action='store_true')
    p.add_argument('--pause-case', action='store_true')
    p.add_argument('--calibration-from', type=Path, help='Reuse same-image/SPL bench measurements without moving the board')
    p.add_argument('--compare-old-bin',type=Path,help='Interleave --runs conversations per level with an archived image')
    p.add_argument('--compare-old-elf',type=Path)
    mode = p.add_mutually_exclusive_group(); mode.add_argument('--soak-minutes', type=int); mode.add_argument('--interruptions', action='store_true')
    mode.add_argument('--restart-regression', action='store_true')
    p.add_argument('--memory-tail-seconds',type=float,default=QUALIFICATION_MEMORY_TAIL_SECONDS)
    p.add_argument('--playback-target',type=int,help='RAM-only normalized playback target for a matched level comparison; ceiling 26%')
    a = p.parse_args()
    if a.playback_target is not None and not -40<=a.playback_target<=-12:p.error('Playback target outside firmware range')
    if a.turn_limit is not None and (a.turn_limit<1 or a.turn_limit>a.runs*6 or a.soak_minutes):p.error('--turn-limit must be positive, within --runs*6, and cannot limit a soak')
    if a.runs < 1: p.error('--runs must be positive')
    if a.soak_minutes is not None and a.soak_minutes not in (10,20,45): p.error('Soak must be 10 (transport check), 20 or 45 minutes')
    if bool(a.compare_old_bin)!=bool(a.compare_old_elf):p.error('Both old BIN and ELF are required')
    if a.compare_old_bin:
        if a.soak_minutes or a.interruptions or a.restart_regression:p.error('Image comparison is a conversation-only campaign')
        from step8b2_agent_compare import run_comparison
        raise SystemExit(run_comparison(a))
    raise SystemExit(run(a))
