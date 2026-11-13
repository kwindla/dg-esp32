"""Gap 3 restore-only verification: faked flash/boot/lock; nothing touches a device."""
import contextlib
import json
from pathlib import Path

import pytest

import echoear_aec_bench as bench_mod
import echoear_restore_verify as rv
import step9a_echoear

H = bench_mod.NORMAL_HASH
GOOD = ['rst:0x15 (USB_UART_CHIP_RESET),boot:0xb (SPI_FAST_FLASH_BOOT)', 'I (24) boot: ESP-IDF v6.1 2nd stage bootloader',
        'I (1342) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+H, 'READY transport-console']


def fakes(monkeypatch, tmp_path, *, logs=None, flash_errors=(), boot_errors=()):
    """logs: serial-log line lists per boot attempt; *_errors: exceptions raised by successive attempts."""
    trace, logs = [], list(logs or [GOOD])
    flash_errors, boot_errors = list(flash_errors), list(boot_errors)
    image = tmp_path/'bundle'; image.mkdir(); (image/'manifest.json').write_text('{}')
    monkeypatch.setattr(bench_mod, 'NORMAL', image)
    monkeypatch.setattr(rv, 'EVIDENCE', tmp_path)
    @contextlib.contextmanager
    def bench():
        trace.append('lock-enter')
        try:yield
        finally:trace.append('lock-exit')
    monkeypatch.setattr(step9a_echoear, 'bench', bench)
    monkeypatch.setattr(step9a_echoear, 'check_esptool', lambda: None)
    monkeypatch.setattr(bench_mod, 'verify_bundle', lambda image, expected: (trace.append('verify'), {'dg-esp32.bin': 'h'})[1])
    def flash(image, expected, out, label):
        trace.append('flash:'+label)
        if flash_errors and (e := flash_errors.pop(0)):raise e
    class Board:
        def close(self):trace.append('serial-close')
    def boot(out, label, expected):
        trace.append('boot:'+label)
        (out/(label+'-serial.log')).write_text('\n'.join(logs.pop(0) if logs else GOOD)+'\n')
        if boot_errors and (e := boot_errors.pop(0)):raise e
        return Board()
    monkeypatch.setattr(bench_mod, 'flash', flash)
    monkeypatch.setattr(bench_mod, 'boot', boot)
    return trace


def retry(trace):
    return lambda: trace.append('wait-retry')


def test_restore_verify_flashes_and_boot_verifies_the_restore_of_record(monkeypatch, tmp_path):
    trace = fakes(monkeypatch, tmp_path)
    out = tmp_path/'20261028T080000Z'
    assert rv.execute(out, wait=retry(trace)) == 0
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] and r['expected_elf_sha256'].startswith('55152f25') and r['restore']['clean'] is True
    assert r['boot_proof']['boot_records'] == [H] and r['lock_released'] is True
    assert trace == ['lock-enter', 'verify', 'flash:restore-1', 'boot:restore-1', 'serial-close', 'lock-exit']
    with pytest.raises(FileExistsError):rv.execute(out)
    with pytest.raises(ValueError, match='under'):rv.execute(Path('/tmp/elsewhere-gap3'))


@pytest.mark.parametrize('where', ['flash', 'boot'])
def test_a_failed_attempt_keeps_the_lock_until_a_verified_restore(monkeypatch, tmp_path, where):
    errors = {where+'_errors': [RuntimeError('partial flash') if where == 'flash' else TimeoutError('no boot line')]}
    trace = fakes(monkeypatch, tmp_path, **errors)
    out = tmp_path/'t'
    assert rv.execute(out, wait=retry(trace)) == 1   # verified on retry, but not a clean first attempt
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is False and r['restore']['clean'] is False and r['restore']['restored_elf_sha256'] == H
    assert trace.index('wait-retry') < trace.index('lock-exit') and trace[-1] == 'lock-exit'
    assert trace[trace.index('wait-retry')+1:] == ['flash:restore-2', 'boot:restore-2', 'serial-close', 'lock-exit']


