import base64
import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import console_rate as console
import restore_factory as restore


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = b'factory-test' * 128
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.size_patch = patch.object(restore, 'SIZE', len(self.raw)); self.size_patch.start()
        self.sha_patch = patch.object(restore, 'SHA', self.sha); self.sha_patch.start()
        self.addCleanup(self.size_patch.stop); self.addCleanup(self.sha_patch.stop)
        for name in ('factory-a.bin', 'factory-b.bin'): (self.root / name).write_bytes(self.raw)
        self.manifest = ''.join(f'{self.sha}  {name}\n{name} {len(self.raw)}\n' for name in ('factory-a.bin', 'factory-b.bin'))
        (self.root / 'sha256.txt').write_text(self.manifest)

    def test_valid_pair_and_commands(self):
        path = restore.verify_backup(self.root)
        self.assertEqual(path.name, 'factory-a.bin')
        write, verify = restore.commands(path)
        self.assertIn(restore.PORT, write)
        self.assertIn('esp32p4', write)
        self.assertEqual(write[-8:], ['--flash-mode', 'keep', '--flash-freq', 'keep', '--flash-size', 'keep', '0x0', str(path)])
        self.assertEqual(verify[-3:], ['verify-flash', '0x0', str(path)])
        self.assertNotIn('--verify', write)

    def test_dry_run_never_executes_or_opens_serial(self):
        with patch.object(restore, 'checked_esptool_python', return_value='/idf/python'), \
             patch.object(restore.subprocess, 'run', side_effect=AssertionError('exec')), \
             patch.object(restore, 'wait_factory', side_effect=AssertionError('serial')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            restore.main(['--backup', str(self.root), '--dry-run'])
        self.assertIn('verify-flash', output.getvalue())
        self.assertIn('hard-reset', output.getvalue())

    def test_wrong_port(self):
        for port in ('/dev/ttyUSB0', '/dev/ttyACM0', restore.PORT + '-other'):
            with self.subTest(port=port), self.assertRaises(restore.argparse.ArgumentTypeError):
                restore.commands(self.root / 'factory-a.bin', port)

    def test_wrong_cli_port(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            restore.main(['--backup', str(self.root), '--port', '/dev/ttyUSB0', '--dry-run'])
        self.assertEqual(caught.exception.code, 2)

    def test_bad_size(self):
        (self.root / 'factory-b.bin').write_bytes(self.raw[:-1])
        with self.assertRaisesRegex(ValueError, 'size'): restore.verify_backup(self.root)

    def test_same_size_corruption(self):
        (self.root / 'factory-b.bin').write_bytes(b'x' * len(self.raw))
        with self.assertRaisesRegex(ValueError, 'mismatch'): restore.verify_backup(self.root)

    def test_missing_hash(self):
        (self.root / 'sha256.txt').write_text(f'{self.sha}  factory-a.bin\n')
        with self.assertRaisesRegex(ValueError, 'Both'): restore.verify_backup(self.root)

    def test_missing_recorded_size(self):
        (self.root / 'sha256.txt').write_text(''.join(f'{self.sha}  {name}\n' for name in ('factory-a.bin', 'factory-b.bin')))
        with self.assertRaisesRegex(ValueError, 'sizes'): restore.verify_backup(self.root)

    def test_duplicate_hash(self):
        (self.root / 'sha256.txt').write_text(self.manifest + f'{self.sha}  factory-a.bin\n')
        with self.assertRaisesRegex(ValueError, 'duplicate'): restore.verify_backup(self.root)

    def test_changed_pinned_hash(self):
        (self.root / 'sha256.txt').write_text(self.manifest.replace(self.sha, '0' * 64))
        with self.assertRaisesRegex(ValueError, 'pinned'): restore.verify_backup(self.root)

    def test_bad_recorded_size(self):
        (self.root / 'sha256.txt').write_text(self.manifest.replace(str(len(self.raw)), '1'))
        with self.assertRaisesRegex(ValueError, 'size'): restore.verify_backup(self.root)

    def test_path_in_manifest(self):
        (self.root / 'sha256.txt').write_text(self.manifest.replace('factory-a.bin', '../factory-a.bin'))
        with self.assertRaises(ValueError): restore.verify_backup(self.root)

    def test_symlink(self):
        path = self.root / 'factory-b.bin'; path.unlink(); path.symlink_to(self.root / 'factory-a.bin')
        with self.assertRaisesRegex(ValueError, 'symlink'): restore.verify_backup(self.root)


class LineTests(unittest.TestCase):
    def line(self, seq, payload=b'hello', prefix=b'F'):
        return prefix + f' {seq} {console.zlib.crc32(payload):08x} '.encode() + payload

    def test_clean(self):
        v = console.Verifier()
        for i in range(3): v.feed(self.line(i))
        self.assertEqual(v.result(3)['missing_or_corrupt'], 0)
        self.assertEqual(v.result(3)['corrupt_lines'], 0)

    def test_dropped_leading_middle_and_trailing(self):
        v = console.Verifier()
        v.feed(self.line(1)); v.feed(self.line(3))
        self.assertEqual(v.result(5)['lost_lines'], 3)

    def test_corrupt_payload(self):
        v = console.Verifier()
        v.feed(self.line(0)[:-1] + b'X')
        self.assertEqual(v.result(1)['corrupt_lines'], 1)
        self.assertEqual(v.result(1)['lost_lines'], 0)
        self.assertEqual(v.result(1)['missing_or_corrupt'], 1)

    def test_truncated_and_malformed(self):
        v = console.Verifier()
        v.feed(b'F truncated'); v.feed(b'F 0 badcrc data')
        self.assertEqual(v.result(1)['corrupt_lines'], 2)

    def test_duplicate_reordered_unexpected(self):
        v = console.Verifier()
        for i in (1, 0, 0, 4): v.feed(self.line(i))
        r = v.result(2)
        self.assertEqual((r['duplicates'], r['reordered'], r['unexpected_sequences']), (1, 1, 1))

    def test_interleaved_status_ignored(self):
        v = console.Verifier()
        v.feed(b'LOOP END reads=6000 errors=0'); v.feed(b'FLOOD BEGIN')
        v.feed(self.line(0)); self.assertEqual(v.result(1)['corrupt_lines'], 0)

    def test_ring_drop_exposes_tail(self):
        v = console.Verifier(b'R'); v.feed(self.line(0, prefix=b'R'))
        self.assertEqual(v.result(4)['lost_lines'], 3)

    def test_dump_crc_and_decoding(self):
        raw = bytes(range(256)) * 5
        line = self.line(0, base64.b64encode(raw), b'DUMP')
        self.assertEqual(console.decode_dump_line(line), (0, raw))
        v = console.Verifier(b'DUMP'); v.feed(line)
        self.assertEqual(v.result(1)['pcm_bytes'], 1280)
        self.assertEqual(v.result(1)['missing_or_corrupt'], 0)

    def test_dump_crc_failure(self):
        line = self.line(0, base64.b64encode(b'12345678'), b'DUMP')
        with self.assertRaises(ValueError): console.decode_dump_line(line[:-1] + b'X')

    def test_dump_bad_base64_or_partial_frame(self):
        for payload in (b'!!!!', base64.b64encode(b'123')):
            v = console.Verifier(b'DUMP'); v.feed(self.line(0, payload, b'DUMP'))
            self.assertEqual(v.result(1)['corrupt_lines'], 1)

    def test_known_crc_and_fields(self):
        self.assertEqual(console.zlib.crc32(b'123456789'), 0xcbf43926)
        self.assertEqual(console.fields(b'FLOOD END lines=20 bytes=400 elapsed_us=10'),
                         dict(lines=20, bytes=400, elapsed_us=10))


class GuardTests(unittest.TestCase):
    def setUp(self):
        import run_guarded
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patched = patch.object(run_guarded, 'rollback_log_path', return_value=Path(temp.name)/'rollback.log')
        patched.start(); self.addCleanup(patched.stop)

    def test_failed_child_restores_before_reporting_error(self):
        import os
        import subprocess
        import run_guarded
        child = unittest.mock.Mock()
        child.wait.return_value = 3; child.poll.return_value = 3
        with patch.dict(os.environ, {'DG_BENCH_LOCK_FD': '99', 'DG_BUILD_LOCK_HELD': '0'}), \
             patch('sys.argv', ['run_guarded.py', '--backup', '/backup', '--', 'fake-command']), \
             patch.object(run_guarded, 'verify_backup') as verify, \
             patch.object(run_guarded, 'require_bench', return_value=99), \
             patch.object(run_guarded, 'checked_esptool_python', return_value='/idf/python'), \
             patch.object(run_guarded.subprocess, 'Popen', return_value=child), \
             patch.object(run_guarded.subprocess, 'run') as restore_run, \
             patch.object(run_guarded.signal, 'signal'), \
             self.assertRaises(subprocess.CalledProcessError):
            run_guarded.main()
        verify.assert_called_once_with(Path('/backup'))
        self.assertIn('restore_factory.py', restore_run.call_args.args[0][1])
        self.assertEqual(restore_run.call_args.args[0][-2:], ['--backup', '/backup'])
        self.assertTrue(restore_run.call_args.kwargs['check'])

    def test_no_hardware_command_before_backup_validation(self):
        import os
        import run_guarded
        with patch.dict(os.environ, {'DG_BENCH_LOCK_FD': '99', 'DG_BUILD_LOCK_HELD': '0'}), \
             patch('sys.argv', ['run_guarded.py', '--backup', '/backup', '--', 'fake-command']), \
             patch.object(run_guarded, 'verify_backup', side_effect=ValueError('bad backup')), \
             patch.object(run_guarded, 'require_bench', return_value=99), \
             patch.object(run_guarded.subprocess, 'Popen') as start, self.assertRaises(ValueError):
            run_guarded.main()
        start.assert_not_called()


class BootTests(unittest.TestCase):
    def test_known_boot_loss_and_first_command(self):
        board = unittest.mock.Mock()
        board.until.return_value = [b'BOOT 0 uptime_us=1', b'BOOT 2 uptime_us=3', b'READY boot_lines=8 uptime_us=4']
        board.uptime.return_value = 99
        result = console.boot_check(board)
        self.assertEqual(result['cold_reset']['boot_lines_lost'], 6)
        self.assertEqual(result['first_command_uptime_us'], 99)
        board.uptime.assert_called_once()

    def test_first_command_is_not_retried_and_boot_result_survives(self):
        board = unittest.mock.Mock()
        board.until.return_value = [b'READY boot_lines=8 uptime_us=4']
        board.uptime.side_effect = TimeoutError('first command lost')
        result = {}
        with self.assertRaises(TimeoutError): console.boot_check(board, result)
        board.uptime.assert_called_once()
        self.assertEqual(result['cold_reset']['boot_lines_lost'], 8)


if __name__ == '__main__': unittest.main()
