"""Jev routing for the duo bot: who owns a reply while the addressee read is in flight.

Each admitted user turn starts the draft reply and one Jev read in parallel.
The draft's frames wait in ``TextHold`` until the read decides: release them
(nobody else was addressed), or drop them, switch character and rerun the turn.
Everything that knows about a route token lives in this module; the
coordinator in ``duo_bot.py`` keeps the device protocol, the switch
transaction, voice retirement and liveness.

Four invariants carry the correctness. Each has tests in test_duo_pipeline.py.

1. Stamp at the source. The LLM that produces a run stamps every response
   frame with the run's token and output lifetime (``RouteStampingLLM.push_frame``),
   including the End frame the base class pushes from its cancellation
   ``finally``. Nothing downstream infers ownership from arrival order.

2. Supersede on new admission. Admitting a context (``RouteTurns.admit_route``,
   called by the fence in the synchronous step that stamps the frame) decides
   every older undecided route as ``superseded``. Decisions are terminal, so a
   late Jev result or deadline can never decide an old turn once a newer one
   was admitted. An interruption retires the affected routes the same way, at
   arrival, before any cancellation cleanup can yield.

3. Claim and close are atomic. A switch claims its route (``quarantine``) and
   closes the turn gate with no await between the two
   (``RouteTurns._decide_route``), so no inference is admitted between the
   decision to switch and the fence that holds later turns.

4. Rerun frozen before any await. The rerun placeholder is validated against
   the speech epoch, the inference sequence and the exact message objects, and
   becomes a context ending at the switch note before the LLM awaits anything
   (``RouteStampingLLM.process_frame``). Later turns own later runs. With
   DUO_CONTEXT=b2h there is no note: the context ends at the user message,
   whose marker names the character who answers (``duo_context.py``).

Ownership: the hold owns the held FIFO and the deadline timers; the
coordinator task owns the single in-flight read and joins it; the LLM and the
fence only retire routes and never decide one.

An interruption that retires a route also waits for the cancelled Jev request
to finish cleaning up. By default that wait has no limit, so the interruption
reaches the voices only after the HTTP cleanup. DUO_CANCEL_JOIN_MS=<n> limits
the wait to n milliseconds (0: do not wait). The ordering does not depend on
the wait: the route was retired synchronously, and the coordinator still joins
the request before it starts the next read. With the limit set, every read is
supervised: one that has not finished within the transaction deadline of
being cancelled, by a retirement or by its own route deadline, ends the session.

DUO_VOCATIVE_RULE=1 adds one rule to the decision: a turn that opens by
calling the other character switches to them, whatever Jev answered. The rule
sits at the one place a read is decided (``_decide_route``), after the read
and after its cancellation has been honoured, so every guard above applies to
a ruled turn exactly as to any other: a superseded, preempted or late route is
not switched by rule either. The read still runs, and its answer is logged
beside the rule's. It only ends a little before the route deadline, because a
ruled turn does not need the answer and its claim must still beat the deadline.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
from uuid import uuid4
import weakref

from pipecat.frames.frames import (
    CancelFrame, ControlFrame, DataFrame, EndFrame, InterruptionFrame, LLMContextFrame,
    LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame,
    LLMThoughtStartFrame, LLMThoughtTextFrame, LLMThoughtEndFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from duo_addressee import addressee_state, decide, leading_vocative, split_context

DOWN = FrameDirection.DOWNSTREAM


@dataclass(frozen=True)
class RouteAdmitted:
    """A stamped context entered the pipeline; its read is the coordinator's next route."""
    token: str
    snapshot: dict
    admitted: float


@dataclass(frozen=True)
class RouteSuperseded:
    token: str


@dataclass(frozen=True)
class RouteCleanupOverdue:
    token: str


async def join_retired(reads, bound_s):
    """Wait for retired reads to finish cancelling: without limit, or for at most bound_s.

    Neither form cancels a read again when the waiting processor is itself
    cancelled a second time. A read that outlives the bound stays owned by
    the coordinator task.
    """
    if not reads:
        return
    if bound_s is None:
        await asyncio.shield(asyncio.gather(*reads, return_exceptions=True))
    elif bound_s > 0:
        await asyncio.wait(reads, timeout=bound_s)


