# Waveshare ESP32-P4-WIFI6-Touch-LCD-XC

The product port shares the application, Opus transport, AEC, display engine
and simple UI with EchoEar. The supported panel is the **3.4-inch 800×800**
variant. The current campaign is **smoke parity, not qualification**; current
coverage is in [p4xc-parity.md](p4xc-parity.md), with exact coordinator commands
in [Step 16 parity run](../hil/p4_parity.md). Step-16 source preparation does
not establish a hardware pass. The older sections below retain the bring-up
history and its then-pending labels; this overview supersedes those labels.

## Current platform and pins

| Item | Product configuration |
|---|---|
| Silicon | ESP32-P4NRW32, physical rev 1.3; 360 MHz |
| Memory | 32 MB flash, QIO 80 MHz; 32 MB hex PSRAM at 200 MHz |
| Revision Kconfig | `ESP32P4_SELECTS_REV_LESS_V3=y`, `ESP32P4_REV_MIN_100=y` |
| WiFi | ESP32-C6-MINI-1 through esp_hosted/SDIO and WiFi remote; P4 has no radio |
| C6 link | CLK18/CMD19/D0–D3 14–17; reset GPIO54 belongs to esp_hosted |
| Audio codec | esp_audio_codec **2.5.0**; 2.6+ excludes this P4 revision; S3 retains 2.6.2 |
| Audio hardware | I2C SDA7/SCL8; ES8311 0x18, ES7210 0x40; PA GPIO53; MIC3 electrical DAC reference |
| Panel | JD9365, two 1500 Mbps DSI lanes, RGB565 PSRAM framebuffer; DPI 40 MHz / 54.112554 Hz |
| Display geometry | Shared 360×360 logical canvas, 2× to centered 720×720 with 40 px borders |
| Touch | GT911 polling, no wired IRQ; shared reset GPIO27; 10 ms bounded bus operations |
| Power | USB 5 V; no battery/fuel gauge; power reports unavailable |
| Console | UART0 TX37/RX38, CH343P; application 2,000,000 baud; ROM 115,200 baud |
| USB identity | `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00` |
| eFuse MAC | `e8:f6:0a:e0:b8:76` |

esp_hosted owns C6 reset and the host/slave link. Do not toggle GPIO54 in the
board backend or write C6 firmware as a host-port workaround. Unsolicited
C6-reset recovery remains report-only; a reset in a live part leaves that part
inconclusive rather than manufacturing a pass. Any approved C6 update needs
its own host/slave backup/rollback decision.

## Current AEC labels and limitations

| Setting | Value | Evidence label |
|---|---:|---|
| Ref gain | 30 dB | selected-single-trial, predeclared sweep rule |
| Reply-start guard / re-arm | 400 / 2000 ms | provisional; event detection only; physical window validation deferred |
| AFE delay | 1024 samples at 16 kHz | measured-diagnostic, internal reference → AEC output |

Gain, guard, double-talk and near-end level findings are **provisional: bench
speaker not final hardware**. The closing decision accepts the diagnostic
delay from three V1 correlations at r ≥0.99998 and waives the ambiguous near-end
locator only for this value. It does not relax future measurement gates.
V2/re-arm tooling is parked until final speaker hardware. These labels are
authenticated by `p4_aec_close.py`; old recommendation provenance is retained
without re-labelling the original measurements.

The P4 short near-end weakness is localized **upstream of AEC in raw MIC1**.
The step-15d measurement found raw word/phrase losses relative to S3; AEC and
codec/delivery added no localized loss. It does not establish a firmware fix.
The proposed raised-board placement test was dropped under the closing
decision because the speaker is not final. Re-measure on final hardware.
Sources: [near-end measurement](../../dg-esp32/proj-2026-10-30-2143/codex-jobs/measurement-2026-11-03-p4-nearend-localization.md)
and [closing decision](../../dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-close-15-and-pin.md).

## Product build and flash lifecycle

Build only after bench-free permission, in the working tree intended for the
change. `scripts/idf.sh` sources IDF 6.1 and serializes the build. Examples:

```bash
DG_BOARD=p4xc DG_AEC=0 DG_DEMO_AUTOSTART=0 scripts/idf.sh build
DG_BOARD=p4xc DG_AEC=0 DG_DEMO_AUTOSTART=1 scripts/idf.sh build
DG_BOARD=p4xc DG_AEC=1 DG_DEMO_AUTOSTART=0 scripts/idf.sh build
DG_BOARD=p4xc DG_AEC=1 DG_DEMO_AUTOSTART=1 scripts/idf.sh build
```

They use separate `build-p4xc`, `build-p4xc-demo`, `build-p4xc-aec` and
`build-p4xc-aec-demo` directories and `dependencies.p4xc.lock`. Fresh-config
changes must regenerate the selected generated config and pass public-key
checks; never inspect/copy private config values. The AEC image gate uses the
less-v3 IDF6 archive. Static retention floors and runtime stack/heap checks
are distinct. See [board gates](boards.md).

Coordinator-only flash/monitor, after separately building and arranging the
ordinary R7 restore hold, must name an absolute already-built directory:

```bash
DG_BOARD=p4xc python3 hil/bench.py scripts/idf.sh -B "$PWD/build-p4xc" \
  -p /dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00 flash monitor
```

That raw command is a developer entry, not a replacement for a product runner's
signal-safe restore lifecycle. Parity acquisition uses the plan-bound runners
in [the run book](../hil/p4_parity.md) and the **existing step-15 bundles**,
without rebuilding them in the new worktree. The new normal restore is bound
to commit `52482495d1b859b5313cd8c76e4cd0cdd97ea507`; record SHA256
`a2c846832b43636fc73564d6a1d32c22ebcbc95458c8906de0d0550f028d653b`.
Pin creation does not prove boot. Pin verification must pass before step 16.

## Historical bring-up record

The supported panel is the user-confirmed 3.4-inch 800x800 variant. Step 1a uses
independent audio/console and network spike projects, not product firmware.
See [the coordinator run book](../spikes/p4_probe/README.md) for exact commands.

## Product console implementation (step 6)

UART dump pacing budgets each record at `bytes/(0.8*(baud/10))` and rounds up
to whole FreeRTOS ticks. At 2 Mbaud and 1 kHz ticks, a 528-byte line budgets
3.3 ms but sleeps 4 ticks: its realised ceiling is about 132 KB/s, or **0.66 R**,
before formatting/hash/scheduling overhead. This conservative tick rounding is
intentional; it is not a new throughput measurement. The UART host deadline
is `wire_bytes/(0.8*measured_R)+30 s`. USB-JTAG instead uses the expected PCM
record count times 8 ms (AEC) or 4 ms (mic/RX), plus 30 s; a physical USB link
rate cannot describe that producer pacing. The verifier derives PCM record
counts from BEGIN, and uses an explicit count or the 512-batch bound for arrivals.

The UART drain uses FIFO writes without a TX driver ring and sleeps on a
250-us retry timer when needed. `uart_tx_chars` does not wait for FIFO space,
but IDF still takes its TX mutex: a concurrent direct VFS/stdio write can hold
that mutex while filling the FIFO. Future send-gap measurements must include
this contention rather than interpreting the attempt as strictly zero-wait.
UART command acknowledgements now use two reserved 128-byte slots, independent
of the 64-KiB best-effort log/bulk ring. Only the console owner posts them; it
consumes no next command while an ACK is pending. Two slots allow an early OK
followed by ERR from one command. Posting is a bounded copy under the recursive stdout admission lock; it never
sends or sleeps there or takes an audio lock. Each ACK carries the admission
position of the preceding response records, so PERF/dump output stays before
its ACK. The drain task retires those earlier records first (each retains its
existing one-second deadline), gives the ACK priority over later records, polls
an idle UART queue once per tick, and retains partial ACK progress across stalls. It never drops
an admitted ACK on the log deadline. A stalled UART pauses command admission;
other tasks continue and the drain task yields on its existing retry timer.
USB-JTAG keeps its original ACK bytes and best-effort admission path.

All evidence export framing, CHECKs and trailers now use UART wire-rate pacing.
AEC/MIC/RX/ARRIVALS CHECK bytes and existing USB PCM delays are unchanged.
Netprobe's previously unpaced rows now wait 4 ms on USB (same wire bytes), or
`bytes/(0.8*R)` rounded up to ticks on UART. UART netprobe additionally emits a
`NETPROBE_DUMP CHECK` SHA256/count line; the coordinator verifies it and compares
the decoded record count against received + send_errors - overwritten. END or
an ACK alone cannot validate a dump. UART `PERF.console_output.drops` sums
`full` and `transport_drops`; `ack_pending` reports the reserved slots. The
compact `console-drops` command supports timeout diagnostics without a full PERF.

RX FIFO overflow, buffer-full, frame-error and parity-error event counts are
reported under `PERF.console_output.rx`; they count observed driver events,
not an exact number of lost bytes. UART has no host-reader-presence signal.

