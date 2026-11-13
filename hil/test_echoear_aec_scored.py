import copy
import unittest
import numpy as np
from echoear_aec_scored import analyze_original
from echoear_aec_stimuli import trim_word,marker_pcm


class ScoredOriginalTests(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(19)
        self.source=rng.normal(0,1200,160000)
        self.word=rng.normal(0,800,4800)
        self.marker=marker_pcm()
        self.raw=np.zeros((128000,5))
        self.jabra=np.zeros(128000)
        # At0.5s the bot begins. Cancellation at3.1s, real near-end word
        # begins3.0s, late witness5.3s. No generated user/STT stand-in.
        self.raw[8000:49600,2]=self.source[:41600]/50
        self.jabra[8000:49600]=self.source[:41600]
        for x,column in ((self.raw,0),):
            x[48000:52800,column]=self.word
            x[84800:89600,column]=self.marker
        self.jabra[48000:52800]=self.word
        self.jabra[84800:89600]=self.marker
        self.receipts=np.column_stack((np.arange(320,128001,320),10+np.arange(1,401)*.02))
        self.row=dict(kind='barge',user_request_s=12.9,marker_request_s=15.1,
                      generated_before_trigger=len(self.source))
        self.quiet=dict(reference=np.ones(8000),jabra_rms=1,microphone_rms=1)
        self.events=[dict(event='frame',frame='InterruptionFrame',t=13.1),
                     dict(event='frame',frame='TranscriptionFrame',text='Stop.',t=14),
                     dict(event='user-turn-stopped',text='Stop.',t=14)]

    def analyze(self,**kw):
        args=dict(row=copy.deepcopy(self.row),capture={'pcm':self.raw},jabra=self.jabra,
                  receipts=self.receipts,source=self.source,word=self.word,marker=self.marker,
                  quiet=self.quiet,delay=1024,events=self.events)
        args.update(kw)
        return analyze_original(**args)

    def test_bound_has_native_word_marker_source_and_real_interruption(self):
        row=self.analyze()
        self.assertTrue(row['passed'],str(row))
        self.assertLessEqual(row['physical_stop']['latency_upper_s'],1)
        for events in ([],[dict(event='user-turn-stopped',text='Stop.',t=14)]):
            self.assertFalse(self.analyze(events=events)['passed'])

    def test_no_pass_from_early_marker_or_self_suppressed_reference(self):
        row=dict(self.row,marker_request_s=13)
        self.assertFalse(self.analyze(row=row)['passed'])
        raw=self.raw.copy();raw[60000:80000,2]=20
        self.assertFalse(self.analyze(capture={'pcm':raw})['passed'])
        jabra=self.jabra.copy();jabra[48000:52800]=0
        self.assertFalse(self.analyze(jabra=jabra)['passed'])
        delayed=self.receipts.copy();delayed[:,1]+=2
        self.assertFalse(self.analyze(receipts=delayed)['passed'])

    def test_bot_already_silent_before_trigger_is_not_an_interruption(self):
        raw=self.raw.copy();raw[44800:,2]=0
        jabra=self.jabra.copy();jabra[44800:48000]=0
        row=self.analyze(capture={'pcm':raw},jabra=jabra)
        self.assertFalse(row['passed'])
        self.assertFalse(row['native_stop']['active_at_user_onset'])

    def test_no_credit_for_generated_after_trigger_or_missing_tail(self):
        self.assertFalse(self.analyze(row=dict(self.row,generated_before_trigger=20000))['passed'])
        self.assertFalse(self.analyze(jabra=self.jabra[:80000],receipts=self.receipts[:250])['passed'])

    def test_word_preparation_is_finite_and_fixed(self):
        with self.assertRaises(ValueError):trim_word(np.zeros(16000))
        with self.assertRaises(ValueError):trim_word(np.ones(16000)*500)
        self.assertEqual(len(marker_pcm()),4800)
        self.assertTrue(np.array_equal(marker_pcm(),marker_pcm()))


if __name__=='__main__':unittest.main()
