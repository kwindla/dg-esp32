"""Step 10 requal-n1 r2 (Astra r1 #1-#5, Fable F1-F5): host-only fakes; no device, bot, lock, ALSA or network."""
import builtins
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
from types import SimpleNamespace

import pytest

import e2e_common
import echoear_aec_bench
import echoear_image_profile as profile
import echoear_item1_combined as item1
import echoear_item2_campaign as item2
import step9a_echoear
from echoear_requalification import acceptance_verdict, load_plan, PLAN
from esp_serial import ROOT

N1_PLAN = PLAN.with_name('echoear-item2-requalification-n1.json')
BUILD = (ROOT/'build').resolve()


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    saved = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    yield
    for n, h in saved.items(): signal.signal(n, h)


@pytest.fixture
def no_generic_build(monkeypatch):
    """Tripwire: any open/read of ROOT/build (generic ELF or generated config) fails the test."""
    touched = []
    def guard(path):
        try: p = Path(path).resolve()
        except TypeError: return
        if p == BUILD or BUILD in p.parents:
            touched.append(str(p)); raise AssertionError('generic build access: '+str(p))
    real_open = builtins.open
    def opener(file, *a, **k):
        guard(file); return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, 'open', opener)
    for name in ('read_text', 'read_bytes', 'open'):
        real = getattr(Path, name)
        def wrapped(self, *a, _real=real, **k):
            guard(self); return _real(self, *a, **k)
        monkeypatch.setattr(Path, name, wrapped)
    return touched


def boot_lines(elf_hash, *, demo=False, mic1=0x1a, mic3=False):
    lines = ['I (100) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+elf_hash,
             'I (2062) echoear-audio: startup verified REG0D=0x01 REG44=0x58 MIC1_GAIN=0x%02x EXPECTED_REG44=0x58 no_dac_ref=0' % mic1,
             'I (2063) echoear-audio: register readback ok',
             'READY transport-console']
    if demo: lines.append('I (5704) dg-esp32: STACK task=demo-boot free_bytes=15124 priority=2')
    if mic3: lines.append('I (2100) echoear-aec: MIC3_GAIN=0x15 analog_db=15')
    return lines


class FakeBoard:
    def __init__(self, lines): self.lines = []; self.script = list(lines); self.commands = []; self.display_cleanup = False
    @property
    def cursor(self): return len(self.lines)
    def _emit(self, line): self.lines.append((len(self.lines), float(len(self.lines)), line))
    def reset(self, *a, **k):
        for line in self.script: self._emit(line)
    def lines_since(self, cursor): return self.lines[cursor:]
    def wait_for(self, pattern, timeout=0, since=0):
        for _, _, line in self.lines[since:]:
            if re.search(pattern, line): return line
        raise TimeoutError(pattern)
    def command(self, text, timeout=None): self.commands.append(text); return 'OK'


# ---- 1. strict turn setup uses the bound external ELF and never the generic build ------------------------------

def test_check_firmware_with_bound_elf_reads_no_generic_build(tmp_path, no_generic_build):
    elf = tmp_path/'external.elf'; elf.write_bytes(b'\x7fELF...echoear-audio...')
    want = hashlib.sha256(elf.read_bytes()).hexdigest()
    board = FakeBoard(boot_lines(want)); board.reset()
    assert step9a_echoear.check_firmware(board, 0, elf=elf, profile='normal') == want
    with pytest.raises(RuntimeError, match='codec register readback'):
        step9a_echoear.check_firmware(FakeBoard([]), 0, elf=elf)
    other = FakeBoard(boot_lines('0'*64)); other.reset()
    with pytest.raises(RuntimeError, match='Board ELF differs'):step9a_echoear.check_firmware(other, 0, elf=elf)
    demo = FakeBoard(boot_lines(want, demo=True)); demo.reset()
    with pytest.raises(RuntimeError, match='public profile'):step9a_echoear.check_firmware(demo, 0, elf=elf, profile='normal')
    aec = FakeBoard(boot_lines(want, mic3=True)); aec.reset()
    with pytest.raises(RuntimeError, match='public profile'):step9a_echoear.check_firmware(aec, 0, elf=elf, profile='normal')
    bad = tmp_path/'other.elf'; bad.write_bytes(b'not the board image')
    with pytest.raises(RuntimeError, match='not an EchoEar'):step9a_echoear.check_firmware(board, 0, elf=bad)
    assert not no_generic_build


def test_real_strict_turn_setup_with_external_elf_and_no_generic_build(tmp_path, monkeypatch, no_generic_build):
    """Runs the real e2e_common.turn() through reset, identity, codec, strict check_firmware and server-url, and
    stops at the display setup it would perform next. Nothing in turn() itself is stubbed."""
    import alsa, host_nic, failure_snapshot, step9a_support
    elf = tmp_path/'bundle'/'dg-esp32.elf'; elf.parent.mkdir(); elf.write_bytes(b'ELF echoear-audio frozen bundle')
    want = hashlib.sha256(elf.read_bytes()).hexdigest()
    monkeypatch.setenv('ECHOEAR_STRICT_MERGE', '1'); monkeypatch.setenv('DG_BOARD', 'echoear')
    monkeypatch.delenv('ECHOEAR_ACOUSTIC_QUALIFICATION', raising=False)
    def room(out, arm):
        Path(out).mkdir(parents=True); return dict(baseline_dbfs=-60.0, noise_contaminated=False, wav=str(Path(out)/'room.wav'))
    monkeypatch.setattr(alsa, 'record_room_baseline', room)
    monkeypatch.setattr(e2e_common, 'resolve_jabra', lambda: ('0', 'jabra'))
    monkeypatch.setattr(e2e_common, 'set_volumes', lambda card, path: None)
    monkeypatch.setattr(host_nic, 'snapshot', lambda: dict(monotonic_s=0.0, interfaces={}))
    monkeypatch.setattr(host_nic, 'gate', lambda a, b: dict(passed=True))
    monkeypatch.setattr(failure_snapshot, 'capture_failure', lambda board, out, why: dict(reason=why))
    class StopAtDisplay(Exception): pass
    def display_setup(board, **k): raise StopAtDisplay('display setup reached')
    monkeypatch.setattr(step9a_support, 'setup', display_setup)
    board = FakeBoard(boot_lines(want)); board.e2e_perf_windows = []
    result = e2e_common.turn(board, None, 'jabra', tmp_path/'run1', True, elf=elf, server_url='http://host:7861',
                             display='cat', image_profile='normal')
    assert result['boot_elf_sha256'] == want and result['codec_boot']['passed']
    assert result['error'] == 'StopAtDisplay: display setup reached'
    assert 'server-url http://host:7861' in board.commands and 'playback-volume 0' in board.commands
    assert not no_generic_build


# ---- 2. restore-of-record before unlock --------------------------------------------------------------------------

class Trace(list):
    pass


@pytest.fixture
def fake_restore(monkeypatch):
    trace = Trace(); state = dict(fail=0)
    def flash(image, expected, out, label, expected_gain_db=None):
        assert expected == echoear_aec_bench.NORMAL_HASH and image == echoear_aec_bench.NORMAL
        trace.append('restore-flash:'+label)
        if state['fail']: state['fail'] -= 1; raise RuntimeError('esptool failed')
    class Board:
        def close(self): trace.append('restore-serial-close')
    def boot(out, label, expected):
        assert expected == '55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'   # gap 3 restore-of-record
        trace.append('restore-boot-verified:'+label); return Board()
    monkeypatch.setattr(echoear_aec_bench, 'flash', flash); monkeypatch.setattr(echoear_aec_bench, 'boot', boot)
    @contextlib.contextmanager
    def bench():
        trace.append('lock-enter')
        try: yield
        finally: trace.append('lock-exit')
    monkeypatch.setattr(step9a_echoear, 'bench', bench)
    monkeypatch.setattr(step9a_echoear, 'check_esptool', lambda: None)
    return trace, state


def test_restore_constants_are_the_gap3_restore_of_record():
    """Gap 3 (after the acceptance r20 PASS of record): the bench restores the qualified AEC-off normal image in place;
    the 9936da45 image remains only as the named historical constant."""
    assert echoear_aec_bench.NORMAL_HASH == '55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'
    assert echoear_aec_bench.NORMAL.parts[-2:] == ('requal-n1-bundles', 'normal')
    assert echoear_aec_bench.HISTORICAL_NORMAL_9936DA45_HASH.startswith('9936da45')
    import echoear_rollback
    assert echoear_rollback.NORMAL is echoear_aec_bench.NORMAL and echoear_rollback.NORMAL_HASH is echoear_aec_bench.NORMAL_HASH


@pytest.mark.parametrize('outcome', ['success', 'failure', 'interrupt', 'untouched'])
def test_restore_hold_restores_before_unlock(tmp_path, fake_restore, outcome):
    trace, _ = fake_restore
    hold = None
    with contextlib.suppress(RuntimeError, KeyboardInterrupt):
        with step9a_echoear.bench():
            with item2.RestoreHold(tmp_path, wait=lambda: pytest.fail('no retry expected')) as hold:
                if outcome != 'untouched': hold.touch(); trace.append('flash-candidate')
                if outcome == 'failure': raise RuntimeError('acquisition failed')
                if outcome == 'interrupt': raise KeyboardInterrupt()
                trace.append('acquisition')
    if outcome == 'untouched':
        assert trace == ['lock-enter', 'acquisition', 'lock-exit'] and hold.record == dict(device_untouched=True) and hold.clean
        return
    assert trace[-1] == 'lock-exit'
    assert trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
    assert trace.index('flash-candidate') < trace.index('restore-flash:restore-1')
    assert hold.clean and hold.record['restored_elf_sha256'].startswith('55152f25')
    assert json.loads((tmp_path/'restore.json').read_text())['clean'] is True


def test_sigterm_during_hold_becomes_interrupt_and_still_restores(tmp_path, fake_restore):
    trace, _ = fake_restore
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        with step9a_echoear.bench():
            with item2.RestoreHold(tmp_path, wait=lambda: None) as hold:
                hold.touch(); os.kill(os.getpid(), signal.SIGTERM)
    assert trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
    assert signal.getsignal(signal.SIGTERM) is not previous      # hold handler kept until the verdict is written
    hold.release_signals(); assert signal.getsignal(signal.SIGTERM) is previous


def test_failed_restore_is_a_failed_hold_and_keeps_the_lock_until_retry(tmp_path, fake_restore):
    trace, state = fake_restore; state['fail'] = 1; waits = []
    def wait():
        waits.append(trace.copy()); assert 'lock-exit' not in trace   # lock retained while waiting for SIGUSR1
    with step9a_echoear.bench():
        with item2.RestoreHold(tmp_path, wait=wait) as hold:
            hold.touch()
    assert len(waits) == 1 and not hold.clean and hold.record['clean'] is False
    assert [a['error'] is None for a in hold.record['attempts']] == [False, True]
    assert trace.index('restore-boot-verified:restore-2') < trace.index('lock-exit')


def fake_item2_environment(tmp_path, monkeypatch, *, stage, run_result=None, body_error=None, real_gates=False, serial_text='', plan_path=None):
    image = tmp_path/'normal'; image.mkdir()
    binding = tmp_path/'smoke'/'binding.json'; binding.parent.mkdir()
    binding.write_text(json.dumps(dict(counted=False, purpose='smoke', revision='test')))
    plan = json.loads(N1_PLAN.read_text())
    monkeypatch.setattr(item2, 'verified_binding', lambda path: (dict(policy_path=str(plan_path or N1_PLAN), budget='b', a6_plan='a',
        stress_bank='s', analog_bank='an', final_item1_passed=False, output_root=str(binding.parent)), image))   # r14: bound --out
    import echoear_jabra_rebaseline, host_nic, step9a_assets, step9a_support
    monkeypatch.setattr(echoear_jabra_rebaseline, 'load', lambda p: dict(target_dbfs=plan['strict_gates']['jabra_reply_target_dbfs']))
    monkeypatch.setattr(item2, 'flash_image', lambda image, out: (fake_item2_environment.trace.append('flash-candidate'), 'port')[1])
    monkeypatch.setattr(host_nic, 'snapshot', lambda: dict(monotonic_s=0.0))
    monkeypatch.setattr(host_nic, 'gate', lambda a, b: dict(passed=True, elapsed_s=30.0))   # a 30 s eligibility window
    monkeypatch.setattr(step9a_assets, 'asset_server', contextlib.nullcontext)
    monkeypatch.setattr(step9a_support, 'host_address', lambda: '127.0.0.1')
    def run(args, live):
        fake_item2_environment.trace.append('acquisition')
        if body_error: raise body_error
        args.out.mkdir(parents=True); (args.out/'serial.log').write_text(serial_text)
        rr = run_result(args) if callable(run_result) else run_result
        (args.out/'result.json').write_text(json.dumps(rr or dict(passed=True, runs=[], reruns=[])))
        return 2 if (rr or {}).get('deferred') else 0
    monkeypatch.setattr(e2e_common, 'run', run)
    if not real_gates:
        import echoear_requalification
        monkeypatch.setattr(echoear_requalification, 'observation_gate', lambda *a: dict(observations_passed=True, checks={}))
    return binding


@pytest.mark.parametrize('case', ['success', 'acquisition-exception', 'interrupt'])
def test_item2_execute_restores_before_unlock(tmp_path, monkeypatch, fake_restore, case):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    error = dict(success=None, **{'acquisition-exception': RuntimeError('boom'), 'interrupt': KeyboardInterrupt()})[case]
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline', body_error=error)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True)); monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'attempt-1'
    if error is None:
        item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: pytest.fail('no retry'))
        assert json.loads((out/'result.json').read_text())['restore']['clean'] is True
    else:
        with pytest.raises(type(error)):
            item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: pytest.fail('no retry'))
    assert trace[0] == 'lock-enter' and trace[-1] == 'lock-exit'
    assert trace.index('flash-candidate') < trace.index('acquisition') < trace.index('restore-flash:restore-1')
    assert trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
    assert json.loads((out.with_name('attempt-1-identity')/'restore.json').read_text())['clean'] is True


def test_item2_failed_restore_fails_the_stage(tmp_path, monkeypatch, fake_restore):
    trace, state = fake_restore; state['fail'] = 1; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True)); monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'attempt-1'
    assert item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None) == 1
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is False and r['restore_failed'] is True and r['restore']['clean'] is False


def item1_binding(monkeypatch, tmp_path):
    """item1 now requires --binding (r11); a fake verified binding naming the demo image the test passes."""
    path = tmp_path/'binding-item1.json'
    if not path.exists():
        path.write_text(json.dumps(dict(counted=False, purpose='smoke', revision='test', demo_elf_sha256=item1.ELF, demo_image=str((tmp_path/'demo').resolve()),
                                        output_root=str(tmp_path))))   # r14: item1 --out is <output_root>/item1/attempt-<n>
    monkeypatch.setattr(item2, 'verified_binding', lambda p: (json.loads(Path(p).read_text()), tmp_path/'normal'))
    return path