The product heap classifier now mirrors IDF's revision selection of
`SOC_ROM_STACK_START` versus `SOC_ROM_STACK_START_REV2` and the complete recycled
L2 startup-data range (through `SOC_DRAM_HIGH - CONFIG_CACHE_L2_CACHE_SIZE` on
rev<3, through `SOC_DRAM_HIGH` on rev>=3). Host tests cover both revisions and
the old `0x4ff40000` boundary. The coordinator still needs to inspect the first
real product `REGION` records and bind the observed starts in tests; synthetic
classification tests cannot establish the actual reservation/heap splits.
The P4 descriptor must use `dg_board::console_kind`, as the existing S3
descriptors do, so the console choice and compile-time target check stay shared.

Step 8 carry-over: run the unchanged frozen AEC parser and new CHECK verifier
on one recorded S3 CHECK-bearing export. Host compatibility currently uses a
compiled checksum fixture plus a source-literal equivalence check; it is not
evidence of a hardware transfer.

## Headless product image (step 12a)

At step 12a, `DG_BOARD=p4xc scripts/idf.sh build` selected the null audio/display backends,
IDF 6.1's in-tree Wi-Fi remote API and hosted 2.12.4. The P4 lock pins audio
codec 2.5.0; the peer manifest explicitly retains 2.6.2 on S3. The factory C6
version RPC is logged as unavailable on failure, without treating zero-filled
version fields as an identity or blocking WiFi initialization. Hosted owns
GPIO54; the board backend never drives it.

The null mic produces 320 silence samples every 20 ms at 16 kHz, preserving the
normal Opus/SDP path. Early callers wait on absolute deadlines; late callers
receive a frame immediately and restart the grid there, without a catch-up
burst or an extra 20-ms skip. Playback consumes PCM using a software sink clock, with
no I2S channels. The null display initializes its logical runtime before the
transport console is ready so manual sessions can advertise `display-ready`.
It has no scan-out fence. S3-only clock/TE/SPI/DMA stress diagnostics return
`unsupported board=p4xc cmd=<action>` before runtime-state checks (including
`disp init`, since headless boot already initializes the runtime). Touch remains
an unsupported stub described as Polled; real polling arrives in step 13b.

UART driver-backed RX is installed before `DG_BOOT` and `READY`, at the overlay's
2,000,000 baud. Boot at that baud, WiFi join, product network timing, idle/session
heap and stack floors, and the headless S3/P4 wire pair require coordinator
hardware evidence. Build/host evidence does not claim these observations.

## Product audio (step 12b, remaining hardware checks pending)

`DG_BOARD=p4xc scripts/idf.sh build` now selects the shared ES8311/ES7210
backend. Null audio remains independently host-tested. Display stays null.
The table uses I2C 7/8, ES8311 at 0x18, ES7210 at 0x40, PA GPIO53 high = on,
no rail, RX GPIO13/12/10/11 and TX GPIO9. RX owns external 256fs MCLK;
paired TX uses internal 512fs with no MCLK pin, data16 in slot32. Capture
stays stereo32 TDM with AEC **off**: MIC1=w0 high, MIC2=w1 high,
MIC3=w0 low electrical DAC reference, MIC4 deliberately unselected/returned
as 0. The spike measured a small nonzero MIC4 floor with mask 0xF; product
mask 7 is a policy choice. Reconfirm MIC3 w0-low under mask 7 in step 15.
RX clock checks and overflow/completion instrumentation remain active.
Startup requires REG0D=0x01, independently table-pinned REG44=0x58 and
MIC1=0x1a. PA-gain 0 follows the spike's 5 V / 3.3 V vendor calculation;
requested DAC −6 dB becomes register 0xba (−2.5 dB), **+6 dB in register
terms** versus S3's 0xae (−8.5 dB). PA gain 0 is a vendor default, not a
measured P4 PA gain. Voice smoke first requires a non-empty P4 clean band.

| P4 setting | Authoritative source (resolved value in bundle metadata) | Measurement status |
|---|---:|---|
| MIC1 analog | P4 `AudioHw.registers.analog_gain_db` | EchoEar placeholder |
| Digital mic gain | P4 overlay `CONFIG_DG_MIC_GAIN_PERCENT` | EchoEar placeholder |
| Playback startup seed | peer Kconfig P4 `DG_PLAYBACK_SEED_DBFS_X10` branch | EchoEar placeholder |
| Peer encoder stack, AEC off | peer Kconfig P4 `DG_AUDIO_TX_STACK_BYTES` branch | EchoEar placeholder |
| Tone THD / acoustic level band at −6 dB | 6–30% at 800–3000 Hz | Saved run, post-hoc P4 decision below |
| Standard-prompt native level and received-uplink STT | pending | Coordinator run required |
| Live Opus encode/decode wall cost | pending | Standalone Facts-B cycles do not establish session cost |
| Headless voice smoke / full product wire pair | pending | Host tests/builds do not establish hardware parity |

Regenerate the selected build's generated config for every P4 bench image.
The bundler also re-resolves current audio/peer Kconfig to reject stale seed/stack defaults. `scripts/p4_bundle.py` records
public overlay values and resolved audio settings in the bundle manifest;
the coordinator helper checks mic-gain-status and playback level after boot.
Seed and stack stay out of the overlay so AEC/frame40 defaults take priority.
The predeclared selection rules, P4 fixture grid, exact coordinator commands,
fixed timing bounds, R7 restore policy, live cost export and owed S3 console
check are in [hil/p4_audio.md](../hil/p4_audio.md). R8 requires internal
minimum ≥ 81920 B, largest ≥ 32768 B and zero failed allocations; all four
session workers must retain ≥ 4096 B free stack. Final measured defaults and
links to retained evidence belong in a follow-up fix round after those runs.
P4 advertises simple-UI support for the future panel/demo path; the current
normal image continues using the null display. With cache counters compiled
in, P4's unsupported cache backend produces JSON null in PERF.

### P4 clean-band decision and 500 Hz speaker characteristic

The user's [2026-11-01 decision][P4BANDDEC] excludes 500 Hz and treats each
chain's tone as unmeasurable below 10 dB fundamental-to-noise margin (exactly
10 dB remains measurable). Other distortion/clip limits are unchanged.
The contiguous band starts at the lowest all-pass-or-unmeasurable amplitude
with a measurable pass at every retained frequency. Applied offline to the
[original coordinator acquisition][P4ACOUSTIC], the band is **6–30% for
800/1000/1500/3000 Hz**. This is explicitly **post-hoc and P4 only**; it does
not change S3 gates, select final defaults or establish live voice parity.
The [decision reanalysis][P4REANALYSIS] retains every classification and source
hash; original files were not edited. The original gate remains available,
while `voice-smoke --acoustic-reanalysis` accepts this decision-bound result
only for the same ELF with a clean original restore and matching input hashes.

At relative playback −6 dB, 500 Hz shows increasing measured acoustic H2 with
level. The table gives corrected H2 dBc ranges over the two original repeats,
rounded to 0.01 dB; these are receiver measurements, not a listening judgement.
Jabra H2 already exceeds the unchanged −26 dBc limit at 8%, with 28.01–28.89 dB
chain SNR. At higher amplitudes both chains show pronounced H2:

| Tone amplitude | P4 MIC1 corrected H2, dBc | Jabra corrected H2, dBc |
|---:|---:|---:|
|8% |−30.06 to −28.82 |−25.39 to −23.13 |
|10% |−28.94 to −28.19 |−22.66 to −21.36 |
|15% |−22.47 to −19.53 |−18.68 to −18.67 |
|20% |−19.58 to −19.10 |−16.04 to −16.00 |
|26% |−18.93 to −18.60 |−14.32 to −14.10 |
|30% |−17.00 to −16.97 |−13.39 to −13.14 |

Evidence: [saved acoustic JSON][P4ACOUSTIC], `points` at `hz=500`, each chain's
`corrected_harmonics_dbc.H2` and `fundamental_dbfs - noise_dbfs`. The electrical
reference remains separately characterized in Facts B; this table reports the
speaker/receiver acoustic characteristic. P4 defaults are unchanged.

[P4BANDDEC]: /home/khkramer/src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-01-p4-clean-band.md
[P4ACOUSTIC]: /home/khkramer/src/evidence/echoear/p4/step-12b/fix-1/coordinator-acoustic/acoustic.json
[P4REANALYSIS]: /home/khkramer/src/evidence/echoear/p4/step-12b/fix-1/reanalysis-decision-2026-11-01.json

## Product display (step 13a, writer builds/tests; hardware checks pending)

The product selects JD9365 2.0.2 with the spike's 800×800 / 3.4-inch command
table, two 1,500 Mbps DSI lanes, PHY LDO 3 at 2,500 mV and one RGB565 PSRAM
framebuffer. DPI is fixed at **40 MHz / 54.112554 Hz**, the lowest completed
≥50 Hz point in Facts B. This is not an underrun-free result: the spike saw an
underrun at this clock. The 500 ms bounded DCS read wrapper and linker root
are retained after the 38 MHz read-ID hang. GPIO27 remains the shared
LCD/Goodix reset. It runs before touch probing. The board mutex now also
serializes touch probes/reads with any later reset; the 13b driver re-probes
after a reset. Codec transactions retain IDF transaction locking. See the
product touch section below. No extra DCS SWRESET is added.

