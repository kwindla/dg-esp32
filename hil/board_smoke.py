#!/usr/bin/env python3
"""Board smoke A. Hardware entry is coordinator-only under hil/bench.py.

An existing demo_bot process owns 7861. Its uncoloured bot.log, bot.pid and
ECHOEAR_AEC_SESSION_DIR recorder outputs are inputs, never services we own.
Use --calibration to collect evidence before measured bounds are fixed; this
reports acquisition_passed separately and cannot claim qualification.
"""
import argparse
from contextlib import contextmanager
from itertools import combinations
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from make_bundle import CHIPS, digest, local_file, verify_bundle
from wire_contract import (Bounds, ContractError, identity_schema, measured,
                           parse_capture, require, sdp_mline, validate_capture)

ROOT = Path(__file__).resolve().parents[1]
SERVER_URL = 'http://192.168.7.222:7861'
BOUNDS_FILE = ROOT/'hil/fixtures/wire_contract_bounds.json'
NORMAL_HASH = '55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'
FACTORY_BACKUP = Path.home()/'src/evidence/echoear/p4/factory-20261031T060646Z'
VARIANTS = ('normal', 'demo', 'aec-normal', 'aec-demo')
WAIT_SAFETY_SECONDS = 60


def preflight_tools():
    """Dry imports only: also callable outside bench.py, before taking its lock."""
    import boards
    python = boards.checked_esptool_python()
    os.environ.setdefault('ESPTOOL_PYTHON', python)
    for interpreter in dict.fromkeys((sys.executable, python)):
        result = subprocess.run([interpreter, '-c', 'import esptool, serial; print(esptool.__version__)'],
                                capture_output=True, text=True, timeout=10)
        require(result.returncode == 0 and result.stdout.strip() == '5.5.0',
                'dependency preflight: esptool 5.5.0 and serial required in '+interpreter)
        version = subprocess.run([interpreter, '-m', 'esptool', 'version'],
                                 capture_output=True, text=True, timeout=10)
        require(version.returncode == 0 and version.stdout.splitlines()[-1:] == ['5.5.0'],
                'dependency preflight: esptool CLI must report 5.5.0 in '+interpreter)
    for name in ('serial', 'esp_serial', 'board_serial', 'alsa', 'demo_policy', 'step9a_assets',
                 'demo_session', 'step9a_merge_checks', 'echoear_item2_campaign',
                 'echoear_restore_verify', 'echoear_rollback'):
        try: importlib.import_module(name)
        except ImportError as exc: raise RuntimeError('dependency preflight: cannot import '+name) from exc
    from step9a_echoear import check_esptool
    check_esptool()
    return python


def verify_flash_mac(board):
    # Frozen board_port uses sys.executable and hard-reset. Keep its pinned
    # resolution, but use the checked tool for both boards without a reset.
    import boards
    fd = boards.require_bench()
    info = boards.hardware_board(board)
    port = boards.resolve_port(board=board)
    result = subprocess.run([boards.checked_esptool_python(), '-m', 'esptool', '--chip', info['chip'],
                             '--port', port, '--after', 'no-reset', 'read-mac'],
                            capture_output=True, text=True, timeout=30, pass_fds=(fd,))
    macs = re.findall(r'MAC:\s*([0-9a-f:]{17})', result.stdout, re.I)
    require(result.returncode == 0 and macs and all(m.lower() == info['efuse_mac'] for m in macs),
            'esptool failed to verify '+board+' eFuse MAC; refusing access')
    return port


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def smoke_mode(manifest):
    require(type(manifest.get('autostart')) is bool, 'missing verified autostart flag')
    return 'demo' if manifest['autostart'] else 'manual'


def boot_check(lines, manifest, board):
    chip = 'ESP32-S3 step 7' if board == 'echoear' else 'esp32p4'
    pattern = re.compile(r'(?:^|\s)I \(\d+\) dg-esp32: '+re.escape(chip)+r' audio elf_sha256=([0-9a-f]{64})\s*$')
    candidates = [line for line in lines if 'elf_sha256=' in line]
    require(len(candidates) == 1, 'need one boot hash record')
    match = pattern.search(candidates[0])
    require(match is not None and candidates[0].count('elf_sha256=') == 1 and
            match[1] == manifest['elf_sha256'], 'wrong/malformed boot hash')
    boot_at = lines.index(candidates[0])
    require(any(line.strip() == 'READY transport-console' for line in lines[boot_at+1:]), 'missing console ready after boot')
    if manifest.get('schema') == 1:
        require(any('DG_BOOT board='+board+' chip='+CHIPS[board]+' ' in line for line in lines), 'wrong boot board/target')
    require(not any(re.search(r'Guru Meditation|panic.ed|Stack canary|GPIO ownership conflict|Virtual address not enough', l, re.I)
                    for l in lines), 'fatal boot diagnostic')
    if manifest['aec_enabled']:
        gain = manifest.get('boot_ref_gain_db')
        require(type(gain) is int and gain in range(0, 34, 3), 'missing AEC boot ref gain')
        gains = [re.search(r'MIC3_GAIN=0x([0-9a-fA-F]+) analog_db=(\d+)', line) for line in lines]
        gains = [g for g in gains if g]
        require(len(gains) == 1 and int(gains[0][1], 16) == 0x10+gain//3 and int(gains[0][2]) == gain,
                'AEC MIC3_GAIN boot mismatch')
    return match[1]


def slice_sessions(lines):
    """Close on PHASE closed, including queued msg evidence AFTER intervals."""
    sessions, active = [], None
    for line in lines:
        if 'WIRE_CONTRACT start=' in line:
            require(active is None, 'new session before previous close')
            active = []
        if active is not None:
            active.append(line)
            if re.search(r'\bPHASE closed\b', line):
                sessions.append(active); active = None
    require(active is None, 'session missing close')
    return sessions


def check_ports(capture):
    return [sdp_mline(line)[1] for line in capture.records.get('sdp', []) if line.startswith('m=')]


def bounds_for(path, variant):
    fixture = json.loads(Path(path).read_text())
    require(fixture.get('schema') == 1 and fixture.get('status') == 'measured', 'bounds pending coordinator measurement')
    row = fixture['variants'][variant]
    require(row.get('pairs', 0) >= 3, 'bounds need three S3 pairs per variant')
    require(row['coverage'] >= 0.95 or bool(fixture.get('coverage_decision')), 'coverage below 0.95 needs coordinator decision')
    return Bounds(stall_us=row['stall_us'], p99_delta_us=row['p99_delta_us'], coverage=row['coverage'])


