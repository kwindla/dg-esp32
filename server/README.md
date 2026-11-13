# LAN Pipecat server (step 2)

Python >=3.12, Pipecat 1.15.0 and SmallWebRTC. The bot speaks RTVI 2.3.0 and the
firmware 2.1.0; `user-input`, which the firmware does not read, is turned off. Install with
`uv sync` from this directory. Keep the committed uv.lock and fixtures together.

| BOT_MODE | Behavior | Credentials |
| --- | --- | --- |
| offline-test (default) | Local greeting WAV, play/echo commands, input RMS and Silero VAD telemetry | None |
| live-test | Deepgram STT/TTS, fixed greeting, final transcripts trigger "The code word is pineapple." | DEEPGRAM_API_KEY |
| voice | Deepgram STT -> OpenAI LLM -> Deepgram TTS, one-sentence answers | DEEPGRAM_API_KEY, OPENAI_API_KEY |

Live modes load credentials using python-dotenv, first server/.env, then
../pipecat/.env relative to the repository root. Existing environment variables
take precedence. Offline mode does not call the credential loader or instantiate
cloud services. .env.example contains only empty credential names.

The server and pytest share SERVER_PORT, default 7861. Export a different
SERVER_PORT to change both; explicit runner --port overrides it for a manual
invocation. For example, SERVER_PORT=7862 BOT_MODE=offline-test uv run bot.py
--host 0.0.0.0 starts on 7862. Changing the server port also requires matching
the board URL. SERVER_PORT is a shell environment setting; credentials alone
are loaded from the .env files.

From this directory:

```sh
BOT_MODE=offline-test uv run bot.py --host 0.0.0.0
BOT_MODE=live-test uv run bot.py --host 0.0.0.0
BOT_MODE=voice uv run bot.py --host 0.0.0.0
```

The board-facing address is http://192.168.7.222:7861. The optional runner
compatibility mode is `--esp32 --host 192.168.7.222`; this filters
answer candidates/fingerprints and belongs to the step 3 hardware comparison.
Plain mode is the step 2 reference.

AUDIO_OUT_10MS_CHUNKS defaults to 4 (40 ms); both pipeline sample rates are
16000 Hz mono. Opus SDP uses 48000/2 regardless. No STUN/TURN is needed on this
LAN. The client creates an ordered reliable data channel named chat.

## Duo bot (angel and devil)

`duo_bot.py` runs the two-character bot on the same port. `duo_switch.py` is
the device protocol, `duo_route.py` the Jev addressee routing (its docstring
states the four routing invariants), `duo_addressee.py` the classifier
question and decision floor, and `duo_prompts.py` the personas.

