"""Angel & Devil: silent, fenced persona/voice switching over RTVI.

DUO_CONTEXT chooses how the model is told who speaks. b2h, the default, is in
duo_context.py: one prompt and a marker on each user message. labels is the
earlier design, all in this file: the prompt swap and note of ``_mutate``,
``CharacterContext`` and ``spoken_text``.

Runs on Pipecat 1.16. The file:line references below were taken from the
Pipecat 1.12 source (server/.venv/lib/python3.12/site-packages/pipecat/ at that
version) and were not renumbered since; the functions they name still exist.
No network connection is made on import; the coordinator launches this module.
"""

import asyncio
from collections import OrderedDict, deque
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import gc
import hashlib
import importlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
import weakref

from loguru import logger
from pipecat.observers.base_observer import BaseObserver
from pipecat.frames.frames import (
    CancelFrame, ControlFrame, EagerTranscriptionFrame, EndFrame, ErrorFrame, InterimTranscriptionFrame,
    InterruptionFrame, InterruptionWorkerFrame, LLMContextFrame,
    LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame,
    LLMMessagesAppendFrame, LLMRunFrame,
    LLMUpdateSettingsFrame, ManuallySwitchServiceFrame,
    ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame,
    TranscriptionFrame, TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyManual
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi.observer import RTVIObserverParams
from pipecat.services.deepgram.flux.tts import DeepgramFluxTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.turns.empty_user_turn import EmptyUserTurnConfig
from pipecat.utils.text.base_text_aggregator import Aggregation, TextType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from demo_interrupts import RTVIProcessor
from demo_policy import capability_aec, make_stt, turn_policy
import deepgram_think
from duo_context import TaggedContext, TaggedLLM, stamp
from duo_prompts import (INAUDIBLE, auto_note, character_instructions, duo_instructions, history_line,
                         silent_note, untagged)
from duo_addressee import (Addressee, DEFAULT_AUTO_SWITCH_FLOOR, DEFAULT_ROUTE_BUDGET_S, DEFAULT_JEV_MODEL,
                           addressee_question, configuration as route_configuration)
from duo_route import (DOWN, RESPONSE_FRAMES, HoldWake, RouteAdmitted, RouteCleanupOverdue, RouteStampingLLM,
                       RouteSuperseded, RouteTurns, TextHold, cancelled_output, join_retired)
from duo_switch import Request, SwitchState

SAMPLE_RATE = 16000
# What DUO_STT_KEYTERMS=1 asks Flux to listen for: live, "Angel" was heard as "Bangel" and "Dangel".
STT_KEYTERMS = ("Angel", "Devil")
# One device request plus the retransmissions a slow transaction can collect:
# dg_duo::Device::tick resends every two seconds, four times.
MAX_PENDING_REQUESTS = 8
# The LLM endpoint is never written here: it comes from the first of these that is set, and the bot
# refuses to start without one. The bench launcher's identity check (hil/duo_identity.py) accepts only
# this model, and an endpoint that came from one of these variables.
LLM_URL_VARIABLES = ("DUO_LLM_URL", "LOCAL_LLM_URL")
DEFAULT_LLM_MODEL = "phonellm-mini-alpha-2-nvfp4"
# DUO_CONTEXT, how the model is told who speaks. b2h: one prompt, a marker at the end of each user
# message, replies stored as spoken (duo_context.py). labels: a prompt per character swapped at
# each switch, a note in the conversation, replies stored under [Angel] / [Devil].
CONTEXTS = ("b2h", "labels")


@dataclass
class TransactionMarker(ControlFrame):
    # FrameProcessor interruption clearing reads this field directly:
    # processors/frame_processor.py:1132-1146 (and preserved queue entries).
    transaction: str
    interruptible: bool = field(default=False, init=False)


@dataclass
class SilentNote(LLMMessagesAppendFrame):
    interruptible: bool = field(default=False, init=False)


# Coordinator events. Pipeline callbacks only construct and enqueue one; the
# coordinator task alone acts on them. ``Deferred`` events are transactions:
# they run one at a time, in arrival order. The others are witnesses a running
# transaction is waiting for, so they are observed as soon as they arrive.
class Deferred:
    pass


@dataclass(frozen=True)
class ClientReady(Deferred):
    pass


@dataclass(frozen=True)
class DeviceRequest(Deferred):
    data: object


@dataclass(frozen=True)
class PipelineError(Deferred):
    frame: ErrorFrame


@dataclass(frozen=True)
class ResponseStarted(Deferred):
    owner: tuple  # (conn, rev, service, context_id)


@dataclass(frozen=True)
class ResponseCompleted(Deferred):
    owner: tuple


@dataclass(frozen=True)
class ResponseAbandoned(Deferred):
    owner: tuple


@dataclass(frozen=True)
class TurnStarted(Deferred):
    character: str


@dataclass(frozen=True)
class MarkerPassed:
    transaction: str


@dataclass(frozen=True)
class VoiceSwitched:
    transaction: str | None
    service: object


@dataclass(frozen=True)
class InterruptionPassed:
    transaction: str | None
    phase: str


@dataclass(frozen=True)
class ConnectionResult:
    attempt: asyncio.Future
    success: bool


@dataclass(frozen=True)
class SetupFailed:
    service: object


@dataclass(frozen=True)
class Pong:
    data: object


@dataclass(frozen=True)
class Shutdown:
    pass


class DuoTimingObserver(BaseObserver):
    """Read-only reply timing; all timestamps use the pipeline's monotonic clock.

    Synchronous notifications retain identities before shared history changes.
    Frame observations use source timestamps, not the observer queue's receipt
    time. No frame, route record, context or scheduling decision is modified.
    """
    def __init__(self, *, fence, llm, output, enabled):
        super().__init__(observe_every_push=True)
        self.fence, self.llm, self.output = fence, llm, output
        self.mode = "on" if enabled else "off"
        self.turns, self.runs, self.routes = OrderedDict(), OrderedDict(), OrderedDict()
        self.responses, self.ends = OrderedDict(), OrderedDict()
        self.first_texts, self.held_texts = OrderedDict(), OrderedDict()
        self.contexts, self.sentences = OrderedDict(), OrderedDict()
        self.service_starts = {}
        self.active_llm, self.output_context = None, None

    @staticmethod
    def _put(mapping, key, value, limit=128):
        mapping[key] = value
        while len(mapping) > limit:
            mapping.popitem(last=False)
        return value

    def now(self):
        return self.fence.get_clock().get_time() / 1_000_000

    def _turn(self, user):
        key = id(user)
        if key not in self.turns:
            self._put(self.turns, key, dict(user=user, end=None, route=None))
        return self.turns[key]

    def turn_ended(self, user):
        if user is not None:
            turn = self._turn(user)
            if turn["end"] is None:
                turn["end"] = self.now()
            for row in tuple(self.responses.values()):
                if row["turn"] is turn and row["A"] is not None:
                    self._emit(row)

    def admit(self, frame, *, rerun_user=None):
        if not isinstance(frame, LLMContextFrame):
            return
        user = next((m for m in reversed(frame.context.get_messages()) if m.get("role") == "user"), None)
        if user is None:
            return
        turn = self._turn(user)
        token = frame.metadata.get("duo-route")
        parent = turn["route"] if rerun_user is not None else None
        row = dict(turn=turn, fence=self.now(), parent=parent, token=token,
                   route_mode="rerun" if rerun_user is not None else "routed" if token else "unrouted",
                   J=None, W=None, decision="unrouted", p=None, target=None, result=None,
                   T1=None, S1=None, A=None, held_delay_ms=0, emitted=False, reply_id=None)
        if parent is not None:
            row["fence"] = parent["fence"]
        self._put(self.runs, frame.id, row)
        if token:
            turn["route"] = row
            self._put(self.routes, token, row)

    def route_result(self, token, target, reason, probability):
        row = self.routes.get(token)
        if row is not None:
            row["result"] = (reason or "switch", probability, target)

    def terminal(self, token, decision, deadline):
        row = self.routes.get(token)
        if row is None or row["J"] is not None:
            return
        row["J"] = self.now()
        if decision == "release" and asyncio.get_running_loop().time() >= deadline:
            result = ("timeout", 0, None)
        else:
            result = row["result"] or (decision, 0, None)
        row["decision"], row["p"], row["target"] = result

    def held(self, frame):
        if isinstance(frame, LLMTextFrame):
            self._put(self.held_texts, frame.id, self.now(), limit=512)

    def wake(self, token):
        row = self.routes.get(token)
        if row is not None and row["W"] is None:
            row["W"] = self.now()

    def service_started(self, service, frame):
        self.service_starts[service] = frame.id

    def context_created(self, service, context_id):
        self._put(self.contexts, context_id, self.service_starts.get(service))

    def sentence(self, context_id):
        if context_id not in self.sentences:
            self._put(self.sentences, context_id, self.now())

    def _context_row(self, context_id):
        row = self.responses.get(self.contexts.get(context_id))
        if row is not None and row["S1"] is None:
            row["S1"] = self.sentences.get(context_id)
        return row

    def _emit(self, row, *, force=False):
        if row["emitted"] or row["S1"] is None:
            return
        origin = row["turn"]["end"]
        if origin is None and not force:
            return
        route = row["parent"] or row
        def offset(value):
            return round(value - origin, 3) if value is not None and origin is not None else None
        payload = dict(mode=self.mode, route_mode=row["route_mode"], reply_id=row["reply_id"],
                       origin="aggregator-user-stopped" if origin is not None else "unobserved",
                       fence_ms=offset(row["fence"]), J_ms=offset(route["J"]), W_ms=offset(route["W"]),
                       decision=route["decision"], p=route["p"], target=route["target"],
                       T1_ms=offset(row["T1"]), S1_ms=offset(row["S1"]), A_ms=offset(row["A"]),
                       held_delay_ms=round(row["held_delay_ms"], 3),
                       hold_delayed=row["held_delay_ms"] > 0)
        row["emitted"] = True
        logger.info("DUO timing {}", json.dumps(payload, sort_keys=True, allow_nan=False))

    async def on_process_frame(self, data):
        if data.processor is self.llm and isinstance(data.frame, LLMContextFrame) and data.direction == DOWN:
            self.active_llm = self.runs.get(data.frame.id)

    async def on_push_frame(self, data):
        if data.direction != DOWN:
            return
        frame, timestamp = data.frame, data.timestamp / 1_000_000
        if data.source is self.llm:
            row = self.active_llm
            if isinstance(frame, LLMFullResponseStartFrame) and row is not None:
                row["reply_id"] = frame.id
                self._put(self.responses, frame.id, row)
            elif isinstance(frame, LLMTextFrame) and row is not None and row["T1"] is None:
                row["T1"] = timestamp
                self._put(self.first_texts, frame.id, row)
            elif isinstance(frame, LLMFullResponseEndFrame):
                if row is not None:
                    self._put(self.ends, frame.id, row)
                self.active_llm = None
        if isinstance(frame, LLMTextFrame) and data.source is not self.llm:
            # Only a captured text's push out of TextHold can accrue hold delay.
            row = self.first_texts.get(frame.id)
            captured = self.held_texts.get(frame.id)
            if row is not None and captured is not None and data.source is self.fence.coordinator.hold:
                row["held_delay_ms"] = max(0, timestamp - captured)
                self.held_texts.pop(frame.id, None)
        if data.source is not self.output:
            return
        if isinstance(frame, TTSStartedFrame):
            self.output_context = frame.context_id
        elif isinstance(frame, TTSAudioRawFrame) and frame.audio:
            row = self._context_row(frame.context_id or self.output_context)
            if row is not None and row["A"] is None:
                row["A"] = timestamp  # Output pushes PCM only after its successful write.
                self._emit(row)
        elif isinstance(frame, TTSStoppedFrame):
            self._context_row(frame.context_id)
            if self.output_context == frame.context_id:
                self.output_context = None
        elif isinstance(frame, LLMFullResponseEndFrame):
            row = self.ends.get(frame.id)
            if row is not None:
                self._emit(row, force=True)
        elif isinstance(frame, (InterruptionFrame, EndFrame, CancelFrame)):
            row = self._context_row(self.output_context)
            if row is not None:
                self._emit(row, force=True)
            self.output_context = None


class DuoServiceSwitcher(ServiceSwitcher):
    """Keep a withdrawn reply out of the voices.

    Pipecat 1.12 needed a second override here, because its ParallelPipeline
    dropped the returning leg of the worker's flush probe. Since 1.14 each leg
    of the probe is a frame of its own, and the stock switcher drains.
    """
    async def process_frame(self, frame, direction):
        # A hold push can have yielded before enqueue when cancellation arrives.
        # Recheck at the consumer boundary before creating any TTS context.
        if (direction == DOWN and isinstance(frame, RESPONSE_FRAMES) and
                cancelled_output(frame, settle_admitted=True)):
            return
        await super().process_frame(frame, direction)


class TurnGate(FrameProcessor):
    """Hold user events without blocking this processor's queue.

    Aggregator inference can originate in a controller task, independently of
    inbound frames (llm_response_universal.py:1367-1401). InferenceFence below
    catches that path too. Neither processor waits on the coordinator.
    """
    held_types = (TranscriptionFrame, InterimTranscriptionFrame, EagerTranscriptionFrame,
                  UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
                  VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
                  ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame)

    def __init__(self):
        super().__init__()
        self.closed = False
        self.held = deque()
        self.fence = InferenceFence(self)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self.closed and direction == DOWN and isinstance(frame, self.held_types):
            self.held.append((frame, direction))
            return
        await self.push_frame(frame, direction)

    def close(self):
        self.closed = True

    async def reopen(self):
        # Controller completions from an already-open turn precede buffered
        # future transcripts. No new user turn runs before verified commit.
        # Keep admission closed while draining: a push can yield, and newer
        # arrivals must not overtake the buffered frames. Recheck both queues
        # because an aggregator controller can complete during the release.
        while self.fence.held or self.held:
            while self.fence.held:
                frame, direction = self.fence.held.popleft()
                await self.fence.push_frame(frame, direction)
            if self.held:
                frame, direction = self.held.popleft()
                await self.push_frame(frame, direction)
        self.closed = False


class InferenceFence(FrameProcessor):
    def __init__(self, gate):
        super().__init__()
        self.gate = gate
        self.held = deque()
        self.coordinator = None
        self.inference_seq = 0
        self.timing = None
        self._route_in_process = None

    async def queue_frame(self, frame, direction=DOWN, callback=None):
        if self.coordinator and isinstance(frame, InterruptionFrame):
            # Retire before cancellation cleanup can yield to a late Jev result.
            # The coordinator and cancelled-context handler join the read.
            self.coordinator.context_cancelled(self._route_in_process)
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame, direction):
        wake = False
        closed_at_admission = None
        coordinator = self.coordinator
        if coordinator and isinstance(frame, (LLMContextFrame, LLMRunFrame)):
            closed_at_admission = self.gate.closed
            self.inference_seq += 1
            if (not closed_at_admission and direction == DOWN and
                    isinstance(frame, LLMContextFrame) and not frame.speculation):
                wake = coordinator.admit_route(frame, self.inference_seq)
        if self.timing and direction == DOWN:
            self.timing.admit(frame)
        held_at_admission = closed_at_admission and direction == DOWN
        if held_at_admission:
            # Reserve the closed-gate arrival before an observer can yield, so
            # a concurrent reopen cannot drain past an as-yet-unregistered hold.
            self.held.append((frame, direction))
        token = frame.metadata.get("duo-route") if coordinator else None
        if token is not None:
            self._route_in_process = token
        try:
            await super().process_frame(frame, direction)
            if wake:
                await coordinator.hold.queue_frame(HoldWake())
            if held_at_admission:
                return
            # Admission can precede a transaction's gate closure during an await.
            # Replay a newly held context through the original unrouted path.
            if (self.gate.closed and direction == DOWN and
                    isinstance(frame, (LLMContextFrame, LLMRunFrame))):
                if token is not None:
                    coordinator.context_cancelled(token)
                    if coordinator.hold.decision(token) == "quarantine":
                        # Its own auto claim already owns the replacement; a
                        # second held copy would answer the same turn twice.
                        return
                    frame.metadata.pop("duo-route", None)
                    frame.metadata.pop("duo-output", None)
                    if self.timing:
                        self.timing.admit(frame)
                self.held.append((frame, direction))
                return
            await self.push_frame(frame, direction)
        except asyncio.CancelledError:
            # A context can be cancelled here before it reaches the LLM queue.
            if coordinator:
                read = coordinator.context_cancelled(frame.metadata.get("duo-route"))
                if read is not None:
                    await join_retired((read,), coordinator.cancel_join_bound_s)
            raise
        finally:
            if token is not None and self._route_in_process == token:
                self._route_in_process = None