The 360×360 logical canvas is replicated 2× into a centered 720×720 rectangle
with four 40-pixel black borders, through LogicalAdapter alone. Backend copy
converts BE RGB565 source bytes into LE framebuffer pixels and writes cache
back before commit. Frame-complete counters provide a separate scan fence;
wire admission/render completion semantics are unchanged. Details and bounded
history policy are in [display.md](display.md); the coordinator recipe and
latency/wire helper are in [p4_display.md](../hil/p4_display.md).

Host tests, P4 normal/demo builds and the S3/Waveshare build matrix pass after
the coordinator granted build permission. S3 config fingerprints match step 12b. No step-13a hardware boot, optical border, load/show or latency
histogram has been acquired by this writer. S3 behavioural smokes remain owed.
Tearing is **unverified** until the user positions the webcam. Step 13b adds
touch and the contention measurement. The handler keeps the measured spike
placement (`LCD_DSI_ISR_HANDLER_IN_IRAM=y`, `CACHE_SAFE=n`). The static
panel/history and init-command array originally cost **17,632 B internal RAM**.
Fix 2 moves this state to external BSS: it must not consume the pre-main retention
heap. Budget the remaining internal pointer/guard/driver allocations
when remeasuring the 13b internal minimum ≥81,920 B and largest ≥32,768 B.
The display helper reports superseded fraction and `underrun happens` line
count as baseline diagnostics; neither is proof of tear-free presentation.

## Demo, liveness and restore tooling (step 14; hardware pending)

The board descriptor supplies `has_battery=false`; `POWER`/`PERF.power`
now explicitly report `available=0`. No battery worker runs on P4, and the
simple demo's three screen states have no low-battery transition. Demo
autostart continues to use the existing board-neutral settings/assets path.
No new board conditional or Kconfig input is introduced.

[The step-14 run book](../hil/p4_liveness.md) provides the full demo/manual
smokes through `p4_step14.py` → `p4_product.py` → `board_smoke_bounds.py`,
with bot-content calibration and fixed measured timing bounds. It also fixes
F18/F19/F20 bounds for both P4 and the S3 AEC-off demo of record `5bbd868a`.
The new metadata-only bot observer runs unchanged `server/demo_bot.py`, records
raw ping/pong receipt, and sends diagnostic old-epoch display probes that must
be rejected. Generation/sequence, WiFi and bot recovery, twenty sessions,
heap decline, hold/resume/cold assets, and six-minute idle are scored separately
from qualification. Host-only evidence is not a live pass.

The initial step-14 dirty-tree build produced the following **historical pin**;
fix round 1 requires a clean-commit rebuild and a new pin before acquisition.
No pin is redone in this round. Historical normal ELF:
`f60a960a1f3142cb67965dde2b5013bbced85a9b9cfe480cc0d45d2fc6b72ca2`, with
record `/home/khkramer/src/evidence/echoear/p4/step-14/restore-of-record.json`
at SHA256 `47431e0bdb29e64a6dec897421f009eeaaf06be459067a82e0535bca2f61b18c`.
The pin includes the normal ELF/manifest,
P4 target lock, ELF-bound headroom report and checked partition table hashes.
The canonical step-14 entrypoint requires the record and its explicit SHA256
before any P4 flash. Success, failure and abort all restore that normal image;
factory remains explicit deep recovery. A pinned normal boot failure selects
factory recovery in the retained runner; SIGUSR1 resumes factory, never another
attempt at the unbootable pin. The new one-shot boot-proof command uses a
factory-backed failure hold before any acquisition relies on the new pin.
After verified normal flash, the
adapter erases only the named `storage` partition and verifies its full FF
readback before boot. Storage is empty after restore; re-provision assets for
the next demo acquisition. NVS persists; in-run cold proof resets preserve
assets. Restore failures retain the inherited bench lock/retry loop.

The new smoke adapter requires non-negated Paris/Rome in qualification mode;
calibration retains content findings as report-only. Contention reports now
expose a top-level `capture_overflows_summary` and `all_checks_measured`, so a
dead S3 reference counter cannot appear to prove zero capture overflows.
Use the step-14 entrypoint for those summaries; the frozen direct entrypoints
retain their historical behavior.

The full host suite and R4 gate pass. Fresh P4 normal/demo builds and bundles
pass the headroom gate at 116,160 B (1,232 B above the declared floor).
The demo ELF is
`7a71ee8cef7096845ca2c4506bc696853f81b4bc2be161a8216cfba4dbe3b8d0`.
All live checks, normal-image restore boot proof and the S3 regression smokes
remain coordinator-owned; the writer report records the completed build matrix.
The four S3 variants and build-only Waveshare variant also pass, with all four
S3 keydiffs empty against step 13b and both S3 AEC placement checks passing.

The step-14 reference-leg decision makes demo wire timing report-only within
the liveness campaign, while structural wire checks remain mandatory. Added
PERF sampling occurs after interval freeze and display epoch comes from the
bot observer. S3 reference heap floors/decline are report-only; P4 gates them.
Unsolicited C6-reset interruptions produce an explicit inconclusive report,
without claiming behaviour passed; routine boot slave resets do not match.
Settled heap uses bounded multi-sample convergence. The run book carries the
exact boot-proof, factory/SIGUSR1 and clean-commit pin ordering, plus the step-16
requirement to use the content/summary adapters.

Fix round 2 drives the real frozen restore loop with fake device calls for both
successful and failed acquisition starts. A pinned restore boot failure clears
the pin and forces `guard.success=false`; SIGUSR1 reaches factory write, verify
and boot proof before the hold returns. Product preflight verifies the factory
backup pair, and liveness accepts `--factory-backup` with the pinned default.
Transient pre-boot IO errors retain the pin for retry. `restore.json` records the
actual final action; a factory action is not product restore proof.

The required pre-campaign sequence is candidate-path boot proof, first normal
acquisition with a clean `p4-product-of-record` restore, then the short recorded
held-idle settle check. Inconclusive C6 results now retain `error`, require reset
evidence before the failure in its current timestamp/cursor window, and exit 2
to stop scripted chains. GPIO slave resets are excluded only during recorded
boot windows. Each S3 settled point carries its convergence flag. No new pin or
hardware validation is produced by these host-side changes.

## Bring-up evidence

All hardware observations below come from coordinator evidence; the writer
only read saved files. Links name the exact evidence files, with JSON keys in
the evidence column where useful. Rounded values are identified or accompanied
by the source precision. Unavailable measurements are stated explicitly.

## Bring-up facts A

