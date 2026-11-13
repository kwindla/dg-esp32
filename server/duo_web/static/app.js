// Browser stand-in for the EchoEar duo device. Mirrors components/dg_duo/protocol.cpp:
// hello on Ready, one switch request in flight, 2 s retransmit (same seq) up to
// 4 attempts, then a resync hello; taps while a request is pending are ignored.
// Liveness: every server {t:'ping',seq} is answered with client-message pong {seq}.
import { PipecatClient, RTVIEvent } from "./pipecat.js";
import { SmallWebRTCTransport } from "./pipecat.js";
import { receiveCounters, receiveDuo } from "./protocol.mjs";

const $ = (id) => document.getElementById(id);
const CHARACTERS = ["angel", "devil"];
const RETRY_MS = 2000, ATTEMPTS = 4, DOUBLE_TAP_MS = 400;

// ---------- log ----------
function log(cls, text) {
  const el = $("log"), line = document.createElement("div");
  line.className = cls;
  line.textContent = `${new Date().toLocaleTimeString([], { hour12: false })}  ${text}`;
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
  el.appendChild(line);
  while (el.childNodes.length > 600) el.removeChild(el.firstChild);
  if (stick) el.scrollTop = el.scrollHeight;
}
const wire = (text) => { if ($("verbose").checked) log("sys", text); };

// ---------- face ----------
const art = {};
async function loadCharacter(name) {
  const base = `characters/${name}/`;
  const manifest = await (await fetch(base + "animation.json")).json();
  const img = (src) => new Promise((ok, fail) => { const i = new Image(); i.onload = () => ok(i); i.onerror = fail; i.src = base + src; });
  const layers = {};
  for (const [layer, spec] of Object.entries(manifest.layers)) {
    const frames = {};
    for (const [frame, src] of Object.entries(spec.frames)) frames[frame] = await img(src);
    layers[layer] = { bounds: spec.bounds, frames };
  }
  art[name] = { base: await img(manifest.base), layers };
}

const face = { blinkUntil: 0, nextBlink: performance.now() + 2500, happyUntil: 0, level: 0, fade: 1, from: null };
const canvas = $("face"), g = canvas.getContext("2d");

function frameFor(now) {
  let eyes = "open";
  if (now > face.nextBlink) { face.blinkUntil = now + 160; face.nextBlink = now + 2500 + Math.random() * 3500; }
  if (now < face.blinkUntil) eyes = face.blinkUntil - now > 110 || face.blinkUntil - now < 50 ? "half" : "closed";
  else if (now < face.happyUntil) eyes = "happy";
  else if (dev.userSpeaking) eyes = "look-left";
  const open = face.level;
  const mouth = open < 0.08 ? "rest-smile" : open < 0.3 ? "small" : open < 0.55 ? "medium" : open < 0.8 ? "wide" : "round";
  return { eyes, mouth };
}

function paint(name, alpha, f) {
  const a = art[name]; if (!a) return;
  g.globalAlpha = alpha;
  g.drawImage(a.base, 0, 0, 412, 412);
  for (const [layer, frame] of [["eyes", f.eyes], ["mouth", f.mouth]]) {
    const L = a.layers[layer], im = L && L.frames[frame];
    if (im) g.drawImage(im, L.bounds[0], L.bounds[1], L.bounds[2], L.bounds[3]);
  }
  g.globalAlpha = 1;
}

function render(now) {
  face.level *= 0.82;  // decay between remote audio level callbacks
  const f = frameFor(now);
  g.clearRect(0, 0, 412, 412);
  if (face.from && face.fade < 1) {
    face.fade = Math.min(1, face.fade + 0.08);
    paint(face.from, 1, { eyes: "closed", mouth: "rest-smile" });
    paint(dev.character, face.fade, f);
  } else paint(dev.character, 1, f);
  if (dev.state !== "ready") {
    g.fillStyle = "rgba(0,0,0,.55)"; g.fillRect(0, 0, 412, 412);
    g.fillStyle = "#fff"; g.font = "600 22px system-ui"; g.textAlign = "center";
    g.fillText(dev.state === "connecting" ? "Connecting…" : dev.state === "error" ? "Connection failed" : "Tap Connect", 206, 214);
  } else if (dev.pending && dev.pending.op === "toggle") {
    g.strokeStyle = "rgba(255,255,255,.7)"; g.lineWidth = 6;
    g.beginPath(); g.arc(206, 206, 196, now / 150 % (2 * Math.PI), now / 150 % (2 * Math.PI) + 1.2); g.stroke();
  }
  requestAnimationFrame(render);
}

