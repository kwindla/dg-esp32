"""Host-only network version-policy tests; serial, UDP and TCP are mocked."""
import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
FW = b'HOSTED FW rpc_rc=-1 version=0.0.0 revision=0 prerelease=0 build=0'
BOOT = [
    b'P4_NET boot',
    b'I (3625) transport: Base transport is set-up, TRANSPORT_TX_ACTIVE',
    b'I (3626) H_API: Transport active',
    FW,
    b'API connect rc=0 name=ESP_OK',
    b'WIFI IP=192.0.2.33',
    b'API sta_get_ap_info rc=0 name=ESP_OK',
    b'AP bssid=00:00:00:00:00:01 rssi=-47 phy_11ax=1',
    b'API statis_dump rc=262 name=ESP_ERR_NOT_SUPPORTED',
    b'API set_ps_none rc=0 name=ESP_OK',
    b'API set_tx_done_cb not linkable',
    b'P4_NET READY uptime_us=9035202',
]


class SlaveVersionTests(unittest.TestCase):
    def campaign(self, boot=BOOT, allow=False, error=None, udp_error=None, tcp_end=b'TCP END bytes=100 errno=0'):
        spec=importlib.util.spec_from_file_location('p4_run_net_fix3', ROOT/'spikes/p4_net/host/run_net.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.board=Mock();self.board.__enter__=Mock(return_value=self.board)
        self.board.__exit__=Mock(return_value=False)
        self.board.until.side_effect=[boot, [tcp_end]]
        def command(text, *args):
            if text=='stop': return [b'UDP END received=3 sent=3 echo_send_errors=0']
            if text=='netstats': return [b'NETPROBE {"paths":[]}']
            if text=='netdump': return [b'NETPROBE_DUMP END records=0']
            return []
        self.board.command.side_effect=command
        def streams(*args, **kwargs):
            kwargs['ready']()
            if udp_error: raise udp_error
            kwargs['finish']()
        self.udp=Mock(side_effect=streams)
        netprobe=types.SimpleNamespace(run_streams=self.udp,
            analyze_streams=Mock(return_value={'esp-0': {'rtt_ms': {'p50': 1}}}), decode_dump=Mock(return_value=[]))
        self.child=Mock();self.child.returncode=0;self.child.poll.return_value=0
        with tempfile.TemporaryDirectory() as tmp:
            argv=['run_net.py', '--out', tmp] + (['--allow-unknown-slave-version'] if allow else [])
            with patch('sys.argv', argv), patch.dict('sys.modules', {'netprobe': netprobe}), \
                 patch.object(module, 'Board', return_value=self.board), patch.object(module.time, 'sleep'), \
                 patch.object(module.subprocess, 'Popen', return_value=self.child) as self.tcp:
                if error:
                    with self.assertRaisesRegex(RuntimeError, error): module.main()
                else:
                    module.main()
            return json.loads((Path(tmp)/'result.json').read_text())

    def test_unknown_version_aborts_by_default_before_udp_and_tcp(self):
        result=self.campaign(error='Slave firmware RPC unavailable')
        self.assertEqual(result['slave_version'], 'unavailable (rpc_rc=-1)')
        self.assertFalse(result['slave_version_override'])
        self.assertIn('--allow-unknown-slave-version', result['error'])
        self.udp.assert_not_called();self.tcp.assert_not_called();self.board.command.assert_not_called()

    def test_flag_runs_udp_and_tcp_and_preserves_version_and_evidence(self):
        result=self.campaign(allow=True)
        self.assertEqual(result['slave_version'], 'unavailable (rpc_rc=-1)')
        self.assertTrue(result['slave_version_override'])
        self.assertEqual(result['boot'], [row.decode() for row in BOOT])
        evidence=result['connection_evidence']
        self.assertEqual(evidence['transport'], [row.decode() for row in BOOT[1:3]])
        self.assertEqual(evidence['join'], ['WIFI IP=192.0.2.33'])
        self.assertEqual(evidence['ap'], [BOOT[7].decode()])
        self.assertIn('API set_ps_none rc=0 name=ESP_OK', evidence['api'])
        self.assertIn('API statis_dump rc=262 name=ESP_ERR_NOT_SUPPORTED', evidence['api'])
        self.assertIn('API set_tx_done_cb not linkable', evidence['api'])
        self.udp.assert_called_once();self.tcp.assert_called_once()
        self.board.command.assert_any_call('udp 192.168.7.222 47010 120 50 130')
        self.board.command.assert_any_call('stop', 10)
        self.board.command.assert_any_call('tcp 192.168.7.222 47011 60')
        self.assertEqual(result['udp_device']['received'], 3)
        self.assertEqual(result['udp_host']['esp-0']['rtt_ms']['p50'], 1)
        self.assertEqual(result['tcp_device'], {'bytes': 100, 'errno': 0})
        self.assertNotIn('error', result)

    def test_known_version_works_with_and_without_flag(self):
        boot=[row if row!=FW else b'HOSTED FW rpc_rc=0 version=2.12.4' for row in BOOT]
        for allow in (False, True):
            with self.subTest(allow=allow):
                result=self.campaign(boot=boot, allow=allow)
                self.assertEqual(result['slave_version'], '2.12.4')
                self.assertFalse(result['slave_version_override'])
                self.udp.assert_called_once();self.tcp.assert_called_once()

    def test_flag_requires_transport_and_join_evidence(self):
        for remove in (lambda row: b'TRANSPORT_TX_ACTIVE' in row or b'H_API: Transport active' in row,
                       lambda row: row.startswith(b'WIFI IP=')):
            with self.subTest(remove=remove):
                result=self.campaign(boot=[row for row in BOOT if not remove(row)], allow=True,
                                     error='requires active transport and WiFi join evidence')
                self.assertFalse(result['slave_version_override'])
                self.udp.assert_not_called();self.tcp.assert_not_called()

    def test_flag_does_not_invent_rpc_rc_for_missing_or_malformed_record(self):
        for replacement in (None, b'HOSTED FW rpc_rc=oops version=0.0.0', b'HOSTED FW rpc_rc=-1 version=broken'):
            with self.subTest(replacement=replacement):
                boot=[row for row in BOOT if row!=FW]
                if replacement: boot.insert(3, replacement)
                result=self.campaign(boot=boot, allow=True, error='Missing or malformed')
                self.assertEqual(result['slave_version'], 'unavailable (missing or malformed firmware-version record)')
                self.udp.assert_not_called();self.tcp.assert_not_called()

    def test_other_rpc_failure_is_recorded_exactly(self):
        boot=[row if row!=FW else b'HOSTED FW rpc_rc=263 version=0.0.0' for row in BOOT]
        result=self.campaign(boot=boot, allow=True)
        self.assertEqual(result['slave_version'], 'unavailable (rpc_rc=263)')
        self.assertTrue(result['slave_version_override'])

    def test_flag_keeps_udp_failure_fatal_and_evidence_durable(self):
        result=self.campaign(allow=True, udp_error=RuntimeError('UDP test failed'), error='UDP test failed')
        self.assertEqual(result['slave_version'], 'unavailable (rpc_rc=-1)')
        self.assertTrue(result['connection_evidence']['transport'])
        self.assertIn('UDP test failed', result['error'])
        self.tcp.assert_not_called()

    def test_flag_keeps_tcp_failure_fatal_and_evidence_durable(self):
        result=self.campaign(allow=True, tcp_end=b'TCP END bytes=0 errno=104', error='TCP device transfer failed')
        self.assertEqual(result['slave_version'], 'unavailable (rpc_rc=-1)')
        self.assertTrue(result['connection_evidence']['join'])
        self.assertEqual(result['udp_device']['received'], 3)
        self.assertIn('TCP device transfer failed', result['error'])
        self.tcp.assert_called_once()


if __name__=='__main__': unittest.main()