def test_interrupt_during_flash_still_restores_before_unlock(monkeypatch, tmp_path):
    trace = fakes(monkeypatch, tmp_path, flash_errors=[KeyboardInterrupt()])
    out = tmp_path/'t'
    rv.execute(out, wait=retry(trace))
    r = json.loads((out/'result.json').read_text())
    assert r['restore']['restored_elf_sha256'] == H and r['restore']['signals_during_restore'] == ['KeyboardInterrupt']
    assert trace[-3:] == ['boot:restore-1', 'serial-close', 'lock-exit'] and r['lock_released'] is True


def test_pre_touch_bundle_failure_releases_without_touching(monkeypatch, tmp_path):
    trace = fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(bench_mod, 'verify_bundle', lambda image, expected: (_ for _ in ()).throw(ValueError('bad bundle')))
    out = tmp_path/'t'
    with pytest.raises(ValueError):rv.execute(out)
    r = json.loads((out/'result.json').read_text())
    assert r['passed'] is False and 'restore' not in r and trace == ['lock-enter', 'lock-exit']


def test_missing_manifest_is_refused_before_the_lock(monkeypatch, tmp_path):
    trace = fakes(monkeypatch, tmp_path)
    (bench_mod.NORMAL/'manifest.json').unlink()
    with pytest.raises(FileNotFoundError):rv.execute(tmp_path/'t')
    assert trace == []


