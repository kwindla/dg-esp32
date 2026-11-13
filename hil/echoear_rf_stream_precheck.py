#!/usr/bin/env python3
"""Uncounted strict-path RF pre-check (decision 137): uplink link_tail on the normal image, < 0.5 % before the go.

One bench-locked hold: 30 s wlp8s0 RF pre-gate, flash the bound normal image, boot/identity/profile check, the
strict path's display (cat), then SESSIONS (2) streaming sessions back to back. Each session is a fresh connect,
audio 1, quiet console, 270 s (default) of continuous uplink streaming captured by the owned bot's RTP evidence (the
same capture and link_tail computation as the strict stage), then disconnect. RestoreHold restores and boot-verifies
the bench restore-of-record (echoear_aec_bench.NORMAL_HASH; 55152f25 since gap 3) before the lock is released. No prompt is played, no STT/LLM is used (offline-test bot) and nothing is
scored or counted. Writes rf-precheck.json in a fresh output directory. It is NOT an acceptance gate: the
strict/quiet gates keep their own <=1 % limit (decisions 134-137).

r12 (idle-timeout fix). Run rf-precheck/20261027T172129Z streamed one 320 s session and was cut at ~300 s by the
offline-test bot's Pipecat idle timeout (bot log: CancelFrame(reason: idle timeout); the device then logged a DTLS
close, which the strict stop rule refused, and reconnected as a second peer). The bot's RTP evidence also retains at
most RTP_EVIDENCE_MAX_PACKETS (default 10000 = 200 s at 50 packets/s) per connection, so that run measured only
the first 200 s. Chosen fix (option B): keep the bot configuration identical to the strict stage (default 300 s idle
timeout) and measure SESSIONS sessions of at most 270 s each (--seconds >= 290 is refused, leaving setup margin
under the 300 s timeout), each with a fresh connect and its own window, link_tail filtered to that session's one
pc_id; every session must clear. Two sessions (>= 540 s streamed) still cover a full 304 s host bgscan period,
minus the reconnect gap. RTP evidence is raised to RTP_MAX_PACKETS per connection, and any session whose pc_id
reached that cap, any window with other than exactly one pc_id, or any idle-timeout cancellation in the bot log is
labelled harness_failure (also on the exception path) and never clears.

r13 (Astra r12 B2). Only a qualifying run can clear: at least SESSIONS (2) sessions of QUALIFYING_SECONDS (270) to
under MAX_SESSION_SECONDS (290) each. A shorter --seconds is still accepted for diagnostics: the record says
diagnostic: true, the clearance check qualifying_duration is false, rf_clearance/passed are false, and the exit
code is DIAGNOSTIC_EXIT (5) when every other check passed (1 otherwise).

r13 (endpoint coverage). Each session keeps streaming STOP_MARGIN_SECONDS (2 s) after its window end before
`quiet 0`/`audio 0`. Run 20261027T184859Z session 1 had its last RTP packet 12.7 ms before the window end: the
commands after the end stopped the uplink inside one 20 ms packet period, so link_tail found no packet at or after
the endpoint and returned covered false / passed null. That was a pre-check race (about one session in two), not a
stall: the device sent 13554 packets and all 13554 were captured.

r14 (Fable r13 N3). Each session records setup_s (connect issued to window start), lifetime_s (connect issued to
disconnect returned, an upper bound on the bot pipeline's life) and idle_timeout_margin_s (300 - lifetime_s); the
record carries inter_window_gaps_s between consecutive windows. Report only: no gate reads them. Exit codes: 0 qualified clearance, 1 no
clearance / failed check / harness failure / exception, 2 in-hold RF pre-gate deferral, 5 diagnostic run.
"""
import argparse
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
THRESHOLD = 0.005          # decision 137: uplink tail must be below 0.5 % before asking for the quiet-room go
STREAM_SECONDS = 270       # r12: per session; below the offline-test bot's 300 s Pipecat idle timeout
MAX_SESSION_SECONDS = 290  # r12: refuse windows that leave under 10 s for connect/gate setup before the idle timeout
QUALIFYING_SECONDS = 270   # r13 (Astra r12 B2): a qualifying session streams >= 270 s (and < 290 s); shorter is diagnostic
STOP_MARGIN_SECONDS = 2.0  # r13: keep streaming this long after the window end, so a packet always follows the endpoint
BOT_IDLE_TIMEOUT_SECONDS = 300  # offline-test bot's Pipecat idle timeout (bot default; not overridden here)
DIAGNOSTIC_EXIT = 5        # r13: a shorter diagnostic run whose checks all pass exits 5, never 0 (rf_clearance false)
SESSIONS = 2               # r12: >= 540 s streamed in total, spanning a 304 s host bgscan period (Fable r9 N2)
RTP_MAX_PACKETS = 60000    # r12: per-connection RTP evidence cap (bot default 10000 = 200 s)
STARTUP_SECONDS = 5
EVIDENCE = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-10/requal-n1/rf-precheck')


