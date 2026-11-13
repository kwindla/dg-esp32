import signal
import subprocess
import unittest
from unittest.mock import Mock,patch
from bot_restart import stop_owned,lifecycle_sequence,restart_and_recover
from step9a_r2_checks import DisplayMonitor

class RestartTests(unittest.TestCase):
    def test_sigterm_exits_without_kill(self):
        p=Mock(pid=123,returncode=0);p.poll.return_value=None
        r=stop_owned(p)
        p.send_signal.assert_called_once_with(signal.SIGTERM);p.wait.assert_called_once_with(timeout=10)
        self.assertFalse(r['fallback_kill']);self.assertEqual(r['returncode'],0)

    def test_timeout_records_sigkill_and_reaps_with_bound(self):
        p=Mock(pid=123,returncode=-9);p.poll.return_value=None
        p.wait.side_effect=[subprocess.TimeoutExpired('bot',10),-9]
        r=stop_owned(p)
        self.assertEqual([c.args[0] for c in p.send_signal.call_args_list],[signal.SIGTERM,signal.SIGKILL])
        self.assertEqual([c.kwargs['timeout'] for c in p.wait.call_args_list],[10,5])
        self.assertTrue(r['fallback_kill']);self.assertEqual(r['returncode'],-9)

    def test_group_signals_reject_unowned_process_group(self):
        p=Mock(pid=123);p.poll.return_value=None
        with patch('bot_restart.os.getpgid',return_value=1),patch('bot_restart.os.killpg') as kill:
            with self.assertRaises(RuntimeError):stop_owned(p,group=True)
        kill.assert_not_called()

    def test_lifecycle_requires_disconnect_start_ready_in_order(self):
        lines=[(i,float(i),f'I (10) dg-app: STATE {s} gen={g} desired=1 t={i+1}') for i,(s,g) in enumerate([('Disconnected',2),('Starting',3),('Ready',3)])]
        self.assertTrue(lifecycle_sequence(lines)['passed']);self.assertFalse(lifecycle_sequence(lines[1:])['passed'])

    def test_shared_arm_checks_generation_screens_and_post_ready_turn(self):
        monitor=DisplayMonitor();lines=[];board=Mock(cursor=1,display_monitor=monitor)
        def ready(*a,**kw):
            lines.extend((i,float(i),f'I (10) dg-app: STATE {s} gen={g} desired=1 t={i+1}') for i,(s,g) in enumerate([('Disconnected',2),('Starting',3),('Ready',3)]))
            monitor.feed((lines[-1][2]+'\n').encode());return lines[-1][2]
        board.wait_for.side_effect=ready;board.lines_since.side_effect=lambda _:lines
        proc=Mock(pid=123,returncode=0);proc.poll.return_value=None
        def turn(bot):
            self.assertEqual(monitor.result()['bot_down_windows'][0]['end_reason'],'STATE Ready')
            return dict(passed=True)
        r=restart_and_recover(board,proc,lambda budget:Mock(),turn,initial_generation=1,screen_proof=lambda lines:dict(available=True,passed=True))
        self.assertTrue(r['passed']);self.assertTrue(r['checks']['fresh_generation'])

    def test_no_screen_proof_never_passes_combined_arm(self):
        monitor=DisplayMonitor();board=Mock(cursor=1,display_monitor=monitor)
        board.wait_for.return_value='STATE Ready gen=3 desired=1 t=3'
        board.lines_since.return_value=[(i,float(i),f'STATE {s} gen={g} desired=1 t={i+1}') for i,(s,g) in enumerate([('Disconnected',2),('Starting',3),('Ready',3)])]
        proc=Mock(pid=123,returncode=0);proc.poll.return_value=None
        r=restart_and_recover(board,proc,lambda budget:Mock(),lambda bot:dict(passed=True),initial_generation=1,screen_proof=None)
        self.assertFalse(r['passed']);self.assertTrue(r['recovery_without_screens_passed'])

    def test_final_monitor_failure_invalidates_earlier_success(self):
        monitor=Mock();monitor.begin_bot_down.return_value=dict(deadline_host_s=1e12)
        monitor.result.side_effect=[dict(passed=True),dict(passed=False,errors=['late error'])]
        board=Mock(cursor=1,display_monitor=monitor)
        board.wait_for.return_value='STATE Ready gen=3 desired=1 t=3'
        board.lines_since.return_value=[(i,float(i),f'STATE {s} gen={g} desired=1 t={i+1}') for i,(s,g) in enumerate([('Disconnected',2),('Starting',3),('Ready',3)])]
        proc=Mock(pid=123,returncode=0);proc.poll.return_value=None
        r=restart_and_recover(board,proc,lambda budget:Mock(),lambda bot:dict(passed=True),initial_generation=1,screen_proof=lambda lines:dict(passed=True))
        self.assertFalse(r['passed']);self.assertFalse(r['recovery_without_screens_passed']);self.assertFalse(r['checks']['monitor'])

if __name__=='__main__':unittest.main()
