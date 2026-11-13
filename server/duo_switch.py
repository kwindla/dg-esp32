"""Wire protocol v1 and connection-owned switch state; no Pipecat dependencies."""

from collections import OrderedDict
from dataclasses import dataclass, field
from uuid import uuid4

CHARACTERS = ("angel", "devil")
# A device keeps one session ID for a whole boot (dg_duo::Device), so a
# connection sees one. The cap bounds memory against a peer that invents IDs.
MAX_SESSIONS = 4


@dataclass(frozen=True)
class Request:
    op: str
    session: str
    seq: int
    have: str

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) != {"v", "op", "session", "seq", "have"}:
            raise ValueError("shape")
        if type(data["v"]) is not int or data["v"] != 1:
            raise ValueError("version")
        if data["op"] not in ("hello", "toggle") or data["have"] not in CHARACTERS:
            raise ValueError("operation or character")
        if not isinstance(data["session"], str) or not 1 <= len(data["session"]) <= 128:
            raise ValueError("session")
        if type(data["seq"]) is not int or not 1 <= data["seq"] <= 0xffffffff:
            raise ValueError("sequence")
        return cls(data["op"], data["session"], data["seq"], data["have"])


@dataclass
class Session:
    high_water: int = 0
    outcomes: OrderedDict = field(default_factory=OrderedDict)


@dataclass(frozen=True)
class Verification:
    conn: str
    rev: int
    service: object
    old_character: str
    request: Request
    response: str | None = None


class SwitchState:
    def __init__(self, *, conn=None, character="angel"):
        self.conn = conn or uuid4().hex
        self.character = character
        self.rev = 0
        self.adopted = False
        self.sessions = {}
        self.verification = None
        self.device_session = None

    def outcome(self, request, *, op="state", reason=None):
        # Protocol amendment: even a rejected/stale hello reads authoritative
        # state so the device can establish its connection baseline.
        if request.op == "hello":
            op = "state"
        result = dict(type="duo", v=1, op=op, session=request.session, conn=self.conn,
                      seq=request.seq, rev=self.rev, character=self.character)
        if reason:
            result["reason"] = reason
        return result

    def admit(self, request):
        """Return a replay/error, or reserve a new seq for the sole coordinator."""
        session = self.sessions.get(request.session)
        if session is None:
            if len(self.sessions) >= MAX_SESSIONS:
                # Refused without a record. A known session is never evicted to
                # make room: its high-water mark is what keeps an old toggle
                # from executing again.
                return self.outcome(request, op="error", reason="sessions")
            session = self.sessions[request.session] = Session()
        cached = session.outcomes.get(request.seq)
        if cached:
            op, result = cached
            if op != request.op:
                return self.outcome(request, op="error", reason="mismatch")
            return dict(result)
        if request.seq <= session.high_water:
            return self.outcome(request, op="error", reason="stale")
        session.high_water = request.seq
        return None

    def remember(self, request, outcome):
        session = self.sessions[request.session]
        session.outcomes[request.seq] = (request.op, dict(outcome))
        while len(session.outcomes) > 8:
            session.outcomes.popitem(last=False)
        return outcome

    def failed(self, request, reason):
        if request.op == "hello":
            # Failure still finishes adopt-once. A later hello is a resync,
            # never a second attempt to adopt the device's stale character.
            self.adopted = True
            return self.remember(request, self.outcome(request, reason="adoption-failed"))
        return self.remember(request, self.outcome(request, op="error", reason=reason))

    def target(self, request):
        if request.op == "hello":
            return request.have if not self.adopted else None
        return "devil" if self.character == "angel" else "angel"

    def commit(self, request, character, service):
        old = self.character
        self.character = character
        self.rev += 1
        if request.op == "hello":
            self.adopted = True
        self.verification = Verification(self.conn, self.rev, service, old, request) if old != character else None
        return self.remember(request, self.outcome(request))

    def bind_response(self, conn, rev, service, response):
        from dataclasses import replace
        record = self.verification
        if record and record.response is None and (conn, rev, service) == (
                record.conn, record.rev, record.service):
            self.verification = replace(record, response=response)

    def matches(self, conn, rev, service, response):
        record = self.verification
        return bool(record and response is not None and self.rev == record.rev and
                    (conn, rev, service, response) ==
                    (record.conn, record.rev, record.service, record.response))

    def completed(self, *owner):
        if self.matches(*owner):
            self.verification = None

    def abandoned(self, *owner):
        """An interrupted first reply cannot verify a later response."""
        if self.matches(*owner):
            self.verification = None

    def revert(self, record):
        self.character = record.old_character
        self.rev += 1
        self.verification = None
        return self.outcome(record.request, reason="reverted")

    def note_admitted(self, session):
        """Record a newly admitted hello; the dispatch wrapper owns provenance."""
        self.device_session = session

    def auto_eligible(self, target):
        session = self.sessions.get(self.device_session)
        return bool(self.adopted and session and session.high_water >= 1 and
                    target != self.character)

    def commit_auto(self, target, service):
        """Commit a bot switch without changing any device replay outcome."""
        old = self.character
        request = Request("auto", self.device_session,
                          self.sessions[self.device_session].high_water, old)
        self.character = target
        self.rev += 1
        self.verification = Verification(self.conn, self.rev, service, old, request)
        return self.outcome(request, reason="addressed")
