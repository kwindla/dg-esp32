"""Host-only board identity/reset tests. Serial is fake; flock files are private."""
import contextlib
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import boards
import board_serial
import board_live_check


class IdentityTests(unittest.TestCase):
    def test_echoear_positional_and_keyword_calls_delegate_unchanged(self):
        for args in ((), ('/requested',)):
            with patch.object(boards.board_port, 'resolve_port', return_value='legacy') as call:
                self.assertEqual(boards.resolve_port(*args), 'legacy')
                call.assert_called_once_with(args[0] if args else None)
            with patch.object(boards.board_port, 'verify_flash_mac', return_value='legacy') as call:
                self.assertEqual(boards.verify_flash_mac(*args, board='echoear'), 'legacy')
                call.assert_called_once_with(args[0] if args else None)

    def test_unknown_and_build_only_refused_before_io(self):
        for board in ('unknown', 'waveshare_146'):
            for call in (boards.resolve_port, boards.verify_flash_mac):
                with self.subTest(board=board, call=call), patch.object(Path, 'resolve') as path, patch.object(boards.subprocess, 'run') as run:
                    with self.assertRaises((ValueError, RuntimeError)) as exc: call(board=board)
                    if board=='waveshare_146': self.assertIn('build-only board', str(exc.exception))
                    path.assert_not_called(); run.assert_not_called()
            with self.assertRaises((ValueError, RuntimeError)): board_serial.open_board(board)

    def resolve_p4(self, serial='5B90169930', requested='/dev/ttyUSB7'):
        with patch.object(Path, 'resolve', side_effect=[Path('/dev/ttyUSB7'),Path('/dev/ttyUSB7'),Path('/mock/usb/device')]), \
             patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'read_text', return_value=serial):
            return boards.resolve_port(requested, board='p4xc')

    def test_p4_usb_identity_and_renumbered_requested_alias(self):
        self.assertEqual(self.resolve_p4(), boards.BOARDS['p4xc']['by_id'])
        with self.assertRaisesRegex(RuntimeError, 'USB serial'): self.resolve_p4('different-device')

    def test_p4_wrong_requested_port_refused_before_usb_read(self):
        with patch.object(Path, 'resolve', side_effect=[Path('/dev/ttyUSB1'),Path('/dev/ttyUSB9')]), patch.object(Path, 'read_text') as read:
            with self.assertRaisesRegex(RuntimeError, 'pinned P4'): boards.resolve_port('/dev/ttyUSB9', board='p4xc')
            read.assert_not_called()

    def test_p4_esptool_uses_idf_python_and_checks_version_chip_and_mac(self):
        for mac in ('e8:f6:0a:e0:b8:76','00:11:22:33:44:55'):
            with self.subTest(mac=mac), patch.object(boards, 'require_bench'), patch.object(boards, 'resolve_port', return_value='/pinned-p4'), \
                 patch.dict(os.environ, {'ESPTOOL_PYTHON':'/idf/python'}), \
                 patch.object(boards.subprocess, 'run', side_effect=[subprocess.CompletedProcess([],0,'5.5.0\n'),subprocess.CompletedProcess([],0,'MAC: '+mac)]) as run:
                if mac==boards.BOARDS['p4xc']['efuse_mac']:
                    self.assertEqual(boards.verify_flash_mac('/requested',board='p4xc'), '/pinned-p4')
                else:
                    with self.assertRaisesRegex(RuntimeError, 'eFuse MAC'): boards.verify_flash_mac(board='p4xc')
                self.assertEqual(run.call_args_list[0].args[0][0], '/idf/python')
                self.assertEqual(run.call_args.args[0], ['/idf/python','-m','esptool','--chip','esp32p4','--port','/pinned-p4','read-mac'])

    def test_wrong_esptool_version_stops_before_read_mac(self):
        with patch.dict(os.environ, {'ESPTOOL_PYTHON':'/idf/python'}), \
             patch.object(boards.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'5.4.0')) as run:
            with self.assertRaisesRegex(RuntimeError, '5.5.0'): boards.checked_esptool_python()
            self.assertEqual(run.call_count,1)

    def test_idf_python_default(self):
        with patch.dict(os.environ, {'IDF_PYTHON_ENV_PATH':'/idf/env'}, clear=True), \
             patch.object(boards.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'5.5.0')) as run:
            self.assertEqual(boards.checked_esptool_python(),'/idf/env/bin/python')


