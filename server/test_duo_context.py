"""DUO_CONTEXT=b2h: one prompt, a marker on each user message, replies stored as spoken.

The marker helpers and the stream filter are tested alone. Everything else
runs through the real 1.16 worker of test_duo_pipeline, whose own tests pin
the labelled context: stay, tap, auto-switch, rollback, revert and interrupted
turns, what reaches the voices, the device and the logs, what Jev is asked,
and that each request repeats the one before it byte for byte.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import random
import re

from loguru import logger
import pytest
from pipecat.frames.frames import (
    InterruptionWorkerFrame, LLMTextFrame, LLMUpdateSettingsFrame, ManuallySwitchServiceFrame,
    ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.tts_service import TextAggregationMode

import duo_bot
from duo_bot import CharacterContext, SilentNote, configuration, identity, make_llm, make_tts, spoken_text
from duo_context import MARK, TaggedContext, TaggedLLM, Unmarked, stamp
from duo_prompts import TAGS, character_instructions, duo_instructions, tagged, untagged
from duo_route import RouteStampingLLM
from test_duo_first_clause import by_word, provider, stream
from test_duo_pipeline import TEST_LLM_URL, FakeAddressee, FakeLLM, Harness, auto_open_turn, pipeline, until

B2H_PROMPT_SHA256 = "74f388caef7befcacf178c52891e45bee356990e14407d34fc2dffe353cc4f65"
# What make_tts gives the voices with DUO_CONTEXT=b2h and the default DUO_TTS_FIRST_CLAUSE=1.
VOICES = dict(text_aggregation_mode=TextAggregationMode.SENTENCE, text_transforms=[], first_clause=True)
LABELLED_VOICES = dict(VOICES, text_transforms=[("*", spoken_text)])  # And with DUO_CONTEXT=labels.
ANY_MARK = re.compile(r"\[\s*(?:angel|devil)\s*\]", re.IGNORECASE)
REPLY = "A small playful reply. A final sentence."


def b2h(**kwargs):
    return pipeline(config={"context": "b2h"} | kwargs.pop("config", {}), **kwargs)


@pytest.fixture(params=[False, True], ids=["routing-off", "routing-on"])
def routing(request):
    return request.param


def stored(h):
    return h.control.context.get_messages()


def users(h):
    return [m["content"] for m in stored(h) if m["role"] == "user"]


def replies(h):
    return [m["content"] for m in stored(h) if m["role"] == "assistant"]


def said(h, character):
    return [text for text, _ in h.services[character].speaks]


async def settled(h, count=1):
    await until(lambda: len(replies(h)) >= count)
    await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
    assert await h.worker.flush_pipeline(timeout=.5)


def record_requests(h, requests=None):
    """Every request as the service would send it: the messages array of the chat completion.

    Wraps the fake completion as it is now; call it again, with the same list, after replacing that.
    """
    requests = [] if requests is None else requests
    process = h.llm._process_context

    async def recorded(context):
        params = await h.llm.get_llm_adapter().get_llm_invocation_params(
            context, system_instruction=h.llm.settings.system_instruction,
            convert_developer_to_user=not h.llm.supports_developer_role)
        requests.append(deepcopy(h.llm.build_chat_completion_params(params)["messages"]))
        await process(context)

    h.llm._process_context = recorded
    return requests


def wire_text(h, kind):
    return [m["data"]["text"] for m in h.transport.client.messages if m.get("type") == kind]


# The prompt and the marker.

def test_the_prompt_is_the_evaluated_b2h_text():
    prompt = duo_instructions()
    assert hashlib.sha256(prompt.encode()).hexdigest() == B2H_PROMPT_SHA256
    # The user's decision: the revised harm sentence, and not a word beyond it.
    assert ("The Devil never goes along with hurting anyone, damaging or taking anything, breaking the law or "
            "anything unsafe: when the human wants that, the Devil laughs it off, says no, and tempts them with "
            "something harmless instead. ") in prompt
    assert "Every message from the human ends with a marker, [ANGEL] or [DEVIL]. " in prompt
    assert "Silent context" not in prompt and "[Angel]" not in prompt and "history labels" not in prompt
    # The labelled prompts are not derived from it and did not move.
    assert "Silent context" in character_instructions("angel")


def test_a_user_message_carries_exactly_one_marker_at_its_end():
    assert tagged("Should I go?", "angel") == "Should I go? [ANGEL]"
    assert tagged("Should I go? [ANGEL]", "devil") == "Should I go? [DEVIL]"
    assert tagged(tagged(tagged("Hm", "devil"), "angel"), "devil") == "Hm [DEVIL]"
    assert untagged("Should I go? [DEVIL]") == "Should I go?" == untagged("Should I go?")
    # Only an ending is a marker; the words are the user's.
    assert untagged("[ANGEL] is what I call her") == "[ANGEL] is what I call her"
    assert set(TAGS) == {"angel", "devil"}


def test_stamp_writes_the_newest_user_message_in_place_and_nothing_else():
    first, reply, newest = (dict(role="user", content="One [ANGEL]"), dict(role="assistant", content="Said."),
                            dict(role="user", content="Two [ANGEL]"))
    messages = [first, reply, newest, dict(role="assistant", content="A late partial")]
    assert stamp(messages, "devil") is True and messages[2] is newest
    assert [m["content"] for m in messages] == ["One [ANGEL]", "Said.", "Two [DEVIL]", "A late partial"]
    assert stamp(messages, "devil") is False
    assert stamp([reply], "devil") is False and stamp([], "angel") is False
    parts = [dict(role="user", content=[dict(type="text", text="not a string")])]
    assert stamp(parts, "devil") is False and parts[0]["content"][0]["text"] == "not a string"


# The stream filter.

def unmarked(chunks):
    filter_ = Unmarked()
    return "".join(filter_.feed(chunk) for chunk in chunks) + filter_.finish()


@pytest.mark.parametrize("chunks, expected", [
    (["[ANGEL] Go on."], "Go on."),
    (["[Devil] Go on. [DEVIL]"], "Go on. "),
    (["Sure, [angel] go on."], "Sure, go on."),
    (["[", "AN", "GEL", "]", " Go", " on."], "Go on."),
    (["Sure,", " [Dev", "il]", " go on."], "Sure, go on."),
    (["Sure, [Devil]", "   ", "\n", "go on."], "Sure, go on."),
    (["[Angel][Devil] [ANGEL]Go."], "Go."),
    # Not markers: the text is the model's and goes out whole.
    (["An [angle] and a [devilish] one."], "An [angle] and a [devilish] one."),
    (["Brackets [", "ang", "ry] stay."], "Brackets [angry] stay."),
    (["It ends on [ang"], "It ends on [ang"),
    (["It ends on ["], "It ends on ["),
    (["[[ANGEL]]"], "[]"),
])
def test_unmarked_removes_markers_and_labels_whole_or_split(chunks, expected):
    assert unmarked(chunks) == expected


def test_unmarked_holds_back_only_what_may_become_a_marker():
    filter_ = Unmarked()
    assert filter_.feed("[Angel] Well. [DEV") == "Well. " and filter_.removed == 1
    assert filter_.feed("IL] ") == "" and filter_.removed == 2 and filter_.kept == ""
    assert filter_.feed("Honestly, [") == "Honestly, " and filter_.kept == "["
    assert filter_.feed("dev") == "" and filter_.kept == "[dev"
    assert filter_.feed("otion] yes") == "[devotion] yes" and filter_.kept == ""
    assert filter_.feed("no bracket at all") == "no bracket at all"


def test_unmarked_gives_the_same_text_however_the_reply_is_cut():
    random.seed(12)
    texts = ["[Angel] Honestly, I would say yes, but only once. [DEVIL] Then stop. [ANGEL]",
             "No marker in here, only [brackets] and [ANGELS].", "[DEVIL]\n\n[devil]  Twice.  [Angel]\tAnd a tab.",
             "[ANG [ANGEL] [DE [DEVIL]] tail ["]
    for text in texts:
        whole = unmarked([text])
        assert not ANY_MARK.search(whole)
        for _ in range(200):
            cuts = sorted(random.sample(range(1, len(text)), random.randint(1, min(12, len(text) - 1))))
            chunks = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
            assert unmarked(chunks) == whole, chunks
        assert unmarked(list(text)) == whole


# Configuration and construction.

def test_duo_context_defaults_to_b2h_and_is_part_of_the_identity():
    assert configuration({})["context"] == "b2h"
    assert configuration({"DUO_CONTEXT": "labels"})["context"] == "labels"
    for value in ("", "B2H", "notes", "1"):
        with pytest.raises(ValueError, match="DUO_CONTEXT"):
            configuration({"DUO_CONTEXT": value})
    one, other = identity(configuration({})), identity(configuration({"DUO_CONTEXT": "labels"}))
    assert one["config"]["context"] == "b2h" and other["config"]["context"] == "labels"
    assert one["config_sha256"] != other["config_sha256"]
    assert "duo_context.py" in one["code_sha256"]


@pytest.mark.parametrize("routed", [False, True])
def test_the_b2h_llm_has_the_one_prompt_and_writes_and_removes_markers(routed):
    config = configuration({"DUO_LLM_URL": TEST_LLM_URL, "DUO_AUTO_SWITCH": "1" if routed else "0"})
    llm = make_llm(config, service_class=FakeLLM)
    assert llm.settings.system_instruction == duo_instructions()
    assert isinstance(llm, TaggedLLM) and isinstance(llm, RouteStampingLLM) is routed
    if routed:
        # The route stamp is outermost: text the marker filter releases late is stamped like the rest.
        order = type(llm).__mro__
        assert order.index(RouteStampingLLM) < order.index(TaggedLLM) < order.index(FakeLLM)


@pytest.mark.parametrize("routed", [False, True])
async def test_labels_builds_what_eebf273_built(routed, monkeypatch):
    config = configuration({"DUO_LLM_URL": TEST_LLM_URL, "DUO_CONTEXT": "labels",
                            "DUO_AUTO_SWITCH": "1" if routed else "0"})
    llm = make_llm(config, service_class=FakeLLM)
    assert llm.settings.system_instruction == character_instructions("angel")
    assert not isinstance(llm, TaggedLLM)
    assert type(llm).__name__ == ("RoutedFakeLLM" if routed else "FakeLLM")
    assert [base.__name__ for base in type(llm).__mro__[:2]] == (
        ["RoutedFakeLLM", "RouteStampingLLM"] if routed else ["FakeLLM", "OpenAILLMService"])
    monkeypatch.setenv("DEEPGRAM_API_KEY", "offline-placeholder")
    assert all(voice._text_transforms == [("*", spoken_text)] for voice in make_tts(config).values())
    h = Harness(config=config, auto_switch=routed)
    assert type(h.control.context) is CharacterContext and h.control.tags is False


async def test_b2h_voices_have_no_transform_and_the_context_and_llm_share_the_switch_state(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "offline-placeholder")
    config = configuration({"DUO_LLM_URL": TEST_LLM_URL})
    voices = make_tts(config)
    assert all(voice._text_transforms == [] for voice in voices.values())
    assert all(type(voice._text_aggregator) is duo_bot.FirstClause for voice in voices.values())
    h = Harness(config={"context": "b2h"})
    assert type(h.control.context) is TaggedContext and h.control.tags is True
    assert h.control.context.state is h.control.state is h.llm.duo_state
    assert h.llm.duo_context is h.control.context
    # An empty turn that interrupts a reply is answered through the human's own turn, not Pipecat's note.
    recovery = h.control.user_aggregator._params.empty_user_turn
    assert (recovery.interrupted_prompt, recovery.idle_prompt, recovery.max_consecutive_recoveries) == (
        "(inaudible)", None, 1)
    with pytest.raises(ValueError, match="marker-writing"):
        duo_bot.build_worker(h.transport, h.stt, make_llm(config | {"context": "labels"}, service_class=FakeLLM),
                             h.services, config=h.config)


# Turns: what is stored, what the model is sent.

async def test_stay_turns_are_tagged_for_the_speaker_and_replies_are_stored_as_spoken(routing):
    async with b2h(auto_switch=routing) as h:
        requests = record_requests(h)
        await h.hello()
        assert stored(h) == []  # The hello's transaction wrote nothing: no note.
        await h.turn("What should I do?")
        await settled(h)
        await h.turn("And what about tomorrow?")
        await settled(h, 2)
        assert stored(h) == [dict(role="user", content="What should I do? [ANGEL]"),
                             dict(role="assistant", content=REPLY),
                             dict(role="user", content="And what about tomorrow? [ANGEL]"),
                             dict(role="assistant", content=REPLY)]
        prompt = dict(role="system", content=duo_instructions())
        assert requests == [[prompt] + stored(h)[:1], [prompt] + stored(h)[:3]]
        assert said(h, "angel") == ["A small playful reply. ", "A final sentence. "] * 2 and not said(h, "devil")
        assert h.llm.updates == [] and h.llm.settings.system_instruction == duo_instructions()


async def test_a_tap_changes_the_next_marker_and_nothing_else(routing):
    async with b2h(auto_switch=routing) as h:
        requests = record_requests(h)
        queued = []
        queue = h.worker.queue_frame

        async def watched(frame, *args, **kwargs):
            queued.append(type(frame))
            await queue(frame, *args, **kwargs)

        h.worker.queue_frame = watched
        await h.hello()
        await h.turn("What should I do?")
        await settled(h)
        before = deepcopy(stored(h))
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        assert stored(h) == before  # The switch itself leaves the conversation alone.
        await h.turn("And you?")
        await settled(h, 2)
        assert stored(h) == before + [dict(role="user", content="And you? [DEVIL]"),
                                      dict(role="assistant", content=REPLY)]
        assert requests[1][:3] == requests[0] + [dict(role="assistant", content=REPLY)]
        assert said(h, "devil") == ["A small playful reply. ", "A final sentence. "]
        assert [h.control.context.speaker(m) for m in stored(h) if m["role"] == "assistant"] == ["angel", "devil"]
        await h.send()
        assert (await h.outcome())["character"] == "angel"
        await h.turn("Back again?")
        await settled(h, 3)
        assert users(h) == ["What should I do? [ANGEL]", "And you? [DEVIL]", "Back again? [ANGEL]"]
        # No prompt swap and no note was ever queued; the voice still changed hands, verified.
        assert LLMUpdateSettingsFrame not in queued and SilentNote not in queued and h.llm.updates == []
        assert queued.count(ManuallySwitchServiceFrame) == 2
        assert [data["character"] for kind, data, _ in h.events if kind == "verified"] == ["angel", "devil", "angel"]
        assert all(request[0] == dict(role="system", content=duo_instructions()) for request in requests)


async def test_the_one_prompt_is_read_back_by_every_switch(routing):
    # With labels a switch sets the prompt and reads it back. Here it must find the session's prompt untouched.
    async with b2h(auto_switch=routing) as h:
        await h.hello()
        h.llm._settings.system_instruction = character_instructions("devil")
        await h.send()
        await until(lambda: h.control.stopping)
        await until(h.worker.has_finished)
        # Neither the switch nor its rollback can verify: the session ends, and nothing is published.
        assert h.control.gate.closed and h.control.state.character == "angel"
        assert not any(state["seq"] == 2 for state in h.states())
        assert [kind for kind, _, _ in h.events if kind == "rollback-failed"] == ["rollback-failed"]


async def test_a_hello_that_adopts_the_devil_tags_the_first_turn_for_him(routing):
    async with b2h(auto_switch=routing) as h:
        assert (await h.hello("devil"))["character"] == "devil"
        await h.turn()
        await settled(h)
        assert users(h) == ["What should I do? [DEVIL]"] and said(h, "devil") and not said(h, "angel")


async def test_a_turn_held_through_a_tap_is_answered_and_stored_under_the_new_marker(routing):
    async with b2h(auto_switch=routing) as h:
        requests = record_requests(h)
        await h.hello()
        await auto_open_turn(h, "An open turn")
        h.stt.flush_delay = .10
        await h.send()
        await until(lambda: h.control.gate.closed)
        await h.control.user_aggregator.push_aggregation()
        await until(lambda: h.control.gate.fence.held)
        # Stored at once, for the character of that moment; nothing has been sent.
        assert users(h) == ["An open turn [ANGEL]"] and not requests
        assert (await h.outcome())["character"] == "devil"
        await settled(h)
        assert users(h) == ["An open turn [DEVIL]"] and replies(h) == [REPLY]
        assert [request[-1]["content"] for request in requests] == ["An open turn [DEVIL]"]
        assert said(h, "devil") and not said(h, "angel")


async def test_an_auto_switch_reruns_the_turn_under_the_other_marker_and_stores_it_once():
    fake = FakeAddressee("devil", "either", delay=.04)
    async with b2h(auto_switch=True, addressee=fake) as h:
        requests = record_requests(h)
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        await h.turn("Devil, should I eat cake?")
        await settled(h)
        # The draft was asked as the Angel and withheld; the same message was then asked as the Devil.
        assert [request[-1]["content"] for request in requests] == ["Devil, should I eat cake? [ANGEL]",
                                                                    "Devil, should I eat cake? [DEVIL]"]
        assert requests[0][:-1] == requests[1][:-1] == [dict(role="system", content=duo_instructions())]
        assert stored(h) == [dict(role="user", content="Devil, should I eat cake? [DEVIL]"),
                             dict(role="assistant", content=REPLY)]
        assert not h.services["angel"].starts and not said(h, "angel")
        assert h.services["devil"].text_responses == [2, 2]
        assert [s["character"] for s in h.states() if s.get("reason") == "addressed"] == ["devil"]
        assert h.control.context.speaker(stored(h)[1]) == "devil"
        await h.turn("And then?")
        await settled(h, 2)
        assert users(h)[1] == "And then? [DEVIL]" and requests[2][:3] == requests[1] + [stored(h)[1]]
        assert h.llm.updates == []


async def test_an_auto_switch_that_fails_before_commit_reruns_the_turn_under_the_old_marker():
    fake = FakeAddressee("angel", delay=.04)
    async with b2h(auto_switch=True, addressee=fake) as h:
        requests = record_requests(h)
        await h.hello("devil")
        h.stt.sabotage = ManuallySwitchServiceFrame  # The voice switch is lost: the mutation cannot verify.
        await h.turn("Angel, answer")
        await settled(h)
        assert [kind for kind, _, _ in h.events if kind == "route-rollback"] == ["route-rollback"]
        assert [request[-1]["content"] for request in requests] == ["Angel, answer [DEVIL]"] * 2
        assert users(h) == ["Angel, answer [DEVIL]"] and replies(h) == [REPLY]
        assert h.services["devil"].text_responses == [2, 2] and not h.services["angel"].starts
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_a_reverted_tap_leaves_the_failed_turn_as_asked_and_tags_the_next_for_the_old_character(routing):
    async with b2h(fault="tts-fail-next", auto_switch=routing) as h:
        requests = record_requests(h)
        await h.hello()
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        h.llm.remainder.clear()
        await h.turn("What should I do?")
        await until(lambda: h.states()[-1].get("reason") == "reverted")
        await until(lambda: not h.control.gate.closed)
        h.llm.remainder.set()
        assert h.states()[-1]["character"] == "angel"
        await h.turn("After revert")
        await settled(h)
        # The Devil was asked and his voice failed. That message is history, written once.
        assert users(h) == ["What should I do? [DEVIL]", "After revert [ANGEL]"] and replies(h) == [REPLY]
        assert requests[-1][1:] == [dict(role="user", content="What should I do? [DEVIL]"),
                                    dict(role="user", content="After revert [ANGEL]")]
        assert said(h, "angel") and not said(h, "devil")


async def test_a_reverted_auto_switch_keeps_the_devils_marker_on_the_turn_he_was_asked():
    async with b2h(auto_switch=True, addressee=FakeAddressee("devil", "either", delay=.04)) as h:
        await h.hello()
        devil = h.services["devil"]
        devil.auto_complete = False
        await h.turn("Devil, go on")
        await until(lambda: devil.speaks)
        await devil._handle_message(dict(type="Error", code="offline", description="failed reply"))
        await until(lambda: any(s.get("reason") == "reverted" for s in h.states()))
        await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
        assert h.control.state.character == "angel"
        await h.turn("Never mind")
        await until(lambda: said(h, "angel"))
        assert users(h) == ["Devil, go on [DEVIL]", "Never mind [ANGEL]"]


INTERRUPTED = "A small playful reply. And a final sentence."


async def interrupted_reply(style, routing, *, tap):
    """A reply cut off after its first sentence was heard, by a barge-in or by a tap; then one more turn."""
    async with pipeline(auto_switch=routing, tts_kwargs=VOICES if style == "b2h" else LABELLED_VOICES,
                        config={"context": style}) as h:
        provider(h)
        await h.hello()
        stream(h, by_word(INTERRUPTED), pause_after=len(by_word("A small playful reply. And")))
        requests = record_requests(h) if style == "b2h" else None
        h.llm.remainder.clear()
        await h.turn("Tell me more")
        await until(lambda: said(h, "angel") and h.transport.client.audio)
        if tap:
            await h.send()
            assert (await h.outcome())["character"] == "devil"
        else:
            await h.worker.queue_frame(InterruptionWorkerFrame())
        await until(lambda: replies(h))
        stream(h, by_word(REPLY))
        if style == "b2h":
            record_requests(h, requests)
        h.llm.remainder.set()
        await h.turn("Something else")
        await settled(h, 2)
        speakers = ([h.control.context.speaker(m) for m in stored(h) if m["role"] == "assistant"]
                    if style == "b2h" else None)
        return deepcopy(stored(h)), requests, {name: said(h, name) for name in h.services}, speakers


@pytest.mark.parametrize("tap", [False, True], ids=["barge-in", "tap"])
async def test_an_interrupted_reply_is_stored_as_it_is_with_labels_minus_the_label(routing, tap):
    history, requests, spoke, speakers = await interrupted_reply("b2h", routing, tap=tap)
    labelled, _, labelled_spoke, _ = await interrupted_reply("labels", routing, tap=tap)
    second = "devil" if tap else "angel"
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in history if m["role"] == "user"] == [
        "Tell me more [ANGEL]", f"Something else {TAGS[second]}"]
    # What an interruption leaves of a reply is Pipecat's business and the same in both styles.
    partial = history[1]["content"]
    assert partial.startswith("A small playful reply.") and "final sentence" not in partial
    assert [m["content"] for m in labelled if m["role"] == "assistant"] == [
        "[Angel] " + partial, f"[{second.capitalize()}] " + REPLY]
    assert history[3]["content"] == REPLY and speakers == ["angel", second]
    assert spoke == labelled_spoke and spoke["angel"][0] == "A small playful reply. "
    assert requests[1][:3] == requests[0] + [dict(role="assistant", content=partial)]


async def test_a_turn_interrupted_before_any_speech_keeps_its_marker_and_the_next_gets_its_own(routing):
    async with b2h(auto_switch=routing) as h:
        requests = record_requests(h)
        await h.hello()
        h.llm.generate.clear()  # The request is out, the model has written nothing.
        await h.turn("First try")
        await until(lambda: len(requests) == 1)
        await h.worker.queue_frame(InterruptionWorkerFrame())
        await asyncio.sleep(.03)
        h.llm.generate.set()
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        await h.turn("Second try")
        await settled(h)
        # The unanswered message is not rewritten: what was sent stays what was sent.
        assert users(h) == ["First try [ANGEL]", "Second try [DEVIL]"] and replies(h) == [REPLY]
        assert requests[-1][1:] == [dict(role="user", content="First try [ANGEL]"),
                                    dict(role="user", content="Second try [DEVIL]")]
        assert said(h, "devil") and not said(h, "angel")


async def test_an_answered_turn_keeps_its_marker_when_a_request_comes_without_new_words(routing):
    # Nothing in the bot asks again without a new user message (an empty turn brings its own, below).
    # If something does: the answered message is history. With the same character it is sent as it is;
    # after a switch the request is withdrawn, because the new voice must not speak under the old marker.
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
    try:
        async with b2h(auto_switch=routing) as h:
            requests = record_requests(h)
            await h.hello()
            await h.turn("What should I do?")
            await settled(h)
            answered = deepcopy(stored(h))
            assert h.control.context.answered(stored(h)[0])
            await h.control.user_aggregator.push_context_frame()
            await settled(h, 2)
            assert stored(h)[:2] == answered and replies(h) == [REPLY, REPLY]
            assert requests[1] == requests[0] + [dict(role="assistant", content=REPLY)]
            before = deepcopy(stored(h))
            await h.send()
            assert (await h.outcome())["character"] == "devil"
            await h.control.user_aggregator.push_context_frame()
            await until(lambda: any(line.startswith("DUO request-withdrawn") for line in lines))
            assert await h.worker.flush_pipeline(timeout=.5)
            await asyncio.sleep(.05)
            # Not rewritten, not sent, not spoken.
            assert stored(h) == before and len(requests) == 2 and not said(h, "devil")
            await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
            await h.turn("And you?")
            await settled(h, 3)
            assert stored(h)[:len(before)] == before and users(h) == ["What should I do? [ANGEL]", "And you? [DEVIL]"]
            assert requests[2][:len(requests[1])] == requests[1] and said(h, "devil")
    finally:
        logger.remove(sink)
    assert [line for line in lines if line.startswith("DUO request-withdrawn")] == [
        'DUO request-withdrawn {"reason": "answered-turn", "character": "devil"}']


async def test_a_request_without_new_words_is_not_routed():
    # No words, nobody addressed: Jev is not asked, nothing is held, the committed character answers.
    # Jev would have named the Devil here, and a switch claimed on an answered turn has nothing to rerun.
    async with b2h(auto_switch=True, addressee=FakeAddressee("either", "devil", delay=.04)) as h:
        requests = record_requests(h)
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        await h.turn("What should I do?")
        await settled(h)
        before, routes = deepcopy(stored(h)), len(h.control.hold.routes)
        await h.control.user_aggregator.push_context_frame()
        await settled(h, 2)
        assert stored(h)[:2] == before and h.control.state.character == "angel" and not said(h, "devil")
        assert len(h.addressee.calls) == 1 and len(h.control.hold.routes) == routes
        assert [data for kind, data, _ in h.events if kind == "route-skipped"] == [dict(reason="answered-turn")]
        assert requests[1] == requests[0] + [dict(role="assistant", content=REPLY)]


async def forced_opposite_recovery(tap):
    """Sol's WP12R case: on the real recovery turn the classifier would name the other character, 0.99."""
    opposite = "angel" if tap else "devil"
    fake = FakeAddressee("either", opposite, delay=.02)
    async with b2h(auto_switch=True, addressee=fake, tts_kwargs=VOICES) as h:
        provider(h)
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        before, requests = await empty_turn(h, interrupting=True, tap=tap)
        await until(lambda: len(requests) == 2)
        await settled(h, 2)
        return h, fake, before, requests


