"""Fix-round regressions. Locks are private temp files; serial/network are mocked."""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import console_rate as console
import flash_project as flash
import hardware
import restore_factory as restore
import run_guarded as guard

ROOT = Path(__file__).resolve().parents[3]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_override_python_and_exact_version(self):
        with patch.dict(os.environ, {'ESPTOOL_PYTHON': '/idf/python'}), \
             patch.object(hardware.subprocess, 'run', return_value=types.SimpleNamespace(stdout='5.5.0\n')) as run:
            self.assertEqual(hardware.checked_esptool_python(), '/idf/python')
        self.assertEqual(run.call_args.args[0][0:2], ['/idf/python', '-c'])
        self.assertIn('esptool.__version__', run.call_args.args[0][2])

    def test_wrong_esptool_version_refused(self):
        with patch.object(hardware.subprocess, 'run', return_value=types.SimpleNamespace(stdout='5.4.0\n')):
            with self.assertRaisesRegex(RuntimeError, '5.5.0'): hardware.checked_esptool_python()

    def test_idf_environment_and_fallback(self):
        with patch.dict(os.environ, {'IDF_PYTHON_ENV_PATH': '/idf/env'}, clear=True):
            self.assertEqual(hardware.esptool_python(), '/idf/env/bin/python')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hardware.esptool_python(), str(Path.home()/'.espressif/python_env/idf6.1_py3.12_env/bin/python'))

    def bench(self, fd):
        return patch.dict(os.environ, {'DG_BENCH_LOCK_FD': str(fd), 'DG_BUILD_LOCK_HELD': '0'})

    def test_missing_context_is_rejected_before_opening_real_lock(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError): hardware.require_bench()

    def test_private_held_lock_and_ownership(self):
        path = self.root/'bench.lock'
        with path.open('w') as held, patch.object(hardware, 'BENCH_LOCK', path):
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.bench(held.fileno()): self.assertEqual(hardware.require_bench(), held.fileno())
            with path.open('rb') as unowned, self.bench(unowned.fileno()):
                with self.assertRaises(RuntimeError): hardware.require_bench()
            with path.open('rb') as probe:
                with self.assertRaises(BlockingIOError): fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_unlocked_private_file_rejected_and_released(self):
        path = self.root/'bench.lock'
        with path.open('w') as free, patch.object(hardware, 'BENCH_LOCK', path), self.bench(free.fileno()):
            with self.assertRaisesRegex(RuntimeError, 'not held'): hardware.require_bench()
            fcntl.flock(free, fcntl.LOCK_EX | fcntl.LOCK_NB) # probe left no lock behind

    def test_wrong_file_fd_and_build_lock_refused(self):
        path = self.root/'bench.lock'; path.touch()
        with (self.root/'other').open('w') as other, patch.object(hardware, 'BENCH_LOCK', path), self.bench(other.fileno()):
            with self.assertRaisesRegex(RuntimeError, 'not the bench'): hardware.require_bench()
            with patch.dict(os.environ, {'DG_BUILD_LOCK_HELD': '1'}):
                with self.assertRaisesRegex(RuntimeError, 'build lock'): hardware.require_bench()

    def test_config_metadata_guard(self):
        build = self.root/'build'; build.mkdir()
        project = dict(project_path=str(self.root), config_file=str(build/'sdkconfig'))
        hardware.check_project_config(project, build)
        (self.root/'sdkconfig').touch() # empty synthetic file, never read
        with self.assertRaisesRegex(ValueError, 'Stale root'): hardware.check_project_config(project, build)
        (self.root/'sdkconfig').unlink()
        project['config_file'] = str(self.root/'sdkconfig')
        with self.assertRaisesRegex(ValueError, 'Only'): hardware.check_project_config(project, build)

    def test_cmake_refuses_stale_root_or_wrong_config_before_idf(self):
        for name in ('p4_probe', 'p4_net'):
            for stale in (True, False):
                with self.subTest(project=name, stale=stale), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); build = root/'build'
                    (root/'CMakeLists.txt').write_text((ROOT/'spikes'/name/'CMakeLists.txt').read_text())
                    if stale: (root/'sdkconfig').touch() # public empty fixture; no contents inspected
                    config = build/'sdkconfig' if stale else root/'wrong-config'
                    env = dict(os.environ, DG_BUILD_LOCK_FILE=str(root/'private-build.lock'))
                    run = subprocess.run([str(ROOT/'scripts/build_locked.sh'), 'cmake', '-S', str(root), '-B', str(build),
                                          '-DSDKCONFIG='+str(config)], env=env, capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn('Stale root' if stale else 'Only -DSDKCONFIG', run.stdout+run.stderr)


class RestoreExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patched = patch.object(guard, 'rollback_log_path', return_value=Path(temp.name)/'rollback.log')
        patched.start(); self.addCleanup(patched.stop)

    def test_restore_timeout_interpreter_timing_and_witness(self):
        with patch.object(restore, 'require_bench', return_value=99), \
             patch.object(restore, 'checked_esptool_python', return_value='/idf/python'), \
             patch.object(restore, 'verify_backup', return_value=Path('/backup/factory-a.bin')), \
             patch.object(restore.subprocess, 'run') as run, \
             patch.object(restore.time, 'monotonic', side_effect=[1, 3, 5, 9]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            restore.main(['--backup', '/backup'])
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ['/idf/python']*3)
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [1800, 1800, 30])
        self.assertTrue(all(call.kwargs['pass_fds'] == (99,) for call in run.call_args_list))
        self.assertIn('RESTORE write elapsed_s=2.000', output.getvalue())
        self.assertIn('RESTORE verify elapsed_s=4.000', output.getvalue())
        self.assertIn('wait_factory', run.call_args.args[0][2])

    def test_write_failure_logs_elapsed_and_stops_before_verify(self):
        with patch.object(restore, 'require_bench', return_value=99), \
             patch.object(restore, 'checked_esptool_python', return_value='/idf/python'), \
             patch.object(restore, 'verify_backup', return_value=Path('/backup/factory-a.bin')), \
             patch.object(restore.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fake', 1800)) as run, \
             contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(subprocess.TimeoutExpired):
            restore.main(['--backup', '/backup'])
        run.assert_called_once()
        self.assertIn('RESTORE write elapsed_s=', output.getvalue())

    def test_restore_refuses_non_dry_run_without_context(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(restore, 'verify_backup') as backup:
            with self.assertRaises(RuntimeError): restore.main(['--backup', '/backup'])
        backup.assert_not_called()

    def test_flash_timeout_version_preflight_and_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); build = root/'build'; build.mkdir()
            for name in ('boot.bin', 'parts.bin', 'app.bin'): (build/name).touch()
            (build/'flasher_args.json').write_text(json.dumps(dict(extra_esptool_args=dict(chip='esp32p4'),
                write_flash_args=[], flash_files={'0x2000':'boot.bin','0x8000':'parts.bin','0x10000':'app.bin'})))
            (build/'project_description.json').write_text(json.dumps(dict(target='esp32p4',project_path=str(root),config_file=str(build/'sdkconfig'))))
            with patch('sys.argv', ['flash_project.py','--build',str(build)]), \
                 patch.object(flash, 'require_bench', return_value=99), \
                 patch.object(flash, 'checked_esptool_python', return_value='/idf/python') as checked, \
                 patch.object(flash.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
                flash.main()
            checked.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], '/idf/python')
            self.assertEqual(run.call_args.kwargs['timeout'], 1800)
            self.assertEqual(run.call_args.kwargs['pass_fds'], (99,))
            self.assertIn('FLASH write elapsed_s=', output.getvalue())
            with patch('sys.argv', ['flash_project.py','--build',str(build)]), \
                 patch.dict(os.environ, {}, clear=True), patch.object(flash.subprocess, 'run') as run:
                with self.assertRaises(RuntimeError): flash.main()
            run.assert_not_called()

    def test_guard_checks_python_before_child(self):
        with patch('sys.argv', ['run_guarded.py','--backup','/backup','--','fake']), \
             patch.object(guard, 'require_bench', return_value=99), patch.object(guard, 'verify_backup'), \
             patch.object(guard, 'checked_esptool_python', side_effect=RuntimeError('wrong esptool')), \
             patch.object(guard.subprocess, 'Popen') as start, self.assertRaises(RuntimeError): guard.main()
        start.assert_not_called()

    def test_guard_hup_and_term_rollback_use_idf_and_preserve_fd(self):
        for sig in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=sig):
                handlers = {}; child = Mock(); child.poll.return_value = None
                def install(number, handler): handlers[number] = handler
                def interrupt(*args, **kwargs):
                    if not args and not kwargs: handlers[sig](sig, None)
                    return 0
                child.wait.side_effect = interrupt
                with patch('sys.argv', ['run_guarded.py','--backup','/backup','--','fake']), \
                     patch.object(guard, 'require_bench', return_value=99), patch.object(guard, 'verify_backup'), \
                     patch.object(guard, 'checked_esptool_python', return_value='/idf/python'), \
                     patch.object(guard.signal, 'signal', side_effect=install), patch.object(guard.os, 'killpg'), \
                     patch.object(guard.subprocess, 'Popen', return_value=child) as start, \
                     patch.object(guard.subprocess, 'run') as rollback, self.assertRaises(InterruptedError): guard.main()
                self.assertEqual(start.call_args.kwargs['pass_fds'], (99,))
                self.assertEqual(rollback.call_args.args[0][0], '/idf/python')
                self.assertEqual(rollback.call_args.kwargs['pass_fds'], (99,))
                for shield in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): self.assertEqual(handlers[shield], signal.SIG_IGN)


