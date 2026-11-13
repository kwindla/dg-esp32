"""Latency summaries from the bot's log: reply timing per route decision, and barge-in.

`DUO timing` rows (server/duo_bot.py, DuoTimingObserver._emit) hold offsets in
milliseconds from the end of the user turn: fence_ms (the reply's context
admitted), J_ms (route decided), W_ms (held text released), T1_ms (first LLM
text), S1_ms (first sentence given to TTS), A_ms (first audio written), and
held_delay_ms (how long the first text waited for the route). A switched turn
is reported by its rerun, under the decision "switch".
"""
from collections import Counter
import re
import statistics

KEYS = ("fence_ms", "J_ms", "W_ms", "T1_ms", "S1_ms", "A_ms", "held_delay_ms")
ERROR = re.compile(r"\| (ERROR|CRITICAL) ")
PROJECT_ID = re.compile(r"dg-project-id[^,}]*")  # In the response headers a failed Deepgram connection logs.
# analyze.py stopped at project ids. A logged request header would hold a key,
# so a line is also cut at the first word that announces one.
CREDENTIAL = re.compile(r"\b(authorization|bearer|token|api[-_]?key|secret)\b.*", re.I)


def pct(values, p):
    """The p-th percentile, linearly interpolated between the two nearest values."""
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    rank = (len(values) - 1) * p / 100
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (rank - low), 1)


def timing(rows):
    """Replies per route decision, and n/p50/p95/max of each latency key per decision."""
    decisions = Counter(str(row.get("decision")) for row in rows)
    keys = {}
    for key in KEYS:
        for decision in sorted(decisions):
            values = [row[key] for row in rows if str(row.get("decision")) == decision and row.get(key) is not None]
            if values:
                keys.setdefault(key, {})[decision] = dict(n=len(values), p50=pct(values, 50), p95=pct(values, 95),
                                                          max=pct(values, 100))
    return dict(replies=len(rows), decisions=dict(sorted(decisions.items())), keys=keys)


def barge_in(records):
    """Barge-in latency in ms: Flux hears a turn start while the bot speaks, until the bot stops speaking.

    Keyed, as barge.py was, on the Pipecat functions that log the three
    events: the output transport's _bot_started_speaking/_bot_stopped_speaking
    and Flux's _handle_start_of_turn. `overdue` counts sessions the bot ended
    because a cancelled route read never finished (`DUO route-cleanup-overdue`).
    """
    speaking, began, latencies, overdue = False, None, [], 0
    for record in records:
        if record.function == "_bot_started_speaking":
            speaking = True
        elif record.function == "_bot_stopped_speaking":
            if began is not None:
                latencies.append(round((record.t - began) * 1000, 1))
                began = None
            speaking = False
        elif record.function == "_handle_start_of_turn" and speaking and began is None:
            began = record.t
        elif record.message.startswith("DUO route-cleanup-overdue "):
            overdue += 1
    return dict(n=len(latencies), p50=pct(latencies, 50), p95=pct(latencies, 95), max=pct(latencies, 100),
                mean=round(statistics.mean(latencies), 1) if latencies else None, overdue=overdue, all=latencies)


def errors(lines):
    """ERROR, CRITICAL and traceback lines, cut to 220 characters, without project ids or credentials."""
    return [CREDENTIAL.sub(r"\1 <r>", PROJECT_ID.sub("dg-project-id:<r>", line))[:220]
            for line in lines if ERROR.search(line) or "Traceback" in line]
