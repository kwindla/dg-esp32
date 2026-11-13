# EchoEar uplink tail comparison

The frozen-image A/B/A did **not establish a regression in 9936da45**. Its
receive tail fell between two old-image arms. All three diagnostic populations
failed the unchanged 1% limit. The captures place the threshold crossings after
successful ESP socket send-return; they do not identify a particular AP,
radio, driver or kernel fault. B's original strict and acoustic failures remain
unchanged.

| Arm | Frozen ELF | Receive intervals >60 ms | Rate | Maximum receive gap | >150 ms |
|---|---|---:|---:|---:|---:|
| Old 14284c8 | 94a5a13c | 54/3,603 | 1.498751% | 235 ms | 2 |
| New 9bc2d42 | 9936da45 | 80/3,603 | 2.220372% | 151 ms | 1 |
| Old 14284c8 repeat | 94a5a13c | 118/3,596 | 3.281424% | 314 ms | 5 |

Pooled old is 172/7,199 = 2.389221%; new is 0.168849 percentage points lower.
The difference between old repeats is larger than the first old-to-new change.
This short, time-ordered sample cannot prove equivalence, but it provides no
basis for bisecting a degradation specific to the new image.

## Protocol and artifacts

B explicitly granted the bench after stopping its current acquisition.
Executed harness commit `5aa04c1` took three separate holds lasting
115.509/118.217/115.963 seconds, each with by-id/MAC verification, full flash
and complete boot ELF hash verification. Old and new use the same bootloader
and partition image; NVS is preserved. B's old app BIN was regenerated from
its exact original ELF, rather than rebuilt from source. Full hashes:

- Old ELF: `94a5a13ccd5555a8fd39710a140cf514c012d7d7e248554d632915b61ec7d444`.
- New ELF: `9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`.

Each arm replayed the exact same three calibrated prompt WAVs from B strict
attempt-2, with the same long live-test fixed reply text, live STT/TTS, volume
0, cat at 30 fps and one wire show per reply. No per-arm calibration, mixer
writes or movement occurred. Actual cloud TTS waveforms and timing can vary;
their references/events are retained. One old-arm transcript omitted the final
word "France"; this is not an STT or acoustic pass. The fixed reply text is
independent of that transcript's content.

Three fixed 24-second windows per arm began after six seconds of session
settling. Capture remained enabled throughout. `quiet 1` and identical
`arrival-trace=1`/`call-timing=1` controls applied to both images. PERF and trace
exports occurred outside the declared windows, verified against command
timestamps. Separate whole-arm results retain those inter-window operations:
54/3,852 = 1.401869%, 83/3,934 = 2.109812%, and 123/3,843 = 3.200625%.
No packet population or threshold in B's qualification was changed.

Raw evidence is `~/src/evidence/echoear/A/uplink-ab-14284c8-9936da45-1/`:
frozen `plan.json` with 494 input hashes, `arm1..3/` identity/flash/serial logs,
commands, PERF, authenticated RTP, early socket callbacks, device traces,
live bot events and original results. `summary.json`/`summarize.py` audit the
comparison. The bench was released to B immediately after arm 3, with the old
image still installed for B's independent full reflash. No further hold ran.

## Timing boundary

| Arm | Consecutive source send intervals in trace | Source >60 ms | Maximum source gap | Receive reordering pairs |
|---|---:|---:|---:|---:|
| Old | 3,905 | 0 | 51.180 ms | 4 |
| New | 4,029 | 0 | 49.645 ms | 6 |
| Old repeat | 3,907 | 0 | 48.845 ms | 24 |

All 11,841 consecutive source intervals include uninterrupted trace boundary
margins, not only the 24-second windows. Every sequence step is 1 and every
RTP timestamp step is 960. Trace overwrites are zero. All measured receive
interval endpoints match a successful source send by SSRC, sequence and RTP
timestamp. This requires no clock-offset or one-way-latency assumption.

All 252 receive intervals over 60 ms cross the threshold after source
send-return. Ten old-repeat tails have intervening source sends; comparing
only the two packets adjacent at the receiver would mistake skipped or
reordered arrivals for a source stall. The corrected analyzer walks the
intervening source records and preserves a real source gap when present.
Regressions cover both cases. The original conservative analyzer marked
coverage incomplete for reordered pairs and incorrectly labelled seven tails
as before-send. Original `result.json` files remain unchanged; corrected
`analysis-ordered-send.json` files and archived executed sources disclose the
offline correction. Host receive counts, rates, maxima and verdicts are
unchanged.