class Deferred(Exception):
    pass


def load_packets(out):
    path = out/'rtp'/'rtp.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def precheck_verdict(packets, start, end, *, threshold=THRESHOLD, startup_seconds=STARTUP_SECONDS):
    """Pure: link_tail over the streaming window, reported against the decision-137 pre-check threshold."""
    from uplink_continuity import link_tail
    tail = link_tail(packets, start, end, startup_seconds=startup_seconds)
    rate = tail['tail_rate']
    keep = {k: tail[k] for k in ('covered', 'intervals', 'tail_rate', 'over_threshold', 'threshold_ms', 'over_extreme_threshold',
                                 'extreme_threshold_ms', 'eligible_maximum_ms', 'hard_cap_ms', 'reorders', 'duplicates', 'passed')}
    keep.update(link_tail_passed=tail['passed'], precheck_threshold=threshold,
                # Raw measurement only (Fable r9 N3): rf_clearance below is the decision-137 answer.
                tail_below_threshold=bool(tail['covered'] and tail['intervals'] and rate is not None and rate < threshold),
                disorder=tail['disorder'][:50],
                basis='Strict-path uplink link_tail (uplink_continuity.link_tail) over the streaming window; decision 137 '
                      'asks tail_rate < 0.5 % before the go. The acceptance gates themselves are unchanged.')
    return keep


def session_pc_ids(packets, start, end):
    """Pure: the pc_ids with at least one RTP packet observed inside [start, end] (host monotonic seconds)."""
    return sorted({r['pc_id'] for r in packets if start <= r['observed_ms']/1000 <= end})


def harness_problems(out, packets, sessions, *, cap=RTP_MAX_PACKETS):
    """r12: conditions under which the measurement itself is invalid (labelled harness_failure, never cleared)."""
    problems = []
    for log in sorted((out/'bot').glob('server-*.log')):
        for n, line in enumerate(log.read_text(errors='replace').splitlines(), 1):
            if 'idle timeout' in line.lower():
                problems.append(dict(kind='bot_idle_timeout', log=log.name, line=n, text=line[:300])); break
    seen = []
    for s in sessions:
        if 'window' not in s: continue
        ids = session_pc_ids(packets, s['window']['start_s'], s['window']['end_s'])
        if len(ids) != 1:
            problems.append(dict(kind='session_pc_ids', session=s['index'], pc_ids=ids))
        elif ids[0] in seen:
            problems.append(dict(kind='session_reused_peer', session=s['index'], pc_id=ids[0]))
        seen += ids
    counts = {}
    for r in packets: counts[r['pc_id']] = counts.get(r['pc_id'], 0)+1
    for pc in seen:
        if counts.get(pc, 0) >= cap:
            problems.append(dict(kind='rtp_evidence_cap', pc_id=pc, packets=counts[pc], cap=cap))
    return problems


def summarize(tails):
    """Pure: the run-level link_tail view over every session (worst rate, summed counts, all must pass)."""
    rates = [t['tail_rate'] for t in tails]
    return dict(sessions=len(tails), covered=bool(tails) and all(t['covered'] for t in tails),
                intervals=sum(t['intervals'] or 0 for t in tails),
                tail_rate=None if not tails or None in rates else max(rates),
                over_threshold=sum(t['over_threshold'] for t in tails),
                over_extreme_threshold=sum(t['over_extreme_threshold'] for t in tails),
                eligible_maximum_ms=max((t['eligible_maximum_ms'] or 0 for t in tails), default=None),
                reorders=sum(t['reorders'] for t in tails), duplicates=sum(t['duplicates'] for t in tails),
                link_tail_passed=bool(tails) and all(t['link_tail_passed'] is True for t in tails),
                tail_below_threshold=bool(tails) and all(t['tail_below_threshold'] is True for t in tails),
                precheck_threshold=THRESHOLD)


