"""Offline: the bot's log lines as server/duo_bot.py, server/duo_route.py and Pipecat 1.15 write them."""
import json
import os
import subprocess
import time

import pytest

import journal

# One web run of the functional cohort, with auto-switch on. The WAV began at 01:00:00.
RUN = """\
2026-11-10 01:00:01.200 | INFO     | __main__:bot:1777 - DUO_IDENTITY {"config": {"auto_switch": true}, "config_sha256": "0f"}
2026-11-10 01:00:01.950 | INFO     | __main__:event:1497 - DUO outcome {'type': 'duo', 'v': 1, 'op': 'state', 'session': '9f3c2a1b5d6e7f80', 'conn': '4be1c7d2a9f04c6e8b1d2f3a4c5e6f70', 'seq': 1, 'rev': 1, 'character': 'angel'}
2026-11-10 01:00:10.400 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking
2026-11-10 01:00:13.090 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_end_of_turn:883 - User stopped speaking
2026-11-10 01:00:13.100 | INFO     | __main__:user_stopped:1561 - Transcript: user: Devil, should I eat the whole cake tonight?
2026-11-10 01:00:13.340 | INFO     | __main__:event:1497 - DUO route {'token': '5a0c1e6f9d2b4c7e8a1f3b5d7c9e0f21', 'target': 'devil', 'reason': None, 'p': 0.97, 'elapsed_ms': 238.6, 'jev': {'connection': 'reused'}}
2026-11-10 01:00:13.352 | INFO     | __main__:event:1497 - DUO phase-timing {'phase': 'retirement_wait', 'operation': 'c41d', 'transaction': None, 'conn': '4be1c7d2a9f04c6e8b1d2f3a4c5e6f70', 'rev': 1, 'session': None, 'seq': None, 'reverting': False, 'elapsed_ms': 0.021, 'success': True}
2026-11-10 01:00:13.610 | INFO     | __main__:event:1497 - DUO verified {'transaction': '77aa', 'character': 'devil', 'reverting': False}
2026-11-10 01:00:13.612 | INFO     | __main__:event:1497 - DUO outcome {'type': 'duo', 'v': 1, 'op': 'state', 'session': '9f3c2a1b5d6e7f80', 'conn': '4be1c7d2a9f04c6e8b1d2f3a4c5e6f70', 'seq': 1, 'rev': 2, 'character': 'devil', 'reason': 'addressed'}
2026-11-10 01:00:14.480 | INFO     | __main__:_emit:290 - DUO timing {"A_ms": 1380.0, "J_ms": 240.0, "S1_ms": 1100.0, "T1_ms": 900.0, "W_ms": 241.0, "decision": "switch", "fence_ms": 0.4, "held_delay_ms": 0, "hold_delayed": false, "mode": "on", "origin": "aggregator-user-stopped", "p": 0.97, "reply_id": 4412, "route_mode": "rerun", "target": "devil"}
2026-11-10 01:00:14.481 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking
2026-11-10 01:00:19.700 | DEBUG    | pipecat.transports.base_output:_handle_frame:883 - Bot stopped speaking based on TTSStoppedFrame
2026-11-10 01:00:19.700 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:817 - Bot stopped speaking
2026-11-10 01:00:19.702 | INFO     | __main__:assistant_stopped:1572 - Transcript: assistant: Oh, absolutely. Every last crumb.
2026-11-10 01:00:34.900 | DEBUG    | pipecat.services.deepgram.flux.stt_base:_handle_start_of_turn:813 - User started speaking
2026-11-10 01:00:37.050 | INFO     | __main__:user_stopped:1561 - Transcript: user: And what would you put on top of it?
2026-11-10 01:00:37.262 | INFO     | __main__:event:1497 - DUO route {'token': '0b7d2c4e6a8f1b3d5c7e9a0b2d4f6c81', 'target': None, 'reason': 'stay', 'p': 0.94, 'elapsed_ms': 210.3, 'jev': {'connection': 'reused'}}
2026-11-10 01:00:37.870 | INFO     | __main__:_emit:290 - DUO timing {"A_ms": 820.0, "J_ms": 212.0, "S1_ms": 560.0, "T1_ms": 380.0, "W_ms": 213.0, "decision": "stay", "fence_ms": 0.3, "held_delay_ms": 0, "hold_delayed": false, "mode": "on", "origin": "aggregator-user-stopped", "p": 0.94, "reply_id": 4890, "route_mode": "routed", "target": null}
2026-11-10 01:00:37.871 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking
2026-11-10 01:00:42.300 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:817 - Bot stopped speaking
2026-11-10 01:01:01.200 | INFO     | __main__:user_stopped:1561 - Transcript: user: Can I hear from the angel now?
2026-11-10 01:01:01.415 | INFO     | __main__:event:1497 - DUO route {'token': '9c3e5a7b1d2f4c6e8a0b2d4f6c8e0a13', 'target': 'angel', 'reason': None, 'p': 0.99, 'elapsed_ms': 213.0, 'jev': {'connection': 'reused'}}
2026-11-10 01:01:01.700 | INFO     | __main__:event:1497 - DUO outcome {'type': 'duo', 'v': 1, 'op': 'state', 'session': '9f3c2a1b5d6e7f80', 'conn': '4be1c7d2a9f04c6e8b1d2f3a4c5e6f70', 'seq': 1, 'rev': 3, 'character': 'angel', 'reason': 'addressed'}
2026-11-10 01:01:02.620 | INFO     | __main__:_emit:290 - DUO timing {"A_ms": 1420.0, "J_ms": 214.0, "S1_ms": 1150.0, "T1_ms": 940.0, "W_ms": 215.0, "decision": "switch", "fence_ms": 0.5, "held_delay_ms": 0, "hold_delayed": false, "mode": "on", "origin": "aggregator-user-stopped", "p": 0.99, "reply_id": 5377, "route_mode": "rerun", "target": "angel"}
2026-11-10 01:01:26.900 | INFO     | __main__:user_stopped:1561 - Transcript: user: Speak of the devil, I just found another cake in the fridge.
2026-11-10 01:01:27.150 | INFO     | __main__:event:1497 - DUO route {'token': '2d4f6c8e0a1b3d5c7e9a0b2d4f6c8e05', 'target': 'devil', 'reason': None, 'p': 0.88, 'elapsed_ms': 248.0, 'jev': {'connection': 'new', 'connect_ms': 11.2, 'tls_ms': 38.4}}
2026-11-10 01:01:27.420 | INFO     | __main__:event:1497 - DUO outcome {'type': 'duo', 'v': 1, 'op': 'state', 'session': '9f3c2a1b5d6e7f80', 'conn': '4be1c7d2a9f04c6e8b1d2f3a4c5e6f70', 'seq': 1, 'rev': 4, 'character': 'devil', 'reason': 'addressed'}
2026-11-10 01:01:28.400 | INFO     | __main__:_emit:290 - DUO timing {"A_ms": 1500.0, "J_ms": 250.0, "S1_ms": 1200.0, "T1_ms": 1000.0, "W_ms": 251.0, "decision": "switch", "fence_ms": 0.6, "held_delay_ms": 0, "hold_delayed": false, "mode": "on", "origin": "aggregator-user-stopped", "p": 0.88, "reply_id": 5903, "route_mode": "rerun", "target": "devil"}
2026-11-10 01:01:40.000 | ERROR    | pipecat.services.deepgram.flux.tts_base:_receive:402 - DuoFluxTTS#1 connection closed: {'dg-project-id': 'f00dfeed-1234-4abc-9def-000011112222', 'dg-request-id': '6b1f'}
Traceback (most recent call last):
  File "server/duo_bot.py", line 1802, in bot
""".splitlines()
T0 = 1794272400.0  # 2026-11-10 01:00:00 UTC
MARKS = [
    dict(start_s=10.0, end_s=12.5, text="Devil, should I eat the whole cake tonight?", expected="devil"),
    dict(start_s=34.5, end_s=36.5, text="And what would you put on top of it?", expected="none"),
    dict(start_s=58.5, end_s=60.5, text="Can I hear from the angel now?", expected="angel"),
    dict(start_s=82.5, end_s=86.0, text="Speak of the devil, I just found another cake in the fridge.", expected="none"),
]
PLAYED = [dict(t=T0, wav_s=0, len_s=145)]