The active host event-loop probe reports no lag above its 20 ms reporting
threshold in any of the nine windows. This does not exclude shorter stalls or
kernel/driver buffering. Send-return timestamps an API completion rather than
the RF transmission; callback time follows host kernel processing. The experiment cannot separate ESP
lwIP/WiFi buffering, AP/radio effects and host network-driver/kernel delivery.
Per-packet app enqueue and TX-task timestamps are unavailable on these frozen
images. Equal tracing/call timing adds instrumentation cost, and no RF or
kernel packet capture was taken.

Read-only joins of B's retained failures independently found 119/120 strict
and 201/203 offline eligible tails already over 60 ms at the earlier host UDP
callback. Later aiortc receive/authentication processing therefore cannot
explain most of those tails. This corroborates a boundary, not a unique cause.

## Other gates and limits

All arms retained zero clip deltas, failed allocations, raw monitor errors and
cleanup errors. Internal minima were 86,164/86,556/88,100 B; lowest sampled
largest blocks were 49,152/47,104/47,104 B. Existing 81,920/32,768 B floors
were unchanged. New-image transport deltas were zero for all 13 counters in
all windows. Old-repeat window 3 had one drift insertion; old startup
`playout_dropped` was already 2 and 6, respectively, before measurement.
Those observations are retained, not exempted or called zero.

WiFi totals were 1,454,842/1,486,962/1,418,052 B, each below the proportional
20 MB/300 s bound. Wired traffic was recorded without gating. Power save was
off; no host network setting or privilege change occurred. NIC eligibility
does not measure other stations' RF airtime or guarantee steady delivery.
No acoustic qualification, firmware fix or threshold relaxation follows from
this diagnostic. The evidence does not trigger candidate firmware bisection;
any further lower-path probe needs a separately coordinated bench hold.

## Proposed device-side observations after send

These are off-bench source findings and proposed measurements, not a new
network experiment or a configuration change. `dg_net_init()` uses
`WIFI_INIT_CONFIG_DEFAULT()` and returns `esp_wifi_set_ps(WIFI_PS_NONE)`;
`main/main.cpp` checks that return with `ESP_ERROR_CHECK`. This requests no
station power saving. The earlier host power-save readback is a separate
fact; no current **device** `esp_wifi_get_ps()` readback was acquired.
The network initialization body is unchanged from production source9bc2d42.

Allowlisted generated configuration of the reviewed AEC diagnostic build
contains the following. These are build settings, not live queue occupancy
or an independent readback of the frozen9936 image.

| Setting | Configured value |
| --- | --- |
| TX buffers | Static,16 buffers |
| TX cache |32 buffers |
| RX buffers |10 static,32 dynamic |
| AMPDU | TX and RX enabled |
| Block Ack windows | TX6, RX16 |
| WiFi task | Core0 |

The installed ESP-IDF6.1 primary sources are
`components/esp_wifi/Kconfig` and `include/esp_wifi.h`:
static TX buffers are approximately1.6KB each; when unavailable, the configured
cache queues upper-layer packets. This makes buffering after a successful
socket send plausible, but the A/B/A timestamps do not establish that it
caused these delays. AMPDU being configured does not prove actual aggregation
of the measured packets. No buffer count, power-save policy or aggregation
setting should change on this evidence alone.

A future separately reviewed observation could:

1. Read `esp_wifi_get_ps()` once after association and after reconnect, recording
   its return code and enum. Record the actual initialization buffer/AMPDU
   fields and build hash once. Do not call setters or tune the driver.
2. Collect timestamped RSSI, primary/secondary channel and BSSID from
   `esp_wifi_sta_get_ap_info()` at low frequency into a fixed bounded buffer;
   export it after acquisition. Existing PERF already contains RSSI/channel
   and association logs contain their initial values. Record missing reads
   explicitly. Compare tail times with these observations without treating
   RSSI as channel utilization or AP capability as negotiated PHY rate.
3. Retain `wifi-stats` RX/TX/buffer dumps before and after the acquisition,
   outside the scored send window. Its existing implementation calls
   `esp_wifi_statis_dump(WIFI_STATIS_RXTX|WIFI_STATIS_BUFFER)` with a temporary
   logger level. Determine which queue/retry fields the actual retained output
   supplies before declaring a metric available. Avoid per-packet console
   prints and mid-window PERF: prior measurements showed console interference.
