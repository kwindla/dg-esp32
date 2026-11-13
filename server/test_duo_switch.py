"""Shared wire tables, retry/resync and immutable response ownership."""
import pytest

from dataclasses import dataclass
from duo_switch import Request, SwitchState


@dataclass
class DeviceView:
    """Executable device-side protocol oracle for the shared protocol tables."""
    session: str
    character: str = "angel"
    generation: int = 0
    conn: str | None = None
    rev: int = -1
    seq: int = 0
    pending: Request | None = None
    swaps: int = 0

    def request(self, op):
        self.seq += 1
        self.pending = Request(op, self.session, self.seq, self.character)
        return self.pending

    def connect(self):
        self.generation += 1
        self.conn, self.rev, self.pending = None, -1, None
        return self.request("hello")

    def receive(self, outcome, generation):
        if generation != self.generation or outcome["session"] != self.session:
            return False
        pending = self.pending
        if self.conn is None:
            if not pending or pending.op != "hello" or outcome["seq"] != pending.seq:
                return False
            if outcome["op"] == "state":
                self.conn = outcome["conn"]
        elif outcome["conn"] != self.conn:
            return False
        if outcome["op"] == "state" and outcome["rev"] > self.rev:
            self.rev, self.character = outcome["rev"], outcome["character"]
            self.swaps += 1
        if pending and outcome["seq"] == pending.seq:
            self.pending = None
        return True


def payload(op="hello", seq=1, have="angel", session="boot"):
    return dict(v=1, op=op, session=session, seq=seq, have=have)


def execute(state, request, *, failed=False):
    replay = state.admit(request)
    if replay:
        return replay
    target = state.target(request)
    if target is None:
        return state.remember(request, state.outcome(request))
    if failed:
        return state.failed(request, "switch-failed")
    return state.commit(request, target, target)


@pytest.mark.parametrize("change", [
    {"v": 2}, {"v": True}, {"seq": 0}, {"seq": -1}, {"seq": 2**32},
    {"seq": True}, {"seq": 1.0}, {"op": "set"}, {"have": "cat"},
    {"session": ""}, {"session": "x" * 129}, {"session": None}, {"extra": 1},
])
def test_protocol_rejects_malformed(change):
    with pytest.raises(ValueError):
        Request.parse(payload() | change)


def test_protocol_exact_shape_and_u32():
    assert Request.parse(payload(seq=0xffffffff)).seq == 0xffffffff
    for data in (None, [], {}, payload() | {"v": "1"}):
        with pytest.raises(ValueError):
            Request.parse(data)


@pytest.mark.parametrize("lost", range(1, 5))
def test_every_ack_lost_through_retry_limit_then_resync(lost):
    state, device = SwitchState(conn="epoch"), DeviceView("boot")
    hello = device.connect()
    device.receive(execute(state, hello), device.generation)
    request = device.request("toggle")
    original = execute(state, request)
    for attempt in range(4):  # original plus three retries, two seconds apart on device
        result = execute(state, request)
        assert result == original
        assert state.character == "devil" and state.rev == 2
        if attempt >= lost:
            device.receive(result, device.generation)
            break
    if device.pending:
        resync = device.request("hello")
        assert resync.have == "angel"
        # Old response can update state, but cannot settle the superseding hello.
        device.receive(original, device.generation)
        assert device.pending == resync
        device.receive(execute(state, resync), device.generation)
    assert device.pending is None and device.character == "devil"


def test_lost_error_replays_error_even_after_service_recovery():
    state = SwitchState()
    execute(state, Request.parse(payload()))
    request = Request.parse(payload("toggle", 2))
    error = execute(state, request, failed=True)
    assert execute(state, request) == error
    assert error["op"] == "error" and state.rev == 1
    assert state.sessions["boot"].high_water == 2


def test_high_water_eight_outcomes_mismatch_stale_and_session_isolation():
    state = SwitchState()
    for seq in range(1, 14):
        execute(state, Request.parse(payload("hello" if seq == 1 else "toggle", seq)))
    assert list(state.sessions["boot"].outcomes) == list(range(6, 14))
    assert execute(state, Request.parse(payload("hello", 13)))["reason"] == "mismatch"
    assert execute(state, Request.parse(payload("toggle", 5)))["reason"] == "stale"
    assert state.sessions["boot"].high_water == 13
    assert execute(state, Request.parse(payload(session="another")))["op"] == "state"
    assert state.sessions["another"].high_water == 1


