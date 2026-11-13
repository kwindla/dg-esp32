import unittest
import numpy as np
from mic_headroom import microphone_headroom


class MicHeadroom(unittest.TestCase):
    def test_protection_bounds_loud_signal_but_does_not_exempt_adc_clips(self):
        profile=dict(schema=1,knee=8192,ceiling=16384)
        raw=np.r_[np.zeros(1600),np.tile([30000,-30000],400)]
        result=microphone_headroom(raw,125,profile)
        self.assertFalse(result['passed'])
        self.assertGreater(result['headroom_db'],6)
        self.assertLess(result['unprotected_headroom_db'],3)
        self.assertGreater(result['unprotected_clips'],0)
        self.assertGreater(result['limited_samples'],0)
        raw[1800]=32767
        result=microphone_headroom(raw,125,profile)
        self.assertFalse(result['passed'])
        self.assertEqual(result['adc_clips'],1)

    def test_engaged_protection_with_unprotected_margin_can_pass(self):
        raw=np.r_[np.zeros(1600),np.tile([12000,-12000],400)]
        result=microphone_headroom(raw,125,dict(schema=1,knee=8192,ceiling=16384))
        self.assertTrue(result['passed'])
        self.assertGreater(result['limited_samples'],0)
        self.assertGreater(result['unprotected_headroom_db'],3)

    def test_unknown_protection_fails_closed(self):
        self.assertFalse(microphone_headroom(np.zeros(3200),125,dict(schema=2))['available'])

    def test_peak_includes_closed_gate_echo_and_gain(self):
        raw = np.r_[np.zeros(1600), 16842/.980742, np.zeros(320)]
        old = microphone_headroom(raw, 200)
        new = microphone_headroom(raw, 125)
        self.assertEqual(old['reconstructed_clips'], 1)
        self.assertFalse(old['passed'])
        self.assertTrue(new['passed'])
        self.assertAlmostEqual(new['headroom_db'], 3.84266, places=4)

    def test_unclipped_but_insufficient_headroom_fails(self):
        raw = np.r_[np.zeros(1600), 16842/.980742, np.zeros(320)]
        result = microphone_headroom(raw, 150)
        self.assertEqual(result['reconstructed_clips'], 0)
        self.assertFalse(result['passed'])

    def test_initialization_does_not_exempt_later_clips(self):
        raw = np.zeros(2000); raw[0] = 32767; raw[1700] = -32768
        result = microphone_headroom(raw, 200)
        self.assertEqual(result['reconstructed_clips'], 1)
        self.assertFalse(result['passed'])

    def test_missing_pcm_fails_closed(self):
        for raw in ([], np.zeros(1600), [float('nan')]*2000):
            self.assertFalse(microphone_headroom(raw, 125)['available'])
