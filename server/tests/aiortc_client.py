"""An independent aiortc peer validating RTVI and actual decoded waveforms."""
import asyncio
from fractions import Fraction
import json
from pathlib import Path
import time
import uuid
import wave

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCConfiguration, RTCSessionDescription, RTCRtpSender
from av import AudioFrame, AudioResampler
import httpx
import numpy as np
from scipy.signal import correlate

SERVER = Path(__file__).resolve().parents[1]
FIXTURES = SERVER / "fixtures"
RATE = 16000

def read_wav(path):
    with wave.open(str(path)) as source:
        assert source.getframerate() == RATE and source.getnchannels() == 1 and source.getsampwidth() == 2
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").copy()

def write_wav(path, pcm):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(np.asarray(pcm, dtype="<i2").tobytes())

from audio_analysis import correlation

def client_ready():
    # Exact Message::client_ready / to_json structure from pipecat-client-cxx
    # v1.1.0 ca56dc45, src/rtvi/messages.cpp:171–175,206–235.
    # AboutClientData is caller supplied; optional platform fields are omitted.
    return {"id": str(uuid.uuid4()), "label": "rtvi-ai", "type": "client-ready",
            "data": {"version": "2.1.0",
                     "about": {"library": "pipecat-client-cxx", "library_version": "1.1.0"}}}

class WavTrack(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        self.pts = 0
        self.started = None
        self.pending = np.empty(0, dtype="<i2")
        self.prompt_start = None
        self.prompt_end = None

    def play(self, pcm):
        assert not len(self.pending)
        self.pending = pcm.copy()
        self.prompt_start = time.monotonic()
        self.prompt_end = self.prompt_start + len(pcm) / RATE

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started + self.pts/RATE - time.monotonic()))
        block = np.zeros(320, dtype="<i2")
        count = min(320, len(self.pending))
        if count:
            block[:count] = self.pending[:count]
            self.pending = self.pending[count:]
        frame = AudioFrame.from_ndarray(block.reshape(1,-1), format="s16", layout="mono")
        frame.sample_rate, frame.time_base, frame.pts = RATE, Fraction(1,RATE), self.pts
        self.pts += 320
        return frame

class Peer:
    def __init__(self, url, out):
        self.url, self.out = url, Path(out)
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.track = WavTrack()
        sender = self.pc.addTrack(self.track)
        transceiver = next(t for t in self.pc.getTransceivers() if t.sender is sender)
        transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities("audio").codecs
                                        if c.mimeType.lower() == "audio/opus"])
        self.channel = self.pc.createDataChannel("chat")
        self.messages = []
        self.audio = []
        self.receiver = None
        self.opened = asyncio.Event()

        @self.channel.on("open")
        def opened():
            self.opened.set()

        @self.channel.on("message")
        def message(raw):
            self.messages.append((time.monotonic(), json.loads(raw)))

        @self.pc.on("track")
        def track(remote):
            if remote.kind == "audio":
                self.receiver = asyncio.create_task(self.receive(remote))

    async def receive(self, remote):
        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while True:
                frame = await remote.recv()
                for output in resampler.resample(frame):
                    self.audio.append(output.to_ndarray().reshape(-1).copy())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from aiortc.mediastreams import MediaStreamError
            if not isinstance(exc, MediaStreamError):
                raise

    def pcm(self):
        return np.concatenate(self.audio) if self.audio else np.empty(0, dtype="<i2")

    def send(self, kind, data):
        ident = str(uuid.uuid4())
        self.channel.send(json.dumps(dict(id=ident,label="rtvi-ai",type=kind,data=data)))
        return ident

    async def wait_message(self, predicate, since=0, timeout=20):
        async with asyncio.timeout(timeout):
            while True:
                for stamp, message in self.messages[since:]:
                    if predicate(message):
                        return stamp, message
                if self.receiver and self.receiver.done():
                    self.receiver.result()
                await asyncio.sleep(.02)

    async def connect(self):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self.url+"/start", json={"transport":"webrtc"})
            response.raise_for_status()
            start = response.json()
            self.session_id = start["sessionId"]
            route = f"/sessions/{self.session_id}/api/offer"
            await self.pc.setLocalDescription(await self.pc.createOffer())
            offer = self.pc.localDescription
            (self.out/"offer.sdp").write_text(offer.sdp)
            response = await client.post(self.url+route,json={"sdp":offer.sdp,"type":offer.type})
            response.raise_for_status()
            answer = response.json()
            self.pc_id = answer["pc_id"]
            (self.out/"answer.sdp").write_text(answer["sdp"])
            (self.out/"routes.json").write_text(json.dumps({"start":start,"offer_route":route,"pc_id":self.pc_id},indent=2))
            assert "a=candidate:" in answer["sdp"]
            assert "opus/48000/2" in answer["sdp"]
            await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"],type=answer["type"]))
        await asyncio.wait_for(self.opened.wait(), 15)
        ready = client_ready()
        (self.out/"client-ready.json").write_text(json.dumps(ready,indent=2))
        self.channel.send(json.dumps(ready))
        _, message = await self.wait_message(lambda m:m.get("type")=="bot-ready")
        assert message["id"] == ready["id"]

    async def assert_audio(self, reference, start=0, timeout=15):
        deadline = time.monotonic()+timeout
        best = 0.
        while time.monotonic()<deadline:
            best = correlation(self.pcm()[start:],reference)
            if best >= .80:
                return best
            if self.receiver and self.receiver.done():
                self.receiver.result()
            await asyncio.sleep(.15)
        raise AssertionError(f"Waveform correlation {best:.3f} < 0.80")

    async def close(self):
        await self.pc.close()
        if self.receiver:
            self.receiver.cancel()
            await asyncio.gather(self.receiver,return_exceptions=True)
        write_wav(self.out/"received.wav",self.pcm())
        (self.out/"messages.json").write_text(json.dumps(self.messages,indent=2))
