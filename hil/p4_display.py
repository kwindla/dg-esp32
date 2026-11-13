#!/usr/bin/env python3
"""Step 13a coordinator acquisition and offline scan-latency/pair analysis.

No bot lifecycle, audio IO or touch. Live commands require the bench runner;
imports and offline commands never open devices. Frozen helpers are reused.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from display_session import display_session, display_sent, display_result, stale_bridge_sends

ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = 'p4-display-load'


def require(condition, message):
    if not condition: raise ValueError(message)


def write_new(path, data):
    with Path(path).open('x') as stream: stream.write(json.dumps(data, indent=2)+'\n')


def latency(lines):
    """Exact sampled callback timestamps; superseded samples stay separate."""
    samples, superseded, untimed, underruns, seen = [], 0, 0, 0, set()
    for line in lines:
        if 'underrun happens' in line: underruns += 1
        if 'P4_SCAN ' not in line: continue
        row = json.loads(line.split('P4_SCAN ', 1)[1])
        require(type(row.get('id')) is int and row['id'] > 0 and row['id'] % 16 == 1,
                'invalid scan sample id')
        require(row['id'] not in seen, 'duplicate scan sample; analyze one boot at a time')
        seen.add(row['id'])
        require(row.get('sample_every') == 16 and type(row.get('timed')) is bool, 'invalid scan sample metadata')
        require(row.get('outcome') in ('presented', 'superseded'), 'invalid scan outcome')
        for key in ('committed_us32', 'completed_us32', 'latency_us'):
            require(type(row.get(key)) is int and 0 <= row[key] <= 0xffffffff, 'invalid timestamp')
        if row['outcome'] == 'superseded': superseded += 1
        if not row['timed']:
            untimed += 1
            continue
        elapsed = (row['completed_us32']-row['committed_us32']) & 0xffffffff
        require(row['latency_us'] == elapsed, 'scan timestamp/latency mismatch')
        if row['outcome'] == 'presented': samples.append(elapsed)
    require(samples, 'no timed presented samples')
    samples.sort(); histogram = {}
    for value in samples:
        bucket = str(value//1000*1000); histogram[bucket] = histogram.get(bucket, 0)+1
    def quantile(percent): return samples[max(0, math.ceil(len(samples)*percent/100)-1)]
    return dict(sample_every=16, presented_samples=len(samples), superseded_samples=superseded,
                superseded_fraction=superseded/len(seen), underrun_lines=underruns,
                untimed_samples=untimed, total_records=len(seen), bin_width_us=1000,
                histogram_lower_edge_us=histogram, p50_us=quantile(50), p95_us=quantile(95),
                p99_us=quantile(99), min_us=samples[0], max_us=samples[-1],
                basis='commit after msync to second frame-complete ISR; deterministic 1-in-16 sample',
                tearing='unverified', qualification=False)


def bot_identity(args, identity):
    """Read-only identity check for the coordinator's existing bench bot."""
    from board_smoke import process_token
    from wire_contract import identity_schema
    identity_schema(identity)
    pid = int((args.bot_dir/'bot.pid').read_text())
    require(identity['bot_instance'] == process_token(pid), 'bench bot process changed')
    repo = args.bot_repo.resolve()
    command = (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
    require(str(repo/'server/bot.py').encode() in command, 'expected unchanged server/bot.py')
    for field, name in (('bot_source_sha', 'bot.py'), ('policy_source_sha', 'demo_policy.py')):
        expected = subprocess.check_output(['git', 'rev-parse', 'HEAD:server/'+name], cwd=repo, text=True).strip()
        actual = subprocess.check_output(['git', 'hash-object', 'server/'+name], cwd=repo, text=True).strip()
        require(expected == actual == identity[field], 'bench bot source identity differs')
    profile = identity['profile_manifest']
    require(profile.get('entrypoint') == 'server/bot.py' and profile.get('BOT_MODE') == 'offline-test' and
            profile.get('SERVER_PORT') == '7861' and profile.get('DISPLAY_RETRY_BUDGET') == '0', 'bench bot profile differs')
    require(Path(profile.get('DISPLAY_HIL_COMMANDS', '')).resolve() == args.commands.resolve(), 'bench bot command path differs')
    require((args.bot_dir/'events.jsonl').is_file() and (args.bot_dir/'bot.log').is_file(), 'bench bot evidence files missing')
    return pid


def acquire(args, io, manifest, identity):
    import boards
    from board_serial import open_board
    from board_smoke import boot_check, process_token, verify_flash_mac
    from make_bundle import local_file, verify_bundle
    from step9a_assets import asset_server
    from step9a_support import read, terminal, host_address
    from step9a_merge_checks import publish_command, rendered_show
    from demo_session import prepare_board
    from echoear_status_assets import provision_status
    from transport_test import wait_until
    pid = bot_identity(args, identity)
    io.flash(args.bundle, manifest, 'candidate')
    _, flash = verify_bundle(args.bundle, args.board)
    port = verify_flash_mac(args.board)
    command = [io.python, '-m', 'esptool', '--chip', boards.BOARDS[args.board]['chip'],
               '--port', port, '--after', 'no-reset', 'verify-flash']
    for address, name in flash['flash_files'].items(): command += [address, str(local_file(args.bundle, name))]
    with (args.out/'candidate-verify.log').open('w') as log:
        subprocess.run(command, check=True, timeout=180, pass_fds=(boards.require_bench(),), stdout=log, stderr=subprocess.STDOUT)
    with open_board(args.board, port=port, log_path=args.out/'serial.log') as board, asset_server():
        # P4Serial already resets on open and waits for DG_BOOT. A second
        # reset could race that first boot's READY and mixes scan id epochs.
        start = 0 if args.board == 'p4xc' else board.cursor
        if args.board == 'echoear': board.reset()
        board.wait_for('READY transport-console', 90, since=start)
        boot_check([line for _, _, line in board.lines_since(start)], manifest, args.board)
        if args.board == 'p4xc':
            require(any('800x800 RGB565 LE framebuffer PSRAM=1 DPI=40MHz' in line
                        for _, _, line in board.lines_since(start)), 'missing product DSI configuration')
        # No persisted setting is changed. Demo images are held during this
        # display-only run; normal images are initialized by the existing helper.
        board.command('demo hold', 45)
        setup = prepare_board(board, args.server_url, args.out/'arena-budget.json', volume_db=-6)
        origin = 'http://'+host_address()+':7874'
        status_assets = provision_status(board, origin)
        screens = []
        for screen, number in (('disconnected', 0), ('connecting', 1), ('live', 2)):
            previous = read(board, 'demo-proof', 'DISPLAY_DEMO_PROOF')['total']
            board.command('disp demo on'); board.command('disp demo-screen '+screen)
            def screen_ready():
                proof = read(board, 'demo-proof '+str(previous+1), 'DISPLAY_DEMO_PROOF')
                return next((row for row in proof['items'] if row['screen'] == number), None)
            proof = wait_until(screen_ready, 15); require(proof is not None, 'missing committed demo screen '+screen)
            screens.append(dict(screen=screen, proof=proof))
        board.command('disp demo off'); board.command('disp character off')
        event_file, bot_log = args.bot_dir/'events.jsonl', args.bot_dir/'bot.log'
        event_at, log_at = event_file.stat().st_size, bot_log.stat().st_size
        serial_at = board.cursor
        def events():
            with event_file.open('rb') as stream: stream.seek(event_at); text = stream.read().decode()
            return [json.loads(line) for line in text.splitlines() if line.endswith('}')]
        def alive(): require(process_token(pid) == identity['bot_instance'], 'bench bot restarted/exited')
        pairs = []
        image = setup['images'][0]
        load_body = dict(hash=image['hash'], bytes=image['bytes'], url=origin+'/'+image['hash']+'.dgi')
        # Replace the previous session's command BEFORE display-ready so the
        # first logged admission is the known load id on both boards.
        publish_command(args.commands, 'load', load_body, REQUEST_ID)
        try:
            board.command('connect', 100); board.wait_for(r'\bSTATE Ready gen=\d+', 30, since=serial_at)
            device_status = read(board, 'status', 'DISPLAY')
            session = wait_until(lambda: display_session(events(), device_status.get('epoch')), 15)
            require(session is not None, 'missing current-session bot display-ready')
            for operation, rid, body in (
                ('load', REQUEST_ID, load_body),
                ('show', 'p4-display-show', dict(hash=image['hash'], layer=7, x=0, y=0))):
                alive(); publish_command(args.commands, operation, body, rid)
                sent = wait_until(lambda: display_sent(events(), session, rid), 15)
                require(sent is not None, 'missing bot sent '+rid)
                ack = wait_until(lambda: display_result(events(), session, sent['packet']['d']), 15)
                require(ack is not None and ack['result'].get('accepted') is True and not ack['result'].get('parse_failed'), 'wire request rejected')
                completed = terminal(board, rid); require(completed['error'] == 'none', 'display terminal failure')
                if operation == 'show':
                    require(rendered_show(completed, read(board, 'status', 'DISPLAY'), read(board, 'status layers', 'DISPLAY_LAYERS'), body), 'show not rendered')
                pairs.append(dict(operation=operation, request_id=rid, sent=sent, ack=ack, completed=completed))
            # Generate enough sampled scans for the latency histogram, with
            # unchanged bench-bot connection and audio left off throughout.
            board.command('disp character on'); until = time.monotonic()+args.seconds
            while time.monotonic() < until:
                alive()
                require(not any(re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE|Guru Meditation|panic.ed|Stack canary', line, re.I)
                                for _, _, line in board.lines_since(serial_at)), 'session/display failed')
                time.sleep(.1)
            # Bench-display's exact-wire window ends before intentional
            # disconnect/freeze. Preserve all records inside that window.
            (args.out/'wire.log').write_text('\n'.join(line for _, _, line in board.lines_since(serial_at))+'\n')
        finally:
            # Cleanup failures are acquisition failures and keep the restore
            # guard armed. Do not mask one by setting success in a finally block.
            board.command('disp character off'); board.command('audio 0'); board.command('disconnect', 45)
        alive()
        session_lines = [line for _, _, line in board.lines_since(serial_at)]
        require(not any(re.search(r'Guru Meditation|panic.ed|Stack canary', line, re.I) for line in session_lines), 'fatal display diagnostic')
        run_lines = [line for _, _, line in board.lines_since(start)]
        (args.out/'run-serial.log').write_text('\n'.join(run_lines)+'\n')
        with bot_log.open('rb') as stream: stream.seek(log_at); (args.out/'bot.log').write_bytes(stream.read())
        require(b'\x1b[' not in (args.out/'bot.log').read_bytes(), 'bot log must be uncoloured')
        write_new(args.out/'events.json', events())
        return dict(boot_verified=True, screens=screens, status_assets=status_assets, wire_requests=pairs,
                    display_session=session, stale_bridge_sends=stale_bridge_sends(events(), session),
                    underrun_lines=sum('underrun happens' in line for line in run_lines),
                    border='unverified: requires coordinator image/inspection of 40 px on all four sides',
                    tearing='unverified', touch='deferred to 13b')


def live(args):
    from p4_audio import HardwareIO
    from make_bundle import verify_bundle
    from wire_contract import parse_capture, validate_capture
    from board_smoke import process_token
    import boards
    boards.require_bench()
    manifest, _ = verify_bundle(args.bundle, args.board)
    require(not manifest['aec_enabled'], 'step 13a uses AEC-off bundles')
    identity = json.loads(args.bot_identity.read_text()); pid = bot_identity(args, identity)
    os.environ['STEP9A_FIXTURES'] = str(ROOT/'hil/fixtures/echoear360')
    args.out.mkdir(parents=True, exist_ok=False)
    write_new(args.out/'identity.json', identity); write_new(args.out/'image.json', manifest)
    io = HardwareIO(args, manifest); io.preflight_tools(); io.preflight_restore()
    result = dict(acquisition_passed=False, qualification=False, board=args.board, tearing='unverified')
    guard = io.hold()
    try:
        with guard:
            require(process_token(pid) == identity['bot_instance'], 'bot changed before flash')
            guard.touch(); result.update(acquire(args, io, manifest, identity)); guard.success = True
        result['restore'] = guard.record
        require(guard.record.get('clean') is True, 'unclean restore; inspect evidence')
        result['acquisition_passed'] = True
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc), restore=guard.record)
    finally:
        guard.release_signals()
        write_new(args.out/'acquisition.json', result)
    if not result['acquisition_passed']: return 1
    # Offline verdicts never cause a successful P4 acquisition to restore factory.
    try:
        validate_capture(parse_capture((args.out/'wire.log').read_text().splitlines()), profile='bench-display',
                         intended_aec=False, target=boards.BOARDS[args.board]['chip'], request_id=REQUEST_ID)
        if args.board == 'p4xc': write_new(args.out/'scan-latency.json', latency((args.out/'run-serial.log').read_text().splitlines()))
    except Exception as exc:
        write_new(args.out/'analysis-error.json', dict(error=type(exc).__name__+': '+str(exc), qualification=False))
        return 2
    return 0


