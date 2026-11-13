"""Offline tests through real 1.15 worker, Flux queues and WebRTC output.

Only the LLM completion and the two network provider/client boundaries are
fake. No server, socket, device or audio playback is opened by these tests.
"""
import asyncio
from contextlib import asynccontextmanager
import time
import hashlib
from pathlib import Path
from contextvars import ContextVar
import inspect
from collections import deque

import pytest
from pipecat.frames.frames import (
    EndFrame, ErrorFrame, InputAudioRawFrame, InputTransportMessageFrame, InterimTranscriptionFrame,
    InterruptionFrame, InterruptionWorkerFrame, LLMContextFrame,
    LLMFullResponseStartFrame, LLMTextFrame, LLMUpdateSettingsFrame,
    ManuallySwitchServiceFrame, PipelineFlushFrame,
    ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame,
    StartFrame, STTMetadataFrame, TranscriptionFrame, TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.observers.base_observer import BaseObserver
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCOutputTransport
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from websockets.protocol import State

from duo_bot import (ClientReady, DeviceRequest, DuoFluxTTS, PipelineError, Pong, Shutdown, SilentNote,
                     TailObserver, TransactionMarker,
                     build_worker, configuration, fault_config, identity, make_llm, make_tts,
                     require_llm_url)
from duo_route import DOWN, AutoRerunFrame, HoldWake, RouteStampingLLM, RouteSuperseded, TextHold
from duo_prompts import character_instructions, silent_note
from pipecat.classifiers.base_classifier import ChoiceResult


EXISTING_STAY_MODE = ContextVar("existing_stay_mode", default=False)
CANCEL_JOIN_BOUND = ContextVar("cancel_join_bound", default=None)


def pytest_generate_tests(metafunc):
    if (inspect.iscoroutinefunction(metafunc.function) and
            not metafunc.function.__name__.startswith("test_auto_")):
        metafunc.parametrize("existing_stay_mode", [False, True], indirect=True)


@pytest.fixture(autouse=True)
def existing_stay_mode(request):
    token = EXISTING_STAY_MODE.set(getattr(request, "param", False))
    yield
    EXISTING_STAY_MODE.reset(token)


@pytest.fixture(params=[None, 0], ids=["join-unbounded", "join-bounded"])
def join_bound(request):
    """Run a test with the default unlimited cancellation join and with DUO_CANCEL_JOIN_MS=0."""
    token = CANCEL_JOIN_BOUND.set(request.param)
    yield request.param
    CANCEL_JOIN_BOUND.reset(token)


class FakeAddressee:
    def __init__(self, *script, delay=0):
        self.script = deque(script)
        self.delay = delay
        self.calls, self.cancelled, self.active = [], 0, 0
        self.asked_tokens = []
        self.route_token = None
        self.started = asyncio.Event()
        self.joined = asyncio.Event()
        self.cancel_delay = 0

    async def ask(self, state):
        self.calls.append(state)
        self.asked_tokens.append(self.route_token() if self.route_token else None)
        self.active += 1
        self.started.set()
        item = self.script.popleft() if self.script else "either"
        try:
            await asyncio.sleep(self.delay)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return await item(state)
            return ChoiceResult(choice=item, probabilities={item: .99}, confidence=.99)
        except asyncio.CancelledError:
            self.cancelled += 1
            await asyncio.sleep(self.cancel_delay)
            self.joined.set()
            raise
        finally:
            self.active -= 1


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.002)


class FakeClient:
    def __init__(self):
        self.messages, self.audio, self.audio_times = [], [], []

    async def setup(self, *args):
        pass

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def send_message(self, frame):
        self.messages.append(frame.message)

    async def write_audio_frame(self, frame):
        self.audio.append(frame)
        self.audio_times.append(time.monotonic())
        await asyncio.sleep(.001)
        return True


class PassThrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class FakeTransport:
    def __init__(self):
        self.source = PassThrough()
        self.client = FakeClient()
        # Exact production class; its queues, sender, interruption, flush and
        # arbitrary-control handling are unmodified.
        self.sink = SmallWebRTCOutputTransport(self.client,
            TransportParams(audio_out_enabled=True, audio_out_sample_rate=16000))

    def input(self):
        return self.source

    def output(self):
        return self.sink


