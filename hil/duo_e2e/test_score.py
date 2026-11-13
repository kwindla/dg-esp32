"""Offline: every class the scorer gives, the active-character rule, and both alignments."""
import score

MARKS = [
    dict(start_s=10.0, end_s=12.5, text="Devil, should I eat the whole cake tonight?", expected="devil"),
    dict(start_s=34.5, end_s=36.5, text="And what would you put on top of it?", expected="none"),
    dict(start_s=58.5, end_s=60.5, text="Can I hear from the angel now?", expected="angel"),
    dict(start_s=82.5, end_s=86.0, text="Speak of the devil, I just found another cake in the fridge.", expected="none"),
]
WEB = [dict(t=1000.0, wav_s=0, len_s=145)]  # One span: the WAV began at wall-clock 1000 and ran 145 s.


def turn(t, transcript, active, target=None, reason="stay", p=0.9, did=None):
    did = did or ("switched" if target else "stayed")
    return dict(t=t, transcript=transcript, active=active, routed=True, target=target, reason=reason, p=p,
                did=did, switched=target if did == "switched" else None)


def results(rows):
    return [row["result"] for row in rows]


def test_a_clean_run_is_all_ok_and_each_row_says_what_was_decided():
    rows, stray = score.score(MARKS, [
        turn(1013.1, "Devil, should I eat the whole cake tonight?", "angel", "devil", "switch", 0.97),
        turn(1037.0, "And what would you put on top of it?", "devil"),
        turn(1061.2, "Can I hear from the angel now?", "devil", "angel", "switch", 0.99),
        turn(1086.9, "Speak of the devil, I just found another cake in the fridge.", "angel"),
    ], WEB)
    assert results(rows) == ["ok", "ok", "ok", "ok"] and stray == []
    assert rows[0] == dict(phrase=1, text="Devil, should I eat the whole cake tonight?", expected="devil", result="ok",
                           transcript="Devil, should I eat the whole cake tonight?", verbatim=True, active="angel",
                           wanted="devil", target="devil", reason="switch", p=0.97, did="switched",
                           switched="devil", extra=0)
    assert score.summary(rows) == {"ok": 4, "stt-noise": 0, "route-miss": 0, "stt-miss": 0, "missing": 0}


def test_a_proposed_switch_that_did_not_happen_is_scored_as_no_switch():
    # The claim lost to the deadline, or the switch was taken back: the character did not change.
    rows, _ = score.score(MARKS, [
        turn(1013.1, "Devil, should I eat the whole cake tonight?", "angel", "devil", "vocative-rule", 0.7, "claim-lost"),
        turn(1037.0, "And what would you put on top of it?", "angel", "devil", "switch", 0.9, "claim-lost"),
        turn(1061.2, "Can I hear from the angel now?", "devil", "angel", "switch", 0.99, "reverted"),
    ], WEB)
    # 1: should have switched and did not. 2: a wrong proposal that changed nothing. 3: switched back.
    assert results(rows) == ["route-miss", "ok", "route-miss", "missing"]
    assert [(row["target"], row["did"], row["switched"]) for row in rows[:3]] == [
        ("devil", "claim-lost", None), ("devil", "claim-lost", None), ("angel", "reverted", None)]


def test_a_wrong_route_on_a_faithful_transcript_is_a_route_miss():
    rows, _ = score.score(MARKS, [
        turn(1013.1, "Devil, should I eat the whole cake tonight?", "angel", None, "below-floor", 0.71),  # Stayed.
        turn(1086.9, "Speak of the devil, I just found another cake in the fridge.", "angel", "devil", "switch", 0.88),
    ], WEB)
    assert results(rows) == ["route-miss", "missing", "missing", "route-miss"]
    assert (rows[0]["wanted"], rows[0]["target"], rows[0]["reason"]) == ("devil", None, "below-floor")
    assert (rows[3]["wanted"], rows[3]["target"]) == (None, "devil")
    assert rows[1] == dict(phrase=2, text="And what would you put on top of it?", expected="none", result="missing")


def test_a_mangled_name_makes_the_miss_an_stt_miss_and_a_right_route_stt_noise():
    marks = [dict(start_s=10.0, end_s=12.0, text="Angel, is it okay to skip the gym today?", expected="angel"),
             dict(start_s=34.0, end_s=36.0, text="Devil, should I call my sister?", expected="devil"),
             dict(start_s=58.0, end_s=60.0, text="What should I have for breakfast?", expected="none")]
    rows, _ = score.score(marks, [
        turn(1012.6, "Bangel, is it okay to skip the gym today?", "devil"),                       # Name lost, stayed.
        turn(1036.5, "Double, should I call my sister?", "devil"),                                # Name lost; devil is active anyway.
        turn(1060.4, "Angel should I have for breakfast?", "devil", "angel", "switch", 0.93),     # A name nobody said.
    ], WEB)
    assert results(rows) == ["stt-miss", "stt-noise", "stt-miss"]
    assert [row["verbatim"] for row in rows] == [False, False, False]
    rows, _ = score.score(marks[:1], [turn(1012.6, "Dangel, is it okay to skip the gym today?", "devil", "angel", "switch", 0.9)], WEB)
    assert results(rows) == ["stt-noise"]  # The router coped with the mishearing.