def test_session_records_are_capped_per_connection_and_no_high_water_is_evicted():
    from duo_switch import MAX_SESSIONS
    state = SwitchState()
    for seq in range(1, 13):
        committed = execute(state, Request.parse(payload("hello" if seq == 1 else "toggle", seq)))
    for number in range(MAX_SESSIONS - 1):
        assert execute(state, Request.parse(payload(session=f"known-{number}")))["op"] == "state"
    assert len(state.sessions) == MAX_SESSIONS
    before = (state.rev, state.character)
    for number in range(4096):  # A peer that invents a session ID per request.
        refused = execute(state, Request.parse(payload("toggle", 1, session=f"invented-{number}")))
        assert (refused["op"], refused["reason"], refused["rev"]) == ("error", "sessions", state.rev)
    hello = execute(state, Request.parse(payload(session="one-more")))
    assert (hello["op"], hello["reason"]) == ("state", "sessions")  # A hello still reads the state.
    assert len(state.sessions) == MAX_SESSIONS and "one-more" not in state.sessions
    assert (state.rev, state.character) == before
    # Every known session is intact: replay, mismatch, stale, and a fresh seq.
    assert execute(state, Request.parse(payload("toggle", 12))) == committed
    assert execute(state, Request.parse(payload("hello", 12)))["reason"] == "mismatch"
    assert execute(state, Request.parse(payload("toggle", 3)))["reason"] == "stale"
    assert state.rev == before[0]  # The old toggle did not execute again.
    assert execute(state, Request.parse(payload("toggle", 13)))["rev"] == before[0] + 1
    assert execute(state, Request.parse(payload("toggle", 2, session="known-0")))["rev"] == before[0] + 2


def test_hello_adopts_once_across_sessions_and_resync_never_mutates():
    state = SwitchState()
    execute(state, Request.parse(payload(have="devil")))
    assert state.character == "devil" and state.adopted
    for seq in range(2, 5):
        result = execute(state, Request.parse(payload(seq=seq)))
        assert result["character"] == "devil" and result["rev"] == 1
    assert execute(state, Request.parse(payload(session="new-boot")))["character"] == "devil"


def test_reconnect_pending_toggle_lower_rev_baseline_and_old_events():
    state, device = SwitchState(conn="old"), DeviceView("boot")
    device.receive(execute(state, device.connect()), device.generation)
    for _ in range(5):
        device.receive(execute(state, device.request("toggle")), device.generation)
    record = state.verification
    reverted = state.revert(record)
    device.receive(reverted, device.generation)
    assert device.rev == 7
    pending = device.request("toggle")
    late = execute(state, pending)
    previous_generation = device.generation
    state = SwitchState(conn="new")
    first = execute(state, device.connect())
    assert first["rev"] == 1 and first["character"] == reverted["character"]
    device.receive(first, device.generation)
    assert device.rev == 1
    assert not device.receive(late, previous_generation)
    assert not device.receive(late, device.generation)
    device.receive(execute(state, device.request("toggle")), device.generation)
    assert device.rev == 2 and device.character != reverted["character"]


def test_equal_rev_replay_settles_pending_without_scene_swap():
    state, device = SwitchState(), DeviceView("boot")
    hello = device.connect()
    result = execute(state, hello)
    device.receive(result, device.generation)
    swaps = device.swaps
    device.pending = hello
    device.receive(execute(state, hello), device.generation)
    assert device.pending is None and device.swaps == swaps
    resync = device.request("hello")
    device.receive(execute(state, resync), device.generation)
    assert device.pending is None and device.swaps == swaps


def test_verification_owns_first_response_connection_revision_service_and_completion():
    state, service = SwitchState(), object()
    request = Request.parse(payload())
    state.admit(request)
    state.commit(request, "devil", service)
    state.bind_response(state.conn, state.rev, service, "first")
    state.bind_response(state.conn, state.rev, service, "second")
    good = (state.conn, state.rev, service, "first")
    assert state.matches(*good)
    for owner in (("old", *good[1:]), (good[0], 0, *good[2:]),
                  (*good[:2], object(), good[3]), (*good[:3], "second"), (*good[:3], None)):
        assert not state.matches(*owner)
        state.completed(*owner)
        assert state.verification is not None
    state.completed(*good)
    assert state.verification is None


def test_revert_bumps_revision_keeps_original_replay_and_fences_old_failure():
    state = SwitchState()
    hello = Request.parse(payload())
    execute(state, hello)
    toggle = Request.parse(payload("toggle", 2))
    committed = execute(state, toggle)
    record = state.verification
    state.bind_response(state.conn, state.rev, record.service, "bad")
    owner = (state.conn, state.rev, record.service, "bad")
    reverted = state.revert(state.verification)
    assert reverted["reason"] == "reverted" and reverted["rev"] == committed["rev"] + 1
    assert reverted["character"] == "angel" and not state.matches(*owner)
    assert execute(state, toggle) == committed


@pytest.mark.parametrize("failure", ["voice", "deadline"])
def test_failed_hello_establishes_authoritative_baseline_and_later_hello_is_resync(failure):
    state, device = SwitchState(conn="epoch"), DeviceView("boot", character="devil")
    request = device.connect()
    assert state.admit(request) is None
    result = state.failed(request, failure)
    assert result == dict(type="duo", v=1, op="state", session="boot", conn="epoch", seq=1,
                          rev=0, character="angel", reason="adoption-failed")
    assert state.adopted and state.admit(request) == result
    assert device.receive(result, device.generation) and device.conn == "epoch" and device.pending is None
    assert execute(state, Request.parse(payload(seq=2, have="devil")))["character"] == "angel"
    assert state.rev == 0
    assert execute(state, Request.parse(payload("toggle", 3)))["character"] == "devil"