def execute(binding_path, out, *, seconds=STREAM_SECONDS, sessions=SESSIONS, restore_wait=None):
    from echoear_item2_campaign import verified_binding, flash_image, RestoreHold, write_final
    from echoear_item1_combined import rf_pre_gate
    if not 0 < seconds < MAX_SESSION_SECONDS:   # + STOP_MARGIN_SECONDS still leaves setup time under 300 s
        raise ValueError(f'--seconds must be below {MAX_SESSION_SECONDS}: the offline-test bot cancels an idle pipeline '
                         'after 300 s (Pipecat idle timeout), which ended run 20261027T172129Z mid-window')
    if sessions < 2: raise ValueError('At least two sessions are required (bgscan coverage, option B)')
    binding, image = verified_binding(binding_path)
    out = out.resolve()
    if not out.is_relative_to(EVIDENCE): raise ValueError('Output must be under '+str(EVIDENCE))
    if out.exists(): raise FileExistsError('Fresh output directory required')
    os.environ.update(DG_BOARD='echoear', STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'), STEP9A_BUDGET_PATH=binding['budget'],
                      PIPECAT_SCTP_MAX_CHUNK_SIZE='1100', RTP_EVIDENCE_DIR=str(out/'rtp'),
                      RTP_EVIDENCE_MAX_PACKETS=str(RTP_MAX_PACKETS))
    os.environ.setdefault('ESPTOOL_PYTHON', str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    from step9a_echoear import bench, check_esptool, check_firmware
    from host_nic import snapshot, gate as nic_gate
    check_esptool()
    out.mkdir(parents=True); identity = out/'identity'; identity.mkdir()
    qualifying = QUALIFYING_SECONDS <= seconds < MAX_SESSION_SECONDS and sessions >= SESSIONS
    result = dict(kind='rf-precheck', counted=False, diagnostic=not qualifying, qualifying_seconds=QUALIFYING_SECONDS, decision=137, precheck_threshold=THRESHOLD, stream_seconds=seconds, session_count=sessions,
                  rtp_max_packets=RTP_MAX_PACKETS,
                  binding=str(binding_path), normal_elf_sha256=binding['normal_elf_sha256'], passed=False)
    hold = None; code = 1; result['sessions'] = runs = []
    try:
        with bench():
            with RestoreHold(identity, wait=restore_wait) as hold:
                result['rf_pre_gate'] = rf_pre_gate(snapshot, nic_gate)
                if not result['rf_pre_gate']['passed']:
                    result.update(deferred=True); raise Deferred('RF pre-gate failed before flash; nothing touched')
                hold.touch(); port = flash_image(image, identity); nic = snapshot()
                from esp_serial import EspSerial
                from step7_support import BotServer
                from step9a_assets import asset_server
                from step9a_support import host_address, setup
                with asset_server(), BotServer(out/'bot', 'offline-test') as bot, \
                        EspSerial(port, log_path=out/'serial.log', display_checks=True) as board:
                    start = board.cursor; board.reset(); board.wait_for('READY transport-console', 60, since=start)
                    result['boot_elf_sha256'] = check_firmware(board, start, elf=image/'dg-esp32.elf', profile='normal')
                    board.command('playback-volume 0'); board.command('server-url http://'+host_address()+':7861')
                    result['display_setup'] = setup(board, mode='cat')
                    for index in range(1, sessions+1):
                        session = dict(index=index); runs.append(session)
                        # r14 (Fable r13 N3): session lifetime on a separate monotonic-delta clock (perf_counter), so
                        # the window clock is unchanged: connect issued -> window start (setup) -> disconnect returned.
                        connect_at = time.perf_counter()
                        cursor = board.cursor; board.command('connect', 90); board.wait_for('STATE Ready', 5, since=cursor)
                        board.command('quiet 1'); board.command('audio 1'); board.wait_for('GATE open', 15, since=cursor)
                        session['setup_s'] = time.perf_counter()-connect_at
                        stream_start = time.monotonic(); time.sleep(seconds); stream_end = time.monotonic()
                        session['window'] = dict(start_s=stream_start, end_s=stream_end)
                        # r13: 20261027T184859Z session 1 ended 12.7 ms after its last packet and `audio 0` stopped the
                        # uplink before the next one, so link_tail had no packet at/after the end (covered false). Keep
                        # streaming past the endpoint before stopping.
                        time.sleep(STOP_MARGIN_SECONDS)
                        board.command('quiet 0'); board.command('audio 0'); board.command('disconnect', 40)
                        session['lifetime_s'] = time.perf_counter()-connect_at
                        session['idle_timeout_margin_s'] = BOT_IDLE_TIMEOUT_SECONDS-session['lifetime_s']
                    board.command('playback-volume -6')
                    result['display_monitor'] = board.display_monitor.result()
                result['stage_nic'] = nic_gate(nic, snapshot())
        packets = load_packets(out)
        for session in runs:
            w = session['window']; ids = session_pc_ids(packets, w['start_s'], w['end_s'])
            session['pc_ids'] = ids
            session['link_tail'] = precheck_verdict([r for r in packets if r['pc_id'] in ids[:1]], w['start_s'], w['end_s'])
        result['harness_failure'] = harness_problems(out, packets, runs)
        # r14 (Fable r13 N3): measured gaps between consecutive windows (window clock); a periodic RF event inside a gap
        # is unobserved (miss probability ~ gap / period).
        result['inter_window_gaps_s'] = [b['window']['start_s']-a['window']['end_s'] for a, b in zip(runs, runs[1:])
                                         if 'window' in a and 'window' in b]
        result['link_tail'] = summarize([s['link_tail'] for s in runs])
        result['restore'] = hold.record
        # r10 (Astra r9 #2): one conjunction answers decision 137 and drives passed/exit_code: the raw tail below
        # 0.5 %, a valid PASSING link_tail result (rate, extreme rate and hard cap, coverage), the stage NIC gate,
        # the display monitor, and a restore that was clean on its FIRST attempt (a retried restore is a failed hold).
        lt = result['link_tail']; restore = hold.record
        checks = dict(tail_below_threshold=lt['tail_below_threshold'] is True, link_tail_passed=lt['link_tail_passed'] is True,
                      stage_nic=(result.get('stage_nic') or {}).get('passed') is True,
                      display_monitor=(result.get('display_monitor') or {}).get('passed') is True,
                      restore_first_attempt=restore.get('clean') is True and len(restore.get('attempts') or []) == 1,
                      sessions_complete=len(runs) == sessions and all('link_tail' in s for s in runs),
                      harness_clean=not result['harness_failure'],
                      # r13 (Astra r12 B2): measured durations, not only the requested one.
                      qualifying_duration=qualifying and len(runs) >= SESSIONS and all(
                          QUALIFYING_SECONDS <= s['window']['end_s']-s['window']['start_s'] for s in runs if 'window' in s))
        result['clearance_checks'] = checks
        result['rf_clearance'] = result['passed'] = all(checks.values())
        others = all(v for k, v in checks.items() if k != 'qualifying_duration')
        code = 0 if result['passed'] else (DIAGNOSTIC_EXIT if result['diagnostic'] and others else 1)
    except Deferred as exc:
        result.update(passed=False, error='Deferred: '+str(exc), restore=hold.record if hold is not None else dict(device_untouched=True),
                      exit_code=2)
        write_final(out/'rf-precheck.json', result, hold)
        print(json.dumps(dict(passed=False, deferred=True, reason=str(exc))), flush=True)
        return 2
    except BaseException as exc:
        result.update(passed=False, error=type(exc).__name__+': '+str(exc), interrupted=isinstance(exc, KeyboardInterrupt),
                      restore=hold.record if hold is not None else dict(device_untouched=True))
        try:   # r12: an idle-timeout cut (or a second peer in a window) is a harness failure, not an RF verdict
            result['harness_failure'] = harness_problems(out, load_packets(out), runs)
        except Exception as label_exc:
            result['harness_failure_unavailable'] = type(label_exc).__name__+': '+str(label_exc)
        result['exit_code'] = 1
        write_final(out/'rf-precheck.json', result, hold)
        raise
    result['exit_code'] = code
    write_final(out/'rf-precheck.json', result, hold)
    lt = result['link_tail']
    print(json.dumps(dict(rf_clearance=result['rf_clearance'], passed=result['passed'], diagnostic=result['diagnostic'], harness_failure=result['harness_failure'],
                          sessions=lt['sessions'], tail_rate=lt['tail_rate'], over_threshold=lt['over_threshold'],
                          over_extreme_threshold=lt['over_extreme_threshold'], reorders=lt['reorders'], duplicates=lt['duplicates'],
                          threshold=THRESHOLD)), flush=True)
    return code


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binding', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=STREAM_SECONDS, help=f'per session, below {MAX_SESSION_SECONDS}')
    p.add_argument('--sessions', type=int, default=SESSIONS)
    a = p.parse_args(); raise SystemExit(execute(a.binding.resolve(), a.out, seconds=a.seconds, sessions=a.sessions))
