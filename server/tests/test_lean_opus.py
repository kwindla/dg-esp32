import fractions
from collections import Counter

import numpy as np
from av import AudioFrame

import lean_opus


def _encode(pcm):
    encoder, packets = lean_opus.LeanOpusEncoder(), []
    for pts in range(0, len(pcm) - 319, 320):
        frame = AudioFrame.from_ndarray(pcm[pts:pts + 320].reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 16000, pts, fractions.Fraction(1, 16000)
        packets += encoder.encode(frame)[0]
    return packets


def _toc(packet):
    config = packet[0] >> 3
    return config, bool(packet[0] & 0x04)


def test_speech_is_mono_silk_wideband_near_target_rate():
    t = np.arange(32000) / 16000
    pcm = (6000 * np.sin(2 * np.pi * 220 * t) * (1 + np.sin(2 * np.pi * 3 * t))).astype(np.int16)
    packets = _encode(pcm)
    assert {_toc(p) for p in packets} <= {(8, False), (9, False), (10, False), (11, False)}  # SILK WB, mono
    kbps = sum(map(len, packets)) * 8 / (len(packets) * 0.02) / 1000
    assert kbps < 36


def test_silence_packets_stay_small():
    sizes = Counter(len(p) for p in _encode(np.zeros(16000, dtype=np.int16)))
    assert max(sizes) <= 12


def test_install_replaces_only_opus():
    from aiortc import rtcrtpsender
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters
    saved = rtcrtpsender.get_encoder
    try:
        lean_opus.install()
        opus = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, channels=2)
        pcmu = RTCRtpCodecParameters(mimeType="audio/PCMU", clockRate=8000)
        assert isinstance(rtcrtpsender.get_encoder(opus), lean_opus.LeanOpusEncoder)
        assert not isinstance(rtcrtpsender.get_encoder(pcmu), lean_opus.LeanOpusEncoder)
    finally:
        rtcrtpsender.get_encoder = saved
