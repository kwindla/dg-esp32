"""DUO_TTS_FIRST_CLAUSE: a reply's first sentence may reach the voice in two pieces.

The rule itself is tested on the aggregator alone. What depends on sentence
chunks (speech filter, history, interruption, the route hold, DUO timing and
bot-tts-text) is tested through the real 1.16 worker of test_duo_pipeline.
"""
import asyncio
import json
import random
import time
import types

from loguru import logger
import pytest

from pipecat.frames.frames import LLMTextFrame
from pipecat.services.tts_service import TextAggregationMode
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

import duo_bot
from duo_bot import FirstClause, NOTE_MARKER, configuration, make_tts, spoken_text
from test_duo_pipeline import FakeAddressee, pipeline, until


def by_word(text):
    """The way an LLM streams: each token starts with the space before its word."""
    words = text.split(" ")
    return [words[0]] + [" " + word for word in words[1:]]


async def pieces(aggregator, tokens, *, end=True):
    out = []
    for token in tokens:
        async for aggregation in aggregator.aggregate(token):
            out.append(aggregation.text)
    if end and (rest := await aggregator.flush()):
        out.append(rest.text)
    return out


async def first_clause(text, tokens=None):
    return await pieces(FirstClause(), by_word(text) if tokens is None else tokens)


async def sentences(text, tokens=None):
    return await pieces(SimpleTextAggregator(), by_word(text) if tokens is None else tokens)


def voiced(aggregations):
    """What the service gives Flux for these aggregations with no transform: tts_service._trim_text and
    _prepare_text_for_tts in sentence mode. The pipeline tests below check the same thing on the real service."""
    trimmed = [text.lstrip("\n") for text in aggregations]
    return "".join(text if text.endswith(" ") else text + " " for text in trimmed if text.strip())


@pytest.mark.parametrize("mark", [",", ";", ":", "—", "–", ".", "!", "?", "…"])
async def test_first_piece_ends_at_the_first_mark_with_four_words_before_it(mark):
    text = f"Well, I think so{mark} and you can keep the rest of it, too. A second, longer sentence, with commas."
    assert await first_clause(text) == [f"Well, I think so{mark}", "and you can keep the rest of it, too.",
                                        "A second, longer sentence, with commas."]


async def test_a_mark_is_a_boundary_only_before_whitespace():
    # No cut inside 1,000 or at a dash or comma that touches the next word.
    text = "It costs 1,000 dollars—really,\"no less\" than that today, so there. Fine."
    assert await first_clause(text) == ["It costs 1,000 dollars—really,\"no less\" than that today,", "so there.", "Fine."]


async def test_the_piece_is_out_before_the_rest_of_the_sentence_exists():
    aggregator = FirstClause()
    assert await pieces(aggregator, by_word("Honestly, I would say yes,"), end=False) == []
    # The space, and the character after it that shows the space is alone, arrive with the next token.
    assert await pieces(aggregator, [" but"], end=False) == ["Honestly, I would say yes,"]
    assert await pieces(aggregator, by_word(" only once. Then stop.")) == ["but only once.", "Then stop."]


async def test_without_a_mark_the_first_piece_is_eight_words():
    text = "You should really go outside and enjoy the sunshine today because it will rain tomorrow"
    assert await first_clause(text) == ["You should really go outside and enjoy the", "sunshine today because it will rain tomorrow"]
    # Marks that come too early do not count, and seven words are not enough.
    assert await first_clause("So, go and see the sea now") == ["So, go and see the sea now"]
    assert await first_clause("So, go and see the sea right now please") == ["So, go and see the sea right now", "please"]


@pytest.mark.parametrize("text", ["Sure.", "Yes, do it.", "No.", "Go on, then!", "Why not?", "Mm", "One two three four"])
async def test_a_short_reply_goes_out_exactly_as_before(text):
    assert await first_clause(text) == await sentences(text) == [text]


async def test_a_sentence_that_ends_first_is_the_first_piece_however_short():
    text = "Sure. Well, I do think so, yes, and so does everyone else here."
    assert await first_clause(text) == await sentences(text) == ["Sure.", "Well, I do think so, yes, and so does everyone else here."]


