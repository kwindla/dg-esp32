"""Bot turn policy: the same for every firmware image (decision 141).

The device alone decides what reaches the bot: the AEC-off image closes its mic gate while
the speaker plays, the AEC image cancels the echo. The bot never mutes the user and always
allows interruptions. The firmware's AEC capability is parsed for the record only.
"""
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams


def capability_aec(body):
    if not isinstance(body, dict):
        return False
    audio = body.get('audio')
    if not isinstance(audio, dict):
        return False
    if type(audio.get('v')) is not int or audio['v'] != 1:
        return False
    if type(audio.get('aec')) is not bool:
        return False
    gain, guard = audio.get('ref_gain_db'), audio.get('guard_ms')
    if type(gain) is not int or gain not in range(0, 34, 3):
        return False
    if type(guard) is not int or not 0 <= guard <= 1000:
        return False
    return audio['aec']


def turn_policy(*, profile="demo"):
    profile = profile_name(profile)
    params = LLMUserAggregatorParams()
    if profile == "dev":
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        from pipecat.turns.user_turn_strategies import UserTurnStrategies
        from pipecat.turns.user_start import VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy
        from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
        params.vad_analyzer = SileroVADAnalyzer()
        params.user_turn_strategies = UserTurnStrategies(
            start=[VADUserTurnStartStrategy(),
                   TranscriptionUserTurnStartStrategy(use_interim=True)],
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())])
    return params


# Pipecat's STTMetadataFrame safety budget is measured from speech end,
# NOT from Smart Turn COMPLETE. Nova is_final is a segment, not utterance-end.
# The retained sentence04 last segment arrived 0.534s after speech end;
# the provider default 0.35s released the turn before "cooking." arrived.
# Keep the documented transcript wait mechanism and a conservative 1s budget.
# This changes turn END only; interim starts/interruptions stay immediate.
NOVA_TTFS_P99_S = 1.0
PROFILE_ENV = "ECHOEAR_STT_PROFILE"


def profile_name(value=None):
    value = "demo" if value is None else value
    if value not in ("demo", "dev"):
        raise ValueError("ECHOEAR_STT_PROFILE must be demo or dev")
    return value


def make_stt(api_key, *, profile="demo", event=None, nova_class=None):
    profile = profile_name(profile)
    if profile == "demo":
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        return DeepgramFluxSTTService(api_key=api_key, sample_rate=16000)
    from pipecat.services.deepgram.stt import DeepgramSTTService
    # Version-pinned metadata adapter: model version is vendor-observed, never
    # inferred from the requested alias. Empty/echo-only sessions can report it.
    class ObservedNova(nova_class or DeepgramSTTService):
        async def _on_message(self, message):
            metadata = getattr(message, "metadata", None)
            if event and metadata is not None:
                info = metadata.model_dump(mode="json") if hasattr(metadata, "model_dump") else {}
                models = {k: info[k] for k in ("models", "model_info", "model_uuid") if k in info}
                if models and models != getattr(self, "_logged_models", None):
                    self._logged_models = models
                    event("stt-model-observed", **models)
            await super()._on_message(message)
    return ObservedNova(api_key=api_key, sample_rate=16000,
        settings=DeepgramSTTService.Settings(model="nova-3-general", interim_results=True),
        ttfs_p99_latency=NOVA_TTFS_P99_S)


def profile_manifest(profile):
    """Hash effective settings, installed stack and local models (no secrets)."""
    import hashlib
    import json
    from importlib.metadata import version
    from pathlib import Path
    import pipecat
    profile = profile_name(profile)
    config = dict(profile=profile, sample_rate=16000,
                  tts="DeepgramFluxTTSService (unchanged)")
    if profile == "dev":
        from pipecat.audio.vad.vad_analyzer import VADParams
        config.update(stt="DeepgramSTTService", model="nova-3-general", interim_results=True,
                      keyterm=None, ttfs_p99_latency=NOVA_TTFS_P99_S,
                      vad=VADParams().model_dump(),
                      start=["VADUserTurnStartStrategy", "TranscriptionUserTurnStartStrategy"],
                      use_interim=True, enable_interruptions=True,
                      stop="TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3)",
                      wait_for_transcript=True, transcript_boundary="segment-final-not-utterance-final-v1")
    else:
        config.update(stt="DeepgramFluxSTTService", model="flux-general-en", turns="provider")
    root = Path(pipecat.__file__).parent
    models = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in (root/"audio/vad", root/"audio/turn")
              for p in folder.rglob("*.onnx")} if profile == "dev" else {}
    versions = {name: version(name) for name in ("pipecat-ai", "deepgram-sdk", "onnxruntime")}
    code_hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ("demo_policy.py", "demo_bot.py")}
    payload = dict(config=config, versions=versions, local_model_sha256=models, code_sha256=code_hashes)
    return dict(**payload, config_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                vendor_version="unobserved until stt-model-observed")


def transcript_boundary(profile, event=None):
    """Nova Finalize acknowledgements can precede trailing media/transcripts.

    Pipecat's public TranscriptionFrame.finalized flag bypasses its TTFS safety
    timer. A provider segment remains final text, but must not claim utterance
    completeness here. Smart Turn + STTMetadataFrame's deadline own that choice.
    No text is suppressed, merged or invented; interim interruption is untouched.
    """
    if profile_name(profile) == 'demo':
        return None
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
    class NovaTranscriptBoundary(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
                if event:
                    event('nova-transcript-boundary', provider_finalized=frame.finalized,
                          utterance_finalized=False, frame_id=frame.id)
                frame.finalized = False
            await self.push_frame(frame, direction)
    return NovaTranscriptBoundary()