class FakeSTT(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.sabotage = None
        self.coordinator = None
        self.flush_delay = 0
        self.worker_interrupt_delay = 0
        self.upstream_interrupt_delay = 0
        self.flush_started = asyncio.Event()
        self.frames = []
        self.interrupt_marker = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append((type(frame), time.monotonic()))
        if direction == DOWN and isinstance(frame, PipelineFlushFrame) and self.flush_delay:
            delay, self.flush_delay = self.flush_delay, 0
            self.flush_started.set()
            await asyncio.sleep(delay)
        if direction == DOWN and isinstance(frame, InterruptionWorkerFrame):
            await asyncio.sleep(self.worker_interrupt_delay)
        if direction != DOWN and isinstance(frame, InterruptionWorkerFrame):
            await asyncio.sleep(self.upstream_interrupt_delay)
        if direction == DOWN and isinstance(frame, TransactionMarker) and self.interrupt_marker:
            self.interrupt_marker = False
            await self.coordinator.worker.queue_frame(InterruptionWorkerFrame())
            await asyncio.sleep(.035)
        if (direction == DOWN and self.sabotage and
                isinstance(frame, self.sabotage)):
            self.sabotage = None
            # Inject an actual worker interruption while mutation is queued;
            # deliberately lose one mutation but allow the marker to survive.
            await self.coordinator.worker.queue_frame(InterruptionWorkerFrame())
            return
        await self.push_frame(frame, direction)
        if direction == DOWN and isinstance(frame, StartFrame):
            await self.push_frame(STTMetadataFrame(service_name="FakeFlux", ttfs_p99_latency=0,
                user_turn_strategies=ExternalUserTurnStrategies()))


class FakeLLM(OpenAILLMService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls, self.updates = [], []
        self.response_ids = []
        self.generate = asyncio.Event()
        self.generate.set()
        self.remainder = asyncio.Event()
        self.remainder.set()
        self.setting_delay = 0
        self.settings_started = asyncio.Event()
        self.first_frame_delay = 0
        self.first_token_delay = 0
        self.inter_token_delay = .005
        self.tokens = ("A small playful reply. ", "A final sentence. ")

    async def process_frame(self, frame, direction):
        if isinstance(frame, LLMUpdateSettingsFrame) and frame.service is self:
            self.settings_started.set()
            delay, self.setting_delay = self.setting_delay, 0
            await asyncio.sleep(delay or .005)
            self.updates.append((frame.delta.system_instruction, time.monotonic()))
        await super().process_frame(frame, direction)

    async def _process_context(self, context):
        # Real OpenAI process_frame emits response start/end and handles
        # cancellation; only the HTTP completion body is substituted.
        self.calls.append((self.settings.system_instruction, list(context.get_messages())))
        await self.generate.wait()
        response = len(self.calls)
        await asyncio.sleep(self.first_token_delay)
        first = LLMTextFrame(self.tokens[0])
        first.metadata["test-response"] = response
        await self.push_frame(first)
        await self.remainder.wait()
        await asyncio.sleep(self.inter_token_delay)
        last = LLMTextFrame(self.tokens[1])
        last.metadata["test-response"] = response
        await self.push_frame(last)

    async def push_frame(self, frame, direction=DOWN):
        if direction == DOWN and isinstance(frame, LLMFullResponseStartFrame):
            await asyncio.sleep(self.first_frame_delay)
            self.response_ids.append(frame.id)
        await super().push_frame(frame, direction)


class FakeTTS(DuoFluxTTS):
    def __init__(self, character, **kwargs):
        super().__init__(api_key="offline-placeholder", sample_rate=16000,
            settings=self.Settings(voice=f"offline-{character}"), **kwargs)
        self.character = character
        self.speaks, self.interruptions = [], []
        self.release = asyncio.Event()
        self.release.set()
        self.interrupt_delay = .005
        self.provider_delay = .015
        self.provider_tasks = set()
        self.returned = asyncio.Event()
        self.auto_complete = True
        self.emit_audio = True
        self.outcome_delay = 0
        self.starts = []
        self.frame_interruptions, self.text_responses = [], []
        self.connect_delay = 0
        self.connect_started = asyncio.Event()

    async def _connect(self):
        self.connect_started.set()
        await asyncio.sleep(self.connect_delay)
        await self._call_event_handler("on_connected")

    async def _disconnect(self):
        for task in list(self.provider_tasks):
            task.cancel()
        await asyncio.gather(*self.provider_tasks, return_exceptions=True)

    def _transport_is_active(self):
        return True

    async def _transport_send_json(self, message):
        if message["type"] == "Speak":
            self.speaks.append((message["text"], time.monotonic()))
            # Provider audio starts asynchronously, independently of run_tts.
            if self.emit_audio:
                await self._handle_audio(b"\x20\x01" * 320)
        elif message["type"] == "Flush" and self.auto_complete:
            task = asyncio.create_task(self.finish())
            self.provider_tasks.add(task)
            task.add_done_callback(self.provider_tasks.discard)

    async def finish(self):
        await self.release.wait()
        await asyncio.sleep(self.provider_delay)
        await self._handle_message(dict(type="SpeechMetadata"))

    async def run_tts(self, text, context_id):
        async for frame in super().run_tts(text, context_id):
            yield frame
        self.returned.set()

    async def on_audio_context_interrupted(self, context_id):
        self.interruptions.append(("start", time.monotonic()))
        await asyncio.sleep(self.interrupt_delay)
        await super().on_audio_context_interrupted(context_id)
        self.interruptions.append(("done", time.monotonic()))

    async def _record_context_audio_outcome(self, context_id, received_audio):
        await asyncio.sleep(self.outcome_delay)
        await super()._record_context_audio_outcome(context_id, received_audio)

    async def process_frame(self, frame, direction):
        if direction == DOWN and isinstance(frame, LLMFullResponseStartFrame):
            self.starts.append(frame.id)
        if direction == DOWN and isinstance(frame, LLMTextFrame):
            self.text_responses.append(frame.metadata["test-response"])
        if isinstance(frame, InterruptionFrame):
            await asyncio.sleep(self.interrupt_delay)
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self.frame_interruptions.append(time.monotonic())


class SinkWitness(BaseObserver):
    def __init__(self):
        super().__init__()
        self.audio, self.markers, self.interruptions = [], [], []
        self.routes = set()

    async def on_push_frame(self, data):
        if type(data.source).__name__ == "InferenceFence" and data.direction == DOWN:
            token = data.frame.metadata.get("duo-route")
            if token:
                self.routes.add(token)
        if data.direction == DOWN and isinstance(data.source, TailObserver):
            if isinstance(data.frame, InputAudioRawFrame):
                self.audio.append(time.monotonic())
            elif isinstance(data.frame, TransactionMarker):
                self.markers.append(data.frame.transaction)
            elif isinstance(data.frame, InterruptionFrame):
                self.interruptions.append(time.monotonic())


class MemorySocket:
    def __init__(self):
        self.state, self.response = State.OPEN, None
        self.incoming = asyncio.Queue()
        self.auto_complete = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        if isinstance(message, Exception):
            raise message
        return message

    async def send(self, payload):
        import json
        message = json.loads(payload)
        if message["type"] == "Speak":
            self.incoming.put_nowait(b"\x20\x01" * 320)
        elif message["type"] == "Flush" and self.auto_complete:
            self.incoming.put_nowait(json.dumps(dict(type="SpeechMetadata")))

    async def close(self):
        self.state = State.CLOSED
        self.incoming.put_nowait(None)


class SocketBoundaryTTS(DuoFluxTTS):
    """Only the socket factory is fake; all Flux lifecycle/receiver code is real."""
    def __init__(self, character):
        super().__init__(api_key="offline-placeholder", sample_rate=16000,
            settings=self.Settings(voice=f"offline-{character}"))
        self.sockets = []

    async def _websocket_connect(self, uri, **kwargs):
        socket = MemorySocket()
        self.sockets.append(socket)
        return socket


# No real endpoint is written in this repository; nothing here connects to one.
TEST_LLM_URL = "http://llm.invalid:8000/v1"


class Harness:
    def __init__(self, fault="", deadline=.7, real_lifecycle=False, *, config=None,
                 auto_switch=None, addressee=None, tts_kwargs=None):
        if auto_switch is None:
            auto_switch = EXISTING_STAY_MODE.get()
        config = (configuration({"DUO_TEST": "1", "DUO_FAULT": fault, "DUO_LLM_URL": TEST_LLM_URL,
                                 "DUO_AUTO_SWITCH": "1" if auto_switch else "0"}) |
                  {"cancel_join_bound_s": CANCEL_JOIN_BOUND.get()} | (config or {}))
        self.config = config
        self.scripted_stay = addressee is None
        self.addressee = (addressee or FakeAddressee()) if config["auto_switch"] else None
        self.transport, self.stt = FakeTransport(), FakeSTT()
        self.llm = make_llm(config, service_class=FakeLLM)
        tts_class = SocketBoundaryTTS if real_lifecycle else FakeTTS
        self.services = {name: tts_class(name, **(tts_kwargs or {})) for name in ("angel", "devil")}
        self.worker, self.control = build_worker(self.transport, self.stt, self.llm, self.services,
                                                  config=config, deadline_secs=deadline, addressee=self.addressee)
        self.stt.coordinator = self.control
        if self.addressee:
            self.addressee.route_token = lambda: self.control.route_read[0] if self.control.route_read else None
        self.witness = SinkWitness()
        self.worker.add_observer(self.witness)
        self.events, self.outcomes = [], []
        self.control.event = lambda kind, **data: self.events.append((kind, data, time.monotonic()))
        self.seq = 0
        self.runner = WorkerRunner(handle_sigint=False)

    async def start(self):
        await self.runner.add_workers(self.worker)
        self.running = asyncio.create_task(self.runner.run())
        self.coordinating = asyncio.create_task(self.control.run())
        await until(lambda: self.transport.sink._media_senders)
        assert await self.worker.flush_pipeline(timeout=.5)
        await self.worker.queue_frame(InputTransportMessageFrame(message={
            "label": "rtvi-ai", "type": "client-ready", "id": "ready",
            "data": {"version": "1.0.0", "about": {"library": "duo-offline-test"}}}))
        await until(lambda: any(m.get("type") == "bot-ready" for m in self.transport.client.messages))

    def states(self):
        return [m["data"] for m in self.transport.client.messages
                if isinstance(m, dict) and m.get("type") == "server-message"
                and isinstance(m.get("data"), dict) and m["data"].get("type") == "duo"]

    async def send(self, op="toggle", have="angel", seq=None):
        self.seq = seq if seq is not None else self.seq + 1
        data = dict(v=1, op=op, session="boot", seq=self.seq, have=have)
        # Real RTVI envelope/parser/handler, not direct _request invocation.
        await self.worker.queue_frame(InputTransportMessageFrame(message={
            "label": "rtvi-ai", "type": "client-message", "id": f"req-{self.seq}",
            "data": {"t": "duo", "d": data}}))
        return self.seq

    async def outcome(self, seq=None):
        seq = self.seq if seq is None else seq
        await until(lambda: any(s["seq"] == seq for s in self.states()))
        await until(lambda: not self.control.gate.closed or self.control.stopping)
        return next(s for s in reversed(self.states()) if s["seq"] == seq)

    async def hello(self, have="angel"):
        await self.send("hello", have)
        return await self.outcome()

    async def turn(self, text="What should I do?"):
        await self.worker.queue_frames([
            ProposedUserStartedSpeakingFrame(),
            TranscriptionFrame(text=text, user_id="user", timestamp="now", finalized=True),
            ProposedUserStoppedSpeakingFrame(),
        ])

    async def close(self):
        # Stop the coordinator before the worker is torn down.
        self.control.stopping = True
        self.coordinating.cancel()
        await asyncio.gather(self.coordinating, return_exceptions=True)
        await self.worker.cancel()
        await asyncio.wait_for(self.running, 3)
        await self.worker.rtvi.stop_interrupt_repeats()
        if self.addressee:
            assert self.addressee.active == 0
            assert not self.control.hold.timers
            if self.scripted_stay:
                asked = set(self.addressee.asked_tokens)
                assert asked <= self.witness.routes
                skipped = self.witness.routes - asked
                # Shutdown/supersession can withdraw a just-admitted future turn.
                assert all(self.control.hold.decision(t) in ("stopped", "superseded") for t in skipped)
                assert len(self.addressee.calls) == len(asked) == len(self.witness.routes) - len(skipped)


@asynccontextmanager
async def pipeline(fault="", deadline=.7, real_lifecycle=False, *, config=None,
                   auto_switch=None, addressee=None, tts_kwargs=None):
    if auto_switch is None:
        auto_switch = EXISTING_STAY_MODE.get()
    h = Harness(fault=fault, deadline=deadline, real_lifecycle=real_lifecycle,
                config=config, auto_switch=auto_switch, addressee=addressee, tts_kwargs=tts_kwargs)
    try:
        await h.start()
        yield h
    finally:
        await h.close()


def local_import_closure(start="duo_bot.py"):
    """Every local module the bot reaches through imports, at module level or inside a function."""
    import ast
    root = Path(__file__).parent
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(ast.parse((root / name).read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            else:
                continue
            todo += [f"{module.split('.')[0]}.py" for module in modules
                     if (root / f"{module.split('.')[0]}.py").exists()]
    return seen


def test_identity_binds_every_local_module_the_bot_imports(tmp_path, monkeypatch):
    import shutil
    import duo_bot
    closure = local_import_closure()
    # bot() imports these three inside the function; a top-level scan would miss them.
    assert {"demo_liveness.py", "sctp_flight_fix.py", "settings.py"} <= closure
    assert set(duo_bot.IDENTITY_SOURCES) == closure and len(duo_bot.IDENTITY_SOURCES) == len(closure)
    # Mutation, on a copy: changing any one bound file changes the identity digest.
    server = Path(__file__).parent
    for name in duo_bot.IDENTITY_SOURCES:
        shutil.copy(server / name, tmp_path / name)
    (tmp_path / "fixtures").mkdir()
    shutil.copy(server / "fixtures" / "duo_addressee_accept.json", tmp_path / "fixtures")
    monkeypatch.setattr(duo_bot, "__file__", str(tmp_path / "duo_bot.py"))
    config = configuration({})
    original = identity(config)
    assert original == identity(config)
    monkeypatch.undo()
    assert original == identity(config)  # The copy is the same identity as the tree.
    monkeypatch.setattr(duo_bot, "__file__", str(tmp_path / "duo_bot.py"))
    for name in duo_bot.IDENTITY_SOURCES:
        source = (tmp_path / name).read_bytes()
        (tmp_path / name).write_bytes(source + b"\n# changed\n")
        changed = identity(config)
        assert changed["config_sha256"] != original["config_sha256"], name
        assert [n for n in changed["code_sha256"] if changed["code_sha256"][n] != original["code_sha256"][n]] == [name]
        (tmp_path / name).write_bytes(source)
    assert identity(config) == original


def test_configuration_fault_guard_and_identity():
    config = configuration({})
    assert config["llm_url"] is None and config["llm_url_source"] is None  # No endpoint is built in.
    assert config["llm_model"] == "phonellm-max-alpha-2-nvfp4"
    assert config["voices"] == dict(angel="flux-hannah-en", devil="flux-rufus-en")
    assert config["idle_timeout_secs"] is None and config["enable_thinking"] is False
    assert config["temperature"] == 0 and config["stt_model"] == "flux-general-en"
    manifest = identity(config)
    assert manifest["config"]["duo_test"] is False and manifest["config"]["fault"] == ""
    assert manifest["config"]["tts_reconnect_on_error"] is False
    assert manifest["versions"]["pipecat-ai"] == "1.15.0"
    assert set(manifest["code_sha256"]) == local_import_closure()
    for name, digest in manifest["code_sha256"].items():
        assert digest == hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
    tested = identity(configuration({"DUO_TEST": "1", "DUO_FAULT": "tts-fail-next"}))
    assert tested["config"]["duo_test"] is True and tested["config"]["fault"] == "tts-fail-next"
    for fault in ("tts-fail-next", "drop-state-1", "drop-state-4"):
        with pytest.raises(ValueError):
            fault_config({"DUO_FAULT": fault})
        assert fault_config({"DUO_TEST": "1", "DUO_FAULT": fault}) == fault
    for bad in ("oops", "drop-state-0", "drop-state--1"):
        with pytest.raises(ValueError):
            configuration({"DUO_TEST": "1", "DUO_FAULT": bad})
    for bad in ({"DUO_LLM_TEMPERATURE": "1"}, {"DUO_LLM_ENABLE_THINKING": "true"}):
        with pytest.raises(ValueError):
            configuration(bad)


def test_llm_endpoint_comes_only_from_the_environment():
    other = "http://other.invalid:8000/v1"
    for env, url, source in (({"DUO_LLM_URL": TEST_LLM_URL}, TEST_LLM_URL, "DUO_LLM_URL"),
                             ({"LOCAL_LLM_URL": other}, other, "LOCAL_LLM_URL"),
                             ({"DUO_LLM_URL": TEST_LLM_URL, "LOCAL_LLM_URL": other}, TEST_LLM_URL, "DUO_LLM_URL"),
                             ({"DUO_LLM_URL": "", "LOCAL_LLM_URL": other}, other, "LOCAL_LLM_URL")):
        config = configuration(env)
        assert (config["llm_url"], config["llm_url_source"]) == (url, source)
        assert require_llm_url(config) == url
        assert identity(config)["config"]["llm_url_source"] == source
        assert str(make_llm(config, service_class=FakeLLM)._client.base_url).rstrip("/") == url
    # Unset or empty: configuration() still answers (duo_voice_samples.py reads the voices from it),
    # and whatever would call the LLM refuses, naming the variable.
    for env in ({}, {"DUO_LLM_URL": ""}, {"DUO_LLM_URL": "", "LOCAL_LLM_URL": ""}):
        config = configuration(env)
        assert config["llm_url"] is None and config["llm_url_source"] is None
        for refuse in (require_llm_url, lambda c: make_llm(c, service_class=FakeLLM)):
            with pytest.raises(ValueError, match="DUO_LLM_URL is not set") as error:
                refuse(config)
            assert "LOCAL_LLM_URL" in str(error.value) and "no default" in str(error.value)
    # A value that is not an endpoint is refused by name, never echoed.
    for bad in ("llm.invalid:8000/v1", "ftp://llm.invalid/v1", "http://", "http://llm.invalid:port/v1",
                "http://u:p@llm.invalid:8000/v1"):
        with pytest.raises(ValueError, match="^DUO_LLM_URL must") as error:
            require_llm_url(configuration({"DUO_LLM_URL": bad}))
        assert "llm.invalid" not in str(error.value) and "u:p" not in str(error.value)
    with pytest.raises(ValueError, match="^LOCAL_LLM_URL must be an http"):
        require_llm_url(configuration({"LOCAL_LLM_URL": "llm.invalid"}))


def test_bot_script_refuses_to_start_without_an_llm_endpoint():
    """The script's own entry point, in a process of its own, exits before it reaches the runner.

    The runner's main() is replaced, as in the GC test below, so a regression cannot bind a port either.
    """
    import os
    import subprocess
    import sys
    server = Path(__file__).parent
    code = """
import runpy, sys
import pipecat.runner.run as runner
runner.main = lambda: print("RUNNER")
sys.argv = ["duo_bot.py", "--port", "9"]
runpy.run_path("duo_bot.py", run_name="__main__")
"""
    def run(**variables):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("DUO_") and name not in ("LOCAL_LLM_URL", "LOCAL_LLM_MODEL")}
        return subprocess.run([sys.executable, "-c", code], cwd=server, capture_output=True, text=True, timeout=120,
                              env=dict(environment, PYTHONPATH=str(server), PYTHONDONTWRITEBYTECODE="1", **variables))
    for variables, message in (({}, "DUO_LLM_URL is not set"), ({"DUO_LLM_URL": ""}, "DUO_LLM_URL is not set"),
                               ({"LOCAL_LLM_URL": "llm.invalid:8000"}, "LOCAL_LLM_URL must be an http(s) URL")):
        result = run(**variables)
        assert result.returncode == 1 and "RUNNER" not in result.stdout
        assert result.stderr.strip().splitlines()[-1].startswith("duo bot not started: " + message)
        assert "Traceback" not in result.stderr and "llm.invalid" not in result.stderr
    for variables in ({"DUO_LLM_URL": TEST_LLM_URL}, {"LOCAL_LLM_URL": TEST_LLM_URL}):
        result = run(**variables)
        assert result.returncode == 0 and result.stdout.splitlines()[-1] == "RUNNER", result.stderr[-2000:]


def test_no_tracked_source_names_an_llm_endpoint():
    """The bot and its launcher carry no endpoint literal: every URL in them is a known public service."""
    import re
    root = Path(__file__).parents[1]
    allowed = ("wss://api.deepgram.com/", "http://<host>:<port>/v1")
    for path in (root / "server/duo_bot.py", root / "hil/duo_identity.py"):
        found = re.findall(r"(?:https?|wss?)://[^\s\"'`)]+", path.read_text())
        assert [url for url in found if not url.startswith(allowed)] == [], path.name
    assert "DEFAULT_LLM_URL" not in (root / "server/duo_bot.py").read_text()


def test_duo_stt_gets_the_character_keyterms_only_when_asked(monkeypatch):
    import dataclasses
    import demo_policy
    from duo_bot import make_stt_for
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
    off, on = configuration({}), configuration({"DUO_STT_KEYTERMS": "1"})
    assert off["stt_keyterms"] is None and on["stt_keyterms"] == ["Angel", "Devil"]
    assert identity(on)["config"]["stt_keyterms"] == ["Angel", "Devil"]
    with pytest.raises(ValueError):
        configuration({"DUO_STT_KEYTERMS": "Angel,Devil"})
    # Constructors only; no connection is opened.
    plain, tuned = make_stt_for(off, "offline-placeholder"), make_stt_for(on, "offline-placeholder")
    assert type(plain) is type(tuned) is DeepgramFluxSTTService
    assert plain.settings.keyterm == [] and tuned.settings.keyterm == ["Angel", "Devil"]
    # Nothing but the keyterms differs from the service the flag-off pipeline gets.
    assert [f.name for f in dataclasses.fields(plain.settings)
            if getattr(plain.settings, f.name) != getattr(tuned.settings, f.name)] == ["keyterm"]
    assert tuned.sample_rate == plain.sample_rate
    # Flag off, the duo bot still asks the shared demo profile for its service, unchanged.
    calls = []
    monkeypatch.setattr("duo_bot.make_stt", lambda key, **kw: calls.append((key, kw)) or "demo-profile-stt")
    assert make_stt_for(off, "offline-placeholder") == "demo-profile-stt"
    assert calls == [("offline-placeholder", {"profile": "demo"})]
    assert make_stt_for(on, "offline-placeholder") != "demo-profile-stt" and len(calls) == 1
    # The demo bot's own profile has no keyterms either way.
    assert demo_policy.make_stt("offline-placeholder").settings.keyterm == []


def test_gc_flags_are_off_unless_asked_and_recorded_in_identity(monkeypatch):
    import gc
    import duo_bot
    off, on = configuration({}), configuration({"DUO_GC_LOG": "1", "DUO_GC_FREEZE": "1"})
    assert (off["gc_log"], off["gc_freeze"]) == (False, False)
    assert (on["gc_log"], on["gc_freeze"]) == (True, True)
    assert identity(on)["config"]["gc_freeze"] is True and identity(on)["config"]["gc_log"] is True
    for name in ("DUO_GC_LOG", "DUO_GC_FREEZE"):
        with pytest.raises(ValueError, match=name):
            configuration({name: "yes"})
    # Each flag starts its own half, the log first, and neither starts without its flag.
    started = []
    monkeypatch.setattr(duo_bot, "SlowCollections",
                        lambda: type("Log", (), {"start": lambda self: started.append("log")})())
    monkeypatch.setattr(duo_bot, "freeze_startup_heap", lambda: started.append("freeze"))
    callbacks = list(gc.callbacks)
    duo_bot.start_gc_diagnostics(off)
    assert started == [] and gc.callbacks == callbacks
    duo_bot.start_gc_diagnostics(dict(off, gc_log=True))
    duo_bot.start_gc_diagnostics(dict(off, gc_freeze=True))
    assert started == ["log", "freeze"]
    started.clear()
    duo_bot.start_gc_diagnostics(on)
    assert started == ["log", "freeze"]


def test_gc_log_keeps_only_collections_of_20_ms_or_more():
    import duo_bot
    ticks = iter([10.0, 10.019, 20.0, 20.025, 30.0, 30.091])
    log = duo_bot.SlowCollections(clock=lambda: next(ticks))
    log("stop", dict(generation=2, collected=1, uncollectable=0))  # Installed mid-collection: no start seen.
    for generation, collected in ((0, 3), (1, 7), (2, 40000)):
        log("start", dict(generation=generation, collected=0, uncollectable=0))
        log("stop", dict(generation=generation, collected=collected, uncollectable=0))
    rows = [log.rows.get_nowait() for _ in range(log.rows.qsize())]
    assert rows == [dict(generation=1, ms=25.0, collected=7), dict(generation=2, ms=91.0, collected=40000)]
    assert duo_bot.GC_LOG_FLOOR_S == 0.020


def test_gc_log_reports_a_real_collection_from_its_own_thread():
    import gc
    import threading
    import duo_bot
    rows, threads = [], set()
    log = duo_bot.SlowCollections(floor_s=0, sink=lambda row: (rows.append(row), threads.add(threading.get_ident())))
    log.start()
    try:
        assert log in gc.callbacks
        gc.collect()
    finally:
        log.stop()  # Joins the writer, so every row measured before this line has been written.
    assert log not in gc.callbacks and not log.writer.is_alive()
    assert any(row["generation"] == 2 for row in rows)
    assert all(set(row) == {"generation", "ms", "collected"} for row in rows)
    assert threads and threading.get_ident() not in threads


def test_gc_freeze_imports_what_a_session_imports_then_collects_then_freezes():
    import ast
    import importlib.util
    import duo_bot
    source = ast.parse(Path(duo_bot.__file__).read_text())
    bot = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "bot")
    lazy = {node.module for node in ast.walk(bot) if isinstance(node, ast.ImportFrom)}
    lazy |= {alias.name for node in ast.walk(bot) if isinstance(node, ast.Import) for alias in node.names}
    # The recorder is imported only for a recorded session; everything else a session imports is listed.
    assert lazy - {"aec_session_recorder"} <= set(duo_bot.SESSION_IMPORTS)
    for name in duo_bot.SESSION_IMPORTS:
        assert importlib.util.find_spec(name) is not None, name
    steps = []
    collector = type("Collector", (), dict(collect=lambda self: steps.append("collect"),
                                           freeze=lambda self: steps.append("freeze"),
                                           get_freeze_count=lambda self: 7))()
    duo_bot.freeze_startup_heap(import_module=lambda name: steps.append(name), collector=collector)
    assert steps == [*duo_bot.SESSION_IMPORTS, "collect", "freeze"]


def test_gc_freeze_leaves_the_session_factories_nothing_to_import():
    """After the freeze, constructing what a session constructs must not import a module:
    whatever is imported later stays in the heap every full collection walks.
    A process of its own; constructors only, no connection and no credential."""
    import json
    import os
    import subprocess
    import sys
    server = Path(__file__).parent
    code = """
import json, os, sys
os.environ["DEEPGRAM_API_KEY"] = "offline-placeholder"
import duo_bot
duo_bot.freeze_startup_heap()
before = set(sys.modules)
config = duo_bot.configuration({"DUO_LLM_URL": "http://llm.invalid:8000/v1"})
made = [duo_bot.make_llm(config), duo_bot.make_llm(dict(config, auto_switch=True)), duo_bot.make_tts(config),
        duo_bot.make_stt_for(config, "offline-placeholder"),
        duo_bot.make_stt_for(duo_bot.configuration({"DUO_STT_KEYTERMS": "1"}), "offline-placeholder"),
        duo_bot.Addressee("offline-placeholder", budget=0.5, model="jev-1.14.0")]
print("LEFT " + json.dumps(sorted(set(sys.modules) - before)))
"""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("DUO_")}
    result = subprocess.run([sys.executable, "-c", code], cwd=server, capture_output=True, text=True, timeout=120,
                            env=dict(environment, PYTHONPATH=str(server), PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode == 0, result.stderr[-2000:]
    left = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("LEFT "))[5:])
    assert left == []


def test_process_start_applies_the_flags_once(monkeypatch):
    import duo_bot
    started = []
    monkeypatch.setattr(duo_bot, "_process_started", False)
    monkeypatch.setattr(duo_bot, "start_gc_diagnostics", started.append)
    config = configuration({"DUO_GC_LOG": "1"})
    duo_bot.start_process(config)
    assert started == [config]
    with pytest.raises(RuntimeError, match="already started"):
        duo_bot.start_process(config)
    assert started == [config]


@pytest.mark.parametrize("log,freeze", [(False, False), (True, False), (False, True), (True, True)])
def test_gc_flags_reach_the_process_before_the_server_starts(log, freeze):
    """The real entry point in a process of its own, with the server's main() replaced: nothing binds.

    hil/test_duo_identity.py does the same for the coordinator's launch path, serve().
    """
    import json
    import os
    import subprocess
    import sys
    server = Path(__file__).parent
    code = """
import gc, json, runpy, sys
import pipecat.runner.run as runner
runner.main = lambda: print("STATE " + json.dumps(dict(
    frozen=gc.get_freeze_count(), unfrozen=len(gc.get_objects()),
    logs=[type(callback).__name__ for callback in gc.callbacks])))
sys.argv = ["duo_bot.py", "--port", "9"]
runpy.run_path("duo_bot.py", run_name="__main__")
"""
    def state(**flags):
        environment = {name: value for name, value in os.environ.items() if not name.startswith("DUO_")}
        result = subprocess.run([sys.executable, "-c", code], cwd=server, capture_output=True, text=True, timeout=120,
                                env=dict(environment, PYTHONPATH=str(server), PYTHONDONTWRITEBYTECODE="1",
                                         DUO_LLM_URL=TEST_LLM_URL, **flags))
        assert result.returncode == 0, result.stderr[-2000:]
        return json.loads(next(line for line in result.stdout.splitlines() if line.startswith("STATE "))[6:]), result
    flags = dict(DUO_GC_LOG="1") if log else {}
    flags.update(dict(DUO_GC_FREEZE="1") if freeze else {})
    seen, output = state(**flags)
    assert seen["logs"] == (["SlowCollections"] if log else [])
    if freeze:
        # The import heap is frozen and what is left to collect is a small fraction of it.
        assert seen["unfrozen"] * 20 < seen["frozen"] and "DUO gc-freeze" in output.stderr
    else:
        assert seen["frozen"] < seen["unfrozen"] and "DUO gc-freeze" not in output.stderr


async def test_real_switcher_output_marker_already_active_and_silent_note():
    async with pipeline() as h:
        result = await h.hello()
        assert result["character"] == "angel" and result["rev"] == 1
        assert not h.llm.calls and not h.services["angel"].speaks
        assert not [event for event in h.events if event[0] == "strategy-switched"]
        assert len([e for e in h.events if e[0] == "verified"]) == 1
        assert h.witness.markers and h.witness.interruptions
        assert any(m.get("type") == "bot-interrupted" for m in h.transport.client.messages)
        assert any(m.get("content") == silent_note("angel", "angel")
                   for m in h.control.context.get_messages())
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        assert len([event for event in h.events if event[0] == "strategy-switched"]) == 1
        await h.turn()
        await until(lambda: h.services["devil"].speaks)
        assert h.llm.calls[-1][0] == character_instructions("devil")
        assert not h.services["angel"].speaks
        await until(lambda: h.control.state.verification is None)
        assert h.transport.client.audio


@pytest.mark.parametrize("when", ["before-audio", "playback"])
@pytest.mark.parametrize("upstream_delay", [0, .06])
async def test_interrupt_round_trip_old_tts_and_history_settle_before_any_mutation(when, upstream_delay):
    async with pipeline() as h:
        await h.hello()
        h.llm.updates.clear()
        h.stt.worker_interrupt_delay = .025
        h.stt.upstream_interrupt_delay = upstream_delay
        angel = h.services["angel"]
        angel.interrupt_delay = .035
        if when == "before-audio":
            h.llm.generate.clear()
        else:
            angel.release.clear()
            h.llm.remainder.clear()
        await h.turn()
        await until(lambda: h.llm.calls)
        if when == "playback":
            await until(lambda: angel.speaks)
        # Delay processing in the REAL assistant aggregator, including its
        # history settlement, instead of replacing it with an enqueue fake.
        assistant = h.control.assistant_aggregator
        original_process = assistant.process_frame
        settled = []
        async def delayed_history(frame, direction):
            if isinstance(frame, InterruptionFrame):
                await asyncio.sleep(.035)
            await original_process(frame, direction)
            if isinstance(frame, InterruptionFrame):
                settled.append(time.monotonic())
        assistant.process_frame = delayed_history
        before = len(h.events)
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        interrupt_done = max(angel.frame_interruptions)
        assert h.llm.updates[0][1] > interrupt_done
        assert h.llm.updates[0][1] > settled[-1]
        verification = next(t for kind, data, t in h.events[before:] if kind == "verified")
        publish = next(t for kind, data, t in h.events[before:] if kind == "outcome")
        assert publish >= verification
        if when == "playback":
            assert any(m["role"] == "assistant" and m["content"].startswith("[Angel]")
                       for m in h.control.context.get_messages())
        h.llm.generate.set()
        h.llm.remainder.set()
        await h.turn("Now a new question")
        await until(lambda: h.services["devil"].speaks)
        assert h.llm.response_ids[0] not in h.services["devil"].starts
        assert 1 not in h.services["devil"].text_responses
        assert h.llm.calls[-1][0] == character_instructions("devil")


@pytest.mark.parametrize("lost_mutation", [ManuallySwitchServiceFrame, SilentNote])
async def test_surviving_marker_cannot_commit_missing_mutation(lost_mutation):
    async with pipeline() as h:
        await h.hello()
        h.stt.sabotage = lost_mutation
        await h.send()
        result = await h.outcome()
        assert result["op"] == "error"
        assert h.control.state.character == "angel" and h.control.state.rev == 1
        assert h.control.switcher.strategy.active_service is h.services["angel"]
        assert h.llm.settings.system_instruction == character_instructions("angel")


async def test_mutation_interruption_fails_even_if_all_mutations_survive():
    async with pipeline() as h:
        await h.hello()
        h.stt.interrupt_marker = True
        await h.send()
        assert (await h.outcome())["op"] == "error"
        assert h.control.state.rev == 1
        assert h.control.switcher.strategy.active_service is h.services["angel"]


async def test_unusable_target_error_remains_cached_after_recovery():
    async with pipeline() as h:
        await h.hello()
        h.services["angel"].release.clear()
        prior_interruptions = len(h.witness.interruptions)
        await h.turn()
        await until(lambda: h.services["angel"].speaks and len(h.witness.interruptions) > prior_interruptions)
        interruptions = len(h.witness.interruptions)
        epochs = {name: service.connection_generation for name, service in h.services.items()}
        notes = list(h.control.context.get_messages())
        await h.services["devil"].set_usable(False)
        await h.send()
        error = await h.outcome()
        assert error["op"] == "error"
        assert len(h.witness.interruptions) == interruptions
        assert {name: service.connection_generation for name, service in h.services.items()} == epochs
        assert h.control.context.get_messages() == notes
        await h.services["devil"].set_usable(True)
        before = len(h.states())
        await h.send(seq=2)
        await until(lambda: len(h.states()) > before)
        assert h.states()[-1] == error
        assert h.control.state.character == "angel"


async def test_gate_holds_turn_and_fences_already_open_aggregator_inference():
    async with pipeline() as h:
        await h.hello()
        # Open a real aggregator turn before admission closes.
        await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
        await h.worker.queue_frame(TranscriptionFrame(text="An open turn", user_id="u",
                                                     timestamp="now", finalized=True))
        assert await h.worker.flush_pipeline(timeout=.5)
        assert not h.llm.calls
        h.stt.flush_delay = .10
        await h.send()
        await until(lambda: h.control.gate.closed)
        # A controller can finish independently of ingress: exercise its public
        # push_context_frame path while closed, using the real aggregator.
        user = h.control.user_aggregator
        await user.push_aggregation()
        await until(lambda: h.control.gate.fence.held)
        await h.turn("A buffered future turn")
        await until(lambda: h.control.gate.held)
        assert not h.llm.calls
        result = await h.outcome()
        assert result["character"] == "devil"
        await until(lambda: h.llm.calls)
        assert all(prompt == character_instructions("devil") for prompt, _ in h.llm.calls)


async def test_end_frame_is_never_held_by_closed_gate():
    async with pipeline() as h:
        h.control.gate.close()
        await h.worker.queue_frame(EndFrame())
        await asyncio.wait_for(asyncio.shield(h.running), 1)
        assert h.worker.has_finished()
        assert not h.control.gate.held


async def test_gate_release_preserves_arrival_order_when_a_push_yields():
    async with pipeline() as h:
        await h.hello()
        gate = h.control.gate
        gate.close()
        entered, release = asyncio.Event(), asyncio.Event()
        forwarded = []
        original_push = gate.push_frame
        async def delayed_push(frame, direction=DOWN):
            if isinstance(frame, InterimTranscriptionFrame):
                if frame.text == "first":
                    entered.set()
                    await release.wait()
                forwarded.append(frame.text)
            await original_push(frame, direction)
        gate.push_frame = delayed_push
        await h.worker.queue_frame(InterimTranscriptionFrame("first", "u", "now"))
        await until(lambda: gate.held)
        reopening = asyncio.create_task(gate.reopen())
        try:
            await entered.wait()
            await h.worker.queue_frame(InterimTranscriptionFrame("second", "u", "now"))
            await until(lambda: gate.held)
            assert gate.closed and forwarded == []
        finally:
            release.set()
            await reopening
        assert forwarded == ["first", "second"] and not gate.closed


async def test_absolute_deadline_with_continuous_audio_and_verified_rollback():
    async with pipeline(deadline=.16) as h:
        await h.hello()
        # Unrelated system/audio frames continue to the real sink while the
        # control probe is deliberately stalled longer than the deadline.
        h.stt.flush_delay = .22
        await h.send()
        await h.stt.flush_started.wait()
        started = time.monotonic()
        async def audio():
            for _ in range(70):
                await h.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16000, 1))
                await asyncio.sleep(.004)
        pumping = asyncio.create_task(audio())
        await h.turn("Held during deadline and rollback")
        await asyncio.sleep(.17)
        assert h.control.gate.closed
        assert not h.llm.calls
        assert any(started <= t <= started + .17 for t in h.witness.audio)
        result = await h.outcome()
        await pumping
        assert result["op"] == "error" and result["reason"] == "deadline"
        assert time.monotonic() - started < .6
        assert h.control.state.character == "angel"
        assert h.llm.settings.system_instruction == character_instructions("angel")
        assert any(t is InputAudioRawFrame for t, _ in h.stt.frames)
        assert await h.worker.flush_pipeline(timeout=.5)
        assert h.llm.settings.system_instruction == character_instructions("angel")


async def test_watchdog_pongs_are_serviced_while_a_flush_is_delayed():
    async with pipeline() as h:
        class Watchdog:
            received = []
            def pong(self, data):
                self.received.append((data, time.monotonic()))
        watchdog = Watchdog()
        h.control.liveness = watchdog
        h.stt.flush_delay = .12
        await h.send("hello")
        await h.stt.flush_started.wait()
        h.control.enqueue(Pong({"seq": 17}))
        await until(lambda: watchdog.received, timeout=.08)
        assert h.control.gate.closed
        assert (await h.outcome())["op"] == "state"