def check_capture(lines, capability_lines, *, manifest, board, headless=False, bounds=None, calibration=False, coverage=.95, coverage_decision=None):
    sessions = slice_sessions(lines)
    require(len(sessions) == 1, 'need exactly one closed session per capture')
    require(not any('UPLINK_INTERVALS_NONE' in line for line in sessions[0]), 'UPLINK_INTERVALS_NONE: unavailable evidence')
    profile = 'headless' if headless else 'product'
    wire = sessions[0] + capability_lines
    capture = parse_capture(wire)
    validate_capture(capture, profile=profile, intended_aec=manifest['aec_enabled'], target=CHIPS[board])
    check_ports(capture)
    require(len(capture.intervals) == 1, 'need one interval generation')
    timing = None
    if not calibration:
        require(bounds is not None, 'measured bounds required')
        timing = measured(capture.intervals[0], bounds)
    else:
        # Fixed requirements and declared coverage still apply. No production
        # stall bound is inferred by testing against this sample's own maximum.
        row = capture.intervals[0]
        require(row['n'] >= 500, 'calibration needs 500 intervals')
        require(coverage >= .95 or bool(coverage_decision), 'coverage below 0.95 needs coordinator decision')
        measured(row, Bounds(stall_us=max(1, row['max_us'], row['last_ok_age_us']),
                             p99_delta_us=0, coverage=coverage))
    return capture, wire, timing


def text_rows(events, role):
    kind = 'aggregator-'+role+'-turn-stopped'
    return [e['text'] for e in events if e.get('kind') == kind and isinstance(e.get('text'), str)]


def words(text):
    return set(re.findall(r'\w+', text.casefold()))


def completed_count(text):
    # Deepgram may concatenate a spoken numeric sequence into one token.
    text = re.sub(r'\b'+''.join(map(str, range(1, 21)))+r'\b', '1 20', text)
    tokens = words(text)
    return bool(tokens & {'one', '1'}) and bool(tokens & {'twenty', '20'})


def turn_checks(events, kind, aec, *, calibration=False):
    """Real bot recorder, not device VAD or a substring count of 'interrupt'."""
    frames = check_bot_session(events)
    require(frames and any(e.get('frame') == 'BotStartedSpeakingFrame' for e in frames), 'missing bot speech witness')
    require(any(e.get('frame') == 'BotStoppedSpeakingFrame' for e in frames), 'missing bot speech completion')
    users, assistants = text_rows(events, 'user'), text_rows(events, 'assistant')
    starts = [e for e in frames if e.get('frame') == 'UserStartedSpeakingFrame']
    require(all(type(e.get('bot_active')) is bool for e in frames if e.get('frame') in ('UserStartedSpeakingFrame', 'InterruptionFrame')),
            'missing bot-active event witness')
    during = [e for e in starts if e.get('bot_active') is True]
    interrupts = [e for e in frames if e.get('frame') == 'InterruptionFrame' and e.get('bot_active') is True]
    require(users and assistants and starts, 'missing bot turn evidence')
    findings = []
    def content(ok, finding, reason):
        if calibration:
            if not ok:
                description = {'missing_paris': 'France reply lacks Paris', 'missing_rome': 'Italy reply lacks Rome'}.get(finding, reason)
                findings.append(dict(kind=finding, reason=description, assistants=assistants))
        else:
            require(ok, reason)
    if kind == 'france':
        require('france' in words(users[0]), 'France prompt not transcribed' if calibration else 'France/Paris turn failed')
        content(any('paris' in words(t) for t in assistants), 'missing_paris', 'France/Paris turn failed')
    else:
        require('count' in words(users[0]) and bool(words(users[0]) & {'twenty', '20'}), 'count prompt not transcribed')
    if kind == 'talkover' and aec:
        require(len(starts) == 2 and len(during) == 1 and interrupts, 'AEC talk-over must interrupt during bot speech')
        require(any('italy' in words(t) for t in users[1:]), 'AEC Italy prompt not transcribed' if calibration else 'AEC must hear Italy and answer Rome')
        content(any('rome' in words(t) for t in assistants), 'missing_rome', 'AEC must hear Italy and answer Rome')
    else:
        require(len(starts) == 1 and not during and not interrupts, 'self-interruption or unexpected user turn')
        if kind != 'france':
            content(any(completed_count(t) for t in assistants), 'off_script_reply', 'count reply did not complete through twenty')
        if kind == 'talkover':
            require(not any('italy' in words(t) for t in users), 'AEC-off must ignore talk-over')
    require(not any(e.get('frame') in ('ErrorFrame', 'FatalErrorFrame') for e in frames), 'bot pipeline error')
    if calibration and (kind == 'france' or (kind == 'talkover' and aec)):
        city = 'paris' if kind == 'france' else 'rome'
        # Report obvious off-script/negated answers even if they name the city.
        # Qualification retains its original token checks, without new heuristics.
        if any(re.search(r"\b(?:not\s+|never\s+|isn't\s+)"+city+r'\b|\b(?:don.t|do not) know\b', t, re.I) for t in assistants):
            findings.append(dict(kind='off_script_reply', reason='reply is off-script or negates the expected answer', assistants=assistants))
    result = dict(passed=True, user_turn_starts=len(starts), user_starts_during_bot=len(during),
                  interruptions_during_bot=len(interrupts), users=users, assistants=assistants)
    if calibration: result['bot_content_findings'] = findings
    return result


def downstream_frames(events):
    frames = [e for e in events if e.get('kind') == 'frame']
    require(all(e.get('direction') in ('DOWNSTREAM', 'UPSTREAM') for e in frames), 'missing frame direction')
    return [e for e in frames if e['direction'] == 'DOWNSTREAM']


def check_bot_session(events):
    frames = downstream_frames(events)
    ended = any(e.get('kind') == 'session-end' for e in events)
    ended = ended or any(e.get('frame') in ('CancelFrame', 'EndFrame') for e in frames)
    require(not ended, 'bot session ended during turn')
    return frames