| Variable | Default | Effect |
| --- | --- | --- |
| DUO_AUTO_SWITCH | 0 | 1 routes each turn through Jev and switches character when the other one is addressed. Needs TYPESAFE_API_KEY, otherwise it stays off and says so in the identity. |
| DUO_TIMING | follows DUO_AUTO_SWITCH | 1 logs one `DUO timing` row per reply; set it with auto-switch off to record the baseline for a latency comparison. |
| DUO_AUTO_SWITCH_FLOOR, DUO_ROUTE_BUDGET_S, DUO_JEV_MODEL | 0.80, 0.5, jev-1.14.0 | Calibrated routing values. Changing one requires rerunning the acceptance set. |
| DUO_CANCEL_JOIN_MS | unset | Unset: an interruption that retires a route waits, without limit, for the cancelled Jev request to finish cleaning up. A number n limits each such wait to n ms (0: do not wait). The coordinator still joins the request before the next read, and a cleanup that outlasts the 3 s transaction deadline ends the session. Off until a live A/B decides. |
| DUO_STT_KEYTERMS | 0 | 1 passes the keyterms `Angel` and `Devil` to Flux in the duo pipeline only; nothing else about the STT service changes. For a live A/B: the names were heard as "Bangel" and "Dangel". |
| DUO_JEV_KEEPWARM_S | unset | n (1 to 120): while a session lives, send `GET /v1/models` whenever the Jev connection has been idle for n seconds, so the next read does not pay for a new connection. For a live A/B. |
| DUO_JEV_HEDGE_MS | unset | n (below the route budget): a read that has not answered after n ms gets one duplicate request, and the first answer wins. The loser is cancelled and joined, within DUO_CANCEL_JOIN_MS when that is set. For a live A/B. |
| DUO_VOCATIVE_RULE | 0 | 1: a turn that opens by calling the other character (`Devil,` `Devil.` `Devil?` `Devil!`, a bare `Devil`, or one of those after `hey` / `okay` / `ok` / `so`) switches to them whatever Jev answered. The route event then says `reason: 'vocative-rule'` with `jev_target` and `jev_reason` beside it. Every other turn is Jev's, and every guard on a switch applies. A ruled turn's Jev read ends 50 ms before the route deadline. For a live A/B. |
| DUO_GC_LOG | 0 | 1 logs one `DUO gc` row (`generation`, `ms`, `collected`) for each garbage collection that takes 20 ms or more. A full collection walks the whole import heap: about 150,000 objects and 60 to 75 ms offline, on the event loop. For a live A/B. |
| DUO_GC_FREEZE | 0 | 1 imports what a session imports, collects once and calls `gc.freeze()` before the server starts, and logs `DUO gc-freeze` with the object count. Later collections skip the import heap (offline: a full collection drops from about 65 ms to under 0.1 ms). Nothing a session creates is frozen. Both GC flags are applied once per process by `start_process()`, whether the bot is run as a script or launched through `hil/duo_identity.py`. For a live A/B. |
| DUO_LLM_URL | none, required | The OpenAI-compatible base URL of the duo LLM, `http://<host>:<port>/v1`. No endpoint is written in this repository; `LOCAL_LLM_URL` is read as a fallback. With neither set, `duo_bot.py` exits before it binds (`duo bot not started: DUO_LLM_URL is not set`), and the bench launcher refuses to record an identity. The identity records the URL and which variable it came from; the check requires an http(s) URL from one of the two, without credentials, and pins no host. |
| DUO_LLM_MODEL | `phonellm-max-alpha-2-nvfp4` | `LOCAL_LLM_MODEL` is read as a fallback. The bench launcher's identity check accepts only the default. |
| DUO_TEST, DUO_FAULT | unset | Fault injection for the harness; DUO_FAULT is refused without DUO_TEST=1. |

Where the flags added in November 2026 stand. The behaviour flags are off
unless set; DUO_TIMING alone has a conditional default and follows
DUO_AUTO_SWITCH. A bot with none of them set behaves as the flag-off baseline
test records.
"Live" means the bench runs of 2026-11-10 against the EchoEar and the web
simulator; "offline" means the tests and probes in this directory.

| Flag | Evidence so far | Recommended on? |
| --- | --- | --- |
| DUO_STT_KEYTERMS | Live: transcripts that mangled "Angel" ("Bangel", "Dangel") went from 3 to 5 in a 21-turn run to none. | Yes. |
| DUO_VOCATIVE_RULE | Live, with transcripts that were exactly right: "Devil, what should I read next?" scored 0.77 and a bare "Devil." 0.68, both under the 0.80 floor, so neither switched. With the rule on, the web cohort's functional2 set (two addressed turns, an idiom and a mention) was routed right 4 of 4, with no false switch, and 17 of 17 addressed turns switched. | Yes, if the rule itself is wanted: a turn that opens with the other name always switches. It is a product decision, not a tuning. |
| DUO_JEV_KEEPWARM_S, DUO_JEV_HEDGE_MS | The reason for them is live: some Jev reads outlasted the 0.5 s budget and the turn lost its switch. Offline: an idle connection kept warm is reused, and a hedge's losing request is cancelled and joined. No live comparison has been recorded. | Not yet. Compare live first. |
| DUO_CANCEL_JOIN_MS | Offline only: with a 300 ms request cleanup, a barge-in reached the end of the pipeline after 302 ms unlimited, 27 ms at 20 and 7 ms at 0. | Not yet. It moves a wait off the interruption path, so it needs a live comparison of barge-in behaviour. |
| DUO_GC_FREEZE | Offline: a full collection takes 58 to 75 ms over the import heap and under 0.1 ms once it is frozen. Not measured live. A bot launched through `hil/duo_identity.py` applies it only from the commit that added `start_process()`; a comparison made through the launcher before that had two untreated arms. | Not yet. Compare live first, with DUO_GC_LOG on in both arms. |
| DUO_GC_LOG | A diagnostic: it shows the pauses, it does not change them. | Only while investigating pauses. |
| DUO_TIMING | Instrumentation. Its rows are what the latency comparisons above are read from. | As needed. |