def test_item1_partial_setup_and_rf_failure_restore_semantics(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore
    monkeypatch.setattr(item1, 'EVIDENCE', tmp_path)
    monkeypatch.setattr(item1, 'validate_image', lambda image: (dict(commit=item1.CANDIDATE), dict(write_flash_args=[], flash_files={})))
    monkeypatch.setattr(item1, 'rf_pre_gate', lambda snapshot, gate: dict(passed=fake_item1.rf))
    monkeypatch.setattr(step9a_echoear, 'verify', lambda out: (trace.append('mac-verify'), 'port')[1])
    monkeypatch.setattr(step9a_echoear, 'esptool_python', lambda: 'python')
    import alsa, step7_support, bot_restart
    monkeypatch.setattr(item1, 'subprocess', SimpleNamespace(run=lambda *a, **k: trace.append('flash-candidate'),
        STDOUT=-2, check_output=lambda *a, **k: 'e'*40+'\n'))
    monkeypatch.setattr(alsa, 'resolve_jabra', lambda: ('0', 'jabra'))
    monkeypatch.setattr(alsa, 'set_volumes', lambda card, path: None)
    monkeypatch.setattr(step7_support, 'prepare_prompt', lambda out: 'prompt')
    class Bot:
        def __init__(self, out): pass
        def __enter__(self): trace.append('bot-start'); raise RuntimeError('bot failed to start')
        def __exit__(self, *a): return False
    monkeypatch.setattr(bot_restart, 'DemoBot', Bot)
    class fake_item1: rf = True
    assert item1.main(['--binding', str(item1_binding(monkeypatch, tmp_path)), '--image', str(tmp_path/'demo'), '--out', str(tmp_path/'item1'/'attempt-1')], restore_wait=lambda: pytest.fail('no retry')) == 1
    r = json.loads((tmp_path/'item1'/'attempt-1'/'result.json').read_text())
    assert 'bot failed to start' in r['error'] and r['restore']['clean'] is True
    assert r['audio_capability_lines'] == []        # Astra r3 #6: present on every exit (no serial captured here)
    assert trace.index('flash-candidate') < trace.index('restore-flash:restore-1') < trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
    trace.clear(); fake_item1.rf = False
    assert item1.main(['--binding', str(item1_binding(monkeypatch, tmp_path)), '--image', str(tmp_path/'demo'), '--out', str(tmp_path/'item1'/'attempt-2')], restore_wait=lambda: pytest.fail('no retry')) == 1
    r = json.loads((tmp_path/'item1'/'attempt-2'/'result.json').read_text())
    assert r['deferred'] is True and r['scored'] is False and r['restore'] == dict(device_untouched=True)
    assert trace == ['lock-enter', 'lock-exit']


# ---- 4. device-reported public profile ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent/'fixtures'/'requal_n1_profile'


def perf(volume=0, *, aec=None, demo_task=False, session=True):
    row = dict(tasks=[dict(name='audio-tx')] + ([dict(name='demo-boot')] if demo_task else []))
    if session: row.update(pc_id='peer', output_volume_db=volume)
    if aec is not None: row['aec'] = dict(enabled=aec)
    return 'PERF '+json.dumps(row)


def vol(db): return 'I (5) dg-audio: OUTPUT_VOLUME relative_db=%d max_relative_db=0 default_relative_db=-6' % db


def session(*, demo=False, capability='AUDIO_CAPABILITY aec=0 ref_gain_db=0 guard_ms=0', gain='MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=125',
            limiter='MIC_PROTECTION schema=1 knee=8192 ceiling=16384 limited_samples=0 adc_clips=0', perf_rows=None, tail=()):
    lines = boot_lines('a'*64, demo=demo) + [perf(session=False, demo_task=demo), vol(0), 'I (9) dg-app: '+capability,
                                           'I (10) echoear-audio: '+gain, 'I (11) dg-audio: '+limiter]
    if demo: lines.append('I (82) demo-boot: RESTORED assets=16 no_http=1 connect_intent=1')
    lines += perf_rows if perf_rows is not None else [perf(demo_task=demo), perf(demo_task=demo)]
    return lines + [vol(-6), perf(-6, demo_task=demo)] + list(tail)   # intentional cleanup and the disconnect PERF


def test_public_profile_normal_and_demo_pass_on_device_reports():
    assert profile.session_gate(session(), 'normal', require_cleanup=True)['passed']
    assert profile.session_gate(session(demo=True), 'demo')['passed']
    assert not profile.session_gate(session(demo=True), 'normal')['passed']
    assert not profile.session_gate(session(), 'demo')['passed']


@pytest.mark.parametrize('change,check', [
    (dict(capability='AUDIO_CAPABILITY aec=1 ref_gain_db=15 guard_ms=400'), 'aec_capability'),
    (dict(capability='no capability line'), 'aec_capability'),
    (dict(perf_rows=[perf(aec=True)]), 'perf_aec_absent'),
    (dict(perf_rows=[perf(aec=False)]), None),
    (dict(perf_rows=[]), None),
    (dict(gain='MIC_GAIN_PATH analog_db=24 reg=0x18 digital_percent=125'), 'gain_path'),
    (dict(gain='MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=100'), 'gain_path'),
    (dict(gain='none'), 'gain_path'),
    (dict(limiter='MIC_PROTECTION schema=1 knee=4096 ceiling=16384 limited_samples=0 adc_clips=0'), 'limiter'),
    (dict(limiter='none'), 'limiter'),
    # A session PERF disagreeing with the device-reported volume state (0 dB reported, -6 measured) fails.
    (dict(perf_rows=[perf(), perf(-6)]), 'volume_state_consistent'),
    (dict(perf_rows=[perf(3)]), 'volume_state_consistent'),
])
def test_public_profile_fails_closed(change, check):
    verdict = profile.session_gate(session(**change), 'normal', require_cleanup=True)
    if check is None: assert verdict['passed']
    else: assert not verdict['passed'] and verdict['checks'][check] is False


@pytest.mark.parametrize('bad', [
    # Astra r2 #2 probes: a valid session followed by each malformed / missing record must FAIL.
    'I (99) dg-app: AUDIO_CAPABILITY aec=? ref_gain_db=0 guard_ms=0',
    'I (99) echoear-audio: MIC_GAIN_PATH analog_db=? reg=0x1a digital_percent=125',
    'I (99) dg-audio: MIC_PROTECTION schema=? knee=8192 ceiling=16384 limited_samples=0 adc_clips=0',
    'I (99) echoear-audio: startup verified REG0D=0x01 REG44=0x58 MIC1_GAIN=0x? EXPECTED_REG44=0x58 no_dac_ref=0',
    'PERF not-json',
    'PERF {"tasks":[]}',                                       # a pre-session row after Start = missing fields
    'PERF {"pc_id":"peer","tasks":[]}',                        # session row without output_volume_db
    'PERF {"output_volume_db":-6,"tasks":[]}',                 # volume without a session
    'PERF {"pc_id":"peer","output_volume_db":-6,"output_volume_db":0}',   # duplicated key
    'PERF ["not","an","object"]',
    'I (99) dg-app: AUDIO_CAPABILITY aec=0 ref_gain_db=0 guard_ms=0 aec=1',   # conflicting trailing field
    'I (99) dg-app: AUDIO_CAPABILITY aec=0 ref_gain_db=0 guard_ms=0 AUDIO_CAPABILITY aec=1 ref_gain_db=0 guard_ms=0',
    'I (99) dg-audio: OUTPUT_VOLUME relative_db=x max_relative_db=0 default_relative_db=-6',
])
def test_profile_rejects_mixed_valid_and_malformed_evidence(bad):
    assert profile.session_gate(session(), 'normal', require_cleanup=True)['passed']
    assert not profile.session_gate(session(tail=[bad]), 'normal', require_cleanup=True)['passed']


def test_profile_requires_intentional_cleanup_after_measurement_setup():
    lines = session()
    no_cleanup = [l for l in lines if 'OUTPUT_VOLUME relative_db=-6' not in l][:-1]   # drop the cleanup report and its PERF
    assert profile.session_gate(no_cleanup, 'normal')['passed']
    assert profile.session_gate(no_cleanup, 'normal', require_cleanup=True)['checks']['cleanup_restored'] is False
    odd = session(tail=[vol(-3), perf(-3)])
    assert profile.session_gate(odd, 'normal')['checks']['volume_reports_intentional'] is False


def fixture_lines(name): return (FIXTURES/name).read_text().splitlines()


def test_replay_retained_normal_logs_fail_only_on_the_then_missing_capability_line():
    """Real retained serial excerpts (hil/fixtures/requal_n1_profile/manifest.json), each ending with the
    harness's -6 dB cleanup and the disconnect PERF. 9bc2d42 predates AUDIO_CAPABILITY, so that check alone fails
    there; the 3eaa629a mute-03 log (masked-equal to the new normal image) passes every check."""
    manifest = json.loads((FIXTURES/'manifest.json').read_text())
    strict = fixture_lines('strict-9bc2d42-attempt-2.serial.txt')
    perf_rows = [l for l in strict if l.startswith('PERF')]
    assert json.loads(perf_rows[-1][5:])['output_volume_db'] == -6        # the disconnect PERF after cleanup
    r = profile.session_gate(strict, 'normal', require_cleanup=True)
    assert [k for k, v in r['checks'].items() if not v] == ['aec_capability'] and r['counts']['session_perf'] == 13
    hot = fixture_lines('hot-stt-9bc2d42-native-27.serial.txt') + fixture_lines('hot-stt-9bc2d42-native-39.serial.txt')
    r = profile.session_gate(hot, 'normal', require_cleanup=True)
    assert [k for k, v in r['checks'].items() if not v] == ['aec_capability']
    mute = fixture_lines('mute-03.serial.txt')
    r = profile.session_gate(mute, 'normal')
    assert r['passed'] and r['counts']['capability'] >= 1 and -6 in r['counts']['volume_reports'] and 0 in r['counts']['volume_reports']
    assert all(v['source_sha256'] for k, v in manifest.items() if k.endswith('.serial.txt'))


def test_replay_mutated_retained_log_fails_on_a_minus_6_measurement_row():
    strict = fixture_lines('strict-9bc2d42-attempt-2.serial.txt')
    i = next(n for n, l in enumerate(strict) if l.startswith('PERF') and json.loads(l[5:]).get('output_volume_db') == 0)
    row = json.loads(strict[i][5:]); row['output_volume_db'] = -6
    mutated = strict[:i]+['PERF '+json.dumps(row)]+strict[i+1:]
    assert profile.session_gate(mutated, 'normal', require_cleanup=True)['checks']['volume_state_consistent'] is False


def test_measurement_volume_is_scoped_to_scored_records_on_retained_results():
    strict = json.loads((FIXTURES/'strict-9bc2d42-attempt-2.result-excerpt.json').read_text())
    v = profile.measurement_volume_gate(strict, 'strict_voice_cat')
    assert v['passed'] and v['records'] == 12
    bad = json.loads(json.dumps(strict)); bad['runs'][1]['after_perf']['output_volume_db'] = -6
    assert not profile.measurement_volume_gate(bad, 'strict_voice_cat')['passed']
    gone = json.loads(json.dumps(strict)); del gone['runs'][0]['reply_window_transport']['before']['output_volume_db']
    assert not profile.measurement_volume_gate(gone, 'strict_voice_cat')['passed']
    unscored = json.loads(json.dumps(strict)); unscored['runs'][2]['scored'] = False; unscored['runs'][2]['after_perf']['output_volume_db'] = -6
    assert profile.measurement_volume_gate(unscored, 'strict_voice_cat')['passed']   # setup-failure rows carry no expectation
    assert not profile.measurement_volume_gate(dict(runs=[]), 'quiet_offline')['passed']
    hot = json.loads((FIXTURES/'hot-stt-9bc2d42.result-excerpt.json').read_text())
    h = profile.measurement_volume_gate(hot, 'a6_hot_stt')
    assert h['passed'] and h['applicable'] is False and h['observed'] == [-6, -6, -6, -6]
    assert not profile.measurement_volume_gate(dict(points=[dict(before=dict(output_volume_db=0), after=dict(output_volume_db=-6))]), 'a6_echo_stress')['passed']


def test_demo_capability_expectation_is_grounded_in_firmware_source():
    app = (ROOT/'main/app.cpp').read_text(); boot = (ROOT/'main/demo_boot.cpp').read_text()
    pipeline = (ROOT/'components/dg_audio/src/pipeline.cpp').read_text()
    cap = (ROOT/'components/dg_audio/include/dg_audio/capability.h').read_text()
    assert 'app.request_connect()' in boot and 'App::request_connect() {impl_->command(EventKind::Connect);}' in app
    start = app.index('} else if(a.kind==ActionKind::Start) {')
    assert 'app_log("AUDIO_CAPABILITY aec=%d ref_gain_db=%u guard_ms=%u"' in app[start:start+1200]
    body = pipeline[pipeline.index('Capability Pipeline::capability() const {'):]
    body = body[:body.index('\n}')]
    assert '#if CONFIG_DG_ECHOEAR_AEC' in body and body.strip().endswith('return {};')
    assert 'struct Capability {bool aec=false;unsigned ref_gain_db=0,guard_ms=0;};' in cap


def test_profile_module_never_reads_generated_config():
    text = Path(profile.__file__).read_text()
    assert 'sdkconfig' not in text.replace('generated configuration', '').replace('generated build configuration', '') and "'build" not in text


# ---- 5. D6: analog report-only and quiet_live continuation -------------------------------------------------------

def run_row(index, *, scored=True, passed=True, pc='p1', warm=None):
    return dict(run=index, scored=scored, passed=passed and scored, pc_id=pc if scored else None,
                temperature=warm if warm is not None else ('cold' if index == 1 else 'warm'))


def seg(rows): return dict(runs=rows, reruns=[], passed=all(r['passed'] for r in rows))


def test_quiet_live_single_complete_segment():
    a = item2.aggregate_quiet_live([seg([run_row(i) for i in range(1, 6)])])
    assert a['passed'] and a['complete'] and a['next_original_slot'] is None


def test_quiet_live_setup_failure_is_unscored_and_filled_by_continuation():
    first = seg([run_row(1), run_row(2, scored=False)])
    planned = item2.aggregate_quiet_live([first])
    assert not planned['passed'] and planned['next_original_slot'] == 2 and planned['slots'] == [1]
    cont = seg([run_row(2, pc='p2', warm='cold')]+[run_row(i, pc='p2') for i in (3, 4, 5)])
    done = item2.aggregate_quiet_live([first, cont])
    assert done['passed'] and done['slots'] == [1, 2, 3, 4, 5] and done['segment_slots'] == [[1], [2, 3, 4, 5]]


def test_quiet_live_original_fail_is_preserved_and_not_continuable():
    first = seg([run_row(1, passed=False), run_row(2), run_row(3), run_row(4), run_row(5)])
    a = item2.aggregate_quiet_live([first])
    assert not a['passed'] and a['original_failures'] == [1] and a['next_original_slot'] is None
    failed_then_setup = seg([run_row(1, passed=False), run_row(2, scored=False)])
    b = item2.aggregate_quiet_live([failed_then_setup])
    assert b['next_original_slot'] == 2
    c = item2.aggregate_quiet_live([failed_then_setup, seg([run_row(2, pc='p2', warm='cold')]+[run_row(i, pc='p2') for i in (3, 4, 5)])])
    assert c['complete'] and not c['passed'] and c['original_failures'] == [1]


@pytest.mark.parametrize('segments,problem', [
    ([seg([run_row(1), run_row(2)]), seg([run_row(3, pc='p2', warm='cold')])], 'without an unscored'),
    ([seg([run_row(1), run_row(2, scored=False)]), seg([run_row(1, pc='p2', warm='cold')])], 'do not continue'),
    ([seg([run_row(1, scored=False), run_row(2)])], 'before its end'),
    ([seg([run_row(1), run_row(2, pc='p9')])], 'not one peer'),
    ([seg([run_row(1), run_row(2, warm='cold')])], 'non-warm'),
    ([dict(seg([run_row(1)]), reruns=[run_row(1)])], 'reruns'),
    ([dict(seg([run_row(1)]), deferred=True)], 'deferral'),
])
def test_quiet_live_continuation_rejects_replacement_and_broken_coverage(segments, problem):
    a = item2.aggregate_quiet_live(segments)
    assert not a['passed'] and any(problem in p for p in a['problems']) and a['next_original_slot'] is None


def test_quiet_live_segment_budget_is_finite():
    s1 = seg([run_row(1), run_row(2, scored=False)])
    s2 = seg([run_row(2, pc='p2', warm='cold'), run_row(3, scored=False)])
    assert item2.aggregate_quiet_live([s1, s2])['next_original_slot'] == 3
    s3 = seg([run_row(3, pc='p3', warm='cold'), run_row(4, scored=False)])
    assert item2.aggregate_quiet_live([s1, s2, s3])['next_original_slot'] is None


def segment_of(binding, record, where):
    """r13: a quiet_live segment as execute() finalises it under `binding` (provenance, stage, image); r14: at `where`."""
    return dict(record, stage='quiet_live', normal_elf_sha256=item2.NORMAL, out_path=str(Path(where).resolve()),
                **item2.binding_provenance(binding))


def test_continuation_only_for_quiet_live_from_the_same_stage_root(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    with pytest.raises(RuntimeError, match='Only quiet_live'):
        item2.execute(binding, 'quiet_offline', binding.parent/'quiet_offline'/'attempt-2', continue_from=[binding.parent/'quiet_offline'/'attempt-1'])
    prior = binding.parent/'quiet_live'/'attempt-1'; prior.mkdir(parents=True)
    (prior/'result.json').write_text(json.dumps(segment_of(binding, seg([run_row(i) for i in range(1, 6)]), prior)))
    with pytest.raises(RuntimeError, match='No continuable'):
        item2.execute(binding, 'quiet_live', prior.with_name('attempt-2'), continue_from=[prior])
    assert 'lock-enter' not in trace


def test_quiet_live_continuation_runs_only_the_unscored_slots(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    prior_rows = [run_row(1), run_row(2, scored=False)]
    cont_rows = [run_row(2, pc='p2', warm='cold')]+[run_row(i, pc='p2') for i in (3, 4, 5)]
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_live', run_result=seg(cont_rows))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True)); monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    seen = {}
    real_run = e2e_common.run
    def spy(args, live): seen['start'] = args.original_start; return real_run(args, live)
    monkeypatch.setattr(e2e_common, 'run', spy)
    monkeypatch.setattr(item2, 'segment_observation', lambda plan, seg, text, current=False, **k: dict(passed=True))   # real gate: tests below
    prior = binding.parent/'quiet_live'/'attempt-1'; prior.mkdir(parents=True)
    (prior/'result.json').write_text(json.dumps(segment_of(binding, seg(prior_rows), prior))); (prior/'serial.log').write_text('')
    out = prior.with_name('attempt-2')
    assert item2.execute(binding, 'quiet_live', out, continue_from=[prior], restore_wait=lambda: None) == 0
    r = json.loads((out/'result.json').read_text())
    assert seen['start'] == 2 and r['quiet_live_aggregate']['passed'] and r['quiet_live_aggregate']['slots'] == [1, 2, 3, 4, 5]
    assert r['continued_from'][0]['result_sha256'] == hashlib.sha256((prior/'result.json').read_bytes()).hexdigest()


def test_analog_probe_report_only_keeps_measurement_and_exits_on_acquisition_validity(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='a6_analog_probe')
    import echoear_echo_stress, echoear_analog_probe
    measured = dict(passed=False, investigation_required=True, points=[1, 2])
    def stress(out, bank, budget, plan, calibrate_prompt, preflight):
        out.mkdir(parents=True); (out/'serial.log').write_text(''); return dict(points=[1, 2], passed=True)
    monkeypatch.setattr(echoear_echo_stress, 'run', stress)
    monkeypatch.setattr(echoear_analog_probe, 'analyze', lambda out, bank: dict(measured))
    pre = lambda *a: None; pre.observations = []
    monkeypatch.setattr(item2, 'a6_preflight', lambda plan, stage, journal: pre)
    monkeypatch.setattr(item2, 'a6_resources', lambda out, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True)); monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'a6_analog_probe'/'attempt-1'
    assert item2.execute(binding, 'a6_analog_probe', out, restore_wait=lambda: None) == 0
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is False and r['investigation_required'] is True and r['report_only'] and r['acquisition_valid']
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=False))
    # r14: a fresh stage root now needs its own binding (output_root); the no-replay rule still holds.
    (tmp_path/'second').mkdir(); second = fake_item2_environment(tmp_path/'second', monkeypatch, stage='a6_analog_probe')
    other = second.parent/'a6_analog_probe'/'attempt-1'
    assert item2.execute(second, 'a6_analog_probe', other, restore_wait=lambda: None) == 1
    r = json.loads((other/'result.json').read_text())
    assert r['acquisition_valid'] is False and r['investigation_required'] is True