| Fact | Measured value / result | Evidence |
|---|---|---|
| P4 eFuse MAC, chip revision and flash ID | MAC `e8:f6:0a:e0:b8:76`; ESP32-P4 revision v1.3; flash manufacturer `c8` (GD), device `4019`, detected size 32 MB. | [Factory flash identity][FID]; [read-mac][MAC] |
| Factory backup pair size and SHA256 | `factory-a.bin` and `factory-b.bin`: 33,554,432 bytes each; both SHA256 `37db1cc6fcc1da5956d76dfb4100cff5a06b1112bbda862b6c4b98c644bacdcd`. | [Factory size/hash manifest][BACKUP] |
| Probe boot, verified factory restore and factory banner | Probe reaches `P4_PROBE boot`, `READY`, first command/OK. Restore proof PASS: write 196.522 s, separate verify 70.831 s, digest matched, hard-reset factory banner `Project name:     esp_brookesia_demo`. These are wrapper stage timings, not the shorter esptool write-only time. | [Restore-proof probe serial][RPS]; [restore-proof JSON][RP]; [restore proof log][RESTORE] |
| Automatic rollback after the earlier network failure | PASS: factory write 190.473 s, verify 70.799 s; digest matched and factory banner witnessed. | [Original network failure and rollback log][ROLLBACK] |
| First command after READY (single attempt) | First `uptime` succeeds with `UPTIME us=269412`, followed by `OK`; no warm-up command/retry. Console `READY ... uptime_us=106599`; restore-proof run independently has the same successful first-command uptime. The driver-backed RX/READY change resolves the observed first-line loss for these runs. | [Console JSON][C], `first_command_uptime_us`; [console serial][CS]; [restore-proof JSON][RP]; [single-attempt implementation](../spikes/p4_probe/host/console_rate.py) |
| Cold-reset numbered BOOT lines lost | 0 lost out of 8 expected (`BOOT 0` through `BOOT 7`), READY observed, in both console and restore-proof runs. This loss oracle covers numbered driver records, not unnumbered ROM output. | [Console JSON][C], `cold_reset`; [restore-proof JSON][RP], `cold_reset`; [console serial][CS] |
| 2,000,000 baud: loaded flood and four-channel dump | PASS. Flood: 6,676/6,676 valid, 0 lost, 0 corrupt; 12,016,800 received bytes / 60.084656 s = 199,997.816 B/s. Dump: 500/500 valid, 0 lost, 0 corrupt; 640,000/640,000 PCM bytes. | [Console JSON][C], `baud_runs[0].flood`, `.dump` |
| 1,500,000 baud: loaded flood and four-channel dump | FAIL on dump. Flood: 5,011/5,011 valid, 0 lost, 0 corrupt; 150,056.859 B/s. Dump: 499/500 valid, 0 lost, 1 corrupt; 638,720/640,000 PCM bytes. | [Console JSON][C], `baud_runs[1]` |
| 921,600 baud: loaded flood and four-channel dump | FAIL on dump. Flood: 3,083/3,083 valid, 0 lost, 0 corrupt; 92,217.756 B/s. Dump: 499/500 valid, 0 lost, 1 corrupt; 638,720/640,000 PCM bytes. | [Console JSON][C], `baud_runs[2]` |
| 115,200 baud: loaded flood and four-channel dump | FAIL. Flood: 391/392 valid, 0 lost, 1 corrupt; 11,471.343 B/s. Dump: 498/500 valid, 0 lost, 2 corrupt; 637,440/640,000 PCM bytes. | [Console JSON][C], `baud_runs[3]` |
| Highest passing baud / measured R bytes per second | **2,000,000 baud; R = 199,998 B/s rounded** (199,997.81641422727 B/s measured). Each flood targets 60 s under I2S load; each dump captures four channels for 5 s. Boot/network-command baud remains 115,200. | [Console JSON][C], `highest_passing_baud`, `measured_bytes_per_second`, `baud_runs`; [console serial][CS], `LOOP START`, `DUMP BEGIN`, `BAUD`; [network serial][NS] and [host console defaults](../spikes/p4_probe/host/console_rate.py) |
| I2S load read errors / driver write failures / RX events | Flood loop reads: 6,010 / 6,010 / 6,010 / 6,000 in descending baud order; loop errors 0 at every baud. Every flood and both ring summaries report `driver_drops=0`, `rx_errors=0`, `tx_wait_rc=0`. Both ring loops report 610 reads, 0 errors. These are the instrumented counters, not a claim of zero host corruption. | [Console JSON][C], `baud_runs[*].loop`, `.flood.summary`, `ring_baseline.summary`, `slow_reader.summary`; [console serial][CS], `LOOP END` |
| Attach without reset: uptime witness | FAIL; **reset_on_open=true** observed despite DTR/RTS low. At selected 2,000,000 baud, command timed out; recovery at 115,200 showed uptime 368,133,398 → 1,673,411 us. Step 2 must handle reboot on open. | [Console JSON][C], `attach_without_reset`; [console serial][CS], corresponding `UPTIME`/`BAUD` records |
| Close/reopen mid-flood: loss, corruption and uptime | FAIL/incomplete; 99 observed sequences, 0 observed corrupt lines, no `FLOOD END`, so total loss and achieved rate are **unknown**. Requested baud 2,000,000; recovery baud 115,200; uptime before 2,179,397 us, after recovery 21,712,491 us. The post-recovery uptime does not itself rewind; the recovery baud and coordinator observation support reset-on-open. | [Console JSON][C], `reopen_mid_flood`; [console serial][CS] |
| Ring baseline at selected baud | 785 attempted; 698 received, 696 valid; 87 lost and 2 corrupt (89 missing-or-corrupt). Device ring drops 87, sent 698; unexplained missing/corrupt 2. Host received 1,253,897 bytes / 6.290740 s = 199,324.245 B/s. | [Console JSON][C], `ring_baseline` |
| Slow reader: ring drops and unexplained loss/corruption | With 2 s read pauses: 796 attempted; 269 received, 267 valid; 527 lost and 2 corrupt (529 missing-or-corrupt). Device ring drops 89, sent 707; unexplained missing/corrupt 440. Host received 485,208 bytes / 6.363658 s = 76,246.712 B/s. This is an incomplete/corrupt host capture, not a clean UART-rate pass. | [Console JSON][C], `slow_reader`; [slow-reader pause implementation](../spikes/p4_probe/host/console_rate.py) |
| Ring production versus target | Both ring runs target 240,000 B/s. Baseline achieved production = 1,413,000 bytes / 6.011121 s = 235,064.308 B/s; slow-reader production = 1,432,800 bytes / 6.291060 s = 227,751.762 B/s. These are produced bytes including device drops, distinct from host received throughput. | Derived from [console JSON][C], `ring_baseline.summary`, `slow_reader.summary`: `bytes * 1e6 / produced_us` |
| Dump pacing/deadline derived from measured R | For future pacing, wire allowance = `bytes / (0.8 * R)` with measured R above; effective budget 159,998.253 B/s. The clean 863,390-byte serial dump requires at least 5.396246 s wire allowance, in addition to its 5 s capture and command/processing margins. This is a derived planning budget, not a measured dump duration or proof of an implemented product deadline. | Derived from [console JSON][C], `measured_bytes_per_second`, `baud_runs[0].dump.received_bytes`; [run book](../spikes/p4_probe/README.md) |
| Network host stack and ownership | Managed `espressif/esp_hosted` **2.12.4**, IDF **6.1.0** in-tree Wi-Fi remote API; additional managed `esp_wifi_remote` shim absent/unnecessary in the completed build comparison. SDIO CLK18 CMD19 D0–D3=14–17, reset GPIO54 owned solely by hosted. | [Pinned network dependency lock](../spikes/p4_net/dependencies.lock); [manifest](../spikes/p4_net/main/idf_component.yml); [public network defaults](../spikes/p4_net/sdkconfig.defaults); [working network boot][NS] |
| Factory C6 firmware version RPC and transport compatibility | **`unavailable (rpc_rc=-1)`**; raw RPC fields show `version=0.0.0`, which is not a measured slave version. Explicit `--allow-unknown-slave-version` used (`slave_version_override=true`). Hosted transport active, join/AP/PS RPCs work, UDP/TCP complete: functional compatibility demonstrated for this campaign. Factory firmware predating the version RPC is the coordinator's likely explanation, not a proven version identity. | [Network result][N], `slave_version`, `slave_version_override`, `connection_evidence`, `udp_*`, `tcp_device`; [network serial][NS] |
| WiFi join IP, RSSI and AP fields | IP `192.168.4.33`; BSSID `b4:20:46:f4:ea:e6`; RSSI -47 dBm; primary channel 6, secondary 0; authmode 3, pairwise cipher 4, group cipher 4; `phy_11b=1`, `phy_11g=1`, `phy_11n=1`, `phy_11ax=1`. Connect and `sta_get_ap_info`: rc=0 ESP_OK. Network READY at uptime 8,588,327 us. | [Network result][N], `connection_evidence`, `boot`; [network serial][NS] |
| WiFi statis_dump / PS_NONE / private TX-done callback results | `statis_dump`: rc=262 ESP_ERR_NOT_SUPPORTED; `set_ps_none`: rc=0 ESP_OK; `set_tx_done_cb`: not linkable (no runtime success/return code invented). | [Network result][N], `connection_evidence.api`; [network serial][NS] |
| UDP campaign parameters | 60 s host campaign, 50 packets/s, 130 bytes, interface `wlp8s0`; independent uplink plus downlink echo. Device active duration 60.612205 s including stop/tail handling. | [UDP raw records][U], first `start` record; [network result][N], `udp_device.elapsed_us` |
| UDP RTT | p50 **5.380264 ms** (5.4 rounded), p99 **28.816624 ms** (28.8 rounded), max 96.442787 ms; 0 tail RTT samples excluded. | [Network result][N], `udp_host.esp-0.rtt_ms`, `rtt_tail_samples_excluded` |
| UDP uplink loss, jitter and ordering | **0% loss**, 3,031/3,031 unique received, 0 lost; wire loss also 0%. Jitter p50/p99/max = 1.189729 / 20.189413 / 77.995158 ms. Duplicates 0, reorders 1, longest gap 97.995158 ms; 0 stalls at the analyzer's 100 ms threshold. | [Network result][N], `udp_host.esp-0.uplink` |
| UDP downlink loss, jitter and ordering | **0% loss**, 2,998/2,998 unique received, 0 lost; capture complete. Jitter p50/p99/max = 0.721986 / 15.557931 / 56.539683 ms. Duplicates 0, reorders 1, longest gap 76.073 ms; 0 stalls at the analyzer's 100 ms threshold. Jitter is absolute inter-arrival minus send interval, as defined by the analyzer. | [Network result][N], `udp_host.esp-0.downlink` |
| UDP local send errors and device counters | Host send errors 0; device uplink send errors 0, echo send errors 0. Device sent 3,031; received/unique 2,998/2,998; duplicates 0, invalid packets 0, max RX gap 76,073 us. No local failures excluded from either direction's loss denominator. | [Network result][N], `udp_device`, `udp_host.esp-0.send_errors`, `.uplink`, `.downlink` |
| TCP device-to-host throughput | PASS, **4.37 Mbit/s rounded**. Device: 32,763,904 bytes / 60.006843 s, 4,368,022 bit/s, errno=0. Host: same 32,763,904 bytes / 59.99673237744719 s, 4,368,758.457561062 bit/s. | [Network result][N], `tcp_device`; [TCP host JSON][T] |
| Sleep-clock ICG boot-loop fix | Earlier network image failed before app_main in `sleep_clock_icg_startup_init`, PC `0x4805b676`, error `0x101` (allocation failure). Disable unused `CONFIG_PM_SLEEP_CLK_ICG_ENABLE` in this throughput-only spike; stale enabled configs fail compilation. Post-fix ELF omits the ICG startup/allocator symbols; the coordinator's new image reaches network READY and completes UDP/TCP. | [Original failed network serial][OLDNET]; [PC mapping][PC]; [post-fix key counts][PM]; [post-fix ELF symbols][SYM]; [public defaults](../spikes/p4_net/sdkconfig.defaults); [compile guard](../spikes/p4_net/main/main.cpp); [successful rerun][N] |
| Comparison with last S3 reference on the same host/load | **Unavailable in supplied evidence.** These files contain the P4 campaign only; no paired S3 reference identity, measurements or same-load witness is supplied. No numerical comparison or S3 parity claim can be made. | [P4 network result][N]; [UDP campaign metadata][U] |

