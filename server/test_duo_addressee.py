"""Offline Jev policy and real classifier/client tests at the HTTP boundary."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from pipecat.classifiers.base_classifier import ChoiceResult, ClassifierError

from duo_addressee import (
    Addressee, Decision, HEARD_VIA, MODEL, addressee_question, addressee_state,
    configuration, decide, leading_vocative, split_context,
)
from duo_prompts import silent_note
import settings

HTTP_CLIENT = httpx.AsyncClient


def result(choice="devil", p=.9, confidence=.1):
    return ChoiceResult(choice=choice, probabilities={choice: p}, confidence=confidence)


def response(choice="devil", p=.9):
    return httpx.Response(200, json={"answers": {"addressee": {
        "choice": choice, "probabilities": {"angel": 1 - p, "devil": p, "either": 0},
        "confidence": .1,
    }}, "usage": {"input_tokens": 12, "output_tokens": 3}})


@pytest.fixture
async def routers(monkeypatch):
    owned = []

    def make(handler, budget=.1, model=MODEL, **transport):
        def http(**kwargs):
            assert kwargs["http2"] is True
            assert kwargs["timeout"] == budget
            return HTTP_CLIENT(**kwargs, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(httpx, "AsyncClient", http)
        router = Addressee("offline-placeholder", budget=budget, model=model, **transport)
        owned.append(router)
        return router

    yield make
    for router in owned:
        await router.close()


def test_question_has_described_options_and_sample_clues():
    question = addressee_question()
    assert set(question.options) == {"angel", "devil", "either"}
    assert all(isinstance(description, str) and len(description.split()) > 5
               for description in question.options.values())
    assert "gentle voice of conscience" in question.options["angel"]
    assert "cheeky voice of temptation" in question.options["devil"]
    assert "No one is singled out" in question.options["either"]
    assert set(question.instructions) == {"question", "clues"}
    clues = " ".join(question.instructions["clues"])
    for wording in ("Angela", "the good one", "the naughty one", "other one", "hear from",
                    "Talking about", "devil's advocate", "speak of the devil", "you're an angel",
                    "unnamed reply", "follow-up", "speech-to-text"):
        assert wording in clues


def test_split_state_excludes_latest_and_tail_notes_and_drops_silent_context():
    messages = [
        {"role": "system", "content": "private system instructions"},
        {"role": "system", "content": silent_note("angel", "devil")},
        {"role": "user", "content": "Should I?"},
        {"role": "assistant", "content": "[Angel] Think it over."},
        {"role": "assistant", "content": "[Devil] Go on!"},
        {"role": "assistant", "content": "[Angel] " + silent_note("devil", "angel")},
        {"role": "assistant", "content": "Unlabelled history cannot identify a speaker."},
        {"role": "tool", "content": "not conversation"},
        {"role": "user", "content": "Devil, what do you think?"},
        {"role": "system", "content": silent_note("angel", "devil")},
    ]
    original = deepcopy(messages)
    prior, latest = split_context(messages)
    assert prior == messages[:8] and latest == messages[8]["content"]
    state = addressee_state(prior, latest, "angel")
    assert set(state) == {"setting", "characters", "current_speaker", "conversation", "latest"}
    assert state["current_speaker"] == "angel"
    assert state["latest"] == dict(said=latest, heard_via=HEARD_VIA)
    assert state["conversation"] == [
        dict(speaker="user", said="Should I?"), dict(speaker="angel", said="Think it over."),
        dict(speaker="devil", said="Go on!"),
    ]
    assert set(state["characters"]) == {"angel", "devil"}
    assert all(set(character) == {"role", "about"} for character in state["characters"].values())
    assert messages == original
    prior[0]["content"] = "changed snapshot"
    assert messages == original


def test_state_keeps_eight_prior_spoken_lines_after_filtering():
    prior = [{"role": "user", "content": f"Line {i}"} for i in range(12)]
    prior += [{"role": "system", "content": silent_note("angel", "devil")}] * 10
    assert addressee_state(prior, "Latest", "devil")["conversation"] == [
        dict(speaker="user", said=f"Line {i}") for i in range(4, 12)]


def test_text_blocks_empty_context_and_empty_latest():
    assert split_context([]) == ([], "")
    prior = [{"role": "assistant", "content": [
        {"type": "text", "text": "[Devil] Try it."}, {"type": "image_url", "image_url": {}}]}]
    assert split_context(prior) == (prior, "")
    messages = prior + [{"role": "user", "content": [
        {"type": "text", "text": "Hello"}, {"type": "text", "text": "Angel"}]}]
    history, latest = split_context(messages)
    assert latest == "Hello\nAngel"
    assert addressee_state(history, latest, "devil")["conversation"] == [
        dict(speaker="devil", said="Try it.")]
    assert split_context([{"role": "user", "content": ""}]) == ([], "")
    assert addressee_state([{"role": "user", "content": silent_note("angel", "angel")}],
                           "", "angel")["conversation"] == []


@pytest.mark.parametrize("current,target", [("angel", "devil"), ("devil", "angel")])
def test_decide_acts_on_choice_probability_at_inclusive_floor(current, target):
    assert decide(result(target, .85), current, .85, True) == Decision(target, .85, None)
    assert decide(result(target, .84, confidence=1), current, .85, True) == Decision(None, .84, "below-floor")
    assert decide(result(target, 1), current, 1, True) == Decision(target, 1, None)
    assert decide(result(target, .9), current, .85, False) == Decision(None, .9, "no-baseline")
    assert decide(result(current, .1), current, .85, True) == Decision(None, .1, "stay")
    assert decide(result("either", .9), current, .85, True) == Decision(None, .9, "stay")


@pytest.mark.parametrize("error,reason", [
    (RuntimeError("offline"), "error"), (ClassifierError("offline"), "error"),
    (TimeoutError(), "timeout"), (httpx.ReadTimeout("offline"), "timeout"),
    (None, "error"),
])
def test_decide_failures(error, reason):
    assert decide(error, "angel", .85, True) == Decision(None, 0, reason)


@pytest.mark.parametrize("latest,called", [
    # A call: the name first, followed by , . ? ! or the end of the turn.
    ("Angel, what should I do?", "angel"), ("Angel.", "angel"), ("Angel?", "angel"), ("Angel!", "angel"),
    ("Angel", "angel"), ("devil, be honest", "devil"), ("DEVIL.", "devil"), ("  Devil!", "devil"),
    ("Devil , hm", "devil"), ("Angel\n", "angel"), ("Devil. What should I read next?", "devil"),
    # ... or after one of hey / okay / ok / so.
    ("Hey Angel, are you there?", "angel"), ("Okay Devil, your turn", "devil"), ("OK Angel,", "angel"),
    ("ok, devil.", "devil"), ("So Angel, what now?", "angel"), ("hey devil", "devil"),
    # Not a call: the name is part of something else, or does not lead.
    ("Angel food cake is the best cake.", None), ("Angels are everywhere.", None),
    ("The angel said I should rest.", None), ("Devil's advocate: what if I stay?", None),
    ("Angela, are you there?", None), ("Hey, angels of mercy", None), ("Angel what do you think", None),
    ("Spare me a response from Angel", None),  # The frozen negation case: the name comes last.
    ("So what, Angel?", None), ("Thanks, you're an angel for saying that.", None),
    ("Speak of the devil, I just found another cake.", None), ("Can I hear from the angel now?", None),
    # Left to Jev on purpose: a misheard name, another opener, a full stop after the opener.
    ("Bangel, are you there?", None), ("Well, Devil, go on.", None), ("Okay. Angel, go on.", None),
    ("Okay so Angel, go on.", None), ("", None), ("Hey", None),
])
def test_leading_vocative_names_the_character_a_turn_opens_by_calling(latest, called):
    assert leading_vocative(latest) == called


def test_decide_recognizes_client_wrapped_http_timeout():
    try:
        try:
            raise httpx.ReadTimeout("offline")
        except httpx.ReadTimeout as error:
            raise ClassifierError("offline") from error
    except ClassifierError as error:
        assert decide(error, "angel", .85, True).reason == "timeout"


@pytest.mark.parametrize("bad", [result("unknown"), result(p=float("nan")),
                                 result(p=float("inf")), result(p=-.1), result(p=1.1),
                                 ChoiceResult(choice="devil", probabilities={}, confidence=1)])
def test_decide_rejects_unusable_choice_or_probability(bad):
    assert decide(bad, "angel", .85, True) == Decision(None, 0, "error")


async def test_real_http_payload_and_parser_no_cache_and_connect_once(routers):
    requests = []

    async def handler(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": []})
        assert request.url.path == "/v1/systemone"
        assert request.headers["authorization"] == "Bearer offline-placeholder"
        return response()

    router = routers(handler)
    await router.connect()
    await router.connect()
    state = addressee_state([], "Devil, should I?", "angel")
    for _ in range(2):
        answer = await router.ask(state)
        assert answer.choice == "devil" and answer.confidence == .1
        assert answer.probabilities == pytest.approx({"angel": .1, "devil": .9, "either": 0})
    assert [request.method for request in requests] == ["GET", "POST", "POST"]
    payload = json.loads(requests[1].content)
    question = addressee_question()
    assert payload == {"model": MODEL, "state": state, "questions": {"addressee": {
        "type": "choice", "instructions": question.instructions, "criteria": question.options}}}
    assert router.client.usage.input_tokens == 24
    assert router.client.usage.output_tokens == 6


@pytest.mark.parametrize("status", [429, 529, 500])
async def test_http_failures_make_exactly_one_request(routers, status):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(status)

    router = routers(handler)
    with pytest.raises(ClassifierError) as caught:
        await router.ask({})
    assert len(calls) == 1
    assert decide(caught.value, "angel", .85, True).reason == "error"


@pytest.mark.parametrize("body", [[], {}, {"answers": {}},
    {"answers": {"addressee": {"choice": "not-an-option", "confidence": 1}}}])
async def test_bad_http_replies_are_errors(routers, body):
    router = routers(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ClassifierError):
        await router.ask({})


async def test_outer_budget_cancels_and_joins_request(routers):
    started, cancelled, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.sleep(.005)
            raise
        finally:
            joined.set()

    router = routers(handler, budget=.02)
    with pytest.raises(TimeoutError) as caught:
        async with asyncio.timeout(router.budget):
            await router.ask({})
    assert started.is_set() and cancelled.is_set() and joined.is_set()
    assert not router._pending
    assert decide(caught.value, "angel", .85, True).reason == "timeout"


async def test_http_timeout_is_preserved_as_fallback_reason(routers):
    async def handler(request):
        raise httpx.ReadTimeout("offline")

    router = routers(handler)
    with pytest.raises(ClassifierError) as caught:
        await router.ask({})
    assert decide(caught.value, "angel", .85, True).reason == "timeout"


@pytest.mark.parametrize("operation", ["ask", "connect"])
async def test_parent_cancellation_propagates_and_joins(routers, operation):
    started, joined = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()

    router = routers(handler)
    task = asyncio.create_task(router.ask({}) if operation == "ask" else router.connect())
    await asyncio.wait_for(started.wait(), .5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert joined.is_set() and not router._pending


@pytest.mark.parametrize("operation", ["ask", "connect"])
async def test_close_cancels_joins_then_closes_http(routers, operation):
    started, joined = asyncio.Event(), asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # The request must finish cancellation before its pool closes.
            await asyncio.sleep(.005)
            assert not router.client._http.is_closed
            joined.set()

    router = routers(handler)
    task = asyncio.create_task(router.ask({}) if operation == "ask" else router.connect())
    await asyncio.wait_for(started.wait(), .5)
    await router.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert joined.is_set() and router.client._http.is_closed and not router._pending
    await router.close()
    with pytest.raises(RuntimeError, match="closed"):
        await router.ask({})


@pytest.mark.parametrize("mode", ["error", "timeout"])
async def test_connect_warns_and_falls_back_within_budget(routers, monkeypatch, mode):
    warnings, joined = [], asyncio.Event()
    monkeypatch.setattr("duo_addressee.logger.warning", lambda *args: warnings.append(args))

    async def handler(request):
        if request.method == "POST":
            return response()
        try:
            if mode == "error":
                return httpx.Response(500)
            await asyncio.Event().wait()
        finally:
            joined.set()

    router = routers(handler, budget=.02)
    await asyncio.wait_for(router.connect(), .5)
    assert joined.is_set() and len(warnings) == 1 and not router._pending
    assert (await router.ask({})).choice == "devil"


BASE_CONFIG = {"DUO_AUTO_SWITCH_FLOOR": ".85", "DUO_ROUTE_BUDGET_S": ".3"}


def test_configuration_pins_model_and_parses_explicit_policy():
    assert configuration(BASE_CONFIG) == dict(jev_model=MODEL, auto_switch_floor=.85, route_budget_s=.3)
    assert configuration(dict(BASE_CONFIG, DUO_JEV_MODEL="jev-1.13.0"))["jev_model"] == "jev-1.13.0"
    assert configuration(dict(BASE_CONFIG, DUO_AUTO_SWITCH_FLOOR="1", DUO_ROUTE_BUDGET_S="2"))


@pytest.mark.parametrize("value", [".5", "0", "-.1", "1.01", "nan", "inf", "-inf", "bad"])
def test_configuration_rejects_invalid_floor(value):
    with pytest.raises(ValueError):
        configuration(dict(BASE_CONFIG, DUO_AUTO_SWITCH_FLOOR=value))


@pytest.mark.parametrize("value", ["0", "-.1", "2.01", "nan", "inf", "-inf", "bad"])
def test_configuration_rejects_invalid_budget(value):
    with pytest.raises(ValueError):
        configuration(dict(BASE_CONFIG, DUO_ROUTE_BUDGET_S=value))


@pytest.mark.parametrize("value", ["jev-latest", "jev", "jev-1", "jev-1.14", "jev-1.14.0-preview", ""])
def test_configuration_rejects_model_aliases(value):
    with pytest.raises(ValueError, match="versioned"):
        configuration(dict(BASE_CONFIG, DUO_JEV_MODEL=value))


@pytest.mark.parametrize("missing", ["DUO_AUTO_SWITCH_FLOOR", "DUO_ROUTE_BUDGET_S"])
def test_configuration_uses_calibrated_default_for_missing_policy(missing):
    config = configuration({key: value for key, value in BASE_CONFIG.items() if key != missing})
    assert config == dict(jev_model=MODEL,
                          auto_switch_floor=.80 if missing == "DUO_AUTO_SWITCH_FLOOR" else .85,
                          route_budget_s=.5 if missing == "DUO_ROUTE_BUDGET_S" else .3)


def test_configuration_defaults_without_overrides():
    from duo_addressee import DEFAULT_AUTO_SWITCH_FLOOR, DEFAULT_ROUTE_BUDGET_S, DEFAULT_JEV_MODEL
    assert configuration({}) == dict(jev_model=DEFAULT_JEV_MODEL,
                                      auto_switch_floor=DEFAULT_AUTO_SWITCH_FLOOR,
                                      route_budget_s=DEFAULT_ROUTE_BUDGET_S)
    assert (DEFAULT_AUTO_SWITCH_FLOOR, DEFAULT_ROUTE_BUDGET_S, DEFAULT_JEV_MODEL) == (.80, .5, "jev-1.14.0")


@pytest.mark.parametrize("key_present", [False, True])
@pytest.mark.parametrize("approved_only", [False, True])
def test_credentials_load_only_typesafe_name_without_opening_private_files(
        monkeypatch, key_present, approved_only):
    paths, dotenv_paths = [], []
    for name in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY", "UNAPPROVED_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "offline-deepgram")
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-inherited")
    typesafe_path = Path.home() / ".config" / "typesafe.env"

    def values(path):
        paths.append(path)
        if path == typesafe_path:
            return dict(UNAPPROVED_NAME="offline-extra", **(
                {"TYPESAFE_API_KEY": "offline-approved"} if key_present else {}))
        assert path == settings.ROOT.parent / "pipecat/.env"
        return {"DEEPGRAM_API_KEY": "offline-deepgram", "OPENAI_API_KEY": "offline-openai"}

    monkeypatch.setattr(settings, "dotenv_values", values)
    monkeypatch.setattr(settings, "load_dotenv", lambda path, **kwargs: dotenv_paths.append(path))
    settings.load_credentials(approved_only=approved_only)
    assert (typesafe_path in paths) == approved_only
    assert settings.os.getenv("TYPESAFE_API_KEY") == (
        ("offline-approved" if key_present else None) if approved_only else "offline-inherited")
    assert settings.os.getenv("UNAPPROVED_NAME") is None
    assert len(dotenv_paths) == (0 if approved_only else 2)


class Scripted:
    """A Jev endpoint whose n-th read follows the n-th script entry.

    An entry is a delay in seconds followed by an answer, an HTTP status, or
    ``None`` for a read that never answers. ``cleanup`` is how long a cancelled
    read takes to finish.
    """
    def __init__(self, *script, cleanup=0):
        self.script, self.cleanup = list(script), cleanup
        self.reads, self.cancelled, self.finished, self.models = 0, 0, 0, 0
        self.active, self.most_active = 0, 0
        self.traced = []

    async def __call__(self, request):
        if request.method == "GET":
            self.models += 1
            return httpx.Response(200, json={"data": []})
        delay, outcome = self.script[self.reads]
        self.reads += 1
        self.traced.append("trace" in request.extensions)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        try:
            await asyncio.sleep(delay)
            if outcome is None:
                await asyncio.Event().wait()
            return httpx.Response(outcome) if isinstance(outcome, int) else response(outcome)
        except asyncio.CancelledError:
            self.cancelled += 1
            await asyncio.sleep(self.cleanup)
            raise
        finally:
            self.active -= 1
            self.finished += 1


async def test_read_reports_whether_it_opened_a_connection(routers):
    events = []

    async def handler(request):
        if request.method == "GET":
            assert "trace" not in request.extensions  # The warm-up is not a read.
            return httpx.Response(200, json={"data": []})
        # Drive the trace the way httpcore does for this request.
        for name in events:
            await request.extensions["trace"](name, {})
        return response()

    router = routers(handler)
    await router.connect()
    assert router.last_read is None
    events[:] = ["connection.connect_tcp.started", "connection.connect_tcp.complete",
                 "connection.start_tls.started", "connection.start_tls.complete",
                 "http2.send_connection_init.started", "http2.send_request_headers.started",
                 "http2.receive_response_headers.complete"]
    await router.ask({})
    opened = router.last_read
    assert opened["connection"] == "new"
    assert opened["connect_ms"] >= 0 and opened["tls_ms"] >= 0
    events[:] = ["http2.send_request_headers.started", "http2.receive_response_headers.complete"]
    await router.ask({})
    assert router.last_read == {"connection": "reused"} and router.last_read is not opened
    events[:] = ["connection.connect_tcp.started", "connection.connect_tcp.failed"]
    await router.ask({})
    assert router.last_read == {"connection": "new"}  # Attempted, never completed.
    events[:] = []
    await router.ask({})
    assert router.last_read == {}  # No transport report: nothing is claimed.


async def test_no_hedge_and_no_keepwarm_unless_configured(routers):
    endpoint = Scripted((.03, "devil"), (.03, "devil"))
    router = routers(endpoint)
    assert router.hedge_s is None and router.keepwarm_s is None
    await router.connect()
    assert (await router.ask({})).choice == "devil"
    await asyncio.sleep(.05)
    assert (await router.ask({})).choice == "devil"
    assert (endpoint.reads, endpoint.models, endpoint.most_active) == (2, 1, 1)
    assert router._keepwarm is None and endpoint.traced == [True, True]
    assert "hedged" not in router.last_read


@pytest.mark.parametrize("script,reads,winner,choice", [
    ([(.005, "devil")], 1, None, "devil"),                    # Answered before the hedge is due.
    ([(0, None), (.005, "angel")], 2, "hedge", "angel"),      # The first never answers.
    ([(.05, "devil"), (0, None)], 2, "first", "devil"),       # The first answers after the hedge left.
    ([(.05, 500), (.07, "angel")], 2, "hedge", "angel"),      # An error is not an answer...
    ([(.07, "devil"), (.005, 500)], 2, "first", "devil"),     # ...whichever request it comes from.
])
async def test_hedge_sends_at_most_one_duplicate_and_takes_the_first_answer(routers, script, reads, winner, choice):
    endpoint = Scripted(*script)
    router = routers(endpoint, budget=.5, hedge_s=.02)
    answer = await router.ask({})
    assert answer.choice == choice
    assert endpoint.reads == reads and endpoint.most_active <= 2
    assert router.last_read.get("hedged", False) == (reads == 2)
    assert router.last_read.get("winner") == (winner or "first")
    # The loser was cancelled and joined before the answer was returned.
    assert endpoint.finished == reads and not router._pending


@pytest.mark.parametrize("script,reads", [([(.005, 500)], 1), ([(.04, 500), (.005, 503)], 2)])
async def test_hedge_reports_the_first_failure_when_nothing_answers(routers, script, reads):
    endpoint = Scripted(*script)
    router = routers(endpoint, budget=.5, hedge_s=.02)
    with pytest.raises(ClassifierError) as caught:
        await router.ask({})
    assert endpoint.reads == reads and endpoint.finished == reads and not router._pending
    assert decide(caught.value, "angel", .85, True).reason == "error"


@pytest.mark.parametrize("stop", ["cancel", "budget", "close"])
async def test_hedge_cancellation_cancels_and_joins_both_requests(routers, stop):
    endpoint = Scripted((0, None), (0, None), cleanup=.03)
    router = routers(endpoint, budget=.5, hedge_s=.02)

    async def read():
        if stop == "budget":
            async with asyncio.timeout(.06):
                return await router.ask({})
        return await router.ask({})

    task = asyncio.create_task(read())
    while endpoint.reads < 2:
        await asyncio.sleep(.002)
    if stop == "cancel":
        task.cancel()
        await asyncio.sleep(.005)
        task.cancel()  # A second cancellation must not abandon the join.
    elif stop == "close":
        await router.close()
    with pytest.raises(TimeoutError if stop == "budget" else asyncio.CancelledError):
        await task
    assert endpoint.cancelled == 2 and endpoint.finished == 2 and not router._pending
    assert router.last_read == {"hedged": True}


@pytest.mark.parametrize("bound,waits", [(None, True), (0, False), (.01, False)])
async def test_hedge_loser_join_follows_the_cancellation_bound(routers, bound, waits):
    loop = asyncio.get_running_loop()
    endpoint = Scripted((0, None), (.005, "angel"), (.005, "devil"), cleanup=.15)
    router = routers(endpoint, budget=.5, hedge_s=.02, join_bound_s=bound)
    started = loop.time()
    assert (await router.ask({})).choice == "angel"
    elapsed = loop.time() - started
    if waits:
        assert elapsed >= .15 and not router._pending
    else:
        assert elapsed < .1 and len(router._pending) == 1  # The loser is still owned.
        # While it is outstanding the next read is not hedged: never a third request.
        assert (await router.ask({})).choice == "devil"
        assert "hedged" not in router.last_read and endpoint.most_active == 2
        while router._pending:
            await asyncio.sleep(.005)
    assert endpoint.finished == endpoint.reads


async def test_keepwarm_fires_only_after_idle_time_and_stops_at_close(routers):
    loop = asyncio.get_running_loop()
    endpoint = Scripted((.12, "devil"), (0, "devil"))
    router = routers(endpoint, budget=.5, keepwarm_s=.05)
    await router.connect()
    assert endpoint.models == 1 and router._keepwarm is not None
    await asyncio.sleep(.13)
    idle_pings = endpoint.models
    assert 2 <= idle_pings <= 4  # About one per 50 ms of idle time, after the warm-up.
    # A read in flight is activity: no keep-alive request is sent beside it.
    await router.ask({})
    assert endpoint.models <= idle_pings + 1
    during = endpoint.models
    await router.ask({})
    finished = loop.time()
    while endpoint.models == during:
        await asyncio.sleep(.002)
    assert loop.time() - finished >= .04  # Counted from the read, not from the last ping.
    await router.close()
    assert router._keepwarm.done() and not router._pending
    after = endpoint.models
    await asyncio.sleep(.08)
    assert endpoint.models == after


@pytest.mark.parametrize("connecting", [False, True])
async def test_read_never_runs_beside_a_keepwarm_that_started_first(routers, connecting):
    """Sol R1. The keep-alive request is in flight when the read arrives."""
    parked, release = asyncio.Event(), asyncio.Event()
    seen = dict(models=0, warming=0, overlap=None, cancelled=0, completed=0)

    async def handler(request):
        if request.method == "POST":
            seen["overlap"] = seen["warming"] > 0
            return response()
        seen["models"] += 1
        if seen["models"] == 1:  # The warm-up.
            return httpx.Response(200, json={"data": []})
        seen["warming"] += 1
        try:
            if connecting:  # As httpcore reports a request that has to open a connection.
                await request.extensions["trace"]("connection.connect_tcp.started", {})
            parked.set()
            await release.wait()
            seen["completed"] += 1
            return httpx.Response(200, json={"data": []})
        except asyncio.CancelledError:
            seen["cancelled"] += 1
            raise
        finally:
            seen["warming"] -= 1

    router = routers(handler, budget=.5, keepwarm_s=.02)
    await router.connect()
    await asyncio.wait_for(parked.wait(), 1)
    read = asyncio.create_task(router.ask({}))
    if connecting:
        # It is opening the connection the read needs: the read waits, and does not cancel it.
        await asyncio.sleep(.03)
        assert not read.done() and seen["cancelled"] == 0 and seen["overlap"] is None
        release.set()
    answer = await asyncio.wait_for(read, 1)
    assert answer.choice == "devil" and seen["overlap"] is False
    # On an open connection it is cancelled and joined instead: no round trip is spent waiting.
    assert (seen["cancelled"], seen["completed"]) == ((0, 1) if connecting else (1, 0))
    # Being preempted does not end the keep-alive loop.
    release.set()
    sent = seen["models"]
    while seen["models"] == sent:
        await asyncio.sleep(.005)
    assert not router._keepwarm.done()
    await router.close()
    assert router._keepwarm.done() and not router._pending


async def test_hedge_cancelled_during_the_bounded_loser_join_returns_only_when_both_have_joined(routers):
    """Sol R2. A winner exists, the loser's cleanup is blocked, and the caller is cancelled meanwhile."""
    cleaning, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def handler(request):
        nonlocal reads
        reads += 1
        if reads == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                while not release.is_set():  # A cleanup that outlasts repeated cancellation.
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
        return response("angel")

    router = routers(handler, budget=.5, hedge_s=.01, join_bound_s=.05)
    read = asyncio.create_task(router.ask({}))
    await asyncio.wait_for(cleaning.wait(), 1)  # The duplicate won; the first request is being retired.
    read.cancel()
    await asyncio.sleep(.12)  # Well past the 50 ms bound a winner's loser would get.
    assert not read.done() and len(router._pending) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert not router._pending and reads == 2
    assert router.last_read == {"hedged": True, "winner": "hedge"}
