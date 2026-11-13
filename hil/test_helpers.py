"""Regression checks for false-positive prevention; no hardware required."""
from collections import deque
from pathlib import Path
import tempfile
import contextlib
import importlib.util
import io
import threading
import unittest
from unittest import mock
import wave
import numpy as np
from alsa import analyze_wav, band_rise_db, tone_windows
from esp_serial import EspSerial, port_lock

class HelpersTest(unittest.TestCase):
    def test_latest_gate_after_serial_ring_eviction(self):
        esp = EspSerial.__new__(EspSerial)
        esp._cv = threading.Condition()
        esp._lines = deque([(20001, 1, 'GATE open'),
                            (20002, 2, 'GATE closed'), (20003, 3, 'MIC noise')])
        esp._cursor = 20003
        esp._error = None
        self.assertEqual(esp.latest_entry(r'GATE (closed|open)')[0], 20002)
        self.assertIsNone(esp.latest_entry('absent'))
        esp._error = OSError('disconnected')
        with self.assertRaises(RuntimeError):
            esp.latest_entry('GATE')

    def test_stale_reply_and_reader_error(self):
        esp = EspSerial.__new__(EspSerial)
        esp._cv = threading.Condition()
        esp._lines = deque([(1, 0, "OK #1")])
        esp._cursor = 1
        esp._error = None
        with self.assertRaises(TimeoutError):
            esp.wait_for(r"OK #1", .01, since=1)
        self.assertEqual(esp.wait_for(r"OK #1", .01, since=0), "OK #1")
        esp._error = OSError("disconnected")
        with self.assertRaises(RuntimeError):
            esp.wait_for("anything", .01)

    def test_config_override_missing_and_redaction(self):
        script = Path(__file__).resolve().parents[1] / "scripts/check-sdkconfig.py"
        spec = importlib.util.spec_from_file_location("config_check", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "sdkconfig.defaults").write_text('CONFIG_TEST=1\nCONFIG_REQUIRED=1\n')
            (project / "sdkconfig.local").write_text('CONFIG_TEST="test-private-value"\n')
            (project / "sdkconfig").write_text('CONFIG_TEST="test-private-value"\nCONFIG_REQUIRED=1\n')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertFalse(module.check(project))
                (project / "sdkconfig").write_text("")
                self.assertTrue(module.check(project))
            self.assertNotIn("test-private-value", output.getvalue())

    def test_lock_contention(self):
        # Exercise flock without contending with a real, authorized HIL run.
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "serial.lock"
            with mock.patch("esp_serial.open", side_effect=lambda *_: open(lock, "a"), create=True):
                with port_lock():
                    with self.assertRaises(RuntimeError):
                        with port_lock():
                            self.fail("Acquired an occupied port")

    def analyze_samples(self, samples, target=1000, start=0, end=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((samples*32767).astype("<i2").tobytes())
            return analyze_wav(path, target, start, end)

    def test_harmonics_are_distortion_not_noise(self):
        t = np.arange(16000) / 16000
        x = (.3*np.sin(2*np.pi*1000*t) + .2*np.sin(2*np.pi*2000*t)
             + .2*np.sin(2*np.pi*3000*t))
        metrics = self.analyze_samples(x)
        self.assertGreater(metrics["band_snr_db"], 20)
        self.assertAlmostEqual(metrics["peak_hz"], 1000)

    def test_broadband_noise_fails(self):
        t = np.arange(16000) / 16000
        x = .01*np.sin(2*np.pi*1000*t) + np.random.default_rng(42).normal(0, .1, len(t))
        self.assertLess(self.analyze_samples(x)["band_snr_db"], 15)

    def test_band_rise_is_normalized_across_window_lengths(self):
        t = np.arange(48000) / 16000
        # Same recording, unequal windows, tenfold amplitude -> 20 dB power rise.
        x = np.where(t < 1, .01, .1)*np.sin(2*np.pi*1000*t)
        baseline = self.analyze_samples(x, start=.2, end=.8)
        tone = self.analyze_samples(x, start=1.2, end=2.7)
        self.assertAlmostEqual(band_rise_db(tone, baseline), 20, delta=.05)
        self.assertAlmostEqual(tone["band_power_dbfs"], 10*np.log10(.1**2/2), delta=.05)

    def test_ack_windows_and_short_baseline(self):
        self.assertEqual(tone_windows(10, 11.2, 11.25, 2000),
                         ((.2, 1.0999999999999992), (1.55, 3.05)))
        with self.assertRaises(ValueError):
            tone_windows(10, 10.5, 10.55, 2000)
        with self.assertRaises(ValueError):
            tone_windows(10, 11.2, 11.25, 900)

    def test_band_snr_rejects_interfering_tone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            t = np.arange(16000) / 16000
            for interferer, expected in ((0, True), (.2, False)):
                x = .2*np.sin(2*np.pi*1500*t) + interferer*np.sin(2*np.pi*2000*t)
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes((x*32767).astype("<i2").tobytes())
                self.assertEqual(analyze_wav(path, 1500)["band_snr_db"] >= 15, expected)

if __name__ == "__main__":
    unittest.main()