PROV = dict(release_binding='b.json', binding_sha256='0'*64, counted=True, purpose='acceptance')
FINAL = dict(PROV, kind='item2-stage', exit_code=0, restore=dict(clean=True), normal_elf_sha256='55152f25')
ITEM1 = dict(PROV, kind='item1-combined', passed=True, exit_code=0, restore=dict(clean=True), candidate='16c45a3', elf_sha256='5bbd868a',
             predeclared={}, checks={}, autonomous_turns=[], cycles=[])


def final(stage, **fields): return dict(FINAL, stage=stage, **fields)


def test_acceptance_verdict_excludes_report_only_measurement():
    plan = load_plan(N1_PLAN)
    ok = {s['id']: final(s['id'], passed=True) for s in plan['order']}
    ok['a6_analog_probe'] = final('a6_analog_probe', passed=False, investigation_required=True, acquisition_valid=True)
    ok['quiet_live'] = final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True))
    v = acceptance_verdict(plan, ITEM1, ok)
    assert v['passed'] and v['stages']['a6_analog_probe'] == dict(counted=False, report_only=True, passed=True, measurement_passed=False, investigation_required=True)
    assert not acceptance_verdict(plan, dict(ITEM1, passed=False), ok)['passed']
    bad = dict(ok, a6_analog_probe=final('a6_analog_probe', passed=True, acquisition_valid=False))
    assert not acceptance_verdict(plan, ITEM1, bad)['passed']
    assert not acceptance_verdict(plan, ITEM1, dict(ok, quiet_live=final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=False))))['passed']
    assert not acceptance_verdict(plan, ITEM1, dict(ok, a6_hot_stt=final('a6_hot_stt', passed=False)))['passed']
    missing = dict(ok); del missing['a6_echo_stress']
    assert not acceptance_verdict(plan, ITEM1, missing)['passed']


@pytest.mark.parametrize('change', [dict(scoring_failed=True), dict(interrupted=True), dict(restore_failed=True),
    dict(restore=dict(clean=False)), dict(restore=None), dict(exit_code=None), dict(exit_code=1), dict(stage=None),
    dict(release_binding=None), dict(normal_elf_sha256=None)])
def test_acceptance_loader_rejects_non_final_or_failed_records(change):
    """Fable r8 N2: scoring_failed/interrupted/restore_failed, an unclean restore, a missing exit code or a missing
    final-record field never count, even with passed true."""
    from echoear_requalification import final_record_problems
    plan = load_plan(N1_PLAN)
    ok = {s['id']: final(s['id'], passed=True) for s in plan['order']}
    ok['a6_analog_probe'] = final('a6_analog_probe', passed=False, acquisition_valid=True)
    ok['quiet_live'] = final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True))
    assert acceptance_verdict(plan, ITEM1, ok)['passed']
    for stage in ('a6_hot_stt', 'strict_voice_cat', 'a6_analog_probe'):
        record = {k: v for k, v in dict(ok[stage], **change).items() if v is not None}
        v = acceptance_verdict(plan, ITEM1, dict(ok, **{stage: record}))
        assert not v['passed'] and v['stages'][stage]['passed'] is False
    if set(change) <= {'scoring_failed', 'interrupted', 'restore_failed', 'restore', 'exit_code'}:
        item1 = {k: v for k, v in dict(ITEM1, **change).items() if v is not None}
        assert not acceptance_verdict(plan, item1, ok)['passed']
    assert final_record_problems(dict(ITEM1), item1=True) == []


# ---- r3: D6 nested gate on the real observation gate (Astra r2 #3) ------------------------------------------------

@pytest.fixture(scope='module')
def rehearsed_quiet_live(tmp_path_factory):
    """A healthy five-original quiet_live cohort produced by the existing host-only rehearsal (real turn()/run()
    bookkeeping, synthetic device), exactly as the dry run builds it."""
    from echoear_requalification_dry_run import rehearse
    out = tmp_path_factory.mktemp('rehearsal')/'dry'; out.mkdir()
    rehearse(load_plan(N1_PLAN), out)
    root = out/'quiet_live'
    return json.loads((root/'result.json').read_text()), (root/'serial.log').read_text()


def as_continuation(row):
    return dict(row, temperature='cold')    # a continuation is a new session


def test_real_gate_full_cohort_passes(rehearsed_quiet_live):
    result, text = rehearsed_quiet_live
    plan = load_plan(N1_PLAN)
    from echoear_requalification import observation_gate
    assert observation_gate(plan, 'quiet_live', result, text)['observations_passed']
    a = item2.aggregate_quiet_live([result], 5, plan, [text])
    assert a['passed'] and a['segment_observations'][0]['passed']


def test_real_gate_one_plus_four_continuation_passes(rehearsed_quiet_live):
    result, text = rehearsed_quiet_live
    plan = load_plan(N1_PLAN); runs = result['runs']
    setup_failure = dict(run=2, scored=False, passed=False, error='synthetic setup failure before the scored prompt')
    first = dict(result, runs=[runs[0], setup_failure], restore=dict(clean=True), stage_nic=dict(passed=True),
                 image_profile=dict(passed=True), measurement_volume=dict(passed=True))
    second = dict(result, runs=[as_continuation(runs[1])]+runs[2:])
    a = item2.aggregate_quiet_live([first, second], 5, plan, [text, text])
    assert a['complete'] and a['slots'] == [1, 2, 3, 4, 5] and not a['problems']
    for o in a['segment_observations']:
        assert o['passed'], o['required']
        assert 'source.five_originals' not in o['required'] and 'exact_originals' not in o['required']
        assert any(k.startswith('source.run') for k in o['required'])     # per-original acoustic checks kept
    assert a['passed']


def test_real_gate_zero_scored_setup_failure_then_five_passes(rehearsed_quiet_live):
    result, text = rehearsed_quiet_live
    plan = load_plan(N1_PLAN)
    zero = dict(reruns=[], passed=False, runs=[dict(run=1, scored=False, passed=False, error='setup failure')],
                restore=dict(clean=True), stage_nic=dict(passed=True))
    zero_text = 'STACK task=app-control free_bytes=9000 priority=4\n'
    second = dict(result, runs=[as_continuation(result['runs'][0])]+result['runs'][1:])
    a = item2.aggregate_quiet_live([zero, second], 5, plan, [zero_text, text])
    zero_obs = a['segment_observations'][0]
    assert zero_obs['scored'] == 0 and set(zero_obs['required']) == {'raw_monitor', 'raw_stack', 'no_retakes', 'crash_free', 'hold.restore', 'hold.stage_nic'}
    assert zero_obs['passed'] and a['passed'] and a['slots'] == [1, 2, 3, 4, 5]
    # Resource validation of a zero-scored segment is real: a dirty restore or raw-monitor failure fails it.
    dirty = dict(zero, restore=dict(clean=False))
    assert not item2.aggregate_quiet_live([dirty, second], 5, plan, [zero_text, text])['passed']
    bad_text = zero_text + 'E (1) dg-app: SESSION FAIL boom\nGuru Meditation Error\n'
    assert not item2.aggregate_quiet_live([zero, second], 5, plan, [bad_text, text])['segment_observations'][0]['passed']


def test_real_gate_keeps_per_original_failures_and_the_aggregate_count(rehearsed_quiet_live):
    result, text = rehearsed_quiet_live
    plan = load_plan(N1_PLAN); runs = result['runs']
    noisy = dict(runs[3], room_baseline=dict(runs[3].get('room_baseline', {}), noise_contaminated=True))
    second = dict(result, runs=[as_continuation(runs[1]), runs[2], noisy, runs[4]])
    first = dict(result, runs=[runs[0], dict(run=2, scored=False, passed=False)], restore=dict(clean=True),
                 stage_nic=dict(passed=True), image_profile=dict(passed=True), measurement_volume=dict(passed=True))
    a = item2.aggregate_quiet_live([first, second], 5, plan, [text, text])
    assert not a['passed'] and a['segment_observations'][1]['required']['source.run3_quiet'] is False
    short = dict(result, runs=[as_continuation(runs[1]), runs[2], runs[3]])
    b = item2.aggregate_quiet_live([first, short], 5, plan, [text, text])
    assert not b['complete'] and not b['passed']                      # five unique originals required, once
    prior_profile = dict(first, image_profile=dict(passed=False))
    clean_second = dict(result, runs=[as_continuation(runs[1])]+runs[2:])
    assert item2.aggregate_quiet_live([first, clean_second], 5, plan, [text, text])['passed']
    assert not item2.aggregate_quiet_live([prior_profile, clean_second], 5, plan, [text, text])['passed']   # prior hold profile required


# ---- r3: a signal mid-restore never replaces the exception in flight (Fable r2 N1) -------------------------------

def test_signal_during_restore_keeps_the_original_exception_and_records_it(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore
    real_flash = echoear_aec_bench.flash
    def flash_and_signal(*a, **k):
        os.kill(os.getpid(), signal.SIGINT); return real_flash(*a, **k)     # pending: SIGINT is blocked here
    monkeypatch.setattr(echoear_aec_bench, 'flash', flash_and_signal)
    with pytest.raises(RuntimeError, match='acquisition failed'):
        with step9a_echoear.bench():
            with item2.RestoreHold(tmp_path, wait=lambda: None) as hold:
                hold.touch(); raise RuntimeError('acquisition failed')
    assert hold.record['signals_during_restore'] == ['SIGINT'] and hold.clean
    assert trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
    # Without an exception in flight the signal is only recorded (Fable r3 N3, r4 N3): nothing is raised.
    trace.clear()
    with step9a_echoear.bench():
        with item2.RestoreHold(tmp_path, wait=lambda: None) as hold:
            hold.touch()
    assert hold.pending_signals == ['SIGINT'] and trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')


def test_item2_writes_result_json_on_exception_and_mid_restore_signal(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline', body_error=RuntimeError('boom'))
    real_flash = echoear_aec_bench.flash
    def flash_and_signal(*a, **k):
        os.kill(os.getpid(), signal.SIGTERM); return real_flash(*a, **k)
    monkeypatch.setattr(echoear_aec_bench, 'flash', flash_and_signal)
    out = binding.parent/'quiet_offline'/'attempt-1'
    with pytest.raises(RuntimeError, match='boom'):
        item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None)
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is False and r['error'] == 'RuntimeError: boom' and r['interrupted'] is False
    assert r['restore']['clean'] is True and r['restore']['signals_during_restore'] == ['SIGTERM']


# ---- r4 -------------------------------------------------------------------------------------------------------

STANDALONE_PANIC = ["Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
                    'Backtrace: 0x40000000:0x3f000000']
HEALTHY_STACK = 'STACK task=app-control free_bytes=9000 priority=4'


@pytest.mark.parametrize('line', STANDALONE_PANIC + [
    'abort() was called at PC 0x40385c7f on core 0', 'assert failed: xQueueGenericSend queue.c:832',
    'E (99) task_wdt: Task watchdog got triggered.', 'Brownout detector was triggered',
    'rst:0xc (RTC_SW_CPU_RST),boot:0xb (SPI_FAST_FLASH_BOOT)', 'rst:0x7 (TG0WDT_SYS_RST),boot:0xb (SPI_FAST_FLASH_BOOT)'])
def test_raw_monitor_rejects_standalone_crash_evidence(line):
    from step9a_r2_checks import audit
    healthy = (HEALTHY_STACK+'\n').encode()
    assert audit(healthy)['passed']
    with pytest.raises(RuntimeError, match='Strict non-display stop rule') as stop:
        audit((HEALTHY_STACK+'\n'+line+'\n').encode())
    if not line.startswith('E ('):assert "'component': 'crash'" in str(stop.value)   # standalone: no E-level companion
    assert audit((HEALTHY_STACK+'\n'+line+'\n').encode(), diagnostic=True)['passed'] is False


def test_expected_boot_reset_reasons_are_not_crashes():
    from step9a_r2_checks import audit, crash_lines
    boots = ['rst:0x15 (USB_UART_CHIP_RESET),boot:0xb (SPI_FAST_FLASH_BOOT)', 'rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)']
    assert crash_lines('\n'.join(boots)) == [] and audit(('\n'.join([HEALTHY_STACK]+boots)+'\n').encode())['passed']
    assert crash_lines('rst:0x10 (RTCWDT_RTC_RST),boot:0xb') != []


def test_zero_scored_segment_with_standalone_panic_fails(rehearsed_quiet_live):
    """Astra r3 #4 isolated regression: standalone Guru line + healthy STACK + unscored slot + clean restore."""
    result, text = rehearsed_quiet_live
    plan = load_plan(N1_PLAN)
    zero = dict(reruns=[], passed=False, runs=[dict(run=1, scored=False, passed=False, error='setup failure')],
                restore=dict(clean=True), stage_nic=dict(passed=True))
    second = dict(result, runs=[as_continuation(result['runs'][0])]+result['runs'][1:])
    clean_text = HEALTHY_STACK+'\n'
    assert item2.aggregate_quiet_live([zero, second], 5, plan, [clean_text, text])['passed']
    crashed = item2.aggregate_quiet_live([zero, second], 5, plan, ['\n'.join([HEALTHY_STACK]+STANDALONE_PANIC)+'\n', text])
    obs = crashed['segment_observations'][0]
    assert not crashed['passed'] and obs['required']['crash_free'] is False and obs['crash_lines']
    # A scored segment is held to the same crash rule.
    crashed_scored = item2.aggregate_quiet_live([second], 5, plan, [text+'\n'.join(STANDALONE_PANIC)+'\n'])
    assert not crashed_scored['passed'] and crashed_scored['segment_observations'][0]['required']['crash_free'] is False


@pytest.mark.parametrize('panic', [False, True])
def test_continuation_through_execute_rejects_a_panicked_zero_scored_prior(tmp_path, monkeypatch, fake_restore, rehearsed_quiet_live, panic):
    """Real execute() with the real observation, nested acoustic, segment and aggregate gates; only acquisition,
    lock, flash, NIC and the device profile/volume gates are faked."""
    result, text = rehearsed_quiet_live
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    cont = dict(result, runs=[dict(r, run=r['run']) for r in [as_continuation(result['runs'][0])]+result['runs'][1:]])
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_live', run_result=cont, real_gates=True, serial_text=text)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    prior = binding.parent/'quiet_live'/'attempt-1'; prior.mkdir(parents=True)
    zero = dict(reruns=[], passed=False, runs=[dict(run=1, scored=False, passed=False, error='setup failure')],
                restore=dict(clean=True), stage_nic=dict(passed=True), image_profile=dict(passed=False))
    (prior/'result.json').write_text(json.dumps(segment_of(binding, zero, prior)))
    (prior/'serial.log').write_text('\n'.join([HEALTHY_STACK]+(STANDALONE_PANIC if panic else []))+'\n')
    code = item2.execute(binding, 'quiet_live', prior.with_name('attempt-2'), continue_from=[prior], restore_wait=lambda: None)
    r = json.loads((prior.with_name('attempt-2')/'result.json').read_text())
    assert r['quiet_live_aggregate']['complete']
    if panic:
        assert code == 1 and r['passed'] is False and r['quiet_live_aggregate']['segment_observations'][0]['required']['crash_free'] is False
    else:
        assert code == 0 and r['passed'] is True


def test_completed_verdict_survives_a_signal_during_the_restore(tmp_path, monkeypatch, fake_restore):
    """Fable r3 N3: the scored verdict is written first; the interrupt is recorded alongside and raised after."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    real_flash = echoear_aec_bench.flash
    def flash_and_signal(*a, **k):
        os.kill(os.getpid(), signal.SIGINT); return real_flash(*a, **k)
    monkeypatch.setattr(echoear_aec_bench, 'flash', flash_and_signal)
    out = binding.parent/'quiet_offline'/'attempt-1'
    assert item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None) == 0   # verdict exit code kept (Fable r4 N3)
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is True and 'interrupted' not in r and r['interrupt_after_verdict'] == ['SIGINT']
    assert r['restore']['clean'] is True


def test_hot_stt_post_hold_scoring_failure_records_the_final_error(tmp_path, monkeypatch, fake_restore):
    """Astra r3 #5: real score_hot_once path; a scoring exception keeps the one-shot record and adds the error."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='a6_hot_stt')
    import echoear_hot_stt
    def acquire(plan, out, preflight):
        out.mkdir(parents=True); (out/'serial.log').write_text('')
        return dict(acquisition_complete=True, points=[dict(before=dict(output_volume_db=-6), after=dict(output_volume_db=-6))], passed=None)
    monkeypatch.setattr(echoear_hot_stt, 'acquire', acquire)
    def score(out): raise ConnectionError('transcription transport failed')
    monkeypatch.setattr(echoear_hot_stt, 'score', score)
    pre = lambda *a: None; pre.observations = []
    monkeypatch.setattr(item2, 'a6_preflight', lambda plan, stage, journal: pre)
    monkeypatch.setattr(item2, 'a6_resources', lambda out, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    out = binding.parent/'a6_hot_stt'/'attempt-1'
    with pytest.raises(ConnectionError):
        item2.execute(binding, 'a6_hot_stt', out, restore_wait=lambda: None)
    r = json.loads((out/'result.json').read_text())
    assert r['acquisition_complete'] is True and r['points']                     # one-shot evidence preserved
    assert r['error'] == 'ConnectionError: transcription transport failed' and r['interrupted'] is False
    assert r['stage'] == 'a6_hot_stt' and r['release_binding'] == str(binding) and r['scoring_failed'] and r['restore']['clean']
    assert r['measurement_volume']['applicable'] is False                         # decision 136: recorded, not gated


def test_pre_hold_refusal_never_overwrites_an_existing_population(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    out = binding.parent/'quiet_offline'/'attempt-1'; out.mkdir(parents=True)
    (out/'result.json').write_text('{"kept": true}')
    with pytest.raises((FileExistsError, RuntimeError)):
        item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None)
    assert (out/'result.json').read_text() == '{"kept": true}' and 'lock-enter' not in trace


def test_session_gate_requires_a_boot():
    lines = [l for l in session() if 'elf_sha256=' not in l]
    assert profile.session_gate(lines, 'normal')['checks']['boot_seen'] is False


def test_plan_names_the_decision_136_hot_stt_exception():
    plan = json.loads(N1_PLAN.read_text())
    assert 'decision 136' in plan['release_requirements'][8] and 'a6_hot_stt' in plan['release_requirements'][8]
    assert 'Decision 136' in plan['order'][2]['volume_expectation'] and 'decision 136' in plan['firmware']['volume_scope']


# ---- r5: deterministic signal injection at every line of the hold lifecycle (Astra r4 #1) ------------------------

import inspect


def hold_lines():
    """Every source line of RestoreHold.__enter__, touch, __exit__, _handler and _protected."""
    rows = []
    for fn in (item2.RestoreHold.__enter__, item2.RestoreHold.touch, item2.RestoreHold.__exit__):
        lines, start = inspect.getsourcelines(fn)
        rows += [(fn.__code__, start+i) for i in range(1, len(lines)) if lines[i].strip() and not lines[i].strip().startswith(('#', '"'))]
    return rows


LINES = hold_lines()


def pre_touch_default_sigterm(where, sig):
    # Before __enter__ has installed its SIGTERM handler the default SIGTERM ends the process: nothing is touched
    # then (kernel releases the flock, board still on the restore image). Not injectable in-process; documented.
    code, line = where
    if sig != signal.SIGTERM or code is not item2.RestoreHold.__enter__.__code__: return False
    src, start = inspect.getsourcelines(item2.RestoreHold.__enter__)
    install = start + next(i for i, t in enumerate(src) if 'signal.signal(signal.SIGTERM' in t)
    return line <= install


def run_injected(code, line, sig, body):
    injected = []
    def tracer(frame, event, arg):
        if not injected and frame.f_code is code and event == 'line' and frame.f_lineno == line:
            injected.append(True); os.kill(os.getpid(), sig)
        return tracer
    previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    sys.settrace(tracer)
    try:
        try: outcome = body()
        except (KeyboardInterrupt, RuntimeError) as exc: outcome = exc
    finally:
        sys.settrace(None)
        for n, h in previous.items(): signal.signal(n, h)
    return injected, outcome


def assert_invariant(trace, record):
    assert trace[-1] == 'lock-exit'
    if 'flash-candidate' in trace:
        assert trace.index('flash-candidate') < trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
        assert record.get('clean') is True and record.get('device_untouched') is not True
    else:
        assert record.get('device_untouched') is True or record.get('clean') is True


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
@pytest.mark.parametrize('where', LINES, ids=[f'{c.co_name}:{l}' for c, l in LINES])
def test_item2_signal_injected_at_every_hold_line_never_unlocks_unverified(tmp_path, monkeypatch, fake_restore, where, sig):
    if pre_touch_default_sigterm(where, sig): pytest.skip('default SIGTERM before the hold handler: process ends untouched')
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'attempt-1'
    injected, outcome = run_injected(*where, sig, lambda: item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None))
    if not injected: pytest.skip('line not executed on this path (e.g. the escaped-prologue fallback)')
    r = json.loads((out/'result.json').read_text())
    assert_invariant(trace, r['restore'])
    if where[0] is item2.RestoreHold.__exit__.__code__:
        # Inside __exit__ the signal is only recorded: the stage verdict and its exit code are kept.
        assert outcome == 0 and r['passed'] is True and r['interrupt_after_verdict'] == [sig.name]


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
@pytest.mark.parametrize('where', LINES, ids=[f'{c.co_name}:{l}' for c, l in LINES])
def test_item1_signal_injected_at_every_hold_line_never_unlocks_unverified(tmp_path, monkeypatch, fake_restore, where, sig):
    if pre_touch_default_sigterm(where, sig): pytest.skip('default SIGTERM before the hold handler: process ends untouched')
    trace, _ = fake_restore
    monkeypatch.setattr(item1, 'EVIDENCE', tmp_path)
    monkeypatch.setattr(item1, 'validate_image', lambda image: (dict(commit=item1.CANDIDATE), dict(write_flash_args=[], flash_files={})))
    monkeypatch.setattr(item1, 'rf_pre_gate', lambda snapshot, gate: dict(passed=True))
    monkeypatch.setattr(step9a_echoear, 'verify', lambda out: 'port')
    monkeypatch.setattr(step9a_echoear, 'esptool_python', lambda: 'python')
    import alsa, step7_support, bot_restart
    monkeypatch.setattr(item1, 'subprocess', SimpleNamespace(run=lambda *a, **k: trace.append('flash-candidate'),
        STDOUT=-2, check_output=lambda *a, **k: 'e'*40+'\n'))
    monkeypatch.setattr(alsa, 'resolve_jabra', lambda: ('0', 'jabra'))
    monkeypatch.setattr(alsa, 'set_volumes', lambda card, path: None)
    monkeypatch.setattr(step7_support, 'prepare_prompt', lambda out: 'prompt')
    class Bot:
        def __init__(self, out): pass
        def __enter__(self): raise RuntimeError('bot failed to start')
        def __exit__(self, *a): return False
    monkeypatch.setattr(bot_restart, 'DemoBot', Bot)
    injected, outcome = run_injected(*where, sig, lambda: item1.main(['--binding', str(item1_binding(monkeypatch, tmp_path)), '--image', str(tmp_path/'demo'), '--out', str(tmp_path/'item1'/'attempt-1')], restore_wait=lambda: None))
    if not injected: pytest.skip('line not executed on this path (e.g. the escaped-prologue fallback)')
    r = json.loads((tmp_path/'item1'/'attempt-1'/'result.json').read_text())
    assert_invariant(trace, r.get('restore', dict(device_untouched=True)))
    assert 'audio_capability_lines' in r


def test_signal_during_touch_and_right_after_it(tmp_path, fake_restore):
    """touch() marks the record truthfully before the flash; a signal just after touch restores."""
    trace, _ = fake_restore
    with pytest.raises(KeyboardInterrupt):
        with step9a_echoear.bench():
            with item2.RestoreHold(tmp_path, wait=lambda: None) as hold:
                hold.touch(); assert hold.record == dict(device_untouched=False, restore_pending=True)
                os.kill(os.getpid(), signal.SIGTERM)
                trace.append('never-reached')
    assert 'never-reached' not in trace and hold.record['clean'] is True
    assert trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')


@pytest.mark.parametrize('code,deferred', [(0, False), (2, True)])
def test_signal_during_restore_keeps_the_verdict_exit_code(tmp_path, monkeypatch, fake_restore, code, deferred):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    run_result = dict(passed=False, runs=[], reruns=[], deferred=True, next_original_slot=1) if deferred else None
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline', run_result=run_result)
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    monkeypatch.setattr(item2, 'validate_deferral', lambda out, result: True)
    real_flash = echoear_aec_bench.flash
    def flash_and_signal(*a, **k):
        os.kill(os.getpid(), signal.SIGTERM); return real_flash(*a, **k)
    monkeypatch.setattr(echoear_aec_bench, 'flash', flash_and_signal)
    if deferred:
        import e2e_common as e2
        real = e2.run
        monkeypatch.setattr(e2, 'run', lambda args, live: (real(args, live), 2)[1])
    out = binding.parent/'quiet_offline'/'attempt-1'
    assert item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None) == code
    r = json.loads((out/'result.json').read_text())
    assert r['interrupt_after_verdict'] == ['SIGTERM'] and bool(r.get('deferred')) is deferred


