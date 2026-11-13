import unittest
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from acoustic_response import fit,predict,filtered_source,calibration_source,TAPS
class AcousticResponse(unittest.TestCase):
    def test_independent_source_predicts_colored_audio(self):
        rng=np.random.default_rng(8)
        x=rng.normal(0,1800,32000)
        h=np.zeros(TAPS);h[TAPS//2]=.3;h[TAPS//2+3]=-.12;h[TAPS//2+17]=.09
        def chain(source,calibration=False):
            matrix=sliding_window_view(np.pad((calibration_source if calibration else filtered_source)(source),(TAPS//2,TAPS//2)),TAPS)[:,::-1]
            return matrix@h*32768
        model={'chains':{'jabra':fit(x,chain(x,True))}}
        self.assertGreater(model['chains']['jabra']['holdout_correlation'],.99)
        distinct=rng.normal(0,2500,20000)
        self.assertGreater(np.corrcoef(predict(distinct,model,'jabra'),chain(distinct))[0,1],.99)
    def test_unrelated_noise_cannot_qualify_calibration(self):
        rng=np.random.default_rng(42)
        with self.assertRaises(RuntimeError):fit(rng.normal(0,1800,32000),rng.normal(0,1800,32000))

class AcousticFaults(unittest.TestCase):
    def test_wrong_program_clipping_and_dropouts_fail_modeled_gate(self):
        from step7_support import acoustic_match,write_pcm
        from pathlib import Path
        import tempfile
        rng=np.random.default_rng(82)
        training=rng.normal(0,2000,16000)
        model={'chains':{'jabra':fit(training,calibration_source(training)*32768)}}
        source=rng.normal(0,5000,8000)
        expected=predict(source,model,'jabra')
        # Severe clipping and 80% packet loss must not be hidden by a frozen FIR.
        clipped=np.clip(expected,-40,40)
        dropped=expected.copy();dropped[np.arange(len(source))%1600<1280]=0
        wrong=predict(rng.normal(0,5000,len(source)),model,'jabra')
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);write_pcm(d/'ref.wav',source)
            for name,observed in [('wrong',wrong),('clipped',clipped),('dropped',dropped)]:
                write_pcm(d/f'{name}.wav',np.r_[np.zeros(6400),observed,np.zeros(3200)])
                m=acoustic_match(d/f'{name}.wav',d/'ref.wav',.3,0,.3,reference_pcm=expected)
                if name in ('wrong','dropped'):
                    self.assertLess(m['correlation'],.6,name)
                # Clipping Gaussian audio can retain high correlation: the
                # quality gate additionally requires per-band SDR >=10 dB.
                from step8b_speech_sweep import band_sdr
                from step8b2_speech_sweep import quality_checks
                segment=np.r_[np.zeros(6400),observed,np.zeros(3200)][round(m['offset_s']*16000):round(m['offset_s']*16000)+len(source)]
                from acoustic_response import fractional_align
                aligned,delay=fractional_align(expected,segment)
                self.assertLessEqual(abs(delay),1)
                chain=dict(correlation=m['correlation'],distortion_sdr_db=band_sdr(aligned,segment))
                self.assertFalse(all(quality_checks({'board':chain,'jabra':chain},10000,0).values()),name)
    def test_calibration_retries_once_and_records_errors(self):
        from acoustic_response import calibrate_response
        from unittest.mock import patch
        from pathlib import Path
        import tempfile,json
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            def capture(*args):
                args[2].mkdir(parents=True)
                raise RuntimeError('holdout rejected')
            with patch('acoustic_response._capture_response',side_effect=capture) as mock:
                with self.assertRaises(RuntimeError):calibrate_response(None,None,d)
                self.assertEqual(mock.call_count,2)
            for i in (1,2):
                self.assertEqual(json.loads((d/f'attempt{i}/calibration-error.json').read_text())['attempt'],i)
    def test_single_failure_then_qualified_second_capture(self):
        from acoustic_response import calibrate_response
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            def capture(board,device,out):
                out.mkdir(parents=True)
                if out.name=='attempt1':raise TimeoutError('greeting still closed gate')
                return {'chains':{}}
            with patch('acoustic_response._capture_response',side_effect=capture):
                model=calibrate_response(None,None,Path(d))
            self.assertEqual(model['qualified_attempt'],2)
            self.assertEqual(len(model['failed_attempts']),1)

class FractionalAlignment(unittest.TestCase):
    def test_single_fractional_delay_recovers_timing_without_band_fits(self):
        from scipy import fft
        from acoustic_response import fractional_align
        rng=np.random.default_rng(33);x=rng.normal(0,1000,16000)
        size=fft.next_fast_len(len(x)+256)
        z=fft.rfft(np.pad(x,(128,size-len(x)-128)))
        y=fft.irfft(z*np.exp(-2j*np.pi*fft.rfftfreq(size)*.43),size)[128:128+len(x)]
        aligned,delay=fractional_align(x,y)
        self.assertAlmostEqual(delay,.43,places=2)
        self.assertGreater(np.corrcoef(aligned,y)[0,1],.9999)
    def test_silence_and_shape_validation(self):
        from acoustic_response import fractional_align
        aligned,delay=fractional_align(np.zeros(1000),np.zeros(1000))
        self.assertEqual(delay,0);self.assertFalse(aligned.any())
        with self.assertRaises(ValueError):fractional_align(np.ones(1000),np.ones(500))

if __name__=='__main__':unittest.main()

def test_rendered_prediction_applies_physical_fir_without_second_dsp():
    from acoustic_response import predict_rendered,TAPS
    rng=np.random.default_rng(888);rendered=rng.normal(0,5000,16000)
    taps=np.zeros(TAPS);taps[TAPS//2]=2
    model=dict(chains=dict(board=dict(fir=taps.tolist())))
    np.testing.assert_allclose(predict_rendered(rendered,model,'board'),2*rendered)
