import unittest
import numpy as np
from scipy import signal
from echoear_aec_path import path_response,analyze_path
from echoear_aec_alignment import analyze,probe_pcm


class PathTests(unittest.TestCase):
    def setUp(self):
        self.x=np.random.default_rng(47).normal(0,400,32000)

    def test_colored_multipath_predicts_both_halves_without_inventing_one_delay(self):
        h=np.zeros(180);h[9]=-.9;h[19]=1;h[31]=-.7;h[41]=.5
        y=signal.lfilter(h,[1],self.x)
        r=path_response(self.x,y)
        self.assertTrue(r['available'])
        self.assertIsNone(r['scalar_physical_delay_samples']);self.assertIsNone(r['first_arrival_samples'])
        for e in r['estimates']:
            self.assertGreater(e['explained_variance'],.98)
            np.testing.assert_allclose(e['energy_lags_samples'],[9,19,41],atol=2)

    def test_delays_of_both_signs_remain_visible(self):
        for lag in (-37,700):
            y=np.roll(self.x,lag);r=path_response(self.x,y)
            self.assertTrue(r['available'],(lag,r['checks']))
            for e in r['estimates']:np.testing.assert_allclose(e['energy_lags_samples'],[lag]*3,atol=2)

    def test_periodic_excitation_and_sub_lsb_reference_are_unavailable(self):
        sine=1000*np.sin(2*np.pi*1000*np.arange(32000)/16000)
        for x in (sine,self.x/1000):
            r=path_response(x,np.roll(x,20))
            self.assertFalse(r['available']);self.assertFalse(r['checks']['broadband_excitation'])

    def test_independent_noise_and_out_of_window_delay_fail_prediction(self):
        for y in (np.random.default_rng(48).normal(0,400,32000),np.roll(self.x,1500)):
            r=path_response(self.x,y)
            self.assertFalse(r['available']);self.assertFalse(r['checks']['held_out_prediction'])

    def test_changed_path_between_halves_is_not_a_stable_measurement(self):
        y=np.r_[np.roll(self.x,20)[:16000],np.roll(self.x,50)[16000:]]
        r=path_response(self.x,y)
        self.assertFalse(r['available']);self.assertFalse(r['checks']['repeatability'])

    def test_missing_nonfinite_and_short_capture_fail_closed(self):
        for x in (self.x[:-1],np.full(32000,np.nan),np.zeros(32000)):
            self.assertFalse(path_response(x,x)['available'])

    def test_new_diagnostic_keeps_legacy_failure_and_nearend_identity_requirements(self):
        p=probe_pcm();n=64000;ref=np.pad(p,(8000,n-8000-len(p)))
        dac=np.zeros((n,5));dac[:,2]=ref
        h=np.zeros(100);h[9]=-1;h[19]=1
        dac[:,0]=signal.lfilter(h,[1],ref)
        near=np.zeros((n,5));prompt=self.x[:16000];near[8000:24000,0]=prompt
        near[:,4]=np.r_[np.zeros(1024),near[:-1024,0]]
        self.assertFalse(analyze(dac,near,prompt)['passed'])
        r=analyze_path(dac,near,prompt)
        self.assertTrue(r['passed']);self.assertFalse(r['legacy_correlation_analysis']['passed'])
        self.assertFalse(r['acoustic_qualification']);self.assertIsNone(r['erle'])
        near[:,2]=near[:,0]/32
        self.assertFalse(analyze_path(dac,near,prompt)['passed'])


if __name__=='__main__':unittest.main()