@pytest.fixture(autouse=True)
def utc():
    """The bot stamps local time; the literal epochs below are those lines read in UTC."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    time.tzset()


def at(seconds):
    return round(T0 + seconds, 3)


def test_a_record_is_a_loguru_line_and_nothing_else_is():
    assert journal.records([
        "2026-11-10 01:00:13.100 | INFO     | __main__:user_stopped:1561 - Transcript: user: Devil, - should I?",
        "2026-11-10 01:00:14.481 | DEBUG    | pipecat.transports.base_output:_bot_started_speaking:712 - Bot started speaking",
        "2026-11-10 01:00:15.000 | CRITICAL | duo_bot:<lambda>:779 - DUO coordinator-exit {'error': 'TypeError'}",
        "Traceback (most recent call last):",
        '  File "server/duo_bot.py", line 1802, in bot',
        "",
        "INFO:     127.0.0.1:53122 - \"POST /start HTTP/1.1\" 200 OK",
    ]) == [
        journal.Record(1794272413.1, "INFO", "user_stopped", "Transcript: user: Devil, - should I?"),
        journal.Record(1794272414.481, "DEBUG", "_bot_started_speaking", "Bot started speaking"),
        journal.Record(1794272415.0, "CRITICAL", "<lambda>", "DUO coordinator-exit {'error': 'TypeError'}"),
    ]


def test_duo_events_are_python_reprs_and_timing_rows_are_json():
    assert journal.duo("DUO route {'token': 'ab', 'target': None, 'reason': 'stay', 'p': 0.94, 'elapsed_ms': 210.3, "
                       "'jev': {'connection': 'new', 'hedged': True}}") == (
        "route", dict(token="ab", target=None, reason="stay", p=0.94, elapsed_ms=210.3, jev=dict(connection="new", hedged=True)))
    assert journal.duo('DUO timing {"A_ms": null, "decision": "timeout", "hold_delayed": false, "p": 0}') == (
        "timing", dict(A_ms=None, decision="timeout", hold_delayed=False, p=0))
    assert journal.duo("DUO invalid-request {}") == ("invalid-request", {})
    assert journal.duo('DUO gc {"generation": 2, "ms": 91.4, "collected": 12}') == ("gc", dict(generation=2, ms=91.4, collected=12))
    for other in ("DUO auto-switch disabled: no TypeSafe key", 'DUO_IDENTITY {"config": {}}', "Transcript: user: DUO route {}",
                  "DUO route {'token': ", "DUO route [1, 2]", "DUO timing {'A_ms': None}"):
        assert journal.duo(other) is None


def test_turns_carry_the_transcript_the_active_character_and_the_route():
    assert journal.turns(journal.records(RUN)) == [
        dict(t=at(13.1), transcript="Devil, should I eat the whole cake tonight?", active="angel", routed=True,
             target="devil", reason="switch", p=0.97, did="switched", switched="devil"),
        dict(t=at(37.05), transcript="And what would you put on top of it?", active="devil", routed=True,
             target=None, reason="stay", p=0.94, did="stayed", switched=None),
        dict(t=at(61.2), transcript="Can I hear from the angel now?", active="devil", routed=True,
             target="angel", reason="switch", p=0.99, did="switched", switched="angel"),
        dict(t=at(86.9), transcript="Speak of the devil, I just found another cake in the fridge.", active="angel",
             routed=True, target="devil", reason="switch", p=0.88, did="switched", switched="devil"),
    ]


def line(clock, function, message):
    return f"2026-11-10 01:00:{clock} | INFO     | __main__:{function}:1 - {message}"


def test_a_route_superseded_by_the_next_turn_stays_with_its_own_turn():
    # The first turn's read was still out when the second turn ended: its route is logged after the second transcript.
    heard = journal.turns(journal.records([
        line("20.000", "user_stopped", "Transcript: user: Devil,"),
        line("20.300", "user_stopped", "Transcript: user: should I eat the whole cake tonight?"),
        line("20.310", "event", "DUO route {'token': 'a', 'target': None, 'reason': 'superseded', 'p': 0, 'elapsed_ms': 309.0}"),
        line("20.520", "event", "DUO route {'token': 'b', 'target': None, 'reason': 'stay', 'p': 0.91, 'elapsed_ms': 219.0}"),
    ]))
    assert [(turn["transcript"], turn["reason"], turn["p"]) for turn in heard] == [
        ("Devil,", "superseded", 0), ("should I eat the whole cake tonight?", "stay", 0.91)]


def test_a_switch_by_the_vocative_rule_carries_what_jev_alone_would_have_done():
    # server/duo_route.py logs a ruled switch with reason 'vocative-rule' and Jev's own decision beside it.
    run = [line("13.100", "user_stopped", "Transcript: user: Devil, what should I read next?"),
           line("13.190", "event", "DUO route {'token': 'a', 'target': 'devil', 'reason': 'vocative-rule', 'p': 0.77, "
                                   "'elapsed_ms': 84.0, 'jev_target': None, 'jev_reason': 'below-floor'}"),
           line("13.500", "event", "DUO outcome {'op': 'state', 'character': 'devil', 'reason': 'addressed'}"),
           line("35.100", "user_stopped", "Transcript: user: Angel, should I learn the guitar?"),
           line("35.180", "event", "DUO route {'token': 'b', 'target': 'angel', 'reason': 'vocative-rule', 'p': 0.95, "
                                   "'elapsed_ms': 75.0, 'jev_target': 'angel', 'jev_reason': None}"),
           line("35.500", "event", "DUO outcome {'op': 'state', 'character': 'angel', 'reason': 'addressed'}"),
           line("57.100", "user_stopped", "Transcript: user: What should I have for breakfast?"),
           line("57.180", "event", "DUO route {'token': 'c', 'target': None, 'reason': 'stay', 'p': 0.9, 'elapsed_ms': 75.0}")]
    heard = journal.turns(journal.records(run))
    assert [(turn["target"], turn["reason"], turn.get("jev")) for turn in heard] == [
        ("devil", "vocative-rule", "below-floor"), ("angel", "vocative-rule", "switch"), (None, "stay", None)]
    marks = [dict(start_s=10.0, end_s=12.5, text="Devil, what should I read next?", expected="devil"),
             dict(start_s=32.0, end_s=34.5, text="Angel, should I learn the guitar?", expected="angel"),
             dict(start_s=54.0, end_s=56.5, text="What should I have for breakfast?", expected="none")]
    result = journal.report(run, marks, [dict(t=at(0), wav_s=0, len_s=90)])
    assert result["ruled"] == dict(applied=2, switched=2, changed=1)
    assert [(row["result"], row.get("jev")) for row in result["rows"]] == [("ok", "below-floor"), ("ok", "switch"), ("ok", None)]
    text = "\n".join(journal.render("r", result))
    assert "vocative rule: applied to 2 turns, 2 switched, 1 of those Jev alone would not have made" in text
    assert "route=devil (by rule; Jev alone: below-floor) p=0.77" in text
    assert "route=angel (by rule; Jev alone: switch) p=0.95" in text


def test_a_proposed_switch_counts_only_when_the_bot_says_it_switched():
    # The route line is logged before the claim. Its target is a proposal: only an `outcome` with
    # reason 'addressed' says the character changed. Three turns that each should switch to the Devil.
    marks = [dict(start_s=10.0, end_s=12.5, text="Devil, what should I read next?", expected="devil"),
             dict(start_s=22.0, end_s=24.5, text="Devil, should I call my sister?", expected="devil"),
             dict(start_s=34.0, end_s=36.5, text="Devil, should I buy new shoes?", expected="devil"),
             dict(start_s=46.0, end_s=48.5, text="Devil, is coffee a good idea right now?", expected="devil")]
    run = [
        # 1. The claim lost to the deadline: a target and the rule's fields, but no outcome. The Angel answered.
        line("13.100", "user_stopped", "Transcript: user: Devil, what should I read next?"),
        line("13.700", "event", "DUO route {'token': 'a', 'target': 'devil', 'reason': 'vocative-rule', 'p': 0, "
                                "'elapsed_ms': 600.0, 'jev_target': None, 'jev_reason': 'timeout'}"),
        # 2. The Devil's voice could not take the turn: the rule's fields are there, the reason is not the rule's.
        line("25.100", "user_stopped", "Transcript: user: Devil, should I call my sister?"),
        line("25.180", "event", "DUO route {'token': 'b', 'target': None, 'reason': 'not-eligible', 'p': 0.9, "
                                "'elapsed_ms': 75.0, 'jev_target': None, 'jev_reason': 'stay'}"),
        # 3. Switched, then taken back because the first reply failed.
        line("37.100", "user_stopped", "Transcript: user: Devil, should I buy new shoes?"),
        line("37.180", "event", "DUO route {'token': 'c', 'target': 'devil', 'reason': None, 'p': 0.93, 'elapsed_ms': 75.0}"),
        line("37.400", "event", "DUO outcome {'op': 'state', 'character': 'devil', 'reason': 'addressed'}"),
        line("38.900", "event", "DUO outcome {'op': 'state', 'character': 'angel', 'reason': 'reverted'}"),
        # 4. Switched.
        line("49.100", "user_stopped", "Transcript: user: Devil, is coffee a good idea right now?"),
        line("49.180", "event", "DUO route {'token': 'd', 'target': 'devil', 'reason': 'vocative-rule', 'p': 0.7, "
                                "'elapsed_ms': 75.0, 'jev_target': None, 'jev_reason': 'below-floor'}"),
        line("49.400", "event", "DUO outcome {'op': 'state', 'character': 'devil', 'reason': 'addressed'}"),
    ]
    heard = journal.turns(journal.records(run))
    assert [(turn["target"], turn["did"], turn["switched"], turn["active"]) for turn in heard] == [
        ("devil", "claim-lost", None, "angel"), (None, "stayed", None, "angel"),
        ("devil", "reverted", None, "angel"), ("devil", "switched", "devil", "angel")]
    result = journal.report(run, marks, [dict(t=at(0), wav_s=0, len_s=120)])
    assert [row["result"] for row in result["rows"]] == ["route-miss", "route-miss", "route-miss", "ok"]
    # The rule was applied to two turns, one of them switched, and Jev alone would not have made that one.
    assert result["ruled"] == dict(applied=2, switched=1, changed=1)
    text = "\n".join(journal.render("r", result))
    assert "vocative rule: applied to 2 turns, 1 switched, 1 of those Jev alone would not have made" in text
    assert "route=devil (by rule; Jev alone: timeout) BUT claim-lost p=0" in text
    assert "route=not-eligible p=0.9" in text and "route=devil BUT reverted p=0.93" in text


def test_empty_turns_are_not_turns_and_a_turn_without_a_route_is_unrouted():
    heard = journal.turns(journal.records([
        line("05.000", "event", "DUO outcome {'type': 'duo', 'v': 1, 'op': 'error', 'session': 's', 'conn': 'c', 'seq': 2, "
                                 "'rev': 5, 'character': 'devil', 'reason': 'stale'}"),
        line("10.000", "user_stopped", "Transcript: user: "),
        line("11.000", "user_stopped", "Transcript: user: None"),
        line("12.000", "user_stopped", "Transcript: user:"),
        line("20.000", "user_stopped", "Transcript: user: Hello there."),
        line("30.000", "event", "DUO route {'token': 'late', 'target': 'angel', 'reason': None, 'p': 0.9, 'elapsed_ms': 200.0}"),
        line("40.000", "assistant_stopped", "Transcript: assistant: Hello."),
    ]), start="angel")
    assert heard == [dict(t=at(20.0), transcript="Hello there.", active="devil", routed=False,
                          target=None, reason="unrouted", p=None, did="stayed", switched=None)]


def test_the_report_scores_the_run_and_summarizes_its_latency():
    result = journal.report(RUN, MARKS, PLAYED)
    assert (result["records"], result["turns"], result["routed"], result["aligned"]) == (29, 4, 4, "time")
    assert result["events"] == {"outcome": 4, "phase-timing": 1, "route": 4, "timing": 4, "verified": 1}
    assert result["score"] == {"ok": 3, "stt-noise": 0, "route-miss": 1, "stt-miss": 0, "missing": 0}
    assert (result["extra"], result["stray"], result["unplayed"]) == (0, [], 0)
    assert [(row["result"], row["active"], row["wanted"], row["target"]) for row in result["rows"]] == [
        ("ok", "angel", "devil", "devil"), ("ok", "devil", None, None),
        ("ok", "devil", "angel", "angel"), ("route-miss", "angel", None, "devil")]
    assert result["timing"]["decisions"] == {"stay": 1, "switch": 3}
    assert result["timing"]["keys"]["A_ms"] == {"stay": dict(n=1, p50=820.0, p95=820.0, max=820.0),
                                               "switch": dict(n=3, p50=1420.0, p95=1492.0, max=1500.0)}
    assert result["barge_in"] == dict(n=0, p50=None, p95=None, max=None, mean=None, overdue=0, all=[])
    assert result["errors"] == [
        "2026-11-10 01:01:40.000 | ERROR    | pipecat.services.deepgram.flux.tts_base:_receive:402 - "
        "DuoFluxTTS#1 connection closed: {'dg-project-id:<r>, 'dg-request-id': '6b1f'}",
        "Traceback (most recent call last):"]


def test_the_rendered_report_names_every_phrase_and_no_project_id():
    text = journal.render("a1-functional", journal.report(RUN, MARKS, PLAYED))
    assert text[0] == "a1-functional turns=4 routed=4 replies=4 decisions={'stay': 1, 'switch': 3} errors=2"
    assert "  A_ms          switch       n= 3 p50=1420.0 p95=1492.0 max=1500.0" in text
    assert "  barge-ins=0 p50=None p95=None max=None mean=None overdue=0 all=[]" in text
    assert "  score ok=3 stt-noise=0 route-miss=1 stt-miss=0 missing=0 extra=0 stray=0 unplayed=0 (aligned by time)" in text
    assert "   1 ok         expected=devil active=angel route=devil p=0.97 heard: Devil, should I eat the whole cake tonight?" in text
    assert "   2 ok         expected=none  active=devil route=stay p=0.94 heard: And what would you put on top of it?" in text
    assert ("   4 route-miss expected=none  active=angel route=devil p=0.88 "
            "heard: Speak of the devil, I just found another cake in the fridge.") in text
    assert "f00dfeed" not in "\n".join(text)


def test_a_missing_and_a_misheard_phrase_are_rendered_with_what_was_said():
    lines = [entry.replace("Can I hear from the angel now?", "Can I hear from the Angela now?")
             for entry in RUN if "And what would you put" not in entry]
    text = journal.render("x", journal.report(lines, MARKS, PLAYED))
    assert "   2 missing    expected=none  said: And what would you put on top of it?" in text
    index = text.index("   3 stt-noise  expected=angel active=devil route=angel p=0.99 heard: Can I hear from the Angela now?")
    assert text[index + 1] == "                said: Can I hear from the angel now?"
    # The second phrase's route is still in the log, and now belongs to no turn.
    assert "  routes without a transcript=1" in text
    assert not any("routes without" in entry for entry in journal.render("x", journal.report(RUN, MARKS, PLAYED)))


def test_a_second_turn_in_a_phrase_window_is_flagged_on_its_row():
    lines = RUN[:14] + [
        line("20.000", "user_stopped", "Transcript: user: Tonight."),
        line("20.210", "event", "DUO route {'token': 'd', 'target': None, 'reason': 'stay', 'p': 0.9, 'elapsed_ms': 209.0}"),
    ] + RUN[14:]
    text = journal.render("x", journal.report(lines, MARKS, PLAYED))
    assert ("   1 ok         expected=devil active=angel route=devil p=0.97 "
            "heard: Devil, should I eat the whole cake tonight? (+1 more in its window)") in text
    assert "  score ok=3 stt-noise=0 route-miss=1 stt-miss=0 missing=0 extra=1 stray=0 unplayed=0 (aligned by time)" in text


def test_without_marks_the_report_is_the_turns_and_the_latency():
    result = journal.report(RUN)
    assert "rows" not in result and [turn["transcript"] for turn in result["heard"]] == [mark["text"] for mark in MARKS]
    assert "score" not in "\n".join(journal.render("window", result))


def run_directory(root, **files):
    root.mkdir()
    for name, content in files.items():
        (root / name.replace("_", ".")).write_text(content if isinstance(content, str) else json.dumps(content))
    return root


def test_main_reads_a_run_directory_and_saved_lines_and_writes_the_report(tmp_path, capsys):
    run = run_directory(tmp_path / "a1-functional", marks_json=dict(cohort="functional", marks=MARKS), played_json=PLAYED)
    saved = tmp_path / "bot.log"
    saved.write_text("\n".join(RUN) + "\n")
    journal.main(["--lines", str(saved), "run-unify-bot.sh", str(run)])
    shown = capsys.readouterr().out
    assert shown.startswith("a1-functional turns=4 routed=4 replies=4 ")
    assert "score ok=3 stt-noise=0 route-miss=1 stt-miss=0 missing=0" in shown
    written = json.loads((run / "report.json").read_text())
    assert (written["tag"], written["score"]["ok"], written["rows"][3]["result"]) == ("run-unify-bot.sh", 3, "route-miss")
    assert "f00dfeed" not in (run / "report.json").read_text()


def test_main_takes_the_window_from_the_run_directory_and_the_lines_from_the_journal(tmp_path, capsys, monkeypatch):
    first = run_directory(tmp_path / "a1-functional", start="2026-11-10 01:00:00\n", end="2026-11-10 01:02:26\n",
                          marks_json=dict(marks=MARKS), played_json=PLAYED)
    second = run_directory(tmp_path / "a1-empty", start="2026-11-10 01:03:00\n", end="2026-11-10 01:04:00\n",
                           marks_json=dict(marks=MARKS[:2]))
    asked = []

    def journalctl(command, **options):  # The second window has no bot lines, only a notice that is not one.
        asked.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="\n".join(RUN) + "\n" if len(asked) == 1 else "-- No entries --\n")

    monkeypatch.setattr(journal.subprocess, "run", journalctl)
    journal.main(["run-unify-bot.sh", str(first), str(second)])
    assert asked == [
        ["journalctl", "--user", "--since", "2026-11-10 01:00:00", "--until", "2026-11-10 01:02:26", "-o", "cat", "-t", "run-unify-bot.sh"],
        ["journalctl", "--user", "--since", "2026-11-10 01:03:00", "--until", "2026-11-10 01:04:00", "-o", "cat", "-t", "run-unify-bot.sh"]]
    shown = capsys.readouterr().out.splitlines()
    assert "  score ok=0 stt-noise=0 route-miss=0 stt-miss=0 missing=2 extra=0 stray=0 unplayed=0 (aligned by order)" in shown
    assert "  no bot lines: is run-unify-bot.sh the bot's journal identifier, and is the window right?" in shown
    assert shown[-1] == "total ok=3 stt-noise=0 route-miss=1 stt-miss=0 missing=2"
    assert json.loads((first / "report.json").read_text())["start"] == "2026-11-10 01:00:00"


def test_an_s3_run_starts_with_the_character_its_first_status_recorded(tmp_path, capsys):
    # The board joined the session before the window: no hello in it, and the devil was already active.
    run = run_directory(
        tmp_path / "s3-switch", played_json=[dict(t=T0 + 5.0, wav_s=9.5, len_s=3.0)],
        marks_json=dict(marks=[dict(start_s=10.0, end_s=12.0, text="Devil, should I call my sister?", expected="devil")]),
        status_json=[dict(tag="start", t=T0 + 1.0, status=dict(character="devil", rev=6)),
                     dict(tag="after-seg0", t=T0 + 17.0, status=dict(character="devil", rev=6))])
    saved = tmp_path / "bot.log"
    saved.write_text("\n".join([
        line("08.300", "user_stopped", "Transcript: user: Devil, should I call my sister?"),
        line("08.520", "event", "DUO route {'token': 'c', 'target': None, 'reason': 'stay', 'p': 0.95, 'elapsed_ms': 219.0}"),
    ]) + "\n")
    journal.main(["--lines", str(saved), "run-unify-bot.sh", str(run)])
    assert "   1 ok         expected=devil active=devil route=stay p=0.95 heard: Devil, should I call my sister?" in capsys.readouterr().out
    journal.main(["--lines", str(saved), "--start-character", "angel", "run-unify-bot.sh", str(run)])
    assert "   1 route-miss expected=devil active=angel route=stay p=0.95" in capsys.readouterr().out


def test_main_refuses_a_run_directory_inside_the_repository(tmp_path):
    with pytest.raises(ValueError, match="inside the repository"):
        journal.main(["--lines", str(tmp_path / "none"), "tag", str(journal.Path(__file__).parent)])