class CharacterContext(LLMContext):
    """Label at insertion, before the line enters shared history.

    The real assistant aggregator writes via add_message during interruption
    settlement (llm_response_universal.py:1856-1873, 1902-1904). The tail captures
    the speaking character per response before the aggregator starts that turn.
    """
    speaking_character = "angel"

    def add_message(self, message):
        if message.get("role") == "assistant" and isinstance(message.get("content"), str):
            # Pipecat (since 1.15) keeps the model's text here, not the text the voice
            # was given, so a label the model copied arrives with it. Left in,
            # it gets a second label below and the model copies both next time.
            spoken = SPOKEN_LABEL.sub("", message["content"])
            if not spoken.strip():
                return  # Labels only: nothing was said.
            message = dict(message, content=history_line(self.speaking_character, spoken))
        super().add_message(message)


class TailObserver(FrameProcessor):
    """Placed after the real output and assistant aggregator, before the sink."""
    def __init__(self, coordinator):
        super().__init__()
        self.coordinator = coordinator

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == DOWN:
            if isinstance(frame, TransactionMarker):
                self.coordinator.enqueue(MarkerPassed(frame.transaction))
            elif isinstance(frame, InterruptionFrame):
                self.coordinator.enqueue(InterruptionPassed(self.coordinator.transaction,
                                                            self.coordinator.phase))
            elif isinstance(frame, TTSStoppedFrame):
                owner = frame.metadata.get("duo-completed")
                if owner:
                    self.coordinator.enqueue(ResponseCompleted(owner))
            elif isinstance(frame, (EndFrame, CancelFrame)):
                self.coordinator.enqueue(Shutdown())
        await self.push_frame(frame, direction)


class SpeakingCharacter(FrameProcessor):
    """Output-order character capture, before the real assistant aggregator."""
    def __init__(self, context):
        super().__init__()
        self.context = context

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == DOWN and isinstance(frame, LLMFullResponseStartFrame):
            self.context.speaking_character = frame.metadata.get(
                "duo-character", self.context.speaking_character)
            if "duo-user" in frame.metadata:  # DUO_CONTEXT=b2h: the user message this reply answers.
                self.context.replying_to = frame.metadata["duo-user"]
        await self.push_frame(frame, direction)


SPOKEN_LABEL = re.compile(r"\[(?:Angel|Devil)\]\s*")
NOTE_MARKER = "Silent context"


async def spoken_text(text, text_type):
    """Keep context-only text out of speech, one aggregated sentence at a time.

    The local model copies the [Angel]/[Devil] history labels and, once, read
    a silent context note aloud (demo run, 2026-11-05). Labels are removed;
    a sentence that echoes a note is dropped. A TTS text transform: it changes
    only what is spoken. A dropped sentence never reaches the history, and
    CharacterContext removes the labels there.

    With DUO_TTS_FIRST_CLAUSE=1 a reply's first sentence can arrive in two
    pieces. FirstClause never cuts a piece off a sentence in which it has seen
    the marker, and ends the rest of a cut sentence at a marker that comes
    later, so no marker reaches this function in the second piece.
    """
    return "" if NOTE_MARKER in text else SPOKEN_LABEL.sub("", text)