def test_other_word_errors_do_not_change_the_class_but_show_as_not_verbatim():
    rows, _ = score.score(MARKS[:1], [turn(1013.1, "Devil, should I eat the hole cake to night?", "angel", "devil")], WEB)
    assert (rows[0]["result"], rows[0]["verbatim"]) == ("ok", False)
    rows, _ = score.score(MARKS[:1], [turn(1013.1, "devil should I eat the whole cake tonight", "angel", "devil")], WEB)
    assert (rows[0]["result"], rows[0]["verbatim"]) == ("ok", True)


def test_addressing_the_active_character_means_stay():
    assert score.wanted("angel", "devil") == "angel"
    assert score.wanted("angel", "angel") is None
    assert score.wanted("none", "devil") is None
    marks = [dict(start_s=10.0, end_s=12.0, text="Angel, should I tidy my desk?", expected="angel")]
    stayed = [turn(1012.6, "Angel, should I tidy my desk?", "angel")]
    switched = [turn(1012.6, "Angel, should I tidy my desk?", "angel", "devil", "switch", 0.9)]
    assert results(score.score(marks, stayed, WEB)[0]) == ["ok"]
    assert results(score.score(marks, switched, WEB)[0]) == ["route-miss"]
    # The same turn with the devil active is a switch the bot did not make.
    assert results(score.score(marks, [dict(stayed[0], active="devil")], WEB)[0]) == ["route-miss"]


def test_time_alignment_keeps_later_phrases_paired_when_a_turn_is_missing():
    heard = [turn(1013.1, "Devil, should I eat the whole cake tonight?", "angel", "devil"),
             turn(1061.2, "Can I hear from the angel now?", "devil", "angel"),
             turn(1086.9, "Speak of the devil, I just found another cake in the fridge.", "angel")]
    assert results(score.score(MARKS, heard, WEB)[0]) == ["ok", "missing", "ok", "ok"]
    # By order alone the third phrase's turn stands in for the second, and every later pair is off by one.
    rows, stray = score.score(MARKS, heard)
    assert results(rows) == ["ok", "stt-miss", "stt-noise", "missing"] and stray == []


def test_extra_turns_in_a_window_are_counted_and_turns_before_the_first_phrase_are_stray():
    early = turn(1004.0, "Hello?", "angel")
    rows, stray = score.score(MARKS[:2], [
        early,
        turn(1013.1, "Devil, should I eat the whole cake tonight?", "angel", "devil"),
        turn(1020.0, "Tonight.", "devil"),                                    # Echo, or a split turn.
        turn(1037.0, "And what would you put on top of it?", "devil"),
    ], WEB)
    assert results(rows) == ["ok", "ok"] and [row["extra"] for row in rows] == [1, 0]
    assert stray == [early]


def test_only_phrases_a_span_played_whole_are_scored():
    rows, _ = score.score(MARKS, [], [dict(t=1000.0, wav_s=0, len_s=60)])  # The run stopped before phrase 3 ended.
    assert [row["phrase"] for row in rows] == [1, 2] and results(rows) == ["missing", "missing"]
    assert score.windows(MARKS, [dict(t=1000.0, wav_s=0, len_s=145)]) == [(0, 1010.0), (1, 1034.5), (2, 1058.5), (3, 1082.5)]


def test_fixture_segments_place_each_phrase_at_its_own_wall_clock_time():
    # An S3 run: phrase 3 was played first, at 5000, from 0.5 s before its start; then phrase 1 at 5100.
    played = [dict(t=5000.0, wav_s=58.0, len_s=3.0), dict(t=5100.0, wav_s=9.5, len_s=3.5)]
    assert score.windows(MARKS, played) == [(2, 5000.5), (0, 5100.5)]
    rows, stray = score.score(MARKS, [
        turn(5003.4, "Can I hear from the angel now?", "devil", "angel"),
        turn(5104.0, "Devil, should I eat the whole cake tonight?", "angel", "devil"),
    ], played)
    assert [(row["phrase"], row["result"]) for row in rows] == [(3, "ok"), (1, "ok")] and stray == []