4. Join those observations with consecutive device send-return timestamps,
   first host UDP-callback timestamps, sequence/SSRC and event-loop probes,
   preserving loss/reordering and all original thresholds. Any added task or
   sampler needs a resource/observer-cost control before scored use.

Channel occupancy remains **unavailable**: the inspected public station AP
record has no channel-busy/CCA airtime field, and no supported occupancy API
was identified in those headers. A statistics dump may or may not expose a
usable counter; retain its definition, units and reset semantics before using
one. RSSI and throughput cannot substitute for airtime occupancy. This proposal
does not enable scans, promiscuous mode, CSI or packet capture, and makes no
host/AP network changes. No proposed measurement has been run here.

## Implemented opt-in buffered observations; hardware cost unmeasured

`CONFIG_DG_WIFI_OBSERVE` defaults off. Disabled builds have no observer task,
ring allocation, sampler WiFi API calls or registered callback. PERF adds
observation status; `wifi-observe` exports retained records only when explicitly
requested after acquisition. Neither command enables the feature or tunes
network behavior. Existing frozen9936/B attempt3 images are unchanged and
cannot acquire this new telemetry retrospectively.

An enabled image starts a low-priority core0 sampler with4096-byte PSRAM
stack and a256-record PSRAM ring (record size reported in metadata). It calls
`esp_wifi_get_ps()` and `esp_wifi_sta_get_ap_info()` no more often than once
per second, without sampling-time console output. PS enum/API errors, RSSI,
primary/secondary channels, BSSID and device timestamps are retained. API
failure produces explicit errors and null values, including during reconnect;
no last-known RSSI or mode is presented as a fresh read. Each sample carries
read begin/end and prior counter-boundary timestamps, so slow/late samples
and varying observation intervals remain visible. There are no catch-up bursts.
Actual initialization buffer/AMPDU/core fields are copied from the exact
`wifi_init_config_t` passed to `esp_wifi_init`; TX BA window is configured Kconfig.

The pinned ESP32-S3/IDF6.1 native private API `esp_wifi_set_tx_done_cb()`
exposes completion success/failure for transmitted data. Its opt-in callback
only increments lock-free32-bit STA counters. It does not inspect payloads,
log, allocate, obtain timestamps, block or call the driver. Unsigned deltas
handle counter wrap between samples. These are **native completion statuses**,
not retry counts, successful air transmissions proven independently, packet
loss, per-RTP identity or socket-send-to-ACK latency. Driver callback coverage
has not yet been verified on hardware. The feature exclusively owns that
single callback registration, so another future application user must be
coordinated; it must not silently overwrite another owner's observer.
The SDK's PSRAM atomic workaround makes the generic compile-time
`is_always_lock_free` trait false. Counters therefore have explicit aligned
DRAM placement and their internal addresses are validated before registration;
the pinned SDK routes those32-bit operations to its S32C1I hardware path.
External storage is rejected before allocating or registering anything.
Registration/task/allocation errors disable sampling and remain in status,
without changing the network initialization return or retrying registration.
The explicit experimental AEC heap-abort policy still makes exhaustion fatal.
Other SDK/chip versions are rejected at compile time while enabled.

No ESP32-S3 retry counter API was identified. The inspected TX-statistics
getter in `esp_private/esp_wifi_he_private.h` is guarded by
`CONFIG_SOC_WIFI_HE_SUPPORT` and is unavailable on this S3. The public raw
802.11 TX callback does not establish ordinary socket-send-to-air timing;
no supported per-packet air timestamp was identified. Consequently
`tx_retries`, `send_to_air_us` and `channel_occupancy` remain **null**. Native
completion counts must never be substituted for those unavailable metrics.
The existing optional pre/post `wifi-stats` dump remains separate and is
not called by the sampler.

The bounded ring records overwritten count and absolute sequences. Export
snapshots a sequence range, copies records with short critical sections,
paces console records and explicitly errors if an expected row disappeared.
`hil/wifi_observation.py` rejects missing/reordered/duplicate rows, malformed
extent or clocks, fabricated values after API errors and non-null unsupported
metrics. It retains overwrite information and never calls these observations
transport qualification. If none of the callbacks fire, coverage remains
unproven, even if failed-completion counts are zero. Export outside scored
windows: console interference remains a demonstrated confounder.