@pytest.mark.parametrize('line,crash', [
    ('I (12) dg-app: RTVI bot[1]: The brownout detector and the Backtrace: section are explained here.', False),
    ('I (12) dg-app: RTVI user[2]: what does panic\'ed mean, and what is task_wdt:?', False),
    ('I (12) dg-app: SERVER {"t":"bot-llm-text","d":{"text":"assert failed"}}', False),
    ("Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.", True),
    ('I (12) dg-app: STATE Ready gen=1 desired=1 t=5 Backtrace: 0x40000000', True),
    ('rst:0x15 (USB_UART_CHIP_RE', False), ('rst:0x15', False), ('rst:0x1 (POWERON),boot:0x8', False),
    ('rst:0x16 (USB_JTAG_CHIP_RESET),boot:0xb', True), ('rst:0x10 (RTCWDT_RTC_RST)', True), ('rst:0xc (RTC_SW_CPU_RST)', True)])
def test_crash_search_excludes_free_text_echo_and_torn_expected_boots(line, crash):
    from step9a_r2_checks import crash_lines, audit
    assert bool(crash_lines(line)) is crash
    assert audit((HEALTHY_STACK+'\n'+line+'\n').encode(), diagnostic=True)['passed'] is (not crash)


def test_escaped_interrupt_in_the_restore_prologue_still_restores(tmp_path, monkeypatch, fake_restore):
    """Fallback branch: a KeyboardInterrupt that escapes the first restore attempt never skips the restore."""
    trace, _ = fake_restore
    real = item2.restore_bench; calls = []
    def flaky(out, wait=None):
        calls.append(1)
        if len(calls) == 1: raise KeyboardInterrupt('escaped')
        return real(out, wait=wait)
    monkeypatch.setattr(item2, 'restore_bench', flaky)
    with step9a_echoear.bench():
        with item2.RestoreHold(tmp_path, wait=lambda: None) as hold:
            hold.touch(); trace.append('flash-candidate')
    hold.release_signals()
    assert len(calls) == 2 and hold.record['clean'] is True and 'KeyboardInterrupt' in hold.record['signals_during_restore']
    assert trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')


# ---- r6 ---------------------------------------------------------------------------------------------------------

ASTRA_R5_PATTERNS = ["Guru Meditation Error: Core 0 panic'ed (LoadProhibited).", 'Backtrace: 0x40000000:0x3f000000',
    'abort() was called at PC 0x40385c7f on core 0', 'assert failed: xQueueGenericSend queue.c:832', 'Task watchdog got triggered.',
    'E (5) task_wdt: Task watchdog got triggered.', 'Brownout detector was triggered', 'rst:0xc', 'rst:0x7', 'rst:0x10', 'rst:0x16 (USB_JTAG_CHIP_RESET)']
# r7 (coordinator decision on Astra r6 #1 / Fable r6 N5): Astra r5's 'task_wdt: watchdog' composition (bare tag)
# no longer counts; only "Task watchdog got triggered" and an E-level task_wdt record do. See the test below.
ECHO_PREFIXES = ['I (12) dg-app: RTVI bot[1]: ', 'I (12) dg-app: RTVI user[1]: ', 'I (12) dg-app: SERVER ']


@pytest.mark.parametrize('prefix', ECHO_PREFIXES)
@pytest.mark.parametrize('pattern', ASTRA_R5_PATTERNS)
def test_torn_echo_prefix_cannot_hide_a_structural_crash(prefix, pattern, rehearsed_quiet_live):
    """Astra r5 #4: the 33 torn-prefix compositions fail through raw audit() and the D6 aggregate."""
    from step9a_r2_checks import audit
    line = prefix+pattern
    assert audit((HEALTHY_STACK+'\n'+line+'\n').encode(), diagnostic=True)['passed'] is False
    result, text = rehearsed_quiet_live
    zero = dict(reruns=[], passed=False, runs=[dict(run=1, scored=False, passed=False)], restore=dict(clean=True),
                stage_nic=dict(passed=True), image_profile=dict(passed=True), measurement_volume=dict(passed=True))
    second = dict(result, runs=[as_continuation(result['runs'][0])]+result['runs'][1:])
    a = item2.aggregate_quiet_live([zero, second], 5, load_plan(N1_PLAN), [HEALTHY_STACK+'\n'+line+'\n', text])
    assert not a['passed'] and a['segment_observations'][0]['required']['crash_free'] is False


@pytest.mark.parametrize('prefix', ECHO_PREFIXES)
@pytest.mark.parametrize('words', ['a brownout is a voltage dip', 'the Backtrace: section lists frames',
    'assert failed means a check did not hold', "the kernel panic'ed once", 'a watchdog resets a stuck task', 'what is task_wdt:?'])
def test_benign_transcript_mentions_still_pass(prefix, words):
    from step9a_r2_checks import audit, crash_lines
    line = prefix+words
    assert crash_lines(line) == [] and audit((HEALTHY_STACK+'\n'+line+'\n').encode())['passed']


def test_bare_generic_words_still_count_outside_echo_lines():
    from step9a_r2_checks import crash_lines
    for line in ['Backtrace: (corrupted)', 'brownout', "panic'ed", 'assert failed']:
        assert crash_lines(line) == [line]


def write_line(fn, needle):
    src, start = inspect.getsourcelines(fn)
    return start + next(i for i, t in enumerate(src) if needle in t)


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
@pytest.mark.parametrize('where', ['pre-write', 'inside-write'])
def test_item2_final_write_includes_signals_up_to_the_write(tmp_path, monkeypatch, fake_restore, sig, where):
    """Astra r5 #3: a signal just before or during the final write is in result.json (write, re-check, rewrite)."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'attempt-1'
    if where == 'pre-write':
        code, line = item2.execute.__code__, write_line(item2.execute, "write_final(out/'result.json',result,hold)   # Fable r3 N3")
    else:
        code, line = item2.atomic_write_json.__code__, write_line(item2.atomic_write_json, 'stream.write(data)')
    injected, outcome = run_injected(code, line, sig, lambda: item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None))
    assert injected and outcome == 0
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is True and r['interrupt_after_verdict'] == [sig.name] and r['signals_by_phase'] == dict(after_restore=[sig.name])
    assert signal.getsignal(sig) is not item2.RestoreHold._handler


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
def test_item1_final_write_includes_a_signal_at_the_pre_write_line(tmp_path, monkeypatch, fake_restore, sig):
    trace, _ = fake_restore
    monkeypatch.setattr(item1, 'EVIDENCE', tmp_path)
    monkeypatch.setattr(item1, 'validate_image', lambda image: (dict(commit=item1.CANDIDATE), dict(write_flash_args=[], flash_files={})))
    monkeypatch.setattr(item1, 'rf_pre_gate', lambda snapshot, gate: dict(passed=True))
    monkeypatch.setattr(step9a_echoear, 'verify', lambda out: 'port')
    monkeypatch.setattr(step9a_echoear, 'esptool_python', lambda: 'python')
    import alsa, step7_support, bot_restart
    monkeypatch.setattr(item1, 'subprocess', SimpleNamespace(run=lambda *a, **k: trace.append('flash-candidate'),
        STDOUT=-2, check_output=lambda *a, **k: 'e'*40+'\n'))
    monkeypatch.setattr(alsa, 'resolve_jabra', lambda: ('0', 'jabra'))
    monkeypatch.setattr(alsa, 'set_volumes', lambda card, path: None)
    monkeypatch.setattr(step7_support, 'prepare_prompt', lambda out: 'prompt')
    class Bot:
        def __init__(self, out): pass
        def __enter__(self): raise RuntimeError('bot failed to start')
        def __exit__(self, *a): return False
    monkeypatch.setattr(bot_restart, 'DemoBot', Bot)
    line = write_line(item1.main, "write_final(out/'result.json',result,hold)")
    injected, outcome = run_injected(item1.main.__code__, line, sig,
        lambda: item1.main(['--binding', str(item1_binding(monkeypatch, tmp_path)), '--image', str(tmp_path/'demo'), '--out', str(tmp_path/'item1'/'attempt-1')], restore_wait=lambda: None))
    assert injected and outcome == 1          # the verdict (bot failure) and its exit code are kept
    r = json.loads((tmp_path/'item1'/'attempt-1'/'result.json').read_text())
    assert r['interrupt_after_verdict'] == [sig.name] and r['restore']['clean'] is True


def test_hot_stt_scoring_is_interruptible_after_the_unlock(tmp_path, monkeypatch, fake_restore):
    """Fable r5 N1: real execute(); a SIGINT during post-unlock scoring interrupts it, labelled during_scoring."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='a6_hot_stt')
    import echoear_hot_stt
    def acquire(plan, out, preflight):
        out.mkdir(parents=True); (out/'serial.log').write_text('')
        return dict(acquisition_complete=True, points=[dict(before=dict(output_volume_db=-6), after=dict(output_volume_db=-6))], passed=None)
    monkeypatch.setattr(echoear_hot_stt, 'acquire', acquire)
    scored = []
    def score(out):
        assert trace[-1] == 'lock-exit'                      # scoring runs after the verified restore and unlock
        os.kill(os.getpid(), signal.SIGINT); scored.append('continued past the signal')
        return dict(passed=True, item2_resources=dict(passed=True), stage_nic=dict(passed=True))
    monkeypatch.setattr(echoear_hot_stt, 'score', score)
    pre = lambda *a: None; pre.observations = []
    monkeypatch.setattr(item2, 'a6_preflight', lambda plan, stage, journal: pre)
    monkeypatch.setattr(item2, 'a6_resources', lambda out, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    out = binding.parent/'a6_hot_stt'/'attempt-1'
    with pytest.raises(KeyboardInterrupt, match='during scoring'):
        item2.execute(binding, 'a6_hot_stt', out, restore_wait=lambda: None)
    r = json.loads((out/'result.json').read_text())
    assert not scored and r['interrupted'] is True and r['scoring_failed'] is True and r['passed'] is False
    assert r['signals_by_phase'] == dict(during_scoring=['SIGINT']) and 'signals_during_restore' not in r['restore']
    assert r['acquisition_complete'] is True and r['restore']['clean'] is True


def test_signal_raising_out_of_enter_leaves_no_hold_handler(tmp_path):
    """Fable r5 N4: an interrupt at the last line of __enter__ releases both handlers it installed."""
    previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    hold = item2.RestoreHold(tmp_path)
    line = write_line(item2.RestoreHold.__enter__, 'return self')
    injected, outcome = run_injected(item2.RestoreHold.__enter__.__code__, line, signal.SIGINT, lambda: hold.__enter__())
    assert injected and isinstance(outcome, KeyboardInterrupt) and hold.previous == {}
    assert all(signal.getsignal(n) is previous[n] for n in previous)



# ---- r7 ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('line,crash', [
    ('I (1) task_wdt: initialized', False), ('D (1) task_wdt: retention', False), ('W (1) task_wdt: retention note', False),
    ('I (12) dg-app: RTVI bot[1]: task_wdt: watchdog', False), ('task_wdt: watchdog', False),
    ('E (5) task_wdt: Task watchdog got triggered. The following tasks did not reset the watchdog in time:', True),
    ('E (5) task_wdt:  - IDLE0 (CPU 0)', True), ('I (12) dg-app: RTVI bot[1]: E (5) task_wdt: torn', True),
    ('Task watchdog got triggered.', True)])