@pytest.mark.parametrize("text", ["...", ", ; : —", "— — — — — — — — —", " ", "", "?! ?! ?! ?! ?!"])
async def test_punctuation_alone_is_never_cut(text):
    assert await first_clause(text) == await sentences(text)


async def test_only_the_first_piece_of_a_reply_is_special():
    aggregator = FirstClause()
    text = "Honestly, I would say yes, but only once. And then, if you still want to, do it again, slowly."
    assert await pieces(aggregator, by_word(text)) == [
        "Honestly, I would say yes,", "but only once.", "And then, if you still want to, do it again, slowly."]
    # The end of a reply arms the next one.
    assert await pieces(aggregator, by_word(text)) == [
        "Honestly, I would say yes,", "but only once.", "And then, if you still want to, do it again, slowly."]
    assert await aggregator.flush() is None
    assert await pieces(aggregator, by_word("Honestly, I would say yes, again."), end=False) == ["Honestly, I would say yes,"]


@pytest.mark.parametrize("heard", ["Honestly, I wou", "Honestly, I would say yes, but on"])
async def test_an_interruption_drops_what_is_held_and_arms_the_next_reply(heard):
    aggregator = FirstClause()
    before = await pieces(aggregator, [heard], end=False)
    assert before == ([] if heard.endswith("wou") else ["Honestly, I would say yes,"])
    await aggregator.handle_interruption()
    assert aggregator.text.text == ""
    assert await pieces(aggregator, by_word("Right, let us do that, then. Good.")) == [
        "Right, let us do that,", "then.", "Good."]


async def test_labels_are_not_words_and_are_never_cut_apart():
    text = "[Angel] [Devil] Well, I think so, yes. [Angel] Next, a second one, with marks."
    got = await first_clause(text)
    assert got == ["[Angel] [Devil] Well, I think so,", "yes.", "[Angel] Next, a second one, with marks."]
    assert [await spoken_text(piece + " ", "sentence") for piece in got] == [
        "Well, I think so, ", "yes. ", "Next, a second one, with marks. "]
    # Eight labels are not eight words.
    assert await first_clause("[Angel] " * 8 + "hello there") == await sentences("[Angel] " * 8 + "hello there")


NOTES = [
    "Silent context: the Angel is speaking. A real sentence, with a clause, follows it.",
    "Okay then here it is Silent context: the Angel has handed over to the Devil. Then more.",
    "one two three four five six seven Silent context: the Angel is speaking, and so on. Then more.",
    "one two three four five six Silent context: the Angel is speaking, and so on.",
    "\"Silent context: the Angel is speaking, as you can see, right now.\" Then more.",
    "[Angel] Silent context: the human addressed the Devil, who is speaking now. Fine.",
]


@pytest.mark.parametrize("text", NOTES)
async def test_a_sentence_that_holds_a_note_is_never_cut_before_or_inside_the_note(text):
    for tokens in (by_word(text), list(text), [text]):
        got, whole = await first_clause(text, tokens), await sentences(text, tokens)
        # Nothing is cut off the front of the note's sentence, so the filter drops all of it, as before.
        assert got[0] == whole[0] and NOTE_MARKER in got[0]
        said = [await spoken_text(piece, "sentence") for piece in got]
        assert said[0] == "" and not any(NOTE_MARKER in piece for piece in said)
        assert said == [await spoken_text(piece, "sentence") for piece in whole]


@pytest.mark.parametrize("text,expected", [
    ("Okay then, here it is now: Silent context: the Angel is speaking. Then more.",
     ["Okay then, here it is now:", "Then more."]),
    ("Okay then, here it is now: and so Silent context: the Angel is speaking. Then more.",
     ["Okay then, here it is now:", "and so", "Then more."]),
    ("one two three four five six seven eight nine Silent context: the Angel is speaking. Then more.",
     ["one two three four five six seven eight", "nine", "Then more."]),
    ("Okay then, here it is now: Silent context: the Angel is speaking", ["Okay then, here it is now:"]),
    ("Okay then, here it is now: and Silent context: the Angel is speaking", ["Okay then, here it is now:", "and"]),
])
async def test_after_a_first_piece_the_rest_of_its_sentence_ends_at_a_note(text, expected):
    for tokens in (by_word(text), list(text), [text]):
        assert await first_clause(text, tokens) == expected
    # Only the rest of the cut sentence is treated so: a later sentence with a note still goes to the filter whole.
    later = "Okay then, here it is now: yes. And so Silent context: the Angel is speaking."
    assert await first_clause(later) == ["Okay then, here it is now:", "yes.", "And so Silent context: the Angel is speaking."]