async def test_watchdog_pongs_are_serviced_during_voice_reconnect():
    async with pipeline() as h:
        await h.hello()
        class Watchdog:
            received = []
            def pong(self, data):
                self.received.append(data)
        watchdog = Watchdog()
        h.control.liveness = watchdog
        angel = h.services["angel"]
        angel.connect_started.clear()
        angel.connect_delay = .12
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        await angel.connect_started.wait()
        await h.send()
        await until(lambda: h.control.phase == "retirement-wait" and h.control.gate.closed)
        h.control.enqueue(Pong({"seq": 23}))
        await until(lambda: watchdog.received, timeout=.08)
        assert h.control.gate.closed
        assert (await h.outcome())["character"] == "angel"


async def test_production_factory_request_invariants_survive_switch_and_revert(monkeypatch):
    config = configuration({})
    monkeypatch.setenv("DEEPGRAM_API_KEY", "offline-placeholder")
    voices = make_tts(config)  # Constructor only; no setup or provider connection.
    assert {name: service.settings.voice for name, service in voices.items()} == config["voices"]
    async with pipeline(fault="tts-fail-next") as h:
        def assert_params():
            params = h.llm.build_chat_completion_params({"messages": []})
            assert params["model"] == config["llm_model"]
            assert params["temperature"] == 0 and params["max_completion_tokens"] == 250
            assert params["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        assert_params()
        await h.hello()
        assert_params()
        await h.send()
        await h.outcome()
        assert_params()
        await h.turn()
        await until(lambda: h.states()[-1].get("reason") == "reverted")
        assert_params()


async def test_coordinator_failure_is_supervised_and_cancels_worker():
    async with pipeline() as h:
        async def fail_ready(*args):
            raise RuntimeError("offline ready failure")
        h.worker.rtvi.set_bot_ready = fail_ready
        h.control.enqueue(ClientReady())
        await until(lambda: h.coordinating.done())
        assert isinstance(h.coordinating.exception(), RuntimeError)
        await asyncio.wait_for(asyncio.shield(h.running), 2)
        assert h.worker.has_finished() and h.control.gate.closed


async def test_untyped_coordinator_event_ends_the_session_instead_of_spinning():
    async with pipeline() as h:
        h.control.enqueue(("pong", {"seq": 1}))  # The tuple form the typed events replaced.
        await until(lambda: h.coordinating.done())
        assert isinstance(h.coordinating.exception(), TypeError)
        await asyncio.wait_for(asyncio.shield(h.running), 2)
        assert h.worker.has_finished() and h.control.gate.closed and not h.control.deferred


async def test_request_admission_is_bounded_and_never_refuses_a_pong():
    from types import SimpleNamespace
    from duo_bot import MAX_PENDING_REQUESTS
    async with pipeline(deadline=3) as h:
        await h.hello()
        pongs = []
        h.control.liveness = SimpleNamespace(pong=pongs.append)
        publish = h.control._publish
        entered, release = asyncio.Event(), asyncio.Event()
        async def parked(result):
            entered.set()
            await release.wait()
            await publish(result)
        h.control._publish = parked
        try:
            await h.send(seq=2)  # The coordinator parks in this toggle's publication.
            await entered.wait()
            flood = 3 * MAX_PENDING_REQUESTS
            for seq in range(3, 3 + flood):
                await h.send(seq=seq)
            await until(lambda: h.control.refused_requests == flood - MAX_PENDING_REQUESTS)
            assert h.control.pending_requests == MAX_PENDING_REQUESTS
            # One admitted request may be in transit between the queue and the deferred list.
            assert MAX_PENDING_REQUESTS - 1 <= len(h.control.deferred) + h.control.queue.qsize() <= MAX_PENDING_REQUESTS
            refusals = [data["refused"] for kind, data, _ in h.events if kind == "request-refused"]
            assert refusals == [1, 2, 4, 8, 16]
            h.control.enqueue(Pong({"seq": 7}))
            await until(lambda: pongs)
            assert pongs == [{"seq": 7}] and h.control.pending_requests == MAX_PENDING_REQUESTS
        finally:
            release.set()
        last_admitted = 2 + MAX_PENDING_REQUESTS
        await h.outcome(last_admitted)
        await until(lambda: h.control.pending_requests == 0)
        # A refused request left no trace, so its retransmission runs exactly once.
        assert h.control.state.sessions["boot"].high_water == last_admitted
        rev = h.control.state.rev
        await h.send(seq=last_admitted + 1)
        assert (await h.outcome(last_admitted + 1))["rev"] == rev + 1
        await h.send(seq=last_admitted + 1)
        await until(lambda: h.control.pending_requests == 0 and h.control.phase == "idle")
        assert h.control.state.rev == rev + 1 and not h.control.stopping


async def test_untyped_event_during_a_mutation_ends_the_session_without_rolling_back():
    async with pipeline() as h:
        await h.hello()
        h.llm.settings_started.clear()
        h.llm.setting_delay = .1
        await h.send()
        await h.llm.settings_started.wait()
        assert h.control.phase == "mutate"
        h.control.enqueue(("legacy-event", ()))
        await until(lambda: h.control.stopping)
        await until(h.worker.has_finished)
        assert h.control.gate.closed
        # Not a recoverable switch failure: nothing is published for the request.
        assert not any(state["seq"] == 2 for state in h.states())
        assert not any(kind == "outcome" and data["seq"] == 2 for kind, data, _ in h.events)


async def test_untyped_event_cannot_skip_the_cleanup_of_the_operation_it_interrupts():
    control = Harness().control  # Built, never run: this test is the queue's only reader.
    entered, cleaned = asyncio.Event(), asyncio.Event()
    owned = []
    async def operation():
        owned.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.01)
            cleaned.set()
    waiting = asyncio.create_task(control._wait_with_events(operation()))
    await entered.wait()
    control.enqueue(("legacy-event", ()))
    with pytest.raises(TypeError):
        await waiting
    # The operation was cancelled and joined before the error left the wait.
    assert owned[0].done() and cleaned.is_set()
    assert control.stopping and control.gate.closed and control.queue.empty()


@pytest.mark.parametrize("error_kind", ["json", "receive"])
async def test_real_flux_lifecycle_captures_receive_epoch_at_socket_boundary(error_kind):
    import json
    async with pipeline(real_lifecycle=True) as h:
        await h.hello()
        await h.send()
        await h.outcome()
        devil = h.services["devil"]
        old_socket, old_task = devil.sockets[-1], devil._receive_task
        await h.turn()
        await until(lambda: h.control.state.verification is None)
        await h.send()
        await h.outcome()
        await h.send()
        await h.outcome()
        assert old_task.done() and old_socket.state is State.CLOSED
        new_socket = devil.sockets[-1]
        assert new_socket is not old_socket
        new_socket.auto_complete = False
        await h.turn("The first reply on the new socket")
        await until(lambda: h.control.state.verification and h.control.state.verification.response)
        record = h.control.state.verification
        raw_error = json.dumps(dict(type="Error", code="offline", description="old queued error"))
        old_socket.incoming.put_nowait(raw_error)
        await asyncio.sleep(.02)
        assert h.control.state.verification == record and not h.control.gate.closed
        if error_kind == "json":
            new_socket.incoming.put_nowait(raw_error)
        else:
            new_socket.incoming.put_nowait(OSError("offline receive failure"))
        await until(lambda: h.states()[-1].get("reason") == "reverted")
        assert h.control.state.character == "angel"


async def test_late_uninterruptible_settings_timeout_restored_before_error():
    async with pipeline(deadline=.16) as h:
        await h.hello()
        h.llm.settings_started.clear()
        h.llm.setting_delay = .21
        await h.send()
        await h.llm.settings_started.wait()
        result = await h.outcome()
        assert result["op"] == "error" and result["reason"] == "deadline"
        assert h.llm.settings.system_instruction == character_instructions("angel")
        assert await h.worker.flush_pipeline(timeout=.5)
        assert h.llm.settings.system_instruction == character_instructions("angel")


async def test_cancellation_rolls_back_before_releasing_gate():
    async with pipeline() as h:
        await h.hello()
        h.llm.settings_started.clear()
        h.llm.setting_delay = .12
        await h.send()
        await h.llm.settings_started.wait()
        h.coordinating.cancel()
        result = await h.outcome()
        assert result["op"] == "error" and result["reason"] == "cancelled"
        assert h.llm.settings.system_instruction == character_instructions("angel")
        assert h.control.switcher.strategy.active_service is h.services["angel"]


async def test_rollback_failure_closes_pipeline_and_keeps_gate_closed():
    async with pipeline(deadline=.08) as h:
        await h.hello()
        h.llm.setting_delay = .5
        await h.send()
        await until(lambda: h.control.stopping)
        await asyncio.wait_for(asyncio.shield(h.running), 2)
        assert h.control.gate.closed and h.control.state.rev == 1
        assert not any(s["seq"] == 2 for s in h.states())


@pytest.mark.parametrize("fault", ["tts-fail-next", "provider-error-after-return", "receive-error-after-return"])
async def test_first_synthesis_failure_streaming_and_arriving_turn_full_fenced_revert(fault):
    async with pipeline(fault="tts-fail-next" if fault == "tts-fail-next" else "") as h:
        await h.hello()
        await h.send()
        committed = await h.outcome()
        assert committed["character"] == "devil"
        h.llm.remainder.clear()  # LLM still streaming when synthesis fails.
        h.llm.setting_delay = .06  # Delay rollback mutation, never a reconnect on that path.
        devil = h.services["devil"]
        devil.auto_complete = False
        await h.turn()
        await devil.returned.wait()
        if fault == "provider-error-after-return":
            assert h.control.state.verification is not None
            # Generator returned successfully, but the asynchronous provider
            # errors before SpeechMetadata/TTSStoppedFrame.
            await devil._handle_message(dict(type="Error", code="offline", description="late failure"))
        elif fault == "receive-error-after-return":
            await devil.push_error_frame(ErrorFrame(error="offline receive failed", processor=devil))
        await until(lambda: h.control.gate.closed)
        await h.turn("Arrives during rollback")
        await until(lambda: any(s.get("reason") == "reverted" for s in h.states()))
        reverted = h.states()[-1]
        assert reverted["rev"] == committed["rev"] + 1 and reverted["character"] == "angel"
        assert h.llm.settings.system_instruction == character_instructions("angel")
        assert h.llm.response_ids[0] not in h.services["angel"].starts
        assert 1 not in h.services["angel"].text_responses
        h.llm.remainder.set()
        await until(lambda: len(h.llm.calls) == 2)
        await until(lambda: h.services["angel"].speaks)
        assert h.llm.calls[-1][0] == character_instructions("angel")
        if fault != "tts-fail-next":
            assert any(m["role"] == "assistant" and m["content"].startswith("[Devil]")
                       for m in h.control.context.get_messages())


async def test_completion_correlates_stop_and_ignores_delayed_old_revision_error():
    async with pipeline() as h:
        await h.hello()
        await h.send()
        await h.outcome()
        devil = h.services["devil"]
        devil.release.clear()
        await h.turn()
        await devil.returned.wait()
        await until(lambda: h.control.state.verification and h.control.state.verification.response)
        record = h.control.state.verification
        owner = (record.conn, record.rev, record.service, record.response)
        wrong = TTSStoppedFrame(context_id="unrelated")
        wrong.metadata["duo-completed"] = (*owner[:3], "unrelated")
        await devil.push_frame(wrong)
        await asyncio.sleep(.03)
        assert h.control.state.verification is not None
        devil.release.set()
        await until(lambda: h.control.state.verification is None)
        # Several later switches must not be reverted by an old failure.
        for _ in range(3):
            await h.send()
            await h.outcome()
        rev, character = h.control.state.rev, h.control.state.character
        error = ErrorFrame(error="delayed old failure", processor=devil)
        error.metadata["duo-owner"] = owner
        await devil.push_error_frame(error)
        await until(lambda: any(e[0] == "ignored-error" for e in h.events))
        assert (h.control.state.rev, h.control.state.character) == (rev, character)


async def test_retirement_preserves_emitted_error_ownership():
    import json
    assert "_receive_messages" not in DuoFluxTTS.__dict__
    assert "_receive_task_handler" not in DuoFluxTTS.__dict__
    async with pipeline(real_lifecycle=True) as h:
        await h.hello()
        await h.send()
        await h.outcome()
        devil = h.services["devil"]
        old_socket, old_receiver = devil.sockets[-1], devil._receive_task
        old_socket.auto_complete = False
        await h.turn()
        await until(lambda: h.control.state.verification and h.control.state.verification.response)
        # Hold an error after it leaves the real service, before coordinator
        # delivery. The receiver is still alive and must be awaited on retirement.
        error = ErrorFrame(error="delayed old socket error", processor=devil)
        original_enqueue = h.control.enqueue
        def hold_error(event):
            if not (isinstance(event, PipelineError) and event.frame is error):
                original_enqueue(event)
        h.control.enqueue = hold_error
        await devil.push_error_frame(error)
        owner, generation = error.metadata["duo-owner"], error.metadata["duo-generation"]
        assert owner is not None and not old_receiver.done()
        await h.send()
        await h.outcome()
        await h.send()
        await h.outcome()
        assert devil.connection_generation > generation and old_receiver.done()
        new_socket = devil.sockets[-1]
        new_socket.auto_complete = False
        await h.turn("New response on the retired voice")
        await until(lambda: h.control.state.verification and h.control.state.verification.response)
        record = h.control.state.verification
        h.control.enqueue = original_enqueue
        old_socket.incoming.put_nowait(json.dumps(dict(type="Error", code="old", description="retired raw error")))
        h.control.enqueue(PipelineError(error))
        await until(lambda: any(kind == "ignored-error" for kind, _, _ in h.events))
        assert error.metadata["duo-owner"] == owner
        assert error.metadata["duo-generation"] == generation
        assert h.control.state.verification == record and not h.control.gate.closed
        new_socket.incoming.put_nowait(json.dumps(dict(type="Error", code="current", description="current error")))
        await until(lambda: h.states()[-1].get("reason") == "reverted")
        assert h.control.state.character == "angel"


async def test_zero_audio_stop_does_not_disarm_before_background_synthesis_error():
    async with pipeline() as h:
        await h.hello()
        await h.send()
        await h.outcome()
        devil = h.services["devil"]
        devil.emit_audio = False
        devil.outcome_delay = .08
        await h.turn()
        await devil.returned.wait()
        await asyncio.sleep(.035)  # SpeechMetadata/stop arrives before the background error.
        assert h.control.state.verification is not None
        await until(lambda: h.states()[-1].get("reason") == "reverted")
        assert h.control.state.character == "angel"


async def test_abandoned_first_response_cannot_own_second_response_error():
    async with pipeline() as h:
        await h.hello()
        await h.send()
        await h.outcome()
        devil = h.services["devil"]
        devil.release.clear()
        await h.turn()
        await until(lambda: h.control.state.verification and h.control.state.verification.response)
        first = h.control.state.verification.response
        await h.turn("Interrupt the first response and ask another question")
        await until(lambda: len(h.llm.calls) == 2 and h.control.state.verification is None)
        await until(lambda: devil.response_owner is not None and devil.response_owner[3] != first)
        assert devil.response_owner[3] != first
        await devil._handle_message(dict(type="Error", code="offline", description="second response error"))
        await until(lambda: devil.connection_generation > 0 and not h.control.gate.closed)
        assert not h.control.stopping and not h.worker.has_finished()
        assert h.control.state.rev == 2 and h.control.state.character == "devil"
        assert not any(s.get("reason") == "reverted" for s in h.states())


@pytest.mark.parametrize("during_revert", [False, True])
@pytest.mark.parametrize("failure", ["cancel-publish", "send-failed", "cancel-release"])
async def test_publication_and_release_failure_terminate_cleanly(during_revert, failure):
    async with pipeline(fault="tts-fail-next" if during_revert else "") as h:
        await h.hello()
        if during_revert:
            await h.send()
            await h.outcome()
        entered = asyncio.Event()
        blocked = asyncio.Event()
        if failure == "cancel-release":
            async def blocked_reopen():
                entered.set()
                await blocked.wait()
            h.control.gate.reopen = blocked_reopen
        else:
            original_send = h.worker.rtvi.send_server_message
            async def failed_send(data):
                if not during_revert or data.get("reason") == "reverted":
                    entered.set()
                    if failure == "send-failed":
                        raise OSError("offline send failed")
                    await blocked.wait()
                await original_send(data)
            h.worker.rtvi.send_server_message = failed_send
        if during_revert:
            await h.turn()
        else:
            await h.send()
        await asyncio.wait_for(entered.wait(), 2)
        if failure.startswith("cancel"):
            h.coordinating.cancel()
        await until(lambda: h.control.stopping)
        await asyncio.wait_for(asyncio.shield(h.running), 2)
        assert h.control.gate.closed and h.worker.has_finished()


@pytest.mark.parametrize("drops", [1, 4])
async def test_drop_state_retries_use_cached_state_and_resync_never_adopts(drops):
    async with pipeline(fault=f"drop-state-{drops}") as h:
        # Initial adoption succeeds; fault arms on the first committed toggle.
        result = await h.hello()
        assert result["rev"] == 1
        assert h.control.drop_states == drops
        await h.send()
        await until(lambda: h.control.state.rev == 2)
        for _ in range(3):
            await h.send(seq=2)
        if drops == 4:
            await until(lambda: h.control.drop_states == 0)
            await h.send("hello", have="angel")
        result = await h.outcome()
        assert result["character"] == "devil" and result["rev"] == 2


async def test_history_labels_capture_each_turn_and_do_not_relabel_old_history():
    async with pipeline() as h:
        await h.hello()
        await h.turn()
        await until(lambda: any(m["role"] == "assistant" for m in h.control.context.get_messages()))
        old = next(m.copy() for m in h.control.context.get_messages() if m["role"] == "assistant")
        await h.send()
        await h.outcome()
        await h.turn("A different question")
        await until(lambda: len([m for m in h.control.context.get_messages() if m["role"] == "assistant"]) == 2)
        replies = [m for m in h.control.context.get_messages() if m["role"] == "assistant"]
        assert replies[0] == old and old["content"].startswith("[Angel]")
        assert replies[1]["content"].startswith("[Devil]")


async def test_spoken_text_filter_drops_labels_and_echoed_notes():
    from duo_bot import spoken_text
    assert await spoken_text("[Angel] I'd say no. ", "sentence") == "I'd say no. "
    assert await spoken_text("Sure, [Devil] go on. ", "sentence") == "Sure, go on. "
    assert await spoken_text(silent_note("angel", "devil") + " ", "sentence") == ""
    assert await spoken_text("Ask the angel. ", "sentence") == "Ask the angel. "
    for outgoing, incoming in (("angel", "angel"), ("angel", "devil"), ("devil", "angel")):
        note = silent_note(outgoing, incoming)
        assert note.startswith("Silent context:") and "[" not in note and "Switch" not in note


async def test_labels_the_model_copies_never_accumulate_in_history():
    # Board smoke on Pipecat 1.15 (OPUS NOTE 22): history kept the model's own
    # text, so a copied label was stored under a second one, and each reply
    # copied one more. Twenty turns of a model that labels every sentence and
    # echoes a note must leave what was spoken, labelled once.
    from duo_bot import spoken_text
    from pipecat.services.tts_service import TextAggregationMode
    production = dict(text_aggregation_mode=TextAggregationMode.SENTENCE, text_transforms=[("*", spoken_text)])
    async with pipeline(tts_kwargs=production) as h:
        h.llm.tokens = ("[Angel] A labelled reply. Silent context: Angel is speaking now. ",
                        "[Angel] [Devil] A final sentence. ")
        await h.hello()
        for turn in range(1, 21):
            await h.turn("And then?")
            await until(lambda: len([m for m in h.control.context.get_messages()
                                     if m.get("role") == "assistant"]) >= turn)
            await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
        replies = [m["content"] for m in h.control.context.get_messages() if m.get("role") == "assistant"]
        assert replies == ["[Angel] A labelled reply. A final sentence."] * 20
        said = [text for text, _ in h.services["angel"].speaks]
        assert len(said) == 40 and not any("Silent context" in text or "[" in text for text in said)
        sizes = [sum(len(str(m.get("content"))) for m in messages) for _, messages in h.llm.calls]
        growth = {later - earlier for earlier, later in zip(sizes, sizes[1:])}
        assert len(sizes) == 20 and len(growth) == 1  # Every prompt grows by one turn and one reply.


async def auto_reply(h, character="angel", count=1):
    await until(lambda: len([m for m in h.control.context.get_messages() if m.get("role") == "assistant"]) >= count)
    await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
    assert h.services[character].speaks
    assert await h.worker.flush_pipeline(timeout=.5)


async def auto_open_turn(h, text="B arrives"):
    await h.worker.queue_frames([
        ProposedUserStartedSpeakingFrame(),
        TranscriptionFrame(text=text, user_id="u", timestamp="now", finalized=True),
    ])
    assert await h.worker.flush_pipeline(timeout=.5)


def auto_user(call):
    return next(m["content"] for m in reversed(call[1]) if m.get("role") == "user")


async def test_auto_production_order_off_structure_and_calibrated_identity():
    off = Harness(config={"auto_switch": False})
    assert off.control.hold is None and off.control.addressee is None
    assert not isinstance(off.llm, RouteStampingLLM)
    assert off.control.gate.fence.inference_seq == 0
    assert off.llm._next is off.control.switcher
    on = Harness(auto_switch=True)
    assert isinstance(on.llm, RouteStampingLLM)
    assert on.llm._next is on.control.hold
    assert on.control.hold._next is on.control.switcher
    cfg = configuration({"DUO_AUTO_SWITCH": "1"})
    assert cfg["auto_switch_floor"] == .80 and cfg["route_budget_s"] == .5
    assert cfg["jev_model"] == "jev-1.14.0"
    manifest = identity(cfg)
    assert len(manifest["config"]["question_sha256"]) == 64
    assert len(manifest["config"]["case_set_sha256"]) == 64
    for override in ({"DUO_AUTO_SWITCH": "yes"}, {"DUO_ROUTE_BUDGET_S": "inf"},
                     {"DUO_AUTO_SWITCH_FLOOR": "0.5"}, {"DUO_JEV_MODEL": "jev-latest"}):
        with pytest.raises(ValueError):
            configuration(override)


