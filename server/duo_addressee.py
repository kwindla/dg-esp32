"""One end-of-turn Jev read and the calibrated routing configuration."""
import asyncio
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import math
import os
import re
from typing import Any

import httpx
from loguru import logger
from pipecat.classifiers.base_classifier import ChoiceQuestion, ChoiceResult
from pipecat.classifiers.typesafe.jev.classifier import JevClassifier
from pipecat.classifiers.typesafe.jev.client import JevClient

# Floor selected by the coordinator from the repaired-question step 2c replay.
DEFAULT_AUTO_SWITCH_FLOOR = .80
DEFAULT_ROUTE_BUDGET_S = .5
DEFAULT_JEV_MODEL = "jev-1.14.0"
MODEL = DEFAULT_JEV_MODEL  # Preserve the existing pinned-model export.
HEARD_VIA = "live speech-to-text, so a name may be misheard"
CHARACTERS = {
    "angel": {"role": "the gentle voice of conscience", "about": "Kind, thoughtful advice."},
    "devil": {"role": "the cheeky voice of temptation", "about": "Playful, tempting advice."},
}
LABEL = re.compile(r"^\[(Angel|Devil)\]\s*")
# DUO_VOCATIVE_RULE: a turn that opens by calling a character is for that
# character. The name comes first, or after one of hey / okay / ok / so, and
# is followed by , . ? ! or ends the turn. So "Angel food cake", "Angels are"
# and "Devil's advocate" are not calls, and neither is a name later in a turn.
VOCATIVE = re.compile(r"\s*(?:(?:hey|okay|ok|so)[\s,]+)?(angel|devil)\s*(?:[,.?!]|$)", re.IGNORECASE)


def _floor(value):
    value = float(value)
    if not math.isfinite(value) or not .5 < value <= 1:
        raise ValueError("DUO_AUTO_SWITCH_FLOOR must be finite and in (0.5, 1]")
    return value