ODD_SPACE = ["  ", "\n", "\t", "\u00a0", " \n", "\n ", " \u00a0", "\u00a0 ", "   "]


@pytest.mark.parametrize("gap", ODD_SPACE)
async def test_no_cut_where_the_whitespace_is_not_one_space(gap):
    # At the comma and at the eighth word: the place is passed over, and the text goes out as the sentence it is.
    for text in (f"One two three four,{gap}five six.", f"One two three four five six seven eight{gap}nine."):
        for tokens in (by_word(text), list(text), [text]):
            assert await first_clause(text, tokens) == await sentences(text, tokens), (text, tokens)
    # A later place with one space is still used, and the odd whitespace before it stays inside the piece.
    text = f"One two three four,{gap}five six, seven eight."
    assert await first_clause(text) == [f"One two three four,{gap}five six,", "seven eight."]


REPLIES = [
    "Oh, darling, skip the gym and have the cake instead. You have earned it, truly.",
    "I would call her today; she will be glad to hear from you.",
    "Naps are lovely: twenty minutes, no more, and you wake up kinder.",
    "Dr. Smith said so, I believe, on the 3rd of May. It was 4.5 degrees, e.g. cold.",
    "Honestly I think you should just go for a long walk by the river and then decide. No rush.",
    "Yes. No. Maybe, if it rains, we stay in, read, and drink tea.",
    "Well — if you must know — I already ate it. Sorry!",
    "Wait... are you sure, really sure, that you locked the door? I am not.",
    "One, two, three, four, five, six, seven, eight, nine, ten.",
    "[Angel] Be kind, be brief, and be on time. [Devil] Or be late, be loud, and be fun.",
    "It costs $1,000.50, which is a lot, for a hat.\nBuy it anyway, I say.",
    "Tell me more",
    "One two three four,  five six, seven eight nine. Ten.",
    "One two three four,\nfive six seven eight nine ten eleven twelve. Ten.",
    "One two three four,\tfive\u00a0six, seven  eight nine ten. Ten.",
    "\nOne two three four, five six.  Seven, eight, nine, ten, eleven.",
]


@pytest.mark.parametrize("text", REPLIES)
async def test_cutting_changes_no_character_given_to_the_voice(text):
    rng = random.Random(text)
    cut_somewhere = False
    for attempt in range(40):
        if attempt == 0:
            tokens = by_word(text)
        elif attempt == 1:
            tokens = list(text)
        else:
            cuts = sorted(rng.sample(range(1, len(text)), rng.randint(1, min(12, len(text) - 1))))
            tokens = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
        got, whole = await first_clause(text, tokens), await sentences(text, tokens)
        assert voiced(got) == voiced(whole), tokens
        if got != whole:
            # One cut, inside the first piece the sentence aggregator would have sent, at one space.
            cut_somewhere = True
            assert len(got) == len(whole) + 1 and got[2:] == whole[1:], tokens
            assert got[0] + " " + got[1] == whole[0], tokens
    assert cut_somewhere == (text not in ("Tell me more", "Yes. No. Maybe, if it rains, we stay in, read, and drink tea."))


