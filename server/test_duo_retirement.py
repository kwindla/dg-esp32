"""Fix-1 regressions: real Flux lifecycle, fake socket boundary, no live I/O."""
import asyncio
from copy import deepcopy
import time

import pytest

from duo_prompts import character_instructions
from duo_bot import ConnectionResult, DuoServiceSwitcher, PipelineError, ResponseCompleted
from pipecat.pipeline.service_switcher import ServiceSwitcher
from test_duo_pipeline import pipeline, until, pytest_generate_tests, existing_stay_mode, context_style


async def retired(h, service):
    await until(lambda: service in h.control.retirements and h.control.retirements[service].done())
    assert h.control.retirements[service].result()
    assert service.connection_ready and service.is_usable


async def test_production_receiver_loss_after_partial_audio_reverts_without_completion():
    async with pipeline(real_lifecycle=True, deadline=.3) as h:
        await h.hello()
        assert all(not voice._reconnect_on_error for voice in h.services.values())
        assert len(h.services["angel"].sockets) == 1  # hello never reconnects
        await h.send()
        committed = await h.outcome()
        angel, devil = h.services["angel"], h.services["devil"]
        assert len(devil.sockets) == 1  # untouched, preconnected target
        await retired(h, angel)
        socket = devil.sockets[-1]
        socket.auto_complete = False
        devil._stop_frame_timeout_s = .06
        await h.turn()
        await until(lambda: h.control.state.verification and h.control.state.verification.response
                    and h.transport.client.audio)
        context = h.control.state.verification.response
        socket.incoming.put_nowait(OSError("offline socket lost after partial audio"))
        await until(lambda: any(state.get("reason") == "reverted" for state in h.states()))
        await asyncio.sleep(.09)  # beyond the old context timeout, with no provider completion
        assert h.control.state.verification is None
        assert not devil.audio_context_available(context)
        assert h.control.state.character == "angel" and not h.control.gate.closed
        assert h.control.state.sessions["boot"].outcomes[2][1] == committed
        assert not h.worker.has_finished()


async def test_failed_outgoing_retirement_cannot_be_acknowledged_as_rollback_voice():
    async with pipeline(real_lifecycle=True, fault="tts-fail-next", deadline=.2) as h:
        await h.hello()
        angel = h.services["angel"]
        h.control.retirement_backoff_secs = 1
        async def reject_handshake(*args, **kwargs):
            raise OSError("offline retired angel handshake rejected")
        angel._websocket_connect = reject_handshake
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        await until(lambda: angel in h.control.failed_voices)
        assert not angel.connection_ready and not angel.is_usable
        before = len(h.states())
        await h.turn()
        await asyncio.wait_for(asyncio.shield(h.running), 1)
        assert h.control.gate.closed and h.worker.has_finished()
        assert h.control.state.character == "devil" and h.control.state.rev == 2
        assert not any(state["character"] == "angel" for state in h.states()[before:])
        assert not any(state.get("reason") == "reverted" for state in h.states())


async def test_failed_retirement_preflight_voice_error_retries_with_backoff_and_replays_error():
    async with pipeline(real_lifecycle=True, deadline=.3) as h:
        await h.hello()
        angel = h.services["angel"]
        original = angel._websocket_connect
        h.control.retirement_backoff_secs = .08
        attempts = []
        async def fail_twice(*args, **kwargs):
            attempts.append(time.monotonic())
            if len(attempts) <= 2:
                raise OSError("offline retirement handshake rejected")
            return await original(*args, **kwargs)
        angel._websocket_connect = fail_twice
        await h.send()
        await h.outcome()
        await until(lambda: angel in h.control.failed_voices)
        interruptions = len(h.witness.interruptions)
        await h.send()
        error = await h.outcome()
        assert error["op"] == "error" and error["reason"] == "voice"
        assert error["character"] == "devil" and error["rev"] == 2
        assert len(h.witness.interruptions) == interruptions
        await until(lambda: len(attempts) == 3 and angel not in h.control.failed_voices)
        await retired(h, angel)
        assert attempts[1] - attempts[0] >= .07
        assert attempts[2] - attempts[1] >= .14
        before = len(h.states())
        await h.send(seq=3)
        await until(lambda: len(h.states()) > before)
        assert h.states()[-1] == error
        await h.send()
        assert (await h.outcome())["character"] == "angel"


