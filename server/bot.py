"""Pipecat 1.15.0 LAN bot, with offline and credential-backed acceptance modes."""
import asyncio
from collections import deque
import json
import math
import os
from pathlib import Path
import time
import wave
import rtp_evidence
import ice_path
ice_path.install()
from loguru import logger

# The runner imports this module before constructing any peer connections.
# Step 6c: lean halves device decode cost; BOT_OPUS=aiortc restores the stock encoder.
OPUS_MODE = os.getenv("BOT_OPUS", "lean")
if OPUS_MODE not in {"lean", "aiortc"}:
    raise ValueError("BOT_OPUS must be lean or aiortc")
if OPUS_MODE == "lean":
    import lean_opus
    lean_opus.install()
logger.info("BOT_OPUS={}", OPUS_MODE)

import numpy as np
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    InputAudioRawFrame, InputTransportMessageFrame, OutputAudioRawFrame,
    OutputTransportMessageFrame, OutputTransportMessageUrgentFrame, TranscriptionFrame,
    TTSAudioRawFrame, TTSSpeakFrame, TTSStartedFrame, TTSStoppedFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    MetricsFrame, InterruptionFrame, CancelFrame, LLMFullResponseStartFrame,
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, UserMuteStartedFrame, UserMuteStoppedFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.processors.frameworks.rtvi.observer import RTVIObserverParams
from pipecat.runner.types import RunnerArguments
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_start import VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from settings import SERVER, FIXTURES, GREETING, REPLY, VOICE, load_credentials, server_port
from settings import vad_min_volume, voice_policy, smartturn_stop_secs, fixture_paths as allowed_fixture_paths, skip_greeting, offline_rtvi_burst_enabled
from voice_audit import VoiceSTTAudit, VoiceResponseAudit, half_duplex_strategies, configure_voice_turns
from offline_fixture_reply import OfflineFixtureReply, enabled as usb_fixture_enabled

class SessionLog:
    def __init__(self, pc_id, mode):
        self.pc_id, self.mode = pc_id, mode
        self.path = Path(os.getenv("EVENTS_PATH", str(SERVER / "events.jsonl")))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event, **data):
        record = dict(t=time.monotonic(), pc_id=self.pc_id, mode=self.mode, event=event, **data)
        with self.path.open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

def speech_vad_params(mode):
    old = mode == "voice" and voice_policy() == "8d"
    return VADParams(start_secs=.1 if old else .2, stop_secs=.6 if old else .2,
                     min_volume=vad_min_volume(mode))

def speech_stt_settings(mode):
    from pipecat.services.deepgram.stt import DeepgramSTTService
    if mode != "voice":
        return DeepgramSTTService.Settings(endpointing=False)
    if voice_policy() in {"8d", "timeout"}:
        return DeepgramSTTService.Settings(endpointing=500)
    return DeepgramSTTService.Settings()