@pytest.mark.parametrize("delay", [.01, .06])
async def test_auto_01_02_stay_timing_fifo_and_flagoff_equivalence(delay):
    from copy import deepcopy
    results = []
    for enabled in (False, True):
        fake = FakeAddressee(delay=delay)
        async with pipeline(auto_switch=enabled, addressee=fake) as h:
            await h.hello()
            h.llm.first_token_delay = .025
            h.llm.inter_token_delay = .04
            # T1 is incomplete. S1 is at the second token, after J in both rows.
            h.llm.tokens = ("A small playful", " reply. ")
            started = time.monotonic()
            await h.turn()
            await auto_reply(h)
            results.append((deepcopy(h.llm.calls), deepcopy(h.control.context.get_messages()),
                            [s[0] for s in h.services["angel"].speaks],
                            h.services["angel"].speaks[0][1] - started,
                            sum(len(f.audio) for f in h.transport.client.audio),
                            h.transport.client.audio_times[0] - started))
            assert len(fake.calls) == int(enabled)
    assert results[0][:3] == results[1][:3]
    assert results[0][4] == results[1][4]
    assert abs(results[1][3] - results[0][3]) < .05
    assert abs(results[1][5] - results[0][5]) < .05


async def test_auto_02_late_stay_releases_finite_response_fifo():
    fake = FakeAddressee(delay=.15)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": .3}) as h:
        await h.hello()
        started = time.monotonic()
        await h.turn()
        await until(lambda: len(h.control.hold.held) == 4)
        assert not h.services["angel"].speaks and not h.transport.client.audio
        await auto_reply(h)
        assert h.services["angel"].text_responses == [1, 1]
        assert .12 <= h.services["angel"].speaks[0][1] - started < .23


async def test_auto_03_switch_quarantines_draft_and_reruns_once():
    fake = FakeAddressee("devil", delay=.04)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        hello = await h.hello()
        assert h.control.state.device_session == "boot"
        await h.turn("Devil, should I eat cake?")
        await auto_reply(h, "devil")
        assert len(fake.calls) == 1 and len(h.llm.calls) == 2
        assert [c[0] for c in h.llm.calls] == [character_instructions("angel"), character_instructions("devil")]
        assert not h.services["angel"].starts and not h.services["angel"].speaks
        assert h.services["devil"].text_responses == [2, 2]
        addressed = [s for s in h.states() if s.get("reason") == "addressed"]
        assert len(addressed) == 1 and addressed[0]["rev"] == hello["rev"] + 1
        assert addressed[0]["seq"] == hello["seq"]
        messages = h.control.context.get_messages()
        user = next(i for i, m in enumerate(messages) if m.get("role") == "user")
        assert [m["role"] for m in messages[user:]] == ["user", "system", "assistant"]
        assert "already happened" in messages[user + 1]["content"]
        assert h.llm.calls[-1][1][-1] is messages[user + 1]
        assert all("double-tap" not in text for text, _ in h.services["devil"].speaks)
        responses = [data for kind, data, _ in h.events if kind == "assistant-turn-started"]
        assert all(d["character"] == "devil" for d in responses)


@pytest.mark.parametrize("error", [False, True])
async def test_auto_04_budget_wake_while_coordinator_busy(error):
    fake = FakeAddressee(RuntimeError("offline") if error else "either", delay=.2)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": .06}) as h:
        await h.hello()
        publish = h.control._publish
        entered = asyncio.Event()
        async def slow_publish(result):
            entered.set()
            await asyncio.sleep(.2)
            await publish(result)
        h.control._publish = slow_publish
        await h.send("hello", seq=1)  # replay publish: gate stays open
        await entered.wait()
        started = time.monotonic()
        await h.turn()
        await until(lambda: h.services["angel"].speaks)
        assert .04 <= h.services["angel"].speaks[0][1] - started < .14
        assert not fake.calls  # Route still queued behind publication; timer owns release.
        await auto_reply(h)
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_04_error_releases_without_switch():
    async with pipeline(auto_switch=True, addressee=FakeAddressee(RuntimeError("offline"))) as h:
        await h.hello(); await h.turn(); await auto_reply(h)
        assert any(k == "route" and d["reason"] == "error" for k, d, _ in h.events)


async def test_auto_05_preturn_interruption_preserves_new_stamped_response():
    async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", delay=.04)) as h:
        await h.hello(); await h.turn("Devil, answer")
        await auto_reply(h, "devil")
        assert len(h.llm.calls) == 2 and h.services["devil"].text_responses == [2, 2]
        assert h.witness.interruptions


async def test_auto_06_bargein_drops_held_frames_and_next_turn_routes(join_bound):
    fake = FakeAddressee("devil", "either", delay=.12)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn("A")
        await until(lambda: h.control.hold.held)
        old = next(iter(h.control.hold.routes))
        await h.worker.queue_frame(InterruptionWorkerFrame())
        await until(lambda: h.control.hold.decision(old) == "superseded")
        assert not h.services["angel"].speaks
        await h.turn("B"); await auto_reply(h)
        assert len(fake.calls) == 2
        assert h.services["angel"].text_responses == [2, 2]
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_07_rapid_turns_only_newest_switches(join_bound):
    fake = FakeAddressee("angel", "devil", delay=.12)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn("A")
        await fake.started.wait()
        await h.turn("B")
        await auto_reply(h, "devil")
        assert fake.cancelled == 1 and len(fake.calls) == 2
        assert len([s for s in h.states() if s.get("reason") == "addressed"]) == 1
        assert auto_user(h.llm.calls[-1]) == "B"


async def test_auto_08_manual_preempt_cancels_and_joins_read(join_bound):
    fake = FakeAddressee("devil", delay=2)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn(); await fake.started.wait()
        started = time.monotonic()
        await h.send(); result = await h.outcome()
        assert time.monotonic() - started < .25
        assert result["character"] == "devil" and "reason" not in result
        assert fake.cancelled == 1 and fake.joined.is_set()
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_09_busy_declines_aggregator_completion():
    async with pipeline(auto_switch=True) as h:
        await h.hello(); await auto_open_turn(h)
        h.stt.flush_delay = .08
        await h.send(); await until(lambda: h.control.gate.closed)
        await h.control.user_aggregator.push_aggregation()
        await until(lambda: h.control.gate.fence.held)
        assert not h.addressee.calls
        await h.outcome(); await auto_reply(h, "devil")
        assert not h.addressee.calls and len(h.llm.calls) == 1


async def test_auto_10_barriers_pass_capturing_hold():
    fake = FakeAddressee(delay=.3)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn(); await until(lambda: len(h.control.hold.held) == 4)
        count = len(h.control.hold.held)
        marker = "held-probe"
        await h.worker.queue_frame(TransactionMarker(marker))
        assert await h.worker.flush_pipeline(timeout=.1)
        await until(lambda: marker in h.witness.markers)
        assert len(h.control.hold.held) == count and not h.services["angel"].speaks


async def test_auto_11_precommit_failure_restores_once_preserves_verification():
    fake = FakeAddressee("angel", delay=.04)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello("devil")
        previous = h.control.state.verification
        assert previous is not None
        h.stt.sabotage = ManuallySwitchServiceFrame
        original = h.control._queue_rerun
        def capture(snapshot, note):
            assert h.control.state.verification is previous
            assert previous.response is None
            original(snapshot, note)
        h.control._queue_rerun = capture
        await h.turn("Angel, answer")
        await auto_reply(h, "devil")
        assert len(h.llm.calls) == 2
        assert h.llm.calls[-1][0] == character_instructions("devil")
        assert "handed back" in h.llm.calls[-1][1][-1]["content"]
        assert not any(s.get("reason") == "addressed" for s in h.states())
        # The restored response, not the quarantined draft, can verify the old record.
        assert h.services["devil"].text_responses == [2, 2]
        assert h.services["angel"].starts == []
        assert h.control.state.rev == previous.rev


@pytest.mark.parametrize("failure", ["restore", "publish", "release"])
async def test_auto_12_transaction_failures_end_pipeline(failure):
    fake = FakeAddressee("devil", delay=.04)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        if failure == "restore":
            h.stt.sabotage = ManuallySwitchServiceFrame
            original = h.control._mutate
            async def broken_restore(*a, **kw):
                if kw.get("reverting"):
                    raise RuntimeError("offline restoration failure")
                return await original(*a, **kw)
            h.control._mutate = broken_restore
        elif failure == "publish":
            async def broken(*a, **kw):
                raise RuntimeError("offline publish")
            h.control._publish = broken
        else:
            async def broken(*a, **kw):
                raise RuntimeError("offline reopen")
            h.control.gate.reopen = broken
        await h.turn(); await until(lambda: h.control.stopping)
        await until(h.worker.has_finished)
        assert h.control.gate.closed


async def test_auto_13_failed_first_reply_reverts_auto_switch():
    async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", delay=.04)) as h:
        await h.hello()
        devil = h.services["devil"]
        devil.auto_complete = False
        await h.turn(); await until(lambda: devil.speaks)
        await devil._handle_message(dict(type="Error", code="offline", description="failed reply"))
        await until(lambda: any(s.get("reason") == "reverted" for s in h.states()))
        addressed = next(s for s in h.states() if s.get("reason") == "addressed")
        reverted = h.states()[-1]
        assert reverted["seq"] == addressed["seq"] and reverted["rev"] == addressed["rev"] + 1
        assert reverted["character"] == "angel"


async def test_auto_14_pongs_during_read_and_cancellation_join(join_bound):
    from types import SimpleNamespace
    fake = FakeAddressee("devil", delay=2); fake.cancel_delay = .05
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        pongs = []
        h.control.liveness = SimpleNamespace(pong=lambda data: pongs.append(data))
        await h.turn(); await fake.started.wait()
        h.control.enqueue(Pong({"seq": 1}))
        await until(lambda: pongs)
        await h.send()
        h.control.enqueue(Pong({"seq": 2}))
        await h.outcome()
        assert pongs == [{"seq": 1}, {"seq": 2}]
        assert fake.joined.is_set()
        h.control.liveness = None


async def test_auto_15_shutdown_during_read_joins_request():
    fake = FakeAddressee(delay=2)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn(); await fake.started.wait()
        h.control.enqueue(Shutdown())
        await until(h.worker.has_finished)
        assert fake.joined.is_set() and fake.active == 0
        assert not h.services["angel"].speaks


@pytest.mark.parametrize("target", ["either", "devil"])
async def test_auto_16_rtvi_released_and_quarantined_draft_events(target):
    decide_now = asyncio.Event()
    async def decision(state):
        await decide_now.wait()
        return ChoiceResult(choice=target, probabilities={target: .99}, confidence=.99)
    fake = FakeAddressee(decision)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello(); h.transport.client.messages.clear()
        await h.turn(); await until(lambda: len(h.control.hold.held) == 4)
        raw_events = {"bot-llm-started", "bot-llm-text", "bot-llm-stopped", "bot-transcription"}
        await until(lambda: raw_events <= {m.get("type") for m in h.transport.client.messages})
        types = {m.get("type") for m in h.transport.client.messages}
        response_events = {"bot-llm-started", "bot-llm-text", "bot-llm-stopped", "bot-transcription",
                           "bot-tts-started", "bot-tts-text", "bot-tts-stopped", "bot-output"}
        assert types & response_events == raw_events
        decide_now.set()
        await auto_reply(h, "devil" if target == "devil" else "angel")
        assert h.services["angel"].text_responses == ([1, 1] if target == "either" else [])
        assert h.services["devil"].text_responses == ([2, 2] if target == "devil" else [])
        await until(lambda: response_events <= {m.get("type") for m in h.transport.client.messages})
        types = {m.get("type") for m in h.transport.client.messages}
        assert types & response_events == response_events


@pytest.mark.parametrize("target,delay", [("either", .005), ("devil", .005), ("either", .3)])
async def test_auto_17_decision_before_first_frame_or_deadline(target, delay):
    fake = FakeAddressee(target, delay=delay)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": .06}) as h:
        await h.hello(); h.llm.first_frame_delay = .10
        await h.turn()
        await auto_reply(h, "devil" if target == "devil" else "angel")
        assert h.services["angel"].text_responses == ([] if target == "devil" else [1, 1])
        assert len(h.llm.calls) == 1  # Early switch cancels before draft completion starts.
        assert len(fake.calls) == 1
        if delay > .06:
            assert fake.cancelled == 1
        assert h.llm._route is None


async def test_auto_17_terminal_decisions_are_sticky_and_late_claim_loses():
    async with pipeline(auto_switch=True) as h:
        hold = h.control.hold
        loop = asyncio.get_running_loop()
        hold.expect("expired", loop.time() - .001)
        assert not hold.claim_switch("expired")
        assert hold.decision("expired") == "release"
        assert not hold.decide("expired", "quarantine")
        for decision in ("release", "superseded", "quarantine", "stopped"):
            hold.expect(decision, loop.time() + 1)
            assert hold.decide(decision, decision)
            assert not hold.decide(decision, "release")
        for i in range(10):
            hold.expect(str(i), loop.time() + 1)
        assert len(hold.routes) == 8
        assert hold.decision("expired") == "release"


@pytest.mark.parametrize("stage", ["before-stamp", "after-stamp", "after-enqueue"])
@pytest.mark.parametrize("rollback", [False, True])
async def test_auto_17_interruption_around_response_start_keeps_token(stage, rollback, monkeypatch):
    fake = FakeAddressee("devil", delay=.035)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        blocked = asyncio.Event()
        original = (h.llm.push_frame if stage == "before-stamp" else
                    FakeLLM.push_frame if stage == "after-stamp" else h.control.hold.process_frame)
        once = True
        if stage == "before-stamp":
            async def before(frame, direction=DOWN):
                nonlocal once
                if isinstance(frame, LLMFullResponseStartFrame) and once:
                    once = False; blocked.set(); await asyncio.sleep(.15)
                await original(frame, direction)
            h.llm.push_frame = before
        elif stage == "after-stamp":
            async def after(service, frame, direction=DOWN):
                nonlocal once
                if service is h.llm and isinstance(frame, LLMFullResponseStartFrame) and once:
                    assert frame.metadata.get("duo-route")
                    once = False; blocked.set(); await asyncio.sleep(.15)
                await original(service, frame, direction)
            monkeypatch.setattr(FakeLLM, "push_frame", after)
        else:
            async def queued(frame, direction):
                nonlocal once
                if isinstance(frame, LLMFullResponseStartFrame) and once:
                    assert frame.metadata.get("duo-route")
                    once = False; blocked.set(); await asyncio.sleep(.15)
                await original(frame, direction)
            h.control.hold.process_frame = queued
        if rollback:
            h.stt.sabotage = ManuallySwitchServiceFrame
        await h.turn("A"); await blocked.wait()
        await auto_reply(h, "angel" if rollback else "devil")
        spoken = h.services["angel" if rollback else "devil"].text_responses
        assert len(spoken) == 2 and len(set(spoken)) == 1
        assert h.llm._route is None
        assert len(fake.calls) == 1


@pytest.mark.parametrize("shutdown", [False, True])
async def test_auto_18_15_interruption_or_shutdown_cancels_suspended_drain(shutdown):
    fake = FakeAddressee(delay=.04)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        hold = h.control.hold
        pushing = asyncio.Event(); cancelled = asyncio.Event()
        original = hold.push_frame
        async def suspended(frame, direction=DOWN):
            if isinstance(frame, LLMTextFrame):
                pushing.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.set(); raise
            await original(frame, direction)
        hold.push_frame = suspended
        await h.turn(); await pushing.wait()
        if shutdown:
            h.control.enqueue(Shutdown())
            await until(h.worker.has_finished)
        else:
            await h.worker.queue_frame(InterruptionWorkerFrame())
            await until(cancelled.is_set)
            assert await h.worker.flush_pipeline(timeout=.3)
        assert cancelled.is_set()
        assert not h.services["angel"].speaks and not hold.held


async def test_auto_19_lost_wake_does_not_resurrect_cancelled_response():
    fake = FakeAddressee(delay=.04)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        hold = h.control.hold
        blocked = asyncio.Event()
        original = hold.process_frame
        once = True
        async def parked(frame, direction):
            nonlocal once
            if isinstance(frame, HoldWake) and once:
                once = False; blocked.set(); await asyncio.sleep(10)
            await original(frame, direction)
        hold.process_frame = parked
        await h.turn("A"); await blocked.wait()
        await hold.queue_frame(HoldWake())  # Also leave a wake queued behind the blocked one.
        await h.worker.queue_frame(InterruptionWorkerFrame())
        await until(lambda: not hold.held)
        assert not h.services["angel"].speaks
        await h.turn("B"); await auto_reply(h)
        assert h.services["angel"].text_responses == [2, 2]


@pytest.mark.parametrize("phase", ["voice", "interrupt", "mutate", "rollback"])
async def test_auto_20_open_turn_completion_during_transaction_owns_reply(phase):
    fake = FakeAddressee("devil", delay=.08)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        h.llm.first_frame_delay = .2
        name = {"voice": "_voice", "interrupt": "_interrupt", "mutate": "_mutate", "rollback": "_restore"}[phase]
        original = getattr(h.control, name)
        completed = False
        async def completion(*args, **kwargs):
            nonlocal completed
            if not completed:
                completed = True
                assert await h.control.user_aggregator.push_aggregation() == "B"
            return await original(*args, **kwargs)
        setattr(h.control, name, completion)
        if phase == "rollback":
            h.stt.sabotage = ManuallySwitchServiceFrame
        h.control.context.add_message(dict(role="user", content="A"))
        await auto_open_turn(h, "B")
        await h.control.user_aggregator.push_context_frame()
        await auto_reply(h, "angel" if phase == "rollback" else "devil")
        assert completed and len(fake.calls) == 1
        assert [auto_user(call) for call in h.llm.calls] == ["B"]
        assert h.llm.calls[0][0] == character_instructions("angel" if phase == "rollback" else "devil")
        assert any(k == "route-rerun-withdrawn" for k, _, _ in h.events)


@pytest.mark.parametrize("parent", [False, True])
async def test_auto_21_parent_or_unexplained_child_cancellation_propagates(parent):
    fake = FakeAddressee(delay=2)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn(); await fake.started.wait()
        if parent:
            h.coordinating.cancel()
        else:
            h.control.route_read[1].cancel()  # No recorded preempt/supersede reason.
        await until(h.worker.has_finished)
        assert fake.cancelled == 1 and fake.joined.is_set()
        assert h.control.route_read is None
        assert not h.services["angel"].speaks


async def test_auto_21_two_manual_requests_order_and_stale_supersession_consumed():
    fake = FakeAddressee(delay=2); fake.cancel_delay = .04
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn(); await fake.started.wait()
        await h.send(seq=2); await h.send(seq=3)
        await h.outcome(3)
        assert [(s["seq"], s["character"]) for s in h.states()] == [(1, "angel"), (2, "devil"), (3, "angel")]
        assert fake.cancelled == 1 and h.control.route_read is None
        h.control.enqueue(RouteSuperseded("old-token"))
        await until(lambda: h.control.queue.empty() and not h.control.deferred)
        assert not h.control.stopping


@pytest.mark.parametrize("phase", ["retirement-wait", "interrupt", "mutate", "publish", "rollback"])
async def test_auto_21_device_requests_deferred_in_transaction_phases(phase):
    fake = FakeAddressee("devil", delay=.04)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        original = h.control._timed if phase != "rollback" else h.control._restore
        sent = False
        async def timed(current, operation):
            nonlocal sent
            if current == phase and not sent:
                sent = True
                before = h.control.state.rev
                await h.send(seq=2)
                await asyncio.sleep(.01)
                assert h.control.state.sessions["boot"].high_water == 1
                assert h.control.state.rev == before
            return await original(current, operation)
        async def restore(*args, **kwargs):
            nonlocal sent
            if not sent:
                sent = True; await h.send(seq=2); await asyncio.sleep(.01)
                assert h.control.state.sessions["boot"].high_water == 1
            return await original(*args, **kwargs)
        if phase == "rollback":
            h.stt.sabotage = ManuallySwitchServiceFrame
            h.control._restore = restore
        else:
            h.control._timed = timed
        await h.turn(); await h.outcome(2)
        assert sent
        states = h.states()
        assert states[-1]["seq"] == 2
        assert states[-1]["character"] == ("devil" if phase == "rollback" else "angel")
        if phase != "rollback":
            assert states[-2]["reason"] == "addressed"


async def test_auto_22_device_session_provenance_all_admission_cases():
    fake = FakeAddressee("devil", "angel", delay=.03)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        assert h.control.state.device_session == "boot"  # first-ever hello
        async def request(op, session, seq):
            h.control.enqueue(DeviceRequest(dict(v=1, op=op, session=session, seq=seq, have="angel")))
            await until(lambda: any(s["session"] == session and s["seq"] == seq for s in h.states()))
            await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
        await request("hello", "second", 1)
        assert h.control.state.device_session == "second"
        for seq in range(2, 12):
            await request("toggle", "boot", seq)
        assert h.control.state.device_session == "second"
        for seq in (1, 11):  # stale and mismatched hello in boot
            count = len(h.states()); h.control.enqueue(DeviceRequest(dict(v=1, op="hello", session="boot", seq=seq, have="angel")))
            await until(lambda: len(h.states()) > count)
            assert h.control.state.device_session == "second"
        count = len(h.states()); h.control.enqueue(DeviceRequest(dict(v=1, op="hello", session="boot", seq=12, have="angel")))
        await until(lambda: len(h.states()) > count)
        await until(lambda: h.control.state.device_session == "boot")
        await h.turn("Devil, answer"); await auto_reply(h, "devil")
        addressed = next(s for s in reversed(h.states()) if s.get("reason") == "addressed")
        assert (addressed["session"], addressed["seq"]) == ("boot", 12)
        count = len(h.states()); h.control.enqueue(DeviceRequest(dict(v=1, op="hello", session="second", seq=1, have="angel")))
        await until(lambda: len(h.states()) > count)
        assert h.control.state.device_session == "boot"  # replay of second hello
        await request("hello", "second", 2)
        await h.turn("Angel, answer"); await auto_reply(h, "angel", count=2)
        addressed = next(s for s in reversed(h.states()) if s.get("reason") == "addressed")
        assert (addressed["session"], addressed["seq"]) == ("second", 2)


