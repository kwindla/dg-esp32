"""macOS: identity is the USB descriptor, never the cu.usbmodem name."""
import os
import unittest
from unittest.mock import Mock, patch

import board_port_macos


class MacPortIdentity(unittest.TestCase):
    def ports(self, *serials, vid=0x303a, pid=0x1001):
        return [Mock(device=f'/dev/cu.usbmodem{i}', vid=vid, pid=pid, serial_number=s)
                for i, s in enumerate(serials)]

    def resolve(self, ports, requested=None, board='echoear'):
        with patch.dict(os.environ, {'DG_BOARD': board}), \
             patch.object(board_port_macos, '_comports', return_value=ports):
            return board_port_macos.resolve_port(requested)

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
            self.resolve(self.ports('B4:3A:45:16:2D:94'), board='waveshare_146')


if __name__ == '__main__':
    unittest.main()