def pair(left, right):
    from wire_contract import parse_capture, compare
    from board_smoke import BOUNDS_FILE, bounds_for
    for directory, board in ((left, 'echoear'), (right, 'p4xc')):
        acquisition = json.loads((directory/'acquisition.json').read_text())
        require(acquisition.get('acquisition_passed') is True and acquisition.get('board') == board and
                acquisition.get('restore', {}).get('clean') is True and not acquisition.get('error'), 'unclean acquisition')
    measured = compare(parse_capture((left/'wire.log').read_text().splitlines()), parse_capture((right/'wire.log').read_text().splitlines()),
        profile='bench-display', left_aec=False, right_aec=False, bounds=bounds_for(BOUNDS_FILE, 'normal'), request_id=REQUEST_ID,
        left_identity=json.loads((left/'identity.json').read_text()), right_identity=json.loads((right/'identity.json').read_text()))
    return dict(exact_wire_passed=True, measured=measured, qualification=False, request_id=REQUEST_ID,
                scan_latency=latency((right/'run-serial.log').read_text().splitlines()), tearing='unverified')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest='action', required=True)
    scan = sub.add_parser('latency'); scan.add_argument('serial', type=Path); scan.add_argument('--out', type=Path, required=True)
    paired = sub.add_parser('pair'); paired.add_argument('left', type=Path); paired.add_argument('right', type=Path); paired.add_argument('--out', type=Path, required=True)
    run = sub.add_parser('live'); run.add_argument('--board', choices=('echoear', 'p4xc'), required=True)
    for name in ('bundle', 'out', 'bot-dir', 'bot-identity', 'commands'): run.add_argument('--'+name, type=Path, required=True)
    run.add_argument('--bot-repo', type=Path, default=Path.home()/'src/dg-esp32')
    run.add_argument('--server-url', default='http://192.168.7.222:7861')
    run.add_argument('--seconds', type=int, default=30)
    run.add_argument('--factory-backup', dest='p4_factory_backup', type=Path, default=Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z')
    run.set_defaults(reference_only=False, p4_restore_bundle=None, p4_restore_elf=None)
    args = parser.parse_args(argv)
    if args.action == 'live':
        require(10 <= args.seconds <= 120, 'seconds must be 10..120')
        require(args.server_url.startswith('http://') and args.server_url.endswith(':7861'), 'bench bot must use port 7861')
        return live(args)
    result = latency(args.serial.read_text().splitlines()) if args.action == 'latency' else pair(args.left, args.right)
    write_new(args.out, result); return 0


if __name__ == '__main__': raise SystemExit(main())