async def test_auto_22_failed_hello_notes_session():
    async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", delay=.03)) as h:
        h.stt.sabotage = SilentNote
        baseline = await h.hello()
        assert baseline["reason"] == "adoption-failed" and baseline["rev"] == 0
        assert h.control.state.device_session == "boot"
        await h.turn(); await auto_reply(h, "devil")
        assert h.states()[-1]["reason"] == "addressed" and h.states()[-1]["seq"] == 1


async def test_auto_23_only_deadline_wake_releases_finite_buffer():
    fake = FakeAddressee(delay=2)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": .1}) as h:
        await h.hello(); await h.turn(); await until(lambda: len(h.control.hold.held) == 4)
        assert not h.services["angel"].speaks
        await auto_reply(h)
        assert fake.cancelled == 1
        assert h.services["angel"].text_responses == [1, 1]
        assert not h.control.hold.held


@pytest.mark.parametrize("phase", ["publish", "reopen", "frozen-start", "same-context", "same-run", "several", "before-note", "after-note", "rollback"])
async def test_auto_24_placeholder_ownership_and_exact_note_identity(phase):
    from pipecat.frames.frames import LLMRunFrame
    fake = FakeAddressee("devil", delay=.05)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        context = h.control.context
        context.add_message(dict(role="user", content="A"))
        routed = context.get_messages()[-1]
        await auto_open_turn(h, "B")
        h.llm.first_frame_delay = .12
        completed = False
        saved_note = None
        placeholders = []
        original_mutate = h.control._mutate
        original_rerun = h.control._queue_rerun
        original_push = h.control.gate.fence.push_frame
        async def complete():
            nonlocal completed
            if not completed:
                completed = True
                assert await h.control.user_aggregator.push_aggregation() == "B"
        def rerun(snapshot, note):
            nonlocal saved_note
            saved_note = note
            assert any(m is note for m in context.get_messages())
            original_rerun(snapshot, note)
            placeholders.append(h.control.gate.fence.held[-1][0])
        h.control._queue_rerun = rerun
        async def mutate(*args, **kwargs):
            nonlocal saved_note
            if phase == "before-note" and not kwargs.get("reverting"):
                await complete()
            note = await original_mutate(*args, **kwargs)
            saved_note = note
            if phase in ("after-note", "rollback"):
                await complete()
                if phase == "rollback":
                    await until(lambda: h.control.gate.fence.held)
            elif phase == "same-context":
                await h.control.user_aggregator.push_context_frame()
                await until(lambda: h.control.gate.fence.held)
            elif phase == "same-run":
                await h.worker.queue_frame(LLMRunFrame())
                await until(lambda: h.control.gate.fence.held)
            elif phase == "several":
                await complete()
                for _ in range(2):
                    await h.control.user_aggregator.push_context_frame()
                await until(lambda: len(h.control.gate.fence.held) == 3)
            return note
        h.control._mutate = mutate
        if phase == "rollback":
            h.stt.sabotage = ManuallySwitchServiceFrame
        if phase == "publish":
            original_publish = h.control._publish
            async def publish(result):
                if result.get("reason") == "addressed":
                    await complete()
                await original_publish(result)
            h.control._publish = publish
        if phase == "reopen":
            async def yielding_push(frame, direction):
                if isinstance(frame, AutoRerunFrame):
                    # Sol r5: B reaches held while the placeholder is popped but not enqueued.
                    await complete()
                    await until(lambda: h.control.gate.fence.held)
                await original_push(frame, direction)
            h.control.gate.fence.push_frame = yielding_push
        if phase == "frozen-start":
            original_start = h.llm.push_frame
            once = True
            async def frozen_start(frame, direction=DOWN):
                nonlocal once
                if isinstance(frame, LLMFullResponseStartFrame) and h.llm._route is None and once:
                    once = False
                    await complete()
                await original_start(frame, direction)
            h.llm.push_frame = frozen_start
        if phase in ("before-note", "after-note"):
            # Sol r7: append B, but keep B's inference upstream until after placeholder consumption.
            user = h.control.user_aggregator
            original_context_push = user.push_context_frame
            release = asyncio.Event()
            delayed_tasks = []
            async def delayed_push(*a, **kw):
                if not completed:
                    return await original_context_push(*a, **kw)
                async def later():
                    await release.wait()
                    await original_context_push(*a, **kw)
                delayed_tasks.append(asyncio.create_task(later()))
            user.push_context_frame = delayed_push
            original_process = h.llm.process_frame
            async def placeholder_processed(frame, direction):
                await original_process(frame, direction)
                if isinstance(frame, AutoRerunFrame):
                    release.set()
            h.llm.process_frame = placeholder_processed
        await h.control.user_aggregator.push_context_frame()
        character = "angel" if phase == "rollback" else "devil"
        expected = 2 if phase == "frozen-start" else 3 if phase == "several" else 1
        await auto_reply(h, character, count=expected)
        assert len(fake.calls) == (2 if phase in ("frozen-start", "before-note", "after-note") else 1)
        assert len(h.llm.calls) == expected
        if phase == "frozen-start":
            assert [auto_user(call) for call in h.llm.calls] == ["A", "B"]
            assert all(m.get("content") != "B" for m in h.llm.calls[0][1])
        else:
            assert [auto_user(call) for call in h.llm.calls] == (["A"] if phase in ("same-context", "same-run") else ["B"] * expected)
        assert all(c[0] == character_instructions(character) for c in h.llm.calls)
        assert placeholders[0].user_object is routed
        assert placeholders[0].note_object is saved_note
        assert all(any(m is saved_note for m in call[1]) for call in h.llm.calls)
        assert all("duo-route" not in frame.metadata for frame, _ in h.control.gate.fence.held)
        if phase != "frozen-start":
            assert any(k == "route-rerun-withdrawn" for k, _, _ in h.events)
        if phase in ("before-note", "after-note"):
            await asyncio.gather(*delayed_tasks)


async def test_auto_24_success_and_rollback_frozen_prefix_end_at_exact_note():
    for rollback in (False, True):
        async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", delay=.04)) as h:
            await h.hello()
            if rollback:
                h.stt.sabotage = ManuallySwitchServiceFrame
            saved = []
            original = h.control._queue_rerun
            def capture(snapshot, note):
                saved.append((snapshot["user_object"], note))
                original(snapshot, note)
            h.control._queue_rerun = capture
            await h.turn("A"); await auto_reply(h, "angel" if rollback else "devil")
            user, note = saved[0]
            request = h.llm.calls[-1][1]
            assert request[-1] is note
            user_index = next(i for i, m in enumerate(request) if m is user)
            assert user_index < len(request) - 1
            assert not any(m.get("role") == "user" for m in request[user_index + 1:])
            assert len(h.llm.calls) == 2
            assert ("handed back" in note["content"]) == rollback


async def test_auto_outer_deadline_cancels_real_jev_trickling_body(monkeypatch):
    import httpx
    from duo_addressee import Addressee
    original_client = httpx.AsyncClient
    stream_closed = asyncio.Event()
    chunks = []
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                for _ in range(30):
                    await asyncio.sleep(.015)  # Each chunk is faster than the read timeout.
                    chunks.append(time.monotonic())
                    yield b" "
            finally:
                stream_closed.set()
        async def aclose(self):
            stream_closed.set()
    async def handler(request):
        return httpx.Response(200, stream=Trickle())
    fake = FakeAddressee()
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": .07}) as h:
        await h.hello()
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(**kw, transport=httpx.MockTransport(handler)))
        real = Addressee("offline-placeholder", budget=.07)
        try:
            fake.script.append(real.ask)
            started = time.monotonic(); await h.turn(); await auto_reply(h)
            assert time.monotonic() - started < .2
            assert 1 <= len(chunks) < 30
            assert stream_closed.is_set() and not real._pending
            assert any(k == "route" and d["reason"] == "timeout" for k, d, _ in h.events)
        finally:
            await real.close()


async def test_auto_missing_key_effective_worker_and_identity():
    from duo_bot import effective_configuration
    configured = configuration({"DUO_AUTO_SWITCH": "1", "DUO_LLM_URL": TEST_LLM_URL})
    effective = effective_configuration(configured, has_typesafe_key=False)
    assert effective["auto_switch_requested"] is True
    assert effective["auto_switch"] is False and effective["auto_switch_reason"] == "no-key"
    async with pipeline(config=effective) as h:
        assert not isinstance(h.llm, RouteStampingLLM)
        assert h.control.hold is None and h.control.addressee is None
        assert identity(effective)["config"]["auto_switch_reason"] == "no-key"
        await h.hello(); await h.turn(); await auto_reply(h)
    assert effective_configuration(configured, has_typesafe_key=True)["auto_switch"] is True


async def test_auto_addressee_warm_up_runs_beside_startup_and_is_joined_before_close(monkeypatch):
    from duo_bot import open_addressee
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-placeholder")
    events, release = [], asyncio.Event()
    class Warm:
        def __init__(self, key, *, budget, model):
            events.append(("made", budget, model))
        async def connect(self):
            events.append("connect-started")
            try:
                await release.wait()
                events.append("connect-finished")
            except asyncio.CancelledError:
                await asyncio.sleep(.01)  # Cancellation cleanup takes a moment.
                events.append("connect-cancelled")
                raise
        async def close(self):
            events.append("closed")
    config = configuration({"DUO_AUTO_SWITCH": "1"})
    async with open_addressee(config, factory=Warm) as addressee:
        await asyncio.sleep(0)
        # The session is built while the warm-up request is still in flight.
        assert isinstance(addressee, Warm)
        assert events == [("made", .5, "jev-1.14.0"), "connect-started"]
    assert events[2:] == ["connect-cancelled", "closed"]
    events.clear(); release.set()
    async with open_addressee(config, factory=Warm):
        await until(lambda: "connect-finished" in events)
    assert events[1:] == ["connect-started", "connect-finished", "closed"]
    async with open_addressee(configuration({}), factory=Warm) as absent:
        assert absent is None
    assert len(events) == 4  # No client exists with auto-switch off.


@pytest.mark.parametrize("failure", ["build", "cancel", "cancel-during-join"])
async def test_auto_addressee_is_closed_after_failure_and_cancellation_of_the_session(monkeypatch, failure):
    """Sol S2, with the real client behind an in-memory HTTP boundary."""
    import httpx
    from duo_addressee import Addressee
    from duo_bot import open_addressee
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-placeholder")
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events, made = [], []
    async def handler(request):  # The warm-up never answers, and its cancellation cleanup waits.
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("request-cleanup-started")
            cleaning.set()
            await release.wait()
            events.append("request-cleanup-finished")
    http_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client(**kw, transport=httpx.MockTransport(handler)))
    def factory(*args, **kwargs):
        made.append(Addressee(*args, **kwargs))
        return made[0]
    async def session():
        async with open_addressee(configuration({"DUO_AUTO_SWITCH": "1"}), factory=factory):
            await entered.wait()
            if failure != "cancel":
                raise RuntimeError("pipeline construction failed")
            await asyncio.Event().wait()
    task = asyncio.create_task(session())
    try:
        await entered.wait()
        if failure == "cancel":
            task.cancel()
        await cleaning.wait()  # The exit is joining the warm-up now.
        router = made[0]
        if failure == "cancel-during-join":
            task.cancel()
            await asyncio.sleep(.02)
        assert not task.done() and not router._closed and not router.client._http.is_closed
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
    finally:
        release.set()
    assert events == ["request-cleanup-started", "request-cleanup-finished"]
    assert router._closed and router.client._http.is_closed and not router._pending
    assert isinstance(result, RuntimeError if failure == "build" else asyncio.CancelledError)


async def test_auto_addressee_transport_options_are_passed_only_when_set(monkeypatch):
    from duo_bot import open_addressee
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-placeholder")
    built = []
    class Recorded:
        def __init__(self, key, **options):
            built.append(options)
        async def connect(self):
            pass
        async def close(self):
            pass
    plain = configuration({"DUO_AUTO_SWITCH": "1"})
    assert (plain["jev_keepwarm_s"], plain["jev_hedge_s"], plain["cancel_join_bound_s"]) == (None, None, None)
    async with open_addressee(plain, factory=Recorded):
        pass
    assert built == [dict(budget=.5, model="jev-1.14.0")]  # The default client is built as before.
    tuned = configuration({"DUO_AUTO_SWITCH": "1", "DUO_JEV_KEEPWARM_S": "10", "DUO_JEV_HEDGE_MS": "150",
                           "DUO_CANCEL_JOIN_MS": "0"})
    async with open_addressee(tuned, factory=Recorded):
        pass
    assert built[1] == dict(budget=.5, model="jev-1.14.0", hedge_s=.15, keepwarm_s=10, join_bound_s=0)
    manifest = identity(tuned)["config"]
    assert (manifest["jev_keepwarm_s"], manifest["jev_hedge_s"]) == (10, .15)
    assert (manifest["route_budget_s"], manifest["auto_switch_floor"], manifest["jev_model"]) == (.5, .80, "jev-1.14.0")
    for bad in ({"DUO_JEV_KEEPWARM_S": "0.5"}, {"DUO_JEV_KEEPWARM_S": "121"}, {"DUO_JEV_KEEPWARM_S": "often"},
                {"DUO_JEV_HEDGE_MS": "0"}, {"DUO_JEV_HEDGE_MS": "500"}, {"DUO_JEV_HEDGE_MS": "nan"},
                {"DUO_JEV_HEDGE_MS": "150", "DUO_ROUTE_BUDGET_S": "0.1"}):
        with pytest.raises(ValueError):
            configuration(bad)


@pytest.mark.parametrize("travelled", [None, {}, {"connection": "new", "connect_ms": 31.5, "tls_ms": 212.0}])
async def test_auto_route_event_says_how_the_read_travelled_when_the_client_reports_it(travelled):
    fake = FakeAddressee("either")
    if travelled is not None:
        fake.last_read = travelled
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn(); await auto_reply(h)
        route = next(data for kind, data, _ in h.events if kind == "route")
        assert route["reason"] == "stay"
        if travelled:
            assert route["jev"] == travelled and route["jev"] is not travelled
        else:
            assert "jev" not in route


@pytest.mark.parametrize("policy", ["below-floor", "no-baseline", "unusable", "failed-voice", "no-turn"])
async def test_auto_declined_policy_and_no_turn_call_counts(policy):
    fake = FakeAddressee("devil", delay=.03)
    if policy == "below-floor":
        async def low(state):
            return ChoiceResult(choice="devil", probabilities={"devil": .6, "angel": .4}, confidence=1)
        fake.script = deque([low])
    async with pipeline(auto_switch=True, addressee=fake) as h:
        if policy != "no-baseline":
            await h.hello()
        if policy == "unusable":
            await h.services["devil"].set_usable(False)
        if policy == "failed-voice":
            h.control.failed_voices.add(h.services["devil"])
        if policy != "no-turn":
            await h.turn(); await auto_reply(h)
        assert len(fake.calls) == (0 if policy == "no-turn" else 1)
        assert not any(s.get("reason") == "addressed" for s in h.states())


def routes(h):
    return [data for kind, data, _ in h.events if kind == "route"]


def answered(choice, p):
    async def answer(state):
        return ChoiceResult(choice=choice, probabilities={choice: p}, confidence=p)
    return answer


VOCATIVE = {"vocative_rule": True}


def test_vocative_rule_is_off_unless_asked_and_recorded_in_identity():
    off, on = configuration({}), configuration({"DUO_VOCATIVE_RULE": "1"})
    assert off["vocative_rule"] is False and on["vocative_rule"] is True
    assert identity(on)["config"]["vocative_rule"] is True
    with pytest.raises(ValueError, match="DUO_VOCATIVE_RULE"):
        configuration({"DUO_VOCATIVE_RULE": "yes"})


@pytest.mark.parametrize("latest,jev,jev_target,jev_reason", [
    ("Devil, what should I read next?", ("devil", .77), None, "below-floor"),  # The S3 soak turn.
    ("Devil.", ("devil", .68), None, "below-floor"),                           # The phrase Flux split.
    ("Hey Devil, are you there?", ("either", .9), None, "stay"),
    ("Devil, should I eat cake?", ("devil", .95), "devil", None),              # Jev agrees.
])
async def test_auto_vocative_rule_switches_on_a_leading_call_and_logs_what_jev_said(latest, jev, jev_target, jev_reason):
    fake = FakeAddressee(answered(*jev), delay=.02)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        await h.hello(); await h.turn(latest); await auto_reply(h, "devil")
        assert len(fake.calls) == 1 and fake.calls[0]["latest"]["said"] == latest
        [route] = routes(h)
        assert (route["target"], route["reason"], route["p"]) == ("devil", "vocative-rule", jev[1])
        assert (route["jev_target"], route["jev_reason"]) == (jev_target, jev_reason)
        assert len([s for s in h.states() if s.get("reason") == "addressed"]) == 1
        assert not h.services["angel"].speaks and auto_user(h.llm.calls[-1]) == latest


async def test_auto_vocative_rule_changes_nothing_while_unset():
    fake = FakeAddressee(answered("devil", .77), delay=.02)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn("Devil, what should I read next?"); await auto_reply(h)
        [route] = routes(h)
        assert (route["target"], route["reason"], route["p"]) == (None, "below-floor", .77)
        assert "jev_reason" not in route and "jev_target" not in route
        assert not any(s.get("reason") == "addressed" for s in h.states())


@pytest.mark.parametrize("latest,jev,reason", [
    ("Spare me a response from Devil", ("either", .9), "stay"),      # The name does not lead.
    ("The devil said I should rest.", ("either", .9), "stay"),
    ("Devil's advocate: what if I stay?", ("devil", .6), "below-floor"),
    ("Angel, should I rest?", ("either", .9), "stay"),               # Calls the one already speaking.
])
async def test_auto_vocative_rule_leaves_every_other_turn_to_jev(latest, jev, reason):
    fake = FakeAddressee(answered(*jev), delay=.02)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        await h.hello(); await h.turn(latest); await auto_reply(h)
        [route] = routes(h)
        assert (route["target"], route["reason"], route["p"]) == (None, reason, jev[1])
        assert "jev_reason" not in route and "jev_target" not in route
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_vocative_rule_does_not_stand_in_jevs_way():
    # The turn calls the Angel, who is speaking, and asks for the Devil: Jev's switch, not the rule's.
    fake = FakeAddressee("devil", delay=.02)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        await h.hello(); await h.turn("Angel, let the Devil answer this one."); await auto_reply(h, "devil")
        [route] = routes(h)
        assert (route["target"], route["reason"]) == ("devil", None) and "jev_reason" not in route


async def test_auto_vocative_rule_claims_before_the_deadline_when_jev_is_slow():
    # Without the rule a read this slow ends at the deadline, which has released the draft by then.
    fake = FakeAddressee("devil", delay=2)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        assert h.control.route_budget_s == .5 and h.control.RULED_READ_MARGIN_S == .05
        await h.hello(); await h.turn("Devil, are you there?"); await auto_reply(h, "devil")
        [route] = routes(h)
        assert (route["target"], route["reason"], route["p"]) == ("devil", "vocative-rule", 0)
        assert (route["jev_target"], route["jev_reason"]) == (None, "timeout")
        assert 440 <= route["elapsed_ms"] < 500  # The read was given up 50 ms before the 500 ms deadline.
        assert fake.cancelled == 1 and not h.services["angel"].speaks
        assert len([s for s in h.states() if s.get("reason") == "addressed"]) == 1


# The rule sits where a read is decided, so the guards that stop a Jev switch stop a ruled one.
async def test_auto_vocative_rule_does_not_switch_an_interrupted_turn(join_bound):
    fake = FakeAddressee("either", "either", delay=.12)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        await h.hello(); await h.turn("Devil, A")
        await until(lambda: h.control.hold.held)
        old = next(iter(h.control.hold.routes))
        await h.worker.queue_frame(InterruptionWorkerFrame())
        await until(lambda: h.control.hold.decision(old) == "superseded")
        assert not h.services["angel"].speaks
        await h.turn("B"); await auto_reply(h)
        assert [(r["target"], r["reason"]) for r in routes(h)] == [(None, "superseded"), (None, "stay")]
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_vocative_rule_switches_only_the_newest_of_two_rapid_calls(join_bound):
    fake = FakeAddressee("either", "either", delay=.12)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        await h.hello(); await h.turn("Devil, A")
        await fake.started.wait()
        await h.turn("Devil, B")
        await auto_reply(h, "devil")
        assert fake.cancelled == 1 and len(fake.calls) == 2
        assert [(r["target"], r["reason"]) for r in routes(h)] == [(None, "superseded"), ("devil", "vocative-rule")]
        assert len([s for s in h.states() if s.get("reason") == "addressed"]) == 1
        assert auto_user(h.llm.calls[-1]) == "Devil, B" and not h.services["angel"].speaks


async def test_auto_vocative_rule_yields_to_a_device_request(join_bound):
    fake = FakeAddressee("either", delay=2)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        await h.hello(); await h.turn("Devil, A"); await fake.started.wait()
        await h.send(); result = await h.outcome()
        assert result["character"] == "devil" and "reason" not in result  # The tap's switch, not an addressed one.
        assert fake.cancelled == 1 and fake.joined.is_set()
        assert [(r["target"], r["reason"]) for r in routes(h)] == [(None, "preempt")]
        assert not any(s.get("reason") == "addressed" for s in h.states())


@pytest.mark.parametrize("policy", ["no-baseline", "unusable", "failed-voice"])
async def test_auto_vocative_rule_needs_a_character_that_can_take_the_turn(policy):
    fake = FakeAddressee("either", delay=.03)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE) as h:
        if policy != "no-baseline":
            await h.hello()
        if policy == "unusable":
            await h.services["devil"].set_usable(False)
        if policy == "failed-voice":
            h.control.failed_voices.add(h.services["devil"])
        await h.turn("Devil, A"); await auto_reply(h)
        [route] = routes(h)
        assert (route["target"], route["reason"]) == (None, "not-eligible")
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_vocative_rule_cannot_claim_a_turn_the_deadline_released():
    # The coordinator is busy past the route deadline: the timer releases the draft and the route is never read.
    fake = FakeAddressee("either", delay=.2)
    async with pipeline(auto_switch=True, addressee=fake, config=VOCATIVE | {"route_budget_s": .06}) as h:
        await h.hello()
        publish, entered = h.control._publish, asyncio.Event()
        async def slow_publish(result):
            entered.set()
            await asyncio.sleep(.2)
            await publish(result)
        h.control._publish = slow_publish
        await h.send("hello", seq=1)
        await entered.wait()
        await h.turn("Devil, A")
        await until(lambda: h.services["angel"].speaks)
        assert not fake.calls
        await auto_reply(h)
        assert not routes(h) and not any(s.get("reason") == "addressed" for s in h.states())