@pytest.mark.parametrize("tap", [False, True], ids=["no-tap", "tap"])
async def test_an_inaudible_turn_is_not_routed_and_the_committed_character_answers(tap):
    speaker = "devil" if tap else "angel"
    h, fake, before, requests = await forced_opposite_recovery(tap)
    # Jev was asked about the first turn only. The recovery turn has no route to wait for.
    assert len(fake.calls) == 1 and fake.calls[0]["latest"]["said"] == "Tell me more"
    assert [data for kind, data, _ in h.events if kind == "route-skipped"] == [dict(reason="inaudible")]
    assert len([kind for kind, _, _ in h.events if kind == "route"]) == 1
    assert not any(s.get("reason") == "addressed" for s in h.states()) and h.control.state.character == speaker
    assert stored(h) == before + [dict(role="user", content=f"(inaudible) {TAGS[speaker]}"),
                                  dict(role="assistant", content=REPLY)]
    assert [h.control.context.speaker(m) for m in stored(h) if m["role"] == "assistant"] == ["angel", speaker]
    assert len(requests) == 2 and bool(said(h, "devil")) is tap


async def test_an_unrouted_inaudible_turn_does_not_wait_for_a_route_deadline():
    # A Jev that never answers and a 2 s budget: a routed turn would sit in the hold that long.
    never = asyncio.Event()

    async def silent(state):
        await never.wait()

    fake = FakeAddressee("either", silent)
    async with b2h(auto_switch=True, addressee=fake, tts_kwargs=VOICES, config={"route_budget_s": 2}) as h:
        provider(h)
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        before, requests = await empty_turn(h, interrupting=True, tap=False)
        waited = asyncio.get_running_loop().time()
        await until(lambda: len(replies(h)) == 2, timeout=1)
        assert asyncio.get_running_loop().time() - waited < 1 and len(fake.calls) == 1
        assert not h.control.hold.held and h.control.route_read is None


