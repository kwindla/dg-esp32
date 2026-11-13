"""Exercise the demo wiring and installed Pipecat observer without services/network."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import demo_bot
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.observer import RTVIObserver, RTVIObserverParams
from pipecat.runner.types import SmallWebRTCRunnerArguments


@pytest.mark.parametrize('body', [
    {'audio': {'v': 1, 'aec': True, 'ref_gain_db': 15, 'guard_ms': 400}},
    {'audio': {'v': 1, 'aec': False, 'ref_gain_db': 15, 'guard_ms': 400}},
    {},
])
@pytest.mark.parametrize('proactive', [None, '0', '1'])
@pytest.mark.parametrize('profile', [None, 'dev'])
async def test_demo_disables_only_rtvi_metrics_and_user_input_forwarding(monkeypatch, body, proactive, profile):
    # Stop before any worker/service is run; capture the actual bot's kwargs.
    for name in ("ECHOEAR_AEC_EVALUATION_DIR", "ECHOEAR_AEC_SESSION_DIR"):
        monkeypatch.delenv(name, raising=False)
    import tts_connection_probe
    proactive_class = MagicMock()
    monkeypatch.setattr(tts_connection_probe, 'ProactiveConnectionTTS', proactive_class)
    for name in ('DEMO_TTS_PROACTIVE_RECONNECT', 'DEMO_TTS_RECONNECT_EACH_GENERATION',
                 'DEMO_TTS_TRACE_GENERATIONS'):
        monkeypatch.delenv(name, raising=False)
    if proactive is not None:
        monkeypatch.setenv('DEMO_TTS_PROACTIVE_RECONNECT', proactive)
    if profile is None:
        monkeypatch.delenv("ECHOEAR_STT_PROFILE", raising=False)
    else:
        monkeypatch.setenv("ECHOEAR_STT_PROFILE", profile)
    monkeypatch.setenv("DEMO_LLM", "local")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "host-test-unused")
    monkeypatch.delenv("ECHOEAR_AEC_FULL_DUPLEX", raising=False)
    monkeypatch.setattr(demo_bot, "load_credentials", lambda **_: None)
    for name in ("SmallWebRTCTransport", "make_stt",
                 "DeepgramFluxTTSService", "OpenAILLMService", "Pipeline"):
        monkeypatch.setattr(demo_bot, name, MagicMock())
    captured_user_params = []
    def pair(*args, **kwargs):
        captured_user_params.append(kwargs["user_params"].user_mute_strategies)
        return MagicMock(), MagicMock()
    monkeypatch.setattr(demo_bot, "LLMContextAggregatorPair", pair)
    captured = {}

    class WorkerConstructed(Exception):
        pass

    def capture_worker(pipeline, **kwargs):
        captured.update(kwargs)
        raise WorkerConstructed

    monkeypatch.setattr(demo_bot, "PipelineWorker", capture_worker)
    with pytest.raises(WorkerConstructed):
        await demo_bot._bot(SmallWebRTCRunnerArguments(webrtc_connection=MagicMock(), body=body))
    # Decision 141: one turn policy whatever the firmware reports.
    assert "full_duplex" not in demo_bot.make_stt.call_args.kwargs
    assert captured_user_params == [[]]
    assert demo_bot.make_stt.call_args.kwargs["profile"] == (profile or "demo")
    assert proactive_class.call_count == (proactive != '0')
    assert demo_bot.DeepgramFluxTTSService.call_count == (proactive == '0')
    assert captured["params"].enable_metrics
    assert captured["params"].enable_usage_metrics
    params = captured["rtvi_observer_params"]
    defaults = RTVIObserverParams()
    # user-input (Pipecat 1.13) repeats every user-transcription, and the firmware reads only the latter.
    assert params.metrics_enabled is False and params.user_input_enabled is False
    # No other RTVI option or interaction setting changes with this fix.
    assert {k: v for k, v in vars(params).items() if k not in ("metrics_enabled", "user_input_enabled")} == {
        k: v for k, v in vars(defaults).items() if k not in ("metrics_enabled", "user_input_enabled")
    }

    async def messages(observer_params):
        observer = RTVIObserver(params=observer_params)
        observer.send_rtvi_message = AsyncMock()
        for frame in (
            MetricsFrame(data=[]),  # startup empty envelope
            MetricsFrame(data=[TTFBMetricsData(processor="tts", value=0.3),
                               ProcessingMetricsData(processor="tts", value=0.5)]),
            BotStartedSpeakingFrame(), InterruptionFrame(), BotStoppedSpeakingFrame(),
        ):
            await observer.on_push_frame(FramePushed(
                source=None, destination=None, frame=frame,
                direction=FrameDirection.DOWNSTREAM, timestamp=0, first_push=True,
            ))
        return [call.args[0].type for call in observer.send_rtvi_message.await_args_list]

    controls = ["bot-started-speaking", "bot-interrupted", "bot-stopped-speaking"]
    # Positive control proves these frames exercise actual metrics serialization.
    assert await messages(defaults) == ["metrics", "metrics", *controls]
    assert await messages(params) == controls


@pytest.mark.parametrize('value', ['0', '1', ''])
async def test_removed_duplex_override_fails_loudly(monkeypatch, value):
    # Decision 141: an old harness that still sets the override must not run silently.
    monkeypatch.setenv("ECHOEAR_AEC_FULL_DUPLEX", value)
    monkeypatch.setattr(demo_bot, "load_credentials", lambda **_: None)
    monkeypatch.setattr(demo_bot, "SmallWebRTCTransport", MagicMock())
    with pytest.raises(ValueError, match="ECHOEAR_AEC_FULL_DUPLEX was removed"):
        await demo_bot._bot(SmallWebRTCRunnerArguments(webrtc_connection=MagicMock(), body={}))


@pytest.mark.parametrize('value', ['0', ''])
def test_removed_duplex_override_refused_at_process_start(value):
    # Astra decision-141 review #1: refused before Pipecat's runner starts (it would report HTTP ready).
    # --help: even without the refusal, main() would only print usage, never bind a port.
    import os, subprocess, sys
    from pathlib import Path
    env = dict(os.environ, ECHOEAR_AEC_FULL_DUPLEX=value)
    done = subprocess.run([sys.executable, str(Path(demo_bot.__file__)), '--help'], env=env,
                          capture_output=True, text=True, timeout=120, cwd=Path(demo_bot.__file__).parent)
    assert done.returncode == 1 and 'ECHOEAR_AEC_FULL_DUPLEX was removed' in done.stderr
    assert 'usage' not in done.stdout
