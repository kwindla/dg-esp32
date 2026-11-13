# ESP peer transport logic (step 5, phase 1)

The native transport and codec integration are intentionally pending the step-5
review gate. This component currently builds only pure C++17 logic; it does not
open esp_peer, create tasks, or change the application firmware.

Run all host tests with `scripts/host-tests.sh`. The local suite uses gtest and
has no esp_peer, esp_audio_codec, ESP-IDF, or libdatachannel dependency.

- `params`: adapts the vendored SmallWebRTC URL and parameter rules (camelCase
  wins over snake_case; explicit request parameters win over session IDs).
  Session offers inherit start headers and retain the URL query/fragment.
  Explicit offers use their own headers and request data. UDP STUN URLs are
  retained for the native API; TURN is rejected with the required LAN-mode error.
  Answer validation requires 2xx, an object with `type=answer`, a string SDP and
  a candidate line, with a 32 KiB response and 16-level JSON nesting cap.
- `framer`: accumulates arbitrary mono PCM chunks into 320-sample frames, with
  a four-frame queue and millisecond PTS advancing by 20 even across dropped
  frames. All input is consumed; the return value counts this call's retained
  samples, so dropped data must not be retried. Reset discards partial audio and
  starts a new session timestamp. Storage is supplied by the caller.
- `playout`: schedules metadata IDs against a caller-supplied monotonic clock.
  PTS ordering handles uint32 wrap; the esp_peer callback has no RTP sequence
  number. Decode/Drop actions identify payloads the caller must release.
  Reordering is allowed before a packet's deadline; five missing frames use PLC,
  longer gaps use silence and resync. After 500 ms without accepted arrivals,
  Idle asks the consumer to wait 100 ms; Disconnected returns immediately.
  Three late packets grow delay by 20 ms up to the configured cap (at most
  300 ms); five stable seconds shrink it by 1 ms. Stale payloads are explicitly
  dropped, and a stalled consumer does not drain overdue frames in a burst.
- `msg_queue`: 31 FIFO ordinary-message slots plus a priority control slot.
  Bot-ready can replace a pending signalling message and cannot be evicted by
  subsequent traffic. Overflow/replacement, oversize and malformed input are
  counted. SAX classification bounds nesting and avoids a JSON DOM in the
  producer. Input lengths are exact, with no NUL terminator on the wire.
  SCTP owns network fragmentation; this queue takes complete messages, never
  concatenates callbacks, and rejects partial JSON. Native reassembly and the
  size matrix still need hardware verification in phase 2.

All four modules are caller-serialized. The integration must provide a short
synchronization policy, explicitly allocate large backing buffers in PSRAM, and
release pending payloads before playout reset/disconnect. Message storage is
32 times the configured message limit (512 KiB at 16 KiB). The SAX parser may
allocate individual string tokens, bounded by the message size. Native task
ownership, callback dispatch, teardown, allocation placement and actual wait
semantics remain phase-2 responsibilities.

Mandatory integration facts from the spike remain unchanged: plain runner on
7861, named active-DTLS offer rewrite, zero-length-only UDP guard, millisecond
PTS, explicit 16 kHz mono codecs, 32/16 KiB encoder/decoder stacks, 10 KiB peer
loop stack, 112 KiB peer caches/64 RTP entries, and the channel's returned stream
ID. There is no backend abstraction in these modules.

Step 8e2a2 keeps schema 2. `tick_resync` counts mismatches between the device
skip ledger and consecutive render indices; recovery retires skipped media once.
Render kind 7 is prefill/Starved zero output. Trace kind 17 announces a device
epoch (low 32 bits in timestamp, high bits in value) for subsequent renders in
that Session. Archived images without these records retain legacy analysis.
`ready_high_water` is a per-session high-water mark, including prior streams.

A same-SSRC sequence restart with continuing timestamps can confirm a rebase
when its signed sequence advance produces a discontinuity residual. Two coherent
packets are required, followed by the fixed 140 ms prefill from candidate ingress.
Small backwards sequence inconsistencies remain conflicts. Arbitrary restarts
are not guaranteed to be distinguishable from reorder; signaling is authoritative.

Legacy stat aliases retained for harness compatibility and marked for retirement:
`playout_queue` / `current_depth` = physical occupancy; `target_depth` = 7;
`playout_target_ms` = 140; `lost` = missing_slots; `concealed` / `plc_frames` =
plc_calls; `rebuffers` = starvation; `prefills`, `drift_drops`, `drift_inserts`,
`post_plc_zeros`, `device_silence_blocks`, `discontinuity`, and `latency_trim` mirror
Playout counters. `pulled_frames` / `pull_span_us` are render adapter observations,
not RTP-loss counters. Retire these aliases when consumers use `playout` directly.

Host recovery tests require the libopus runtime library (`libopus.so.0` on
Linux). They decode the checked-in 20 ms fixtures with real libopus; console
`playback-selftest` runs the same envelope, invalid-packet, mute-reset, and
inactive-expansion checks through the vendor decoder on the board. Console
`playback-pause <ms>` accepts 200..1000 ms and parks only the playback task while
DMA and ingress continue. `hil/step8e2a2_campaign.py` retains the pause checks,
four five-minute arms, and final five-turn e2e evidence.