async def test_a_user_who_says_inaudible_is_an_ordinary_routed_turn():
    # Provenance, not text: these words came from the transcript.
    fake = FakeAddressee("devil", delay=.02)
    async with b2h(auto_switch=True, addressee=fake) as h:
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        await h.turn("(inaudible)")
        await settled(h)
        assert len(fake.calls) == 1 and fake.calls[0]["latest"]["said"] == "(inaudible)"
        assert users(h) == ["(inaudible) [DEVIL]"] and not h.control.context.unheard(stored(h)[0])
        assert not any(kind == "route-skipped" for kind, _, _ in h.events)


EMPTY_TURN_WAIT_S = 12  # Pipecat ends an open turn without words after its own stop timeout, about 5 s.


async def empty_turn(h, *, interrupting, tap):
    """A user turn with no words, through Pipecat's own turn lifecycle. Returns what was stored before it.

    interrupting: the turn starts while a reply is being spoken and cuts it off. Otherwise the reply
    has finished and the bot is idle. tap: a double tap arrives while the turn is open.
    """
    stopped = asyncio.Event()

    @h.control.user_aggregator.event_handler("on_user_turn_stopped")
    async def empty_turn_stopped(aggregator, strategy, message):
        if not message.content:
            stopped.set()

    stream(h, by_word(INTERRUPTED), pause_after=len(by_word("A small playful reply. And")) if interrupting else None)
    requests = record_requests(h)
    h.llm.remainder.clear()
    await h.turn("Tell me more")
    await until(lambda: said(h, "angel") and h.transport.client.audio)
    if not interrupting:
        h.llm.remainder.set()
        await settled(h)
    await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
    await until(lambda: replies(h))
    assert await h.worker.flush_pipeline(timeout=.5)
    before = deepcopy(stored(h))
    if tap:
        await h.send()
        assert (await h.outcome())["character"] == "devil"
    stream(h, by_word(REPLY))
    record_requests(h, requests)
    h.llm.remainder.set()
    await h.worker.queue_frame(ProposedUserStoppedSpeakingFrame())
    await asyncio.wait_for(stopped.wait(), EMPTY_TURN_WAIT_S)
    return before, requests


