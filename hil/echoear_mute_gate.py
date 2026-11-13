"""Pure scoring for the mute-image gate (hil/plans/echoear-mute-gate-v1.json).

No hardware, socket, serial or bot access. The runner (echoear_rollback.py,
profile mute-01) supplies retained recorder rows, serial lines as
(index, host_monotonic_s, text) and PERF snapshots; everything here is a
function of those inputs and the frozen plan. Missing witnesses are never a
pass: they classify a cell INCOMPLETE (or the gate INVALID for a mode mismatch).
"""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np

from echoear_aec_checks import admission_counters
from echoear_aec_resources import check_snapshots

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'hil/plans/echoear-mute-gate-v1.json'
FALSE_TURN_FRAMES = ('UserStartedSpeakingFrame', 'InterruptionFrame')
FALSE_TURN_KINDS = ('aggregator-user-turn-started', 'aggregator-user-turn-stopped')
REPORT_FRAMES = ('InterimTranscriptionFrame', 'VADUserStartedSpeakingFrame', 'ProposedUserStartedSpeakingFrame')
FATAL = r'Guru Meditation|panic.ed|abort\(\)|CORRUPT HEAP|Stack canary'


def load_plan(path=PLAN):
    path = Path(path)
    return json.loads(path.read_text()), hashlib.sha256(path.read_bytes()).hexdigest()


def gate_samples(lines):
    out = []
    for _, host_s, text in lines:
        m = re.search(r'MIC_GATE t=(\d+) closed=(\d+) pre=(\S+) post=(\S+)', text)
        if m:
            out.append(dict(host_s=host_s, t=int(m[1]), closed=int(m[2]), post=float(m[4])))
    return out


def gate_open_after(lines, after_s):
    """Host receive time of the first device 'GATE open' after after_s."""
    for _, host_s, text in lines:
        if host_s > after_s and re.search(r'\bGATE open\b', text):
            return host_s
    return None


def band_db(samples, rate, band):
    """Band RMS in dBFS of int16 samples (FFT mask; no filter state)."""
    x = np.asarray(samples, dtype=float)/32768.0
    if len(x) < 64:
        return None
    spectrum = np.fft.rfft(x*np.hanning(len(x)))
    freqs = np.fft.rfftfreq(len(x), 1/rate)
    keep = (freqs >= band[0]) & (freqs <= band[1])
    power = np.sum(np.abs(spectrum[keep])**2)/np.sum(np.hanning(len(x))**2)/len(x)*2
    return 10*math.log10(power+1e-20)


def playback_witness(recording, plan, prompt_start_s, prompt_end_s, active_start_s, active_end_s):
    """recording: dict(samples, rate, start_interval_s=[lo, hi], healthy). Reply level rise over the room floor.

    Sample zero lies somewhere in the evidence-derived interval [lo, hi] (RawRecording.health, decision
    111(b): lo = spawn - drift allowance, hi = reap - written duration + drift allowance). Sample i is
    therefore at a real time in [lo + i/rate, hi + i/rate]. Every window keeps only samples that lie
    inside it for EVERY admissible origin: the floor never contains the prompt, the active window never
    starts before prompt end + min_gap, and required coverage is computed on those samples only.
    """
    spec = plan['echo_cell']['playback_witness']
    if not recording or recording.get('samples') is None:
        return dict(passed=False, missing=True, reason='no recording')
    if recording.get('healthy') is not True:
        return dict(passed=False, missing=True, reason='recording unhealthy', health=recording.get('health'))
    interval = recording.get('start_interval_s')
    if (not isinstance(interval, (list, tuple)) or len(interval) != 2 or not all(type(x) in (int, float) for x in interval)
            or interval[1] < interval[0] or interval[1]-interval[0] > spec['max_start_uncertainty_s']):
        return dict(passed=False, missing=True, reason='recording origin not bounded', start_interval_s=interval)
    lo, hi = interval
    active_start_s = max(active_start_s, prompt_end_s+spec['min_gap_after_prompt_s'])
    rate = recording['rate']
    s = recording['samples']
    def window(a, b):
        i = int(math.ceil((a-lo)*rate))      # earliest admissible time of sample i >= a
        j = int(math.floor((b-hi)*rate))     # latest admissible time of sample j <= b
        return s[i:j] if 0 <= i < j <= len(s) else None
    floor = window(prompt_start_s-.5, prompt_start_s)
    active = window(active_start_s, active_end_s)
    if floor is None or len(floor) < rate*.3 or active is None or len(active) < rate*.2:
        return dict(passed=False, missing=True, reason='recording does not cover floor/active windows for every admissible origin')
    f, a = band_db(floor, rate, spec['band_hz']), band_db(active, rate, spec['band_hz'])
    rise = None if f is None or a is None else a-f
    return dict(passed=rise is not None and rise >= spec['rise_db_min'], missing=False,
                floor_dbfs=f, active_dbfs=a, rise_db=rise, rise_db_min=spec['rise_db_min'],
                active_window_s=[active_start_s, active_end_s], start_interval_s=[lo, hi], uncertainty_s=hi-lo,
                floor_samples=len(floor), active_samples=len(active))


def keyword_groups_matched(text, groups):
    words = set(re.findall(r"[a-z0-9]+", (text or '').lower()))
    return sum(1 for alternatives in groups if words.intersection(a.lower() for a in alternatives))


def _frame(e):
    return e.get('frame')


def false_turn_events(rows, lo_ns, hi_ns):
    accepted, report = [], []
    for e in rows:
        if not lo_ns <= e['monotonic_ns'] <= hi_ns:
            continue
        if e.get('kind') in FALSE_TURN_KINDS or _frame(e) in FALSE_TURN_FRAMES or (
                _frame(e) == 'TranscriptionFrame' and (e.get('text') or '').strip()):
            accepted.append(e)
        elif _frame(e) in REPORT_FRAMES:
            report.append(e)
    return accepted, report


def gate_transitions(lines):
    out = []
    for _, host_s, text in lines:
        m = re.search(r'\bGATE (open|closed)\b', text)
        if m:
            out.append((host_s, m[1]))
    return out


def gate_transitions_device(lines):
    """(host receipt s, 'open'|'closed', device t us or None) for every GATE transition line."""
    out = []
    for _, host_s, text in lines:
        m = re.search(r'\bGATE (open|closed)\b(?:.*?\bt=(\d+))?', text)
        if m:
            out.append((host_s, m[1], int(m[2]) if m[2] else None))
    return out


