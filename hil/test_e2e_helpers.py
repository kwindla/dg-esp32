import tempfile
import unittest
from pathlib import Path
import numpy as np
from step7_support import acoustic_match,write_pcm,leakage,word_accuracy
class E2EHelpers(unittest.TestCase):
    def test_prompt_match_cannot_select_later_audio(self):
        rng=np.random.default_rng(10);ref=rng.normal(0,2000,16000)
        x=rng.normal(0,2,80000);x[16000:32000]+=ref+rng.normal(0,300,16000);x[48000:64000]+=ref
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.wav';reference=Path(temp)/'ref.wav';write_pcm(source,x);write_pcm(reference,ref)
            found=acoustic_match(source,reference,.9,100,.8,last=2.1)
        self.assertLess(found['end'],102.1)
        self.assertGreater(found['correlation'],.9)
    def test_audible_leakage_cannot_be_satisfied_by_other_closed_samples(self):
        lines=[(1,1,'MIC_GATE t=1 closed=1 pre=-40 post=-120'),(2,2,'MIC_GATE t=2 closed=0 pre=-40 post=-40')]
        with self.assertRaises(AssertionError):leakage(lines,1.7,2.3)
    def test_accuracy_counts_insertions_and_deletions(self):
        self.assertEqual(word_accuracy('The code word is pineapple.','the code word is pineapple'),1)
        self.assertAlmostEqual(word_accuracy('The code word is pineapple.','code word is apple'),.6)

class RetryHeapCensus(unittest.TestCase):
    def test_readiness_accepts_app_transition_and_saved_control_log(self):
        from transport_test import READY_LOG,ready_timestamp
        import re
        app='STATE Ready gen=3 desired=1 t=123456'
        old='on_bot_ready t=123789'
        self.assertRegex(app,READY_LOG);self.assertRegex(old,READY_LOG)
        self.assertEqual(ready_timestamp(app),123456)
        self.assertEqual(ready_timestamp(old),123789)
        self.assertEqual(ready_timestamp(app+'\n'+old,last=False),123456)
        self.assertIsNone(ready_timestamp('STATE Connected gen=3 desired=1 t=123456'))
        self.assertIsNone(re.search(READY_LOG,'STATE Ready gen=3 desired=0 t=123456'))
    def test_internal_retry_keeps_both_setup_baselines(self):
        from transport_test import pair_connect_heaps
        heaps=[('before-connect',210000,0),('closed',200000,0),
               ('before-connect',209500,0),('after-disconnect',209000,0),
               ('before-connect',209000,0),('after-disconnect',209100,0)]
        self.assertEqual(pair_connect_heaps(heaps,2),[(210000,209000),(209500,209000),(209000,209100)])
    def test_missing_completed_disconnect_is_rejected(self):
        from transport_test import pair_connect_heaps
        with self.assertRaises(AssertionError):pair_connect_heaps([('before-connect',210000,0)],1)
    def test_unpaired_snapshot_is_rejected(self):
        from transport_test import pair_connect_heaps
        with self.assertRaises(AssertionError):
            pair_connect_heaps([('before-connect',210000,0),('after-disconnect',209000,0),('before-connect',209000,0)],1)

class PerfConsoleContract(unittest.TestCase):
    def test_snapshot_uses_stats_and_parses_perf_payload(self):
        from transport_test import perf
        class Console:
            cursor=8
            def command(self,command):
                if command!='stats':raise RuntimeError('ERR unsupported command')
            def wait_for(self,pattern,timeout,since):
                if since!=8:raise AssertionError('lost cursor')
                return 'PERF {"device_us":123,"display":{"rendered":7}}'
        self.assertEqual(perf(Console())['display']['rendered'],7)