def test_the_flag_is_on_by_default_and_off_is_the_stock_sentence_aggregator(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "offline-placeholder")
    assert configuration({})["tts_first_clause"] is True
    assert configuration({"DUO_TTS_FIRST_CLAUSE": "1"})["tts_first_clause"] is True
    off = configuration({"DUO_TTS_FIRST_CLAUSE": "0"})
    assert off["tts_first_clause"] is False
    for value in ("", "2", "true", "on"):
        with pytest.raises(ValueError, match="DUO_TTS_FIRST_CLAUSE"):
            configuration({"DUO_TTS_FIRST_CLAUSE": value})
    labels = {"DUO_CONTEXT": "labels"}
    for config, expected in ((configuration(labels), FirstClause),
                             (configuration(labels | {"DUO_TTS_FIRST_CLAUSE": "0"}), SimpleTextAggregator)):
        for service in make_tts(config).values():
            assert type(service._text_aggregator) is expected
            assert service._text_aggregation_mode == TextAggregationMode.SENTENCE
            assert service._text_transforms == [("*", spoken_text)]
    # The default context, b2h, has nothing to keep out of speech; the aggregator is chosen the same way.
    for config, expected in ((configuration({}), FirstClause), (off, SimpleTextAggregator)):
        for service in make_tts(config).values():
            assert type(service._text_aggregator) is expected and service._text_transforms == []
            assert service._text_aggregation_mode == TextAggregationMode.SENTENCE
    # The two settings are different bots to the launcher's identity check.
    assert duo_bot.identity(configuration({}))["config_sha256"] != duo_bot.identity(off)["config_sha256"]
    with pytest.raises(RuntimeError, match="DUO_TTS_FIRST_CLAUSE"):
        duo_bot.DuoFluxTTS(api_key="offline-placeholder", sample_rate=16000, first_clause=True,
                           text_aggregation_mode=TextAggregationMode.TOKEN,
                           settings=duo_bot.DuoFluxTTS.Settings(voice="offline"))


def production(first_clause):
    return dict(text_aggregation_mode=TextAggregationMode.SENTENCE, text_transforms=[("*", spoken_text)],
                first_clause=first_clause)


def stream(h, tokens, *, pause_after=None):
    """Make the fake LLM stream these tokens, waiting on h.llm.remainder after token number pause_after."""
    async def process(self, context):
        self.calls.append((self.settings.system_instruction, list(context.get_messages())))
        await self.generate.wait()
        response = len(self.calls)
        for index, token in enumerate(tokens):
            frame = LLMTextFrame(token)
            frame.metadata["test-response"] = response  # The fake voice records it.
            await self.push_frame(frame)
            if index + 1 == pause_after:
                await self.remainder.wait()
            await asyncio.sleep(.002)
    h.llm._process_context = types.MethodType(process, h.llm)


def provider(h):
    """Make the fake voices answer the way the provider does: later, and not at all for an interrupted turn.

    The stock fake hands back audio inside the Speak call, before a turn's
    first Speak has an audio context to put it in, and completes a turn 15 ms
    after its Flush even when an Interrupt came in between. Neither matters to
    a reply whose first sentence is followed by more text. A first clause that
    is interrupted is exactly the other case.
    """
    def install(service):
        def later(coroutine):
            task = asyncio.create_task(coroutine)
            service.provider_tasks.add(task)
            task.add_done_callback(service.provider_tasks.discard)

        async def audio():
            await asyncio.sleep(.003)
            await service._handle_audio(b"\x20\x01" * 1280)  # Two 40 ms chunks: the output writes whole chunks.

        async def send(message):
            if message["type"] == "Speak":
                service.speaks.append((message["text"], time.monotonic()))
                later(audio())
            elif message["type"] == "Flush":
                later(service.finish())
            elif message["type"] == "Interrupt":
                for task in tuple(service.provider_tasks):
                    task.cancel()
        service._transport_send_json = send
    for service in h.services.values():
        install(service)


def assistant(h):
    return [m["content"] for m in h.control.context.get_messages() if m.get("role") == "assistant"]


def tts_text(h):
    return [m["data"]["text"] for m in h.transport.client.messages if m.get("type") == "bot-tts-text"]


async def settled(h, replies):
    await until(lambda: len(assistant(h)) >= replies)
    await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
    assert await h.worker.flush_pipeline(timeout=.5)


REPLY = "Honestly, I would say yes, but only once. And then, if you like, again."
PAUSE = len(by_word("Honestly, I would say yes, but"))