def gate_check(lines, first_s, last_s, open_s, tail_end_s, spec):
    """Full-interval mic-gate witness: one rule for short and long replies (decisions 120-122).

    The mute image closes its gate on the RTVI bot-started-speaking message; the "GATE closed" serial line reaches
    the host 22-307 ms after BotStartedSpeaking in retained cells, sometimes after the first non-silent write, while
    nothing can be audible before playout prefill. Hence a declared close-witness allowance A = close_witness_
    allowance_s (= playback_lead_s, 0.5 s):
      - Close witness: among GATE transitions RECEIVED at or before first write + A, the latest must be 'closed';
        the closed run starts at the first close after the last open (a duplicate close never moves it forward).
      - Positive evidence (FAIL), from the run's close onward: any MIC_GATE sample received in [first write, hi]
        whose DEVICE time is at or after the close's device time and is open (closed=0) or nonzero; any GATE open
        received in [first write, hi] (premature); a re-close after the final GATE open (flap).
      - Samples received before the close line AND with device time before the close are report-only (the device had
        not closed yet; at most A of the reply start goes unscored - the qualified pre-118 exposure). A sample that is
        post-close on either clock is evidence (fail-closed against an incoherent close t).
      - Witness window: transitions received at or before min(first write + A, hi); the reply's own ending GATE open
        is never the 'latest transition', and positive evidence is scanned over [first write, hi] regardless.
      - No close witness (none, or the latest transition by first write + A is an open, or the close came later than
        A): samples received after first write + A that are open/nonzero FAIL (the gate must be closed by then);
        otherwise INCOMPLETE ('gate close not witnessed within the allowance'), never PASS.
      - hi = just before the final GATE open (the device reopens only after playback drained), or the last write if
        none follows (then the cell lacks its GATE open witness and is INCOMPLETE).
    Coverage (min samples, max gap on the device clock, endpoint allowances) is checked over [max(first write, close
    receipt), hi]. Returns (failures, missing, record).
    """
    g = spec['gate_closed']
    allowance = g.get('close_witness_allowance_s', g['playback_lead_s'])
    failures, missing = [], []
    samples = gate_samples(lines)
    hi = max(last_s, open_s-1e-6) if open_s is not None else last_s
    # Fable r14 should-fix 2: the witness window is bounded by the reply, so the reply's own ending GATE open (received
    # inside the allowance) never masquerades as the latest transition.
    witnessed = [t for t in gate_transitions_device(lines) if t[0] <= min(first_s+allowance, hi)]
    close = None
    if witnessed and witnessed[-1][1] == 'closed':
        opens = [i for i, t in enumerate(witnessed) if t[1] == 'open']
        run = witnessed[(opens[-1]+1) if opens else 0:]
        close = run[0]
    received = [x for x in samples if first_s <= x['host_s'] <= hi]
    if close is not None and close[2] is not None:
        # Fable r14 should-fix 1 (fail-closed): a sample is evidence if its device time is at or after the close's OR
        # it was received at or after the close line; only a sample that is pre-close on BOTH clocks is report-only, so
        # a close with an incoherent (e.g. future) device t cannot silence the samples behind it.
        evidence = [x for x in received if x['t'] >= close[2] or x['host_s'] >= close[0]]
        report_only = [x for x in received if x['t'] < close[2] and x['host_s'] < close[0]]
    elif close is not None:                                  # a close line without device time: receipt order
        evidence = [x for x in received if x['host_s'] >= close[0]]
        report_only = [x for x in received if x['host_s'] < close[0]]
    else:
        evidence = [x for x in received if x['host_s'] > first_s+allowance]
        report_only = [x for x in received if x['host_s'] <= first_s+allowance]
    lo = max(first_s, close[0]) if close is not None else first_s+allowance
    record = dict(interval_s=[lo, hi], close_receipt_s=close[0] if close else None,
                  close_device_t=close[2] if close else None, allowance_s=allowance,
                  pre_close_report_only=dict(samples=len(report_only), open=sum(not x['closed'] for x in report_only)),
                  basis=('close witnessed' if close else 'no close within the allowance')+' -> GATE open')
    if any(not x['closed'] for x in evidence):
        failures.append('open mic sample during witnessed playback')
    if any(x['closed'] and x['post'] > g['post_max_dbfs'] for x in evidence):
        failures.append('nonzero closed mic sample during playback')
    if [h for _, h, text in lines if first_s <= h <= hi and re.search(r'\bGATE open\b', text)]:
        failures.append('premature GATE open during playback')
    if close is None:
        missing.append('gate close not witnessed within the allowance')
    if open_s is None:
        missing.append('no GATE open after the reply')
    inside = [x for x in samples if lo <= x['host_s'] <= hi]
    record['samples'] = len(inside)
    if hi <= lo:
        missing.append('no witnessed coverage before GATE open')
        return failures, missing, record
    gaps = [(b['t']-a['t'])/1e6 for a, b in zip(inside, inside[1:])]
    record.update(max_gap_s=max(gaps) if gaps else None)
    if (len(inside) < g['min_samples'] or inside[0]['host_s']-lo > g['endpoint_allowance_s']
            or hi-inside[-1]['host_s'] > g['endpoint_allowance_s'] or any(x > g['max_gap_s'] for x in gaps)):
        missing.append('mic-gate telemetry gap')
    if open_s is not None:
        # Fable r12 nit 3: hi trusts the device's first GATE open after the reply; a re-close (gate flap) before the
        # tail ends means the device saw playback again after that open, so that open did not end the playback.
        flap = [h for h, state in gate_transitions(lines) if open_s < h <= tail_end_s and state == 'closed']
        if flap:
            failures.append('mic gate re-closed after the reply GATE open (flap)')
            record['flap_s'] = flap[:5]
        after = [x for x in samples if open_s < x['host_s'] <= tail_end_s]
        if after and all(x['closed'] for x in after):
            failures.append('mic gate did not reopen after playback')
        elif not after:
            missing.append('no mic-gate sample after GATE open')
    return failures, missing, record


def contexts_check(rows, first_ns, stop_ns, hi_ns, writes):
    """Per-context TTS lifecycle (rollback per_turn_witnesses rule): every reply context starts
    before its first write and stops after its last; the three context sets are equal."""
    reply = [e for e in writes if first_ns <= e['monotonic_ns'] <= stop_ns]
    contexts = {e.get('context_id') for e in reply}
    starts = [e for e in rows if _frame(e) == 'TTSStartedFrame' and e.get('boundary') == 'tts-written' and e['monotonic_ns'] <= stop_ns]
    ends = [e for e in rows if _frame(e) == 'TTSStoppedFrame' and e.get('boundary') == 'tts-written' and first_ns <= e['monotonic_ns'] <= hi_ns]
    start_ctx = {e.get('context_id') for e in starts if e.get('context_id') in contexts}
    end_ctx = {e.get('context_id') for e in ends}
    ok = bool(contexts) and None not in contexts and contexts == start_ctx == end_ctx
    if ok:
        for c in contexts:
            ws = [e['monotonic_ns'] for e in reply if e.get('context_id') == c]
            ok &= any(e.get('context_id') == c and e['monotonic_ns'] <= min(ws) for e in starts)
            ok &= any(e.get('context_id') == c and e['monotonic_ns'] >= max(ws) for e in ends)
    return bool(ok), sorted(str(c) for c in contexts)


