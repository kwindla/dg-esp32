"""Offline: percentiles, the reply-timing summary, barge-in latency and the error lines."""
import journal
import latency


def test_percentiles_interpolate_between_the_two_nearest_values():
    assert latency.pct([4, 1, 3, 2], 50) == 2.5
    assert latency.pct([10, 20, 30], 95) == 29.0
    assert latency.pct([10, 20, 30], 100) == 30
    assert latency.pct([5, None], 95) == 5
    assert latency.pct([], 50) is None and latency.pct([None], 50) is None


def row(decision, J, T1, A, held=0):
    """A `DUO timing` payload as DuoTimingObserver._emit builds it; None is an offset it could not observe."""
    return dict(mode="on", route_mode="rerun" if decision == "switch" else "routed", reply_id=7, decision=decision,
                origin="aggregator-user-stopped", p=0.9, target=None, fence_ms=0.4, J_ms=J, W_ms=J and J + 1,
                T1_ms=T1, S1_ms=T1 and T1 + 200, A_ms=A, held_delay_ms=held, hold_delayed=held > 0)


def test_timing_is_summarized_per_key_and_route_decision():
    summary = latency.timing([
        row("stay", 212.0, 380.0, 820.0), row("stay", 190.0, 150.0, 900.0, held=40.0),
        row("switch", 240.0, 900.0, 1380.0), row("timeout", 500.2, 310.0, 1010.0, held=190.6),
        row("unrouted", None, 250.0, None)])
    assert (summary["replies"], summary["decisions"]) == (5, {"stay": 2, "switch": 1, "timeout": 1, "unrouted": 1})
    assert list(summary["keys"]) == ["fence_ms", "J_ms", "W_ms", "T1_ms", "S1_ms", "A_ms", "held_delay_ms"]
    assert summary["keys"]["J_ms"] == {
        "stay": dict(n=2, p50=201.0, p95=210.9, max=212.0),
        "switch": dict(n=1, p50=240.0, p95=240.0, max=240.0),
        "timeout": dict(n=1, p50=500.2, p95=500.2, max=500.2)}  # The unrouted reply has no route to time.
    assert summary["keys"]["A_ms"]["stay"] == dict(n=2, p50=860.0, p95=896.0, max=900.0)
    assert summary["keys"]["T1_ms"]["unrouted"] == dict(n=1, p50=250.0, p95=250.0, max=250.0)
    assert summary["keys"]["held_delay_ms"]["stay"] == dict(n=2, p50=20.0, p95=38.0, max=40.0)
    assert latency.timing([]) == dict(replies=0, decisions={}, keys={})


BARGE = [
    # A turn that starts while the bot is silent is not a barge-in.
    "2026-11-10 01:00:10.000 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking",
    "2026-11-10 01:00:12.000 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking",
    "2026-11-10 01:00:16.500 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking",
    "2026-11-10 01:00:16.600 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking",
    "2026-11-10 01:00:16.742 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:817 - Bot stopped speaking",
    # A reply the bot finishes by itself.
    "2026-11-10 01:00:20.000 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking",
    "2026-11-10 01:00:25.000 | DEBUG    | pipecat.transports.base_output:_handle_frame:883 - Bot stopped speaking based on TTSStoppedFrame",
    "2026-11-10 01:00:25.000 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:817 - Bot stopped speaking",
    "2026-11-10 01:00:27.000 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking",
    "2026-11-10 01:00:30.000 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking",
    "2026-11-10 01:00:33.100 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking",
    "2026-11-10 01:00:33.418 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:817 - Bot stopped speaking",
    "2026-11-10 01:00:40.000 | INFO     | __main__:event:1497 - DUO route-cleanup-overdue {'token': '5a0c1e6f9d2b4c7e8a1f3b5d7c9e0f21'}",
    "2026-11-10 01:00:41.000 | INFO     | __main__:event:1497 - DUO route {'token': 'b', 'target': None, 'reason': 'stay', 'p': 0.9, 'elapsed_ms': 100.0}",
]


def test_barge_in_latency_runs_from_the_turn_start_to_the_bot_stopping_and_counts_overdue_cleanups():
    assert latency.barge_in(journal.records(BARGE)) == dict(
        n=2, p50=280.0, p95=314.2, max=318.0, mean=280.0, overdue=1, all=[242.0, 318.0])
    assert latency.barge_in([]) == dict(n=0, p50=None, p95=None, max=None, mean=None, overdue=0, all=[])


def test_error_lines_lose_project_ids_and_credentials_and_are_cut_short():
    stamp = "2026-11-10 01:01:40.000 | ERROR    | pipecat.services.deepgram.flux.tts_base:_receive:402 - "
    assert latency.errors([
        "2026-11-10 01:01:39.000 | INFO     | __main__:event:1497 - DUO ignored-error {'error': 'ERROR | none'}",
        stamp + "closed: {'dg-project-id': 'f00dfeed-1234-4abc-9def-000011112222', 'dg-request-id': '6b1f'}",
        "2026-11-10 01:01:41.000 | CRITICAL | __main__:run:1326 - coordinator failed",
        "Traceback (most recent call last):",
        '  File "server/duo_bot.py", line 1802, in bot',
        stamp + "handshake failed, request headers {'Authorization': 'Token 0123456789abcdef', 'x': 1}",
        stamp + "x" * 300,
    ]) == [
        stamp + "closed: {'dg-project-id:<r>, 'dg-request-id': '6b1f'}",
        "2026-11-10 01:01:41.000 | CRITICAL | __main__:run:1326 - coordinator failed",
        "Traceback (most recent call last):",
        stamp + "handshake failed, request headers {'Authorization <r>",
        stamp + "x" * (220 - len(stamp)),
    ]
