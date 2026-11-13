#!/usr/bin/env python3
"""60 s serial-reader absence, with three microphone/offline-fixture turns.

Run only after Claude releases the demo bench. This is NOT a cable-unplug or
battery-power test: USB SOF can remain present after the serial fd closes.
No sudo/network changes; one short bench hold; no automatic retry.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path('/home/khkramer/src/evidence/echoear/A')


def network_gate(before, after):
    from host_nic import gate
    result = gate(before, after)
    # Preserve existing evidence keys for consumers of this functional harness.
    result['deltas'] = result.get('byte_deltas', {})
    result['allowance_bytes'] = result.get('allowed_total_bytes')
    return result


def score(before, after, events, start, end):
    from playback_quality import transport_gate, microphone_clip_gate
    pc = before.get('pc_id')
    same = bool(pc) and pc == after.get('pc_id') and before.get('generation') == after.get('generation')
    during = [e for e in events if start <= e['t'] <= end]
    ends = [e for e in during if e['event'] == 'bot-audio-end' and e['pc_id'] == pc]
    fixtures = [e for e in during if e['event'] == 'usb-fixture-reply' and e['pc_id'] == pc]
    turns = [e for e in during if e['event'] == 'vad-stop' and e['pc_id'] == pc]
    # Each scheduled prompt must produce a finished reply in its own window.
    windows = [(start+offset, min(end, start+offset+18)) for offset in (3, 23, 43)]
    replies = [any(a<=e['t']<=b for e in ends) for a,b in windows]
    transport = transport_gate(before, after)
    clips = microphone_clip_gate(before, after)
    counters = {key: after.get(key, 0)-before.get(key, 0) for key in ('sent', 'received')}
    workers = ('audio-capture', 'audio-playback', 'peer-loop', 'app-control')
    # PERF CPU interval covers absence; null/missing counters cannot pass.
    tasks = {row['name']: row for row in after.get('tasks', [])}
    prior = {row['name']: row for row in before.get('tasks', [])}
    active = all(name in tasks and name in prior and tasks[name]['id']==prior[name]['id']
                 and (tasks[name]['runtime_us']-prior[name]['runtime_us']) % (2**32)>0
                 for name in workers)
    checks = dict(sixty_seconds=end-start>=60, same_session=same,
                  all_three_reply_windows=all(replies), at_least_three_turns=len(turns)>=3,
                  fixture_replies=len({e['turn'] for e in fixtures})>=3,
                  no_session_end=not any(e['event'] in ('session-end', 'transport-disconnected') for e in during),
                  media_progress=all(n>0 for n in counters.values()),
                  transport=transport['passed'], clips=clips['passed'],
                  output_telemetry='console_output' in before and 'console_output' in after,
                  worker_runtime_progress=active,
                  system_event_stack=tasks.get('sys_evt', {}).get('stack_free_bytes', 0)>=1024,
                  display_progress=after.get('display', {}).get('rendered', 0)>
                      before.get('display', {}).get('rendered', 0))
    return dict(passed=all(checks.values()), checks=checks, reply_windows=replies,
                replies=len(ends), vad_stops=len(turns), counters=counters,
                transport=transport, clips=clips, absent_seconds=end-start)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--flash', action='store_true')
    parser.add_argument('--functional-nic-observe-only', action='store_true',
                        help='Explicit functional-only run: record NIC totals without gating them')
    parser.add_argument('--esptool-python', type=Path,
                        default=Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python',
                        help='IDF esptool interpreter; checked before bench acquisition')
    parser.add_argument('--bench-released', action='store_true', help='Claude has explicitly released the demo bench')
    args = parser.parse_args(argv)
    if not args.bench_released:
        parser.error('Demo owns bench; ask Claude in the report before running')
    out = args.out.resolve()
    if not out.is_relative_to(EVIDENCE):
        parser.error('--out must be under '+str(EVIDENCE))
    from step9a_echoear import check_esptool
    os.environ['ESPTOOL_PYTHON'] = str(args.esptool_python)
    check_esptool()
    out.mkdir(parents=True, exist_ok=False)
    os.environ.update(DG_BOARD='echoear', STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
                      OFFLINE_USB_ABSENCE_TURNS='1', OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
    from demo_session import nic_snapshot, prepare_board, cleanup_board
    from step9a_echoear import bench, verify, flash, check_firmware, require_echoear_image
    from step9a_assets import asset_server
    from step9a_support import host_address
    from esp_serial import EspSerial
    from step7_support import BotServer, prepare_prompt
    from alsa import resolve_jabra, set_volumes, play_wav
    from transport_test import perf
    result = dict(passed=False, interrupted=False, cable_unplug_test=False,
                  purpose='Functional reader-absence regression, not network/acoustic/battery qualification',
                  nic_gate_enforced=not args.functional_nic_observe_only,
                  elf_sha256=hashlib.sha256((ROOT/'build/dg-esp32.elf').read_bytes()).hexdigest())
    board = None
    try:
        require_echoear_image()
        with bench():
            initial = nic_snapshot()
            time.sleep(30)
            result['nic_preflight'] = network_gate(initial, nic_snapshot())
            if not args.functional_nic_observe_only and not result['nic_preflight']['passed']:
                raise RuntimeError('Host NIC contamination or missing counters; no test started')
            if args.flash:
                flash(out)
            port = verify(out)
            card, device = resolve_jabra()
            set_volumes(card, out/'mixer.log')
            prompt = prepare_prompt(out)
            with BotServer(out/'bot') as bot, asset_server():
                try:
                    board = EspSerial(port, log_path=out/'serial-before.log', display_checks=True)
                    cursor = board.cursor
                    board.reset()
                    board.wait_for('READY transport-console', 60, since=cursor)
                    result['boot_hash'] = check_firmware(board, cursor)
                    prepare_board(board, 'http://'+host_address()+':7861', out/'arena-budget.json')
                    board.command('connect', 90)
                    board.command('audio 1')
                    time.sleep(5)  # greeting/codec warmup outside the acquisition
                    result['before'] = perf(board)
                    result['strict_monitor_before'] = board.display_monitor.result()
                    board.close()
                    board = None
                    result['closed_at'] = time.monotonic()
                    result['nic_before'] = nic_snapshot()
                    for offset in (3, 23, 43):
                        while time.monotonic()<result['closed_at']+offset:
                            if bot.proc.poll() is not None:
                                raise RuntimeError('Offline bot exited during reader absence')
                            time.sleep(.25)
                        play_wav(prompt, device)
                    while time.monotonic()<result['closed_at']+60:
                        if bot.proc.poll() is not None:
                            raise RuntimeError('Offline bot exited during reader absence')
                        time.sleep(.25)
                    result['reopen_at'] = time.monotonic()
                    result['nic_arm'] = network_gate(result['nic_before'], nic_snapshot())
                    board = EspSerial(None, log_path=out/'serial-after.log', reset_on_open=False, display_checks=True)
                    # No reset, connect or audio-enable on return. Existing session only.
                    board.s.write(b'\n')
                    board.command('ping')
                    result['after'] = perf(board)
                    result['strict_monitor_after'] = board.display_monitor.result()
                    result['analysis'] = score(result['before'], result['after'], bot.events(),
                                               result['closed_at'], result['reopen_at'])
                    result['passed'] = (result['analysis']['passed']
                                        and (args.functional_nic_observe_only or result['nic_arm']['passed'])
                                        and result['strict_monitor_before']['passed']
                                        and result['strict_monitor_after']['passed'])
                finally:
                    if board is not None:
                        board.display_cleanup = True
                        result['cleanup_errors'] = cleanup_board(board)
                        board.close()
                        board = None
                    else:
                        result['cleanup_skipped'] = 'No serial handle'
    except KeyboardInterrupt:
        result['interrupted'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('before','after')}, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