The ICG failure was an eager REGDMA sleep-retention allocation from a constrained
retention-capable heap before app_main; aggregate free DIRAM did not establish
an eligible allocation. [IDF's ICG startup source][ICGSRC] and
[REGDMA allocator][REGDMA] show that path and its
`MALLOC_CAP_DEFAULT | MALLOC_CAP_RETENTION` requirement. The
[before/after configuration counts][PMBEFORE] / [post-fix counts][PM] show that
PM and tickless idle were disabled already, while ICG was enabled before the
fix and disabled after it. Exact heap fragmentation and the allocation that
consumed the needed block were not measured. The passing [network rerun][N]
confirms the scoped fix works on this board; it establishes no chip erratum.

The clean flood result and the failed passive attach/ring measurements must be
carried together into console design. The unsupported diagnostic/version APIs
remain recorded limitations of the working factory-slave path. No C6 update
was needed for the passing [network campaign][N], and none is authorized here.

## Earlier audio smoke

This coordinator smoke precedes the driver-backed console fixes. It provides
speaker/microphone/reference and speech evidence, not AEC or full product
acceptance. The source is [audio result.json][A]; settings are in the saved
[smoke script][AS].

| Speaker → Jabra tone | Measured peak | Band rise / SNR | Clipped samples | Evidence |
|---|---|---|---|---|
| 500 Hz | 500 Hz | 60.3 / 39.1 dB | 0 | [Audio JSON][A], `speaker.500` |
| 1,000 Hz | 1,000 Hz | 62.1 / 38.0 dB | 0 | [Audio JSON][A], `speaker.1000` |
| 2,000 Hz | 2,000 Hz | 62.6 / 37.1 dB | 0 | [Audio JSON][A], `speaker.2000` |
| 3,000 Hz | 3,000 Hz | 69.1 / 41.1 dB | 0 | [Audio JSON][A], `speaker.3000` |

| Capture condition | MIC1 / MIC2 / MIC3 / MIC4 RMS (dBFS) | Evidence |
|---|---|---|
| Quiet | -65.8 / -66.1 / -96.5 / -96.5 | [Audio JSON][A], `quiet` |
| DAC duplex tone | -35.9 / -39.8 / -43.7 / -96.5 | [Audio JSON][A], `duplex_1k` |
| Jabra tone | -36.9 / -40.0 / -96.5 / -96.5 | [Audio JSON][A], `jabra_1k` |
| Speech | -50.6 / -52.2 / -96.5 / -96.5 | [Audio JSON][A], `speech_levels` |

The MIC3 tone level is -43.9 dBFS during DAC duplex versus -132.6 dBFS during
Jabra playback, consistent with the electrical reference path; MIC1/MIC2 pick
up external sound, while MIC4 stays near the measured floor ([audio JSON][A],
`duplex_1k`, `jabra_1k`). Both microphone speech transcriptions are
“Please tell me the capital of France” (MIC1 includes a final period;
[audio JSON][A], `speech_stt`). The earlier serial speech dump had **9 bad lines
out of 316** at **115,200 baud** ([audio JSON][A], `dump_bad_lines`, `dump_lines`;
[saved smoke script][AS], serial baud); its STT result is not evidence of a
clean PCM transfer. Later per-baud dump results appear in Bring-up facts A.

## Bring-up facts B

The coordinator completed slots/reference in [campaign A][BAS] and the audio,
Opus and touch stages in [campaign B][BAT]. Panel timing is **partial**: the
sweep stalled on panel reinitialization, not during a completed timing window
([panel result][BPN], [watchdog log][BPNS]). Values below belong to those
measured binaries; the watchdog mitigation at the end is source-only and has
not been rebuilt, flashed or rerun. The earlier audio smoke in facts A is a
separate evidence set. All timing percentiles below are p50 / p99 / maximum.

### Codec policy and static compatibility

R10 codec exception — **user-confirmed 2026-10-31**: **P4 rev 1.3 uses esp_audio_codec
2.5.0; S3 stays on 2.6.2**. The spike enables `P4_PROBE_OPUS=y` and compiles the
unchanged production `opus.cpp` / `codec_memory.c` into its own component.
The two targets' bot-visible Opus configuration stays the same. Sources:
[P4 codec pin](../spikes/p4_probe/main/idf_component.yml),
[P4 defaults](../spikes/p4_probe/sdkconfig.defaults),
[P4 lock](../spikes/p4_probe/dependencies.lock), [S3 lock](../dependencies.lock),
[production wrapper](../components/pipecat_esp_peer/src/opus.cpp),
[production allocator hooks](../components/pipecat_esp_peer/src/codec_memory.c),
[chip identity in the panel flash log][BPFLASH].
No guard is bypassed. The step-1b spike does not change S3/shared source or the main lock.

The alternative is **both boards on codec 2.5.0**. That keeps version parity
but changes the qualified S3 codec archive; its cycle budgets, internal/PSRAM
heap floors and AEC-image placement/audits must be requalified. The split pin
retains the S3 2.6.2 archive at the cost of two intentionally different codec
lock entries. Step 12a must change the shared
[peer manifest](../components/pipecat_esp_peer/idf_component.yml), whose `~2.6`
constraint currently rejects 2.5.0: use a target-specific rule or widened
range plus exact locks. That changes the S3 manifest hash and requires the
S3/Waveshare build matrix, lock/config rechecks and required shared-change
regressions. The spike's direct wrapper compilation does not test that product
resolver path. P4 also forgoes 2.6.x fixes (AAC mono OOB, LC3, future fixes),
although neither codec is used in this Opus-only path. Identical settings/API
never establish identical native bitstreams. These costs are accepted by the
user-confirmed 2026-10-31 exception; PLAN/step-12a frozen inputs were not edited here.

The [API/ABI check][CODECABI] finds **no differences in the used API/ABI**:
seven used public headers are byte-identical, seven function signatures and
all used enum values match, and eight structures have identical sizes,
alignments and member offsets when compiled for the same RV32 ABI. In both
versions, encoder config is 32 bytes; decoder config 16; encoder input/output
frames 8/24; encoder frame-info 16; decoder input/output/info 16/16/16.
Encoder configuration fields and default VOIP/CBR behavior match. Module
malloc/calloc/realloc symbols are weak in both packages; their entire
`audio_codec_mem.c.obj` is byte-identical. The exact production hooks compile
unchanged and override the live allocation symbols in the P4 ELF
([symbol evidence][CODECSYM]); unused module-realloc is section-GC'd.

Differences are the package version/native codec binary/hash and vendor
silicon support: 2.6.2 rejects P4 rev<3; 2.5.0 supports its P4 build without that
guard. The [linked P4 codec archive][CODECLINK] SHA256 is
`1fda41dc09d46f1ed8bbe2d687fb6ccad3a5e2ac6726f5d15804f4f0c8a6f0c2`.
The configured frame is 16 kHz mono PCM16, 20 ms = 320 samples = 640 bytes;
24 kbit/s, complexity 0, FEC/DTX off. The unchanged wrapper validates 640
returned PCM bytes and exposes 320 samples. Its optional 40 ms encoder input
would be 1280 bytes; this benchmark keeps the production 20 ms setting
([API/ABI check][CODECABI], [wrapper](../components/pipecat_esp_peer/src/opus.cpp),
[transport defaults](../components/pipecat_esp_peer/include/pipecat/esp_peer/transport.h)).
Runtime cycle/PLC measurements appear below. Audio quality and wire/bitstream
equivalence remain unmeasured; identical API/configuration does not establish them.

The [peer archive screen][CODECABI] checks the linked esp_peer 1.5.7 P4 archive:
ELF32 little-endian RISC-V single-float ABI, no revision gate in its CMake, and
29,450 disassembled instructions spanning 69 base I/M/F/C mnemonics. Its
attributes say `xespv2p2` while the rev<3 compiler uses `xespv2p1`; the full
text scan found **no ESP/PIE, unknown or rev-3-only instruction**, so no rev>=3
instruction dependency was found by this quick check. The linked peer SHA256
remains `4c998cfa3ba178ca419d544a45bcae12a95f546181b6e658fc1119e86d3f6b87`
([link evidence][CODECLINK], [disassembly][PEERDIS]). This is static evidence;
the runtime default-ops check below opens no peer session.

### Measured image and archive identity

Campaign A and campaign B used different audio builds. The following identities
bind each measurement to its image; the watchdog address was decoded with the
panel ELF matching the coordinator's recorded hash ([ELF witnesses][BELF],
[symbol decode][BSYM]).