async def test_fast_toggle_back_waits_for_stalled_background_connect_then_rolls_back_without_connect():
    async with pipeline(real_lifecycle=True, deadline=.12) as h:
        await h.hello()
        angel, devil = h.services["angel"], h.services["devil"]
        original = angel._websocket_connect
        entered, release = asyncio.Event(), asyncio.Event()
        async def stalled(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        angel._websocket_connect = stalled
        await h.send()
        assert (await h.outcome())["character"] == "devil"
        await entered.wait()
        assert not h.control.retirements[angel].done()
        before = len(devil.sockets)
        await h.send()
        result = await h.outcome()
        assert result["op"] == "error" and result["reason"] == "voice"
        assert result["character"] == "devil" and result["rev"] == 2
        assert len(devil.sockets) == before and not h.worker.has_finished()
        assert not h.control.retirements[angel].done()  # transaction did not cancel the background job
        release.set()
        await retired(h, angel)
        await h.send()
        assert (await h.outcome())["character"] == "angel"


async def test_rollback_wait_for_stalled_retirement_ends_pipeline_without_another_handshake():
    async with pipeline(real_lifecycle=True, fault="tts-fail-next", deadline=.12) as h:
        await h.hello()
        angel = h.services["angel"]
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []
        async def stalled(*args, **kwargs):
            attempts.append(time.monotonic())
            entered.set()
            await release.wait()
        angel._websocket_connect = stalled
        await h.send()
        await h.outcome()
        await entered.wait()
        await h.turn()
        await asyncio.wait_for(asyncio.shield(h.running), 1)
        assert len(attempts) == 1
        assert h.control.gate.closed and h.worker.has_finished()
        assert not any(state.get("reason") == "reverted" for state in h.states())
        assert h.control.state.rev == 2


@pytest.mark.parametrize("reverting", [False, True])
@pytest.mark.parametrize("boundary", ["send", "release"])
async def test_absolute_deadline_ends_hung_publication_or_release_without_caller_cancellation(reverting, boundary):
    async with pipeline(real_lifecycle=True, fault="tts-fail-next" if reverting else "", deadline=.12) as h:
        await h.hello()
        if reverting:
            await h.send()
            await h.outcome()
            await retired(h, h.services["angel"])
        entered, never = asyncio.Event(), asyncio.Event()
        cache_at_entry = []
        async def hang():
            cache_at_entry.append(deepcopy(h.control.state.sessions["boot"].outcomes[2][1]))
            entered.set()
            await never.wait()
        if boundary == "send":
            original_send = h.worker.rtvi.send_server_message
            async def send(result):
                if not reverting or result.get("reason") == "reverted":
                    await hang()
                await original_send(result)
            h.worker.rtvi.send_server_message = send
        else:
            h.control.gate.reopen = hang
        if reverting:
            await h.turn()
        else:
            await h.send()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(asyncio.shield(h.running), 1)
        assert h.control.gate.closed and h.worker.has_finished()
        assert h.control.state.sessions["boot"].outcomes[2][1] == cache_at_entry[0]
        assert cache_at_entry[0]["character"] == "devil" and cache_at_entry[0]["rev"] == 2
        assert h.control.state.rev == (3 if reverting else 2)
        assert not never.is_set()


async def test_real_adapter_request_preserves_shared_history_note_and_leading_persona():
    async with pipeline() as h:
        await h.hello()
        await h.turn()
        await until(lambda: any(message["role"] == "assistant" for message in h.control.context.get_messages()))
        await h.send()
        await h.outcome()
        adapter = h.llm.get_llm_adapter()
        invocation = await adapter.get_llm_invocation_params(
            h.control.context, system_instruction=h.llm.settings.system_instruction,
            convert_developer_to_user=not h.llm.supports_developer_role)
        request = h.llm.build_chat_completion_params(invocation)
        messages = request["messages"]
        assert messages[0] == dict(role="system", content=character_instructions("devil"))
        assistant_index = next(i for i, message in enumerate(messages)
                               if message["role"] == "assistant" and message["content"].startswith("[Angel]"))
        assert any(message["role"] == "system" and message["content"].startswith("Silent context:")
                   for message in messages[assistant_index + 1:])
        assert request["temperature"] == 0 and request["model"] == h.control.llm_invariants["model"]
        assert request["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


async def test_phase_timings_are_recorded_for_interrupt_retirement_mutation_and_publish():
    async with pipeline(real_lifecycle=True) as h:
        await h.hello()
        await h.send()
        await h.outcome()
        timings = [data for kind, data, _ in h.events if kind == "phase-timing"]
        assert {data["phase"] for data in timings} == {"interrupt", "retirement_wait", "mutation", "publish"}
        assert all(data["elapsed_ms"] >= 0 and data["operation"] and data["conn"] for data in timings)
        operations = {data["operation"] for data in timings}
        assert len(operations) == 2
        for operation in operations:
            assert len([data for data in timings if data["operation"] == operation]) == 4
        assert {data["seq"] for data in timings} == {1, 2}
        assert all(data["session"] == "boot" and data["reverting"] is False for data in timings)


async def test_idle_active_voice_failure_recovers_in_background_with_new_turn_fenced():
    async with pipeline(real_lifecycle=True, deadline=.4) as h:
        await h.hello()
        angel = h.services["angel"]
        original = angel._websocket_connect
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        angel._websocket_connect = delayed
        before = list(h.states())
        angel.sockets[-1].incoming.put_nowait(OSError("offline idle connection lost"))
        await entered.wait()
        assert h.control.gate.closed and not angel.is_usable
        await h.turn("Wait for the idle voice recovery")
        await until(lambda: h.control.gate.held)
        assert not h.llm.calls
        release.set()
        await until(lambda: angel.is_usable and not h.control.gate.closed)
        await until(lambda: h.llm.calls)
        assert h.states() == before and h.control.state.character == "angel"


async def test_stock_parallel_pipeline_drains_the_flush_probe():
    # Pipecat 1.12 dropped the returning leg of the worker's flush probe at the
    # switcher, and the duo switcher carried a repair. Since 1.14 each leg is a
    # frame of its own, so the repair is gone and the stock push must drain.
    assert DuoServiceSwitcher._parallel_push_frame is ServiceSwitcher._parallel_push_frame
    async with pipeline(real_lifecycle=True) as h:
        assert await h.worker.flush_pipeline(timeout=.5)


async def test_receiver_error_queued_after_completion_recovers_idle_voice_without_reverting():
    import json
    async with pipeline(real_lifecycle=True, deadline=.3) as h:
        await h.hello()
        await h.send()
        await h.outcome()
        devil = h.services["devil"]
        socket = devil.sockets[-1]
        socket.auto_complete = False
        await h.turn()
        await until(lambda: h.control.state.verification and h.control.state.verification.response)
        held = []
        original_enqueue = h.control.enqueue
        def hold_completed_and_error(event):
            if isinstance(event, (ResponseCompleted, PipelineError)):
                held.append(event)
            else:
                original_enqueue(event)
        h.control.enqueue = hold_completed_and_error
        socket.incoming.put_nowait(json.dumps(dict(type="SpeechMetadata")))
        await until(lambda: any(isinstance(event, ResponseCompleted) for event in held))
        socket.incoming.put_nowait(OSError("offline connection lost just after completion"))
        await until(lambda: any(isinstance(event, PipelineError) for event in held))
        # The error is stamped while that reply still owns the adapter, but
        # completion is ahead of it in the coordinator queue. It is an idle
        # connection failure after completion is processed, not a stale error.
        error = next(event.frame for event in held if isinstance(event, PipelineError))
        assert error.metadata["duo-owner"] == devil.response_owner
        h.control.enqueue = original_enqueue
        for event in held:
            original_enqueue(event)
        await until(lambda: len(devil.sockets) == 2 and devil.connection_ready and not h.control.gate.closed)
        assert h.control.state.verification is None
        assert h.control.state.rev == 2 and h.control.state.character == "devil"
        assert not any(state.get("reason") == "reverted" for state in h.states())


async def test_non_armed_reply_socket_loss_preserves_session_history_and_next_turn():
    async with pipeline(real_lifecycle=True, deadline=.5) as h:
        await h.hello()
        angel = h.services['angel']
        await h.turn('First completed question')
        await until(lambda: any(m['role'] == 'assistant' for m in h.control.context.get_messages())
                    and angel.response_owner is None)
        old_history = deepcopy([m for m in h.control.context.get_messages() if m['role'] == 'assistant'])
        assert h.control.state.verification is None
        conn, rev, states = h.control.state.conn, h.control.state.rev, list(h.states())
        old_socket, old_receiver = angel.sockets[-1], angel._receive_task
        old_socket.auto_complete = False
        before_audio = len(h.transport.client.audio)
        await h.turn('Second reply fails halfway')
        await until(lambda: angel.response_owner is not None and len(h.transport.client.audio) > before_audio)
        assert h.control.state.verification is None
        entered, release = asyncio.Event(), asyncio.Event()
        original = angel._websocket_connect
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        angel._websocket_connect = delayed
        old_socket.incoming.put_nowait(OSError('offline ordinary reply socket lost'))
        await asyncio.wait_for(entered.wait(), 1)
        assert h.control.gate.closed and not angel.is_usable and old_receiver.done()
        assert not h.worker.has_finished() and not h.control.stopping
        await h.turn('Third question waits for recovery')
        await until(lambda: h.control.gate.held)
        assert len(h.llm.calls) == 2
        release.set()
        await until(lambda: len(h.llm.calls) == 3 and angel.is_usable and not h.control.gate.closed)
        await until(lambda: angel.response_owner is None and
                    len([m for m in h.control.context.get_messages() if m['role'] == 'assistant']) >= 2)
        assert 'Third question' in str(h.llm.calls[-1][1])
        assert all(m in h.control.context.get_messages() for m in old_history)
        assert any(m['role'] == 'user' and 'Second reply' in m['content'] for m in h.control.context.get_messages())
        assert h.control.state.conn == conn and h.control.state.rev == rev and h.states() == states
        assert len(angel.sockets) == 2 and not h.worker.has_finished()
        assert any(m['content'].startswith('[Angel]') for m in h.control.context.get_messages() if m['role'] == 'assistant')


@pytest.mark.parametrize('failure', ['socket-stall', 'missing-event'])
async def test_initial_setup_uses_retirement_budget_and_removes_handlers_and_socket(failure):
    from types import SimpleNamespace
    from pipecat.clocks.system_clock import SystemClock
    from pipecat.processors.frame_processor import FrameProcessorSetup
    from pipecat.utils.asyncio.task_manager import TaskManager
    from test_duo_pipeline import SocketBoundaryTTS
    before = asyncio.all_tasks()
    service = SocketBoundaryTTS('angel')
    service.coordinator = SimpleNamespace(retirement_timeout_secs=.04, retirement_retries={}, retirements={},
                                          switcher=SimpleNamespace(strategy=SimpleNamespace(active_service=service)))
    def enqueue(event):
        if isinstance(event, ConnectionResult):event.attempt.set_result(event.success)
    service.coordinator.enqueue = enqueue
    never = asyncio.Event()
    if failure == 'socket-stall':
        async def stalled(*args, **kwargs):await never.wait()
        service._websocket_connect = stalled
    else:
        original = service._call_event_handler
        async def silent(event, *args):
            if event != 'on_connected':await original(event, *args)
        service._call_event_handler = silent
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(loop=asyncio.get_running_loop()),
                                                    pipeline_worker=None))
        assert .03 <= time.monotonic() - started < .4
        assert service.connecting is None and not service.connection_ready and not service.is_usable
        assert service._receive_task is None
        assert all(socket.state.name == 'CLOSED' for socket in service.sockets)
        assert not service._event_handlers['on_connected'].handlers
        assert not service._event_handlers['on_connection_error'].handlers
    finally:
        await service.cleanup()
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize('method', ['stop', 'cancel'])
@pytest.mark.parametrize('phase', ['connect', 'retry'])
async def test_tts_shutdown_cancels_retirement_before_base_disconnect_and_no_socket_opens_late(method, phase):
    from pipecat.frames.frames import EndFrame, CancelFrame
    before = asyncio.all_tasks()
    async with pipeline(real_lifecycle=True) as h:
        await h.hello()
        angel = h.services['angel']
        original = angel._websocket_connect
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        attempts = []
        h.control.retirement_backoff_secs = .04
        async def delayed(*args, **kwargs):
            attempts.append(time.monotonic())
            entered.set()
            if phase == 'retry':raise OSError('offline retry pending at shutdown')
            try:await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await original(*args, **kwargs)
        angel._websocket_connect = delayed
        await h.send()
        await h.outcome()
        await asyncio.wait_for(entered.wait(), 1)
        if phase == 'retry':await until(lambda: any(kind == 'voice-retry' for kind, _, _ in h.events))
        assert not h.control.stopping
        await getattr(angel, method)(EndFrame() if method == 'stop' else CancelFrame())
        assert angel.connection_stopping and not angel.connection_ready and not angel.is_usable
        assert h.control.retirements[angel].done() and h.control.retirement_retries[angel].done()
        assert angel.connecting is None and angel._receive_task is None
        assert not angel._event_handlers['on_connected'].handlers
        assert not angel._event_handlers['on_connection_error'].handlers
        if phase == 'connect':assert cancelled.is_set()
        release.set()
        h.control._retire_background(angel)  # a late coordinator event cannot restart it
        assert await angel.retire() is False
        await asyncio.sleep(.07)
        assert len(attempts) == 1 and len(angel.sockets) == 1
        assert all(socket.state.name == 'CLOSED' for socket in angel.sockets)
    assert all(task.done() for task in [*h.control.retirements.values(), *h.control.retirement_retries.values()])
    assert all(service.connecting is None for service in h.services.values())
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize('failure', ['unusable', 'timeout'])
async def test_hello_adoption_failure_returns_current_state_then_resync_and_toggle_work(failure):
    from duo_prompts import character_instructions
    async with pipeline(real_lifecycle=True, deadline=.1) as h:
        target = h.services['devil']
        if failure == 'unusable':
            await target.set_usable(False)
        else:
            h.llm.setting_delay = .13
        result = await h.hello(have='devil')
        assert result['op'] == 'state' and result['reason'] == 'adoption-failed'
        assert result['character'] == 'angel' and result['rev'] == 0 and h.control.state.adopted
        assert h.control.switcher.strategy.active_service is h.services['angel']
        assert h.llm.settings.system_instruction == character_instructions('angel')
        assert not h.control.gate.closed and not h.worker.has_finished()
        if failure == 'unusable':await target.set_usable(True)
        before = len(h.states())
        await h.send('hello', have='devil', seq=1)
        await until(lambda: len(h.states()) > before)
        assert h.states()[-1] == result  # recovered target cannot change cached failure
        await h.send('hello', have='devil')
        resync = await h.outcome()
        assert resync['character'] == 'angel' and resync['rev'] == 0 and 'reason' not in resync
        # This last switch is healthy; only the adoption above tests the short deadline.
        h.control.deadline_secs = .5
        await h.send()
        switched = await h.outcome()
        assert switched['op'] == 'state' and switched['character'] == 'devil' and switched['rev'] == 1