def test_hello_is_state_even_for_mismatch_and_evicted_sequence():
    state = SwitchState(conn="epoch")
    for seq in range(1, 12):
        execute(state, Request.parse(payload("toggle", seq)))
    for seq, reason in ((11, "mismatch"), (1, "stale")):
        result = execute(state, Request.parse(payload("hello", seq)))
        assert result["op"] == "state" and result["reason"] == reason
        assert result["character"] == state.character and result["rev"] == state.rev


def admitted_hello(state, *, session="boot", seq=1, failed=False):
    """Model step 4's dispatch wrapper without changing device execution."""
    entry = state.sessions.get(session)
    before = entry.high_water if entry else 0
    result = execute(state, Request.parse(payload(session=session, seq=seq)), failed=failed)
    if state.sessions[session].high_water > before:
        state.note_admitted(session)
    return result


def test_auto_eligibility_is_pure_and_requires_adoption_and_known_session():
    from copy import deepcopy
    state = SwitchState(conn="epoch")
    assert state.device_session is None
    assert not state.auto_eligible("devil")
    request = Request.parse(payload())
    assert state.admit(request) is None
    state.note_admitted("boot")
    assert not state.auto_eligible("devil")  # admitted, not yet adopted
    state.commit(request, "angel", object())
    assert state.auto_eligible("devil")
    assert not state.auto_eligible("angel")
    state.note_admitted("unknown")
    assert not state.auto_eligible("devil")
    state.note_admitted("boot")
    state.sessions["boot"].high_water = 0
    before = deepcopy(state.__dict__)
    assert not state.auto_eligible("devil")
    assert state.__dict__ == before


def test_auto_commit_and_revert_leave_device_cache_byte_identical():
    import pickle
    state, service = SwitchState(conn="epoch"), object()
    hello = admitted_hello(state)
    toggle_request = Request.parse(payload("toggle", 2))
    toggle = execute(state, toggle_request)
    cache = pickle.dumps(state.sessions)
    result = state.commit_auto("angel", service)
    assert result == dict(type="duo", v=1, op="state", session="boot", conn="epoch",
                          seq=2, rev=3, character="angel", reason="addressed")
    record = state.verification
    assert record.service is service
    assert (record.conn, record.rev, record.old_character) == ("epoch", 3, "devil")
    assert record.request == Request("auto", "boot", 2, "devil")
    assert pickle.dumps(state.sessions) == cache
    assert state.admit(Request.parse(payload())) == hello
    assert state.admit(toggle_request) == toggle
    assert hello["rev"] < result["rev"] and toggle["rev"] < result["rev"]
    state.bind_response(state.conn, state.rev, service, "bad-first-reply")
    assert state.matches(state.conn, state.rev, service, "bad-first-reply")
    reverted = state.revert(state.verification)
    assert reverted == result | dict(rev=4, character="devil", reason="reverted")
    assert state.verification is None
    assert pickle.dumps(state.sessions) == cache
    assert state.admit(Request.parse(payload())) == hello
    assert state.admit(toggle_request) == toggle


def test_toggle_after_auto_uses_new_character_and_normal_sequence():
    state = SwitchState()
    admitted_hello(state)
    state.commit_auto("devil", object())
    request = Request.parse(payload("toggle", 2, have="angel"))
    assert state.target(request) == "angel"
    result = execute(state, request)
    assert result["character"] == "angel" and result["rev"] == 3
    assert state.sessions["boot"].high_water == 2


def test_auto_session_tracks_admitted_hello_not_replays_toggles_or_cache_eviction():
    state = SwitchState()
    admitted_hello(state)
    admitted_hello(state, session="other")
    assert state.device_session == "other"
    admitted_hello(state)  # replay does not change provenance
    assert state.device_session == "other"
    for seq in range(2, 12):
        execute(state, Request.parse(payload("toggle", seq)))
    assert 1 not in state.sessions["boot"].outcomes
    admitted_hello(state, seq=1)  # stale
    admitted_hello(state, seq=11)  # op mismatch
    assert state.device_session == "other"
    result = state.commit_auto("devil", object())
    assert (result["session"], result["seq"]) == ("other", 1)
    admitted_hello(state, seq=12)  # resync in an older session entry
    assert state.device_session == "boot"
    assert state.auto_eligible("angel")
    result = state.commit_auto("angel", object())
    assert (result["session"], result["seq"]) == ("boot", 12)


def test_auto_after_failed_hello_adoption_uses_revision_zero_baseline():
    state = SwitchState(conn="epoch")
    baseline = admitted_hello(state, failed=True)
    assert baseline["reason"] == "adoption-failed" and baseline["rev"] == 0
    assert state.device_session == "boot" and state.auto_eligible("devil")
    result = state.commit_auto("devil", object())
    assert result["rev"] == 1 and result["seq"] == 1


def test_auto_operation_is_internal_only():
    with pytest.raises(ValueError, match="operation"):
        Request.parse(payload("auto"))