def test_task_wdt_counts_only_as_triggered_or_error_level(line, crash):
    from step9a_r2_checks import crash_lines
    assert bool(crash_lines(line)) is crash


def atomic_line(fn=None):
    src, start = inspect.getsourcelines(fn or item2.atomic_write_json)
    return start + next(i for i, t in enumerate(src) if 'stream.write(data)' in t)


def hot_stt_environment(tmp_path, monkeypatch, fake_restore, *, fail):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='a6_hot_stt')
    import echoear_hot_stt
    def acquire(plan, out, preflight):
        out.mkdir(parents=True); (out/'serial.log').write_text('')
        return dict(acquisition_complete=True, points=[dict(before=dict(output_volume_db=-6), after=dict(output_volume_db=-6))], passed=None)
    monkeypatch.setattr(echoear_hot_stt, 'acquire', acquire)
    def score(out):
        if fail: raise ConnectionError('transcription transport failed')
        return dict(passed=True, scored=True, points=[1], item2_resources=dict(passed=True), stage_nic=dict(passed=True))
    monkeypatch.setattr(echoear_hot_stt, 'score', score)
    pre = lambda *a: None; pre.observations = []
    monkeypatch.setattr(item2, 'a6_preflight', lambda plan, stage, journal: pre)
    monkeypatch.setattr(item2, 'a6_resources', lambda out, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    return binding


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
@pytest.mark.parametrize('fail', [False, True], ids=['scoring-success', 'scoring-failed'])
def test_hot_stt_signal_inside_the_final_write_keeps_a_valid_record(tmp_path, monkeypatch, fake_restore, sig, fail):
    """Fable r6 B1: after scoring, raising mode is off and writes are atomic; a signal between the temp file's
    open/truncate and its write is recorded and the final record stays valid JSON."""
    binding = hot_stt_environment(tmp_path, monkeypatch, fake_restore, fail=fail)
    out = binding.parent/'a6_hot_stt'/'attempt-1'
    # Inject on the final write's temp write (the first atomic write in the scoring path is score_hot_once's own).
    hits = []
    real = item2.atomic_write_json
    def counting(path, value):
        hits.append(1); return real(path, value)
    monkeypatch.setattr(item2, 'atomic_write_json', counting)
    injected = []
    line = atomic_line(real)
    def tracer(frame, event, arg):
        if not injected and frame.f_code is real.__code__ and event == 'line' and frame.f_lineno == line and len(hits) >= 2:
            injected.append(True); os.kill(os.getpid(), sig)
        return tracer
    previous = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    sys.settrace(tracer)
    try:
        try: outcome = item2.execute(binding, 'a6_hot_stt', out, restore_wait=lambda: None)
        except ConnectionError as exc: outcome = exc
    finally:
        sys.settrace(None)
    assert injected
    r = json.loads((out/'result.json').read_text())                 # valid JSON, never truncated
    assert r['restore']['clean'] is True and sig.name in r['interrupt_after_verdict']
    assert all(signal.getsignal(n) is previous[n] for n in previous)  # handlers released
    if fail:
        assert isinstance(outcome, ConnectionError) and r['scoring_failed'] is True and r['acquisition_complete'] is True
    else:
        assert outcome == 0 and r['passed'] is True and r['scored'] is True
    assert not list(out.glob('.result.json.tmp-*'))


def test_atomic_write_never_destroys_the_previous_record(tmp_path, monkeypatch):
    path = tmp_path/'result.json'; item2.atomic_write_json(path, dict(first=True))
    def boom(*a, **k): raise KeyboardInterrupt('mid-write')
    monkeypatch.setattr(item2.os, 'fsync', boom)
    with pytest.raises(KeyboardInterrupt): item2.atomic_write_json(path, dict(second=True))
    assert json.loads(path.read_text()) == dict(first=True)


def test_write_final_stops_after_the_storm_cap(tmp_path, monkeypatch):
    """Astra r6 #2: a sustained storm cannot keep the final write looping."""
    hold = item2.RestoreHold(tmp_path); hold.exited = True
    real = item2.atomic_write_json; writes = []
    def stormy(path, value):
        writes.append(1); real(path, value); hold.pending_signals.append('SIGINT')
    monkeypatch.setattr(item2, 'atomic_write_json', stormy)
    item2.write_final(tmp_path/'result.json', dict(passed=True), hold, cap=5)
    r = json.loads((tmp_path/'result.json').read_text())
    assert r['signal_storm_truncated'] is True and r['passed'] is True and len(writes) <= 5+3


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
def test_signal_between_last_check_and_release_is_written(tmp_path, monkeypatch, fake_restore, sig):
    """Astra r6 #3 / Fable r6 N2: the gap between the last stability check and release_signals() is closed."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'attempt-1'
    line = write_line(item2.write_final, 'if hold is not None:hold.release_signals()')
    injected, outcome = run_injected(item2.write_final.__code__, line, sig, lambda: item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None))
    assert injected and outcome == 0
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is True and r['interrupt_after_verdict'] == [sig.name]



# ---- r8 (Astra r7 #1/#2) --------------------------------------------------------------------------------------

def real_score_environment(tmp_path, monkeypatch, fake_restore):
    """Real execute(), real score_hot_once(), real echoear_hot_stt.score(); only acquisition, transcription and the
    measurement/profile gates are faked."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='a6_hot_stt')
    import echoear_hot_stt
    def acquire(plan, out, preflight):
        out.mkdir(parents=True); (out/'serial.log').write_text('')
        rows = [dict(target_dbfs=t, acquisition_passed=True, stt_path=str(out/f'native{t}.wav'), firmware_limited_delta=d,
                     headroom=dict(limited_samples=d, native_peak=9000), before=dict(output_volume_db=-6), after=dict(output_volume_db=-6))
                for t, d in zip(echoear_hot_stt.TARGETS, (0, 5))]
        return dict(acquisition_complete=True, expected='please tell me the capital of france', points=rows, passed=False)
    monkeypatch.setattr(echoear_hot_stt, 'acquire', acquire)
    calls = []
    def transcribe(path):
        calls.append(path); return 'please tell me the capital of france'
    monkeypatch.setattr(echoear_hot_stt, 'transcribe', transcribe)
    pre = lambda *a: None; pre.observations = []
    monkeypatch.setattr(item2, 'a6_preflight', lambda plan, stage, journal: pre)
    monkeypatch.setattr(item2, 'a6_resources', lambda out, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    return binding, calls


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
@pytest.mark.parametrize('write,kept', [(6, 'pair-verdict'), (5, 'both-transcripts'), (4, 'both-transcripts'),
                                        (3, 'first-transcript'), (2, 'before-any-request')])
def test_real_score_interrupted_during_a_write_keeps_the_last_valid_scored_record(tmp_path, monkeypatch, fake_restore, sig, write, kept):
    """Astra r7 #2 'real-score-final' cases. Atomic write order in the scoring path: 1 score_hot_once, 2 scored
    marker, 3/4 after each transcript, 5 pair verdict, 6 score's final write."""
    import atomic_json
    binding, calls = real_score_environment(tmp_path, monkeypatch, fake_restore)
    out = binding.parent/'a6_hot_stt'/'attempt-1'
    real = atomic_json.atomic_write_json
    src, start = inspect.getsourcelines(real)
    line = start + next(i for i, t in enumerate(src) if 'stream.write(data)' in t)
    count = []; injected = []
    def tracer(frame, event, arg):
        if frame.f_code is real.__code__ and event == 'call': count.append(1)
        if not injected and frame.f_code is real.__code__ and event == 'line' and frame.f_lineno == line and len(count) == write:
            injected.append(True); os.kill(os.getpid(), sig)
        return tracer
    sys.settrace(tracer)
    try:
        with pytest.raises(KeyboardInterrupt, match='during scoring'):
            item2.execute(binding, 'a6_hot_stt', out, restore_wait=lambda: None)
    finally: sys.settrace(None)
    assert injected
    r = json.loads((out/'result.json').read_text())       # always valid JSON
    assert r['interrupted'] is True and r['scoring_failed'] is True and r['passed'] is False
    assert r['signals_by_phase']['during_scoring'] == [sig.name] and r['restore']['clean'] is True
    assert r['acquisition_complete'] is True and len(r['points']) == 2
    # An interrupted transcript write is followed by score()'s own atomic finally write of the in-memory record;
    # an interrupted final write leaves the durable pair-verdict write before it.
    has = [('transcript' in row, 'word_errors' in row) for row in r['points']]
    if kept == 'pair-verdict':
        assert 'pair_gate' in r and has == [(True, True), (True, True)] and len(calls) == 2
    elif kept == 'both-transcripts':
        assert has == [(True, True), (True, True)] and len(calls) == 2
        assert ('pair_gate' in r) is (write == 5)            # write 5 is the pair verdict; the finally write lands it
    elif kept == 'first-transcript':
        assert has == [(True, True), (False, False)] and 'pair_gate' not in r and len(calls) == 1
    else:
        # Interrupted while writing the scored marker, before any STT request: nothing was sent, so the record
        # carries no scored marker (a later one-shot scoring would be the first).
        assert 'scored' not in r and has == [(False, False), (False, False)] and len(calls) == 0
    assert not list(out.glob('.result.json.tmp-*'))
    if kept != 'before-any-request':
        assert r['scored'] is True
        import echoear_hot_stt
        with pytest.raises(RuntimeError, match='Do not retake'): echoear_hot_stt.score(out)   # one-shot: never re-scored


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM], ids=['SIGINT', 'SIGTERM'])
@pytest.mark.parametrize('needle', ['scoring_error=None', 'hold.interruptible()'])
def test_signal_at_the_before_score_try_boundary_is_recorded_and_scoring_completes(tmp_path, monkeypatch, fake_restore, sig, needle):
    """Astra r7 #1: before interruptible() runs the hold is record-only; the signal is kept, scoring completes."""
    binding, calls = real_score_environment(tmp_path, monkeypatch, fake_restore)
    out = binding.parent/'a6_hot_stt'/'attempt-1'
    line = write_line(item2.execute, needle)
    injected, outcome = run_injected(item2.execute.__code__, line, sig, lambda: item2.execute(binding, 'a6_hot_stt', out, restore_wait=lambda: None))
    assert injected and outcome in (0, 1)
    r = json.loads((out/'result.json').read_text())
    assert r['scored'] is True and 'pair_gate' in r and len(calls) == 2 and 'interrupted' not in r
    assert sig.name in r['interrupt_after_verdict'] and r['signals_by_phase'] == dict(after_restore=[sig.name])



# ---- r9 -------------------------------------------------------------------------------------------------------

import uplink_continuity as uc
LT_FIXTURE = Path(__file__).parent/'fixtures'/'requal_n1_link_tail'


def lt_rows():
    return [json.loads(l) for l in (LT_FIXTURE/'smoke-r9-strict-rtp-excerpt.jsonl').read_text().splitlines()]


def test_real_r9_capture_reorder_inside_idle_is_not_a_giant_gap():
    """smoke-r9 strict: a late packet (seq back by one, timestamp back by 960) inside an audio-idle window was
    computed as gap_ms 89478465 (unsigned step / 48). It is now a counted reorder that forms no interval."""
    meta = json.loads((LT_FIXTURE/'manifest.json').read_text()); rows = lt_rows()
    window = [meta['idle_window']]
    r = uc.link_tail(rows, 4576716.0, 4576734.0, startup_seconds=0, excluded_windows=window)
    assert r['reorders'] > 0 and r['eligible_maximum_ms'] < 2000 and r['whole_capture_maximum_ms'] < 2000
    assert all(g['seq_step'] >= 1 and g['timestamp_step'] < 2**31 for g in r['gaps'])
    assert all(g['gap_ms'] < 2000 for g in r['gaps'])
    assert any(abs(d['at_s']-4576725.907) < 0.002 and d['kind'] == 'reorder' for d in r['disorder'])
    # The pre-r9 arithmetic on the same interval (for the record): unsigned steps give the 89,478 s artefact.
    assert (4294966336/48) > 8.9e7


def stream(seqs_times):
    return [dict(pc_id='p', ssrc=1, seq=s % 65536, timestamp=(s*960) % 2**32, observed_ms=t) for s, t in seqs_times]


def test_reorders_and_duplicates_are_counted_and_real_gaps_still_fire():
    base = [(i, 1000+20*i) for i in range(400)]
    # A 300 ms stall that ends with a late packet: the gap is measured from the head to the next forward packet.
    stalled = base[:200]+[(198, 1000+20*199+300), (200, 1000+20*199+305)]+[(i, 1000+20*i+300) for i in range(201, 400)]
    r = uc.link_tail(stream(stalled), 1.5, 8.5, startup_seconds=0)
    assert r['reorders'] == 1 and r['eligible_maximum_ms'] >= 300 and r['over_extreme_threshold'] >= 1
    dup = base[:100]+[(99, 1000+20*99+1)]+base[100:]
    r = uc.link_tail(stream(dup), 1.5, 8.5, startup_seconds=0)
    assert r['duplicates'] == 1 and r['eligible_maximum_ms'] <= 21 and r['passed']
    hard = base[:200]+[(i, 1000+20*i+2500) for i in range(200, 400)]
    r = uc.link_tail(stream(hard), 1.5, 10.5, startup_seconds=0)
    assert r['eligible_maximum_ms'] >= 2500 and r['passed'] is False        # hard cap still fires on a real gap
    wrap = [(65530+i, 1000+20*i) for i in range(20)]                          # 16-bit wrap is a forward step
    r = uc.link_tail(stream(wrap), 1.05, 1.3, startup_seconds=0)
    assert r['reorders'] == 0 and all(g['seq_step'] == 1 for g in r['gaps'])


def test_reorder_inside_an_idle_window_cannot_reach_the_extreme_or_hard_cap():
    base = [(i, 1000+20*i) for i in range(300)]
    late = base[:150]+[(148, 1000+20*149+1)]+base[150:]
    window = [dict(start_s=3.0, end_s=5.0, category='idle')]
    r = uc.link_tail(stream(late), 1.5, 6.5, startup_seconds=0, excluded_windows=window)
    assert r['reorders'] == 1 and r['eligible_maximum_ms'] < 60 and r['over_extreme_threshold'] == 0 and r['passed']


def test_rf_precheck_verdict_on_the_real_r9_excerpt():
    from echoear_rf_stream_precheck import precheck_verdict, THRESHOLD
    v = precheck_verdict(lt_rows(), 4576716.0, 4576734.0, startup_seconds=0)
    assert THRESHOLD == 0.005 and v['precheck_threshold'] == 0.005
    assert v['reorders'] > 0 and v['eligible_maximum_ms'] < 2000
    assert v['tail_below_threshold'] is (v['tail_rate'] < 0.005)
    assert set(v) >= {'tail_rate', 'over_threshold', 'over_extreme_threshold', 'reorders', 'duplicates', 'covered', 'intervals'}


def next_session(rows, window, *, pc='second-peer', gap_s=10.0):
    """r12: the same stream replayed as a fresh peer after the first window (a clean second session)."""
    times = [r['observed_ms']/1000 for r in rows]
    shift = max(max(times), window[1])-min(min(times), window[0])+gap_s
    return ([dict(r, pc_id=pc, observed_ms=r['observed_ms']+int(shift*1000)) for r in rows],
            (window[0]+shift, window[1]+shift))


def rf_fakes(tmp_path, monkeypatch, fake_restore, *, rf=True, nic=True, display=True, rows=None, window=(4576716.0, 4576734.0),
             second=None, bot_log='', raise_on=None):
    trace, _ = fake_restore
    import echoear_rf_stream_precheck as rfp
    monkeypatch.setattr(rfp, 'EVIDENCE', tmp_path)
    image = tmp_path/'normal'; image.mkdir(); (image/'dg-esp32.elf').write_bytes(b'echoear-audio')
    monkeypatch.setattr(item2, 'verified_binding', lambda path: (dict(budget='b', normal_elf_sha256='55152f25'), image))
    monkeypatch.setattr(item2, 'flash_image', lambda image, out: (trace.append('flash-candidate'), 'port')[1])
    monkeypatch.setattr(item1, 'rf_pre_gate', lambda snapshot, gate: dict(passed=rf))
    import host_nic, step9a_assets, step9a_support, step7_support, esp_serial
    monkeypatch.setattr(host_nic, 'snapshot', lambda: dict(monotonic_s=0.0))
    monkeypatch.setattr(host_nic, 'gate', lambda a, b: dict(passed=nic))
    monkeypatch.setattr(step9a_assets, 'asset_server', contextlib.nullcontext)
    monkeypatch.setattr(step9a_support, 'host_address', lambda: '127.0.0.1')
    monkeypatch.setattr(step9a_support, 'setup', lambda board, **k: dict(mode=k.get('mode')))
    monkeypatch.setattr(step9a_echoear, 'check_firmware', lambda board, since, **k: '55152f25')
    rows = lt_rows() if rows is None else rows
    second_rows, second_window = next_session(rows, window) if second is None else second
    rows = rows+second_rows
    class Bot:
        def __init__(self, out, mode): self.out = Path(out); assert mode == 'offline-test'
        def __enter__(self):
            rtp = Path(os.environ['RTP_EVIDENCE_DIR']); rtp.mkdir(parents=True)
            (rtp/'rtp.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows)); trace.append('bot')
            self.out.mkdir(parents=True, exist_ok=True); (self.out/'server-1.log').write_text(bot_log); return self
        def __exit__(self, *a): trace.append('bot-stop'); return False
    monkeypatch.setattr(step7_support, 'BotServer', Bot)
    class Board(FakeBoard):
        def __init__(self, port, log_path, display_checks): super().__init__(['READY transport-console'])
        def command(self, text, timeout=None):
            super().command(text, timeout)
            if text in ('connect', 'disconnect', 'audio 0'): trace.append(text)
            if text == raise_on: raise RuntimeError('Strict non-display stop rule: '+repr(dict(component='DTLS')))
            if text == 'connect': self._emit('I (1) dg-app: STATE Ready gen=1 desired=1 t=1')
            if text == 'audio 1': self._emit('GATE open t=2')
            return 'OK'
        def __enter__(self): return self
        def __exit__(self, *a): trace.append('serial-close'); return False
        display_monitor = SimpleNamespace(result=lambda: dict(passed=display))
    monkeypatch.setattr(esp_serial, 'EspSerial', Board)
    clock = iter(tuple(window)+tuple(second_window))
    monkeypatch.setattr(rfp.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(rfp.time, 'sleep', lambda s: trace.append('margin' if s == rfp.STOP_MARGIN_SECONDS else 'stream'))
    return rfp, trace


def test_rf_precheck_restores_before_unlock_and_writes_its_record(tmp_path, monkeypatch, fake_restore):
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore)
    out = tmp_path/'20261027T120000Z'
    code = rfp.execute(tmp_path/'binding.json', out, seconds=1, restore_wait=lambda: None)
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['counted'] is False and r['decision'] == 137 and r['precheck_threshold'] == 0.005 and r['exit_code'] == code
    assert set(r['link_tail']) >= {'tail_rate', 'over_threshold', 'over_extreme_threshold', 'reorders', 'duplicates'}
    assert r['rf_pre_gate']['passed'] and r['restore']['clean'] is True
    assert trace.index('flash-candidate') < trace.index('stream') < trace.index('serial-close') < trace.index('restore-boot-verified:restore-1') < trace.index('lock-exit')
    # r13 (Astra r12 B2): an 18 s excerpt at seconds=1 is a diagnostic run: never clearance, never exit 0.
    assert r['diagnostic'] is True and r['rf_clearance'] is False and r['passed'] is False
    assert r['clearance_checks']['qualifying_duration'] is False and code in (1, rfp.DIAGNOSTIC_EXIT)
    with pytest.raises(FileExistsError): rfp.execute(tmp_path/'binding.json', out, seconds=1)
    with pytest.raises(ValueError, match='under'): rfp.execute(tmp_path/'binding.json', Path('/tmp/elsewhere'), seconds=1)


def test_rf_precheck_pre_gate_failure_touches_nothing(tmp_path, monkeypatch, fake_restore):
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rf=False)
    out = tmp_path/'20261027T120500Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=1, restore_wait=lambda: None) == 2
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['deferred'] is True and r['restore'] == dict(device_untouched=True) and trace == ['lock-enter', 'lock-exit']