class BenchContextTests(unittest.TestCase):
    def test_live_check_refuses_missing_context_before_device_access(self):
        with patch('sys.argv',['board_live_check.py','--log','/unused/serial.log']), \
             patch.object(board_live_check,'require_bench',side_effect=RuntimeError('missing bench')), \
             patch.object(board_live_check,'verify_flash_mac') as mac,patch.object(board_live_check,'open_board') as serial:
            with self.assertRaisesRegex(RuntimeError,'missing bench'): board_live_check.main()
            mac.assert_not_called();serial.assert_not_called()

    def test_missing_or_build_context_refused_without_real_lock_io(self):
        for env in ({}, {'DG_BUILD_LOCK_HELD':'1'}):
            with patch.dict(os.environ,env,clear=True), patch.object(Path,'stat',side_effect=AssertionError('no real lock')):
                with self.assertRaises(RuntimeError): boards.require_bench()

    def test_private_owned_contended_lock_and_stale_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'private.lock'
            with path.open('w') as held, patch.object(boards,'BENCH_LOCK',path), \
                 patch.dict(os.environ,{'DG_BENCH_LOCK_FD':str(held.fileno()),'DG_BUILD_LOCK_HELD':'0'},clear=True):
                with self.assertRaisesRegex(RuntimeError,'not held'): boards.require_bench()
                fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(boards.require_bench(),held.fileno())


class FakeSerial:
    def __init__(self, baud, boot):
        self.baudrate=baud; self.boot=boot; self.queue=[]; self.closed=False; self.opened=False; self._rts=False
        self.dtr=False; self.port=None; self.writes=[]
    @property
    def in_waiting(self): return len(self.queue[0]) if self.queue else 0
    @property
    def rts(self): return self._rts
    @rts.setter
    def rts(self, value):
        if self._rts and not value and self.opened: self.queue=list(self.boot.get(self.baudrate,[]))
        self._rts=value
    def open(self): self.opened=True; self.queue=list(self.boot.get(self.baudrate,[]))
    def read(self, size):
        if self.queue: return self.queue.pop(0)
        time.sleep(.001); return b''
    def close(self): self.closed=True
    def reset_input_buffer(self): self.queue=[]
    def write(self, data): self.writes.append(data); return len(data)


