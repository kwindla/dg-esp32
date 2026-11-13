"""Send bot audio as mono wideband Opus instead of aiortc's fixed 48 kHz stereo 96 kbps.

aiortc 1.15 hardcodes its Opus encoder and ignores SDP fmtp hints, and Pipecat's
SmallWebRTC transport exposes no codec settings. The device decodes 16 kHz mono and
plays through a small speaker, so we keep the 48 kHz RTP clock (RFC 7587) but encode
mono with an 8 kHz cutoff. libopus then picks SILK wideband, which costs the ESP32
far less to decode. Call install() before any peer connection is created.
"""
import fractions
import os

from av import AudioResampler, CodecContext
from aiortc import rtcrtpsender
from aiortc.codecs import opus

BITRATE = int(os.getenv("BOT_OPUS_BITRATE", "30000"))
CUTOFF_HZ = int(os.getenv("BOT_OPUS_CUTOFF_HZ", "8000"))


class LeanOpusEncoder(opus.OpusEncoder):
    def __init__(self) -> None:
        self.codec = CodecContext.create("libopus", "w")
        self.codec.bit_rate = BITRATE
        self.codec.format = "s16"
        self.codec.layout = "mono"
        self.codec.options = {"application": "voip", "cutoff": str(CUTOFF_HZ)}
        self.codec.sample_rate = opus.SAMPLE_RATE
        self.codec.time_base = fractions.Fraction(1, opus.SAMPLE_RATE)
        self.resampler = AudioResampler(format="s16", layout="mono", rate=opus.SAMPLE_RATE,
                                        frame_size=opus.SAMPLES_PER_FRAME)
        self.first_packet_pts = None


_original = rtcrtpsender.get_encoder


def _get_encoder(codec):
    if codec.mimeType.lower() == "audio/opus":
        return LeanOpusEncoder()
    return _original(codec)


def install() -> None:
    rtcrtpsender.get_encoder = _get_encoder