| Image | App bytes; SHA256 | ELF bytes; SHA256 | Evidence |
|---|---|---|---|
| Audio A: slots/reference | 1,133,312; `6c33d76bec045d2ffc617ace13d8fa13476ff245a6ea0c39cf9fd0a2e38fe036` | 13,873,488; `859e0d6d3d96e22ccb8e8acd2e66dbeb34e5840d890c7825eae6530f7a3f14ec` | [A build][BAB] |
| Audio B: AEC/Opus/touch | 1,137,648; `843f565afa2e6df051aff0d386980245e66ee3203446e87ab1ac67cb7d1a362b` | 13,888,272; `1ad9340c49f2b257e12e0bb79e2f69fba72bd60f727c4697eeee963e724d0a1c` | [B build in tone result][BAT] |
| Panel B | 299,440; `b90f3384c8f8bdee1e5e4ab1c5f665a9848ffa72f0c377c26e273a6f98bbba11` | 6,396,572; `49623969e1d9b9736f0010ff8db9b01a68fc957f34da99885ffa7cc092c3cf26` | [Panel build][BPBUILD], [flashed panel][BPFLASH], [ELF witnesses][BELF] |

| Actually linked archive | Bytes; SHA256 | Evidence |
|---|---|---|
| esp-sr 2.4.7 `esp32p4_less_v3_idf6/libesp_audio_processor.a` | 5,176,114; `16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100` | [Runtime build binding][BAT], [fresh link evidence][BBUILD] |
| esp-sr 2.4.7 less-v3 AFE front-end | 746,672; `d6367646474b57eede9a734cf52ccc984596db4e8910bca3df41bc3ba14f8aad` | [Runtime build binding][BAT], [fresh link evidence][BBUILD] |
| esp_peer 1.5.7 P4 | 2,498,228; `4c998cfa3ba178ca419d544a45bcae12a95f546181b6e658fc1119e86d3f6b87` | [Runtime build binding][BAT], [fresh link evidence][BBUILD] |
| esp_audio_codec 2.5.0 P4 | 3,543,944; `1fda41dc09d46f1ed8bbe2d687fb6ccad3a5e2ac6726f5d15804f4f0c8a6f0c2` | [Runtime build binding][BAT], [fresh link evidence][BBUILD] |

### Slots, gain discrimination and reference sweep

Wire order is MIC1 = word0 high, MIC2 = word1 high, MIC3 = word0 low,
MIC4 = word1 low. Each of silence, PA-off DAC chirp and the separate reduced-gain
captures has 32,000 samples per slot and zero RX overflows; all slot clip counts
are zero. Only MIC3 rises under the electrical chirp ([slots][BAS],
[CRC-protected serial][BASS]).

| Slot | Silence RMS dBFS; peak | PA-off DAC chirp RMS dBFS; peak | Rise dB | Evidence |
|---|---|---|---:|---|
| MIC1 | -65.328448; 63 | -65.562669; 75 | -0.234221 | [Slots JSON][BAS] |
| MIC2 | -65.544294; 73 | -65.870560; 71 | -0.326266 | [Slots JSON][BAS] |
| MIC3 | -96.381307; 3 | -44.297823; 359 | 52.083484 | [Slots JSON][BAS] |
| MIC4 | -96.513409; 2 | -96.514349; 2 | -0.000940 | [Slots JSON][BAS] |

MIC4 is a very low nonzero floor, not a literal digital zero in this campaign.
The default gain discrimination is floor-based, with DAC muted and PA off;
all `GAIN` acknowledgments report rc=0 and the host checks the requested mic/gain
([slot serial][BASS], [slot results][BAS]).

| Gain change | Slot RMS drops [MIC1, MIC2, MIC3, MIC4], dB | Observed mapping | Evidence |
|---|---|---|---|
| MIC1: 30 → 0 dB | [26.934333, 0.440203, 0.034818, -0.018801] | slot 1, matches expected | [Gain discrimination][BAS] |
| MIC2: 30 → 0 dB | [0.500461, 26.876272, 0.025186, -0.015295] | slot 2, matches expected | [Gain discrimination][BAS] |

The reduced mic floors are -92.262781 and -92.420566 dBFS respectively, each
peak 3; gains are restored after each check. No Jabra burst was requested
(`acoustic_requested=false`), so this campaign establishes electrical/floor
identity, with **external acoustic isolation unmeasured** ([slots][BAS]).

Reference capture covers all 48 combinations: MIC3 nominal gains 0..33 dB by
3 and DAC volume -20/-12/-6/0 dB. The commanded sine is 1 kHz at -12 dBFS,
with PA off; each capture has 16,000 frames, zero overflow and zero clipping.
Gain-register readback follows `0x10 | gain/3`; values below are measured MIC3
RMS dBFS ([reference JSON][BAR], [serial/readback][BARS],
[stimulus implementation](../spikes/p4_probe/main/audio_b.cpp)).

| Nominal ref gain dB; register | DAC -20 dB | DAC -12 dB | DAC -6 dB | DAC 0 dB | Evidence |
|---|---:|---:|---:|---:|---|
| 0; 0x10 | -57.578929 | -49.548540 | -43.554204 | -37.576637 | [Reference sweep][BAR] |
| 3; 0x11 | -54.225303 | -46.195124 | -40.201247 | -34.223091 | [Reference sweep][BAR] |
| 6; 0x12 | -51.309074 | -43.278241 | -37.284498 | -31.306379 | [Reference sweep][BAR] |
| 9; 0x13 | -48.350791 | -40.320460 | -34.326923 | -28.349042 | [Reference sweep][BAR] |
| 12; 0x14 | -45.353931 | -37.324382 | -31.330575 | -25.352646 | [Reference sweep][BAR] |
| 15; 0x15 | -44.240134 | -36.210602 | -30.217043 | -24.238965 | [Reference sweep][BAR] |
| 18; 0x16 | -41.323168 | -33.294018 | -27.300367 | -21.322325 | [Reference sweep][BAR] |
| 21; 0x17 | -38.368872 | -30.339611 | -24.345994 | -18.367922 | [Reference sweep][BAR] |
| 24; 0x18 | -35.376888 | -27.347498 | -21.353911 | -15.375647 | [Reference sweep][BAR] |
| 27; 0x19 | -32.297515 | -24.268262 | -18.274697 | -12.295951 | [Reference sweep][BAR] |
| 30; 0x1a | -29.363583 | -21.334276 | -15.340662 | -9.361071 | [Reference sweep][BAR] |
| 33; 0x1b | -26.293824 | -18.264864 | -12.270781 | -6.289872 | [Reference sweep][BAR] |

**The nominal 12→15 dB step is much smaller than 3 dB.** The coordinator's
“only about +1.2 dB” description corresponds to retained RMS differences
+1.113797 / +1.113780 / +1.113532 / +1.113681 dB at DAC -20/-12/-6/0 dB.
Thus the exact stored result is approximately **+1.114 dB**. At DAC -6 dB,
MIC3 changes -31.330575→-30.217043 dBFS while reg 0x45 changes 0x14→0x15.
This is measured behavior, not a reason to silently change the production
register formula. Maximum MIC3 peak is 24,447 at DAC 0 dB/ref 33 dB, still
zero clips ([full sweep][BAR], [register records][BARS]). The AEC operating
point below uses ref 15 dB / reg 0x15 and DAC -6 dB, not a claim that the
nominal gain ladder is acoustically calibrated ([tone][BAT], [speech][BAP],
[commanded volume][BATS]).

### Production-shaped AFE runs

The exact `afe_aec_create("MR",4,AFE_TYPE_FD,AFE_MODE_LOW_COST)` path and nested
format guards ran successfully. Telemetry confirms 16 kHz, two input channels
(MIC1 + MIC3 reference), one output, 512-sample chunks / 1,024 output bytes per
chunk, core 1 at 360,000,000 Hz, a 16,384-byte PSRAM worker stack, and production
duplex DMA 4×240 frames (960 frames / 60 ms). Reference gain is 15 dB with
verified reg 0x15. DMA is restored to 6×320 after a run; production duplex uses
`DG_SPEAKER_DMA_*`, not the standalone mic defaults ([tone config][BAT],
[speech config][BAP], [firmware](../spikes/p4_probe/main/audio_b.cpp),
[production duplex](../components/dg_audio/src/echoear_i2s.cpp)).

| Run | Requested seconds; elapsed seconds | Chunks; input/output samples | RX overflow; ref/output clips | Crude ERLE dB | Evidence |
|---|---|---|---|---:|---|
| Tone smoke | 10; 10.018571 | 313; 160,256/160,256 | 0; 0/0 | 31.073892 | [Smoke][BAM] |
| Tone soak | 600; 599.997743 | 18,750; 9,600,000/9,600,000 | 0; 0/0 | 21.041693 | [Tone][BAT] |
| Speech soak | 600; 599.997642 | 18,750; 9,600,000/9,600,000 | 0; 0/0 | 9.664157 | [Speech][BAP] |

