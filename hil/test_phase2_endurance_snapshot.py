import pytest
import phase2_endurance as endurance


class MissingDiagnosticCensus:
    cursor = 0

    def lines_since(self, cursor):
        return []


def test_missing_diagnostic_census_keeps_buffered_perf(monkeypatch):
    captures = []

    def captured(board):
        row = {"received": 100 + len(captures), "heap": {"internal": {"free": 140000}}}
        captures.append(row)
        return row

    monkeypatch.setattr(endurance, "perf", captured)
    monkeypatch.setattr(endurance.time, "sleep", lambda seconds: None)
    row = endurance.snapshot(MissingDiagnosticCensus(), active=True, allow_incomplete=True)
    assert row["perf"] == captures[-1]
    assert [r["perf"] for r in row["incomplete_census_attempts"]] == captures[:-1]
    assert row["heap"] == {}
    assert row["census_complete"] is False


def test_strict_census_still_rejects_missing_diagnostics(monkeypatch):
    monkeypatch.setattr(endurance, "perf", lambda board: {"received": 100})
    monkeypatch.setattr(endurance.time, "sleep", lambda seconds: None)
    with pytest.raises(AssertionError, match="Incomplete heap/audio census"):
        endurance.snapshot(MissingDiagnosticCensus(), active=True)
