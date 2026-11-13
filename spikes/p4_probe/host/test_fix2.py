"""Host-only regression cases for incomplete ring measurements and detached rollback."""
import contextlib
import errno
import io
import os
import subprocess
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock, patch
import console_rate as console
import run_guarded as guard


def wire(prefix, seq, payload=b'payload'):
    return prefix + f' {seq} {zlib.crc32(payload):08x} '.encode() + payload


class RingTrailerTests(unittest.TestCase):
    def board(self):
        board=Mock();board.boots_seen=0;board.uptime.side_effect=[100,200,300,400]
        board.resync_uptime.return_value=1000
        def lines(seconds, slow=False):
            if seconds==1: return iter(())
            if seconds==20: return iter((wire(b'F',0),b'FLOOD END lines=1 elapsed_us=1000 ring_drops=0'))
            if slow: return iter((wire(b'R',0),b'P4_PROBE boot'))
            return iter((wire(b'R',0),b'R 1 invalid-crc payload')) # lost END and OK
        board.lines.side_effect=lines
        return board

    def test_lost_baseline_end_and_slow_reader_reset_are_measurements(self):
        board=self.board();result=dict(highest_passing_baud=2000000,measured_bytes_per_second=200000)
        with patch.object(console.time,'sleep'):
            console.post_sweep(board,types.SimpleNamespace(line_bytes=1800),result,2000000)
        for name in ('ring_baseline','slow_reader'):
            row=result[name]
            self.assertFalse(row['summary_received']);self.assertFalse(row['passed'])
            self.assertEqual(row['observed_sequences'],2 if name=='ring_baseline' else 1)
            self.assertEqual(row['recovery_uptime_us'],1000)
            self.assertEqual(row['recovery_baud'],2000000)
            self.assertNotIn('expected_lines',row)
            self.assertIsNone(row['unexplained_missing']);self.assertIsNone(row['bytes_per_second'])
        self.assertEqual(result['ring_baseline']['corrupt_lines'],1)
        self.assertEqual(result['slow_reader']['corrupt_lines'],0)
        self.assertEqual(result['highest_passing_baud'],2000000)
        self.assertEqual(board.resync_uptime.call_count,2)

    def test_lost_ring_trailer_can_recover_after_reset_to_115200(self):
        board=self.board();board.resync_uptime.side_effect=[TimeoutError('wrong baud'),1000,2000]
        result={}
        with patch.object(console.time,'sleep'):
            console.post_sweep(board,types.SimpleNamespace(line_bytes=1800),result,2000000)
        self.assertEqual(result['ring_baseline']['recovery_baud'],115200)
        board.switch_baud.assert_called_once_with(2000000)
        self.assertIn('slow_reader',result)

    def test_unrecoverable_ring_link_remains_fatal(self):
        board=self.board();board.resync_uptime.side_effect=TimeoutError('dead link')
        result={}
        with patch.object(console.time,'sleep'),self.assertRaisesRegex(TimeoutError,'dead link'):
            console.post_sweep(board,types.SimpleNamespace(line_bytes=1800),result,2000000)
        self.assertFalse(result['ring_baseline']['summary_received'])
        self.assertNotIn('slow_reader',result)


class DeadTerminal(io.StringIO):
    def write(self, text): raise OSError(errno.EIO,'terminal hung up')


class RollbackLogTests(unittest.TestCase):
    def test_restore_output_does_not_use_hung_up_terminal(self):
        child=Mock();child.wait.return_value=3;child.poll.return_value=3
        def restore(command, **kwargs):
            self.assertEqual(command[0],'/idf/python')
            self.assertEqual(kwargs['stderr'],subprocess.STDOUT)
            self.assertFalse(os.isatty(kwargs['stdout'].fileno()))
            kwargs['stdout'].write(b'esptool progress and restore witness\n');kwargs['stdout'].flush()
        with tempfile.TemporaryDirectory() as tmp:
            backup=Path(tmp)/'backup'
            with patch('sys.argv',['run_guarded.py','--backup',str(backup),'--','fake']), \
                 patch.object(guard,'require_bench',return_value=99),patch.object(guard,'verify_backup'), \
                 patch.object(guard,'checked_esptool_python',return_value='/idf/python'), \
                 patch.object(guard.signal,'signal'),patch.object(guard.subprocess,'Popen',return_value=child), \
                 patch.object(guard.subprocess,'run',side_effect=restore), \
                 contextlib.redirect_stdout(DeadTerminal()),contextlib.redirect_stderr(DeadTerminal()), \
                 self.assertRaises(subprocess.CalledProcessError): guard.main()
            logs=list((Path(tmp)/'rollbacks').glob('restore-*.log'))
            self.assertEqual(len(logs),1)
            text=logs[0].read_text()
            self.assertIn('esptool progress and restore witness',text)
            self.assertIn('Factory rollback completed',text)

    def test_unusable_log_path_is_rejected_before_child_starts(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('sys.argv',['run_guarded.py','--backup',tmp,'--rollback-log',tmp,'--','fake']), \
             patch.object(guard,'require_bench',return_value=99),patch.object(guard,'verify_backup'), \
             patch.object(guard,'checked_esptool_python',return_value='/idf/python'), \
             patch.object(guard.subprocess,'Popen') as start,self.assertRaises(IsADirectoryError): guard.main()
        start.assert_not_called()

    def test_explicit_log_path_is_used(self):
        args=types.SimpleNamespace(backup=Path('/unused'),rollback_log=Path('/evidence/chosen.log'))
        self.assertEqual(guard.rollback_log_path(args),Path('/evidence/chosen.log'))


if __name__=='__main__': unittest.main()