def test_auto_nonapproved_credentials_keep_exported_key(monkeypatch):
    import settings
    monkeypatch.setenv("DEEPGRAM_API_KEY", "offline-deepgram")
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-exported")
    monkeypatch.setattr(settings, "load_dotenv", lambda *a, **kw: None)
    def forbidden(*a, **kw):
        raise AssertionError("An exported key must avoid the TypeSafe file read")
    monkeypatch.setattr(settings, "dotenv_values", forbidden)
    settings.load_credentials(approved_only=False)
    assert settings.os.environ["TYPESAFE_API_KEY"] == "offline-exported"


@pytest.mark.parametrize("when", ["voice", "interrupt", "mutate", "publish", "reopen", "rollback", "same-context", "same-run", "several"])
async def test_auto_20_24_message_ownership_counts_match_flagoff_transaction(when):
    from pipecat.frames.frames import LLMRunFrame
    associations = []
    for enabled in (False, True):
        fake = FakeAddressee("devil", delay=.04)
        async with pipeline(auto_switch=enabled, addressee=fake) as h:
            await h.hello()
            h.control.context.add_message(dict(role="user", content="A"))
            await auto_open_turn(h, "B")
            h.llm.first_frame_delay = .12
            hook = {"voice": "_voice", "interrupt": "_interrupt", "mutate": "_mutate",
                    "publish": "_publish", "reopen": None, "rollback": "_restore",
                    "same-context": "_mutate", "same-run": "_mutate", "several": "_mutate"}[when]
            original = h.control.gate.reopen if hook is None else getattr(h.control, hook)
            once = True
            async def operation(*args, **kwargs):
                nonlocal once
                if not once:
                    return await original(*args, **kwargs)
                once = False
                if when == "same-context":
                    await h.control.user_aggregator.push_context_frame()
                elif when == "same-run":
                    await h.worker.queue_frame(LLMRunFrame())
                else:
                    assert await h.control.user_aggregator.push_aggregation() == "B"
                    if when == "several":
                        for _ in range(2):
                            await h.control.user_aggregator.push_context_frame()
                await until(lambda: sum(isinstance(f, LLMContextFrame) for f, _ in h.control.gate.fence.held)
                            >= (3 if when == "several" else 1))
                return await original(*args, **kwargs)
            if hook:
                setattr(h.control, hook, operation)
            else:
                h.control.gate.reopen = operation
            if when == "rollback":
                h.stt.sabotage = ManuallySwitchServiceFrame
            await h.control.user_aggregator.push_context_frame()
            if not enabled:
                await h.send()
            expected_count = 3 if when == "several" else 1
            character = "angel" if when == "rollback" else "devil"
            await auto_reply(h, character, count=expected_count)
            associations.append([(c[0], auto_user(c)) for c in h.llm.calls])
            assert len(h.llm.calls) == expected_count
            assert len(fake.calls) == int(enabled)
    expected_user = "A" if when in ("same-context", "same-run") else "B"
    assert associations[0] == associations[1] == [(character_instructions(character), expected_user)] * expected_count


@pytest.mark.parametrize("target", ["either", "devil"])
async def test_auto_17_thought_frames_and_cancelled_end_stamped_at_source(target):
    from pipecat.frames.frames import (LLMFullResponseEndFrame, LLMThoughtStartFrame,
                                       LLMThoughtTextFrame, LLMThoughtEndFrame)
    from duo_route import RESPONSE_FRAMES
    fake = FakeAddressee(target, delay=.06)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello()
        process = h.llm._process_context
        async def thoughts(context):
            await h.llm.push_frame(LLMThoughtStartFrame())
            await h.llm.push_frame(LLMThoughtTextFrame("Private thought."))
            await h.llm.push_frame(LLMThoughtEndFrame())
            await process(context)
        h.llm._process_context = thoughts
        seen = []
        original = h.control.hold.process_frame
        async def capture(frame, direction):
            if isinstance(frame, RESPONSE_FRAMES):
                seen.append((type(frame), frame.metadata.get("duo-route")))
            await original(frame, direction)
        h.control.hold.process_frame = capture
        await h.turn()
        await auto_reply(h, "devil" if target == "devil" else "angel")
        token = next(iter(h.control.hold.routes))
        assert {cls for cls, owner in seen if owner == token} == set(RESPONSE_FRAMES)
        if target == "devil":
            assert {cls for cls, owner in seen if owner is None} == set(RESPONSE_FRAMES)
        else:
            assert all(owner == token for _, owner in seen)
        assert h.llm._route is None


async def test_auto_17_simultaneous_expiry_and_result_has_one_terminal_decision():
    fake = FakeAddressee()
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": .05}) as h:
        await h.hello()
        hold = h.control.hold
        transitions = []
        decide_record = hold.decide
        def capture(token, decision):
            won = decide_record(token, decision)
            if won:
                transitions.append((token, decision))
            return won
        hold.decide = capture
        async def at_deadline(state):
            token = h.control.route_read[0]
            await asyncio.sleep(max(0, hold.routes[token].deadline - asyncio.get_running_loop().time()))
            return ChoiceResult(choice="devil", probabilities={"devil": 1}, confidence=1)
        fake.script.append(at_deadline)
        await h.turn(); await auto_reply(h)
        token = next(iter(hold.routes))
        assert transitions == [(token, "release")]
        assert not hold.claim_switch(token)
        assert not any(s.get("reason") == "addressed" for s in h.states())


async def test_auto_17_cancelled_draft_finally_end_keeps_original_token():
    from pipecat.frames.frames import LLMFullResponseEndFrame
    fake = FakeAddressee("devil", delay=.05)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); h.llm.remainder.clear()
        ends = []
        process = h.control.hold.process_frame
        async def capture(frame, direction):
            if isinstance(frame, LLMFullResponseEndFrame):
                ends.append(frame.metadata.get("duo-route"))
            await process(frame, direction)
        h.control.hold.process_frame = capture
        await h.turn()
        await until(lambda: any(s.get("reason") == "addressed" for s in h.states()))
        h.llm.remainder.set(); await auto_reply(h, "devil")
        token = next(iter(h.control.hold.routes))
        assert ends == [token, None]
        assert h.services["devil"].text_responses == [2, 2]


@pytest.fixture
def timing_logs():
    import json
    from loguru import logger
    rows = []
    def capture(message):
        text = message.record["message"]
        if text.startswith("DUO timing "):
            assert message.record["level"].name == "INFO"
            rows.append(json.loads(text.removeprefix("DUO timing ")))
    sink = logger.add(capture)
    try:
        yield rows
    finally:
        logger.remove(sink)


@pytest.mark.parametrize("auto_switch,explicit,installed", [
    (False, None, False), (True, None, True), (False, True, True), (True, False, False)])
async def test_auto_timing_observer_follows_auto_switch_unless_told(auto_switch, explicit, installed, timing_logs):
    from duo_bot import DuoTimingObserver
    assert configuration({})["timing"] is None
    assert configuration({"DUO_TIMING": "1"})["timing"] is True
    assert configuration({"DUO_TIMING": "0"})["timing"] is False
    with pytest.raises(ValueError):
        configuration({"DUO_TIMING": "yes"})
    async with pipeline(auto_switch=auto_switch, config={"timing": explicit}) as h:
        observers = [o for o in h.worker._observer._observers if isinstance(o, DuoTimingObserver)]
        assert len(observers) == int(installed)
        assert (h.control.timing is not None) == (h.control.gate.fence.timing is not None) == installed
        await h.hello(); await h.turn(); await auto_reply(h)
        if installed:
            await until(lambda: timing_logs)
            assert timing_logs[0]["mode"] == ("on" if auto_switch else "off")
    assert len(timing_logs) == int(installed)


@pytest.mark.parametrize("mode,target,delay,token_delay,decision,path", [
    (False, "either", 0, 0, "unrouted", "unrouted"),
    (True, "either", .005, .06, "stay", "routed"),
    (True, "either", .05, 0, "stay", "routed"),
    (True, "devil", .05, 0, "switch", "rerun"),
    (True, "either", 2, 0, "timeout", "routed"),
])
async def test_auto_timing_one_row_at_written_audio(mode, target, delay, token_delay, decision, path, timing_logs):
    fake = FakeAddressee(target, delay=delay)
    async with pipeline(auto_switch=mode, addressee=fake, config={"route_budget_s": .09, "timing": True}) as h:
        await h.hello()
        h.llm.first_token_delay = token_delay
        await h.turn("timing test utterance")
        character = "devil" if target == "devil" else "angel"
        await auto_reply(h, character)
        await until(lambda: len(timing_logs) == 1)
        row = timing_logs[0]
        assert row["mode"] == ("on" if mode else "off")
        assert row["route_mode"] == path and row["decision"] == decision
        assert row["origin"] == "aggregator-user-stopped"
        assert row["reply_id"] in h.services[character].starts
        assert all(isinstance(row[k], (int, float)) for k in ("fence_ms", "T1_ms", "S1_ms", "A_ms", "held_delay_ms"))
        assert row["T1_ms"] <= row["S1_ms"] <= row["A_ms"]
        assert row["hold_delayed"] == (row["held_delay_ms"] > 0)
        if not mode:
            assert row["J_ms"] is None and row["p"] is None
            assert row["W_ms"] is None
            assert row["held_delay_ms"] == 0
        else:
            assert isinstance(row["J_ms"], (int, float))
            assert row["p"] == (0 if decision == "timeout" else .99)
        if mode and path == "routed" and not token_delay:
            assert row["held_delay_ms"] >= 20
        else:
            assert row["held_delay_ms"] == 0
        # A is the observation after the actual transport write, not TTS audio production.
        actual_gap = h.transport.client.audio_times[0] - h.services[character].speaks[0][1]
        assert abs((row["A_ms"] - row["S1_ms"]) / 1000 - actual_gap) < .02
        assert "timing test utterance" not in str(row)
    assert len(timing_logs) == 1  # Completion/shutdown never duplicates the first-audio row.


async def test_auto_timing_unrouted_busy_turn(timing_logs):
    from types import SimpleNamespace
    async with pipeline(auto_switch=True) as h:
        await h.hello(); await auto_open_turn(h, "A busy turn")
        h.stt.flush_delay = .06
        await h.send(); await until(lambda: h.control.gate.closed)
        user = h.control.user_aggregator
        await user.push_aggregation()
        # Model the turn controller's public end event after this independent completion.
        await user._call_event_handler("on_user_turn_stopped", None, SimpleNamespace(content="A busy turn"))
        await h.outcome(); await auto_reply(h, "devil")
        await until(lambda: timing_logs)
        assert not h.addressee.calls
        row = timing_logs[0]
        assert (row["mode"], row["route_mode"], row["decision"]) == ("on", "unrouted", "unrouted")
        assert row["J_ms"] is None and row["W_ms"] is None and row["held_delay_ms"] == 0
        assert isinstance(row["A_ms"], (int, float))


@pytest.mark.parametrize("mode", [False, True])
async def test_auto_timing_two_turns_have_independent_origins(mode, timing_logs):
    async with pipeline(auto_switch=mode, config={"timing": True}) as h:
        await h.hello()
        await h.turn("First question"); await auto_reply(h)
        await until(lambda: len(timing_logs) == 1)
        await asyncio.sleep(.03)
        await h.turn("Second question"); await auto_reply(h, count=2)
        await until(lambda: len(timing_logs) == 2)
        assert timing_logs[0]["reply_id"] != timing_logs[1]["reply_id"]
        assert all(r["origin"] == "aggregator-user-stopped" for r in timing_logs)
        assert all(0 <= r["A_ms"] < 80 for r in timing_logs)
    assert len(timing_logs) == 2


async def test_auto_timing_interrupted_draft_does_not_log_or_relabel_next_reply(timing_logs):
    fake = FakeAddressee("devil", "either", delay=.1)
    async with pipeline(auto_switch=True, addressee=fake) as h:
        await h.hello(); await h.turn("Draft")
        await until(lambda: h.control.hold.held)
        await h.worker.queue_frame(InterruptionWorkerFrame())
        assert await h.worker.flush_pipeline(timeout=.3)
        assert not timing_logs
        await h.turn("Next turn"); await auto_reply(h)
        await until(lambda: timing_logs)
        assert len(timing_logs) == 1
        assert timing_logs[0]["route_mode"] == "routed"
        assert timing_logs[0]["decision"] == "stay"
        assert timing_logs[0]["reply_id"] == h.services["angel"].starts[0]


async def test_auto_timing_no_audio_reports_null_write(timing_logs):
    async with pipeline(auto_switch=False, config={"timing": True}) as h:
        await h.hello(); h.services["angel"].emit_audio = False
        await h.turn()
        await until(lambda: timing_logs)
        assert not h.transport.client.audio
        assert len(timing_logs) == 1
        assert timing_logs[0]["A_ms"] is None
        assert timing_logs[0]["S1_ms"] is not None


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("boundary", ["llm", "fence"])
async def test_auto_fix1_pretry_bargein_retires_cancelled_context(enabled, rollback, boundary, timing_logs, join_bound):
    release_a = asyncio.Event()
    a_result_returned = asyncio.Event()
    async def delayed_a(state):
        await release_a.wait()
        a_result_returned.set()
        return ChoiceResult(choice="devil", probabilities={"devil": .99}, confidence=.99)
    fake = FakeAddressee(delayed_a, "devil")
    fake.cancel_delay = .03
    async with pipeline(auto_switch=enabled, addressee=fake, config={"route_budget_s": 1, "timing": True}) as h:
        await h.hello()
        observer = h.llm._setup.observer
        original = observer.on_process_frame
        watched = h.llm if boundary == "llm" else h.control.gate.fence
        parked, cancelled = asyncio.Event(), asyncio.Event()
        once = True
        async def suspend(data):
            nonlocal once
            if once and data.processor is watched and isinstance(data.frame, LLMContextFrame):
                once = False; parked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            await original(data)
        observer.on_process_frame = suspend
        await h.turn("A: Devil, answer me")
        await parked.wait()
        if enabled:
            await fake.started.wait()
            token = next(iter(h.control.hold.routes))
            assert not h.control.hold.held and not h.llm.calls
        await h.worker.queue_frames([ProposedUserStartedSpeakingFrame(),
            TranscriptionFrame(text="B: Devil, answer this instead", user_id="u", timestamp="now", finalized=True)])
        assert await h.worker.flush_pipeline(timeout=.5)
        assert cancelled.is_set()
        assert h.control.user_aggregator.aggregation_string() == "B: Devil, answer this instead"
        if enabled:
            assert h.control.hold.decision(token) == "superseded"
            if join_bound is not None:
                # The interruption no longer waits for the request; it is still joined.
                await until(fake.joined.is_set)
                await until(lambda: fake.active == 0)
            assert fake.joined.is_set() and fake.active == 0 and fake.cancelled == 1
        release_a.set()
        await asyncio.sleep(.04)
        assert not a_result_returned.is_set()
        assert not h.llm.calls and not timing_logs
        assert not any(s.get("reason") == "addressed" for s in h.states())
        if enabled and rollback:
            h.stt.sabotage = ManuallySwitchServiceFrame
        await h.worker.queue_frame(ProposedUserStoppedSpeakingFrame())
        character = "devil" if enabled and not rollback else "angel"
        await auto_reply(h, character)
        assert len(fake.calls) == (2 if enabled else 0)
        assert len(h.llm.calls) == (2 if enabled else 1)
        assert all(auto_user(call) == "B: Devil, answer this instead" for call in h.llm.calls)
        assert len([s for s in h.states() if s.get("reason") == "addressed"]) == int(enabled and not rollback)
        await until(lambda: len(timing_logs) == 1)
        assert h.control.route_read is None
        if enabled:
            assert h.llm._route is None


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("boundary", ["llm", "fence"])
async def test_auto_fix1_pretry_claimed_context_keeps_rerun(rollback, boundary):
    fake = FakeAddressee("devil", delay=.04)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        observer = h.llm._setup.observer
        original = observer.on_process_frame
        watched = h.llm if boundary == "llm" else h.control.gate.fence
        parked = asyncio.Event()
        once = True
        async def suspend(data):
            nonlocal once
            if once and data.processor is watched and isinstance(data.frame, LLMContextFrame):
                once = False; parked.set()
                await asyncio.Event().wait()
            await original(data)
        observer.on_process_frame = suspend
        if rollback:
            h.stt.sabotage = ManuallySwitchServiceFrame
        await h.turn("Devil, answer A")
        await parked.wait()
        await auto_reply(h, "angel" if rollback else "devil")
        token = next(iter(h.control.hold.routes))
        assert h.control.hold.decision(token) == "quarantine"
        assert h.control.hold.outputs[token].cancelled is False
        assert len(h.llm.calls) == len(fake.calls) == 1
        assert auto_user(h.llm.calls[0]) == "Devil, answer A"
        assert fake.cancelled == 0


@pytest.mark.parametrize("where", ["process-queue", "input-queue"])
async def test_auto_fix1_queued_context_cancelled_before_start(where, join_bound):
    release_a, release_processor, parked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    returned_a = asyncio.Event()
    async def result_a(state):
        await release_a.wait()
        returned_a.set()
        return ChoiceResult(choice="devil", probabilities={"devil": .99}, confidence=.99)
    fake = FakeAddressee(result_a, "either")
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        observer = h.llm._setup.observer
        original = observer.on_process_frame
        once = True
        async def suspend(data):
            nonlocal once
            frame = data.frame
            boundary = (isinstance(frame, TransactionMarker) and frame.transaction == "park-queued-context") if where == "process-queue" else isinstance(frame, InterruptionFrame)
            if once and data.processor is h.llm and boundary:
                once = False; parked.set()
                await release_processor.wait()
            await original(data)
        observer.on_process_frame = suspend
        try:
            if where == "process-queue":
                await h.worker.queue_frame(TransactionMarker("park-queued-context"))
                await parked.wait()
            await h.turn("A queued")
            await parked.wait(); await fake.started.wait()
            await until(lambda: h.llm._queued_route_contexts)
            queued = next(iter(h.llm._queued_route_contexts.values()))
            token = queued.metadata["duo-route"]
            if where == "process-queue":
                await until(lambda: h.llm.has_queued_frame(LLMContextFrame))
            else:
                assert not h.llm.has_queued_frame(LLMContextFrame)
            assert not h.llm.calls and not h.control.hold.held
            await h.worker.queue_frames([ProposedUserStartedSpeakingFrame(),
                TranscriptionFrame(text="B owns the next reply", user_id="u", timestamp="now", finalized=True)])
            await until(fake.joined.is_set)
            assert h.control.hold.decision(token) == "superseded"
            assert queued.metadata["duo-route-cancelled"] is True
            assert not h.llm._queued_route_contexts
            release_a.set(); release_processor.set()
            assert await h.worker.flush_pipeline(timeout=.5)
            assert not returned_a.is_set() and not h.llm.calls
            assert not h.llm._cancelled_route_reads
            await h.worker.queue_frame(ProposedUserStoppedSpeakingFrame())
            await auto_reply(h)
            assert len(fake.calls) == 2 and fake.cancelled == 1
            assert [auto_user(call) for call in h.llm.calls] == ["B owns the next reply"]
            assert not any(s.get("reason") == "addressed" for s in h.states())
        finally:
            release_processor.set()


@pytest.mark.parametrize("closed_before", [False, True])
async def test_auto_fix1_gate_state_is_snapshotted_and_rechecked_after_observer_await(closed_before):
    async with pipeline(auto_switch=True, addressee=FakeAddressee()) as h:
        await h.hello()
        h.control.context.add_message(dict(role="user", content="Fence snapshot"))
        gate = h.control.gate
        if closed_before:
            gate.close()
        observer = h.llm._setup.observer
        original = observer.on_process_frame
        parked, release = asyncio.Event(), asyncio.Event()
        once = True
        async def suspend(data):
            nonlocal once
            if once and data.processor is gate.fence and isinstance(data.frame, LLMContextFrame):
                once = False; parked.set()
                await release.wait()
            await original(data)
        observer.on_process_frame = suspend
        try:
            await h.control.user_aggregator.push_context_frame()
            await parked.wait()
            if closed_before:
                # The arrival is already reserved while its observer is parked.
                assert gate.fence.held and not h.addressee.calls and not h.llm.calls
                await gate.reopen()
                assert not gate.fence.held
            else:
                gate.close()
            release.set()
            if not closed_before:
                await until(lambda: gate.fence.held)
                assert not h.llm.calls
                frame = gate.fence.held[0][0]
                assert "duo-route" not in frame.metadata and "duo-output" not in frame.metadata
                token = next(iter(h.control.hold.routes))
                assert h.control.hold.outputs[token].cancelled
                assert h.control.hold.decision(token) == "superseded"
                await gate.reopen()
            await auto_reply(h)
            assert len(h.llm.calls) == 1
            assert len(h.addressee.calls) == int(not closed_before)
        finally:
            release.set()