Host tests compile the actual enabled and disabled implementation. They
exercise registration/cleanup failures, ring overwrites, API failures,
STA-only status counts, unsigned wrap and pacing. Disabled linkage has no
driver/task/heap definitions, proving it introduces none of those dependencies.
The host decoder has independent corruption controls. No sampler hardware
trial, observer-on/off cost comparison or resource qualification has run.
Keep the feature off for existing frozen campaigns; before scored use, measure
heap/stack/CPU and send cadence with observation off/on on matched images.
PS/radio/queue/aggregation settings and all host/AP networking remain unchanged.

Implementation review approved the default-off feature and target counter
placement. Enabled observer, observer-off demo, legacy and AEC/default-off
target builds passed. Nine actual-body C++ tests and eight decoder tests pass;
the reviewer independently reproduced them and the host counterexamples.
The target ELF shows both4-byte counters in aligned internal `.dram0.data`;
linked dispatch reaches hardware S32C1I rather than the external-RAM lock.
The compare/exchange can retry: this is not a wait-free or fixed-cycle claim.
`retained_history_not_overwritten` refers only to ring retention, not
pre-registration/all-packet coverage. No runtime observer cost or coverage
measurement follows from implementation approval. Evidence and review are
under `A/wifi-observe-prep/`; this feature has not been flashed or used in a
scored campaign.

## Frozen observer image for B

The observer-enabled demo image built from exact source
`2a6acb223bc09d4b02ffe04beb3059f73f0592f1` is frozen under
`~/src/evidence/echoear/A/wifi-observer-2a6acb2-enabled-image/`.
It includes ELF/BIN, bootloader, partition table, generated flash arguments,
manifest, allowlisted build flags, decode configuration and the pinned strict
decoder with a standalone raw-log CLI. This is a fresh versioned build:

| Artifact | SHA256 |
|---|---|
| ELF | `81054913f10a7c0d7f97dfe255e9f58ed6242bb982ced1de66a8f13683e339b0` |
| App BIN | `ac8e8f811d5b181178d67668652da12fed398dffb64ce7e2ead379b07f0bb4ec` |
| Manifest | `f201046fe3836cd87f8eca4754ffefd2ef07d3455044fb397c76a9477e3969fa` |

AEC is disabled and demo autostart is enabled. Preserve the generated
DIO/80MHz/16MB flash arguments. The package retains the resolved dependency
lock and its generated AEC-off pruning diff; firmware source matches the
pinned commit. It is a reproducible handoff of inspected artifacts, not a
hermetic SDK/toolchain rebuild claim.

Independent Astra audit verified all20 packaged file hashes, embedded image
identity, pinned source/decoder, internal counter placement and eight decoder
tests. Its report is alongside the package at
`A/wifi-observer-2a6acb2-freeze-prep/astra-bundle-audit.md`.
No hardware was accessed to build or audit this package. B owns observation,
full boot-hash verification and restoration; callback coverage and runtime
heap/stack/CPU/cadence overhead remain unmeasured. Export only after the
acquisition window, preserving raw logs, health and ring-overwrite metadata.

## Decision 138: the requal-n1 uplink gate is the device send cadence

On 2026-10-27, decision 138a changed the requal-n1 uplink gate. The plan revision
`hil/plans/echoear-item2-requalification-n1-d138.json` is derived from the frozen r1 plan, which is unchanged.

**What is gated now:**
- **Network-arrival `link_tail` is report-only.** It is still computed and recorded (rate, extreme rate, hard cap,
  coverage, reset guard), but it no longer gates item1 or item2.
- **S1 gates item2.** For strict_voice_cat, quiet_offline and quiet_live, the gate is the device's own send cadence:
  the same per-packet send trace (kind 12, `uplink_wire`) as the Timing boundary section above.
  - The trace is enabled per session (`peer-option arrival-trace 1`).
  - It is read out with `dump-arrivals` only between scored windows: three drains per original, after the
    pre-prompt PERF, after the reply-end PERF and after the turn.
- **The rule:** 0 eligible send intervals over 60 ms in each scored original, measured from prompt start to reply
  end.
  - Console PERF reads (`CONSOLE_WINDOW` ±120 ms) are excluded and reported.
  - Acknowledged audio-idle windows are excused by containment.
  - Send gaps caused by Wi-Fi back-pressure are not excused.
