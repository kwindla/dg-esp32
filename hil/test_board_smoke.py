"""Host-only smoke orchestration, bundle integrity and recorded-log regressions."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import board_smoke as smoke
import make_bundle as bundle
from test_wire_contract import capture, serialize, intervals, cadence, IDENTITY
from wire_contract import Bounds, ContractError, measured


def image(tmp_path, *, aec=False, demo=False, board='echoear'):
    root = tmp_path/'bundle'; root.mkdir()
    for name, data in (('dg-esp32.elf', b'ELF public test fixture'), ('dg-esp32.bin', b'APP'),
                       ('bootloader/bootloader.bin', b'BOOT'), ('partition_table/partition-table.bin', b'PART')):
        path = root/name; path.parent.mkdir(exist_ok=True); path.write_bytes(data)
    flash = dict(write_flash_args=['--flash_mode', 'dio', '--flash_size', '16MB'],
                 flash_files={'0x0': 'bootloader/bootloader.bin', '0x8000': 'partition_table/partition-table.bin', '0x10000': 'dg-esp32.bin'})
    smoke.write(root/'flasher_args.json', flash)
    hashes = {str(p.relative_to(root)): bundle.digest(p) for p in root.rglob('*') if p.is_file()}
    manifest = dict(schema=1, commit='0'*40, firmware_commit='0'*40, board=board, target=bundle.CHIPS[board],
                    autostart=demo, aec_enabled=aec, profile=('aec-' if aec else '')+('demo' if demo else 'normal'),
                    elf_sha256=hashes['dg-esp32.elf'], sha256=hashes)
    smoke.write(root/'manifest.json', manifest)
    return root, manifest


def events(kind='long', aec=False):
    # Recorder shape from mute-03: two distinct ids, one row per direction.
    def frame(name, active=False): return dict(kind='frame', frame=name, bot_active=active)
    result = [frame('UserStartedSpeakingFrame'),
              dict(kind='aggregator-user-turn-stopped', text='What is the capital of France?' if kind == 'france' else 'Count to twenty slowly'),
              frame('BotStartedSpeakingFrame', True)]
    if kind == 'talkover' and aec:
        result += [frame('UserStartedSpeakingFrame', True), frame('InterruptionFrame', True),
                   frame('BotStoppedSpeakingFrame'),
                   dict(kind='aggregator-assistant-turn-stopped', text='One two three', interrupted=True),
                   dict(kind='aggregator-user-turn-stopped', text='What about Italy?'),
                   frame('BotStartedSpeakingFrame', True)]
    result += [frame('BotStoppedSpeakingFrame'),
               dict(kind='aggregator-assistant-turn-stopped', text='Paris' if kind == 'france' else
                    'Rome' if kind == 'talkover' and aec else 'One two three ... twenty')]
    expanded = []
    for row in result:
        if row['kind'] == 'frame':
            for direction in ('DOWNSTREAM', 'UPSTREAM'):
                expanded.append(dict(row, direction=direction, frame_id=len(expanded)+1, source='LLMUserAggregator'))
        else: expanded.append(row)
    for i, row in enumerate(expanded): row['monotonic_ns'] = i+1
    return expanded


def perf(stack=4096):
    return dict(pc_id='peer-one', output_volume_db=-6, heap=dict(internal=dict(minimum_free=100000, largest=50000),
                   psram=dict(free=8000000, largest=7000000)), failed_allocations=dict(internal=0, total=0),
                tasks=[dict(name='peer-loop', stack_free_bytes=stack)])


def options(tmp_path, root, **kw):
    identity = tmp_path/'identity.json'; smoke.write(identity, IDENTITY)
    args = SimpleNamespace(board='echoear', bundle=root, out=tmp_path/'run', headless=False, reference_only=False,
        calibration=True, bot_identity=identity, bot_dir=tmp_path/'bot', bot_repo=tmp_path,
        bounds=smoke.BOUNDS_FILE, pair_id='pair-one', p4_factory_backup=tmp_path/'factory',
        p4_restore_bundle=None, p4_restore_elf=None)
    vars(args).update(kw)
    return args


class FakeIO:
    def __init__(self, args, manifest, fail=None):
        self.args, self.manifest, self.fail = args, manifest, fail
        self.calls = []; self.now = 0; self.since = 0; self.active = False; self.number = 0
    def clock(self): return self.now
    def preflight_tools(self):
        self.calls.append('tools')
        if self.fail == 'tools': raise RuntimeError('dependency preflight: serial unavailable')
    def preflight_restore(self): self.calls.append('preflight')
    def hold(self):
        io = self
        class Guard:
            success = False
            record = {}
            def __enter__(self): io.calls.append('guard'); return self
            def touch(self): io.calls.append('touch')
            def __exit__(self, *exc):
                io.calls.append('restore')
                self.record = dict(clean=io.fail != 'restore', action=smoke.restore_decision(io.args.board, success=self.success))
            def release_signals(self): io.calls.append('signals-released')
        return Guard()
    def flash(self, *args):
        self.calls.append('flash')
        if self.fail == 'flash': raise RuntimeError('partial flash')
    @contextmanager
    def open(self):
        self.calls.append('open')
        try: yield object()
        finally: self.calls.append('serial-closed')
    def boot(self, board): self.calls.append('boot')
    def provision(self, board): self.calls.append('provision'); return dict(persisted_hashes=['cat'])
    def prepare_manual(self, board): self.calls.append('prepare-manual')
    def begin(self, board, action):
        self.calls.append('begin-'+action); self.active = True; self.since = self.clock(); self.frozen = False
    def snapshot(self, board):
        self.calls.append('snapshot'); return perf(4000 if self.fail == 'stack' else 4096)
    def turn(self, board, kind, index):
        self.calls.append(kind); self.now += 1
        if self.fail == 'turn': raise TimeoutError('turn failed')
        return dict(smoke.turn_checks(events(kind, self.manifest['aec_enabled']), kind, self.manifest['aec_enabled'], calibration=self.args.calibration), kind=kind)
    def pause(self, board, seconds): self.now += seconds
    def freeze(self, board):
        if not self.frozen:
            self.calls.append('freeze' if not self.args.reference_only else 'no-freeze-reference'); self.frozen = True
    def finish(self, board, label, hold=False):
        self.freeze(board)
        self.calls.append('hold' if hold else 'disconnect'); self.active = False
        self.number += 1
        value = capture(bundle.CHIPS[self.args.board], self.manifest['aec_enabled'])
        value.intervals[0]['gen'] = self.number
        lines = serialize(value)
        bot = [l for l in lines if 'DEMO_CAPABILITY ' in l]
        lines = [l for l in lines if 'DEMO_CAPABILITY ' not in l]
        if self.fail == 'none': lines.insert(-1, 'UPLINK_INTERVALS_NONE reason=already-reported')
        # A late message AFTER the intervals must remain in the capture.
        late = next(l for l in lines if 'msg=pong ' in l); lines.remove(late); lines.append(late)
        return lines+['I (50) esp-peer: PHASE closed t=900'], bot
    def held(self, board): self.calls.append('held-check'); return dict(held=True)
    def restored_assets(self, board, provision): self.calls.append('restored-assets'); return dict(passed=True)
    def cleanup(self, board):
        self.calls.append('cleanup')
        if self.fail == 'cleanup': raise RuntimeError('unreachable cleanup')
        if self.active: self.finish(board, 'failed')


@pytest.mark.parametrize('aec,demo', [(False, False), (False, True), (True, False), (True, True)])
def test_verified_bundle_mode_and_full_campaign(tmp_path, aec, demo):
    root, manifest = image(tmp_path, aec=aec, demo=demo)
    args = options(tmp_path, root); io = FakeIO(args, manifest)
    result = smoke.execute(args, backend=io)
    assert result['acquisition_passed'] and not result['passed'] and 'error' not in result
    assert result['mode'] == ('demo' if demo else 'manual')
    assert len(result['sessions']) == (3 if demo else 1)
    assert len(result['turns']) == (14 if demo else 12)
    assert sum(t['kind'] == 'long' for t in result['turns']) == 10
    assert io.calls.count('freeze') == len(result['sessions'])
    assert io.calls.index('touch') < io.calls.index('flash')
    assert io.calls.index('serial-closed') < io.calls.index('restore') < io.calls.index('signals-released')
    if demo:
        assert [c for c in io.calls if c.startswith('begin-')] == ['begin-cold', 'begin-resume', 'begin-cold']
        assert 'restored-assets' in io.calls
    else: assert 'begin-manual' in io.calls and 'provision' not in io.calls
    assert json.loads((args.out/'result.json').read_text()) == result


@pytest.mark.parametrize('failure,reason', [('flash', 'partial flash'), ('turn', 'turn failed'),
    ('stack', 'peer-loop free stack'), ('none', 'UPLINK_INTERVALS_NONE'), ('restore', 'cleanup/restore'),
    ('cleanup', 'cleanup/restore')])
def test_failure_keeps_result_and_restores_even_partial_flash(tmp_path, failure, reason):
    root, manifest = image(tmp_path)
    args = options(tmp_path, root); io = FakeIO(args, manifest, failure)
    result = smoke.execute(args, backend=io)
    assert not result['passed'] and reason in result['error']
    assert 'restore' in io.calls and io.calls[-1] == 'signals-released'
    assert (args.out/'result.json').is_file()


def test_p4_cleanup_failure_restores_factory(tmp_path):
    root, manifest = image(tmp_path, board='p4xc')
    args = options(tmp_path, root, board='p4xc'); io = FakeIO(args, manifest, 'cleanup')
    result = smoke.execute(args, backend=io)
    assert result['restore']['action'] == 'p4-factory' and not result['acquisition_passed']


@pytest.mark.parametrize('board,success,product,want', [('echoear', True, False, 'echoear-55152f25'),
    ('echoear', False, False, 'echoear-55152f25'), ('p4xc', True, False, 'keep-tested-p4'),
    ('p4xc', False, False, 'p4-factory'), ('p4xc', True, True, 'p4-product-of-record'),
    ('p4xc', False, True, 'p4-product-of-record')])
def test_restore_policy(board, success, product, want):
    assert smoke.restore_decision(board, success=success, product_restore=product) == want


def test_reference_only_does_not_require_or_freeze_instrumentation(tmp_path, monkeypatch):
    root, manifest = image(tmp_path, demo=True)
    monkeypatch.setitem(bundle.REFERENCE, manifest['elf_sha256'], (False, True))
    args = options(tmp_path, root, reference_only=True, calibration=False)
    io = FakeIO(args, manifest)
    result = smoke.execute(args, backend=io)
    assert result['passed'] and all('wire_file' not in r for r in result['sessions'])
    assert io.calls.count('no-freeze-reference') == 3 and 'freeze' not in io.calls


def test_unfixed_bounds_fail_before_backend_or_device_access(tmp_path):
    root, _ = image(tmp_path)
    pending = tmp_path/'pending-bounds.json'
    smoke.write(pending, dict(schema=1, status='pending', variants={}))
    args = options(tmp_path, root, calibration=False, bounds=pending)
    with pytest.raises(ContractError, match='bounds pending coordinator measurement'):
        smoke.execute(args, backend=Mock())
    assert not args.out.exists()


@pytest.mark.parametrize('kind,aec', [('france', False), ('france', True), ('talkover', False), ('talkover', True), ('long', True)])
def test_recorded_bot_events_pass_capability_expectations(kind, aec):
    verdict = smoke.turn_checks(events(kind, aec), kind, aec)
    assert verdict['passed']
    assert verdict['user_starts_during_bot'] == (1 if kind == 'talkover' and aec else 0)


@pytest.mark.parametrize('mutation,reason', [('self-start', 'self-interruption'), ('interrupt', 'self-interruption'),
    ('short', 'did not complete'), ('missing-speech', 'speech completion'), ('missing-active', 'bot-active'),
    ('no-paris', 'France/Paris'), ('no-rome', 'answer Rome'), ('aec-ignored', 'must interrupt')])
def test_wrong_behaviour_or_missing_witness_fails(mutation, reason):
    kind = 'france' if mutation == 'no-paris' else 'talkover' if mutation in ('no-rome', 'aec-ignored') else 'long'
    value = events(kind, mutation == 'no-rome')
    if mutation == 'self-start': value.insert(-1, dict(kind='frame', frame='UserStartedSpeakingFrame', bot_active=True, direction='DOWNSTREAM'))
    elif mutation == 'interrupt': value.insert(-1, dict(kind='frame', frame='InterruptionFrame', bot_active=True, direction='DOWNSTREAM'))
    elif mutation == 'short': value[-1]['text'] = 'One two three'
    elif mutation == 'missing-speech': value = [e for e in value if e.get('frame') != 'BotStoppedSpeakingFrame']
    elif mutation == 'missing-active': value[0].pop('bot_active')
    elif mutation in ('no-paris', 'no-rome'): value[-1]['text'] = 'Elsewhere'
    with pytest.raises(ContractError, match=reason): smoke.turn_checks(value, kind, mutation in ('no-rome', 'aec-ignored'))


def test_capture_sliced_at_close_keeps_late_message_and_rejects_none():
    value = capture()
    lines = serialize(value); pong = next(l for l in lines if 'msg=pong ' in l); lines.remove(pong)
    cap = [l for l in lines if 'DEMO_CAPABILITY ' in l]; lines = [l for l in lines if 'DEMO_CAPABILITY ' not in l]
    lines += [pong, 'I (40) esp-peer: PHASE closed t=9', 'unrelated after close']
    # Serial transport framing and unrelated console traffic must not affect
    # capture parsing, including a late msg after the intervals record.
    lines = [l+'\r\n' for l in lines]
    lines.insert(-2, 'OK #8 uplink-intervals\r\n')
    lines.insert(-2, 'PERF '+json.dumps(perf())+'\r\n')
    manifest = dict(aec_enabled=False)
    parsed, _, result = smoke.check_capture(lines, cap, manifest=manifest, board='echoear', bounds=Bounds(60000, 5000))
    assert parsed.records['msg=pong'] and result['median_us'] == 20000
    with pytest.raises(ContractError, match='unavailable evidence'):
        smoke.check_capture(lines[:1]+['UPLINK_INTERVALS_NONE reason=no-frames']+lines[1:], cap,
                            manifest=manifest, board='echoear', bounds=Bounds(60000, 5000))
    with pytest.raises(ContractError, match='missing close'): smoke.slice_sessions([l for l in lines if 'PHASE closed' not in l])
    with pytest.raises(ContractError, match='exactly one closed'): smoke.check_capture(lines+lines, cap, manifest=manifest, board='echoear')


def test_profile_missing_evidence_and_invalid_ports_fail():
    value = capture(); value.records.pop('opus_enc')
    lines = serialize(value)+['PHASE closed']; cap = [l for l in lines if 'DEMO_CAPABILITY ' in l]
    lines = [l for l in lines if 'DEMO_CAPABILITY ' not in l]
    with pytest.raises(ContractError, match='missing mandatory'):
        smoke.check_capture(lines, cap, manifest=dict(aec_enabled=False), board='echoear', calibration=True)
    value = capture(); value.records['sdp'][0] = 'm=audio 20000 UDP/TLS/RTP/SAVPF 111'
    assert smoke.check_ports(value) == [20000, 9]
    value.records['sdp'][0] = 'm=audio 0 UDP/TLS/RTP/SAVPF 111'
    with pytest.raises(ContractError, match='invalid SDP m-line port'):
        smoke.check_ports(value)


def test_headless_uses_contract_profile_without_bot_capability_requirement():
    value = capture(profile='headless')
    _, _, timing = smoke.check_capture(serialize(value)+['PHASE closed'], [], manifest=dict(aec_enabled=False),
                                      board='echoear', headless=True, bounds=Bounds(60000, 5000))
    assert timing


@pytest.mark.parametrize('step,passes', [(18000, True), (22000, True), (17000, False), (23000, False)])
def test_median_includes_exact_22ms(step, passes):
    row = cadence(step_us=step)
    if passes: assert measured(row, Bounds(60000, 5000, coverage=.8))['median_us'] == step
    else:
        with pytest.raises(ContractError, match='median outside'): measured(row, Bounds(60000, 5000, coverage=.8))


@pytest.mark.parametrize('mutation,reason', [('hash', 'hash mismatch'), ('profile', 'profile contradicts'),
    ('boolean', 'boolean capability'), ('board', 'board/target'), ('unhashed', 'Unhashed flash'),
    ('args', 'Unexpected write-flash'), ('offset', 'Invalid flash offset')])
def test_bundle_rejects_changed_or_inconsistent_metadata(tmp_path, mutation, reason):
    root, manifest = image(tmp_path)
    if mutation == 'hash': (root/'dg-esp32.bin').write_bytes(b'CHANGED')
    elif mutation == 'profile': manifest['profile'] = 'demo'
    elif mutation == 'boolean': manifest['autostart'] = 'false'
    elif mutation == 'board': manifest['board'] = 'p4xc'
    elif mutation in ('unhashed', 'args', 'offset'):
        flash = json.loads((root/'flasher_args.json').read_text())
        if mutation == 'unhashed': manifest['sha256'].pop('dg-esp32.bin')
        elif mutation == 'args': flash['write_flash_args'] = ['--erase-all', 'yes']
        else: flash['flash_files']['bad'] = flash['flash_files'].pop('0x0')
        smoke.write(root/'flasher_args.json', flash); manifest['sha256']['flasher_args.json'] = bundle.digest(root/'flasher_args.json')
    smoke.write(root/'manifest.json', manifest)
    with pytest.raises(ValueError, match=reason): bundle.verify_bundle(root)


@pytest.mark.parametrize('name', ['../secret.bin', '/absolute.bin'])
def test_bundle_paths_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError, match='relative'): bundle.local_file(tmp_path, name)
    (tmp_path/'outside').symlink_to(tmp_path.parent)
    with pytest.raises(ValueError, match='escapes'): bundle.local_file(tmp_path, 'outside/secret')


def test_make_bundle_copies_only_flash_inputs_and_verifies_flags(tmp_path, monkeypatch):
    root, manifest = image(tmp_path)
    project = dict(target='esp32s3', project_path=str(tmp_path), app_elf='dg-esp32.elf', app_bin='dg-esp32.bin', config_file='private-unread', project_version='0'*7)
    smoke.write(root/'project_description.json', project)
    monkeypatch.setattr(bundle, 'ROOT', tmp_path)
    monkeypatch.setattr(bundle, 'config_enabled', lambda _, key: key == 'DG_BOARD_ECHOEAR')
    def git(args, **kw):
        if args[1] == 'describe': return '0'*7+'\n'
        if args[1:] == ['diff', 'HEAD']: return b''
        return '0'*40+'\n' if args[1:3] == ['rev-parse', 'HEAD'] else ''
    monkeypatch.setattr(bundle.subprocess, 'check_output', git)
    output = tmp_path/'frozen'
    made = bundle.make_bundle(root, output)
    assert made['profile'] == 'normal' and not made['bench_used']
    assert not (output/'project_description.json').exists()
    assert set(made['sha256']) == set(manifest['sha256'])
    assert bundle.verify_bundle(output)[0]['elf_sha256'] == manifest['elf_sha256']


def test_boot_hash_and_target_are_checked_before_ready_is_trusted(tmp_path):
    _, manifest = image(tmp_path)
    lines = ['I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+manifest['elf_sha256'],
             'I (2) dg-esp32: DG_BOOT board=echoear chip=esp32s3 idf=v6.1', 'READY transport-console']
    assert smoke.boot_check(lines, manifest, 'echoear')
    with pytest.raises(ContractError, match='boot hash'): smoke.boot_check(lines+lines[:1], manifest, 'echoear')
    with pytest.raises(ContractError, match='wrong/malformed'): smoke.boot_check([lines[0].replace(manifest['elf_sha256'], 'f'*64)]+lines[1:], manifest, 'echoear')
    with pytest.raises(ContractError, match='board/target'): smoke.boot_check([lines[0], lines[1].replace('echoear', 'p4xc'), lines[2]], manifest, 'echoear')


def test_live_finish_never_retries_a_freeze_after_ack_timeout(tmp_path):
    io = object.__new__(smoke.LiveIO)
    io.args = SimpleNamespace(reference_only=False); io.interval_requested = False
    board = Mock(); board.command.side_effect = TimeoutError('ack timeout')
    for _ in range(2):
        with pytest.raises(TimeoutError, match='ack timeout'): io.finish(board, 'failed')
    assert [c.args[0] for c in board.command.call_args_list].count('uplink-intervals') == 1


def calibration_inputs(tmp_path):
    paths = []
    for variant in smoke.VARIANTS:
        for side in range(3):
            out = tmp_path/f'{variant}-{side}'; out.mkdir()
            sessions = []
            for label in (('main', 'resume', 'cold') if variant.endswith('demo') else ('main',)):
                value = capture(aec=variant.startswith('aec-')); value.intervals[0]['gen'] = len(paths)+1
                wire = out/(label+'-wire.log'); wire.write_text('\n'.join(serialize(value))+'\n')
                sessions.append(dict(label=label, intervals=value.intervals[0], wire_file=wire.name, wire_sha256=bundle.digest(wire)))
            result = dict(calibration=True, acquisition_passed=True, headless=False, board='echoear', reference_only=False,
                          variant=variant, pair_id='cohort', identity=copy.deepcopy(IDENTITY), elf_sha256='0'*64, sessions=sessions)
            smoke.write(out/'result.json', result); paths.append(out/'result.json')
    return paths


def test_bounds_freeze_requires_three_real_pairs_per_variant_and_records_coverage(tmp_path):
    paths = calibration_inputs(tmp_path); output = tmp_path/'measured.json'
    fixture = smoke.calibrate(paths, output, stall_margin_us=10000, p99_margin_us=2000)
    assert fixture['status'] == 'measured' and len(fixture['inputs']) == 12
    for name in smoke.VARIANTS:
        row = fixture['variants'][name]
        assert row['stall_us'] == 30000 and row['p99_delta_us'] == 3000 and row['pairs'] == 3
        assert row['observed_coverage_min'] >= .95 and row['captures'] == 3
        assert smoke.bounds_for(output, name).coverage == .95
    with pytest.raises(ContractError, match='below 0.95 needs coordinator decision'):
        smoke.calibrate(paths, tmp_path/'weak.json', coverage=.9)
    weak = smoke.calibrate(paths, tmp_path/'decided.json', coverage=.9, coverage_decision='coordinator decision test-only')
    assert weak['coverage_decision'] and weak['variants']['normal']['coverage'] == .9


@pytest.mark.parametrize('mutation,reason', [('incomplete', 'three complete'), ('identity', 'paired bot identity'),
    ('wire', 'capture hash changed'), ('duplicate', 'duplicate calibration'), ('reference', 'successful S3'),
    ('headless', 'successful S3'), ('elf', 'firmware differs')])
def test_calibration_cannot_mix_or_weaken_evidence(tmp_path, mutation, reason):
    paths = calibration_inputs(tmp_path)
    if mutation == 'incomplete': paths.pop()
    elif mutation == 'duplicate': paths.append(paths[0])
    elif mutation == 'wire': (paths[0].parent/'main-wire.log').write_text('tampered')
    else:
        value = json.loads(paths[0].read_text())
        if mutation == 'identity': value['identity']['bot_instance'] = 'another-bot'
        elif mutation == 'reference': value['reference_only'] = True
        elif mutation == 'headless': value['headless'] = True
        elif mutation == 'elf': value['elf_sha256'] = '1'*64
        smoke.write(paths[0], value)
    with pytest.raises(ContractError, match=reason): smoke.calibrate(paths, tmp_path/'bad.json')


def test_import_and_cli_help_are_host_only(capsys):
    with pytest.raises(SystemExit) as result: smoke.main(['--help'])
    assert result.value.code == 0 and '--reference-only' in capsys.readouterr().out


@pytest.fixture
def live_turn(tmp_path, monkeypatch):
    import sys
    def setup(kind='talkover', aec=False, late=False, exit_code=0, calibration=False):
        clock = SimpleNamespace(now=100.0)
        io = object.__new__(smoke.LiveIO)
        io.out = tmp_path; io.manifest = dict(aec_enabled=aec); io.clock = lambda: clock.now
        io.args = SimpleNamespace(calibration=calibration)
        io.poll = lambda board: None
        monkeypatch.setattr(smoke.time, 'monotonic_ns', lambda: int(clock.now*1e9))
        monkeypatch.setattr(smoke.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now+seconds))
        rows = events(kind, aec)
        # The interrupted assistant stop precedes Italy's user stop; Rome stops
        # later. Both recorder directions retain distinct ids and timestamps.
        base_times = [100.1, 102.6, 102.7, 105.7, 105.8, 105.9, 105.91, 107.1, 107.2, 108.1, 108.11] if kind == 'talkover' and aec else [100.1, 102.6, 102.7, 108.1, 108.11]
        logical = -1
        for row in rows:
            if row.get('direction') != 'UPSTREAM': logical += 1
            row['monotonic_ns'] = int((base_times[logical]+(.00001 if row.get('direction') == 'UPSTREAM' else 0))*1e9)
        if late == 'speech':
            for r in rows:
                if r.get('frame') == 'BotStartedSpeakingFrame' and r['monotonic_ns'] < 105e9: r['frame'] = 'BotStoppedSpeakingFrame'
        io.events = lambda: (tmp_path/'events.jsonl', [r for r in rows if r['monotonic_ns'] <= clock.now*1e9])
        waits, completions = [], []
        original_wait = io.wait
        def checked_wait(*a, **kw):
            waits.append((kw.get('label'), a[2], clock.now))
            result = original_wait(*a, **kw)
            if kw.get('label', '').startswith('turn finish'): completions.append(clock.now)
            if late == 'deadline' and kw.get('label') == 'talk-over deadline': clock.now += .5
            return result
        io.wait = checked_wait
        played, recordings = [], []
        def play(path, device):
            played.append((Path(path).name, clock.now))
            start = clock.now; clock.now += 2.36 if path.name == 'long.wav' else 1.36
            return dict(request_monotonic_s=start+(.5 if late is True and path.name == 'italy.wav' else 0), duration_valid=True)
        class Recording:
            def __init__(self, path, device, seconds):
                self.path = path; self.seconds = seconds; self.end = clock.now+seconds
                self.process = Mock(); self.process.wait.return_value = exit_code
                self.process.poll.side_effect = lambda: exit_code if clock.now >= self.end else None
                # Model the reviewed ALSA failure if production ever signals again.
                self.process.send_signal.side_effect = lambda *_: setattr(self.process.wait, 'return_value', 1)
                recordings.append(self)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def wait(self):
                clock.now = max(clock.now, self.end)
                code = self.process.wait(timeout=self.seconds+5)
                if code: raise RuntimeError(f'arecord exited {code}')
                return self.path
        monkeypatch.setitem(sys.modules, 'alsa', SimpleNamespace(play_wav=play, record_wav=Recording,
                             resolve_jabra=lambda: ('card', 'device'), set_volumes=lambda *a: None))
        return SimpleNamespace(io=io, clock=clock, rows=rows, played=played, recordings=recordings, waits=waits, completions=completions)
    return setup


@pytest.mark.parametrize('aec,late', [(False, False), (True, False), (True, True), (True, 'speech'), (True, 'deadline')])
def test_live_turn_schedules_italy_from_transcript_not_poll_time(live_turn, aec, late):
    run = live_turn(aec=aec, late=late)
    if late:
        reason = 'talk-over stimulus missed bot speech' if late == 'speech' else 'talk-over scheduling missed' if late == 'deadline' else 'actual Italy playback missed'
        with pytest.raises(ContractError, match=reason): run.io.turn(object(), 'talkover', 1)
        return
    result = run.io.turn(object(), 'talkover', 1)
    assert result['passed'] and run.clock.now == pytest.approx(130, abs=.025)
    assert run.completions[0] >= 108.11, 'reply wait must include the final reply, even when Recording.wait lasts longer'
    assert run.played[0][0] == 'long.wav' and run.played[1][0] == 'italy.wav'
    assert 105.6 <= run.played[1][1] <= 105.625


@pytest.mark.parametrize('kind,seconds', [('france', 15), ('long', 30), ('talkover', 30)])
def test_recording_finishes_naturally_without_signals(live_turn, kind, seconds):
    run = live_turn(kind=kind)
    assert run.io.turn(object(), kind, 1)['passed']
    recording, = run.recordings
    assert recording.seconds == seconds and run.clock.now == pytest.approx(100+seconds, abs=.025)
    recording.process.send_signal.assert_not_called()
    recording.process.terminate.assert_not_called()
    recording.process.kill.assert_not_called()
    recording.process.wait.assert_called_once_with(timeout=seconds+5)
    label, budget, at = next(w for w in run.waits if w[0].startswith('turn finish'))
    assert budget == pytest.approx(100+seconds-2-at)
    assert label == f'turn finish ({seconds} s recording)'


@pytest.mark.parametrize('code', [1, 2])
def test_nonzero_recording_exit_fails_with_reason(live_turn, code):
    run = live_turn(kind='france', exit_code=code)
    with pytest.raises(RuntimeError, match=f'arecord exited {code}'):
        run.io.turn(object(), 'france', 1)
    run.recordings[0].process.send_signal.assert_not_called()


@pytest.mark.parametrize('kind,seconds', [('france', 15), ('long', 30), ('talkover', 30)])
def test_turn_must_finish_before_recording_tail(live_turn, kind, seconds):
    run = live_turn(kind=kind)
    # Completion arrives inside the recording, but too late for the 2 s tail.
    for row in run.rows:
        if row.get('frame') == 'BotStoppedSpeakingFrame' or row['kind'] == 'aggregator-assistant-turn-stopped':
            row['monotonic_ns'] = int((100+seconds-1)*1e9)
    with pytest.raises(TimeoutError, match=rf'turn finish \({seconds} s recording\) exceeded'):
        run.io.turn(object(), kind, 1)
    assert run.clock.now == pytest.approx(100+seconds-2, abs=.03)
    run.recordings[0].process.wait.assert_not_called()


def test_recording_remainder_cannot_hide_extra_user_start(live_turn):
    run = live_turn(kind='long')
    run.rows.append(dict(kind='frame', frame='UserStartedSpeakingFrame', direction='DOWNSTREAM',
                         bot_active=False, monotonic_ns=120000000000))
    with pytest.raises(ContractError, match='self-interruption or unexpected user turn'):
        run.io.turn(object(), 'long', 1)
    assert run.clock.now == pytest.approx(130, abs=.025)




def test_live_hold_checks_persisted_enabled_settings(tmp_path):
    io = object.__new__(smoke.LiveIO); io.pause = lambda *a: None
    board = Mock(); board.cursor = 1; board.lines_since.return_value = []
    state = dict(held=True, initializing=False, configured=True, enabled=True, volume_db=-6)
    board.wait_for.return_value = 'DEMO '+json.dumps(state)
    assert io.held(board) == state
    state['held'] = False; board.wait_for.return_value = 'DEMO '+json.dumps(state)
    with pytest.raises(ContractError, match='F20 hold state failed'): io.held(board)


@pytest.mark.parametrize('mutation,reason', [('internal', 'heap/allocation'), ('allocations', 'heap/allocation'),
    ('missing-stack', 'peer-loop free stack'), ('psram', 'PSRAM'), ('volume', 'playback volume')])
def test_resource_evidence_fails_closed(mutation, reason):
    row = perf()
    if mutation == 'internal': row['heap']['internal']['minimum_free'] = 81919
    elif mutation == 'allocations': row['failed_allocations']['total'] = 1
    elif mutation == 'missing-stack': row['tasks'].clear()
    elif mutation == 'volume': row['output_volume_db'] = 0
    else: row['heap']['psram'].pop('largest')
    with pytest.raises(ContractError, match=reason): smoke.resource_check([row], 'echoear')


def test_stale_build_cannot_be_labelled_as_current_head(tmp_path, monkeypatch):
    root, _ = image(tmp_path)
    smoke.write(root/'project_description.json', dict(target='esp32s3', project_path=str(tmp_path),
                 app_elf='dg-esp32.elf', app_bin='dg-esp32.bin', project_version='old'))
    monkeypatch.setattr(bundle, 'ROOT', tmp_path)
    monkeypatch.setattr(bundle.subprocess, 'check_output', lambda *a, **k: 'new')
    with pytest.raises(ValueError, match='Stale build version'): bundle.make_bundle(root, tmp_path/'out')
    assert not (tmp_path/'out').exists()



def test_recorder_tail_uses_complete_lines_and_keeps_event_witnesses(tmp_path):
    directory = tmp_path/'session-one'; directory.mkdir(); path = directory/'events.jsonl'
    rows = [dict(kind='session-start', pc_id=None), dict(kind='peer-binding', pc_id='p'),
            dict(kind='audio', pc_id='p'), dict(kind='frame', frame='BotStartedSpeakingFrame', pc_id='p')]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows)+ '{"kind":"frame"')
    assert smoke.recorder_path(tmp_path, 'p') == path
    tail = smoke.RecorderTail(path, 'p')
    assert [r['kind'] for r in tail.poll()] == ['peer-binding', 'frame']
    assert len(tail.poll()) == 2
    with path.open('a') as stream: stream.write(',"pc_id":"p"}\n')
    assert len(tail.poll()) == 3
    path.write_text('')
    with pytest.raises(ContractError, match='rotated/truncated'): tail.poll()


def test_recorder_identity_ambiguity_and_peer_switch_fail(tmp_path):
    for name in ('one', 'two'):
        folder = tmp_path/('session-'+name); folder.mkdir()
        (folder/'events.jsonl').write_text('{"kind":"peer-binding","pc_id":"p"}\n')
    with pytest.raises(ContractError, match='ambiguous'): smoke.recorder_path(tmp_path, 'p')
    path = tmp_path/'session-one/events.jsonl'; tail = smoke.RecorderTail(path, 'p'); tail.poll()
    with path.open('a') as stream: stream.write('{"kind":"frame","pc_id":"other"}\n')
    with pytest.raises(ContractError, match='peer identity changed'): tail.poll()


def test_dependency_failure_precedes_restore_guard_and_touch(tmp_path):
    root, manifest = image(tmp_path)
    args = options(tmp_path, root); io = FakeIO(args, manifest, 'tools')
    result = smoke.execute(args, backend=io)
    assert 'dependency preflight' in result['error']
    assert not {'preflight', 'guard', 'touch', 'flash', 'restore'} & set(io.calls)


@pytest.mark.parametrize('module', ['esptool', 'serial'])
def test_dry_import_failure_has_no_bench_action(monkeypatch, module):
    import boards
    lock = Mock(side_effect=AssertionError('must not take bench'))
    monkeypatch.setattr(boards, 'require_bench', lock)
    monkeypatch.setattr(boards, 'checked_esptool_python', lambda: 'checked-python')
    monkeypatch.setattr(smoke.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='No module named '+module))
    with pytest.raises(ContractError, match='dependency preflight: esptool 5.5.0 and serial'):
        smoke.preflight_tools()
    lock.assert_not_called()


@pytest.mark.parametrize('board', ['echoear', 'p4xc'])
def test_mac_uses_checked_interpreter_and_no_reset(monkeypatch, board):
    import boards
    monkeypatch.setattr(boards, 'require_bench', lambda: 123)
    monkeypatch.setattr(boards, 'resolve_port', lambda **k: 'pinned-port')
    monkeypatch.setattr(boards, 'checked_esptool_python', lambda: 'checked-python')
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='MAC: '+boards.BOARDS[board]['efuse_mac']))
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    assert smoke.verify_flash_mac(board) == 'pinned-port'
    cmd = run.call_args.args[0]
    assert cmd[0] == 'checked-python' and cmd[-3:] == ['--after', 'no-reset', 'read-mac']
    run.return_value.stdout = 'MAC: 00:00:00:00:00:00'
    with pytest.raises(ContractError, match='refusing access'): smoke.verify_flash_mac(board)


def test_freeze_precedes_end_perf_and_stop(tmp_path):
    root, manifest = image(tmp_path, demo=True)
    args = options(tmp_path, root); io = FakeIO(args, manifest)
    assert smoke.execute(args, backend=io)['acquisition_passed']
    for i, call in enumerate(io.calls):
        if call == 'freeze': assert io.calls[i+1:i+3] == ['snapshot', 'hold']


@pytest.mark.parametrize('field,value,reason', [('pairs', 2, 'three S3 pairs'), ('coverage', .94, 'below 0.95')])
def test_measured_bounds_guards(tmp_path, field, value, reason):
    row = dict(pairs=3, coverage=.95, stall_us=60000, p99_delta_us=2000); row[field] = value
    path = tmp_path/'bounds.json'; smoke.write(path, dict(schema=1, status='measured', variants=dict(normal=row)))
    with pytest.raises(ContractError, match=reason): smoke.bounds_for(path, 'normal')


@pytest.mark.parametrize('mutation,reason', [('fatal', 'fatal boot diagnostic'), ('ready', 'ready after boot'), ('gain', 'MIC3_GAIN boot mismatch')])
def test_boot_negative_witnesses(tmp_path, mutation, reason):
    _, manifest = image(tmp_path, aec=mutation == 'gain')
    manifest['boot_ref_gain_db'] = 15
    lines = ['I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+manifest['elf_sha256'],
             'I (2) dg-esp32: DG_BOOT board=echoear chip=esp32s3 idf=v6.1', 'READY transport-console']
    if mutation == 'fatal': lines.append('Guru Meditation Error')
    elif mutation == 'ready': lines = [lines[-1]]+lines[:-1]
    else:
        assert smoke.boot_check(lines+['MIC3_GAIN=0x15 analog_db=15'], manifest, 'echoear')
        lines.append('MIC3_GAIN=0x14 analog_db=12')
    with pytest.raises(ContractError, match=reason): smoke.boot_check(lines, manifest, 'echoear')


def test_new_session_without_close():
    with pytest.raises(ContractError, match='new session before previous close'):
        smoke.slice_sessions(['WIRE_CONTRACT start={}', 'WIRE_CONTRACT start={}', 'PHASE closed'])


@pytest.mark.parametrize('mutation,reason', [('generation', 'need one interval generation'), ('count', 'calibration needs 500 intervals'), ('coverage', 'below 0.95')])
def test_capture_guards_independently(monkeypatch, mutation, reason):
    value = capture()
    if mutation == 'generation': value.intervals.append(copy.deepcopy(value.intervals[0]))
    elif mutation == 'count': value.intervals[0]['n'] = 499
    monkeypatch.setattr(smoke, 'parse_capture', lambda _: value)
    monkeypatch.setattr(smoke, 'validate_capture', lambda *a, **k: None)
    # Isolate these harness checks from the checker's duplicate count gate.
    monkeypatch.setattr(smoke, 'measured', lambda *a: {})
    with pytest.raises(ContractError, match=reason):
        smoke.check_capture(['WIRE_CONTRACT start={}', 'PHASE closed'], [], manifest=dict(aec_enabled=False),
                            board='echoear', calibration=True, coverage=.94 if mutation == 'coverage' else .95)


@pytest.mark.parametrize('mutation,reason', [('italy', 'AEC-off must ignore'), ('error', 'bot pipeline error'), ('direction', 'missing frame direction')])
def test_bot_checks_independent_failures(mutation, reason):
    value = events('talkover')
    if mutation == 'italy': value.insert(-1, dict(kind='aggregator-user-turn-stopped', text='Italy'))
    elif mutation == 'error': value.append(dict(kind='frame', frame='ErrorFrame', direction='DOWNSTREAM'))
    else: value[0].pop('direction')
    with pytest.raises(ContractError, match=reason): smoke.turn_checks(value, 'talkover', False)


def test_interrupted_assistant_stop_does_not_finish_rome_turn():
    rows = events('talkover', True)
    user_at = next(i for i, r in enumerate(rows) if r.get('text') == 'What about Italy?')
    assert not smoke.turn_finished(rows[:user_at+1], 2)
    assert not smoke.turn_finished(rows[:user_at], 2)
    assert smoke.turn_finished(rows, 2)


def test_profile_manifest_failure_is_explicit(tmp_path):
    io = object.__new__(smoke.LiveIO)
    io.out = tmp_path; (tmp_path/'serial.log').touch(); (tmp_path/'bot.log').touch()
    io.args = SimpleNamespace(bot_dir=tmp_path, headless=True)
    io.clock = lambda: 0; io.snapshot = lambda _: perf(); io.identity = IDENTITY
    io.events = lambda: (None, [dict(kind='stt-profile', pc_id='peer-one', monotonic_ns=1, wrong=True)])
    board = Mock(); board.cursor = 1
    board.wait_for_entry.return_value = (1, 0, 'STATE Ready gen=1')
    io.poll = Mock()
    with pytest.raises(ContractError, match='bot profile manifest differs'): io.begin(board, 'manual')


def test_uncoloured_bot_guard_and_serial_crlf(tmp_path):
    io = object.__new__(smoke.LiveIO)
    io.out = tmp_path; io.args = SimpleNamespace(bot_dir=tmp_path, reference_only=False)
    io.interval_requested = False; io.serial_at = io.bot_at = 0
    (tmp_path/'serial.log').write_bytes(b'OK #1\r\nPERF {"pc_id":"p"}\r\nPHASE closed\r\n')
    (tmp_path/'bot.log').write_text('\x1b[31mDEMO_CAPABILITY {}\n')
    io.events = lambda: (None, [])
    board = Mock(); board.cursor = 1
    with pytest.raises(ContractError, match='bot log must be uncoloured'): io.finish(board, 'main')
    (tmp_path/'bot.log').write_text('DEMO_CAPABILITY {}\n')
    lines, _ = io.finish(board, 'main')
    assert lines == ['OK #1', 'PERF {"pc_id":"p"}', 'PHASE closed']


def test_held_reconnect_is_explicit():
    io = object.__new__(smoke.LiveIO); io.pause = lambda *a: None
    board = Mock(); board.cursor = 1; board.lines_since.return_value = [(2, 0, 'STATE Ready gen=2')]
    board.wait_for.return_value = 'DEMO '+json.dumps(dict(held=True, initializing=False, configured=True, enabled=True, volume_db=-6))
    with pytest.raises(ContractError, match='held demo reconnected'): io.held(board)


def test_missing_cold_assets_is_explicit(monkeypatch):
    import sys
    io = object.__new__(smoke.LiveIO); io.args = SimpleNamespace(headless=False)
    monkeypatch.setitem(sys.modules, 'step9a_support', SimpleNamespace(read=lambda *a: dict(items=[], next=0)))
    with pytest.raises(ContractError, match='F20 cold boot persisted assets missing'):
        io.restored_assets(object(), dict(persisted_hashes=['asset']))


@pytest.fixture
def build_setup(tmp_path, monkeypatch):
    root, manifest = image(tmp_path)
    project = dict(target='esp32s3', project_path=str(tmp_path), app_elf='dg-esp32.elf', app_bin='dg-esp32.bin',
                   config_file='private-unread', project_version='1234567')
    smoke.write(root/'project_description.json', project)
    monkeypatch.setattr(bundle, 'ROOT', tmp_path)
    monkeypatch.setattr(bundle, 'config_enabled', lambda _, key: key == 'DG_BOARD_ECHOEAR')
    state = dict(version='1234567-dirty', inputs='')
    def git(args, **kw):
        if args[1] == 'describe': return state['version']
        if args[1:3] == ['rev-parse', 'HEAD']: return '0'*40
        if args[1:3] == ['ls-files', 'main']: return state['inputs']
        return '' if kw.get('text') else b''
    monkeypatch.setattr(bundle.subprocess, 'check_output', git)
    return root, manifest, project, state


def test_dirty_suffix_is_not_a_stale_build(build_setup, tmp_path):
    root, _, _, _ = build_setup
    assert bundle.make_bundle(root, tmp_path/'out')['project_version'] == '1234567-dirty'


@pytest.mark.parametrize('mutation,reason', [('fresh', 'Firmware input newer'), ('board', 'Resolved board mismatch'), ('app', 'Application missing')])
def test_build_failures_independently(build_setup, tmp_path, monkeypatch, mutation, reason):
    import os
    root, _, _, state = build_setup
    if mutation == 'fresh':
        source = tmp_path/'input.cpp'; source.write_text('new'); state['inputs'] = 'input.cpp\n'
        at = (root/'dg-esp32.elf').stat().st_mtime_ns+1000000; os.utime(source, ns=(at, at))
    elif mutation == 'board': monkeypatch.setattr(bundle, 'config_enabled', lambda *a: False)
    else:
        flash = json.loads((root/'flasher_args.json').read_text()); flash['flash_files'].pop('0x10000')
        smoke.write(root/'flasher_args.json', flash)
    with pytest.raises(ValueError, match=reason): bundle.make_bundle(root, tmp_path/'out')


@pytest.mark.parametrize('mutation,reason', [('elf', 'ELF identity mismatch'), ('chip', 'Flash map chip mismatch'),
    ('reference', 'Reference-only requires'), ('commit', 'Missing firmware commit'), ('offset', 'Duplicate flash offset'),
    ('hashmaps', 'files/sha256 disagree')])
def test_bundle_guard_failures_independently(tmp_path, mutation, reason):
    root, manifest = image(tmp_path)
    if mutation == 'elf': manifest['elf_sha256'] = 'f'*64
    elif mutation == 'commit': manifest['firmware_commit'] = 'bad'
    elif mutation == 'hashmaps': manifest['files'] = {'dg-esp32.elf': dict(sha256='f'*64, bytes=1)}
    elif mutation in ('chip', 'offset'):
        flash = json.loads((root/'flasher_args.json').read_text())
        if mutation == 'chip': flash['extra_esptool_args'] = dict(chip='esp32p4')
        else: flash['flash_files']['0x0000'] = flash['flash_files']['0x0']
        smoke.write(root/'flasher_args.json', flash); manifest['sha256']['flasher_args.json'] = bundle.digest(root/'flasher_args.json')
    smoke.write(root/'manifest.json', manifest)
    with pytest.raises(ValueError, match=reason): bundle.verify_bundle(root, reference_only=mutation == 'reference')


def test_aec_bundle_uses_existing_builder_fields(build_setup, tmp_path, monkeypatch):
    root, _, _, _ = build_setup
    monkeypatch.setattr(bundle, 'config_enabled', lambda *a: True)
    monkeypatch.setattr(bundle, 'config_gain', lambda *a: 15)
    made = bundle.make_bundle(root, tmp_path/'aec')
    assert made['boot_ref_gain_db'] == 15 and made['expected_boot_line'] == 'MIC3_GAIN=0x15 analog_db=15'
    assert made['config']['CONFIG_DG_WIFI_OBSERVE'] == 1
    from echoear_aec_bench import verify_bundle
    verify_bundle(tmp_path/'aec', made['elf_sha256'], 15)


@pytest.mark.parametrize('mutation,reason', [('intervals', 'calibration intervals changed'), ('exists', 'bounds output exists'),
    ('margins', 'margins below fixed rule'), ('sessions', 'session labels incomplete')])
def test_calibration_failures_independently(tmp_path, mutation, reason):
    paths = calibration_inputs(tmp_path); output = tmp_path/'bounds.json'
    if mutation == 'intervals':
        run = json.loads(paths[0].read_text()); run['sessions'][0]['intervals']['gen'] += 99; smoke.write(paths[0], run)
    elif mutation == 'exists': output.write_text('keep')
    elif mutation == 'sessions':
        run = json.loads(paths[-1].read_text()); run['sessions'].pop(); smoke.write(paths[-1], run)
    with pytest.raises(ContractError, match=reason):
        smoke.calibrate(paths, output, stall_margin_us=0 if mutation == 'margins' else 10000)


def test_calibration_includes_demo_sessions_and_nonzero_p99(tmp_path):
    paths = calibration_inputs(tmp_path)
    run = json.loads(paths[-1].read_text()); session = run['sessions'][-1]
    value = capture(aec=True); value.intervals[0] = cadence(step_us=22000)
    wire = paths[-1].parent/session['wire_file']; wire.write_text('\n'.join(serialize(value))+'\n')
    session.update(intervals=value.intervals[0], wire_sha256=bundle.digest(wire)); smoke.write(paths[-1], run)
    fixture = smoke.calibrate(paths, tmp_path/'bounds.json', coverage=.9, coverage_decision='test-only')
    row = fixture['variants']['aec-demo']
    assert row['sessions'] == 9 and row['observed_stall_us'] == 22000
    assert row['observed_p99_delta_us'] == 2000 and row['p99_delta_us'] == 3000
    assert .9 < row['observed_coverage_min'] < .95


def test_real_mute03_recorder_shape_and_completion(tmp_path):
    original = Path(__file__).with_name('fixtures')/'board_smoke_mute03_events.jsonl'
    rows = [json.loads(line) for line in original.read_text().splitlines()]
    folder = tmp_path/'session-sanitized'; folder.mkdir(); path = folder/'events.jsonl'
    path.write_bytes(original.read_bytes())
    assert smoke.recorder_path(tmp_path, 'sanitized-peer') == path
    observed = smoke.RecorderTail(path, 'sanitized-peer').poll()
    verdict = smoke.turn_checks(observed, 'long', True)
    assert verdict['user_turn_starts'] == 1 and verdict['interruptions_during_bot'] == 0
    assert smoke.turn_finished(observed, 1)
    frames = [r for r in rows if r['kind'] == 'frame']
    assert len(frames) == 6 and len({r['frame_id'] for r in frames}) == 6
    assert {r['direction'] for r in frames} == {'UPSTREAM', 'DOWNSTREAM'}


def test_snapshot_parses_real_perf_serial_envelope(tmp_path):
    io = object.__new__(smoke.LiveIO); io.out = tmp_path; io.snapshot_index = 0
    board = Mock(); board.cursor = 7
    row = json.loads((Path(__file__).with_name('fixtures')/'board_smoke_mute03_perf.json').read_text())
    board.wait_for.return_value = 'PERF '+json.dumps(row)
    assert io.snapshot(board) == row
    assert json.loads((tmp_path/'perf-001.json').read_text()) == row
    # The older image lacks today's volume witness; it must fail closed.
    with pytest.raises(ContractError, match='playback volume'): smoke.resource_check([row], 'echoear')


@pytest.mark.parametrize('missing', [False, True])
def test_gain_uses_only_exact_grid_counts(monkeypatch, missing):
    calls = []
    def count(args, **kw):
        calls.append(args)
        found = args[2] == '^CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15$' and not missing
        return SimpleNamespace(returncode=0 if found else 1, stdout='1\n' if found else '0\n')
    monkeypatch.setattr(bundle.subprocess, 'run', count)
    if missing:
        with pytest.raises(ValueError, match='Need one approved AEC ref gain'): bundle.config_gain('private-unread')
    else: assert bundle.config_gain('private-unread') == 15
    assert len(calls) == 12 and all(c[:2] == ['grep', '-c'] for c in calls)


def test_wait_timeout_names_condition(monkeypatch):
    io = object.__new__(smoke.LiveIO); io.clock = Mock(side_effect=[0, 2]); io.poll = Mock()
    with pytest.raises(TimeoutError, match='user transcript exceeded'):
        io.wait(object(), lambda: False, 1, label='user transcript')


def test_missing_aec_gain_reports_its_reason(tmp_path):
    _, manifest = image(tmp_path, aec=True)
    lines = ['I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+manifest['elf_sha256'],
             'I (2) dg-esp32: DG_BOOT board=echoear chip=esp32s3 idf=v6.1', 'READY transport-console',
             'MIC3_GAIN=0x15 analog_db=15']
    with pytest.raises(ContractError, match='missing AEC boot ref gain'):
        smoke.boot_check(lines, manifest, 'echoear')


def test_preflight_import_failure_reports_module_before_bench(monkeypatch):
    import boards, step9a_echoear
    monkeypatch.setattr(boards, 'checked_esptool_python', lambda: 'checked-python')
    monkeypatch.setattr(smoke.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='5.5.0\n'))
    monkeypatch.setattr(step9a_echoear, 'check_esptool', lambda: 'checked-python')
    lock = Mock(side_effect=AssertionError('must not access bench'))
    monkeypatch.setattr(boards, 'require_bench', lock)
    imported = []
    def load(name):
        imported.append(name)
        if name == 'alsa': raise ModuleNotFoundError('missing ALSA dependency')
        return SimpleNamespace()
    monkeypatch.setattr(smoke.importlib, 'import_module', load)
    with pytest.raises(RuntimeError, match='dependency preflight: cannot import alsa'):
        smoke.preflight_tools()
    assert imported == ['serial', 'esp_serial', 'board_serial', 'alsa']
    lock.assert_not_called()


def test_active_session_minimum_starts_after_ready(tmp_path):
    io = object.__new__(smoke.LiveIO)
    io.out = tmp_path; (tmp_path/'serial.log').touch(); (tmp_path/'bot.log').touch()
    io.args = SimpleNamespace(bot_dir=tmp_path, headless=True)
    clock = SimpleNamespace(now=0)
    io.clock = lambda: clock.now; io.snapshot = lambda _: perf(); io.identity = IDENTITY
    io.events = lambda: (None, [dict(kind='stt-profile', pc_id='peer-one', monotonic_ns=1, **IDENTITY['profile_manifest'])])
    board = Mock(); board.cursor = 1
    def ready(*args, **kwargs):
        clock.now = 20
        return 1, 20, 'STATE Ready gen=1'
    board.wait_for_entry.side_effect = ready
    io.poll = Mock()
    io.begin(board, 'manual')
    assert io.since == 20, 'reset/connect time must not count toward the active-session minimum'


def test_short_sessions_freeze_only_after_thirteen_active_seconds(tmp_path):
    root, manifest = image(tmp_path, demo=True)
    args = options(tmp_path, root); io = FakeIO(args, manifest)
    frozen_after = []
    freeze = io.freeze
    def timed_freeze(board):
        if not io.frozen: frozen_after.append(io.clock()-io.since)
        freeze(board)
    io.freeze = timed_freeze
    assert smoke.execute(args, backend=io)['acquisition_passed']
    assert frozen_after == [13, 13, 13], 'every session needs the 13-second active minimum'


def test_ten_percent_margin_dominates_floor_for_large_stall(tmp_path):
    paths = calibration_inputs(tmp_path)
    run = json.loads(paths[0].read_text()); session = run['sessions'][0]
    value = capture(); row = value.intervals[0]
    row['hist'][20] -= 1; row['hist'][150] = 1; row['max_us'] = 150001
    row['end_us'] += 130001  # Preserve span/count/coverage with one long gap.
    wire = paths[0].parent/session['wire_file']; wire.write_text('\n'.join(serialize(value))+'\n')
    session.update(intervals=row, wire_sha256=bundle.digest(wire)); smoke.write(paths[0], run)
    fixture = smoke.calibrate(paths, tmp_path/'bounds.json')
    row = fixture['variants']['normal']
    assert row['observed_stall_us'] == 150001
    assert row['stall_margin_us'] == 15001 and row['stall_us'] == 165002, '10% margin must round upward above the 10-ms floor'


def test_bundle_size_mismatch_reports_size_not_hash(tmp_path):
    root, manifest = image(tmp_path)
    manifest['files'] = {name: dict(sha256=sha, bytes=(root/name).stat().st_size) for name, sha in manifest['sha256'].items()}
    manifest['files']['dg-esp32.bin']['bytes'] += 1
    smoke.write(root/'manifest.json', manifest)
    with pytest.raises(ValueError, match='Bundle file size mismatch'): bundle.verify_bundle(root)


def test_multiple_gain_hits_fail_with_reason(monkeypatch):
    def count(args, **kw):
        found = args[2] in ('^CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=12$', '^CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15$')
        return SimpleNamespace(returncode=0 if found else 1, stdout='1\n' if found else '0\n')
    monkeypatch.setattr(bundle.subprocess, 'run', count)
    with pytest.raises(ValueError, match='Need one approved AEC ref gain'): bundle.config_gain('private-unread')


@pytest.mark.parametrize('stale', ['assistant', 'speech'])
def test_each_completion_witness_must_follow_latest_user(stale):
    user = dict(kind='aggregator-user-turn-stopped', monotonic_ns=20)
    assistant = dict(kind='aggregator-assistant-turn-stopped', monotonic_ns=10 if stale == 'assistant' else 30)
    speech = dict(kind='frame', frame='BotStoppedSpeakingFrame', direction='DOWNSTREAM',
                  monotonic_ns=10 if stale == 'speech' else 30)
    assert not smoke.turn_finished([user, assistant, speech], 1), f'stale {stale} completion must not end the latest user turn'


@pytest.fixture
def recorded_run(tmp_path):
    run = tmp_path/'captured'; run.mkdir()
    original = dict(schema=1, board='echoear', variant='normal', mode='manual', headless=False,
                    reference_only=False, calibration=True, identity=IDENTITY, acquisition_coverage=.95,
                    passed=False, acquisition_passed=False, sessions=[],
                    error='ContractError: contract finding: SDP m-line port is not placeholder 9')
    smoke.write(run/'result.json', original); smoke.write(run/'identity.json', IDENTITY)
    value = capture(); value.records['sdp'][4] = 'm=application 50712 UDP/DTLS/SCTP webrtc-datachannel'
    def save(value, *, close=True):
        lines = serialize(value)
        (run/'main-serial.log').write_text('\n'.join(l for l in lines if 'DEMO_CAPABILITY ' not in l)+
                                         ('\nPHASE closed\n' if close else '\n'))
        (run/'main-bot.log').write_text('\n'.join(l for l in lines if 'DEMO_CAPABILITY ' in l)+'\n')
    save(value)
    return SimpleNamespace(path=run, original=original, capture=value, save=save)


def test_offline_recheck_preserves_original_and_never_uses_live_io(recorded_run, monkeypatch):
    run = recorded_run
    live = Mock(side_effect=AssertionError('must not contact a device or bot'))
    monkeypatch.setattr(smoke, 'LiveIO', live); monkeypatch.setattr(smoke, 'preflight_tools', live)
    original_bytes = (run.path/'result.json').read_bytes()
    result = smoke.recheck_run(run.path)
    assert result['checks_passed'] and result['qualification'] is False
    assert result['calibration'] and result['bounds_status'] == 'calibration acquisition only'
    assert result['source_error'] == run.original['error'] and result['source_passed'] is False
    assert result['sessions'][0]['ports'] == [9, 50712]
    assert result['sessions'][0]['timing']['median_us'] == 20000
    assert result['source_result_sha256'] == bundle.digest(run.path/'result.json')
    assert json.loads((run.path/'result-recheck.json').read_text()) == result
    assert (run.path/'result.json').read_bytes() == original_bytes
    live.assert_not_called()


@pytest.mark.parametrize('destination,reason', [('original', 'must be a new result-recheck'),
    ('existing', 'recheck output exists'), ('outside', 'must be a new result-recheck')])
def test_offline_recheck_cannot_overwrite_results_or_escape_run(recorded_run, destination, reason):
    run = recorded_run
    if destination == 'original': output = run.path/'result.json'
    elif destination == 'outside': output = run.path.parent/'result-recheck.json'
    else: output = run.path/'result-recheck-previous.json'; output.write_text('keep this result')
    before = output.read_bytes() if output.exists() else None
    with pytest.raises(ContractError, match=reason): smoke.recheck_run(run.path, output)
    assert (output.read_bytes() if output.exists() else None) == before


@pytest.mark.parametrize('mutation,reason', [('port', 'invalid SDP m-line port'), ('close', 'session missing close'),
    ('count', 'calibration needs 500 intervals'), ('coverage', 'insufficient successful-send coverage'),
    ('median', 'median outside frame cadence'), ('none', 'UPLINK_INTERVALS_NONE'),
    ('identity', 'recorded bot identity differs'), ('colour', 'bot log must be uncoloured')])
def test_offline_recheck_keeps_failure_reasons(recorded_run, mutation, reason):
    run = recorded_run; value = run.capture
    if mutation == 'port': value.records['sdp'][4] = 'm=application nope UDP/DTLS/SCTP webrtc-datachannel'
    elif mutation == 'count': value.intervals[0] = cadence(count=499)
    elif mutation == 'coverage': value.intervals[0]['end_us'] += 2000000
    elif mutation == 'median': value.intervals[0] = cadence(step_us=10000)
    elif mutation == 'identity': smoke.write(run.path/'identity.json', dict(IDENTITY, bot_instance='different-recorded-bot'))
    run.save(value, close=mutation != 'close')
    if mutation == 'none':
        with (run.path/'main-serial.log').open('a') as stream: stream.write('UPLINK_INTERVALS_NONE\n')
        # Unavailable evidence must be within the captured session.
        text = (run.path/'main-serial.log').read_text().replace('PHASE closed\nUPLINK_INTERVALS_NONE', 'UPLINK_INTERVALS_NONE\nPHASE closed')
        (run.path/'main-serial.log').write_text(text)
    elif mutation == 'colour':
        with (run.path/'main-bot.log').open('a') as stream: stream.write('\x1b[31mcoloured\n')
    result = smoke.recheck_run(run.path)
    assert not result['checks_passed'] and reason in result['error']
    assert json.loads((run.path/'result.json').read_text()) == run.original


def test_fixed_bound_recheck_requires_the_fixed_bounds(recorded_run):
    run = recorded_run; run.original['calibration'] = False; smoke.write(run.path/'result.json', run.original)
    pending = run.path/'pending-bounds.json'
    smoke.write(pending, dict(schema=1, status='pending', variants={}))
    result = smoke.recheck_run(run.path, bounds_path=pending)
    assert not result['checks_passed'] and 'bounds pending coordinator measurement' in result['error']
    bounds = run.path/'measured.json'
    smoke.write(bounds, dict(schema=1, status='measured', variants=dict(normal=dict(pairs=3, coverage=.95, stall_us=60000, p99_delta_us=2000))))
    result = smoke.recheck_run(run.path, run.path/'result-recheck-fixed.json', bounds_path=bounds)
    assert result['checks_passed'] and result['bounds_sha256'] == bundle.digest(bounds)
    assert result['qualification'] is False


def test_recheck_cli_is_offline_and_returns_check_status(recorded_run, capsys):
    assert smoke.main(['--recheck', str(recorded_run.path)]) == 0
    assert json.loads(capsys.readouterr().out)['checks_passed']


def test_demo_recheck_requires_all_three_closed_session_logs(recorded_run):
    run = recorded_run; run.original.update(variant='demo', mode='demo')
    smoke.write(run.path/'result.json', run.original)
    result = smoke.recheck_run(run.path)
    assert not result['checks_passed'] and 'resume-serial.log' in result['error']
    for label in ('resume', 'cold'):
        for suffix in ('-serial.log', '-bot.log'):
            (run.path/(label+suffix)).write_bytes((run.path/('main'+suffix)).read_bytes())
    result = smoke.recheck_run(run.path, run.path/'result-recheck-complete.json')
    assert result['checks_passed'] and [s['label'] for s in result['sessions']] == ['main', 'resume', 'cold']


def test_reference_only_recheck_does_not_invent_wire_evidence(recorded_run):
    run = recorded_run; run.original['reference_only'] = True; smoke.write(run.path/'result.json', run.original)
    result = smoke.recheck_run(run.path)
    assert not result['checks_passed'] and 'reference-only run has no candidate wire evidence' in result['error']


@pytest.mark.parametrize('ending', ['session-end', 'CancelFrame', 'EndFrame'])
def test_session_end_rejects_real_turn_five_tail(ending):
    fixture = json.loads((Path(__file__).parent/'fixtures/board_smoke_session_lost_turn5.json').read_text())
    rows = fixture['events']
    alive = [e for e in rows if e['kind'] != 'session-end' and e.get('frame') != 'CancelFrame']
    assert smoke.turn_checks(alive, 'long', False)['passed']
    assert smoke.turn_finished(alive, 1)
    end = next(e for e in rows if e['kind'] == 'session-end') if ending == 'session-end' else dict(
        next(e for e in rows if e['kind'] == 'frame' and e.get('frame') == 'CancelFrame'), frame=ending)
    for check in (lambda: smoke.turn_checks(alive+[end], 'long', False), lambda: smoke.turn_finished(alive+[end], 1)):
        with pytest.raises(ContractError, match='bot session ended during turn'): check()
    with pytest.raises(ContractError, match='bot session ended during turn'):
        smoke.turn_checks(rows, 'long', False)


@pytest.mark.parametrize('frame', ['CancelFrame', 'EndFrame'])
def test_session_end_ignores_upstream_and_server_copies(frame):
    rows = events()
    rows += [dict(kind='frame', frame=frame, direction='UPSTREAM'), dict(kind='server-frame', frame=frame, direction='DOWNSTREAM')]
    assert smoke.turn_checks(rows, 'long', False)['passed']
    assert smoke.turn_finished(rows, 1)


@pytest.fixture
def session_poll(monkeypatch):
    class Board:
        def __init__(self): self.rows = []; self.reads = []; self.command = Mock()
        @property
        def cursor(self): return self.rows[-1][0] if self.rows else 10
        def lines_since(self, cursor):
            self.reads.append(cursor)
            return [row for row in self.rows if row[0] > cursor]
    def setup():
        io = object.__new__(smoke.LiveIO)
        io.pid = 123; io.identity = dict(bot_instance='same-bot'); io.active = True; io.stopping = False
        io.session_gen = 1; io.serial_seen = 10; io.pc_id = 'peer-one'; io.events = lambda: (None, [])
        monkeypatch.setattr(smoke, 'process_token', lambda _: 'same-bot')
        return io, Board()
    return setup


@pytest.mark.parametrize('line', ['SESSION FAIL gen=1 reason=remote disconnected', 'PEER_DEF: Peer disconnected',
    'RTP: Got RTCP_BYE', 'STATE Disconnected gen=3', 'STATE Starting gen=3', 'STATE Idle gen=3'])
def test_session_lost_serial_witnesses_fail_immediately(session_poll, line):
    io, board = session_poll(); board.rows.append((11, 0, line))
    with pytest.raises(ContractError, match='session lost: '+line): io.poll(board)
    assert io.serial_seen == 11


def test_session_serial_cursor_advances_without_skipping_new_lines(session_poll):
    io, board = session_poll()
    board.rows.append((11, 0, 'STATE Idle gen=1'))
    io.poll(board); io.poll(board)
    assert io.serial_seen == 11 and board.reads == [10, 11]
    board.rows.append((12, 0, 'STATE Starting gen=3'))
    with pytest.raises(ContractError, match='session lost'): io.poll(board)
    assert board.reads == [10, 11, 11]


@pytest.mark.parametrize('lost', [False, True])
def test_session_begin_records_ready_generation_and_exact_cursor(tmp_path, session_poll, lost):
    io, board = session_poll()
    io.out = tmp_path; (tmp_path/'serial.log').touch(); (tmp_path/'bot.log').touch()
    io.args = SimpleNamespace(bot_dir=tmp_path, headless=True); io.clock = lambda: 0
    io.identity['profile_manifest'] = dict(profile='demo')
    io.snapshot = lambda _: dict(pc_id='new-peer')
    io.events = lambda: (None, [dict(kind='stt-profile', pc_id='new-peer', monotonic_ns=1, profile='demo')])
    io.stopping = True; io.session_gen = 7
    def ready(*args, **kwargs):
        board.rows.append((11, 0, 'STATE Ready gen=1'))
        if lost: board.rows.append((12, 0, 'STATE Starting gen=3'))
        return board.rows[0]
    board.wait_for_entry = ready
    if lost:
        with pytest.raises(ContractError, match='session lost'): io.begin(board, 'manual')
    else:
        io.begin(board, 'manual')
        assert io.pc_id == 'new-peer' and io.serial_seen == 11
    assert io.session_gen == 1 and io.stopping is False


@pytest.mark.parametrize('kind,ended_at', [('talkover', 102.5), ('talkover', 104), ('talkover', 107.5),
                                         ('long', 109), ('long', 120), ('long', 99)])
def test_session_end_mid_wait_stops_without_more_playback(live_turn, session_poll, kind, ended_at):
    run = live_turn(kind=kind)
    guard, board = session_poll()
    for key in ('pid', 'identity', 'active', 'stopping', 'session_gen', 'serial_seen', 'pc_id'):
        setattr(run.io, key, getattr(guard, key))
    run.io.poll = smoke.LiveIO.poll.__get__(run.io)
    run.rows.append(dict(kind='session-end', monotonic_ns=int(ended_at*1e9)))
    with pytest.raises(ContractError, match='bot session ended during turn'):
        for index in (1, 2): run.io.turn(board, kind, index)
    assert run.clock.now == pytest.approx(max(100, ended_at), abs=.03)
    expected = 0 if ended_at < 100 else 2 if kind == 'talkover' and ended_at > 105.6 else 1
    assert len(run.played) == expected
    assert not (run.io.out/'turn-02.json').exists()


@pytest.mark.parametrize('hold', [False, True])
def test_session_own_finish_stop_is_exempt(tmp_path, session_poll, hold):
    io, board = session_poll()
    io.out = tmp_path; io.args = SimpleNamespace(bot_dir=tmp_path, reference_only=False)
    io.interval_requested = False; io.serial_at = io.bot_at = 0
    (tmp_path/'serial.log').write_text('PHASE closed\n'); (tmp_path/'bot.log').touch()
    ended = []
    io.events = lambda: (None, ended)
    def command(text, *args):
        if text == ('demo hold' if hold else 'disconnect'):
            assert io.stopping
            board.rows.extend([(11, 0, 'SESSION FAIL gen=1 reason=local stop'), (12, 0, 'STATE Idle gen=2')])
            ended.append(dict(kind='session-end'))
            io.poll(board)
    board.command.side_effect = command
    board.wait_for = Mock(return_value='PHASE closed')
    io.finish(board, 'main', hold=hold)
    io.poll(board)
    assert io.active is False and io.stopping and io.serial_seen == 12
    assert [c.args[0] for c in board.command.call_args_list] == ['uplink-intervals', 'demo hold' if hold else 'disconnect']


def test_session_begin_rejects_ended_recorder(tmp_path, session_poll):
    io, board = session_poll()
    io.out = tmp_path; (tmp_path/'serial.log').touch(); (tmp_path/'bot.log').touch()
    io.args = SimpleNamespace(bot_dir=tmp_path, headless=True); io.clock = lambda: 0
    io.identity['profile_manifest'] = dict(profile='demo')
    board.wait_for_entry = Mock(return_value=(11, 0, 'STATE Ready gen=1'))
    io.snapshot = lambda _: dict(pc_id='new-peer')
    io.events = lambda: (None, [dict(kind='stt-profile', pc_id='new-peer', monotonic_ns=1, profile='demo'),
                               dict(kind='session-end', monotonic_ns=2)])
    with pytest.raises(ContractError, match='bot session ended during turn'):
        io.begin(board, 'manual')


def content_fixture(name):
    fixture = json.loads((Path(__file__).parent/'fixtures/board_smoke_bot_content.json').read_text())
    return next(case['assistant'] for case in fixture['cases'] if case['name'] == name)


def content_reply(rows, text):
    # For interrupted turns replace only the final reply, preserving the count
    # prefix and the actual Italy user/start/interruption evidence.
    next(e for e in reversed(rows) if e['kind'] == 'aggregator-assistant-turn-stopped')['text'] = text


@pytest.mark.parametrize('case,kind,aec,finding', [
    ('france_missing_paris', 'france', False, 'missing_paris'),
    ('france_off_script', 'france', True, 'missing_paris'),
    ('france_negated_paris', 'france', True, 'off_script_reply'),
    ('italy_missing_rome', 'talkover', True, 'missing_rome')])
def test_content_real_off_script_answers_are_report_only_in_calibration(case, kind, aec, finding):
    rows = events(kind, aec); content_reply(rows, content_fixture(case))
    verdict = smoke.turn_checks(rows, kind, aec, calibration=True)
    assert verdict['passed'] and finding in {f['kind'] for f in verdict['bot_content_findings']}
    assert any(content_fixture(case) in f['assistants'] for f in verdict['bot_content_findings'])


@pytest.mark.parametrize('kind,aec', [('long', False), ('talkover', False)])
def test_content_off_script_count_does_not_replace_clean_speech_requirement(kind, aec):
    rows = events(kind, aec); content_reply(rows, 'A Halloween cat has other stories to tell.')
    verdict = smoke.turn_checks(rows, kind, aec, calibration=True)
    assert verdict['passed'] and verdict['bot_content_findings'][0]['kind'] == 'off_script_reply'
    rows = [e for e in rows if e.get('frame') != 'BotStoppedSpeakingFrame']
    with pytest.raises(ContractError, match='missing bot speech completion'):
        smoke.turn_checks(rows, kind, aec, calibration=True)


@pytest.mark.parametrize('kind,aec,text,reason', [
    ('france', False, 'france_missing_paris', 'France/Paris turn failed'),
    ('talkover', True, 'italy_missing_rome', 'AEC must hear Italy and answer Rome')])
def test_content_qualification_still_requires_paris_and_rome(kind, aec, text, reason):
    rows = events(kind, aec); content_reply(rows, content_fixture(text))
    with pytest.raises(ContractError, match=reason): smoke.turn_checks(rows, kind, aec)
    with pytest.raises(ContractError, match=reason): smoke.turn_checks(rows, kind, aec, calibration=False)


def test_content_negation_does_not_change_existing_qualification_token_rule():
    rows = events('france'); content_reply(rows, content_fixture('france_negated_paris'))
    verdict = smoke.turn_checks(rows, 'france', False, calibration=False)
    assert verdict['passed'] and 'bot_content_findings' not in verdict


@pytest.mark.parametrize('case,kind,aec,reason', [
    ('france-transcript', 'france', False, 'France prompt not transcribed'),
    ('count-transcript', 'long', False, 'count prompt not transcribed'),
    ('count-transcript', 'talkover', True, 'count prompt not transcribed'),
    ('italy-transcript', 'talkover', True, 'AEC Italy prompt not transcribed'),
    ('aec-start', 'talkover', True, 'AEC talk-over must interrupt during bot speech'),
    ('aec-active', 'talkover', True, 'AEC talk-over must interrupt during bot speech'),
    ('aec-interruption', 'talkover', True, 'AEC talk-over must interrupt during bot speech'),
    ('off-start', 'talkover', False, 'self-interruption or unexpected user turn'),
    ('off-italy', 'talkover', False, 'AEC-off must ignore talk-over'),
    ('quiet-start', 'long', False, 'self-interruption or unexpected user turn'),
    ('quiet-interruption', 'long', True, 'self-interruption or unexpected user turn'),
    ('start', 'france', False, 'missing bot speech witness'),
    ('stop', 'long', False, 'missing bot speech completion'),
    ('assistant', 'france', True, 'missing bot turn evidence'),
    ('active-witness', 'long', True, 'missing bot-active event witness'),
    ('error', 'long', False, 'bot pipeline error'),
    ('fatal', 'long', True, 'bot pipeline error'),
    ('session-end', 'long', True, 'bot session ended during turn'),
    ('CancelFrame', 'long', False, 'bot session ended during turn'),
    ('EndFrame', 'france', True, 'bot session ended during turn')])
def test_calibration_content_never_softens_device_or_session_checks(case, kind, aec, reason):
    rows = events(kind, aec)
    if case in ('france-transcript', 'count-transcript'):
        next(e for e in rows if e['kind'] == 'aggregator-user-turn-stopped')['text'] = 'What is the capital of Spain?'
    elif case == 'italy-transcript':
        [e for e in rows if e['kind'] == 'aggregator-user-turn-stopped'][-1]['text'] = 'What about Spain?'
    elif case == 'aec-start':
        rows = [e for e in rows if not (e.get('frame') == 'UserStartedSpeakingFrame' and e.get('bot_active'))]
    elif case == 'aec-active':
        for e in rows:
            if e.get('frame') == 'UserStartedSpeakingFrame': e['bot_active'] = False
    elif case == 'aec-interruption': rows = [e for e in rows if e.get('frame') != 'InterruptionFrame']
    elif case in ('off-start', 'quiet-start', 'quiet-interruption'):
        rows.append(dict(kind='frame', direction='DOWNSTREAM', bot_active=True,
                         frame='InterruptionFrame' if case == 'quiet-interruption' else 'UserStartedSpeakingFrame'))
    elif case == 'off-italy': rows.append(dict(kind='aggregator-user-turn-stopped', text='What about Italy?'))
    elif case in ('start', 'stop'):
        rows = [e for e in rows if e.get('frame') != ('BotStartedSpeakingFrame' if case == 'start' else 'BotStoppedSpeakingFrame')]
    elif case == 'assistant': rows = [e for e in rows if e['kind'] != 'aggregator-assistant-turn-stopped']
    elif case == 'active-witness': rows[0].pop('bot_active')
    elif case in ('error', 'fatal', 'CancelFrame', 'EndFrame'):
        rows.append(dict(kind='frame', direction='DOWNSTREAM', frame={'error':'ErrorFrame','fatal':'FatalErrorFrame'}.get(case, case)))
    else: rows.append(dict(kind='session-end'))
    with pytest.raises(ContractError, match=reason): smoke.turn_checks(rows, kind, aec, calibration=True)


@pytest.mark.parametrize('case', ['france_missing_paris', 'france_negated_paris'])
def test_content_findings_are_written_to_turn_json(live_turn, case):
    run = live_turn(kind='france', calibration=True); content_reply(run.rows, content_fixture(case))
    verdict = run.io.turn(object(), 'france', 1)
    assert verdict['passed'] and verdict['bot_content_findings']
    assert json.loads((run.io.out/'turn-01.json').read_text())['bot_content_findings'] == verdict['bot_content_findings']
    assert run.recordings[0].seconds == 15
    assert run.clock.now == pytest.approx(115, abs=.03)  # Early replies already have their settle inside capture.


def test_calibration_content_recording_failure_remains_fatal(live_turn):
    run = live_turn(kind='france', calibration=True, exit_code=2)
    content_reply(run.rows, content_fixture('france_missing_paris'))
    with pytest.raises(RuntimeError, match='arecord exited 2'):
        run.io.turn(object(), 'france', 1)
    assert run.recordings[0].seconds == 15 and len(run.played) == 1


def test_calibration_content_late_quiet_start_remains_fatal(live_turn):
    run = live_turn(kind='long', calibration=True)
    run.rows.append(dict(kind='frame', frame='UserStartedSpeakingFrame', direction='DOWNSTREAM',
                         bot_active=False, monotonic_ns=128_000_000_000))
    with pytest.raises(ContractError, match='self-interruption or unexpected user turn'):
        run.io.turn(object(), 'long', 1)
    assert run.clock.now == pytest.approx(130, abs=.03)


@pytest.mark.parametrize('kind,aec,seconds', [('france', False, 15), ('long', False, 30),
                                          ('talkover', False, 30), ('talkover', True, 30)])
def test_calibration_content_long_reply_waits_cleanly_without_extending_recording(live_turn, kind, aec, seconds):
    run = live_turn(kind=kind, aec=aec, calibration=True); template = copy.deepcopy(run.rows)
    last_user = max(e['monotonic_ns'] for e in run.rows if e['kind'] == 'aggregator-user-turn-stopped')
    for e in run.rows:
        if e['monotonic_ns'] > last_user and (e.get('frame') == 'BotStoppedSpeakingFrame' or e['kind'] == 'aggregator-assistant-turn-stopped'):
            e['monotonic_ns'] = int((100+seconds+4+(.01 if e['kind'] == 'aggregator-assistant-turn-stopped' else 0))*1e9)
    first = run.io.turn(object(), kind, 1)
    assert first['passed'] and any(f['kind'] == 'reply_exceeded_recording_window' for f in first['bot_content_findings'])
    clean_stop = next(f['observed_clean_stop_monotonic_ns']/1e9 for f in first['bot_content_findings']
                      if f['kind'] == 'reply_exceeded_recording_window')
    assert clean_stop == pytest.approx(100+seconds+4.01, abs=.03)
    assert run.clock.now >= clean_stop+2 and run.clock.now == pytest.approx(100+seconds+6.01, abs=.06)
    assert any(label == 'bot reply completion safety cap' and budget == 60 for label, budget, _ in run.waits)
    first_end = run.clock.now; played = len(run.played)
    for e in template: e['monotonic_ns'] += int((first_end-100)*1e9)
    run.rows.extend(template)
    assert run.io.turn(object(), kind, 2)['passed']
    assert run.played[played][1] >= clean_stop+2
    assert [r.seconds for r in run.recordings] == [seconds, seconds]
    for r in run.recordings:
        r.process.wait.assert_called_once_with(timeout=seconds+5)
        r.process.send_signal.assert_not_called(); r.process.terminate.assert_not_called(); r.process.kill.assert_not_called()


def test_calibration_content_long_reply_safety_cap_still_fails(live_turn):
    run = live_turn(kind='long', calibration=True)
    run.rows[:] = [e for e in run.rows if e.get('frame') != 'BotStoppedSpeakingFrame' and e['kind'] != 'aggregator-assistant-turn-stopped']
    with pytest.raises(TimeoutError, match='bot reply completion safety cap exceeded 60 seconds'):
        for index in (1, 2): run.io.turn(object(), 'long', index)
    assert run.clock.now == pytest.approx(190, abs=.03) and len(run.played) == 1
    assert run.recordings[0].seconds == 30
    run.recordings[0].process.wait.assert_called_once()


def test_calibration_content_reply_must_start_inside_recording(live_turn):
    run = live_turn(kind='france', calibration=True)
    for e in run.rows:
        if e.get('frame') in ('BotStartedSpeakingFrame', 'BotStoppedSpeakingFrame') or e['kind'] == 'aggregator-assistant-turn-stopped':
            e['monotonic_ns'] += 20_000_000_000
    with pytest.raises(ContractError, match='missing bot speech witness within recording window'):
        run.io.turn(object(), 'france', 1)
    assert run.clock.now == pytest.approx(115, abs=.03)


def test_calibration_content_user_turn_must_stop_inside_recording(live_turn):
    run = live_turn(kind='france', calibration=True)
    next(e for e in run.rows if e['kind'] == 'aggregator-user-turn-stopped')['monotonic_ns'] = 116_000_000_000
    with pytest.raises(ContractError, match='missing bot turn evidence within recording window'):
        run.io.turn(object(), 'france', 1)
    assert run.clock.now == pytest.approx(115, abs=.03) and len(run.played) == 1
    run.recordings[0].process.wait.assert_called_once_with(timeout=20)


def test_calibration_content_session_loss_during_long_reply_is_fatal(live_turn):
    run = live_turn(kind='long', calibration=True)
    run.rows[:] = [e for e in run.rows if e.get('frame') != 'BotStoppedSpeakingFrame' and e['kind'] != 'aggregator-assistant-turn-stopped']
    run.rows.append(dict(kind='session-end', monotonic_ns=132_000_000_000))
    with pytest.raises(ContractError, match='bot session ended during turn'):
        for index in (1, 2): run.io.turn(object(), 'long', index)
    assert run.clock.now == pytest.approx(132, abs=.03) and len(run.played) == 1


def test_calibration_content_device_loss_during_long_reply_is_fatal(live_turn, session_poll):
    run = live_turn(kind='long', calibration=True); identity_io, board = session_poll()
    for field in ('pid', 'identity', 'active', 'stopping', 'session_gen', 'serial_seen', 'pc_id'):
        setattr(run.io, field, getattr(identity_io, field))
    run.rows[:] = [e for e in run.rows if e.get('frame') != 'BotStoppedSpeakingFrame' and e['kind'] != 'aggregator-assistant-turn-stopped']
    def poll(link):
        if run.clock.now >= 132 and not link.rows:
            link.rows.append((11, 0, 'SESSION FAIL gen=1 reason=remote disconnected'))
        smoke.LiveIO.poll(run.io, link)
    run.io.poll = poll
    with pytest.raises(ContractError, match='session lost: SESSION FAIL gen=1 reason=remote disconnected'):
        for index in (1, 2): run.io.turn(board, 'long', index)
    assert run.clock.now == pytest.approx(132, abs=.03) and len(run.played) == 1


@pytest.mark.parametrize('kind,seconds', [('france', 15), ('long', 30)])
def test_calibration_content_completion_inside_last_two_seconds_is_not_overlong(live_turn, kind, seconds):
    run = live_turn(kind=kind, calibration=True); template = copy.deepcopy(run.rows)
    for e in run.rows:
        if e.get('frame') == 'BotStoppedSpeakingFrame' or e['kind'] == 'aggregator-assistant-turn-stopped':
            e['monotonic_ns'] = int((100+seconds-1+(.01 if e['kind'] == 'aggregator-assistant-turn-stopped' else 0))*1e9)
    verdict = run.io.turn(object(), kind, 1)
    assert verdict['passed'] and not verdict['bot_content_findings']
    assert not any(label == 'bot reply completion safety cap' for label, _, _ in run.waits)
    assert run.clock.now == pytest.approx(100+seconds+2, abs=.06)
    assert run.recordings[0].seconds == seconds
    settled_at = run.clock.now; played = len(run.played)
    for e in template: e['monotonic_ns'] += int((settled_at-100)*1e9)
    run.rows.extend(template)
    assert run.io.turn(object(), kind, 2)['passed']
    assert run.played[played][1] >= 100+seconds+2
    assert [r.seconds for r in run.recordings] == [seconds, seconds]


@pytest.mark.parametrize('drained', [False, True])
def test_calibration_content_session_end_during_late_settle_is_fatal(live_turn, session_poll, drained):
    run = live_turn(kind='france', calibration=True); identity_io, board = session_poll()
    for field in ('pid', 'identity', 'active', 'stopping', 'session_gen', 'serial_seen', 'pc_id'):
        setattr(run.io, field, getattr(identity_io, field))
    run.io.poll = lambda link: smoke.LiveIO.poll(run.io, link)
    stopped_at = 119 if drained else 114
    for e in run.rows:
        if e.get('frame') == 'BotStoppedSpeakingFrame' or e['kind'] == 'aggregator-assistant-turn-stopped':
            e['monotonic_ns'] = int((stopped_at+(.01 if e['kind'] == 'aggregator-assistant-turn-stopped' else 0))*1e9)
    lost_at = stopped_at+1 if drained else 116
    run.rows.append(dict(kind='session-end', monotonic_ns=int(lost_at*1e9)))
    with pytest.raises(ContractError, match='bot session ended during turn'):
        for index in (1, 2): run.io.turn(board, 'france', index)
    assert run.clock.now == pytest.approx(lost_at, abs=.03) and len(run.played) == 1
    assert run.recordings[0].seconds == 15


@pytest.mark.parametrize('aec,failure,reason', [(False, None, None), (True, None, None),
    (False, 'stack', 'peer-loop free stack'), (True, 'none', 'UPLINK_INTERVALS_NONE'),
    (False, 'wire', 'need exactly one closed session')])
def test_calibration_content_findings_reach_result_without_weakening_other_gates(tmp_path, aec, failure, reason):
    root, manifest = image(tmp_path, aec=aec); args = options(tmp_path, root)
    class OffscriptIO(FakeIO):
        def turn(self, board, kind, index):
            self.calls.append(kind); self.now += 1
            rows = events(kind, self.manifest['aec_enabled'])
            if kind == 'france': content_reply(rows, content_fixture('france_missing_paris'))
            if kind == 'talkover' and aec: content_reply(rows, content_fixture('italy_missing_rome'))
            return dict(smoke.turn_checks(rows, kind, aec, calibration=self.args.calibration), kind=kind)
        def finish(self, *args, **kw):
            lines, bot = super().finish(*args, **kw)
            if failure == 'wire': lines = [line for line in lines if 'WIRE_CONTRACT start=' not in line]
            return lines, bot
    result = smoke.execute(args, backend=OffscriptIO(args, manifest, failure))
    findings = result['bot_content_findings']; assert findings
    assert 'missing_paris' in {f['kind'] for f in findings}
    if aec: assert 'missing_rome' in {f['kind'] for f in findings}
    assert all(f['session'] == 'main' and f['turn'] in (1, 2) for f in findings)
    assert json.loads((args.out/'result.json').read_text())['bot_content_findings'] == findings
    if reason: assert not result['acquisition_passed'] and reason in result['error']
    else: assert result['acquisition_passed'] and not result['passed']
