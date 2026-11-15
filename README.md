# dg-esp32

ESP-IDF v6.1 Pipecat voice client for EchoEar (ESP32-S3) and Waveshare P4-XC
(ESP32-P4, 3.4-inch 800×800). Both share the application, Opus transport,
display engine and simple UI against an unchanged Pipecat bot in `server/`
over SmallWebRTC. The core is vendored pipecat-client-cxx with `esp_peer`.
Retired Waveshare-1.46 remains build-only.

The P4 project targets **smoke parity, not qualification**. See the
[feature/evidence table](docs/p4xc-parity.md), [P4 hardware and limits](docs/p4xc.md),
[board-porting guide](docs/boards.md), [display interface](docs/display.md),
and [step-16 coordinator run book](hil/p4_parity.md). The short near-end
weakness is localized before AEC; acoustic values remain provisional for
final speaker hardware. Delay is labelled measured-diagnostic.

The two-character duo variant has its own [HIL run book](hil/duo.md) and an
[end-to-end routing harness](hil/duo_e2e/README.md); its bot and the bot's
flags are described in [server/README.md](server/README.md).

Current step-16 work is in `dg-esp32-p4-s16`. Host validation is authorized;
all serial/bench work belongs to the coordinator. The step-15 pin prerequisites
have passed, including 21.2176 dB report-only P4 normal AEC ERLE; step-16
campaign coverage remains pending. Both
boards' locked build and pinned by-id flash commands are in [CLAUDE.md](CLAUDE.md).
The earlier EchoEar qualification commands/images below remain historical;
smoke parity does not promote those images.

## Setup

1. Install ESP-IDF v6.1 (already done on this machine at `~/esp/esp-idf-v6.1`):
   ```sh
   git clone --recursive --branch v6.1 https://github.com/espressif/esp-idf.git ~/esp/esp-idf-v6.1
   ~/esp/esp-idf-v6.1/install.sh esp32s3 esp32p4
   ```
2. Activate the environment in each shell: `get_idf` (or `. ~/esp/esp-idf-v6.1/export.sh`).
   `sdkconfig.defaults` retains the S3 target; `DG_BOARD=p4xc scripts/idf.sh`
   selects the independent P4 defaults, config, build directory and lock.
3. Create the gitignored `sdkconfig.local` in the repository root. There is no
   template file; it holds the three `DG configuration` options from
   `main/Kconfig.projbuild`:
   ```
   CONFIG_DG_WIFI_SSID="<network>"
   CONFIG_DG_WIFI_PASSWORD="<password>"
   CONFIG_DG_SERVER_URL="http://<bot-host>:7861"
   ```
   Without an SSID or server URL the firmware builds, boots and logs
   `TRANSPORT SKIPPED: local configuration absent`. Never commit this file or
   copy a resolved `sdkconfig` into evidence.
4. Bot server: `uv sync` in `server/`. The demo bot reads `DEEPGRAM_API_KEY`
   (and `OPENAI_API_KEY` for `DEMO_LLM=openai`) from `../pipecat/.env`, the
   sibling `pipecat` checkout; see [server/README.md](server/README.md).

## Firmware images

