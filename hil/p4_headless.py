#!/usr/bin/env python3
"""Coordinator-only headless acquisition under bench.py; imports perform no IO.

Live acquisition uses board_smoke's RestoreHold/P4Hold unchanged. Analysis runs
only after that hold exits: an analysis failure leaves a successfully acquired
P4 installed, reports exit 2, and cannot initiate a factory restore.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import boards
from board_smoke import (LiveIO, NORMAL_HASH, BOUNDS_FILE, bounds_for, live_bot_identity,
                         process_token, verify_flash_mac, recorder_path, RecorderTail)
from make_bundle import verify_bundle, local_file, digest
from netprobe import run_streams, decode_dump, analyze_streams
from wire_contract import require

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = Path.home()/'src/evidence/echoear/p4/step-1a/coordinator-net-20261031T090701Z/network/result.json'


def write(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def check_pair_left(args, identity):
    if not args.pair_left:
        return
    require(args.board == 'p4xc', '--pair-left requires P4')
    left = json.loads((args.pair_left/'acquisition.json').read_text())
    require(left.get('board') == 'echoear' and left.get('boot_verified') is True and
            left.get('acquisition_passed') is True, 'pair-left S3 acquisition did not pass')
    require(left.get('wire_sha256') == digest(args.pair_left/'wire.log'), 'pair-left wire changed')
    require(json.loads((args.pair_left/'identity.json').read_text()) == identity, 'pair-left bot identity differs')


def console_command(board, command, timeout=5, *, uart=False, out=None):
    """Add UART timeout diagnostics without retrying the failed command."""
    if not uart:
        return board.command(command, timeout)
    try:
        return board.command(command, timeout)
    except TimeoutError as error:
        try:
            lines = [entry[2] for entry in board.lines_since(0)]
        except Exception:
            # A large dump can expire cursor zero in the reader's bounded ring.
            lines = []
            for pattern in (r'^PERF ', r'.+'):
                try:
                    entry = board.latest_entry(pattern)
                    if entry is not None: lines.append(entry[2])
                except Exception: pass
        last_line = lines[-1] if lines else '<none>'
        drops = None
        for line in reversed(lines):
            if line.startswith('PERF '):
                try: drops = dict(source='last PERF', **json.loads(line[5:])['console_output'])
                except (ValueError, KeyError, TypeError): pass
                break
        # A compact, new diagnostic command may succeed after the burst drains.
        # Never retry the original command, and never suppress its timeout.
        cursor = board.cursor
        try:
            board.command('console-drops', 2)
        except Exception:
            pass
        try: diagnostics = board.lines_since(cursor)
        except Exception: diagnostics = []
        for _, _, line in diagnostics:
            match = re.fullmatch(r'CONSOLE_DROPS full=(\d+) transport_drops=(\d+) ack_pending=(\d+)', line)
            if match:
                drops = dict(source='console-drops', **dict(zip(('full', 'transport_drops', 'ack_pending'), map(int, match.groups()))))
        words = command.split()
        detail = dict(command=words[:2] if words[0] in ('netprobe','disp','demo') else words[:1],
                      last_line=last_line[-512:], console_drops=drops if drops is not None else 'unavailable')
        if out is not None:
            try: write(out/'console-ack-timeout.json', detail)
            except OSError: pass
        raise TimeoutError('P4 ACK timeout '+json.dumps(detail, separators=(',', ':'))) from error


def verify_network_dump(text, stats, *, require_check):
    """Refuse missing rows even when END/ACK arrived; UART also needs CHECK."""
    begun = ended = False; check = None; rows = []; records = 0
    for line in text.splitlines():
        if line.startswith('NETPROBE_DUMP BEGIN'):
            require(not begun and line == 'NETPROBE_DUMP BEGIN version=1 record_bytes=16', 'invalid/duplicate netprobe BEGIN')
            begun = True
        elif line.startswith('NETPROBE_ROW '):
            require(begun and not ended and check is None, 'netprobe row outside dump')
            raw = base64.b64decode(line.split(' ', 1)[1], validate=True)
            require(raw and len(raw) % 16 == 0 and len(raw) <= 384, 'invalid netprobe record extent')
            rows.append(line); records += len(raw)//16
        elif line.startswith('NETPROBE_DUMP CHECK'):
            require(begun and not ended and check is None, 'duplicate/outside netprobe CHECK')
            match = re.fullmatch(r'NETPROBE_DUMP CHECK lines=(\d+) sha256=([0-9a-f]{64})', line)
            require(match is not None, 'malformed netprobe CHECK'); check = (int(match[1]), match[2])
        elif line == 'NETPROBE_DUMP END':
            require(begun and not ended, 'invalid/duplicate netprobe END'); ended = True
    require(begun and ended, 'missing netprobe BEGIN/END')
    expected = sum(path['received']+path['send_errors']-path['overwritten'] for path in stats['paths'])
    require(records == expected, f'netprobe record count mismatch: received={records} expected={expected}')
    require(not require_check or check is not None, 'missing netprobe CHECK')
    if check is not None:
        require(check == (len(rows), hashlib.sha256('\n'.join(rows).encode('ascii')).hexdigest()), 'netprobe CHECK count/SHA256 mismatch')
    return records


def acquire(args, io, manifest, flash):
    """Device commands and raw evidence only; caller owns the restoration hold."""
    from board_serial import open_board
    fd = boards.require_bench(); port = verify_flash_mac(args.board)
    def issue(board, text, timeout=5):
        return console_command(board, text, timeout, uart=args.board=='p4xc', out=args.out)
    base = [io.python, '-m', 'esptool', '--chip', boards.BOARDS[args.board]['chip'],
            '--port', port, '--baud', '921600', '--after', 'no-reset']
    files = []
    for address, name in flash['flash_files'].items():
        files.extend((address, str(local_file(args.bundle, name))))
    commands = [base+['write-flash', *flash['write_flash_args'], *files], base+['verify-flash', *files]]
    write(args.out/'flash-commands.json', commands)
    for index, command in enumerate(commands):
        with (args.out/f'flash-{index}.log').open('w') as log:
            subprocess.run(command, check=True, timeout=180, pass_fds=(fd,), stdout=log, stderr=subprocess.STDOUT)
    with open_board(args.board, port=port, log_path=args.out/'serial.log') as board:
        at = board.cursor; board.reset(); board.wait_for('READY transport-console', 90, since=at)
        boot = [line for _, _, line in board.lines_since(at)]
        require(any(f'DG_BOOT board={args.board} chip={boards.BOARDS[args.board]["chip"]}' in line for line in boot), 'boot board/chip mismatch')
        require(any('elf_sha256='+manifest['elf_sha256'] in line for line in boot), 'boot ELF mismatch')
        require(any('WIFI READY' in line for line in boot), 'WiFi join not witnessed')
        for command in ('demo hold', 'server-url '+args.server_url, 'quiet 1', 'playback-volume -6', 'gate closed', 'audio 0'):
            issue(board, command, 45)
        if args.board == 'echoear': issue(board, 'disp init', 60)
        def snapshot(label):
            start = board.cursor; issue(board, 'stats', 30)
            (args.out/(label+'.log')).write_text('\n'.join(line for _, _, line in board.lines_since(start))+'\n')
        snapshot('idle-heap-stack'); start = board.cursor
        run_streams([f'esp-0,{args.host},{args.port}'], 60, 50, 130, args.out/'udp.jsonl', interface=args.interface,
                    ready=lambda: issue(board, f'netprobe start {args.host} {args.port} 50 130 1'),
                    finish=lambda: issue(board, 'netprobe stop', 15))
        try:
            issue(board, 'netprobe stats', 15); issue(board, 'netprobe dump', 60)
        finally:
            (args.out/'network-serial.log').write_text('\n'.join(line for _, _, line in board.lines_since(start))+'\n')
        snapshot('after-netprobe-heap-stack')
        serial_at = board.cursor
        issue(board, 'connect', 100); issue(board, 'audio 1')
        ready_at, _, ready = board.wait_for_entry(r'\bSTATE Ready gen=\d+', 15, since=serial_at)
        generation = int(re.search(r'gen=(\d+)', ready)[1]); cursor = ready_at
        end = time.monotonic()+60
        while time.monotonic() < end:
            require(process_token(io.pid) == io.identity['bot_instance'], 'bot changed/exited')
            for index, _, line in board.lines_since(cursor):
                cursor = index
                require(not re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE|Guru Meditation|panic.ed|Stack canary', line, re.I), line)
                require(not re.search(r'\bSTATE (Disconnected|Starting|Idle) gen=\d+', line), 'session lost: '+line)
            time.sleep(.1)
        snapshot('connected-heap-stack')
        issue(board, 'uplink-intervals', 15); board.wait_for(r'^UPLINK_INTERVALS gen=', 15, since=serial_at)
        (args.out/'wire.log').write_text('\n'.join(line for _, _, line in board.lines_since(serial_at))+'\n')
        issue(board, 'disconnect', 45); issue(board, 'audio 0'); snapshot('disconnected-heap-stack')
    return dict(boot_verified=True, session_generation=generation)


def analyze(args, raw):
    """Host parsing, resource gates, reference comparisons and wire verdicts."""
    from step9a_merge_checks import memory_gate
    samples = []
    for label in ('idle', 'after-netprobe', 'connected', 'disconnected'):
        path = args.out/(label+'-heap-stack.log')
        perf = json.loads(next(line[5:] for line in path.read_text().splitlines() if line.startswith('PERF ')))
        write(path.with_suffix('.json'), perf); samples.append(perf)
    memory = memory_gate(samples); write(args.out/'memory-gate.json', memory); require(memory['passed'], 'memory gate failed')
    connected = samples[2]; peers = [task for task in connected['tasks'] if task['name'] == 'peer-loop']
    require(peers and min(task['stack_free_bytes'] for task in peers) >= 4096, 'peer-loop stack floor')
    write(args.out/'connected-task-stacks.json', connected['tasks'])
    network = (args.out/'network-serial.log').read_text()
    stats = json.loads(next(line.split(' ', 1)[1] for line in network.splitlines() if line.startswith('NETPROBE ')))
    records = verify_network_dump(network, stats, require_check=args.board=='p4xc')
    write(args.out/'network-dump-integrity.json', dict(records=records, check_required=args.board=='p4xc', verified=True))
    summary = analyze_streams(args.out/'udp.jsonl', device_rows=decode_dump(network), device_stats=stats)
    write(args.out/'network.json', dict(device=stats, host=summary))
    reference = json.loads(REFERENCE.read_text())['udp_host']['esp-0']; current = summary['esp-0']; comparison = {}
    for key in ('p50', 'p99', 'max'):
        before, after = reference['rtt_ms'][key], current['rtt_ms'][key]
        require(after is not None, 'missing RTT witness')
        comparison['rtt_ms_'+key] = dict(step1a=before, product=after, delta=after-before)
    for direction in ('uplink', 'downlink'):
        before, after = reference[direction]['loss_pct'], current[direction]['loss_pct']
        comparison[direction+'_loss_pct'] = dict(step1a=before, product=after, delta=after-before)
        comparison[direction+'_jitter_ms'] = dict(step1a=reference[direction]['jitter_ms'], product=current[direction]['jitter_ms'])
    write(args.out/'network-vs-step1a.json', comparison)
    recorder = RecorderTail(recorder_path(args.bot_dir, connected['pc_id']), connected['pc_id'])
    events = recorder.poll(); write(args.out/'bot-events.json', events)
    profiles = [row for row in events if row.get('kind') == 'stt-profile']
    require(len(profiles) == 1, 'missing bot profile witness')
    observed = {key: value for key, value in profiles[0].items() if key not in ('kind', 'monotonic_ns', 'pc_id')}
    identity = json.loads((args.out/'identity.json').read_text())
    require(observed == identity['profile_manifest'], 'bot profile changed')
    bounds = bounds_for(BOUNDS_FILE, 'normal'); left = args.pair_left or args.out
    left_target = 'esp32s3' if args.pair_left or args.board == 'echoear' else 'esp32p4'
    command = ['uv', 'run', '--project', str(ROOT/'server'), '--frozen', '--offline', '--group', 'dev', 'python',
               str(ROOT/'hil/wire_contract.py'), str(left/'wire.log'), str(args.out/'wire.log'),
               '--profile', 'headless', '--left-aec', '0', '--right-aec', '0', '--left-target', left_target,
               '--right-target', boards.BOARDS[args.board]['chip'], '--left-identity', str(left/'identity.json'),
               '--right-identity', str(args.out/'identity.json'), '--stall-us', str(bounds.stall_us),
               '--p99-delta-us', str(bounds.p99_delta_us), '--coverage', str(bounds.coverage)]
    write(args.out/'wire-pair-command.json', command)
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT/'hil')+':'+str(ROOT/'server'))
    with (args.out/'wire-pair.log').open('w') as log:
        subprocess.run(command, cwd=ROOT, env=env, check=True, stdout=log, stderr=subprocess.STDOUT)
    return dict(wire_pair_checked=bool(args.pair_left), wire_sha256=digest(args.out/'wire.log'))


def execute(args, *, io=None, acquire_fn=acquire, analyze_fn=analyze):
    manifest, flash = verify_bundle(args.bundle, args.board)
    require(manifest['profile'] == 'normal' and not manifest['aec_enabled'], 'normal AEC-off bundle required')
    identity = json.loads(args.bot_identity.read_text()); check_pair_left(args, identity)
    lock = ROOT/('dependencies.p4xc.lock' if args.board == 'p4xc' else 'dependencies.lock')
    args.out.mkdir(parents=True, exist_ok=False)
    image = dict(manifest, target_dependencies_lock_sha256=digest(lock), target_dependencies_lock=str(lock))
    write(args.out/'image.json', image); write(args.out/'identity.json', identity)
    result = dict(board=args.board, acquisition_passed=False, boot_verified=False, qualification=False,
                  target_dependencies_lock_sha256=image['target_dependencies_lock_sha256'])
    guard = None
    try:
        io = io or LiveIO(args, args.out, manifest, identity)
        io.preflight_restore(); guard = io.hold()
        with guard:
            guard.touch()
            result.update(acquire_fn(args, io, manifest, flash))
            guard.success = True  # P4 keep-tested; S3 RestoreHold always restores 55152f25.
        result['restore'] = guard.record
        require(guard.record.get('clean') is True, 'restore required retry; inspect restore evidence')
        result['s3_disposition' if args.board == 'echoear' else 'p4_disposition'] = (
            NORMAL_HASH if args.board == 'echoear' else 'leave tested image')
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc), device_acquisition_failed=True)
        if guard is not None: result['restore'] = guard.record
        write(args.out/'acquisition.json', result)
        return result
    finally:
        if guard is not None: guard.release_signals()
    # No parsing, scoring or offline verdict runs inside the restoration hold.
    try:
        result.update(analyze_fn(args, result)); result['acquisition_passed'] = True
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc), analysis_failed=True,
                      analysis_disposition='acquired P4 left installed' if args.board == 'p4xc' else 'S3 already restored')
    write(args.out/'acquisition.json', result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--board', choices=('echoear', 'p4xc'), required=True)
    p.add_argument('--bundle', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p.add_argument('--bot-dir', type=Path, required=True); p.add_argument('--bot-identity', type=Path, required=True)
    p.add_argument('--bot-repo', type=Path, default=Path.home()/'src/dg-esp32')
    p.add_argument('--host', default='192.168.7.222'); p.add_argument('--server-url', default='http://192.168.7.222:7861')
    p.add_argument('--pair-left', type=Path); p.add_argument('--interface', default='wlp8s0'); p.add_argument('--port', type=int, default=47010)
    p.add_argument('--factory-backup', dest='p4_factory_backup', type=Path, default=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z')
    p.set_defaults(p4_restore_bundle=None, p4_restore_elf=None)
    args = p.parse_args(argv); boards.require_bench()
    result = execute(args); print(json.dumps(result))
    return 0 if result['acquisition_passed'] else 2 if result.get('analysis_failed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