Both full soaks meet the ten-minute sample denominator and telemetry
completeness gates. **Tone ERLE 21.0 dB and speech ERLE 9.7 dB are report-only**:
the estimator is the MIC1/output power ratio after the first two adaptation
seconds, not delay-aligned speech ERLE or a talk-over/STT acceptance score.
The short smoke's higher ratio is not a substitute for the full runs
([tone][BAT], [speech][BAP], [smoke][BAM]).

| Run | Cycles per chunk p50 / p99 / max | AFE-call wall µs p50 / p99 / max | Derived mean cycles; mean µs; call-time fraction | Evidence |
|---|---|---|---|---|
| Smoke | 1,304,918 / 1,333,732 / 1,349,032 | 3,626 / 3,706 / 3,749 | 1,307,564.863; 3633.591; 11.3521% | [Smoke][BAM] |
| Tone | 1,025,996 / 1,299,710 / 1,351,108 | 2,851 / 3,611 / 3,754 | 1,052,557.480; 2924.661; 9.1396% | [Tone][BAT] |
| Speech | 1,005,872 / 1,281,066 / 1,317,400 | 2,795 / 3,560 / 3,660 | 1,033,490.252; 2871.677; 8.9740% | [Speech][BAP] |

The fraction is summed AFE-call wall time divided by run elapsed time; it is
not exclusive CPU utilization. Cycle/time deltas include interrupts and
preemption. CRC/sequence reanalysis reproduces both saved per-chunk
distributions and totals ([raw tone][BATS], [raw speech][BAPS],
[offline check][BDER]).

AFE factory allocation is **13,104 internal bytes + 113,556 PSRAM bytes**;
worker allocation is separately **104 internal + 16,388 PSRAM bytes** for the
requested stack. Chunk buffers add 7,180 PSRAM bytes including allocator
rounding. Factory deltas are taken inside the allocated worker, so no stack
subtraction belongs in them. Both soak runs reach internal minimum 472,024 B
and minimum observed largest block 335,872 B, passing the declared 81,920 /
32,768 B floors. Internal free returns 522,311 B after worker deletion; PSRAM
returns to each run's pre-worker level (32,483,068 tone / 32,413,432 speech).
No net worker/factory leak is observed in those snapshots; this is not a full
peer-session heap qualification ([tone heaps][BAT], [speech heaps][BAP],
[floor/accounting implementation](../spikes/p4_probe/host/measure_b.py)).

Speech input is the retained `prompt_france_16k_m12.wav`: 34,559 mono PCM16
samples, 16 kHz, peak 7,770 / -12.500578 dBFS, RMS -32.745849 dBFS. It is
looped for the speech soak and used by the Opus run. Host/firmware enforce
peak ≤-12 dBFS without implicit scaling ([speech playback][BAP],
[Opus playback][BAO], [input WAV][BWAV]).

### Opus and peer

Opus **2.5.0** on the P4 completes **300 normal + 300 PLC frames**, using
uploaded speech, 16 kHz mono, 20 ms / 320 samples / 640 PCM bytes,
24,000 bit/s, complexity 0, FEC/DTX off. Every normal/PLC output is checked at
320 samples. This exercises the unchanged wrapper's zero-length PLC/non-null
placeholder workaround; no packet-loss quality score is implied. The task is
core 0 with 32,768 internal stack bytes; free high-water is 8,792 bytes
(derived maximum used 23,976 bytes) ([Opus result][BAO],
[per-frame records][BAOS], [production wrapper](../components/pipecat_esp_peer/src/opus.cpp)).

| Operation | Cycles p50 / p99 / maximum | Evidence |
|---|---|---|
| Encode | 1,398,868.0 / 2,196,511 / 2,331,861 | [Opus 2.5.0 cycles][BAO] |
| Normal decode | 125,757.5 / 163,656 / 174,012 | [Opus 2.5.0 cycles][BAO] |
| PLC decode | 222,842.0 / 235,633 / 236,122 | [Opus 2.5.0 cycles][BAO] |

esp_peer **1.5.7** default open/close operations link and are non-null on the
running P4; the static architecture caveat above remains. No session was
opened, so this does not test the prebuilt library's runtime IDF ABI assumptions
or establish bot/wire parity ([peer witness][BAT], [static screen][CODECABI]).

### Goodix touch and shared-bus load

The controller is **GT9271**, product bytes **39323731** (ASCII `9271`),
at **0x5D**, configured **800×800**, polled at **50 Hz**. Campaign A's host-only
failure was its GT911-only product-ID check; the saved controller reads were
good. The updated parser accepts and records both GT911 and GT9271. No physical
touches were recorded, so actual coordinate range/edge response is
**unmeasured**; zero out-of-range events with no touches is not coordinate
qualification ([A touch capture][BAOLDT], [B touch][BATO],
[parser](../spikes/p4_probe/host/measure_b.py)).

| Phase | Polls / events / I2C errors | Read µs p50 / p99 / max | Poll interval µs p50 / p99 / max | Codec ops/errors; audio loop | Evidence |
|---|---|---|---|---|---|
| Idle | 400 / 0 / 0 | 181 / 182 / 189 | 20000 / 20000 / 20000 | 0/0; not running | [Touch JSON][BATO] |
| Loaded | 400 / 0 / 0 | 184 / 185 / 261 | 20000 / 20000 / 20001 | 800/0; 610 reads, 0 errors | [Touch JSON][BATO] |

Each poll window spans eight seconds by poll count/rate. Loaded mode includes
the six-second audio loop and concurrent codec register traffic; the host
requires the completed `LOOP END` witness with positive reads/zero errors
([touch serial][BATOS]). GPIO27 is shared LCD/touch reset; panel setup can
relatch the address from INT. Touch after panel reset and fallback address
0x14 were not exercised in these captures. The product must serialize reset
with reads and re-probe ([BSP notice](../spikes/p4_panel/BSP-NOTICE.md),
[touch source](../spikes/p4_probe/main/audio_b.cpp)).

### Panel timing, underruns and partial sweep

The flashed spike configures JD9365 **2.0.2**, RGB565 **800×800**, horizontal/
vertical totals **880×840**, two DSI lanes at **1,500 Mbps**, PHY LDO channel
**3 / 2,500 mV**, reset GPIO27 and inverted backlight GPIO26 at **50%**.
Both framebuffers are in PSRAM. The one-pixel border, colour bars and moving
bar are generated, but no visual-stability/border-integrity witness was
recorded ([panel metadata][BPN], [panel flash log][BPFLASH],
[measured-binary source](../spikes/p4_panel/main/main.cpp),
[BSP constants](../spikes/p4_panel/BSP-NOTICE.md)).

Effective MHz below is **timing-derived** `refresh × 880 × 840 / 1e6`, not a
direct PLL-register measurement; IDF compensates the horizontal timing for
clock-divider quantization. All completed commands requested ten seconds;
callback captures have zero telemetry-array overflow. Underrun counts cover
the whole command response including setup, so they are not localized to the
steady measurement window ([panel JSON][BPN], [raw log][BPNS],
[IDF timing compensation][BDPIHAL]).

| Requested MHz | Effective MHz | Refresh Hz | Vsync µs p50 / p99 / max | Frame-complete µs p50 / p99 / max | Vsync/frame callbacks | Underruns | Evidence |
|---|---:|---:|---|---|---|---:|---|
| 80 | 80.000000 | 108.225108 | 9240 / 9245 / 9246 | 9240 / 9245 / 9245 | 1089/1089 | 0 | [Panel result][BPN], [serial][BPNS] |
| 60 | 60.000000 | 81.168831 | 12320 / 12322 / 12322 | 12320 / 12322 / 12323 | 813/813 | 1 | [Panel result][BPN], [serial][BPNS] |
| 50 | 49.989856 | 67.626970 | 14787 / 14792 / 14794 | 14787 / 14791 / 14793 | 678/678 | 1 | [Panel result][BPN], [serial][BPNS] |
| 45 | 44.982657 | 60.853161 | 16433 / 16811 / 16830 | 16433 / 16811 / 16830 | 612/612 | 1 | [Panel result][BPN], [serial][BPNS] |
| 40 | 40.000000 | 54.112554 | 18480 / 18483 / 18483 | 18480 / 18483 / 18483 | 546/546 | 1 | [Panel result][BPN], [serial][BPNS] |
| 38 | Unmeasured | Unmeasured | No completed window | No completed window | No completed window | 1 before stall | [Watchdog log][BPNS], [partial result][BPN] |
| 36 | Not reached | Not reached | Not reached | Not reached | Not reached | Not measured | [Default grid](../spikes/p4_probe/host/run_panel.py), [partial result][BPN] |

