import tempfile
import unittest
import wave
from pathlib import Path
import numpy as np
from speaker_sweep_test import distortion
class HarmonicAnalysis(unittest.TestCase):
    def test_known_harmonics_and_rms_normalization(self):
        rate=48000;t=np.arange(rate*2)/rate
        x=.2*np.sin(2*np.pi*1000*t)+.006*np.sin(2*np.pi*2000*t)+.008*np.sin(2*np.pi*3000*t)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'tone.wav'
            with wave.open(str(p),'wb') as w:w.setparams((1,2,rate,0,'NONE','not compressed'));w.writeframes((x*32768).astype('<i2').tobytes())
            r=distortion(p,1000,.2,1.8)
        self.assertAlmostEqual(r['fundamental_dbfs'],20*np.log10(.2/np.sqrt(2)),places=2)
        self.assertAlmostEqual(r['thd_percent'],5,places=2)
        self.assertAlmostEqual(r['harmonics_dbc']['H2'],20*np.log10(.03),places=2)
        self.assertAlmostEqual(r['harmonics_dbc']['H3'],20*np.log10(.04),delta=.02)

class PlayoutAcceptance(unittest.TestCase):
    def test_hardware_and_rebuffer_counters_are_required(self):
        from step6b_checks import playout_checks
        good=dict(i2s_underflows=0,rebuffers=0,decode_errors=0,queue_dropped=0,decode_stack=5000,playback_stack_bytes=17408)
        playout_checks(good)
        for key in ['i2s_underflows','rebuffers']:
            bad=good.copy();bad[key]=1
            with self.assertRaises(AssertionError):playout_checks(bad)
            del bad[key]
            with self.assertRaises(AssertionError):playout_checks(bad)


class CampaignPolicy(unittest.TestCase):
    def test_network_p95_does_not_waive_media_or_capture_failures(self):
        from phase1_compare import compare_step6b
        keys=['decode_p95_us','enqueue_send_mean_us','max_gap_ms','p95_gap_ms','missing','i2s_underflows','rebuffers','greeting_correlation','reply_correlation']
        metrics={k:dict(p95=10,mean=5,min=.96,max=0) for k in keys}
        timings={k:dict(p95=10) for k in ['start_ready_us','wifi_to_ready_us','open_connected_us']}
        candidate=dict(boots=1,media_sessions=3,metrics=metrics,timings=timings,failures=[dict(greeting=True,reply=True,acceptance_error='Server RTP loss/timing')],boot_failures=[1],boot_errors=[],audio_counters=[[0,0,0]])
        base=dict(metrics=metrics,timings=timings)
        self.assertTrue(compare_step6b(base,candidate,3)['passed'])
        candidate['failures'][0]['reply']=False
        self.assertFalse(compare_step6b(base,candidate,3)['checks']['media_acceptance'])
        candidate['failures'][0]['reply']=True;candidate['boot_errors']=[dict(error='Capture incomplete')]
        self.assertFalse(compare_step6b(base,candidate,3)['checks']['boot_acceptance'])


class NativeCaptureAnalysis(unittest.TestCase):
    def test_linear_upsampling_is_not_extra_measurement_bandwidth(self):
        rate=16000;t=np.arange(rate*2)/rate
        native=.2*np.sin(2*np.pi*1500*t)+.008*np.sin(2*np.pi*4500*t)
        native=np.rint(native*32768).astype('<i2')
        interpolated=np.interp(np.arange(len(native)*3)/3,np.arange(len(native)),native)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'tone.wav'
            with wave.open(str(p),'wb') as w:
                w.setparams((1,2,48000,0,'NONE','not compressed'));w.writeframes(interpolated.astype('<i2').tobytes())
            p.with_suffix('.alsa.log').write_text('Plug PCM: Rate conversion PCM (16000, sformat=S16_LE)\n')
            r=distortion(p,1500,.2,1.8)
        self.assertEqual(r['native_capture_rate_hz'],16000)
        self.assertEqual(r['recovered_phase'],0)
        self.assertAlmostEqual(r['harmonics_dbc']['H3'],20*np.log10(.04),delta=.02)
        self.assertTrue(r['thd_complete'])
    def test_above_nyquist_harmonics_are_unavailable(self):
        rate=16000;t=np.arange(rate*2)/rate;x=.1*np.sin(2*np.pi*3000*t)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'tone.wav'
            with wave.open(str(p),'wb') as w:
                w.setparams((1,2,rate,0,'NONE','not compressed'));w.writeframes((x*32768).astype('<i2').tobytes())
            r=distortion(p,3000,.2,1.8)
        self.assertFalse(r['thd_complete'])
        self.assertIsNone(r['harmonics_dbc']['H3'])
        self.assertIsNone(r['harmonics_dbc']['H5'])

class ChainCorrection(unittest.TestCase):
    def test_response_correction_removes_chain_boost_and_preserves_nyquist(self):
        from speaker_dual_sweep_test import correct
        response={c:{'1000':-50,'2000':-44,'3000':-30,'4000':-50,'5000':-50,'6000':-50} for c in ('board','jabra')}
        measurement=dict(harmonics_dbc={'H2':-24,'H3':-10,'H4':-60,'H5':-60},clipping_count=0,peak_dbfs=-10)
        row=correct(dict(hz=1000,chains={c:measurement.copy() for c in response}),response)
        for chain in row['chains'].values():
            self.assertAlmostEqual(chain['corrected_harmonics_dbc']['H2'],-30)
            self.assertAlmostEqual(chain['corrected_harmonics_dbc']['H3'],-30)
            self.assertTrue(chain['passed'])
        measurement['harmonics_dbc']={'H2':-60,'H3':None,'H4':None,'H5':None}
        row=correct(dict(hz=3000,chains={c:measurement.copy() for c in response}),response)
        self.assertIsNone(row['chains']['board']['corrected_harmonics_dbc']['H3'])
        self.assertTrue(row['passed'])

if __name__=='__main__':unittest.main()
