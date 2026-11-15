"""macOS: identity is the USB descriptor, never the cu.usbmodem name."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import board_port_macos


class MacPortIdentity(unittest.TestCase):
    def ports(self, *serials, vid=0x303a, pid=0x1001):
        return [Mock(device=f'/dev/cu.usbmodem{i}', vid=vid, pid=pid, serial_number=s)
                for i, s in enumerate(serials)]

    def resolve(self, ports, requested=None, env='echoear', **kwargs):
        with patch.dict(os.environ, {'DG_BOARD': env}), \
             patch.object(board_port_macos, '_comports', return_value=ports):
            return board_port_macos.resolve_port(requested, **kwargs)

    def test_matches_usb_serial_on_any_name(self):
        self.assertEqual(self.resolve(self.ports('aa:bb', 'B4:3A:45:16:2D:94')), '/dev/cu.usbmodem1')

    def test_requires_exactly_one_match(self):
        for ports in (self.ports('aa:bb'), self.ports('B4:3A:45:16:2D:94', 'b4:3a:45:16:2d:94'),
                      self.ports('B4:3A:45:16:2D:94', vid=0x1a86)):
            with self.subTest(n=len(ports)), self.assertRaises(RuntimeError):
                self.resolve(ports)

    def test_rejects_other_requested_port(self):
        with self.assertRaises(RuntimeError):
            self.resolve(self.ports('B4:3A:45:16:2D:94'), '/dev/cu.usbmodem9')

    def test_other_boards_are_refused(self):
        with self.assertRaises(RuntimeError):
            self.resolve(self.ports('B4:3A:45:16:2D:94'), env='waveshare_146')

    def test_p4_matches_its_bridge_serial_only(self):
        p4 = self.ports('other', '5B90169930', vid=0x1a86, pid=0x55d3)
        self.assertEqual(self.resolve(p4, env='p4xc', board='p4xc'), '/dev/cu.usbmodem1')
        with self.assertRaises(RuntimeError):
            self.resolve(p4, '/dev/cu.usbmodem0', env='p4xc', board='p4xc')
        # The EchoEar's descriptor is never the P4, and the P4's is never the EchoEar.
        for ports, board in ((self.ports('5B90169930'), 'p4xc'), (p4, 'echoear')):
            with self.subTest(board=board), self.assertRaises(RuntimeError):
                self.resolve(ports, env=board, board=board)

    def test_p4_needs_the_bench_lock_and_a_matching_build(self):
        p4 = self.ports('5B90169930', vid=0x1a86, pid=0x55d3)
        with patch.dict(os.environ, {'DG_BOARD': 'p4xc'}), \
             patch.object(board_port_macos, '_comports', return_value=p4), \
             patch.object(board_port_macos.subprocess, 'run') as run:
            os.environ.pop('DG_BENCH_LOCK_FD', None)
            with self.assertRaisesRegex(RuntimeError, 'bench lock'):
                board_port_macos.verify_flash_mac(board='p4xc')
            run.assert_not_called()
            with tempfile.TemporaryDirectory() as build:
                (Path(build)/'project_description.json').write_text('{"target": "esp32s3"}')
                with self.assertRaisesRegex(RuntimeError, 'Build target'):
                    board_port_macos.main(['--board', 'p4xc', '--build', build])
            with patch.object(board_port_macos.boards, 'require_bench'), \
                 patch.object(board_port_macos.boards, 'checked_esptool_python', return_value='pinned-python'), \
                 tempfile.TemporaryDirectory() as build, patch('builtins.print') as printed:
                (Path(build)/'project_description.json').write_text('{"target": "esp32p4"}')
                run.return_value = Mock(returncode=0, stdout='MAC:                e8:f6:0a:e0:b8:76\n')
                self.assertEqual(board_port_macos.verify_flash_mac(board='p4xc'), '/dev/cu.usbmodem0')
                self.assertIn('esp32p4', run.call_args.args[0])
                self.assertEqual(run.call_args.args[0][0], 'pinned-python')
                board_port_macos.main(['--board', 'p4xc', '--build', build, ''])
                printed.assert_called_with('/dev/cu.usbmodem0')
                run.return_value = Mock(returncode=0, stdout='MAC: b4:3a:45:16:2d:94\n')
                with self.assertRaises(RuntimeError):
                    board_port_macos.verify_flash_mac(board='p4xc')

    def test_command_line_default_is_the_echoear_with_an_empty_port(self):
        # scripts/idf.sh always passes the requested port, empty when none was given.
        with patch.dict(os.environ, {'DG_BOARD': 'echoear'}), \
             patch.object(board_port_macos, '_comports', return_value=self.ports('B4:3A:45:16:2D:94')), \
             patch.object(board_port_macos.subprocess, 'run',
                          return_value=Mock(returncode=0, stdout='MAC: b4:3a:45:16:2d:94\n')) as run, \
             patch('builtins.print') as printed:
            board_port_macos.main([''])
            printed.assert_called_with('/dev/cu.usbmodem0')
            self.assertIn('esp32s3', run.call_args.args[0])

    def test_build_only_and_unknown_boards_are_refused(self):
        for board in ('waveshare_146', 'bogus'):
            with self.subTest(board=board), self.assertRaises((RuntimeError, ValueError)):
                board_port_macos.resolve_port(board=board)


if __name__ == '__main__':
    unittest.main()
