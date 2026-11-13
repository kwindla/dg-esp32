// Device::receive parity, independent of DOM, transport and timers.
export const FAULTS = [
  "malformed", "session", "connection", "baseline", "stale-revision", "mailbox",
  "send", "timeout", "render", "switch-failed", "mismatch", "stale", "voice",
  "deadline", "cancelled", "other",
];

export function receiveCounters() {
  return { answered: 0, errors: 0, faults: Object.fromEntries(FAULTS.map((f) => [f, 0])) };
}

export function receiveDuo(dev, m, generation, applyCharacter = (name) => { dev.character = name; }) {
  const result = { changed: false, completed: false, pending: dev.pending, retryMs: null };
  const fault = (name) => { dev.counts.errors++; dev.counts.faults[name]++; };
  if (dev.state !== "ready" || generation !== dev.generation || m.session !== dev.session) {
    fault("session"); return result;
  }
  const p = dev.pending, establishing = !dev.conn;
  if (establishing) {
    if (!p || p.op !== "hello" || m.seq !== p.seq) { fault("baseline"); return result; }
  } else if (m.conn !== dev.conn) { fault("connection"); return result; }
  if (!["state", "error"].includes(m.op) || !["angel", "devil"].includes(m.character)) {
    fault("malformed"); return result;
  }
  if (establishing && m.op === "state") dev.conn = m.conn;
  result.changed = m.op === "state" && (establishing || m.rev > dev.rev);
  if (result.changed) {
    dev.rev = m.rev; applyCharacter(m.character);
  } else if (m.op === "state" && m.rev < dev.rev) fault("stale-revision");
  if (m.op === "error") {
    fault(["switch-failed", "mismatch", "stale", "voice", "deadline", "cancelled"].includes(m.reason) ? m.reason : "other");
  }
  if (p && m.seq === p.seq) {
    dev.pending = null; dev.counts.answered++; result.completed = true;
    if (p.op === "hello") {
      if (m.op === "state") dev.helloErrors = 0;
      else if (dev.helloErrors < 3) result.retryMs = 1000 << dev.helloErrors++;
    }
  }
  return result;
}