@pytest.fixture
def timing_rows():
    rows = []
    def capture(message):
        text = message.record["message"]
        if text.startswith("DUO timing "):
            rows.append(json.loads(text.removeprefix("DUO timing ")))
    sink = logger.add(capture)
    try:
        yield rows
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("auto_switch", [False, True], ids=["unrouted", "routed"])
async def test_the_voice_starts_on_the_first_clause_and_nothing_else_changes(auto_switch, timing_rows):
    results = {}
    for flag in (False, True):
        timing_rows.clear()
        async with pipeline(auto_switch=auto_switch, config={"timing": True}, tts_kwargs=production(flag)) as h:
            await h.hello()
            angel = h.services["angel"]
            stream(h, by_word(REPLY), pause_after=PAUSE)
            h.llm.remainder.clear()
            h.transport.client.messages.clear()
            await h.turn("Should I?")
            if flag:
                # The model has not finished its first sentence, and the voice already has its first clause.
                await until(lambda: angel.speaks)
                assert [text for text, _ in angel.speaks] == ["Honestly, I would say yes, "]
                context_id = next(reversed(h.control.timing.sentences))
                first_text_at = h.control.timing.sentences[context_id]
            else:
                await asyncio.sleep(.1)
                assert not angel.speaks
            h.llm.remainder.set()
            await settled(h, 1)
            await until(lambda: len(timing_rows) == 1)
            if flag:
                # S1 is still the moment the first text was given to the voice.
                assert h.control.timing.sentences[context_id] == first_text_at
                assert timing_rows[0]["S1_ms"] < timing_rows[0]["A_ms"]
            results[flag] = dict(spoken=[text for text, _ in angel.speaks], history=assistant(h), wire=tts_text(h),
                                 row={key: timing_rows[0][key] for key in ("mode", "route_mode", "decision", "hold_delayed")},
                                 # Not the text messages, and not the interruption notice the bot repeats on a timer.
                                 # The voice starts earlier among the LLM's text messages, so these are counted, not ordered.
                                 other=sorted(m["type"] for m in h.transport.client.messages if m.get("type") not in
                                              ("bot-tts-text", "bot-output", "bot-transcription", "bot-interrupted")))
    off, on = results[False], results[True]
    assert off["spoken"] == ["Honestly, I would say yes, but only once. ", "And then, if you like, again. "]
    assert on["spoken"] == ["Honestly, I would say yes, ", "but only once. ", "And then, if you like, again. "]
    # The voice is given the same characters, the history is the same line, and the device sees the pieces spoken.
    assert "".join(on["spoken"]) == "".join(off["spoken"])
    assert on["history"] == off["history"] == ["[Angel] " + REPLY]
    assert off["wire"] == ["Honestly, I would say yes, but only once.", "And then, if you like, again."]
    assert on["wire"] == ["Honestly, I would say yes,", "but only once.", "And then, if you like, again."]
    assert on["row"] == off["row"] and on["other"] == off["other"]


async def test_a_held_reply_is_cut_the_same_way_once_the_route_lets_it_go(timing_rows):
    fake = FakeAddressee("either", delay=.15)
    async with pipeline(auto_switch=True, addressee=fake, config={"timing": True, "route_budget_s": 1},
                        tts_kwargs=production(True)) as h:
        await h.hello()
        stream(h, by_word(REPLY))
        await h.turn("Should I?")
        await settled(h, 1)
        await until(lambda: len(timing_rows) == 1)
        assert [text for text, _ in h.services["angel"].speaks] == [
            "Honestly, I would say yes, ", "but only once. ", "And then, if you like, again. "]
        assert assistant(h) == ["[Angel] " + REPLY]
        row = timing_rows[0]
        # The whole reply waited behind the route; the first clause went to the voice after the decision.
        assert row["hold_delayed"] and row["held_delay_ms"] > 0.05
        assert row["J_ms"] <= row["W_ms"] <= row["S1_ms"] < row["A_ms"] and row["T1_ms"] < row["J_ms"]


async def interrupted_first_sentence(auto_switch, heard, flag):
    """Barge in before the first clause is complete, or after it and before its sentence ends."""
    async with pipeline(auto_switch=auto_switch, tts_kwargs=production(flag)) as h:
        provider(h)
        await h.hello()
        angel = h.services["angel"]
        pause = PAUSE if heard == "first-clause" else len(by_word("Honestly, I would"))
        stream(h, by_word(REPLY), pause_after=pause)
        h.llm.remainder.clear()
        await h.turn("A")
        await until(lambda: h.llm.calls)
        if flag and heard == "first-clause":
            await until(lambda: angel.speaks and h.transport.client.audio)  # The first clause is being heard.
        else:
            await asyncio.sleep(.05)
        before = [text for text, _ in angel.speaks]
        stream(h, by_word("Right, let us do that, then. Good."))
        await h.turn("B")
        await until(lambda: len(h.llm.calls) == 2)
        h.llm.remainder.set()
        await until(lambda: any("Good." in line for line in assistant(h)))
        await settled(h, 1)
        return before, [text for text, _ in angel.speaks], assistant(h)