@pytest.mark.parametrize("tap", [False, True], ids=["no-tap", "tap"])
async def test_an_empty_turn_that_interrupts_a_reply_is_a_user_turn_of_its_own(routing, tap):
    # With labels Pipecat answers such a turn by adding a developer instruction. Here the same recovery
    # stores the human's turn: heard, not understood, and marked for whoever speaks now.
    speaker = "devil" if tap else "angel"
    async with b2h(auto_switch=routing, tts_kwargs=VOICES) as h:
        provider(h)
        await h.hello()
        before, requests = await empty_turn(h, interrupting=True, tap=tap)
        await until(lambda: len(requests) == 2)
        await settled(h, 2)
        partial = before[1]["content"]
        assert [m["role"] for m in before] == ["user", "assistant"] and partial.startswith("A small playful reply.")
        # Nothing that was there changed, and what was added is a user message and its reply. No note.
        assert stored(h) == before + [dict(role="user", content=f"(inaudible) {TAGS[speaker]}"),
                                      dict(role="assistant", content=REPLY)]
        assert requests[1] == requests[0] + [dict(role="assistant", content=partial),
                                             dict(role="user", content=f"(inaudible) {TAGS[speaker]}")]
        assert said(h, speaker)[-2:] == ["A small playful reply. ", "A final sentence. "]
        assert [h.control.context.speaker(m) for m in stored(h) if m["role"] == "assistant"] == ["angel", speaker]
        assert bool(said(h, "devil")) is tap
        if routing:
            # Jev is not asked about a turn without words. (With labels it is asked the last words again.)
            assert len(h.addressee.calls) == 1 and h.addressee.calls[0]["latest"]["said"] == "Tell me more"
        # The next turn is an ordinary one, and the conversation only grew.
        await h.turn("I said, go on")
        await settled(h, 3)
        assert users(h) == ["Tell me more [ANGEL]", f"(inaudible) {TAGS[speaker]}", f"I said, go on {TAGS[speaker]}"]
        assert requests[2][:len(requests[1])] == requests[1]
        assert "developer" not in json.dumps(requests) and not ANY_MARK.search(json.dumps(h.transport.client.messages))