class SerialTests(unittest.TestCase):
    def test_echoear_default_and_explicit_reset_forwarding(self):
        for kwargs in ({},dict(reset_on_open=False,baud=921600)):
            with patch.object(board_serial,'EspSerial',return_value='legacy') as constructor:
                self.assertEqual(board_serial.open_board('echoear',**kwargs),'legacy')
                constructor.assert_called_once_with(**kwargs)

    def acquire(self, boot, **kwargs):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        fake=FakeSerial(kwargs.get('baud',2000000),boot)
        with patch.object(boards,'require_bench'),patch.object(boards,'resolve_port',return_value='/fake-p4'), \
             patch.object(board_serial,'port_lock',return_value=contextlib.nullcontext()), \
             patch.object(board_serial.serial,'Serial',return_value=fake) as constructor:
            board=board_serial.open_board('p4xc',log_path=Path(temp.name)/'serial.log',boot_timeout=kwargs.pop('boot_timeout',.5),**kwargs)
        self.addCleanup(board.close)
        return board,fake,constructor

    def test_p4_custom_baud_ignores_rom_garbage_and_waits_for_product_boot(self):
        board,fake,constructor=self.acquire({2000000:[b'\xff\xfe ROM noise\n',b'DG_BOOT board=p4xc ready\n']})
        self.assertIn('DG_BOOT board=p4xc',board.boot_line)
        self.assertEqual(fake.baudrate,2000000);self.assertTrue(board.reset_on_open)
        self.assertEqual(board.observed_baud,boards.BOARDS['p4xc']['app_baud'])
        self.assertEqual(constructor.call_args.args[:2],(None,2000000))

    def test_p4_spike_explicit_baud_and_boot_override(self):
        board,fake,constructor=self.acquire({115200:[b'READY boot_lines=8\n']},baud=115200,boot_pattern=r'P4_NET READY|READY')
        self.assertEqual(fake.baudrate,115200);self.assertIn('READY',board.boot_line)

    def test_p4_default_falls_back_with_reset_for_legacy_spike(self):
        board,fake,constructor=self.acquire({115200:[b'P4_NET READY uptime_us=900\n']},boot_pattern=r'P4_NET READY|READY')
        self.assertEqual(board.observed_baud,115200);self.assertIn('P4_NET READY',board.boot_line)

    def test_p4_product_pattern_never_falls_back_to_rom_baud(self):
        for kwargs in ({},dict(boot_pattern=boards.BOARDS['p4xc']['boot_pattern'])):
            with self.subTest(kwargs=kwargs),patch.object(board_serial.P4Serial,'reset') as reset:
                with self.assertRaises(TimeoutError):
                    self.acquire({115200:[b'DG_BOOT board=p4xc\n']},boot_timeout=.01,**kwargs)
                reset.assert_not_called()

    def test_p4_product_pattern_refuses_explicit_wrong_baud_before_io(self):
        with patch.object(boards,'require_bench') as bench,patch.object(board_serial.serial,'Serial') as serial:
            with self.assertRaisesRegex(ValueError,'app_baud'):
                board_serial.open_board('p4xc',baud=115200)
            bench.assert_not_called();serial.assert_not_called()

    def test_p4_ack_timeouts_do_not_send_s3_snapshot_commands(self):
        board,fake,constructor=self.acquire({2000000:[b'DG_BOOT board=p4xc\n']})
        snapshot=Mock()
        with patch.dict(sys.modules,{'failure_snapshot':snapshot}):
            for _ in range(2):
                with self.assertRaises(TimeoutError):board.command('uptime',timeout=.01)
        snapshot.capture_failure.assert_not_called()
        self.assertEqual(fake.writes,[b'#1 uptime\n',b'#2 uptime\n'])

    def test_p4_sequence_ack_and_error_behavior_remains_inherited(self):
        board,fake,constructor=self.acquire({2000000:[b'DG_BOOT board=p4xc\n']})
        with patch.object(board,'wait_for',return_value='OK #1'):
            self.assertEqual(board.command('uptime'),'OK #1')
        with patch.object(board,'wait_for',return_value='ERR #2 unavailable'):
            with self.assertRaisesRegex(RuntimeError,'ERR #2'):board.command('bad-command')

    def test_p4_explicit_no_reset_is_refused_before_port_access(self):
        with patch.object(boards,'require_bench') as bench,patch.object(board_serial.serial,'Serial') as serial:
            with self.assertRaisesRegex(ValueError,'no passive attach'): board_serial.open_board('p4xc',reset_on_open=False)
            bench.assert_not_called();serial.assert_not_called()

    def test_p4_context_failure_closes_without_s3_snapshot_commands(self):
        board,fake,constructor=self.acquire({2000000:[b'DG_BOOT board=p4xc\n']})
        with patch.object(board_serial.EspSerial,'__exit__',side_effect=AssertionError('S3 diagnostics')):
            with self.assertRaisesRegex(RuntimeError,'campaign failed'):
                with board: raise RuntimeError('campaign failed')
        self.assertTrue(fake.closed)

    def test_p4_missing_boot_closes_fake_serial_and_logs(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        fake=FakeSerial(115200,{})
        with patch.object(boards,'require_bench'),patch.object(boards,'resolve_port',return_value='/fake-p4'), \
             patch.object(board_serial,'port_lock',return_value=contextlib.nullcontext()),patch.object(board_serial.serial,'Serial',return_value=fake):
            with self.assertRaises(TimeoutError): board_serial.open_board('p4xc',baud=115200,boot_pattern='READY',log_path=Path(temp.name)/'serial.log',boot_timeout=.01)
        self.assertTrue(fake.closed)


if __name__=='__main__': unittest.main()
