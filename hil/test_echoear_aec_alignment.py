import unittest
import numpy as np
import tempfile
from pathlib import Path
import wave
from echoear_aec_alignment import alignment, analyze, probe_pcm, load_nearend_source


class AlignmentTests(unittest.TestCase):
    def test_independent_common_clock_lag_and_polarity(self):
        x = np.random.default_rng(17).normal(size=32000)
        y = np.r_[np.zeros(37),-x[:-37]]
        r = alignment(x,y)
        self.assertTrue(r['available']);self.assertEqual(r['lag_samples'],37)
        self.assertEqual(r['polarity'],-1)

    def test_periodic_sine_cannot_supply_unambiguous_delay(self):
        x = np.sin(2*np.pi*1000*np.arange(32000)/16000)
        self.assertFalse(alignment(x,np.roll(x,10))['available'])

    def test_full_pipeline_uses_nearend_not_cancellation_to_select_afe_delay(self):
        source=probe_pcm();n=48000
        reference=np.pad(source,(4000,n-len(source)-4000))
        dac=np.zeros((n,5));dac[:,2]=reference;dac[:,0]=np.r_[np.zeros(23),reference[:-23]]
        near=np.zeros((n,5));rng=np.random.default_rng(5)
        prompt=rng.normal(scale=200,size=15000)
        near[5000:20000,0]=prompt
        near[:,4]=np.r_[np.zeros(768),near[:-768,0]]
        result=analyze(dac,near,prompt)
        self.assertTrue(result['passed'])
        self.assertEqual(result['physical_reference_to_mic_peak']['lag_samples'],23)
        self.assertEqual(result['independent_nearend_afe_delay']['lag_samples'],768)
        self.assertEqual(result['source_pattern_offset_in_capture_samples'],4000)
        self.assertIsNone(result['absolute_dac_conversion_delay_samples'])
        self.assertIsNone(result['erle'])
        near[:,2]=near[:,0] # An acoustic reference cannot prove silent-reference AFE delay.
        self.assertFalse(analyze(dac,near,prompt)['passed'])
        self.assertFalse(analyze(dac,near,prompt)['independent_nearend_afe_delay']['available'])

    def test_old_acoustic_reference_and_unplayed_prompt_cannot_pass(self):
        # Astra's exact previous-failure model passed before this regression.
        n=64000;p=np.pad(probe_pcm(),(8000,n-38400-8000))
        dac=np.zeros((n,5));dac[:,0]=np.r_[np.zeros(23),p[:-23]];dac[:,2]=dac[:,0]/32
        near=np.zeros((n,5));near[:,0]=np.random.default_rng(12).normal(0,200,n)
        near[:,2]=near[:,0]/32;near[:,4]=np.r_[np.zeros(768),near[:-768,0]]
        prompt=np.random.default_rng(14).normal(0,200,16000)
        for source in (None,prompt,near[:16000,0]):
            result=analyze(dac,near,source)
            self.assertFalse(result['passed'])
            self.assertFalse(result['nearend_excitation_verified'])
            self.assertFalse(result['independent_nearend_afe_delay']['available'])
        # Even a known prompt that really arrives must reject MIC1/32 as MIC3.
        near[8000:24000,0]+=prompt*10
        near[:,2]=near[:,0]/32;near[:,4]=np.r_[np.zeros(768),near[:-768,0]]
        result=analyze(dac,near,prompt)
        self.assertTrue(result['nearend_excitation_verified'])
        self.assertFalse(result['reference_silent_for_independent_afe'])
        self.assertFalse(result['passed'])

    def test_weak_but_correlated_prompt_cannot_supply_delay(self):
        n=64000;p=np.pad(probe_pcm(),(8000,n-38400-8000))
        dac=np.zeros((n,5));dac[:,2]=p;dac[:,0]=np.r_[np.zeros(23),p[:-23]]
        prompt=np.random.default_rng(18).normal(0,200,16000)
        near=np.zeros((n,5));near[:,0]=np.random.default_rng(19).normal(0,100,n)
        near[8000:24000,0]+=prompt
        near[:,4]=np.r_[np.zeros(768),near[:-768,0]]
        result=analyze(dac,near,prompt)
        self.assertTrue(result['known_nearend_source_to_mic']['available'])
        self.assertLess(result['nearend_mic_active_vs_quiet_db'],10)
        self.assertFalse(result['nearend_excitation_verified'])
        self.assertFalse(result['independent_nearend_afe_delay']['available'])
        self.assertFalse(result['passed'])

    def test_exact_frozen_prompt_resampling_and_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'prompt.wav'
            original=(1000*np.sin(2*np.pi*1000*np.arange(48000)/48000)).astype('<i2')
            with wave.open(str(path),'wb') as w:
                w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(original.tobytes())
            source=load_nearend_source(path)
            self.assertEqual(len(source),16000)
            self.assertGreater(np.corrcoef(source[100:-100],original[::3][100:-100])[0,1],.999)
            with wave.open(str(path),'wb') as w:
                w.setparams((2,2,48000,0,'NONE','not compressed'));w.writeframes(original.tobytes())
            with self.assertRaises(ValueError):load_nearend_source(path)

    def test_incomplete_or_silent_data_does_not_invent_delay(self):
        self.assertFalse(alignment(np.zeros(32000),np.zeros(32000))['available'])
        self.assertFalse(analyze(np.zeros((100,3)),np.zeros((100,3)))['passed'])


if __name__=='__main__':unittest.main()