class AuditObserver(BaseObserver):
    """Deduplicate frame propagation, logging every wire RTVI message and VAD edge."""
    def __init__(self, log, transport):
        super().__init__()
        self.log = log
        self.input, self.output = transport.input(), transport.output()
        self.seen = set()
        self.order = deque()
        self.speaking = False
        self.turn = 0
        self.tts_first = False
        self.response_id = None
        self.tts_index = 0
        self.tts_response_id = None
        self.output_pending = False
        self.bot_speaking = False

    async def on_push_frame(self, data):
        frame = data.frame
        if not isinstance(frame, (InputTransportMessageFrame, OutputTransportMessageFrame,
                                  OutputTransportMessageUrgentFrame, VADUserStartedSpeakingFrame,
                                  VADUserStoppedSpeakingFrame, TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame,
                                  MetricsFrame, InterruptionFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
                                  UserMuteStartedFrame, UserMuteStoppedFrame)):
            return
        if isinstance(frame, InputTransportMessageFrame) and (
                data.source is not self.input or data.direction != FrameDirection.DOWNSTREAM):
            return
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)) and (
                data.destination is not self.output or data.direction != FrameDirection.DOWNSTREAM):
            return
        if frame.id in self.seen:
            return
        self.seen.add(frame.id)
        self.order.append(frame.id)
        if len(self.order) > 4096:
            self.seen.remove(self.order.popleft())
        if isinstance(frame, MetricsFrame):
            for metric in frame.data:
                if hasattr(metric, 'is_complete'):
                    value = metric.model_dump(mode='json')
                    self.log.write('smartturn', turn=self.turn, **value)
            return
        if isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            self.output_pending = isinstance(frame, BotStartedSpeakingFrame)
            self.bot_speaking = self.output_pending
            self.log.write('bot-audio-start' if self.output_pending else 'bot-audio-end',
                           turn=self.turn, response_id=self.response_id)
        elif isinstance(frame, (UserMuteStartedFrame, UserMuteStoppedFrame)):
            self.log.write('user-mute-start' if isinstance(frame, UserMuteStartedFrame) else 'user-mute-stop',
                           turn=self.turn, response_id=self.response_id, bot_speaking=self.bot_speaking)
        elif isinstance(frame, InterruptionFrame):
            if hasattr(self, 'response_audit'):
                self.response_audit.cancel('InterruptionFrame')
            self.log.write('interruption', turn=self.turn, response_id=self.response_id,
                           during_bot_speech=self.bot_speaking)
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            speaking = isinstance(frame, VADUserStartedSpeakingFrame)
            if speaking == self.speaking:
                return
            self.speaking = speaking
            if speaking: self.turn += 1
            self.log.write("vad-start" if self.speaking else "vad-stop", turn=self.turn)
        elif isinstance(frame, TTSStartedFrame):
            self.tts_first = True
            self.tts_index += 1
            self.tts_response_id = self.response_id
            self.log.write("tts-start", turn=self.turn, index=self.tts_index, response_id=self.tts_response_id)
        elif isinstance(frame, TTSAudioRawFrame):
            if self.tts_first:
                self.tts_first = False
                self.log.write("first-tts-byte", turn=self.turn, bytes=len(frame.audio),
                               index=self.tts_index, response_id=self.tts_response_id)
        elif isinstance(frame, TTSStoppedFrame):
            self.log.write("tts-stop", turn=self.turn, index=self.tts_index, response_id=self.tts_response_id)
        else:
            self.log.write("rtvi-in" if isinstance(frame, InputTransportMessageFrame)
                           else "rtvi-out", message=frame.message)

class InputRecording:
    """Opt-in PCM evidence, closed on every session exit (including cancellation)."""
    def __init__(self, log):
        self.stream = None
        self.samples = 0
        self.log = log
        self.started = False
        if os.getenv("RECORD_INPUT") == "1":
            directory = Path(os.getenv("RECORD_INPUT_DIR", str(SERVER / "out")))
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / f"{log.pc_id}.wav"
            self.stream = wave.open(str(self.path), "wb")
            self.stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))

    def write(self, frame):
        if self.stream is not None:
            if not self.started:
                self.started = True
                self.log.write('input-recording-start', path=str(self.path))
            if frame.sample_rate != 16000 or frame.num_channels != 1 or len(frame.audio) % 2:
                raise ValueError("Input recording requires 16 kHz mono PCM16")
            self.stream.writeframes(frame.audio)
            self.samples += len(frame.audio) // 2

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
            self.log.write("input-recording", path=str(self.path), samples=self.samples)