def test_score_refuses_a_record_finalised_by_execute(tmp_path):
    import echoear_hot_stt
    (tmp_path/'result.json').write_text(json.dumps(dict(stage='a6_hot_stt', points=[])))
    with pytest.raises(RuntimeError, match='finalised by execute'): echoear_hot_stt.score(tmp_path)
    (tmp_path/'result.json').write_text(json.dumps(dict(scoring_failed=True, points=[])))
    with pytest.raises(RuntimeError, match='finalised by execute'): echoear_hot_stt.score(tmp_path)


def test_item2_and_item1_record_their_exit_code(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'attempt-1'
    code = item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None)
    assert json.loads((out/'result.json').read_text())['exit_code'] == code == 0



# ---- r10 ------------------------------------------------------------------------------------------------------

def clean_stream(n, t0=0.0, *, pc='p', ssrc=1):
    return [dict(pc_id=pc, ssrc=ssrc, seq=i % 65536, timestamp=(i*960) % 2**32, observed_ms=int(t0*1000+20*i)) for i in range(n)]


def astra_terminal_repro():
    """Astra r9 #1: 20 ms packets through t=145.000 s (seq 7250), seq 7249 delayed to t=150.020 s; window [1,150]."""
    rows = [dict(pc_id='p', ssrc=1, seq=i, timestamp=i*960, observed_ms=20*i) for i in range(7251) if i != 7249]
    rows.append(dict(pc_id='p', ssrc=1, seq=7249, timestamp=7249*960, observed_ms=150020))
    return rows


def test_astra_terminal_late_packet_cannot_hide_the_suffix():
    r = uc.link_tail(astra_terminal_repro(), 1, 150)
    assert r['reorders'] == 1 and r['covered'] is True
    assert r['unresolved_tail_ms'] == pytest.approx(5000) and r['eligible_maximum_ms'] >= 5000 and r['passed'] is False


@pytest.mark.parametrize('kind', ['reorder', 'duplicate'])
def test_terminal_late_or_duplicate_packet_inside_the_window(kind):
    rows = clean_stream(5000)                                  # forward packets to 99.98 s
    late_seq = 4998 if kind == 'reorder' else 4999
    rows.append(dict(pc_id='p', ssrc=1, seq=late_seq, timestamp=late_seq*960, observed_ms=101500))
    r = uc.link_tail(rows, 1, 103)                             # window ends after the late packet
    assert r[kind+'s'] == 1 and r['unresolved_tail_ms'] == pytest.approx(1520)
    assert r['eligible_maximum_ms'] >= 1519.9 and r['over_extreme_threshold'] >= 1
    assert r['passed'] is None                                  # the stream itself does not reach the window end


@pytest.mark.parametrize('kind', ['reorder', 'duplicate'])
def test_terminal_late_packet_just_past_the_endpoint(kind):
    rows = clean_stream(5000)                                  # last forward at 99.98 s
    late_seq = 4998 if kind == 'reorder' else 4999
    rows.append(dict(pc_id='p', ssrc=1, seq=late_seq, timestamp=late_seq*960, observed_ms=100030))
    r = uc.link_tail(rows, 1, 100)
    assert r['covered'] is True and r['unresolved_tail_ms'] == pytest.approx(20) and r['passed'] is True
    stalled = clean_stream(4000) + [dict(pc_id='p', ssrc=1, seq=3998, timestamp=3998*960, observed_ms=100030)]
    r = uc.link_tail(stalled, 1, 100)                          # last forward at 79.98 s: the 20 s suffix is charged
    assert r['covered'] is True and r['unresolved_tail_ms'] == pytest.approx(20020) and r['passed'] is False


def test_terminal_unresolved_interval_is_net_of_exclusions():
    r = uc.link_tail(astra_terminal_repro(), 1, 150, excluded_windows=[dict(start_s=145.0, end_s=150.0, category='idle')])
    assert r['unresolved_tail_ms'] in (None, 0) or r['unresolved_tail_ms'] == pytest.approx(0)
    assert r['passed'] is True


def test_sequence_reset_suffix_fails_closed():
    """Fable r9 N1 / Astra r9: a same-stream reset (500 -> 0) would leave the rest 'late' and hide a stall."""
    rows = clean_stream(501)
    rows += [dict(pc_id='p', ssrc=1, seq=i, timestamp=i*960, observed_ms=10020+20*i+(400 if i > 100 else 0)) for i in range(250)]
    r = uc.link_tail(rows, 1, 15)
    assert r['reset_suspected'] is True and r['longest_nonforward_run'] > 64 and r['passed'] is False
    # Ordinary reordering stays below both bounds.
    rows = clean_stream(1000); rows[500], rows[501] = rows[501], rows[500]
    rows[500]['observed_ms'], rows[501]['observed_ms'] = rows[501]['observed_ms'], rows[500]['observed_ms']
    r = uc.link_tail(rows, 1, 19.5)
    assert r['reset_suspected'] is False and r['passed'] is True


@pytest.mark.parametrize('case', ['nic', 'display', 'restore-retry', 'hard-cap', 'terminal-late', 'clean'])
def test_rf_clearance_requires_every_check(tmp_path, monkeypatch, fake_restore, case):
    """Astra r9 #2: rf_clearance, passed and exit_code are one conjunction (tail below 0.5 %, a passing
    link_tail, stage NIC, display monitor, restore clean on its first attempt); the raw rate is kept."""
    trace, state = fake_restore
    rows, window = clean_stream(14000), (0.5, 279.0)
    if case == 'hard-cap':
        rows = clean_stream(7000) + [dict(r, seq=r['seq']+7000, timestamp=r['timestamp']+7000*960, observed_ms=r['observed_ms']+140000+2500)
                                    for r in clean_stream(6900)]
        window = (0.5, 279.0)
    if case == 'terminal-late':
        rows, window = astra_terminal_repro(), (1, 150)
    if case == 'restore-retry': state['fail'] = 1
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, nic=case != 'nic', display=case != 'display', rows=rows, window=window)
    out = tmp_path/'20261027T130000Z'
    code = rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None)
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['passed'] is r['rf_clearance'] and r['exit_code'] == code == (0 if r['rf_clearance'] else 1)
    if case == 'clean':
        assert r['rf_clearance'] is True and all(r['clearance_checks'].values())
    else:
        assert r['rf_clearance'] is False and code == 1
        failed = {k for k, v in r['clearance_checks'].items() if not v}
        expect = dict(nic={'stage_nic'}, display={'display_monitor'}, **{'restore-retry': {'restore_first_attempt'},
                      'hard-cap': {'link_tail_passed'}, 'terminal-late': {'link_tail_passed', 'qualifying_duration'}})[case]
        assert failed == expect
        if case == 'hard-cap':
            assert r['link_tail']['tail_below_threshold'] is True and r['link_tail']['eligible_maximum_ms'] >= 2500   # raw rate kept


IDLE_LOG = ('2026-10-27 10:27:58.064 | DEBUG    | pipecat.pipeline.worker:_wait_for_pipeline_end:1228 - PipelineWorker#0: Closing. '
            'Waiting for CancelFrame#0(reason: idle timeout) to reach the end of the pipeline...\n')


def test_rf_precheck_refuses_windows_at_the_bot_idle_timeout(tmp_path, monkeypatch, fake_restore):
    """r12: 20261027T172129Z was cut at ~300 s by the offline-test bot's Pipecat idle timeout; a session window
    of >= 290 s (or fewer than two sessions) is refused before anything is touched."""
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore)
    assert rfp.STREAM_SECONDS == 270 and rfp.MAX_SESSION_SECONDS == 290 and rfp.SESSIONS == 2
    for seconds in (290, 320, 0):
        with pytest.raises(ValueError, match='idle timeout|below'):
            rfp.execute(tmp_path/'binding.json', tmp_path/'20261027T150000Z', seconds=seconds)
    with pytest.raises(ValueError, match='two sessions'):
        rfp.execute(tmp_path/'binding.json', tmp_path/'20261027T150000Z', seconds=270, sessions=1)
    assert trace == [] and not (tmp_path/'20261027T150000Z').exists()


def test_rf_precheck_two_fresh_sessions_each_measured_on_its_own_peer(tmp_path, monkeypatch, fake_restore):
    rows, window = clean_stream(14000), (0.5, 279.0)
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=rows, window=window)
    out = tmp_path/'20261027T150500Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None) == 0
    r = json.loads((out/'rf-precheck.json').read_text())
    # r13 (20261027T184859Z): the uplink keeps running STOP_MARGIN_SECONDS past each window end before `audio 0`.
    assert [t for t in trace if t in ('connect', 'disconnect', 'stream', 'margin', 'audio 0')] == ['connect', 'stream', 'margin', 'audio 0', 'disconnect']*2
    assert rfp.STOP_MARGIN_SECONDS >= 1 and rfp.MAX_SESSION_SECONDS+rfp.STOP_MARGIN_SECONDS < 300
    assert [s['pc_ids'] for s in r['sessions']] == [['p'], ['second-peer']] and r['harness_failure'] == []
    assert r['link_tail']['sessions'] == 2 and r['clearance_checks']['sessions_complete'] and r['rf_clearance']
    assert os.environ['RTP_EVIDENCE_MAX_PACKETS'] == str(rfp.RTP_MAX_PACKETS)


@pytest.mark.parametrize('case', ['idle-timeout', 'reused-peer', 'two-peers', 'second-session-fails'])
def test_rf_precheck_harness_failures_and_second_session_never_clear(tmp_path, monkeypatch, fake_restore, case):
    rows, window = clean_stream(14000), (0.5, 279.0)
    second, bot_log = None, ''
    if case == 'idle-timeout': bot_log = IDLE_LOG
    if case == 'reused-peer': second = next_session(rows, window, pc='p')
    if case == 'two-peers':
        second_rows, second_window = next_session(rows, window)
        second = (second_rows+next_session(rows, window, pc='third-peer')[0][:100], second_window)
    if case == 'second-session-fails':
        second_rows, second_window = next_session(rows, window)
        second = ([dict(r, observed_ms=r['observed_ms']+2500) if i >= 3600 else r for i, r in enumerate(second_rows)], second_window)
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=rows, window=window, second=second, bot_log=bot_log)
    out = tmp_path/'20261027T151000Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None) == 1
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['rf_clearance'] is False and r['passed'] is False and r['exit_code'] == 1
    kinds = {p['kind'] for p in r['harness_failure']}
    expect = {'idle-timeout': {'bot_idle_timeout'}, 'reused-peer': {'session_reused_peer'}, 'two-peers': {'session_pc_ids'},
              'second-session-fails': set()}[case]
    assert kinds == expect and r['clearance_checks']['harness_clean'] is (not expect)
    if case == 'second-session-fails':
        assert r['sessions'][0]['link_tail']['link_tail_passed'] is True and r['sessions'][1]['link_tail']['link_tail_passed'] is False
        assert {k for k, v in r['clearance_checks'].items() if not v} == {'link_tail_passed'}


def test_rf_precheck_idle_timeout_is_labelled_on_the_exception_path(tmp_path, monkeypatch, fake_restore):
    """The 172129Z shape: the stop rule (DTLS close after the bot's idle cancel) raises at the post-stream command."""
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=clean_stream(14000), window=(0.5, 279.0),
                          bot_log=IDLE_LOG, raise_on='quiet 0')
    out = tmp_path/'20261027T151500Z'
    with pytest.raises(RuntimeError, match='stop rule'):
        rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None)
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['exit_code'] == 1 and r['passed'] is False and 'rf_clearance' not in r
    assert [p['kind'] for p in r['harness_failure']] == ['bot_idle_timeout'] and r['harness_failure'][0]['log'] == 'server-1.log'
    assert r['restore']['clean'] is True


def test_rf_precheck_rtp_evidence_cap_is_a_harness_failure(tmp_path):
    import echoear_rf_stream_precheck as rfp
    (tmp_path/'bot').mkdir()
    rows = clean_stream(500)+clean_stream(500, 20.0, pc='q')
    sessions = [dict(index=1, window=dict(start_s=1.0, end_s=9.0)), dict(index=2, window=dict(start_s=21.0, end_s=29.0)),
                dict(index=3)]   # an interrupted session without a window is skipped
    assert rfp.harness_problems(tmp_path, rows, sessions, cap=1000) == []
    assert rfp.harness_problems(tmp_path, rows, sessions, cap=500) == [
        dict(kind='rtp_evidence_cap', pc_id='p', packets=500, cap=500), dict(kind='rtp_evidence_cap', pc_id='q', packets=500, cap=500)]
    assert rfp.RTP_MAX_PACKETS >= 50*rfp.MAX_SESSION_SECONDS*2


def test_rf_short_diagnostic_run_never_clears(tmp_path, monkeypatch, fake_restore):
    """Astra r12 B2 exact repro: execute(seconds=10, sessions=2), two clean distinct peers, windows [0.5,10.5] and
    [22.48,32.48]. It used to exit 0 with rf_clearance true; it is now diagnostic, rf_clearance false, exit 5."""
    rows = clean_stream(600)
    second = ([dict(r, pc_id='second-peer', observed_ms=r['observed_ms']+21980) for r in clean_stream(600)], (22.48, 32.48))
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=rows, window=(0.5, 10.5), second=second)
    out = tmp_path/'20261027T160000Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=10, sessions=2, restore_wait=lambda: None) == rfp.DIAGNOSTIC_EXIT == 5
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['diagnostic'] is True and r['rf_clearance'] is False and r['passed'] is False and r['exit_code'] == 5
    assert {k for k, v in r['clearance_checks'].items() if not v} == {'qualifying_duration'}


def test_rf_requested_270_but_short_measured_window_does_not_clear(tmp_path, monkeypatch, fake_restore):
    rows = clean_stream(7200)
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=rows, window=(0.5, 143.0))
    out = tmp_path/'20261027T160500Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None) == 1
    r = json.loads((out/'rf-precheck.json').read_text())
    assert r['diagnostic'] is False and r['rf_clearance'] is False
    assert {k for k, v in r['clearance_checks'].items() if not v} == {'qualifying_duration'}


def test_rf_precheck_record_cannot_fill_the_item1_slot(tmp_path, monkeypatch, fake_restore):
    """Astra r9 #3 exact regression: the clean RF record from real execute() offered as item1."""
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=clean_stream(14000), window=(0.5, 279.0))
    out = tmp_path/'20261027T140000Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None) == 0
    rf_record = json.loads((out/'rf-precheck.json').read_text())
    plan = load_plan(N1_PLAN)
    ok = {s['id']: final(s['id'], passed=True) for s in plan['order']}
    ok['a6_analog_probe'] = final('a6_analog_probe', passed=False, acquisition_valid=True)
    ok['quiet_live'] = final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True))
    assert acceptance_verdict(plan, ITEM1, ok)['passed']
    v = acceptance_verdict(plan, rf_record, ok)
    assert v['passed'] is False and v['item1'] is False
    assert {'uncounted record', 'rf-precheck record', 'not an item1 record (kind)'} <= set(v['item1_record_problems'])
    for stage in ok:
        assert acceptance_verdict(plan, ITEM1, dict(ok, **{stage: rf_record}))['passed'] is False
    swapped = dict(ok, strict_voice_cat=final('quiet_offline', passed=True))
    assert 'stage mismatch' in acceptance_verdict(plan, ITEM1, swapped)['stages']['strict_voice_cat']['record_problems']
    assert 'item2 record in the item1 slot' in acceptance_verdict(plan, dict(ITEM1, stage='strict_voice_cat'), ok)['item1_record_problems']