- **S2 gates item1.** item1 can't run the trace (no-console rule, autostart image), so it gates its session PERF
  counters instead:
  - every uplink drop and error counter is zero;
  - frames encoded but not yet sent stay within the queue depth plus 2;
  - the worst encode-to-send wait is at most 40 ms.

  **These counters cannot see a stall before encoding.** item2 records S2 too, report-only.

**Completeness fails closed.** If any of these fail, the original is not passed and the record carries
`harness_failure`:
- the trace is enabled at both ends;
- one session spans the window;
- no record was overwritten up to the post-reply readout;
- the trace covers both ends of the window;
- every sequence step is 1.

**Threshold rationale (retained traces):**
- **AEC off:** 17 runs from 2026-10-14/15 give 35,520 eligible intervals with none over 60 ms. The maximum is
  52.7 ms and the per-run p99 is 32–44.5 ms. This includes the bad-RF nights, when arrival tails were 1.2–4.9 %.
- **AEC on (step-9):** 0.37 % of intervals fall between 60 and 74 ms, from 32 ms AEC chunking. **The limit applies
  only to the AEC-off line.**

**What S1 adds to the configuration being qualified:**
- the trace, about 300 rows/s into an 8192-row ring, so a readout interval holds about 27 s;
- three drains plus three trace-counter PERF reads per original.

Each original records `source_cadence.trace_cost`: the record rate, maximum pending, overwrite delta, and every
drain's duration and row count.

The r16 smokes answer whether this changes the configuration (`echoear_source_cadence.trace_configuration_comparison`).

**Unchanged:** downlink loss gates (item2 zero; item1 decision-134 D5) and every other gate. The RF stream pre-check
(`hil/echoear_rf_stream_precheck.py`) is a report-only diagnostic and no longer a precondition.

**Decision 138b:** a clean eligibility or RF deferral after scored originals is a continuation point for strict,
quiet_offline, quiet_live, a6_echo_stress and a6_hot_stt.
- It uses `--continue-from`, with the same binding and root, the `continued_from` identity and sha256, and the scored
  originals kept.
- The continuation acquires the remaining originals or arms in a new session.
- a6_analog_probe has one arm, and item1 has no deferral point mid-run.

## Decision 139 (2026-10-27)
The plan revision `echoear-item2-requalification-n1-d139.json` is derived from the d138 revision, which smoke-r16
binds and which is unchanged.
- **(a) item1 S2 is ratified.** item1 claims only that no uplink frame was dropped, or held more than 40 ms, after
  encoding. It does not claim the device send cadence.
- **(b) `excuse_firmware_backpressure: false`.** Every send gap over 60 ms fails S1, including firmware send-budget
  stale-frame skips under Wi-Fi back-pressure.
- **(c) `item1_s2.pcm_queue_drops_max: 1` per item1 session.** Every other S2 counter stays 0.

**Superseded prose.** Under a decision-138/139 plan, older text saying "only a deferral with no scored run is clean"
or "a mid-population deferral is terminal" is superseded by decision 138b. The decision-134 D6 limit of 3 segments
becomes at most `runs` segments, of which at most 2 end in a quiet_live setup failure. The plan's
`decision_138.supersedes` records this.

**Trace-on vs trace-off comparison.** `trace_configuration_comparison` compares per stage and per counter, using
per-window deltas. Incomplete or unmatched inputs give `insufficient_evidence`, and a negative result reads "not
detected in these samples". The trace-off baseline was declared before any trace-on data
(`decision_138.trace_comparison.baseline_trace_off`).

## S1 readout after the r17 smokes (plan revision d139-r2)
In smoke-r17 (6913c4d) the trace ring filled between originals: about 45–60 s at about 276–300 rows/s, with the
downlink's five record kinds per packet streaming even while the uplink was idle. One `dump-arrivals` reads at most
8192 rows, in about 2.1 s, while new rows keep arriving. So about 60 old records were overwritten during the full
pre-prompt dump.

The r1 completeness rule took its overwrite baseline from the pre-prompt PERF. It therefore failed every original
after the first, although no record inside a window was lost.

**Under d139-r2:**
- The pre-prompt readout repeats until pending is at most 512 (at most 4 dumps).
- The overwrite baseline is the snapshot after the last pre-prompt drain, which must precede the window start.
- Sequence contiguity, from the last send before the start to the first send after the end, still catches any
  missing record.

No firmware change was made.