@pytest.mark.parametrize('log, problem', [
    (GOOD[:2]+['I (1342) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+H+'0', GOOD[3]], 'exactly one'),     # extra suffix
    (GOOD[:2]+['W (9) diag: saw elf_sha256='+H, GOOD[3]], 'exactly one'),                                  # non-boot text
    (GOOD[:3]+['I (2000) dg-esp32: other elf_sha256='+'0'*64, GOOD[3]], 'exactly one'),                   # conflicting
    (GOOD[:3], 'READY'),                                                                                  # no READY
    (['READY transport-console']+GOOD[:3], 'READY'),                                                      # READY before boot
    (GOOD[2:], 'reset'),                                                                                  # no reset
    (GOOD+GOOD[:2], 'exactly one'),                                                                       # stale record before last reset
])
def test_boot_proof_requires_an_exact_reset_scoped_witness(tmp_path, log, problem):
    p = tmp_path/'s.log'; p.write_text('\n'.join(log)+'\n')
    proof = rv.boot_proof(p, H)
    assert proof['passed'] is False and problem in proof['problem']
    p.write_text('\n'.join(GOOD)+'\n'); assert rv.boot_proof(p, H)['passed'] is True


@pytest.mark.parametrize('bad', ['W (9) diag: saw elf_sha256='+H,
                                 'I (1342) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+H+'0',
                                 'I (1342) dg-esp32: x elf_sha256='+'0'*64+' elf_sha256='+H])
def test_a_malformed_boot_witness_keeps_the_lock_and_retries(monkeypatch, tmp_path, bad):
    trace = fakes(monkeypatch, tmp_path, logs=[GOOD[:2]+[bad, GOOD[3]], GOOD])
    out = tmp_path/'t'
    assert rv.execute(out, wait=retry(trace)) == 1   # proven only on the retry: not a clean first attempt
    r = json.loads((out/'result.json').read_text())
    assert r['boot_proofs']['restore-1']['passed'] is False and r['boot_proof']['passed'] is True
    assert trace.index('wait-retry') < trace.index('lock-exit')
    assert trace[trace.index('wait-retry')+1:] == ['flash:restore-2', 'boot:restore-2', 'serial-close', 'lock-exit']


def test_two_digest_fields_on_one_line_never_pass(tmp_path):
    p = tmp_path/'s.log'
    p.write_text('\n'.join(GOOD[:2]+['I (1342) dg-esp32: x elf_sha256='+'0'*64+' elf_sha256='+H, GOOD[3]])+'\n')
    assert rv.boot_proof(p, H)['passed'] is False


def test_an_interleaved_boot_record_is_accepted(tmp_path):
    p = tmp_path/'s.log'
    line = 'I (119) esp_image: segment 1: paddr=00094204 vaddr=3fca0200 sizeI (1382) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+H
    p.write_text('\n'.join(GOOD[:2]+[line, GOOD[3]])+'\n')
    assert rv.boot_proof(p, H)['passed'] is True


def test_a_failure_journal_write_error_does_not_release_the_lock(monkeypatch, tmp_path):
    trace = fakes(monkeypatch, tmp_path, boot_errors=[TimeoutError('no boot line')])
    out = tmp_path/'t'
    real = Path.write_text
    def write_text(self, *a, **k):
        if self.name == 'restore.json' and 'wait-retry' not in trace:raise OSError('disk full')
        return real(self, *a, **k)
    monkeypatch.setattr(Path, 'write_text', write_text)
    assert rv.execute(out, wait=retry(trace)) == 1
    assert trace.index('wait-retry') < trace.index('lock-exit') and trace[-1] == 'lock-exit'


def test_an_embedded_record_in_a_diagnostic_line_never_passes(tmp_path):
    p = tmp_path/'s.log'
    p.write_text('\n'.join(GOOD[:2]+['W (9) diag: previously observed '+GOOD[2], GOOD[3]])+'\n')
    assert rv.boot_proof(p, H)['passed'] is False


@pytest.mark.parametrize('broken', ['RESTORE JOURNAL', 'RESTORE FAILED', 'CLEANUP FAILED'])
def test_a_failing_diagnostic_output_never_releases_the_lock(monkeypatch, tmp_path, broken):
    import builtins, echoear_rollback, echoear_item2_campaign as item2
    trace = fakes(monkeypatch, tmp_path, boot_errors=[TimeoutError('no boot line')])
    real_print = builtins.print
    def print_(*a, **k):
        if a and str(a[0]).startswith(broken):raise BrokenPipeError('closed')
        return real_print(*a, **k)
    monkeypatch.setattr(builtins, 'print', print_)
    if broken == 'RESTORE JOURNAL':
        real = Path.write_text
        monkeypatch.setattr(Path, 'write_text', lambda self, *a, **k: (_ for _ in ()).throw(OSError('disk full'))
                            if self.name == 'restore.json' and 'wait-retry' not in trace else real(self, *a, **k))
    if broken == 'CLEANUP FAILED':   # the default SIGUSR1 wait: retry immediately in the test
        monkeypatch.setattr(echoear_rollback.time, 'sleep', lambda s: setattr(holder, 'retry', True))
        holder = None
        def wait_recovery(h):
            nonlocal holder; holder = h; trace.append('wait-retry'); orig(h)
        orig = echoear_rollback.Live.wait_recovery
        monkeypatch.setattr(echoear_rollback.Live, 'wait_recovery', staticmethod(wait_recovery))
        assert rv.execute(tmp_path/'t') == 1
    else:
        assert rv.execute(tmp_path/'t', wait=retry(trace)) == 1
    assert trace.index('wait-retry') < trace.index('lock-exit') and trace[-1] == 'lock-exit'


def test_a_failing_custom_wait_falls_back_to_the_retained_lock_wait(monkeypatch, tmp_path):
    import echoear_rollback
    trace = fakes(monkeypatch, tmp_path, boot_errors=[TimeoutError('no boot line')])
    def bad_wait():trace.append('bad-wait'); raise RuntimeError('wait broke')
    monkeypatch.setattr(echoear_rollback.Live, 'wait_recovery', staticmethod(lambda h: trace.append('fallback-wait')))
    assert rv.execute(tmp_path/'t', wait=bad_wait) == 1
    assert trace.index('bad-wait') < trace.index('fallback-wait') < trace.index('lock-exit')


@pytest.mark.parametrize('prefix', ['I (9) dg-esp32: previously observed ',
                                    'I (119) esp_image: segment 1: paddr=00094204 vaddr=3fca0200 size W (9) diag: previously observed '])
def test_diagnostic_text_swallowed_by_the_grammar_never_passes(tmp_path, prefix):
    p = tmp_path/'s.log'
    p.write_text('\n'.join(GOOD[:2]+[prefix+GOOD[2], GOOD[3]])+'\n')
    assert rv.boot_proof(p, H)['passed'] is False