Every `DUO route` event carries `jev`, how the read travelled: `connection` is
`new` or `reused`, with `connect_ms` and `tls_ms` for a new one, and `hedged`
and `winner` when a duplicate was sent. The same record is logged at debug
level as `Jev read`, and keep-alive requests as `Jev keep-warm`. Only event
names and times are recorded.

One connection keeps at most four device session records and eight waiting
device requests. A request that names a fifth session ID is answered
`op:"error", reason:"sessions"` (a hello still gets the state, with that
reason) and leaves no record; no known session is evicted. A request that
arrives while eight already wait is not queued, and the event
`request-refused` is logged; the device's retransmission of the same seq runs
later. Pongs and lifecycle events are never refused.

Offline tests (no sockets, no credentials), from the repository root:

```sh
PYTHONPATH=hil:server uv run --project server --frozen --offline --group dev python -m pytest \
  server/test_duo_switch.py server/test_duo_pipeline.py server/test_duo_retirement.py \
  server/test_duo_addressee.py server/test_duo_baseline.py
```

### Pipecat upgrade checklist

`configuration()` refuses any Pipecat other than 1.15.0 because the bot leans
on behaviour that is not public API. Before raising the pin, re-read each of
these in the new version and rerun the five files above (last done for
1.12.0 to 1.15.0, 2026-11-11):

1. `BaseOpenAILLMService.process_frame` pushes the response Start, runs
   `_process_context`, pushes the End in a `finally`, and does not forward the
   context frame. `RouteStampingLLM` stamps and retires a run on that shape.
2. `FrameProcessor` interruption handling cancels and awaits the processing
   task, reads `interruptible` on the frame being processed, and keeps
   uninterruptible frames in the queue. `TransactionMarker`, `SilentNote`
   and the cancellation handlers in `duo_route.py` depend on it.
3. The LLM response frame classes in `RESPONSE_FRAMES`, and `frame.metadata`
   surviving every push between the LLM and the voices. `TextHold` and
   `DuoServiceSwitcher` judge frames by that metadata.
4. `PipelineWorker.flush_pipeline()` drains through the `ServiceSwitcher`: each
   leg of its probe is a `PipelineFlushFrame` of its own (since 1.14), so
   `ParallelPipeline`'s deduplication by frame ID lets all three pass. The bot
   flushes between a switch's steps and no longer repairs this itself.
5. `DeepgramFluxTTSService` connection lifecycle: `_connect`, `_disconnect`,
   `_handle_message`, `_handle_audio`, `on_turn_context_created`,
   `get_active_audio_context_id` and the `on_connected` and
   `on_connection_error` events. `DuoFluxTTS` retires and reconnects a voice
   through them.
6. The assistant aggregator writes history through `LLMContext.add_message`,
   also when it settles an interrupted reply. `CharacterContext` labels the
   speaker there.
7. `sctp_flight_fix.py` patches aiortc's SCTP transport; check it against the
   aiortc version the new Pipecat pins.
8. `JevClient` keeps its `httpx.AsyncClient` in `_http`, builds it with
   `http2=True`, and has no keep-alive of its own beyond a 240 s pool expiry.
   `Addressee` attaches its request trace and its keep-alive request there.
9. `tts_connection_probe.py` (demo bot, AEC harness) names the Pipecat version
   and the hashes of `flux/tts.py` and `flux/tts_base.py` it was checked
   against, and refuses to reconnect on any other. Diff both files, then
   update `EXPECTED_VERSION` and `EXPECTED_HASHES`.
10. What the firmware reads from RTVI: `bot-ready` (`version`),
   `user-transcription`, `bot-tts-text`, `server-message`, the speaking events
   and `error`. `hil/duo_identity.py` names the version too.

## Routes and messages

| Method | Route | Request | Response |
| --- | --- | --- | --- |
| POST | /start | {"transport":"webrtc"} | sessionId (and optional iceConfig) |
| POST | /sessions/<sessionId>/api/offer | {"sdp":"...","type":"offer"} | {"sdp":"...","type":"answer","pc_id":"..."} |
| POST | /api/offer | Same offer; runner's direct endpoint | Same answer; tests use the session route |

