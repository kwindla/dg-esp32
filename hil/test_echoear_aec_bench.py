import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
from contextlib import nullcontext
import wave

import numpy as np
from alsa import play_wav
from echoear_aec_bench import analyze
from echoear_aec_metrics import RATE, AFE_DELAY, post_opus_residual, character_health
from echoear_aec_timing import sample_interval


class BenchTest(unittest.TestCase):
    def test_alsa_resampling_and_duration(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'test.wav'
            with wave.open(str(path), 'wb') as w:
                w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                w.writeframes(np.ones(16000, dtype='<i2').tobytes())
            with patch('alsa.subprocess.run') as run, patch('alsa.time.monotonic', side_effect=[1, 2]):
                result = play_wav(path, 'test-device')
                self.assertTrue(result['duration_valid'])
                self.assertEqual(len(run.call_args.kwargs['input']), 96000)
                self.assertIn('48000', run.call_args.args[0])
            with patch('alsa.subprocess.run'), patch('alsa.time.monotonic', side_effect=[1, 1.33]):
                with self.assertRaisesRegex(RuntimeError, 'duration mismatch'):
                    play_wav(path, 'test-device')

    def test_independent_modes_and_legacy_missing_timing(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            x = np.zeros((4*RATE, 5))
            x[:, 0] = 1000
            x[RATE:2*RATE, 2] = 100
            x[AFE_DELAY:, 4] = x[:-AFE_DELAY, 0]/10
            np.savez(d/'capture.npz', pcm=x, rate=RATE)
            np.savez(d/'quiet.npz', pcm=np.ones_like(x), rate=RATE)
            for mode in ('quiet', 'probe', 'speech', 'near', 'doubletalk', 'live'):
                result = analyze(mode, d/'capture.npz', d/'quiet.npz', dict(far_samples=RATE))
                self.assertEqual(result['mode'], mode)
            events = [dict(kind='server-frame', frame='BotStartedSpeakingFrame', frame_id=1, monotonic_ns=10**9)]
            result = analyze('live', d/'capture.npz', d/'quiet.npz', {}, events=events)
            self.assertEqual(result['denominator'], 1)
            self.assertEqual(result['unscorable_attempts'], 1)
            self.assertEqual(result['missing_playback'], 0)

    def test_post_opus_mapping_requires_evidence_and_coverage(self):
        mask = np.ones(10, dtype=bool)
        uplink = np.full(5000, 10.)
        self.assertFalse(post_opus_residual(uplink, mask, [])['scorable'])
        mapping = [dict(board_start=AFE_DELAY, board_end=AFE_DELAY+3200, uplink_start=0,
                        gap_free=True, evidence='independent-near-calibration-and-RTP.json')]
        result = post_opus_residual(uplink, mask, mapping)
        self.assertTrue(result['scorable'])
        self.assertAlmostEqual(result['residual_dbfs'], -70.30899869919436)
        mapping[0]['gap_free'] = False
        self.assertFalse(post_opus_residual(uplink, mask, mapping)['scorable'])

    def test_sample_bounds_and_gap(self):
        anchors = [dict(coordinate=0, lower_s=10, upper_s=10.01),
                   dict(coordinate=1000000, lower_s=11, upper_s=11.01)]
        self.assertEqual(sample_interval(100, [[0, 320, 100, 10000, 0]], anchors), [10, 11.01])
        self.assertIsNone(sample_interval(100, [[0, 320, 100, 10000, 1]], anchors))

    def test_character_activity_without_fps_floor(self):
        rows = [dict(enabled=True, errors=0, posts=5), dict(enabled=True, errors=0, posts=6)]
        self.assertTrue(character_health(rows, 0)['passed'])
        self.assertFalse(character_health(rows, 1)['passed'])
        rows[1]['posts'] = 5
        self.assertFalse(character_health(rows, 0)['passed'])

    def test_missing_flash_dependency_fails_before_bench_ownership(self):
        from echoear_aec_bench import acquire
        with patch('step9a_echoear.check_esptool', side_effect=RuntimeError('esptool unavailable')), \
             patch('echoear_aec_bench.bench_lock') as lock:
            with self.assertRaisesRegex(RuntimeError, 'esptool unavailable'):
                acquire('quiet', {}, Path('/unused'))
            lock.assert_not_called()

    def test_failed_candidate_still_restores_and_retains_failure(self):
        from echoear_aec_bench import acquire, NORMAL_HASH
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            def mixer(args, **kwargs):
                if args[0] == 'alsactl' and args[3] == 'store':
                    Path(args[2]).write_text('unchanged mixer')
            restored = Mock()
            with patch('step9a_echoear.check_esptool'), \
                 patch('echoear_aec_bench.bench_lock', return_value=nullcontext()), \
                 patch('echoear_aec_bench.verify_bundle'), \
                 patch('echoear_aec_bench.jabra_card_zero'), \
                 patch('alsa.resolve_jabra', return_value=('Jabra', 'test')), \
                 patch('echoear_aec_bench.subprocess.run', side_effect=mixer), \
                 patch('echoear_aec_bench.flash', side_effect=[RuntimeError('candidate failed'), None]) as flash, \
                 patch('echoear_aec_bench.boot', return_value=restored):
                with self.assertRaisesRegex(RuntimeError, 'candidate failed'):
                    acquire('quiet', dict(image='candidate', elf_sha256='candidate-hash'), out)
            self.assertEqual([c.args[3] for c in flash.call_args_list], ['candidate', 'restore'])
            restored.close.assert_called_once()
            result = json.loads((out/'acquisition.json').read_text())
            self.assertFalse(result['acquisition_complete'])
            self.assertEqual(result['restored_elf_sha256'], NORMAL_HASH)
            self.assertEqual(result['cleanup_errors'], [])


if __name__ == '__main__':
    unittest.main()