def turn_finished(events, need):
    frames = check_bot_session(events)
    users = [e for e in events if e.get('kind') == 'aggregator-user-turn-stopped']
    assistants = [e for e in events if e.get('kind') == 'aggregator-assistant-turn-stopped']
    speech = [e for e in frames if e.get('frame') in ('BotStartedSpeakingFrame', 'BotStoppedSpeakingFrame')]
    # An interrupted assistant's stop precedes the new user stop. Wait for the
    # reply to the latest user, including its BotStopped witness (the recorder
    # can log the aggregator's assistant stop just after BotStopped).
    return bool(len(users) >= need and assistants and speech and
                assistants[-1]['monotonic_ns'] > users[-1]['monotonic_ns'] and
                speech[-1]['frame'] == 'BotStoppedSpeakingFrame' and
                speech[-1]['monotonic_ns'] > users[-1]['monotonic_ns'])


def resource_check(samples, board):
    from step9a_merge_checks import memory_gate
    gate = memory_gate(samples)
    require(samples and all(row.get('output_volume_db') == -6 for row in samples), 'playback volume must be -6 dB')
    require(gate['passed'], 'heap/allocation gate failed')
    peer = [task for row in samples for task in row.get('tasks', []) if task.get('name') == 'peer-loop']
    require(len(peer) == len(samples) and all(type(t.get('stack_free_bytes')) is int and t['stack_free_bytes'] >= 4096 for t in peer),
            'peer-loop free stack below 4096 or missing')
    psram = [row.get('heap', {}).get('psram', {}) for row in samples]
    require(all(type(p.get('free')) is int and p['free'] > 0 and type(p.get('largest')) is int and p['largest'] > 0 for p in psram),
            'missing/exhausted PSRAM evidence')
    return dict(gate, peer_stack_min=min(t['stack_free_bytes'] for t in peer),
                psram_free_min=min(p['free'] for p in psram), psram_session_growth_note='step 7 added approximately 15 KB/session', board=board)


def restore_decision(board, *, success, product_restore=False):
    if board == 'echoear': return 'echoear-55152f25'
    if product_restore: return 'p4-product-of-record'
    return 'keep-tested-p4' if success else 'p4-factory'


def process_token(pid):
    # Never read process environment or any .env. PID start time prevents reuse.
    stat = Path('/proc')/str(pid)/'stat'
    fields = stat.read_text().rsplit(')', 1)[1].split()
    require(fields[0] != 'Z', 'bot process is a zombie')
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+':'+str(pid)+':'+fields[19]