def acceptance_population(tmp_path, *, binding_overrides=None, root_name='acceptance'):
    """A complete, valid on-disk acceptance root bound to an acceptance binding that names it (r12 collector)."""
    tmp_path.mkdir(parents=True, exist_ok=True); binding = tmp_path/'acceptance-binding.json'
    root = tmp_path/root_name
    binding.write_text(json.dumps(dict(dict(counted=True, purpose='acceptance', execution_authorized=True, normal_elf_sha256='55152f25',
                                            demo_elf_sha256='5bbd868a', no_retakes=True, output_root=str(root)), **(binding_overrides or {}))))
    digest = hashlib.sha256(binding.read_bytes()).hexdigest()
    prov = dict(release_binding=str(binding.resolve()), binding_sha256=digest, counted=True, purpose='acceptance')
    plan = load_plan(N1_PLAN)
    records = {s['id']: dict(final(s['id'], passed=True), **prov) for s in plan['order']}
    records['a6_analog_probe'] = dict(final('a6_analog_probe', passed=False, acquisition_valid=True), **prov)
    records['quiet_live'] = dict(final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True)), **prov)
    records['item1'] = dict(ITEM1, **prov)
    for slot, record in records.items():write_attempt(root, slot, 1, record)
    return plan, root, binding, digest, prov


def write_attempt(root, slot, n, record, code=None):
    d = root/slot/f'attempt-{n}'; d.mkdir(parents=True, exist_ok=True)
    if record is not None and 'release_binding' in record and 'out_path' not in record:
        record = dict(record, out_path=str(d.resolve()))   # r14: as execute() stamps it
    if record is not None:(d/'result.json').write_text(json.dumps(record))
    exit_code = code if code is not None else (record.get('exit_code', 1) if record else 1)
    (root/slot/f'attempt-{n}.typescript').write_text('Script started\n...\nEXITED %d\n' % exit_code)
    return d


def clean_item2_deferral(stage, prov, **extra):
    return dict(final(stage, passed=False, deferred=True, runs=[], exit_code=2, deferral_resources=dict(passed=True),
                      restore=dict(clean=True, attempts=[dict(label='restore-1', error=None)])), **prov, **extra)


def test_acceptance_collector_compares_typescript_exit_codes(tmp_path):
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    r = collect(plan, binding, digest)
    assert r['passed'] is True and not r['binding_problems'] and all(not v['problems'] for v in r['slots'].values())
    assert r['output_root'] == str(root)
    (root/'quiet_offline'/'attempt-1.typescript').write_text('EXITED 1\n')
    r = collect(plan, binding, digest)
    assert r['passed'] is False and 'typescript EXITED 1 != record exit_code 0' in r['slots']['quiet_offline']['problems']
    for text in ('', 'echo EXITED $?\n', 'XEXITED 0\n', 'EXITED 0 trailing\n'):    # only a complete marker line counts
        (root/'quiet_offline'/'attempt-1.typescript').write_text(text)
        assert any('no complete EXITED line' in p for p in collect(plan, binding, digest)['slots']['quiet_offline']['problems'])
    (root/'quiet_offline'/'attempt-1.typescript').write_text('EXITED 0\r\n')
    assert collect(plan, binding, digest)['passed'] is True


def test_collector_binding_must_be_the_expected_counted_acceptance_binding(tmp_path):
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    assert collect(plan, tmp_path/'missing.json', digest)['binding_problems'][0].startswith('acceptance binding does not exist')
    assert 'sha256' in collect(plan, binding, '0'*64)['binding_problems'][0]
    for override, problem in ((dict(counted=False), 'binding is not counted'), (dict(purpose='smoke'), 'binding purpose is not acceptance'),
                              (dict(execution_authorized=False), 'binding is not authorised'), (dict(output_root=None), 'binding declares no output_root'),
                              (dict(authorized_prior_failures={'/x/result.json': '0'*64}), 'authorized_prior_failures with no_retakes and no authorising decision')):
        plan2, root2, b2, d2, _ = acceptance_population(tmp_path/problem.replace(' ', '-'), binding_overrides=override)
        r = collect(plan2, b2, d2)
        assert r['passed'] is False and problem in r['binding_problems']


def test_collector_refuses_a_real_smoke_execute_record_copied_or_symlinked(tmp_path, monkeypatch, fake_restore):
    """Astra r10 #1 composition: a quiet_offline PASS finalised by the real execute() under an (uncounted) smoke
    binding, copied into the acceptance root, symlinked in, or pointing at a nonexistent binding."""
    from echoear_acceptance_collect import collect
    import shutil
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    (tmp_path/'smoke-run').mkdir()
    smoke_binding = fake_item2_environment(tmp_path/'smoke-run', monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    smoke_out = smoke_binding.parent/'quiet_offline'/'attempt-1'
    assert item2.execute(smoke_binding, 'quiet_offline', smoke_out, restore_wait=lambda: None) == 0
    smoke_record = json.loads((smoke_out/'result.json').read_text())
    assert smoke_record['counted'] is False and smoke_record['purpose'] == 'smoke' and smoke_record['binding_sha256']
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'acc')
    assert collect(plan, binding, digest)['passed'] is True
    shutil.rmtree(root/'quiet_offline'); (root/'quiet_offline.typescript').unlink(missing_ok=True)
    for f in root.glob('quiet_offline/*'):f.unlink()
    write_attempt(root, 'quiet_offline', 1, smoke_record)                                                # copied in
    r = collect(plan, binding, digest)
    q = r['slots']['quiet_offline']['problems']
    assert r['passed'] is False and 'record release_binding is not the acceptance binding' in q and 'record is not a counted acceptance record' in q
    shutil.rmtree(root/'quiet_offline'); (root/'quiet_offline').symlink_to(smoke_out.parent)              # symlinked in
    (smoke_out.parent/'attempt-1.typescript').write_text('EXITED 0\n')
    r = collect(plan, binding, digest)
    assert r['passed'] is False and 'slot directory is a symlink' in r['slots']['quiet_offline']['problems']
    (root/'quiet_offline').unlink()
    ghost = dict(json.loads(json.dumps(prov)), release_binding=str(tmp_path/'nonexistent-binding.json'))
    write_attempt(root, 'quiet_offline', 1, dict(final('quiet_offline', passed=True), **ghost))           # nonexistent binding
    r = collect(plan, binding, digest)
    assert r['passed'] is False and 'record release_binding is not the acceptance binding' in r['slots']['quiet_offline']['problems']


def test_collector_reads_the_whole_attempt_history(tmp_path):
    """Astra r10 #2 on-disk repros, and the accounted-for shapes that must keep passing."""
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    write_attempt(root, 'quiet_live', 2, dict(final('quiet_live', passed=False, interrupted=True, exit_code=1), **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is False and q['slots']['quiet_live']['attempt'] == 2 and 'newest quiet_live attempt has no aggregate' in q['slots']['quiet_live']['problems']
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'two')
    write_attempt(root, 'quiet_offline', 2, None, code=1)
    q = collect(plan, binding, digest)
    assert q['passed'] is False and 'newest attempt-2 has no result.json' in q['slots']['quiet_offline']['problems']
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'three')
    write_attempt(root, 'quiet_offline', 1, dict(final('quiet_offline', passed=False, exit_code=1, runs=[1]), **prov))
    write_attempt(root, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is False and any('earlier attempt-1 (FAIL)' in p for p in q['slots']['quiet_offline']['problems'])
    # An exact prior authorised by a named user decision (path + sha256) is accounted for.
    prior = (tmp_path/'four'/'acceptance'/'quiet_offline'/'attempt-1'/'result.json').resolve()
    fail4 = dict(final('quiet_offline', passed=False, exit_code=1, runs=[1]), out_path=str(prior.parent))   # r14: hashed as written
    plan4, root4, binding4, digest4, prov4 = acceptance_population(tmp_path/'four', binding_overrides=dict(
        authorized_prior_failures={str(prior): hashlib.sha256(json.dumps(fail4).encode()).hexdigest()}, authorized_prior_decision='decision 999 (test)'))
    write_attempt(root4, 'quiet_offline', 1, fail4)
    write_attempt(root4, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov4))
    q = collect(plan4, binding4, digest4)
    assert q['slots']['quiet_offline']['problems'] == [] and q['slots']['quiet_offline']['history'] == [dict(attempt=1, state='authorised-prior')]
    # A clean eligibility deferral before the PASS is accounted for; an unexplained incomplete attempt is not.
    plan5, root5, binding5, digest5, prov5 = acceptance_population(tmp_path/'five')
    write_attempt(root5, 'quiet_offline', 1, clean_item2_deferral('quiet_offline', prov5))
    write_attempt(root5, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov5))
    (root5/'strict_voice_cat'/'attempt-1'/'result.json').unlink()
    write_attempt(root5, 'strict_voice_cat', 2, dict(final('strict_voice_cat', passed=True), **prov5))
    q = collect(plan5, binding5, digest5)
    assert q['slots']['quiet_offline']['problems'] == [] and q['slots']['quiet_offline']['history'] == [dict(attempt=1, state='deferral')]
    assert 'earlier attempt-1 has no result.json (unaccounted)' in q['slots']['strict_voice_cat']['problems']


@pytest.mark.parametrize('shape', ['1+4', '0+5'])
def test_collector_accepts_the_declared_quiet_live_continuations(tmp_path, shape):
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    first = dict(final('quiet_live', passed=False, exit_code=1, runs=[1] if shape == '1+4' else [], quiet_live_aggregate=dict(passed=False)), **prov)
    write_attempt(root, 'quiet_live', 1, first)
    first_path = (root/'quiet_live'/'attempt-1').resolve(); first_sha = hashlib.sha256((first_path/'result.json').read_bytes()).hexdigest()
    cont = dict(final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True), continued_from=[dict(path=str(first_path), result_sha256=first_sha)]), **prov)
    write_attempt(root, 'quiet_live', 2, cont)
    q = collect(plan, binding, digest)
    assert q['passed'] is True and q['slots']['quiet_live']['history'] == [dict(attempt=1, state='continued')]
    bad = dict(cont, continued_from=[dict(path=str(first_path), result_sha256='0'*64)])
    (root/'quiet_live'/'attempt-2'/'result.json').write_text(json.dumps(bad))
    assert collect(plan, binding, digest)['passed'] is False


# ---- r12 (Astra r11 #1, Fable r11 B1/N2/N3) ------------------------------------------------------------------------

def test_failed_restore_deferral_from_real_execute_is_not_a_clean_deferral(tmp_path, monkeypatch, fake_restore):
    """Astra r11 #1: real execute() finalises an eligibility deferral whose first restore failed as exit 1,
    restore_failed, restore.clean false. Copied byte-for-byte before a bound PASS, it must stay unaccounted.
    The same acquisition with a first-attempt clean restore (exit 2) remains a clean deferral."""
    from echoear_acceptance_collect import collect
    trace, state = fake_restore; fake_item2_environment.trace = trace
    for case in ('failed-restore', 'clean-restore'):
        work = tmp_path/case; work.mkdir()
        smoke = fake_item2_environment(work, monkeypatch, stage='quiet_offline',
                                       run_result=dict(passed=False, runs=[], reruns=[], deferred=True, next_original_slot=1))
        import e2e_common as e2
        real = e2.run
        monkeypatch.setattr(e2, 'run', lambda args, live, _r=real: (_r(args, live), 2)[1])
        monkeypatch.setattr(item2, 'validate_deferral', lambda out, result: result.update(deferral_resources=dict(passed=True)) or True)
        monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
        monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
        state['fail'] = 1 if case == 'failed-restore' else 0
        out = smoke.parent/'quiet_offline'/'attempt-1'
        code = item2.execute(smoke, 'quiet_offline', out, restore_wait=lambda: None)
        produced = json.loads((out/'result.json').read_text())
        plan, root, binding, digest, prov = acceptance_population(work/'acc')
        # the prior keeps its production bytes; only the later PASS is a bound acceptance record
        d = root/'quiet_offline'/'attempt-1'; (d/'result.json').write_bytes((out/'result.json').read_bytes())
        (root/'quiet_offline'/'attempt-1.typescript').write_text('EXITED %d\n' % code)
        write_attempt(root, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov))
        # r14 (Fable r13 N1): a byte copy from another root is refused whatever its shape ...
        q = collect(plan, binding, digest)
        assert q['slots']['quiet_offline']['history'] == [dict(attempt=1, state='unaccounted')]
        # ... so model the record as produced in place (its out_path names this attempt); the shape decides.
        (d/'result.json').write_text(json.dumps(dict(produced, out_path=str(d.resolve()))))
        q = collect(plan, binding, digest)
        if case == 'failed-restore':
            assert code == 1 and produced['restore_failed'] is True and produced['deferred'] is True and produced['restore']['clean'] is False
            assert q['passed'] is False and q['slots']['quiet_offline']['history'] == [dict(attempt=1, state='unaccounted')]
        else:
            assert code == 2 and produced['restore']['clean'] is True
            assert q['slots']['quiet_offline']['history'] == [dict(attempt=1, state='deferral')] and q['slots']['quiet_offline']['problems'] == []


