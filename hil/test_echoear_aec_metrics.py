import unittest
import numpy as np
from echoear_aec_metrics import (RATE, FRAME, AFE_DELAY, activity, echo_metrics,
                                 near_metrics, missing_playback, interval_latency)
from echoear_aec_timing import map_interval, transaction_anchor


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.raw = np.full(4*RATE, 1000.)
        self.ref = np.full(4*RATE, 100.)
        self.quiet = np.ones(RATE)
        self.afe = np.r_[np.zeros(AFE_DELAY), self.raw/10]

    def test_known_attenuation_and_startup_leak(self):
        r = echo_metrics(self.raw, self.afe, self.ref, self.quiet, [(0, 3*RATE)])
        self.assertAlmostEqual(r['integrated']['integrated_reduction_db'], 20)
        self.afe[AFE_DELAY:AFE_DELAY+RATE//2] = self.raw[:RATE//2]
        r = echo_metrics(self.raw, self.afe, self.ref, self.quiet, [(0, 3*RATE)])
        self.assertAlmostEqual(r['replies'][0]['first_500ms']['integrated_reduction_db'], 0)
        self.assertLess(r['integrated']['integrated_reduction_db'], 8)
        self.assertAlmostEqual(r['integrated']['median_frame_reduction_db'], 20)

    def test_high_gain_quiet_never_active(self):
        noise = np.random.default_rng(1).normal(0, 1000, 4*RATE)
        self.assertFalse(activity(noise, noise, [(0, len(noise))]).any())
        self.assertFalse(activity(self.ref, self.quiet, []).any())

    def test_each_reply_has_own_startup(self):
        self.ref[RATE:2*RATE] = 0
        self.afe[AFE_DELAY+2*RATE:AFE_DELAY+2*RATE+RATE//2] = 1000
        r = echo_metrics(self.raw, self.afe, self.ref, self.quiet,
                         [(0, RATE), (2*RATE, 3*RATE)])
        self.assertEqual(len(r['replies']), 2)
        self.assertEqual(r['replies'][1]['reference_onset_sample'], 2*RATE)
        self.assertAlmostEqual(r['replies'][1]['first_500ms']['integrated_reduction_db'], 0)

    def test_missing_tail_not_zero_padding(self):
        r = echo_metrics(self.raw, self.afe[:len(self.raw)], self.ref, self.quiet,
                         [(0, len(self.raw))])
        self.assertFalse(r['integrated']['scorable'])

    def test_near_preservation(self):
        r = near_metrics(self.raw, np.r_[np.zeros(AFE_DELAY), self.raw], [(RATE, 2*RATE)])
        self.assertEqual(r['near_only_attenuation_db'], 0)
        self.assertTrue(r['within_preservation_bound'])

    def test_missing_playback_denominator_and_uncertainty(self):
        self.assertEqual(missing_playback([2, 2.01], [], [0, 4])['outcome'], 'missing-playback')
        self.assertTrue(missing_playback(None, [], None)['failed'])
        self.assertEqual(missing_playback([2, 2.01], [[2.3, 2.4]], [0, 4])['outcome'], 'playback')
        self.assertEqual(missing_playback([2, 2.01], [[3, 3.1]], [0, 4])['outcome'], 'timing-unscorable')

    def test_timing_is_bounded_and_not_extrapolated(self):
        anchors = [transaction_anchor(100, 10_000_000_000, 10_010_000_000),
                   transaction_anchor(200, 11_000_000_000, 11_020_000_000)]
        self.assertEqual(map_interval(150, anchors), [10, 11.02])
        self.assertIsNone(map_interval(99, anchors))
        self.assertEqual(interval_latency([10, 10.1], [10.2, 10.4])['upper_s'], .40000000000000036)
        self.assertFalse(interval_latency(None, [1, 2])['scorable'])


if __name__ == '__main__':
    unittest.main()
