# P4 step 12b coordinator procedure

> **STEP 14 RESTORE GUARD:** Live commands now require `hil/p4_step14.py run`
> and the clean-commit restore record/hash plus its successful boot proof from
> [the step-14 procedure](p4_liveness.md). Do not invoke frozen `p4_product.py`
> directly: its historical factory/keep-tested semantics do not enforce R7.
> The live commands below have been migrated; export `RESTORE_RECORD` and
> `RESTORE_RECORD_SHA256` from the newly approved record before copying them.

**Step 12b default round closed (2026-11-01).** User decision
`decision-2026-11-01-p4-mic-revert-30db` restores MIC1 **30 dB / ES7210
0x43=0x1a**, matching the qualified P4 baseline. The 24 dB candidate passed
standard-prompt STT with WER 0 and zero clips, but its retained dedicated
linearity captures cannot establish a measurable board doubling step; its
v2 clean band remains unavailable. All 24 dB evidence and the fixes 7–7f
tools are retained. Encoder stack stays **26624 B**, digital gain is the
bundle default and playback seed stays **−289**; derived seed **−257** remains
unconfirmed. See [the default-round record](p4_defaults.md). Step 15b measures
AEC-on clipping/ERLE at 30 dB before any gain reconsideration. Historical
24 dB commands/results below do not authorize reopening this default round.


Writer work uses no hardware. All numbers below are **rules set before the run**,
not measured P4 product results. The acoustic fixture is
[`fixtures/p4xc/audio-plan.json`](fixtures/p4xc/audio-plan.json). Keep its SHA256,
source diff/commit, bundle manifest hashes, actual target lock hash, bot identity,
mixer readbacks, placement description, raw WAVs, serial, PERF and analysis under
`~/src/evidence/echoear/p4/step-12b/`. Do not put acquired evidence in git.

P4 must be placed **next to the Jabra by the user**. Record that placement and
keep it fixed throughout a sweep/pair. No S3 geometry, microphone sensitivity,
SPL conversion or speaker level band is assigned to P4. Jabra identity/mixer
readbacks use the existing `alsa` tools. Quiet-room/room-baseline eligibility
applies before acoustic work; an invalid window is retained and labelled.

## Entry points and restore

Run builds separately, through `scripts/idf.sh` / `scripts/build_locked.sh`.
Release every build lock before using the following coordinator commands.
`hil/bench.py` owns the one bench lock; do not wrap it in flock. No C6 writes.
`p4_audio.py` inherits `board_smoke.LiveIO`'s verified flash/boot and protected
restore hold, opens through `board_serial`, and checks board eFuse identity.
The P4 stays on a successfully acquired test image. Failure/abort invokes the
existing verified factory write + separate verify + reset/banner recipe (R7).
S3 restores `55152f25` with boot proof after each run. An **offline** acoustic,
wire or cost analysis failure does not restore/reflash anything.

```bash
cd /home/khkramer/src/dg-esp32-p4
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
P4_AUDIO_EVIDENCE="$HOME/src/evidence/echoear/p4/step-12b/fix-1"
P4_AUDIO_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
```

The writer creates `bundle-p4xc` and four `bundle-s3-{normal,demo,aec-normal,aec-demo}`
bundles there. `make_bundle.py` is frozen and its `dependencies_lock_sha256`
always names the root S3 lock. Read each bundle's supplemental
`board-provenance.json` for the actual target lock SHA256; do not interpret the
frozen P4 field as P4 lock provenance. Dirty-source hashes are part of the
manifest because the writer was told not to commit.

## Every P4 bench build: regenerate and declare its audio settings

A cached generated config can retain step-12a's 1600% mic gain. Before **every**
P4 bench build (normal, later AEC, or another build directory), regenerate that
build's generated SDKCONFIG. Overlay equality alone cannot establish the
Kconfig-owned seed/stack defaults. The bundler additionally re-resolves current
board/audio/peer Kconfig with the build's defaults chain and rejects a seed or
TX-stack mismatch before creating a bundle. Never remove/read `sdkconfig.local`. For the
normal image use this explicit fresh-config recipe, under the build lock:

```bash
scripts/build_locked.sh bash <<'P4_BUILD'
set -euo pipefail
export TMPDIR="$HOME/.cache/gap3-tmp"
rm -f build-p4xc/sdkconfig
DG_BOARD=p4xc scripts/idf.sh build
cmake -S components/dg_board/test_host -B build/host-board
cmake --build build/host-board --target p4_audio_profile -j 4
uv run --project hil --frozen --offline python scripts/p4_bundle.py \
  --build build-p4xc \
  --out "$HOME/src/evidence/echoear/p4/step-12b/fix-1/bundle-p4xc"
P4_BUILD
```

Use a new bundle destination on each rebuild. The adapter wraps frozen
`make_bundle.py`, rejects any differing/missing P4 overlay key and exports
**all public P4 overlay values** into `manifest.json.p4_overlay_values`.
`p4_overlay_keydiff` records the names-only proof. `p4_audio` declares the actual
resolved digital gain, seed and TX stack, plus analog gain and output volume
from a host executable linked to the actual P4 AudioHw table. No private
config values are exported. A stale profile executable is rejected.
`p4_kconfig_defaults` records the freshly resolved public seed/stack values.
The focused resolver uses the real main/audio/peer Kconfig sources and the
build's defaults chain, without requiring another target's managed graph. The P4
lock sidecar binds the augmented manifest and ELF.

Both `live()` and the `voice-smoke` adapter read `mic-gain-status` and the
`stats` OUTPUT_PATH playback level immediately after verified boot, before
any playback or gain command. They require the bundle's declared analog,
digital and relative-volume values, including the MIC1 register readback.
A missing declaration or mismatch refuses acquisition; it is never silently
corrected to the expected gain. Acoustic teardown restores those observed
boot gains even when capture/analysis throws.

## Owed step-11 console check (one bench command)

