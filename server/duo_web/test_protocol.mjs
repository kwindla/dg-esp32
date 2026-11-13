// The same ten traces and counter checkpoints as test_host/protocol_test.cpp.
import assert from "node:assert/strict";
import { FAULTS, receiveCounters, receiveDuo } from "./static/protocol.mjs";

class Harness {
  constructor(session = "0000000000001234") {
    Object.assign(this, {
      session, state: "disconnected", character: "angel", generation: 0,
      conn: "", rev: 0, seq: 0, pending: null, helloErrors: 0,
      counts: { toggles: 0, retransmitted: 0, resyncs: 0, ignored: 0, ...receiveCounters() },
    });
  }
  begin(op) { this.pending = { op, seq: ++this.seq, attempts: 1, resync: false }; }
  connect(generation = 1) {
    Object.assign(this, { state: "ready", generation, conn: "", rev: 0, pending: null, helloErrors: 0 });
    this.begin("hello");
  }
  ready() { this.connect(); assert(this.receive(this.outcome(1))); }
  tap() {
    if (this.pending) { this.counts.ignored++; return; }
    if (this.state === "ready" && this.conn) { this.counts.toggles++; this.begin("toggle"); }
  }
  outcome(rev, character = "angel", conn = "epoch", state = true) {
    return { type: "duo", v: 1, session: this.session, conn, seq: this.seq, rev, character, op: state ? "state" : "error" };
  }
  receive(m, generation = this.generation) { return receiveDuo(this, m, generation).changed; }
}

let checkpoints = 0;
function check(trace, d, character, rev, pending, answered, toggles, stale = 0, failed = 0, session = 0, connection = 0, baseline = 0) {
  assert.equal(d.character, character, trace);
  assert.equal(d.rev, rev, trace);
  assert.equal(!!d.pending, pending, trace);
  assert.deepEqual(d.counts, {
    toggles, retransmitted: 0, resyncs: 0, ignored: 0, answered,
    errors: stale + failed + session + connection + baseline,
    faults: Object.fromEntries(FAULTS.map((f) => [f, {
      "stale-revision": stale, "switch-failed": failed, session, connection, baseline,
    }[f] ?? 0])),
  }, trace);
  console.log(`auto trace ${trace}: character=${character} rev=${rev} pending=${Number(pending)} answered=${answered} toggles=${toggles} stale=${stale} switch-failed=${failed} session=${session} connection=${connection} baseline=${baseline}`);
  checkpoints++;
}
const addressed = (m) => ({ ...m, reason: "addressed" });

// 1. Answered toggle, then unsolicited high-water state.
{
  const h = new Harness(); h.ready(); h.tap(); assert(h.receive(h.outcome(2, "devil")));
  assert(h.receive(addressed(h.outcome(3))));
  check("1", h, "angel", 3, false, 2, 1);
}
// 2. Lost toggle answer; later cached replay is stale.
{
  const h = new Harness(); h.ready(); h.tap(); const cached = h.outcome(2, "devil");
  assert(h.receive(addressed(h.outcome(3))));
  check("2-auto", h, "angel", 3, false, 2, 1);
  assert(!h.receive(cached));
  check("2-replay", h, "angel", 3, false, 2, 1, 1);
}
// 3. Lost error answer is settled by auto without a fault.
{
  const h = new Harness(); h.ready(); h.tap();
  const lost = { ...h.outcome(1, "angel", "epoch", false), reason: "switch-failed" };
  assert.equal(lost.seq, h.pending.seq); assert.equal(lost.op, "error");
  assert(h.receive(addressed(h.outcome(2, "devil"))));
  check("3", h, "devil", 2, false, 2, 1);
}
// 4. A pending hw+1 toggle survives auto at hw, then completes.
{
  const h = new Harness(); h.ready(); h.tap(); assert(h.receive(h.outcome(2, "devil")));
  const next = addressed(h.outcome(3)); h.tap();
  assert.equal(next.seq + 1, h.pending.seq); assert(h.receive(next));
  check("4-pending", h, "angel", 3, true, 2, 2);
  assert(h.receive(h.outcome(4, "devil")));
  check("4-done", h, "devil", 4, false, 3, 2);
}
// 5. Rollover session and old conn are rejected.
{
  const h = new Harness(); h.ready(); const old = addressed(h.outcome(2, "devil"));
  const rolled = new Harness("0000000000001235"); rolled.connect(); assert(!rolled.receive(old));
  const current = { ...old, session: rolled.session, conn: "new", rev: 1, character: "angel" };
  assert(rolled.receive(current)); assert(!rolled.receive({ ...old, session: rolled.session }));
  check("5", rolled, "angel", 1, false, 1, 0, 0, 0, 1, 1);
}
// 6. Revision-zero adoption failure establishes, then auto applies.
{
  const h = new Harness(); h.connect(); const first = { ...h.outcome(0), reason: "adoption-failed" };
  assert(h.receive(first)); assert(h.conn);
  assert(h.receive(addressed(h.outcome(1, "devil"))));
  check("6", h, "devil", 1, false, 1, 0);
}
// 7. Lost hello, then same-seq auto establishes; replay is stale.
{
  const h = new Harness(); h.connect(); const hello = h.outcome(1);
  assert(h.receive(addressed(h.outcome(2, "devil")))); assert.equal(h.conn, "epoch");
  check("7-auto", h, "devil", 2, false, 1, 0);
  assert(!h.receive(hello));
  check("7-replay", h, "devil", 2, false, 1, 0, 1);
}
{
  const h = new Harness(); h.connect(4); const o = addressed(h.outcome(2, "devil"));
  assert(!h.receive({ ...o, seq: o.seq + 1 }));
  assert(!h.receive({ ...o, session: "other" })); assert(!h.receive(o, 3)); assert.equal(h.conn, "");
  check("7-negative", h, "angel", 0, true, 0, 0, 0, 0, 2, 0, 1);
  assert(h.receive(o, 4));
  check("7-recovered", h, "devil", 2, false, 1, 0, 0, 0, 2, 0, 1);
}
// 8. Cached hello and toggle after auto cannot change the face.
for (const toggle of [false, true]) {
  const h = new Harness(); h.connect(); let cached = h.outcome(1); assert(h.receive(cached));
  if (toggle) { h.tap(); cached = h.outcome(2, "devil"); assert(h.receive(cached)); }
  assert(h.receive(addressed(h.outcome(toggle ? 3 : 2, toggle ? "angel" : "devil"))));
  assert(!h.receive(cached));
  check(toggle ? "8-toggle" : "8-hello", h, toggle ? "angel" : "devil", toggle ? 3 : 2, false, toggle ? 2 : 1, toggle ? 1 : 0, 1);
}
// 9. A delayed cached error is still counted after auto settled the request.
{
  const h = new Harness(); h.ready(); h.tap();
  const cached = { ...h.outcome(1, "angel", "epoch", false), reason: "switch-failed" };
  assert(h.receive(addressed(h.outcome(2, "devil")))); assert(!h.receive(cached));
  check("9", h, "devil", 2, false, 2, 1, 0, 1);
}
// 10. Equal/older rev matching seq settles pending without application.
{
  const h = new Harness(); h.ready(); assert(h.receive(addressed(h.outcome(3, "devil")))); h.tap();
  assert(!h.receive(h.outcome(3)));
  check("10-equal", h, "devil", 3, false, 2, 1);
  h.tap(); assert(!h.receive(h.outcome(2)));
  check("10-older", h, "devil", 3, false, 3, 2, 1);
}
assert.equal(checkpoints, 17);
console.log("duo simulator protocol: 10 traces, 17 counter checkpoints passed");