def _budget(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 2:
        raise ValueError("DUO_ROUTE_BUDGET_S must be finite and in (0, 2]")
    return value


def _model(value):
    if not re.fullmatch(r"jev-[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("DUO_JEV_MODEL must be a versioned Jev ID; aliases are not allowed")
    return value


def configuration(environ=None):
    """Validate overrides using the defaults accepted in step 2.

    Changing the pinned model requires rerunning the step 2 acceptance set.
    """
    env = os.environ if environ is None else environ
    return dict(jev_model=_model(env.get("DUO_JEV_MODEL", DEFAULT_JEV_MODEL)),
                auto_switch_floor=_floor(env.get("DUO_AUTO_SWITCH_FLOOR", DEFAULT_AUTO_SWITCH_FLOOR)),
                route_budget_s=_budget(env.get("DUO_ROUTE_BUDGET_S", DEFAULT_ROUTE_BUDGET_S)))


def addressee_question() -> ChoiceQuestion:
    return ChoiceQuestion(
        instructions={
            "question": "In `latest`, which single character, if any, does the user intentionally "
            "select to answer? Otherwise carry on with `current_speaker` by choosing `either`.",
            "clues": [
                "Highest priority: affectionate compliments, thanks, teasing and idioms aimed "
                "at 'you' are reactions to `current_speaker`, even when they use a character's "
                "name or adjective. They do not select a different speaker: choose `either`. "
                "Only a request for a character to answer or speak, or direct address by name "
                "to ask or tell that character something, counts as selecting them. Interpret "
                "a name in context, rather than treating a flattering or teasing description "
                "of the current speaker as a handover.",
                "For intentional address, a spoken name or epithet identifies the addressee even when speech-to-text "
                "mishears it: 'Angela', 'the good one', 'the naughty one'.",
                "'The other one' and corrections such as 'not you' address the character "
                "other than `current_speaker`.",
                "A request to hear from a character goes to that character.",
                "Talking about a character is not addressing them. Choose the character "
                "addressed, or carry on with the current speaker if nobody is addressed.",
                "Idioms such as 'devil's advocate', 'speak of the devil' and 'you're an angel' "
                "are not address.",
                "An unnamed reply or follow-up is `either`: carry on with whoever is speaking.",
            ],
        },
        options={
            "angel": "The Angel, the gentle voice of conscience, is singled out to answer.",
            "devil": "The Devil, the cheeky voice of temptation, is singled out to answer.",
            "either": "No one is singled out to take the speaking turn, both are addressed, "
            "or the user is reacting affectionately to the current speaker. Carry on with "
            "whoever is speaking, including compliments, thanks, teasing and idioms.",
        },
    )


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
    return ""


def split_context(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict], str]:
    """Freeze the history before the latest user message, excluding it and any tail notes."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return deepcopy(list(messages[:index])), _text(messages[index].get("content"))
    return deepcopy(list(messages)), ""


def addressee_state(prior, latest: str, current: str) -> dict:
    if current not in CHARACTERS:
        raise ValueError("current speaker must be angel or devil")
    conversation = []
    for message in prior:
        role = message.get("role")
        said = _text(message.get("content")).strip()
        if role not in {"user", "assistant"} or not said:
            continue
        speaker = "user"
        if role == "assistant":
            label = LABEL.match(said)
            # Historical speakers come only from the insertion-time label.
            if not label:
                continue
            speaker, said = label[1].lower(), said[label.end():]
        if not said or said.startswith("Silent context:"):
            continue
        conversation.append(dict(speaker=speaker, said=said))
    return dict(
        setting="A spoken conversation between a user and two characters, the Angel and the Devil.",
        characters=deepcopy(CHARACTERS), current_speaker=current,
        conversation=conversation[-8:], latest=dict(said=latest, heard_via=HEARD_VIA),
    )


def leading_vocative(latest: str) -> str | None:
    """The character a turn opens by calling, or None. Says nothing about who is speaking now."""
    called = VOCATIVE.match(latest)
    return called[1].lower() if called else None


@dataclass(frozen=True)
class Decision:
    target: str | None
    p: float
    # Successful selection has a target and no fallback reason.
    reason: str | None


def decide(result_or_error, current: str, floor: float, has_baseline: bool) -> Decision:
    floor = _floor(floor)
    if current not in CHARACTERS:
        raise ValueError("current speaker must be angel or devil")
    if isinstance(result_or_error, Exception):
        error = result_or_error
        while error is not None:
            if isinstance(error, (TimeoutError, httpx.TimeoutException)):
                return Decision(None, 0, "timeout")
            error = error.__cause__
        return Decision(None, 0, "error")
    if not isinstance(result_or_error, ChoiceResult):
        return Decision(None, 0, "error")
    choice = result_or_error.choice
    p = result_or_error.probabilities.get(choice, float("nan"))
    if choice not in {*CHARACTERS, "either"} or not math.isfinite(p) or not 0 <= p <= 1:
        return Decision(None, 0, "error")
    if choice in {current, "either"}:
        return Decision(None, p, "stay")
    if p < floor:
        return Decision(None, p, "below-floor")
    if not has_baseline:
        return Decision(None, p, "no-baseline")
    return Decision(choice, p, None)


# The read a request belongs to, so the HTTP hook can say how that read travelled.
_READ = ContextVar("duo_jev_read", default=None)


class Addressee:
    """Own the HTTP client and join every request, including caller cancellation.

    Three things concern only how a read travels, never what is asked or how
    the answer is judged. Each read records whether it opened a connection
    (``last_read``). ``keepwarm_s`` sends the cheapest request whenever the
    connection has been idle that long; a read and a keep-alive request never
    run side by side, whichever comes first. ``hedge_s`` sends one duplicate
    of a read that has not answered by then and takes the first answer. Both
    are off unless configured.
    """
    def __init__(self, api_key: str, *, budget: float, model: str = MODEL,
                 hedge_s: float | None = None, keepwarm_s: float | None = None,
                 join_bound_s: float | None = None):
        self.budget = _budget(budget)
        self.client = JevClient(api_key=api_key, model=_model(model),
                                timeout=self.budget, max_retries=0)
        self.classifier = JevClassifier(client=self.client)
        self._pending = set()
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.hedge_s, self.keepwarm_s, self.join_bound_s = hedge_s, keepwarm_s, join_bound_s
        self.last_read = None
        self._keepwarm = None
        self._warming = None  # (request, its record) while a keep-alive request is in flight
        self._last_activity = 0.0
        # JevClient builds its own pool; its httpx client is the one place a
        # per-request trace can be attached.
        self._http = getattr(self.client, "_http", None)
        if self._http is not None:
            self._http.event_hooks["request"].append(self._trace_request)

    async def _trace_request(self, request):
        """Let httpcore report this request's connection events. Names and times only."""
        read = _READ.get()
        if read is None:
            return
        started = {}

        async def trace(name, info):
            now = asyncio.get_running_loop().time()
            stage, _, phase = name.rpartition(".")
            if phase == "started":
                started[stage] = now
                if stage == "connection.connect_tcp":
                    read["connection"] = "new"
                elif stage.endswith(".send_request_headers"):
                    read.setdefault("connection", "reused")
            elif phase == "complete" and stage in ("connection.connect_tcp", "connection.start_tls"):
                key = "connect_ms" if stage.endswith("tcp") else "tls_ms"
                read[key] = round((now - started.get(stage, now)) * 1000, 1)

        request.extensions["trace"] = trace

    def _start(self, operation, *args):
        if self._closed:
            raise RuntimeError("Addressee is closed")
        task = asyncio.create_task(operation(*args))
        self._pending.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self._pending.discard(task)
        self._last_activity = asyncio.get_running_loop().time()

    async def _request(self, operation, *args):
        task = self._start(operation, *args)
        try:
            # Direct awaiting propagates cancellation into the HTTP request and
            # does not return until that request's cancellation has completed.
            return await task
        finally:
            self._pending.discard(task)

    async def connect(self):
        try:
            async with asyncio.timeout(self.budget):
                await self._request(self.client.connect)
        except Exception as error:
            # Exception text can contain network details. Log the type only.
            logger.warning("Jev warm-up failed ({}); routing may fall back", type(error).__name__)
        if self.keepwarm_s and self._keepwarm is None and self._http is not None and not self._closed:
            self._keepwarm = asyncio.create_task(self._keep_warm())

    async def _keep_warm(self):
        """Send the cheapest request whenever the connection has been idle for keepwarm_s."""
        loop = asyncio.get_running_loop()
        while not self._closed:
            idle = loop.time() - self._last_activity
            if self._pending or idle < self.keepwarm_s:
                await asyncio.sleep(max(self.keepwarm_s - idle, .05) if not self._pending else self.keepwarm_s)
                continue
            read = {}
            token = _READ.set(read)
            try:
                request = self._start(self._http.get, "/v1/models")
            finally:
                _READ.reset(token)
            self._warming = (request, read)
            try:
                async with asyncio.timeout(self.budget):
                    await request
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # This loop is being stopped; close() joins the request.
                read["preempted"] = True  # A read arrived and took precedence.
            except Exception as error:
                read["error"] = type(error).__name__
            finally:
                self._warming = None
            logger.debug("Jev keep-warm {}", read)

    async def _make_way_for_read(self):
        """A read never runs beside a keep-alive request that started before it.

        A keep-alive request on an open connection is cancelled and joined:
        that costs no round trip and HTTP/2 keeps the connection. One that is
        opening a connection is opening the connection the read needs, so the
        read waits for it instead of tearing the handshake down.
        """
        if self._warming is None:
            return
        request, record = self._warming
        if record.get("connection") != "new":
            request.cancel()
        await asyncio.wait({request})

    async def ask(self, state: dict) -> ChoiceResult:
        """One uncached read. The caller owns the absolute routing deadline."""
        questions = {"addressee": addressee_question()}
        await self._make_way_for_read()
        read = {}
        token = _READ.set(read)
        try:
            if self.hedge_s is None:
                answers = await self._request(self.classifier.ask, state, questions)
            else:
                answers = await self._hedged(state, questions, read)
            return answers["addressee"]
        finally:
            _READ.reset(token)
            self.last_read = read
            logger.debug("Jev read {}", read)

    async def _hedged(self, state, questions, read):
        """The read, plus one duplicate if it has not answered after hedge_s.

        The first answer wins; an error is not an answer while the other
        request can still give one. The loser is cancelled and joined before
        this returns, for at most join_bound_s when that is set. Cancelling the
        caller cancels both requests and returns only when both have finished,
        also when the cancellation arrives during that bounded join.
        Nothing is hedged while another request is outstanding, so at most two
        are ever in flight.
        """
        tasks = [self._start(self.classifier.ask, state, questions)]
        won = False
        try:
            if not (await asyncio.wait(tasks, timeout=self.hedge_s))[0] and len(self._pending) == 1:
                read["hedged"] = True
                tasks.append(self._start(self.classifier.ask, state, questions))
            failure, waiting = None, set(tasks)
            while waiting:
                done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    if task in done and not task.cancelled() and task.exception() is None:
                        read["winner"] = "first" if task is tasks[0] else "hedge"
                        won = True
                        return task.result()
                for task in tasks:
                    if task in done and failure is None:
                        failure = asyncio.CancelledError() if task.cancelled() else task.exception()
            raise failure
        finally:
            await self._retire(tasks, self.join_bound_s if won else None)

    async def _retire(self, tasks, bound_s):
        """Cancel what is still running and wait for it, even if the caller is cancelled again."""
        tasks = [task for task in tasks if not task.done()]
        for task in tasks:
            task.cancel()
        loop = asyncio.get_running_loop()
        deadline = None if bound_s is None else loop.time() + bound_s
        cancelled = False
        while any(not task.done() for task in tasks):
            timeout = None if deadline is None else deadline - loop.time()
            if timeout is not None and timeout <= 0:
                break
            try:
                await asyncio.wait([task for task in tasks if not task.done()], timeout=timeout)
            except asyncio.CancelledError:
                # The caller is cancelled: it does not return before everything
                # has joined, whatever bound a winner's loser had. The caller's
                # owner supervises this wait.
                cancelled, deadline = True, None
        if cancelled:
            raise asyncio.CancelledError

    async def close(self):
        async with self._close_lock:
            self._closed = True
            pending = tuple(self._pending) + ((self._keepwarm,) if self._keepwarm else ())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.client.close()