@pytest.mark.parametrize('failure', ['timeout', 'rejected-handshake'])
async def test_nonstarting_voice_setup_failure_preserves_start_and_retries_until_toggle_can_succeed(failure):
    from test_duo_pipeline import Harness
    before = asyncio.all_tasks()
    h = Harness(real_lifecycle=True)
    h.control.retirement_timeout_secs = .1
    h.control.retirement_backoff_secs = .02
    angel, devil = h.services['angel'], h.services['devil']
    original = devil._websocket_connect
    waiting, release, timed_out = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []
    async def connect(*args, **kwargs):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            assert not devil.initial_setup_complete
            if failure == 'rejected-handshake':raise OSError('offline initial devil handshake rejected')
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                timed_out.set()
                raise
        assert devil.initial_setup_complete
        waiting.set()
        await release.wait()
        return await original(*args, **kwargs)
    devil._websocket_connect = connect
    try:
        await asyncio.wait_for(h.start(), 1)
        await asyncio.wait_for(waiting.wait(), 1)
        assert devil.initial_setup_complete and not devil.is_usable and not devil.connection_ready
        assert devil in h.control.failed_voices and len(angel.sockets) == 1
        if failure == 'timeout':assert timed_out.is_set()
        assert not h.worker.has_finished() and not h.control.gate.closed
        assert (await h.hello())['character'] == 'angel'
        await h.turn('The starting voice still answers')
        await until(lambda: any(m['role'] == 'assistant' for m in h.control.context.get_messages()))
        assert h.transport.client.audio
        history = deepcopy(h.control.context.get_messages())
        await h.send()
        result = await h.outcome()
        assert result['op'] == 'error' and result['reason'] == 'voice'
        assert result['character'] == 'angel' and result['rev'] == 1
        assert not h.worker.has_finished() and not h.control.gate.closed
        release.set()
        await until(lambda: devil.connection_ready and devil.is_usable)
        await retired(h, devil)
        assert devil not in h.control.failed_voices
        count = len(h.states())
        await h.send(seq=2)
        await until(lambda: len(h.states()) > count)
        assert h.states()[-1] == result  # the old failure remains immutable after recovery
        await h.send()
        switched = await h.outcome()
        assert switched['op'] == 'state' and switched['character'] == 'devil' and switched['rev'] == 2
        assert len(angel.sockets) >= 1 and all(m in h.control.context.get_messages() for m in history)
    finally:
        await h.close()
    assert all(t.done() for t in [*h.control.retirements.values(), *h.control.retirement_retries.values()])
    assert all(service.connecting is None for service in h.services.values())
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize('failure', ['timeout', 'rejected-handshake'])
async def test_starting_voice_setup_failure_still_prevents_pipeline_start(failure):
    from test_duo_pipeline import Harness
    before = asyncio.all_tasks()
    h = Harness(real_lifecycle=True)
    h.control.retirement_timeout_secs = .04
    angel = h.services['angel']
    cancelled = asyncio.Event()
    async def stalled(*args, **kwargs):
        if failure == 'rejected-handshake':raise OSError('offline starting voice handshake rejected')
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    angel._websocket_connect = stalled
    starting = asyncio.create_task(h.start())
    try:
        await until(lambda: h.worker.has_finished(), 1)
        await asyncio.wait_for(asyncio.shield(h.running), 1)
        if failure == 'timeout':assert cancelled.is_set()
        assert not angel.initial_setup_complete and h.control.gate.closed and h.control.stopping
        assert not angel.is_usable and not angel.connection_ready and angel.connecting is None
        assert not h.control.retirements and not h.control.retirement_retries
        assert not any(message.get('type') == 'bot-ready' for message in h.transport.client.messages)
        assert not h.llm.calls and not h.states()
    finally:
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        await h.close()
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - before)