The historical EchoEar procedures below describe the original image family.
Use [the locked board commands](CLAUDE.md#common-commands) for current development
and the step-16 run book for the new pins. Both board image families select
half or full duplex in the same bot (`server/demo_bot.py`) from the audio
capability sent in `/start`; this port does not change the bot.

| | AEC-off image (default) | AEC image |
|---|---|---|
| Defaults files | `sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear` | same + `sdkconfig.defaults.demo;sdkconfig.defaults.echoear-aec` |
| Build directory | `build-mute` (or `build/` via plain `idf.py build`) | `build/accept` (keep this name) |
| Duplex | Half duplex: mic gate closed while the bot speaks | Full duplex: ESP-SR AFE AEC on the MIC3 speaker reference |
| Interrupting the bot | Not possible; speak after it stops | Barge-in: speech interrupts the reply |
| Session start | Manual, console `connect` + `audio 1` | Autostart after boot (needs provisioning) |
| Status | Passed mute-03 on 2026-10-26 (image `3eaa629a`) | Qualified (candidate `cbabb650`, campaign q1-aec-v87-01); promotion smoke-04 PASSED on image `69d5d764` |

Mute gate (AEC-off builds, `components/dg_audio/src/gate.cpp`): capture stays
closed from RTVI bot-started until bot-stopped plus a 300 ms tail. If
bot-stopped never arrives, 20 s without audible playback reopens it. The AEC
build does not mute for bot speech: it closes capture only for a 400 ms
reply-start guard after accepted non-silent output, re-armed after 2000 ms of
playback silence.

Details: [docs/echoear-aec.md](docs/echoear-aec.md) ("Two firmware images"),
[docs/echoear-mute-gate.md](docs/echoear-mute-gate.md) (both gates and the mute
gate's firmware history), [docs/echoear.md](docs/echoear.md) (board, display,
standalone boot and provisioning), [docs/echoear-aec-q-scope.md](docs/echoear-aec-q-scope.md)
(what the AEC qualification does and does not claim).

### Build rules for both images

- Build sequentially, never in parallel: both builds share `managed_components/`
  and `dependencies.lock`.
- The AEC-off configure rewrites `dependencies.lock` without the AEC-only
  entries (esp-sr, dl_fft, cjson, esp-dsp). Run `git checkout -- dependencies.lock`
  after a mute build and before the AEC build, or the AEC image's project
  version carries `-dirty`. Never commit the rewritten lockfile.
- After changing any defaults file (including `sdkconfig.local`), delete the
  generated `build-mute/sdkconfig` or `build/config-accept`; a stale one
  overrides the defaults.
- `scripts/idf.sh` refuses `build` combined with `flash`/`monitor`; build first.
- Flash with the same `DG_*` variables you built with (for example
  `DG_DEMO_AUTOSTART=1 DG_PUBLIC_DEMO=1` for `build-public`). A flash re-runs
  CMake with the current selection, so a mismatch would rebuild and flash a
  different image; `idf.sh` now refuses it when the build directory's cached
  `SDKCONFIG_DEFAULTS` differ. This covers this repository's P4 images too
  (`DG_BOARD=p4xc DG_VARIANT=duo` for `build-p4xc-duo`); a P4 build of another
  project is not compared.
- On macOS the boards are found by USB descriptor (`hil/board_port_macos.py`),
  for flash and monitor through `scripts/idf.sh` only. The P4 lookup is
  unit-tested but has not yet been run against the board from a Mac.

### Bot server

Start the bot before connecting a board. From `server/`:

```sh
uv run python demo_bot.py --host 0.0.0.0
```

It serves SmallWebRTC on `SERVER_PORT`, default **7861** (the launchers pass
`--port 7861` explicitly; never use 7860). The bench harnesses also set
`PIPECAT_SCTP_MAX_CHUNK_SIZE=1100`, which keeps data-channel chunks below the
path MTU. Leave `ECHOEAR_AEC_FULL_DUPLEX` unset for both images; `0` forces half
duplex. The default LLM is a local vLLM at `http://127.0.0.1:8080/v1`, model
`gb-phone` (`LOCAL_LLM_URL`, `LOCAL_LLM_MODEL`); `DEMO_LLM=openai` uses GPT-4.1.
STT and TTS are Deepgram Flux. Transcripts appear in the bot log as
`Transcript: user: ...` and `Transcript: assistant: ...`.

The device finds the bot by URL only: `CONFIG_DG_SERVER_URL` from
`sdkconfig.local` (bench host `http://192.168.7.222:7861`). Valid demo settings
in NVS namespace `dg-demo` replace it at boot on either image, and the console
`server-url <url>` command changes it until reboot. The firmware POSTs
`/start` there, then completes the WebRTC offer/answer.

### Flashing and the console

The EchoEar enumerates as `/dev/ttyACM1` on this bench, but TTY numbers change
on replug. The docs and harnesses use only the pinned by-id path:

```sh
PORT=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00
```

On the shared bench, run flash/monitor under `python3 hil/bench.py ...`, which
holds the EchoEar bench flock and fails at once if another run holds it.
`idf.py flash` writes the bootloader, partition table and app; it leaves NVS
and display storage intact; `idf.py erase-flash` wipes them.

Console commands are typed into the monitor as `#<seq> <command> [arg]`, for
example `#1 connect`; replies are `OK #<seq> <command>` or `ERR #<seq> <command>`.
The console is ready after `READY transport-console`. Ctrl-] exits the monitor.

