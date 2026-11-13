"""Paths and credential loading; offline mode never loads service credentials."""
import os
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parent
FIXTURES = SERVER / "fixtures"
GREETING = "Hello. I am ready for your question."
REPLY = "The code word is pineapple."
PROMPT = "Please tell me the capital of France."
VOICE = "aura-2-thalia-en"

def load_credentials(*, approved_only=False):
    if approved_only:
        values = dotenv_values(ROOT.parent / "pipecat/.env")
        for key in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(key, None)
            if values.get(key):
                os.environ[key] = values[key]
    else:
        load_dotenv(SERVER / ".env", override=False)
        load_dotenv(ROOT.parent / "pipecat/.env", override=False)
    # Read only the approved name into the environment; a missing file/key
    # must not leave an inherited TypeSafe credential in approved-only mode.
    if approved_only or "TYPESAFE_API_KEY" not in os.environ:
        typesafe = dotenv_values(Path.home() / ".config" / "typesafe.env")
        os.environ.pop("TYPESAFE_API_KEY", None)
        if typesafe.get("TYPESAFE_API_KEY"):
            os.environ["TYPESAFE_API_KEY"] = typesafe["TYPESAFE_API_KEY"]
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise RuntimeError("DEEPGRAM_API_KEY is required for live modes or fixture generation")

def server_port():
    """Shared command-line and test default; CLI --port may explicitly override."""
    port = int(os.getenv("SERVER_PORT", "7861"))
    if not 1 <= port <= 65535:
        raise ValueError("SERVER_PORT must be between 1 and 65535")
    return port

def voice_policy():
    policy = os.getenv("BOT_VOICE_POLICY", "smartturn")
    if policy not in {"smartturn", "timeout", "8d"}:
        raise ValueError("BOT_VOICE_POLICY must be smartturn, timeout or 8d")
    return policy

def smartturn_stop_secs():
    value = float(os.getenv('BOT_SMARTTURN_STOP_SECS', '3'))
    if not 0 < value <= 5:
        raise ValueError('BOT_SMARTTURN_STOP_SECS must be finite and >0..5 seconds')
    return value

def vad_min_volume(mode=None):
    # Pipecat 1.15.0 VADParams default, for every speech pipeline.
    default = "0.6"
    volume = float(os.getenv("BOT_VAD_MIN_VOLUME", default))
    if not 0 <= volume <= 1:
        raise ValueError("BOT_VAD_MIN_VOLUME must be 0..1")
    return volume

def fixture_paths(mode):
    paths = {name: FIXTURES / name for name in ("greeting.wav", "reply_pineapple.wav", "prompt_france.wav")}
    if mode == "offline-test":
        if os.getenv("OFFLINE_BENCH_FIXTURE_DIR"):
            bank = Path(os.environ["OFFLINE_BENCH_FIXTURE_DIR"])
            paths.update({name: bank / name for name in ("tts_1.wav", "tts_2.wav")})
    return paths

def skip_greeting(mode):
    return mode == "offline-test" and os.getenv("OFFLINE_SKIP_GREETING") == "1"

def offline_rtvi_burst_enabled(mode):
    flag = os.getenv("OFFLINE_RTVI_BURST_SWEEP", "0")
    if flag not in {"0", "1"}:
        raise ValueError("OFFLINE_RTVI_BURST_SWEEP must be 0 or 1")
    if flag == "1" and mode != "offline-test":
        raise ValueError("RTVI burst sweep requires offline-test")
    return flag == "1"
