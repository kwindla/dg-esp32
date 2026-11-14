"""Canonical offline flag-off recorder. Run with ``uv run python duo_baseline.py``.

That prints the recording of the labelled context (DUO_CONTEXT=labels), which
is the bot as it was before DUO_CONTEXT existed. ``uv run python
duo_baseline.py b2h`` prints the same six scenarios with the b2h context.

Only the existing Harness's fake service boundaries are instrumented. Causal
edges, rather than a clock-sorted event log, leave independent frame scheduling
out of the contract. No service credentials or network connections are used.
"""
import asyncio
from copy import deepcopy
import json

from pipecat.frames.frames import (
    ManuallySwitchServiceFrame, ProposedUserStartedSpeakingFrame, TranscriptionFrame,
)

from test_duo_pipeline import Harness, until


class Recorder:
    def __init__(self, harness):
        self.h = harness
        self.connections = {}
        self.outcomes, self.calls = [], []
        self.tts = {name: dict(text=[], audio_bytes=0) for name in harness.services}
        self.edges = set()
        self.first_audio = False
        self.first_text = None
        self.last_outcome = None
        self.last_text = {}
        self._instrument()

    def _instrument(self):
        client = self.h.transport.client
        event = self.h.control.event

        def published(kind, **data):
            # The coordinator's publication boundary precedes gate release.
            # RTVI's independent delivery queue may reach the fake client later.
            if kind == "outcome":
                outcome = deepcopy(data)
                conn = outcome["conn"]
                outcome["conn"] = self.connections.setdefault(conn, len(self.connections) + 1)
                node = f"outcome:{len(self.outcomes)}"
                if self.last_outcome:
                    self.edges.add((self.last_outcome, node))
                if outcome.get("reason") == "reverted" and self.calls:
                    self.edges.add((f"llm:{len(self.calls) - 1}", node))
                self.last_outcome = node
                self.outcomes.append(outcome)
            event(kind, **data)

        self.h.control.event = published
        process = self.h.llm._process_context

        async def context(context):
            node = f"llm:{len(self.calls)}"
            if self.calls:
                self.edges.add((f"llm:{len(self.calls) - 1}", node))
            if self.last_outcome:
                self.edges.add((self.last_outcome, node))
            self.calls.append(dict(system_instruction=self.h.llm.settings.system_instruction,
                                   messages=deepcopy(context.get_messages())))
            await process(context)

        self.h.llm._process_context = context
        write = client.write_audio_frame

        async def audio(frame):
            if not self.first_audio:
                self.first_audio = True
                assert self.first_text is not None
                self.edges.add((self.first_text, "first-audio"))
            return await write(frame)

        client.write_audio_frame = audio
        for name, service in self.h.services.items():
            self._instrument_tts(name, service)

    def _instrument_tts(self, name, service):
        send = service._transport_send_json
        handle_audio = service._handle_audio

        async def text(message):
            if message["type"] == "Speak":
                node = f"tts:{name}:{len(self.tts[name]['text'])}"
                self.edges.add((f"llm:{len(self.calls) - 1}", node))
                if name in self.last_text:
                    self.edges.add((self.last_text[name], node))
                self.last_text[name] = node
                if self.first_text is None:
                    self.first_text = node
                self.tts[name]["text"].append(message["text"])
            await send(message)

        async def audio(data):
            self.tts[name]["audio_bytes"] += len(data)
            await handle_audio(data)

        service._transport_send_json = text
        service._handle_audio = audio

    async def settled_reply(self, count=1):
        await until(lambda: len([m for m in self.h.control.context.get_messages()
                                 if m.get("role") == "assistant"]) == count)
        await until(lambda: self.h.control.state.verification is None)
        assert await self.h.worker.flush_pipeline(timeout=.5)
        await until(lambda: self.first_audio)

    def snapshot(self):
        return dict(outcomes=self.outcomes, llm_calls=self.calls,
                    context_messages=deepcopy(self.h.control.context.get_messages()),
                    tts=self.tts, first_audio=self.first_audio,
                    causal_order=[list(edge) for edge in sorted(self.edges)])


async def record_scenario(name, *, config=None):
    h = Harness(fault="tts-fail-next" if name == "revert" else "",
                **({"config": config} if config is not None else {}))
    from duo_route import RouteStampingLLM, TextHold
    assert h.config["auto_switch"] is False
    assert h.addressee is None and h.control.addressee is None
    assert h.control.hold is None and not isinstance(h.control.hold, TextHold)
    assert not isinstance(h.llm, RouteStampingLLM)
    recorder = Recorder(h)
    try:
        await h.start()
        await h.hello()
        if name == "hello":
            assert not h.llm.calls
        elif name == "plain-turns":
            await h.turn("What should I do?")
            await recorder.settled_reply()
            await h.turn("And what about tomorrow?")
            await recorder.settled_reply(count=2)
        elif name == "toggle":
            await h.send()
            await h.outcome()
            await h.turn()
            await recorder.settled_reply()
        elif name == "open-turn":
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await h.worker.queue_frame(TranscriptionFrame(
                text="An open turn", user_id="u", timestamp="now", finalized=True))
            assert await h.worker.flush_pipeline(timeout=.5)
            h.stt.flush_delay = .10
            await h.send()
            await until(lambda: h.control.gate.closed)
            await h.control.user_aggregator.push_aggregation()
            await until(lambda: h.control.gate.fence.held)
            assert not h.llm.calls
            await h.outcome()
            await recorder.settled_reply()
        elif name == "rollback":
            h.stt.sabotage = ManuallySwitchServiceFrame
            await h.send()
            assert (await h.outcome())["op"] == "error"
            await h.turn("After rollback")
            await recorder.settled_reply()
        elif name == "revert":
            await h.send()
            await h.outcome()
            h.llm.remainder.clear()
            await h.turn()
            await until(lambda: h.states()[-1].get("reason") == "reverted")
            await until(lambda: not h.control.gate.closed)
            h.llm.remainder.set()
            await h.turn("After revert")
            await recorder.settled_reply()
        else:
            raise ValueError(name)
        return recorder.snapshot()
    finally:
        await h.close()


async def record_baseline(*, config=None):
    config = {"auto_switch": False, "context": "labels"} if config is None else config
    return {name: await record_scenario(name, config=config)
            for name in ("hello", "plain-turns", "toggle", "open-turn", "rollback", "revert")}


def canonical_json(baseline):
    return json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    import sys
    style = sys.argv[1] if len(sys.argv) > 1 else "labels"
    print(canonical_json(asyncio.run(record_baseline(config={"auto_switch": False, "context": style}))), end="")