@pytest.mark.parametrize("boundary", ["llm-finally", "fence-finally"])
async def test_auto_fix1_cancellation_cleanup_cannot_race_late_switch(boundary, join_bound):
    release_a, release_cleanup = asyncio.Event(), asyncio.Event()
    entered, cleanup_parked, a_result_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def result_a(state):
        await release_a.wait()
        a_result_returned.set()
        return ChoiceResult(choice="devil", probabilities={"devil": .99}, confidence=.99)
    fake = FakeAddressee(result_a, "either")
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        once = True
        async def cancelled_work():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_parked.set()
                await release_cleanup.wait()
        if boundary == "llm-finally":
            original = h.llm._process_context
            async def completion(context):
                nonlocal once
                if once:
                    once = False
                    h.llm.calls.append((h.llm.settings.system_instruction, list(context.get_messages())))
                    await cancelled_work()
                else:
                    await original(context)
            h.llm._process_context = completion
        else:
            observer = h.llm._setup.observer
            original = observer.on_process_frame
            async def observe(data):
                nonlocal once
                if once and data.processor is h.control.gate.fence and isinstance(data.frame, LLMContextFrame):
                    once = False
                    await cancelled_work()
                await original(data)
            observer.on_process_frame = observe
        try:
            await h.turn("A waiting on cancellation")
            await entered.wait(); await fake.started.wait()
            token = next(iter(h.control.hold.routes))
            await h.worker.queue_frames([ProposedUserStartedSpeakingFrame(),
                TranscriptionFrame(text="B after slow cleanup", user_id="u", timestamp="now", finalized=True)])
            await cleanup_parked.wait()
            await until(fake.joined.is_set)
            assert h.control.hold.decision(token) == "superseded"
            assert not h.services["angel"].speaks and not h.services["devil"].speaks
            release_a.set()
            assert not a_result_returned.is_set()
            release_cleanup.set()
            assert await h.worker.flush_pipeline(timeout=.5)
            await h.worker.queue_frame(ProposedUserStoppedSpeakingFrame())
            await auto_reply(h)
            assert auto_user(h.llm.calls[-1]) == "B after slow cleanup"
            assert h.services["angel"].text_responses == [len(h.llm.calls)] * 2
            assert len(h.llm.calls) == (2 if boundary == "llm-finally" else 1)
            assert fake.cancelled == 1 and len(fake.calls) == 2
            assert not any(s.get("reason") == "addressed" for s in h.states())
        finally:
            release_cleanup.set()


@pytest.mark.parametrize("boundary", ["llm", "fence"])
async def test_auto_fix2_b_finishes_before_a_cleanup_without_releasing_a(boundary, timing_logs, join_bound):
    cleanup_parked, release_cleanup, b_wake_processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def result_a(state):
        await asyncio.Event().wait()
    fake = FakeAddressee(result_a, "either")
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        original = h.llm._process_context
        once = True
        if boundary == "llm":
            async def completion(context):
                nonlocal once
                if not once:
                    return await original(context)
                once = False
                try:
                    await original(context)
                    await asyncio.Event().wait()
                finally:
                    cleanup_parked.set()
                    await release_cleanup.wait()
            h.llm._process_context = completion
        else:
            observer = h.llm._setup.observer
            original_observe = observer.on_process_frame
            parked = asyncio.Event()
            async def observe(data):
                nonlocal once
                if once and data.processor is h.control.gate.fence and isinstance(data.frame, LLMContextFrame):
                    once = False; parked.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup_parked.set()
                        await release_cleanup.wait()
                await original_observe(data)
            observer.on_process_frame = observe
        hold = h.control.hold
        original_hold = hold.process_frame
        async def observe_wake(frame, direction):
            await original_hold(frame, direction)
            if isinstance(frame, HoldWake) and len(fake.calls) == 2:
                b_wake_processed.set()
        hold.process_frame = observe_wake
        try:
            h.llm.tokens = ("Old A must stay silent. ", "Another old sentence. ")
            await h.turn("A")
            await fake.started.wait()
            if boundary == "llm":
                await until(lambda: len(hold.held) == 3)
            else:
                await parked.wait()
            token_a = next(iter(hold.routes))
            assert not h.services["angel"].speaks and not h.transport.client.audio
            h.llm.tokens = ("Fresh B reply. ", "B's own ending. ")
            await h.turn("B")  # Includes stop and ordinary inference, while A cleanup stays parked.
            await cleanup_parked.wait(); await until(fake.joined.is_set)
            await until(lambda: any(m.get("role") == "user" and m.get("content") == "B"
                                    for m in h.control.context.get_messages()))
            assert h.control.user_aggregator.aggregation_string() == ""
            if boundary == "llm":
                await b_wake_processed.wait()
                # Let the ordinary wake traverse TTS/output while A's native
                # cancellation cleanup remains parked (the review's 40 ms window).
                await asyncio.sleep(.04)
                assert len(fake.calls) == 2 and len(h.llm.calls) == 1
                assert hold.decision(next(reversed(hold.routes))) == "release"
            else:
                # The fence's input task itself awaits cancellation: B's inferred
                # context is queued there and cannot classify until cleanup returns.
                assert len(fake.calls) == 1 and not h.llm.calls
            assert hold.decision(token_a) == "superseded"
            assert not h.services["angel"].starts
            assert not h.services["angel"].text_responses and not h.services["angel"].speaks
            assert not h.services["angel"].owners and not h.transport.client.audio
            assert not any(m.get("role") == "assistant" for m in h.control.context.get_messages())
            release_cleanup.set()
            await auto_reply(h)
            await until(lambda: len(timing_logs) == 1)
            assert len(fake.calls) == 2 and fake.cancelled == 1
            assert [auto_user(call) for call in h.llm.calls] == (["A", "B"] if boundary == "llm" else ["B"])
            assert h.services["angel"].text_responses == [len(h.llm.calls)] * 2
            assert [text for text, _ in h.services["angel"].speaks] == ["Fresh B reply. ", "B's own ending. "]
            replies = [m["content"] for m in h.control.context.get_messages() if m.get("role") == "assistant"]
            assert len(replies) == 1 and "Fresh B reply." in replies[0] and "Old A" not in replies[0]
            assert not hold.held and not h.llm._queued_route_contexts and not h.llm._cancelled_route_reads
            assert h.control.route_read is None and fake.active == 0
        finally:
            release_cleanup.set()
    assert not hold.timers


async def test_auto_fix2_timing_separates_decision_from_parked_wake(timing_logs):
    parked, release_wake = asyncio.Event(), asyncio.Event()
    async with pipeline(auto_switch=True, addressee=FakeAddressee("either", delay=.04)) as h:
        await h.hello()
        hold = h.control.hold
        original = hold.process_frame
        observed_wake = []
        once = True
        async def suspend_wake(frame, direction):
            nonlocal once
            if once and isinstance(frame, HoldWake):
                once = False; parked.set()
                await release_wake.wait()
                observed_wake.append(h.control.timing.now())
            await original(frame, direction)
        hold.process_frame = suspend_wake
        try:
            await h.turn(); await parked.wait()
            await asyncio.sleep(.12)
            assert not h.services["angel"].speaks and not h.transport.client.audio
            release_wake.set(); await auto_reply(h)
            await until(lambda: timing_logs)
            row = timing_logs[0]
            latest = next(m for m in reversed(h.control.context.get_messages()) if m.get("role") == "user")
            origin = h.control.timing.turns[id(latest)]["end"]
            assert row["J_ms"] < row["W_ms"] - 100
            assert abs(row["W_ms"] - (observed_wake[0] - origin)) < 10
            assert row["T1_ms"] < row["J_ms"] < row["W_ms"] <= row["S1_ms"] <= row["A_ms"]
            assert row["held_delay_ms"] > 100
        finally:
            release_wake.set()


async def test_auto_fix2_cancelled_output_survives_route_cache_eviction():
    from pipecat.frames.frames import LLMFullResponseEndFrame
    async with pipeline(auto_switch=True) as h:
        await h.hello()
        hold = h.control.hold
        deadline = asyncio.get_running_loop().time() + 2
        hold.expect("cancelled-old", deadline)
        owner = hold.outputs["cancelled-old"]
        frames = [LLMFullResponseStartFrame(), LLMTextFrame("Never synthesize this. "), LLMFullResponseEndFrame()]
        for frame in frames:
            frame.metadata.update({"duo-route": "cancelled-old", "duo-output": owner, "test-response": 99})
            await hold.queue_frame(frame)
        await until(lambda: len(hold.held) == 3)
        h.control.context_cancelled("cancelled-old")
        assert hold.decision("cancelled-old") == "superseded" and owner.cancelled
        for number in range(10):
            hold.expect(f"newer-{number}", deadline)
        assert "cancelled-old" not in hold.routes and len(hold.routes) == 8
        assert hold.decision("cancelled-old") == "release"  # Existing evicted-record policy stays intact.
        await hold.queue_frame(HoldWake())
        late = LLMTextFrame("A late old token. ")
        late.metadata.update({"duo-route": "cancelled-old", "duo-output": owner, "test-response": 99})
        await hold.queue_frame(late)
        assert await h.worker.flush_pipeline(timeout=.5)
        assert not hold.held and not h.services["angel"].starts and not h.transport.client.audio
        await h.turn("A genuinely newer turn"); await auto_reply(h)
        assert h.services["angel"].text_responses == [1, 1]
        assert len(h.addressee.calls) == 1


@pytest.mark.parametrize("decision", ["quarantine", "stopped"])
async def test_auto_route_decision_survives_token_index_eviction(decision):
    """Hardening: a frame is judged by the route it carries, not by the eight-entry index.

    No pipeline schedule is known to reach this (a claimed route closes the
    gate, so nothing is admitted until its run has drained), which is why the
    routes are created on the hold directly.
    """
    from pipecat.frames.frames import LLMFullResponseEndFrame
    async with pipeline(auto_switch=True) as h:
        await h.hello()
        hold = h.control.hold
        deadline = asyncio.get_running_loop().time() + 2
        hold.expect("decided-old", deadline)
        route = hold.routes["decided-old"]
        assert route is hold.outputs["decided-old"] and route.token == "decided-old"
        assert hold.decide("decided-old", decision)
        for number in range(8):
            hold.expect(f"newer-{number}", deadline)
        assert "decided-old" not in hold.routes and len(hold.routes) == 8
        assert hold.decision("decided-old") == "release"  # The index has forgotten the token.
        assert not hold.decide("decided-old", "release") and route.decision == decision
        for frame in (LLMFullResponseStartFrame(), LLMTextFrame("Never synthesize this. "), LLMFullResponseEndFrame()):
            frame.metadata.update({"duo-route": "decided-old", "duo-output": route, "test-response": 99})
            await hold.queue_frame(frame)
        assert await h.worker.flush_pipeline(timeout=.5)
        assert not hold.held and not h.services["angel"].starts and not h.transport.client.audio
        await h.turn("A genuinely newer turn"); await auto_reply(h)
        assert h.services["angel"].text_responses == [1, 1]


async def test_auto_fix2_cancelled_release_cannot_cross_suspended_push():
    cleanup_parked, release_cleanup = asyncio.Event(), asyncio.Event()
    push_parked, release_push, late_push_returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    b_wake_processed = asyncio.Event()
    fake = FakeAddressee("either", "either", delay=.04)
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        hold = h.control.hold
        original_completion, original_push, original_hold = h.llm._process_context, hold.push_frame, hold.process_frame
        completion_once, push_once = True, True
        token_b = None
        async def completion(context):
            nonlocal completion_once
            if not completion_once:
                return await original_completion(context)
            completion_once = False
            try:
                await original_completion(context)
                await asyncio.Event().wait()
            finally:
                cleanup_parked.set()
                await release_cleanup.wait()
        async def suspended_push(frame, direction=DOWN):
            nonlocal push_once
            if push_once and isinstance(frame, LLMFullResponseStartFrame):
                push_once = False; push_parked.set()
                await release_push.wait()
                assert frame.metadata["duo-output"].cancelled
                await original_push(frame, direction)  # Deliberately enqueue after cancellation.
                late_push_returned.set()
            else:
                await original_push(frame, direction)
        async def observe_wake(frame, direction):
            await original_hold(frame, direction)
            if isinstance(frame, HoldWake) and frame.token == token_b:
                b_wake_processed.set()
        h.llm._process_context, hold.push_frame, hold.process_frame = completion, suspended_push, observe_wake
        try:
            h.llm.tokens = ("Old A must stay silent. ", "Another old sentence. ")
            await h.turn("A"); await push_parked.wait()
            token_a = next(iter(hold.routes))
            assert hold.decision(token_a) == "release"
            h.llm.tokens = ("Fresh B reply. ", "B finishes once. ")
            await h.turn("B"); await cleanup_parked.wait()
            await until(lambda: len(fake.calls) == 2 and hold.decision(next(reversed(hold.routes))) == "release")
            token_b = next(reversed(hold.routes))
            release_push.set(); await b_wake_processed.wait(); await late_push_returned.wait()
            await asyncio.sleep(.04)
            assert hold.decision(token_a) == "release"  # Cancellation never rewrites the sticky decision.
            assert not h.services["angel"].starts and not h.services["angel"].owners
            assert not h.services["angel"].speaks and not h.transport.client.audio
            assert not any(m.get("role") == "assistant" for m in h.control.context.get_messages())
            release_cleanup.set(); await auto_reply(h)
            assert h.services["angel"].text_responses == [2, 2]
            assert [text for text, _ in h.services["angel"].speaks] == ["Fresh B reply. ", "B finishes once. "]
            assert len(fake.calls) == len(h.llm.calls) == 2
            assert fake.cancelled == 0 and fake.active == 0
            assert not hold.held and not h.llm._queued_route_contexts and not h.llm._cancelled_route_reads
        finally:
            release_push.set(); release_cleanup.set()


async def test_auto_fix2_ordinary_supersession_still_releases_valid_output():
    async def pending_read(state):
        await asyncio.Event().wait()
    fake = FakeAddressee(pending_read, "either")
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello(); await h.turn("Still a valid completed draft")
        hold = h.control.hold
        await until(lambda: len(hold.held) == 4)
        token_a = next(iter(hold.routes))
        # An ordinary inference supersedes routing ownership without any user
        # start/interruption or cancellation of the already-completed LLM run.
        await h.control.user_aggregator.push_context_frame()
        await auto_reply(h, count=2)
        assert hold.decision(token_a) == "superseded"
        assert hold.outputs[token_a].cancelled is False
        assert h.services["angel"].text_responses == [1, 1, 2, 2]
        assert len(h.llm.calls) == len(fake.calls) == 2 and fake.cancelled == 1
        assert not hold.held


async def test_auto_fix2_old_completion_cannot_finalize_newer_tts_context():
    from pipecat.frames.frames import LLMFullResponseEndFrame
    async with pipeline(auto_switch=True) as h:
        await h.hello()
        hold, angel = h.control.hold, h.services["angel"]
        h.llm.remainder.clear()
        await h.turn("A already spoken")
        await until(lambda: angel.speaks)
        token_a = next(iter(hold.routes))
        owner_a = hold.outputs[token_a]
        assert owner_a.tts_owner is not None
        angel.auto_complete = False  # Complete B explicitly; no delayed fake A provider event.
        angel.returned.clear()
        flushes = []
        send = angel._transport_send_json
        async def count_flush(message):
            if message["type"] == "Flush":
                flushes.append(message)
            await send(message)
        angel._transport_send_json = count_flush
        await h.turn("B remains streaming")
        await until(lambda: 2 in angel.text_responses)
        await angel.returned.wait()
        assert owner_a.cancelled
        owner_b = angel.response_owner
        assert owner_b is not None and owner_b != owner_a.tts_owner
        count = len(flushes)
        late = LLMFullResponseEndFrame()
        late.metadata.update({"duo-route": token_a, "duo-output": owner_a})
        await hold.queue_frame(late)
        await asyncio.sleep(.04)
        assert angel.response_owner == owner_b and len(flushes) == count
        h.llm.remainder.set()
        await until(lambda: len(flushes) > count)
        await angel.finish()
        await auto_reply(h, count=2)
        assert angel.text_responses == [1, 2, 2]
        assert len(h.addressee.calls) == len(h.llm.calls) == 2


async def test_auto_fix2_wake_discards_cancelled_end_even_for_admitted_tts():
    from pipecat.frames.frames import LLMFullResponseEndFrame
    async with pipeline(auto_switch=True) as h:
        await h.hello()
        h.llm.remainder.clear()
        await h.turn("A already admitted")
        angel, hold = h.services["angel"], h.control.hold
        await angel.returned.wait()
        token = next(iter(hold.routes))
        output = hold.outputs[token]
        assert output.tts_started and output.tts_owner == angel.response_owner
        h.control.context_cancelled(token)
        flushes = []
        send = angel._transport_send_json
        async def count_flush(message):
            if message["type"] == "Flush":
                flushes.append(message)
            await send(message)
        angel._transport_send_json = count_flush
        end = LLMFullResponseEndFrame()
        end.metadata.update({"duo-route": token, "duo-output": output})
        hold.held.append((end, DOWN))
        await hold.queue_frame(HoldWake(token=token))
        await until(lambda: not hold.held)
        await asyncio.sleep(.04)
        assert not flushes and angel.response_owner == output.tts_owner
        # A real interruption can still settle the text already spoken by A;
        # none of its retained frames were released by the ordinary wake.
        await h.turn("B is newer")
        await until(lambda: len(h.llm.calls) == 2)
        h.llm.remainder.set()
        await auto_reply(h, count=2)
        assert angel.text_responses == [1, 2, 2]
        assert not hold.held


async def test_auto_interrupted_sentence_plus_partial_settles_as_it_does_without_routing():
    """Sol, bot qualification 1: an admitted reply's End still reaches its voice.

    A barge-in after one spoken sentence and a pending partial therefore
    settles speech and history exactly as the flag-off pipeline does.
    """
    from pipecat.services.tts_service import TextAggregationMode
    results = []
    for enabled in (False, True):
        async with pipeline(auto_switch=enabled, addressee=FakeAddressee("either", "either"),
                            tts_kwargs={"text_aggregation_mode": TextAggregationMode.SENTENCE}) as h:
            await h.hello()
            angel = h.services["angel"]
            h.llm.tokens = ("A whole first sentence. Then a partial", " that never completes. ")
            h.llm.remainder.clear()
            await h.turn("A")
            await until(lambda: angel.speaks)  # The whole sentence is with the voice; the partial waits.
            if enabled:
                output = next(iter(h.control.hold.outputs.values()))
                assert output.tts_started and not output.cancelled
            h.llm.tokens = ("Fresh B reply. ", "B ends. ")
            await h.turn("B")
            await until(lambda: len(h.llm.calls) == 2)
            if enabled:
                assert output.cancelled
            h.llm.remainder.set()
            await until(lambda: any("B ends." in m["content"] for m in h.control.context.get_messages()
                                    if m.get("role") == "assistant"))
            assert await h.worker.flush_pipeline(timeout=.5)
            results.append(([text for text, _ in angel.speaks],
                            [m["content"] for m in h.control.context.get_messages() if m.get("role") == "assistant"],
                            len(h.llm.calls)))
    assert results[0] == results[1]
    spoken, history, calls = results[1]
    # Native settlement in both modes: the End flushes the partial the voice already held.
    assert [text.strip() for text in spoken] == ["A whole first sentence.", "Then a partial",
                                                 "Fresh B reply.", "B ends."]
    assert history == ["[Angel] A whole first sentence. Then a partial", "[Angel] Fresh B reply. B ends."]
    assert calls == 2


async def test_auto_fix2_cancelled_partial_text_cannot_start_synthesis_on_end():
    from pipecat.services.tts_service import TextAggregationMode
    async with pipeline(auto_switch=True, tts_kwargs={"text_aggregation_mode": TextAggregationMode.SENTENCE}) as h:
        await h.hello()
        h.llm.tokens = ("Old A unfinished fragment", "Should never be spoken. ")
        h.llm.remainder.clear()
        await h.turn("A")
        await until(lambda: h.services["angel"].text_responses == [1])
        token_a = next(iter(h.control.hold.routes))
        output = h.control.hold.outputs[token_a]
        assert output.tts_owner is not None and not output.tts_started
        assert not h.services["angel"].speaks and not h.transport.client.audio
        h.llm.tokens = ("Fresh B reply. ", "B finishes once. ")
        await h.turn("B")
        await until(lambda: len(h.llm.calls) == 2)
        h.llm.remainder.set()
        await auto_reply(h)
        assert output.cancelled and not output.tts_started
        assert [text for text, _ in h.services["angel"].speaks] == ["Fresh B reply. ", "B finishes once. "]
        replies = [m["content"] for m in h.control.context.get_messages() if m.get("role") == "assistant"]
        assert len(replies) == 1 and "Old A" not in replies[0]


