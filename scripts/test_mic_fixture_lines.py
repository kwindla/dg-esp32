#!/usr/bin/env python3
"""The generated commands, checked against the device loader's rules (main/fixture_load.h)."""
import base64
import re
import struct
import tempfile
import unittest
import wave
import zlib
from pathlib import Path

import mic_fixture_lines as fixture

PREFIX = len("#4294967295 ")  # The longest sequence prefix the runner can add.
LINE_END = len("\r\n")


def device_load(commands):
    """The device's acceptance rules: ordered chunks, each with its own count and CRC, then the total."""
    begin, *data, end = commands
    name, samples = begin.split()
    assert name == "mic-load-begin" and 1 <= int(samples) <= 480000
    received = b""
    for command in data:
        name, offset, count, crc, text = command.split()
        assert name == "mic-load-data" and int(offset) * 2 == len(received)
        chunk = base64.b64decode(text, validate=True)
        assert chunk and len(chunk) == int(count) * 2 and int(crc, 16) == zlib.crc32(chunk)
        received += chunk
    name, count, crc = end.split()
    assert name == "mic-load-end" and int(count) == int(samples) == len(received) // 2
    assert int(crc, 16) == zlib.crc32(received)
    return received


class MicFixtureLines(unittest.TestCase):
    def test_commands_reload_the_same_pcm_and_every_line_fits_the_receive_ring(self):
        for samples in (1, 2, 359, 360, 361, 16000, 480000):
            pcm = struct.pack(f"<{samples}h", *((i * 31 - 12000) % 65536 - 32768 for i in range(samples)))
            commands = list(fixture.lines(pcm))
            self.assertEqual(device_load(commands), pcm)
            self.assertEqual(len(commands), 2 + -(-samples // fixture.CHUNK_SAMPLES))
            self.assertLessEqual(PREFIX + max(map(len, commands)) + LINE_END, fixture.RX_RING)
            self.assertTrue(all("\n" not in c and c.isascii() for c in commands))

    def test_the_longest_line_and_the_ring_are_what_the_script_says(self):
        self.assertEqual(PREFIX + max(len(line) for line in fixture.lines(bytes(480000 * 2))) + LINE_END, 1008)
        # The ring the S3 console is given. The UART console of the P4 has a larger one.
        source = (Path(__file__).resolve().parents[1] / "components/dg_diagnostics/usb_serial_jtag.cpp").read_text()
        self.assertEqual([int(size) for size in re.findall(r"rx_buffer_size=(\d+)", source)], [fixture.RX_RING])

    def test_the_lines_are_the_ones_the_device_test_accepts(self):
        # main/test_host/test_fixture_load.cpp feeds exactly this text to the device's loader.
        self.assertEqual(list(fixture.lines(struct.pack("<4h", 1, -2, 3, -4))),
                         ["mic-load-begin 4", "mic-load-data 0 4 cbca8ec0 AQD+/wMA/P8=", "mic-load-end 4 cbca8ec0"])

    def test_refuses_empty_odd_and_oversize_pcm(self):
        for bad in (b"", b"\0", b"\0" * (480001 * 2)):
            with self.assertRaises(ValueError):
                list(fixture.lines(bad))

    def test_reads_only_16k_mono_pcm16_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            def write(name, rate, channels, width, frames):
                path = Path(directory) / name
                with wave.open(str(path), "wb") as out:
                    out.setframerate(rate); out.setnchannels(channels); out.setsampwidth(width)
                    out.writeframes(frames)
                return path
            pcm = struct.pack("<4h", 1, -2, 3, -4)
            self.assertEqual(fixture.read_wav(write("ok.wav", 16000, 1, 2, pcm)), pcm)
            for name, rate, channels, width in (("rate.wav", 8000, 1, 2), ("stereo.wav", 16000, 2, 2), ("wide.wav", 16000, 1, 1)):
                with self.assertRaises(ValueError):
                    fixture.read_wav(write(name, rate, channels, width, pcm))


if __name__ == "__main__":
    unittest.main()
