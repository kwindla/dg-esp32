"""Replay the pre-change behavioural contract without credentials or sockets."""
from pathlib import Path

from duo_baseline import canonical_json, record_baseline


async def test_flagoff_matches_c2cc221_baseline(monkeypatch):
    monkeypatch.setenv("DUO_AUTO_SWITCH", "1")  # Explicit harness config must win.
    expected = Path(__file__).with_name("fixtures") / "duo_flagoff_baseline.json"
    assert canonical_json(await record_baseline(config={"auto_switch": False})) == expected.read_text()
