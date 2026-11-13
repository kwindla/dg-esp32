#!/usr/bin/env python3
"""Console commands that load a WAV as the device's microphone fixture.

    python3 scripts/mic_fixture_lines.py phrase.wav > phrase.cmds

Prints one console command per line, without the "#<seq> " prefix: the bench
runner numbers and paces commands itself. No serial port is opened here.

The image needs CONFIG_DG_SPEAKER_DIAGNOSTICS. Send `audio 0`, then these
lines in order (each after the previous OK), then `mic-fixture 2` and
`audio 1`. `mic-fixture 0` turns the fixture off again; `mic-fixture 1` is
still the built-in prompt. The fixture loops, so pad the WAV with the silence
you want between repetitions. At most 30 s, 16 kHz mono 16-bit PCM.

Every data line names its own sample count and CRC-32. The device refuses a
line that does not match them, answers `MIC_LOAD_ERROR <reason>` before the
`ERR`, and keeps nothing of it, so the same line can be sent again.
"""
import base64
import sys
import wave
import zlib

# The S3 console receives through a 1024-byte ring
# (components/dg_diagnostics/usb_serial_jtag.cpp), and the IDF driver drops a
# 64-byte packet that does not fit, without an error. The device reads the
# ring while a line arrives, so longer lines often survive, but only a line
# that fits whole is safe whatever the device is doing: the ring is empty when
# a line starts, because the previous command was read to its end before it
# was answered. 360 samples are 960 base64 characters, and the longest line,
# with a ten-digit sequence number and CR LF, is 1008 bytes.
RX_RING = 1024
CHUNK_SAMPLES = 360
MAX_SAMPLES = 16000 * 30


def lines(pcm):
    """The commands for little-endian PCM16 bytes: begin, ordered chunks, end with the zlib CRC-32."""
    if not pcm or len(pcm) % 2 or len(pcm) // 2 > MAX_SAMPLES:
        raise ValueError("fixture must be 1..480000 16-bit samples")
    samples = len(pcm) // 2
    yield f"mic-load-begin {samples}"
    for at in range(0, samples, CHUNK_SAMPLES):
        chunk = pcm[at * 2:(at + CHUNK_SAMPLES) * 2]
        yield f"mic-load-data {at} {len(chunk) // 2} {zlib.crc32(chunk):08x} {base64.b64encode(chunk).decode()}"
    yield f"mic-load-end {samples} {zlib.crc32(pcm):08x}"


def read_wav(path):
    with wave.open(str(path), "rb") as source:
        layout = (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype())
        if layout != (16000, 1, 2, "NONE"):
            raise ValueError("fixture must be 16 kHz mono 16-bit PCM")
        return source.readframes(source.getnframes())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    for line in lines(read_wav(sys.argv[1])):
        print(line)
