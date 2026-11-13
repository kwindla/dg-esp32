"""Boundary attribution must not confuse a delayed receive with a late send."""
import unittest
from echoear_uplink_analysis import joined_intervals, summarize_joined


def packet(seq, timestamp, observed_ms, ssrc=6, pc='p'):
    return dict(seq=seq, timestamp=timestamp, observed_ms=observed_ms, ssrc=ssrc, pc_id=pc)


def send(packet, device_us):
    return dict(packet, kind=12, device_us=device_us)


class UplinkBoundaryTests(unittest.TestCase):
    def compare(self, device_gap, host_gap):
        # First packet establishes startup origin; measured pair is past 5 s.
        origin = packet(65534, 2**32-960, 1000)
        a, b = packet(65535, 0, 7000), packet(0, 960, 7000+host_gap)
        pairs = joined_intervals([origin, a, b], [send(a, 123456789), send(b, 123456789+device_gap)], 7, 7.2)
        return pairs[-1], summarize_joined(pairs)

    def test_arbitrary_clock_offset_and_sequence_wrap_preserve_pair(self):
        row, _ = self.compare(20000, 95)
        self.assertEqual(row['device_gap_us'], 20000)
        self.assertEqual(row['spacing_change_ms'], 75)
        self.assertEqual(row['attribution'], 'after ESP socket send-return')

    def test_real_device_gap_stays_before_boundary(self):
        row, _ = self.compare(81000, 95)
        self.assertEqual(row['attribution'], 'before/at ESP socket send-return')

    def test_exact_sixty_is_not_greater_than_sixty(self):
        row, _ = self.compare(60000, 61)
        self.assertEqual(row['attribution'], 'after ESP socket send-return')
        row, _ = self.compare(60001, 61)
        self.assertEqual(row['attribution'], 'before/at ESP socket send-return')
        row, _ = self.compare(20000, 60)
        self.assertIsNone(row['attribution'])

    def test_missing_send_fails_complete_coverage_and_keeps_host_tail(self):
        origin, a, b = packet(1, 0, 1000), packet(2, 960, 7000), packet(3, 1920, 7090)
        joined = joined_intervals([origin, a, b], [send(b, 1000)], 7.01, 7.1)
        summary = summarize_joined(joined)
        self.assertEqual(summary['host_over60'], 1)
        self.assertFalse(summary['complete'])
        self.assertIsNone(summary['device_tail_rate'])
        self.assertEqual(summary['host_tails_attributed'], {'unmatched': 1})

    def test_ssrc_collision_cannot_supply_missing_send(self):
        a, b = packet(1, 0, 7000), packet(2, 960, 7090)
        other = packet(1, 0, 7000, ssrc=7)
        joined = joined_intervals([a, b], [send(other, 1000), send(b, 21000)], 7, 7.1)
        self.assertIsNone(joined[0]['device_gap_us'])

    def test_multiple_peer_sessions_require_explicit_filter(self):
        a, b = packet(1, 0, 7000), packet(2, 960, 7090, pc='other')
        with self.assertRaises(ValueError):joined_intervals([a, b], [], 7, 7.1)

    def test_receive_skip_does_not_invent_a_device_send_stall(self):
        origin = packet(0, 0, 1000)
        a, middle, b = packet(1, 960, 7000), packet(2, 1920, 7100), packet(3, 2880, 7090)
        trace = [send(a, 100000), send(middle, 140000), send(b, 180000)]
        joined = joined_intervals([origin, a, b, middle], trace, 7.01, 7.2)
        self.assertEqual(joined[0]['device_gap_us'], 80000)
        self.assertEqual(joined[0]['attribution'], 'after ESP socket send-return (intervening sends)')
        self.assertEqual(joined[0]['device_intervening_sends'], 1)
        self.assertEqual(joined[0]['device_max_adjacent_gap_us'], 40000)
        summary = summarize_joined(joined)
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['device_over60'], 0)
        self.assertEqual(summary['receive_reordered_pairs'], 1)

    def test_intervening_send_cannot_hide_a_real_source_stall(self):
        a, middle, b = packet(1, 960, 7000), packet(2, 1920, 7120), packet(3, 2880, 7110)
        trace = [send(a, 100000), send(middle, 180000), send(b, 200000)]
        joined = joined_intervals([a, b, middle], trace, 7.01, 7.2)
        self.assertEqual(joined[0]['attribution'], 'before/at ESP socket send-return')


if __name__ == '__main__':unittest.main()
