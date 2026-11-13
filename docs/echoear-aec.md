# EchoEar AEC

**Status (step 9(d), branch aec-merge).** Candidate aec-v87 (source 58f567e,
ELF `cbabb650…f056`) is AEC-qualified by q1-aec-v87-01 under q1-freeze-03
(decision 107), within the [qualification scope](echoear-aec-q-scope.md):
AEC endpoints only, with no claim on STT, transport, resources or sustained
double talk. AEC stays **default off**. The tree builds two images.

**AEC gate for a rebuilt image (decision 109(a)).** A rebuild whose difference
from cbabb650 is metadata only inherits the qualification. It needs two things:
the masked-compare JSON (`hil/compare_masked_image.py`, [V2 flash-set
comparison](compare-masked-image.md)) as the proof of record, showing zero
unmasked bytes against cbabb650, and a promotion smoke on that exact bundle.
The smoke is an equivalence/promotion check, not a new qualification run, and
it leaves the q1-freeze-03 evidence untouched. Any functional difference
(source, resolved configuration, dependencies, toolchain or layout) re-runs
the frozen qualification on the new image. The history below is kept as
written; its "unqualified" wording describes the state at the time.

## Two firmware images

Both images come from the same tree and differ only in Kconfig. Build them
side by side in separate build directories, after `get_idf` (or
`. ~/esp/esp-idf-v6.1/export.sh`). `sdkconfig.local` holds the WiFi
credentials and is gitignored; the commands reference it by name only.

1. **Mute image (default).** AEC off; the mic is muted while the speaker
   plays (half duplex). This is the default `sdkconfig.defaults` path:

   ```sh
   idf.py -B build-mute -D SDKCONFIG="$PWD/build-mute/sdkconfig" \
     -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear' build
   ```

   A plain `idf.py build` (or `scripts/idf.sh build`) resolves the same AEC-off
   configuration into `build/` and the root `sdkconfig`. Add
   `sdkconfig.defaults.demo` before the last entry for an autostart demo
   variant of the mute image; that variant is not the rollback image.

2. **AEC image.** The qualified configuration: demo autostart plus the
   `sdkconfig.defaults.echoear-aec` overlay (`DG_ECHOEAR_AEC=y`, reference
   gain 15 dB, guard 400 ms, re-arm 2000 ms, WiFi observer on, as in the
   qualified binary). esp-sr 2.4.7 AFE_AEC FD_LOW_COST with filter length 4
   is fixed in `components/dg_audio/src/aec.cpp`:

   ```sh
   idf.py -B build/accept -D SDKCONFIG="$PWD/build/config-accept" \
     -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear;sdkconfig.defaults.demo;sdkconfig.defaults.echoear-aec' build
   ```

   The resolved config must read back `CONFIG_DG_ECHOEAR_AEC=1`,
   `CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15`, `CONFIG_DG_DEMO_AUTOSTART=1` and
   `CONFIG_DG_WIFI_OBSERVE=1` in `build/accept/config/sdkconfig.h`, and the
   boot log must show `MIC3_GAIN=0x15 analog_db=15`.

   Keep the build directory `build/accept`, the one the qualified candidate
   used. `cmake/display_http_psram.cmake` compiles a copy of
   `esp_http_client.c` from the build directory, so the directory's relative
   path is embedded in one `__FILE__` string. Another name of a different
   length shifts `.flash.rodata` and every pointer into it. With
   `build/accept`, the image matches the qualified ELF in every loadable
   section except `.flash.appdesc` (project version, build date and time).

Delete the generated `sdkconfig` (`build-mute/sdkconfig`,
`build/config-accept`) after changing any defaults file; a stale one
overrides the defaults. Build sequentially: both builds share
`managed_components/` and `dependencies.lock`. The AEC-off configure rewrites
`dependencies.lock` without the AEC-only entries (esp-sr, dl_fft, cjson,
esp-dsp). Run `git checkout -- dependencies.lock` after a mute build and
before the AEC build. Otherwise the AEC image's project version carries
`-dirty`, and the rewritten lockfile must not be committed.

Each image pairs with the same bot, and the bot behaves the same for both
(decision 141, 2026-10-30): it never mutes the user and always allows
interruptions. The device alone decides what reaches the bot. The AEC-off image
closes its capture gate while the speaker plays; the AEC image cancels the echo.
`server/demo_bot.py` logs the firmware's `/start` audio capability
(`DEMO_CAPABILITY aec=...`); the only thing it gates is admission to the opt-in
AEC evaluation hooks. `ECHOEAR_AEC_FULL_DUPLEX` was removed: `demo_bot.py` exits
at process start if it is set, item1 refuses it before the bench lock, and
rollback-01/mute-01 preparation and runs are refused before bench access (their
judges score the old bot's witnesses). Harnesses that run the bench test bot
`server/bot.py` are unaffected. Before decision 141 the demo bot also muted the
user in half duplex (`AlwaysUserMuteStrategy`); AEC-off demo-bot results through
step-10 acceptance r20 (item1, mute-01..03) used that policy, so a demo-bot
AEC-off claim on the new policy needs fresh evidence. The item2 stages ran the
separate `bot.py`. Both merged images answer the
bot's liveness pings. **Gap 3 (after the
step-10 requal-n1 acceptance r20 PASS of record):** the bench restore-of-record
and the rollback target is the qualified AEC-off normal image of master 16c45a3,
ELF `55152f25…` (`aec-demo/step-10/requal-n1-bundles/normal`, used in place),
via `hil/echoear_aec_bench.py` `NORMAL`/`NORMAL_HASH`; `hil/echoear_rollback.py`
imports them. Before gap 3 the step 9(e) rollback target was the previous
half-duplex image `9936da45` (source 9bc2d42) with `ECHOEAR_AEC_FULL_DUPLEX=0`
and `ECHOEAR_LIVENESS_SECONDS=0`; that firmware has no pong handler. It remains
`HISTORICAL_NORMAL_9936DA45` for the frozen step-9 AEC campaign family and the
retained-evidence judges. This pairing was bench-run as
[rollback-01](echoear-rollback-01.md) on 2026-10-19 (16:50–16:54, user at the
bench; decisions 22, 23, 25). All six operator checks and every per-turn witness
passed. The run's final reconnect check failed on a harness defect: its window
included the old peer's close event. That check was fixed and the retained run
was re-judged report-only at 5e555d6 (Fable APPROVE; evidence
`step-9/rollback-01/rejudge.json`). Outcome of record: functional claims
supported, reconnect included; `all_declared_checks_passed: false` is preserved
because the declared stimulus sequence was not followed exactly. That record is
for 9936da45. The new target has not had a rollback-01-style operator session; a
restore-only flash/boot verification is planned (gap 3,
`step-10/gap3/launch-restore-verify.sh`). 9936da45 stays the independent
fallback (decision 109(f)). After its own gate, the merged mute image becomes
the AEC-off production image as a frozen, manifested bundle. It is not a
rollback for the shared playout, transport and cancel changes.

The gates for both images are predeclared in
[echoear-mute-gate.md](echoear-mute-gate.md): the mute-01 functional gate
(`hil/plans/echoear-mute-gate-v1.json`) and the AEC promotion smoke
(`hil/plans/echoear-aec-promotion-smoke-v1.json`).

