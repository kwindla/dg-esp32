#!/usr/bin/env python3
"""One short functional acquisition: three reconnects, strict cumulative gates.

No campaign retakes or scored acoustic/STT qualification. NIC totals are
observed on both interfaces; only wlp8s0 is the board path.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time

from playback_quality import TRANSPORT_COUNTERS

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path('/home/khkramer/src/evidence/echoear/A')
CLIP_COUNTERS = ('mic_in_turn_clips', 'mic_gate_open_clips', 'mic_gate_closed_clips')


def strict_session(sample):
    """Include startup counters; a dirty first snapshot cannot become baseline."""
    values = {k: sample.get(k) for k in TRANSPORT_COUNTERS + CLIP_COUNTERS + ('preconnect_packets',)}
    rejected = sample.get('playout', {}).get('rejected', {})
    zero = lambda v: type(v) is int and v == 0
    return dict(values=values, rejected=rejected, passed=(
        sample.get('playout_schema') == 2 and all(zero(v) for v in values.values())
        and bool(rejected) and all(zero(v) for v in rejected.values())))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--firmware-ref', required=True)
    p.add_argument('--bench-released', action='store_true')
    p.add_argument('--flash', action='store_true')
    p.add_argument('--chat-delay-ms', type=int, choices=(0, 3000), default=0)
    a = p.parse_args()
    if not a.bench_released:
        p.error('Explicit bench release required')
    out = a.out.resolve()
    if not out.is_relative_to(EVIDENCE):
        p.error('Evidence must remain under ' + str(EVIDENCE))
    os.environ.update(DG_BOARD='echoear', STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        OFFLINE_SKIP_GREETING='1', OFFLINE_USB_ABSENCE_TURNS='1',
        OFFLINE_BENCH_CONTINUOUS_AUDIO='0', PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
    os.environ.setdefault('ESPTOOL_PYTHON', str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))
    if a.chat_delay_ms:
        os.environ['PRECHAT_PROBE_HOLD_MS'] = str(a.chat_delay_ms)
    from demo_session import preflight, prepare_board, cleanup_board
    from step9a_echoear import bench, flash, verify, check_firmware
    from step7_support import BotServer, prepare_prompt
    from alsa import resolve_jabra, set_volumes, play_wav
    from step9a_assets import asset_server
    from step9a_support import host_address
    from echoear_status_assets import provision_status
    from esp_serial import EspSerial
    from transport_test import perf
    from playback_quality import transport_gate, microphone_clip_gate
    from host_nic import snapshot, gate
    result = dict(preflight(a.firmware_ref), passed=False, cycles=[],
        purpose='Functional pre-chat admission diagnosis; not scored qualification',
        nic_gate_enforced=False, chat_delay_ms=a.chat_delay_ms)
    out.mkdir(parents=True, exist_ok=False)

    def save():
        (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')

    try:
        with bench():
            print('Bench acquired: one flash and three reconnects, then release', flush=True)
            if a.flash:
                flash(out)
            port = verify(out)
            card, device = resolve_jabra()
            set_volumes(card, out/'mixer.log')
            prompt = prepare_prompt(out)
            with ExitStack() as stack:
                bot = stack.enter_context(BotServer(out/'bot', 'offline-test',
                    entrypoint='prechat_probe_bot.py' if a.chat_delay_ms else 'bot.py'))
                stack.enter_context(asset_server())
                board = EspSerial(port, log_path=out/'serial.log', display_checks=True)
                stack.callback(board.close)
                try:
                    cursor = board.cursor
                    board.reset()
                    board.wait_for('READY transport-console', 60, since=cursor)
                    result['boot_hash'] = check_firmware(board, cursor)
                    board.command('demo hold', 45)
                    result['setup'] = prepare_board(board, 'http://'+host_address()+':7861', out/'arena-budget.json')
                    board.command('disp character off')
                    result['status_assets'] = provision_status(board, 'http://'+host_address()+':7874')
                    board.command('disp demo on')
                    board.command('connect', 90)
                    board.command('audio 1')
                    result['initial'] = perf(board)
                    if not strict_session(result['initial'])['passed']:
                        raise AssertionError('Initial session cumulative counters are nonzero/unavailable')
                    previous = result['initial']['pc_id']
                    for number in range(1, 4):
                        arm = dict(number=number, passed=False, nic_before=snapshot())
                        result['cycles'].append(arm)
                        cursor = board.cursor
                        board.command('request-reconnect')
                        board.wait_for('RECONNECT admitted=1', 3, since=cursor)
                        arm['ready'] = board.wait_for('STATE Ready', 90, since=cursor)
                        before = arm['before'] = perf(board)
                        arm['startup_gate'] = strict_session(before)
                        if not arm['startup_gate']['passed']:
                            raise AssertionError('Fresh-session cumulative counter failure')
                        started = time.monotonic()
                        play_wav(prompt, device)
                        deadline = time.monotonic()+25
                        while not any(e['event']=='bot-audio-end' and e['pc_id']==before['pc_id']
                                      and e['t']>=started for e in bot.events()):
                            if time.monotonic()>=deadline:
                                raise TimeoutError('Fixture reply did not finish')
                            time.sleep(.05)
                        board.wait_for(r'GATE open t=', 10, since=cursor)
                        time.sleep(2)
                        after = arm['after'] = perf(board)
                        arm['cumulative_gate'] = strict_session(after)
                        arm['transport'] = transport_gate(before, after)
                        arm['clips'] = microphone_clip_gate(before, after)
                        arm['nic_observation'] = gate(arm['nic_before'], snapshot())
                        count = after.get('rtp_before_chat_packets')
                        first = after.get('first_rtp_before_chat_us')
                        channel = after['connect_timings']['channel_open_us']
                        arm['prechat_ordering'] = (type(count) is int and count >= 0 and
                            ((count == 0 and first == 0) or (count > 0 and 0 < first < channel)))
                        arm['prechat_budget_activations'] = after['peer_profile'].get('prechat_receive_budget_activations')
                        arm['prechat_budget'] = (arm['prechat_budget_activations'] == (1 if count else 0))
                        if a.chat_delay_ms:
                            arm['prechat_budget'] &= count >= 50 and channel-first >= 2000000
                        arm['passed'] = (arm['cumulative_gate']['passed'] and arm['transport']['passed']
                            and arm['clips']['passed'] and arm['prechat_ordering'] and arm['prechat_budget']
                            and after['pc_id'] == before['pc_id'] != previous
                            and after['sent'] > before['sent'] and after['received'] > before['received'])
                        previous = after['pc_id']
                        save()
                        print(json.dumps(dict(cycle=number, passed=arm['passed'], prechat=count)), flush=True)
                        if not arm['passed']:
                            raise AssertionError('Reconnect transport/clip/continuity gate failure')
                    result['final_perf'] = perf(board)
                    samples = [result['initial'], result['final_perf']] + [
                        arm[key] for arm in result['cycles'] for key in ('before', 'after')]
                    result['minimum_internal'] = min(s['heap']['internal']['minimum_free'] for s in samples)
                    result['largest_internal'] = min(s['heap']['internal']['largest'] for s in samples)
                    result['tx_stack_minimum'] = min(t['stack_free_bytes'] for s in samples
                        for t in s['tasks'] if t['name']=='audio-tx')
                    result['prechat_total'] = sum(s.get('rtp_before_chat_packets', 0)
                        for s in [result['initial']] + [arm['after'] for arm in result['cycles']])
                    result['monitor'] = board.display_monitor.result()
                    result['checks'] = dict(three_reconnects=len(result['cycles'])==3,
                        all_cycles=all(arm['passed'] for arm in result['cycles']),
                        prechat_observed=result['prechat_total']>0,
                        all_snapshots_strict=all(strict_session(s)['passed'] for s in samples),
                        internal_floor=result['minimum_internal']>=81920,
                        largest=result['largest_internal']>=32768,
                        tx_stack=result['tx_stack_minimum']>=4096,
                        allocations=all(s['failed_allocations']['total']==0 for s in samples),
                        monitor=result['monitor']['passed'])
                    result['passed'] = all(result['checks'].values())
                finally:
                    board.display_cleanup = True
                    result['cleanup_errors'] = cleanup_board(board)
                    result['passed'] &= not result['cleanup_errors']
    except KeyboardInterrupt:
        result.update(interrupted=True, passed=False)
    except Exception as exc:
        result.update(error=type(exc).__name__+': '+str(exc), passed=False)
    finally:
        save()
    print(json.dumps({k:result.get(k) for k in ('passed', 'error', 'prechat_total',
        'minimum_internal', 'tx_stack_minimum', 'checks', 'cleanup_errors')}), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