`#<seq> help` describes `uplink-intervals`: it freezes the current session's
measurement permanently. The harness calls `#<seq> uplink-intervals` exactly once,
just before an intentional stop. An unavailable snapshot prints
`UPLINK_INTERVALS_NONE` with a reason; an emitted generation is never repeated.

### AEC-off image (default, half duplex)

Build (after `get_idf`, from the repository root):

```sh
idf.py -B build-mute -D SDKCONFIG="$PWD/build-mute/sdkconfig" \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear' build
git checkout -- dependencies.lock
```

A plain `idf.py build` (or `scripts/idf.sh build`) resolves the same AEC-off
configuration into `build/` and the root `sdkconfig`.

Flash and monitor:

```sh
python3 hil/bench.py idf.py -B build-mute -p "$PORT" flash monitor
```

Start a session from the console once `READY transport-console` appears:

```
#1 connect
#2 audio 1
```

`connect` waits for `STATE Ready`; `audio 1` enables microphone uplink
(`AUDIO_STATE enabled=1`). Optional: `#3 playback-volume -6` (dB, 0 is the top
of the range). `#4 disconnect` ends the session and prints a `PERF` record.

Use: speak first; the bot waits for the user. It answers in one or two short
sentences ("Mini Embedded Pipecat"). While it speaks the mic is muted, so it
cannot be interrupted; talk again after it finishes. The manual image does not
set up the LCD by itself. `hil/demo_session.py` (see
[server/README.md](server/README.md#echoear-recording-demo)) runs the same
session with the animated cat: it owns the bench lock and ports 7861 and 7874,
loads the cat assets, connects, enables audio, and stops everything on Ctrl-C.
It checks the image in `build/`, so use the plain `idf.py build` path for it.

Autostart variant: add `sdkconfig.defaults.demo` before the last entry of the
defaults list, in its own build directory. It is not bench-tested yet;
requalification is pending. It is not the rollback image.

### AEC image (full duplex, autostart)

Build, in exactly this directory (`cmake/display_http_psram.cmake` embeds the
build path in one string, so another name changes the image layout):

```sh
git checkout -- dependencies.lock
idf.py -B build/accept -D SDKCONFIG="$PWD/build/config-accept" \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear;sdkconfig.defaults.demo;sdkconfig.defaults.echoear-aec' build
```

The overlay sets `DG_ECHOEAR_AEC=y`, reference gain 15 dB, guard 400 ms,
re-arm 2000 ms and `DG_WIFI_OBSERVE=y`; changing any of them leaves the
qualified configuration. A rebuild inherits the qualification only when it
differs from `cbabb650` in metadata alone; the proof is
`hil/compare_masked_image.py` plus a promotion smoke
([docs/echoear-aec.md](docs/echoear-aec.md), [docs/compare-masked-image.md](docs/compare-masked-image.md)).

Flash and monitor:

```sh
python3 hil/bench.py idf.py -B build/accept -p "$PORT" flash monitor
```

Start: none needed. After WiFi, the demo worker restores the cat and status
screens from display storage, applies the stored volume (default −6 dB),
connects to the stored URL and opens the mic at bot-ready. Failures retry with
1–30 s backoff. This needs one-time provisioning of the NVS demo settings
(`demo configure`) and the persisted cat/status assets; an unprovisioned board
stays idle. `hil/demo_session.py --provision-only --flash` does both and exits
after the autonomous reboot (it requires an autostart image in `build/`; see
"Standalone boot and provisioning" in [docs/echoear.md](docs/echoear.md)).
Console control: `#1 demo hold` stops capture and the session, `#2 demo run`
resumes, `#3 demo status` prints a `DEMO {...}` record.

Use: the screen shows a dim DISCONNECTED cat, then CONNECTING with moving dots,
then the live cat, whose mouth follows the bot's audio. Speak first. Speaking
over a reply interrupts it ("Stop" or a new question); the bot then answers the
new turn. A double tap on the screen requests a fresh session while one is
live. The qualification covers echo cancellation endpoints only, at volumes
0 and −6 dB, with no claim on STT, transport, resources or sustained double
talk.

### Which image is running

- Every connect logs `AUDIO_CAPABILITY aec=0 ...` (AEC-off) or
  `AUDIO_CAPABILITY aec=1 ref_gain_db=15 guard_ms=400` (AEC).
- The AEC image's boot log contains `MIC3_GAIN=0x15 analog_db=15`.
- The bot logs `DEMO_DUPLEX aec_full_duplex=False user_mute=True ...` for the
  AEC-off image and `aec_full_duplex=True` for the AEC image.
- `#1 stats` prints a `PERF {...}` record; only the AEC image has an `aec`
  block.
- `#1 demo status` reports `"held":true` on a manual image, where
  `#2 demo run` answers `ERR #2 demo`.
- The boot log's app version and ELF SHA-256 identify the exact build.

## Console diagnostics

The `stats` console command emits one `PERF {...}` record with connection
timestamps, frame timing quantiles, task runtime/CPU and stack margins, heap
free/minimum/largest blocks, queue ages/depths, playout counters and WiFi RSSI.
CPU is a percentage of one core. Its first observation and newly created tasks
have a null CPU value; subsequent observations use runtime deltas. Frame
quantiles cover the current session, with 100 µs buckets. Wrapped peer-call
intervals are inclusive wall/cycle measurements, including waits and preemption;
they are nested and must not be added as exclusive CPU costs.
Detailed nested call timing and arrival tracing default off. While the app is
Idle, `peer-option call-timing 1` enables detailed timers for investigation;
`peer-option arrival-trace 1` enables the fixed arrival ring before connection.
Disabled timing fields are null. These instrumentation defaults follow the
user-approved step-8 redirect; no performance tuning knob was adopted.

`dump-arrivals` drains enabled tracing as bounded base64 `ARRIVAL` lines. The
collector decodes socket-read and authenticated RTP, native delivery timestamps
and playout events. Socket timestamps follow WiFi/lwIP queues and cannot identify
an AP or radio fault alone. With tracing disabled the ring receives no records.
`wifi-stats` requests the native diagnostic dump; unavailable retry/RX counters
are explicitly reported as unavailable. Measurement artifacts belong outside
the repository under `../evidence/step8/`.

## History

The sections below record the step-8 tuning and qualification results as
written at the time. They describe earlier images, not the two current
EchoEar images above.

### Step 8 results

Final tuning remains `4097a13`: DIO 80 MHz, VFS socket select, server idle RTP,
24 kbps complexity-0 uplink, 100 ms initial reserve and AUTO DTLS. Changes are
protocol/disconnect fixes and PERF instrumentation; detailed nested timers and
arrival recording default off. QIO reduced measured task CPU about 30%, but
failed quality gates that also fail intermittently on the control. A follow-up
will separate CPU effects from bench network noise before re-testing QIO.

The user confirmed a board/Jabra placement change during the last few hours of
screening. Those acoustic/WiFi results are potentially affected. RSSI hourly
medians shift from −44 to −46 dBm, then −44 and −49 dBm; placement and radio
conditions are not independently controlled. In the fresh bottom-facing soak,
67 timed RSSI snapshots remain between −46 and −44 dBm (median −45). The final
acoustic population below was collected after the board was turned with its
bottom speaker facing the Jabra and a fresh loopback check;
all earlier failed originals and once-only repeats remain in external evidence.
The source prompt and firmware gains are unchanged. Before each acoustic test,
the harness restores and verifies the Step-7 mixer readbacks: PCM 8/11 (73%,
−4 dB), Mic 6/7 (86%, +3 dB), both enabled. Source fixture hashes and playback
identities are retained. Output loudness is a separate follow-up.
No tuning gain is claimed from these small, non-interleaved E2E populations.

The first final-image attempt after original-placement restoration failed reply
correlation in all five offline and all five live originals, and in all ten
once-only repeats. The loop and speech VAD pass; Jabra reception remains 8.5 dB
below Step 7. The unchanged `4097a13` image reproduces the failure (0.446,
then 0.442 on its once-only repeat). This is failed qualification, with no
verified latency population from those turns.

After the board was turned, loopback passes with board tone −32.7 dBFS and
Jabra tone −36.068 dBFS, versus Step-7 −26.2 and about −26.44 dBFS. The fresh
offline and live tests each finish **0/5 originals and 0/5 once-only repeats**,
timing out at the required speech telemetry. Offline has no VAD-stop events;
live raw transcripts are retained but do not satisfy that telemetry gate.
There are no qualified cold or warm final latency samples. The table records
that missing coverage explicitly and uses `final-redirect-v17-bottom/` as the
after population.

Values are median / lower empirical p95 with actual counts. Before: Step-7
final five cold turns per mode; after: the redirect's five original turns per
mode, split by verified peer-session continuity. Repeats are excluded.
Prompt end is host ALSA playback completion; reply onset is acoustic. Negative
stage offsets are possible when a stop/transcript precedes playback completion.
Missing timestamps stay N/A. These replace the skipped 96-turn campaign.

| Prompt end → stage (ms) | Before cold | After cold | After warm |
|---|---:|---:|---:|
| Offline: vad_stop | -219.82 / -206.28 (n=5) | N/A (n=0) | N/A (n=0) |
| Offline: first_fixture_byte | 432.05 / 444.17 (n=5) | N/A (n=0) | N/A (n=0) |
| Offline: audible_reply | 769.03 / 787.55 (n=5) | N/A (n=0) | N/A (n=0) |
| Live: vad_stop | -229.41 / -185.41 (n=5) | N/A (n=0) | N/A (n=0) |
| Live: transcript | -83.86 / -11.05 (n=5) | N/A (n=0) | N/A (n=0) |
| Live: first_tts_byte | 1027.55 / 1104.41 (n=5) | N/A (n=0) | N/A (n=0) |
| Live: audible_reply | 1529.86 / 1562.36 (n=5) | N/A (n=0) | N/A (n=0) |

Session frame histograms below are snapshots, not independent per-turn frame
populations: warm turns share a cumulative session histogram. The outer p95 is
across retained original snapshots, and is distinct from per-frame p95.

| Session codec metric (µs) | Before offline | After offline | Before live | After live |
|---|---:|---:|---:|---:|
| encode_p50_us | 7799.00 / 8599.00 (n=5) | N/A (n=0) | 9599.00 / 9599.00 (n=5) | N/A (n=0) |
| encode_p95_us | 12699.00 / 13599.00 (n=5) | N/A (n=0) | 13299.00 / 13499.00 (n=5) | N/A (n=0) |
| encode_max_us | 18473.00 / 19717.00 (n=5) | N/A (n=0) | 18914.00 / 20090.00 (n=5) | N/A (n=0) |
| decode_p50_us | 1599.00 / 1599.00 (n=5) | N/A (n=0) | 1499.00 / 1599.00 (n=5) | N/A (n=0) |
| decode_p95_us | 2699.00 / 2699.00 (n=5) | N/A (n=0) | 2299.00 / 2299.00 (n=5) | N/A (n=0) |
| decode_max_us | 5352.00 / 5978.00 (n=5) | N/A (n=0) | 2713.00 / 2725.00 (n=5) | N/A (n=0) |
| dtls_duration_us | 804682.00 / 814714.00 (n=5) | N/A (n=0) | 804911.00 / 811547.00 (n=5) | N/A (n=0) |

The first final fault/turn soak completed 1,800.5 s, all 90 prompt/reply cycles
and three recovered faults. It fails the 4 KiB heap-minimum gate: PSRAM decreases
8,876 B (internal/DMA 2,280 B including final disconnect). Stack margins remain
≥25.32%, with no decode or I2S errors. Dropouts: 4 rebuffers, 8 drift drops,
307 PLC frames. The fresh once-only full repeat at the bottom-facing placement
**passes all soak gates**: 1,801.8 s, 90 physical prompt/reply cycles and three
recovered faults; minimum decreases PSRAM 3,324 B, internal/DMA 2,544 B; stacks
≥25.32%, no decode/I2S errors. Its counters are 12 rebuffers, 14 drift drops
and 354 PLC frames. Four fault-overlapping cycles remain marked. Physical
fixture playback does not establish 90 recognized speech turns or restore the
failed E2E qualification. Both full soaks and the interrupted repeat prefix
remain separate.

Detailed dropout attribution, every screened knob and rejection, retained failures,
final fault/turn soak and identities are in [the step-8 report](proj-2026-10-08-0949/codex-jobs/step-8-report.md).

### Step 8d3 correction to the 8d2 pause interpretation

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


The latest step 8d3 user decision keeps **Pipecat SmartTurn defaults (3 s)**
until strong human testing justifies a change. Automated selection is withdrawn;
the 96-turn pause comparison and user-stopped eight-turn repeat are informational
only. Production uplink returns to **20 ms** with `DG_OPUS_UPLINK_FRAME40=n`,
retaining the 32768-byte TX stack and other 8c optimizations. See
[the step 8d3 report](proj-2026-10-08-0949/codex-jobs/step-8d3-report.md)
for image-specific results and qualification limits. Production VAD already
matches Pipecat 1.16.0 defaults: confidence 0.7, start/stop 0.2 s, volume floor
0.6. The explicit speaking-state mute and firmware gate remain.

Final 20 ms qualification retains nominal **8/10**, quiet **4/5**, and
fault-free soak **47/60** strict speech passes. The 1200.511 s soak passes every
corrected resource gate; speech failures keep its overall result failed.
Offline e2e is 5/5 original trials (the wrapper's evidence-path failure is
reconciled separately), and both 120 s silence checks pass at current defaults.


Agent/e2e scoring checks content, presence and coarse level only. Its Jabra
level check uses ≥10 dB room rise and ±7 dB around the per-level 8b3 midpoint.
A baseline above −50 dBFS makes acoustic timing and room-rise evidence
unavailable; correlation, decoded PCM content and calibrated level remain gated.
E2E/agent reply correlations are not an independent guarantee against every
clipping fault. In `LOUDNESS`, `lookahead_ms` is deprecated; use
`limiter_attack_ms` and `limiter_delay_ms`.


Step 8f keeps `event_wake` off. Six paired boots per stratum improved
successful device-send p99 by 12.52 ms in speech and 11.29 ms in silence,
but owner CPU increased 2.55/3.20 percentage points against the predeclared
2-point limit. CPU averages two snapshots within each boot; cadence p99 is
computed over the full 41 s stratum span. The adopted p99 ≤45 ms target is a
regression guard. Raw cadence is primary; the 60 ms device-send ceiling and
40 ms ordinary-owner ceiling apply after declared fault/reconnect and
`link-backpressure` categories. Backpressure uses kind-11 send_audio/dtls_write/control SlowCalls ≥40 ms
(with `call-timing=1`), plus device-stamped kind-7 `send_cutoff` drops padded
±250 ms and corroborated by same-session PERF counter increments. Counters
never excuse a whole bracket. `control` includes local event/heap work and is
not purely link evidence. Interval count, union excused seconds and maximum
accompany raw results. `stats`/`wifi-stats`
±120 ms windows are informational; dump commands create no windows.
Link arrival gaps require >60 ms ≤1%, >150 ms ≤0.05%, and no gap >2 s.

The quiet-NIC gate measures host `wlp8s0` TX and RX, each ≤20,000,000 bytes
per 300 s (scaled). For the Ethernet/STUN bench, board media uses `enp7s0`
and the board's mapped address `192.168.42.93`; owned WiFi board-class byte
counts are zero, so the gate is total host WiFi traffic. Direct-WiFi arms
subtract owned board-class bytes in each direction. Impairment arms with no
bytes in an owned impaired direction are invalid. The proposed Ethernet-media
gate would account both directions on `enp7s0`, match the mapped board endpoint
and selected UDP ports, and subtract that flow from Ethernet totals; this step
does not apply that gate or change routing/interface settings.

The historical 120 KB heap heuristic is superseded by four adopted LCD gates:
active-media owner-loop internal window minimum ≥98,304 B (96 KiB),
boot-cumulative internal minimum ≥81,920 B (80 KiB) after a forced-reconnect
soak, owner-sampled largest internal block ≥32,768 B (32 KiB), and zero failed
internal allocations. Durable PERF windows must be continuous. DMA values
accompany internal values and overlap them. These KiB floors are intentionally
2,304/1,920 B stricter than the review's original decimal 96,000/80,000 B floors.
The floors already allow for display allocations; the post-allocation
measurement must still reach them. Sampling does not observe every allocation
event. Generic/DMA allocation failures conservatively count as internal,
including a benign first failure in a future internal-then-PSRAM fallback.

The conservative step-8f minima, including retained interrupted evidence,
set the step-9 allocation budget (KiB = 1,024 bytes):

| Internal basis | Baseline | Adopted floor | Whole display-stack budget | After 20 KiB staging |
|---|---:|---:|---:|---:|
| Active-session window | 127,087 B | 98,304 B | 28,783 B | 8,303 B left |
| Boot-cumulative | 109,028 B | 81,920 B | **27,108 B** | **6,628 B left** |
| Largest sampled block | 69,632 B | 32,768 B | Remeasure fragmentation | Remeasure fragmentation |

Step 9's whole display stack (staging, panel/SPI driver objects and
LVGL/compositor objects) must cost **≤27,108 B of internal memory**, measured
by the drop in the boot-cumulative minimum on the production image.
Budget staging at **20 KiB (20,480 B)**, leaving 6,628 B for the remaining
objects. Artwork lives in flash/PSRAM. Repeat all four gates with the actual
stack allocated and measure LCD refresh/XIP/PSRAM-bus contention.

Step 8f2 retains the original step-8f tails and reclassifies them with device
evidence. Heap free minima stay per iteration; largest internal/PSRAM blocks
are sampled by the owner at 1 Hz with a `heap_sample` PERF part, and the S3 DMA
largest column is identical to internal by construction, not independently
measured. Runtime log levels use IDF's bounded `esp_log_level_get_timeout`
before deferral and suppress `NONE`. The diagnostic queue holds 128 × 192 B in PSRAM to preserve `quiet 0`
MIC/GATE attribution. Drift inserts and latency trims are `edited`; decoded
content uses piecewise alignment anchored on the whole reply. Acoustic STT mismatch is informational when
decoded and board correlations pass. The forced reconnect occurs after six
minutes so the first generation supplies a settled window.
See [the step-8f2 report](proj-2026-10-08-0949/codex-jobs/step-8f2-report.md)
for measured qualification outcomes and the historical board-flow audit.

Design backlog: measure `MSG_DONTWAIT` on the RTP socket under TX
backpressure. A blocking `sendto` can hold the owner for ~40 ms per call;
an immediate ENOMEM could use the existing send budget. Do not adopt it
without measuring device cadence and loss behaviour.

Step 8f3 fixes matcher pauses, localises backpressure excusals, records commanded
audio idle, and predeclares the −22 reply window at 71–85 dB. Voice qualification
adds a connected 180 s memory tail and checks pooled fit feasibility. Media
fault-injection arms use direct WiFi without `ethernet_bench`; nominated ICE
pairs must match the impaired interface, with a 50%-of-RTP byte gate or a
100,000-byte/minute fallback. See `hil/README.md` and the step-8f3 report.