def score_echo_cell(rows, lines, recording, plan, prompt_id, prompt_start_s, prompt_end_s, end_s, stimulus_error=None,
                    retired=None):
    """One prompt -> reply -> tail cell: PASS / FAIL / INCOMPLETE.

    Witnessed functional failures are latched FIRST, from whatever evidence exists, and always
    win: a missing recording, an invalid stimulus or a truncated observation can never turn a
    witnessed failure into a retakeable INCOMPLETE.
    """
    cell = plan['echo_cell']
    aec = aec_mode(plan)        # AEC reconnect check: acoustic consequences are report-only, no mic gate
    # Decision 117: a deliberately disconnected peer's post-send rows are teardown evidence, never this cell's.
    rows, teardown = split_teardown(rows, retired)
    rows = sorted(rows, key=lambda e: e['monotonic_ns'])
    failures, missing, acoustic_report = [], [], []
    users = [e for e in rows if e.get('kind') == 'aggregator-user-turn-stopped' and (e.get('text') or '').strip()]
    assistants = [e for e in rows if e.get('kind') == 'aggregator-assistant-turn-stopped' and (e.get('text') or '').strip()]
    writes = [e for e in rows if e.get('kind') == 'tts-transport-write']
    loud = [e for e in writes if e.get('non_silent')]
    fatal = [e for e in rows if _frame(e) == 'FatalErrorFrame' or e.get('kind') in ('pipeline-error', 'gap')]
    provider = [e for e in rows if _frame(e) == 'ErrorFrame']
    first = min((e['monotonic_ns'] for e in loud), default=None)
    stops = [e for e in rows if _frame(e) == 'BotStoppedSpeakingFrame' and e.get('boundary') == 'tts-written'
             and first is not None and e['monotonic_ns'] >= first]
    stop_ns = min((e['monotonic_ns'] for e in stops), default=None)
    reply = [e for e in loud if stop_ns is None or e['monotonic_ns'] <= stop_ns]
    last = max((e['monotonic_ns'] for e in reply), default=None)
    open_s = gate_open_after(lines, last/1e9) if last is not None else None
    tail_end_s = open_s+cell['tail_after_gate_open_s'] if open_s is not None else end_s
    if aec and last is not None:
        # No mic gate in AEC mode: the tail is measured from the reply's BotStoppedSpeakingFrame.
        open_s = (stop_ns/1e9) if stop_ns is not None else None
        tail_end_s = open_s+cell['tail_after_gate_open_s'] if open_s is not None else end_s
    lo = first if first is not None else int(prompt_end_s*1e9)
    hi = int(min(tail_end_s, end_s)*1e9)
    # -- latched failures (independent of any missing witness) --
    if fatal:
        failures.append('fatal frame, pipeline error or recorder gap')
    if first is not None and any(e['monotonic_ns'] >= first for e in provider):
        failures.append('provider error after exposure began')
    # No reply output means no exposure: there is no echo window to scan (the prompt's own turn
    # precedes the first write by definition).
    accepted, report = false_turn_events(rows, lo, hi) if first is not None else ([], [])
    if accepted:
        (acoustic_report if aec else failures).append('accepted false turn or interruption in the reply/tail window')
    if any(e.get('drop') for e in writes if lo <= e['monotonic_ns'] <= hi):
        failures.append('dropped transport write')
    if stop_ns is not None:
        after_close = [e for e in loud if stop_ns < e['monotonic_ns'] <= hi]
        new_ctx = [e for e in rows if _frame(e) == 'TTSStartedFrame' and e.get('boundary') == 'tts-written' and stop_ns < e['monotonic_ns'] <= hi]
        if after_close or new_ctx:
            (acoustic_report if aec else failures).append('bot output after reply closure')
    if len(users) > 1 or len(assistants) > 1:
        (acoustic_report if aec else failures).append('more than one user or assistant turn')
    if len({e.get('pc_id') for e in users+assistants+loud}) > 1:
        failures.append('more than one peer')
    gate = None
    if first is not None and last is not None and not aec:
        gf, gm, gate = gate_check(lines, first/1e9, last/1e9, open_s, tail_end_s, cell)
        failures += gf; missing += gm
    ctx_ok, contexts = (False, [])
    if first is not None and stop_ns is not None:
        ctx_ok, contexts = contexts_check(rows, first, stop_ns, hi, writes)
        if not ctx_ok:
            failures.append('TTS context lifecycle incomplete')
    witness = None
    if first is not None and stop_ns is not None:
        witness = playback_witness(recording, plan, prompt_start_s, prompt_end_s, first/1e9+.3, stop_ns/1e9)
        if not witness.get('missing') and not witness['passed']:
            failures.append('playback not witnessed above the room floor')
    keywords = plan['stimulus']['keywords'][prompt_id]
    matched = keyword_groups_matched(users[0]['text'] if users else '', keywords)
    if users and matched < 1:
        failures.append('uplink viability: prompt transcript has no fixture keyword')
    complete_observation = open_s is not None and end_s >= tail_end_s
    if complete_observation and stop_ns is not None and not assistants:
        failures.append('reply not complete (no assistant turn)')
    # -- missing witnesses (only matter when nothing failed) --
    if stimulus_error:
        missing.insert(0, stimulus_error)
    if provider and (first is None or min(e['monotonic_ns'] for e in provider) < first):
        missing.append('provider ErrorFrame before the first write')
    if not users:
        missing.append('no user turn within user_turn_timeout_s')
    if first is None:
        missing.append('no reply output')
    elif stop_ns is None or open_s is None:
        missing.append('no BotStoppedSpeakingFrame or GATE open witness')
    elif end_s < tail_end_s:
        missing.append('observation ended before the tail closed')
    if witness is not None and witness.get('missing'):
        missing.append('ALSA recording failed')
    status = 'FAIL' if failures else ('INCOMPLETE' if missing else 'PASS')
    return dict(prompt=prompt_id, prompt_start_s=prompt_start_s, prompt_end_s=prompt_end_s, status=status,
                cause=(failures or missing or [None])[0], failures=failures, missing=missing,
                acoustic_report_only=acoustic_report, mode='aec' if aec else 'mute',
                first_write_s=first/1e9 if first is not None else None, last_write_s=last/1e9 if last is not None else None,
                bot_stopped_s=stop_ns/1e9 if stop_ns is not None else None, gate_open_s=open_s, tail_end_s=tail_end_s,
                pc_ids=sorted({e.get('pc_id') for e in users+assistants+loud if e.get('pc_id')}),
                user_transcripts=[e['text'] for e in users], assistant_transcripts=[e['text'] for e in assistants],
                gate=gate, contexts=contexts, contexts_ok=ctx_ok,
                accepted_false_turns=[dict(kind=e.get('kind'), frame=_frame(e), text=e.get('text'), monotonic_ns=e['monotonic_ns'])
                                      for e in accepted], report_only_events=len(report), playback=witness,
                keyword_groups=dict(matched=matched, total=len(keywords), full_match_report_only=matched == len(keywords)),
                teardown_report_only=teardown_summary(teardown))


