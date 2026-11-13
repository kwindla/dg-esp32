#!/usr/bin/env python3
"""Frozen-image uplink A/B/A; explicit arm selection, no retries or acoustics score.

Prepare is host-only. Each arm needs B's explicit handoff and bench.py. Device
send-return and host receive gaps are matched by full RTP identity, without
assuming synchronized clocks. Existing strict uplink limits are unchanged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path('/home/khkramer/src/evidence/echoear/B')
IMAGES = [EVIDENCE/'geometry-14284c8-reference-image',
          EVIDENCE/'item2-master-9bc2d42-normal-image']
ELFS = ['94a5a13ccd5555a8fd39710a140cf514c012d7d7e248554d632915b61ec7d444',
        '9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758']
SCRIPT = EVIDENCE/'item2-final-master-9bc2d42/strict_voice_cat/attempt-2'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_identity(path, expected):
    from demo_session import firmware_artifacts
    version = json.loads((path/'project_description.json').read_text())['project_version']
    if firmware_artifacts(path, version) != expected:
        raise ValueError('Wrong frozen ELF/bin')
    flash = json.loads((path/'flasher_args.json').read_text())
    names = {'dg-esp32.elf', 'flasher_args.json', 'project_description.json', *flash['flash_files'].values()}
    if any(not (path/name).resolve().is_relative_to(path.resolve()) for name in names):
        raise ValueError('Flash input escapes bundle')
    return dict(path=str(path), elf_sha256=expected, version=version,
                files={name: digest(path/name) for name in sorted(names)})


def prepare(out):
    images = [image_identity(p, h) for p, h in zip(IMAGES, ELFS)]
    if images[0]['files']['bootloader/bootloader.bin'] != images[1]['files']['bootloader/bootloader.bin']:
        raise ValueError('Common bootloader required')
    # Follow each original's actual frozen prompt rather than assume its name.
    original = json.loads((SCRIPT/'result.json').read_text())
    prompts = [Path(row['calibration']['prompt']['path']) for row in original['runs']]
    if len(prompts) != 3:
        raise ValueError('Three original strict prompts required')
    paths = [ROOT/p for p in subprocess.check_output(
        ['git', 'ls-files', '-z', '--', 'hil', 'server'], cwd=ROOT).decode().split('\0') if p]
    paths += [Path(__file__), SCRIPT/'result.json', *prompts]
    out.mkdir(parents=True, exist_ok=False)
    plan = dict(qualification=False, order=['old', 'new', 'old-repeat'],
                images=images, prompts=[str(p) for p in prompts],
                windows_each=3, window_seconds=24, settle_seconds=6,
                same_live_test_fixed_reply=True, cat_fps=30, volume_db=0, quiet=1,
                options={'arrival-trace': 1, 'call-timing': 1},
                mid_window_perf=False, trace_export='Only outside declared measurement windows',
                mixer='Read-only; no calibration/movement/settings changes',
                uplink_limits={'over60_rate': .01, 'over150_rate': .0005, 'maximum_ms': 2000},
                raw_whole_arm_also_retained=True, no_retries=True,
                script_basis='Exact three calibrated prompts and long fixed reply from B strict attempt-2; live STT/TTS, same cat and one wire show per reply',
                host_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                input_sha256={str(p): digest(p) for p in sorted(set(paths))})
    (out/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    print('Host-only prepared '+str(out/'plan.json'))


def verify_plan(plan):
    for name, wanted in plan['input_sha256'].items():
        if digest(Path(name)) != wanted:
            raise ValueError('Frozen input changed: '+name)
    for row in plan['images']:
        if image_identity(Path(row['path']), row['elf_sha256']) != row:
            raise ValueError('Frozen bundle changed')


def mixer(card):
    values = {}
    for control in ('PCM', 'Mic'):
        raw = subprocess.check_output(['amixer', '-c', card, 'sget', control], text=True, timeout=5)
        match = re.search(r'Mono: (Playback|Capture) (\d+) \[(\d+)%\] \[([+-]?[\d.]+)dB\] \[on\]', raw)
        values[control] = [match[1], int(match[2]), int(match[3]), float(match[4])] if match else None
    if values != {'PCM': ['Playback', 8, 73, -4.0], 'Mic': ['Capture', 6, 86, 3.0]}:
        raise ValueError('Bench mixer changed; do not reset it during comparison')
    return values


def arm(out, index):
    from step9a_echoear import verify, check_esptool, esptool_python
    from host_nic import snapshot, gate
    from echoear_transport_probe import TimedSerial
    from step7_support import BotServer
    from transport_test import wait_until
    from alsa import resolve_jabra, play_wav
    from arrival_trace import drain
    from playback_quality import transport_gate, microphone_clip_gate
    from step9a_merge_checks import memory_gate, wire_show
    from echoear_checks import codec_boot_evidence
    from e2e_common import perf
    from echoear_uplink_analysis import analyze_arm

    if not os.getenv('DG_BENCH_LOCK_FD'):
        raise ValueError('Use bench.py after explicit B grant')
    plan = json.loads((out/'plan.json').read_text())
    verify_plan(plan)
    if index > 1 and not (out/f'arm{index-1}/result.json').exists():
        raise ValueError('Previous original arm not completed')
    image = plan['images'][1 if index == 2 else 0]
    path = out/f'arm{index}';path.mkdir(exist_ok=False)
    os.environ.update(DG_BOARD='echoear', STEP9A_FIXTURES=str(ROOT/'hil/fixtures/echoear360'),
        STEP9A_BUDGET_PATH=str(path/'budget.json'), ECHOEAR_STRICT_MERGE='1',
        DISPLAY_HIL_COMMANDS=str(path/'display-command.json'), RTP_EVIDENCE_DIR=str(path/'rtp'),
        RTP_EVIDENCE_MAX_PACKETS='20000', STEP8E_EVENT_LOOP_PROBE='1',
        PIPECAT_SCTP_MAX_CHUNK_SIZE='1100', ECHOEAR_AEC_FULL_DUPLEX='0',
        OFFLINE_USB_ABSENCE_TURNS='0', OFFLINE_RTVI_BURST_SWEEP='0', OFFLINE_BENCH_CONTINUOUS_AUDIO='0')
    for name in ('OFFLINE_BENCH_FIXTURE_DIR', 'ECHOEAR_ACOUSTIC_QUALIFICATION'):
        os.environ.pop(name, None)
    # Display modules bind environment-selected paths at import time.
    from step9a_assets import asset_server
    from step9a_support import setup, host_address
    check_esptool()
    result = dict(qualification=False, image=image, role=plan['order'][index-1],
                  windows=[], cleanup_errors=[], nic_before=snapshot(), completed=False)
    board = None
    def save():
        (path/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    try:
        port = verify(path)
        flash = json.loads((Path(image['path'])/'flasher_args.json').read_text())
        command = [esptool_python(), '-m', 'esptool', '--chip', 'esp32s3', '--port', port,
                   '--baud', '460800', '--after', 'no-reset', 'write-flash', *flash['write_flash_args']]
        for address, name in flash['flash_files'].items():command += [address, name]
        with (path/'flash.log').open('w') as log:
            subprocess.run(command, cwd=image['path'], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=100)
        card, device = resolve_jabra();result['mixer_before'] = mixer(card)
        with asset_server(), BotServer(path/'bot', 'live-test') as bot, TimedSerial(
                port, log_path=path/'serial.log', display_checks=True, reset_on_open=False) as board:
            try:
                board.reset();board.wait_for('READY transport-console', 35, since=0)
                board.wait_for('elf_sha256='+image['elf_sha256'], 5, since=0)
                result['codec_boot'] = codec_boot_evidence('\n'.join(row[2] for row in board.lines_since(0)))
                if not result['codec_boot']['passed']:raise ValueError('Codec startup failed')
                for text in ('audio 0', 'quiet 1', 'playback-volume 0',
                             'server-url http://'+host_address()+':7861', 'mic-gain-status'):
                    board.command(text)
                initial = perf(board)
                (path/'budget.json').write_text(json.dumps(dict(budget=491520, F=initial['heap']['psram']['free'],
                    W=1287760, largest=initial['heap']['psram']['largest']))+'\n')
                result['display_setup'] = setup(board, mode='cat')
                for name, value in plan['options'].items():board.command(f'peer-option {name} {value}')
                board.command('connect', 90);board.command('audio 1');board.command('quiet 1')
                time.sleep(plan['settle_seconds'])
                result['whole_before'] = perf(board);pc = result['whole_before']['pc_id']
                for i, prompt in enumerate(plan['prompts'], 1):
                    before = perf(board)
                    drain(board, path/f'trace{i}-before.jsonl')
                    time.sleep(.5)
                    row = dict(index=i, before=before, start_s=time.monotonic())
                    result['windows'].append(row);save()
                    cursor = board.cursor
                    row['prompt_start_s'] = time.monotonic();play_wav(prompt, device)
                    row['prompt_end_s'] = time.monotonic()
                    board.wait_for('GATE closed', 10, since=cursor)
                    first = wait_until(lambda: next((e for e in bot.events() if e.get('pc_id') == pc and
                        e['event']=='first-tts-byte' and e['t'] >= row['start_s']), None), 10)
                    row['first_tts_s'] = first['t']
                    row['wire_show'] = wire_show(board, bot, pc, os.environ['DISPLAY_HIL_COMMANDS'], f'uplink-show-{i}')
                    opened = board.wait_for('GATE open', max(.1, row['start_s']+23-time.monotonic()), since=cursor)
                    row['gate_observed_s'] = time.monotonic();row['gate_line'] = opened
                    time.sleep(max(0, row['start_s']+plan['window_seconds']-time.monotonic()))
                    row['end_s'] = time.monotonic()
                    row['after'] = perf(board)
                    drain(board, path/f'trace{i}.jsonl')
                    row['export_trace'] = perf(board)['peer_profile']['trace']
                    row['transport'] = transport_gate(before, row['after'])
                    row['clips'] = microphone_clip_gate(before, row['after'])
                    row['memory'] = memory_gate([before, row['after']])
                    if not row['clips']['passed'] or not row['memory']['passed']:
                        raise ValueError('Clip or resource gate failed')
                    save();print(result['role'], 'turn', i, 'acquired', flush=True)
                result['whole_after'] = perf(board)
                result['monitor'] = board.display_monitor.result()
                result['mixer_after'] = mixer(card)
                result['completed'] = True
            finally:
                board.display_cleanup = True
                for text in ('audio 0', 'disconnect', 'disp bench off', 'playback-volume -6'):
                    try:board.command(text, 35)
                    except Exception as exc:result['cleanup_errors'].append(text+': '+str(exc))
                result['monitor'] = board.display_monitor.result()
    except BaseException as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
    finally:
        result['nic_after'] = snapshot();result['nic'] = gate(result['nic_before'], result['nic_after'])
        save()
    verify_plan(plan)
    result['analysis'] = analyze_arm(path, result)
    save();print(json.dumps(dict(role=result['role'], completed=result['completed'],
        summary=result['analysis'].get('summary'), error=result.get('error'))), flush=True)
    return 0 if result['completed'] and not result['cleanup_errors'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--arm', type=int, choices=(1, 2, 3))
    args = parser.parse_args()
    if args.prepare:prepare(args.out)
    else:raise SystemExit(arm(args.out, args.arm))
