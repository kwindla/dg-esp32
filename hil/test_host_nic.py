import unittest
from host_nic import gate


class NicTests(unittest.TestCase):
    def sample(self, t, rx=0, tx=0):
        return dict(monotonic_s=t, interfaces={name: dict(rx_bytes=rx, tx_bytes=tx)
                    for name in ('enp7s0', 'wlp8s0')})

    def test_wifi_directions_count_and_wired_is_recorded(self):
        result = gate(self.sample(0), self.sample(300, 4000000, 1000000))
        self.assertTrue(result['passed'])
        self.assertEqual(result['total_bytes'], 5000000)
        self.assertEqual(result['byte_deltas']['enp7s0'],dict(rx_bytes=4000000,tx_bytes=1000000))
        self.assertEqual(result['gated_interface'],'wlp8s0')

    def test_wired_download_does_not_gate_quiet_board_path(self):
        after=self.sample(30)
        after['interfaces']['enp7s0']['rx_bytes']=180000000
        after['interfaces']['wlp8s0']['tx_bytes']=2000000
        result=gate(self.sample(0),after)
        self.assertTrue(result['passed']);self.assertEqual(result['total_bytes'],2000000)
        after['interfaces']['wlp8s0']['rx_bytes']=1
        self.assertFalse(gate(self.sample(0),after)['passed'])

    def test_wired_missing_or_reset_is_observation_error_only(self):
        before=self.sample(0);before['interfaces']['enp7s0']['rx_bytes']=1
        for after in (self.sample(30),dict(monotonic_s=30,interfaces={'wlp8s0':dict(rx_bytes=0,tx_bytes=0)})):
            result=gate(before,after)
            self.assertTrue(result['passed']);self.assertIn('enp7s0',result['observation_errors'])

    def test_limit_scales_to_arm_duration_without_board_subtraction(self):
        self.assertTrue(gate(self.sample(0), self.sample(30, 500000))['passed'])
        self.assertTrue(gate(self.sample(0), self.sample(30, 1000001))['passed'])
        self.assertFalse(gate(self.sample(0), self.sample(30, 1000001,1000000))['passed'])

    def test_missing_reset_or_zero_duration_fails_closed(self):
        for before, after in ((self.sample(0), self.sample(0)),
                              (self.sample(0, 1), self.sample(1)),
                              (self.sample(0), dict(monotonic_s=1, interfaces={}))):
            self.assertFalse(gate(before, after)['passed'])


if __name__ == '__main__':
    unittest.main()