@pytest.mark.parametrize("enabled", [False, True])
async def test_auto_fix3_toggle_during_fence_suspension(enabled):
    fake = FakeAddressee("either", delay=.2)
    async with pipeline(auto_switch=enabled, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        fence = h.control.gate.fence
        observer = h.llm._setup.observer
        original = observer.on_process_frame
        parked, release = asyncio.Event(), asyncio.Event()
        once = True
        async def suspend(data):
            nonlocal once
            if once and data.processor is fence and isinstance(data.frame, LLMContextFrame):
                once = False; parked.set()
                await release.wait()
            await original(data)
        observer.on_process_frame = suspend
        try:
            await h.turn("A: what should I do")
            await parked.wait()
            await h.send("toggle")
            await until(lambda: h.control.gate.closed)
            release.set()
            assert (await h.outcome())["character"] == "devil"
            await until(lambda: any(m.get("role") == "assistant" for m in h.control.context.get_messages()))
            assert await h.worker.flush_pipeline(timeout=.5)
            assert not h.services["angel"].speaks
            assert h.services["devil"].text_responses == [1, 1]
            assert [auto_user(c) for c in h.llm.calls] == ["A: what should I do"]
            assert h.llm.calls[0][0] == character_instructions("devil")
            assert not fence.held and not h.control.gate.closed
            if enabled:
                token = next(iter(h.control.hold.routes))
                assert h.control.hold.outputs[token].cancelled
                # Device preemption decided release before the gate closed;
                # output cancellation must preserve that terminal decision.
                assert h.control.hold.decision(token) == "release"
                assert not h.control.hold.held and h.control.route_read is None
        finally:
            release.set()


async def test_auto_fix3_claim_during_inflight_bargein_answers_only_b(join_bound):
    release_a = asyncio.Event()
    async def result_a(state):
        await release_a.wait()
        return ChoiceResult(choice="devil", probabilities={"devil": .99}, confidence=.99)
    fake = FakeAddressee(result_a, "either")
    async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
        await h.hello()
        observer = h.llm._setup.observer
        original = observer.on_push_frame
        fence = h.control.gate.fence
        parked, release = asyncio.Event(), asyncio.Event()
        armed = False
        async def suspend(data):
            nonlocal armed
            if armed and data.source is fence and isinstance(data.frame, InterruptionFrame):
                armed = False; parked.set()
                await release.wait()
            await original(data)
        observer.on_push_frame = suspend
        h.llm.generate.clear()
        try:
            await h.turn("A: Devil, answer me")
            await fake.started.wait()
            await until(lambda: h.llm._route is not None)
            token = next(iter(h.control.hold.routes))
            armed = True
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await parked.wait()
            assert h.control.user_speech_epoch == 2
            release_a.set()
            await until(lambda: h.control.hold.decision(token) == "quarantine")
            release.set(); h.llm.generate.set()
            await h.worker.queue_frames([
                TranscriptionFrame(text="B: something else", user_id="u", timestamp="now", finalized=True),
                ProposedUserStoppedSpeakingFrame()])
            await auto_reply(h, "devil")
            assert [auto_user(c) for c in h.llm.calls] == ["A: Devil, answer me", "B: something else"]
            assert h.services["devil"].text_responses == [2, 2]
            assert not h.services["angel"].speaks
            replies = [m for m in h.control.context.get_messages() if m.get("role") == "assistant"]
            assert len(replies) == 1 and replies[0]["content"].startswith("[Devil]")
            assert len(fake.calls) == 2 and fake.cancelled == 0
            assert len([s for s in h.states() if s.get("reason") == "addressed"]) == 1
            assert any(k == "route-rerun-withdrawn" for k, _, _ in h.events)
            assert not fence.held and not h.control.hold.held
            assert not h.llm._queued_route_contexts and not h.llm._cancelled_route_reads
        finally:
            release.set(); release_a.set(); h.llm.generate.set()


@pytest.mark.parametrize("rollback", [False, True])
async def test_auto_fix3_bargein_during_mutate_withdraws_old_rerun(rollback):
    async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", "either", delay=.04)) as h:
        await h.hello()
        original = h.control._mutate
        parked, release = asyncio.Event(), asyncio.Event()
        once = True
        async def suspend(*args, **kwargs):
            nonlocal once
            if once:
                once = False; parked.set()
                await release.wait()
            return await original(*args, **kwargs)
        h.control._mutate = suspend
        if rollback:
            h.stt.sabotage = ManuallySwitchServiceFrame
        try:
            await h.turn("A: Devil, answer me")
            await parked.wait()
            assert h.control.user_speech_epoch == 1
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await until(lambda: any(isinstance(f, ProposedUserStartedSpeakingFrame)
                                    for f, _ in h.control.gate.held))
            assert h.control.user_speech_epoch == 2
            release.set()
            await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
            assert await h.worker.flush_pipeline(timeout=.5)
            assert h.control.user_speech_epoch == 2  # Replaying the held start does not count again.
            assert not any(s.speaks for s in h.services.values())
            assert len(h.llm.calls) == 1  # The draft ran; no A rerun after B's start alone.
            assert not any(m.get("role") == "assistant" for m in h.control.context.get_messages())
            assert any(k == "route-rerun-withdrawn" for k, _, _ in h.events)
            await h.worker.queue_frames([
                TranscriptionFrame(text="B is the new turn", user_id="u", timestamp="now", finalized=True),
                ProposedUserStoppedSpeakingFrame()])
            character = "angel" if rollback else "devil"
            await auto_reply(h, character)
            assert [auto_user(c) for c in h.llm.calls] == ["A: Devil, answer me", "B is the new turn"]
            assert h.llm.calls[-1][0] == character_instructions(character)
            assert h.services[character].text_responses == [2, 2]
            assert len(h.addressee.calls) == 2
            assert not h.control.gate.fence.held and not h.control.hold.held
        finally:
            release.set()


@pytest.mark.parametrize("rollback", [False, True])
async def test_auto_fix3_bargein_after_rerun_queued_withdraws_at_llm(rollback):
    async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", "either", delay=.04)) as h:
        await h.hello()
        fence = h.control.gate.fence
        original = fence.push_frame
        parked, release = asyncio.Event(), asyncio.Event()
        async def suspend(frame, direction=DOWN):
            if isinstance(frame, AutoRerunFrame):
                assert frame.speech_epoch == h.control.user_speech_epoch == 1
                parked.set()
                await release.wait()
            await original(frame, direction)
        fence.push_frame = suspend
        if rollback:
            h.stt.sabotage = ManuallySwitchServiceFrame
        try:
            await h.turn("A: Devil, answer me")
            await parked.wait()
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await until(lambda: any(isinstance(f, ProposedUserStartedSpeakingFrame)
                                    for f, _ in h.control.gate.held))
            release.set()
            await until(lambda: h.control.phase == "idle" and not h.control.gate.closed)
            assert await h.worker.flush_pipeline(timeout=.5)
            assert not any(s.speaks for s in h.services.values()) and len(h.llm.calls) == 1
            assert any(k == "route-rerun-withdrawn" for k, _, _ in h.events)
            await h.worker.queue_frames([
                TranscriptionFrame(text="B owns the reply", user_id="u", timestamp="now", finalized=True),
                ProposedUserStoppedSpeakingFrame()])
            character = "angel" if rollback else "devil"
            await auto_reply(h, character)
            assert [auto_user(c) for c in h.llm.calls] == ["A: Devil, answer me", "B owns the reply"]
            assert h.services[character].text_responses == [2, 2]
            assert not fence.held and not h.control.hold.held and h.control.route_read is None
        finally:
            release.set()


@pytest.mark.parametrize("rollback", [False, True])
async def test_auto_fix3_own_claim_during_fence_await_keeps_one_reply(rollback):
    async with pipeline(auto_switch=True, addressee=FakeAddressee("devil", delay=.04)) as h:
        await h.hello()
        observer = h.llm._setup.observer
        observe = observer.on_process_frame
        parked, release = asyncio.Event(), asyncio.Event()
        voice_parked, release_voice = asyncio.Event(), asyncio.Event()
        once = True
        async def suspend(data):
            nonlocal once
            if once and data.processor is h.control.gate.fence and isinstance(data.frame, LLMContextFrame):
                once = False; parked.set()
                await release.wait()
            await observe(data)
        observer.on_process_frame = suspend
        voice = h.control._voice
        async def suspend_voice(*args, **kwargs):
            voice_parked.set()
            await release_voice.wait()
            return await voice(*args, **kwargs)
        h.control._voice = suspend_voice
        if rollback:
            h.stt.sabotage = ManuallySwitchServiceFrame
        try:
            await h.turn("A: Devil, answer me")
            await parked.wait(); await voice_parked.wait()
            assert h.control.gate.closed
            release.set()
            assert await h.worker.flush_pipeline(timeout=.5)
            assert not h.llm.calls and not h.control.gate.fence.held
            release_voice.set()
            character = "angel" if rollback else "devil"
            await auto_reply(h, character)
            assert len(h.llm.calls) == len(h.addressee.calls) == 1
            assert h.services[character].text_responses == [1, 1]
        finally:
            release.set(); release_voice.set()


@pytest.mark.parametrize("boundary", ["llm", "fence"])
async def test_auto_fix3_cancel_join_propagation_timing(boundary):
    timings = []
    for delay in (0, .08):
        async def pending(state):
            await asyncio.Event().wait()
        fake = FakeAddressee(pending, "either")
        fake.cancel_delay = delay
        async with pipeline(auto_switch=True, addressee=fake, config={"route_budget_s": 1}) as h:
            await h.hello()
            h.llm.generate.clear()
            if boundary == "fence":
                observer = h.llm._setup.observer
                original = observer.on_process_frame
                parked = asyncio.Event()
                once = True
                async def suspend(data):
                    nonlocal once
                    if once and data.processor is h.control.gate.fence and isinstance(data.frame, LLMContextFrame):
                        once = False; parked.set()
                        await asyncio.Event().wait()
                    await original(data)
                observer.on_process_frame = suspend
            await h.turn("A cancellation timing")
            await fake.started.wait()
            if boundary == "fence":
                await parked.wait()
            else:
                await until(lambda: h.llm._route is not None)
            before = len(h.witness.interruptions)
            started = time.monotonic()
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await until(fake.joined.is_set)
            assert await h.worker.flush_pipeline(timeout=.5)
            await until(lambda: len(h.witness.interruptions) > before)
            elapsed = (h.witness.interruptions[-1] - started) * 1000
            timings.append(elapsed)
            assert fake.cancelled == 1 and fake.active == 0
            assert not h.llm._cancelled_route_reads and not h.services["angel"].speaks
            h.llm.generate.set()
            await h.worker.queue_frames([
                TranscriptionFrame(text="B after cancellation", user_id="u", timestamp="now", finalized=True),
                ProposedUserStoppedSpeakingFrame()])
            await auto_reply(h)
            assert not h.control.hold.held
    print(f"FIX3 join timing boundary={boundary} zero_ms={timings[0]:.3f} "
          f"cancel_80ms={timings[1]:.3f} added_ms={timings[1] - timings[0]:.3f}")
    assert timings[1] >= 80 and timings[1] - timings[0] > 40


def blocked_cleanup():
    """A Jev read that never answers, and whose cancellation cleanup waits to be released.

    The cleanup also outlasts a second cancellation, such as the route
    deadline's own, the way a shielded HTTP close does.
    """
    cleaning, release = asyncio.Event(), asyncio.Event()
    async def read(state):
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
    return read, cleaning, release


async def settled_turn(h, text, fake, parked):
    """Start a turn whose run stays parked, and let the turn's own interruption reach the tail."""
    settled = len(h.witness.interruptions)
    await h.turn(text)
    await fake.started.wait()
    if parked:
        await parked.wait()
    else:
        await until(lambda: h.llm._route is not None)
    await until(lambda: len(h.witness.interruptions) > settled)


async def park_fence(h):
    """Suspend the fence on its next context, as an observer await would."""
    observer = h.llm._setup.observer
    original = observer.on_process_frame
    parked = asyncio.Event()
    once = True
    async def suspend(data):
        nonlocal once
        if once and data.processor is h.control.gate.fence and isinstance(data.frame, LLMContextFrame):
            once = False; parked.set()
            await asyncio.Event().wait()
        await original(data)
    observer.on_process_frame = suspend
    return parked


@pytest.mark.parametrize("boundary", ["llm", "fence"])
async def test_auto_bounded_join_interruption_does_not_wait_for_blocked_cleanup(boundary):
    read, cleaning, release = blocked_cleanup()
    fake = FakeAddressee(read, "either")
    async with pipeline(auto_switch=True, addressee=fake, deadline=2,
                        config={"route_budget_s": .15, "cancel_join_bound_s": 0}) as h:
        try:
            await h.hello()
            h.llm.generate.clear()
            parked = await park_fence(h) if boundary == "fence" else None
            await settled_turn(h, "A is interrupted", fake, parked)
            token = next(iter(h.control.hold.routes))
            # Two interruptions arrive while the request's cleanup cannot finish.
            # Each reaches the tail: neither waits for the cleanup.
            for frame in (ProposedUserStartedSpeakingFrame(), InterruptionWorkerFrame()):
                before = len(h.witness.interruptions)
                await h.worker.queue_frame(frame)
                await until(lambda: len(h.witness.interruptions) > before, timeout=1)
                assert cleaning.is_set() and not fake.joined.is_set() and fake.active == 1
            assert h.control.hold.decision(token) == "superseded" and h.control.hold.outputs[token].cancelled
            assert h.control.route_read[0] == token and h.control.route_read[1].cancelling() == 1
            # B is answered meanwhile. Its own read waits behind the one outstanding request,
            # so its deadline releases the reply.
            h.llm.generate.set()
            await h.worker.queue_frames([
                TranscriptionFrame(text="B after the barge-in", user_id="u", timestamp="now", finalized=True),
                ProposedUserStoppedSpeakingFrame()])
            await until(lambda: h.services["angel"].speaks)
            assert len(fake.calls) == 1 and fake.active == 1 and not fake.joined.is_set()
            assert auto_user(h.llm.calls[-1]) == "B after the barge-in"
            assert h.services["angel"].text_responses[0] == len(h.llm.calls)
            # Cooperative cleanup is still joined, by its owner.
            release.set()
            await until(fake.joined.is_set)
            await auto_reply(h)
            await until(lambda: h.control.route_read is None and fake.active == 0)
            assert not h.llm._cancelled_route_reads and h.control.route_overdue is None
            assert fake.cancelled == 1 and len(fake.calls) == 1 and not h.control.stopping
            assert not any(s.get("reason") == "addressed" for s in h.states())
            replies = [m["content"] for m in h.control.context.get_messages() if m.get("role") == "assistant"]
            assert len(replies) == 1
            await h.turn("C routes again"); await auto_reply(h, count=2)
            assert len(fake.calls) == 2
        finally:
            release.set()


@pytest.mark.parametrize("bound_ms", [0, 20])
@pytest.mark.parametrize("boundary", ["llm", "fence"])
async def test_auto_bounded_join_added_latency_follows_the_bound_not_the_cleanup(boundary, bound_ms):
    cleanup = .3
    timings = []
    for delay in (0, cleanup):
        async def pending(state):
            await asyncio.Event().wait()
        fake = FakeAddressee(pending, "either")
        fake.cancel_delay = delay
        async with pipeline(auto_switch=True, addressee=fake, deadline=2,
                            config={"route_budget_s": 1, "cancel_join_bound_s": bound_ms / 1000}) as h:
            await h.hello()
            h.llm.generate.clear()
            parked = await park_fence(h) if boundary == "fence" else None
            await settled_turn(h, "A cancellation timing", fake, parked)
            before = len(h.witness.interruptions)
            started = time.monotonic()
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await until(lambda: len(h.witness.interruptions) > before)
            timings.append((h.witness.interruptions[-1] - started) * 1000)
            if delay:
                assert not fake.joined.is_set()  # The interruption overtook the cleanup.
            await until(fake.joined.is_set)
            await until(lambda: h.control.route_read is None and fake.active == 0)
            assert fake.cancelled == 1 and not h.llm._cancelled_route_reads
            h.llm.generate.set()
            await h.worker.queue_frames([
                TranscriptionFrame(text="B after cancellation", user_id="u", timestamp="now", finalized=True),
                ProposedUserStoppedSpeakingFrame()])
            await auto_reply(h)
            assert not h.control.hold.held and not h.control.stopping
    added = timings[1] - timings[0]
    print(f"BOUNDED join timing boundary={boundary} bound_ms={bound_ms} zero_ms={timings[0]:.3f} "
          f"cleanup_{cleanup * 1000:.0f}ms={timings[1]:.3f} added_ms={added:.3f}")
    # Half the cleanup leaves room for a loaded host; the unlimited join adds all of it.
    assert added < cleanup * 1000 / 2
    if bound_ms:
        assert added >= bound_ms * .75


async def test_auto_bounded_join_overdue_cleanup_ends_the_session():
    read, cleaning, release = blocked_cleanup()
    fake = FakeAddressee(read)
    async with pipeline(auto_switch=True, addressee=fake, deadline=.3,
                        config={"route_budget_s": 1, "cancel_join_bound_s": 0}) as h:
        try:
            await h.hello()
            h.llm.generate.clear()
            await h.turn("A never finishes cancelling")
            await fake.started.wait()
            await until(lambda: h.llm._route is not None)
            token = next(iter(h.control.hold.routes))
            started = time.monotonic()
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await cleaning.wait()
            assert not h.control.stopping and h.control.route_overdue is not None
            await until(lambda: h.control.stopping, timeout=2)
            assert time.monotonic() - started >= .25
            await until(h.worker.has_finished)
            assert h.control.gate.closed
            assert [data for kind, data, _ in h.events if kind == "route-cleanup-overdue"] == [dict(token=token)]
            # The session is over, yet the request is not abandoned: its owner still waits for it.
            assert fake.active == 1 and not h.coordinating.done()
        finally:
            release.set()
            await until(lambda: fake.active == 0)


@pytest.mark.parametrize("late_interruption", [False, True])
async def test_auto_bounded_join_supervises_a_read_cancelled_by_its_own_deadline(late_interruption):
    """Sol S1: nobody retires this read; its route deadline cancels it and the cleanup never ends."""
    from types import SimpleNamespace
    read, cleaning, release = blocked_cleanup()
    fake = FakeAddressee(read)
    async with pipeline(auto_switch=True, addressee=fake, deadline=.15,
                        config={"route_budget_s": .04, "cancel_join_bound_s": 0}) as h:
        try:
            await h.hello()
            h.llm.generate.clear()
            started = time.monotonic()
            await h.turn("The read reaches its own route deadline")
            await asyncio.wait_for(cleaning.wait(), 1)
            token, request = h.control.route_read
            assert request.cancelling() == 1 and h.control.route_cancel_reason is None
            assert h.control.route_overdue is not None and not h.control.stopping
            if late_interruption:
                await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
                await until(lambda: h.control.hold.outputs[token].cancelled)
                assert request.cancelling() == 1  # Retired, not cancelled a second time.
            # Witnesses are still served while a device request waits behind the read.
            pongs = []
            h.control.liveness = SimpleNamespace(pong=pongs.append)
            h.control.enqueue(Pong({"seq": 7}))
            await until(lambda: pongs)
            await h.send(seq=2)
            await until(lambda: h.control.pending_requests == 1)
            await until(lambda: h.control.stopping, timeout=2)
            assert time.monotonic() - started >= .04 + .15 - .02
            await until(h.worker.has_finished)
            assert [data for kind, data, _ in h.events if kind == "route-cleanup-overdue"] == [dict(token=token)]
            assert h.control.gate.closed and fake.active == 1
        finally:
            release.set()
            await until(lambda: fake.active == 0)


async def test_auto_bounded_join_supervises_a_hedge_loser_when_the_read_is_cancelled(monkeypatch):
    """Sol R2 through the coordinator: the cancelled read does not finish while its duplicate is cleaning up."""
    import httpx
    from duo_addressee import Addressee
    from test_duo_addressee import response
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
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
        return response("either")
    http_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client(**kw, transport=httpx.MockTransport(handler)))
    class Measured(Addressee):
        @property
        def active(self):
            return len(self._pending)
    router = Measured("offline-placeholder", budget=.8, hedge_s=.01, join_bound_s=.1)
    async with pipeline(auto_switch=True, addressee=router, deadline=.15,
                        config={"route_budget_s": .8, "cancel_join_bound_s": .1}) as h:
        try:
            await h.hello()
            h.llm.generate.clear()
            await h.turn("A hedged read is interrupted while its loser cleans up")
            await asyncio.wait_for(cleaning.wait(), 1)
            token, read = h.control.route_read
            await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
            await until(lambda: read.cancelling())
            await asyncio.sleep(.12)  # Past the loser's 100 ms join bound.
            assert not read.done() and h.control.route_read is not None and h.control.route_overdue is not None
            await until(lambda: h.control.stopping, timeout=2)
            await until(h.worker.has_finished)
            assert [data for kind, data, _ in h.events if kind == "route-cleanup-overdue"] == [dict(token=token)]
            assert router.active == 1 and reads == 2
        finally:
            release.set()
            await until(lambda: not router._pending)
            await router.close()


@pytest.mark.parametrize("trouble", ["cancelled-during-the-join", "liveness-fails", "repeats-fail"])
async def test_auto_session_shutdown_runs_every_step_through_cancellation_and_failure(trouble):
    """Sol R3, with the real Liveness: nothing of the session outlives its shutdown."""
    from demo_liveness import Liveness
    from duo_bot import end_session, finish
    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    async def coordinator():
        try:
            await asyncio.Event().wait()
        finally:
            entered.set()
            await release.wait()
            events.append("coordinator-joined")
    control = asyncio.create_task(coordinator())
    await asyncio.sleep(0)
    async def send(data):
        events.append("ping")
    async def close_peer():
        events.append("peer-closed")
    live = Liveness(send, close_peer, period=.1)
    live.start()
    tasks = list(live.tasks)
    assert len(tasks) == 2
    if trouble == "liveness-fails":
        stop = live.stop
        async def failing_stop():
            await stop()
            raise RuntimeError("liveness summary failed")
        live.stop = failing_stop
    async def stop_repeats():
        events.append("repeats-stopped")
        if trouble == "repeats-fail":
            raise RuntimeError("repeat shutdown failed")
    rtvi = type("Rtvi", (), dict(stop_interrupt_repeats=staticmethod(stop_repeats)))
    recorder = type("Recorder", (), dict(close=staticmethod(lambda why: events.append(f"recorder-closed:{why}"))))
    ending = asyncio.create_task(finish(end_session(control, live, rtvi, recorder)))
    try:
        await entered.wait()
        if trouble == "cancelled-during-the-join":
            ending.cancel()
            await asyncio.sleep(.03)
            assert not ending.done()  # It keeps waiting for the coordinator it owns.
        release.set()
        result = (await asyncio.gather(ending, return_exceptions=True))[0]
    finally:
        release.set()
        await asyncio.gather(control, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError if trouble == "cancelled-during-the-join" else RuntimeError)
    assert all(task.done() for task in tasks) and live.tasks == []
    steps = [event for event in events if event != "ping"]
    assert steps == ["coordinator-joined", "repeats-stopped", "recorder-closed:bot-exit"]
    pings = events.count("ping")
    await asyncio.sleep(.15)
    assert events.count("ping") == pings  # No liveness sender is left running.


async def test_auto_unbounded_join_arms_no_cleanup_deadline():
    fake = FakeAddressee(delay=2); fake.cancel_delay = .05
    async with pipeline(auto_switch=True, addressee=fake) as h:
        assert h.config["cancel_join_bound_s"] is None and h.control.cancel_join_bound_s is None
        await h.hello(); await h.turn(); await fake.started.wait()
        await h.worker.queue_frame(ProposedUserStartedSpeakingFrame())
        await until(lambda: fake.cancelled == 1)
        assert h.control.route_overdue is None
        await until(fake.joined.is_set)
    for value, expected in (("0", 0), ("20", .02), ("1000", 1)):
        assert configuration({"DUO_CANCEL_JOIN_MS": value})["cancel_join_bound_s"] == expected
    assert configuration({"DUO_CANCEL_JOIN_MS": ""})["cancel_join_bound_s"] is None
    for bad in ("-1", "1001", "nan", "soon"):
        with pytest.raises(ValueError):
            configuration({"DUO_CANCEL_JOIN_MS": bad})
