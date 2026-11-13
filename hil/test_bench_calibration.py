import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import numpy as np
from bench_calibration import dbfs, active_mask, playback, calibrate, SENSITIVITY_DBFS

class BenchCalibration(unittest.TestCase):
    def test_gain_path_requires_known_protection_readback(self):
        from bench_calibration import echoear_gain_path
        board=MagicMock()
        board.wait_for.return_value='MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=125'
        board.lines_since.return_value=[(0,0,'MIC_PROTECTION schema=1 knee=8192 ceiling=16384 limited_samples=12 adc_clips=0')]
        result=echoear_gain_path(board)
        self.assertEqual(result['protection'],dict(schema=1,knee=8192,ceiling=16384))
        self.assertEqual(result['limited_samples'],12)
        for line in ('','MIC_PROTECTION schema=2 knee=8192 ceiling=16384 limited_samples=12 adc_clips=0'):
            board.lines_since.return_value=[(0,0,line)]
            with self.assertRaises(AssertionError):echoear_gain_path(board)

    def test_spl_arithmetic_and_source_activity(self):
        level = dbfs(np.ones(16000)*32768*10**(-55/20))
        self.assertAlmostEqual(94+level-SENSITIVITY_DBFS, 65)
        self.assertFalse(active_mask(np.zeros(16000)).any())
        mask=active_mask(np.r_[np.zeros(8000),np.ones(8000)*1000,np.zeros(8000)])
        self.assertTrue(mask[8200:15800].all())
        self.assertFalse(mask[:7800].any())
    def test_clipped_host_playback_fails(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): playback(Path(d)/'clipped.wav',np.ones(1000)*40000)
    def run_calibration(self, levels, echoear=False, analog=24, digital=400, output_reference=True):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        directory=Path(tmp.name)
        board=MagicMock()
        rows=[dict(estimated_spl=None if echoear else s, active_rms_dbfs=s,scale=.1,path='prompt.wav') for s in ([-50] if echoear else [60])+levels]
        with patch.dict(os.environ, {'DG_BOARD':'echoear' if echoear else 'waveshare_146'}), \
             patch('bench_calibration.resolve_jabra',return_value=('card','device')), \
             patch('bench_calibration.set_volumes'), patch('bench_calibration.time.sleep'), \
             patch('bench_calibration.dump_mic'),patch('bench_calibration.play_wav'), \
             patch('bench_calibration.capture',return_value={}) as capture, \
             patch('bench_calibration.echoear_gain_path',return_value=dict(analog_db=analog,register=0x10|analog//3,digital_percent=digital)), \
             patch('bench_calibration.playback'),patch('bench_calibration.pcm',return_value=np.ones(32000)*100), \
             patch('bench_calibration.record_prompt',side_effect=rows) as prompt:
            try: result=calibrate(board,'device',directory,65,output_reference=output_reference)
            except RuntimeError: result=json.loads((directory/'calibration.json').read_text())
        self.assertEqual(capture.call_count,int(output_reference))
        self.assertFalse(any(call.args[0].startswith('mic-gain ') for call in board.command.call_args_list))
        return result,prompt.call_args_list
    def test_convergence_accepts_within_one_db(self):
        result,calls=self.run_calibration([63,64.6])
        self.assertTrue(result['passed']);self.assertEqual(len(result['adjustments']),2)
        self.assertAlmostEqual(calls[1].args[4],.1*10**(5/20))
        self.assertAlmostEqual(calls[2].args[4],calls[1].args[4]*10**(2/20))
    def test_echoear_uses_measured_pcm_without_claiming_spl(self):
        result,calls=self.run_calibration([-47,-45.4],echoear=True)
        self.assertTrue(result['passed'])
        self.assertEqual(result['target_active_rms_dbfs'],-45)
        self.assertEqual(result['digital_gain_percent'],400)
        self.assertIsNone(result['target_spl'])
        self.assertIsNone(result['sensitivity_dbfs'])
        self.assertIsNone(result['prompt']['estimated_spl'])
        self.assertAlmostEqual(calls[1].args[4],.1*10**(5/20))

    def test_30db_calibration_preserves_physical_prompt_and_headroom_gain(self):
        result,calls=self.run_calibration([-39.4],echoear=True,analog=30,digital=125)
        self.assertTrue(result['passed'])
        self.assertEqual(result['analog_gain_db'],30)
        self.assertEqual(result['gain_register'],0x1a)
        self.assertEqual(result['target_active_rms_dbfs'],-39)
        self.assertEqual(result['digital_gain_percent'],125)
        self.assertAlmostEqual(result['nominal_post_gain_target_dbfs'],-37.06179974)
        self.assertIsNone(result['target_spl'])

    def test_calibration_retains_explicit_gain_for_comparisons(self):
        for digital in (100,125,200):
            with self.subTest(digital=digital):
                result,_=self.run_calibration([-39.4],echoear=True,analog=30,digital=digital)
                self.assertEqual(result['digital_gain_percent'],digital)
                self.assertEqual(result['target_active_rms_dbfs'],-39)

    def test_warm_calibration_omits_direct_tone_but_keeps_prompt_gate(self):
        result,calls=self.run_calibration([-39.4],echoear=True,analog=30,output_reference=False)
        self.assertTrue(result['passed'])
        self.assertFalse(result['output_reference']['available'])
        self.assertEqual(result['target_active_rms_dbfs'],-39)
        self.assertTrue(calls)

    def test_five_failed_adjustments_retained(self):
        result,calls=self.run_calibration([60]*5)
        self.assertFalse(result['passed']);self.assertEqual(len(result['adjustments']),5)
        self.assertEqual(len(calls),6)

class ToneTransfer(unittest.TestCase):
    def test_echoear_reply_does_not_use_waveshare_sensitivity(self):
        from playback_quality import jabra_level_gate
        result=jabra_level_gate(np.ones(1600)*1000,20,{},board='echoear')
        self.assertFalse(result['passed'])
        self.assertTrue(result['available'])
        self.assertIsNone(result['board_mic_tone_transfer_spl'])

    def test_transfer_is_board_position_spectral_estimate_only(self):
        import step8b2_spl
        self.assertAlmostEqual(step8b2_spl.transferred_spl(-50,-40,-42),68)
        self.assertFalse(hasattr(step8b2_spl,'range_at_one_m'))
        self.assertIn('board microphone only',step8b2_spl.__doc__)
