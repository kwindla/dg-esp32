"""Offline witnesses for sampled P4 scan latency; never enters a device adapter."""
import json
import unittest
from p4_display import latency


def sample(identifier=1, outcome='presented', committed=100, completed=36100, timed=True):
    return 'P4_SCAN '+json.dumps(dict(id=identifier, outcome=outcome, committed_us32=committed,
        completed_us32=completed, latency_us=(completed-committed)&0xffffffff, timed=timed, sample_every=16))


class ScanLatency(unittest.TestCase):
    def test_histogram_and_exact_max_use_presented_samples_only(self):
        result = latency([sample(), sample(17, completed=20100), sample(33, 'superseded', completed=10100),
                          sample(49, completed=999999, timed=False), sample(65, completed=40101)])
        self.assertEqual(result['presented_samples'], 3)
        self.assertEqual(result['superseded_samples'], 1); self.assertEqual(result['untimed_samples'], 1)
        self.assertEqual(result['superseded_fraction'], .2); self.assertEqual(result['underrun_lines'], 0)
        self.assertEqual(result['histogram_lower_edge_us'], {'20000': 1, '36000': 1, '40000': 1})
        self.assertEqual(result['min_us'], 20000); self.assertEqual(result['p50_us'], 36000)
        self.assertEqual(result['max_us'], 40001); self.assertEqual(result['p99_us'], 40001)
        self.assertEqual(result['tearing'], 'unverified'); self.assertFalse(result['qualification'])

    def test_micros_wrap_and_log_prefix_are_supported(self):
        result = latency(['I (123) diagnostics: '+sample(committed=0xfffffff0, completed=2000)])
        self.assertEqual(result['max_us'], 2016)

    def test_no_timed_presented_evidence_fails(self):
        for lines in ([], ['other log'], [sample(timed=False)], [sample(outcome='superseded')]):
            with self.subTest(lines=lines), self.assertRaisesRegex(ValueError, 'no timed presented'):
                latency(lines)

    def test_multiple_boots_and_replayed_records_fail(self):
        with self.assertRaisesRegex(ValueError, 'duplicate scan sample'):
            latency([sample(), sample()])

    def test_timestamp_mismatch_and_bad_metadata_fail(self):
        base = json.loads(sample().split('P4_SCAN ', 1)[1])
        for key, value in (('latency_us', 1), ('outcome', 'committed'), ('timed', 1), ('sample_every', 1),
                           ('id', 2), ('id', True), ('committed_us32', -1), ('completed_us32', 1 << 32)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                latency(['P4_SCAN '+json.dumps(dict(base, **{key: value}))])

    def test_superseded_fraction_includes_untimed_outcomes_and_uses_all_records(self):
        result = latency([sample(), sample(17, 'superseded'), sample(33, 'superseded', timed=False)])
        self.assertEqual(result['superseded_samples'], 2); self.assertEqual(result['superseded_fraction'], 2/3)
        self.assertEqual(result['total_records'], 3); self.assertEqual(result['presented_samples'], 1)
        self.assertEqual(sum(result['histogram_lower_edge_us'].values()), 1)

    def test_underrun_lines_are_report_only_and_never_latency_samples(self):
        lines = ['W (50) lcd: underrun happens', sample(), 'other diagnostic', 'W (60) lcd: underrun happens']
        result = latency(lines)
        self.assertEqual(result['underrun_lines'], 2); self.assertEqual(result['presented_samples'], 1)
        self.assertEqual(result['max_us'], 36000); self.assertFalse(result['qualification'])


if __name__ == '__main__': unittest.main()