class FirstClause(SimpleTextAggregator):
    """Sentences, except that a reply's first sentence may go out in two pieces.

    The voice then starts on the first clause while the model is still writing
    the rest of the sentence. Flux appends every Speak of a reply to one turn
    and takes the text as written, so the cut is not a break in the speech.

    The first piece ends before a single ASCII space, either after the first
    punctuation mark in BOUNDARIES with WORDS_AT_BOUNDARY spoken words before
    it, or after WORDS_WITHOUT_BOUNDARY words when no such mark came. A
    sentence the tokenizer ends first goes out whole, as before, however
    short. After the first piece of a reply, whichever it was, everything is a
    whole sentence (the rest of a cut sentence counts as one).

    A cut removes exactly that one space, and the service puts exactly one
    space back between the pieces for the voice and for the history, so both
    get the characters the uncut sentence gives them. Any other whitespace at
    the place (a second space, a tab, a newline, a no-break space) means no
    cut there. A full stop may be a boundary without asking the tokenizer for
    the same reason: after "Dr." the cut changes nothing that is said.

    Notes. spoken_text drops a sentence that echoes a note, whole. No cut is
    made while the text held contains the note marker or ends on its first
    word, so a note at the start of a reply's first sentence is still dropped
    whole. A first piece that has gone out is speech. If the rest of its
    sentence then turns out to hold the marker, the rest is given to the voice
    only as far as the marker, and the marker and what follows it in that
    sentence are dropped here: the voice and the history get the same text.
    """
    WORDS_AT_BOUNDARY = 4
    WORDS_WITHOUT_BOUNDARY = 8
    BOUNDARIES = ",;:\u2014\u2013.!?\u2026"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._first = True
        self._rest_due = False  # A first piece was cut off; the next aggregation is the rest of its sentence.

    async def aggregate(self, text):
        for char in text:
            self._text += char
            result = await self._check_sentence_with_lookahead(char)
            if result is None and self._first and not char.isspace():
                result = self._first_piece()
                self._rest_due = result is not None
            elif result is not None:
                result = self._rest(result)
            if result:
                self._first = False
                yield result

    def _first_piece(self):
        # Judged at the character after the space, so the space is known to be alone.
        piece = self._text[:-2]
        if self._text[-2:-1] != " " or not piece or piece[-1].isspace():
            return None
        if NOTE_MARKER in piece or piece.endswith(NOTE_MARKER.split()[0]):
            return None
        words = sum(any(c.isalnum() for c in word) for word in SPOKEN_LABEL.sub("", piece).split())
        needed = self.WORDS_AT_BOUNDARY if piece[-1] in self.BOUNDARIES else self.WORDS_WITHOUT_BOUNDARY
        if words < needed:
            return None
        self._text = self._text[len(piece):]
        # A sentence end waiting for its lookahead was in the piece and went with it.
        self._lookahead_state = type(self._lookahead_state).IDLE
        return Aggregation(text=piece.strip(" "), type=TextType.SENTENCE)

    def _rest(self, aggregation):
        """The rest of a cut sentence ends at a note marker; anything else passes as it is."""
        rest_due, self._rest_due = self._rest_due, False
        if not rest_due or NOTE_MARKER not in aggregation.text:
            return aggregation
        kept = aggregation.text[:aggregation.text.index(NOTE_MARKER)].strip(" ")
        return Aggregation(text=kept, type=aggregation.type) if kept.strip() else None

    async def flush(self):
        rest_due = self._rest_due  # The flush resets it.
        result = await super().flush()
        self._rest_due = rest_due and result is not None
        return self._rest(result) if result is not None else None

    # A reply ends in a flush or an interruption, and a flush with text held
    # resets. Text is always held once a piece has gone out: the space or the
    # lookahead character that released it stays behind.
    async def handle_interruption(self):
        await super().handle_interruption()
        self._first, self._rest_due = True, False

    async def reset(self):
        await super().reset()
        self._first, self._rest_due = True, False


class DuoFluxTTS(DeepgramFluxTTSService):
    """Correlate asynchronous Flux events with immutable connection/rev/context.

    Public context hook: services/tts_service.py:816-824, 882-888.
    Flux run_tts only sends Speak; provider SpeechMetadata emits completion:
    services/deepgram/flux/tts_base.py:330-361, 391-405.
    Only that provider completion marks a stop successful (the base TTS also
    creates timeout stops, services/tts_service.py:1880-1890).
    """
    def __init__(self, *args, first_clause=False, **kwargs):
        kwargs["reconnect_on_error"] = False
        super().__init__(*args, **kwargs)
        if first_clause:
            # TTSService (1.16) takes no aggregator argument; it builds this
            # attribute in __init__ and only replaces it for expressive
            # conversation, which this bot does not enable.
            if type(self._text_aggregator) is not SimpleTextAggregator or self._is_streaming_tokens:
                raise RuntimeError("DUO_TTS_FIRST_CLAUSE needs the sentence aggregator of Pipecat 1.16")
            self._text_aggregator = FirstClause(aggregation_type=self._text_aggregation_mode,
                                                language=self._text_aggregator.language)
        self.coordinator = None
        self.owners = {}
        self.provider_completed = set()
        self.provider_audio = set()
        self.response_owner = None
        self.connection_generation = 0
        self.connection_ready = False
        self.connecting = None
        self.connection_stopping = False
        self.initial_setup_complete = False
        self._pending_output = None
        self._output_owners = weakref.WeakValueDictionary()

    async def _connect_observed(self, operation):
        """Observe public events; Flux swallows handshake errors.

        tts.py:167-195 reports on_connected/on_connection_error. Event handlers
        enqueue their captured attempt future, never infer success from return.
        """
        attempt = dict(result=asyncio.get_running_loop().create_future(), failed=False)
        self.connecting = attempt
        async def connected(service):
            self.coordinator.enqueue(ConnectionResult(attempt["result"], True))
        async def failed(service, error):
            self.coordinator.enqueue(ConnectionResult(attempt["result"], False))
        self.add_event_handler("on_connected", connected)
        self.add_event_handler("on_connection_error", failed)
        try:
            await operation()
            self.connection_ready = await attempt["result"] and not attempt["failed"]
            return self.connection_ready
        finally:
            self.remove_event_handler("on_connected", connected)
            self.remove_event_handler("on_connection_error", failed)
            self.connecting = None

    async def setup(self, setup):
        try:
            async with asyncio.timeout(self.coordinator.retirement_timeout_secs):
                await self._connect_observed(lambda: super(DuoFluxTTS, self).setup(setup))
                if not self.connection_ready:
                    raise VoiceUnavailable("initial voice handshake failed")
        except (Exception, asyncio.CancelledError) as error:
            self.connection_ready = False
            await self.set_usable(False)
            await self._disconnect()
            if isinstance(error, asyncio.CancelledError):
                raise
            if self is self.coordinator.switcher.strategy.active_service:
                # BasePipeline catches setup exceptions, and ServiceSwitcher
                # remains usable while its other branch is healthy. Explicitly
                # stop this session rather than accepting a missing start voice.
                self.coordinator.enqueue(SetupFailed(self))
                raise
            # Only the starting voice is required. Its inactive peer has a
            # fully initialized processor and may recover off the startup path.
            self.initial_setup_complete = True
            self.coordinator.failed_voices.add(self)
            self.coordinator._retire_background(self)
            return
        self.initial_setup_complete = True
        await self.set_usable(self.connection_ready)

    async def _stop_retirement(self):
        # Fence new attempts before awaiting the currently owned jobs. The
        # service can see End/Cancel before the coordinator's tail observer.
        self.connection_stopping = True
        tasks = [mapping[self] for mapping in
                 (self.coordinator.retirement_retries, self.coordinator.retirements) if self in mapping]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.connection_ready = False
        await self.set_usable(False)

    async def stop(self, frame):
        await self._stop_retirement()
        await super().stop(frame)

    async def cancel(self, frame):
        await self._stop_retirement()
        await super().cancel(frame)

    async def retire(self):
        """Background only: await old receiver cancellation before new binding.

        tts.py:159-173 cancels and awaits its receiver before closing the socket.
        Already-emitted errors retain their stamped owner and generation; the
        cancelled receiver cannot relabel an error for a subsequent response.
        """
        if self.connection_stopping:
            return False
        self.connection_ready = False
        await self._disconnect()
        self.response_owner = None
        self.connection_generation += 1
        self.provider_completed.clear()
        self.provider_audio.clear()
        return await self._connect_observed(self._connect)

    async def process_frame(self, frame, direction):
        if direction == DOWN and isinstance(frame, LLMFullResponseStartFrame):
            frame.metadata["duo-character"] = self.coordinator.state.character
            self._pending_output = frame.metadata.get("duo-output")
            if getattr(self.coordinator, "timing", None):
                self.coordinator.timing.service_started(self, frame)
        if isinstance(frame, InterruptionFrame):
            self._pending_output = None
            if self.response_owner:
                self.coordinator.enqueue(ResponseAbandoned(self.response_owner))
                self.provider_audio.discard(self.response_owner[3])
                self.provider_completed.discard(self.response_owner[3])
                self.response_owner = None
        await super().process_frame(frame, direction)

    async def on_turn_context_created(self, context_id):
        await super().on_turn_context_created(context_id)
        if getattr(self.coordinator, "timing", None):
            self.coordinator.timing.context_created(self, context_id)
        state = self.coordinator.state
        owner = (state.conn, state.rev, self, context_id)
        output, self._pending_output = self._pending_output, None
        if output is not None:
            output.tts_owner = owner
            self._output_owners[context_id] = output
        self.response_owner = owner
        self.owners[context_id] = owner
        if len(self.owners) > 64:
            del self.owners[next(iter(self.owners))]
        self.coordinator.enqueue(ResponseStarted(owner))

    async def run_tts(self, text, context_id):
        output = self._output_owners.get(context_id)
        if output is not None:
            output.tts_started = True
        if getattr(self.coordinator, "timing", None):
            self.coordinator.timing.sentence(context_id)
        if self.coordinator.fail_next(self):
            error = ErrorFrame(error="DUO_FAULT tts-fail-next", processor=self)
            error.metadata["duo-owner"] = self.owners.get(context_id)
            yield error
            return
        async for frame in super().run_tts(text, context_id):
            if isinstance(frame, ErrorFrame):
                frame.metadata["duo-owner"] = self.owners.get(context_id)
            yield frame

    async def _handle_message(self, msg):
        # Provider adapter only: this is the same extension point used by the
        # demo's tts_connection_probe.py, with no private readiness reads.
        if msg.get("type") == "SpeechMetadata":
            context_id = self.get_active_audio_context_id()
            if context_id:
                self.provider_completed.add(context_id)
        await super()._handle_message(msg)

    async def _handle_audio(self, audio):
        context_id = self.get_active_audio_context_id()
        if context_id and audio:
            self.provider_audio.add(context_id)
        await super()._handle_audio(audio)

    async def push_error_frame(self, error, force_treat_as_permanent=False):
        if error.processor is None:
            error.processor = self
        if "duo-owner" not in error.metadata:
            error.metadata["duo-owner"] = self.response_owner
        error.metadata.setdefault("duo-generation", self.connection_generation)
        if (error.metadata["duo-generation"] == self.connection_generation and
                error.metadata["duo-owner"] in (None, self.response_owner)):
            self.connection_ready = False
        if self.connecting is not None:
            self.connecting["failed"] = True
        self.coordinator.enqueue(PipelineError(error))
        await super().push_error_frame(error, force_treat_as_permanent=force_treat_as_permanent)

    async def push_frame(self, frame, direction=DOWN):
        if isinstance(frame, TTSStoppedFrame):
            if frame.context_id in self.provider_completed and frame.context_id in self.provider_audio:
                frame.metadata["duo-completed"] = self.owners.get(frame.context_id)
            self.provider_completed.discard(frame.context_id)
            self.provider_audio.discard(frame.context_id)
        await super().push_frame(frame, direction)