class ConsoleMeasurementTests(unittest.TestCase):
    def stream(self, **extra):
        return dict(missing_or_corrupt=0,corrupt_lines=0,duplicates=0,unexpected_sequences=0,reordered=0,
                    summary=dict(driver_drops=999,ring_drops=0,tx_wait_rc=0),bytes_per_second=150000,**extra)

    def test_corrupt_boot_record_counts_as_lost(self):
        board = Mock()
        board.until.return_value = [b'BOOT corrupt', *[f'BOOT {i} uptime_us=10'.encode() for i in range(1,8)], b'READY boot_lines=8 uptime_us=20']
        board.uptime.return_value = 30
        self.assertEqual(console.boot_check(board)['cold_reset']['boot_lines_lost'], 1)

    def test_resync_ignores_stale_error_and_ack_without_forcing_reset(self):
        board=console.Board.__new__(console.Board)
        board.send=Mock();board.until=Mock(side_effect=[[b'ERR partial line',b'OK',b'UPTIME us=123'],[b'OK']])
        self.assertEqual(board.resync_uptime(),123)
        self.assertEqual([call.args[0] for call in board.send.call_args_list],['','uptime'])

    def sweep_board(self):
        board = Mock(); board.loop_ends = []
        board.command.side_effect = lambda *args: board.loop_ends.append(dict(errors=0))
        return board

    def test_summary_selected_before_post_tests_and_write_counter_is_not_loss_gate(self):
        board = self.sweep_board(); result = {}
        with patch.object(console,'BAUDS',(2000000,115200)), patch.object(console,'boot_check'), \
             patch.object(console,'collect',return_value=self.stream()), \
             patch.object(console,'dump_check',return_value=dict(missing_or_corrupt=0,corrupt_lines=0,pcm_size_ok=True)), \
             patch.object(console.time,'sleep'), patch.object(console,'post_sweep',side_effect=OSError('port')) as post:
            with self.assertRaises(OSError): console.campaign(board,types.SimpleNamespace(seconds=60,line_bytes=1800),result)
        self.assertEqual(result['highest_passing_baud'],2000000)
        self.assertEqual(result['measured_bytes_per_second'],150000)
        self.assertTrue(all(row['passed'] for row in result['baud_runs']))
        self.assertEqual(board.switch_baud.call_args.args,(2000000,))
        self.assertEqual(post.call_args.args[-1],2000000)

    def test_no_baud_pass_fails_before_post_sweep(self):
        board=self.sweep_board(); result={}; flood=self.stream(); flood['missing_or_corrupt']=1
        with patch.object(console,'BAUDS',(115200,)),patch.object(console,'boot_check'), \
             patch.object(console,'collect',return_value=flood),patch.object(console,'dump_check',return_value=dict(missing_or_corrupt=0,corrupt_lines=0,pcm_size_ok=True)), \
             patch.object(console.time,'sleep'),patch.object(console,'post_sweep') as post:
            with self.assertRaisesRegex(RuntimeError,'No baud passed'): console.campaign(board,types.SimpleNamespace(seconds=60,line_bytes=1800),result)
        self.assertIsNone(result['highest_passing_baud']);post.assert_not_called()

    def test_attach_and_mid_flood_reboots_do_not_skip_ring_tests(self):
        board=Mock();board.boots_seen=0;board.lines.return_value=[]
        board.uptime.side_effect=[1000,900,1100,1000]
        result={}
        with patch.object(console,'collect',side_effect=[self.stream(),self.stream(),self.stream()]),patch.object(console.time,'sleep'):
            console.post_sweep(board,types.SimpleNamespace(line_bytes=1800),result,2000000)
        self.assertFalse(result['attach_without_reset']['passed'])
        self.assertFalse(result['reopen_mid_flood']['passed'])
        self.assertEqual(result['ring_baseline']['baud'],2000000)
        self.assertEqual(result['slow_reader']['baud'],2000000)

    def test_mid_flood_reset_missing_trailer_recovers_baud_and_continues(self):
        board=Mock();board.boots_seen=0;board.lines.return_value=[]
        board.uptime.side_effect=[1000,1100,1200,TimeoutError('reset')];board.resync_uptime.return_value=20
        result={}
        with patch.object(console,'collect',side_effect=[console.ConsoleReset('boot'),self.stream(),self.stream()]),patch.object(console.time,'sleep'):
            console.post_sweep(board,types.SimpleNamespace(line_bytes=1800),result,1500000)
        self.assertFalse(result['reopen_mid_flood']['passed'])
        self.assertFalse(result['reopen_mid_flood']['summary_received'])
        board.switch_baud.assert_called_once_with(1500000)
        self.assertIn('slow_reader',result)