class AudioTelemetry(FrameProcessor):
    def __init__(self, rtvi, audit, log, recording, connection):
        super().__init__()
        self.rtvi, self.audit, self.log = rtvi, audit, log
        self.recording = recording
        self.connection = connection
        self.energy = 0.
        self.samples = 0
        self.peak = 0.
        self.clips = 0
        self.last = time.monotonic()
        self.last_frame_at = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            now = time.monotonic()
            if self.recording.stream is not None and (self.last_frame_at is None or now-self.last_frame_at > .06):
                self.log.write('input-clock', sample_offset=self.recording.samples,
                               gap_s=now-self.last_frame_at if self.last_frame_at is not None else None)
            self.last_frame_at = now
            self.recording.write(frame)
            pcm = np.frombuffer(frame.audio, dtype="<i2").astype(float) / 32768
            self.energy += float(np.dot(pcm, pcm))
            self.samples += len(pcm)
            self.peak = max(self.peak, float(np.max(np.abs(pcm))))
            self.clips += int(np.count_nonzero(np.abs(pcm) >= 32767/32768))
            if time.monotonic() - self.last >= .5:
                data = dict(rms_dbfs=10*math.log10(max(self.energy / max(1, self.samples), 1e-12)),
                            speaking=self.audit.speaking)
                self.log.write('input-level', turn=self.audit.turn, **data,
                               sample_endpoint=self.recording.samples, window_samples=self.samples)
                self.log.write('input-peaks', turn=self.audit.turn, peak=self.peak, clipped_samples=self.clips)
                await self.rtvi.send_server_message({"t": "user-audio", "d": data})
                report = rtp_evidence.summary(self.connection)
                if report is not None:
                    await self.rtvi.send_server_message({"t": "rtp-stats", "d": report})
                self.energy, self.samples, self.last = 0., 0, time.monotonic()
                self.peak, self.clips = 0., 0
        await self.push_frame(frame, direction)

class FixedReply(FrameProcessor):
    """Reply once after turn-stop, coalescing late STT finals for that turn.

    SmartTurn can complete before Deepgram delivers the trailing final. A
    one-second quiet window after stop events prevents that final from being
    cut off by the half-duplex reply. VAD and SmartTurn settings stay unchanged.
    """
    def __init__(self, log, audit, settle_secs=1.0):
        super().__init__()
        self.log, self.audit = log, audit
        self.settle_secs = settle_secs
        self.last_turn = None
        self.pending = None
        self.content = ""
        self.reply_text = ("Pineapple. The capital of France is Paris. The animated cat is staying on the screen while I finish this longer spoken reply. Pineapple marks the beginning and the end of this display and voice check."
                           if os.getenv('ECHOEAR_STRICT_MERGE') == '1' else REPLY)
        self.task = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM and frame.text.strip():
            self.log.write("final-transcript", text=frame.text, turn=self.audit.turn)
        await self.push_frame(frame, direction)

    async def user_turn_stopped(self, turn, content, emit, publish=None):
        if turn == self.last_turn:
            return
        if not content or not content.strip():
            if self.pending is None:
                return
            # Noise can consume a VAD turn ID without producing text. Resume
            # the original reply with its original transcript and turn ID.
            turn, content = self.pending, self.content
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.pending != turn:
            self.content = content.strip()
        elif content.strip() != self.content:
            # Aggregator callbacks may contain an updated full message or just
            # the final fragment following its previous context flush.
            self.content = content.strip() if content.strip().startswith(self.content) else self.content + " " + content.strip()
        self.pending = turn

        settled_turn = self.audit.turn
        async def finish():
            await asyncio.sleep(self.settle_secs)
            if self.audit.turn != settled_turn or getattr(self.audit, "speaking", False):
                # Retain pending/content; the next turn-stop re-arms settlement.
                return
            self.pending = None
            self.last_turn = turn
            self.log.write("fixed-reply", turn=turn, text=self.content)
            if publish:
                await publish(turn, self.content)
            await emit([TTSSpeakFrame(self.reply_text)])
        self.task = asyncio.create_task(finish(), name="fixed-reply-turn-settle")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.pending = None
        self.content = ""