def fault_config(environ):
    fault = environ.get("DUO_FAULT", "")
    if fault and environ.get("DUO_TEST") != "1":
        raise ValueError("DUO_FAULT requires DUO_TEST=1")
    if fault and fault != "tts-fail-next":
        if not fault.startswith("drop-state-") or not fault[11:].isdigit() or int(fault[11:]) < 1:
            raise ValueError("Unknown DUO_FAULT")
    return fault


class VoiceUnavailable(Exception):
    pass


class SwitchCoordinator(RouteTurns):
    """The only owner of transactions; callbacks only enqueue immutable events."""
    def __init__(self, *, llm, services, switcher, context, gate, state=None,
                 deadline_secs=3, fault="", event=None, addressee=None,
                 route_budget_s=DEFAULT_ROUTE_BUDGET_S, auto_switch_floor=DEFAULT_AUTO_SWITCH_FLOOR,
                 cancel_join_bound_s=None, vocative_rule=False, tags=False):
        self.llm, self.services, self.switcher = llm, services, switcher
        self.context, self.gate = context, gate
        # DUO_CONTEXT=b2h: a switch writes nothing into the conversation. The committed character
        # becomes the marker of the user message it answers (duo_context.py).
        self.tags = tags
        self.state = state or SwitchState()
        self.deadline_secs, self.fault = deadline_secs, fault
        self.event = event or (lambda kind, **data: logger.info("DUO {} {}", kind, data))
        self.queue = asyncio.Queue()
        self.deferred = deque()
        self.pending_requests = self.refused_requests = 0
        self.worker = None
        self.phase, self.transaction = "idle", None
        self.markers, self.switched = set(), set()
        self.mutation_interrupted = False
        self.stopping = False
        self.drop_states = int(fault[11:]) if fault.startswith("drop-state-") else 0
        self.drop_armed = False
        self.fault_used = False
        self.liveness = None
        self.interrupt_witnesses = set()
        self.error_seen = {}
        self.llm_invariants = llm_invariants(llm)
        self.retirements, self.retirement_retries = {}, {}
        self.failed_voices = set()
        self.retirement_timeout_secs = 3
        self.retirement_backoff_secs = .25
        self.operation_id = None
        self.request = None
        self.reverting = False
        self._init_routing(addressee=addressee, route_budget_s=route_budget_s,
                           auto_switch_floor=auto_switch_floor, cancel_join_bound_s=cancel_join_bound_s,
                           vocative_rule=vocative_rule)
        self.timing = None

    def enqueue(self, event):
        if isinstance(event, DeviceRequest):
            self.pending_requests += 1
        self.queue.put_nowait(event)

    def submit_request(self, data):
        """Queue a device request unless too many already wait.

        Only requests are bounded: witnesses, pongs and lifecycle events
        always enter. A refused request never reaches the switch state, so
        the device's retransmission of the same seq still runs, once.
        """
        if self.pending_requests >= MAX_PENDING_REQUESTS:
            self.refused_requests += 1
            if self.refused_requests & (self.refused_requests - 1) == 0:  # Log the 1st, 2nd, 4th, 8th...
                self.event("request-refused", pending=self.pending_requests, refused=self.refused_requests)
            return False
        self.enqueue(DeviceRequest(data))
        return True

    def attach(self, worker):
        self.worker = worker

        @worker.rtvi.event_handler("on_client_ready")
        async def ready(rtvi):
            self.enqueue(ClientReady())

        @worker.rtvi.event_handler("on_client_message")
        async def message(rtvi, message):
            if message.type == "duo":
                self.submit_request(message.data)

        @self.switcher.strategy.event_handler("on_service_switched")
        async def switched(strategy, service):
            self.enqueue(VoiceSwitched(self.transaction, service))

        @worker.event_handler("on_pipeline_error")
        async def error(worker, frame):
            self.enqueue(PipelineError(frame))

    def fail_next(self, service):
        record = self.state.verification
        if (self.fault == "tts-fail-next" and not self.fault_used and record and
                record.service is service and self.state.adopted and record.request.op == "toggle"):
            self.fault_used = True
            return True
        return False

    def _observe(self, event):
        """Act on a witness now; keep a transaction for the main loop, in order."""
        match event:
            case MarkerPassed(transaction):
                if transaction == self.transaction:
                    self.markers.add(transaction)
            case VoiceSwitched(transaction, service):
                self.event("strategy-switched", transaction=transaction, service=service.name)
                if transaction == self.transaction:
                    self.switched.add((transaction, service))
            case InterruptionPassed(transaction, phase):
                if (transaction, phase) == (self.transaction, "mutate"):
                    self.mutation_interrupted = True
                elif (transaction, phase) == (self.transaction, "interrupt"):
                    self.interrupt_witnesses.add(self.transaction)
            case Shutdown():
                self.stopping = True
                if self.route_read:
                    self.route_read[1].cancel()
            case SetupFailed(service):
                self.gate.close()
                self.stopping = True
                self.event("starting-voice-unavailable", service=service.name)
            case Pong(data):
                if self.liveness:
                    self.liveness.pong(data)
            case ConnectionResult(attempt, success):
                if not attempt.done():
                    attempt.set_result(success)
            case RouteSuperseded(token):
                if self.route_read and token == self.route_read[0]:
                    self._cancel_route("superseded")
            case RouteCleanupOverdue(token):
                self.route_cleanup_overdue(token)
            case DeviceRequest():
                # A device request preempts the read it arrives during.
                if self.route_read:
                    self._cancel_route("preempt")
                self.deferred.append(event)
            case Deferred() | RouteAdmitted():
                self.deferred.append(event)
            case _:
                # A programming error, and fatal in every phase: with admission
                # closed and the coordinator stopping, a transaction that
                # catches this ends the pipeline instead of rolling back.
                self.gate.close()
                self.stopping = True
                raise TypeError(f"Not a coordinator event: {type(event).__name__}")

    def _drain(self):
        while not self.queue.empty():
            self._observe(self.queue.get_nowait())

    async def _wait_with_events(self, operation):
        """Keep callbacks enqueue-only without starving watchdog pongs."""
        pending = asyncio.ensure_future(operation)
        receive = None
        try:
            while not pending.done():
                receive = asyncio.create_task(self.queue.get())
                done, _ = await asyncio.wait((pending, receive), return_when=asyncio.FIRST_COMPLETED)
                if receive in done:
                    # Consumed before it is observed: an observation that raises is not repeated below.
                    event, receive = receive.result(), None
                    self._observe(event)
                else:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    receive = None
            return await pending
        finally:
            try:
                # Cancellation can land after get() removed an item but before the
                # wait returned. Preserve that event rather than silently losing it.
                if receive is not None and receive.done() and not receive.cancelled():
                    self._observe(receive.result())
            finally:
                # Whatever the observation did, the operation is still owned here.
                tasks = [task for task in (pending, receive) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _timed(self, phase, operation):
        self.phase = phase
        started = asyncio.get_running_loop().time()
        success = False
        try:
            result = await operation
            success = True
            return result
        finally:
            self.event("phase-timing", phase={"mutate": "mutation", "retirement-wait": "retirement_wait"}.get(phase, phase),
                       operation=self.operation_id, transaction=self.transaction, conn=self.state.conn,
                       rev=self.state.rev, session=self.request.session if self.request else None,
                       seq=self.request.seq if self.request else None, reverting=self.reverting,
                       elapsed_ms=(asyncio.get_running_loop().time() - started) * 1000,
                       success=success)

    async def _retire_once(self, service):
        if service.connection_stopping:
            return False
        await service.set_usable(False)
        try:
            async with asyncio.timeout(self.retirement_timeout_secs):
                if not await service.retire():
                    raise VoiceUnavailable("retirement handshake failed")
            self.failed_voices.discard(service)
            await service.set_usable(True)
            self.event("voice-retired", service=service.name, generation=service.connection_generation)
            return True
        except (Exception, asyncio.CancelledError) as error:
            self.failed_voices.add(service)
            service.connection_ready = False
            await service.set_usable(False)
            self.event("voice-retirement-failed", service=service.name, error=type(error).__name__)
            if isinstance(error, asyncio.CancelledError):
                raise
            return False

    async def _retry_retirement(self, service, attempt):
        backoff = self.retirement_backoff_secs
        while not self.stopping and not service.connection_stopping:
            succeeded = await attempt
            if succeeded and service.connection_ready and service.is_usable and service not in self.failed_voices:
                return
            self.event("voice-retry", service=service.name, backoff_secs=backoff)
            await asyncio.sleep(backoff)
            if self.stopping or service.connection_stopping:
                return
            attempt = asyncio.create_task(self._retire_once(service))
            self.retirements[service] = attempt
            backoff = min(backoff * 2, 4)

    def _retire_background(self, service, *, idle_recovery=False):
        if self.stopping or service.connection_stopping or not service.initial_setup_complete or (service is self.switcher.strategy.active_service and not idle_recovery):
            return
        if idle_recovery:
            assert self.gate.closed and service.response_owner is None
        retry = self.retirement_retries.get(service)
        if retry is not None and not retry.done():
            return
        attempt = asyncio.create_task(self._retire_once(service))
        self.retirements[service] = attempt
        self.retirement_retries[service] = asyncio.create_task(self._retry_retirement(service, attempt))

    async def _voice(self, service):
        if service in self.failed_voices:
            raise VoiceUnavailable("voice retirement failed")
        attempt = self.retirements.get(service)
        if attempt is not None:
            # A transaction timeout must not cancel the background handshake.
            if not await self._wait_with_events(asyncio.shield(attempt)):
                raise VoiceUnavailable("voice retirement failed")
        if not service.is_usable or not service.connection_ready:
            raise VoiceUnavailable("voice is unavailable")

    async def _end_pipeline(self):
        self.gate.close()
        self.stopping = True
        tasks = [*self.retirement_retries.values(), *self.retirements.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.worker.cancel()

    async def _recover_idle_voice(self, service):
        # An idle active voice can fail too. Fence new turns while its ordinary
        # background retry job runs; this is recovery, never a switch/rollback
        # handshake. The same absolute budget bounds admission and release.
        self.operation_id = uuid4().hex
        self.request = None
        self.reverting = False
        self.gate.close()
        deadline = asyncio.get_running_loop().time() + self.deadline_secs
        try:
            async with asyncio.timeout_at(deadline):
                await self._timed("interrupt", self._interrupt())
                self._retire_background(service, idle_recovery=True)
                await self._timed("retirement-wait", self._wait_with_events(
                    asyncio.shield(self.retirement_retries[service])))
                if not service.is_usable or not service.connection_ready:
                    raise VoiceUnavailable("idle voice recovery failed")
                await self._timed("publish", self._wait_with_events(self.gate.reopen()))
            self.phase, self.transaction = "idle", None
        except (Exception, asyncio.CancelledError):
            await self._end_pipeline()

    async def _interrupt(self):
        self.phase = "interrupt"
        self.transaction = uuid4().hex
        transaction = self.transaction
        self.interrupt_witnesses.clear()
        await self.worker.queue_frame(InterruptionWorkerFrame())
        # worker.py:922-968: no-progress timeout, NOT an overall deadline.
        # The caller always wraps this in an absolute asyncio.timeout_at.
        if not await self._wait_with_events(self.worker.flush_pipeline(timeout=self.deadline_secs)):
            raise TimeoutError("interruption did not drain")
        self._drain()
        while transaction not in self.interrupt_witnesses:
            self._observe(await self.queue.get())
            if self.stopping:
                raise RuntimeError("pipeline stopped")
        # A delayed upstream worker leg can let the first flush overtake the
        # interruption. The tail witness is after old-TTS/history settlement;
        # this second round trip also settles output-transport cancellation.
        if not await self._wait_with_events(self.worker.flush_pipeline(timeout=self.deadline_secs)):
            raise TimeoutError("post-interruption drain failed")
        self._drain()
        if self.stopping:
            raise RuntimeError("pipeline stopped")

    async def _mutate(self, outgoing, incoming, *, reverting=False, note=None):
        self.phase = "mutate"
        self.transaction = uuid4().hex
        transaction = self.transaction
        self.markers.clear()
        self.switched.clear()
        self.mutation_interrupted = False
        target = self.services[incoming]
        needed = self.switcher.strategy.active_service is not target
        if self.tags:
            # The voice is all that changes hands. The one prompt stays, and nothing is added to the conversation.
            note, instruction = None, duo_instructions()
        else:
            note = dict(role="system", content=(silent_note(outgoing, incoming, reverting=reverting)
                                               if note is None else note))
            instruction = character_instructions(incoming)
            await self.worker.queue_frame(LLMUpdateSettingsFrame(
                delta=OpenAILLMService.Settings(system_instruction=instruction,
                                                **deepcopy(self.llm_invariants)),
                service=self.llm))
        if needed:
            await self.worker.queue_frame(ManuallySwitchServiceFrame(service=target))
        if note is not None:
            await self.worker.queue_frame(SilentNote(messages=[note], run_llm=False))
        await self.worker.queue_frame(TransactionMarker(transaction))
        while transaction not in self.markers or (needed and (transaction, target) not in self.switched):
            self._observe(await self.queue.get())
            if self.mutation_interrupted:
                raise RuntimeError("mutation interrupted")
            if self.stopping:
                raise RuntimeError("pipeline stopped")
        self._drain()
        # Every mutation is read back; an uninterruptible marker alone proves
        # nothing about the interruptible manual switch. Public readers:
        # services/ai_service.py:61-69; service_switcher.py:78-87, 359-361.
        if (self.mutation_interrupted or
                self.llm.settings.system_instruction != instruction or
                llm_invariants(self.llm) != self.llm_invariants or
                self.switcher.strategy.active_service is not target or not target.is_usable or
                not target.connection_ready or
                (note is not None and not any(message is note for message in self.context.get_messages()))):
            raise RuntimeError("mutation verification failed")
        self.event("verified", transaction=transaction, character=incoming, reverting=reverting)
        self.markers.discard(transaction)
        self.switched.discard((transaction, target))
        return note

    async def _restore(self, outgoing, incoming, *, return_note=False):
        # Cleanup has a fresh absolute budget. Gate stays closed on failure.
        if self.stopping or self.worker.has_finished():
            await self._end_pipeline()
            return None
        try:
            self.reverting = True
            deadline = asyncio.get_running_loop().time() + self.deadline_secs
            async with asyncio.timeout_at(deadline):
                await self._timed("interrupt", self._interrupt())
                await self._timed("retirement-wait", self._voice(self.services[incoming]))
                note = await self._timed("mutate", self._mutate(outgoing, incoming, reverting=True))
            return (deadline, note) if return_note else deadline
        except (Exception, asyncio.CancelledError) as error:
            self.event("rollback-failed", error=type(error).__name__)
            await self._end_pipeline()
            return None

    async def _publish(self, result):
        try:
            self.event("outcome", **result)
            if result["op"] == "state" and self.drop_armed and self.drop_states:
                self.drop_states -= 1
                self.event("dropped-state", seq=result["seq"], remaining=self.drop_states)
                return
            await self.worker.rtvi.send_server_message(result)
        except (Exception, asyncio.CancelledError):
            await self._end_pipeline()
            raise

    async def _release(self, result, deadline):
        # Publication and release remain owned cleanup phases. A failed send
        # or cancellation cannot strand a live pipeline behind a closed gate.
        try:
            async with asyncio.timeout_at(deadline):
                await self._timed("publish", self._deliver(result))
            self.phase, self.transaction = "idle", None
        except (Exception, asyncio.CancelledError):
            await self._end_pipeline()
            raise

    async def _deliver(self, result):
        await self._wait_with_events(self._publish(result))
        await self._wait_with_events(self.gate.reopen())

    async def _request(self, data):
        deadline = asyncio.get_running_loop().time() + self.deadline_secs
        self.operation_id = uuid4().hex
        self.reverting = False
        try:
            request = Request.parse(data)
        except (ValueError, TypeError):
            self.event("invalid-request")
            return
        self.request = request
        replay = self.state.admit(request)
        if replay:
            await self._release(replay, deadline)
            return
        target = self.state.target(request)
        if target is None:
            await self._release(self.state.remember(request, self.state.outcome(request)), deadline)
            return
        old = self.state.character
        self.gate.close()
        target_service = self.services[target]
        pending_retirement = self.retirements.get(target_service)
        if (target_service in self.failed_voices or
                (not target_service.is_usable and pending_retirement is None)):
            result = self.state.failed(request, "voice")
            await self._release(result, deadline)
            return
        cancelled = False
        committed = False
        try:
            async with asyncio.timeout_at(deadline):
                await self._timed("retirement-wait", self._voice(target_service))
                await self._timed("interrupt", self._interrupt())
                await self._timed("mutate", self._mutate(old, target))
                result = self.state.commit(request, target, target_service)
                committed = True
                if old != target:
                    self._retire_background(self.services[old])
                if request.op == "toggle":
                    self.drop_armed = True
                await self._release(result, deadline)
        except (Exception, asyncio.CancelledError) as error:
            if committed:
                await self._end_pipeline()
                return
            cancelled = isinstance(error, asyncio.CancelledError)
            voice_failure = isinstance(error, VoiceUnavailable) or self.phase == "retirement-wait"
            rollback_deadline = await self._restore(target, old)
            if rollback_deadline is None:
                return
            self._retire_background(target_service)
            reason = "cancelled" if cancelled else "voice" if voice_failure else "deadline" if isinstance(error, TimeoutError) else "switch-failed"
            result = self.state.failed(request, reason)
            await self._release(result, rollback_deadline)
        if cancelled:
            raise asyncio.CancelledError

    async def _route_turn(self, route):
        claim = await self._decide_route(route)
        if claim is None:
            return
        target, service = claim
        snapshot = route.snapshot
        old = self.state.character
        self.operation_id, self.reverting = uuid4().hex, False
        self.request = None
        deadline = asyncio.get_running_loop().time() + self.deadline_secs
        committed = False
        try:
            async with asyncio.timeout_at(deadline):
                await self._timed("retirement-wait", self._voice(service))
                await self._timed("interrupt", self._interrupt())
                note = await self._timed("mutate", self._mutate(
                    old, target, note=None if self.tags else auto_note(old, target)))
                outcome = self.state.commit_auto(target, service)
                committed = True
                if self.tags and not self.context.answered(snapshot["user_object"]):
                    # The turn that asked for the switch is the new character's from here on,
                    # also if a later turn takes its place before it is run again.
                    stamp([snapshot["user_object"]], target)
                self._queue_rerun(snapshot, note)
                self._retire_background(self.services[old])
                await self._release(outcome, deadline)
        except (Exception, asyncio.CancelledError) as error:
            if committed:
                await self._end_pipeline()
                return
            cancelled = isinstance(error, asyncio.CancelledError)
            restored = await self._restore(target, old, return_note=True)
            if restored is None:
                return
            rollback_deadline, note = restored
            self._queue_rerun(snapshot, note)
            self._retire_background(service)
            try:
                async with asyncio.timeout_at(rollback_deadline):
                    await self._wait_with_events(self.gate.reopen())
                self.phase, self.transaction = "idle", None
            except (Exception, asyncio.CancelledError):
                await self._end_pipeline()
                raise
            self.event("route-rollback", error=type(error).__name__)
            if cancelled:
                raise

    def _unrouted(self, latest):
        # DUO_CONTEXT=b2h: a request that brings no words of the user's has nobody to be addressed
        # to. That is the turn stored for speech that was not understood, and any request whose
        # newest user message already has its reply. No read, no hold: it goes out like a turn
        # with routing off, and the committed character answers (or the LLM withdraws it).
        if not self.tags or not (self.context.unheard(latest) or self.context.answered(latest)):
            return False
        self.event("route-skipped", reason="inaudible" if self.context.unheard(latest) else "answered-turn")
        return True

    def _history(self, messages):
        if self.tags:
            return self.context.labelled_history(messages)
        return super()._history(messages)

    async def _device_request(self, data):
        request = None
        try:
            parsed = Request.parse(data)
            if parsed.op == "hello":
                request = parsed
        except (ValueError, TypeError):
            pass
        session = self.state.sessions.get(request.session) if request else None
        high_water = session.high_water if session else 0
        await self._request(data)
        if request:
            session = self.state.sessions.get(request.session)
            if session and session.high_water > high_water:
                self.state.note_admitted(request.session)

    async def _error(self, frame):
        if frame.id in self.error_seen:
            return
        self.error_seen[frame.id] = True
        if len(self.error_seen) > 64:
            del self.error_seen[next(iter(self.error_seen))]
        owner = frame.metadata.get("duo-owner")
        service = frame.processor
        generation = frame.metadata.get("duo-generation")
        if service not in self.services.values() or generation != service.connection_generation:
            self.event("ignored-error", error=frame.error)
            return
        if not service.initial_setup_complete:
            # setup owns the first connection attempt. Starting a retirement
            # here would race its captured handlers and partially built worker.
            self.failed_voices.add(service)
            await service.set_usable(False)
            return
        if not owner or not self.state.matches(*owner):
            # Completion can be queued before a socket error that was already
            # stamped with the just-finished response. Once completion clears
            # response_owner, that current-revision idle failure still needs
            # recovery. Older responses/revisions remain stale.
            completed_idle = (service is self.switcher.strategy.active_service and
                              service.response_owner is None and owner is not None and
                              owner[:2] == (self.state.conn, self.state.rev) and
                              owner == next(reversed(service.owners.values()), None))
            if owner is not None and owner != service.response_owner and not completed_idle:
                self.event("ignored-error", error=frame.error)
                return
            self.failed_voices.add(service)
            await service.set_usable(False)
            if service is self.switcher.strategy.active_service:
                await self._recover_idle_voice(service)
            else:
                self._retire_background(service)
            return
        record = self.state.verification
        self.operation_id = uuid4().hex
        self.request = record.request
        self.gate.close()
        self.failed_voices.add(service)
        await service.set_usable(False)
        rollback_deadline = await self._restore(self.state.character, record.old_character)
        if rollback_deadline is None:
            return
        self._retire_background(service)
        await self._release(self.state.revert(record), rollback_deadline)

    async def run(self):
        try:
            await self._run()
        except (Exception, asyncio.CancelledError) as error:
            self.gate.close()
            self.stopping = True
            try:
                self.event("coordinator-exit", error=type(error).__name__)
            finally:
                await self._end_pipeline()
            raise
        finally:
            await self._end_pipeline()

    async def _run(self):
        while not self.stopping:
            event = self.deferred.popleft() if self.deferred else await self.queue.get()
            match event:
                case DeviceRequest(data):
                    self.pending_requests -= 1
                    if self.addressee:
                        await self._device_request(data)
                    else:
                        await self._request(data)
                case RouteAdmitted():
                    await self._route_turn(event)
                case PipelineError(frame):
                    await self._error(frame)
                case ClientReady():
                    await self.worker.rtvi.set_bot_ready()
                    if self.liveness:
                        self.liveness.start()
                case ResponseStarted(owner):
                    self.state.bind_response(*owner)
                case ResponseCompleted(owner):
                    self.state.completed(*owner)
                    service = owner[2]
                    if service.response_owner == owner:
                        service.response_owner = None
                case ResponseAbandoned(owner):
                    self.state.abandoned(*owner)
                case TurnStarted(character):
                    self.event("assistant-turn-started", character=character)
                case Deferred():
                    raise TypeError(f"Unhandled coordinator event: {type(event).__name__}")
                case _:
                    self._observe(event)


def configuration(environ=None):
    if version("pipecat-ai") != "1.16.0":
        raise RuntimeError("Duo compatibility adapters require pipecat-ai 1.16.0")
    env = os.environ if environ is None else environ
    temperature = float(env.get("DUO_LLM_TEMPERATURE", "0"))
    thinking = env.get("DUO_LLM_ENABLE_THINKING", "false").lower()
    if temperature != 0 or thinking != "false":
        raise ValueError("Duo requires temperature=0 and enable_thinking=false")
    flag = env.get("DUO_AUTO_SWITCH", "0")
    if flag not in ("0", "1"):
        raise ValueError("DUO_AUTO_SWITCH must be 0 or 1")
    timing = env.get("DUO_TIMING", "")
    if timing not in ("", "0", "1"):
        raise ValueError("DUO_TIMING must be 0 or 1")
    join_ms = env.get("DUO_CANCEL_JOIN_MS", "")
    cancel_join_bound_s = None  # Unset: an interruption waits for the cancelled Jev request without limit.
    if join_ms:
        cancel_join_bound_s = float(join_ms) / 1000
        if not math.isfinite(cancel_join_bound_s) or not 0 <= cancel_join_bound_s <= 1:
            raise ValueError("DUO_CANCEL_JOIN_MS must be between 0 and 1000")
    routing = route_configuration(env)
    keyterms = env.get("DUO_STT_KEYTERMS", "0")
    if keyterms not in ("0", "1"):
        raise ValueError("DUO_STT_KEYTERMS must be 0 or 1")
    for name in ("DUO_GC_LOG", "DUO_GC_FREEZE", "DUO_VOCATIVE_RULE"):
        if env.get(name, "0") not in ("0", "1"):
            raise ValueError(f"{name} must be 0 or 1")
    first_clause = env.get("DUO_TTS_FIRST_CLAUSE", "1")
    if first_clause not in ("0", "1"):
        raise ValueError("DUO_TTS_FIRST_CLAUSE must be 0 or 1")
    context = env.get("DUO_CONTEXT", "b2h")
    if context not in CONTEXTS:
        raise ValueError("DUO_CONTEXT must be b2h or labels")
    keepwarm, hedge = env.get("DUO_JEV_KEEPWARM_S", ""), env.get("DUO_JEV_HEDGE_MS", "")
    jev_keepwarm_s = jev_hedge_s = None  # Unset: no keep-alive request, no duplicate read.
    if keepwarm:
        jev_keepwarm_s = float(keepwarm)
        if not math.isfinite(jev_keepwarm_s) or not 1 <= jev_keepwarm_s <= 120:
            raise ValueError("DUO_JEV_KEEPWARM_S must be between 1 and 120")
    if hedge:
        jev_hedge_s = float(hedge) / 1000
        if not math.isfinite(jev_hedge_s) or not 0 < jev_hedge_s < routing["route_budget_s"]:
            raise ValueError("DUO_JEV_HEDGE_MS must be positive and below the route budget")
    llm_url_source = next((name for name in LLM_URL_VARIABLES if env.get(name)), None)
    return dict(llm_url=env[llm_url_source] if llm_url_source else None, llm_url_source=llm_url_source,
                llm_model=env.get("DUO_LLM_MODEL", env.get("LOCAL_LLM_MODEL", DEFAULT_LLM_MODEL)),
                temperature=temperature, enable_thinking=False,
                voices={"angel": env.get("DUO_ANGEL_VOICE", "flux-hannah-en"),
                        "devil": env.get("DUO_DEVIL_VOICE", "flux-rufus-en")},
                stt_model="flux-general-en", turns="provider", sample_rate=SAMPLE_RATE,
                idle_timeout_secs=None, tts_reconnect_on_error=False,
                duo_test=env.get("DUO_TEST") == "1", fault=fault_config(env),
                auto_switch=flag == "1", timing=timing == "1" if timing else None,
                cancel_join_bound_s=cancel_join_bound_s,
                jev_keepwarm_s=jev_keepwarm_s, jev_hedge_s=jev_hedge_s,
                stt_keyterms=list(STT_KEYTERMS) if keyterms == "1" else None,
                gc_log=env.get("DUO_GC_LOG", "0") == "1", gc_freeze=env.get("DUO_GC_FREEZE", "0") == "1",
                vocative_rule=env.get("DUO_VOCATIVE_RULE", "0") == "1",
                tts_first_clause=first_clause == "1", context=context,
                **routing)


def require_llm_url(config):
    """The configured LLM endpoint, or a ValueError that names the variable to set.

    configuration() does not raise for a missing endpoint, because tools that never
    call the LLM read it too (duo_voice_samples.py). Whatever starts a bot or builds
    its LLM service calls this. The messages never contain the value.
    """
    url, source = config.get("llm_url"), config.get("llm_url_source")
    if not url:
        raise ValueError("DUO_LLM_URL is not set: export DUO_LLM_URL=http://<host>:<port>/v1, the "
                         "OpenAI-compatible endpoint of the duo LLM (LOCAL_LLM_URL is read as a fallback). "
                         "There is no default.")
    try:
        parts = urlsplit(url)
        parts.port  # Raises for a port that is not a number.
        usable = parts.scheme in ("http", "https") and bool(parts.hostname)
        plain = parts.username is None and parts.password is None
    except ValueError:
        usable, plain = False, True
    if not usable:
        raise ValueError(f"{source or 'DUO_LLM_URL'} must be an http(s) URL such as http://<host>:<port>/v1")
    if not plain:
        # The URL is written to the identity record; a credential must not travel with it.
        raise ValueError(f"{source or 'DUO_LLM_URL'} must not contain a user name or password")
    return url


def llm_invariants(llm):
    return {key: deepcopy(getattr(llm.settings, key))
            for key in ("model", "temperature", "max_completion_tokens", "extra")}


def timing_enabled(config):
    """Reply timing rows follow auto-switch unless DUO_TIMING says otherwise.

    A flag-off session installs no observer by default. DUO_TIMING=1 keeps the
    ``mode: off`` rows available as the baseline for a latency comparison.
    """
    explicit = config.get("timing")
    return bool(config.get("auto_switch")) if explicit is None else explicit


def effective_configuration(config, *, has_typesafe_key):
    config = dict(config, auto_switch_requested=config["auto_switch"])
    if config["auto_switch"] and not has_typesafe_key:
        config.update(auto_switch=False, auto_switch_reason="no-key")
        logger.warning("DUO auto-switch disabled: no TypeSafe key")
    return config


def tags_enabled(config):
    return config.get("context") == "b2h"


def make_llm(config, service_class=OpenAILLMService):
    tags = tags_enabled(config)
    url = require_llm_url(config)
    api_key = "not-required"
    if deepgram_think.is_think_url(url) and service_class is OpenAILLMService:
        # Deepgram's hosted PhoneLLM: same settings, adapted on the wire.
        api_key = os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DUO_LLM_URL is Deepgram's hosted PhoneLLM; set DEEPGRAM_API_KEY")
        service_class = deepgram_think.DeepgramThinkLLMService
    if tags:
        # Inside the route stamp: a frame is stamped before the marker filter sees it, and the
        # filter copies that stamp to text it held back and releases late.
        service_class = type(f"Tagged{service_class.__name__}", (TaggedLLM, service_class), {})
    if config.get("auto_switch"):
        service_class = type(f"Routed{service_class.__name__}", (RouteStampingLLM, service_class), {})
    return service_class(base_url=url, api_key=api_key,
        settings=OpenAILLMService.Settings(model=config["llm_model"], temperature=config["temperature"],
            system_instruction=duo_instructions() if tags else character_instructions("angel"),
            max_completion_tokens=250,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": config["enable_thinking"]}}}))


def make_stt_for(config, api_key):
    """Flux for the duo pipeline: the demo profile's service, plus the character names when asked."""
    if not config.get("stt_keyterms"):
        return make_stt(api_key, profile="demo")
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
    return DeepgramFluxSTTService(api_key=api_key, sample_rate=SAMPLE_RATE,
                                  settings=DeepgramFluxSTTService.Settings(keyterm=list(config["stt_keyterms"])))


def make_tts(config):
    return {character: DuoFluxTTS(api_key=os.environ["DEEPGRAM_API_KEY"], sample_rate=SAMPLE_RATE,
        reconnect_on_error=False,
        text_aggregation_mode=TextAggregationMode.SENTENCE,
        # DUO_CONTEXT=b2h has no labels or notes to keep out of speech, and its LLM removes a copied
        # marker at the source. With no transform here the device is told exactly what is spoken.
        text_transforms=[] if tags_enabled(config) else [("*", spoken_text)],
        first_clause=bool(config.get("tts_first_clause")),
        settings=DeepgramFluxTTSService.Settings(voice=voice)) for character, voice in config["voices"].items()}


# Every local module the bot imports, at module level or inside bot(). A test
# derives this set from the imports, and hil/duo_identity.py must list the same.
IDENTITY_SOURCES = ("duo_bot.py", "duo_route.py", "duo_context.py", "duo_prompts.py", "duo_switch.py",
                    "duo_addressee.py", "demo_policy.py", "demo_interrupts.py", "demo_liveness.py",
                    "sctp_flight_fix.py", "settings.py", "aec_session_recorder.py", "deepgram_think.py",
                    "ice_path.py")


def identity(config=None):
    config = configuration() if config is None else config
    payload = dict(config=config, versions={name: version(name) for name in ("pipecat-ai", "deepgram-sdk")},
                   code_sha256={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in IDENTITY_SOURCES})
    question = json.dumps(addressee_question().model_dump(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False).encode()
    payload["config"] = dict(config, question_sha256=hashlib.sha256(question).hexdigest(),
        case_set_sha256=hashlib.sha256(Path(__file__).with_name("fixtures").joinpath(
            "duo_addressee_accept.json").read_bytes()).hexdigest())
    return dict(**payload, config_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())


def build_worker(transport, stt, llm, services, *, config=None, recorder=None, deadline_secs=3, addressee=None):
    config = configuration() if config is None else config
    tags = tags_enabled(config)
    if tags and not isinstance(llm, TaggedLLM):
        raise ValueError("DUO_CONTEXT=b2h requires the marker-writing LLM")
    context = TaggedContext() if tags else CharacterContext()
    user_params = turn_policy(profile="demo")
    if tags:
        # An empty turn that interrupted a reply is still answered, as with labels, but not by
        # Pipecat's developer instruction: the context stores this as the human's own turn.
        user_params.empty_user_turn = EmptyUserTurnConfig(interrupted_prompt=INAUDIBLE)
    user, assistant = LLMContextAggregatorPair(context, user_params=user_params)
    gate = TurnGate()
    switcher = DuoServiceSwitcher(services=[services["angel"], services["devil"]],
                               strategy_type=ServiceSwitcherStrategyManual)
    def event(kind, **data):
        logger.info("DUO {} {}", kind, data)
        if recorder:
            recorder.log(kind, **data)
    coordinator = SwitchCoordinator(llm=llm, services=services, switcher=switcher,
                                    context=context, gate=gate, deadline_secs=deadline_secs,
                                    fault=config["fault"], event=event,
                                    addressee=addressee if config.get("auto_switch") else None,
                                    route_budget_s=config.get("route_budget_s", DEFAULT_ROUTE_BUDGET_S),
                                    auto_switch_floor=config.get("auto_switch_floor", DEFAULT_AUTO_SWITCH_FLOOR),
                                    cancel_join_bound_s=config.get("cancel_join_bound_s"),
                                    vocative_rule=bool(config.get("vocative_rule")), tags=tags)
    if tags:
        context.state = llm.duo_state = coordinator.state
        llm.duo_context = context
    if config.get("auto_switch"):
        if addressee is None or not isinstance(llm, RouteStampingLLM):
            raise ValueError("Enabled routing requires an addressee and a source-stamping LLM")
        coordinator.hold = TextHold(coordinator)
        gate.fence.coordinator = coordinator
        llm.route_fence = gate.fence

        @gate.event_handler("on_before_process_frame")
        def user_speech_started(processor, frame):
            # Flux's proposed start reaches the gate before it can be held or
            # generate an interruption. Reopen pushes held frames without
            # processing them here again. Ignore the aggregator's upstream
            # UserStartedSpeakingFrame echo, which is not a new speech start.
            if isinstance(frame, ProposedUserStartedSpeakingFrame):
                coordinator.user_speech_epoch += 1
    timing = None
    if timing_enabled(config):
        timing = DuoTimingObserver(fence=gate.fence, llm=llm, output=transport.output(),
                                   enabled=config.get("auto_switch", False))
    coordinator.timing = gate.fence.timing = timing
    coordinator.user_aggregator, coordinator.assistant_aggregator = user, assistant
    for service in services.values():
        service.coordinator = coordinator
    processors = [transport.input()]
    if recorder:
        processors.append(recorder.tap("uplink"))
    processors.append(stt)
    if recorder:
        processors.append(recorder.tap("flux"))
    processors.extend([gate, user, gate.fence, llm])
    if coordinator.hold:
        processors.append(coordinator.hold)
    processors.append(switcher)
    if recorder:
        processors.append(recorder.tap("tts"))
    processors.append(transport.output())
    if recorder:
        processors.append(recorder.tap("tts-written"))
    processors.extend([SpeakingCharacter(context), assistant, TailObserver(coordinator)])
    worker = PipelineWorker(Pipeline(processors), idle_timeout_secs=None,
                            rtvi_processor=RTVIProcessor(),
                            # The firmware reads user-transcription, not its 1.13 successor user-input,
                            # so the copy of every transcript is left out.
                            # With DUO_CONTEXT=b2h user-llm-text is left out too: it quotes the stored
                            # user message, marker included. The firmware does not read it.
                            rtvi_observer_params=RTVIObserverParams(
                                metrics_enabled=False, user_input_enabled=False,
                                **(dict(user_llm_enabled=False) if tags else {})),
                            observers=([timing] if timing else []) + ([recorder.observer] if recorder else []),
                            params=PipelineParams(audio_in_sample_rate=SAMPLE_RATE,
                                                  audio_out_sample_rate=SAMPLE_RATE,
                                                  enable_metrics=True, enable_usage_metrics=True))
    coordinator.attach(worker)

    @user.event_handler("on_user_turn_stopped")
    async def user_stopped(aggregator, strategy, message):
        def heard(content):  # The stored message without the marker DUO_CONTEXT=b2h keeps on it.
            return untagged(content) if tags and isinstance(content, str) else content
        latest = next((m for m in reversed(context.get_messages())
                       if m.get("role") == "user" and heard(m.get("content")) == message.content), None)
        if timing:
            timing.turn_ended(latest)
        logger.info("Transcript: user: {}", message.content)
        if recorder:
            recorder.log("aggregator-user-turn-stopped", text=message.content,
                         strategy=type(strategy).__name__)

    @assistant.event_handler("on_assistant_turn_started")
    async def assistant_started(aggregator):
        coordinator.enqueue(TurnStarted(context.speaking_character))

    @assistant.event_handler("on_assistant_turn_stopped")
    async def assistant_stopped(aggregator, message):
        logger.info("Transcript: assistant: {}", message.content)
        if recorder:
            recorder.log("aggregator-assistant-turn-stopped", text=message.content)

    return worker, coordinator


async def finish(cleanup):
    """Run a cleanup to its end even if the caller is cancelled meanwhile.

    The cleanup runs as a task of its own, so a cancellation of the caller
    does not reach it. The caller keeps waiting, and the cancellation is
    raised again once the cleanup is over.
    """
    task = asyncio.ensure_future(cleanup)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = cancelled or bool(asyncio.current_task().cancelling())
        except Exception:
            pass  # The cleanup's own failure; task.result() raises it below.
    if cancelled:
        if not task.cancelled():
            task.exception()  # Retrieved; the cancellation takes precedence.
        raise asyncio.CancelledError
    return task.result()


async def end_session(control, liveness, rtvi, recorder):
    """The session's shutdown, in order. Every step runs, whatever an earlier one did."""
    try:
        control.cancel()
        await asyncio.gather(control, return_exceptions=True)
    finally:
        try:
            await liveness.stop()
        finally:
            try:
                await rtvi.stop_interrupt_repeats()
            finally:
                if recorder:
                    recorder.close("bot-exit")


@asynccontextmanager
async def open_addressee(config, factory=Addressee):
    """The session's Jev client, or None with auto-switch off.

    The warm-up request runs beside pipeline construction instead of delaying
    it. A first read that arrives before it finishes is bounded by its own
    route deadline. On exit the warm-up is joined before the client closes.
    """
    if not config["auto_switch"]:
        yield None
        return
    # Transport options are passed only when set, so the default client is built exactly as before.
    options = {name: config[key] for name, key in (("hedge_s", "jev_hedge_s"), ("keepwarm_s", "jev_keepwarm_s"),
                                                   ("join_bound_s", "cancel_join_bound_s"))
               if config.get(key) is not None}
    addressee = factory(os.environ["TYPESAFE_API_KEY"],
                        budget=config["route_budget_s"], model=config["jev_model"], **options)
    warm_up = asyncio.create_task(addressee.connect())
    try:
        yield addressee
    finally:
        async def shut():
            warm_up.cancel()
            await asyncio.gather(warm_up, return_exceptions=True)
            await addressee.close()
        # A session cancelled while this runs must still close the client.
        await finish(shut())


# What the runner and a session import on first use. DUO_GC_FREEZE=1 imports
# them before it freezes, so the frozen heap is the whole import heap. First
# the modules bot() imports itself; then the ones its factories pull in when a
# service is first constructed: the Flux STT service, the HTTP stack under the
# LLM client (httpcore2), and the one under the Jev client (httpcore) with the
# CA bundle lookup it does (importlib.readers).
# Two tests hold this list to the code: one reads bot(), the other freezes and
# then constructs every factory, and fails if anything is still imported.
SESSION_IMPORTS = ("pipecat.runner.types", "pipecat.transports.base_transport",
                   "pipecat.transports.smallwebrtc.transport", "pipecat.transports.smallwebrtc.request_handler",
                   "pipecat.workers.runner", "settings", "demo_liveness", "sctp_flight_fix",
                   "pipecat.services.deepgram.flux.stt", "httpcore2", "httpcore", "importlib.readers")
GC_LOG_FLOOR_S = 0.020


class SlowCollections:
    """DUO_GC_LOG=1: one ``DUO gc`` row for each garbage collection of 20 ms or more.

    The interpreter calls this object wherever a collection happens to run,
    possibly while that thread is inside the logger. So the call only measures
    and puts the row on a queue that is safe to use from there; a thread of
    its own writes the log.
    """

    def __init__(self, floor_s=GC_LOG_FLOOR_S, clock=time.perf_counter, sink=None):
        self.floor_s, self.clock = floor_s, clock
        self.sink = sink or self._log
        self.began = None
        self.rows = queue.SimpleQueue()
        self.writer = threading.Thread(target=self._write, name="duo-gc-log", daemon=True)

    def __call__(self, phase, info):
        if phase == "start":
            self.began = self.clock()
        elif self.began is not None:
            elapsed, self.began = self.clock() - self.began, None
            if elapsed >= self.floor_s:
                self.rows.put(dict(generation=info["generation"], ms=round(elapsed * 1000, 1),
                                   collected=info["collected"]))

    @staticmethod
    def _log(row):
        logger.info("DUO gc {}", json.dumps(row))

    def _write(self):
        while (row := self.rows.get()) is not None:
            self.sink(row)

    def start(self):
        self.writer.start()
        gc.callbacks.append(self)
        return self

    def stop(self):
        gc.callbacks.remove(self)
        self.rows.put(None)
        self.writer.join()


def freeze_startup_heap(import_module=importlib.import_module, collector=gc):
    """DUO_GC_FREEZE=1: take the import heap out of every later collection.

    Called once, before the server starts. Nothing a session creates is
    frozen, so a session's own garbage is still collected.
    """
    for name in SESSION_IMPORTS:
        import_module(name)
    collector.collect()
    collector.freeze()
    logger.info("DUO gc-freeze {}", json.dumps(dict(objects=collector.get_freeze_count())))


def start_gc_diagnostics(config):
    """For the whole process, before the server binds. Both are off unless asked for."""
    if config["gc_log"]:
        SlowCollections().start()  # First, so the freeze's own full collection is a row.
    if config["gc_freeze"]:
        freeze_startup_heap()


_process_started = False


def start_process(config):
    """What a bot process does once, before its server binds.

    There are two ways to launch the bot: this file run as a script, and
    hil/duo_identity.py serve(), which imports it and calls the runner itself.
    Both call this with the configuration they record as the identity, so a
    process-wide flag is applied however the bot was launched. Not per session:
    a freeze there would keep a session's own objects for ever.
    """
    global _process_started
    if _process_started:
        raise RuntimeError("the bot process was already started")
    if config.get("llm_url"):
        deepgram_think.is_think_url(config["llm_url"])  # A hosted endpoint without TLS is refused here too.
    _process_started = True
    if os.getenv("BOT_ICE_INTERFACE"):
        # Gather on one interface only: on a host with Tailscale, aioice otherwise
        # waits out a 5 s STUN/TURN timeout on the tailnet addresses before answering.
        import ice_path
        ice_path.install()
    start_gc_diagnostics(config)


async def bot(runner_args):
    from pipecat.runner.types import SmallWebRTCRunnerArguments
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.workers.runner import WorkerRunner
    from settings import load_credentials
    from demo_liveness import Liveness, cadence
    import sctp_flight_fix

    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise TypeError("Run duo with the SmallWebRTC transport")
    config = configuration()
    sctp_flight_fix.install()
    load_credentials(approved_only=True)
    config = effective_configuration(config, has_typesafe_key=bool(os.getenv("TYPESAFE_API_KEY")))
    async with AsyncExitStack() as lifetime:
        addressee = await lifetime.enter_async_context(open_addressee(config))
        recorder = None
        if os.getenv("ECHOEAR_AEC_SESSION_DIR"):
            from aec_session_recorder import SessionRecorder
            recorder = SessionRecorder.from_environment()
            recorder.pc_id = runner_args.webrtc_connection.pc_id
            recorder.log("peer-binding")
        transport = SmallWebRTCTransport(webrtc_connection=runner_args.webrtc_connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True,
                                   audio_in_sample_rate=SAMPLE_RATE, audio_out_sample_rate=SAMPLE_RATE,
                                   audio_out_10ms_chunks=int(os.getenv("AUDIO_OUT_10MS_CHUNKS", "4"))))
        llm, services = make_llm(config), make_tts(config)
        worker, coordinator = build_worker(transport, make_stt_for(config, os.environ["DEEPGRAM_API_KEY"]),
                                           llm, services, config=config, recorder=recorder, addressee=addressee)
        manifest = identity(config)
        logger.info("DUO_IDENTITY {}", json.dumps(manifest, sort_keys=True))
        if recorder:
            recorder.log("duo-identity", **manifest)
            recorder.log("duo-capability", aec=capability_aec(runner_args.body))
        liveness = Liveness(worker.rtvi.send_server_message, runner_args.webrtc_connection.pc.close,
                            period=cadence(os.getenv("ECHOEAR_LIVENESS_SECONDS", "0.5")),
                            event=recorder.log if recorder else lambda *a, **kw: None,
                            label=runner_args.webrtc_connection.pc_id)

        coordinator.liveness = liveness

        @worker.rtvi.event_handler("on_client_message")
        async def message(rtvi, message):
            if message.type == "pong":
                coordinator.enqueue(Pong(message.data))

        @transport.event_handler("on_client_disconnected")
        async def disconnected(transport, client):
            coordinator.enqueue(Shutdown())
            await worker.cancel()

        runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
        await runner.add_workers(worker)
        control = asyncio.create_task(coordinator.run())
        try:
            await runner.run()
        finally:
            # A session cancelled during this shutdown must not leave the
            # liveness tasks running or the recording open.
            await finish(end_session(control, liveness, worker.rtvi, recorder))


if __name__ == "__main__":
    process_config = configuration()  # Refuse unauthorized test faults before the server binds.
    try:
        # And a bot with no LLM to call, or a hosted endpoint without TLS.
        deepgram_think.is_think_url(require_llm_url(process_config))
    except ValueError as error:
        sys.exit(f"duo bot not started: {error}")
    if os.getenv("ECHOEAR_AEC_FULL_DUPLEX") is not None:
        sys.exit("ECHOEAR_AEC_FULL_DUPLEX was removed; the device alone gates the mic")
    from pipecat.runner.run import main
    from settings import server_port
    if not any(arg == "--port" or arg.startswith("--port=") for arg in sys.argv[1:]):
        sys.argv.extend(["--port", str(server_port())])
    start_process(process_config)
    main()
