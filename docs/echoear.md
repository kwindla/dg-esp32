# Espressif EchoEar board foundation

## Source provenance

Pins cross-checked against the EchoEar V1.2 schematic and Espressif's current
ESP VoCat V1.2 board definition (the current upstream name). Pinned Brookesia
commit: `90ba55a446d62ccfb9ae0671760583011301b45a`. Retrieved 2026-10-14.
Source files and SHA-256:

- [board_devices.yaml](https://github.com/espressif/esp-brookesia/blob/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/board_devices.yaml): `abee7445df13871d1deccf8c58c4d539301194be22fbe3cfc6b57a5e89f10868`.
- [board_peripherals.yaml](https://github.com/espressif/esp-brookesia/blob/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/board_peripherals.yaml): `393d41b75ceb08377b311118c6f7463f642c005134f2d559eb7f0a2900a5ff63`.
- [setup_device.c](https://github.com/espressif/esp-brookesia/blob/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/setup_device.c): `7900245d748fce959b2d669113b52d39799c26821ffdad9d74c0b2fd9dd96f36`.
- [sdkconfig.defaults.board](https://github.com/espressif/esp-brookesia/blob/90ba55a446d62ccfb9ae0671760583011301b45a/hal/brookesia_hal_boards/boards/espressif/esp_vocat_board_v1_2/sdkconfig.defaults.board): `49c57c138964202ac62d8e6d7bf568c8041e2ff9a0fee89ee98f899a53fc0a36`.
- [EchoEar V1.2 schematic](https://dl.espressif.com/AE/esp-dev-kits/EchoEar_SCH_V1_2.pdf): `c4bc1a0348575ecf080333d6ce6ccbff87283b97e5f772e3eba0e772008726a5`.

## Wiring

All GPIO numbers below are ESP32-S3 GPIOs. I2C addresses here are **7-bit**;
Espressif's codec YAML uses shifted 8-bit addresses (0x30 and 0x80).

| Function | Connection |
| --- | --- |
| Shared I2C0 | SDA 2, SCL 1; 400 kHz codecs |
| ES8311 DAC | I2C 0x18; analog output to NS4150B amplifier |
| ES7210 ADC | I2C 0x40; MIC1/MIC2 microphones, MIC3 hardware playback reference; MIC4 unused |
| I2S0 shared clocks | MCLK 42, BCLK 40, WS 39 |
| I2S data | ESP TX 41 to ES8311; ESP RX 3 from ES7210 |
| Codec rail | GPIO48 high enables CODEC_3V3 |
| Speaker PA | GPIO15 high enables amplifier; physical gain 2.4x (7.6 dB); upstream model uses 6 dB |
| LCD/SD power | GPIO9 low enables rail; do not confuse with LCD reset |
| ST77916 QSPI | SPI2, SCLK 18, CS 14, D0 46, D1 13, D2 11, D3 12 |
| LCD DC | GPIO45 in upstream panel IO configuration; QSPI command width 32 |
| LCD reset | GPIO47, active high at MCU due to external transistor; panel reset active low |
| LCD backlight | GPIO44, upstream LEDC 2 kHz, 10-bit, starts at 0% |
| LCD TE | GPIO8; not used for initial port |
| Touch | schematic CST816T, compatible CST816S family driver, 7-bit 0x15 (YAML 0x2a), interrupt GPIO10; shared I2C |
| SD (unused) | CLK16, CMD38, D0 17 |
| Green LED | GPIO43, active low |
| Other | BMI270 interrupt GPIO21; BQ27220 battery gauge on shared I2C |

Upstream display: 360 x 360, SPI mode 0, QSPI enabled. The board-specific
ST77916 init sequence is `setup_device.c` above, not the Waveshare SPD2010
sequence. The upstream YAML uses `need_reset: false`; a manual board reset
must respect the GPIO47 inversion. No TCA9554 expander exists on this board.

The schematic page 4 labels the ES8311 differential OUTP/OUTN feedback network
as AEC and connects it to ES7210 ADC_MIC3_P/N. Upstream ADC mask is 0b0111,
labels from high to low are NA/RE/FR/FL. AEC is not enabled in this port.
Capture selects MIC1|MIC2 in non-TDM stereo and extracts MIC1 for mono.
Selecting three microphones switches ES7210 to TDM; SDOUT2 is unconnected.
Future reference capture must qualify TDM slot order and change both TX/RX
geometry together: they share I2S0 clocks. The reference is not mixed in.

Codec startup raises GPIO48, waits 50 ms for CODEC_3V3 and its I2C level
shifter, starts MCLK, and probes 0x18/0x40 within a 200 ms rail deadline.
ES8311 writes report the register and esp_err. Only the first REG44=0x08
NACK may be retried once, with mandatory readback; all other failures abort.
Final REG0D=0x01, REG44=0x58 and MIC1_GAIN=0x1a are startup assertions.

GPIO9 must be driven low before SPI/touch access and settled for at least
10 ms. GPIO47 reset is asserted high for at least10 ms then deasserted low
for at least120 ms.

GPIO45/46 are strapping pins: 45 is superseded by the measured voltage eFuse;
46 must retain the required boot level. GPIO3 affects JTAG selection only
when STRAP_JTAG_SEL is burned (currently zero). GPIO43/44 are repurposed
ROM UART pins; backlight initialization must clear GPIO44's ROM pull-up.

## Bench identity and memory

The supplied factory measurements (2026-10-14) identify ESP32-S3 rev v0.2,
MAC `b4:3a:45:16:2d:94`, 16 MB GD QIO flash and 16 MB AP 1.8 V octal PSRAM.
Read-only evidence: `/home/khkramer/src/evidence/echoear-factory/`.
Its efuse-summary.txt confirms PSRAM_CAP=16M, PSRAM_VENDOR=AP_1v8,
VDD_SPI_FORCE=1, VDD_SPI_XPD=1, VDD_SPI_TIEH=0. IDF must not change eFuses;
80 MHz octal PSRAM with auto type detection is appropriate. The S3 PSRAM
Kconfig has no separate voltage selector: the existing eFuse selects 1.8 V.
XIP-from-PSRAM and the existing internal heap reserve remain enabled.
The schematic's nominal N32R16V module designation does not override the
physical board's measured 16 MB flash capacity.
The ROM image header may say DIO while the second-stage bootloader configures
QIO: the QIO boot log is the runtime check. PSRAM validation rejects
`Virtual address not enough`. Physical total is 16 MiB; XIP pages consume
part of it, so the allocator heap total is approximately 14 MiB, not 16 MiB.

Factory image strings confirm `hal_echoear`, ES8311, ES7210, ST77916 and
CST816S drivers. This verifies driver identity, not runtime pin programming.
Use only `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00`.
Never access the retired Waveshare (`44:1b:f6:84:fc:8c`).

## Qualification

Pin provenance established. Build/boot/audio/voice qualification pending;
no hardware pass is claimed by this document's initial revision.

### First boot (2026-10-14)

Automated capture: QIO, Found 16MB PSRAM, WiFi READY and console ping.
Idle general internal free 161880 B; largest main block 126976 B; reserve
free 15967 B. Existing audio host tests 42/42; Waveshare firmware builds
without touching its hardware. Evidence: `~/src/evidence/echoear/A/boot/`.
These idle figures precede connected-call and endurance qualification.

IDF 6.1 duplex I2S chooses the later-initialized direction as slave. EchoEar
must initialize RX first: capture supplies the shared clocks while TX idles.
Codec driver dependency: espressif/esp_codec_dev **1.6.2**, exact registry pin
and dependency lock hash. The existing pipeline owns bounded DMA I/O and
callbacks; codec/control interfaces supply Espressif's register sequences.

### Audio bring-up measurements

With RX initialized before TX, both directions passed the causal tone checks.
After applying ADC gain after `enable()`, MIC1 gain register reads `0x18`
(24 dB); DAC volume register reads `0xae` (-6 dB). Digital gain defaults to
400% (+12 dB), matching the measured voice diagnostic calibration.

Final tone capture (`A/audio-loop-3`): native mic baseline -62.75 dBFS,
1 kHz input tone median -38.5 dBFS; output THD 0.447%, output SNR 20.73 dB,
zero clipped samples. These are **not acoustic qualification**: Jabra room
baseline -49.80 dBFS exceeded the -50 dBFS gate. Earlier captures are retained.
No old Waveshare frequency-response correction was applied.

Offline/live E2E qualification attempts were each stopped by room-noise gates
(-36.70/-38.40 dBFS), before a scored turn. Neither is 5/5. Explicit noisy
runs preserve all checks and measurements but can never count as qualified.
EchoEar prompt calibration targets ADC speech RMS (-45 dBFS normally), with
no claim about physical SPL or microphone sensitivity.

### Reproduce safely

- Build EchoEar with `scripts/idf.sh build`; flash separately with
  `scripts/idf.sh flash`. Never combine build and flash under the bench lock.
- `hil/board_port.py` accepts only the EchoEar by-id port and verifies USB
  serial/MAC before opening; flash also verifies the chip MAC with esptool.
- Wrap bot/acoustic campaigns in `python hil/bench.py COMMAND ...` so port
  7861 and serial share `/tmp/claude-1000/echoear-bench.lock` with other workers.
  Supply `--out` under `/home/khkramer/src/evidence/echoear/A/`.
- `DG_BOARD=waveshare_146 scripts/idf.sh build` selects legacy defaults only
  on a fresh generated configuration. Keep builds isolated (the validation
  used `build/waveshare/`, a separate SDKCONFIG, and common + Waveshare
  defaults). Changing defaults does not overwrite an existing sdkconfig.
  Legacy hardware access is deliberately rejected by the harness.
- Preserve LCD TX GDMA priority 1, internal-memory hashing and the flash gate.
  This worker made no display-driver changes.

### Voice path and remaining qualification

`A/offline-diagnostic-2`: all five board recordings contain the expected
reply, verified by STT, and all five reply transport gates are clean. This
is functional evidence, not a 5/5 qualified campaign. Four runs had noisy
baselines; the fifth exposed a remaining legacy SPL inference, now removed.

`A/live-diagnostic`: run3 recognizes the complete intended France prompt,
associates the fixed reply to that turn, and records the expected reply on
the board microphone. All available functional assertions pass with zero
I2S underflows and decode errors. Runs2/4/5 record the reply but fail strict
prompt-turn association; run1 times out. Background conversation appears in
retained STT evidence. Overall live qualification is not 5/5.

EchoEar reply level is measured directly at the Jabra: -22 dBFS +/-3 dB
RMS and at least 10 dB above the measured room, declared before acquisition.
SPL fields remain null; no Waveshare sensitivity is substituted. Missing
samples or room-rise evidence fail this gate, including diagnostic runs.
Noisy diagnostic runs cannot become acoustic passes.

The original `AUDIO write_errors` includes bounded 5 ms I2S write timeouts.
`write_timeouts` is now reported separately without changing playback or
suppressing the original error counter. The existing pipeline retries the
unwritten suffix of the same render interval; underruns and decoded-content
checks remain separate. The partial endurance results are recorded below. The user cancelled all
further soaks; no completed 20-minute result is claimed.

### Endurance checkpoint (interrupted)

The requested 20-minute run received a host SIGINT after **600.34 seconds**;
this is not a completed soak. Evidence: `A/soak20/interrupted-summary.json`
and the original serial, bot, RTP and result files. Across 21 snapshots:

| Measurement | Observed |
| --- | --- |
| Peer sessions | 1 |
| General internal free | 94416–94428 B |
| Largest main block | 57344 B throughout |
| PSRAM free | 10975744–10976400 B |
| Internal reserve free | 15967 B throughout |
| Minimum observed stack margin | 29.62% |
| Lost / PLC frames | 8 / 8 |
| Drift corrections | 1 |
| Rebuffers / I2S underruns / decode errors / queue drops | 0 / 0 / 0 / 0 |
| Capture errors / non-timeout I2S write errors | 0 / 0 |
| Total I2S write errors / timeouts | 10167 / 10167 |

The firmware has a demonstrated live voice path and stable observed memory,
but acoustic 5/5, loss-free transport, and the full 20-minute endurance gate
remain unqualified. No touch or battery work was attempted.

### Review corrections and evidence limits

The DAC API request of -6 dB includes esp_codec_dev's modeled hardware gain;
it is not the DAC register attenuation. Measured REG32=0xae is -8.5 dB
relative to 0xbf (0.5 dB steps). NS4150B resistor gain is 240k/100k=2.4x,
about 7.6 dB; the upstream 6 dB value is software model metadata.

All Jabra/acoustic/live evidence before 2026-10-14 12:45 local is potentially
contaminated by the retired Waveshare joining the same bot and playing audio.
Those results do not independently qualify EchoEar playback. The cancelled
soak remains partial evidence; the first run measured eight lost frames.

Startup fix validation: `A/codec-startup-3` passed boot, WiFi, ping, 16 MiB
PSRAM and register assertions, with zero strict raw-monitor errors. The
first REG44=0x08 write returned ESP_ERR_INVALID_RESPONSE; the single retry
read 0x08, and completed initialization read REG0D=0x01, REG44=0x58,
MIC1_GAIN=0x18. The earlier instrumentation run intentionally failed a wrong
final REG44 expectation (0x08); that assertion is corrected to the driver's
final reference setting, 0x58. All evidence remains retained.

### DMA scheduling and amplifier lifecycle

The old 5 ms live-write timeout occurred continuously, including the partial
soak, rather than only on TX enable. A 240-sample DMA completion releases
15 ms of capacity; interpolation could schedule a 320-sample write after
20 ms while only 240 samples were writable. The next completion was 10 ms
away. SpeakerClock now waits for actual completed descriptors before issuing
the render opportunity, retaining the original media deadline and the
existing 5 ms bounded write and suffix retry. No error counter is suppressed.
PERF exports I2S write errors/timeouts; the transport gate requires zero
write-error and underflow deltas. Bounded error logs include esp_err, bytes,
timeout and sample ledger. Host audio tests: 43/43; focused HIL tests: 51/51.

PA control is removed from the vendor driver's enable sequence. Speaker start
starts TX, unmutes the DAC, waits 5 ms, then raises GPIO15. Idle lowers GPIO15,
mutes the DAC, then disables TX. The codec stays initialized and RX clocks
continue while playback idles. This preserves the shared-clock geometry.

Post-contamination-cutoff live validation `A/dma-live-1`: one complete live
turn passed with exact prompt recognition and pineapple reply. Jabra room
-57.07 dBFS, reply RMS -24.335 dBFS, rise36.352 dB. Reply transport counters
including write errors and underflows all had zero deltas. Cumulative capture
errors, write errors, timeouts and microphone clips were zero. First TTS byte
1153 ms and audible reply1529 ms after prompt end. This is one clean live
turn, not a replacement five-turn qualification or a soak. The old timeouts
are eliminated in this run; audible click causation is not independently
established by comparing the contaminated old recordings.

### Volume policy

The user-heard level is the top of the standard range. `playback-volume DB`
selects attenuation from -36 to 0 dB after DSP; positive gain is rejected.
EchoEar boots at -6 dB. Q15 scaling cannot increase PCM amplitude. The E2E
qualification harness explicitly selects 0 dB to retain the declared
-22 dBFS Jabra reference gate; this is separate from the quieter boot default.
The tone sweep tests 0/-3/-6/-9/-12 dB with fixed11% speech-level and26%
peak-ceiling tones, measuring native16 kHz Jabra RMS/peak/THD plus accepted
I2S PCM peak, without a Waveshare frequency-response correction.

The clean live decoded stream peaked at18228/32768 (-5.094 dBFS), with zero
full-scale samples. Its output normalizer targets-22 dBFS and limits peaks
to26%; DAC REG32 remained0xae (-8.5 dB). These facts do not show digital
overflow. Acoustic distortion and PA/speaker attribution require the sweep;
Jabra measurements alone cannot uniquely separate PA from speaker distortion.

Volume evidence `A/volume-2`: room-60.69 dBFS; all ten points passed the
predeclared THD<5%, H2/H3<=-26 dBc, zero clipped samples and20 dB room-rise
gates. No I2S write errors. Maximum stays0 dB; default is-6 dB; range-36..0.
The first attempt (`volume-1`) stopped before tones because its disconnected
PERF snapshot lacks transport fields; the harness now reads AUDIO counters.

| Offset dB | Speech-level THD % | Peak-ceiling THD % | Peak-ceiling Jabra peak dBFS | Accepted PCM peak |
| --- | --- | --- | --- | --- |
| 0 | 0.067 | 0.202 | -10.88 | 8519 |
| -3 | 0.081 | 0.131 | -13.87 | 6030 |
| -6 | 0.084 | 0.081 | -16.76 | 4269 |
| -9 | 0.128 | 0.085 | -19.60 | 3022 |
| -12 | 0.225 | 0.087 | -22.44 | 2139 |

The current maximum produced0.202% THD at the26% peak ceiling, with accepted
PCM peak8519 (about-11.70 dBFS), and no digital or Jabra full-scale clipping.
This does not reproduce clipping in the fixed1000 Hz test. It supports
keeping the current maximum; it does not uniquely locate a transient click
or prove absence of distortion on every speech waveform. The earlier live
write timeouts are fixed and the clean live path independently passes.

### Display pin ownership consolidation

Adopted B's exact board implementation from122426d: the first app_main action
is `dg_board::early_board_init()`, which owns GPIO9 rail power/settle and
GPIO44 off/ROM pull clearing. `dg_board::lcd::reset()` owns GPIO47 latch-before-output
and10/120 ms minimum reset timing. `dg_board::backlight::init()` owns GPIO44 LEDC and
uses1023 on EchoEar to retain B's qualified brightness. B removes its former
display-local owners in the same commit. There is no second early_init API;
A's board/header/main files match B's implementation for merge compatibility.

### Microphone review controls

`mic-analog-gain 24|30|33` changes only MIC1, preserving enable bits and
verifying REG43. The pinned driver's get_db(33) returns GAIN_30DB despite
its enum defining GAIN_33DB=11; the board control writes the actual0x1b
nibble for33 dB. `mic-gain-status` asserts the expected register and reports
the analog/digital tuple. Every E2E turn checks that tuple after calibration,
including warm turns. Calibration targets move with analog gain to preserve
the physical prompt level:24 dB/-45 dBFS;30 dB/-39 dBFS;
33 dB/-36 dBFS. These are measured-PCM targets, never SPL estimates.
The initial30 dB/200% selection below is superseded by30 dB/125% for
speaker-echo headroom. Calibration preserves the configured digital gain.

Capture enable discards the first1600 samples (100 ms), sending intentional
silence in those intervals to retain transport cadence. In-turn microphone
clip counts exclude settling and must have zero delta over the complete
prompt/reply window. Missing counters, counter resets or session changes
fail the gate. Boot checks also require at least14,000,000 bytes of external
heap regions plus the16 MiB physical PSRAM identification and no virtual
address truncation warning; XIP consumes the remainder of physical PSRAM.

### B3 reply-gate handoff clarification

B3 item5 describes the older unavailable SPL gate. Commit07692a7 replaced
it with direct Jabra RMS and already passed `A/dma-live-1` at-24.335 dBFS.
The current master and echoear-display refs both contain that implementation.
The gate now also records and requires reference volume0 explicitly; the
attenuated boot default is not scored against the maximum-volume reference.
Missing room-rise/samples still fail; SPL fields stay null and the diagnostic
bypass is unchanged. Focused regression reproduces the measured passing
level and rejects the same measurement if volume metadata says-6.

### A4 startup clarification

The verified one-shot REG44 retry is the fix. The50 ms rail settle is margin
only: the same NACK was observed after50 ms and successful address probes.
The200 ms deadline includes rail settling and I2S setup; it is not200 ms of
additional probe time. The first REG0D read is now logged to establish whether
its reset value makes the driver's initial0xFA write unnecessary.

The ES8311 configuration explicitly sets no_dac_ref=false; a single derived
C++ constant supplies both final REG44 assertion and expected-value telemetry.
Shared host boot checks compare actual against that emitted expectation.
This internal ES8311 ADC reference is unused in DAC-only operation and is
separate from the ES7210 MIC3 analog AEC path. No reference routing is changed.

Remaining A4 risks: unexpected codec failures still abort into the bench
reboot/failure path; a product audio_fault state retaining WiFi/console is
future work. ES7210 uses the vendor I2C control, so strict host I2C-error
rejection and MIC1 register assertions remain necessary. Only the proven
initial REG44 NACK is recoverable; a REG0D failure continues to fail closed.

### Display review B2 / A3B3 integration

GPIO9/47/44 now have one module owner, `dg_board`: early power/settle is the first app_main action; reset and LEDC follow through board helpers. The backlight ROM pull-up is cleared before LEDC. The census fails display initialization if the 16 MB chip provides less than 14,000,000 bytes of PSRAM heap (previous image measured 14,554,892 bytes). This is a decimal byte floor, not 14 MiB. Whole-cache-line DMA staging remains asserted.

App-control uses a 16 KiB PSRAM stack; HIL requires at least 2048 free bytes. On bot-ready, the device sends `display-ready` with v, epoch, next_seq, wire_bytes=1023 and max_ops=8. `server/display_client.py` consumes this message, supplies monotonic sequence/epoch metadata, checks the compact UTF-8 data-object byte limit, and sends via RTVIProcessor.send_server_message. Eight operations is the engine limit; the complete wire request must also fit 1023 bytes, so large batches need splitting. Cat benchmark submissions do not consume the bot's sequence numbers.

Admission waits at most the 5 ms tick conversion plus one tick (scheduler delays are measured separately); `disp wire-wait 0` in Idle retains the old zero-wait policy for the required baseline comparison. `disp wire-status` separates received/accepted, mutex-busy, queue-full and invalid counts, maximum measured mutex wait and deferred-log drops. Each parsed request gets a `display-result` client message with epoch, seq, accepted/error and wait_us. This acknowledges admission, not render completion. DISPLAY_WIRE uses the bounded deferred diagnostic logger. No automatic retry is hidden by the bot sender.

The production bot's DisplayClient is shared with the explicit live-test/offline-test command-file adapter (`DISPLAY_HIL_COMMANDS`). The old transport monkeypatch is removed. Wire and voice runs both use SCTP chunk size1100. Strict live HIL defaults to >=3 original turns, checks boot ELF/codec readback/no GPIO conflict, PSRAM census, internal minimum81920/largest32768/failed allocations0, stack>=2048, >=24 rendered fps in a measured bracket inside a reply, and a bot wire show completed in that bracket. It gates capture drops/read errors/clips, playback write_errors and I2S underflows on zero deltas, transport damage counters, uplink continuity, raw driver errors and one bot peer. Acoustic gates remain unchanged; failed/missing evidence cannot qualify. Hardware results for this revision remain pending.

Flood proposal only: bound peer-loop work and message bytes before parse/lambda-copy/dump (currently three message-sized internal allocations before app truncation), instrument terminal-dead setters and admission loss causes, and enforce an internal-heap bound. The parser's PSRAM budget does not bound those peer-loop allocations. No flood implementation is included.

### Post-isolation B review validation (2026-10-14)

B checkpoint0a7ad44 includes master07692a7 (codec startup and DMA writable-capacity fixes); the render-proof harness correction iscb15696. Tested ELF SHA256 `a1ef70d2910e2ef349bd53c7daa3c71fec49e25286a53d14558c36679efd6876`. Sources/builds and all evidence are retained separately. EchoEar and legacy builds pass; both display host geometries pass46/46, with no legacy hardware access.

Fresh panel-B2-1 passes8/8 staging CRCs, zero raw boot/SPI/DMA errors and actual GDMA tx_pri1. Cat-B2-1 measures29.34fps idle and29.09fps during persist, internal minimum157920/largest118784, failed allocations0. The camera sees the quadrants and cat; saturation limits optical color claims. The latest image's census reports PSRAM heap14550356 bytes, cache32 and10240-byte staging halves.

With the same latest ELF and100 wire shows under cat, the zero-wait baseline rejects4% on mutex contention. Bounded5 ms-plus-tick admission rejects0/100; max measured mutex wait712us, no queue-full/invalid errors or deferred-log drops. Wire session internal minimum89004/largest49152; app-control stack minimum9036. Both use the device-published epoch and production bot sender, not serial epoch injection. These numbers describe the measured population, not a guarantee of zero future admission loss.

`B/voice-cat-B2-strict-2` completes three original live turns on one bot peer. Each scored reply passes acoustic/content gates, zero reply damage/write-error/underflow deltas, and a distinct wire show in a completed cat frame. Reply brackets measure26.80,27.05,27.67fps. Campaign internal minimum87488/largest47104, failed allocations0, app-control free8972, zero display monitor errors. The compositor can mark an applied request Superseded by a later cat revision; the harness therefore proves completed revision>=applied wire revision and exact visible layer7 hash/position. It does not accept a superseded request that never applied or disappeared.

**The strict campaign fails; GRAPHICS WORKING is not declared.** Uplink tails exceed both unchanged rates:50/3815 intervals over60ms (1.3106% >1%), and2/3815 over150ms (0.05242% >0.05%), maximum238ms. Separately, full-turn snapshots add9 lost/concealed frames after reply1 and3 after reply3. I2S underflows increment once between each pair of turns, outside the scored reply windows; write_errors stay zero. Thus three clean replies do not prove a clean whole session. Whole-turn transport deltas are now independently gated as well as reply deltas, and decode/PCM/uplink-queue drops fail closed. `whole-turn-audit.json` preserves the stricter audit without changing the original failed result. No network settings or thresholds were changed, and no blind retake was launched.

The first strict campaign is also retained: one clean reply, then an insufficient render-result-state check on turn2, plus an independent166ms uplink gap. Earlier pre-12:45 acoustic/live evidence is potentially contaminated by the retired Waveshare; wire-1 logs contain two overlapping peer IDs without board attribution. The user unplugged the retired board; current campaigns contain one peer. All B-owned test processes and bench holds are released. Remaining work is the transport/late-silence loss and inter-turn underflow diagnosis; the flood work remains a proposal awaiting review.


### Strict display merge after master73dc840

Rebased B onto master73dc840, including698d644's verified codec startup retry,
master's adoption of the single dg_board pin owner, volume0 qualification and
analog/digital gain readback. EchoEar and legacy builds pass; no legacy hardware
was accessed. Monitor/merge22, E2E helper7 and codec/calibration/playback60
host tests pass. Exact tested ELF SHA256:
`5a8c913e4a551bd4e9270e30a7217b83b49be6810bcfea81e6356a3dc67390bb`.
Retained campaign `B/voice-cat-master73-strict-1` uses three original turns,
one warm bot peer, SCTP chunk1100, and no retries. Its codec boot assertion
passes (REG0D=0x01, REG44=expected0x58), gain24 dB/400%, reference volume0.
PSRAM heap14548992 bytes exceeds the14000000-byte floor; cache32 and DMA
half10240 remain consistent. The newer analog30 dB choice was not in this
master snapshot and is not claimed here.

Each reply renders a distinct real-bot wire show using published epoch
233481753427555, seq1/2/3, with accepted acknowledgments and completed-frame
layer7 proof. Reply FPS is26.2013/27.1334/26.7329. All reply transport counters,
capture/audio error counters and in-turn microphone clips have zero delta.
Campaign internal minimum87208 bytes, largest49152, failed internal and total
allocations0; app-control minimum free8960. Strict raw/display monitors report
zero errors. Uplink tails pass:38/3816 over60ms (0.995807%), none over150ms,
maximum eligible136ms. Declared idle intervals remain separately reported.

**Overall FAILED: two of three original turns pass.** After reply3 and before
its final turn snapshot, drift_drops and drift_inserts each increment once;
the stricter whole-turn gate rejects this even though the reply bracket is
clean. Lost/concealed/playout-drop counters stay zero in all turn windows.
Separately, I2S underflows rise0->1->2 and device_silence_blocks0->61->122
between turns; the retained snapshots do not establish the exact inter-turn
phase or cause. Playback write_errors remain zero. This is evidence of drift
correction and inter-turn underflow, not proof of network packet loss.
`summary-audit.json` records the phase snapshots without altering the original
failed result. No threshold is relaxed and no unchanged-image retry follows.
GRAPHICS WORKING remains unqualified pending audio/transport investigation.
### Quiet analog-gain selection

`A/mic-ab-1` acquired after isolation with Jabra room-60.76 dBFS. Both
settings had zero native ADC clips and zero software clip delta under the
same fixed host tone and spoken stimulus:

| MIC1 analog / digital | REG43 | Native floor dBFS | Tone RMS dBFS | Tone SNR dB |
| --- | --- | --- | --- | --- |
|30 dB /200% |0x1a |-57.58 |-31.53 |30.57 |
|33 dB /142% |0x1b |-53.03 |-28.51 |31.31 |

The initial selection was30 dB/200%:33 dB adds only0.74 dB measured SNR, below the predeclared
3 dB benefit needed to trade away headroom. Initialization now sets only
MIC1 gain after ADC enable and asserts0x1a. MIC2 remains unused for mono;
the reference input's gain is not increased. The normal prompt calibration
target becomes-39 dBFS native active RMS (quiet variant-46), preserving the
approximately36 dB combined analog/digital gain and physical prompt level.
All historical24 dB results retain their original gain labels.

This acquisition also records initial ES8311 REG0D=0xfc: the driver's first
REG0D=0xfa write succeeds, then the first REG44 write NACKs and the verified
retry succeeds. The known quirk is specific to that REG44 transaction in this
boot, rather than every first write to any register.

The selected-default validation in `A/mic-selected-live-1` booted with
MIC1=0x1a and digital200%, and calibrated the prompt to -39.12 dBFS against
-39. Room baseline was -59.41 dBFS; the bot log contained one peer. In-turn
clips and all13 reply transport deltas were zero, including write errors
and underflows; decoded reply correlation was0.9875 and board-mic STT
recognized pineapple. **The overall live run failed:** Jabra reply RMS
was -26.71 dBFS at volume0, outside the unchanged [-25,-19] window, despite
33.86 dB rise. A separate whole-turn audit also rejects one drift drop
(two drift inserts); lost/concealed/write errors/underflows stayed zero.
This validates the gain path, not full voice qualification or5/5 acceptance.
The harness now restores normal -6 dB playback after each turn and explicitly
selects qualification0 again for warm turns. That cleanup change was made
after this process imported the harness and is covered by source review;
it was not exercised by this acquisition.

The same quiet acquisition supplies the deferred ESP-to-host SNR retake:
1500 Hz output reference at20% amplitude, Jabra fundamental -15.96 dBFS,
noise -60.47 dBFS, **SNR44.51 dB**, THD0.237%, zero clips. Noise integrates
100 Hz to8 kHz excluding +/-20 Hz around the fundamental and harmonics;
this is a spectral tone SNR, not speech SNR or calibrated SPL. Raw evidence
is `run1/calibration/output-reference-jabra.wav` under that acquisition.
No old potentially contaminated acoustic result was promoted to acceptance.

### A5 follow-up

A5 independently approves the completed-descriptor write fix and PA ordering.
Volume storage is now atomic with one snapshot per PCM block; accepted-peak
publication uses a compare/exchange maximum of only the current block. A reset
can include an already in-flight block, but cannot resurrect an earlier peak.
Production callers already serialize Speaker writes and volume changes with
speaker_mutex; this also makes the volume storage safe for standalone callers.
The 49-decision one-second clock test documents its initial descriptor deferral.

The tested maximum remains0 and the shipping default remains-6 dB, as requested.
Speech/STT/turn-taking evidence at qualification0 does not qualify those behaviors
at the default. The existing sweep is1 kHz only:300 Hz at0/-6 dB and a Jabra
capture around PA enable/disable remain unmeasured follow-ups. They require a
short free bench hold, not a soak. No higher maximum or broader distortion
claim follows from the attenuation-only sweep.

### Transport diagnosis: calibration versus real traffic

Retained B strict-1/2 traces show the166 ms uplink gap overlapping a225.6 ms
stats dump, but the238 ms gap does not overlap a stats window. Host downlink
send cadence has no >60 ms gaps in either campaign. PA remains enabled and
the device epoch stays constant throughout all three strict-2 turns, excluding
PA/TX idle transitions as the cause of those particular late-silence losses.
These observations do not establish the cause of every uplink or downlink gap.

`A/transport-logging-1` compares quiet1/quiet0 with/without a stats command
inside four7-second arms. All arms have zero lost/concealed, I2S underflow,
write errors, or drift corrections. Host uplink maximum gaps are102/163/67/74
ms; ESP socket-send maxima are45.24/43.65/41.29/41.33 ms. Thus those observed
uplink delays occur after the ESP socket call; stats correlation alone does
not prove capture/encoder starvation. Device traces have zero overwritten
records. The transport probe reports diagnostics, never qualification passes.

`A/transport-tone-1` isolates a concrete harness defect: a1200 ms direct
console tone during an active media session adds exactly one I2S underflow;
control and streamed pineapple fixture arms each add zero. The direct tone
holds speaker_mutex while bypassing scheduled render ticks. Firmware now
rejects direct tones while media is ready, before onset or writing PCM.
Warm calibration omits this independent output-tone diagnostic, explicitly
marking it unavailable, while retaining measured input prompt calibration,
streamed response calibration, gain assertions and every reply/transport gate.
Cold calibration still measures the output tone before connecting.

A host packet-capture attempt in `A/transport-tone-fixed-1` stopped before
measuring phases because dumpcap is not executable by this account; no
permissions or network settings were changed. Endpoint timing remains bounded
by socket observations, not radio capture. All soaks remain cancelled.

Hardware verification `A/transport-tone-fixed-3` confirms the active tone is
rejected and subsequent lost/concealed/underflow/write-error deltas stay zero.
`transport-tone-fixed-2` is retained as a harness error: it expected verbose
exception text but firmware returns the generic `ERR #N tone`; it is not
counted as a passing acquisition.46 audio host tests and68 focused HIL tests
pass, plus2 analysis threshold tests. No acceptance threshold was changed.

The later `A/transport-tail-1`20-second fixture/post-reply trace also has zero
lost/concealed, drift edits, underflows and write errors. Host uplink max88 ms
(8/999 >60 ms), ESP socket send max46.53 ms, host downlink max22 ms, device
receive max56.54 ms. The enabled existing bot event-loop probe reports no
>20 ms stalls, and the device trace has no overwritten records.

After fast-forward to merged master0b3dc43, `A/transport-cat-tail-1` repeats
that20-second measurement with the production cat. Display29.15 FPS, strict
monitor clean, app-control stack minimum8968 B. All damage/drift counters
remain zero; host uplink max124 ms(4/998 >60 ms), ESP send max48.50 ms, host
downlink22 ms, device receive44.29 ms, render30.54 ms. Again no host loop
stall >20 ms or overwritten trace. These are fixture transport diagnostics,
not live STT/TTS or complete voice-plus-graphics qualification.

The evidence separates the reproduced direct-tone harness failure from
remaining endpoint-path variability. Quiet/stats arms did not reproduce
capture/send starvation; post-reply fixture arms did not reproduce the old
late-silence loss or drift drop. Host and ESP power saving are already off,
and no network settings were changed. The old B failures lack device packet
traces, so their exact source remains unresolved. A requested single live
trace could not acquire the bench while B began its updated strict campaign;
no live diagnostic was started and B's campaign retains priority.

The subsequent `A/transport-dump-1` reproduces late packet damage during a
bulk raw-mic USB export with cat animation:3 lost/concealed frames, device
receive gap185.55 ms and host uplink gap173 ms. ESP send calls stay within
50.89 ms, render within31.25 ms, and no I2S underflow/write error occurs.
All three PLC events fall inside the export. This isolates an export-related
failure; the precise WiFi/driver mechanism and every historical loss remain
unproven. Control and decoded-PCM export arms have no packet loss.

Both PCM exports now yield4 ms after each192-sample chunk, outside stdout
and audio locks. `A/transport-dump-paced-1` repeats the same probe on the same
merged firmware baseline plus this change. All three arms have zero loss,
concealment, drift corrections, I2S underflows and write errors. During raw-mic
export, device receive max50.26 ms, ESP send44.24 ms, host uplink62 ms
(1/583 >60 ms), render30.26 ms. No traces are overwritten, the strict display
monitor passes, and exported chunks are contiguous and complete. Decoded PCM
export takes6.19 s for280640 samples; raw mic8.65 s for391360 samples. Existing
30-second mic/120-second decoded export deadlines remain sufficient. Both
board configurations build. This short diagnostic supports pacing as a
mitigation; it is not a full live campaign or radio-level causal proof.

B's separate quiet/no-mid-turn-PERF acquisition passes uplink timing and all
whole-turn transport deltas but stops on8 real microphone software clips in
turn2. Native ADC peak17168 does not clip; replaying the production high-pass
and200% gain yields exactly8 saturated samples during the reply. That remaining
gain-headroom issue is independent of the repaired calibration underflow and
bulk-export interference. No acceptance threshold or clip exemption changed.

### B3 protocol and measurement follow-up

The bot now awaits correlated admission acknowledgments, with one in-flight
request and a shared budget of two retries for explicit `queue-full` rejections
or acknowledgment timeouts (the latter added after Fable B7). Explicit queue-full
retries use the same ID and a fresh sequence. Timeout retries replay the exact
epoch, sequence, ID and snapshotted payload: the engine duplicate check includes
all of these fields. Each acknowledgment wait defaults to 3 seconds; retries
back off 20 then 40 ms. Exhaustion reports admission unknown, never success.
An evicted duplicate may return stale-sequence; that rejection remains terminal,
with no fresh-ID/sequence fallback that could reapply an ambiguous operation.
An epoch change stops the request rather than replaying it into a new session.
A matching `stale-epoch` result clears readiness until the next device
publication; old-session acks cannot invalidate a new epoch. `send()` returns
only after accepted admission, not completed rendering. The HIL file bridge
marks a command consumed after success or a logged terminal failure, and retries
known not-ready races at most three times. Invalid commands and ambiguous sends
remain explicit failures. Wire rejection experiments set retry budget0 so their
original-attempt populations remain comparable.

The device echoes independently validated id/epoch/seq even if operation parsing
fails, and includes `parse_failed`. An unparseable/truncated envelope cannot be
safely attributed to display and is still rejected by the existing app boundary;
there is no regex recovery of untrusted truncated JSON. Recognized display
messages lacking usable metadata return zero/empty correlation plus parse_failed.
`display-result` goes through an eight-entry PSRAM queue to app-session, one ack
per idle worker iteration, with session actions taking priority. The control
thread only enqueues the fixed-size record. `disp wire-status` counts ack_sent,
ack_queue_full, ack_send_failed and ack_stale; strict merge requires all three
failure counters0. Exceptions from transport enqueue count as send failures.

The raw error monitor still fails on every E-level match, regardless of component.
It now records the matched line, component, byte offset and display/non-display/
unknown classification; it introduces no E-level exemptions. The known startup
retry remains a warning. The console PERF helper uses `stats`, matching the merged
firmware command, with a regression test for that contract.

Strict live-test mode scripts a longer pineapple reply. In addition to the
contained short FPS bracket and mid-reply wire show, the entire first-TTS-byte to
gate-reopen interval must last at least5 seconds and sustain a conservative
rendered-FPS lower bound of24. Only frames between snapshots provably inside that
interval count; elapsed time includes the full interval and edge frames receive
no credit. Periodic200ms-plus-command snapshots retain raw device and host times.
This is a lower bound, not an interpolated exact frame count. The full interval
and raw samples are retained, and missing endpoints fail. Previous three-turn
campaigns are historical evidence and do not acquire this new full-reply verdict.


B3 first campaign `B/voice-cat-B3-strict-1` is retained as failed after its first
original turn. The short bracket measured27.61fps, but the full11.616s interval
had a19.886fps lower bound. Repeated full stats calls each took about200ms;
this is measurement overhead, not evidence sufficient to blame all missed frames.
Whole-turn audio/transport counters, ack-delivery counters, allocations and raw
monitor were clean; Jabra RMS-23.276dBFS and uplink tails passed. No further turns
were attempted after the failed gate. The harness now samples a small
`disp frame-clock` record (device timestamp plus rendered counter) every200ms
instead of serializing full PERF repeatedly. The firmware releases the compositor
mutex before printing that record. Full PERF snapshots remain at the existing
turn and short-bracket boundaries for all memory/audio gates. FPS thresholds and
conservative whole-interval arithmetic are unchanged; the next campaign tests
this concrete instrumentation change without rewriting the failed result.


### B3 full-reply campaign after compact sampling

`B/voice-cat-B3-strict-2` tests d439c9d on master0b3dc43, exact ELF SHA256
`3d28d14979dcaf821f713bd08cf0d5830fa14de840e3ab56601270cda42bb97c`.
All three original turns pass, on one warm bot peer, with no retakes. Full-reply
FPS lower bounds are28.3192/28.2254/28.3897 over11.4763/10.8767/11.8705s.
Contained short brackets are27.7186/26.6505/26.6502fps. Frame-clock requests
have median2.060ms and maximum5.684ms across152 observations, versus the prior
full-stats median198.158ms; the failed first campaign remains unchanged.

Three distinct real-bot shows use device-published epoch312940106762191 and
seq1/2/3; each is admitted first attempt and proven in a completed frame during
the reply. Final wire counters: received3/accepted3/ack_sent3, busy/queue-full/
invalid/ack-queue-full/ack-send-failed/ack-stale/log-drops all0, max admission
wait10us. Host tests exercise retry, stale epoch and malformed-ack failures;
the live campaign verifies the successful admission/worker-delivery path.

All thirteen reply and whole-turn transport deltas are0 in all three turns,
as are capture/drop/decode/PCM/uplink-queue/write-error/microphone-clip deltas.
The sampled cumulative underflow and device-silence counters also remain0 across
the warm transitions after A's calibration fix. Jabra RMS values are
-22.823/-23.069/-23.499dBFS at volume0; gain30dB/200% assertions pass.
Internal minimum88604 bytes, largest49152, failed internal and total allocations0,
app-control minimum free8984; PSRAM heap14548692. Raw/display monitor errors0,
no display/SPI/DMA/recovery failure, cache32/DMAhalf10240, one bot peer.

**Overall strict campaign still FAILED solely on the uplink tail rate:**
51/4530 eligible intervals exceed60ms (1.125828% >1%); none exceed150ms and
eligible maximum117ms. Explicit idle intervals remain separately reported.
The earlier drift and inter-turn underflow failures are absent from this measured
campaign, but this does not waive the independent uplink requirement. Retained
`result.json` and derived `summary-audit.json` distinguish all per-turn passes
from the failed campaign. No further unchanged-image attempt is made.
GRAPHICS WORKING qualification remains blocked on uplink timing; B3 requested
source fixes, full-reply display measurement and real-bot rendering are complete.


### Console timing attribution and quiet acquisition

The retained166ms uplink gap in B/voice-cat-B2-strict-1 starts42.673ms after a
stats command begins and overlaps its225.612ms dump. AUDIO_STATE anchors give
1.164ms spread in host/device offset. This establishes timing correlation;
it does not prove causation. The historical calibration source explicitly sent
quiet1, raw quiet acknowledgments omit the argument, and no verbose capture
reports or mic-stats toggles were retained. A quiet0 attribution is unsupported.
`stats-gap-attribution.json` preserves the calculation without changing verdicts.

Strict acquisition now explicitly acknowledges quiet1 after calibration and
issues no full PERF query from prompt start through gate reopen. Short and full
FPS brackets use the compact frame-clock query; before-reply transport counters
come from the completed pre-prompt snapshot. Full memory/audio/transport snapshots
remain before the prompt and after gate reopen/turn end. A host query-window
assertion rejects any accidental mid-acquisition PERF overlap. All prior rate,
FPS, memory, stack, clip, audio and acoustic thresholds remain unchanged; earlier
quiet-NIC observations are not used to exempt uplink tails or waive the150ms gate.


`B/voice-cat-quiet1-no-perf-1` tests harness a0a8d2a on the same d439c9d ELF,
with acknowledged quiet1 and unchanged thresholds. The first original turn
passes; the second fails on8 microphone clips and stops the campaign before
turn3. Full-reply FPS lower bounds29.0038/29.3000 pass, both bot shows render
mid-reply with clean ack delivery, and every whole-turn transport/write-error/
underflow delta is0. Host query windows and independently aligned raw
CONSOLE_WINDOW records show no full stats dump overlapping either prompt/reply.

Uplink passes over the acquired population:5/2656>60ms (0.188253%),0>150ms,
max90ms. This is consistent with reducing console interference; the historical
42.673ms coincidence and this changed acquisition do not establish a cause for
every prior gap. There are only two acquired turns, so this is not a three-turn
qualification pass. Internal minimum87132 bytes, largest47104, failed internal
and total allocations0, stack8984, PSRAM14548692, raw/display errors0, one peer.
Cumulative lost/concealed counters are already8/5 before the first scored turn
and do not increase during either scored turn; the whole session is not claimed
loss-free. Original failure evidence remains unchanged.

The run2 clip counter increment is reproduced exactly from its retained raw
microphone PCM with the firmware float32 high-pass recurrence and200% gain:
eight saturated samples at9.47025..9.4863125 seconds into the recording, about
5.35 seconds after the matched reply reference begins. Raw peak17168 is below
ADC full scale; filtered/gained pre-clamp peaks exceed32767 (minimum-33683).
This supports digital-gain clipping during the reply region, not a startup-only
counter artifact. `clip-reconstruction.json` and `summary-audit.json` retain
the calculation and raw dump-window audit. No gain, volume, calibration or clip
threshold was changed. Remaining acceptance requires A's audio investigation
and a complete strict campaign; GRAPHICS WORKING remains unqualified.


## GRAPHICS WORKING (display, protocol, memory, transport), 2026-10-14

The explicitly requested single fresh campaign
`B/voice-cat-master0b3-fresh-1` passes all three original turns and every
aggregate strict gate, with no retakes and unchanged thresholds. The tested
integration is `60ab701`, a descendant of requested audio merge `0b3dc43`
and warm-calibration fix `964d3ea`, retaining the subsequent B3 display fixes.
ELF SHA-256 is
`3d28d14979dcaf821f713bd08cf0d5830fa14de840e3ab56601270cda42bb97c`.
This is the same image as the previous quiet campaign, not a new firmware fix.

Full first-TTS-to-gate-reopen cat FPS lower bounds are
28.788250 / 28.725824 / 28.981806 over 11.914583 / 11.418297 / 11.524472
seconds. Three real-bot wire shows are admitted and rendered mid-reply,
with one warm peer throughout. Final wire counters received/accepted/ack_sent
are each3; busy, queue_full, invalid, acknowledgment failures/staleness and
log drops are all0, maximum admission wait11us. Published epochs and correlated
acknowledgments drive the bot path. Explicit quiet1 and compact frame-clock
queries are used; full PERF dumps never overlap prompt-to-gate acquisition.

All13 whole-turn transport deltas and reply audio deltas are0 on all three
turns. Independently inspecting all13 raw PERF samples finds cumulative
lost, concealed, PLC frames, drift drops/inserts, I2S underflows/write errors,
device-silence blocks and in-turn microphone clips all0, including warm-turn
boundaries. There is no drift event to hand off from this campaign. Jabra reply
levels are -22.808402 / -23.653881 / -23.173389dBFS at volume0, with room rise
38.28 / 37.09 / 37.66dB; the gain path remains30dB/200%.

Uplink passes:18/4490 eligible intervals exceed60ms (0.400891%, limit1%),
none exceed150ms, maximum105ms. Internal minimum free88160 and largest49152
exceed81920/32768 floors; internal and total failed allocations0. App-control
minimum stack free8984 exceeds2048. PSRAM heap14548692 exceeds14000000,
with16MiB chip,32-byte cache line and10240-byte DMA half. Strict raw/display
monitor has no errors; codec boot, GPIO ownership and DMA-priority checks pass.

`result.json` retains the original passing verdict; `summary-audit.json`
independently recomputes the merge gate and records these measurements. This
qualifies the display deliverable on one strict3/3 campaign. Audio front-end
margin is **not qualified on this200% ELF**, and VOICE+GRAPHICS remains blocked:
one of five live turns across the preceding quiet campaign and this campaign
clipped speaker echo at volume0. The passing campaign is a retake of a
content-dependent condition, not a fix. The earlier failure remains an open
audio qualification issue, not merely historical evidence. Flood robustness
remains proposal-only pending review.
### Microphone echo headroom correction

The new default is30 dB analog (REG43=0x1a) and125% digital gain. Replaying
all five retained B3/quiet campaign turn WAVs through the production float32
high-pass gives3.84–6.04 dB peak headroom at125%. On the failing waveform,
filtered peak16841.715 becomes21052.144 rather than33683.43 at200%.
The proposed150% alternative would leave only2.26 dB and was not selected.
Raw ADC headroom and the analog SNR choice remain unchanged. No limiter or
half-duplex clipping exemption was introduced.

Calibration now retains the actual configured digital gain instead of
silently restoring200%. Native active speech remains targeted to-39 dBFS
(quiet variant-46); no physical prompt increase compensates for lower gain.
The nominal post-gain target is-37.062 dBFS before high-pass effects,4.08 dB
below the old processed target. Replaying the retained calibrated prompt at
125% gives the exact full transcript with prerecorded STT; this does not
establish live VAD/recognition performance.

Each EchoEar E2E turn now measures pre-mute headroom from its native PCM and
requires at least3 dB in addition to the unchanged firmware clip-delta0 gate.
The reconstruction discards its first100 ms solely to remove unknown initial
filter state; it does not exempt firmware clip counts or closed-gate echo.
Both board builds and84 focused HIL tests plus3 subtests pass. Evidence:
`A/mic-headroom-retained.json`, `A/mic-headroom-prompt-replay`, and the short
live validation recorded below. No soak is authorized or performed.

`A/mic-headroom-live-2` verifies actual30 dB/125% readback, clean room
-60.40 dBFS, native prompt-39.136 dBFS against target-39, exact live prompt
transcript, zero in-turn clips and5.344 dB reconstructed headroom at volume0.
Jabra reply-23.099 dBFS passes, decoded correlation0.98761 passes, full-reply
FPS28.966 and strict display monitor pass; one bot peer. Uplink passes:
3/776 intervals >60 ms (0.387%), none >150 ms, maximum77 ms. All reply-window
transport deltas are0, including underflows/write errors.

**The single run remains failed overall:**2 lost/concealed frames appear
after the clean reply snapshot, between device62.066615 and70.790021 seconds,
before either PCM export. Host downlink maximum send gap is22 ms in that
interval. PA stays on from32.321 to89.824 seconds, so this particular loss
does not coincide with an idle/start transition. No device arrival trace was
enabled in this gain test; its exact receive-path cause remains unresolved.
The microphone correction is measured, but this is not a complete strict
campaign or a transport pass. Bench released, no unchanged-image retry.
`mic-headroom-live-1-identity` retains the earlier environment failure:
the HIL Python lacked esptool and stopped before touching hardware. The
successful flash for live-2 used the guarded IDF wrapper.


### B5 qualification correction and next image

Master `b2f5ab3` incorporates A's `c133924` (125% microphone digital gain,
per-turn minimum3dB reconstructed pre-mute headroom) and `16690d5` (paced PCM
export). The next explicitly requested single strict three-turn campaign on
this new image is the VOICE+GRAPHICS qualification attempt. Thresholds remain
unchanged. Both gate-open and gate-closed clip deltas are reported separately
and must be zero; missing, reset or inconsistent split counters fail closed.
The split uses the actual PCM-block mute decision, with the existing capture
enable and100ms settling eligibility. It distinguishes saturation in unmuted
capture from front-end saturation while muted; neither state is exempted.


### B5 new-image campaign: readiness telemetry failure

The single requested campaign `B/voice-cat-B5-125-strict-1` on `ad710d4`
(atop `b2f5ab3`) fails closed before its first scored prompt; no second campaign
or retake was run. ELF SHA-256:
`ee92fbcd624580531300839c31578476497d280704b9049ec4bb6b65e07a20a2`.
Both board builds pass;105 HIL and47 audio host tests pass. Firmware exports
the new open/closed clip counters, and the host integration test verifies
classification against actual muted/unmuted PCM and the settling exclusion.

Boot codec expectations pass and calibration confirms30dB/125%. At device
30.425680s, raw serial line1924's `DISPLAY_R4_GDMA_READY` JSON is cut inside
the fifth pair and followed by an `esp-peer: PHASE open` log. The strict monitor
reports an unterminated JSON string and aborts. Failure is observed at host
monotonic3484554.39557108s. This establishes corrupted console telemetry,
not a measured LCD DMA fault; the underlying write/interleaving cause is not
proven. No monitor exemption or synthetic repair was made.

The sole PERF sample at device30.682566s reports total/open/closed clips0,
lost/concealed/underflows/write-errors0; these are pre-prompt observations,
not qualifying reply measurements. PSRAM heap14548628, internal minimum87312,
largest51200 and failed allocations0 are retained. No scored reply completed,
so there is no late-reply loss interval for A to diagnose from this attempt.
`run1/result.json` preserves the failure even though the outer monitor raises
before appending that turn to aggregate `runs`; `summary-audit.json` records
this explicitly with the raw offending line. VOICE+GRAPHICS is not qualified.
The next display action is to make readiness telemetry complete and serialized;
strict parsing remains required. A further campaign was not run.


### Serialized readiness telemetry

The new display image routes `DISPLAY_R4_GDMA_READY` and asynchronous
`DISPLAY_WORK_RESULT` through the existing application buffered console writer.
That writer holds `flockfile(stdout)`, the lock also used by IDF's normal log
formatter, across the whole record and retries partial USB writes in bounded
2048-byte chunks. Readiness runs on app-session; store completion releases the
display guard before output. Their fixed768/256-byte buffers preserve existing
JSON schemas. Command-response JSON already uses this writer; `DISPLAY_WIRE`
uses the bounded deferred logger. No new queue or logging exception is added.

The strict cold-start harness now requires a complete readiness record with
all five register pairs; a missing or truncated record cannot silently pass.
A host regression reproduces the fifth-pair splice followed by an informational
peer log across every receive-byte boundary. It must fail, while the same two
complete serialized records pass. Additional checks reject missing pair fields
and incomplete five-pair records. The raw-error rule remains unchanged.


## VOICE+GRAPHICS qualified — serialized-image strict campaign

Qualification caveats from Fable B6 apply to this label:

- Headroom was2.0–2.5dB above the3dB floor and depends on TTS content. The
  loudest earlier bench reply would leave about3.67dB at125% gain, only0.67dB
  over the floor. This qualification covers the scripted phrase, volume0 and
  this room; it does not bound louder phrases or other voices. Volume control
  currently permits attenuation only.
- Serialized store-result output can block the display-store task for up to1s
  per USB chunk if the host stops draining. It does not run on audio/transport
  callbacks. Work-result write failure is logged but has no dedicated counter.
- Cumulative playout_dropped was2 before the first scored snapshot, during
  greeting/connect, and stayed unchanged; those drops are outside scored deltas.
- Scope is three turns in one session over about4minutes in a quiet room
  (approximately-58dBFS floor): no soak, network impairment or reconnect test.
  The late-silence investigation handed to A (`d8ff86c`) is outside this evidence.

`B/voice-cat-B6-serialized-strict-1` passes all three original turns and every
aggregate strict gate on new image `dfe4787` atop `b2f5ab3`. Exactly one campaign
was run, with no retakes and unchanged thresholds. ELF SHA-256:
`b6704fe2960c719fd61ef4d3d539f1d144a2c9661e6790fa8cc6b0de907c1403`.
Both firmware builds and107 HIL tests pass, including the splice regression.
The old200% ELF remains qualified only for display/protocol/memory/transport;
this new125% image supplies the requested VOICE+GRAPHICS evidence.

The complete five-pair readiness record arrived at device31.016792s, including
LCD TX priority1 on pair1. Strict raw/display monitoring reports zero errors.
Full first-TTS-to-gate-reopen FPS lower bounds are28.984877 /29.075811 /28.888248
over11.695754 /11.624783 /11.942573s. Three real-bot shows were admitted and
rendered mid-reply in one warm peer session. Final wire received/accepted/ack_sent
are each3; all rejection, acknowledgment failure/staleness and log-drop
counters0, maximum admission wait11us.

At30dB analog/125% digital gain and volume0, independently reconstructed
pre-mute headroom is5.009037 /5.478885 /5.401624dB, exceeding the3dB floor on
every turn. Gate-open and gate-closed clip deltas are each0, consistent with
the aggregate clip counter. Jabra reply RMS is-23.398360 /-23.083653 /-23.308328
dBFS, with room rise38.11 /38.30 /37.66dB. Every reply and whole-turn transport
gate passes. Across all13 raw PERF samples, lost/concealed, drift drops/inserts, write
errors, underflows and all three clip counters stay0. Cumulative
playout_dropped is already2 before scoring and remains2 in every snapshot;
all other transport counters stay0. All scored deltas are0. No late loss was
observed in this campaign; this is not a zero-drop whole-session claim.

Uplink passes:26/5420 eligible gaps exceed60ms (0.479705%, limit1%), none
exceed150ms, maximum101ms. Internal minimum87816 and largest47104 exceed
81920/32768; failed internal and total allocations0. App-control stack minimum
8980 exceeds2048; PSRAM heap14548516 exceeds14000000, with16MiB chip,
32-byte cache line and10240-byte DMA half. Explicit quiet1 and no mid-turn
full PERF acquisition are retained; SCTP chunk1100 is unchanged.

The original `result.json` and independently recomputed `summary-audit.json`
retain the gates, PCM headroom reconstruction, complete readiness record and
raw counter maxima. Prior failed evidence remains intact. This qualifies the
requested three-turn campaign, not a soak or proof that all historical
late-silence failures are eliminated. Bench and owned services were released
to A for the `d8ff86c` traced late-silence diagnostic; no further B hardware
run was made. Flood robustness remains proposal-only pending review.
The next diagnostic is `hil/echoear_late_trace.py`, one original live reply
with cat at the same maximum volume and current microphone profile. It uses
arrival-trace1, the existing host event-loop probe, RTP send/receive files and
timestamped console commands. Trace exports occur before the prompt and after
the25-second acoustic recording; no new bulk exports run during the reply or
its late-silence tail. Existing boundary PERF calls remain timestamped so any
coincidence with receive delay is visible. The8192-record ring must retain
both endpoints with no overwrite delta, or diagnosis is marked incomplete.
The report separates reply and late-silence counters and matches concealed
RTP timestamps across host send, socket read, authentication, authoritative
ingress and render completion. PA transitions are retained. It cannot identify
an RF versus driver/lwIP cause from socket timestamps alone.

Run only after B releases the bench, with an already built current merged
image and a valid display budget, using `hil/bench.py .../python
hil/echoear_late_trace.py --out <new-evidence-directory> --budget
<arena-budget.json> --flash`. No sudo, packet capture or network change is
needed. The runner always marks qualification false and preserves the
original turn verdict; an acquisition exit0 only means usable trace coverage.

`A/late-trace-1` ran once during B's explicitly released bench window on
d8ff86c/masterb2f5ab3, ELF
`e334972caa04e8003cb846f29e277e1fb6d1199aa84b79d252019f0f07802b23`.
The turn passes and trace coverage is complete, with zero overwritten
records through post-export verification. Late silence spans device
62.987488–71.668227 seconds (8.680739 s):435 decoded render decisions,
zero lost/concealed/drift/underflow/write-error deltas. Socket receive maximum
35.39 ms, authoritative ingress35.366 ms, render31.269 ms; host downlink
maximum22 ms. PA remains enabled32.417–92.334 seconds. This did not reproduce
the earlier late loss and does not establish its cause or resolution.

The earlier reply bracket also has zero damage. Its single62.057 ms socket
receive gap has consecutive sequence numbers and no PLC; a1.502 ms compact
frame-clock request falls inside the gap, starting17.23 ms after it begins.
That timing alone does not establish causation. The enabled20 ms host
event-loop probe records no lag over20 ms. Host/device anchor spread0.720 ms.
Prompt calibration-39.116 dBFS, exact live transcript, zero microphone clips,
5.688 dB headroom, Jabra-23.007 dBFS, full-reply display28.773 FPS, strict
display monitor clean and one peer. Independent uplink gate passes2/777
intervals >60 ms (0.257%), none >150 ms, eligible maximum84 ms.

Bench/services released immediately after this one acquisition; no retake or
soak. B's in-progress split clip telemetry and logging correction are not in
this image. This diagnostic is not a full voice-plus-graphics qualification;
the previous failed evidence remains unchanged.

### Phrase-independent digital microphone protection

EchoEar now applies a smooth limiter after the50 Hz high-pass and before
digital gain. Its output-domain knee is8192 PCM units and its asymptotic
ceiling16384 (at least6.02 dB digital headroom for every supported gain).
Below the knee it is exactly unity. Above it, with pre-gain knee `k` and
width `w`, magnitude becomes `k + w * excess / (w + excess)`. This avoids
depending on PA/gate timing or a particular phrase. Native ADC saturation
still increments the same fatal open/closed clip counters; a limiter cannot
repair ADC overload. Limiter activity is reported separately, never called
clipping-free raw input merely because the output is bounded. Legacy board
processing is unchanged.

`MIC_PROTECTION` identifies schema1/knee8192/ceiling16384 and reports limited
samples and ADC saturation. The harness requires the known readback and
reconstructs both protected and unprotected peaks from retained native PCM.
The>=3 dB gate now applies to the **unprotected** reconstructed peak (A6
decision below); both zero-clip gates remain.50 audio host tests include
all1600 supported gain percentages, large full-scale filter transitions,
monotonicity, sign symmetry, knee continuity and exact low-level passthrough.

`A/echo-stress-1` measures two new Thalia TTS phrases through the production
transport/playback path with cat, volume0 and30 dB/125% microphone gain.
Source durations19.080/22.520 s exceed the previous roughly12-second reply;
source boosts3.867/3.662 dB stay below PCM saturation (peak32700). Jabra
active RMS is-22.676/-21.735 dBFS, with peaks15419/18167. These are loudness
measurements, not SPL or speech-THD estimates. Output volume/DAC/normalizer
settings remain unchanged.

Native mic peaks14747/16485 produce unprotected post-gain peaks18026.697/
20440.023 (5.190/4.099 dB headroom). Protection gives peaks12661.251/
13100.791, **8.259/7.963 dB headroom**, with5575/11719 limited samples.
Both original points pass: zero ADC saturation, open and closed clip deltas0,
all13 transport deltas0, and strict display monitor clean. No retake or soak.
The room measures-60.69 dBFS. Native prompt calibration remains-39.115 dBFS
against-39; its protected/unprotected peaks both2368.949 and limited samples0,
so the calibrated speech path is unchanged sample for sample. This stress
measurement supplements the B6 phrase-specific qualification; fresh acoustic
five-run campaigns on the protected image are still separate work.
## Bounded data-channel admission (flood implementation)

The UDP peer receive pass stops between native reads at 4 ms, four datagrams,
or 4096 received bytes (one native datagram can cross the byte/time boundary).
Both select and recvfrom enforce the boundary, including native nonblocking
drains without another select. The owner blocks for at least one scheduler tick after every pass; elapsed CPU
work no longer masquerades as a socket wait. An opaque DTLS/SCTP call cannot be
preempted by this policy. Native application callbacks admit at most four
messages / 4096 bytes per pass, with a 128 KiB/s byte token bucket (4 KiB burst).
A separate 4 KiB/s / 1 KiB burst reserve admits compact <=512-byte messages
when the main byte bucket is depleted, without parsing a claimed message type;
all traffic still shares the per-pass cap. This protects occasional show/ack
traffic from 1 KiB floods while bounding arbitrary small-message floods too.
Admission occurs before any copy or parse, with a 2048-byte wire
limit. Excess is counted, including queue-full drops; overload does not parse
messages or synthesize a terminal session failure on the owner.

The PSRAM wire queues retain fixed capacities. The receive worker classifies
at most two messages / 4096 bytes per iteration and dispatches at most one DOM.
A nonallocating structure scan bounds input to 64 structural tokens before the
JSON lexer; JSON validation retains its depth limit. SDK admission waits until
its callback loop, including the executing callback's capture, is empty. This
bounds the parser, RTVI message copy, callback capture and application dump by
one wire-limited message cohort instead of an unbounded callback backlog.
Before classification and DOM construction, admission reserves a conservative
`2048 + 8*wire_bytes + 192*structural_tokens` live-allocation allowance above
81920 free internal bytes, and `2*wire_bytes + 1024` above the 32768 largest-block
floor. This is an admission envelope, not a reservation against unrelated
concurrent audio/native allocations; HIL must still enforce actual heap minima
and zero failed allocations. Oversized/dense messages are counted rejections.
The existing display payload limit remains 1023 bytes inside that envelope.

The app queue reserves eight of 32 slots for lifecycle work and counts ordinary
server/metrics drops (`app_data_drops`) without converting them to a session
failure. Transport `data_admission` plus `drop_reasons.admission_budget`,
`admission_shape`, `admission_heap`, `invalid_data`, and `message_queue` expose
admission losses; `sdk_backpressure` counts waiting iterations, not messages.
Drop summaries are rate limited to one per second.

Display wall time inside an API no longer proves a lost DMA. The independent
2-second watchdog requires a color transfer that actually started and still
lacks its completion. A completed ISR followed by a descheduled owner, or an
owner delayed before queueing, does not set terminal-dead. Missing completion
still fails closed; driver timeouts retain their safety stop. Mutex delay and
flash-gate contention are counted separately. PERF `terminal_reason` is 0
(none), 1 (watchdog missing completion), 2 (notification timeout while color
pending), 3 (native driver timeout), or 4 (other driver/DMA failure).

Qualification protocol: separate short holds for 0/25/50/100/saturating 1024-byte
RTVI messages, 30 seconds per rate plus drain, cat running throughout, same
1100-byte SCTP chunks. Required: continuously advancing display, zero DMA/raw
errors/watchdogs, internal boot minimum >=81920, session-window minimum >=98304,
largest >=32768, zero failed
allocations, connected through drain, and counted saturation losses. Actual
accepted/dropped rates, frame cadence and session-window minima are reported;
this display flood sweep is not another acoustic voice qualification.

## Data-channel flood sweep — five short holds passed

Evidence: `/home/khkramer/src/evidence/echoear/B/flood-read-bound-{0,25,50,100,saturating}`
and independent `B/flood-rate-sweep-audit.json`. All five fresh boots use firmware
implementation `6e3d27f`, ELF SHA-256
`1e52325572176179fbb0bbed3cb3a79e6109c5fe2e4c04b045d451e3edecd77d`.
Each has a 30-second sending window plus drain, cat30, offline-test RTVI,
1024-byte `bot-llm-text` messages, 1100-byte SCTP chunks and an 8192-byte host
flight limit. Audio is off: this qualifies the display/data-channel sweep,
not a new acoustic VOICE+GRAPHICS image or a 300-second saturation soak.

| Offered messages/s | Flood messages sent | Counted budget drops | Observed cat FPS | Session internal minimum (B) |
| --- | ---: | ---: | ---: | ---: |
| 0 | 0 | 0 | 29.061 | 104563 |
| 24.999 | 750 | 7 | 28.856 | 104267 |
| 50 | 1500 | 12 | 28.610 | 104475 |
| 100 | 3000 | 32 | 28.102 | 102423 |
| Saturating: 226.9 | 6807 | 3291 | 16.443 | 101995 |

Every received message reconciles as dispatched or counted admission loss:
`1=1+0`, `751=744+7`, `1501=1489+12`, `3001=2969+32`, and
`6808=3517+3291`. Each received total includes one additional real-bot `show`.
Every show was acknowledged and proven applied/rendered by revision and exact
layer identity/position during the flood. There was one peer per run, all
server queues drained to zero, and app data-queue drops were zero. Heap,
shape, parse, invalid-frame and message-queue admission losses were zero.
**The low rates are not lossless:** the per-pass/burst admission policy also
sheds the small counts shown above. SDK backpressure keeps payloads bounded;
it does not make dropped messages reliably delivered.

Fable B7 traced all 7 drops at 25/s and all 12 at 50/s to the sender's startup
catch-up bursts, with no further losses during the remaining 25 seconds. The
sender can release 8 KiB while catching up; that exceeds the 4 KiB bucket burst.
This evidence does not indicate a steady-rate limit at 25 or 50 messages/s.
The earlier B6 live bot produced at most 13 RTVI messages/s, a largest message
of 629 bytes, and a worst 20 ms burst of 7 messages (about 1 KiB).

Admission has **no control-message priority**. The compact reserve is based on
size, shares the per-pass count/byte bounds, and can itself be exhausted.
`bot-ready`, `signalling/peerLeft` and `disconnect-bot` can therefore be shed
under flood. Losing `bot-ready` leaves the session Connected until the 90-second
app timeout. A lost `bot-stopped-speaking` can retain the speaking flag for up
to 2 seconds; the gate also follows actual playback with a 300 ms tail. Reserved
app lifecycle slots cannot recover control messages lost at native admission.

The transport wire limit is **2048 UTF-8 bytes for the complete received RTVI
message**, including its envelope. Larger messages are dropped before parsing,
counted as `drop_reasons.invalid_data`, and receive no application acknowledgment.
The device reports the limit as `data_admission.wire_limit`. Long bot output or
function-call payloads must be kept within it or split at a protocol boundary
that supports splitting. DisplayClient separately enforces the advertised 1023
byte compact display data-object limit; this is not a 2048-byte display payload.

Firmware follow-up deferred until the final 14284c8 qualification runs finish:
bound repeated engine `lock()` misses. The current loop counts scheduler delays
but can wait indefinitely on a real mutex deadlock; HIL frame-progress checks
catch that freeze, while production has no terminal lock-wait bound yet. The
follow-up must preserve delayed-owner versus absent-DMA safety and receive its
own short hardware check. Bot timeout retries and these docs do not change the
frozen 14284c8 ELF.

All five runs have zero DMA errors, raw monitor errors, watchdogs and failed
internal/total allocations. Every sampled LCD TX priority is 1. Worst global
internal minimum is **83888 B** (1968 B above the 81920 gate); session-window
minimum is **101995 B** (3691 B above 98304); smallest observed largest block
is **45056 B**, above 32768. App-control stack free is at least9044 B; PSRAM
heap census is14548228 B with32-byte cache lines and10240-byte aligned halves.

FPS uses device frame clocks spanning sending plus the final drain. Every
interval advances. The worst observed short bracket is24.236 FPS at100/s,
but only **2.143 FPS at saturation**, whose full bracket averages16.443 FPS.
This is graceful degradation with a live display, not a claim of sustained
24/28 FPS at saturation. Retain this limitation and the narrow global-heap
margin when choosing production load or planning a longer soak.

Two additional isolated diagnostic boots on the same ELF verify the deadline
policy: `B/flood-deadline-lost` suppresses completion and stays dead/failed
with terminal reason2 and no subsequent frame progress; `B/flood-deadline-gate`
holds the gate for3 seconds, counts one timeout/scheduler delay, then recovers
and renders45 frames without terminal-dead. Both have zero DMA/raw errors.
These intentionally injected diagnostic health failures are recorded separately
from the normal strict sweep; no monitor rule was exempted.

Earlier trials remain intact. `flood-final-25` stopped because the harness's
one-second acknowledgment polling let cat updates evict the32-entry result
history before its query. Its accepted acknowledgment alone was not a render
qualification. Prompt polling fixed that harness defect; the final five arms
also use the later firmware with recvfrom-boundary enforcement. The earlier
`flood-0-1` and `flood-final-0` baselines are not substituted into this sweep.

Validation:92 transport/admission host checks (including the independent
allocation census),96 SDK,42 app and49 tests per display geometry;32 HIL unit
checks and15 bot checks. EchoEar and legacy Waveshare firmware both build;
only EchoEar hardware was accessed. Every bench hold ended before edits or
builds, and the bench is released after the sweep.

## Expressive cat character mode — CHARACTER READY

`disp character on|off` is separate from the unchanged `disp bench cat 30`
throughput benchmark. Idle uses a static smile, open/half/closed/half/open
blinks over180 ms at deterministic pseudorandom3–6 s intervals, and rest/lift/drop
whisker twitches held300–500 ms at2–5 s intervals. Open-mic energy above the
adaptive noise estimate biases the next whisker interval toward2–3 s. Speaking
suppresses blinks and whisker motion. Smoothed, hysteretic mouth selection uses
closed/small/medium/round/wide sprites, with at least100 ms between changes and
no catch-up burst after a delayed display task. Silence returns to listening
within about200 ms; this is energy-reactive animation, not phoneme alignment.

The device-local bridge publishes only fixed32-bit metadata. `Speaker::write`
measures accepted post-volume samples and timestamps their midpoint using
submitted-minus-DMA-completed samples, including the hardware cushion. Decode,
jitter and DSP lookahead are upstream of that point. A32-entry fixed atomic
ring lets the display choose energy due at playout rather than at arrival.
Capture reuses the already-computed pre-mute mean-square energy and the actual
mic gate decision. No PCM copies, allocation, logging, or new audio locks are
introduced. Xtensa assembly inspection confirms load/store instructions and
memory barriers with no helper calls for the bridge.

The display task polls the state machine every20 ms and posts only changed
layers. Existing dirty-rectangle composition redraws the faceplate underneath
each transparent feature, including overlapping features. The first frame
installs the base and hides unused layers. Asset pins and all GDMA/hash/flash/
console guards remain in force. `disp character-status` is a compact serialized
JSON record: `frames` and cumulative `changes` are ordered mouth, eyes, whiskers;
`blinks`, `twitches`, `posts`, `errors`, `rendered` and the latest audio powers
support per-second diagnostics without full PERF dumps. The existing PERF
`animation` flag continues to identify the continuous benchmark; sparse idle
character frames are checked over the ten-second window, not expected on every
PERF query. Raw errors and hardware-health stop rules are unchanged.

The demo launcher retains Claude's duplicate-init and open-MIC_GATE fixes and
uses character mode. `hil/step9a_echoear_character.py` prepares a ten-second
offline speech fixture and checks ten seconds idle plus ten seconds playback,
logging compact per-second changes under the unchanged strict monitor. It must
only run after the live demo releases the bench. Host timing/audio/launcher
checks and both target builds pass. The short board check passed on firmware
8fcf1d9 (merged master ff8f054, including Kelsey demo settings), ELF SHA-256
`74eae8f2c06e4ea90c67913fbbbc55d62d48f9b16304d9b4bd203429c5590ca9`.
Evidence: `/home/khkramer/src/evidence/echoear/B/character-smoke-2/`.
Ten seconds idle produced mouth/eyes/whisker changes0/8/5; playback produced
43/0/0 changes. Mouth rate, speaking features, rendering, DMA and strict raw
monitor checks passed. Idle return was observed45.047 ms after the first
silent playout-energy sample (roughly23 ms sampling interval). Internal heap
minimum87984 bytes, largest47104, failed allocations0; app-control minimum
free stack8988 bytes. Cleanup succeeded and the bench was released.

The first check remains recorded as failed: its fixed300 ms wait after the
command-relative ten-second window sampled the buffered audio tail before the
next mouth update. The harness now observes actual playout silence and requires
idle within200 ms, with a bounded two-second tail observation. The repeated
short check used the identical ELF; this is a character smoke check, not a
new voice qualification. Earlier voice/flood qualifications cover earlier ELFs.


## Standalone touch and connection screens — host/build milestone, HIL pending

B provides `dg_board/touch.h` and the nonblocking
`dg_display::demo_screen(DemoScreen)` hook. A owns app lifecycle, USB-independent
startup, reconnect/backoff and provisioning/launcher integration. These B APIs
alone do not start a session or enable touch polling. The user's running demo
has not been modified or interrupted for this milestone.

Call `dg_board::touch::init()` once after the existing board I2C bus is ready.
It adds CST816 address0x15 at400 kHz on that bus and owns only GPIO10 input/IRQ.
It does not create another bus, toggle LCD/rail/reset pins, probe a sleeping
controller, or allocate a touch task. A single non-audio worker calls
`dg_board::touch::poll(monotonic_ms, accept_reconnect)` every10–20 ms. The IRQ only latches
a counter; polling performs at most one five-byte read with a10 ms timeout.
Coalesced IRQ edges or read failures invalidate a partial pair. Set acceptance
false throughout teardown/connecting/retry. `touch_status()` provides atomic
individual counters for interrupts, reads/errors, coalescing, taps, double taps,
ignored taps and rejected contacts; fields can span adjacent poll iterations.
No logging or work occurs on the audio path.

Software recognition requires two distinct contacts lasting20–250 ms, release
spacing80–399 ms, movement at most30 px per contact and separation at most60 px.
A held finger, bounce, drag, stale pair or reconnect-time taps cannot request a
new session. Initialization success means the bus device/IRQ were installed;
actual touch communication still needs hardware verification. No hardware
double-click configuration is assumed.

Register layout and sleeping-device behavior follow Espressif's
[CST816S driver](https://github.com/espressif/esp-bsp/blob/901d9481034e2095a2faf8a72f4ecf05680ddd71/components/lcd_touch/esp_lcd_touch_cst816s/esp_lcd_touch_cst816s.c)
and its [README](https://github.com/espressif/esp-bsp/blob/901d9481034e2095a2faf8a72f4ecf05680ddd71/components/lcd_touch/esp_lcd_touch_cst816s/README.md).
Reference C SHA-256:068ab69e77a9e361e016ecfa361f1a8f6e2f0d2d00c15dea96cac07743a0c94f.
The direct reader uses0x02 count,0x03/04 X,0x05/06 Y; no vendor source is copied.

`tools/echoear_status.py` derives five DGI fixtures from the existing cat PNGs
without writing to `assets/`. It uses the canonical `tools/dgi.py` encoder,
Pillow LANCZOS scaling and a fixed bitmap label font. The manifest is
`hil/fixtures/echoear360/status.json`; matching compiled hashes/geometry are in
`dg_display/demo_assets.h`. Two360px bases show a dim closed-eye cat labelled
DISCONNECTED and a half-eye cat labelled CONNECTING. Three72x18 dots patches
at(144,285) advance every350 ms. Full bases change only on state transitions or
scene reset; each dot update recomposes only its dirty patch through the normal
compositor and DMA guards. Live restores the existing audio-reactive character.
The additional encoded assets total135855 bytes, fitting the491520-byte arena
with the existing85248-byte cat allocation.

Integration order for A:

1. Provision cat assets as before, then call
   `hil.echoear_status_assets.provision_status(board, origin)` while idle and
   serving the360px fixture directory. Every status asset requires a successful
   persist acknowledgement. `preload_status(board)` uses flash only; native boot
   can iterate `dg_display::demo_assets` with existing preload requests.
2. With cat slots configured and all status assets persisted/preloaded, issue
   `disp demo on`. This pins both sets. Failure to load any status asset rejects
   enablement. `disp demo off` stops/unpins both. Existing `disp character` and
   `disp bench` commands explicitly leave demo-screen mode.
3. On a double-tap request, A disables audio and closes the current peer; publish
   Disconnected and retain it visibly (suggest300 ms), then perform a fresh
   `/start`/connect with Connecting. Suppress taps until the new session is live.
4. Publish Live only on the accepted current-generation RTVI bot-ready event,
   alongside display-ready. Natural loss publishes Disconnected; each backoff
   retry publishes Connecting. Keep this policy in A's single app owner.

`disp demo-screen disconnected|connecting|live` is a diagnostic setter;
`disp demo-status` reports enabled/requested/presented screen (0/1/2), dot phase,
posts/errors and rendered count. A's lifecycle hook stores only a mailbox value,
so it never waits on the compositor or runs I2C. Rapid transitions coalesce;
the app's visible disconnected interval is therefore necessary.

Host coverage includes debounce/hold/drag/late-pair/wrap/reconnect suppression,
dot cadence and explicit Live selection, manifest hashes/geometry/arena bound,
and fail-closed persist/preload acknowledgements. Hardware validation must wait
for Claude's bench release and A's lifecycle integration. It must verify a fresh
`/start`, pc_id and bot pipeline after one physical double tap, ignored taps
during reconnect, proper loss/retry screens, and a clean strict display monitor.


## B7(c): finite engine-mutex acquisition

Engine mutex acquisition now permits at most three2000 ms semaphore waits
(6000 ms total wait budget; scheduler descheduling can add wall time). Unrelated
render progress never resets the attempt limit. Each semaphore timeout increments
`lock_timeouts` and the existing `scheduler_delays`. Exhaustion latches
`engine_lock_failed`, increments `lock_failures` once, and sets display dead/failed
with terminal reason5 unless an earlier terminal reason already exists. This
labels engine-mutex unavailability separately from absent-DMA reasons1–4.
The DMA completion checks and flash/crypto gates retain their existing limits.

Acquisition failure never permits protected state access or release of another
owner's mutex. Session hooks, wire-ready and DMA-error/health APIs return safely;
console operations report an exception with a static message; display/store task
entries catch that exception and quiesce their own worker. A fetch connection is
closed during unwind without using the failed mutex. Arena allocation occurs
only after acquisition. Existing DMA/cache staging storage is retained until
reboot, avoiding reuse while a transfer may still be pending.

`disp lock-status` uses atomic loads without acquiring the engine mutex, so its
wait budget, timeout/failure counts and terminal status remain readable after
failure. PERF includes `display.lock_timeouts`, `lock_failures` and
`engine_snapshot`. A false engine_snapshot means the protected engine fields
could not be read; zero defaults are not a fresh frame/recovery measurement.
Subsequent acquisitions after the latched mutex failure return immediately.
Normal admission paths retain their existing zero/5ms waits and reject new
requests after terminal engine failure.

Host regressions exercise permanent hold, transient recovery, token ownership,
terminal races, progress-independent bounds, exception cleanup, static exception
construction and HTTP close-on-unwind. Host policy timing uses a fake backend;
no wall-clock hardware-timeout qualification is claimed by those tests.
### Fresh quiet-room acoustic campaign preparation

Firmware limiter telemetry independently matches the retained stress PCM:
cumulative counts0/5575/17294 give increments5575/11719, with ADC saturation0.

`hil/echoear_acoustic_qualification.py` freezes the protected125% EchoEar ELF,
arena budget and reference WAV hashes before acquisition. Its `--prepare`
mode needs no bench. A changed image/reference/budget requires a new plan;
existing plans and acquisition directories cannot be overwritten.

Run `--mode offline` and `--mode live` in separate coordinated `hil/bench.py`
holds, each with five original warm-session turns, cat graphics, Jabra and
volume max0. The coordinator installs the shared final image through the
MAC-guarded flash path before these runs. Schema2 plan
`A/final-14284c8/plan.json` pins ELF
`94a5a13ccd5555a8fd39710a140cf514c012d7d7e248554d632915b61ec7d444`,
byte-identical to B's final build, plus B's budget and both A fixture banks.
The earlier `A/quiet-qualification-1/plan.json` is superseded. Use fresh
`offline` and `live` directories beside the new plan; the report records the
absolute commands. Do not rebuild the shared image for host-harness edits.

Each turn requires a quiet baseline,30 dB/125% readback with the known
protection profile, calibrated speech,>=3 dB unprotected reconstructed headroom, zero
open/closed clips, the unchanged Jabra-22±3 dBFS reply gate and zero whole-turn
transport deltas. Full PERF queries stay outside prompt/reply acquisition.
The campaign also requires one bot peer, the existing uplink-tail gate and
strict display monitoring. Missing data, credentials, a noisy turn or a failed
original cannot qualify; there are no retakes or diagnostic noise bypasses.
SPL remains unmeasured. No earlier acoustic result counts toward either5/5.
These campaigns are prepared, **not yet executed**; B's final three-turn
qualification takes the first bench hold. They do not include the canceled soak.

The read-only late-loss comparison (`A/late-loss-readonly-update.json`) retains
the old untraced2/2 lost/concealed increment at62.066615–70.790021 seconds.
The complete later arrival trace and B6 serial evidence have no such increment.
These clean observations do not establish a cause or a fix for the intermittent
failure; no additional bench run was used for this comparison.

### A6: final-image requalification and limiter distortion

B6 qualified ELF `b6704fe2…`, before the limiter. That verdict does not
transfer to the changed microphone path. The earlier stress acquisition is
supporting evidence on its recorded precommit image, not qualification of the
final merged firmware. B's strict three-turn voice+cat campaign, A's stress
rerun, hot STT comparison, analog probe and fresh quiet offline5/live5 must all
identify **one coordinator-frozen ELF**. These A6 acquisitions remain pending
at harness preparation. No firmware change is needed for these harnesses.

The limiter is continuous with a continuous slope at its knee, but is nonlinear
above it. Host characterization is reproducible with
`hil/mic_protection_characterization.py`; retained numbers are in
`A/limiter-characterization-A6.json`. Two-second coherent16 kHz float32 tones
at125% gain, overdrive relative to the knee, give:

| Overdrive | Output peak | THD,300 Hz | THD,2.5 kHz | Nonharmonic energy,300 Hz /2.5 kHz |
| --- | ---: | ---: | ---: | ---: |
| 0 dB | 8192 | <0.001% | <0.001% | roundoff floor, about-151 dBc |
| +3 dB | 10585 | 3.42% | 3.18% | -78.9 /-38.0 dBc |
| +6 dB | 12278 | 9.71% | 9.67% | -71.7 /-38.5 dBc |
| +9 dB | 13477 | 16.05% | 15.80% | -64.1 /-30.0 dBc |
| +12 dB | 14326 | 21.74% | 20.52% | -58.6 /-23.3 dBc |

These are limiter-only software measurements before integer truncation,
not speaker or ADC THD. THD includes baseband harmonic bins. Nonharmonic
energy includes folded aliases and roundoff; aliases coinciding with harmonic
bins cannot be separated by this measurement. There is no oversampling.
Below-knee speech is unchanged; above-knee speech is not lossless. The hot STT
test is required before making an intelligibility claim there.

**Gate decision:** retain the3 dB minimum on the counterfactual unprotected
high-pass-plus-digital-gain peak. The protected peak cannot establish that
margin because its bound already guarantees>=6.02 dB. Reports now give both
headrooms, native ADC-word headroom and limited-sample counts. This tightens
the post-limiter harness interpretation without raising the allowed clipping
or transport thresholds. Unprotected headroom is a conservative linear-path
range check, not a measurement of analog saturation. In normal protected
EchoEar operation, `clips` means native ADC-word saturation; the software
overflow guard remains as a fault check. The limiter does not hide native
full-scale endpoints, and below-full-scale analog nonlinearity requires the
separate probe.

`hil/echoear_acoustic_qualification.py --prepare --elf FINAL_ELF` now freezes
an external ELF, the budget and additional `--reference` files/directories.
Freeze both `A/echo-stress-bank-1` and `A/analog-probe-bank-A6`. All A runners
use the same `--plan`; boot hash and30 dB/125% protection readback must match.
Schema2 rejects the earlier `quiet-qualification-1` plan. Do not rebuild or
flash A's local image in place of the final shared ELF. The coordinator installs
the frozen binary once; these runners verify it and use separate short holds.

The prepared acquisitions are:

- `hil/echoear_echo_stress.py`: rerun both original immutable boosted phrases
  at max0; retain image identity, native/Jabra audio, limiter activity, both
  peaks, zero clips and all13 zero transport deltas.
- `hil/echoear_hot_stt.py`: calibrate the same France prompt to native active
  RMS-39 and-27 dBFS, each within1 dB; five bounded unscored calibration
  adjustments maximum and one original scored prompt per level. Keep gain
  fixed. Score **actual bot-received Opus-decoded uplink** with the same nova-3
  recognizer, exactly once, after bench release. In the step-10 campaign
  `echoear_item2_campaign.py` does this itself (`score_hot_once`); never run
  `--score` by hand on a launcher attempt (`score()` refuses a record that
  `execute()` finalised). Both need>=90% normalized
  speech accuracy, hot WER no worse than baseline, zero clips and transport
  damage. Require no baseline limiting and positive firmware/reconstructed
  limiting on the hot prompt with native peak>6554. An inactive limiter does
  not satisfy the experiment. Preserve true WER, including insertions above
  100%; this one short paired prompt does not establish broad STT accuracy.
- `hil/echoear_analog_probe.py`: one26.020-second compound source, three1 kHz
  levels(-12/-6/0 dB relative to source peak32700), then the entire loudest
 22.520-second fixture. Capture native/Jabra once at max0 through production
  playback DSP. Analyze central tone windows; no THD is assigned to speech.
  Compare board versus Jabra fundamental compression and harmonic growth
  relative to the low-level window, cancelling fixed linear response.
  Predeclared investigation flags: extra board compression>1 dB or extra
  H2/H3 growth>3 dB when the native harmonic exceeds-40 dBc. Require tone
  peaks to cover the speech peak, tone signal/noise>=30 dB, no native/Jabra
  word clipping and complete H2–H5 coverage; otherwise the probe is
  inconclusive/failed. These flags do not isolate ES7210 from speaker/Jabra
  nonlinearity or certify every frequency/level. No calibration sweep or
  automatic scored retake is hidden inside this one-capture probe.

Each result retains every original failure. A credential failure cannot pass.
The user-canceled soak remains excluded.

### Final-image observations and user-requested test stop

The user stopped testing to run the demo. A's offline campaign was SIGINTed
during turn5 recording and exited130. Turns1–4 completed their individual
checks; turn5 and the final campaign uplink/acoustic gates are incomplete.
The fresh live campaign never started. **This is not offline5/5, live5/5 or
a new VOICE+GRAPHICS qualification.** No bench, serial or7861/7874 service may
be acquired until Claude/user explicitly releases the demo hold. The later
demo image on master891dd33 does not inherit qualification from these captures.

All A acquisitions below verified the same B-built14284c8 ELF
`94a5a13ccd5555a8fd39710a140cf514c012d7d7e248554d632915b61ec7d444`.
A also built the exact clean14284c8 checkout successfully, producing local
ELF `ef32bb8db1e433996b71a35f4eb4b9768d2c0576629b543910ec18670e27ebca`;
that distinct build was recorded but never installed. Evidence lives under
`A/final-14284c8`; original results remain unchanged.

| Observation | Recorded result | Qualification limit |
| --- | --- | --- |
| Two boosted long TTS phrases | Unprotected headroom5.076/4.013 dB; protected8.226/7.941 dB; limiter counts5756/11794 match PCM; zero clips and13 transport deltas | Supporting measurements; host NIC accounting absent |
| Original STT pair | Native RMS-39.383/-27.489 dBFS; both exact seven-word transcripts, WER0%; zero clips/transport deltas | Hot limiter engaged only2 samples, so this does not establish accuracy under heavy limiting |
| One analog probe | Native tone THD10.296/6.568/5.856%; Jabra14.175/9.250/8.206%; differential compression+0.024/+0.042 dB | Inconclusive: tone peaks6757/5191/5039 did not cover speech peak15304; production normalization prevented an increasing physical-level sweep |
| Offline original turns1–4 | Jabra RMS-23.908/-23.915/-23.942/-23.933 dBFS; unprotected headroom7.028/6.831/6.877/6.832 dB | User interrupted turn5; final campaign gates absent |

The analog probe saw no additional native-path harmonic-growth flag at its
lower measured tone amplitudes. It cannot rule out analog saturation at the
larger speech peaks or isolate distortion to the ES7210, PA or speaker.
No extra capture was taken to replace this inconclusive result.

STT setup/analysis failures are retained separately. The first attempt timed
out before any scored prompt because a no-greeting session starts with an
already-open gate and emits no open transition. A harness-only fix observes
`MIC_GATE closed=0`, then enables quiet acquisition. Both scored levels use
the existing Jabra PCM100 loud-test setting: PCM75 would require host PCM
overflow to reach-27 dBFS; microphone gain and target tolerances did not change.
The baseline's dry-source alignment subsequently failed at0.527. Offline
alignment against the paired native acoustic prompt gave0.958 at the same0.6
threshold; the baseline was recovered from the identical retained recordings
and serial endpoints, not played again. Only the outstanding hot prompt was
then acquired. Both original received uplink recordings were transcribed once,
off-bench. Recovery provenance and raw hashes accompany `hot-stt-pair`.

Claude reported an unrelated sustained approximately6 MB/s host download on
`enp7s0`. A did not collect per-arm NIC counters, so network cleanliness for
these observations is **unknown**, not retrospectively clean. This prevents a
clean-host-network qualification claim. B's final three-turn campaign failure
(provider timeouts, uplink gate and inter-turn loss) remains; the download is
not a proven cause without synchronized accounting.

The initial policy required recording `/proc/net/dev` RX/TX deltas for
**both `enp7s0` and `wlp8s0`**, with monotonic start/end times and a30-second
quiet preflight. Retain justified board-flow byte accounting separately;
subtract only independently measured matching traffic. The historical gate
is20 MB non-board traffic per300 seconds in either direction. Without reliable
subtraction, total interface traffic is a conservative upper bound; it must
not be relabeled measured non-board traffic. Missing/reset counters cannot
establish cleanliness. Do not change host networking or interfere with another
project's transfer. No auto-resume: explicit release and confirmation that the
host network is quiet are required before further testing.

The current board-path NIC policy below supersedes that initial aggregate
gate; the historical failed/interrupted results keep their recorded verdicts.

`A/final-14284c8/interruption-and-network-hold.json` records the stop, exit code,
partial population, absent gates and original result/log hashes. The demo
remains undisturbed; no soak or replacement campaign has been started.

After the user unplugged the Waveshare and replugged the EchoEar, the EchoEar
enumerated as `/dev/ttyACM0`. TTY numbers are not board identities and must not
be rejected by name. Serial access resolves the pinned Espressif by-id path
and verifies USB serial/MAC `b4:3a:45:16:2d:94`; esptool verifies the chip MAC
before flashing. A requested port that differs from that pinned device or a
wrong MAC still fails. The demo hold remains in force; this identity correction
was checked with mocked filesystem/subprocess calls only.

### USB-started battery demo: output and launcher lifetime

The USB-started demo changes are implemented; hardware qualification is pending.
Autostart and battery-gauge work follow this test.
Runtime console output now uses a bounded 64 KiB PSRAM queue (maximum individual
record just under 32 KiB), with zero-wait admission even when USB SOF is absent.
A full queue drops a record;
logging never throws a session out of its control loop. `PERF.console_output`
records queued, SOF-false observations (`disconnected`, not a drop count),
full/oversize, and drain-failure counts. These are
best-effort logs, not a durable black-box recorder.

The audit covers `console_line`/`app_console_line` (session transitions and display
work results), all wrapped `printf`/`vprintf`/`puts`, ESP logging through its
installed vprintf sink, and the deferred audio/peer logger's final printf. IDF
log-v2 prefix/body/trailer serialization remains under the stdout lock; that lock
covers formatting and zero-wait queue admission, never USB drainage. The sole
`console-output` task writes USB with timeout zero. Only that low-priority task
retries driver backpressure, for at most one second per record. Expired records
are dropped; a newline follows partial delivery before output resumes. A shared
1536-byte PSRAM formatting buffer under stdout serialization avoids consuming
small system-task stacks.
Deferred audio/peer logging retains its existing bounded queue and drop counter.
Bootloader/ROM panic output is outside this runtime session path.

IDF 6.1's `usb_serial_jtag_is_connected()` observes USB SOF packets, not whether
pyserial has an open fd. The bounded queue is therefore needed even while that
API says connected: closing a host serial application must not block media.
SOF-false indications also occurred while the cable stayed attached, so runtime
output admission/delivery no longer uses that indication as a veto. Normal
connected operation still needs HIL validation for log completeness and
stack/heap margins after adding the 4 KiB PSRAM drain task. Host tests cover
absent USB, disconnect during a record, a stalled connected host, whole-record
admission, and queue-full drops without retries.

The demo launcher retains its owned bot on 7861 and asset server on 7874 after
serial loss. It waits for Ctrl-C and retries the pinned by-id/USB-MAC identity
every two seconds. Reopening sets DTR and RTS low **before** opening; it sends
only a parser-boundary newline and ping, never reset/connect/audio commands.
The initial launch still performs chip-MAC verification. Unreachable devices
receive no shutdown commands; loss during cleanup stops further commands.
`serial-lifecycle.jsonl` and separate reattach logs retain the transitions.
Other hardware harnesses retain their fail-on-reader-error behavior.

Prepared test: `hil/usb_absence.py --bench-released --flash --out
/home/khkramer/src/evidence/echoear/A/usb-absence-<unique-run>`, using the HIL
venv Python and the separate IDF esptool interpreter (`--esptool-python`, default
`~/.espressif/python_env/idf6.1_py3.12_env/bin/python`). Both the test and launcher
now check esptool imports before bench acquisition; the launcher accepts
`ESPTOOL_PYTHON` for that interpreter. **The first authorized attempt stopped
before device access because the server venv lacked esptool.** Its quiet-NIC
preflight passed; the bench was released to B. Attempt
`usb-absence-1c19014-2` flashed and reached Ready but lost connect ACK #61 before
closing serial. Cleanup succeeded; counters were SOF-false=1, queue-full=0,
drain-drops=3. A transient connection indication is the suspected cause, not a
proven cable loss. sys_evt minimum free stack was only 60 bytes with the old
stack-based formatter; the shared PSRAM buffer removes that added stack burden.
Both fixes require hardware retest. The 60-second arm is still unrun.

The test records a quiet
30-second NIC preflight, starts voice plus character mode, takes PERF, closes
serial for at least 60 seconds, and plays three microphone speech fixtures at
3/23/43 seconds. Offline bot logs must show a completed reply in each window and
at least three VAD turns on the original pc_id. Explicit
`OFFLINE_USB_ABSENCE_TURNS=1` selects a once-per-turn archived reply in offline
mode only; cached-TTS start/audio/stop frames produce the output transport's
actual bot-speech events. Other offline operation and the voice/demo policy keep
their existing behavior. Plain offline raw fixture playback does not emit those
speech events and has no automatic reply policy; the initial harness assumption
was corrected off-bench before the absence arm ever ran. It reopens without resetting,
checks media/worker/display progress, sys_evt >=1024 bytes free stack, unchanged
transport/clip gates, and strict
monitor results before/after the intentional serial blackout. It records RX/TX
on enp7s0 and wlp8s0 per arm and conservatively rejects total traffic above
20 MB per five minutes; no unsupported subtraction or sudo is used. Serial logs
cannot establish error-free output during the blackout. This is a host-reader
absence regression, **not** proof of cable-unplug or battery operation. A real
unplug test requires Claude to coordinate the user after this check passes.

### USB reader absence and Linux reattachment (2026-10-14)

The functional reader-absence harness can explicitly observe NIC counters
without enforcing the network-qualification threshold using
`--functional-nic-observe-only`. This exception is limited to this harness;
transport, clipping, same-session continuity and monitor gates remain strict.
The tested firmware remains b8986a8 (ELF SHA256
`bfe9bf3709f840d38aa8a4894b8585ba603cde23423eed3bbc4bae44d831721e`).

Attempt `A/usb-absence-b8986a8-4` completed all three offline fixture turns
while the reader was closed for 60.217 s, but FAILED continuity on reopening:
the boot log reports `USB_UART_CHIP_RESET`. No cable-unplug qualification is
claimed. The first PERF showed sys_evt stack free 1436 bytes (previously 60),
and no console full/transport drops before reader closure.

Linux CDC ACM asserts DTR and RTS together when opening the port. Configuring
pyserial with both False does not avoid its separate DTR-then-RTS writes:
clearing DTR first creates RTS=1,DTR=0. That is the reset state in the
[ESP32-S3 TRM, section 33.4.2, tables 33.4-3/4](https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf).
Reattachment now keeps both asserted, avoiding that intermediate reset state.
This host-only correction needs another same-session HIL check; firmware is
unchanged. The actual reset command remains available for initial setup.

The corrected host reattachment PASSED `A/usb-absence-b8986a8-5`: 60.2165 s,
three unique fixture turns with replies in all windows, same peer/generation,
3030 additional sent and received frames, zero deltas for every transport
counter and all clip classes, strict monitors clean, worker/display progress,
sys_evt minimum free stack 1436 bytes. Console transport drops increased by54
while no reader was present, as intended; media remained uninterrupted. NIC
traffic was recorded but not gated (functional-only authorization). This proves
reader absence and reattachment, not yet battery operation or physical unplug.

The launcher accepts `--firmware-ref b8986a8` for this explicitly authorized
image; default remains master. ELF, source and demo bot compatibility checks
still apply. `--unplug-check` saves baseline PERF and a new PERF plus strict
transport/clip/session counter checks after serial reattachment. Bot logs and
serial lifecycle timestamps establish what happened during physical absence;
these counter checks alone do not qualify the acoustic or network paths.

### Physical USB unplug: functional continuity confirmed (2026-10-14)

The user completed the battery unplug/replug procedure on b8986a8, ELF
`bfe9bf3709f840d38aa8a4894b8585ba603cde23423eed3bbc4bae44d831721e`,
using the patched launcher. Evidence is `B/demo-unplug-b8986a8-1`; read-only
analysis and input hashes are `A/demo-unplug-b8986a8-1-analysis.json`.
The demo remains running. This was an interactive functional check.

| Check | Evidence/result |
| --- | --- |
| USB absence | Host lifecycle detach to successful reattach: 76.1946 s (approximately 20:01:29.329–20:02:45.523 host local time); physical duration reported by user as about 60 s. |
| Bot process | PID 2878678 at startup, reattach, and analysis; process start time 17:35:27. |
| Session during unplug | `SmallWebRTCConnection#28-31b41b6969da46059b96b577c7091222`, generation 57, established before detachment and retained through reattachment; no connection lifecycle event during absence. |
| Conversation | Seven user transcripts and eight assistant completions during the observed absence (including the reply already underway); three further exchanges after reattach. The exact requested phrase “Are you still there?” is not in the saved transcript. |
| Reattachment/reset | First actual open attempt succeeded through pinned by-id/MAC resolution. No boot/reset marker in `serial-reattach-1.log`; device uptime advanced to 8,835,011,886 us and persistent audio/app/display task IDs remained unchanged. |
| Media/display progress | Recovered session sent/received 14775/14777; audio capture/playback and app/display task runtime increased. Render count advanced from 1 to 12987 over the full observation interval. |
| I2S | Recovered write errors, underflows, queue overflows and write timeouts all zero. |
| Strict loss/clip gate | **Not qualified.** The original `unplug-check.json` remains `passed=false`; see baseline limitation below. |

The launcher baseline was taken at 17:35:40, about 147.1 minutes before
reattachment. During the long wait, 28 five-minute idle-timeout reconnects
advanced generation 1 to 57. Thus the startup peer/generation differ from the
unplug peer, despite continuity across the actual unplug. Only two setup PERF
records exist before detachment; neither brackets the physical test. Recovered
session counters have lost/concealed/PLC/playout-dropped=8, and lifetime capture
clips=64 (12 gate-open, 52 gate-closed). `MIC_PROTECTION adc_clips=64` identifies
ADC saturation. Their timing cannot be assigned to the unplug interval, and
zero deltas cannot be claimed. No strict gate was relaxed or result overwritten.
A later idle-timeout reconnect at 20:09:30 occurred after this test window.

Recovered lifetime heap low-water marks were internal **87648 B**, DMA
**79860 B**, and PSRAM **10085720 B**. Current free internal/PSRAM were
104659/10144228 B; largest blocks 47104/9961472 B. Stack free low-water marks:
capture 13412 B, playback 3940 B, peer-loop 3912 B, peer-rx 1216 B,
app-control 9988 B, sys_evt 1436 B, console-output 3188 B, display 2136 B.
The smallest system-task value was ipc0 332 B (unchanged from startup).
These are recovered lifetime measurements, not fresh per-unplug minima.
Console full=0 and transport_drops=67 are consistent with bounded log dropping
while USB is absent; they are separate from media loss counters.

Result: **functional battery-session survival and automatic USB reattachment
confirmed; strict zero-loss/zero-clip physical-unplug qualification unproven**.
A future strict measurement needs a fresh PERF baseline in the active peer
immediately before the cable cue. No repeat run or demo restart was performed.

### Standalone lifecycle integration contract (overnight item 1)

`dg::App::Hooks::lifecycle(DemoPhase, uint32_t generation)` is an optional tail
member in `main/app.h`. The single app-control owner calls it after accepted
policy transitions: Connecting on Starting, Live on accepted current-generation
bot-ready, Disconnected when leaving an active/starting peer (including WiFi
loss, bot failure and explicit reconnect). Duplicate/stale Ready events emit
no Live notification. Callbacks must only admit bounded mailbox work; do no
I2C, display rendering, network IO, or waits in them. Existing audio-ready hook
now executes once at accepted bot-ready, before display-ready publication.

B should append the lifecycle callback to the `App::Hooks` initializer in
`run_console`, forwarding to its status-screen mailbox. A owns App policy.
`App::request_reconnect()` performs zero-wait event admission; the policy accepts
it only while Ready, retains desired-connect intent, stops audio/peer first,
shows Disconnected for at least 300 ms, then starts a fresh generation after
teardown. Requests during reconnect are ignored. `request_connect()` queues
normal connection intent without waiting for the network/bot; retry remains
1–30 seconds with reset after 60 stable seconds. Console `connect`/`disconnect`
retain their synchronous compatibility API.


## Integrated touch reconnect — functional bench proof pending

A's lifecycle2b34b73 is integrated. The optional hook now publishes B's display
mailbox, while a scoped `demo-touch` worker polls CST816 at10ms on a4096-byte
PSRAM stack. The app remains the sole session owner. A pending-request latch
blocks repeat delivery until a new accepted bot-ready. The normal
Starting→Connected transition retains CONNECTING; only current-generation Ready
selects the live cat. Startup/exception cleanup stops touch before destroying App
while all callback targets remain alive. The launcher remains A-owned.

`touch-inject` posts one bounded synthetic double-tap to the same driver-delivery
path as a recognized pair; `touch-status` reports real interrupt/read/gesture
counters separately from injected and injected-ignored counts, reconnect requests
and queue loss. `touch-enable 0|1` controls acceptance. This test hook establishes
delivery/reconnect behavior and does not prove physical controller recognition.

After a successful, current render lease finishes DMA, the display records an
ordered `DISPLAY_DEMO_RENDERED` transition and a16-entry `disp demo-proof` ring.
Proof validates exact base/feature identities and positions and hidden extra
layers, not merely the desired screen flag. Missing/reconfigured asset pins are
safe and cannot produce proof. Dot and feature patches retain normal composition
and DMA safety. Screen numbers are Disconnected0, Connecting1, Live2; app touch
phase numbers use DemoPhase (Connecting0, Live1, Disconnected2).

`hil/echoear_touch_proof.py` predeclares three injected reconnects plus one owned
bot kill/restart recovery. Each requires ordered completed screen proofs, a fresh
/start, peer and per-peer pipeline-start event, one ignored reconnect-time tap,
and a functional turn. Turns use a Jabra speech stimulus and VAD-triggered cached
PCM replies in the existing offline bot. Per-turn transport/clip checks, DMA,
strict monitor, memory floors and failed-allocation gates remain enforced.
NIC deltas are recorded per arm as observations: this functional protocol check
does not establish a quiet network/acoustic run. Strict voice+cat qualification
remains a separate campaign on the final merged image; no thresholds are changed.
### Standalone boot and provisioning implementation (hardware proof pending)

Normal images default `CONFIG_DG_DEMO_AUTOSTART=n`. Build the demo overlay with
`DG_DEMO_AUTOSTART=1 scripts/idf.sh -DSDKCONFIG=/absolute/worktree/build/sdkconfig-demo build`;
this uses a separate generated configuration and opts in through
`sdkconfig.defaults.demo`. NVS namespace `dg-demo` holds versioned URL, volume,
enabled and configured settings. Settings are loaded on the internal boot
stack; an internal-stack worker writes, commits and verifies updates under the
LCD/flash gate, with a bounded caller wait. `demo hold` stops capture/session
and suspends boot setup; `demo configure JSON` requires held/Idle. Provisioned
settings survive reset; normal test images remain manual even with enabled NVS.

The demo worker waits for WiFi, restores all 16 character slots from the
content-hash LittleFS store, enables character mode, applies volume (default
−6 dB), requests connect and enables capture at accepted bot-ready. Failed
restores retry with 1–30 s backoff; network/peer failures retain normal app retry
intent, including idle-timeout disconnects. `demo run` resumes a held demo image.
`standalone_assets()` and `preload_persisted()` use the store owner and digest
checks; they never fetch from 7874. B's status hashes are preloaded when its
`dg_display/demo_assets.h` is included in the merged build. Until then this
milestone restores the cat only; B owns the status-screen lifecycle integration.

Launcher `--provision` loads, explicitly persists and checks durable inventory
before rebooting and observing. `--provision-only --flash` performs that step
and exits after autonomous reboot, starting no bot. `--observe` starts the bot
and listens for autostart without sending configuration, capture or connect
commands after boot; it does not open the fixture server. The launcher still
starts the bot for a running demo; a persistent host bot service is out of scope.

`power` and PERF.power expose cached BQ27220 SOC, mV, signed average mA,
temperature °C, sample time, age and error counters. A low-priority 3 KiB PSRAM
worker reads at 5 s intervals on the sole shared I2C bus, 10 ms timeout per
transaction; audio and PERF do no gauge IO. Read failures produce invalid/null
measurements. Register definitions and units follow TI's
[BQ27220 TRM, standard data commands](https://www.ti.com/lit/ug/sluubd4a/sluubd4a.pdf):
0x06 decikelvin, 0x08 mV, 0x14 signed average mA, 0x2c SOC percent, address0x55.


The first integrated board run stopped before cycles on a raw GPIO duplicate
ISR-service installation error; `dg_board` now initializes that service exactly
once for touch and LCD TE. The next run onfe69866 proved three original injected
reconnect/turn cycles, but its bot-drop arm never restarted the server because a
20s pre-restart device-loss wait expired. That run remains an overall failure.
The corrected-procedure run on the same ELF proved reconnect/screens in all
three cycles; its third functional turn failed on drift_drops+1 (all other
transport and clip deltas0). The event is bracketed only by device timestamps
28321344–34008029us, spanning the prompt/reply/tail; it is not located precisely.
Evidence directories: B/touch-reconnect-proof-{1,2,3}. No whole-proof success or
strict qualification is inferred from those partial results.

Normal recovery testing now restarts the owned bot with SIGTERM and records its
actual exit status; a fallback SIGKILL cannot pass normal-stop mode. `--hard-kill`
retains the separate diagnostic. A's abrupt-loss run triggered a native raw
AGENT peer-not-response binding error after30000ms; abrupt SIGKILL recovery
remains unqualified, and the monitor has no exception for that error.

Combined autostart restores cat slots without animating them, then preloads the
status assets and enables demo mode before requesting connection. This prevents
a live-looking cat during startup restoration. The default `standalone_assets`
API still enables the character for existing callers; autostart explicitly opts
out until all status assets are ready. The combined normal and demo images build;
subsequent hardware results must identify their own ELF and original run.
### Standalone review corrections (Astra, hardware rerun pending)

The first autonomous live acquisition on 2afe78d restored the cat with 7874
closed and completed 3/3 live user/assistant turns. An added SIGKILL crash
experiment restarted the bot in 3.526 s, then the strict monitor stopped on the
peer library's E-level ICE binding timeout at 30 s. That acquisition remains
failed as a combined restart proof. The normal SIGTERM stop/restart proof is
still pending; its next attempt deferred before acquisition because host NIC
traffic was 2,282,108 B/30 s against the unchanged 2 MB limit.

Astra's standalone review requested five fixes, now implemented:

- `demo hold` ends the display session after successful peer teardown, allowing
  configuration writes and later restore. Teardown failure retains the guard.
- Codec unmute and its settle complete during codec initialization with PA off.
  Runtime Speaker start/idle now call a separate GPIO-only PA function, with no
  I2C or delay, removing gauge-bus contention from the audio task. The DAC stays
  unmuted across intentional idle; PA-off and TX silence provide idle suppression.
- Reconnect admission requires published Ready, tags its generation, and the
  policy validates that generation again. A full queue rejects the request
  without synthesizing a session failure; queued taps from reconnect cannot
  tear down a newly ready peer.
- Provisioning detects/reuses initialized display and the matching arena,
  turning character mode off before remapping slots. It can repair missing
  assets after an earlier autonomous restore attempt.
- Implicit observe mode from either provisioning option rejects `--unplug-check`
  before any hardware access, preserving command-free observation.

Host tests cover queued Ready/stale reconnect interleavings, hold ordering and
failure guards, duplicate-init repair, option combinations, and the actual
GPIO-only playback function with no I2C symbols linked. The held-bus host test
checks transition completion; it does not claim a measured hardware stall bound.
A new committed image and hardware rerun are required before qualification.


After A's generation-aware reconnect admission merged, touch status names a
false `request_reconnect()` result `request_rejected`; it can mean stale/not-live
intent as well as a full app queue. It is not attributed solely to queue loss.
Earlier proof artifacts retain their original `queue_full` field and counters.

### Combined touch proof on d4dc37c — incomplete

The new combined normal image, ELF SHA256
`27e409f834f42a2feb4594e3dd2e7a605d6009eb1e777594969e1f9283759a15`,
failed its original functional proof in `B/touch-reconnect-combined-d4dc37c-1`.
The first cycle completed Disconnected → Connecting → Live at device times
11.444766, 12.821755 and 12.986147 seconds, created a fresh peer/pipeline,
accepted one reconnect and ignored one reconnect-time injected tap. Physical
IRQ/read/tap counters remained zero. These results establish the exercised
injected action and screen sequence only.

That cycle's turn failed on `drift_drops` increasing from zero to one between
13.085789 and 18.786418 seconds; reply gate closure was 16.295638–18.335641
seconds. The snapshots cannot locate the drop within the prompt/reply/tail.
All other transport and both clip deltas were zero. The raw monitor, DMA and
lock counters were clean, with no failed allocations. Internal minimum was
82,688 bytes (768 above the 81,920 floor), and the smallest reported largest
block was 43,008 bytes. NIC traffic exceeded the conservative quiet threshold:
682,500 bytes in 7.616 seconds versus a 507,761-byte allowance. This observation
does not establish the cause of the drop.

Fail-fast left cycles two and three and normal SIGTERM recovery unexecuted.
Cleanup succeeded and released the bench. Astra independently checked the raw
serial and bot evidence and confirmed this failed verdict. Earlier failures
remain recorded; this is neither strict voice qualification nor a completed
combined reconnect/recovery proof. A's subsequent synchronous demo activation
fix is merged separately and does not repair or rescore this acquisition.

The touch UI accepts driver delivery only in Live with no pending reconnect.
It also rechecks that admission and the lifecycle generation after the bounded
touch poll, counting a recognized event discarded across a transition as
`phase_ignored`. App's independent Ready/generation admission remains in place.
The functional proof now injects three events per cycle: the Live reconnect,
one ignored event during Disconnected teardown, and a separate event during
Connecting. The last case requires status in the same Connecting generation
before injection and after counted ignored consumption, with no additional
request or request rejection. A tap consumed after Ready cannot pass that case.
These new cases have host coverage; their hardware proof is pending and is not
inferred from the earlier Disconnected-only ignored injection.

### Board-path NIC gate policy

The EchoEar path uses `wlp8s0` on the 192.168.7.x subnet. `enp7s0` serves a
separate wired subnet for host internet traffic. Continue recording monotonic
`/proc/net/dev` RX/TX deltas for both interfaces, but gate only the sum of
`wlp8s0` RX+TX with the unchanged 20,000,000-byte-per-300-second bound, scaled
to the measured interval. Do not subtract unmeasured board traffic. Missing or
reset WiFi counters fail closed. Missing/reset wired counters are recorded as
observation errors and do not fail the WiFi gate; a wired download alone does
not reject a quiet board path. Host networking remains untouched.

`hil/host_nic.py` emits policy `echoear-wifi-only-v2`, the gated interface,
per-interface deltas, and wired observation errors. `total_bytes` now means
the gated WiFi RX+TX sum, not the historical aggregate. Strict voice+cat
requalification enforces this result for each arm through `e2e_common.turn`.
The functional touch/recovery harness records the same result with
`nic_gate_enforced=false`; its NIC observations do not affect its pass/fail.
Functional proofs remain separate from strict network/acoustic qualification.
Earlier acquisitions are not silently rescored under this policy.

### Shared owned-bot restart arm

`hil/bot_restart.py` supplies the same arm to the standalone and touch proofs.
It sends SIGTERM to its owned bot, waits at most 10 seconds, then sends SIGKILL
if necessary and reaps it within five seconds. Evidence records each signal,
host timestamp, exit status, and whether fallback was needed. The replacement
bot must be HTTP-ready within 30 seconds of the initial SIGTERM. No existing
service is replaced, and group signals require the bot to own that group.

The declared bot-loss/recovery window begins immediately before SIGTERM and
ends at the first subsequent complete production `dg-app: STATE Ready` record,
explicit failure cleanup, or a 180-second deadline. This window includes device
loss detection after the host bot has restarted; the actual process exit and
HTTP-ready times are recorded separately. Only these exact complete E-level
line forms, observed in the earlier crash evidence, are permitted inside it:

```
E (<decimal timestamp>) AGENT: 0 Peer not response for binding after 30000
E (<decimal timestamp>) PEER_DEF: Peer disconnected
```

Every permitted match retains its raw line, component, byte offset, receipt
time and window label. Every other error still fails, including display/DMA,
codec errors, altered loss text, splices, and these same loss lines outside the
window. Ready closes the exception at its raw byte boundary, so a later error
in the same USB chunk fails. An incomplete candidate cannot pass the final
monitor verdict. Host tests cover chunk boundaries, deadlines, production Ready
framing, final-verdict races, and stop escalation.

Recovery requires ordered Disconnected → Starting → Ready lifecycle records,
a fresh generation, completed Disconnected → Connecting → cat frames, and a
successful turn on a distinct peer. `hil/echoear_restart_proof.py` can run just
this outstanding arm against a hashed frozen image bundle without repeating
earlier turns. A's requested 2291113 demo ELF predates the status-screen code:
it can establish lifecycle/turn recovery only, reported separately as
`recovery_without_screens_passed`; it cannot pass the full screen requirement.
B's image must supply all completed frames. Neither functional result replaces
the final strict voice/acoustic campaign or rescales earlier failed results.

The remaining-arm acquisitions used shared harness commit6584c42 and frozen
ELFs, with no firmware rebuild:

| Image | SIGTERM outcome | HTTP-ready restart | Remaining-arm result |
|---|---|---|---|
| A2291113 demo | 10 s timeout, SIGKILL, exit−9 | 10.526871 s | Autonomous lifecycle/new-peer/live-turn recovery passes; screen proof unavailable |
| Bd217582 normal | 10 s timeout, SIGKILL, exit−9 | 10.526561 s | Full remaining recovery arm passes, including completed status screens |

A's ELF SHA256 is
`1bb2f9cb0e02beaaaa9645c1dbba44f53334e867f95eb1a2834415ee0d4ebfb0`;
B's is
`0ab4ee14c5eb73c46189b2a004ade9acf373827187d841b5fcf00cd2d22418df`.
Evidence is `B/restart-A-2291113-1` and `B/restart-B-d217582-1`. Each acquisition
contains exactly the two declared loss lines, zero other raw errors, fresh
Disconnected gen2 → Starting gen3 → Ready gen3 progression, distinct peers,
and a recovered live user/assistant transcript turn. A sent zero console
commands. Both required SIGKILL fallback; graceful SIGTERM exit is not proven.

The recorded exception windows are 42.167885 s for A and 40.848132 s for B,
ending at the exact raw recovered Ready byte. B's completed frame records4/5/6
show Disconnected → Connecting → cat at device50.466306/51.678523/52.794333 s.
Astra independently rederived both results from raw serial and bot logs.
WiFi observations were within the declared bound but were unenforced. Both
holds cleaned up and released the bench. These are separate missing-arm
results; earlier three-turn failures, drift findings and unexecuted touch cases
remain recorded and are not converted to passing campaigns.

Item2 is predeclared in [the requalification plan](echoear-item2-requalification.md)
with strict3/3, A-owned A6 checks, quiet5+5, both room floors≤−55dBFS, WiFi-only
NIC gating and recorded unscored deferrals. It remains blocked from scored
launch until Claude announces the final master/ELF and drift-diagnosis outcome.
The host-only rehearsal cannot qualify hardware or authorize execution.

NIC policy correction (2026-10-14): the board route is `wlp8s0` on
192.168.7.222/22 (`daily-633`). Qualification gates total RX+TX on this interface
at 20 MB per 300 seconds, scaled to arm duration, without subtracting board
traffic. Record `enp7s0` deltas too, but do not gate them: its separate wired
192.168.42.75/24 subnet carries host internet traffic and does not share the
board's 2.4 GHz air. This supersedes the earlier combined-interface policy.
Standalone/restart/tap functional proofs observe NIC counters only; their
functional and integrity criteria remain unchanged. `host_nic.gate` supplies
qualification scoring; `standalone_proof.py` records that score as evidence
with `nic_gate_enforced=false` and never aborts on traffic or observation age.

Standalone corrected functional evidence: `A/standalone-2291113-2`, commit
`2291113`, ELF `1bb2f9cb0e02beaaaa9645c1dbba44f53334e867f95eb1a2834415ee0d4ebfb0`,
passes initialized-display hold/configure/reprovision/resume and three live
turns after autonomous persisted-asset boot with no fixture server and no
console commands after boot. Overall proof remains incomplete/failed: SIGTERM
left the host bot waiting for Uvicorn background tasks beyond the stop deadline,
so restart and the fourth turn did not run. No recovery or acoustic qualification
claim follows from the three functional turns. The first corrected attempt
stopped during setup on the wrong fixture directory; its raw failure is retained.


Reconnect drift diagnostic instrumentation: `PLAYOUT_DRIFT` is emitted through
zero-wait deferred logging for every drop/insert, including render deadline,
transport/playout generations, decision-time ready depth, target, 50-sample
remaining-occupancy sum/count and persistence duration. Ready includes the frame
being read; the filter uses ready minus one. Optional arrival kind6 retains its
count/direction fields and uses flags1 to identify added depth (`seq`) and target
(`bytes`); render kind14 now carries ready depth in `seq`. RTP record fields and
strict transport verdicts are unchanged. `hil/echoear_drift_probe.py` runs one
fixed set of six original reconnect arms alternating immediate PERF and quiet
acquisition, exporting traces only afterward. This is causal evidence, never a
qualification pass or permission to exclude warm-up corrections.

Measured startup-offset correction (diagnostic, not qualification):
`A/drift-reconnect-69f2574-burst3-1` releases the first three offline RTP frames
in one batch (recorded send gaps 0/0 ms, then approximately20 ms cadence).
Fresh sessions in arms1/5 start with eight ready frames and drop one at
2.081/2.092 seconds after Ready: target5, filtered remaining mean6.70/6.96,
persistence1.000/1.020 seconds. Lost/concealed are zero in those arms. This
reproduces a static startup queue offset with equal clocks; it does not prove
the missing timing history in B's original untraced failures. Arm4 separately
has lost/concealed4 and an insert; all raw failures remain failures.

Prefill now starts early only when six **contiguous** frames already provide
five remaining frames (100 ms) after the first decode. The original100 ms wall
deadline still handles a slowly filling reserve. This prevents double-counting
a queued startup batch as an additional100 ms wait and later deleting a frame
to remove the excess. It can close the startup reorder window earlier once
that contiguous reserve exists; it neither advances the watermark nor edits
media to do so. Drift deadband/persistence, loss/clip counters, strict gates,
and the deadline fallback are unchanged. No warm-up exclusion is introduced.
New zero-rate startup-burst/reserve/fallback tests fail against the unchanged
69f2574 playout body and pass with this change; all95 peer tests pass.
Hardware verification of the changed prefill follows on its committed ELF.

Targeted verification of `c19d0b4`: ELF
`5d2ccb8112fa187449fb7e8021a2717d4b441ab6e81d77330c4fc8467c7ae253`,
`A/drift-reconnect-c19d0b4-burst3-1`, same explicit startup-batch condition,
six fresh reconnect arms. Drift drops/inserts, lost/concealed, I2S errors and
clips all remain zero in recorded counters; first decode depths6/6/6/7/6/6,
trace overwrite0, raw monitor errors0, cleanup errors0. Immediate-PERF arm
gates pass; quiet-arm before snapshots remain unavailable, so the diagnostic
is explicitly **not** an overall integrity/qualification pass. This verifies
the measured startup-offset fix; B's earlier untraced event remains attributed
only by consistency, not retrospective proof. No warm-up exclusion is needed.

Separate blocker retained: historical internal heap minimum80,372 bytes is
below the81,920-byte floor in this instrumented combined workload. Largest
internal block remains43,008 and allocation failures0. This candidate must not
be called qualification-ready. The earlier quiet-arm six-frame lateness and
injected-baseline arm4 four-frame lateness are also retained failures.

Internal heap correction for the standalone integration: the live task census
is already in PSRAM (1,116 bytes, zero internal allocation delta). The remaining
hole was the string type of `StatJson`: nodes/containers used the private192 KiB
PSRAM arena, while long key/value character buffers and serializer scratch used
`std::string`'s general allocator. `StatString` now uses `StatAllocator<char>`
too, preserving the JSON schema. The production dumper has host round-trip and
allocation coverage; the character-allocation test fails with the old header.

The same two-session/six-PERF heap probe improved historical internal minimum
from81,484 to84,312 bytes, resident free by about2,460 bytes, largest block from
40,960 to45,056, with zero allocation failures and no minimum dip while task
rows are assembled. No minimum counter is reset and the81,920-byte floor stays.

EchoEar's20 ms Opus profile additionally returns4,096 resident internal bytes
by sizing its TX stack at28,672 instead of32,768. Prior speech/reconnect probes
measured worst free10,888 at32,768; the reduction leaves an estimated6,792-byte
margin to verify with actual speech before integration handoff. The40 ms Opus
Kconfig lower bound remains32,768, and the legacy profile stays32,768. No
playback/owner stack, codec configuration or CPU priority changes. Final proof
must measure actual stack and heap margins on the selected committed demo ELF;
AEC remains off until its additional memory/CPU budget is measured.

The single combined item1 acquisition on integration candidate
`f8b857337c1a2c78e352664dec9433b649f78c4f`, demo ELF SHA-256
`6bc610a56357b74cac9484836b08d70a36f7008e3d8058a7240be29631e52b61`,
is **INCOMPLETE**, with the original failed verdict retained at
`B/item1-f8b8573-combined-1`. Persisted boot without the fixture server,
completed Connecting → cat frames, and three functional live transcript turns
on one peer/pipeline succeeded without console commands. Raw monitor and
cleanup errors were zero. The runner then incorrectly required a Disconnected
frame at initial boot, applying the reconnect-only sequence to fresh startup.
This harness error stopped acquisition before all injected cycles, restart,
and strict PERF sampling; transport, heap, allocation and TX-stack acceptance
remain unproven on this acquisition. The host fix tests Connecting → cat at
boot and preserves Disconnected → Connecting → cat for reconnects. No firmware
change, retake, final acoustic qualification, or physical touch claim follows.
Astra independently audited the raw partial evidence and failure cause.

Claude authorized `B/item1-f8b8573-combined-2` as the first full acquisition on
that same frozen ELF after the host-only boot fix. It **FAILED the unchanged
strict transport gate**: three autonomous no-console turns and their cumulative
transport/clip counters passed, then the first injected reconnect's initial
PERF reported `playout_dropped=3`, entirely `playout.rejected.preconnect=3`
(`preconnect_packets=3`). The sample is at device59.178871 s, 236.608 ms after
App Ready and before the reconnect's first prompt/reply. Other transport and
split-clip counters are zero. The first reconnect completed Disconnected →
Connecting → cat, created a fresh peer/pipeline, and consumed the Connecting
injection while still in Connecting without another reconnect request.
Its reply, cycles2/3 and bot restart were not executed after the gate failed.
Observed internal minimum88,364 bytes, largest49,152, allocation failures0,
TX stack28,672/free minimum6,792, DMA/raw-monitor/cleanup errors0. Autonomous
WiFi traffic was743,419 bytes over57.866 s (observe-only); the failed cycle
retains its initial NIC snapshot but has no completed end bracket. No packet
arrival trace was enabled, so exact rejection times and cause beyond the
recorded preconnect classification remain unproven. The bench was released;
the failed gate is retained without a startup exclusion or qualification claim.

### Reconnect RTP admission after the f8b8573 combined proof

B's `item1-f8b8573-combined-2` is a strict transport FAIL. Its initial three
autonomous turns passed with zero console commands and zero transport/clip
counters. After the first injected reconnect, the first PERF at device
59.178871 s reported three dropped packets, all `playout.rejected.preconnect`;
other rejection reasons and the remaining transport/clip gates were zero.
The packet arrival timestamps were not traced, so the evidence cannot establish
their exact timing. The reconnect reply and subsequent arms were not run.
Heap minimum remained 88,364 B (6,444 B above the unchanged floor); TX stack
minimum free across the acquisition was 6,792 B.

The receiver previously rejected RTP until the separate SCTP `chat` channel
opened. The patch admits the authenticated receiver's valid, SDP-authorized
Opus track while that handshake finishes. Stopped, failed and retired sessions,
unknown tracks, payload types, malformed packets and non-20-ms frames still
fail admission. The existing bounded playout retains and renders admitted
payloads; no counters, thresholds, warm-up exclusions or buffer targets change.
A first-packet deferred `RTP_BEFORE_CHAT` record and cumulative
`rtp_before_chat_packets` / `first_rtp_before_chat_us` expose this ordering;
optional arrival tracing also records ingress rejection reasons and identities.
Host coverage exercises 0/1/3/6 early packets through production admission and
playout over four seconds, checking sequence preservation and ordered decode decisions,
zero rejected/missing/PLC/drift frames, and invalid/retired-session rejection.
This patch awaits hardware measurement and review; the frozen f8b8573 image
and the failed acquisition remain unchanged.

### Bound native receive work before chat opens

Astra's review of e24e69a found that admitting early media left the native
receive pass exempt from B7's work budget until chat opened. The policy now
activates immediately at the first authenticated, SDP-authorized media callback,
before playout reservation/copy/accounting. The current pass includes all UDP
reads, bytes and time already elapsed since its beginning; later callbacks
cannot renew its deadline. Media activation persists into subsequent passes
while chat remains closed. The existing four-datagram, 4096-byte and 4 ms
limits stay unchanged. ICE/DTLS-only setup remains exempt until chat or valid
media, write-select behavior stays unchanged, and after-read activation still
preserves idle read waits. A new session starts with fresh policy state.

`peer_profile.prechat_receive_budget_activations` records the first activation
before chat. Host regression drives sustained authorized media through actual
admission, budget and playout: four small reads or three extension-bearing
large reads stop the same pass before queue overflow. It also covers time-only
exhaustion, prior setup work, repeated callbacks, select read/error clearing,
write preservation and subsequent passes. Restoring the old setup exemption
makes this regression fail. No preconnect drops or counter exemptions return.

The optional offline probe delays only SCTP writes by three seconds, leaving
ICE/DTLS and RTP bytes/cadence intact, to exercise sustained pre-chat playback.
The B7 flood runner can use the same delayed chat and then its unchanged
25/50/100/saturating **post-chat SCTP message** rates. Such rates are not a
pre-chat SCTP flood: the data channel is not open in that phase. Host burst
coverage independently checks pre-chat packet/byte/time exhaustion. Hardware
results and exact candidate provenance are recorded in A's report.

The earlier c19d0b4 prefill change cannot cause or cure `rejected.preconnect`:
that rejection ran before playout insertion, outside the prefill controller.
Historical f8/B6 packets were not individually traced, so their validity,
content and audibility cannot be reconstructed from the rejection category.
Discarding early valid speech could truncate a greeting. Prospective tests
prove valid early media is now retained, with no startup counter exclusion;
B's combined-3 on e24e69a remains functional evidence for that admission fix.

The c3a02be demo was tested with the new budget. The natural-timing control
passed three reconnects: 4/4/3 early packets, one pre-chat budget activation
per session, all eight gated snapshots' cumulative transport13 and split clip
counters zero; internal minimum 87,696 B and TX free 6,704 B. A separate
three-second delayed-chat acquisition remains **FAIL**: its first reconnect
passed with 153 early packets; its second admitted 154 early packets with a
clean startup sample, then recorded one drift insertion about 1.435 s after
the reply gate reopened. No loss, PLC, rejection or clip accompanied it. The
third cycle was not run. Its cause is untraced; there is no exemption or
retrospective rescore.

On the same c3a02be demo ELF, four independent B7 resource-stress arms each
observed more than 150 early packets across roughly three seconds, activation
count one, then the declared 30-second **post-chat** message load:

| Offered messages/s | Sent | Counted shedding | Internal minimum B | Session sampled minimum B | Lowest display interval fps |
|---|---:|---:|---:|---:|---:|
| 25 | 750 | 6 | 86,972 | 104,591 | 26.65 |
| 50 | 1,500 | 14 | 86,192 | 105,107 | 26.36 |
| 100 | 3,000 | 40 | 87,428 | 104,583 | 23.94 |
| Saturating (253 achieved) | 7,590 | 4,008 | 85,604 | 104,095 | 2.72 |

All existing B7 resource gates passed: counted admission accounting, successful
wire/show/render, progressing display, memory floors, no failed allocations or
display DMA errors, drain completion and strict raw monitoring. Those existing
flood gates require display progress, not the voice campaign's 24 fps floor.
Do not claim that stricter frame-rate gate passed at 100/s or saturation.
Sampled transport13 counters stayed zero at 25/50/100; saturation ended with
one drift insertion, all other transport counters zero. This is not a zero-edit
transport pass at saturation. WiFi quiet observations passed at 25/50 and
failed at 100/s and saturation; wired totals were retained separately. Offered
board traffic is included in those totals, with no subtraction or changed
quiet threshold. These functional stress results cannot qualify network or
acoustic quality. Each hold cleaned up its owned processes and reader.

Raw evidence: A/prechat-budget-c3a02be-{1,natural-control-1,flood-25-2,
flood-50,flood-100,flood-saturating}; summary A/prechat-budget-flood-summary.json.
The earlier flood-25 setup attempt is retained as a harness failure before
connect/load (a display mode command preceded cold LCD initialization). The
runner now reuses shared initialization before mode commands, with cold and
restored-display regression coverage; resource gates were not weakened.

The authorized successor acquisition `B/item1-e24e69a-combined-3` on exact
candidate `e24e69aff8059c024ad6f4e0475aa116c13019bc`, frozen demo ELF SHA-256
`67e9da123caa869b3ce28b9fe1b22ba29c4375466b82f64ac89ac7d9cb589249`,
passes the combined **item1 functional proof with strict counters**. It covers
persisted autonomous boot with the fixture server stopped, three live turns
with zero console commands, three injected reconnects with completed
Disconnected → Connecting → cat frames and fresh peers/pipelines, and bounded
bot-restart recovery followed by another turn. Every reconnect consumed and
ignored an injected Connecting-phase tap before Ready without another request.
The final driver counts are nine injections, six ignored, three accepted
requests and zero rejections; physical IRQ/I2C/tap recognition remains untested.

All17 PERF samples have absolute cumulative zero for all13 transport counters
and total/open/closed clip counters, including startup; no baseline exclusion
or threshold change was used. Observed internal minimum is88,352 B (6,432 B
above81,920), largest block49,152 B, allocation failures0, audio-TX stack28,672 B
with minimum free6,600 B. DMA/lock/send/late errors are zero; app-control minimum
free9,052 B. The bot required SIGKILL after the10-second SIGTERM grace;
replacement HTTP readiness took10.528724 s from TERM, device Ready41.298685 s.
Only the two exact expected AGENT/PEER_DEF loss records occur inside the
bounded bot-down window, which closes at recovered Ready; other raw errors0.
This does not demonstrate graceful SIGTERM-only server shutdown.

Complete per-arm WiFi observations are below the proportional20 MB/300 s
bound; wired traffic is recorded separately and not gated. NIC remains
observe-only for this functional proof. Cleanup errors0; the bench is released.
Ordered transcript pairs establish functional continuity, not answer correctness
or final acoustic/STT quality. Earlier f8b8573 partial/failing acquisitions stay
unchanged. Item2 strict voice/acoustic qualification still requires its own
coordinator-approved final ELF and predeclared campaign.

Final master9bc2d42 has firmware source identical to c3a02be. The final demo
ELF86229001's `B/item1-c3a02be-combined-4` is a retained strict FAIL: functional
boot, autonomous3, injected3/status/Connecting ignores and bot recovery work,
but the recovered session ends with one lost/concealed/PLC frame and one late
rejection. The event is bounded149.611026–164.842697s, encompassing reply gate
153.437147–156.797023s; no packet trace establishes its exact time or cause.
Astra independently confirms the failure; no earlier pass substitutes for it.

Released item2 uses one frozen normal master ELF
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`,
version9bc2d42, mic125%/TX28672 and autostart off. Evidence-only release binding
`B/item2-final-master-9bc2d42/binding.json` freezes379 harness/bot/input hashes.
The original strict3/3 cohort stops on its first scored turn: Jabra reply
RMS−29.452196dBFS fails unchanged−22±3dBFS at volume0. Both room floors and
WiFi eligibility pass; this is a scored failure, never a deferral or retake.
Transport13/splitclips0, full-reply FPS28.979 over11.905s, wire render proof
and unprotected headroom5.070dB pass. Heap minimum84,808/largest49,152,
allocations/locks/raw errors0. Astra replays the WAV and confirms this FAIL;
originals2/3 remain unexecuted under the existing fail-fast choreography.

The separate A6 echo-stress arm completes both frozen boosted phrases with
runner PASS: unprotected headroom5.589/3.894dB, protected8.380/7.912dB,
ADCclips0, transport13/splitclips0. Native peaks13,987/17,003; unprotected
17,218.83/20,929.10; protected12,486.59/13,177.51. Firmware and reconstructed
limited counts match4,692/9,867. Both room sensors and30s WiFi eligibility pass
before each point; memory minimum88,796/largest49,152, allocation/lock/raw
errors0, cleanup0. This supporting finite limiter test cannot erase either
retained failure or establish final voice/acoustic/analog qualification.

Astra independently confirms the A6 echo-stress PASS from native-WAV replay,
including the exact limited counts and unprotected peaks. The separate hot-STT
pair also has runner PASS: actual native RMS−39.279/−27.454dBFS, both actual
received-uplink France prompts transcribed exactly (7words, edits0, WER0).
Baseline limiting0; hot limiting10 samples in firmware and reconstruction,
native hot peak8,652 above the6,554 knee threshold. This modest exposure does
not establish accuracy under heavy limiting. Both room/NIC checks and unchanged
transport/clip/resource gates pass; heap minimum96,184/largest55,296,
allocations/locks/raw errors0. STT requests ran once after bench release.
This finite pair remains supporting evidence; earlier qualification failures
are retained and overall firmware qualification is false.

Astra confirms the finite hot-STT pair PASS using actual received-uplink
alignment and native-WAV limiter replay. Once-only off-bench scoring follows
the reviewed code/stored marker; separate HTTP request timing/count records
were not retained. The single analog compound capture is **INCONCLUSIVE**:
measurement-valid tone windows have completeH2–H5, SNR≥30dB and no clipping,
but native tone peaks5,913/4,586/4,488 do not reach speech peak15,006.
Physical tone levels are nonmonotonic despite increasing source steps.
Native/Jabra THD percentages are9.002/7.165,5.835/4.709,5.215/4.261 respectively;
these apply only to tones. No differential investigation flag is raised, but
missing speech-level coverage prevents an analog-linearity PASS or ES7210
certification. The fixture and DSP remain frozen; no second capture is taken.

The user moved the board, Jabra and camera before the final-master campaign.
The low Jabra level requires an old/new image comparison before attributing
it to that change. The original strict attempt remains **FAIL**. The selected
historical reference is the exact retained14284c8 ELF
`94a5a13ccd5555a8fd39710a140cf514c012d7d7e248554d632915b61ec7d444`.
Its application BIN was regenerated with esptool, matching DIO/80MHz/16MB
and64KB MMU settings, and the embedded version/full ELF hash were verified.
Both comparison images use the same frozen final-master bootloader and
partition binary; this is not described as an original complete14284c8
flash bundle. Its original offline run1 used the identical fixed reply:
matched full-reference Jabra RMS−23.908268dBFS, native echo peak11,828.
Evidence and the predeclared A/B/A plan live in
`B/geometry-14284c8-reference-image` and `B/geometry-14284c8-ab-plan.json`.

The optional B6 source rebuild also completed with clean tracked source at
dfe47875cdc9fdd462510b27bd22ce9b2a8c72fd. Rebuilt ELF
`46d9149e8adc609053c62cfeaa76593241ab01d93e1f5689f9b0674d19293eb0`
is explicitly different from the original qualifiedb6704fe2 image and is
not selected for this comparison. No new band or qualification result follows
from preparing either reference. A possible future band shift must use only
the historical-image measurements in the new arrangement, receive raw-data
review, and precede a fresh declared three-original campaign.

The original A/B/A acquisition is complete at
`B/geometry-14284c8-ab-1`. All three full flashes and boot hashes match
their frozen image. Fixed-reference results are:

| Arm | Jabra RMS, dBFS | Native echo peak | Diagnostic validity |
| --- | ---: | ---: | --- |
| Old14284c8 A1 | -31.754075 | 12,893 | FAIL: two drift inserts, four latency trims |
| New9936da45 B | -31.707268 | 12,937 | PASS: all13 transport deltas zero |
| Old14284c8 A2 | -31.720937 | 12,749 | PASS: all13 transport deltas zero |

Both old-image measurements are about7.8dB below the same reference's
historical−23.908268dBFS, while the independently valid B/A2 pair differs
by only0.013670dB. The two old measurements differ0.033138dB; native echo
peaks stay within about0.75dB of historical11,828. These observations
support a changed acoustic measurement path with no detected old/new
playback-level difference. They do not uniquely identify the physical cause.

The declared all-three diagnostic remains **FAIL/inconclusive** because A1
violates its zero-edit prerequisite. The timing of its edits within the
25.249-second bracket is unknown. A1 was never repeated or rescored:
an explicitly reviewed continuation collected only previously unrun B/A2,
preserving the original result and declaration. Raw errors, eligibility,
mixers, clipping and cleanup pass in all three arms. No Jabra rebaseline
has been applied and no new strict cohort has been launched. The original
strict and combined-item1 failures remain unchanged.

Claude subsequently authorized a prospective geometry rebaseline from old
image A1 and canceled any further A/B/A acquisition. The old14284c8 edits
predate the c19d0b4 prefill fix and remain diagnostic observations; no such
exemption applies to9936da45. The earlier diagnostic and attempt1 verdicts
remain recorded as FAIL. The unused reply-window validation is canceled.

For the identical fixed reply, old-geometry Jabra RMS was−23.908268277dBFS
and old-image A1 now measures−31.754075143dBFS. Attenuation is therefore
7.845806866dB. Shift the original nominal−22dBFS by that OLD-image-only
offset: new center **−29.845806866dBFS**, unchanged±3dB interval
**[−32.845806866,−26.845806866]**. No measurement from the new image
enters this calibration. Frozen arithmetic/source hashes and the explicit
coordinator decision live in
`B/item2-geometry-rebaseline-9936da45/jabra-policy.json`; Astra reviewed
the derivation in `B/geometry-rebaseline-decision-review.md`.

The expected rise shifts by the same offset. Using the historical replay's
baseline window, fixed-reply rise36.301475dB becomes28.455668dB; historical
long-reply rise37.995435dB becomes30.149628dB. These are descriptive
expectations that also depend on room floor and content. The actual minimum
rise gate remains **10dB**. The earlier original-score baseline window gave
a slightly different descriptive fixed-reply expectation; all subsequent
declarations use the consistently frozen replay values.

Fresh strict attempt2 requires all three original runs, followed by fresh
quiet offline5/live5 on unchanged normal ELF9936da45. No retakes or prior
runs count. Finite scored failures remain failures while collecting remaining
originals; fatal raw/device/setup/cleanup failures stop an incomplete cohort.
Peer continuity, both room floors≤−55dBFS, WiFi eligibility, full-turn zero
transport counters, clipping/headroom, display/FPS/wire proof and memory
gates remain unchanged. Wired traffic is recorded without gating. Prior
strict attempt1 and quiet-offline attempt1 remain FAIL, including the latter's
independent uplink-tail failure. The short AEC hold precedes these new
acquisitions; B independently reflashes and verifies the frozen boot hash
after the handoff.

Final-master requalification on frozen normal ELF9936da45 remains **FAIL**.
All fresh attempt2 originals were collected without repeating a scored turn.
The reviewed geometry band was fixed before capture and remains unchanged.

| Attempt2 population | Reply checks | Aggregate result |
| --- | --- | --- |
| Strict voice+cat | 3/3 PASS | FAIL:120/5354 eligible uplink gaps exceeded60ms,2.241315% against1% |
| Quiet offline | 5/5 PASS | FAIL:203/8505 uplink gaps,2.386831%; original4 whole-turn late/lost/concealed/PLC/drop+7 |
| Quiet LIVE | Original1 levelFAIL; originals2–5 replyPASS | FAIL: original1 below band; continuation119/6510 uplink gaps,1.827957%; original2 whole-turn late/lost/concealed/PLC/drop+3 |

The offline damage appears after the clean reply-end snapshot: last clean
263.368345s, first dirty281.858018s, gate reopen262.910088s. It occurred
within(0.458257,18.947930]seconds after gate reopening. LIVE original2 has
last clean53.046581s, first dirty71.042870s and gate reopen52.592506s,
so its corresponding bound is(0.454075,18.450364]seconds. These are snapshot
bounds; precise arrival times and causes were not captured. All scored reply
transport deltas are zero. Later turns retain the nonzero absolute counters;
zero subsequent deltas do not erase earlier loss. Raw teardown snapshots
also retain one additional playout drop outside each quiet cohort's final
scored window.

Strict full-reply FPS lower bounds are29.223196,28.711076 and28.923000;
all three wire commands have rendered-layer proof. Terminal acknowledgments
were Completed, Superseded and Superseded respectively. Historical internal
heap minima/largest blocks were87168/47104 bytes for strict,
88828/49152 for offline, and85008/47104 for the LIVE continuation.
Failed allocations, display DMA/lock failures and both gate-state clip deltas
were zero. All original room-floor and WiFi eligibility checks passed;
wired enp7s0 traffic was recorded without gating. Protected and unprotected
microphone peaks, limiter counts and actual memory values remain in each
capture's evidence; these resource passes do not override transport failures.

LIVE original1 measured−33.970835dBFS, below the reviewed lower bound
−32.845807dBFS. Before original2's scored prompt, a requested cold reset
exposed a host monitor defect: it compared cumulative rendered counts across
boots. Original result bytes and unscored setup evidence remain preserved.
The requested priority handoff released the bench to A's uplink A/B/A before
any further scored LIVE prompt. After A returned it, B independently verified
the MAC, fully reflashed9936da45 and verified the full boot hash.

Host commit6593a39 fixes requested boot epochs while retaining fail-closed
monitoring. A reset must complete with the exact full-hash production boot
record. Missing completion and unexpected resets fail; prior errors survive.
The reviewed continuation collected only previously unscored originals2–5,
with the original band and all transport/resource/raw/NIC gates. Its reply
levels were−32.017036,−29.953656,−31.283088 and−31.737872dBFS;
unprotected headroom was7.738535,6.009922,6.333024 and5.870069dB.
All five original slots now have scores across the interrupted acquisition,
with an explicit peer/session discontinuity and overall qualification=false.
This is not a clean same-peer5/5 result. The historical510-input binding,
original LIVE failure and every attempt1 failure remain unchanged; a separate
526-input amendment records the host correction and continuation provenance.
Host validation passed99 tests plus8 subtests, with independent Astra review.

Evidence is under `~/src/evidence/echoear/B/item2-final-master-9bc2d42/`:
`strict_voice_cat/attempt-2`, `quiet_offline/attempt-2`, and
`quiet_live/attempt-2` including `continuation-1` and `resumed-result.json`.
Each quiet late-event bound is retained as `late-silence-timing.json`.
The amendment and independent provenance audit are in
`B/item2-geometry-rebaseline-9936da45/live-resume-binding.json` and
`live-resume-binding-review.md`. No further hardware acquisition is queued.

The later prospective strict attempt3 did not start: all five RF eligibility
probes failed the fixed<0.5% admission gate, with rates3.498834%,1.966011%,
0.833056%,2.068045% and2.400000%. The initial probe plus four30-minute
retries exhausted the declared schedule without consuming any scored turn.
All prior failures remain unchanged. The bench and timer are released;
normal9936da45 remains installed. See the
[RF acquisition record](echoear-attempt3-results.md) for counts, timing,
resource values, packet caveats and evidence locations.


One later observation-only WiFi telemetry acquisition on A's frozen2a6acb2
observer image completed three scripted prompt windows, with receive tails
15/1200,14/1199 and58/1195 and no source adjacent-send gap above60ms in the
surrounding retained traces. Sampled RSSI was−40..−37dBm, PS0/channel6;
retry counts remain unavailable. Whole-bracket drift drops/inserts increased
by1/2. This is descriptive evidence, with no new qualification or unique RF
cause claim. Normal9936da45 was restored and verified before releasing the
bench. See the [WiFi observation results](echoear-wifi-observation-results.md)
for the transcript-fragment caveat, timing boundaries and raw evidence.

## Experimental AEC resource/slot trial

The single approved trial on experimental ELF 82d032bd (source 38d0e16)
passed startup/capture resource guards but **failed reference slot identity**.
Internal minimum 157,568 B, capture CPU 33.4% steady/39.4% during direct tones,
and capture/playback stack minima 11,812/14,956 B were observed without a peer
or LCD load. No failed allocations, native/AEC clips or I2S errors occurred.
These are short-trial values, not full-session margins.

The opt-in selector reads the second 32-bit DMA word's high half as MIC3;
ES7210 TDM I2S order 1,3,2,4 makes that MIC2. The electrical loopback should
occupy the first word's low half and was not saved. True loopback alignment
and total AFE delay remain unmeasured; no ERLE or acoustic qualification was
scored. AEC stays default-off. See [the measured audit and pinned datasheet](echoear-aec.md#single-resourceslot-trial-on-frozen-82d032bd).
Bench released to B after the one 33.77 s hold; B reflashes its normal image.

## Uplink tail A/B/A on frozen old and new images

The short comparison measured old/new/old receive tails of 1.498751%,
2.220372% and 3.281424% over 60 ms. All exceed the unchanged 1% limit;
the new image lies between the old repeats. All 11,841 consecutive source
send intervals in the retained traces were below 60 ms, with receive
reordering and post-send variability on both images. No new-image regression
was established and no firmware candidate was changed. See the
[full method, counts and attribution limits](echoear-uplink.md).
The three holds ended with bench release to B; prior strict/acoustic failures
remain unchanged.


The experimental AEC candidate now repairs MIC3 selection to word0 low16,
with a regression against the actual old Mic body. Optional schema2 capture
retains all four raw slots plus AFE output. The prepared short trial uses an
aperiodic DAC probe and a separate silent-reference near-end capture to
measure relative loopback alignment and total AFE delay independently.
Astra review and a B-coordinated hold are still required; the old failed trial
is unchanged, AEC remains default-off, and no acoustic qualification is implied.
See [the candidate and measurement limits](echoear-aec.md#mic3-repair-and-the-next-reviewed-trial).

## First scored AEC acquisition remains unconsumed

The approved corrected harness (`6485e8b`, binding`5799a9ac`) passed quiet
eligibility and remeasured the independent AFE delay at64ms, then stopped
before opening a live peer: the short Stop calibration's waveform identity
failed its fixed correlation and uniqueness checks. No scored originals
started (0/15). ERLE, false turns, barge-in, full-peer heap and uplink are
unmeasured; this is a measurement prerequisite failure, not an RF result.
AEC remains default off. [Measurements and independent raw audit](echoear-aec.md#first-scored-acquisition-prerequisites-failed-campaign-unconsumed)
retain both prerequisite failures and the unchanged thresholds.

Normal firmware`9936da45` was restored under the same lock and its complete
boot hash verified before bench release at10:25UTC. No bot, fixture server
or serial reader was left running.
