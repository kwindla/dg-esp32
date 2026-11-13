"""Safety regression checks without opening any serial device."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import board_port
import step9a_echoear

class PortIdentity(unittest.TestCase):
    def test_retired_board_is_rejected_before_device_lookup(self):
        with patch.dict(os.environ, {'DG_BOARD':'waveshare_146'}), patch.object(Path,'resolve') as resolve:
            with self.assertRaises(RuntimeError): board_port.resolve_port()
            resolve.assert_not_called()

    def test_wrong_requested_port_is_rejected_before_usb_lookup(self):
        with patch.dict(os.environ, {'DG_BOARD':'echoear'}), patch.object(Path,'resolve',side_effect=[Path('/dev/ttyACM1'),Path('/dev/ttyACM0')]) as resolve:
            with self.assertRaises(RuntimeError): board_port.resolve_port('/dev/ttyACM0')
            self.assertEqual(resolve.call_count,2)

    def test_echoear_accepts_renumbered_tty_with_matching_usb_identity(self):
        for tty in ('ttyACM0', 'ttyACM1', 'ttyACM7'):
            port=Path('/dev')/tty
            with self.subTest(tty=tty), patch.dict(os.environ, {'DG_BOARD':'echoear'}), \
                 patch.object(Path,'resolve',side_effect=[port,port,Path('/mock/usb/device')]), \
                 patch.object(Path,'is_file',return_value=True), \
                 patch.object(Path,'read_text',return_value=board_port.ECHOEAR_MAC):
                self.assertEqual(board_port.resolve_port(str(port)),str(board_port.ECHOEAR_PORT))

    def test_ttyacm0_does_not_bypass_usb_identity_check(self):
        with patch.dict(os.environ, {'DG_BOARD':'echoear'}), \
             patch.object(Path,'resolve',side_effect=[Path('/dev/ttyACM0'),Path('/dev/ttyACM0'),Path('/mock/usb/device')]), \
             patch.object(Path,'is_file',return_value=True), \
             patch.object(Path,'read_text',return_value='44:1b:f6:84:fc:8c'):
            with self.assertRaisesRegex(RuntimeError,'USB MAC mismatch'):
                board_port.resolve_port('/dev/ttyACM0')

    def test_smoke_verification_uses_pinned_identity_when_echoear_is_ttyacm0(self):
        out=Mock();out.__truediv__=Mock(return_value=Mock())
        for mac in (board_port.ECHOEAR_MAC,'44:1b:f6:84:fc:8c'):
            with self.subTest(mac=mac), patch.object(Path,'resolve',return_value=Path('/dev/ttyACM0')), \
                 patch('step9a_echoear.subprocess.run',return_value=subprocess.CompletedProcess([],0,'MAC: '+mac,'')) as run:
                if mac==board_port.ECHOEAR_MAC:
                    self.assertEqual(step9a_echoear.verify(out),str(board_port.ECHOEAR_PORT))
                else:
                    with self.assertRaisesRegex(RuntimeError,'MAC verification failed'):
                        step9a_echoear.verify(out)
                self.assertEqual(run.call_args.args[0][-3:],['--port',str(board_port.ECHOEAR_PORT),'read-mac'])
    def test_usb_identity_accepts_reenumeration_and_rejects_wrong_mac(self):
        with tempfile.TemporaryDirectory() as directory:
            usb=Path(directory)/'usb';device=usb/'interface';device.mkdir(parents=True)
            for tty in ('ttyACM0','ttyACM1','ttyACM7'):
                for mac in (board_port.ECHOEAR_MAC,'44:1b:f6:84:fc:8c'):
                    (usb/'serial').write_text(mac)
                    port=Path('/dev')/tty
                    with self.subTest(tty=tty,mac=mac), patch.dict(os.environ,{'DG_BOARD':'echoear'}), patch.object(Path,'resolve',side_effect=[port,port,device]):
                        if mac==board_port.ECHOEAR_MAC:
                            self.assertEqual(board_port.resolve_port(str(port)),str(board_port.ECHOEAR_PORT))
                        else:
                            with self.assertRaisesRegex(RuntimeError,'USB MAC mismatch'):
                                board_port.resolve_port(str(port))

    def test_step9a_identity_uses_chip_mac_regardless_of_tty_number(self):
        import step9a_echoear
        with tempfile.TemporaryDirectory() as directory:
            for tty in ('ttyACM0','ttyACM1'):
                for mac in (board_port.ECHOEAR_MAC,'44:1b:f6:84:fc:8c'):
                    response=subprocess.CompletedProcess([],0,'MAC: '+mac,'')
                    with self.subTest(tty=tty,mac=mac), patch.object(Path,'resolve',return_value=Path('/dev')/tty), patch.object(step9a_echoear.subprocess,'run',return_value=response) as run:
                        if mac==board_port.ECHOEAR_MAC:
                            self.assertEqual(step9a_echoear.verify(Path(directory)),str(step9a_echoear.BY_ID))
                        else:
                            with self.assertRaisesRegex(RuntimeError,'MAC verification failed'):
                                step9a_echoear.verify(Path(directory))
                        self.assertEqual(run.call_args.args[0][-3:],['--port',str(step9a_echoear.BY_ID),'read-mac'])

    def test_flash_requires_success_and_matching_chip_mac(self):
        for code,stdout in [(0,'MAC: 44:1b:f6:84:fc:8c'),(0,''),(1,'MAC: b4:3a:45:16:2d:94')]:
            with self.subTest(code=code,stdout=stdout), patch('board_port.resolve_port',return_value='pinned'), patch('board_port.subprocess.run',return_value=subprocess.CompletedProcess([],code,stdout,'')):
                with self.assertRaises(RuntimeError): board_port.verify_flash_mac()

    def test_matching_chip_mac_allows_only_pinned_port(self):
        with patch('board_port.resolve_port',return_value='pinned'), patch('board_port.subprocess.run',return_value=subprocess.CompletedProcess([],0,'MAC: b4:3a:45:16:2d:94','')) as run:
            self.assertEqual(board_port.verify_flash_mac(),'pinned')
            self.assertEqual(run.call_args.args[0][-3:],['--port','pinned','read-mac'])

if __name__=='__main__': unittest.main()