On a **normal AEC-off** S3 bundle (step-11 or this step's shared-code image):

```bash
"$P4_AUDIO_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio.py console-check --board echoear \
  --bundle "$P4_AUDIO_EVIDENCE/bundle-s3-normal" \
  --out "$P4_AUDIO_EVIDENCE/coordinator-console"
```

This checks `cache-counters 0` (OK with counters enabled, or explicit
`unsupported build=cache-counters-disabled` with counters compiled out),
`mic-analog-gain 30` (OK **and** MIC1 `0x1a` readback), and
`aec-ref-gain-status` (ERR **and** `unsupported build=aec-disabled`). A final
ping must succeed. The bundle must verify as AEC-off; any other rejection
reason fails. An enabled-counter run additionally covers the successful cache
branch. This command does not claim to be the S3 behavioural smoke.

## Tone sweep and native microphone levels

```bash
"$P4_AUDIO_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_audio.py acoustic --board p4xc \
  --bundle "$P4_AUDIO_EVIDENCE/bundle-p4xc" \
  --out "$P4_AUDIO_EVIDENCE/coordinator-acoustic"
```

The adapter reuses frozen `speaker_dual_sweep_test.capture/correct`,
`bench_calibration.record_prompt`, `alsa` and `dump_verify`, with P4 serial and
P4 fixtures. Every raw MIC dump must pass its CHECK, count and framing check.
An ACK alone is insufficient. The volume command is −6 dB relative output;
the codec also requests −6 dB. Direct tones bypass PlaybackDsp but traverse
the production Speaker volume/PA path. P4's PA-gain table value 0 follows the
spike's 5V/3.3V vendor calculation, so DAC_VOLUME is `0xba`; S3 remains `0xae`.
This is a **+6 dB register offset** on P4 relative to S3 for the same
“−6 dB” command: P4 is −2.5 dB (0xba) and S3 −8.5 dB (0xae). PA gain 0
is a vendor default, not a measured P4 PA gain. Neither is an acoustic level
measurement.

Procedure and predeclared selection:

1. Import all acoustic helpers, then record Jabra room floor/mixer **before
   entering the flash/restore hold**. Missing imports or a noisy room leave the
   installed firmware untouched. `live()` first runs `preflight_tools()`. Calibrate the response at 2% at the harmonics
   of 500/800/1000/1500/3000 Hz below 8 kHz, independently for Jabra and native
   MIC1. Verify the first two 1→2→4% doublings are 6.0206 ± 1 dB on both chains.
   Retain the 4→8% check as well. If the low-level response is not linear, the
   clean amplitude set is empty; change placement only in a new declared run.
2. Sweep 2/3/4/6/8/10/15/20/26/30%, two original repeats at every frequency.
   Each point needs zero PCM clips, corrected THD ≤5%, H2 and available H3
   ≤−26 dBc on **both** chains, using the frozen harmonic analysis. Harmonics
   above native Nyquist are unavailable; 3 kHz is a partial-harmonic result,
   never a full-band THD claim. The clean set is the contiguous prefix whose
   every frequency and repeat passes; do not promote isolated higher passes.
   Repeats after a failed original are diagnostic and cannot overwrite it.
3. Report the measured −6 dB level band separately per frequency and chain as
   min/max fundamental dBFS across that clean set. Also report the exact two
   original 1 kHz / 26% values and the raw/corrected THD/peaks. Empty clean set
   means no usable P4 band/default can be selected. These dBFS values are
   measured receiver levels, not SPL and not S3-equivalent geometry.
4. At MIC1 analog 24/30/33 dB, digital 100%, acquire an independent floor and
   France, Italy and the standard speech prompt at fixed host scale 0.8.
   Retain native PCM, active RMS, floor, peak, clips and alignment. MIC2/3 stay
   untouched by the analog-gain command. Restore the verified boot analog/digital values afterward, even on failure;
   every prompt row records its absolute native `peak` for the gain rule.
   `record_prompt`'s P4 `estimated_spl` field is **not used**: that frozen
   helper's sensitivity belongs to another microphone; use PCM dBFS only.

## Default derivation (freeze this rule before acquisition)

P4 defaults remain unmeasured EchoEar placeholders. Each has one operative
source; the bundle records the resolved values for that image:

| Value | Single authoritative source for a follow-up calibration edit |
|---|---|
| Digital mic gain | `boards/p4xc/sdkconfig.defaults`, `CONFIG_DG_MIC_GAIN_PERCENT` |
| Playback seed | `components/pipecat_esp_peer/Kconfig`, P4 default of `DG_PLAYBACK_SEED_DBFS_X10` |
| Encoder stack, AEC off | same peer Kconfig, P4 default of `DG_AUDIO_TX_STACK_BYTES` |
| MIC1 analog / startup output volume | `components/dg_board/src/p4xc/audio_hw.cpp` |

The six formerly duplicated locations now have distinct roles: (1)
`dg_audio/Kconfig` retains the S3/generic mic defaults and no P4 mic override;
seed ownership moved to peer Kconfig; (2) the P4 overlay owns **only mic**;
(3) `test_check_sdkconfig.py` checks allowed keys/ranges without copying P4
values; (4) `test_kconfig_boards.py` checks source ownership, the measured P4
stack result and AEC/frame40 precedence; (5) `audio-plan.json` names the
sources rather than repeating defaults; (6) `docs/p4xc.md` lists those sources.
Change each selected value in its one authoritative location, regenerate the
config, and rebuild metadata/tests/bundles. Independent register golden facts
still need reviewed updates if analog hardware programming deliberately changes.
Do **not** add seed or stack to the overlay: that would override step-15's AEC
stack and the frame40 minimum. No P4 AEC calibration/guard/ref gain is selected
in 12b. MIC3's nominal 12→15 dB step was only about 1.1 dB in Facts B; reconfirm
MIC3 w0-low with product mask 7 before step-15 reference use.

* **Mic analog:** reject candidates with ADC clips or non-positive prompt SNR
  on any prompt. Prefer the lowest analog gain; promote to a higher gain only
  when the worst-prompt SNR improves by at least 3 dB. Retain all candidates.
* **Digital gain:** for that analog candidate, define the allowable integer
  percent set 1..1600 using the worst original native peak: require
  `peak * percent / 100 <= 0.9 * 32767`. Start with the bundle's declared digital gain (clamped to that set),
  then, if live STT fails, test the fixed sequence 100, 150, 200, 250 intersected
  with that set, in order, each on a new, labelled capture. Stop at the first
  candidate passing all standard-prompt STT/clip gates; no scored retakes.
  Zero ADC/gain clips is required on normal prompts. Validate actual received
  uplink recordings, with the unchanged bot STT profile and standard expected
  text, using frozen `speech_accuracy`/`echoear_hot_stt.word_errors`; minimum
  accuracy 0.90 and no WER regression outside the recorded S3 band's worst WER
  on the same original prompt/profile. The coordinator must name that S3
  reference evidence before scoring. Missing/mismatched reference means
  unverified, not a new arbitrary P4 success threshold.
* **Playback seed:** retain actual live bot TTS source PCM and Jabra playback
  with the source/profile identity. Replay that PCM through the existing host
  PlaybackDsp path using the product HPF, compression ratio/threshold and
  activity mask, and compute the active effective source RMS after HPF and
  compression. For three independent original long replies, take the median
  RMS in dBFS and round `10*dBFS` to the nearest integer (half away from zero),
  constrained to Kconfig's −600..−100 range. This is the candidate seed;
  confirm cold-start first-word level and steady-state Jabra active RMS stay
  within the newly measured P4 clean band with zero clips. If they do not,
  retain the measurements and defer seed selection; do not compensate by
  importing an S3 acoustic band. Target/ceiling/HPF remain the product policy
  unless a separately reviewed measured defect calls for a change.
* **Peer stack:** take the minimum observed live `tx_stack` over three normal
  P4 sessions with at least500 encoded frames and 100 speech decodes each.
  Each session starts from a fresh boot (`voice-smoke` flashes and boots per
  invocation); three reconnects in one boot do not give independent minima.
  Candidate bytes = `ceil((bundle_tx_stack_bytes - min_free + 4096)/1024)*1024`, clamped to
  Kconfig's 16384..49152 range. Any overflow or insufficient free stack calls
  for a larger candidate and a fresh complete run. Confirm ≥4096 B free for
  encoder, decoder and owner on all reruns. Dispatcher (peer-rx) uses the
  coordinator decision plus addendum: P4 free ≥ max(1744 B, 25% of allocation),
  rounded up; S3 peer-rx is report-only. The 1744 B reference comes from this
  step's uncommitted fix-1 S3 normal bundle, not image of record 55152f25.
  Report every task minimum. Census marks cover task lifetime across same-boot reconnects.
  No changes to 20 ms,
  mono16k Opus, bitrate/complexity, receive timeout or transport behaviour.
* **Heap/resources:** internal minimum ≥81920 B, largest block ≥32768 B,
  zero failed allocations; retain PSRAM, stack and per-core CPU evidence at
  idle, active uplink, long downlink and session end. These are P4 gates (R8).

Final selection is a **follow-up code/fix round after hardware measurement**:
update `AudioHw`/P4 defaults/P4 peer branch as applicable, add evidence paths
and the actual numbers to `docs/p4xc.md`, then rebuild and repeat the same
checks. Do not relabel placeholders or standalone spike timings as live data.

## Voice smoke, actual live codec cost and full product wire pair

**Superseded entrypoints:** `p4_audio.py voice-smoke` and `p4_audio.py costs`
are historical fix-3 commands. Use the corrected `p4_audio_fix4.py` entrypoint
for voice-smoke, pair and costs, as shown below and in
[p4_audio_fix4.md](p4_audio_fix4.md). Acoustic/reanalysis commands are unchanged.

The P4 voice adapter requires a completed acoustic acquisition for the **same
ELF**, a clean original restore record, and a **non-empty measured clean band**.
The original `--acoustic-run` path is unchanged. For the saved run's post-hoc
P4 decision, use `--acoustic-reanalysis FILE` instead; the flags are mutually
exclusive. The reanalysis gate verifies the adopted decision id, 500 Hz
exclusion, 10 dB SNR threshold, source JSON hashes, original restore and ELF,
then recomputes the band. A changed input or altered result is refused before
live IO. The accepted decision id and reanalysis hash are retained in the
voice result. This does not qualify the image or replace the voice/product pair.

Keep the +6 dB register offset above in view when interpreting the P4 receiver
level band; do not compare it with S3 acoustic levels.

### Adopted P4 clean-band decision (2026-11-01, post-hoc)

`decision-2026-11-01-p4-clean-band` adopts the corrected proposal's option A
for **P4 only**. S3 procedures and limits are unchanged:

1. Exclude 500 Hz from the clean band; report its distortion versus level as
   a P4 speaker characteristic (see `docs/p4xc.md`).
2. For each chain independently, `fundamental_dbfs - noise_dbfs < 10` means
   **unmeasurable**, neither pass nor fail. Exactly 10 dB is measurable. Every
   measurable chain must meet the original corrected THD≤5%, H2/available
   H3≤−26 dBc and zero-clip limits. A frequency/repeat fails if any measurable
   chain fails; it is unmeasurable if both chains are unmeasurable.
3. Start at the lowest amplitude where all retained frequency/repeat points
   pass or are unmeasurable **and** every frequency has at least one measurable
   pass across its chains/repeats. Wholly unmeasurable points cannot establish
   the start. Continue over adjacent swept amplitudes until the first
   measurable failure; never restart after the band ends. The existing
   low-level linearity check remains in force.

Apply the decision offline before the voice run, without changing any original
acquisition file. This command writes a new file and refuses an existing path:

```bash
uv run --project server --frozen --offline --group dev python hil/p4_audio.py reanalyze \
  "$P4_AUDIO_EVIDENCE/coordinator-acoustic" --exclude-hz 500 --min-chain-snr-db 10 \
  --out "$P4_AUDIO_EVIDENCE/reanalysis-decision-2026-11-01.json"
```

On the saved run the band is **6–30% at 800/1000/1500/3000 Hz**. The JSON records
`post_hoc=true`, the decision id, thresholds, exclusions, each chain/point's
classification, source hashes and the measured level band. Per-chain dBFS
bounds include measurable passes only; unmeasurable readings do not establish
those bounds. `qualification=false` remains explicit.

If the clean prefix stops below 20%, stop the canonical voice smoke. Record a
coordinator decision and first try voice at a lower relative playback-volume
using an explicitly unscored manual session (audio off, set the lower volume,
connect, then audio on; record actual readbacks and received/Jabra audio).
Frozen `board_smoke` prepares −6 dB and its resource gate requires −6 dB: it
cannot certify a lower-volume staging trial, and no undocumented override is
provided. Resume the canonical procedure only after the coordinator's measured
follow-up establishes a suitable clean band for its −6 dB product profile.

### Measurable-harmonic THD decision (2026-11-01, post-hoc, offline only)

`decision-2026-11-01-p4-thd-measurable-harmonics` adds an explicit offline
entrypoint, `p4_audio_measurable.py reanalyze`. The frozen sweep and the earlier
`p4_audio.py reanalyze` retain their original behavior and output format.

For each chain and calibration frequency, the new module reads the native
mono 16 kHz PCM16 calibration WAV in its saved scoring window. It uses the
frozen analyzer's mean removal, Hann window, one-sided power normalization,
peak search within ±20 Hz and tone integration within ±20 Hz. It verifies
the recomputed tone against the saved calibration measurement. The local
noise floor is the **sum** of FFT power in the two sidebands **40 < |bin−tone|
≤ 200 Hz**, nominally 320 Hz total. The 40 Hz guard excludes the tone/leakage
skirt. This fixed local bandwidth measures the chain background near the
harmonic in the same capture, including room/interference noise, without
assuming a flat broadband noise spectrum or borrowing another chain's floor.
It is conservative integrated local noise, **not** a per-bin noise density or
a 40 Hz equivalent-band estimate. The same constants apply at all frequencies.
Resampled, missing or inconsistent calibration evidence is refused.

A harmonic enters corrected THD only when its chain's calibration tone minus
that local noise is **≥10 dB**. Each harmonic retains raw/corrected dBc,
calibration SNR, its label and whether it was summed. Below-Nyquist H2/H3
with unmeasurable calibration cannot establish a chain pass: the chain is
unmeasurable unless another measurable distortion/clip violation fails it.
Above-Nyquist harmonics retain their distinct unavailable label. The original
point fundamental SNR, 500 Hz exclusion, contiguous-band/coverage rule, THD
≤5%, H2/H3 ≤−26 dBc, zero clips and low-level linearity limits remain in force.
Every calibration WAV hash/window and both source JSON hashes are recorded.

```bash
P4_F7_COORD="$HOME/src/evidence/echoear/p4/step-12b/fix-7/coordinator"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_measurable.py reanalyze "$P4_F7_COORD/acoustic-retake" \
  --out "$P4_F7_COORD/clean-band-retake-measurable.json"
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_measurable.py reanalyze "$P4_F7_COORD/acoustic" --report-only \
  --out "$P4_F7_COORD/clean-band-attempt3-measurable.json"
```

Both results are `post_hoc=true`, `qualification=false`, under the new decision
id; attempt 3 is explicitly report-only. These files have a separate kind and
are **not accepted by the existing live voice eligibility gate**. A successful
CLI exit means the offline analysis completed, not that a clean band exists.
On these retained runs the H5 artifact is removed, but the unchanged linearity
gate still fails: retake board 2→4% = **7.0549543 dB**; attempt-3 Jabra 1→2% =
**7.0655458 dB**, versus **6.0206 ±1 dB**. Both therefore have an **empty clean
band** and null dBFS bounds. The JSON reports the offending linearity steps;
this harmonic decision does not waive them or authorize another acquisition.

### Measurable linearity endpoints and voice gate (fix 7f)

`decision-2026-11-01-p4-linearity-measurable` extends the 10 dB SNR rule to
the endpoints of the original dedicated 1000 Hz linearity captures at
1/2/4/8%. Each original doubling step records both endpoints' fundamental,
noise, SNR and source identity. A step is `evaluated` only when both endpoints
reach 10 dB; its original 6.0206 ±1 dB tolerance then applies. Otherwise the
step is `unmeasurable`, with `passed=null`. All three original steps are
reported. Each chain must have at least one evaluated step, and every
evaluated step must pass. Zero evaluated steps means **unavailable**, never
passing linearity. Missing/non-finite levels or noise are refused. The
analyzer does not substitute sweep pairs for the dedicated captures.

The v2 output retains the harmonic, clean-band and linearity decision ids.
The 7e noise method/40–200 Hz constants and 10 dB threshold are unchanged.
Following the 7e review, an unmeasurable fundamental calibration is refused,
missing calibration frequencies raise explicit contract errors, and dropped
H4/H5 also report `floor_response_bound_dbc = raw + fundamental_response -
calibration_noise`. The corresponding `floor_response_thd_bound_percent` is
informational; it does not change the adopted measurable-only THD rule.

```bash
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_measurable.py reanalyze "$P4_F7_COORD/acoustic-retake" \
  --out "$P4_F7_COORD/clean-band-retake-measurable-v2.json"
```

The retained retake still has **no clean band**. Board endpoint SNRs at
1/2/4/8% are **−5.359/0.812/7.505/13.102 dB**. Thus every board step has
an unmeasurable endpoint, and board linearity is unavailable. In particular,
2→4% = 7.0549543 dB is now labelled unmeasurable, not failed. Jabra's 2→4%
and 4→8% steps are evaluated and pass. No evaluated step fails; the missing
evaluated board step is the sole linearity blocker. The result records
`linearity_available=false`, `linearity_valid=false`,
`linearity_unavailable_chains=["board"]`, an empty amplitude band and null
dBFS bounds. Stop here; this output cannot establish voice eligibility.

For a non-empty band, the new `p4_audio_measurable_voice.py` is an additive
outer adapter with the same arguments as `p4_step14.py`. It invokes that
unchanged runner and its restore/storage/product checks. Its scoped gate
dispatch accepts the new output kind only after checking the ELF, all three
decision ids, clean original acquisition/restore, both source JSON hashes,
every calibration WAV hash, and equality with a full offline recomputation.
It rejects `report_only=true`, empty bands and unavailable/failing linearity.
Old kinds delegate to the unchanged old gate; the original callable is
restored even on errors. No frozen source file is edited.

For future eligible evidence, use the existing bench launch environment and
replace the outer runner in the voice command with
`hil/p4_audio_measurable_voice.py run --restore-record "$RESTORE_RECORD"
--restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_audio_fix4.py
voice-smoke ... --acoustic-reanalysis FILE`. All other voice arguments and
the `hil/bench.py` wrapper remain required. This is an adapter interface,
not a claim that the current empty-band v2 file is eligible or that hardware
has run. The old standalone entrypoints keep their original behavior.

Use **one already-running unchanged `server/demo_bot.py` process on 7861** for
both captures; record its source/policy hashes, process token and actual STT
profile in `BOT_IDENTITY`. Do not start/restart or edit that bot between the
pair. Set these paths to that process's existing files:

```bash
: "${FIX5_OUT:?new evidence directory for this acquisition pair}"
: "${BOT_DIR:?existing bot directory with bot.pid and uncoloured bot.log}"
: "${BOT_IDENTITY:?existing bot identity JSON with profile_manifest}"
for P4_AUDIO_BOARD in echoear p4xc; do
  if [ "$P4_AUDIO_BOARD" = echoear ]; then
    P4_AUDIO_BUNDLE="$P4_AUDIO_EVIDENCE/bundle-s3-normal"
    P4_AUDIO_SWEEP_ARGS=()
  else
    P4_AUDIO_BUNDLE="$P4_AUDIO_EVIDENCE/bundle-p4xc"
    P4_AUDIO_SWEEP_ARGS=(--acoustic-reanalysis "$P4_AUDIO_EVIDENCE/reanalysis-decision-2026-11-01.json")
  fi
  "$P4_AUDIO_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
    hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_audio_fix4.py voice-smoke --board "$P4_AUDIO_BOARD" --bundle "$P4_AUDIO_BUNDLE" \
    --out "$FIX5_OUT/coordinator-voice-$P4_AUDIO_BOARD" \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" \
    --bot-repo /home/khkramer/src/dg-esp32 --pair-id step12b-product \
    --headless --calibration "${P4_AUDIO_SWEEP_ARGS[@]}"
done
# Offline, outside bench.py; frozen calibration/recheck ignores --bounds.
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_fix4.py pair \
  "$FIX5_OUT/coordinator-voice-echoear" \
  "$FIX5_OUT/coordinator-voice-p4xc" \
  --bounds hil/fixtures/wire_contract_bounds.json \
  --out "$FIX5_OUT/product-pair.json"
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_fix4.py costs \
  "$FIX5_OUT/coordinator-voice-p4xc" \
  --out "$FIX5_OUT/live-opus-cost.json"
```

`voice-smoke` delegates to frozen `board_smoke.execute` with a boot-checking
LiveIO subclass. Normal bundle metadata selects **manual** mode (`connect`); AEC off ignores
Italy talk-over at +3 s and completes the reply, and ten long replies must have
zero self-interruptions. `--headless` skips UI/assets/touch checks; S3 display
runtime initialization is retained, P4 initialization is verified, and both
legs must pass the full product wire profile before acquisition succeeds.
`--calibration` keeps bot-content findings report-only. Review
all original turn/resource/restore findings separately: acquisition success
and the offline wire result are not a qualification claim.

`pair` reparses the raw closed-session serial plus `DEMO_CAPABILITY` bot lines
as **product**, requiring `start`, `client_ready`, SDP, `opus_enc`, `dc_open`,
pong, display-ready and UPLINK_INTERVALS. It invokes unchanged
`wire_contract.compare` with `bounds_for(...,'normal')`: stall 68411 µs,
p99 pair difference 7000 µs, coverage 0.95, ≥500 intervals, median 20 ± 2 ms,
including exact max and trailing open gap. Missing/asymmetric evidence or
changed bot identity fails. No bounds are inferred from this P4 run.

`costs` reports live encoder and decoder p50/p95/max wall µs, speech/quiet
counts and decode percentiles, histogram resolution, stack, heap and CPU.
Both `pair` and `costs` reject any recorded error, cleanup failure, interruption
or missing/failed clean restore and retain the accepted restore records.
`costs` requires real speech decodes (not just null-audio silence) and the P4
resource floors. PERF has p50/p95 and exact maxima; do not invent p99 or
convert quantiles into exact cycle counts. Standalone `codec-bench` and the
step-1b standalone 3.9 ms encode are not substitutes. Repeat three independent
P4 sessions with separate boots for independent task high-water marks before
default derivation. The task floors are encoder/decoder/owner ≥4096 B,
P4 peer-rx ≥max(1744 B, 25% of allocation), rounded up; S3 peer-rx is
report-only per the addendum. All task minima are reported.
Display contention is added in 13b;
12b reports null-display load explicitly.

Shared audio/Kconfig changes also owe **four S3 behavioural smokes**, using
`bundle-s3-{normal,demo,aec-normal,aec-demo}` and the existing `board_smoke.py`
procedure in each metadata-selected mode, with display checks on S3 (omit
`--headless`). Preserve the fixed per-variant bounds and the established
S3 restore after every run. These are coordinator work, not writer evidence.

## Other offline exclusion analyses

Omitting `--min-chain-snr-db` preserves fix round 2's original contiguous-prefix
calculation and labels the output as an alternative (`decision_applied=false`).
Other declared thresholds/exclusions can be analyzed, but only the adopted
P4 combination (exclude 500 Hz, minimum chain SNR 10 dB) is eligible through the
new decision gate. Unknown frequencies or excluding every frequency fail.

All output paths must be new and outside the read-only acquisition directory;
neither originals nor earlier analyses are overwritten. No firmware default
is changed by reanalysis. For network-only launch commands see
[p4_headless.md](p4_headless.md).

## P4 storage preparation

P4 live commands must use [p4_step14.py run](p4_step14.py), which applies the pinned restore policy and the p4_product storage adapter
shown above; frozen helpers called directly do not perform this step. See
[p4_product.md](p4_product.md) for the named-partition checks, erase/readback
evidence and coordinator decision. S3 behavior and offline analysis are unchanged.