def perf_get(p, path):
    for key in path.split('.'):
        if not isinstance(p, dict) or key not in p:
            return None
        p = p[key]
    return p


def counter_epoch(before, after, plan):
    spec = plan['counters']
    paths = list(spec['zero_delta_per_epoch'])
    for prefix in spec['zero_delta_prefix']:
        head = prefix.rstrip('.')
        paths += [head+'.'+k for k in sorted(set(perf_get(before, head) or {}) | set(perf_get(after, head) or {}))]
    paths = [k for k in paths if k not in spec.get('zero_delta_exclude', ())]
    values = {k: [perf_get(before, k), perf_get(after, k)] for k in paths}
    bad = [k for k, (a, b) in values.items() if type(a) is not int or type(b) is not int or not 0 <= a == b]
    same = (before.get('pc_id') is not None and before.get('pc_id') == after.get('pc_id')
            and before.get('generation') == after.get('generation'))
    lost = [perf_get(x, 'lost') for x in (before, after)]
    received = [perf_get(x, 'received') for x in (before, after)]
    loss_valid = all(type(v) is int and v >= 0 for v in lost+received) and lost[1] >= lost[0] and received[1] >= received[0]
    fraction = (lost[1]-lost[0])/max(1, received[1]-received[0]) if loss_valid else None
    loss_ok = loss_valid and fraction <= spec['loss']['max_lost_fraction']
    report = {k: [perf_get(before, k), perf_get(after, k)] for k in ('plc_frames', 'late', 'concealed')}
    return dict(passed=bool(same and not bad and loss_ok), same_session=same, failing=bad, values=values,
                lost_fraction=fraction, loss_passed=bool(loss_ok), report_only=report)


def liveness_epoch(before, after):
    keys = ('pings', 'pongs', 'timeouts')
    a, b = before.get('liveness', {}), after.get('liveness', {})
    valid = all(type(x.get(k)) is int and x[k] >= 0 for x in (a, b) for k in keys)
    same = before.get('pc_id') is not None and (before.get('pc_id'), before.get('generation')) == (after.get('pc_id'), after.get('generation'))
    d = {k: b[k]-a[k] for k in keys} if valid else {}
    passed = (valid and same and d['pings'] > 0 and d['pongs'] > 0 and d['pongs'] >= d['pings']-1 and d['timeouts'] == 0
              and b['timeouts'] == 0 and all(0 <= x['pings']-x['pongs'] <= 1 for x in (a, b)))
    return dict(passed=bool(passed), delta=d, before=a, after=b, same_session=same)


def admission_epoch(before, after):
    integrity = admission_counters(before, after)
    pings = perf_get(after, 'liveness.pings'), perf_get(before, 'liveness.pings')
    delta = pings[0]-pings[1] if all(type(v) is int for v in pings) else None
    reconciled = (integrity['received_delta'] is not None and delta is not None and delta > 0
                  and integrity['received_delta'] >= delta)
    return dict(passed=bool(integrity['passed'] and reconciled), integrity=integrity,
                liveness_pings_delta=delta, reconciled=bool(reconciled))


def playout_epoch(snapshots, plan):
    spec = plan['playout']
    checks = dict(
        target_ms=all(perf_get(p, 'playout.target_ms') == spec['target_ms'] and p.get('playout_target_ms') == spec['target_ms'] for p in snapshots),
        target_depth=all(p.get('target_depth') == spec['target_depth'] for p in snapshots),
        high_water=all(type(perf_get(p, 'playout.ready_high_water')) is int and perf_get(p, 'playout.ready_high_water') <= spec['max_ready_high_water'] for p in snapshots),
        prefill=bool(snapshots) and type(snapshots[-1].get('prefills')) is int and snapshots[-1]['prefills'] > 0)
    return dict(passed=bool(snapshots) and all(checks.values()), checks=checks)


def display_epoch(snapshots):
    rendered = [perf_get(p, 'display.rendered') for p in snapshots]
    valid = len(rendered) >= 2 and all(type(v) is int for v in rendered)
    flags = all(perf_get(p, 'display.failed') is False and perf_get(p, 'display.dead') is False for p in snapshots)
    return dict(passed=bool(valid and rendered[-1] > rendered[0] and flags), first=rendered[:1], last=rendered[-1:], flags_ok=flags)


def aec_mode(plan):
    return plan['mode'].get('perf_aec_block') == 'present'


def resources(snapshots, plan=None):
    """Step-2 floors. Mute mode: AEC-only checks are N/A, and only when no snapshot has an aec block.
    AEC mode: the aec block is required and its checks are gated like every other floor."""
    checks = check_snapshots(snapshots)
    if plan is not None and aec_mode(plan):
        present = bool(snapshots) and all(isinstance(p.get('aec'), dict) for p in snapshots)
        return dict(passed=present and all(c['passed'] for c in checks.values()), checks=checks, aec_block_present=present)
    aec_absent = bool(snapshots) and all('aec' not in p for p in snapshots)
    na = {}
    for key in [k for k in checks if k.startswith('aec.')]:
        na[key] = dict(checks.pop(key), status='N/A' if aec_absent else 'INVALID')
    return dict(passed=aec_absent and all(c['passed'] for c in checks.values()), checks=checks,
                aec_not_applicable=na, aec_block_absent=aec_absent)


TERMINAL_LEAF_PREFIXES = ('internal_', 'failed_allocations.', 'pcm_queue_drops', 'i2s_', 'audio.', 'console_output.', 'aec.')
# Session workers that disconnect joins/destroys (main/app.cpp, transport.cpp): absent from a
# post-disconnect census when the retired session reports tasks_exited=true. Their stack is then
# witnessed by the transport's own exit high-water (uxTaskGetStackHighWaterMark at worker exit).
SESSION_WORKERS = {'audio-tx': 'tx_stack', 'peer-loop': 'owner_stack', 'peer-rx': 'rx_stack'}