// ---------- device protocol ----------
const dev = {
  state: "disconnected", character: "angel", session: crypto.getRandomValues(new Uint32Array(2)).reduce((s, v) => s + v.toString(16).padStart(8, "0"), ""),
  seq: 0, conn: "", rev: 0, generation: 0, pending: null, timer: null, helloErrors: 0, userSpeaking: false,
  counts: { toggles: 0, retransmitted: 0, resyncs: 0, ignored: 0, pings: 0, ...receiveCounters() },
  lastPing: 0,
};

function show() {
  const s = $("state");
  s.textContent = dev.state; s.className = "pill " + ({ ready: "s-ready", connecting: "s-busy", error: "s-bad" }[dev.state] || "s-off");
  $("character").textContent = dev.character;
  $("pending").textContent = dev.pending ? `${dev.pending.op} seq ${dev.pending.seq} (attempt ${dev.pending.attempts})` : dev.conn ? "idle" : "no baseline";
  $("rev").textContent = dev.conn ? `${dev.rev} / ${dev.conn.slice(0, 8)}` : "–";
  $("live").textContent = dev.state === "ready" ? `${dev.counts.pings} pings, last ${dev.lastPing ? ((performance.now() - dev.lastPing) / 1000).toFixed(1) + " s ago" : "–"}` : "–";
  const c = dev.counts;
  $("counts").textContent = `toggles ${c.toggles} · retx ${c.retransmitted} · resyncs ${c.resyncs} · ignored ${c.ignored} · errors ${c.errors}`;
  $("connect").textContent = dev.state === "disconnected" || dev.state === "error" ? "Connect" : "Disconnect";
  $("tap").disabled = dev.state !== "ready";
}
setInterval(show, 500);

function send(p) {
  const data = { v: 1, op: p.op, session: dev.session, seq: p.seq, have: dev.character };
  try { client.sendClientMessage("duo", data); } catch (e) { log("err", `duo send failed: ${e.message || e}`); }
  wire(`→ duo ${JSON.stringify(data)}`);
}

function begin(op, resync = false) {
  clearTimeout(dev.timer);
  dev.pending = { op, seq: ++dev.seq, attempts: 0, resync };
  if (op === "toggle") dev.counts.toggles++;
  tick();
}

function tick() {
  const p = dev.pending; if (!p || dev.state !== "ready") return;
  if (p.attempts === ATTEMPTS) {
    log("err", `${p.op} seq ${p.seq}: no answer after ${ATTEMPTS} attempts`);
    dev.pending = null;
    if (!p.resync) { dev.counts.resyncs++; dev.conn = ""; begin("hello", true); }
    return show();
  }
  if (p.attempts) dev.counts.retransmitted++;
  p.attempts++; send(p);
  dev.timer = setTimeout(tick, RETRY_MS);
  show();
}

function tap(source) {
  if (dev.state !== "ready") return;
  if (dev.pending) { dev.counts.ignored++; log("sys", `double-tap (${source}) ignored: request pending`); return show(); }
  if (!dev.conn) { log("sys", "double-tap ignored: no baseline yet"); return; }
  log("duo", `double-tap (${source}) → toggle`);
  begin("toggle");
}

function setCharacter(name) {
  if (name === dev.character) return;
  face.from = dev.character; face.fade = 0; face.happyUntil = performance.now() + 900;
  dev.character = name;
}

function onDuo(m, generation) {
  wire(`← duo ${JSON.stringify(m)}`);
  const was = dev.character;
  const result = receiveDuo(dev, m, generation, setCharacter), p = result.pending;
  if (result.changed) {
    const why = m.reason ? ` (${m.reason})` : "";
    if (result.completed && p.op === "hello") log("duo", `baseline: ${m.character}, rev ${m.rev}${why}`);
    else if (was !== m.character) log("duo", `now ${m.character}, rev ${m.rev}${why}`);
    else log("duo", `state ${m.character}, rev ${m.rev}${why}`);
  }
  if (result.completed) {
    clearTimeout(dev.timer);
    if (m.op === "error") log("err", `${p.op} seq ${p.seq} failed: ${m.reason}`);
    if (result.retryMs !== null) setTimeout(() => generation === dev.generation && dev.state === "ready" && !dev.pending && begin("hello", p.resync), result.retryMs);
  }
  show();
}

