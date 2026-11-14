"""Replay the pre-change behavioural contract without credentials or sockets."""
import json
from pathlib import Path
import re

from duo_baseline import canonical_json, record_baseline
from duo_prompts import duo_instructions

FIXTURES = Path(__file__).with_name("fixtures")


async def test_flagoff_matches_c2cc221_baseline(monkeypatch):
    # DUO_CONTEXT=labels is the bot as it was at eebf273: this recording did not change when b2h arrived.
    monkeypatch.setenv("DUO_AUTO_SWITCH", "1")  # Explicit harness config must win.
    monkeypatch.setenv("DUO_CONTEXT", "b2h")
    expected = FIXTURES / "duo_flagoff_baseline.json"
    assert canonical_json(await record_baseline(config={"auto_switch": False, "context": "labels"})) == expected.read_text()


async def test_b2h_matches_its_recording():
    # The same six scenarios with the default context. Regenerate with ``uv run python duo_baseline.py b2h``.
    expected = (FIXTURES / "duo_b2h_baseline.json").read_text()
    assert canonical_json(await record_baseline(config={"auto_switch": False, "context": "b2h"})) == expected
    recorded = json.loads(expected)
    assert "Silent context" not in expected and not re.search(r"\[(Angel|Devil)\]", expected)
    for scenario in recorded.values():
        for call in scenario["llm_calls"]:
            assert call["system_instruction"] == duo_instructions()
        for messages in [scenario["context_messages"]] + [call["messages"] for call in scenario["llm_calls"]]:
            assert {m["role"] for m in messages} <= {"user", "assistant"}
            assert all(re.search(r" \[(ANGEL|DEVIL)\]$", m["content"]) for m in messages if m["role"] == "user")
            assert not any("[" in m["content"] for m in messages if m["role"] == "assistant")
        # No sentence handed to a voice carries a marker.
        assert not any("[" in text for voice in scenario["tts"].values() for text in voice["text"])