class TTSReference(FrameProcessor):
    """Optional evidence of service PCM before Opus, for live cross-correlation."""
    def __init__(self, log):
        super().__init__()
        self.log = log
        self.index = 0
        self.pcm = bytearray()
        self.rate = 16000
        self.response_id = None
        self.active = False

    def flush(self, cancelled=False):
        if self.pcm:
            directory = Path(os.environ["TTS_REFERENCE_DIR"]) / self.log.pc_id
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"tts-{self.index}.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, self.rate, 0, "NONE", "not compressed"))
                stream.writeframes(self.pcm)
            self.log.write("tts-reference", path=str(path), index=self.index,
                           response_id=self.response_id, cancelled=cancelled,
                           duration_s=len(self.pcm)/(2*self.rate))
        self.pcm.clear()
        self.active = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and os.getenv("TTS_REFERENCE_DIR"):
            if isinstance(frame, LLMFullResponseStartFrame):
                self.response_id = (self.response_id or 0) + 1
            elif isinstance(frame, TTSStartedFrame):
                self.index += 1
                self.pcm.clear()
                self.active = True
            elif isinstance(frame, TTSAudioRawFrame):
                self.rate = frame.sample_rate
                self.pcm.extend(frame.audio)
            elif isinstance(frame, TTSStoppedFrame):
                self.flush()
            elif isinstance(frame, (InterruptionFrame, CancelFrame)) and self.active:
                self.flush(cancelled=True)
        await self.push_frame(frame, direction)