def live_bot_identity(bot_dir, repo, identity):
    identity_schema(identity)
    pid = int((Path(bot_dir)/'bot.pid').read_text())
    require(identity['bot_instance'] == process_token(pid), 'bot process identity changed')
    repo = Path(repo).resolve()
    cmd = (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
    require(str(repo/'server/demo_bot.py').encode() in cmd, 'bot command does not name the unchanged demo_bot')
    for field, name in (('bot_source_sha', 'demo_bot.py'), ('policy_source_sha', 'demo_policy.py')):
        expected = subprocess.check_output(['git', 'rev-parse', 'HEAD:server/'+name], cwd=repo, text=True).strip()
        actual = subprocess.check_output(['git', 'hash-object', 'server/'+name], cwd=repo, text=True).strip()
        require(expected == actual == identity[field], 'bot source identity differs')
    return pid


def recorder_path(directory, pc_id):
    matches = []
    for path in Path(directory).glob('session-*/events.jsonl'):
        # peer-binding is at the beginning. Do not reread historical audio logs
        # on every poll (a long campaign can have gigabytes of recorder data).
        with path.open('rb') as stream: prefix = stream.read(65536)
        rows = [json.loads(line) for line in prefix.split(b'\n')[:-1] if line]
        if any(row.get('pc_id') == pc_id for row in rows): matches.append(path)
    require(len(matches) == 1, 'missing/ambiguous bot recorder session for peer')
    return matches[0]


class RecorderTail:
    def __init__(self, path, pc_id):
        self.path, self.pc_id = Path(path), pc_id
        stat = self.path.stat(); self.inode = (stat.st_dev, stat.st_ino)
        self.offset = 0; self.pending = b''; self.rows = []

    def poll(self):
        stat = self.path.stat()
        require((stat.st_dev, stat.st_ino) == self.inode and stat.st_size >= self.offset, 'bot recorder rotated/truncated')
        with self.path.open('rb') as stream:
            stream.seek(self.offset); data = stream.read(); self.offset += len(data)
        lines = (self.pending+data).split(b'\n'); self.pending = lines.pop()
        for line in lines:
            if not line: continue
            row = json.loads(line)
            require(row.get('pc_id') in (None, self.pc_id), 'bot recorder peer identity changed')
            # Audio sample indexes remain in the coordinator's original log.
            # Retain the frame/transcript/profile witnesses used by this harness.
            if row.get('pc_id') == self.pc_id and row.get('kind') != 'audio': self.rows.append(row)
        return self.rows


class LiveIO:
    """The only device/audio adapter. Importing this module performs no IO."""
    clock = staticmethod(time.monotonic)

    def __init__(self, args, out, manifest, identity):
        import boards
        boards.require_bench()
        self.args, self.out, self.manifest, self.identity = args, out, manifest, identity
        self.pid = live_bot_identity(args.bot_dir, args.bot_repo, identity)
        self.python = boards.checked_esptool_python()
        os.environ.setdefault('ESPTOOL_PYTHON', self.python)
        self.serial_seen = 0
        self.session_gen = self.pc_id = None
        self.stopping = False
        self.since = self.clock()
        self.active = False
        self.snapshot_index = 0

    def poll(self, board):
        require(process_token(self.pid) == self.identity['bot_instance'], 'bot process exited/restarted')
        for cursor, _, line in board.lines_since(self.serial_seen):
            self.serial_seen = cursor
            if self.active and not self.stopping and self.session_gen is not None:
                lost = re.search(r'\bSESSION FAIL\b|\bPEER_DEF:\s*Peer disconnected\b|\bRTCP_BYE\b', line)
                state = re.search(r'\bSTATE (?:Disconnected|Starting|Idle) gen=(\d+)\b', line)
                require(not (lost or (state and int(state[1]) != self.session_gen)), 'session lost: '+line)
        if self.active and not self.stopping and self.pc_id is not None:
            check_bot_session(self.events()[1])

    def wait(self, board, condition, seconds=WAIT_SAFETY_SECONDS, *, label='condition'):
        deadline = self.clock()+seconds
        while self.clock() < deadline:
            self.poll(board)
            value = condition()
            if value: return value
            time.sleep(.025)
        raise TimeoutError('Smoke wait for '+label+' exceeded '+str(seconds)+' seconds')

    def pause(self, board, seconds):
        until = self.clock()+seconds
        self.wait(board, lambda: self.clock() >= until, seconds+1, label='settling/tail pause')

    def flash(self, bundle, manifest, label):
        import boards
        boards.require_bench()
        _, flash = verify_bundle(bundle, self.args.board, self.args.reference_only if label == 'candidate' else False)
        port = verify_flash_mac(self.args.board)
        command = [self.python, '-m', 'esptool', '--chip', CHIPS[self.args.board], '--port', port,
                   '--baud', '460800' if self.args.board == 'echoear' else '921600', '--after', 'no-reset',
                   'write-flash', *flash['write_flash_args']]
        for address, name in flash['flash_files'].items(): command += [address, str(local_file(bundle, name))]
        with (self.out/(label+'-flash.log')).open('w') as log:
            subprocess.run(command, check=True, timeout=120, pass_fds=(boards.require_bench(),), stdout=log, stderr=subprocess.STDOUT)

    def open(self):
        import boards
        from board_serial import open_board
        port = verify_flash_mac(self.args.board)
        return open_board(self.args.board, port=port, log_path=self.out/'serial.log')

    def hold(self):
        from echoear_item2_campaign import RestoreHold
        from echoear_restore_verify import boot_proof
        if self.args.board == 'echoear':
            def proof(out, label):
                require(boot_proof(out/(label+'-serial.log'), NORMAL_HASH)['passed'], 'restore boot proof failed')
            self.guard = RestoreHold(self.out, verify=proof)
        else:
            # Reuse the existing signal protection and lock-retaining retry loop.
            from echoear_rollback import Live
            from types import SimpleNamespace
            parent = self
            class P4Hold(RestoreHold):
                success = False
                def _restore(self):
                    attempts = []
                    while True:
                        try:
                            action = restore_decision('p4xc', success=self.success,
                                                      product_restore=bool(parent.args.p4_restore_bundle))
                            if action == 'p4-factory':
                                import boards
                                with (parent.out/'factory-restore.log').open('a') as log:
                                    subprocess.run([parent.python, str(ROOT/'spikes/p4_probe/host/restore_factory.py'),
                                                    '--backup', str(parent.args.p4_factory_backup)], check=True,
                                                   pass_fds=(boards.require_bench(),), stdout=log, stderr=subprocess.STDOUT)
                            elif action == 'p4-product-of-record':
                                manifest, _ = verify_bundle(parent.args.p4_restore_bundle, 'p4xc')
                                require(manifest['elf_sha256'] == parent.args.p4_restore_elf, 'P4 restore pin mismatch')
                                parent.flash(parent.args.p4_restore_bundle, manifest, 'restore')
                                from board_serial import open_board
                                with open_board('p4xc', log_path=parent.out/'restore-serial.log') as link:
                                    at = link.cursor; link.reset(); link.wait_for('READY transport-console', 60, since=at)
                                    boot_check([l for _, _, l in link.lines_since(at)], manifest, 'p4xc')
                            return dict(clean=not attempts, action=action, attempts=attempts)
                        except Exception as exc:
                            attempts.append(type(exc).__name__+': '+str(exc))
                            write(parent.out/'restore.json', dict(clean=False, attempts=attempts, lock_retained=True))
                            Live.wait_recovery(SimpleNamespace(retry=False))
            self.guard = P4Hold(self.out)
        return self.guard

    def preflight_restore(self):
        if self.args.board == 'echoear':
            from echoear_aec_bench import NORMAL, verify_bundle as verify_restore
            verify_restore(NORMAL, NORMAL_HASH)
        elif self.args.p4_restore_bundle:
            manifest, _ = verify_bundle(self.args.p4_restore_bundle, 'p4xc')
            require(manifest['elf_sha256'] == self.args.p4_restore_elf, 'P4 restore pin mismatch')
        else:
            import importlib.util
            path = ROOT/'spikes/p4_probe/host/restore_factory.py'
            # Its existing local imports need the spike's host directory.
            sys.path.insert(0, str(path.parent))
            try:
                spec = importlib.util.spec_from_file_location('smoke_factory_restore', path)
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                module.verify_backup(self.args.p4_factory_backup)
            finally: sys.path.pop(0)

    def preflight_tools(self):
        preflight_tools()

    def boot(self, board):
        at = board.cursor; board.reset()
        board.wait_for('READY transport-console', 60, since=at)
        boot_check([l for _, _, l in board.lines_since(at)], self.manifest, self.args.board)
        return at

    def provision(self, board):
        if self.args.headless:
            board.command('demo hold', 45)
            board.command('demo configure '+json.dumps(dict(url=SERVER_URL, volume_db=-6, enabled=True), separators=(',', ':')))
            return dict(headless=True, persisted_hashes=[])
        os.environ['STEP9A_FIXTURES'] = str(ROOT/'hil/fixtures/echoear360')
        from step9a_assets import asset_server
        from demo_session import provision_assets
        with asset_server():
            return provision_assets(board, SERVER_URL, self.out/'arena-budget.json', volume_db=-6)

    def prepare_manual(self, board):
        board.command('audio 0'); board.command('server-url '+SERVER_URL)
        board.command('playback-volume -6'); board.command('gate auto')
        if not self.args.headless:
            os.environ['STEP9A_FIXTURES'] = str(ROOT/'hil/fixtures/echoear360')
            from step9a_assets import asset_server
            from demo_session import prepare_board
            with asset_server(): prepare_board(board, SERVER_URL, self.out/'arena-budget.json', volume_db=-6)

    def begin(self, board, action):
        self.serial_at = (self.out/'serial.log').stat().st_size
        self.bot_at = (self.args.bot_dir/'bot.log').stat().st_size
        self.since = self.clock(); self.active = True; self.interval_requested = False
        self.stopping = False; self.session_gen = self.pc_id = None
        if action == 'cold': at = self.boot(board)
        else:
            at = board.cursor
            board.command('connect' if action == 'manual' else 'demo run', 90)
        ready_at, _, ready = board.wait_for_entry(r'\bSTATE Ready gen=\d+', 120, since=at)
        self.session_gen = int(re.search(r'\bSTATE Ready gen=(\d+)', ready)[1])
        # Start at Ready itself, so an already-buffered failure is not skipped.
        self.serial_seen = ready_at
        self.poll(board)
        if action in ('cold', 'resume') and not self.args.headless:
            board.wait_for(r'demo-boot: RESTORED assets=16 no_http=1', 10, since=at)
        if action == 'manual': board.command('audio 1')
        self.since = self.clock()  # Minimum active duration starts AFTER ready, not before reset/connect.
        self.pc_id = self.snapshot(board)['pc_id']
        _, events = self.events()
        check_bot_session(events)
        profiles = [e for e in events if e.get('kind') == 'stt-profile']
        require(len(profiles) == 1, 'missing bot profile identity witness')
        observed = {k: v for k, v in profiles[0].items() if k not in ('kind', 'monotonic_ns', 'pc_id')}
        require(observed == self.identity['profile_manifest'], 'bot profile manifest differs')

    def snapshot(self, board):
        at = board.cursor; board.command('stats')
        line = board.wait_for(r'^PERF \{', 10, since=at)
        row = json.loads(line[5:]); self.snapshot_index += 1
        write(self.out/f'perf-{self.snapshot_index:03d}.json', row)
        return row

    def events(self):
        if not hasattr(self, 'recorder') or self.recorder.pc_id != self.pc_id:
            self.recorder = RecorderTail(recorder_path(self.args.bot_dir, self.pc_id), self.pc_id)
        return self.recorder.path, self.recorder.poll()

    def turn(self, board, kind, index):
        from alsa import play_wav, record_wav, resolve_jabra, set_volumes
        self.poll(board)  # Reject a lost session before another stimulus starts.
        calibration = self.args.calibration
        if not hasattr(self, 'device'):
            card, self.device = resolve_jabra(); set_volumes(card, self.out/'mixer.log')
        fixture = ROOT/'hil/fixtures/agent_conversation'
        definitions = json.loads((fixture/'manifest.json').read_text())['turns']
        for name in ('france', 'long', 'italy'):
            spec = next(x for x in definitions if x['id'] == name)
            require(digest(fixture/spec['file']) == spec['sha256'], 'audio fixture changed')
        started = time.monotonic_ns()
        def rows():
            events = [e for e in self.events()[1] if e['monotonic_ns'] >= started]
            check_bot_session(events)
            return events
        def finished():
            events = rows()
            need = 2 if kind == 'talkover' and self.manifest['aec_enabled'] else 1
            return turn_finished(events, need)
        playback = []
        seconds = 15 if kind == 'france' else 30
        recording_at = self.clock()
        recording_end = recording_at+seconds
        finish_at = recording_at+seconds-2  # Reserve the observed reply's tail inside the recording.
        settle_after_recording = False
        with record_wav(self.out/f'reply-{index}.wav', self.device, seconds) as recording:
            playback.append(play_wav(fixture/('france.wav' if kind == 'france' else 'long.wav'), self.device))
            if kind == 'talkover':
                event = self.wait(board, lambda: next((e for e in rows() if e.get('kind') == 'aggregator-user-turn-stopped'), None),
                                  max(0, min(25, finish_at-self.clock())), label='user transcript')
                target = event['monotonic_ns']/1e9+3
                self.wait(board, lambda: bool(rows()) and self.clock() >= target,
                          max(0, min(target-self.clock()+1, finish_at-self.clock())), label='talk-over deadline')
                require(self.clock()-target <= .25, 'talk-over scheduling missed +3 second deadline')
                speech = [e for e in downstream_frames(rows()) if e.get('frame') in ('BotStartedSpeakingFrame', 'BotStoppedSpeakingFrame')]
                require(speech and speech[-1]['frame'] == 'BotStartedSpeakingFrame', 'talk-over stimulus missed bot speech')
                playback.append(play_wav(fixture/'italy.wav', self.device))
                require(abs(playback[-1]['request_monotonic_s']-target) <= .25, 'actual Italy playback missed +3 second deadline')
            if calibration:
                # Reach the unchanged recording boundary before classifying a
                # long bot reply. No other timeout/error is turned into a finding.
                self.wait(board, lambda: finished() or self.clock() >= recording_end,
                          max(0, recording_end-self.clock())+1, label=f'turn finish ({seconds} s recording)')
                settle_after_recording = self.clock() > finish_at
                self.pause(board, min(2, max(0, recording_end-self.clock())))
            else:
                self.wait(board, finished, max(0, finish_at-self.clock()), label=f'turn finish ({seconds} s recording)')
                self.pause(board, 2)  # Detect starts/interruptions in the reply tail.
            # Let arecord reach -d naturally: SIGINT exits nonzero and can leave
            # a full-duration header on a truncated WAV. Keep checking events
            # through the remaining capture, and let the frozen helper check exit.
            def recorded():
                rows()
                return recording.process.poll() is not None
            self.wait(board, recorded, max(0, recording_at+seconds+5-self.clock()),
                      label=f'recording finish ({seconds} s recording)')
            recording.wait()
        timing_findings = []
        if calibration:
            in_window = [e for e in rows() if e['monotonic_ns'] <= int(recording_end*1e9)]
            users = [e for e in in_window if e.get('kind') == 'aggregator-user-turn-stopped']
            require(users, 'missing bot turn evidence within recording window')
            speech = [e for e in downstream_frames(in_window) if e.get('frame') in ('BotStartedSpeakingFrame', 'BotStoppedSpeakingFrame')]
            require(any(e['frame'] == 'BotStartedSpeakingFrame' and e['monotonic_ns'] > users[-1]['monotonic_ns'] for e in speech),
                    'missing bot speech witness within recording window')
            if not turn_finished(in_window, 2 if kind == 'talkover' and self.manifest['aec_enabled'] else 1):
                speaking_at_end = bool(speech and speech[-1]['frame'] == 'BotStartedSpeakingFrame')
                # The recorder has closed naturally. Wait for a clean reply end
                # under the existing safety cap; never start the next stimulus
                # while the bot still speaks, and never extend the WAV capture.
                self.wait(board, finished, WAIT_SAFETY_SECONDS, label='bot reply completion safety cap')
                settle_after_recording = True
                if speaking_at_end:
                    timing_findings.append(dict(kind='reply_exceeded_recording_window',
                        reason='bot reply continued beyond the recording window', recording_seconds=seconds,
                        recording_end_monotonic_ns=int(recording_end*1e9), observed_clean_stop_monotonic_ns=time.monotonic_ns()))
            # BotStopped can precede device playout/mic-gate recovery. Keep the
            # same guarded settle as qualification without extending the WAV.
            if settle_after_recording: self.pause(board, 2)
        events = rows()
        # Include the two-second tail when checking for extra user starts.
        verdict = turn_checks(events, kind, self.manifest['aec_enabled'], calibration=calibration)
        if calibration: verdict['bot_content_findings'].extend(timing_findings)
        verdict.update(kind=kind, playback=playback, events=events, recording=str(recording.path))
        write(self.out/f'turn-{index:02d}.json', verdict)
        return verdict

    def freeze(self, board):
        # The only uplink-intervals call for this session, before capture teardown.
        if not self.args.reference_only and not self.interval_requested:
            self.interval_requested = True  # Also covers command timeout after device received it.
            board.command('uplink-intervals')

    def finish(self, board, label, *, hold=False):
        self.stopping = True  # Own hold/disconnect is exempt, including cleanup.
        self.freeze(board)
        at = board.cursor; board.command('demo hold' if hold else 'disconnect', 45)
        board.wait_for(r'\bPHASE closed\b', 15, since=at)
        self.active = False
        with (self.out/'serial.log').open('rb') as stream:
            stream.seek(self.serial_at); lines = stream.read().decode(errors='replace').splitlines()
        with (self.args.bot_dir/'bot.log').open('rb') as stream:
            stream.seek(self.bot_at); bot = stream.read().decode(errors='replace').splitlines()
        require(not any('\x1b[' in l for l in bot), 'bot log must be uncoloured')
        capability = [l for l in bot if 'DEMO_CAPABILITY ' in l]
        text = '\n'.join(lines)+'\n'; (self.out/(label+'-serial.log')).write_text(text)
        (self.out/(label+'-bot.log')).write_text('\n'.join(bot)+'\n')
        path, events = self.events(); write(self.out/(label+'-bot-events.json'), events)
        return lines, capability

    def held(self, board):
        at = board.cursor; board.command('demo status')
        state = json.loads(board.wait_for(r'^DEMO \{', 5, since=at)[5:])
        require(state['held'] is True and state['initializing'] is False and state['configured'] is True and
                state['enabled'] is True and state['volume_db'] == -6, 'F20 hold state failed')
        at = board.cursor; self.pause(board, 2)
        require(not any('STATE Ready ' in l for _, _, l in board.lines_since(at)), 'held demo reconnected')
        return state

    def restored_assets(self, board, provision):
        if self.args.headless: return dict(skipped='headless display/assets')
        from step9a_support import read
        inventory = []; at = 0
        while True:
            page = read(board, 'inventory '+str(at), 'DISPLAY_INVENTORY')
            inventory += page['items']; at = page['next']
            if not at or at >= 256: break
        known = {r[0]: r for r in inventory}
        require(provision['persisted_hashes'] and all(h in known and known[h][4] and known[h][5] and known[h][6] == 'none'
                for h in provision['persisted_hashes']), 'F20 cold boot persisted assets missing')
        return dict(passed=True, persisted_hashes=provision['persisted_hashes'])

    def cleanup(self, board):
        if self.active:
            # Failed sessions still freeze once before any intentional stop.
            self.finish(board, 'failed', hold=smoke_mode(self.manifest) == 'demo')


def execute(args, *, backend=None):
    manifest, _ = verify_bundle(args.bundle, args.board, args.reference_only)
    identity = json.loads(args.bot_identity.read_text()); identity_schema(identity)
    bounds = None if args.reference_only or args.calibration else bounds_for(args.bounds, manifest['profile'])
    args.out.mkdir(parents=True, exist_ok=False)
    result = dict(schema=1, passed=False, acquisition_passed=False, reference_only=args.reference_only,
                  calibration=args.calibration, headless=args.headless, board=args.board, variant=manifest['profile'], mode=smoke_mode(manifest),
                  elf_sha256=manifest['elf_sha256'], firmware_commit=manifest['firmware_commit'],
                  pair_id=args.pair_id, identity=identity, acquisition_coverage=getattr(args, 'coverage', .95),
                  coverage_decision=getattr(args, 'coverage_decision', None), sessions=[], turns=[], cleanup_errors=[])
    if args.calibration: result['bot_content_findings'] = []
    write(args.out/'identity.json', identity)
    io = guard = None
    try:
        io = backend or LiveIO(args, args.out, manifest, identity)
        io.preflight_tools()
        io.preflight_restore()
        guard = io.hold()
        with guard:
            guard.touch()  # Restore even if the first flash only partly succeeds.
            io.flash(args.bundle, manifest, 'candidate')
            with io.open() as board:
                try:
                    io.boot(board)
                    provision = io.provision(board) if result['mode'] == 'demo' else io.prepare_manual(board)
                    if result['mode'] == 'demo': result['provision'] = provision
                    def session(label, action, full):
                        io.begin(board, action)
                        samples = [io.snapshot(board)]
                        kinds = ['france', 'talkover']+['long']*10 if full else ['france']
                        for kind in kinds:
                            index = len(result['turns'])+1
                            turn = io.turn(board, kind, index)
                            result['turns'].append(turn)
                            if args.calibration:
                                result['bot_content_findings'].extend(dict(finding, session=label, turn=index, prompt_kind=kind)
                                    for finding in turn.get('bot_content_findings', []))
                        # F20 short sessions must still contain >=500 intervals after warm-up.
                        io.pause(board, max(0, 13-(io.clock()-io.since)))
                        io.freeze(board)
                        samples.append(io.snapshot(board))
                        resources = resource_check(samples, args.board)
                        lines, capability = io.finish(board, label, hold=result['mode'] == 'demo')
                        row = dict(label=label, resources=resources, reference_only=args.reference_only)
                        if not args.reference_only:
                            capture, wire, timing = check_capture(lines, capability, manifest=manifest, board=args.board,
                                headless=args.headless, bounds=bounds, calibration=args.calibration,
                                coverage=getattr(args, 'coverage', .95), coverage_decision=getattr(args, 'coverage_decision', None))
                            path = args.out/(label+'-wire.log'); path.write_text('\n'.join(wire)+'\n')
                            row.update(wire_file=path.name, wire_sha256=digest(path), intervals=capture.intervals[0], timing=timing)
                        result['sessions'].append(row)
                    session('main', 'cold' if result['mode'] == 'demo' else 'manual', True)
                    if result['mode'] == 'demo':
                        result['hold'] = io.held(board)
                        session('resume', 'resume', False)
                        io.held(board)
                        session('cold', 'cold', False)
                        result['cold_assets'] = io.restored_assets(board, provision)
                    result['acquisition_passed'] = True
                    # P4 pre-step14 keeps a successfully acquired tested image.
                    guard.success = True
                finally:
                    try: io.cleanup(board)
                    except Exception as exc:
                        result['cleanup_errors'].append(type(exc).__name__+': '+str(exc))
                        result['acquisition_passed'] = False; guard.success = False
        result['restore'] = guard.record
        require(not result['cleanup_errors'] and guard.record.get('clean') is True, 'cleanup/restore failed or required retry')
        result['passed'] = not args.calibration
    except BaseException as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
        if guard is not None: result['restore'] = guard.record
        result['interrupted'] = isinstance(exc, (KeyboardInterrupt, SystemExit))
    finally:
        write(args.out/'result.json', result)
        if guard is not None: guard.release_signals()
    return result


def calibrate(paths, output, *, coverage=0.95, coverage_decision=None, stall_margin_us=10000, p99_margin_us=1000):
    """Three captures per cohort yield three all-pairs S3 comparisons.

    Fixed rule v1: max stall + max(10 ms, 10%); max(pair p99 delta,
    one 1-ms bin) + 1 ms. CLI margins may only increase those floors.
    """
    import math
    from wire_contract import compare
    require(0 < coverage <= 1 and (coverage >= .95 or bool(coverage_decision)), 'coverage below 0.95 needs coordinator decision')
    require(stall_margin_us >= 10000 and p99_margin_us >= 1000, 'margins below fixed rule floors')
    groups, inputs = {}, []
    require(len({Path(p).resolve() for p in paths}) == len(paths), 'duplicate calibration input')
    for path in paths:
        path = Path(path).resolve(); run = json.loads(path.read_text())
        require(run.get('calibration') is True and run.get('acquisition_passed') is True and not run.get('error') and
                run.get('board') == 'echoear' and not run.get('reference_only') and not run.get('headless'), 'need successful S3 calibration acquisitions')
        require(run.get('variant') in VARIANTS and run.get('pair_id'), 'missing calibration pair identity')
        expected = ['main', 'resume', 'cold'] if run['variant'].endswith('demo') else ['main']
        require([s['label'] for s in run['sessions']] == expected, 'calibration session labels incomplete')
        captures = {}
        for main in run['sessions']:
            wire = local_file(path.parent, main['wire_file'])
            require(digest(wire) == main['wire_sha256'], 'calibration capture hash changed')
            capture = parse_capture(wire.read_text().splitlines())
            require(len(capture.intervals) == 1 and capture.intervals[0] == main['intervals'], 'calibration intervals changed')
            validate_capture(capture, profile='product', intended_aec=run['variant'].startswith('aec-'), target='esp32s3')
            check_ports(capture)
            captures[main['label']] = capture
        groups.setdefault((run['variant'], run['pair_id']), []).append((run, captures))
        inputs.append(dict(path=str(path), sha256=digest(path),
                           wire_sha256={s['label']: s['wire_sha256'] for s in run['sessions']}))
    variants = {}
    for variant in VARIANTS:
        cohorts = [v for (name, _), v in groups.items() if name == variant]
        require(cohorts and all(len(c) >= 3 for c in cohorts), 'need three complete S3 captures per cohort: '+variant)
        pairs = [pair for cohort in cohorts for pair in combinations(cohort, 2)]
        rows = [capture.intervals[0] for cohort in cohorts for _, captures in cohort for capture in captures.values()]
        observed_stall = max(max(r['max_us'], r['last_ok_age_us']) for r in rows)
        margin = max(stall_margin_us, math.ceil(observed_stall/10))
        stall = observed_stall+margin
        preliminary = Bounds(stall_us=stall, p99_delta_us=10**12, coverage=coverage)
        for row in rows: measured(row, preliminary)
        differences = []
        for left, right in pairs:
            a, b = left[0], right[0]
            require(a['elf_sha256'] == b['elf_sha256'], 'calibration pair firmware differs')
            for label in left[1]:
                values = compare(left[1][label], right[1][label], profile='product', left_aec=variant.startswith('aec-'),
                    right_aec=variant.startswith('aec-'), bounds=preliminary, left_identity=a['identity'], right_identity=b['identity'],
                    left_target='esp32s3', right_target='esp32s3')
                differences.append(abs(values[0]['left']['p99_us']-values[0]['right']['p99_us']))
        variants[variant] = dict(stall_us=stall, p99_delta_us=max(1000, max(differences))+p99_margin_us,
                                 coverage=coverage, pairs=len(pairs), captures=sum(map(len, cohorts)), sessions=len(rows),
                                 observed_stall_us=observed_stall, observed_p99_delta_us=max(differences),
                                 observed_coverage_min=min(r['ok']*20000/(r['end_us']-r['start_us']) for r in rows),
                                 stall_margin_us=margin, p99_margin_us=p99_margin_us)
    fixture = dict(schema=1, status='measured', coverage_decision=coverage_decision, variants=variants, inputs=inputs,
                   margin_rule='v1: stall max + max(10000 us, ceil(10% max)); p99 max(1000 us, max pair delta) + 1000 us; CLI may increase floors',
                   basis='All-pairs same-process S3 captures including demo resume/cold; peer-submit only. Median lower bin edge, 1 ms resolution: 22.x ms cannot be distinguished from 22.0 ms.')
    require(not Path(output).exists(), 'bounds output exists; choose a new output for review')
    write(output, fixture)
    return fixture


def recheck_run(directory, output=None, *, bounds_path=BOUNDS_FILE):
    """Re-evaluate captured wire/timing evidence only; never contact live IO.

    A sidecar does not replace the original run verdict or qualify a board.
    Calibration still checks count/cadence/coverage without inventing a stall
    bound. Original turn/resource/restore verdicts remain in result.json.
    """
    directory = Path(directory).resolve()
    output = (Path(output) if output is not None else directory/'result-recheck.json').resolve()
    require(output.parent == directory and output.name.startswith('result-recheck') and output.suffix == '.json',
            'recheck output must be a new result-recheck*.json next to result.json')
    require(not output.exists(), 'recheck output exists; choose a new result-recheck file')
    source = directory/'result.json'
    original = json.loads(source.read_text())
    result = dict(schema=1, kind='offline-wire-recheck', checks_passed=False, qualification=False,
                  scope='captured wire/measured checks only; original turn/resource/restore verdicts retained',
                  source_result=str(source), source_result_sha256=digest(source),
                  source_passed=original.get('passed'), source_acquisition_passed=original.get('acquisition_passed'),
                  source_error=original.get('error'), sessions=[])
    try:
        require(not original.get('reference_only'), 'reference-only run has no candidate wire evidence to recheck')
        board, variant = original['board'], original['variant']
        require(board in CHIPS and variant in VARIANTS, 'unknown recheck board/variant')
        require(type(original['calibration']) is bool, 'missing recheck calibration mode')
        calibration = original['calibration']
        headless = original.get('headless', False)
        identity = json.loads((directory/'identity.json').read_text())
        identity_schema(identity)
        require(identity == original['identity'], 'recorded bot identity differs')
        result.update(board=board, variant=variant, calibration=calibration, headless=headless,
                      identity_sha256=digest(directory/'identity.json'),
                      bounds_status='calibration acquisition only' if calibration else 'fixed measured bounds')
        bounds = None if calibration else bounds_for(bounds_path, variant)
        if not calibration: result['bounds_sha256'] = digest(bounds_path)
        manifest = dict(aec_enabled=variant.startswith('aec-'))
        labels = ('main', 'resume', 'cold') if variant.endswith('demo') else ('main',)
        for label in labels:
            serial, bot = directory/(label+'-serial.log'), directory/(label+'-bot.log')
            lines, bot_lines = serial.read_text().splitlines(), bot.read_text().splitlines()
            require(not any('\x1b[' in line for line in bot_lines), 'bot log must be uncoloured')
            coverage = original.get('acquisition_coverage', .95)
            capture, _, timing = check_capture(lines, [l for l in bot_lines if 'DEMO_CAPABILITY ' in l],
                manifest=manifest, board=board, headless=headless, bounds=bounds, calibration=calibration,
                coverage=coverage, coverage_decision=original.get('coverage_decision'))
            row = capture.intervals[0]
            if calibration:
                timing = measured(row, Bounds(stall_us=max(1, row['max_us'], row['last_ok_age_us']),
                                             p99_delta_us=0, coverage=coverage))
            result['sessions'].append(dict(label=label, serial_sha256=digest(serial), bot_sha256=digest(bot),
                ports=check_ports(capture), intervals=row, timing=timing,
                observed_coverage=row['ok']*20000/(row['end_us']-row['start_us'])))
        result['checks_passed'] = True
    except Exception as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
    # Exclusive creation protects the original verdict and previous rechecks,
    # including a concurrent writer arriving after the existence check.
    with output.open('x') as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False)+'\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', choices=CHIPS)
    parser.add_argument('--preflight-tools', action='store_true', help='dry imports only, before bench.py')
    parser.add_argument('--recheck', type=Path, metavar='RUN_DIR', help='offline captured wire/timing checks; --out names a new result-recheck*.json')
    parser.add_argument('--bundle', type=Path); parser.add_argument('--out', type=Path)
    parser.add_argument('--headless', action='store_true'); parser.add_argument('--reference-only', action='store_true')
    parser.add_argument('--calibration', action='store_true')
    parser.add_argument('--bot-dir', type=Path); parser.add_argument('--bot-identity', type=Path)
    parser.add_argument('--bot-repo', type=Path, default=Path.home()/'src/dg-esp32')
    parser.add_argument('--pair-id'); parser.add_argument('--bounds', type=Path, default=BOUNDS_FILE)
    parser.add_argument('--p4-factory-backup', type=Path, default=FACTORY_BACKUP)
    parser.add_argument('--p4-restore-bundle', type=Path); parser.add_argument('--p4-restore-elf')
    parser.add_argument('--calibrate-bounds', nargs='+', type=Path, metavar='RESULT_JSON')
    parser.add_argument('--coverage', type=float, default=.95); parser.add_argument('--coverage-decision')
    parser.add_argument('--stall-margin-us', type=int, default=10000); parser.add_argument('--p99-margin-us', type=int, default=1000)
    args = parser.parse_args(argv)
    if args.recheck:
        result = recheck_run(args.recheck, args.out, bounds_path=args.bounds)
        print(json.dumps(dict(checks_passed=result['checks_passed'], qualification=False,
                              source_result=result['source_result'])))
        return 0 if result['checks_passed'] else 1
    if args.preflight_tools:
        preflight_tools()
        print('Dependency preflight passed (no bench/device access)')
        return 0
    if args.calibrate_bounds:
        if not args.out: parser.error('--out is the new bounds JSON path')
        calibrate(args.calibrate_bounds, args.out, coverage=args.coverage, coverage_decision=args.coverage_decision,
                  stall_margin_us=args.stall_margin_us, p99_margin_us=args.p99_margin_us)
        return 0
    if not all((args.board, args.bundle, args.out, args.bot_dir, args.bot_identity)):
        parser.error('--board, --bundle, --out, --bot-dir and --bot-identity are required')
    if args.calibration and (args.reference_only or not args.pair_id): parser.error('calibration requires --pair-id and candidate evidence')
    if bool(args.p4_restore_bundle) != bool(args.p4_restore_elf): parser.error('P4 product restore needs bundle AND pinned ELF')
    result = execute(args)
    print(json.dumps(dict(passed=result['passed'], acquisition_passed=result['acquisition_passed'], out=str(args.out))))
    return 0 if result['passed'] or (args.calibration and result['acquisition_passed'] and not result.get('error')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