RESPONSE_FRAMES = (LLMFullResponseStartFrame, LLMTextFrame, LLMThoughtStartFrame,
                   LLMThoughtTextFrame, LLMThoughtEndFrame, LLMFullResponseEndFrame)


@dataclass
class HoldWake(ControlFrame):
    token: str | None = None  # Observational attribution; draining remains FIFO/global.


@dataclass
class AutoRerunFrame(DataFrame):
    seq0: int
    user_object: dict
    note_object: dict | None  # None with DUO_CONTEXT=b2h: the switch is the marker on the user message.
    speech_epoch: int


@dataclass(eq=False)
class Route:
    """One admitted context: its routing decision and its output lifetime.

    The context frame and every response frame of its run carry this object
    (``duo-output``; ``duo-route`` is its token). A frame is judged by the
    route it carries, so a decision or a cancellation still holds after the
    hold's eight-entry token index has forgotten the token.
    """
    token: str
    deadline: float
    # release, superseded or quarantine is final. Only shutdown overwrites it, with stopped.
    decision: str = "undecided"
    cancelled: bool = False  # This run's output must not reach a voice.
    tts_owner: tuple | None = None
    tts_started: bool = False


def route_decision(frame):
    route = frame.metadata.get("duo-output")
    return route.decision if route is not None else "release"


def cancelled_output(frame, *, settle_admitted=False):
    output = frame.metadata.get("duo-output")
    if output is None or not output.cancelled:
        return False
    if (settle_admitted and isinstance(frame, LLMFullResponseEndFrame) and
            output.tts_started and output.tts_owner is not None):
        # The End of a reply already admitted to TTS still reaches that voice,
        # so its spoken history settles exactly as it does without routing:
        # the voice may flush a sentence it had already been given. The End
        # carries no new text, and can never close a newer context or a
        # different active voice. A withheld draft has no such recipient.
        owner = output.tts_owner
        service = owner[2]
        if (service.response_owner == owner and
                service.coordinator.switcher.strategy.active_service is service):
            return False
    return True


