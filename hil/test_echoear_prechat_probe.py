import unittest
from echoear_prechat_probe import CLIP_COUNTERS, TRANSPORT_COUNTERS, strict_session


class PrechatProbeTests(unittest.TestCase):
    def sample(self):
        return dict(playout_schema=2, playout={'rejected': {'source':0, 'preconnect':0}},
            **{k:0 for k in TRANSPORT_COUNTERS + CLIP_COUNTERS + ('preconnect_packets',)})

    def test_startup_rejections_cannot_be_subtracted_as_baseline(self):
        sample = self.sample()
        self.assertTrue(strict_session(sample)['passed'])
        sample['playout_dropped'] = 3
        self.assertFalse(strict_session(sample)['passed'])
        sample['playout_dropped'] = 0
        sample['playout']['rejected']['preconnect'] = 3
        self.assertFalse(strict_session(sample)['passed'])

    def test_every_counter_is_required_and_zero_including_edits_and_closed_clips(self):
        for key in TRANSPORT_COUNTERS + CLIP_COUNTERS + ('preconnect_packets',):
            for value in (None, 1, -1, False):
                with self.subTest(key=key, value=value):
                    sample = self.sample()
                    sample[key] = value
                    self.assertFalse(strict_session(sample)['passed'])
            sample = self.sample()
            del sample[key]
            self.assertFalse(strict_session(sample)['passed'])

    def test_missing_schema_or_rejection_evidence_fails(self):
        for field in ('playout_schema', 'playout'):
            sample = self.sample()
            del sample[field]
            self.assertFalse(strict_session(sample)['passed'])


if __name__ == '__main__':
    unittest.main()