class NetworkBindingTests(unittest.TestCase):
    def test_host_address_and_interface_are_used_without_real_network(self):
        spec=importlib.util.spec_from_file_location('p4_run_net',ROOT/'spikes/p4_net/host/run_net.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        board=Mock();board.__enter__=Mock(return_value=board);board.__exit__=Mock(return_value=False)
        board.until.side_effect=[[b'HOSTED FW rpc_rc=0 version=2.0.0',b'P4_NET READY'],[b'TCP END bytes=100 errno=0']]
        def command(text,*args):
            if text=='stop': return [b'UDP END received=3 sent=3 echo_send_errors=0']
            if text=='netstats': return [b'NETPROBE {"paths":[]}']
            if text=='netdump': return [b'NETPROBE_DUMP END records=0']
            return []
        board.command.side_effect=command
        runner=Mock(side_effect=lambda *args,**kwargs:(kwargs['ready'](),kwargs['finish']()))
        stub=types.SimpleNamespace(run_streams=runner,analyze_streams=Mock(return_value={'esp-0':dict(rtt_ms=dict(p50=1))}),decode_dump=Mock(return_value=[]))
        child=Mock();child.returncode=0;child.poll.return_value=0
        with tempfile.TemporaryDirectory() as tmp,patch('sys.argv',['run_net.py','--out',tmp]), \
             patch.dict('sys.modules',{'netprobe':stub}),patch.object(module,'Board',return_value=board), \
             patch.object(module.subprocess,'Popen',return_value=child) as start,patch.object(module.time,'sleep'):
            module.main()
        self.assertEqual(runner.call_args.args[0],['esp-0,192.168.7.222,47010'])
        self.assertEqual(runner.call_args.kwargs['interface'],'wlp8s0')
        tcp_args=start.call_args.args[0]
        self.assertEqual(tcp_args[tcp_args.index('--bind')+1],'192.168.7.222')


if __name__=='__main__': unittest.main()