@pytest.mark.parametrize("auto_switch", [False, True], ids=["unrouted", "routed"])
@pytest.mark.parametrize("heard", ["nothing", "first-clause"])
async def test_history_holds_what_was_spoken_when_the_first_sentence_is_interrupted(auto_switch, heard):
    before, spoken, history = await interrupted_first_sentence(auto_switch, heard, True)
    assert before == (["Honestly, I would say yes, "] if heard == "first-clause" else [])
    # The reply after the interruption is cut at its own first clause.
    assert spoken[-3:] == ["Right, let us do that, ", "then. ", "Good. "]
    assert history[-1] == "[Angel] Right, let us do that, then. Good."
    # Whatever was given to the voice is what the history holds, and nothing the model wrote after the pause is in either.
    assert "".join(spoken) == "".join(line.removeprefix("[Angel] ") + " " for line in history)
    assert not any("only once" in text for text in spoken + history)
    off_before, off_spoken, off_history = await interrupted_first_sentence(auto_switch, heard, False)
    assert off_before == []
    if heard == "nothing":
        # Nothing had been cut yet: the interrupted reply settles exactly as it does with the flag off.
        assert history == off_history and "".join(spoken) == "".join(off_spoken)
    else:
        # The voice had started on the reply, so the reply settles the way a started reply always has:
        # the piece the voice held is flushed with it. The uncut sentence had not started.
        assert history[0] == "[Angel] Honestly, I would say yes, but"
        assert "".join(off_spoken) == "".join(line.removeprefix("[Angel] ") + " " for line in off_history)


async def spoken_and_stored(text, flag, auto_switch, tokens=None):
    """One completed reply through the real worker: every Speak, the stored history, every bot-tts-text."""
    async with pipeline(auto_switch=auto_switch, tts_kwargs=production(flag)) as h:
        provider(h)
        await h.hello()
        stream(h, by_word(text) if tokens is None else tokens)
        h.transport.client.messages.clear()
        await h.turn("Well?")
        await until(lambda: h.services["angel"].speaks)
        await until(lambda: h.control.phase == "idle" and not h.control.gate.closed and not h.services["angel"].provider_tasks)
        assert await h.worker.flush_pipeline(timeout=.5)
        await asyncio.sleep(.02)
        return [text for text, _ in h.services["angel"].speaks], assistant(h), tts_text(h)


EXACT = ([f"One two three four,{gap}five six. And then, seven." for gap in ODD_SPACE] +
         [f"One two three four five six seven eight{gap}nine ten. And then, seven." for gap in ODD_SPACE[:4]] +
         [f"One two three four,{gap}five six, seven eight. Nine." for gap in ODD_SPACE[:4]] +
         ["One two three four [Angel] five six seven eight [Devil] nine ten. Eleven.",
          "\n\nWell, I do think so, yes.\nAnd more, much more.", REPLY])


@pytest.mark.parametrize("auto_switch", [False, True], ids=["unrouted", "routed"])
async def test_the_voice_and_the_history_get_the_same_characters_as_with_the_flag_off(auto_switch):
    for text in EXACT:
        (off_speaks, off_history, off_wire), (on_speaks, on_history, on_wire) = [
            await spoken_and_stored(text, flag, auto_switch) for flag in (False, True)]
        # Not squeezed: repeated spaces, newlines, tabs and no-break spaces included.
        assert "".join(on_speaks) == "".join(off_speaks), text
        assert on_history == off_history and len(on_history) == 1, text
        assert " ".join(on_wire) == " ".join(off_wire), text
        # One cut at most, and none where the only place for one has other whitespace.
        assert len(on_speaks) - len(off_speaks) == (0 if text in EXACT[:len(ODD_SPACE)] else 1), text