class TextHold(FrameProcessor):
    """A response FIFO drained only by this processor's interruptible wake."""
    def __init__(self, coordinator):
        super().__init__()
        self.coordinator = coordinator
        self.routes = {}  # token -> Route: the eight newest.
        # The same routes by token for as long as any frame or context carries
        # them, so a retirement still finds one the index above has evicted.
        self.outputs = weakref.WeakValueDictionary()
        self.held = deque()
        self.timers = set()

    def decision(self, token):
        record = self.routes.get(token)
        return record.decision if record else "release"

    def decide(self, token, decision):
        record = self.routes.get(token)
        if record is None or record.decision != "undecided":
            return False
        record.decision = decision
        if self.coordinator.timing:
            self.coordinator.timing.terminal(token, decision, record.deadline)
        return True

    def claim_switch(self, token):
        record = self.routes.get(token)
        if record and asyncio.get_running_loop().time() >= record.deadline:
            self.decide(token, "release")
            return False
        return self.decide(token, "quarantine")

    def supersede(self, token):
        changed = self.decide(token, "superseded")
        if changed:
            self.coordinator.enqueue(RouteSuperseded(token))
        return changed

    def expire(self, token):
        return self.decide(token, "release")

    def cancel_output(self, token):
        output = self.outputs.get(token)
        if output is not None:
            output.cancelled = True

    def expect(self, token, deadline):
        wake = False
        for old in tuple(self.routes):
            wake = self.supersede(old) or wake
        self.routes[token] = self.outputs[token] = Route(token, deadline)
        while len(self.routes) > 8:
            del self.routes[next(iter(self.routes))]
        task = asyncio.create_task(self._deadline(token, deadline))
        self.timers.add(task)
        task.add_done_callback(self.timers.discard)
        return wake

    async def _deadline(self, token, deadline):
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        if self.expire(token):
            await self.queue_frame(HoldWake(token=token))

    async def _stop_timers(self):
        tasks = tuple(self.timers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def cleanup(self):
        await self._stop_timers()
        await super().cleanup()

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            for held, _ in self.held:
                self.supersede(held.metadata.get("duo-route"))
            self.held.clear()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            # Shutdown closes every live route, including already released runs.
            for route in tuple(self.outputs.values()):
                route.decision = "stopped"
            self.held.clear()
            await self._stop_timers()
        await super().process_frame(frame, direction)
        if isinstance(frame, HoldWake) and direction == DOWN:
            if self.coordinator.timing:
                self.coordinator.timing.wake(frame.token)
            while self.held:
                held, leg = self.held[0]
                decision = route_decision(held)
                discard = cancelled_output(held)
                if decision == "undecided" and not discard:
                    break
                if self.coordinator.timing:
                    self.coordinator.timing.wake(held.metadata.get("duo-route"))
                self.held.popleft()
                if not discard and decision in ("release", "superseded"):
                    await self.push_frame(held, leg)
            return
        if direction == DOWN and isinstance(frame, RESPONSE_FRAMES):
            if "duo-output" not in frame.metadata:
                output = self.outputs.get(frame.metadata.get("duo-route"))
                if output is not None:
                    frame.metadata["duo-output"] = output
            if cancelled_output(frame, settle_admitted=True):
                return
            decision = route_decision(frame)
            if decision in ("quarantine", "stopped"):
                return
            if self.held or decision == "undecided":
                self.held.append((frame, direction))
                if self.coordinator.timing:
                    self.coordinator.timing.held(frame)
                return
        await self.push_frame(frame, direction)


class RouteStampingLLM:
    """Stamp at the source, including the base LLM's cancellation finally."""
    _route = None
    _route_output = None

    def __init__(self, *args, **kwargs):
        self._queued_route_contexts = weakref.WeakValueDictionary()
        self._cancelled_route_reads = set()
        super().__init__(*args, **kwargs)

    def _retire_route(self, token):
        read = self.route_fence.coordinator.context_cancelled(token)
        if read is not None:
            self._cancelled_route_reads.add(read)
            read.add_done_callback(self._cancelled_route_reads.discard)

    async def _join_cancelled_routes(self):
        # The coordinator owns these reads and joins each one; the HTTP client
        # closes only after Addressee.close() has joined its requests.
        await join_retired(tuple(self._cancelled_route_reads),
                           self.route_fence.coordinator.cancel_join_bound_s)

    async def queue_frame(self, frame, direction=DOWN, callback=None):
        if isinstance(frame, LLMContextFrame) and frame.metadata.get("duo-route"):
            self._queued_route_contexts[frame.id] = frame
        elif isinstance(frame, InterruptionFrame):
            self._retire_route(self._route)
            # Snapshot at interruption arrival, not processing time: a later
            # context belongs to the new turn after a pre-turn interruption.
            for queued in tuple(self._queued_route_contexts.values()):
                if queued.interruptible:
                    queued.metadata["duo-route-cancelled"] = True
                    self._queued_route_contexts.pop(queued.id, None)
                    self._retire_route(queued.metadata["duo-route"])
        await super().queue_frame(frame, direction, callback)

    async def cleanup(self):
        try:
            await self._join_cancelled_routes()
        finally:
            await super().cleanup()

    async def process_frame(self, frame, direction):
        self._queued_route_contexts.pop(frame.id, None)
        if isinstance(frame, LLMContextFrame) and frame.metadata.get("duo-route-cancelled"):
            await self._join_cancelled_routes()
            return
        if isinstance(frame, AutoRerunFrame):
            fence = self.route_fence
            messages = fence.coordinator.context.get_messages()
            latest = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            user_index = next((i for i, m in enumerate(messages) if m is frame.user_object), -1)
            if fence.coordinator.tags:
                # DUO_CONTEXT=b2h: no note follows. The run ends at the user message, and
                # TaggedLLM gives it the committed character's marker on the way to the model.
                note_index = user_index if frame.note_object is None else -1
            else:
                note_index = next((i for i, m in enumerate(messages) if m is frame.note_object), -1)
            if (fence.coordinator.user_speech_epoch != frame.speech_epoch or
                    fence.inference_seq != frame.seq0 or latest is not frame.user_object or
                    user_index < 0 or note_index < user_index or
                    (note_index == user_index and not fence.coordinator.tags)):
                fence.coordinator.event("route-rerun-withdrawn", seq0=frame.seq0)
                return
            # Freeze before any awaited response-start; later turns own later runs.
            user_object = frame.user_object
            frame = LLMContextFrame(context=LLMContext(messages=list(messages[:note_index + 1])))
            if fence.timing:
                fence.timing.admit(frame, rerun_user=user_object)
        if isinstance(frame, LLMContextFrame):
            token = self._route = frame.metadata.get("duo-route")
            self._route_output = frame.metadata.get("duo-output")
            try:
                await super().process_frame(frame, direction)
            except asyncio.CancelledError:
                # This also covers the superclass observer await before its
                # completion try/finally, where no response frame exists yet.
                self._retire_route(token)
                await self._join_cancelled_routes()
                raise
            finally:
                self._route = None
                self._route_output = None
        else:
            try:
                await super().process_frame(frame, direction)
            finally:
                if isinstance(frame, InterruptionFrame):
                    await self._join_cancelled_routes()

    async def push_frame(self, frame, direction=DOWN):
        if self._route is not None and isinstance(frame, RESPONSE_FRAMES):
            frame.metadata["duo-route"] = self._route
            frame.metadata["duo-output"] = self._route_output
        await super().push_frame(frame, direction)


class RouteTurns:
    """The coordinator's route half: one Jev read per admitted turn.

    Mixed into ``SwitchCoordinator``, which supplies ``state``, ``services``,
    ``failed_voices``, ``gate``, ``timing``, ``event``, ``enqueue``, ``tags``,
    ``stopping``, ``deadline_secs``, ``_wait_with_events`` and
    ``_end_pipeline``. Reads run one at a time in the coordinator task; the
    next route waits until the previous read has finished its cancellation
    cleanup, so at most one retired request is ever outstanding.
    """

    # A ruled turn's read ends this long before the route deadline; the claim that follows it is synchronous.
    RULED_READ_MARGIN_S = .05

    def _init_routing(self, *, addressee, route_budget_s, auto_switch_floor, cancel_join_bound_s=None,
                      vocative_rule=False):
        self.addressee = addressee
        self.route_budget_s, self.auto_switch_floor = route_budget_s, auto_switch_floor
        self.cancel_join_bound_s = cancel_join_bound_s
        self.vocative_rule = vocative_rule
        self.hold = None
        self.route_read = None
        self.route_cancel_reason = None
        self.route_overdue = None
        self.route_ending = None
        self.user_speech_epoch = 0

    def admit_route(self, frame, seq0):
        """Stamp a fresh inference and queue its read. True if older held output must wake."""
        messages = frame.context.get_messages()
        latest = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if latest is None or self._unrouted(latest):
            return False
        prior, text = self._history(messages)
        snapshot = dict(prior=prior, latest=text, current=self.state.character,
                        user_object=latest, user_index=next(i for i, m in enumerate(messages) if m is latest),
                        seq0=seq0, speech_epoch=self.user_speech_epoch)
        admitted = asyncio.get_running_loop().time()
        token = uuid4().hex
        wake = self.hold.expect(token, admitted + self.route_budget_s)
        frame.metadata["duo-route"] = token
        frame.metadata["duo-output"] = self.hold.outputs[token]
        self.enqueue(RouteAdmitted(token, snapshot, admitted))
        return wake

    def _unrouted(self, latest):
        """Whether this request is answered by the committed character without asking who was addressed."""
        return False

    def _history(self, messages):
        """What the Jev read is given: the conversation before the latest user message, and that message."""
        return split_context(messages)

    def _cancel_route(self, reason):
        if self.route_cancel_reason is None:
            self.route_cancel_reason = reason
        token, task = self.route_read
        if not task.cancelling():
            task.cancel()
        self._supervise_read(token, asyncio.get_running_loop().time() + self.deadline_secs)

    def _supervise_read(self, token, when):
        """With the join bounded, a read still unfinished at `when` ends the session.

        Nothing on the pipeline waits for a cancelled read then, so its
        cleanup needs a deadline of its own, whoever started the cancellation.
        The timer is armed when the read starts, for deadline_secs after the
        route deadline that cancels it at the latest; a retirement only ever
        brings it forward. It is cancelled when the read has joined.
        """
        if self.cancel_join_bound_s is None:
            return
        if self.route_overdue is not None:
            if self.route_overdue.when() <= when:
                return
            self.route_overdue.cancel()
        self.route_overdue = asyncio.get_running_loop().call_at(when, self.enqueue, RouteCleanupOverdue(token))

    def route_cleanup_overdue(self, token):
        """Fail closed: a request that will not finish cancelling blocks every later transaction."""
        if self.route_read and token == self.route_read[0] and not self.stopping:
            self.event("route-cleanup-overdue", token=token)
            self.gate.close()
            self.stopping = True
            self.route_ending = asyncio.ensure_future(self._end_pipeline())

    def context_cancelled(self, token):
        """Withdraw only this context; an auto claim already owns its rerun."""
        if token is None or self.hold.decision(token) == "quarantine":
            return None
        self.hold.cancel_output(token)
        self.hold.supersede(token)
        if self.route_read and self.route_read[0] == token:
            self._cancel_route("superseded")
            return self.route_read[1]
        return None

    def _ruled(self, snapshot):
        """The other character, if the rule is on and the turn opens by calling them."""
        if not self.vocative_rule:
            return None
        called = leading_vocative(snapshot["latest"])
        return called if called and called != snapshot["current"] else None

    async def _read_route(self, token, snapshot, early=0):
        try:
            async with asyncio.timeout_at(self.hold.routes[token].deadline - early):
                return await self.addressee.ask(addressee_state(
                    snapshot["prior"], snapshot["latest"], snapshot["current"]))
        except Exception as error:
            return error

    async def _decide_route(self, route):
        """Read and decide one route. Returns (target, service) once a switch is claimed."""
        token, snapshot, admitted = route.token, route.snapshot, route.admitted
        if self.stopping or self.hold.decision(token) != "undecided":
            return None
        self.route_cancel_reason = None
        ruled, audit = self._ruled(snapshot), {}
        task = asyncio.create_task(self._read_route(token, snapshot, self.RULED_READ_MARGIN_S if ruled else 0))
        self.route_read = (token, task)
        self._supervise_read(token, self.hold.routes[token].deadline + self.deadline_secs)
        try:
            try:
                result = await self._wait_with_events(task)
                decision = decide(result, snapshot["current"], self.auto_switch_floor,
                                  self.state.auto_eligible("devil" if snapshot["current"] == "angel" else "angel"))
                target, reason, probability = decision.target, decision.reason, decision.p
                if ruled:
                    # The call decides. What Jev answered stays beside it, for the audit.
                    audit = dict(jev_target=target, jev_reason=reason)
                    target, reason = ruled, "vocative-rule"
            except asyncio.CancelledError:
                if not self.route_cancel_reason or asyncio.current_task().cancelling() or self.stopping:
                    raise
                target, reason, probability = None, self.route_cancel_reason, 0
        finally:
            self.route_read = None
            if self.route_overdue is not None:
                self.route_overdue.cancel()
                self.route_overdue = None
        service = self.services.get(target)
        if target and (not self.state.auto_eligible(target) or service in self.failed_voices or
                       not service.is_usable or not service.connection_ready):
            target, reason = None, "not-eligible"
        if self.timing:
            self.timing.route_result(token, target, reason, probability)
        # How the read travelled (new or reused connection, hedge), when the client reports it.
        travelled = getattr(self.addressee, "last_read", None)
        self.event("route", token=token, target=target, reason=reason, p=probability,
                   elapsed_ms=(asyncio.get_running_loop().time() - admitted) * 1000,
                   **audit, **({"jev": dict(travelled)} if travelled else {}))
        if target is None:
            if self.hold.decide(token, "release"):
                await self.hold.queue_frame(HoldWake(token=token))
            return None
        # Claim and gate closure are one synchronous step. Admission cannot race them.
        if not self.hold.claim_switch(token):
            await self.hold.queue_frame(HoldWake(token=token))
            return None
        self.gate.close()
        await self.hold.queue_frame(HoldWake(token=token))
        return target, service

    def _queue_rerun(self, snapshot, note):
        if self.user_speech_epoch != snapshot["speech_epoch"]:
            self.event("route-rerun-withdrawn", seq0=snapshot["seq0"])
            return
        self.gate.fence.held.append((AutoRerunFrame(
            snapshot["seq0"], snapshot["user_object"], note, snapshot["speech_epoch"]), DOWN))