**Lowest completed ≥50 Hz timing point: 40 MHz effective, 54.112554 Hz**.
It has an observed underrun, so this is a timing candidate, not an underrun-free
or visually stable minimum. Visual stability is unmeasured for every completed
point; no lowest visually stable clock is established. The partial runner did
not reach its final selection step ([partial result][BPN], [host failure][BPNLOG]).

### Watchdog diagnosis and source-only mitigation

During reinitialization for the 38 MHz command, CPU0 main starves IDLE0;
CPU1 remains in IDLE1. Recorded PC **0x48013cea** (also **0x48013ce8**) resolves
against the measured ELF to `mipi_dsi_hal_host_gen_read_short_packet`,
`mipi_dsi_hal.c:218`: an unbounded `while(read_cmd_busy)` inside the JD9365
DCS **0x04** ID read. The following read-FIFO-empty loop is unbounded too.
This is not evidence that the framebuffer completion wait caused the stall
([register dumps][BPNS], [matching ELF hash][BELF], [decoded PC][BSYM],
[IDF source][BDPIHAL], [JD9365 read site][BJDREAD]).

The spike-local [DCS read replacement](../spikes/p4_panel/main/dsi_read.cpp)
uses a **500,000 µs** shared deadline, blocks the task for one tick while
polling command/read FIFOs and read-busy, and bounds FIFO draining. A stuck
read emits `panel_io_error` and fails closed with timeout instead of spinning
forever; the existing coordinator rollback guard handles failure. The watchdog
stays enabled. This contains the software hang; the underlying reason for the
unanswered DSI read is not established. No panel identity is fabricated.

**No rebuild or hardware rerun was performed for this change.** Existing panel
numbers above remain bound to the old ELF; native host tests exercise the
actual portable [wait helper](../spikes/p4_panel/main/bounded_wait.h), including
permanent stall and a deadline shared across phases ([host test result][BWAIT]).
Firmware link/API validation and the panel rerun are the coordinator's next
decision. No new stable-image or lower-clock pass is claimed.

[FID]: /home/khkramer/src/evidence/echoear/p4/factory-20261031T060646Z/flash-id.txt
[MAC]: /home/khkramer/src/evidence/echoear/p4/factory-20261031T060646Z/read-mac.txt
[BACKUP]: /home/khkramer/src/evidence/echoear/p4/factory-20261031T060646Z/sha256.txt
[RPS]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/restore-proof-probe/serial.log
[RP]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/restore-proof-probe/result.json
[RESTORE]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/restore-proof.log
[ROLLBACK]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/network.log
[C]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/console/result.json
[CS]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/console/serial.log
[N]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-net-20261031T090701Z/network/result.json
[NS]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-net-20261031T090701Z/network/serial.log
[U]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-net-20261031T090701Z/network/udp.jsonl
[T]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-net-20261031T090701Z/network/tcp.json
[OLDNET]: /home/khkramer/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/network/serial.log
[PC]: /home/khkramer/src/evidence/echoear/p4/step-1a/fix-2/before/failing-pc.txt
[PMBEFORE]: /home/khkramer/src/evidence/echoear/p4/step-1a/fix-2/config-before-counts.json
[PM]: /home/khkramer/src/evidence/echoear/p4/step-1a/fix-2/config-after-counts.json
[SYM]: /home/khkramer/src/evidence/echoear/p4/step-1a/fix-2/net-startup-symbols.txt
[ICGSRC]: /home/khkramer/esp/esp-idf-v6.1/components/esp_hw_support/port/esp32p4/pmu_sleep_clock_icg.c:130
[REGDMA]: /home/khkramer/esp/esp-idf-v6.1/components/esp_hw_support/port/regdma_link.c:23
[A]: /home/khkramer/src/evidence/echoear/p4/audio-smoke-20261031T063332Z/result.json
[AS]: /home/khkramer/src/evidence/echoear/p4/audio-smoke-20261031T063332Z/p4_audio_test.py

[CODECABI]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-1/api-abi-peer-check.json
[CODECLINK]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-1/probe-link-evidence.json
[CODECSYM]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-1/probe-defined-symbols.txt
[PEERDIS]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-1/peer-disassembly.txt

[BAS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-a-20261031T111111Z/slots/result.json
[BASS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-a-20261031T111111Z/slots/serial.log
[BAR]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-a-20261031T111111Z/reference/result.json
[BARS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-a-20261031T111111Z/reference/serial.log
[BAB]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-a-20261031T111111Z/audio-build.json
[BAOLDT]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-a-20261031T111111Z/touch/serial.log
[BAM]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/aec-smoke/result.json
[BAT]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/aec-tone/result.json
[BATS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/aec-tone/serial.log
[BAP]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/aec-speech/result.json
[BAPS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/aec-speech/serial.log
[BAO]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/opus/result.json
[BAOS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/opus/serial.log
[BATO]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/touch/result.json
[BATOS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/touch/serial.log
[BPN]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/panel/result.json
[BPNS]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/panel/serial.log
[BPNLOG]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/panel.log
[BELF]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/elf-sha256.txt
[BPFLASH]: /home/khkramer/src/evidence/echoear/p4/step-1b/coordinator-b-20261031T112523Z/flash-panel.log
[BBUILD]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-2/fix-3-audio-build.json
[BPBUILD]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-2/fix-3-panel-build.json
[BSYM]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-4/watchdog-symbols.txt
[BDER]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-4/derived-facts.json
[BWAV]: /home/khkramer/src/evidence/echoear/p4/step-1b/stimulus/prompt_france_16k_m12.wav
[BWAIT]: /home/khkramer/src/evidence/echoear/p4/step-1b/fix-4/host-tests.log
[BDPIHAL]: /home/khkramer/esp/esp-idf-v6.1/components/esp_hal_lcd/mipi_dsi_hal.c
[BJDREAD]: /home/khkramer/src/dg-esp32-p4/spikes/p4_panel/managed_components/espressif__esp_lcd_jd9365/esp_lcd_jd9365.c


## Product touch and contention (step 13b)

The P4 backend now reads the GT911-family register protocol directly, validates
GT9271 (`9271`) or GT911 (`911\0`) identity and 800×800 configuration, and probes
0x5D then 0x14. No IRQ pin or synthetic IRQ counter participates in polling.
The existing non-audio touch owner calls it at most once every 20 ms (≤50 Hz). Raw points
stay native; gestures use the inverse logical layout `(native−40)/2` inside
[40,760)². Border contacts, multi-touch and out-of-range coordinates cancel the
gesture. No-ready samples do not synthesize releases. Existing gesture timing
is unchanged. Axis orientation is provisional until a real user touch.

GPIO27 reset and Goodix probes/reads share the board I2C mutex. Every attempted
reset invalidates the driver's address generation; its next read re-probes and
cancels the pre-reset gesture. The touch driver never drives reset or INT.
Codec transactions continue to use IDF transaction locking. Each touch I2C
transaction and mutex wait is bounded at 10 ms (one point uses status, data and
acknowledgement transactions); absence/errors cannot silently produce taps.

[The coordinator procedure](../hil/p4_contention.md) fixes the 300 s contention
limits before acquisition, keeps intentional restart generations separate,
and derives display storage placement from the candidate ELF while measuring live internal heap.
Host tests/builds do not prove hardware touch coordinates, F11, audio/display
contention or tearing. These remain **unverified** until coordinator acquisition.


Step 13b fix 1: `PERF.capture` is additive on both AEC-off and AEC images. Its
`rx_overflows`/`rx_completions` are the I2S driver's capture counters;
`read_errors`/`read_timeouts`/`read_frames` come from the microphone read path.
Pipeline send-queue drops do not measure DMA overflow. The historical
`aec.capture` object remains unchanged on AEC images. Contention preflights
these board-neutral keys before starting its timed window.
Goodix 2–10 contacts cancel the gesture without becoming a bus error. Failed
reset re-probes back off for one second. A successful reset re-probe deliberately
cancels contact state with INVALID_STATE, adding one `read_errors` status count;
that touch diagnostic is not a zero-bus-error qualification gate.


13a fix 2: the first display image failed before app_main while allocating the
4,096-byte main-task stack. Its RETENT_RAM region was 98,816 B versus the working
12b ELF's 119,024 B; the printed 18 KiB and 384 KiB regions are unavailable until
the main task begins. The panel/history and init-command backing store now lives
in external BSS with first-use placement construction; the constexpr frame-counter
constructor is retained. No reset, timing, framebuffer count or ISR IRAM setting
changes. Every P4 link requires at least 114,928 B pre-main retention headroom
(12b minus a declared 4 KiB margin), external NOBITS panel storage and the ISR in
IRAM. The constructor guard resolves preinit/init addresses to source (including
inline frames), rejecting any JD9365 backend source independently of GCC symbol
names. Real fixture ELFs cover extraction and a floor-specific negative control.
The coordinator's fix-2 normal run confirmed boot and the 113 KiB RETENT_RAM
line; factory storage then blocked LittleFS provisioning. The P4 bench adapter
now prepares that named partition before boot, as documented in
[p4_product.md](../hil/p4_product.md). The floor remains a regression margin:
earliest boot internal-free/retention measurements and live contention evidence
are still coordinator-owned; static checks do not establish either.
