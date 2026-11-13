"""What the bot really logs, read by this package's parser.

The events come from the real coordinator in the bot's offline pipeline
(server/test_duo_pipeline.py), not from lines written by hand. A change to the
bot's `route` or `outcome` events that the scorer would misread fails here,
not in a live run. No service, key or network is involved.
"""
from datetime import datetime
import time

import pytest

import journal
from test_duo_pipeline import FakeAddressee, auto_reply, pipeline, until

RULE = {"vocative_rule": True}


class Log:
    """The bot's log for a pipeline run: its events as loguru would write them, plus the transcript lines."""
    def __init__(self):
        self.began, self.said = time.monotonic(), []

    async def turn(self, h, text):
        self.said.append((time.monotonic(), text))
        await h.turn(text)

    def lines(self, h):
        base = datetime(2026, 11, 10, 1, 0, 0).timestamp()
        def line(at, message):
            stamp = datetime.fromtimestamp(base + at - self.began).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            return at, f"{stamp} | INFO     | duo_bot:event:1 - {message}"
        rows = [line(at, f"Transcript: user: {text}") for at, text in self.said]
        rows += [line(at, "DUO {} {}".format(kind, data)) for kind, data, at in h.events]  # As duo_bot.py formats it.
        return [text for _, text in sorted(rows, key=lambda row: row[0])]


def marks(*phrases):
    return [dict(start_s=float(index), end_s=index + 0.5, text=text, expected=expected)
            for index, (text, expected) in enumerate(phrases)]


@pytest.mark.asyncio
async def test_a_committed_switch_and_a_refused_one_as_the_coordinator_logs_them():
    log = Log()
    async with pipeline(auto_switch=True, addressee=FakeAddressee("either", "either", delay=.02), config=RULE) as h:
        await h.hello()
        await log.turn(h, "Devil, should I eat cake?")
        await auto_reply(h, "devil")
        await h.services["angel"].set_usable(False)  # The Angel's voice cannot take a turn now.
        await log.turn(h, "Angel, should I rest?")
        await auto_reply(h, "devil", count=2)
        lines = log.lines(h)
    heard = journal.turns(journal.records(lines))
    assert [(turn["active"], turn["target"], turn["reason"], turn["did"], turn["switched"]) for turn in heard] == [
        ("angel", "devil", "vocative-rule", "switched", "devil"),
        ("devil", None, "not-eligible", "stayed", None)]
    result = journal.report(lines, marks(("Devil, should I eat cake?", "devil"), ("Angel, should I rest?", "angel")))
    assert [row["result"] for row in result["rows"]] == ["ok", "route-miss"]
    # The refused turn carries the rule's audit fields in the bot's log, and is not counted as a ruled switch.
    assert "'reason': 'not-eligible'" in "".join(lines) and "'jev_reason': 'stay'" in "".join(lines)
    assert result["ruled"] == dict(applied=1, switched=1, changed=1)


@pytest.mark.asyncio
async def test_a_claim_that_lost_to_the_deadline_is_not_a_switch_as_the_coordinator_logs_it():
    # The ruled read is given up 50 ms before a 150 ms deadline, but its cancellation takes 120 ms:
    # the route is decided after the deadline has released the draft. The bot logs the target all the same.
    slow = FakeAddressee("either", delay=2)
    slow.cancel_delay = .12
    log = Log()
    async with pipeline(auto_switch=True, addressee=slow, config=RULE | {"route_budget_s": .15}) as h:
        await h.hello()
        await log.turn(h, "Devil, should I eat cake?")
        await auto_reply(h, "angel")  # The deadline released the draft: the Angel has answered.
        await until(lambda: any(kind == "route" for kind, _, _ in h.events))  # The read ends after that.
        lines = log.lines(h)
        assert not any(state.get("reason") == "addressed" for state in h.states())
    [turn] = journal.turns(journal.records(lines))
    assert (turn["target"], turn["reason"], turn["did"], turn["switched"]) == ("devil", "vocative-rule", "claim-lost", None)
    result = journal.report(lines, marks(("Devil, should I eat cake?", "devil")))
    assert [row["result"] for row in result["rows"]] == ["route-miss"]
    assert result["ruled"] == dict(applied=1, switched=0, changed=0)