@pytest.mark.parametrize('variant', ['error', 'no-resources', 'interrupted', 'exit-1', 'typescript-mismatch', 'restore-retry', 'edited-fail'])
def test_deferral_cleanliness_variants(tmp_path, variant):
    """Fable r11 N2: stronger than the run-time guard. Each defect keeps the prior unaccounted."""
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    prior = clean_item2_deferral('quiet_offline', prov)
    code = None
    if variant == 'error':prior['error'] = 'RuntimeError: boom'
    if variant == 'no-resources':prior.pop('deferral_resources')
    if variant == 'interrupted':prior['interrupted'] = True
    if variant == 'exit-1':prior['exit_code'] = 1
    if variant == 'typescript-mismatch':code = 1
    if variant == 'restore-retry':prior['restore'] = dict(clean=True, attempts=[dict(error='x'), dict(error=None)])
    if variant == 'edited-fail':prior = dict(final('quiet_offline', passed=False, exit_code=1, deferred=True, runs=[]), **prov)   # hand-edited FAIL
    write_attempt(root, 'quiet_offline', 1, prior, code=code)
    write_attempt(root, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is False and q['slots']['quiet_offline']['problems']


def test_item1_pre_touch_rf_deferral_stays_a_clean_deferral(tmp_path):
    from echoear_acceptance_collect import collect, ITEM1_DEFERRAL_ERROR
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    pre = dict(ITEM1, passed=False, deferred=True, scored=False, exit_code=1, error=ITEM1_DEFERRAL_ERROR,
               rf_pre_gate=dict(passed=False), restore=dict(device_untouched=True), **prov)
    write_item1_deferral(root, pre)
    write_attempt(root, 'item1', 2, dict(ITEM1, **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is True and q['slots']['item1']['history'] == [dict(attempt=1, state='deferral')]
    write_attempt(root, 'item1', 1, dict(pre, restore=dict(clean=True, attempts=[{}])))   # touched: not a pre-touch deferral
    assert collect(plan, binding, digest)['slots']['item1']['history'] == [dict(attempt=1, state='unaccounted')]


def write_item1_deferral(root, record):
    d = write_attempt(root, 'item1', 1, record)
    (d/'rf-pre-gate.json').write_text(json.dumps(dict(passed=False)))
    (root/'item1'/'attempt-1.typescript').write_text('Bench acquired\n{"passed": false, "error": "%s"}\nEXITED 1\n' % record['error'])
    return d


@pytest.mark.parametrize('variant', ['no-gate-file', 'gate-passed', 'flashed', 'typescript-without-error'])
def test_item1_deferral_needs_corroboration(tmp_path, variant):
    """Fable r12 N4: an item1 FAIL hand-edited into the deferral shape (both exit 1) is not accounted unless the
    attempt's own in-hold rf-pre-gate.json, the absence of flash.log and the typescript agree."""
    from echoear_acceptance_collect import collect, ITEM1_DEFERRAL_ERROR
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    pre = dict(ITEM1, passed=False, deferred=True, scored=False, exit_code=1, error=ITEM1_DEFERRAL_ERROR,
               rf_pre_gate=dict(passed=False), restore=dict(device_untouched=True), **prov)
    d = write_item1_deferral(root, pre)
    if variant == 'no-gate-file':(d/'rf-pre-gate.json').unlink()
    if variant == 'gate-passed':(d/'rf-pre-gate.json').write_text(json.dumps(dict(passed=True)))
    if variant == 'flashed':(d/'flash.log').write_text('esptool')
    if variant == 'typescript-without-error':(root/'item1'/'attempt-1.typescript').write_text('Autonomous turn 1 completed\nEXITED 1\n')
    write_attempt(root, 'item1', 2, dict(ITEM1, **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is False and q['slots']['item1']['history'] == [dict(attempt=1, state='unaccounted')]


def test_collector_takes_the_root_from_the_binding_only(tmp_path, monkeypatch, fake_restore):
    """Fable r11 B1 repro 1: FAIL in root A under a binding, PASS in a second root B under the same binding.
    The collector reads the binding's output_root (A) and refuses; root B is never collected."""
    from echoear_acceptance_collect import collect
    import shutil
    plan, root_a, binding, digest, prov = acceptance_population(tmp_path, root_name='A')
    write_attempt(root_a, 'quiet_offline', 1, dict(final('quiet_offline', passed=False, exit_code=1, runs=[1]), **prov))
    shutil.copytree(root_a, tmp_path/'B')
    write_attempt(tmp_path/'B', 'quiet_offline', 1, dict(final('quiet_offline', passed=True), **prov))
    r = collect(plan, binding, digest)
    assert r['output_root'] == str(root_a) and r['passed'] is False
    assert r['verdict']['stages']['quiet_offline']['passed'] is False


def test_orphan_typescript_and_gaps_are_problems(tmp_path):
    """Fable r11 B1 repro 2: a deleted FAIL directory leaves its typescript (EXITED 1); the copied PASS as attempt-2
    must not collect. Gaps and orphan identity directories are problems too."""
    from echoear_acceptance_collect import collect
    import shutil
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    write_attempt(root, 'quiet_offline', 1, dict(final('quiet_offline', passed=False, exit_code=1, runs=[1]), **prov))
    shutil.rmtree(root/'quiet_offline'/'attempt-1')
    write_attempt(root, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov))
    q = collect(plan, binding, digest)['slots']['quiet_offline']['problems']
    assert 'attempt-1 has no output directory (orphan typescript)' in q
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'gap')
    write_attempt(root, 'strict_voice_cat', 3, dict(final('strict_voice_cat', passed=True), **prov))
    q = collect(plan, binding, digest)['slots']['strict_voice_cat']['problems']
    assert any('not contiguous' in p for p in q)
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'ident')
    (root/'quiet_live'/'attempt-2-identity').mkdir()
    q = collect(plan, binding, digest)['slots']['quiet_live']['problems']
    assert 'attempt-2 has no output directory (orphan identity directory)' in q

def test_long_nonforward_run_fails_closed_even_when_shallow():
    rows = clean_stream(300)                                        # seq 0..299 to 5.98 s
    rows += [dict(pc_id='p', ssrc=1, seq=200+i, timestamp=(200+i)*960, observed_ms=6000+20*i) for i in range(100)]
    rows += [dict(pc_id='p', ssrc=1, seq=300+i, timestamp=(300+i)*960, observed_ms=8000+20*i) for i in range(200)]
    r = uc.link_tail(rows, 1, 11)
    assert r['deepest_reorder'] > -256 and r['longest_nonforward_run'] == 100 and r['reset_suspected'] is True and r['passed'] is False



def test_deep_single_reorder_trips_the_depth_guard_and_real_straggler_batches_do_not():
    """r11 (Fable r10 N1): depth bound 2048; a straggler batch like the real 238-deep EchoEar one stays clear."""
    rows = clean_stream(5000)
    rows.append(dict(pc_id='p', ssrc=1, seq=4999-3000, timestamp=(4999-3000)*960, observed_ms=100010))
    rows += [dict(pc_id='p', ssrc=1, seq=5000+i, timestamp=(5000+i)*960, observed_ms=100020+20*i) for i in range(100)]
    r = uc.link_tail(rows, 1, 101)
    assert r['deepest_reorder'] == -3000 and r['reset_suspected'] is True and r['passed'] is False
    rows = clean_stream(5000)
    rows.append(dict(pc_id='p', ssrc=1, seq=4999-238, timestamp=(4999-238)*960, observed_ms=100010))
    rows += [dict(pc_id='p', ssrc=1, seq=5000+i, timestamp=(5000+i)*960, observed_ms=100020+20*i) for i in range(100)]
    r = uc.link_tail(rows, 1, 101)
    assert r['deepest_reorder'] == -238 and r['reset_suspected'] is False and r['passed'] is True


def test_nonforward_run_outside_the_window_does_not_count():
    rows = clean_stream(300)
    rows += [dict(pc_id='p', ssrc=1, seq=200+i, timestamp=(200+i)*960, observed_ms=6000+20*i) for i in range(100)]
    r = uc.link_tail(rows, 1, 5.9)
    assert r['longest_nonforward_run'] == 0 and r['reset_suspected'] is False


# ---- r13 (Astra r12 B1/B2, Fable r12 B1/N1/N2/N4) ------------------------------------------------------------------

def test_astra_r12_smoke_segment_copied_into_acceptance_cannot_be_continued_or_collected(tmp_path, monkeypatch, fake_restore):
    """Astra r12 B1 exact repro: a real execute() quiet_live segment under the SMOKE binding (scored original 1,
    unscored setup failure at 2; counted false, purpose smoke) is copied byte-for-byte into an acceptance slot as
    attempt-1. Continuing it under the acceptance binding is refused before the lock; a continuation record that
    names it is not collected."""
    import shutil
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_live', run_result=seg([run_row(1), run_row(2, scored=False)]))
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    monkeypatch.setattr(item2, 'segment_observation', lambda plan, seg, text, current=False, **k: dict(passed=True))
    smoke_out = binding.parent/'quiet_live'/'attempt-1'
    assert item2.execute(binding, 'quiet_live', smoke_out, restore_wait=lambda: None) == 1
    smoke = json.loads((smoke_out/'result.json').read_text())
    assert smoke['counted'] is False and smoke['purpose'] == 'smoke' and smoke['stage'] == 'quiet_live'
    # Run time: copied into an acceptance-bound slot and continued.
    plan, root, acc, digest, prov = acceptance_population(tmp_path/'acc')
    shutil.rmtree(root/'quiet_live'); dest = root/'quiet_live'/'attempt-1'; shutil.copytree(smoke_out, dest)
    (root/'quiet_live'/'attempt-1.typescript').write_text('EXITED 1\n')
    before = (dest/'result.json').read_bytes(); del trace[:]
    real = item2.verified_binding   # r14: the acceptance binding declares its own output_root
    monkeypatch.setattr(item2, 'verified_binding', lambda p: (lambda b, i: (dict(b, output_root=str(root)), i))(*real(p)))
    with pytest.raises(RuntimeError, match='not a segment of this binding') as refused:
        item2.execute(acc, 'quiet_live', dest.with_name('attempt-2'), continue_from=[dest], restore_wait=lambda: None)
    assert 'release_binding' in str(refused.value) and 'counted' in str(refused.value) and 'purpose' in str(refused.value)
    assert 'lock-enter' not in trace and not dest.with_name('attempt-2-identity').exists() and (dest/'result.json').read_bytes() == before
    # Collection: a continuation record naming the copied bytes is still not counted.
    cont = dict(final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True),
                      continued_from=[dict(path=str(dest.resolve()), result_sha256=hashlib.sha256(before).hexdigest())]), **prov)
    write_attempt(root, 'quiet_live', 2, cont)
    q = collect(plan, acc, digest)
    assert q['passed'] is False and q['slots']['quiet_live']['history'] == [dict(attempt=1, state='unaccounted')]
    assert any(p.startswith('continued attempt-1: continuation release_binding') for p in q['slots']['quiet_live']['problems'])


@pytest.mark.parametrize('defect', ['stage', 'image', 'counted', 'sha', 'outside-slot', 'symlink'])
def test_continuation_problems_each_identity_field(tmp_path, defect):
    stage_root = tmp_path/'root'/'quiet_live'; d = stage_root/'attempt-1'; d.mkdir(parents=True)
    prov = dict(release_binding='/b.json', binding_sha256='a'*64, counted=True, purpose='acceptance')
    record = dict(stage='quiet_live', normal_elf_sha256=item2.NORMAL, out_path=str(d.resolve()), **prov)
    path = d/'result.json'; path.write_text(json.dumps(record))
    if defect == 'stage':record['stage'] = 'quiet_offline'
    if defect == 'image':record['normal_elf_sha256'] = '0'*64
    if defect == 'counted':record.update(counted=False)
    if defect == 'sha':record.update(binding_sha256='b'*64)
    if defect == 'outside-slot':
        path = tmp_path/'elsewhere'/'attempt-1'/'result.json'; path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    if defect == 'symlink':
        link = stage_root/'attempt-2'; link.symlink_to(d); path = link/'result.json'
    if defect in ('outside-slot', 'symlink'):record = json.loads((d/'result.json').read_text())   # identity intact
    assert item2.continuation_problems(dict(stage='quiet_live', normal_elf_sha256=item2.NORMAL, out_path=str(d.resolve()), **prov), d/'result.json', stage_root, prov) == []
    assert item2.continuation_problems(record, path, stage_root, prov)


@pytest.mark.parametrize('alias', ['attempt-01', 'attempt-0', 'attempt-001-identity', 'attempt-01.typescript'])
def test_fable_r12_attempt_number_aliases_are_refused(tmp_path, alias):
    """Fable r12 B1: attempt-1 (FAIL, EXITED 1) and attempt-01 (PASS) used to collapse onto one number with the
    winner chosen by readdir order. Only canonical attempt-<n> names count; any other spelling is a problem."""
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    write_attempt(root, 'quiet_offline', 1, dict(final('quiet_offline', passed=False, exit_code=1, runs=[1]), **prov))
    target = root/'quiet_offline'/alias
    if alias.endswith('.typescript'):target.write_text('EXITED 0\n')
    else:
        target.mkdir()
        if '-identity' not in alias:(target/'result.json').write_text(json.dumps(dict(final('quiet_offline', passed=True), **prov)))
    q = collect(plan, binding, digest)
    assert q['passed'] is False
    assert any('non-canonical attempt name' in p and alias in p for p in q['slots']['quiet_offline']['problems'])
    # The canonical FAIL stays the newest attempt whatever the listing order.
    assert q['slots']['quiet_offline']['attempt'] == 1


def test_failed_restore_deferral_is_not_replayable_at_run_time(tmp_path, monkeypatch, fake_restore):
    """Fable r12 N2: the run-time replay guard now applies the collector's clean-deferral test."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    prior = binding.parent/'quiet_offline'/'attempt-1'; prior.mkdir(parents=True)
    clean = dict(passed=False, deferred=True, runs=[], points=[], deferral_resources=dict(passed=True), exit_code=2, error=None,
                 retry_after_host_s=0.0, restore=dict(clean=True, attempts=[dict(label='restore-1', error=None)]))
    for bad in (dict(restore_failed=True, exit_code=1, restore=dict(clean=False, attempts=[{}, {}])), dict(interrupted=True),
                dict(error='RuntimeError: boom'), dict(exit_code=1)):
        (prior/'result.json').write_text(json.dumps(dict(clean, **bad)))
        with pytest.raises(RuntimeError, match='not clean'):
            item2.refuse_replayed_population(prior.parent)
    (prior/'result.json').write_text(json.dumps(clean))
    item2.refuse_replayed_population(prior.parent)   # a clean deferral remains replayable after its wait


def test_item2_bot_rtp_cap_raised_for_every_stage(tmp_path, monkeypatch, fake_restore):
    """Fable r12 N1: execute() sets RTP_EVIDENCE_MAX_PACKETS=60000 before any stage's bot starts."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    seen = {}
    real = e2e_common.run
    def spy(args, live): seen['cap'] = os.environ.get('RTP_EVIDENCE_MAX_PACKETS'); return real(args, live)
    monkeypatch.setattr(e2e_common, 'run', spy)
    monkeypatch.delenv('RTP_EVIDENCE_MAX_PACKETS', raising=False)
    item2.execute(binding, 'quiet_offline', binding.parent/'quiet_offline'/'attempt-1', restore_wait=lambda: None)
    assert seen['cap'] == '60000' == item2.RTP_MAX_PACKETS


def test_rf_window_end_without_a_following_packet_is_uncovered():
    """The 20261027T184859Z session-1 mechanism: the last packet 12.7 ms before the window end and none after
    (the uplink stopped inside one packet period) gives covered false / passed null; packets past the end (the
    r13 stop margin) cover it."""
    from echoear_rf_stream_precheck import precheck_verdict
    rows = clean_stream(14000)   # last packet at 279.98 s
    cut = precheck_verdict(rows, 0.5, 279.9927)
    assert cut['covered'] is False and cut['link_tail_passed'] is None
    margin = precheck_verdict(rows, 0.5, 277.9927)
    assert margin['covered'] is True and margin['link_tail_passed'] is True


# ---- r14 (Fable r13 N1/N3) ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('where', ['other-root', 'wrong-slot', 'leading-zero', 'not-attempt', 'no-output-root'])
def test_item2_out_must_be_the_binding_slot_before_the_lock(tmp_path, monkeypatch, fake_restore, where):
    """Fable r13 N1: execute() refuses any --out other than <output_root>/<stage>/attempt-<n>, before the lock."""
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    out = dict(**{'other-root': tmp_path/'B'/'quiet_offline'/'attempt-1', 'wrong-slot': binding.parent/'strict_voice_cat'/'attempt-1',
                  'leading-zero': binding.parent/'quiet_offline'/'attempt-01', 'not-attempt': binding.parent/'quiet_offline'/'run-1',
                  'no-output-root': binding.parent/'quiet_offline'/'attempt-1'})[where]
    if where == 'no-output-root':
        real = item2.verified_binding
        monkeypatch.setattr(item2, 'verified_binding', lambda p: (lambda b, i: ({k: v for k, v in b.items() if k != 'output_root'}, i))(*real(p)))
    with pytest.raises(RuntimeError, match='output_root'):
        item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None)
    assert trace == [] and not out.exists() and not out.with_name(out.name+'-identity').exists()


def test_item2_records_its_resolved_out_path(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_offline')
    monkeypatch.setattr(profile, 'session_gate_text', lambda texts, p, **k: dict(passed=True))
    monkeypatch.setattr(profile, 'measurement_volume_gate', lambda r, st, **k: dict(passed=True))
    out = binding.parent/'quiet_offline'/'x'/'..'/'attempt-1'
    item2.execute(binding, 'quiet_offline', out, restore_wait=lambda: None)
    r = json.loads((binding.parent/'quiet_offline'/'attempt-1'/'result.json').read_text())
    assert r['out_path'] == str((binding.parent/'quiet_offline'/'attempt-1').resolve())


def test_item1_out_must_be_the_binding_slot_before_the_lock(tmp_path, monkeypatch, fake_restore):
    trace, _ = fake_restore
    monkeypatch.setattr(item1, 'EVIDENCE', tmp_path)
    monkeypatch.setattr(item1, 'validate_image', lambda image: (dict(commit=item1.CANDIDATE), dict(write_flash_args=[], flash_files={})))
    binding = item1_binding(monkeypatch, tmp_path)
    for out in (tmp_path/'B'/'item1'/'attempt-1', tmp_path/'item1', tmp_path/'item1'/'attempt-01', tmp_path/'quiet_live'/'attempt-1'):
        with pytest.raises(SystemExit) as stop:
            item1.main(['--binding', str(binding), '--image', str(tmp_path/'demo'), '--out', str(out)], restore_wait=lambda: None)
        assert stop.value.code == 2 and not out.exists()
    assert 'lock-enter' not in trace


def test_fable_r13_same_binding_record_from_another_root_is_not_collected(tmp_path):
    """Fable r13 N1: a record made under the SAME acceptance binding in another root (its out_path names that root),
    copied into the declared root, is refused as the newest record and as an earlier deferral."""
    from echoear_acceptance_collect import collect
    plan, root, binding, digest, prov = acceptance_population(tmp_path)
    elsewhere = (tmp_path/'B'/'quiet_offline'/'attempt-1').resolve()
    write_attempt(root, 'quiet_offline', 1, dict(final('quiet_offline', passed=True), out_path=str(elsewhere), **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is False and any(p.startswith('record out_path') for p in q['slots']['quiet_offline']['problems'])
    plan, root, binding, digest, prov = acceptance_population(tmp_path/'two')
    write_attempt(root, 'quiet_offline', 1, clean_item2_deferral('quiet_offline', prov, out_path=str(elsewhere)))
    write_attempt(root, 'quiet_offline', 2, dict(final('quiet_offline', passed=True), **prov))
    q = collect(plan, binding, digest)
    assert q['passed'] is False and q['slots']['quiet_offline']['history'] == [dict(attempt=1, state='unaccounted')]


def test_fable_r13_same_binding_segment_from_another_root_cannot_be_continued(tmp_path, monkeypatch, fake_restore):
    """Fable r13 N1, continuation source: a same-binding quiet_live segment whose out_path names another root, copied
    into the slot, is refused by execute() before the lock and is not collected as `continued`."""
    from echoear_acceptance_collect import collect
    trace, _ = fake_restore; fake_item2_environment.trace = trace
    binding = fake_item2_environment(tmp_path, monkeypatch, stage='quiet_live')
    prior = binding.parent/'quiet_live'/'attempt-1'; prior.mkdir(parents=True)
    (prior/'result.json').write_text(json.dumps(segment_of(binding, seg([run_row(1), run_row(2, scored=False)]), tmp_path/'B'/'quiet_live'/'attempt-1')))
    (prior/'serial.log').write_text('')
    with pytest.raises(RuntimeError, match='out_path'):
        item2.execute(binding, 'quiet_live', prior.with_name('attempt-2'), continue_from=[prior], restore_wait=lambda: None)
    assert trace == [] and not prior.with_name('attempt-2-identity').exists()
    plan, root, acc, digest, prov = acceptance_population(tmp_path/'acc')
    import shutil
    shutil.rmtree(root/'quiet_live')
    first = dict(final('quiet_live', passed=False, exit_code=1, runs=[1], quiet_live_aggregate=dict(passed=False)),
                 out_path=str((tmp_path/'B'/'quiet_live'/'attempt-1').resolve()), **prov)
    write_attempt(root, 'quiet_live', 1, first)
    first_path = (root/'quiet_live'/'attempt-1').resolve()
    cont = dict(final('quiet_live', passed=True, quiet_live_aggregate=dict(passed=True), continued_from=[dict(
        path=str(first_path), result_sha256=hashlib.sha256((first_path/'result.json').read_bytes()).hexdigest())]), **prov)
    write_attempt(root, 'quiet_live', 2, cont)
    q = collect(plan, acc, digest)
    assert q['passed'] is False and q['slots']['quiet_live']['history'] == [dict(attempt=1, state='unaccounted')]
    assert any('continuation out_path' in p for p in q['slots']['quiet_live']['problems'])


def test_rf_precheck_records_gaps_and_session_lifetimes(tmp_path, monkeypatch, fake_restore):
    """Fable r13 N3: inter-window gaps (window clock) and per-session setup/lifetime/idle margin are recorded."""
    rows, window = clean_stream(14000), (0.5, 279.0)
    rfp, trace = rf_fakes(tmp_path, monkeypatch, fake_restore, rows=rows, window=window)
    out = tmp_path/'20261027T170000Z'
    assert rfp.execute(tmp_path/'binding.json', out, seconds=270, restore_wait=lambda: None) == 0
    r = json.loads((out/'rf-precheck.json').read_text())
    w1, w2 = (s['window'] for s in r['sessions'])
    assert r['inter_window_gaps_s'] == [w2['start_s']-w1['end_s']] and r['inter_window_gaps_s'][0] > 0
    for s in r['sessions']:
        assert 0 <= s['setup_s'] <= s['lifetime_s'] and s['idle_timeout_margin_s'] == rfp.BOT_IDLE_TIMEOUT_SECONDS-s['lifetime_s']