// ---------- Pipecat client ----------
let client = null;
let botText = "";
function flushBot() { if (botText.trim()) log("bot", `${dev.character}: ${botText.trim()}`); botText = ""; }

function makeClient() {
  const generation = dev.generation;
  const c = new PipecatClient({
    transport: new SmallWebRTCTransport(),
    enableMic: true, enableCam: false,
    callbacks: {
      onTransportStateChanged: (s) => { wire(`transport ${s}`); },
      onBotReady: () => {
        if (generation !== dev.generation) return;
        if (dev.state === "ready") return;  // bot-ready can arrive twice; one hello per connection
        dev.state = "ready"; dev.conn = ""; dev.rev = 0; dev.helloErrors = 0;
        log("sys", "bot ready → hello"); begin("hello");
      },
      onDisconnected: () => {
        if (generation !== dev.generation) return;
        dev.generation++;
        clearTimeout(dev.timer); dev.pending = null; dev.conn = "";
        if (dev.state !== "error") dev.state = "disconnected";
        flushBot(); log("sys", "disconnected"); show();
      },
      onServerMessage: (m) => {
        if (m && m.t === "ping" && Number.isInteger(m.seq)) {
          dev.counts.pings++; dev.lastPing = performance.now();
          try { c.sendClientMessage("pong", { seq: m.seq }); } catch {}
          return;
        }
        if (m && m.type === "duo") return onDuo(m, generation);
        wire(`← server ${JSON.stringify(m)}`);
      },
      onTrackStarted: (track, participant) => {
        if (participant?.local || track.kind !== "audio") return;
        const a = $("botAudio") || Object.assign(document.createElement("audio"), { id: "botAudio", autoplay: true });
        if (!a.isConnected) document.body.appendChild(a);
        a.srcObject = new MediaStream([track]);
        a.play().catch(() => log("err", "audio autoplay blocked: click the page"));
      },
      onUserStartedSpeaking: () => { dev.userSpeaking = true; },
      onUserStoppedSpeaking: () => { dev.userSpeaking = false; },
      onUserTranscript: (d) => { if (d.final) log("user", `you: ${d.text}`); },
      onBotOutput: (d) => { if (d?.text && (d.spoken ?? true) && (d.text_type ?? d.aggregated_by) !== "word") botText += (botText ? " " : "") + d.text; },
      onBotStartedSpeaking: () => { botText = ""; },
      onBotStoppedSpeaking: () => flushBot(),
      onLocalAudioLevel: (l) => { $("mic").style.width = Math.min(100, l * 300) + "%"; },
      onRemoteAudioLevel: (l) => { const v = Math.min(1, l * 4); face.level = Math.max(face.level, v); $("spk").style.width = v * 100 + "%"; },
      onError: (e) => { log("err", `error: ${JSON.stringify(e?.data ?? e)}`); },
    },
  });
  return c;
}

async function connect() {
  dev.generation++;
  dev.state = "connecting"; show();
  client = makeClient();
  try {
    await client.startBotAndConnect({ endpoint: new URL("bot/start", location.href).toString() });
  } catch (e) {
    dev.state = "error"; log("err", `connect failed: ${e?.message || JSON.stringify(e)}`); show();
  }
}

$("connect").onclick = async () => {
  if (dev.state === "disconnected" || dev.state === "error") return connect();
  dev.state = "disconnected"; await client?.disconnect(); show();
};
$("tap").onclick = () => tap("button");
$("mute").onclick = () => {
  if (!client) return;
  const on = !client.isMicEnabled; client.enableMic(on);
  $("mute").textContent = on ? "Mute mic" : "Unmute mic";
};

// Double-tap recognizer: two presses inside 400 ms; a third press in the window is absorbed.
function recognizer(source) {
  let last = 0, armed = true;
  return () => {
    const now = performance.now();
    if (now - last < DOUBLE_TAP_MS && armed) { armed = false; tap(source); }
    else armed = true;
    last = now;
  };
}
canvas.addEventListener("pointerdown", recognizer("screen"));
const key = recognizer("space");
addEventListener("keydown", (e) => { if (e.code === "Space" && !e.repeat && e.target === document.body) { e.preventDefault(); key(); } });

await Promise.all(CHARACTERS.map(loadCharacter));
requestAnimationFrame(render);
show();
log("sys", `device session ${dev.session}`);
