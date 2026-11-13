import unittest
from step9a_fetch_evidence import completion

class FetchEvidence(unittest.TestCase):
    def capture(self,error='none',bytes=306917,persisted=0):
        return '\n'.join(('I display-store: load_terminal epoch=1 order=2 error=none bytes=98787 duration_us=700 persisted=1',
                          'DISPLAY_POST {"id":"in-call-fetch","duplicate":false}',
                          f'I display-store: load_terminal epoch=17 order=998 error={error} bytes={bytes} duration_us=250000 persisted={persisted}',
                          'DISPLAY_POST {"id":"bench-1030","duplicate":false}'))
    def test_ignores_setup_and_retains_completion_without_device_query(self):
        row=completion(self.capture());self.assertEqual(row['epoch'],17);self.assertEqual(row['bytes'],306917);self.assertEqual(row['decimal_kB_per_s'],1227.668)
    def test_rejects_failed_truncated_or_persisted_fetch(self):
        for args in ({'error':'hash'},{'bytes':300000},{'persisted':1}):
            with self.subTest(args=args),self.assertRaises(ValueError):completion(self.capture(**args))
    def test_missing_or_duplicate_terminal_is_unavailable(self):
        text=self.capture()
        for value in (text.split('I display-store: load_terminal epoch=17')[0],text+'\nI display-store: load_terminal epoch=17 order=998 error=none bytes=306917 duration_us=250000 persisted=0'):
            with self.assertRaises(ValueError):completion(value)
    def test_non_admitted_fetch_cannot_be_scored(self):
        with self.assertRaises(ValueError):completion(self.capture().replace('in-call-fetch','other-request'))