async def bot(runner_args: RunnerArguments):
    mode = os.getenv("BOT_MODE", "offline-test")
    usb_fixture = usb_fixture_enabled(mode, os.getenv("OFFLINE_USB_ABSENCE_TURNS", "0"))
    burst_enabled = offline_rtvi_burst_enabled(mode)
    if mode not in {"offline-test", "live-test", "voice"}:
        raise ValueError("BOT_MODE must be offline-test, live-test or voice")
    if mode != "offline-test":
        load_credentials(approved_only=True)
        if mode == "voice" and not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for voice mode")
    connection = runner_args.webrtc_connection
    rtp_evidence.bind(connection)
    log = SessionLog(connection.pc_id, mode)
    auto_silence=os.getenv("AUDIO_OUT_AUTO_SILENCE","1")
    if auto_silence not in {"0","1"}:raise ValueError("AUDIO_OUT_AUTO_SILENCE must be 0 or 1")
    idle_tail_ms=0
    if auto_silence=="0":
        from silence_track import install,PacedIdleAudioTrack
        install()
        idle_tail_ms=PacedIdleAudioTrack.idle_tail_ms
    transport = SmallWebRTCTransport(webrtc_connection=connection, params=TransportParams(
        audio_in_enabled=True, audio_out_enabled=True, audio_in_sample_rate=16000,
        audio_out_sample_rate=16000, audio_out_auto_silence=auto_silence=="1",
        audio_out_10ms_chunks=int(os.getenv("AUDIO_OUT_10MS_CHUNKS", "4"))))
    continuous_bench=os.getenv('OFFLINE_BENCH_CONTINUOUS_AUDIO','0')
    if continuous_bench not in {'0','1'} or (continuous_bench=='1' and mode!='offline-test'):
        raise ValueError('Continuous audio bench requires offline-test and a 0/1 setting')
    log.write("audio-output-config",auto_silence=auto_silence=="1",continuous_bench=continuous_bench=='1',
        idle_tail_ms=idle_tail_ms)
    rtvi = RTVIProcessor()
    from display_client import DisplayClient, hil_commands
    display = DisplayClient(rtvi, log, retry_budget=int(os.getenv('DISPLAY_RETRY_BUDGET', '2')))
    display_commands = os.getenv('DISPLAY_HIL_COMMANDS')
    if display_commands and mode not in ('offline-test', 'live-test'):
        raise ValueError('DISPLAY_HIL_COMMANDS requires an explicit test bot mode')
    display_task = asyncio.create_task(hil_commands(display, display_commands)) if display_commands else None
    audit = AuditObserver(log, transport)
    params = speech_vad_params(mode)
    log.write("vad-settings", **params.model_dump())
    vad_params = dict(vad_analyzer=SileroVADAnalyzer(params=params))
    if mode == "offline-test":
        strategies=UserTurnStrategies(stop=[SpeechTimeoutUserTurnStopStrategy(wait_for_transcript=False)])
        if continuous_bench=='1' or usb_fixture:
            strategies.start=[VADUserTurnStartStrategy(enable_interruptions=False),
                TranscriptionUserTurnStartStrategy(enable_interruptions=False)]
        vad_params["user_turn_strategies"] = strategies
    else:
        policy = voice_policy() if mode == "voice" else "live-test"
        strategies = configure_voice_turns(vad_params, policy, log) if mode == "voice" else half_duplex_strategies()
        vad_params["user_turn_strategies"] = strategies
        log.write("interruption-settings", enabled=mode == "voice" and policy != "8d", policy=policy)
        if mode == 'voice':
            log.write('user-mute-settings', strategies=[type(s).__name__ for s in vad_params['user_mute_strategies']],
                      diagnostic_disable=os.getenv('STEP8E_DISABLE_BOT_MUTE','0')=='1')
        log.write("turn-settings", policy=policy, stop_strategy=type(strategies.stop[0]).__name__,
                  smartturn_stop_secs=smartturn_stop_secs() if mode == 'voice' and policy == 'smartturn' else None)
    user, assistant = LLMContextAggregatorPair(LLMContext(), user_params=LLMUserAggregatorParams(**vad_params))
    recording = InputRecording(log)
    processors = [transport.input(), rtvi, AudioTelemetry(rtvi, audit, log, recording, connection)]
    if mode != "offline-test":
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.services.deepgram.tts import DeepgramTTSService
        from pipecat.services.settings import is_given
        settings = speech_stt_settings(mode)
        log.write("stt-settings", endpointing=settings.endpointing if is_given(settings.endpointing) else "Deepgram default (10 ms)",
                  finalize="pipecat-vad-stop")
        stt_options = {"ttfs_p99_latency": 1.2} if mode == "voice" and voice_policy() == "8d" else {}
        processors.append(DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"], sample_rate=16000,
                          **stt_options, settings=settings))
    fixed_reply = FixedReply(log, audit) if mode == "live-test" else None
    usb_reply = OfflineFixtureReply(FIXTURES/"reply_pineapple.wav", log) if usb_fixture else None
    if fixed_reply:
        processors.append(fixed_reply)
    if mode == "voice":
        processors.append(VoiceSTTAudit(log, audit))
    processors.append(user)
    if mode == "voice":
        from pipecat.services.openai.llm import OpenAILLMService
        processors.append(OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAILLMService.Settings(
                system_instruction="You are a helpful voice assistant. Answer in one concise sentence.")))
        processors.append(VoiceResponseAudit(log, audit))
    if mode != "offline-test":
        processors.extend([DeepgramTTSService(api_key=os.environ["DEEPGRAM_API_KEY"], sample_rate=16000,
                            settings=DeepgramTTSService.Settings(voice=VOICE)), TTSReference(log)])
    processors.extend([transport.output(), assistant])
    worker_options = {}
    if mode == "offline-test" and os.getenv("OFFLINE_IDLE_TIMEOUT_SECS"):
        worker_options["idle_timeout_secs"] = float(os.environ["OFFLINE_IDLE_TIMEOUT_SECS"])
    worker = PipelineWorker(Pipeline(processors),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000,
                              enable_metrics=True), enable_rtvi=False,
        # The firmware reads user-transcription, not its 1.13 successor user-input.
        observers=[RTVIObserver(rtvi, params=RTVIObserverParams(user_input_enabled=False)), audit], **worker_options)
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    fixture_task = None
    burst_task = None
    fixture_paths = allowed_fixture_paths(mode)
    # Explicit offline bench bank: captured once from live TTS, immutable across
    # loudness targets. No client-selected paths or live-mode fixture override.

    async def play_fixture(name, duration_ms=0, loops=1):
        nonlocal fixture_task
        if name not in fixture_paths:
            raise ValueError("Unknown fixture")
        with wave.open(str(fixture_paths[name])) as stream:
            assert stream.getframerate() == 16000 and stream.getnchannels() == 1 and stream.getsampwidth() == 2
            pcm = stream.readframes(stream.getnframes())
        if duration_ms:
            if mode != "offline-test" or not isinstance(duration_ms, int) or not 1 <= duration_ms <= 60000:
                raise ValueError("Long playback requires offline-test and 1..60000 ms")
            size = duration_ms * 32
            pcm = (pcm * ((size + len(pcm)-1)//len(pcm)))[:size]
        if loops != 1:
            if mode != "offline-test" or duration_ms != 60000 or not isinstance(loops, int) or not 1 <= loops <= 30:
                raise ValueError("Fixture loops require offline-test and a 60 s fixture")
        log.write("fixture", name=name, frames=len(pcm)//2 * loops, turn=audit.turn)
        log.write("first-fixture-byte", name=name, turn=audit.turn)
        if loops == 1:
            await worker.queue_frames([OutputAudioRawFrame(pcm[i:i+640], 16000, 1)
                                       for i in range(0, len(pcm), 640)])
        else:
            # Keep only a few seconds ahead of the RTC sender. Flooding the
            # pipeline with 90,000 frames creates avoidable startup contention.
            if fixture_task and not fixture_task.done():
                raise ValueError("Long fixture already active")
            started = asyncio.Event()
            async def feed():
                clock = asyncio.get_running_loop()
                beginning = clock.time()
                submitted = 0
                for _ in range(loops):
                    for start in range(0, len(pcm), 640*100):
                        end = min(start+640*100, len(pcm))
                        frames = [OutputAudioRawFrame(pcm[i:i+640], 16000, 1)
                                  for i in range(start, end, 640)]
                        await worker.queue_frames(frames)
                        submitted += len(frames)
                        started.set()
                        await asyncio.sleep(max(0, beginning+submitted*.02-1-clock.time()))
            fixture_task = asyncio.create_task(feed(), name="offline-long-fixture")
            await started.wait()

    async def publish_turn(turn, content):
        log.write("turn-transcript", turn=turn, text=content)
        await rtvi.send_server_message({"t": "turn-transcript", "d": {"turn": turn, "text": content}})

    @user.event_handler("on_user_turn_started")
    async def user_turn_started(aggregator, strategy):
        log.write("user-turn-started", turn=audit.turn, strategy=type(strategy).__name__,
                  during_bot_speech=audit.bot_speaking)

    @user.event_handler("on_user_turn_stopped")
    async def user_turn_stopped(aggregator, strategy, message):
        content = message.content or ""
        log.write("user-turn-stopped", turn=audit.turn, text=content, strategy=type(strategy).__name__)
        if usb_reply:
            await usb_reply.stopped(audit.turn, worker.queue_frames)
        elif fixed_reply:
            await fixed_reply.user_turn_stopped(audit.turn, content, worker.queue_frames, publish_turn)
        elif content.strip():
            await publish_turn(audit.turn, content)

    @rtvi.event_handler("on_client_ready")
    async def ready(processor):
        log.write("client-ready")
        if mode == "offline-test" and os.getenv("OFFLINE_R4_CIPHER_AUDIT") == "1":
            ssl = connection._pc.sctp.transport._ssl
            log.write("r4-dtls-cipher", name=ssl.get_cipher_name(), protocol=ssl.get_protocol_version_name())
        # PipelineWorker registers the bot-ready sender for this external
        # RTVIProcessor too. Its earlier handler completes the handshake;
        # this handler schedules the greeting.
        nonlocal burst_task
        flood_seconds = float(os.getenv("OFFLINE_R4_FLOOD_SECONDS", "0"))
        if flood_seconds:
            rate_name = os.getenv("OFFLINE_R4_FLOOD_RATE", "saturating")
            if mode != "offline-test" or not 30 <= flood_seconds <= 300 or rate_name not in ("0", "25", "50", "100", "saturating"):
                raise ValueError("R4 flood requires offline-test, 30..300 seconds and a supported rate")
            flood_rate = None if rate_name == "saturating" else int(rate_name)
            async def r4_flood():
                # Keep a bounded SCTP flight while filling it at every opportunity.
                # bufferedAmount excludes already queued/sent SCTP DATA chunks.
                clock = asyncio.get_running_loop()
                await asyncio.sleep(2)
                trigger = os.getenv("OFFLINE_R4_FLOOD_TRIGGER")
                if trigger:
                    deadline = clock.time()+60
                    while not Path(trigger).exists():
                        if clock.time()>deadline:raise TimeoutError("Flood trigger not published")
                        await asyncio.sleep(.02)
                begin = clock.time(); until = begin + flood_seconds
                count = 0; maximum = 0; worst_lag = 0; next_log = begin + 10
                log.write("r4-flood-start", duration_s=flood_seconds, message_bytes=1024,
                          flight_limit_bytes=8192, rate=rate_name)
                def flight():
                    dc = connection._data_channel
                    sctp = connection._pc.sctp
                    return dc.bufferedAmount + sum(len(c.user_data) for c in sctp._outbound_queue) + sum(len(c.user_data) for c in sctp._sent_queue)
                try:
                    while clock.time() < until:
                        dc = connection._data_channel
                        if not dc or dc.readyState != "open":
                            raise RuntimeError("R4 flood data channel closed")
                        backlog = flight(); maximum = max(maximum, backlog)
                        due = flood_rate is None or (flood_rate > 0 and count < (clock.time()-begin)*flood_rate)
                        if due and backlog <= 8192 - 1024:
                            message = dict(label="rtvi-ai", type="bot-llm-text", data=dict(text="", index=count))
                            message["data"]["text"] = "x" * (1024 - len(json.dumps(message).encode()))
                            assert len(json.dumps(message).encode()) == 1024
                            connection.send_app_message(message); count += 1
                        pause = 0 if due and backlog <= 8192 - 1024 else .001
                        expected = clock.time() + pause
                        await asyncio.sleep(pause)
                        worst_lag = max(worst_lag, clock.time() - expected)
                        if clock.time() >= next_log:
                            log.write("r4-flood-progress", count=count, bytes=count*1024,
                                      elapsed_s=clock.time()-begin, flight_bytes=flight(),
                                      max_flight_bytes=maximum, max_event_loop_lag_s=worst_lag)
                            next_log += 10
                    elapsed = clock.time()-begin
                    log.write("r4-flood-sending-complete", count=count, bytes=count*1024,
                              elapsed_s=elapsed, messages_per_s=count/elapsed,
                              max_flight_bytes=maximum, max_event_loop_lag_s=worst_lag)
                    drain = clock.time() + 30
                    while flight() and clock.time() < drain:
                        await asyncio.sleep(.01)
                    log.write("r4-flood-complete", count=count, pending_bytes=flight())
                except Exception as exc:
                    log.write("r4-flood-failed", count=count, elapsed_s=clock.time()-begin, reason=str(exc))
            burst_task = asyncio.create_task(r4_flood(), name="offline-r4-flood")
        elif burst_enabled:
            async def burst_sweep():
                await asyncio.sleep(8)
                clock = asyncio.get_running_loop()
                for repeat in range(3):
                    for count in (1, 5, 20, 50):
                        log.write("rtvi-burst-start", count=count, repeat=repeat)
                        begin = clock.time()
                        for index in range(count):
                            connection.send_app_message(dict(label="rtvi-ai", type="bot-llm-text",
                                data=dict(text=" token", index=index)))
                        log.write("rtvi-burst-sent", count=count, repeat=repeat,
                                  enqueue_ms=1000*(clock.time()-begin))
                        expected = clock.time()+.02
                        await asyncio.sleep(.02)
                        log.write("rtvi-burst-event-loop", count=count, repeat=repeat,
                                  lag_ms=1000*(clock.time()-expected))
                        await asyncio.sleep(3.98)
                duration=float(os.getenv("OFFLINE_RTVI_SUSTAINED_SECONDS","0"))
                if duration:
                    if not 1<=duration<=300:raise ValueError("Sustained burst duration must be 1..300 seconds")
                    until=clock.time()+duration;repeat=0
                    while clock.time()<until:
                        log.write("rtvi-overflow-start",count=1024,repeat=repeat)
                        for index in range(1024):
                            connection.send_app_message(dict(label="rtvi-ai",type="bot-llm-text",
                                data=dict(text=" token",index=index)))
                        log.write("rtvi-overflow-sent",count=1024,repeat=repeat)
                        repeat+=1;await asyncio.sleep(.5)
                    log.write("rtvi-overflow-complete",duration_s=duration,repeats=repeat)
                log.write("rtvi-burst-complete")
            burst_task = asyncio.create_task(burst_sweep(), name="offline-rtvi-burst")
        if mode == "offline-test":
            if skip_greeting(mode):
                log.write("greeting-suppressed", reason="explicit-offline-handoff")
                return
            await play_fixture("greeting.wav")
        else:
            await worker.queue_frames([TTSSpeakFrame(GREETING)])

    @rtvi.event_handler("on_client_message")
    async def message(processor, msg):
        if msg.type == "display-ready":
            display.ready(msg.data)
        elif msg.type == "display-result":
            display.result(msg.data)
        elif msg.type == "echo":
            await processor.send_server_response(msg, msg.data)
        elif msg.type == "rtp-stats" and rtp_evidence.DIRECTORY:
            report = rtp_evidence.summary(connection)
            await processor.send_server_message({"t": "rtp-stats", "d": report})
            await processor.send_server_response(msg, report)
        elif msg.type == "play":
            name = msg.data.get("fixture") if isinstance(msg.data, dict) else None
            if name not in fixture_paths:
                await processor.send_error_response(msg, "Unknown fixture")
                return
            duration_ms = msg.data.get("duration_ms", 0)
            # Permit the bench to repeat a 60 s request from archived firmware
            # which predates the console's explicit loop-count argument.
            default_loops = 1
            if mode == "offline-test" and duration_ms == 60000:
                default_loops = int(os.getenv("OFFLINE_FIXTURE_LOOPS", "1"))
            loops = msg.data.get("loops", default_loops)
            if not isinstance(duration_ms, int) or not 0 <= duration_ms <= 60000 or (duration_ms and mode != "offline-test"):
                await processor.send_error_response(msg, "Invalid fixture duration")
                return
            if not isinstance(loops, int) or not 1 <= loops <= 30 or (loops != 1 and (mode != "offline-test" or duration_ms != 60000)):
                await processor.send_error_response(msg, "Invalid fixture loops")
                return
            await play_fixture(name, duration_ms, loops)
            await processor.send_server_response(msg, {"fixture": name})
        else:
            await processor.send_error_response(msg, "Unknown command")

    @transport.event_handler("on_client_connected")
    async def connected(transport, client):
        ice_path.record(connection, log)
        log.write("transport-connected")

    @transport.event_handler("on_client_disconnected")
    async def disconnected(transport, client):
        log.write("transport-disconnected")
        if fixed_reply:
            await fixed_reply.close()
        await worker.cancel()

    log.write("session-start")
    await runner.add_workers(worker)
    async def loop_probe():
        clock=asyncio.get_running_loop()
        while True:
            expected=clock.time()+.02
            await asyncio.sleep(.02)
            lag=clock.time()-expected
            if lag>.02:log.write('event-loop-lag',lag_ms=lag*1000,expected_s=expected)
    loop_probe_task=asyncio.create_task(loop_probe(),name='step8e-loop-probe') if os.getenv('STEP8E_EVENT_LOOP_PROBE')=='1' else None
    if loop_probe_task:log.write('event-loop-probe-settings',period_ms=20,report_lag_over_ms=20)
    try:
        await runner.run()
    finally:
        if display_task:
            display_task.cancel()
            await asyncio.gather(display_task, return_exceptions=True)
        if loop_probe_task:
            loop_probe_task.cancel()
            await asyncio.gather(loop_probe_task,return_exceptions=True)
        if fixed_reply:
            await fixed_reply.close()
        if burst_task:
            burst_task.cancel()
            await asyncio.gather(burst_task, return_exceptions=True)
        if fixture_task:
            fixture_task.cancel()
            await asyncio.gather(fixture_task, return_exceptions=True)
        recording.close()
        log.write("session-end")

if __name__ == "__main__":
    import sys
    from pipecat.runner.run import main
    if not any(arg == "--port" or arg.startswith("--port=") for arg in sys.argv[1:]):
        sys.argv.extend(["--port", str(server_port())])
    main()