`CONFIG_DG_ECHOEAR_AEC=y` enables Espressif's public AFE AEC-only interface.
**The frozen82d032bd trial found an incorrect reference selector: it read MIC2
instead of the intended MIC3 loopback. The candidate now selects MIC3 from
word0 low16, with a production-read regression. Physical validation and
independent delay measurement are still pending review and a new trial.** The component registry pins
`espressif/esp-sr`2.4.7, `dl_fft`0.8.0 and `cjson`1.7.19~2; the resolver
also selects `esp-dsp`1.8.0. References:

- [Registry version](https://components.espressif.com/components/espressif/esp-sr/versions/2.4.7/readme)
- [AFE](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/audio_front_end/README.html)
- [AEC](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/acoustic_echo_cancellation/README.html)
- [Component manifest conditions](https://docs.espressif.com/projects/idf-component-manager/en/latest/reference/manifest_file.html)

The inspected2.4.7 upstream revision is
`2f8c4b0459db5bbb39abd77adae27962d6d94bcb`; registry package hash is
`809d0041cdddd98a278f0d5afef7bb60a451290577b98cf718dfffc91bdcbd9b`.
The installed `esp_afe_aec.h` is the build's API authority. No SDK or managed
component is edited. NS, AGC, VAD and WakeNet are not instantiated.

## Audio path and open hardware assumptions

The opt-in codec path selects MIC1|MIC2|MIC3, enabling ES7210 TDM. RX receives
two32-bit slots containing four16-bit samples; the draft selects MIC1 from
the upper half of the first word and its supposed MIC3 reference from the
upper half of the second in the superseded82d032bd draft. ES7210 TDM I2S
orders channels1,3,2,4, so that selection is MIC2. The repaired candidate
selects MIC3 from the lower half of the first word. That sample was not
exported in the frozen trial; its historical failure remains unchanged.
MIC1 stays30dB and MIC3 is explicitly0dB with register readback. TX keeps
16-bit PCM/DMA packing, but uses32-bit slots and32-bit WS. Both directions
share16kHz WS and64 BCLK/frame. RX remains the master and supplies the
external codec MCLK at256fs/4.096MHz on GPIO42. TX is initialized second as
the internal duplex slave, using a separate512fs/8.192MHz internal clock,
MCLK GPIO unused, and internal MCLK/BCLK ratio8. IDF6.1 requires TX>=6; the
old shared256fs setting gave4 and was invalid. Both codec formats retain
256fs; the ES7210 coefficient table is not asked for384fs/512fs. Startup
checks channel-info pairing, RX-master/TX-slave roles, both reported MCLK
and BCLK frequencies, and TX ratio>=6 before capture starts. The IDF6.1
master-only MCLK pin binding preserves GPIO42 as RX. These are driver
readbacks/configuration checks, not a physical pin-frequency measurement.
No PA/codec transition is added
to the synchronous audio path. **Slot order, sign/packing, reference identity
and duplex clock/playback operation still require measurement.**

`afe_aec_create("MR",4,AFE_TYPE_FD,AFE_MODE_LOW_COST)` runs synchronously on
the existing capture worker, before the unchanged50Hz high-pass,
125% digital gain and soft limiter. FD low-cost includes vendor residual-echo
nonlinear suppression; absence of a separate NS stage does not mean purely
linear processing. Its chunk size is queried. The adapter
accepts partial blocks up to320 samples and supplies exactly the same count
on output, with one queried chunk of initial silence and a constant adapter
delay. It does not reset on turns/reconnects, remove samples or repeat pad
frames. Short AFE output is a counted error with silence, not stale PCM.
Algorithm delay beyond the adapter is **unknown** until independently
measured with near-end excitation and a silent reference; ERLE may not pick
a delay that maximizes cancellation.

The factory has unchecked allocation uses before it returns, including its
outer handle, input-format maps and nested engine. Experimental AEC selects
`CONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS`; a compile guard rejects an enabled
AEC build without that policy. IDF calls the existing allocation-failure
counter callback, then aborts before a failed supported allocation can return
null to the vendor. This is an explicit **global** fail-stop policy for the
experimental image, including allocations outside AEC. A resource failure
aborts/reboots the board and fails the trial. It is not exception recovery:
there is no safe vendor unwind on this path; reset reclaims partial state.
The standard disabled profiles retain their existing heap policy.

A returned handle is separately checked for the outer and nested engine,
engine workspace, scratch, both channel maps and expected MR/sample-rate/mode
metadata before any processing or publication. The public destroy API tolerates
missing scratch/maps with an otherwise valid engine. This does **not** prove
general partial-engine cleanup: in FD low-cost mode, pinned `aec_destroy()`
passes a null nested DSP workspace to `esp_aec3_hps16fft_destroy()`, which
dereferences it. The current fake tolerates that state and overstates real
cleanup safety. Its rejection check proves no publication at the modeled
boundary, not safe target destruction for a deliberately null workspace.
Such an injected state requires an explicit fatal path or genuinely audited
cleanup in future source work. The approved frozen trial does not inject it;
its enforced heap-abort policy prevents exhausted valid allocations from
returning that state. Completed wrapper buffer allocations are released on
subsequent setup failure. A free-heap snapshot is neither the containment
policy nor an allocation proof.

The firmware automatic microphone gate opens in AEC mode, retaining explicit
manual mute. The bot separately requires `ECHOEAR_AEC_FULL_DUPLEX=1` to
remove `AlwaysUserMuteStrategy` and enable Flux interruption. The standard
demo remains half duplex. Both flags must be verified before scoring.

## Instrumentation and acquisition preparation

PERF adds AEC chunk/delay, allocator deltas, call count, total/max wall time,
input/output sample counts, processing errors and reference/output clips.
Wall time includes scheduling; use task runtime deltas for CPU utilization,
plus internal/PSRAM minima, largest block and capture/TX stack floors. Native
ADC saturation remains fatal; AFE output saturation is independently counted
and also enters microphone clip gates. Nothing exempts clips or transport.

`aec-record 1` starts a bounded30-second PSRAM capture of raw MIC1, the
selected reference and pre-high-pass AFE output. The current draft labels
the reference as MIC3; frozen82d032bd actually selected MIC2 per the
configured codec mode/channel map. Export labels do not prove slot identity.
`aec-record 0` freezes it. `dump-aec` exports
ordered base64 PCM with exact extent, explicit truncation and unknown total
delay. `hil/echoear_aec_capture.py` rejects incomplete/reordered/truncated
exports and writes pickle-free NPZ. Disconnect before bulk export; do not
print PCM or PERF during scored acquisition. Capture allocation occurs
before the brief copy mutex, never under it.

Host tests cover adapter chunk sizes80..1024, partial calls, exact ordering
and constant delay, library short results, wrapper teardown/failed setup,
invalid input, signed three-channel capture, explicit overflow and clip
counts. A fake AFE provides no ERLE, CPU, acoustic or STT evidence.
The returned-state fake now represents all public AFE fields used by the
wrapper and tracks every owned allocation. Missing scratch/maps/engine/
workspace and invalid channel metadata are rejected with zero live fake-owned
allocations and no published active instance. The fake's workspace-null
destructor behavior is not evidence of real vendor cleanup, as described
above. A representative six-stage
unchecked factory model exercises outer/maps/engine/workspace/scratch failures.
With `DG_IDF_HEAP_FAILURE_SOURCE` pointing to the installed IDF `heap_caps.c`,
the host target compiles its failure routine verbatim and death-tests that the
failure callback runs and abort occurs before unchecked use. Without that
source, this SDK containment test is explicitly skipped. It is not execution
of the proprietary Xtensa factory or the complete IDF allocator on a host.
Pinned allocator-entrypoint/disassembly inspection supplies that boundary;
no SDK/managed file is modified. The enabled-wrapper compile negative also
rejects `CONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS=0`.

Clock tests compile the actual production initialization function, checking
submitted geometry, RX-first order, distinct TX sampler clock, GPIO ownership
request and startup readback rejection. Both the old clock body and old
wrapper from `c65ffb7` fail the corresponding new regressions. Hardware clock,
packing/reference repair and verification remain pending. The one short
resource trial below measures startup/capture only, not session load.

## Resource budget before hardware

The first enabled demo build on9bc2d42 adds3072 bytes of initialized data and
32 bytes of BSS, with unchanged97687 bytes IRAM text. The coefficient table
`subband_filter_coef` accounts for3072 bytes in the vendor processor archive.
TDM doubles RX DMA from3840 to7680 bytes with the existing4x240-frame reserve;
TX DMA stays3840 bytes. Static plus RX growth is therefore6944 bytes before
other library allocations and synchronization. The prior worst observed
internal minimum was85604, only3684 above the unchanged81920 floor. This is
insufficient projected margin in the initial draft, **not a measured AEC heap minimum**.

Two subsequent changes recover5704 bytes in that projection:

- The peer timing object shrinks from2960 to328 bytes on ESP32-S3, saving
  2632 resident bytes when detailed timing is off. Only the always-enabled
  iteration measure stays resident; requested detailed measures are allocated
  once before the peer worker starts and remain internal, with unchanged
  counts/histograms. No timer allocates or moves to PSRAM. PERF reports
  resident and optional bytes separately. Allocation failure is fatal to
  construction, rather than silently disabling requested measurement.
- The source coefficient table moves to mapped rodata, saving3072 bytes of
  initialized internal data. This is restricted to processor archive SHA256
  `670639c3d136c7e3909aa90bcaac573600b7e98b6850be8fd5484b4cdea96dcc`.
  Complete relocation/disassembly inspection found one executable reference
  to this local symbol, at offset4 in the initialization literal pool. The
  address is loaded into the source argument of the3072-byte memcpy at
  init offset0x9f..0xa9; the destination is a separate SPIRAM working buffer.
  It does not escape and there are no stores to this source. Other references
  are debug metadata and Xtensa properties. This object has no other
  initialized data. Only its data family is remapped; code and other vendor
  objects retain default placement. No vendor archive is edited.

The placement guard rejects an archive change before linking. The post-link
check requires the one local table to have exactly3072 unchanged bytes in
ESP32-S3 mapped rodata, comparing it directly to the pinned archive member.
A misplaced table or accidental byte change fails the build. Host negatives
cover DRAM placement, extent/content errors and the archive guard. The
first naive custom-section mapping did not override the earlier generic
DRAM rule; the post-link/map check caught this before hardware, and the
IDF data-family mapping now excludes the object from that earlier rule.

Net known growth is now1240 bytes (32 static+3840 RX-2632 peer), giving only
2444 projected bytes above the existing worst-case floor before remaining
AFE allocations. This estimate uses heap payload/object sizes, not allocator
overhead or a measured AEC minimum. Runtime initialization and active-session
measurements can still fail the gate; no floor or DMA reserve changes follow
from this estimate. The subsequent heap-abort policy adds56 bytes of IRAM
text on the enabled target (97743 vs97687). Conservatively also charging
those bytes leaves2388 in the estimate, still before other AFE allocations.
This does not establish how linker/heap regions affect the measured floor.

Do not trim DMA/stack reserves, lower the floor or assume that the largest
block is a budget. Recover internal RAM, then measure real initialization,
steady capture, display, connection, reply and reconnect peaks. AEC's large
buffers request PSRAM; small vendor allocations still need a measured
census. The IDF6.1 installed I2S driver has no PSRAM DMA option. No SDK upgrade
or driver modification is part of this work.

## Single resource/slot trial on frozen 82d032bd

**Resource checks PASS; slot/reference trial FAIL.** One B-authorized bench
hold lasted 33.770 s, including identity, flash, startup, capture and export.
The bench was released immediately; B reflashes its normal image. No bot,
fixture server, Jabra acquisition, scored acoustics or additional hold ran.
AEC remains default-off. Exact tested source is
`38d0e166e7683000c9ae0ad4db0d9062047f8db8`; ELF SHA-256 is
`82d032bd0cd7725eb6e33c6b5220be895499dd79424766f980b865422946411e`,
BIN SHA-256
`5991604ea7f992740504b17dd596928bd24ae2cd806022f535267dce68f2aa6d`.
The approved frozen bundle is unchanged. Its manifest describes the original
pre-trial state; the run and analysis are separate evidence.

| Measurement | Observed in the short trial |
|---|---|
| Capture CPU, steady 3.091422 s window | 33.413% of one core |
| Capture CPU, 2.685647 s tone window | 39.356% of one core |
| Core 1 idle, same windows | 66.324% / 60.517% |
| AFE maximum inclusive call wall time | 18.881 ms per 512-sample/32 ms chunk |
| Internal 8-bit minimum | 157,568 B; 75,648 B above unchanged 81,920 B floor |
| Lowest sampled internal largest block | 114,688 B; floor 32,768 B |
| DMA minimum / PSRAM minimum | 149,780 B / 9,784,588 B |
| AFE construction free-heap deltas | 384 B internal / 131,424 B PSRAM |
| Recording allocation free-heap deltas | 112 B internal / 2,880,004 B PSRAM |
| Capture stack minimum free / allocated | 11,812 / 16,384 B |
| Playback stack minimum free / allocated | 14,956 / 17,152 B |
| App-control stack minimum free / allocated | 10,416 / 16,384 B |
| Audio-TX stack | Unmeasured: no session or audio-tx task existed |

CPU uses task runtime deltas divided by the matching wall window, not AFE
wall time. The AFE maximum includes preemption. Construction deltas exclude
static data, DMA and allocations before the wrapper's baseline; recording
deltas include any concurrent small allocations and are not a complete
allocation census. The final AFE counters have 154,880 input and 154,880 output
samples, 302 calls and zero processing/reference/output clips. Four snapshots
and the raw log show zero failed allocations, audio drops/read/write errors,
write timeouts, native clips, I2S underflows and queue overflows. Exactly one
boot and no fatal or E-level log record occurred. Heap exhaustion would have
failed the trial; none occurred. LCD was not initialized and no peer, Opus TX,
display load or reconnect ran: these figures cannot establish full-session
headroom or a production CPU budget, particularly with the wrong reference.

Driver readbacks report RX master external MCLK 4.096 MHz/BCLK 1.024 MHz,
TX slave internal MCLK 8.192 MHz/BCLK 1.024 MHz, divider 8 >= 6, GPIO42 owned by RX.
Configured WS is 16 kHz, RX stereo32 and TX data16/slot32. Codec startup verified
MIC1_GAIN=0x1a, MIC3_GAIN=0x10, REG0D=0x01, REG44=0x58 and DAC_VOLUME=0xae.
These are software/driver checks, not measured pin waveforms.

The slot defect is established by the pinned 1.6.2 driver's TDM-I2S setup
(REG12=0x02, ES_I2S_NORMAL, 16-bit ADC samples) and
[Everest's ES7210 datasheet, Revision 22.0 May 2023, Fig 2e/page 6](https://files.waveshare.com/wiki/common/ES7210-datasheet.pdf).
The datasheet SHA-256 is
`2928a1e361e8f549b189e596c766630a19a18e5a6f196702f3e153bccc20fce1`;
the saved PDF and inspected diagram show 1,3,2,4, unlike DSP/PCM's 1,2,3,4.
Expected stereo32 packing is therefore word0 high16 MIC1, word0 low16 MIC3,
word1 high16 MIC2, word1 low16 MIC4. The frozen draft fed word1 high16 to AFE.
Future repair must include a channel-map regression and an all-four-slot
hardware capture to verify the physical reference and packing.

The 5.780 s capture contains 92,480 complete triple frames across three 600 ms
10% direct tones at 997/1637/2293 Hz. Column peaks are 1978/74/1135 LSB and RMS
-40.774/-68.905/-55.836 dBFS (MIC1/selected reference/AFE output). Quiet first 3 s
MIC1/reference correlation 0.861 supports a second acoustic mic at a lower gain;
this is corroboration, not independent electrical-reference identification.
The actual MIC3 sample was not saved and cannot be recovered from the triple
export. **Electrical loopback alignment and total AFE delay are unmeasured.**
Only the adapter delay is known: 512 samples/32 ms. There is no ERLE result;
output attenuation with the wrong reference could remove wanted speech.

Evidence is `~/src/evidence/echoear/A/aec-resource-38d0e16-1/`, including raw
serial/identity/flash logs, four PERF JSON files, pickle-free `capture.npz`,
original runner `result.json`, saved datasheet/diagram and reproducible offline
`analyze.py`/`analysis.json`. The original `passed=true` field covers only
startup/resource/export guards and remains unchanged; `analysis.json` records
the overall FAIL and the reference defect. `hil/echoear_aec_resource_trial.py`
is the single-trial runner, not a slot or acoustic scorer. WiFi totals were
9,904 B; wired totals 24,292,764 B are recorded separately without gating.
This resource trial is observe-only for NIC and cannot qualify transport.

## MIC3 repair and the next reviewed trial

The candidate uses `echoear_slots()` for signed MIC1/MIC2/MIC3/MIC4 extraction
from the Fig2e packing. A host test compiles the actual `Mic::read()` and
checks the AFE microphone/reference arguments and original DMA words, with
all four channels distinct and signed. The actual38d0e16 Mic body fails it
because it supplies MIC2; the corrected body passes. This is a source-level
regression, not physical slot proof.

`aec-record-slots 1/0` selects schema2 capture: MIC1,MIC2,MIC3,MIC4,processed,
with five16-bit values per frame. MIC4 remains unselected; its raw bits are
retained for diagnosis, not treated as an enabled microphone. The maximum
30-second allocation is4,800,000 B of PSRAM instead of the legacy triple's
2,880,000 B. Allocation and old-buffer release occur outside the capture copy
mutex. Switching back to triples reuses capacity; missing raw DMA words fails
rather than fabricating channels. `dump-aec` explicitly identifies schema2,
channel order and unknown measured delay. The decoder requires consistent
schema/order/extent and retains pickle-free NPZ. Legacy triple exports remain
readable. Capture capacity/truncation is tested in both modes.

The optional `aec-probe` command emits deterministic bounded PCM while media
is inactive:200ms silence,2s aperiodic noise at <=10% PCM peak,200ms silence,
with10ms edge envelopes. It uses the existing bounded Speaker path and refuses
active media before acknowledging or writing. Incomplete writes fail. A host
test compiles the actual Pipeline method, checks rejection and exact continuous
PCM. Independent host/C++ generators agree on PCM16 SHA-256
`0efe603c5cfa0f7e7a83d390bbfad7d836d2cadb9346867658600eb5ad8731bf`.
This avoids the earlier single-frequency delay ambiguity. Playback volume is
explicitly-6 for this diagnostic; no production volume policy is changed.

After Astra review of the exact candidate, request ONE short B-coordinated
hold. `hil/echoear_aec_slot_trial.py` flashes a caller-specified verified frozen
bundle and records two all-slot captures without a peer, bot or fixture server:

1. EchoEar DAC probe with quiet margins. Compare the known aperiodic PCM to
   MIC3, and MIC3 to simultaneous raw MIC1. Report the dominant
   **reference-to-microphone correlation-peak lag**, polarity and confidence.
2. Independent calibrated Jabra speech with no EchoEar playback command.
   Prove the frozen prompt reached MIC1, and MIC3 remained quiet during it.
   Compare raw MIC1 to pre-HPF/gain AFE output to
   measure total AFE delay separately from the adapter's512 samples.

The analysis never selects delay to improve ERLE. Identical500–2500Hz causal
bandpass filters cancel their own relative delay. Predeclared availability
requires absolute normalized correlation>=0.6, a peak separated by>=0.05 from
all candidates outside+/-4 samples, and no optimum at the search boundary.
Reference-to-mic search is+/-2048 samples; near-end AFE search is0..4096.
The exact emitted mono PCM16 prompt is resampled from48/16kHz to16kHz and
matched independently to raw MIC1 with the same correlation confidence.
Source-defined20ms blocks within30dB of its loudest block define the active
window. At least200ms of active samples must arrive after the capture's first
200ms quiet prefix. Active MIC1 RMS must rise>=10dB above that prefix.
During those verified active samples, MIC3 RMS must be<=-60dBFS and no more
than6dB above its quiet prefix, with DAC-probe MIC3 at least10dB stronger.
Without verified excitation and reference rejection, the independent AFE
delay is unavailable even if ambient noise correlates with processed output.
A known DAC-source match independently corroborates the electrical reference.
The negative controls include Astra's former false-pass model (MIC1/32 used
as MIC3 with an unplayed prompt), a weak but correlated prompt, and that same
acoustic reference during a verified prompt. Failure or
ambiguity remains unavailable/failed; no automatic retake or fitted tolerance.

These common-ADC-clock correlations can measure relative alignment and an
independent AFE delay. The known PCM match's position in a recording is
**not an absolute DAC converter latency**; that needs a synchronized output
sample reference absent here. A dominant acoustic peak is not necessarily the
first physical arrival. No ERLE, STT, barge-in or acoustic qualification is
scored. Keep heap exhaustion/reset fatal, original memory/stack/clip/driver
error gates, a175-second overall bound and AEC default-off. NIC totals are
recorded as observe-only resource evidence; no network changes are made.

## Corrected MIC3 diagnostic: resources pass, alignment unavailable

The ONE Astra-approved, B-granted trial used clean commit57c8fe6 (runtime
firmware unchanged from5e3570e), experimental ELF
`52a61f231e9d81aeaae7f49630e3a1ca57a0f13b884ebaefa449dfd898a9754d`.
Enabled AEC demo, disabled AEC demo and legacy builds passed;75 audio tests,
14 capture/alignment/scorer tests and4 build-policy tests passed. A separate
old-body regression fails the original MIC2 selector. The initial unrun
`57c8fe6-dirty` bundle was excluded, rebuilt clean and never flashed.

Evidence is `A/aec-mic3-57c8fe6-1/`. The42.06-second hold fullflashed the
frozen bundle, verified MAC/full boot hash, captured64,000 DAC-stage frames
and51,200 near-end frames in explicit five-channel schema2, then released
the bench to B. No bot/fixture service or active display/session was used.
Jabra PCM8/11(-4dB), Mic6/7(+3dB) readbacks were unchanged; no mixer, geometry,
NVS or host/AP network changes. Both original raw captures remain retained.

| Observation | Measured result |
| --- | --- |
| Exact aperiodic DAC source to raw MIC3 | Correlation0.97137; secondary0.51943 |
| MIC3 DAC-stage / independent active near-end RMS | -72.109 / -94.787dBFS;22.679dB difference |
| Known Jabra prompt to raw MIC1 | Correlation0.67074; active MIC1-39.456dBFS,18.399dB above quiet |
| MIC3 active near-end versus quiet prefix | -0.208dB; raw peak2, versus41 during DAC probe |
| Independent raw MIC1 to AFE delay | **1,024 samples /64ms**, correlation0.999824; secondary0.371232 |
| Raw MIC3 to MIC1 alignment | **Unavailable**; diagnostic best19 samples, correlation0.52560, secondary0.52188 |
| Absolute DAC converter delay / ERLE | Unknown / unscored |

The DAC match plus rejection of verified independent near-end excitation
corroborates the corrected MIC3 electrical-reference selection. This does
not establish adequate reference level or cancellation performance. The
reference-to-microphone correlation fails both predeclared0.6 magnitude and
0.05 separation criteria (observed separation0.00372). Its19-sample diagnostic
peak is **not** an accepted alignment or physical first arrival. Overall
`result.json` remains **passed=false**, with no threshold change or retake.
The64ms independent AFE measurement includes the512-sample adapter plus
the vendor processing delay; it was not selected to improve ERLE.

Internal heap historical minimum was157,568 B (floor81,920); sampled snapshot
largest blocks were114,688 B (floor32,768), failed allocations0. PSRAM minimum
was7,864,240 B after the4.8MB recording allocation, reused for the second
capture. Capture CPU was33.44% during DAC playback and32.95% during independent
speech; another intervening window was33.99%, with a short allocation/start
window at54.84%. These percentages use task runtime deltas relative to wall
time and one core. Maximum observed vendor AFE call time was19,329us per
512 samples. Stack floors: capture11,796/16,384 B, playback14,956/17,152 B,
app-control10,544/16,384 B. No audio-TX task was present. Wrapper accounting
is384 internal/131,424 PSRAM bytes and excludes vendor allocations; do not
interpret it as total AFE cost. Audio drops/read/write errors/timeouts,
AEC errors/reference/output clips and all captured raw ADC saturations were0.
One boot, raw audit and cleanup passed. WiFi17,985 B and wired5,386,730 B were
recorded under the observe-only NIC policy.

These resources cover the held no-peer diagnostic only. Active-session,
Opus, initialized display and reconnect worst cases remain unmeasured on
this experimental image. AEC stays default off; the trial does not qualify
transport or acoustics. B independently restores its frozen production image.

Independent posttrial Astra audit (`A/aec-mic3-prep/trial-astra-review.md`)
redecoded both serial dumps exactly to the NPZ arrays, reproduced the
unchanged analysis and confirmed all five resource guards. It also retained
a provenance failure:369 of370 frozen input hashes match, but the shared
external approval document changed after binding. Its later editor is not
established; exact source/image/prompt inputs are unchanged. Original binding
and failed postcheck remain intact, alongside a separately preserved copy of
the posttrial review document. The exact-image approval was also received
directly before acquisition. Future bindings must copy a mutable shared
review first. This metadata mismatch grants no qualification or retry.

## Distributed path diagnosis and validation

Read-only examination of the failed52a61f23 capture found active probe rises
of22.05dB at MIC1 and25.62dB at MIC3. Cropping to the2s active source changes
the original peak only from0.52560 to0.52588; each1s half still fails the old
correlation test. Median coherence is0.9955–0.9977 over500–2500Hz, while
opposite-polarity lobes at9 and19 samples nearly tie. The Jabra did not emit
the DAC-stage probe. These observations support a dispersive linear path,
not insufficient excitation, quiet-window dilution or the independent Jabra
path as the explanation for this failure. They do not uniquely identify the
coloring component or exclude all nonlinear distortion.

`hil/echoear_aec_path.py` implements a **different, explicitly named diagnostic**,
`raw-path-response-v2`. It does not lower the scalar correlation thresholds
or reinterpret the old trial as passed. It estimates a distributed raw MIC3
to MIC1 FIR response, separately from the AFE output. Fixed lags are-128..1023
samples; stationary normal equations use ridge0.001 times input mean-square.
Fit each independent1s half of the exact2s source and predict the other half,
excluding fixed2048-sample convolution margins. Each direction must explain
>=90% of held-out variance. Excitation must have RMS>=2LSB, Welch spectral
flatness>=0.1 over300–6000Hz and no bin containing>5% of that band's power.
Both response-energy5/50/95% lag descriptors must agree within4 samples and
lie16 samples inside the estimation window. Negative lags remain visible.

Exploratory application to the retained capture explains99.274%/99.158%
held-out variance and yields energy descriptors4/18/44 samples in both
directions. This motivated the new method; it is **not independent validation
on new hardware data**. The original result and scalar-alignment failure remain
unchanged. Descriptors are not first arrival, a scalar physical delay, AFE
delay, or an ERLE-optimizing shift. Even a successful path diagnostic
leaves `scalar_physical_delay_samples=None`; it only characterizes a distributed
response. Existing independently verified near-end excitation/reference
rejection and AFE-delay requirements still apply.

Host controls cover colored multipath, both delay signs, weak reference,
periodic excitation, independent noise, out-of-window delay, changing path,
incomplete input, and the acoustic-reference alias. No probe amplitude change
is justified by the measured excitation. The trial explicitly selects
`--analysis-method raw-path-response-v2` on the same frozen52a61f23 image,
retains all raw slots and the legacy analysis in a separate field, and keeps
the original resource/clip/reset guards and175s bound. Default remains
`correlation-v1`. Astra approved the committed method before execution and
B granted ONE later hold during its second RF deferral. The preparation alone
did not authorize acquisition. Future holds require their own coordination.

Astra approved this committed method for ONE later bounded diagnostic,
conditional on a new B grant. `A/aec-alignment-diagnosis/prepared-trial/plan.json`
freezes308 inputs, including a **copied** approval, host snapshot fromddf4311,
the unchanged52a61f23 image and exact prompt. The immutable plan retains its
initial prepared-only state; a separate grant receipt and result record the
actual later execution. Host import/help succeeded before hardware access.
Fits characterize a
regularized effective raw-channel response, not a uniquely identified physical
impulse response. The two directions reuse the two halves as train/test swaps;
they are not independent acquisitions. The interior check covers central
5/95% response energy, not all possible tails. Original scalar FAIL remains.

The one later trial, `A/aec-path-v2-52a61f23-1/`, completed in42.23 seconds
on the exact old52a61f23 image, with archived reviewed host code. The bench
was released at08:48UTC, before B's09:14 deadline. No WiFi-observer firmware
was flashed, and there were no mixer, geometry, NVS or network changes.

| New-data observation | Result |
| --- | --- |
| Held-out explained variance, first fit predicts second half |99.1526% |
| Reverse train/test swap |98.9107% |
| Effective response-energy5/50/95% lag descriptors |4/18/44 samples in both fits (0.25/1.125/2.75ms) |
| Independent raw MIC1 to AFE delay |1,024 samples /64ms; correlation0.999777 |
| Independent Jabra source match to MIC1 |Correlation0.604511; narrowly above the unchanged0.6 rule |
| Legacy scalar alignment |Unavailable; peak0.53192, secondary0.52717 |
| Scalar physical delay / first arrival / ERLE |Unknown / unknown / unscored |

The **distributed path diagnostic passes**, with all unchanged v2 excitation,
prediction and repeatability checks. This new result does not resolve a
unique scalar physical lag or qualify AEC. Both legacy scalar results remain
failed/unavailable. Verified independent MIC1 rise was19.083dB and the MIC3
DAC-versus-near-end difference22.570dB, corroborating reference identity again.

All308 frozen input hashes still match and the original failed result hash
is unchanged. Both schema2 serial dumps match their NPZ arrays exactly:
64,320 DAC-stage frames and51,520 near-end frames. Raw four-slot plus processed
saturation counts are all0. Resource/raw/one-boot/cleanup guards pass; internal
minimum157,568 B, snapshot largest114,688 B, PSRAM minimum7,864,240 B.
Capture CPU is33.53%/33.03% in DAC/near-end windows,34.11% in the intervening
window and52.93% in the short273ms allocation window. Max inclusive AFE call
is19,088us; stack floors remain capture11,796, playback14,956, app-control10,544,
console5,996 B. Audio/AFE errors, clips, drops and write timeouts are0. These
are no-peer/uninitialized-display resources only. WiFi126,360 B and wired
24,990,185 B were observed under the resource diagnostic's nonqualifying NIC
policy. Detailed evidence is in `measurement-summary.json` and the retained
raw snapshots; no automatic retry was performed.

Independent Astra audit at
`A/aec-alignment-diagnosis/prepared-trial/astra-path-trial-review.md`
redecoded the raw dumps, reproduced both1152-tap responses and the complete
analysis, and verified all308 post-run hashes and five raw PERF snapshots.
It approves the distributed-path/resource conclusion only, retaining
qualification=false and the historical FAIL. The narrow Jabra source-match
margin is retained; no tolerance was fitted and no repeat was requested.

## Pending qualification

The repaired candidate's trial corroborated reference identity and measured
independent AFE delay, and the later diagnostic characterized its distributed
raw-channel response. Neither establishes unique scalar physical alignment.
Any further acquisition needs a separately reviewed method and coordinated
hold, preserving this failed result. Measure active session/display/reconnect resource
costs on that corrected image. Preserve the original memory, transport,
clip and uplink thresholds for qualification; report an uplink failure
separately rather than discarding the acoustic measurements.
The [first scored protocol](echoear-aec-scored.md) declares the remaining gates:

1. At least10 scripted bot replies, both full-duplex flags active, matched
   gain, no NS/AGC, zero false user turns, median reply ERLE>=20dB, zero clips
   and no transport/uplink regression.
2. Five original Jabra barge-ins: acoustic bot stop<=1s in every trial and
   correct normalized transcripts in at least4/5, retaining the same gates.

The scorers require aligned complete captures and independent measured delay;
they do not infer cancellation from a muted or half-duplex output. Acquisition
and acoustic timing automation are implemented and Astra reviewed. Uplink is
reported separately from the acoustic gates; fatal resource, reset and clipping
failures still stop the campaign. All evidence belongs under `A/`, not git.
Coordinate every hold with B through the progress report.

## First scored acquisition: prerequisites failed, campaign unconsumed

The 2026-10-15 first attempt on approved host source `3da4f03` stopped before
scoring because the recorder rejected its own ALSA SIGTERM shutdown status1.
The correct Jabra identity, format and absence of a stale capture owner were
checked. A software-only null-PCM probe reproduced the same owned shutdown.
Host correction `6485e8b` accepts that exact documented diagnostic only after
our termination of a live child, with no earlier error or ALSA discontinuity;
unexplained nonzero exit, EOF and xrun remain failures. Astra approved the
correction and the new 422-input binding `5799a9ac`.

The corrected acquisition used the unchanged experimental ELF `52a61f23`.
It completed quiet, electrical-reference and independent-delay measurements,
then stopped at the first DAC-silent **Stop** calibration. Both predeclared
source-identity checks failed; neither threshold was changed.

| Measurement | Observed result |
|---|---|
| Quiet raw MIC1 / Jabra | -57.9097 / -61.1231 dBFS; both below -55 |
| Quiet electrical MIC3 | -94.7482 dBFS; independent activity threshold2 LSB RMS |
| DAC probe to MIC3 identity | Correlation0.971756 |
| Independent near-end source to MIC1 | Correlation0.603370, above the fixed0.6 minimum |
| Independent MIC1 to AFE delay | 1,024 samples /64ms; correlation0.998772 |
| Stop source to raw MIC1 | Correlation0.446905 <0.6; peak gap0.042530 <0.05: unavailable |
| ADC/AFE saturation across four raw slots and processed output | All0 in the prerequisite captures |
| Scored originals / full live peer | 0/15 / not started |
| ERLE / false user turns / barge-in / full-peer heap / transport / uplink | Unmeasured |

The failed Stop candidate's active-window level was -39.2148 dBFS, but its
source identity was unavailable. That number is diagnostic only and cannot
qualify speech calibration. The cause of the weak waveform match remains to
be established. The short word and marker measurement must be demonstrably
usable before consuming the scored population; this failure does not measure
AEC cancellation, speech recognition or RF performance.

Evidence: `A/aec-scored-prep/prepared-6485e8b-complete/acquisition/` retains the
original failure, raw serial, schema2 dumps, NPZs, Jabra audio and receipts.
All422 bound inputs and mixer readbacks matched after execution. The WiFi NIC
gate passed with113,880 bytes; this is not an uplink continuity measurement.
The global scored-consumption marker remains absent. No failed acoustic
original was excluded or repeated, and the earlier ALSA failure is retained.

Under the same bench lock, the wrapper restored the frozen normal firmware
and verified the full boot hash
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`.
Restoration identity/flash/boot logs and `restore-result.json` are retained
beside the acquisition. Strict startup and cleanup passed. The bench was
released at10:25UTC, with no reader or bot/fixture server left running.

Independent Astra raw audit at `acquisition/astra-prerequisite-review.md`
redecoded every dump, reproduced the measured delay, identity failures and
zero saturation, checked all422 hashes and verified normal-firmware restore.
There is **no scored AEC qualification**. AEC remains default off.

### Offline short-word identity diagnosis

The retained playback file is not grossly corrupted: after conversion back
to16kHz, its correlation with the intended half-amplitude frozen Stop source
is0.999985. Matching that actual playback waveform against raw MIC1 still
fails, at0.446919 with secondary peak0.404395. An ideal delayed copy of the
source passes the existing matcher at1.0 with secondary0.764313. Thus the
stored resampling difference does not explain the failed physical match;
these controls do not prove what the loudspeaker actually emitted.

A129-tap response fitted only to the independent long Jabra prompt also
failed validation. With alternating160ms training/held-out blocks and10ms
guards, the two held-out correlations were0.540/0.235 and explained variances
0.268/-0.373. Restricting both inputs to the matcher's existing500–2500Hz
band did not validate the model either: correlations0.497/0.485, explained
variances0.129/0.046. The broadband models' predicted Stop matches remained
unavailable. Neither response can be used to replace the scoring source.

Selected local waveform peaks change sign and offset across time and
frequency. Periodic speech and retrospective peak selection prevent treating
those peaks as independent identity passes or measured clock drift. Fixed
acoustic coloration, time-varying processing, noise and timing faults remain
unresolved alternatives. The measured board-DAC-to-MIC1 response follows a
different physical path and cannot substitute for a Jabra-to-MIC1 response.

Scripts, input hashes and diagnostic JSON are retained under
`A/calibration-identity-diagnosis/`. All analyses are explicitly nonqualifying;
the original prerequisite failure and fixed correlation/uniqueness thresholds
remain unchanged. Any future path measurement or scoring-method correction
requires a predeclared procedure and review before another coordinated hold.

## AEC handoff: calibration identity unresolved; work stopped

The final bounded investigation did not resolve the short-word identity
failure. New AEC work is stopped at the coordinator's timebox. AEC remains
**default off**, including in the frozen WiFi-observer image. No production
firmware, scoring threshold or failed result was changed to obtain a pass.
There are **0/15 scored originals**; the campaign-consumption marker is absent.

### What works and what was measured

- The opt-in experimental image builds and captures schema2 raw MIC1–MIC4
  plus pre-HPF/gain/limiter AFE output. The corrected ES7210 mapping selects
  MIC3 from word0 low16. DAC-probe-to-MIC3 correlation was 0.971756, with the
  electrical reference quiet during independent near-end playback.
- Independent raw-MIC1-to-AFE delay measured 1,024 samples / 64 ms, correlation
  0.998772. NS and AGC were disabled. This delay does not establish a unique
  scalar physical loudspeaker-to-microphone path delay.
- Quiet eligibility passed, and all four raw slots and AFE output had zero
  saturation in the retained prerequisites. The ALSA owned-SIGTERM correction
  worked; unrelated nonzero exits, early EOF and xruns still fail.
- The long near-end prompt narrowly passed source identity, correlation
  0.603370 against 0.6. The Stop calibration failed both waveform gates:
  correlation 0.446905 and peak separation 0.042530 against 0.6 / 0.05.
  Its candidate level of -39.2148 dBFS is **not a qualified calibration**.

ERLE, ten-reply false-turn behavior, five barge-ins, STT accuracy with AEC,
full-live-peer heap/stack and session transport/uplink remain unmeasured.
Earlier no-peer resource trials do not satisfy the live internal-heap floor
of 81,920 B or the other session gates. The physical marker was not reached by
this acquisition and its usability is also unproved.

### What the additional offline checks establish

The actual stored playback remains consistent with the intended source, and
neither the 129-tap nor a longer 1,025-tap response validated on the independent
speech record. The latter used fixed 500–2500 Hz filtering, +/-512-sample
support, alternating 160 ms blocks, 40 ms boundary guards and the same fixed
ridge fraction 1e-4. Held-out correlations were 0.371 / 0.355, explained variances
-0.126 / -0.349; its predicted Stop matches also failed. This rejects those
particular speech-excited models, not every possible stationary path.

A bounded retrospective constant-rate sensitivity search selected +100 ppm
on the long prompt only. Applying that single ratio to untouched Stop raised
correlation only to 0.464827, still failing. This is not a measured clock-rate
error or permission to warp scored data. Raw MIC2 likewise failed Stop source
identity at 0.494394; no alternate scoring channel was adopted. Selected
speech-lobe offsets cannot distinguish timing faults, acoustic coloration,
time-varying processing and noise. Further tuning on this one failed word
would not establish the missing independent physical identity.

The host also uses different capture states: prerequisite word playback
runs after the quiet Jabra recorder closes, whereas scored barge playback
runs with `StreamCapture` open. That is a static harness observation, **not
evidence that Jabra capture changes its loudspeaker output**. Preserve this
condition explicitly in the next diagnostic rather than assuming equivalence.

### Next experiment, if work is explicitly resumed

Prepare and review one nonqualifying path diagnostic before seeking a short
bench hold. Keep the frozen experimental image, gains, mixer and geometry;
board DAC stays silent and no live peer is needed. Freeze one playback file
with quiet margins, an aperiodic broadband probe, unchanged Stop and marker,
then an identical repetition. Retain the exact emitted 48 kHz file and 16 kHz
reference. Use a continuous schema2 raw capture within its 30 s bound, with
complete quiet, saturation, continuity and ALSA evidence.

Freeze the Jabra capture state to match the intended barge-in measurement.
If comparing recorder-closed and recorder-open states, declare separate,
otherwise identical repeated blocks and retain their order and transition
timestamps; do not silently change that variable by adding a monitor. An
independent Jabra recording needs full receipts and error logs and does not
automatically constitute a clean electrical output reference.

Before acquisition, declare exact onset windows, filter band, FIR support,
regularization, train/test intervals and acceptance criteria. Estimate one
response from the first broadband occurrence, then predict the untouched
second occurrence and words with unchanged coefficients, lag, gain, polarity
and rate. Retain incorrect-word/reversed/noise controls and rejected results.
Fit only known source to raw MIC1; MIC3 checks DAC silence. Never fit against
AFE output, minimize residual ERLE or realign individual scored windows.

Repeatability plus successful held-out prediction would support a fixed-path
explanation. Disagreement exceeding measured noise would establish missing
repeatability without identifying its cause. Stable repetitions with poor
prediction could still reflect insufficient model memory or reproducible
nonlinearity. Any replacement identity method needs separate review and a
new frozen campaign binding; the present failed prerequisite stays retained.
This experiment is a proposal, not an approved trial or automatic retry.

### Resume map and bench state

All paths below are under `~/src/evidence/echoear/` unless repo-relative:

- Frozen AEC image: `A/aec-mic3-57c8fe6-clean-experimental-image/`, full ELF
  `52a61f231e9d81aeaae7f49630e3a1ca57a0f13b884ebaefa449dfd898a9754d`.
- Retained approved binding and failed acquisition:
  `A/aec-scored-prep/prepared-6485e8b-complete/`; binding prefix 5799a9ac.
  `acquisition/` contains raw serial/dumps, NPZs, playback, result and
  `astra-prerequisite-review.md`; the run never opened a live peer.
- Offline scripts, input hashes and JSON:
  `A/calibration-identity-diagnosis/`. `diagnostic-review.md` records the
  independent audit and limits of the earlier fits/local-peak analysis.
  `timebox-review.md` independently reproduces the constant-rate, other-slot
  and longer-response diagnostics and confirms the host capture-state
  difference, without approving a causal claim, new scorer or hardware trial.
- Harness/protocol: `hil/echoear_aec_scored.py`, `echoear_aec_scoring.py`,
  `echoear_aec_alignment.py`, `echoear_aec_capture.py`, `alsa_stream_capture.py`
  and `hil/plans/echoear-aec-scored-v1.json`; scoring rules are in
  [echoear-aec-scored.md](echoear-aec-scored.md). Current source is not a
  replacement for the previously frozen 422-input binding.

A last restored and verified normal ELF
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`
at 10:25 UTC; `restore-result.json` and `restore-9936/` retain the proof.
Later board use belongs to B. A has no bench lock, serial reader, bot or
asset server running and no new AEC acquisition queued. Resume requires an
explicit coordinator request; preserve default-off and every qualification
gate, including reporting RF failures separately from acoustic measurements.


### Step-8c review corrections

Consumed render intervals always submit silence when the media lease is cancelled
or the tick expires, including any suffix after a partial cancelled write. The
published pre-write ledger must be `submitted - completed == 640 - completed % 320`;
the acceptance analyzer checks every retained post-Stop boundary, including the
silent Stop control. Zero I2S counters alone do not establish this cushion.

The acceptance runner's optional `reconnects` list declares `id`, `text`,
`disconnect_offset_s: 1.0`, and `second_reply_window_seconds` (6–18, including
full quiet post-roll). The timer requires a non-silent transport write within
120 ms. The capture thread requests 50 ms anchors continuously, including
control/connection waits. Explicit PERF boundaries stay outside capture;
disconnect's automatic final PERF retains the old epoch. Both epochs must have
zero loss/concealment/I2S deltas and drained admission boundaries. A new pc_id and
generation with `prefills == 1` at its first snapshot establishes the fresh
prefill. Every MIC3 frame intersecting the disconnect bracket must be active,
and that bracket must be at most 100 ms. Missing or delayed anchors fail the
witness; host tests do not establish live timing. Peer-rx minimum is reported
per epoch. This reconnect block is host-tested only; no additional standalone
hold is authorized, and the full step-9 specification remains pending.

### Step-8b TTS lead-in experiment (incomplete hold)

`server/tts_leading_silence.py` provides an **opt-in experimental** pre-transport
trim (`DEMO_TTS_TRIM_LEADING=1`); the product default remains **off**. It retains
40 ms before the first 20 ms frame above 1024 LSB² mean-square, then preserves
all remaining PCM, including interior pauses. Context changes and interruptions
reset its buffered prefix. Voice, speed, expressivity, user-turn policy and the
firmware guard are unchanged. Evaluation declarations can assign the arm per
reply; raw TTS is recorded before the filter, successful writes after transport.

The declared ABBA comparison stopped at cell 17 when quiet post-roll raw MIC1
rose to -53.70 dBFS, failing the unchanged v2 -55 dBFS absolute limit. Evidence is
`~/src/evidence/echoear/aec-demo/step-8b/ab-1/`. There are eight clean replies per
arm, one retained contaminated A cell and three unacquired cells. No retakes
were performed, and the required ten clean replies per arm were not reached.
No arm was selected; retaining the baseline is not an acceptance result.

Partial clean warm request-to-first-non-silent-write medians were A 259 ms
(n=7) and B 201 ms (n=8). The 58 ms difference is below the declared 500 ms
selection criterion. The strict first-active-MIC3 guard check passed 4/8 A and
0/8 B replies; all source prefixes matched and all 16 clean playbacks were
bracketed. Five B replies removed no samples, so these failures do not establish
that trimming caused a guard regression. Accepted false turns were zero. The
short comparison sentence did not reproduce the earlier long-reply 1.2–1.6 s
padding: server-reported leading-silence medians were 386/379 ms for A/B. Server
TTFA and the fixed low-level PCM threshold measure different onset definitions;
neither is a physical DAC timestamp.

The hold restored and boot-hash-verified normal ELF `9936da45...`, restored the
saved mixer, stopped its bot on 7861 and released the flock. Further bench work
requires a new declaration; step 9 has not started. Full details and remaining
work are in `codex-jobs/impl-step-8b.md` in the coordinator project.

### Step 9 draft campaign runner

The coordinator's reduced acceptance contract is
`hil/plans/echoear-aec-accept-v1.json`. It remains **draft**: no campaign has run.
`hil/echoear_aec_campaign.py` refuses acquisition before opening the bench unless
review approval, explicit campaign authorization, the reviewed JSON hash, paired
code/bundle/fixture hashes and the continuous-source preflight are frozen in a
new declaration. `hil/echoear_aec_campaign_analysis.py` emits the complete verdict.

The gated demo volume is **−6 dB**, using the unchanged calibrated Jabra fixtures
and mixer (PCM75%, Mic80%). The 0 dB block is reported exposure, five Stops and
five sentences. Provisioning the demo at 0 dB requires a separately declared
10+10 qualification block there; the user's real-talker measurement only affects
that extension. Candidate reference gain is15 dB, with a clean generated config
and boot-line verification; this does not deploy a new product default.

Each volume has ten echo replies, including its cold first reply and two shared
quick-gap pairs. At−6dB, require9/10 eligible Stops and9/10 eligible sentences,
with accepted-interruption upper bounds1.0s and2.0s respectively. Two in-window
probes, four near-only controls, two silent-cancel controls, three anchored
reconnects and a600s display-active soak complete the campaign. Barge cells use
the real bot's next reply, without a scripted follow-up delay.

Frozen v2 quiet gates allow at most two measured-noise reacquisitions per cell;
three consecutive distinct noisy cells pause and restore. Two ordered spare IDs
per kind per volume may replace only independent unscorable/no-opportunity/
undelivered classifications. Performance misses remain in the denominator.
Echo integrated reduction stays≥20dB; first500ms≥10dB applies only when that source
window exceeds−30dBFS. Gate post-guard **AFE** residual≤−50dBFS and accepted false
turns0; report proposals and quieter onsets. Sustained double-talk, post-Opus
mapping and−12dB are deferred. Report actual sentence overlap, prefix/cut-step
and every context's lead/request/first-write measurements; lead≥0.8s fails for
**every** generation, including real-bot replies.

The canonical direct probe requires inactive media and therefore runs after the
live block disconnect, at unchanged gain/volume before restore. Soak bookkeeping
uses ten echo requests at fixed60s spacing and bounded captures/exports: snapshots
every10s outside export, immediately pre/post export, export timeout25s, maximum
resource-observation gap30s. These console scheduling details are explicit for
review; heap/stack/CPU floors do not change. A missed deadline fails; it is never
silently shifted. Noise reacquisitions follow the full600s original interval.
Every anchored capture is capped at28s and stopped before worker joins/exports.

Host candidate build (after exporting ESP-IDF6.1):

```sh
server/.venv/bin/python hil/build_echoear_aec_candidate.py /home/khkramer/src/evidence/echoear/aec-demo/step-9/<new-candidate-bundle>
```

This builds and hashes an isolated AEC/autostart/ref15 image and does not flash.
Actual bench use remains prohibited until the coordinator authorizes the reviewed
frozen declaration. Every future hold owns flock through normal9936da45 restore
and boot-hash verification, owned-bot shutdown and saved Jabra mixer restoration.

## Explicit EchoEar demo build profile (step 9(d) preparation)

Step 9(d) preparation (merge-prep) packaged the candidate's four settings as
`sdkconfig.defaults.echoear-demo`. At the merge (decision 109(b)) that overlay was
replaced by `sdkconfig.defaults.echoear-aec`, the single AEC overlay. It holds the
same four resolved settings, with the demo autostart coming from
`sdkconfig.defaults.demo`, and also pins guard 400 ms and re-arm 2000 ms. Those
two are the Kconfig defaults at 58f567e, so pinning them changes no resolved
value; it protects the qualified configuration against later default drift. The
only build procedure is the one in [Two firmware images](#two-firmware-images).
Kconfig defaults remain off, and `DG_DEMO_AUTOSTART=1 scripts/idf.sh` still
selects only the autostart profile, not AEC.

Keep local credentials and resolved configs private. An equivalence check needs
the same local settings, dependency lock, IDF/toolchain and relative build path
as the candidate (`build/accept`); another network produces a different
artifact. Verify the resolved keys against the candidate manifest, then run the
[V2 flash-set comparison](compare-masked-image.md) on both `flasher_args.json`
files with their app ELFs. Require zero unmasked bytes, equal offsets/options
and an identical partition table. This comparison proves metadata-only
equivalence; it is not a qualification run, and it does not by itself authorize
flashing, shipping or merging to master.
