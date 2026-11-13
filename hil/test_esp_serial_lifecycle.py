from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import esp_serial


class SerialLifecycleTests(unittest.TestCase):
    def test_reattach_preserves_linux_asserted_lines_without_reset_state(self):
        device = Mock()
        def open_device():
            # Model CDC ACM asserting both lines at os.open, followed by
            # pyserial applying DTR then RTS. The old False/False settings
            # visit (False, True), which resets USB Serial/JTAG hardware.
            dtr, rts = True, True
            dtr = device.dtr
            self.assertNotEqual((dtr, rts), (False, True))
            rts = device.rts
            self.assertEqual((dtr, rts), (True, True))
            self.assertEqual(device.port, 'verified-by-id')
        device.open.side_effect = open_device
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(esp_serial, 'resolve_port', return_value='verified-by-id'), \
             patch.object(esp_serial, 'port_lock', return_value=nullcontext()), \
             patch.object(esp_serial.threading, 'Thread'), \
             patch.object(esp_serial.serial, 'Serial', return_value=device) as factory:
            board = esp_serial.EspSerial(None, log_path=Path(temp)/'serial.log', reset_on_open=False)
            self.assertIsNone(factory.call_args.args[0])
            device.open.assert_called_once()
            board.close()
            board.close()
            device.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