NOTE_REPLIES = {
    # The marker opens the reply's first sentence, or comes before any place to cut: nothing differs from flag 0.
    "start": ("Silent context: the Angel is speaking, as you can see, right now. Then more, and more.", None, None),
    "labelled-start": ("[Angel] Silent context: the Angel is speaking now. Then more, and more.", None, None),
    "early": ("So, Silent context: the Angel is speaking now, as I said. Then more.", None, None),
    "eighth-word": ("one two three four five six seven Silent context: the Angel is speaking. Then more.", None, None),
    # The marker comes after a first piece that has been sent: the piece is speech, the marker and the rest
    # of that sentence are dropped, and the history holds what was sent.
    "after-clause": ("Okay then, here it is now: Silent context: the Angel is speaking. Then more.",
                     ["Okay then, here it is now: ", "Then more. "], "[Angel] Okay then, here it is now: Then more."),
    "after-clause-and-words": ("Okay then, here it is now: and so Silent context: the Angel is speaking. Then more.",
                               ["Okay then, here it is now: ", "and so ", "Then more. "],
                               "[Angel] Okay then, here it is now: and so Then more."),
    "after-eight-words": ("one two three four five six seven eight nine Silent context: the Angel is speaking. Then more.",
                          ["one two three four five six seven eight ", "nine ", "Then more. "],
                          "[Angel] one two three four five six seven eight nine Then more."),
    "after-clause-at-the-end": ("Okay then, here it is now: Silent context: the Angel is speaking",
                                ["Okay then, here it is now: "], "[Angel] Okay then, here it is now:"),
}
FLAG_OFF = {"after-clause": (["Then more. "], ["[Angel] Then more."]),
            "after-clause-and-words": (["Then more. "], ["[Angel] Then more."]),
            "after-eight-words": (["Then more. "], ["[Angel] Then more."])}


@pytest.mark.parametrize("auto_switch", [False, True], ids=["unrouted", "routed"])
@pytest.mark.parametrize("case", [name for name in NOTE_REPLIES if name != "after-clause-at-the-end"])
async def test_a_note_in_a_completed_reply(case, auto_switch):
    text, speaks, stored = NOTE_REPLIES[case]
    off_speaks, off_history, off_wire = await spoken_and_stored(text, False, auto_switch)
    on_speaks, on_history, on_wire = await spoken_and_stored(text, True, auto_switch)
    assert not any(NOTE_MARKER in said or "speaking" in said for said in off_speaks + on_speaks + off_wire + on_wire)
    assert not any(NOTE_MARKER in line or "speaking" in line for line in off_history + on_history)
    if speaks is None:
        assert (on_speaks, on_history, on_wire) == (off_speaks, off_history, off_wire)
        assert off_speaks[-1].startswith("Then more") and len(off_history) == 1
    else:
        assert (off_speaks, off_history) == FLAG_OFF[case]
        assert (on_speaks, on_history) == (speaks, [stored])
        assert on_wire == [said.strip() for said in speaks]


async def test_a_note_that_ends_the_reply_after_a_sent_clause_leaves_the_clause():
    text, speaks, stored = NOTE_REPLIES["after-clause-at-the-end"]
    on_speaks, on_history, on_wire = await spoken_and_stored(text, True, False)
    assert (on_speaks, on_history, on_wire) == (speaks, [stored], ["Okay then, here it is now:"])


@pytest.mark.parametrize("auto_switch", [False, True], ids=["unrouted", "routed"])
async def test_labels_and_notes_stay_out_of_speech_and_history_with_the_flag_on(auto_switch):
    # The WP8R loop test with a first sentence long enough to be cut, and a note placed where a cut would expose it.
    reply = ("[Angel] A labelled reply, with a clause, goes first. Silent context: Angel is speaking now, as said. "
             "[Angel] [Devil] A final sentence, also with a clause. ")
    async with pipeline(auto_switch=auto_switch, tts_kwargs=production(True)) as h:
        await h.hello()
        stream(h, by_word(reply))
        for turn in range(1, 7):
            await h.turn("And then?")
            await settled(h, turn)
        stored = "[Angel] A labelled reply, with a clause, goes first. A final sentence, also with a clause."
        assert assistant(h) == [stored] * 6
        said = [text for text, _ in h.services["angel"].speaks]
        assert said == ["A labelled reply, with a clause, ", "goes first. ", "A final sentence, also with a clause. "] * 6
        sizes = [sum(len(str(m.get("content"))) for m in messages) for _, messages in h.llm.calls]
        assert len({later - earlier for earlier, later in zip(sizes, sizes[1:])}) == 1