The test client constructs exactly the message structure from
pipecat-client-cxx v1.1.0 (ca56dc45), src/rtvi/messages.cpp:171–175 and 206–235:
`{id, label:"rtvi-ai", type:"client-ready", data:{version:"2.1.0", about:...}}`.
The caller-supplied AboutClientData contains library="pipecat-client-cxx" and
library_version="1.1.0"; optional platform fields are omitted. Library and RTVI
protocol versions are deliberately different. A matching bot-ready ID is required.

Custom client-message data:

- `{"t":"echo","d":{"nonce":"value"}}` returns server-response with the same ID.
- `{"t":"play","d":{"fixture":"reply_pineapple.wav"}}` plays the fixture and
  returns a correlated server-response. Only greeting.wav, reply_pineapple.wav and the unscored response-calibration prompt_france.wav
  are accepted; unknown commands or fixtures return errors.
- Every 500 ms of arriving input audio, server-message carries
  `{"t":"user-audio","d":{"rms_dbfs":number,"speaking":boolean}}`.
  Speaking comes from Silero VAD edges, not a guessed RMS threshold.

server/events.jsonl records all inbound/outbound wire RTVI messages, VAD edges,
transport connect/disconnect, pc_id and monotonic timestamps. EVENTS_PATH
overrides this location. Connection loss cancels the runner and workers.

## Fixtures and acceptance