def terminal_resources(post, plan):
    """Device/resource floors on the snapshot taken right after each disconnect ('stats' with no live
    session: the retired session's transport stats, live tasks/heap/display and the lifetime AUDIO line).

    Stacks: every STACK_FLOORS task present in the census is gated on its floor. A session worker that
    is absent because the retired session reports tasks_exited=true is gated on its exit high-water
    (tx_stack / owner_stack / rx_stack) when the firmware reports a positive value, and reported (not
    invented) when it does not; an absent worker without tasks_exited is INCOMPLETE. Heap, failed
    allocations, I2S/PCM queue, AUDIO lifetime counters, console and display flags are gated. Session-only
    checks (encode, CPU windows, admission, uplink) are reported. Precedence: any measured failure (or an
    aec block in mute mode) is a FAIL even when other evidence (AUDIO line, rows) is missing."""
    from echoear_aec_resources import STACK_FLOORS
    if not isinstance(post, dict):
        return dict(status='INCOMPLETE', passed=False, reason='no post-disconnect snapshot')
    failures, missing, checks = [], [], {}
    if not aec_mode(plan) and 'aec' in post:
        failures.append('aec block present in mute mode')
    probe = dict(post)
    probe.setdefault('audio', dict(drops=0, read_errors=0, write_errors=0, write_timeouts=0))   # schema only; gated below
    leaves = check_snapshots([probe])
    for k, v in leaves.items():
        if not k.startswith(TERMINAL_LEAF_PREFIXES):
            continue
        if k.startswith('aec.') and not aec_mode(plan):
            continue
        if k.startswith('audio.') and 'audio' not in post:
            continue
        checks[k] = v
        if not v['passed']:
            failures.append(k)
    if 'audio' not in post:
        missing.append('post-disconnect snapshot lacks the AUDIO line')
    tasks = {t.get('name'): t for t in post.get('tasks', []) if isinstance(t, dict)}
    exited = post.get('tasks_exited') is True
    for name, floor in STACK_FLOORS.items():
        t = tasks.get(name)
        if t is not None:
            f = max(floor, (t['stack_bytes']+3)//4) if name == 'audio-playback' and type(t.get('stack_bytes')) is int else floor
            ok = type(t.get('stack_free_bytes')) is int and t['stack_free_bytes'] >= f
            checks['stack_'+name] = dict(passed=ok, value=t.get('stack_free_bytes'), floor=f, basis='live census')
            if not ok:
                failures.append('stack_'+name)
        elif name in SESSION_WORKERS and exited:
            hw = post.get(SESSION_WORKERS[name])
            if type(hw) is int and hw > 0:
                ok = hw >= floor
                checks['stack_'+name] = dict(passed=ok, value=hw, floor=floor, basis='worker exit high-water ('+SESSION_WORKERS[name]+')')
                if not ok:
                    failures.append('stack_'+name)
            else:
                checks['stack_'+name] = dict(passed=True, value=None, floor=floor, report_only=True,
                                             basis='retired worker; exit high-water not reported')
        else:
            missing.append('stack_'+name+' absent without tasks_exited')
    display_ok = perf_get(post, 'display.failed') is False and perf_get(post, 'display.dead') is False
    if not display_ok:
        failures.append('display flags')
    status = 'FAIL' if failures else ('INCOMPLETE' if missing else 'PASS')
    return dict(status=status, passed=status == 'PASS', failures=failures, missing=missing, checks=checks,
                tasks_exited=exited, display_ok=display_ok)


def retired_peers(epochs, max_bracket_s):
    """Decisions 117/120: pc_id -> the ACKNOWLEDGED disconnect's OK-receipt (upper) stamp (host monotonic, ns).

    A peer is retired only if its console 'disconnect' was answered OK, both bracket stamps exist (send_lower_s
    taken just before board.send, done_s just after the OK was received) and the bracket is no wider than
    max_bracket_s. Rows before the OK receipt are live and gate: the device has not acknowledged the
    disconnect yet (a delayed wire write would otherwise exempt a live fault). A wider bracket, a missing or
    non-numeric stamp, an ERR answer or an epoch without pc_id gets no teardown credit at all."""
    out = {}
    for e in epochs:
        d = e.get('disconnect') or {}
        pc = (e.get('first') or {}).get('pc_id')
        lo, up = d.get('send_lower_s'), d.get('done_s')
        if (pc and d.get('ok') is True and type(lo) in (int, float) and type(up) in (int, float)
                and 0 <= up-lo <= max_bracket_s):
            out[pc] = int(up*1e9)
    return out


def teardown_brackets(epochs, max_bracket_s):
    """Fable r13 nit: a named leaf for the retirement brackets. Every OK deliberate disconnect's bracket (send lower
    stamp -> OK receipt) is listed; one wider than max_bracket_s earns no teardown credit (decision 120), so its
    peer's later rows gate as live and may cascade - this leaf names that cause: INCOMPLETE (infrastructure: the
    disconnect was not acknowledged within the declared bracket), never PASS-masking and never clearing a FAIL."""
    rows = []
    for e in epochs:
        d = e.get('disconnect') or {}
        lo, up = d.get('send_lower_s'), d.get('done_s')
        if d.get('ok') is True and type(lo) in (int, float) and type(up) in (int, float):
            rows.append(dict(epoch=e.get('id'), pc_id=(e.get('first') or {}).get('pc_id'), bracket_s=up-lo,
                             credited=0 <= up-lo <= max_bracket_s))
    wide = [r for r in rows if not r['credited']]
    if wide:
        return dict(status='INCOMPLETE', passed=False, reason='disconnect bracket wider than teardown_bracket_max_s: no '
                    'teardown credit for that peer', brackets=rows, max_bracket_s=max_bracket_s)
    return dict(passed=True, brackets=rows, max_bracket_s=max_bracket_s)


def split_teardown(rows, retired):
    """Decision 117 teardown principle (amended by 120). A row is teardown evidence iff its pc_id is a retired peer
    AND it is at or after that peer's acknowledged-disconnect OK-receipt stamp (the same host monotonic clock the
    recorder stamps). Teardown
    rows - writes, transcripts, rtvi rows, pipeline-error, FatalErrorFrame, session-end, gaps - are report-only;
    every other row (before the send, of a live or unknown peer, or with no pc_id) is unchanged evidence."""
    retired = retired or {}
    live, teardown = [], []
    for e in rows:
        cut = retired.get(e.get('pc_id'))
        (teardown if cut is not None and type(e.get('monotonic_ns')) is int and e['monotonic_ns'] >= cut else live).append(e)
    return live, teardown


def teardown_summary(rows):
    kinds = {}
    for e in rows:
        k = str(e.get('kind'))+(':'+str(_frame(e)) if _frame(e) else '')
        kinds[k] = kinds.get(k, 0)+1
    flagged = [dict(kind=e.get('kind'), frame=_frame(e), pc_id=e.get('pc_id'), monotonic_ns=e.get('monotonic_ns'))
               for e in rows if e.get('kind') in ('gap', 'pipeline-error') or _frame(e) == 'FatalErrorFrame']
    return dict(rows=len(rows), by_kind=kinds, peers=sorted({str(e.get('pc_id')) for e in rows}), errors=flagged[:20],
                rule='decision 117: post-disconnect bot-side rows of a deliberately disconnected peer are report-only')


def recorder_integrity(rows, retired=None):
    """Every retained recorder row of every session through the final seal: any gap, pipeline error or
    FatalErrorFrame is a FAIL. Positive evidence wins over missing measurements and retakes. Decision 117: rows
    of a peer at/after its deliberate disconnect send are teardown evidence (report-only, retained)."""
    live, teardown = split_teardown(rows, retired)
    bad = [dict(kind=e.get('kind'), frame=_frame(e), pc_id=e.get('pc_id'), monotonic_ns=e.get('monotonic_ns'))
           for e in live if e.get('kind') in ('gap', 'pipeline-error') or _frame(e) == 'FatalErrorFrame']
    return dict(passed=not bad, findings=bad[:20], count=len(bad), teardown_report_only=teardown_summary(teardown))


def mode_check(bot_log, device_lines, recorder_rows, snapshots, plan):
    m = plan['mode']
    profiles = [e for e in recorder_rows if e.get('kind') == 'stt-profile']
    want_fd = m['recorder_stt_profile_full_duplex']
    aec = aec_mode(plan)
    forbidden_bot = 'aec_full_duplex=False' if aec else 'aec_full_duplex=True'
    forbidden_dev = 'AUDIO_CAPABILITY aec=0' if aec else 'AUDIO_CAPABILITY aec=1'
    checks = dict(
        bot_duplex=bot_log.count(m['bot_duplex_line']) >= 1 and forbidden_bot not in bot_log,
        device_capability=any(m['device_capability_line'] in line for line in device_lines)
            and not any(forbidden_dev in line for line in device_lines),
        recorder_profile=bool(profiles) and all(perf_get(e, 'config.full_duplex') is want_fd and perf_get(e, 'config.profile') == m['stt_profile'] for e in profiles))
    if aec:
        checks['perf_aec_present'] = bool(snapshots) and all(perf_get(p, 'aec.enabled') is True for p in snapshots)
    else:
        checks['perf_aec_absent'] = bool(snapshots) and all('aec' not in p for p in snapshots)
    return dict(passed=all(checks.values()), checks=checks)


def interrupt_absent(rows, device_lines, snapshots_by_epoch, retired=None):
    rows, teardown = split_teardown(rows, retired)          # decision 117; device cancels/counters stay authoritative
    frames = [e for e in rows if _frame(e) == 'InterruptionFrame' or
              (e.get('kind') == 'rtvi-output' and isinstance(e.get('message'), dict) and e['message'].get('type') == 'bot-interrupted')]
    cancels = [line for line in device_lines if 'AUDIO_CANCEL' in line]
    counters = all(perf_get(p, 'cancellation.events') == 0 and perf_get(p, 'cancellation.duplicate_notices') == 0
                   for rows_ in snapshots_by_epoch for p in rows_)
    return dict(passed=not frames and not cancels and counters and bool(snapshots_by_epoch),
                interruption_frames=len(frames), audio_cancel_lines=len(cancels), counters_zero=counters,
                teardown_report_only=teardown_summary(teardown),
                note='interrupt/flush N/A in mute mode; any interruption fails')


def check_idle_budget(plan):
    """Decision 118: the runner never idles past the bot's idle timeout (static part; the runner also keeps a cumulative
    account from the last bot activity, MuteLive.idle_deadline). Pipecat's PipelineWorker cancels the
    session after idle_timeout_secs (default 300 s, demo_bot does not override it) without BotSpeaking/
    UserSpeaking/transcription frames. Every planned runner wait between bot activity must stay below
    bot_idle.max_runner_idle_s: block spacing, the soak tail after a complete block (min_epoch_s - (replies-1) x
    spacing_s) and reconnect idle_s."""
    b = plan['bot_idle']
    limit = b['max_runner_idle_s']
    if not limit < b['pipecat_idle_timeout_s']:
        raise ValueError('bot_idle.max_runner_idle_s must be below the bot idle timeout')
    for st in plan.get('sequence', []):
        waits = [st.get('spacing_s', 0), st.get('idle_s', 0)]
        if st.get('min_epoch_s'):
            if type(st.get('replies')) is not int:
                raise ValueError(f"sequence {st['id']}: min_epoch_s needs an integer replies count for the idle budget")
            waits.append(st['min_epoch_s']-(st['replies']-1)*st.get('spacing_s', 0))
        if max(waits) > limit:
            raise ValueError(f"sequence {st['id']}: a planned runner wait exceeds bot_idle.max_runner_idle_s")
    return True


IDLE_CANCEL = re.compile(r'_idle_timeout_detected|reason: idle timeout|reason=idle timeout')


def bot_idle_timeout(bot_log):
    """An explicit leaf for a bot idle-timeout cancel (decision 118): INCOMPLETE (infrastructure: the runner left
    the bot idle past its timeout), never PASS. The runner's idle budget makes this unreachable by plan."""
    hits = [line[:200] for line in bot_log.splitlines() if IDLE_CANCEL.search(line)]
    if hits:
        return dict(status='INCOMPLETE', passed=False, reason='bot idle-timeout cancel: infrastructure stop, later '
                    'session checks of that epoch cascade from it', lines=hits[:5])
    return dict(passed=True, lines=[])


def fatal_and_boot(device_lines):
    fatal = [line for line in device_lines if re.search(FATAL, line)]
    boots = [line for line in device_lines if 'elf_sha256=' in line]
    return dict(passed=not fatal and len(boots) == 1, fatal=fatal, boot_lines=len(boots))


def liveness_logs(bot_log, device_lines):
    alarm = 'LIVENESS alarm' in bot_log
    missing = any('LIVENESS no ping' in line for line in device_lines)
    return dict(passed=not alarm and not missing, bot_alarm=alarm, device_missing_ping=missing)


LOGURU_LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \| *(\w+) *\| (\S+) - (.*)$')
ALARM_MESSAGE = re.compile(r'^LIVENESS alarm: no timely pong for ([0-9.]+) s; closing peer(?: pc_id=(\S+?)(?: silent_s=(\S+))?)?$')
DISCARD_MESSAGE = re.compile(r'Discarding peer connection for pc_id: (\S+)\s*$')


def loguru_wall(stamp):
    """bot.log stamps are loguru's local wall time, truncated to ms, on this host's clock."""
    from datetime import datetime
    return datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f').timestamp()


def liveness_tolerance_s(period_s):
    """demo_liveness.Liveness: tolerance = max(2.0, 2 x period)."""
    return max(2.0, 2*float(period_s))


def liveness_logs_attributed(bot_log, device_lines, peers, plan):
    """Decisions 112/113: a bot LIVENESS alarm is expected only for a peer the harness deliberately disconnected.

    peers: one dict per epoch, on the wall clock (time.time(), the clock loguru stamps with):
      dict(id, pc_id, live_from_wall, disconnect=dict(lower_wall, sent_wall, ok)) - live_from_wall just before
      connect, lower_wall/sent_wall just before/after the console 'disconnect' send.

    Identity (113). Each alarm is attributed ONLY by the pc_id on its own line: demo_liveness.Liveness is
    constructed per peer with label=runner_args.webrtc_connection.pc_id and appends 'pc_id=<id>' to the alarm.
    An alarm without a tag (e.g. the pre-113 bot, smoke-01) or with a pc_id that is not a declared epoch's peer
    FAILs closed. No proximity pairing: an unrelated discard near the alarm cannot move its identity.

    Corroboration. handle_disconnected logs 'Discarding peer connection for pc_id: <P>' when P closes. The
    tagged peer must be discarded after the alarm in file order - file order only, deliberately no time bound, so
    no proximity window can re-enter identity (the alarm awaits close() of its own peer) - or
    - the pre-declared ORPHAN signature - discarded before it: a watchdog tick landing between a graceful
    discard and liveness.stop(). An orphan is credited by the same identity/window rules; P never discarded
    FAILs.

    Expected only if the tagged P is a declared peer with an OK deliberate disconnect, P is not live at the
    alarm (live span [connect - slack, send lower + slack)), the alarm lies in
    [sent + tolerance - period - early_s - slack, sent + tolerance + margin_s + slack], the logged tolerance
    equals max(2, 2 x period), and P has no other alarm. The lower bound follows from the watchdog: the alarm
    needs now - last_timely >= tolerance with last_timely >= send - period - RTT for a peer that answered while
    live (smoke-01: send + 1.957 s against a lower bound of send + 1.25 s). Anything else FAILs. Device
    'LIVENESS no ping' anywhere FAILs.

    Last timely pong (115). The tagged line also carries silent_s (now - last_timely on the bot's monotonic clock,
    3 decimals); last_timely_wall = alarm wall - silent_s must be >= send lower - period - early_s - slack, so a
    failure already owed while the peer was live (a watchdog callback delayed past the disconnect) FAILs even
    when it is emitted inside the window. silent_s missing or malformed FAILs closed; silent_s below the
    tolerance is inconsistent and FAILs.

    Record-only: the stamps assume one non-stepping wall clock (chrony slews) and a non-ambiguous local time
    (the DST fall-back hour, e.g. 2026-11-01 01:00-02:00, would make loguru_wall ambiguous)."""
    rule = plan['liveness_logs']
    period = float(plan['liveness']['period_s'])
    tolerance = liveness_tolerance_s(period)
    if rule['tolerance_s'] != tolerance:
        raise ValueError('liveness_logs.tolerance_s must equal demo_liveness max(2, 2 x period)')
    slack = rule['clock_slack_s']
    lines = bot_log.splitlines()
    parsed = [LOGURU_LINE.match(line) for line in lines]
    discards = []
    for index, m in enumerate(parsed):
        d = DISCARD_MESSAGE.search(m[4]) if m else None
        if d:
            discards.append(dict(index=index, wall=loguru_wall(m[1]), pc_id=d[1]))
    by_pc = {p.get('pc_id'): p for p in peers if p.get('pc_id')}
    alarms, used = [], {}
    for index, line in enumerate(lines):
        if 'LIVENESS alarm' not in line:
            continue
        m = parsed[index]
        a = ALARM_MESSAGE.match(m[4]) if m and m[3].startswith('demo_liveness:') else None
        entry = dict(line=index+1, text=line[:240])
        alarms.append(entry)
        if not a:
            entry.update(status='FAIL', reason='unparseable LIVENESS alarm line'); continue
        wall = loguru_wall(m[1])
        entry.update(wall=wall, logged_tolerance_s=float(a[1]), pc_id=a[2])
        if a[2] is None:
            entry.update(status='FAIL', reason='untagged alarm: identity not bound at emission'); continue
        pc = a[2]
        silent = float(a[3]) if a[3] and re.fullmatch(r'[0-9]+\.[0-9]{3}', a[3]) else None
        if silent is None:
            entry.update(status='FAIL', reason='missing or malformed silent_s: last timely pong not witnessed'); continue
        entry.update(silent_s=silent, last_timely_wall=wall-silent)
        after = [d for d in discards if d['pc_id'] == pc and d['index'] > index]
        before = [d for d in discards if d['pc_id'] == pc and d['index'] < index]
        entry.update(discard_line=after[0]['index']+1 if after else None,
                     orphan_after_discard_s=(wall-before[-1]['wall']) if before and not after else None)
        live = [p['id'] for p in peers if p.get('live_from_wall') is not None and p['live_from_wall']-slack <= wall
                and (not p.get('disconnect') or p['disconnect'].get('lower_wall') is None or wall < p['disconnect']['lower_wall']+slack)]
        entry['live_peers_at_alarm'] = live
        peer = by_pc.get(pc)
        if peer is None:
            entry.update(status='FAIL', reason='alarm on an unknown peer'); continue
        entry['epoch'] = peer['id']
        if not after and not before:
            entry.update(status='FAIL', reason='not corroborated: tagged peer never discarded'); continue
        disc = peer.get('disconnect') or {}
        if disc.get('ok') is not True or disc.get('lower_wall') is None or disc.get('sent_wall') is None:
            entry.update(status='FAIL', reason='peer has no witnessed deliberate disconnect'); continue
        lo = disc['sent_wall']+tolerance-period-rule['early_s']-slack
        hi = disc['sent_wall']+tolerance+rule['margin_s']+slack
        entry.update(window_wall=[lo, hi], after_send_s=wall-disc['sent_wall'])
        if peer['id'] in live:
            entry.update(status='FAIL', reason='alarm on the live peer'); continue
        if not lo <= wall <= hi:
            entry.update(status='FAIL', reason='alarm outside the deliberate-disconnect window'); continue
        if entry['logged_tolerance_s'] != tolerance:
            entry.update(status='FAIL', reason='logged tolerance differs from the declared one'); continue
        if silent < tolerance-0.001:
            entry.update(status='FAIL', reason='silent_s below the tolerance: inconsistent alarm'); continue
        # decision 115: the silence must have begun no earlier than one cadence (+ early_s) before the send, i.e.
        # the peer was still answering on cadence when the harness disconnected it; an alarm owed while it was
        # live (an overdue watchdog callback) fails.
        floor = disc['lower_wall']-period-rule['early_s']-slack
        entry['last_timely_floor_wall'] = floor
        if entry['last_timely_wall'] < floor:
            entry.update(status='FAIL', reason='last timely pong before the disconnect send - period - early_s: failure owed while live'); continue
        if pc in used:
            entry.update(status='FAIL', reason='second alarm for the same peer'); continue
        used[pc] = index
        entry.update(status='EXPECTED', reason='orphan tick after the graceful discard of a deliberately disconnected peer'
                     if before and not after else 'stale deliberately disconnected peer reaped by the bot watchdog')
    missing = [line for line in device_lines if 'LIVENESS no ping' in line]
    passed = all(e['status'] == 'EXPECTED' for e in alarms) and not missing
    return dict(passed=passed, rule='decision-113 tagged', bot_alarm=bool(alarms),
                expected_alarms=sum(e['status'] == 'EXPECTED' for e in alarms), alarms=alarms,
                device_missing_ping=bool(missing), device_missing_ping_lines=missing[:5],
                tolerance_s=tolerance, period_s=period, early_s=rule['early_s'], margin_s=rule['margin_s'])


def liveness_peers(epochs):
    """Runner epochs -> liveness_logs_attributed peers (wall-clock stamps recorded by MuteLive)."""
    out = []
    for e in epochs:
        d = e.get('disconnect') or {}
        out.append(dict(id=e['id'], pc_id=(e.get('first') or {}).get('pc_id'), live_from_wall=e.get('pre_connect_wall'),
                        disconnect=dict(lower_wall=d.get('send_lower_wall'), sent_wall=d.get('sent_wall'), ok=d.get('ok')) if d else None))
    return out


def reconnect_rule(cycle, before, after, ready_states, prior_pcs, rows, ready_seconds, plan, trigger=None):
    """One reconnect cycle: retained trigger (R1/R2), new generation, new peer, no stale replay.

    rows are ALL recorder rows; the new peer's rows are selected by its identity, so a write made
    before Ready (before the new epoch's first snapshot) still counts as stale.
    """
    rule = plan['reconnect']
    new_pc = after.get('pc_id')
    gens = [r['generation'] for r in ready_states]
    new_gen = (type(after.get('generation')) is int and type(before.get('generation')) is int and
               after['generation'] > before['generation'] and bool(gens) and after['generation'] == max(gens))
    peer = new_pc is not None and new_pc not in prior_pcs
    own = sorted([e for e in rows if e.get('pc_id') == new_pc], key=lambda e: e['monotonic_ns'])
    first_user = min((e['monotonic_ns'] for e in own if e.get('kind') == 'aggregator-user-turn-started'), default=None)
    stale = [e for e in own if e.get('kind') == 'tts-transport-write' and e.get('non_silent')
             and (first_user is None or e['monotonic_ns'] < first_user)]
    timely = ready_seconds is not None and ready_seconds <= rule['ready_timeout_s']
    prefill = type(after.get('prefills')) is int
    needs = rule['triggers'].get(cycle)
    trigger_ok = needs is None or (isinstance(trigger, dict) and trigger.get('kind') == needs and trigger.get('fired') is True
                                   and trigger.get('disconnect_lag_s') is not None
                                   and trigger['disconnect_lag_s'] <= rule['max_disconnect_lag_s']
                                   and (needs != 'mid-reply' or trigger.get('active') is True))
    passed = bool(new_gen and peer and not stale and timely and prefill and trigger_ok)
    status = 'PASS' if passed else ('INCOMPLETE' if needs and not trigger_ok and new_gen and peer and not stale else 'FAIL')
    return dict(id=cycle, passed=passed, status=status, new_generation=new_gen, new_peer=peer, stale_writes=len(stale),
                ready_seconds=ready_seconds, timely=timely, trigger=trigger, trigger_ok=trigger_ok)


def old_session_closed(old_rows, new_rows):
    """REPORT-ONLY since decision 117: the old peer's session-end is post-disconnect teardown evidence of a
    deliberately disconnected peer (only the liveness rule applies to it). Retained in the gate output."""
    end = [e['monotonic_ns'] for e in old_rows if e.get('kind') == 'session-end']
    first = [e['monotonic_ns'] for e in new_rows if e.get('kind') == 'tts-transport-write']
    return bool(end) and (not first or min(end) < min(first))


def terminal_epoch(first, terminal, plan, disconnect=None):
    """First snapshot -> the retired session's final PERF printed by 'disconnect'. Counters and
    liveness include teardown; a missing or foreign terminal record is INCOMPLETE, never zero."""
    if isinstance(disconnect, dict) and disconnect.get('ok') is False:
        return dict(status='FAIL', passed=False, reason='disconnect answered ERR', error=disconnect.get('error'))
    if not isinstance(terminal, dict):
        return dict(status='INCOMPLETE', passed=False, reason='no terminal PERF for this generation')
    if (terminal.get('pc_id'), terminal.get('generation')) != (first.get('pc_id'), first.get('generation')):
        return dict(status='INCOMPLETE', passed=False, reason='terminal PERF belongs to another session',
                    terminal=(terminal.get('pc_id'), terminal.get('generation')))
    exit_keys = plan['terminal'].get('exit_associated_report_only', [])
    reduced = copy.deepcopy(plan)
    reduced['counters']['zero_delta_per_epoch'] = [k for k in plan['counters']['zero_delta_per_epoch'] if k not in exit_keys]
    reduced['counters']['zero_delta_exclude'] = list(exit_keys)
    counters = counter_epoch(first, terminal, reduced)
    live = liveness_epoch(first, terminal)
    exit_deltas = {k: [perf_get(first, k), perf_get(terminal, k)] for k in exit_keys}
    return dict(passed=counters['passed'] and live['passed'], counters=counters, liveness=live,
                exit_associated_report_only=exit_deltas)


def block_verdict(cells, required, retakes_allowed, plan, cold_first=False):
    """Cells in order. A FAIL (or any latched failure) is final and never retaken. Only an
    INCOMPLETE with a declared cause and no latched failure may be replaced. With cold_first the
    block's first attempt must itself PASS: a warm retake cannot supply cold coverage."""
    allowed = set(plan['retakes']['allowed_causes'])
    failed = [c for c in cells if c['status'] == 'FAIL' or c.get('failures')]
    passed = [c for c in cells if c['status'] == 'PASS' and not c.get('failures')]
    incomplete = [c for c in cells if c['status'] == 'INCOMPLETE' and not c.get('failures')]
    retaken = [c for c in incomplete if c.get('cause') in allowed]
    cold_ok = not cold_first or (bool(cells) and cells[0]['status'] == 'PASS' and not cells[0].get('failures'))
    if failed:
        status = 'FAIL'
    elif cold_ok and len(passed) >= required and len(retaken) <= retakes_allowed and len(incomplete) == len(retaken):
        status = 'PASS'
    else:
        status = 'INCOMPLETE'
    return dict(status=status, passed=len(passed), failed=len(failed), incomplete=len(incomplete),
                retakes_used=len(retaken), required=required, cold_first=cold_first, cold_ok=cold_ok)


def exclusion_conflict(now, duration_s, window_minutes):
    """True if [now, now + duration_s] touches the local exclusion [start, end) minutes of any day."""
    import datetime
    start_m, end_m = window_minutes
    t0 = now
    t1 = now+datetime.timedelta(seconds=duration_s)
    day = datetime.datetime(t0.year, t0.month, t0.day, tzinfo=t0.tzinfo)-datetime.timedelta(days=1)
    while day <= t1:
        a = day+datetime.timedelta(minutes=start_m)
        b = day+datetime.timedelta(minutes=end_m)
        if t0 < b and a < t1:
            return True
        day += datetime.timedelta(days=1)
    return False


def gate_verdict(parts):
    """parts: dict name -> dict(passed|status). INVALID > FAIL > INCOMPLETE > PASS."""
    statuses = {}
    for name, part in parts.items():
        statuses[name] = part.get('status') or ('PASS' if part.get('passed') else 'FAIL')
    if statuses.get('mode') == 'FAIL':
        overall = 'INVALID'
    elif 'FAIL' in statuses.values():
        overall = 'FAIL'
    elif 'INCOMPLETE' in statuses.values():
        overall = 'INCOMPLETE'
    else:
        overall = 'PASS'
    return dict(outcome=overall, parts=statuses)


def volume_epoch(snapshots, volume_db):
    values = [p.get('output_volume_db') for p in snapshots]
    return dict(passed=bool(values) and all(v == volume_db for v in values), values=sorted(set(map(str, values))),
                declared=volume_db)