@pytest.mark.parametrize("tap", [False, True], ids=["no-tap", "tap"])
async def test_an_empty_turn_after_a_finished_reply_is_left_unanswered_and_changes_nothing(routing, tap):
    # Pipecat's default for an empty turn while the bot is idle, in both styles: it may be noise.
    async with b2h(auto_switch=routing, tts_kwargs=VOICES) as h:
        provider(h)
        await h.hello()
        before, requests = await empty_turn(h, interrupting=False, tap=tap)
        assert await h.worker.flush_pipeline(timeout=.5)
        await asyncio.sleep(.1)
        assert stored(h) == before and len(requests) == 1 and not said(h, "devil")
        assert [m["role"] for m in before] == ["user", "assistant"] and before[1]["content"] == INTERRUPTED
        speaker = "devil" if tap else "angel"
        await h.turn("Still there?")
        await settled(h, 2)
        assert stored(h)[:2] == before and users(h) == ["Tell me more [ANGEL]", f"Still there? {TAGS[speaker]}"]
        assert requests[1] == requests[0] + [before[1], dict(role="user", content=f"Still there? {TAGS[speaker]}")]


async def test_labels_keeps_pipecats_own_answer_to_an_empty_turn():
    # The labelled context is as it was: the default recovery, with its developer instruction.
    from pipecat.turns.empty_user_turn import DEFAULT_EMPTY_USER_TURN_INTERRUPTED_PROMPT
    async with pipeline(config={"context": "labels"}, tts_kwargs=LABELLED_VOICES) as h:
        provider(h)
        await h.hello()
        stopped = asyncio.Event()

        @h.control.user_aggregator.event_handler("on_user_turn_stopped")
        async def empty_turn_stopped(aggregator, strategy, message):
            if not message.content:
                stopped.set()

        stream(h, by_word(INTERRUPTED), pause_after=len(by_word("A small playful reply. And")))
        h.llm.remainder.clear()
        await h.turn("Tell me more")
        await until(lambda: said(h, "angel") and h.transport.client.audio)
        await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
        await until(lambda: replies(h))
        stream(h, by_word(REPLY))
        h.llm.remainder.set()
        await h.worker.queue_frame(ProposedUserStoppedSpeakingFrame())
        await asyncio.wait_for(stopped.wait(), EMPTY_TURN_WAIT_S)
        await settled(h, 2)
        # (The first message is the hello's Silent context note.)
        assert [m["role"] for m in stored(h)] == ["system", "user", "assistant", "developer", "assistant"]
        assert stored(h)[3]["content"] == DEFAULT_EMPTY_USER_TURN_INTERRUPTED_PROMPT