The small committed WAVs are 16 kHz, mono, 16-bit PCM. manifest.json records
text, model, duration and SHA256. They were generated with Deepgram's
[REST synthesis API](https://developers.deepgram.com/reference/text-to-speech/speak-request?explorer=true).
`uv run make_fixtures.py` only generates missing files; `--replace` explicitly
regenerates all. Existing inputs work without credentials.

With the runner stopped (pytest owns the test server process):

```sh
BOT_MODE=offline-test uv run pytest -q -s
BOT_MODE=live-test uv run pytest -q -s
```

The suite starts the real runner, uses the session routes above, and negotiates
an independent aiortc Opus peer. It requires bot-ready, matching echo/play
responses, waveform correlation >=0.80 for greeting and commanded reply,
VAD speaking=true during a transmitted France speech fixture, and speaking=false
afterward. Silence and random noise are explicit negative correlation tests.
The live suite additionally requires a France transcript and correlated audio
for the automatically spoken reply; missing live credentials fail, not skip.

Offline subprocess tests strip API keys, set HF_HUB_OFFLINE, and install a
test-only socket audit guard rejecting external destinations. Offline turn-stop
uses SpeechTimeoutUserTurnStopStrategy without waiting for a transcript so
SmartTurn cannot download a model. Voice/live modes use explicit SmartTurn v3;
their current silence bounds are documented below.

For offline playback correlation, the reference is the committed input WAV.
For live synthesis, optional TTS_REFERENCE_DIR saves the actual Deepgram output
PCM before Opus and the client correlates against it; separate synthesis calls
need not yield the same waveform. The explicit play command still uses the
committed reply fixture in both modes. All correlations use a fresh recording
segment beginning before the corresponding trigger.

Evidence goes under gitignored proj-2026-10-08-0949/evidence/step2/, including
server/event logs, SDP offer/answer, client-ready payload, decoded received.wav,
live source PCM and result JSON. The complete pinned dependency environment
includes local Silero assets; no fixture generation is needed during pytest.

## Observed plain-runner SDP answer

Capture on 2026-10-08, HTTP port 7861 (the project default).
The separate project on port 7860 is left running. UDP ports and certificate fingerprints are
ephemeral. The complete offer/answer stays in local evidence. Representative
audio and host-candidate lines, plus all three fingerprint algorithms:

```sdp
a=rtpmap:96 opus/48000/2
a=candidate:82d120b3c255ed5dae0e4f6943b147f8 1 udp 2130706431 192.168.42.75 47705 typ host
a=candidate:ca2bb291df18de46d73717c97a9ca760 1 udp 2130706431 192.168.7.222 42909 typ host
a=fingerprint:sha-256 CB:3D:47:4C:32:AF:E7:96:48:D7:55:81:C7:15:76:FB:02:B9:86:0C:5A:1C:29:CA:59:A1:C5:E2:10:42:E1:D7
a=fingerprint:sha-384 F1:0E:15:6F:15:45:16:1A:F8:67:10:51:7C:A3:A2:43:8F:33:87:EF:48:BD:75:DA:17:F7:B6:FF:E9:0F:0A:E2:43:25:53:49:A2:A8:F6:D1:D6:0B:48:A7:39:98:AE:D6
a=fingerprint:sha-512 21:FA:9E:CA:FB:48:AC:6F:68:9A:3B:6F:E6:DB:94:EC:FA:52:97:36:A3:2E:01:80:F4:A3:27:F4:45:4F:7A:2F:62:70:D6:81:2A:DB:C7:15:39:B7:77:39:0B:86:38:59:62:EE:6D:D6:F2:08:91:95:F5:23:C0:78:91:43:D9:9D
```

No Opus a=fmtp line was present in this negotiated answer; do not invent
one for step 3. Additional IPv6, Docker and tailnet host candidates were
present. The LAN host candidate for 192.168.7.222 was gathered before the
answer response. ICE and DTLS connected without trickle PATCH or STUN/TURN.

Optional input evidence: `RECORD_INPUT=1` records each session's received 16 kHz
mono PCM16 audio to `server/out/<pc_id>.wav`. Set `RECORD_INPUT_DIR` to an evidence
directory to change the destination. WAV headers are finalized on disconnect
and cancellation; recording is disabled by default.


Step 7 live-test replies are triggered by the user aggregator's
`on_user_turn_stopped` event and emitted once per turn, after all final STT
segments are aggregated. Live-test and voice use the local
SmartTurn V3 stop strategy; offline uses the existing speech timeout strategy
without downloading a model. Session events include turn IDs, VAD stop,
aggregated transcript, first TTS byte and fixture-byte timestamps. Metrics
remain enabled and the disconnected handler cancels its own worker.
`PIPECAT_SCTP_MAX_CHUNK_SIZE=1100` keeps data-channel chunks below the path MTU.

The live-test fixed responder is triggered by `on_user_turn_stopped`, never by
individual STT finals. It coalesces stop callbacks for the same turn through a
one-second quiet window before publishing the complete turn and queuing one
reply. This protects trailing finals from the half-duplex gate. Pending replies
are cancelled on disconnect or superseded VAD turns. The default live-test
SmartTurn analyzer and Silero stop interval remain unchanged.

Historical step 8b bench calibration used `BOT_VAD_MIN_VOLUME=0.4`;
this is superseded by the final 8d3 defaults below (all modes use 0.6).
`confidence=0.7` and live-test start/stop timing stay fixed.
The default 0.6 volume floor vetoed calibrated 65/58 dB SPL speech even when
Silero recognized it; the measured 0.4 floor passes both and a two-minute silent
run. Session evidence logs the actual VAD parameters.

For the offline loudness sweep only, `OFFLINE_BENCH_FIXTURE_DIR` can expose
`tts_1.wav` and `tts_2.wav` captured once from live TTS. The directory is host
configured; clients select only those two bank names or the three committed fixtures.
The fixed unscored `prompt_france.wav` calibrates the acoustic FIR; greeting
and reply recordings are scored separately. HIL explicitly gates modeled FIR
board-mic correlation plus Jabra level (Jabra correlation is informational), which detects nonlinear faults,
dropouts and wrong content but can absorb linear faults. Raw Jabra correlation
is retained as a non-gating diagnostic.

Live-test STT disables Deepgram's short provider endpointing (`endpointing=False`)
and uses Pipecat's explicit VAD-stop finalization. This keeps quiet phonemes
in the same transcription context instead of finalizing word fragments.

For a silent offline handoff reconnect, `OFFLINE_SKIP_GREETING=1` suppresses
only the automatic greeting. Explicit fixture requests remain available.

Historical 8d2 voice mode (superseded by final 8d3 defaults below) used
Silero's 0.2 s start/stop thresholds, `min_volume=0.6`,
explicit LocalSmartTurnAnalyzerV3 with a 1 s incomplete-turn silence bound, both default start
strategies with interruptions enabled, and Deepgram's default endpointing
(10 ms). It uses the provider's built-in TTFS latency. The board input gain is
1600% (+18.06 dB over the former 200%); native mic dumps and SPL calibration
remain before gain. Playback uses the user-selected delivered −22 dBFS / 26% ceiling.

`BOT_VOICE_POLICY=timeout` selects the 0.8 s speech-timeout comparison arm with
0.2 s VAD and endpointing 500; `BOT_VOICE_POLICY=8d` restores the former 2 s
speech-timeout, 0.1/0.6 s VAD, endpointing 500, TTFS 1.2 and disabled
interruptions. `BOT_VAD_MIN_VOLUME` remains an explicit override. These
comparison options affect voice mode only. The default floor for other modes
was 0.4 in that historical comparison. The comparison runner separately pins C’s volume floor to 0.4
and all old-gain controls to 200%; selecting a server policy alone does not
change the board’s input gain.

Voice evidence logs interim/final transcripts, `finalized`, `speech_final`,
`from_finalize`, start/stop strategies, SmartTurn metrics and inference call
time, input RMS/peaks/clipping, LLM generations/cancellations and per-segment
TTS references, including cancelled prefixes. A completed LLM whose queued
speech is interrupted is identified separately by `generation_ended=true` on
the cancellation event.

Every voice comparison policy installs `AlwaysUserMuteStrategy` through
`LLMUserAggregatorParams.user_mute_strategies`. Pending replies remain
interruptible until server bot speech starts. While it speaks, Pipecat mutes
user audio, transcripts and interruptions; speech-end releases that mute.
`user-mute-start`/`user-mute-stop` and `bot-audio-start`/`bot-audio-end` record
these server boundaries. The board gate still follows actual decoded playback;
its jitter-buffer tail is compared independently in HIL evidence.
`BOT_SMARTTURN_STOP_SECS` explicitly selects SmartTurn's incomplete-turn
silence bound (voice default 1 s after the step 8d2 pause probe; the comparison
pins the original 3 s value in its SmartTurn arms). Live-test keeps 3 s.

## Step 8d3 correction to the 8d2 pause interpretation

The production SmartTurn v3 silence bound is restored to Pipecat's **3 s**
default; `BOT_SMARTTURN_STOP_SECS` remains an experiment override. The previous
1 s pause success ended the turn early on “What is the capital”; the continuation
interrupted that generation 84–243 ms after LLM start, before the first TTS byte.
It did not show SmartTurn holding a complete utterance through the pause. Each
trace contains one INCOMPLETE model verdict plus a silence-timeout no-op, rather
than two INCOMPLETE verdicts. In the 3 s control VAD missed the continuation,
so the model never evaluated the complete question. At the corrected gain the
standard prompts yielded COMPLETE 130/130 times; the bound does not delay that
path. The firmware gate and AlwaysUserMuteStrategy are unchanged. Continuations
arriving during bot speech may be lost, so 1 s cannot be selected without the
predeclared pause sweep. New audit events distinguish `kind: model` from
`kind: stop_secs_timeout`; only model calls count as verdicts.

At voice gain 1600%, volume-floor 0.6 rejects the measured −56 dBFS room floor
(volume 0.53 p50, 0.548 max), leaving roughly 5 dB of noise margin. Measured
speech gives volume 0.78 at −31.5 dBFS and 0.69 at −40 dBFS; an unstressed
−52 dBFS onset gives 0.56 and is vetoed. A −49 dBFS room exceeds the floor:
noise rejection then depends on Silero confidence. Quiet unstressed onsets have
little margin. That historical offline/live floor 0.4 is superseded by final 8d3 floor 0.6; those modes require their own silence
qualification at the production gain.

The endpoint probes cover a full France sentence with a 3 s zero/noise tail,
not a short quiet word. At the old 200% input gain, the real `58-2-A` trace
reproduces delayed endpointing: “Sam.” repeats at roughly 1 s cadence and the
final arrives at +4.608 s (`speech_final=false`). At corrected gain no such
raw speech/tail transcripts were observed in 132 A+gain turns. Deepgram's
10 ms default splits “France” at the sentence's 140 ms dip even at corrected
gain; the stop strategy waits for Finalize-driven transcripts or its VAD-stop
fallback. Zero tails themselves did not block endpointing in the probe.


The latest step 8d3 user decision keeps **Pipecat SmartTurn defaults (3 s)**
until strong human testing justifies a change. Automated selection is withdrawn;
the 96-turn pause comparison and user-stopped eight-turn repeat are informational
only. Production uplink returns to **20 ms** with `DG_OPUS_UPLINK_FRAME40=n`,
retaining the 32768-byte TX stack and other 8c optimizations. See
[the step 8d3 report](../proj-2026-10-08-0949/codex-jobs/step-8d3-report.md)
for image-specific results and qualification limits. Production VAD already
matches Pipecat 1.15.0 defaults: confidence 0.7, start/stop 0.2 s, volume floor
0.6. The explicit speaking-state mute and firmware gate remain.

Offline/live modes also return to the Pipecat 1.15.0 volume-floor default **0.6**.
The earlier step 8d3 120 s checks at 0.4 are historical; both modes are repeated
on the final 20 ms image with the default floor. An explicit `BOT_VAD_MIN_VOLUME`
override remains available for experiments. Production qualification clears
that override and uses the library defaults.


Production playback is −22 dBFS / 26% ceiling by the 2026-10-11 user decision. SmartTurn/VAD, AlwaysUserMuteStrategy, limiter, compression and 20 ms uplink remain unchanged. The −19 Step 8e arm is a bounded diagnostic, never a production default.

## EchoEar data-channel limits

EchoEar accepts at most 2048 UTF-8 bytes per complete incoming RTVI message,
including the envelope. Larger messages are dropped before parsing, without an
application acknowledgment, and counted as `drop_reasons.invalid_data`; the
device exports `data_admission.wire_limit`. Keep long bot output and function
payloads within this bound or split them only where their protocol permits it.
DisplayClient separately limits the compact display data object to 1023 bytes.

DisplayClient serializes admissions and shares two retries between queue-full
rejections and acknowledgment timeouts. A timeout replays the identical packet
(including epoch, sequence and ID); queue-full permits a fresh sequence.
Acknowledgment waits default to 3 seconds with 20/40 ms retry backoff. Exhaustion
is an explicit unknown-admission failure. Epoch changes, malformed acks and
permanent rejections stop the request; accepted admission does not prove render
completion. Retry budget 0 disables retries for original-attempt measurements.

There is no control priority at native admission: even `bot-ready` can be shed
under flood, leaving Connected until the 90-second app timeout. The compact
message reserve is bounded and does not guarantee control delivery.

## EchoEar recording demo

After the network/bench hold has been lifted, launch the Flux STT/turns →
GPT-4.1 → Flux TTS half-duplex demo with the animated cat:

```bash
/home/khkramer/src/dg-esp32-echoear-display/build/hil-env/bin/python /home/khkramer/src/dg-esp32-echoear-display/hil/demo_session.py
```

The default verifies the running EchoEar against the local ELF. Add `--flash`
to flash that image first. Local build metadata and firmware inputs must match
current master; server/harness-only commits can use the unchanged firmware ELF.
The launcher does not build or switch branches. `--check-only` checks local
prerequisites without opening serial, acquiring the bench, or starting services.

The launcher owns the bench lock, bot port 7861 and fixture port 7874 until Ctrl-C.
It checks the pinned EchoEar identity, initializes display storage (provisions
only an already erased region; never erases existing storage), loads the 360 px
cat fixtures, enables `disp character on`, sets the board server URL and playback volume 0 dB,
connects and enables audio. Speak first. Existing Wi-Fi configuration and the
approved Deepgram/OpenAI credentials are used by the firmware and demo bot.
No Jabra calibration or strict qualification monitor runs.

User and assistant transcripts appear in the terminal and `transcripts.log`.
Full bot/serial logs, ELF/source provenance, display setup and host NIC byte
deltas go into a new timestamped directory under
`/home/khkramer/src/evidence/echoear/B/` (override with `--out` under that root).
NIC totals do not identify non-board traffic or qualify the network. Ctrl-C
disables audio, disconnects, stops the cat and owned services, and releases the
bench; setup failures take the same cleanup path. This launcher is a demo, not
an acoustic or transport qualification.

Character mode keeps the listening mouth still, blinks occasionally, and makes
small whisker movements. Local speaker energy drives smoothed mouth changes at
most ten times per second; eyes stay open and whiskers rest while speaking.
`disp bench cat 30` remains the separate throughput benchmark. The launcher
waits for an open `MIC_GATE` report and initializes the display only once.

The optional `ECHOEAR_STT_PROFILE=dev` selects Nova-3 plus default Silero/Smart
Turn for development and future acceptance. The unset/default `demo` profile
keeps Flux STT/turns. Both retain the existing AEC capability/mute policy and
Flux TTS. See [the Nova profile contract](../docs/echoear-aec-nova-profile.md)
for turn completion, provenance, offline tests and report-only firmware probes.