def test_only_the_inaudible_turn_is_taken_from_a_developer_message():
    context = TaggedContext()
    context.state = type("State", (), {"character": "devil"})()
    context.add_message(dict(role="developer", content="(inaudible)"))
    context.add_message(dict(role="developer", content="Something else"))
    context.add_message(dict(role="user", content="(inaudible)"))  # Said aloud, and transcribed.
    assert context.get_messages() == [dict(role="user", content="(inaudible) [DEVIL]"),
                                      dict(role="developer", content="Something else"),
                                      dict(role="user", content="(inaudible) [DEVIL]")]
    assert [context.unheard(m) for m in context.get_messages()] == [True, False, False]


def test_a_user_message_is_pending_until_a_reply_to_it_is_stored_whatever_the_order():
    context = TaggedContext()
    context.state = type("State", (), {"character": "angel"})()
    context.add_message(dict(role="user", content="First"))
    first = context.get_messages()[0]
    context.add_message(dict(role="user", content="Second"))
    second = context.get_messages()[1]
    assert not context.answered(first) and not context.answered(second)
    # The reply to the first turn was interrupted and is stored after the second user message.
    context.replying_to = first
    context.add_message(dict(role="assistant", content="Cut off"))
    assert context.answered(first) and not context.answered(second)
    context.replying_to = second
    context.add_message(dict(role="assistant", content="[ANGEL]"))  # Nothing said: nothing answered.
    assert not context.answered(second)
    context.add_message(dict(role="assistant", content="An answer"))
    assert context.answered(second)


async def test_a_switch_claimed_for_a_turn_marks_it_even_when_a_later_turn_takes_its_rerun():
    fake = FakeAddressee("devil", delay=.05)
    async with b2h(auto_switch=True, addressee=fake) as h:
        requests = record_requests(h)
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        h.control.context.add_message(dict(role="user", content="Devil, should I?"))
        await auto_open_turn(h, "wait, one more thing")
        mutate = h.control._mutate

        async def mutate_then_next_turn(*args, **kwargs):
            note = await mutate(*args, **kwargs)
            # The user's next words are stored while the switch is still being committed.
            assert await h.control.user_aggregator.push_aggregation() == "wait, one more thing"
            await until(lambda: h.control.gate.fence.held)
            assert users(h) == ["Devil, should I? [ANGEL]", "wait, one more thing [ANGEL]"]
            return note

        h.control._mutate = mutate_then_next_turn
        await h.control.user_aggregator.push_context_frame()
        await settled(h)
        assert any(kind == "route-rerun-withdrawn" for kind, _, _ in h.events)
        assert users(h) == ["Devil, should I? [DEVIL]", "wait, one more thing [DEVIL]"]
        assert [request[-1]["content"] for request in requests] == ["Devil, should I? [ANGEL]",
                                                                    "wait, one more thing [DEVIL]"]
        assert said(h, "devil") and not said(h, "angel")


# What Jev is asked.

async def conversation(h):
    """Stay, an addressed switch, stay, a tap, stay."""
    await h.hello()
    await until(lambda: h.control.state.device_session == "boot")
    await h.turn("What should I do?")
    await settled(h, 1)
    await h.turn("Devil, what do you think?")
    await settled(h, 2)
    await h.turn("And then?")
    await settled(h, 3)
    await h.send()
    assert (await h.outcome())["character"] == "angel"
    await h.turn("Angel, are you back?")
    await settled(h, 4)
    await h.turn("Good.")
    await settled(h, 5)


@pytest.mark.parametrize("vocative", [False, True])
async def test_jev_is_asked_exactly_what_it_is_asked_with_labels(vocative):
    asked, routes, spoke, wire = {}, {}, {}, {}
    for style in ("labels", "b2h"):
        fake = FakeAddressee("either", "devil", "either", "either", "either", delay=.02)
        async with pipeline(auto_switch=True, addressee=fake,
                            config={"context": style, "vocative_rule": vocative}) as h:
            await conversation(h)
            asked[style] = deepcopy(fake.calls)
            routes[style] = [(data["target"], data["reason"]) for kind, data, _ in h.events if kind == "route"]
            spoke[style] = {name: said(h, name) for name in h.services}
            sent = [m for m in h.transport.client.messages if isinstance(m, dict)]
            # Each interruption notice is repeated 100 ms later; count the interruptions, not the copies.
            wire[style] = (sorted(m.get("type") for m in sent if m.get("type") != "bot-interrupted"),
                           max(m["data"]["interrupt_seq"] for m in sent if m.get("type") == "bot-interrupted"))
    assert asked["b2h"] == asked["labels"] and len(asked["b2h"]) == 5
    # The device gets the same messages in the same number, but for user-llm-text, which would quote the marker.
    assert wire["labels"][0].count("user-llm-text") == 5 and "user-llm-text" not in wire["b2h"][0]
    assert ([kind for kind in wire["labels"][0] if kind != "user-llm-text"], wire["labels"][1]) == wire["b2h"]
    assert routes["b2h"] == routes["labels"] and spoke["b2h"] == spoke["labels"]
    last = asked["b2h"][-1]
    assert last["current_speaker"] == "angel" and last["latest"]["said"] == "Good."
    assert [(line["speaker"], line["said"]) for line in last["conversation"]] == [
        ("user", "What should I do?"), ("angel", REPLY), ("user", "Devil, what do you think?"), ("devil", REPLY),
        ("user", "And then?"), ("devil", REPLY), ("user", "Angel, are you back?"), ("angel", REPLY)]
    assert not ANY_MARK.search(json.dumps(asked["b2h"]))


def test_the_speaker_of_a_reply_is_kept_beside_it_not_read_from_a_neighbouring_marker():
    context = TaggedContext()
    context.state = type("State", (), {"character": "angel"})()
    context.add_message(dict(role="user", content="First"))
    context.state.character = "devil"   # A tap; the Angel's interrupted reply is stored after the next turn.
    context.add_message(dict(role="user", content="Second"))
    context.speaking_character = "angel"
    context.add_message(dict(role="assistant", content="The Angel's cut-off"))
    context.speaking_character = "devil"
    context.add_message(dict(role="assistant", content="[Devil] The Devil's answer [DEVIL]"))
    context.add_message(dict(role="assistant", content=" [ANGEL] "))  # Markers only: nothing was said.
    context.add_message(dict(role="user", content="Third"))
    messages = context.get_messages()
    assert [m["content"] for m in messages] == ["First [ANGEL]", "Second [DEVIL]", "The Angel's cut-off",
                                                "The Devil's answer ", "Third [DEVIL]"]
    assert context.labelled_history(messages) == ([
        dict(role="user", content="First"), dict(role="user", content="Second"),
        dict(role="assistant", content="[Angel] The Angel's cut-off"),
        dict(role="assistant", content="[Devil] The Devil's answer ")], "Third")
    assert context.labelled_history([]) == ([], "")


async def test_the_vocative_rule_reads_the_users_words_without_the_marker():
    # Jev says stay; the turn opens by calling the Devil, and the marker at its end changes nothing.
    fake = FakeAddressee("either", delay=.02)
    async with b2h(auto_switch=True, addressee=fake, config={"vocative_rule": True}) as h:
        await h.hello()
        await until(lambda: h.control.state.device_session == "boot")
        await h.turn("Devil")
        await settled(h)
        assert [data["reason"] for kind, data, _ in h.events if kind == "route"] == ["vocative-rule"]
        assert users(h) == ["Devil [DEVIL]"] and said(h, "devil")


# The marker never leaves the conversation.

MARKED = "[Angel] Honestly, I would say yes, but only once. [DEVIL] Then stop. And [angel] rest. [DEVIL]"
CLEAN = "Honestly, I would say yes, but only once. Then stop. And rest. "


@pytest.mark.parametrize("cut", ["words", "characters", "whole"])
async def test_a_marker_or_label_the_model_writes_reaches_no_voice_device_history_or_log(routing, cut):
    def tokens(text):
        return dict(words=by_word, characters=list, whole=lambda text: [text])[cut](text)

    seen = {}
    for name, text in (("marked", MARKED), ("clean", CLEAN)):
        lines = []
        sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
        try:
            async with b2h(auto_switch=routing, tts_kwargs=VOICES) as h:
                provider(h)
                stream(h, tokens(text))
                await h.hello()
                h.transport.client.messages.clear()
                for turn in range(1, 4):
                    await h.turn("And then?")
                    await settled(h, turn)
                await until(lambda: len(wire_text(h, "bot-tts-text")) == 12)
                wire = json.dumps(h.transport.client.messages)
                seen[name] = dict(said=said(h, "angel"), history=deepcopy(stored(h)),
                                  tts_text=wire_text(h, "bot-tts-text"), output=wire_text(h, "bot-output"),
                                  llm_text="".join(wire_text(h, "bot-llm-text")).split(),
                                  heard=wire_text(h, "user-transcription"))
                events = repr(h.events)
        finally:
            logger.remove(sink)
        assert "user-llm-text" not in wire and not ANY_MARK.search(wire)
        assert not ANY_MARK.search(events)
        assert not any(ANY_MARK.search(line) for line in lines if line.startswith(("Transcript:", "DUO ")))
        assert [line for line in lines if line.startswith("Transcript: user:")] == ["Transcript: user: And then?"] * 3
        # What the filter took out of each reply is counted in the log: 2 labels and 2 markers, or nothing.
        assert [line for line in lines if line.startswith("DUO marker-removed")] == (
            ['DUO marker-removed {"count": 4}'] * 3 if name == "marked" else [])
    assert seen["marked"] == seen["clean"]
    marked = seen["marked"]
    # The first clause still leaves early, and the device is told what the voice was given.
    assert marked["said"] == ["Honestly, I would say yes, ", "but only once. ", "Then stop. ", "And rest. "] * 3
    assert marked["tts_text"] == [piece.strip() for piece in marked["said"]]
    assert [m["content"] for m in marked["history"] if m["role"] == "assistant"] == [CLEAN.strip()] * 3
    assert marked["heard"] == ["And then?"] * 3


async def test_text_that_only_looked_like_a_marker_is_spoken_and_dropped_if_the_reply_is_interrupted(routing):
    async with b2h(auto_switch=routing, tts_kwargs=VOICES) as h:
        provider(h)
        stream(h, ["A reply that ends oddly", " [ang"])
        await h.hello()
        await h.turn()
        await settled(h)
        assert said(h, "angel") == ["A reply that ends oddly [ang "] and replies(h) == ["A reply that ends oddly [ang"]
    async with b2h(auto_switch=routing, tts_kwargs=VOICES) as h:
        provider(h)
        stream(h, ["An interrupted reply.", " Then", " [dev"], pause_after=3)
        h.llm.remainder.clear()
        await h.hello()
        await h.turn()
        await until(lambda: said(h, "angel") and h.transport.client.audio)
        await h.worker.queue_frame(InterruptionWorkerFrame())
        await until(lambda: replies(h))
        h.llm.remainder.set()
        assert await h.worker.flush_pipeline(timeout=.5)
        await asyncio.sleep(.03)
        # The piece held back as a possible marker went nowhere when the request was cancelled. (The voice
        # is still given the word it held, as any started reply's is.)
        assert said(h, "angel")[0] == "An interrupted reply. "
        assert not any("[" in text for text in said(h, "angel") + replies(h))


# Prefix stability: what the model server can reuse from the request before.

async def test_each_request_repeats_the_one_before_it_byte_for_byte():
    fake = FakeAddressee("either", "devil", "either", "either", "either", "either", delay=.02)
    async with b2h(auto_switch=True, addressee=fake) as h:
        requests = record_requests(h)
        await conversation(h)
        # A barge-in on top: an interrupted reply, then one more turn.
        h.llm.remainder.clear()
        spoken = len(said(h, "angel"))
        await h.turn("Tell me more")
        await until(lambda: len(said(h, "angel")) > spoken and len(requests) == 7)
        await h.worker.queue_frame(InterruptionWorkerFrame())
        await until(lambda: len(replies(h)) == 6)
        h.llm.remainder.set()
        await h.turn("Last one")
        await settled(h, 7)
        assert len(requests) == 8  # Seven turns; the addressed one was asked twice.
        sent = [json.dumps(request, ensure_ascii=False) for request in requests]
        for index, (earlier, later) in enumerate(zip(requests, requests[1:])):
            if index == 1:
                # The withheld draft and its rerun: the same request but for the marker, its last characters.
                assert earlier[:-1] == later[:-1] and len(earlier) == len(later)
                assert earlier[-1]["content"] == "Devil, what do you think? [ANGEL]"
                assert later[-1]["content"] == "Devil, what do you think? [DEVIL]"
                assert sent[1][:-len('[ANGEL]"}]')] == sent[2][:-len('[DEVIL]"}]')]
            else:
                # Everything sent before is sent again unchanged, as the same bytes, and only extended.
                assert later[:len(earlier)] == earlier and len(later) == len(earlier) + 2
                assert sent[index + 1].startswith(sent[index][:-1])
        assert all(request[0] == dict(role="system", content=duo_instructions()) for request in requests)
        assert [m["role"] for m in requests[-1][1:]] == ["user", "assistant"] * 6 + ["user"]
        assert [m["content"][-7:] for m in requests[-1][1:] if m["role"] == "user"] == [
            "[ANGEL]", "[DEVIL]", "[DEVIL]", "[ANGEL]", "[ANGEL]", "[ANGEL]", "[ANGEL]"]
        # The stored conversation is, message for message, what was sent.
        assert stored(h)[:-1] == requests[-1][1:]


async def test_a_long_session_grows_by_one_turn_and_one_reply_and_never_collects_markers():
    # The labelled context's loop (a copied label stored under a second one) has no b2h form: nothing is added.
    async with b2h(tts_kwargs=VOICES) as h:
        provider(h)
        stream(h, by_word("[Angel] A labelled reply. [ANGEL] [Devil] A final sentence. [DEVIL]"))
        requests = record_requests(h)
        await h.hello()
        for turn in range(1, 21):
            await h.turn("And then?")
            await settled(h, turn)
        assert replies(h) == ["A labelled reply. A final sentence."] * 20
        assert users(h) == ["And then? [ANGEL]"] * 20
        assert len(said(h, "angel")) == 40 and not any("[" in text for text in said(h, "angel"))
        sizes = [len(json.dumps(request)) for request in requests]
        assert len(sizes) == 20 and len({later - earlier for earlier, later in zip(sizes, sizes[1:])}) == 1


async def test_timing_rows_still_find_the_turn_whose_stored_message_carries_a_marker():
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
    try:
        fake = FakeAddressee("either", "devil", delay=.02)
        async with b2h(auto_switch=True, addressee=fake) as h:
            await h.hello()
            await until(lambda: h.control.state.device_session == "boot")
            await h.turn("What should I do?")
            await settled(h, 1)
            await h.turn("Devil, and you?")
            await settled(h, 2)
            await until(lambda: len([line for line in lines if line.startswith("DUO timing ")]) == 2)
    finally:
        logger.remove(sink)
    rows = [json.loads(line[len("DUO timing "):]) for line in lines if line.startswith("DUO timing ")]
    assert [(row["route_mode"], row["decision"], row["origin"]) for row in rows] == [
        ("routed", "stay", "aggregator-user-stopped"), ("rerun", "switch", "aggregator-user-stopped")]
    assert all(row["A_ms"] is not None and row["A_ms"] >= 0 for row in rows)
