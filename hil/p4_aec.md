# P4 AEC step 15b coordinator run book

**Writer scope is host/build evidence only. No P4 AEC hardware run has taken
place in step 15a.** Step 15b needs its reviewed brief, coordinator bench go
and the user's quiet-room/placement approval. The P4 stays next to the Jabra.
Do not infer measured P4 delay, ref gain, guard, re-arm or AEC performance
from a successful link, S3 calibration, or the standalone step-1b spike.

The closed step-12b default round retains MIC1 **30 dB / 0x1a**, digital
gain **125%**, playback seed **−289**, encoder stack **26624 B** and output
volume **−6 dB**. The 24 dB and derived −257 candidates remain historical
evidence; neither is adopted here. At 30 dB the echo is about 6 dB hotter
than at 24 dB, so the 15b clipping/ERLE measurements decide whether gain
needs reconsideration. Do not tune gains or stacks silently during a trial.

## Static delivery and calibration status

Evidence root: `/home/khkramer/src/evidence/echoear/p4/step-15a`.
Final delivery is `final-bundles/bundle-p4xc-aec-{normal,demo}` under that
root. Exact ELF/manifest SHA256s are in each manifest and in the step-15a
report. Bundles include the target dependency-lock identity and public audio
metadata, with `p4_aec_calibration.status="UNMEASURED"` and `measured=false`.

| Setting in both built variants | Value | Status |
|---|---:|---|
| MIC3 reference analog gain | 0 dB | inherited placeholder; measure 15b |
| Source reply-start guard | 400 ms | inherited placeholder; measure 15b |
| Guard re-arm silence | 2000 ms | inherited placeholder; measure 15b |
| Implementation AFE delay | 1024 samples | inherited timing assumption; independently measure 15b |
| AFE algorithm | MR, filter length 4, FD LOW COST | unchanged implementation; NS/AGC off |
| Pre-main retention headroom | 116128 B | static ELF gate; +1200 B above unchanged floor |

The processor is ESP-SR 2.4.7 `esp32p4_less_v3_idf6`, SHA256
`16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100`.
The unique 3072-byte local source table is mapped to `.flash.rodata` under
`decision-2026-11-02-p4-aec-coefficient-placement`. Its only runtime use is
the flash-resident initializer's read-only copy into a SPIRAM working buffer;
it has no runtime stores, escaping source pointer, ISR or cache-disabled use.
The target-specific image gate verifies its exact original bytes and runtime
relocation contract. See `table-audit.md` and the disassembly/ELF evidence.
The S3 mapping and coefficient bytes are unchanged. No vendor archive or
working-buffer placement is changed, and the retention floor is not lowered.
The 0x48xxxxxx window depends on this board's `SPIRAM_XIP_FROM_PSRAM=y`:
image text/rodata are copied to PSRAM at boot and accessed through cache.
Non-XIP layouts deliberately fail this check. The image gate also rejects
an initialized second copy of the source bytes in internal SRAM.

S3 AEC normal/demo inputs for 15b are the clean-`a2d7765` rebuilds under
`step-15a-fix-1/bundle-echoear-aec-{normal,demo}`. Both compare against the
2b2b04ee pins with zero unmasked bytes; the older 15a snapshots are historical.

## Environment and restore policy

Step 16 uses the six new pins and the plan-bound commands in
[Step 16 parity run](p4_parity.md). Its AEC-demo smoke requires ERLE evidence
from that exact new ELF, with the closing metadata adapter. Prepare source in
`dg-esp32-p4-s16` only; no tests/builds until bench-free go and no live run until
the coordinator's pin verification passes. Historical commands below retain
their original profile and evidence scope.

For the step-15 closing pin round and step 16, the new bundles and restore
selection in [p4_pin_round.md](p4_pin_round.md) supersede the historical paths
below. That run book includes the required boot proof and four normal-image
product smokes. Use `p4_aec_close.py` for the new measured-diagnostic metadata;
the frozen readers below retain their historical label contract.

Run from `/home/khkramer/src/dg-esp32-p4`. Release the build lock before any
live command. The unchanged bench wrapper owns the single bench lock; never
wrap it in another flock or run a bot while holding a build lock. Use one
already-running, unchanged `server/demo_bot.py` process for both boards and
both matching variants, with its PID/log and source/policy/profile identity.
Do not start, restart or modify the bot between paired captures. See the
existing [bot environment](board_smoke.md#coordinator-owned-bot-and-identity).

The accepted normal AEC-off restore record remains:
`/home/khkramer/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json`,
SHA256 `730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a`.
Its successful `boot-proof/restore-boot-proof.json` binds the same hash and
ELF `71896a2abd85053cabc7a4f6e051623c38632e7fb93381764149183a102986e6`.
Never repin an AEC calibration image as a convenience. The step-14 runner
keeps signal-safe restoration, named-storage erase/full readback and the
existing SIGUSR1/factory-selection recovery hold. Follow
[p4_liveness.md](p4_liveness.md) on any failed restore; retain the bench hold.

```bash
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
export AEC15="$HOME/src/evidence/echoear/p4/step-15a"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
: "${STEP15B_OUT:?fresh coordinator evidence parent for the approved 15b run}"
: "${BOT_DIR:?existing unchanged demo bot directory with bot.pid and bot.log}"
: "${BOT_IDENTITY:?verified identity JSON for that process}"
```

## Fixed measurement rules

`p4_aec_policy.py` fixes these rules before acquisition. `plan` binds them,
all stimuli, the five frozen S3 helpers, new measurement helpers, the bundle
manifest and ELF by SHA256. A changed source, stimulus, bundle or rule needs
a new plan and campaign; it cannot silently continue an old sweep.

- Independent delay: DAC probe corroborates raw MIC3 electrical reference;
  the separate known near-end source plays with the DAC/reference silent.
  The frozen S3 method correlates raw MIC1 against AFE in 0..4096 samples.
  Absolute correlation ≥0.6, separation ≥0.05 outside ±4 samples, and an
  interior peak are required. Known source/excitation and silent reference
  must pass the S3 criteria. A distributed reference-to-mic correlation is
  diagnostic; it does not veto the independent AFE delay. This deliberately
  differs from the full S3 `analyze.passed`, which also requires the physical
  path correlation. Unavailable delay
  stops calibration. Never choose delay using scored echo/ERLE.
- Gains: exactly **0,3,6,9,12,15,18,21,24,27,30,33 dB**, in order. Each point
  has ten original long replies and France, Italy and speech standard prompts
  during bot playback. Each point has four bounded parts: replies 1–3, 4–6,
  7–9, then reply 10 plus the three prompts. Each part boots afresh, captures
  its own quiet MIC3 floor, and restores normal afterwards. Gain uses existing
  `aec-ref-gain` while Idle/audio 0, with actual `aec-ref-gain-status` register
  readback; no per-point rebuild is needed.
- ERLE: unchanged `echoear_aec_scoring.reply_erle`, independent delay, 320-sample
  frames, independent quiet MIC3 RMS ×√10 with minimum 2 LSB, per-reply
  active-frame medians, then median of ten replies. Residual uses those same
  active frames and independent delay. Entire original raw slots, dump CHECK,
  block clocks and anchors remain available; no output-derived activity.
- Eligibility: zero native ADC/gain clips, raw PCM saturation, AEC errors and
  reference/output clips; every standard prompt accuracy ≥0.90; zero false
  echo user turns. Select the eligible highest median ERLE; any eligible gain
  within **1 dB of the global best** ties, and the lowest gain wins. No eligible
  point stops the process. Bot-content/persona findings (including off-script
  counting replies) are report-only for sweep eligibility and guard acceptance.
  The ≥20 dB campaign criterion is reported separately;
  it is not an extra per-gain eligibility rule. S3 `nova-live-02` reply medians
  14.3/24.8/30.5 dB are report-only context, never transferred P4 calibration.
- Guards: ordered `(guard_ms,rearm_ms)` candidates are
  `(400,2000),(600,2000),(800,2000),(1000,2000),`
  `(400,3000),(600,3000),(800,3000),(1000,3000),`
  `(400,4000),(600,4000),(800,4000),(1000,4000)`.
  Start at index 0; advance only after every earlier candidate fails. Stop at
  the first pass. Acceptance requires ten complete long replies with zero
  self-interruptions, detected near-end speech after re-arm, and in-range values.
  Actual `AEC_GUARD` onset/deadline/settings/re-arm events are retained. The
  attempted inside-guard injection, its measured source onset and clock
  bracketing are **report-only**: a late or uncertain injection cannot reject
  the 400/2000 candidate or force a larger guard. Guard-probe clip and transcript
  accuracy diagnostics are also retained; the after-rearm speech must be
  detected. Use the known source's first active 20 ms block
  within 30 dB of its maximum, not its leading silence. Dense console clock
  anchors conservatively bracket the physical ADC sample; uncertain timing
  is unavailable, never guessed from a playback request. Guard 0..1000 ms and
  re-arm 500..10000 ms are the bot ranges; all declared candidates fit.

Every live command below uses `bench.py`, pinned R7 restore and candidate
storage preparation/readback. The existing 3 s `alsa.record_room_baseline`
quiet-room gate (−50 dBFS after its 0.2 s lead-in) runs before flash. A noisy
preflight returns **2**, is unscored and untouched, and may be retried later.
Other host-side preflight failures (Jabra, bot identity, room capture, tools)
return **3**, record `preflight-error`, `device_untouched=true`,
`score_produced=false`, and are retryable too. All host checks, including
Jabra resolution/mixer setup, precede `hold.touch()`/flash. Exit **1** means an
acquisition failure or unavailable/failed selection; inspect its result.

Measurement output names must be exactly `attempt-NN` (two decimal digits,
for example `attempt-01`, then `attempt-02`, in increasing order). Retain every
attempt. A part is consumed as soon as its completed cases are scored, including a
missed near-end playback or later cleanup/restore failure. An acquisition
error or missing/incomplete result with no published score is unscored; a
fresh attempt of that same gain/part/candidate is allowed after R7 recovery.
Before retry, the runner records `unscored-attempt.json` alongside retained
failed evidence. Body and cleanup failures are both recorded, with the body
error primary. Before session finish/cleanup/restore, completed scores are
saved in `post-body-payload.json` and an atomic `result.json` checkpoint with
`score_produced=true`, `retryable=false`. Later finalization errors preserve
those scores and consume the original; they cannot become an unscored retry.
The frozen selector accepts only clean acquired results; the pre-declared
fix-4 offline exception below uses consumed scores after finalization failure.
A completed successful
result also gets `scored-original.json`; either score artifact prevents a
retake if `result.json` is lost. R7 retains its existing lock/recovery hold
until a verified restore, and signal protection is released only afterwards. Selection uses each part's single clean scored original
and hashes its earlier unscored history too. Acquired failures of eligibility
or guard acceptance are scored originals and cannot be retaken for a better
score. Raw exports and score payloads use exclusive creation; the owned
progress/final result journal is updated atomically. Product smokes/contention also
record untouched preflight errors with exit 3; retry in a fresh run directory.

## Coordinator commands: independent delay and gain sweep

These are coordinator commands, not writer validation. The coordinator owns
quiet-room go, placement and the already-running unchanged bot. All live
parts release only after the accepted
normal restore; failed restore keeps the existing recovery hold/bench lock.

```bash
aecpy() {
  uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
aeclive() {
  python3 hil/bench.py uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
measurement() {
  aeclive hil/p4_aec_measure.py "$@" --plan "$STEP15B_OUT/plan.json" \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
    --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
}
mkdir -p "$STEP15B_OUT"
aecpy hil/p4_aec_measure.py plan \
  --bundle "$AEC15/final-bundles/bundle-p4xc-aec-normal" \
  --out "$STEP15B_OUT/plan.json"
measurement delay --out "$STEP15B_OUT/delay/attempt-01"
export AEC_DELAY="$STEP15B_OUT/delay/attempt-01/result.json"
```

Plan is offline (<1 s); independent delay is about **3–6 min**, including
flash, two PCM dumps and restore. Exit 1/unavailable means stop, never use the
1024-sample placeholder. The AFE runs on every `mic.read` and slot capture
is session-independent, so this diagnostic uses `audio 0` with no peer; the
S3 bench used connect/audio 1. This is an explicit acquisition difference.
If preflight returned 2 or 3, retry later in `attempt-02`; an unscored
acquisition failure can also resume after recovery. Set `AEC_DELAY` to the
single successfully acquired original, never to an assumed value.

The explicit coordinator decision `decision-2026-11-02-p4-aec-delay-1024`
authorizes a separately labeled 1024-sample input for the unavailable P4 delay
original at `step-15b/coordinator/delay/attempt-01`. This does **not** turn that
measurement into a pass. The approved decision and source attempt are pinned
by path and SHA256, with policy v2 and the same ELF. Create the explicit record
in a fresh file (offline; no plan file or hardware access):

```bash
aecpy hil/p4_aec_measure.py delay-decision \
  --decision "$HOME/src/dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-02-p4-aec-delay-1024.md" \
  --source-attempt "$HOME/src/evidence/echoear/p4/step-15b/coordinator/delay/attempt-01/result.json" \
  --out "$STEP15B_OUT/delay-decision.json"
export AEC_DELAY="$STEP15B_OUT/delay-decision.json"
```

Only commands accepting `--delay` use this exception. Each gain part and
selection records `delay_provenance.kind="decision-backed"` and
`measured_pass=false`; the source original stays unavailable. A new measured
delay still has to pass the original independent method. The coordinator
must generate a fresh plan after source review; the writer does not create
plans during these fix rounds.

At later offline re-scoring, pass a different validated delay record through
`select-gain --delay ...` (and a fresh selection output path). Original gain
parts, their acquisition delay records and cached scores are authenticated
and retained. ERLE is re-derived from raw captures with the newly supplied
delay; `acquisition_delays`, `delay_provenance` and `delay_rescored` disclose
both inputs. Do not overwrite the acquisition delay record or retake captures
to change alignment. If reference diagnosis shows MIC3 is unusable, calibration
stops for the user's decision under the recorded ruling.

The following loop stops on the first unsuccessful part. Budget **4–10 min
per part**, **16–40 min per gain**, approximately **3–8 h for all 48 parts**
including flash/dump/restore costs. Do not squeeze all measurements into one
hold. These are planning estimates, not a runtime/timing acceptance bound.
Each raw reply capture is bounded at 26 s; an overlong/incomplete reply is
retained as unavailable, never trimmed or extended. If bot playback stops
before the near-end injection, record `missed-bot-playback` with no near-end
playback and score the sweep prompt or after-rearm case as failed. It is a
consumed scored original, including when a false turn caused the early stop.
The existing 60 s drain may still time out as an acquisition error; an
incomplete overlong reply makes ERLE unavailable. These remaining P3 limits
are unchanged and can reflect long off-script bot content.

```bash
# First run: start 0/0, attempt 01. To retry an unscored failure, set these to the
# stopped gain/part and a new attempt number. Earlier originals stay intact.
AEC_GAIN_START=0
AEC_PART_START=0
AEC_ATTEMPT=01
for AEC_GAIN in 0 3 6 9 12 15 18 21 24 27 30 33; do
  for AEC_PART in 0 1 2 3; do
    if (( AEC_GAIN < AEC_GAIN_START || (AEC_GAIN == AEC_GAIN_START && AEC_PART < AEC_PART_START) )); then continue; fi
    printf -v AEC_GAIN_DIR 'gain-%02d' "$AEC_GAIN"
    measurement gain --delay "$AEC_DELAY" --gain-db "$AEC_GAIN" --part "$AEC_PART" \
      --out "$STEP15B_OUT/grid/$AEC_GAIN_DIR/part-$AEC_PART/attempt-$AEC_ATTEMPT" || exit $?
  done
done
aecpy hil/p4_aec_measure.py select-gain --plan "$STEP15B_OUT/plan.json" \
  --delay "$AEC_DELAY" --root "$STEP15B_OUT/grid" --out "$STEP15B_OUT/gain-selection.json"
```

Offline selection re-reads every original and re-derives all 12×13 scores,
clip deltas and `aec.reference_health.gain_db` readbacks before and after
each case. Missing scored parts, reordered/changed/different-ELF or different-bot
evidence and multiple scored originals are refused; retained unscored attempts
do not block resumption. Selection is about **<1 min**.
No eligible gain returns 1: stop and report, do not proceed to guards.

## Coordinator commands: guard observations and recommendation

Run candidate 0 first (**8–15 min**: ten 30 s room recordings plus inside/after
raw probes, flash, dump and restore). Inspect its `score.passed`, not just its
acquisition exit code. A clean acquisition may contain a failed candidate.
The next candidate command is allowed only if the earlier score failed.

```bash
measurement guard --gain-selection "$STEP15B_OUT/gain-selection.json" \
  --candidate-index 0 --out "$STEP15B_OUT/guard-0/attempt-01"
```

Only if candidate 0 failed, the exact next command is:

```bash
measurement guard --gain-selection "$STEP15B_OUT/gain-selection.json" \
  --candidate-index 1 --previous "$STEP15B_OUT/guard-0/attempt-01/result.json" \
  --out "$STEP15B_OUT/guard-1/attempt-01"
```

Continue indexes 2..11 only after failures, supplying **every** previous
original result in order to `--previous`. Unscored retries change only that
candidate's attempt path. Stop on a pass; an acquisition error pauses the
sequence for R7 recovery and then a fresh attempt at the same candidate.
Do not advance on an acquisition error. All twelve scored candidate failures
mean no accepted setting. Inside-guard injection timing ambiguity is reported
without affecting acceptance; do not fit timing tolerances. Once one passes, list
all originals through that first pass in order (example: index 0 passed):

```bash
aecpy hil/p4_aec_measure.py recommend --plan "$STEP15B_OUT/plan.json" \
  --gain-selection "$STEP15B_OUT/gain-selection.json" \
  --guard-results "$STEP15B_OUT/guard-0/attempt-01/result.json" \
  --out "$STEP15B_OUT/recommendation.json"
```

Offline recommendation takes **<1 min**, re-scores raw guard probes/turn
events and the complete sweep, and explicitly has `applied=false`. It writes
no firmware. The inherited 0/400/2000/1024 values remain unmeasured defaults
until the separate follow-up below is implemented and reviewed.

## Applying measurements: separate follow-up build

Give `recommendation.json` and every source original to the coordinator/reviewer.
Apply its selected reference gain and guard/re-arm through P4 Kconfig; apply
its approved AFE delay through the appropriate timing definition if it differs
from 1024. The step-15b 1024-sample input remains decision-backed, not an
independently measured pass. Do not choose the implementation delay from ERLE.
This is a **follow-up source change/build**, not a console-only adoption.
Preserve MIC1 30 dB, digital 125%, seed −289, output −6 dB and TX stack 26624 B.

After the follow-up source changes and metadata are reviewed, rebuild with
fresh config/build paths under the build lock. The commands below use the
same 15a target/defaults chain, pinned archive and gates. Build normal and
demo one at a time, outside all bench holds. Budget **5–15 min per build**.

```bash
: "${AEC_APPLY_OUT:?fresh follow-up build evidence directory}"
: "${AEC_RECOMMENDATION:?authenticated step-15b fix-5 recommendation file}"
mkdir -p "$AEC_APPLY_OUT"
scripts/build_locked.sh cmake -S components/dg_board/test_host -B build/host-board
scripts/build_locked.sh cmake --build build/host-board -j 4
for AEC_BUILD_VARIANT in normal demo; do
  AEC_AUTOSTART=0
  if [ "$AEC_BUILD_VARIANT" = demo ]; then AEC_AUTOSTART=1; fi
  AEC_BUILD="$PWD/build-p4xc-aec-selected-$AEC_BUILD_VARIANT"
  test ! -e "$AEC_BUILD" || exit 1
  DG_BOARD=p4xc DG_AEC=1 DG_DEMO_AUTOSTART="$AEC_AUTOSTART" \
    scripts/idf.sh -B "$AEC_BUILD" -DSDKCONFIG="$AEC_BUILD/sdkconfig" build || exit $?
  scripts/build_locked.sh bash -c '
    . "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
    python scripts/check-aec-image.py --board p4xc --elf "$1/dg-esp32.elf" \
      --archive "$2/audited-p4-processor.a" --ar riscv32-esp-elf-ar \
      --expected-sha256 16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100 &&
    python scripts/check_p4_headroom.py --elf "$1/dg-esp32.elf" --out "$3/headroom-$4.json" &&
    python scripts/p4_bundle.py --build "$1" --out "$3/bundle-p4xc-aec-$4" --recommendation "$5"
  ' bash "$AEC_BUILD" "$AEC15" "$AEC_APPLY_OUT" "$AEC_BUILD_VARIANT" "$AEC_RECOMMENDATION" || exit $?
done
```

The unchanged retention floor is 114928 B and the headroom report binds the
ELF hash. No applied-calibration claim is allowed before both image/headroom
checks and fresh manifests pass. Follow-up changes must update public
calibration metadata to the recommendation, retaining the decision-backed
delay label. Do not repin the R7 restore record. If shared AEC/timing or
bundle code changes, repeat the applicable 15a R3/R10 checks as well.

Set `AEC_P4_NORMAL`/`AEC_P4_DEMO` to the fresh bundles after adoption;
otherwise use the original 15a bundles. Each smoke needs an ERLE/residual
report from its **own ELF and actual configured gain**. For baseline normal,
reuse gain 0's original four parts from the full sweep. For demo, and for
both rebuilt selected images, the predeclared verification round is a new
plan, independent delay and four parts at the image's configured gain only.
It reports performance without selecting a different gain. For the demo
binary the measurement adapter issues `demo hold` before the controlled
manual measurement session; the later full demo smoke tests actual autostart,
hold/resume/cold behavior. No normal-image residual is attributed to demo.

```bash
: "${AEC_P4_NORMAL:=$AEC15/final-bundles/bundle-p4xc-aec-normal}"
: "${AEC_P4_DEMO:=$AEC15/final-bundles/bundle-p4xc-aec-demo}"
: "${AEC_REPORT_GAIN:?actual configured reference gain: baseline 0 or measured selected gain}"
# Keep this path absolute if STEP15B_OUT is changed for a follow-up round.
AEC_CAMPAIGN_OUT="$STEP15B_OUT"
export AEC_REPORTS_ROOT="$AEC_CAMPAIGN_OUT/image-reports"
for AEC_REPORT_VARIANT in normal demo; do
  (
    export STEP15B_OUT="$AEC_REPORTS_ROOT/$AEC_REPORT_VARIANT"
    mkdir -p "$STEP15B_OUT"
    if [ "$AEC_REPORT_VARIANT" = normal ] && [ "$AEC_P4_NORMAL" = "$AEC15/final-bundles/bundle-p4xc-aec-normal" ]; then
      # Original baseline normal evidence, including its independently measured delay.
      aecpy hil/p4_aec_measure.py report-gain --plan "$AEC_CAMPAIGN_OUT/plan.json" \
        --delay "$AEC_DELAY" --root "$AEC_CAMPAIGN_OUT/grid" --gain-db "$AEC_REPORT_GAIN" \
        --out "$STEP15B_OUT/erle-report.json" || exit $?
    else
      AEC_REPORT_BUNDLE="$AEC_P4_NORMAL"
      if [ "$AEC_REPORT_VARIANT" = demo ]; then AEC_REPORT_BUNDLE="$AEC_P4_DEMO"; fi
      aecpy hil/p4_aec_measure.py plan --bundle "$AEC_REPORT_BUNDLE" --out "$STEP15B_OUT/plan.json" || exit $?
      measurement delay --out "$STEP15B_OUT/delay/attempt-01" || exit $?
      AEC_REPORT_DELAY="$STEP15B_OUT/delay/attempt-01/result.json"
      printf -v AEC_GAIN_DIR 'gain-%02d' "$AEC_REPORT_GAIN"
      for AEC_PART in 0 1 2 3; do
        measurement gain --delay "$AEC_REPORT_DELAY" --gain-db "$AEC_REPORT_GAIN" --part "$AEC_PART" \
          --out "$STEP15B_OUT/grid/$AEC_GAIN_DIR/part-$AEC_PART/attempt-01" || exit $?
      done
      aecpy hil/p4_aec_measure.py report-gain --plan "$STEP15B_OUT/plan.json" \
        --delay "$AEC_REPORT_DELAY" --root "$STEP15B_OUT/grid" --gain-db "$AEC_REPORT_GAIN" \
        --out "$STEP15B_OUT/erle-report.json" || exit $?
    fi
  ) || exit $?
done
```

The reused normal report is offline (**<1 min**). Each new image verification
round takes **20–46 min** including delay and four bounded parts. The same
unscored-only retry/resume rules apply: use the stopped image/part's fresh
`attempt-02` path and its actual delay result; never restart completed parts.
An unavailable delay or ERLE report stops that image's smoke. This retains ten
reply ERLE/residual measurements and S3 report-only deltas for each exact image.

## Coordinator commands: unchanged 13b contention gate with AEC

Use the coordinator-owned unchanged `server/bot.py` **live-test** process and
its own verified identity, as in `p4_contention.md`; do not use the demo bot
for this workload. Switching phases is coordinator-owned. Never restart a bot
between the two members of a pair. The adapter starts no bot. `AEC_COMMANDS`
is that existing bot's command-file path. Budget **8–12 min** per run, including
the unchanged 300 s workload, restart at 150 s, preflight/flash and restore.

```bash
: "${AEC_P4_NORMAL:=$AEC15/final-bundles/bundle-p4xc-aec-normal}"
: "${AEC_CONTENTION_BOT_DIR:?existing live-test bot directory}"
: "${AEC_CONTENTION_IDENTITY:?verified identity JSON for that process}"
: "${AEC_COMMANDS:?its existing display command file}"
aeclive hil/p4_aec_product.py contention --board p4xc --bundle "$AEC_P4_NORMAL" \
  --out "$STEP15B_OUT/contention-p4xc" \
  --bot-dir "$AEC_CONTENTION_BOT_DIR" --bot-identity "$AEC_CONTENTION_IDENTITY" \
  --bot-repo "$HOME/src/dg-esp32" --commands "$AEC_COMMANDS" \
  --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
```

The unchanged 13b scorer still enforces max stall 68411 µs, paired p99 delta
7000 µs, coverage .95, minimum heap 81920 B, largest 32768 B, zero allocations,
overflows and underruns, real fetch/persistence/delete/touch/render work and
both generations. The adapter changes only AEC intent at validation and adds
AEC counter and existing task-stack gates. Raw wire is never rewritten.
No new timing/resource allowance. For a rebuilt selected image add
`--applied "$AEC_RECOMMENDATION"`; it re-derives the recommendation and checks
fresh ELF and all four selected values. Quiet-room preflight exit 2 and
untouched host preflight error exit 3 are unscored; retry later in a fresh run directory. All failures restore the same R7 pin.

## Coordinator commands: manual/demo smoke and S3/P4 AEC wire pairs

Return to one unchanged `demo_bot.py` process and matching `BOT_DIR`/
`BOT_IDENTITY`, retained for both boards and both variants. Historical 15b
runs use the clean-`a2d7765` fix-1 bundles below. Applied 15c runs use the
fresh pin-compared S3 bundles in the step-15c commands at the end.
Budget **8–12 min per manual smoke**, **10–16 min per demo smoke** including
F20 hold/resume/cold and restore. A pair is offline, **<1 min**.

```bash
: "${AEC_P4_NORMAL:=$AEC15/final-bundles/bundle-p4xc-aec-normal}"
: "${AEC_P4_DEMO:=$AEC15/final-bundles/bundle-p4xc-aec-demo}"
AEC_APPLIED_ARGS=()
# After the separate selected-value rebuild only:
# AEC_APPLIED_ARGS=(--applied "$AEC_RECOMMENDATION")
for AEC_VARIANT in normal demo; do
  for AEC_BOARD in echoear p4xc; do
    AEC_EXTRA=()
    if [ "$AEC_BOARD" = p4xc ]; then
      if [ "$AEC_VARIANT" = normal ]; then AEC_BUNDLE="$AEC_P4_NORMAL"; else AEC_BUNDLE="$AEC_P4_DEMO"; fi
      AEC_EXTRA=(--erle-report "$AEC_REPORTS_ROOT/$AEC_VARIANT/erle-report.json" "${AEC_APPLIED_ARGS[@]}")
    else
      AEC_BUNDLE="$HOME/src/evidence/echoear/p4/step-15a-fix-1/bundle-echoear-aec-$AEC_VARIANT"
    fi
    aeclive hil/p4_aec_product.py smoke --board "$AEC_BOARD" --bundle "$AEC_BUNDLE" \
      --out "$STEP15B_OUT/smoke-$AEC_BOARD-aec-$AEC_VARIANT" \
      --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
      --pair-id "step15b-aec-$AEC_VARIANT" --bounds hil/fixtures/wire_contract_bounds.json \
      --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" \
      "${AEC_EXTRA[@]}" || exit $?
  done
  aecpy hil/p4_aec_product.py pair "$STEP15B_OUT/smoke-echoear-aec-$AEC_VARIANT" \
    "$STEP15B_OUT/smoke-p4xc-aec-$AEC_VARIANT" --out "$STEP15B_OUT/pair-aec-$AEC_VARIANT.json"
done
```

The adapter uses the full frozen executor in scored mode: observed talk-over
must interrupt and answer Rome; ten long replies have zero self-interruptions;
demo hold/resume/cold, product capability, original wire schemas and fixed
variant bounds all apply. ERLE/residual comparisons remain report-only; no
spike or S3 value becomes a P4 pass. Pair checks both AEC intents, actual boot
ELFs/settings, same pair id and bot identity, clean restore, exactly main
(normal) or main/resume/cold (demo), and hashes every original source. No
server or frozen HIL Python source changes, no bot lifecycle in these tools.

Repeat both smokes and both wire pairs after the selected-value rebuild;
retain baseline results and all originals. No hardware parity, calibration
adoption or qualification is claimed by this writer's host tests.


## Offline schema audit and review notes

Before generating a new acquisition plan, replay the retained early P4/S3
normal/demo PERF, turn-event and guard-console records (offline, <1 min):

```bash
aecpy hil/p4_aec_schema.py --root "$HOME/src/evidence/echoear/p4/step-15b-early" \
  --out "$STEP15B_OUT/schema-audit.json"
```

The report inventories each scorer's actual PERF/event field reads and the
five `AEC_GUARD` fields on both boards, including the corrected nested gain.
Modeled PCM exercises ERLE key access only, never claims a measured result.
The early smoke archive has no all-slot dump/clock/runtime-gain transaction;
those separate formats remain covered by firmware templates and host dump,
clock and readback controls until dedicated acquisition exists. Generate the
post-review `plan.json` only in a **fresh path**, after the code is final; keep
old writer-only offline plans as historical evidence. Source-hash changes
cannot be adopted into an already-scored campaign.

Review P3 notes: the contention adapter retains all 13b gates but does not
emit the older runner's optional `display_static_ram` figure or standalone
`limits.json` (the identical limits remain in `acquisition.json`). `quiet 1`
only suppresses routine reports; `AEC_GUARD` AUDIO_LOG lines survive as on S3.
No extra acceptance gate is inferred from either diagnostic.

## Fix-3 report-only probe/reference diagnosis

Evidence: `/home/khkramer/src/evidence/echoear/p4/step-15b-fix-3/`;
`analyze-reference.py`, `reference-diagnosis.json` and its log reproduce the
read-only analysis of the coordinator's immutable delay attempt.

The canonical probe is present in MIC3. The **entire two-second active noise**
matches with signed correlation **+0.973862**, secondary 0.521592. Eight 150 ms
slices each locate the same source-pattern offset, **9335 samples**, with
correlations 0.967467..0.975386. The failed full-template test is an extent
problem: its search cannot place the complete 38400-sample template at that
offset inside a 46400-frame capture.

| Quantity on the original 16 kHz ADC sample coordinates | Samples |
|---|---:|
| Captured extent | 0..46400 |
| Full canonical template's allowed start range | 0..8000 |
| Actual source-pattern offset from active-noise match | 9335 |
| Complete active-noise interval, retained in the capture | 12535..44535 |
| Required end of the complete template, including silent tail | 47735 |
| Missing canonical trailing silence | 1335 (83.44 ms) |

No PCM was padded or rewritten, and the diagnostic active-source crop is not
fed back into delay acceptance or ERLE. The original full-match failure
remains unchanged. Firmware sends `OK ... aec-probe` **before** its writes,
then `DONE` after submitting them; the current helper waits for OK, pauses
0.6 s, and queues record-stop while the console is still writing the probe.
TX submission also precedes physical DMA drain. This explains how capture
can stop before the canonical quiet tail is complete. A future capture-only
follow-up could wait for DONE and a drained quiet margin; this diagnosis
changes neither capture code nor firmware.

The C++ `AecProbe` header and Python `probe_pcm()` emit identical 76800-byte
PCM16 streams (SHA256
`0efe603c5cfa0f7e7a83d390bbfad7d836d2cadb9346867658600eb5ad8731bf`).
The probe goes straight through `Impl::write`/`Speaker::write`, with block
volume scaling and stereo duplication; it bypasses network resampling and
`PlaybackDsp` normalization/compression. Codec rate configuration is 16 kHz.
Boot records RX master external MCLK 4096000/BCLK 1024000 and paired TX slave
internal MCLK 8192000/BCLK 1024000. The TX internal MCLK value is not a second
external codec clock pin. RX owns GPIO13 MCLK; both directions share BCLK/WS
(16000 Hz with two 32-bit slots). This is configured/read-back clock evidence,
not an oscilloscope measurement. Half/double-rate probe controls correlate
only 0.226/−0.112, versus 0.971 at nominal rate. No integer lag drift is resolved
across the eight slices. A DAC/ADC rate mismatch does not explain this failure.

MIC3 is word0-low in the product map, with MIC1 word0-high. MIC3 RMS is
−64.35 dBFS during DAC probing versus −94.71 dBFS during external Jabra speech,
while MIC1 carries that external speech. Together with the earlier PA-off
slot/gain evidence in `docs/p4xc.md`, this corroborates an electrical DAC
reference. Its positive known-probe polarity and the frozen matcher's use of
absolute correlation rule out simple inversion as the failed-match cause.
The simultaneous MIC3→MIC1 peak is negative (−0.693, lag 5), a separate
acoustic-path polarity observation, not a reason to invert the reference.

The independent MIC1→AFE candidate remains **1024 samples**, correlation
0.999933/secondary 0.386063. The Jabra known-source corroboration still fails
(0.538582/0.520286), so this report does not promote the original delay to a
measured pass. The explicit decision-backed input remains required. This
analysis supports a usable MIC3 reference and finds no reference-invalidity
reason to stop calibration; it establishes neither a calibrated gain nor AEC
performance/qualification. No firmware or frozen scorer changed.

## Fix-4 direction deduplication and offline re-selection

**Host validation ran after the coordinator released the bench. Re-selection
and live commands below are coordinator operations after review.** Work from
`/home/khkramer/src/dg-esp32-p4-fix4`, branch
`p4-port-fix4`. Leave `/home/khkramer/src/dg-esp32-p4` and all acquisition
records untouched. The existing `aecpy`/`aeclive` shell functions use this
checkout after changing directory; keep the environment and R7 identity
variables above.

`p4_aec_reselect.py` validates the acquisition plan's 17 source hashes against
immutable Git blobs at `b11f7e30b01b800a1e884d0f6d248b20060c9135`, plus its
original bundle and fixtures. It does not rebind or regenerate the plan to
match the edited checkout. Unadapted scoring helpers must still match those
hashes. Its output binds the adapter sources and both pinned decisions:
`decision-2026-11-02-p4-aec-prompt-dedup` and
`decision-2026-11-02-p4-aec-sweep-predeclared`.

The frozen `board_smoke.turn_checks` uses `downstream_frames`: it validates
every frame direction and counts DOWNSTREAM frames only. The adapter applies
that convention to prompt frames while retaining non-frame events, then calls
the original `prompt_score`. Transcript accuracy, injection overlap, the
intentional-turn witness and earlier bot-active false turns keep their
existing rules. Echo scoring, raw-PCM clipping and gain selection are unchanged.
Each original cached score must still reproduce using the acquisition scorer;
the corrected prompt score is retained separately in the re-scored points.
ERLE is re-derived from raw PCM at the supplied validated delay, with both
acquisition and re-scoring delay provenance retained.

A part scored before disconnect/cleanup/restore failure remains consumed and
is used as scored, exactly as pre-declared. The adapter requires its original
payload hash and matching result/checkpoint metadata, records the failure and
restore status, and never retries or overwrites it. A payload without the
necessary result context is insufficient. **The coordinator must establish R7
recovery before continuing the sweep or guard.** Offline selection is not proof
that the board recovered and does not release any bench/recovery hold.

After the completed sweep, use a fresh output outside its grid:

```bash
cd "$HOME/src/dg-esp32-p4-fix4"
export AEC_SWEEP="$HOME/src/evidence/echoear/p4/step-15b/coordinator-sweep-1"
export AEC_DELAY="$HOME/src/evidence/echoear/p4/step-15b-fix-3/decision-delay-record.json"
: "${AEC_FIX4_OUT:?fresh coordinator output directory outside the original grid}"
mkdir -p "$AEC_FIX4_OUT"
aecpy hil/p4_aec_reselect.py select-gain \
  --plan "$AEC_SWEEP/plan.json" --delay "$AEC_DELAY" \
  --root "$AEC_SWEEP/grid" --out "$AEC_FIX4_OUT/reselection.json"
```

If present, `$AEC_SWEEP/gain-selection.json` is embedded as the original
selection, with its path and hash; use `--original-selection PATH` for a
different retained selection. The fresh output includes `original_selection`
(or null), `rescored_selection`, decision identities/hashes, adapter-source
hashes, raw-source hashes and `finalization_failures`. No eligible gain still
means stop. Do not edit either pinned delay decision/source file, or use ERLE
to choose a delay.

The coordinator's subsequent guard must use the same reviewed fix-4 sources
and new selection. Use the additive entry point, retaining the normal bench,
quiet-room, bot-identity and R7 path:

```bash
aeclive hil/p4_aec_reselect.py guard \
  --plan "$AEC_SWEEP/plan.json" --gain-selection "$AEC_FIX4_OUT/reselection.json" \
  --candidate-index 0 --out "$AEC_FIX4_OUT/guard-00/attempt-01" \
  --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
  --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
```

Advance only after all earlier declared candidates fail, supplying their
ordered result paths through `--previous`. Guard/recommendation replay checks
the selection, decisions and scoring context again and passes the corrected
prompt scorer into both guard probes. Long-turn checks already use the frozen
DOWNSTREAM convention. The guard's existing acceptance and clean restore gates
remain in force. Stop at the first accepted candidate; include every earlier
candidate when producing the recommendation:

```bash
aecpy hil/p4_aec_reselect.py recommend \
  --plan "$AEC_SWEEP/plan.json" --gain-selection "$AEC_FIX4_OUT/reselection.json" \
  --guard-results "$AEC_FIX4_OUT/guard-00/attempt-01/result.json" \
  --out "$AEC_FIX4_OUT/recommendation.json"
```

The recommendation exposes `delay_provenance.kind="decision-backed"` and
`measured_pass=false` for the authorized 1024-sample input; it does not claim
a measured delay or apply settings. The product applied-calibration gate
dispatches fix-4 records through the same replay adapter. Freeze adapter
sources between selection, guard, recommendation and their verification.

Changed files in this round: new `hil/p4_aec_reselect.py` and
`hil/test_p4_aec_reselect.py`; modified `hil/p4_aec_measure.py`,
`hil/p4_aec_product.py`, `hil/p4_aec.md` and `scripts/host-tests.sh`.
The 245 focused host tests include the real Italy control, synthetic earlier
bot-active and three-real-start negatives, and AEC integration controls.
No tests or builds ran during the sweep hold; no hardware result is claimed.

## Fix-5 retained guard event re-score and recommendation

The new offline module `p4_aec_guard_rescore.py` preserves all fix-4 helper
bytes, so the retained reselection and guard keep their original source
bindings. It accepts only the hash-pinned candidate-0 attempt, reselection,
plan and `decision-2026-11-02-p4-aec-guard-event-detection`. It replays gain
selection and the original guard score, verifies the scored marker/payload,
long-turn and probe originals, and hashes every file in the guard attempt.

The event decision requires a DOWNSTREAM bot-active start after injection,
an interruption and one non-empty transcript after injection, no earlier
bot-active start/interruption, and an injection request strictly later than
bot start plus guard plus re-arm. Sample-level arrival timing and its failed
Jabra correlation stay report-only. The existing long-turn, zero-self-turn,
observed settings, re-arm and clean-restore terms remain required. This rule
does not impose the sweep prompt's transcript accuracy threshold on guard
detection. Inside-probe findings remain report-only.

From the fix-4 worktree, use fresh paths outside retained acquisition:

```bash
export AEC_FIX5_OUT="$HOME/src/evidence/echoear/p4/step-15b-fix-5"
aecpy hil/p4_aec_guard_rescore.py guard-rescore \
  --plan "$AEC_SWEEP/plan.json" \
  --gain-selection "$HOME/src/evidence/echoear/p4/step-15b/coordinator-fix4/reselection.json" \
  --attempt "$HOME/src/evidence/echoear/p4/step-15b/coordinator-fix4/guard-00/attempt-01/result.json" \
  --out "$AEC_FIX5_OUT/guard-rescore.json"
aecpy hil/p4_aec_guard_rescore.py recommend \
  --plan "$AEC_SWEEP/plan.json" \
  --gain-selection "$HOME/src/evidence/echoear/p4/step-15b/coordinator-fix4/reselection.json" \
  --guard-results "$AEC_FIX5_OUT/guard-rescore.json" \
  --out "$AEC_FIX5_OUT/recommendation.json"
```

The recommendation is `kind="p4-aec-event-guard-recommendation"`, with
`applied=false`, full source/decision provenance, and an explicit
decision-backed, `measured_pass=false` delay label. Replay it with this module,
not the legacy recommendation command. A follow-up adoption change must wire
this replay into the product applied-calibration gate before claiming applied
calibration. The draft P4 defaults are separate from this tooling commit;
firmware adoption still needs its reviewed rebuild, image/headroom checks,
fresh bundles and hardware validation. S3 defaults and qualification are not
changed by the P4 recommendation.

## Step 15c: applied-image coordinator runs

Run from `/home/khkramer/src/dg-esp32-p4` after the step-15c commit/review.
These commands supersede the baseline 15b commands for applied images. The
fix-4 worktree has been removed. `p4_aec_applied.py` replays the original
recommendation with exact Git-pinned fix-5 code in a temporary source tree;
only the historical source-location label is aliased after all code and
decision hashes match. It verifies original gain/guard/raw evidence without
editing either frozen replay module, any decision or any original record.

Fresh normal ELF: `ae954c7bc18176d46ca35926da1193cba33115d5268280cd8a020bc98c6776f0`.
Fresh demo ELF: `36455c41d8408c75ec5ee3aa0f996b07fc7bfbc7aabd37fa689f04bc0fa65c58`.
Both retain 116128 B pre-main headroom, **1200 B above the unchanged 114928 B
floor**. The image checks verify the original 3072 coefficient bytes in flash
rodata. Six rebuilt preservation comparisons (four S3, two P4 AEC-off) have
zero unmasked differing bytes against their pins.

The P4 bundle metadata records the applied settings and authenticated
recommendation with per-field status: gain `selected-single-trial`, guard
`provisional`, delay `decision-backed`. Gain 30 dB won the pre-declared sweep
rule using one trial per prompt at each gain; repeatability/optimality are
not established. Guard 400/2000 has event-detection evidence only, without
physical in-window validation; that work remains owed to 15d. Delay 1024 has
`measured_pass=false`. The metadata makes no measured-calibration or
qualification claim, and the applied gate requires these labels. The gate
requires the fresh ELF and matching four values; smoke boot/wire and
contention boot/PERF checks verify the configured gain, guard, re-arm and delay.
All existing transport, stack, resource, restart, quiet-room and R7 gates
remain in force. Writer validation runs no live command.

Set up paths and the same `aecpy`/`aeclive` functions defined earlier:

```bash
cd "$HOME/src/dg-esp32-p4"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export AEC15C="$HOME/src/evidence/echoear/p4/step-15c"
export AEC15C_P4_BUNDLES="$HOME/src/evidence/echoear/p4/step-15c-fix-1/final-bundles"
export AEC_RECOMMENDATION="$HOME/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json"
export AEC_P4_NORMAL="$AEC15C_P4_BUNDLES/bundle-p4xc-aec-normal"
export AEC_P4_DEMO="$AEC15C_P4_BUNDLES/bundle-p4xc-aec-demo"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
: "${AEC_15C_RUN:?fresh coordinator live evidence parent}"
mkdir -p "$AEC_15C_RUN"
```

First the applied **normal** AEC contention gate, using the coordinator's
unchanged `server/bot.py` live-test process and verified command path. This
is a different bot phase from the demo-bot image reports/smokes below.
Budget 8–12 minutes. The coordinator owns phase switching; the runner starts
no bot and restores the pinned normal AEC-off image afterwards.

```bash
: "${AEC_CONTENTION_BOT_DIR:?existing live-test bot directory}"
: "${AEC_CONTENTION_IDENTITY:?verified identity for that process}"
: "${AEC_COMMANDS:?existing live-test bot command file}"
aeclive hil/p4_aec_product.py contention --board p4xc --bundle "$AEC_P4_NORMAL" \
  --applied "$AEC_RECOMMENDATION" --out "$AEC_15C_RUN/contention-p4xc" \
  --bot-dir "$AEC_CONTENTION_BOT_DIR" --bot-identity "$AEC_CONTENTION_IDENTITY" \
  --bot-repo "$HOME/src/dg-esp32" --commands "$AEC_COMMANDS" \
  --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
```

Use one unchanged `demo_bot.py` process/identity for the remaining paired
phase, including both boards and both variants. Generate a fresh same-image
report plan for **each** P4 ELF; acquire the ten long replies plus standard
prompts in the existing four bounded parts at fixed gain 30. This new adapter
binds the original approved delay/recommendation to the rebuilt ELF, retaining
its original source/decision provenance and `measured_pass=false`. It neither
re-measures/promotes the delay nor re-selects the gain. Each part retains
raw PCM, clocks, dumps, originals and score payloads. Budget 16–40 minutes
per image; unscored-only retries still use a fresh attempt-NN after R7 recovery.
Demo acquisition holds autostart for controlled reports; the later demo
smoke exercises actual main/resume/cold behavior.

```bash
: "${BOT_DIR:?existing unchanged demo bot directory}"
: "${BOT_IDENTITY:?verified identity for that process}"
applied_measure() {
  aeclive hil/p4_aec_image_measure.py "$@" \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
    --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
}
for AEC_VARIANT in normal demo; do
  AEC_BUNDLE="$AEC_P4_NORMAL"
  if [ "$AEC_VARIANT" = demo ]; then AEC_BUNDLE="$AEC_P4_DEMO"; fi
  AEC_IMAGE_OUT="$AEC_15C_RUN/image-reports/$AEC_VARIANT"
  mkdir -p "$AEC_IMAGE_OUT"
  aecpy hil/p4_aec_image_measure.py plan --bundle "$AEC_BUNDLE" \
    --recommendation "$AEC_RECOMMENDATION" --out "$AEC_IMAGE_OUT/plan.json" || exit $?
  for AEC_PART in 0 1 2 3; do
    applied_measure gain --plan "$AEC_IMAGE_OUT/plan.json" --part "$AEC_PART" \
      --out "$AEC_IMAGE_OUT/grid/gain-30/part-$AEC_PART/attempt-01" || exit $?
  done
  aecpy hil/p4_aec_image_measure.py report-gain --plan "$AEC_IMAGE_OUT/plan.json" \
    --root "$AEC_IMAGE_OUT/grid" --out "$AEC_IMAGE_OUT/erle-report.json" || exit $?
done
```

Fresh scored reference/applied smokes and offline pairs follow. S3 bundles
are the step-15c rebuilds masked-identical to their existing pins. Every
normal pair contains `main`; every demo pair contains ordered `main`,
`resume`, `cold`. Each side validates its actual boot ELF and gain before
the frozen pair comparator normalizes board-specific audio capabilities.
Keep one pair id and bot identity per variant; retain every raw wire original.
Budget 8–12 minutes per normal smoke and 10–16 per demo smoke.

```bash
for AEC_VARIANT in normal demo; do
  for AEC_BOARD in echoear p4xc; do
    AEC_EXTRA=()
    if [ "$AEC_BOARD" = p4xc ]; then
      AEC_BUNDLE="$AEC_P4_NORMAL"
      if [ "$AEC_VARIANT" = demo ]; then AEC_BUNDLE="$AEC_P4_DEMO"; fi
      AEC_EXTRA=(--applied "$AEC_RECOMMENDATION" \
        --erle-report "$AEC_15C_RUN/image-reports/$AEC_VARIANT/erle-report.json")
    else
      AEC_BUNDLE="$AEC15C/bundle-echoear-aec-$AEC_VARIANT"
    fi
    aeclive hil/p4_aec_product.py smoke --board "$AEC_BOARD" --bundle "$AEC_BUNDLE" \
      --out "$AEC_15C_RUN/smoke-$AEC_BOARD-aec-$AEC_VARIANT" \
      --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
      --pair-id "step15c-aec-$AEC_VARIANT" --bounds hil/fixtures/wire_contract_bounds.json \
      --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" \
      "${AEC_EXTRA[@]}" || exit $?
  done
  aecpy hil/p4_aec_product.py pair "$AEC_15C_RUN/smoke-echoear-aec-$AEC_VARIANT" \
    "$AEC_15C_RUN/smoke-p4xc-aec-$AEC_VARIANT" \
    --out "$AEC_15C_RUN/pair-aec-$AEC_VARIANT.json" || exit $?
done
```

Finally, a **report-only** double-talk repeat on the applied 30 dB P4 normal
image: France ×5 and speech ×5 during bot playback, two fresh R7-protected
attempts. Each `trials` entry records transcript, accuracy, interruption,
median ERLE/full ERLE diagnostics, clips and the full original. ERLE here
includes near-end speech and is diagnostic. Accuracy, interruption and ERLE
do not gate these trials, alter calibration or re-select a gain. A failed
acquisition still retains its evidence and must restore safely. Budget about
5–10 minutes per set; use a fresh attempt-NN for an unscored retry only.

```bash
for AEC_PROMPT in france speech; do
  applied_measure double-talk --plan "$AEC_15C_RUN/image-reports/normal/plan.json" \
    --prompt "$AEC_PROMPT" \
    --out "$AEC_15C_RUN/double-talk-$AEC_PROMPT/attempt-01" || exit $?
done
```

These are prepared coordinator commands, not writer hardware results.

## Step 15c fix 2: TX frame trim, carry-over and fresh smokes

**Carry-over FAIL for both P4 variants and S3 normal.** New TX entry frames
are 160 B: S3 normal was 880 B; both P4 variants were 848 B. Effective
configs are equal and only the helper is added, but three other function
sizes change in each P4 ELF and seven in S3. The binding decision therefore
requires the **full 15c demo-phase reacquisition below**. No old ERLE or
double-talk measurement carries to these images. Final hashes and offline
writer gates are in the fix-2 report.
The shared firmware edit moves the unchanged ten-field `opus_enc`
construction into a non-inlined helper after encoder initialization. Stack
allocations and Kconfig stay unchanged: S3 AEC and P4 AEC both retain 26624 B.

This section supersedes the previous step-15c smoke bundle paths. Fresh
bundles will be under
`~/src/evidence/echoear/p4/step-15c-fix-2/final-bundles/`:

| Board | Normal bundle | Demo bundle |
|---|---|---|
| S3 | `bundle-echoear-aec-normal` | `bundle-echoear-aec-demo` |
| P4 | `bundle-p4xc-aec-normal` | `bundle-p4xc-aec-demo` |

| Fix-2 image | ELF SHA-256 |
|---|---|
| S3 AEC normal | `015efd59fe7129cde4031811edb0109f7480cb39064f8de3e5ce48233dba25a0` |
| S3 AEC demo | `8266e9db5ae9ea838e8231888b4938c1e21338ad63dbd44e2b997de2580b382a` |
| P4 AEC normal | `2831d8bd295eaeea21c603f6aaff11a8d42ffe5a7840c96fd12331cadf7665da` |
| P4 AEC demo | `46d7d9cf034c036ad66f92b3c9fc38317ebfd3f245f14c34da0832408a6f983c` |

All four entry frames are 160 B and all four stack allocations remain
26624 B. Both P4 AEC images retain 116128 B pre-main headroom (+1200 B above
the unchanged floor). These are offline results, not new runtime measures.

The binding decision is
`decision-2026-11-02-tx-frame-trim-carryover`. Its P4 source ELFs are normal
`ae954c7bc18176d46ca35926da1193cba33115d5268280cd8a020bc98c6776f0`
and demo `36455c41d8408c75ec5ee3aa0f996b07fc7bfbc7aabd37fa689f04bc0fa65c58`.
The S3 source ELF for the normal frame check is
`d09b3560cdca3bdd8133ea207061f19a24852758560701b28868ed3a143225d7`.
Old bundles and their manifests remain immutable.

The new offline `p4_elf_carryover.py` requires the same defined function
symbol names/multiplicities except the one added helper, equal sizes for
every function other than TX/the helper, decoded old/new entry frames, and
equal effective generated configs. It compares **all** config keys through
`sdkconfig_keydiff`, which is stronger than comparing an audio allow-list;
output contains key names/statuses and config digests only. S3's new TX frame
must be at most 192 B. Unknown prologues, missing provenance, extra compiler
symbols, or any size/config difference produce FAIL; they are not silently
allowed as compiler noise. Record one fresh JSON receipt for each pair under
`step-15c-fix-2/carryover/`.

Carry-over failed; any future passing carry-over would need its own reviewed change.

If a binding condition fails, **no historical measurement carries**. Run the
full previous step-15c demo phase on the new P4 images, including fresh
applied-image report plans, all four gain-report parts for each variant,
and report-only double-talk. Use the ordinary `p4_aec_product.py smoke`
commands with those new same-image ERLE reports. Preserve the FAIL receipt
and identify the failed condition; never widen a mask or symbol exception.

Writer-only offline commands below require a current **bench free** go. Set
each config input from its retained `project_description.json`; the checker
binds it to the bundle, and refuses missing/stale provenance. The S3 old
bundle has no resolved-config digest, so its original build ELF and project
config path must still match. An overwritten original build is a provenance
failure to resolve or report, never permission to substitute a new config.
These commands are not live bench commands and do not acquire a bench lock.

```bash
export AEC15CFIX2="$HOME/src/evidence/echoear/p4/step-15c-fix-2"
export AEC15CFIX2_BUNDLES="$AEC15CFIX2/final-bundles"
export AEC15CFIX2_OLD_P4="$HOME/src/evidence/echoear/p4/step-15c-fix-1/final-bundles"
: "${TX_OLD_P4_NORMAL_CONFIG:?retained normal generated config}"
: "${TX_OLD_P4_DEMO_CONFIG:?retained demo generated config}"
: "${TX_OLD_S3_NORMAL_CONFIG:?retained S3 normal generated config}"
: "${TX_NEW_P4_NORMAL_CONFIG:?fresh fix-2 normal generated config}"
: "${TX_NEW_P4_DEMO_CONFIG:?fresh fix-2 demo generated config}"
: "${TX_NEW_S3_NORMAL_CONFIG:?fresh fix-2 S3 normal generated config}"
mkdir -p "$AEC15CFIX2/carryover"
TX_CARRY_FAILED=0
aecpy hil/p4_elf_carryover.py --pair p4-normal \
  --old-bundle "$AEC15CFIX2_OLD_P4/bundle-p4xc-aec-normal" \
  --new-bundle "$AEC15CFIX2_BUNDLES/bundle-p4xc-aec-normal" \
  --old-config "$TX_OLD_P4_NORMAL_CONFIG" --new-config "$TX_NEW_P4_NORMAL_CONFIG" \
  --out "$AEC15CFIX2/carryover/p4-normal.json" || TX_CARRY_FAILED=1
aecpy hil/p4_elf_carryover.py --pair p4-demo \
  --old-bundle "$AEC15CFIX2_OLD_P4/bundle-p4xc-aec-demo" \
  --new-bundle "$AEC15CFIX2_BUNDLES/bundle-p4xc-aec-demo" \
  --old-config "$TX_OLD_P4_DEMO_CONFIG" --new-config "$TX_NEW_P4_DEMO_CONFIG" \
  --out "$AEC15CFIX2/carryover/p4-demo.json" || TX_CARRY_FAILED=1
aecpy hil/p4_elf_carryover.py --pair s3-normal \
  --old-bundle "$HOME/src/evidence/echoear/p4/step-15c/bundle-echoear-aec-normal" \
  --new-bundle "$AEC15CFIX2_BUNDLES/bundle-echoear-aec-normal" \
  --old-config "$TX_OLD_S3_NORMAL_CONFIG" --new-config "$TX_NEW_S3_NORMAL_CONFIG" \
  --out "$AEC15CFIX2/carryover/s3-normal.json" || TX_CARRY_FAILED=1
```

The three offline diagnostic commands retain separate results rather than
stopping after the first FAIL. Existing fix-2 receipts and manifests remain
historical evidence; use fresh output paths for any later check. The full
reacquisition phase below is required for this delivery.

S3 AEC's final exercised free stack must be at least 4096 B at its existing
allocation; approximately 4512–4524 B is expected if the 160 B TX frame is
restored. Do not accept the larger pre-turn watermark by itself. Normal
smokes contain all twelve turns; demo smokes also include resume and cold
sessions. The default strict smoke still stops on bot content failures. The opt-in
fix-4 rule below allows content-only misses to be reported while later steps
continue; stack and carry-over checks remain required. Commit only when
the user instructs it. Only offline writer checks have run; no coordinator
live command in this fix-2 section has been executed by the writer.

### Required fix-2 phase after the failed carry-over checks

Use the four new bundles above, preserving gain `selected-single-trial`,
guard `provisional` and delay `decision-backed`. Their extra
`tx_frame_carryover` metadata records FAIL and `measurement_carried=false`.
Prior image reports and double-talk originals remain immutable historical
records; do not substitute them for the inputs below. The frozen ordinary
product runner is used throughout this branch, with new same-image reports.

Set the existing `aecpy`/`aeclive` functions and environment from the earlier
setup. The unchanged pinned normal AEC-off restore record remains in force.
Use a fresh output parent. First repeat the applied normal contention phase
with the coordinator's existing live-test bot and its authenticated identity:

```bash
export AEC15CFIX2="$HOME/src/evidence/echoear/p4/step-15c-fix-2"
export AEC15CFIX2_BUNDLES="$AEC15CFIX2/final-bundles"
export AEC_P4_NORMAL="$AEC15CFIX2_BUNDLES/bundle-p4xc-aec-normal"
export AEC_P4_DEMO="$AEC15CFIX2_BUNDLES/bundle-p4xc-aec-demo"
export AEC_RECOMMENDATION="$HOME/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
: "${AEC_15CFIX2_RUN:?fresh fix-2 live output parent}"
: "${AEC_CONTENTION_BOT_DIR:?existing live-test bot process directory}"
: "${AEC_CONTENTION_IDENTITY:?verified identity for the live-test process}"
: "${AEC_COMMANDS:?existing live-test command file}"
mkdir -p "$AEC_15CFIX2_RUN"
aeclive hil/p4_aec_product.py contention --board p4xc --bundle "$AEC_P4_NORMAL" \
  --applied "$AEC_RECOMMENDATION" --out "$AEC_15CFIX2_RUN/contention-p4xc" \
  --bot-dir "$AEC_CONTENTION_BOT_DIR" --bot-identity "$AEC_CONTENTION_IDENTITY" \
  --bot-repo "$HOME/src/dg-esp32" --commands "$AEC_COMMANDS" \
  --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
```

The coordinator then switches to one unchanged demo bot process/identity for
both image reports, report-only double-talk, all four smokes and both pairs.
Acquire all four bounded report parts per P4 variant. The new report plans
bind the new ELF and current labels; the recommendation remains the original
authenticated decision, not a re-selection or measured-delay claim. Repeat
France and speech double-talk on the normal image, five trials per prompt,
as report-only diagnostics. Unscored retries use new attempt directories.

```bash
: "${BOT_DIR:?one unchanged demo bot process directory}"
: "${BOT_IDENTITY:?verified identity for that same process}"
fix2_measure() {
  aeclive hil/p4_aec_image_measure.py "$@" \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
    --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
}
for AEC_VARIANT in normal demo; do
  AEC_IMAGE_OUT="$AEC_15CFIX2_RUN/image-reports/$AEC_VARIANT"
  mkdir -p "$AEC_IMAGE_OUT"
  aecpy hil/p4_aec_image_measure.py plan \
    --bundle "$AEC15CFIX2_BUNDLES/bundle-p4xc-aec-$AEC_VARIANT" \
    --recommendation "$AEC_RECOMMENDATION" --out "$AEC_IMAGE_OUT/plan.json" || exit $?
  for AEC_PART in 0 1 2 3; do
    fix2_measure gain --plan "$AEC_IMAGE_OUT/plan.json" --part "$AEC_PART" \
      --out "$AEC_IMAGE_OUT/grid/gain-30/part-$AEC_PART/attempt-01" || exit $?
  done
  aecpy hil/p4_aec_image_measure.py report-gain --plan "$AEC_IMAGE_OUT/plan.json" \
    --root "$AEC_IMAGE_OUT/grid" --out "$AEC_IMAGE_OUT/erle-report.json" || exit $?
done
for AEC_PROMPT in france speech; do
  fix2_measure double-talk --plan "$AEC_15CFIX2_RUN/image-reports/normal/plan.json" \
    --prompt "$AEC_PROMPT" --out "$AEC_15CFIX2_RUN/double-talk-$AEC_PROMPT/attempt-01" || exit $?
done
for AEC_VARIANT in normal demo; do
  for AEC_BOARD in echoear p4xc; do
    AEC_EXTRA=()
    if [ "$AEC_BOARD" = p4xc ]; then
      AEC_EXTRA=(--applied "$AEC_RECOMMENDATION" \
        --erle-report "$AEC_15CFIX2_RUN/image-reports/$AEC_VARIANT/erle-report.json")
    fi
    aeclive hil/p4_aec_product.py smoke --board "$AEC_BOARD" \
      --bundle "$AEC15CFIX2_BUNDLES/bundle-$AEC_BOARD-aec-$AEC_VARIANT" \
      --out "$AEC_15CFIX2_RUN/smoke-$AEC_BOARD-aec-$AEC_VARIANT" \
      --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
      --pair-id "step15c-fix2-aec-$AEC_VARIANT" --bounds hil/fixtures/wire_contract_bounds.json \
      --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" \
      "${AEC_EXTRA[@]}" || exit $?
  done
  aecpy hil/p4_aec_product.py pair "$AEC_15CFIX2_RUN/smoke-echoear-aec-$AEC_VARIANT" \
    "$AEC_15CFIX2_RUN/smoke-p4xc-aec-$AEC_VARIANT" \
    --out "$AEC_15CFIX2_RUN/pair-aec-$AEC_VARIANT.json" || exit $?
done
```

These are prepared coordinator commands. The writer does not acquire the
bench lock or execute them. S3's expected runtime recovery is still an
estimate until its new smoke measures the exercised minimum.

## Step 15c fix 4: report-only LLM answer text

The binding `decision-2026-11-02-smoke-llm-content-report-only` allows
`p4_aec_product.py smoke --llm-content report-only`. The default is `off`,
preserving the earlier strict behavior. The option continues after a missing,
off-script or negated answer only when the existing smoke STT token rule
passes and the bot completes its reply without self-interruption. It records
user and assistant text, the expected answer, findings and
`LLM content: report-only` in each affected turn and the result summary.
This also applies to a correctly transcribed Italy talk-over with an
off-script answer; "illness" instead of "Italy" still fails. Recording
deadlines, measured wire/uplink bounds, heap and task-stack floors, talk-over
interruption, all twelve main turns, demo resume/cold, held state, persisted
assets, cleanup and pinned restore remain required. Unmeasured steps fail.
The finding "count reply did not complete through twenty" is also an
LLM-text finding: it describes assistant text, not a device playback
completion failure. Bot speech completion and recording deadlines stay
required. Non-AEC manifests are refused before room capture or device use.
Pair output records each side's `llm_content_mode` and finding count; those
fields add provenance and do not change the comparator verdict.
This option does not enable calibration mode or change the demo bot.

Offline retained-evidence re-score (no bench access):

```bash
aecpy hil/p4_smoke_rescore.py "$AEC_RETAINED_SMOKE"
```

This writes a fresh `rescore.json` alongside `result.json`, refuses overwrite,
and retains the original result SHA-256, evidence hashes, per-turn STT/LLM
text, expected answers, findings, missing steps and verdict. Its exit code is
1 for FAIL. Re-scoring cannot fill later steps skipped by an early content
failure. A failed-session recorder can establish that the reply completed,
but cleanup captures cannot stand in for the final scripted session gates.
Historical re-scores are diagnostic; the pair command still requires clean
original acquisitions. All five requested retained content attempts remain
FAIL because later steps or session gates were unmeasured. Fresh acquisition
is required.

Coordinator handoff: obtain Fable and Opus review before bench use, as the
binding decision requires. Using the existing `aecpy`/`aeclive` functions,
re-run the P4 demo in a fresh directory, with the **same unchanged demo bot
identity** as the successful fix-2 S3 demo and the same-image fix-2 demo ERLE
report. Keep the existing authenticated normal AEC-off restore policy. These
commands are prepared only; the writer does not execute them.

```bash
export AEC15CFIX2="$HOME/src/evidence/echoear/p4/step-15c-fix-2"
export AEC15CFIX2_RUN="$AEC15CFIX2/coordinator-run-1"
export AEC_P4_DEMO="$AEC15CFIX2/final-bundles/bundle-p4xc-aec-demo"
export AEC_RECOMMENDATION="$HOME/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
: "${AEC_15CFIX4_RUN:?fresh output parent for fix-4 coordinator acquisition}"
: "${BOT_DIR:?the unchanged demo bot directory used by the S3 demo}"
: "${BOT_IDENTITY:?verified identity of that same demo bot process}"
mkdir -p "$AEC_15CFIX4_RUN"
aeclive hil/p4_aec_product.py smoke --board p4xc --bundle "$AEC_P4_DEMO" \
  --llm-content report-only --applied "$AEC_RECOMMENDATION" \
  --erle-report "$AEC15CFIX2_RUN/image-reports/demo/erle-report.json" \
  --out "$AEC_15CFIX4_RUN/smoke-p4xc-aec-demo" \
  --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
  --pair-id step15c-fix2-aec-demo --bounds hil/fixtures/wire_contract_bounds.json \
  --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" || exit $?
aecpy hil/p4_aec_product.py pair "$AEC15CFIX2_RUN/smoke-echoear-aec-demo" \
  "$AEC_15CFIX4_RUN/smoke-p4xc-aec-demo" \
  --out "$AEC_15CFIX4_RUN/pair-aec-demo.json" || exit $?
```

If that demo bot process/identity has changed, reacquire **both** S3 and P4
demo smokes under one unchanged identity and pair id. Use the fix-2 S3 demo
bundle, the same `--llm-content report-only` option and a fresh S3 output;
point the pair's left input to it. The comparator rejects mismatched bot
identities. Budget 10–16 minutes per demo smoke. Keep attempts 01–03 and
their new diagnostic re-scores immutable.

## Step 15d: word-local near-end localization

Diagnosis only, under `decision-2026-11-02-step-15d-scope`. No firmware,
reference gain, guard or bot changes. Use the **normal fix-2** S3
`015efd59fe71…` and P4 `2831d8bd295e…` bundles. The adapter refuses other
ELFs. The static comparison and ranked hypotheses are in the writer report's
Step 15d. Delay remains **decision-backed**, gain **selected-single-trial**,
and guard **provisional**. The longer-delay, guard-boundary and repeated
France validation are deferred by the binding scope decision.

The frozen smoke `italy.wav` (`cdffc54c…`) is the sole stimulus; the B trigger
is the existing double-talk tool's frozen `long.wav` (count one to twenty).
A starts with a silent bot and plays Italy through the unchanged Jabra.
B requests Italy at bot-start +3 s. Each repetition uses a fresh peer/session,
with the AFE kept running within a part. Four fresh-boot parts per board each
contain five randomized A/B pairs: **20 repetitions per arm per board, 80
total**. The checked plan stores every ordered ID, independent board seeds
151500/151501, bot identity, bundle/fixture/helper hashes and analysis rules.
Interleave boards by part: S3 part 0, P4 part 0, S3 part 1, P4 part 1, and so
on. Earlier-part requirements apply **per board**, so later S3 parts may be
unstarted when a P4 part runs. Keep each board's placement relative to the
Jabra and room conditions fixed; record both placements before beginning.

Each repetition retains all four raw mic slots, MIC3 electrical reference,
processed AEC output, capture-clock anchors and dump checksums, before/after
PERF, the final bot transcript, full decoded uplink/tts WAVs and recorder
JSONL. The latter keeps sample ranges and monotonic arrival timestamps.
**RTP sequence numbers and missing-packet counts are unavailable** in this
unchanged bot. No server change is permitted. Arrival gaps and contiguous WAV
storage do not prove packet loss or its absence. The report keeps
`missing_packets_word=null`; timing suspicions remain distinct from loss.
Optional C and France are omitted to keep this one measurement bounded.

Analysis is predeclared in `p4_nearend_analysis.py`: causal fourth-order
300–3500 Hz bandpass; source-only 20 ms activity blocks above −30 dB relative
power; fixed phrase/Italy split at source sample 12480 (0.780 s). Raw matching
uses normalized waveform correlation ≥0.10, envelope correlation ≥0.65 and
an isolated peak (≥0.02 above rivals outside 20 ms). Windows at or below
10⁻¹² of maximum target-window power cannot supply a correlation peak: this
prevents FFT/cumulative-sum roundoff in exact silence from winning alignment.
AEC uses the unchanged
1024-sample decision-backed delay, never a delay chosen to maximize transfer.
Decoded uplink is independently aligned to the actual processed waveform
(correlation ≥0.60, same envelope/isolation checks). Weak, ambiguous,
boundary or truncated matches remain unavailable for the affected stage.
Raw and AEC evidence are independently valid; a missing delayed tail does
not erase a complete raw window. Physical onset bounds come from the
retained capture-clock anchors; serial receipt and WAV indexes are not clocks.
A requires **mean 300–3500 Hz MIC3 power over the concatenated source-active
phrase and word samples ≤−60 dBFS**, and no bot onset through the source's
final active sample. This is a band-limited mean, not proof of zero echo or
silence at every sample. The report retains full-band and band-limited mean
levels, all source-active 20 ms MIC3 block levels and their maxima as
diagnostics; these do not silently strengthen the declared mean gate.
B requires a +3 s request within ±250 ms and a physical near onset
bracketed inside the first bot speech interval. A failed control is retained
with an explicit exclusion reason; it is never retaken.

Each stage enters the row only after its own evidence validates. Uplink
requires a hash-checked finalized WAV, waveform alignment and complete
phrase/word arrival witnesses. Missing arrivals exclude uplink, while valid
raw/AEC remain usable. `stage_validity` is enforced again by the summarizer.
Recognition has a separate validity path: authenticated finalized transcripts
produce `stt_observed` hits/misses before level analysis. Italy, illness and
an absent transcript remain observed outcomes even with zero AEC output.
`stt` includes control-valid observations; `stt_observed` also shows outcomes
excluded by a failed/unknown control, with `stt_control_excluded` reasons.
An aborted/unretained original is unobserved. A level-analysis failure alone
cannot reduce the STT denominator.

The report gives per-board/arm phrase and word levels, AEC−raw and uplink−AEC,
paired B−A AEC−raw, paired **AEC_B−AEC_A** (`double_talk_output`), and
word−phrase at every stage. The original `double_talk` metric is labelled
**"includes echo-removal confound"**: raw MIC1 in B includes echo, so extra
echo removal can make it more negative without attenuating near speech.
The separate `double_talk_output` stage compares AEC output across the same
declared A/B block, without subtracting raw echo. B output still contains
residual echo; its label is **"includes residual-echo confound"**. Extra S3
residual echo can produce a false relative P4 deficit even when P4 preserves
its near speech exactly. Both are reported as **mixture differences**, not
near-end localization by themselves. Both
metrics keep the same attribution rule, per phrase and word, under
`decision-2026-11-02-15d-double-talk-output-metric`, made before acquisition.

Under `decision-2026-11-02-15d-residual-echo-claim`, each B repetition also
estimates residual power separately for the phrase/word and AEC/uplink.
Use that repetition's own bot-speech capture before the physical source-active
onset, never another trial's noise floor. The window ends no later than
**40 ms before the earliest anchored onset bound**, and is as long as the
segment's source-active sample count. If less is available, use the longest
contiguous suffix, at least **100 ms**. Every included raw block must have
physical bounds within bot speech and before the cutoff; no clock
extrapolation or gap bridging. AEC indices shift by the adopted 1024-sample
delay. The uplink residual window is independently matched to that actual
AEC prefix and requires its own arrival witnesses; quiet/unmatchable
prefixes leave the uplink correction unavailable rather than extrapolating
WAV indexes or treating receiver arrivals as a physical clock.

For each stage/segment retain residual power **R**, B lexical power **P**,
the actual window and clock/alignment provenance. Compute **N = P − R** in
linear power. It is valid only for **P ≥ 2R** and positive N; equality at 2R
passes. Pair each valid B estimate with its own block's A level:
**`double_talk_output_corrected` = 10·log10(N_B / P_A)**. AEC uses
`double_talk_output_corrected/<segment>`; uplink uses
`double_talk_output_corrected/uplink/<segment>`. Both enter the same
≥2 dB/strict-nonoverlap comparison rule. Unavailable corrections retain
reasons and do not become zero; their valid-pair counts are separate from
the original stage completeness counts.

The double-talk near-end predicate requires a corrected P4 deficit, an
output-mixture deficit in the same direction and **at least 15 valid
corrected pairs per board** for that stage/segment. It conservatively also
requires P4's own corrected B−A median ≤−2 dB with its 95% interval entirely
below zero. This extra within-board guard rejects the reviewer's example
where S3 residual echo switches on with near speech: a quiet pre-window
cannot correct it, but P4 has no loss relative to its own A. The three
decision conditions alone cannot reject that nonstationary counterexample.
This guard adds no acquisition or firmware change. Confirmed claims remain
conditional on the assumptions below, not an oracle source separation.
Otherwise report **"mixture difference observed; near-end localization
unresolved"** when a mixture crosses the rule, with all observed values.

Medians use a fixed-seed
10,000-resample percentile bootstrap 95% interval. STT uses the whole token
“Italy”, with two-sided Clopper–Pearson intervals, exact under its binomial
model. These individual intervals assume exchangeable independent trials,
including within a part although the AFE runs continuously in only four
boot parts. They do not adjust for within-part dependence or multiple tested
stages, and are not experiment-wide 95% causal coverage. Inspect retained
part IDs for clustering before treating precision as robust.
The correction assumes roughly stationary residual echo from the pre-onset
window through the lexical segment, and addition of near and echo in power
(negligible cross term). A quiet prefix cannot prove absence of residual
echo that starts or changes with near speech. Alignment still assumes the
adopted delay and does not establish invariance to arbitrary delay errors or
time compression.
The declared attribution rule requires an absolute P4−S3 contribution
≥2 dB and **strictly nonoverlapping** board intervals. The report separately
marks a negative difference as a deficit; a positive difference is not a
P4 attenuation explanation. Positive rule crossings enter `differences`;
negative crossings enter `deficits`. Only supported negative contributions
enter `localized_deficits` and `localization`. The legacy `responsible`
list means only an absolute metric-rule crossing. B mixture-derived metrics
are kept in `mixture_differences`; they cannot localize near speech alone.
`complete=false` discloses any
missing repetitions, stage values or STT. Partial reports are provisional.
Any acoustic attribution is labelled **"acoustic, including placement"**:
the raw level includes board/Jabra geometry as well as mic/analog transfer.
STT rates here are directly comparable with the prior double-talk/sweep
path, which also requests near speech at **bot-start +3 s**. The product
smoke requests it at **user-transcript +3 s**; comparison with its historical
9/11 STT hit rate is indirect, not a timing-matched replication.
Arm B stage levels include residual echo; these are mixture-transfer
measurements, not source-separated double-talk near-end power. Uplink−AEC
also includes the existing HPF, digital gain, limiter and gate before Opus;
a difference there alone cannot assign blame specifically to codec/network.

Budget **12–20 minutes per 10-trial part**, **96–160 minutes for eight parts**,
plus **10–15 minutes** for board change and final offline analysis: roughly
**1.8–2.9 hours**, excluding noisy-room deferrals or recovery holds. Each raw
B capture remains bounded at 26 s; frozen drain/dump/restore timeouts remain
unchanged. Estimate, not a runtime acceptance bound. If the first part exceeds
20 minutes, stop and report projected duration. Propose **10 repetitions per
arm** (about 50–90 minutes) for a new user decision; do not truncate or edit
the existing 20-repetition plan after seeing results.

Coordinator only: use the existing `aecpy`/`aeclive` shell functions above;
`aeclive` invokes `hil/bench.py` for runner-owned locking. The writer runs
none of these live commands. The bot must already have session recording
configured. Keep one unchanged bot process/identity for the whole experiment;
if it changes, stop and retain the interrupted plan rather than mixing bots.

```bash
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export STEP15D_OUT="$HOME/src/evidence/echoear/p4/step-15d/coordinator-run-1"
export STEP15D_BUNDLES="$HOME/src/evidence/echoear/p4/step-15c-fix-2/final-bundles"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
: "${BOT_DIR:?unchanged running demo bot recorder directory}"
: "${BOT_IDENTITY:?verified identity JSON for that bot process}"
mkdir -p "$STEP15D_OUT"
aecpy hil/p4_nearend_capture.py plan \
  --s3-bundle "$STEP15D_BUNDLES/bundle-echoear-aec-normal" \
  --p4-bundle "$STEP15D_BUNDLES/bundle-p4xc-aec-normal" \
  --bot-identity "$BOT_IDENTITY" --restore-record "$RESTORE_RECORD" \
  --restore-record-sha256 "$RESTORE_RECORD_SHA256" --out "$STEP15D_OUT/plan.json" || exit $?
```

The coordinator must generate a fresh plan after the final fix-2 source changes
and before part 0; any earlier source-bound plan is stale. If its output
already exists, choose a fresh `STEP15D_OUT` parent and retain the old plan.
Read and retain the new plan before acquisition. No gain setter is used: S3
reference 15 dB and P4 reference 30 dB remain their boot values; MIC1 analog
30 dB, digital 125%, playback requested −6 dB and guard/rearm 400/2000 ms
remain unchanged. S3 restores its frozen `55152f25…` normal AEC-off image;
P4 restores the authenticated R7 pin, including named storage erase/readback.
Candidate/cleanup/abort/restore failures stay inside inherited signal protection
and recovery holds; explicit factory recovery is never a clean R7 result.

```bash
# First pass. Stop at any nonzero exit, preserving the current board/part.
for NEAR_PART in 0 1 2 3; do
  for NEAR_BOARD in echoear p4xc; do
    aeclive hil/p4_nearend_capture.py capture --plan "$STEP15D_OUT/plan.json" \
      --board "$NEAR_BOARD" --part "$NEAR_PART" \
      --bot-dir "$BOT_DIR" --bot-repo "$HOME/src/dg-esp32" \
      --out "$STEP15D_OUT/$NEAR_BOARD/part-$NEAR_PART/attempt-01" || exit $?
  done
done
```

Exit 2 means noisy preflight, unscored and device untouched. Retry that part
later using the next `attempt-NN`, keeping the failed directory. Other errors
also retain all originals. A `trial-start.json` consumes that repetition
**before** session/stimulus work; crashes, bad audio, STT misses and interrupted
cleanup cannot create a replacement. A resumed part runs only the unstarted
suffix, in the original order, and requires earlier parts **of that same
board** to be consumed. Other boards' later parts may remain unstarted.
A fully consumed part refuses another capture even if some originals have
unavailable evidence. Never delete journals or rename board/part directories.

```bash
# Example: stopped at echoear part 0; attempt-01 was unscored or partial.
aeclive hil/p4_nearend_capture.py capture --plan "$STEP15D_OUT/plan.json" \
  --board echoear --part 0 --bot-dir "$BOT_DIR" --bot-repo "$HOME/src/dg-esp32" \
  --out "$STEP15D_OUT/echoear/part-0/attempt-02"
# Then continue the remaining parts with their original board/part numbers.
# Offline (<1 minute); output is exclusive-create, so keep every report.
aecpy hil/p4_nearend_capture.py report --plan "$STEP15D_OUT/plan.json" \
  --root "$STEP15D_OUT" --out "$STEP15D_OUT/word-local-report-01.json"
```

Report exits 1 for incomplete data, independently of whether a stage crosses
the attribution rule. Inspect retained unavailable reasons, exact STT counts,
levels and intervals; do not describe an incomplete report as localized
acceptance. Propose any firmware/tuning fix with this evidence for the user's
decision. No 15b validation claim follows from this diagnostic.

### Versioned offline re-analysis after acquisition

After acquisition is complete and the coordinator says **bench free**, the
separate tool below can apply the approved arm-B alignment fallback to a new
report. Keep the acquisition plan, journals and original report unchanged.
Use the same `aecpy` environment above, with an output outside `STEP15D_OUT`;
an existing output is refused.

```bash
mkdir -p "$HOME/src/evidence/echoear/p4/step-15d/reanalysis-v1"
aecpy hil/p4_nearend_reanalysis.py \
  --plan "$STEP15D_OUT/plan.json" --root "$STEP15D_OUT" \
  --original-report "$STEP15D_OUT/word-local-report-01.json" \
  --out "$HOME/src/evidence/echoear/p4/step-15d/reanalysis-v1/reanalysis-v1.json"
```

Only a failed raw match in B permits an AEC match minus the adopted 1024-sample
delay. Arm A, successful raw matches, thresholds and attribution rules remain
unchanged. The report retains each alignment method, original-report and
input digests, and unavailable observations. Exit 1 still means incomplete
data; retain that report. S3 fallback trials are selected on surviving near
speech; cross-board arm-B deficits are selection-biased upward. Same-method
sensitivity uses each board's own pairs and needs at least 15 pairs per board
for each comparison; otherwise it says **method comparison unavailable
(insufficient same-method pairs)**. Third-octave arm-A raw MIC1 spectra are
report-only diagnostics and cannot distinguish microphone, analog circuit or
placement causes. These commands are coordinator instructions, not permission
to rerun acquisition or re-analysis during a writer-only task.

## Step 15d validation

This follow-up validates the provisional 15b records after the word-local
localization. Its writer delivers host tools/tests only. The coordinator runs
the commands below after quiet-room go. Keep the P4 and Jabra in their existing
positions: the localized raw near-end weakness, including placement, remains
part of every reported double-talk result. No firmware, bot, gain, delay,
guard or re-arm change is part of this validation. No gain re-selection.

Use only the fix-2 **P4 AEC normal** bundle, ELF
`2831d8bd295eaeea21c603f6aaff11a8d42ffe5a7840c96fd12331cadf7665da`,
and the existing R7 normal AEC-off restore record (SHA256
`730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a`).
`p4_aec_validation_capture.py plan` authenticates the bundle/restore, fixtures,
bot identity, rules and the explicit runtime Python import set (`SOURCES`).
Unrelated helpers and test files are not hashed. `ValidationIO` checks
boot guard/delay/re-arm 400/1024/2000 and MIC1/MIC3 readbacks through the
existing diagnostic adapter. It never calls `configure()` or a gain/guard
setter. The normal preparation still requests the unchanged −6 dB playback
volume and auto gate. Fresh boot per part, fresh peer per double-talk trial.

Coordinator preflight: obtain quiet-room go; retain placement, levels and
one unchanged running demo bot identity/profile throughout. The existing
recorder/event logging must already be configured; do not change or restart
the bot to resume a plan. Keep the host idle and release the build lock.
Every part runs `alsa.record_room_baseline`: 3 s at 16 kHz, first 200 ms
excluded, with the frozen room-level gate and Jabra mixer readbacks. Noisy
preflight is unscored, device untouched, exit 2. Retry later with a new
attempt number. No extra listening judgement or ad hoc noise threshold.
The runner-owned bench wrapper and inherited signal-safe storage cleanup,
R7 restore and recovery holds are mandatory. Never take an outer bench flock.

Use the `aecpy` and `aeclive` shell functions from this run book; the first
is offline Python, the second invokes `hil/bench.py`. Commands here are
coordinator instructions, not writer permission to use the bench.

```bash
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export VALID15D="$HOME/src/evidence/echoear/p4/step-15d-validation/coordinator-run-1"
export VALID15D_BUNDLE="$HOME/src/evidence/echoear/p4/step-15c-fix-2/final-bundles/bundle-p4xc-aec-normal"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
: "${BOT_DIR:?existing unchanged demo bot directory}"
: "${BOT_IDENTITY:?verified JSON identity of that process}"
mkdir -p "$VALID15D"
aecpy hil/p4_aec_validation_capture.py plan \
  --bundle "$VALID15D_BUNDLE" --root "$VALID15D" \
  --bot-identity "$BOT_IDENTITY" --restore-record "$RESTORE_RECORD" || exit $?
```

The plan must be named `plan.json` within its declared evidence root. Do
not edit it after acquisition, change bound runtime sources, or move its input
files. The coordinator decision also freezes all worktree edits from plan
creation through the final report; narrowing hashes does not lift that hold. An existing plan is refused. Retain an abandoned plan separately;
a new plan must not become a way to replace observed failures.

### V1: longer independent delay captures

Three parts, one repetition each. Each starts slots capture 750 ms before
the probe, waits for `DONE #… aec-probe` (the earlier ACK is only entry),
and keeps 1,250 ms after completion. After 2,200 ms electrical silence, the
independent near-only capture keeps the same pre/post margins around the
frozen speech fixture. The DAC capture should be about 4.4 s; near duration
is the complete speech fixture plus at least 2 s. Offline replay requires
full template length + 4,096 samples + 16,000 samples of margin in each
capture, checked dumps/anchors, and the unchanged independent S3 method.
The known-source matches, ≥10 dB near excitation, silent reference,
reference contrast and clip gates still apply. A longer capture cannot waive
the previous ambiguous Jabra match: if it still fails, report unavailable.
The pre-acquisition decision `decision-2026-11-03-15d-validation-run.md`
chooses to run V1 despite an **expected unavailable** result: the retained
Jabra→MIC1 peak was r=0.539 against a rival 0.520. Longer capture fixes
coverage, not that ambiguity. Do not relax this gate; delay stays decision-backed
unless the unchanged declared method confirms it.
Delay correlation ≥0.6, separation ≥0.05, search 0–4096 samples, interior
peak, independent of ERLE. Diagnostic endpoint peaks do not pass.

```bash
for VALID_PART in delay-0 delay-1 delay-2; do
  aeclive hil/p4_aec_validation_capture.py capture \
    --plan "$VALID15D/plan.json" --part "$VALID_PART" --attempt 1 \
    --bot-dir "$BOT_DIR" --bot-repo "$HOME/src/dg-esp32" || exit $?
done
# Offline; exit 1 is expected for missing V2/V3 at this stage.
aecpy hil/p4_aec_validation_capture.py report --plan "$VALID15D/plan.json" \
  --out "$VALID15D/report-after-delay.json"
```

Each successful delay part writes `delay-summary.json` and prints its outcome.
An unstarted third repetition leaves the outcome unavailable; no early
confirmation or stop is inferred from the first two alone. The final result
is the median of passing repetitions, requiring at least 2 of 3.
Median within **±16 samples of 1024**, inclusive: `measured-confirmed`.
Passing median elsewhere: `measured-different`, exit **4**, **stop for the
user's decision**; the tool refuses subsequent double-talk acquisition.
Fewer than two passing repetitions: `unavailable`; retain all failures.
A median of two observations can be half a sample; it is not rounded into
acceptance. V1 validates the unchanged implementation delay; it never
rewrites the decision-backed original, bundle calibration labels or firmware.
If V1 is unavailable, V3 may proceed as a report using the explicitly
labelled adopted 1024-sample delay. Three consumed delay repetitions are
required before V3, including unavailable started repetitions.

### V2: onset suppression and the re-arm boundary — partially available

**V2 is partially available.** In-guard and outside-guard are feasible with
existing hooks; the 1,800/2,200 ms re-arm boundary needs new tooling.
Per `decision-2026-11-03-15d-validation-run.md`, V2 is a separate follow-up,
not implemented or run in this V1/V3 task. No physical guard pass follows
from the current plan/report's unavailable, unexecuted V2 assessment.

For in-guard, start a steady non-speech Jabra fixture before the bot's frozen
long-prompt reply and hold it across the device `AEC_GUARD` onset. For
outside-guard, use the existing live onset-trigger mechanism with a ≥600 ms
offset, verifying physical arrival offline. The unchanged demo recorder's
`uplink.wav` is the received **post-gate** PCM witness. Align it with the AEC
output using the existing word-local tools and per-frame arrival evidence,
then compare raw MIC1/MIC3/AEC and uplink suppression/reopening. A small
follow-up must supply the steady fixture and suppression scorer, with
±(20 ms + measured alignment uncertainty), for **2 × 5 trials** (~25 min).
These two conditions do not need a new fixture-play handler or WebRTC peer.

The five-channel slots dump alone is pre-gate, and the decimated `MIC_GATE`
logs alone cannot establish a 20 ms boundary. `GATE` source-time transitions
and `AEC_GUARD` onset/deadline logs are useful corroboration. The recorded
uplink supplies the independent post-gate witness missing from those logs.

For the re-arm boundary, ordinary demo replies cannot prescribe accepted
playback silence gaps. Direct tones/probes reject active media and the demo
bot lacks fixture playback control. Jabra request timestamps or a host sleep
cannot substitute for accepted device playback end/onset times. This gap
still needs controlled-gap tooling, such as a standalone host fixture peer.

Predeclare **N=5 per condition**, with every repetition required to match:

| Condition | Scheduled physical stimulus | Expected witness |
| --- | --- | --- |
| In guard | Steady near-end begins ≥200 ms before an armed playback onset and stays active through ≥800 ms afterwards | Raw near-end nonzero throughout; post-gate near-end suppressed from onset to onset+400 ms, then present |
| Outside guard | Near-end begins ≥600 ms after measured playback onset | Near-end present; no guard suppression |
| Gap 1800 | Two accepted playback bursts separated by 1,800 ms silence, near-end steady across second onset | No new guard; near-end remains present |
| Gap 2200 | Same with 2,200 ms silence | New guard with +400 ms deadline, suppressed near-end then reopening |

Anchor all windows in device accepted playback/source time. Bound actual
silence intervals from raw MIC3/reference and console accepted-end evidence
where available, including the reference noise floor/threshold uncertainty;
do not infer silence from a host sleep. Require each actual gap interval
to contain its target within ±20 ms and lie wholly below or above 2,000 ms
as appropriate. Match suppression/reopening to **±(20 ms + measured
alignment uncertainty)**; if alignment or source isolation is unavailable,
the repetition is unavailable, never passed. Post-gate near-end presence
must be source-specific, not merely nonzero echo. Preserve raw/reference/AEC,
uplink PCM, clock anchors, source fixture hashes and onset/gate logs. Count
a genuine mismatch as a mismatch and retain it without retrying to pass.
Bot detection after +2.4 s alone does not establish any of these conditions.

Smallest re-arm alternative proposed: a standalone host fixture WebRTC
peer in new HIL modules, sending two controlled far-end bursts and recording
the returned uplink, with unchanged firmware/server. Establish actual gap
and clock/source bounds before the boundary trials. This peer is needed for
the re-arm boundary only, not the two onset-suppression conditions. No V2
acquisition or suppression scorer is implemented by this fix; retain the
four declared N=5 expectations for their follow-up tasks. Do not replace
physical timing with an event-only pass.

### V3: repeated 30 dB double talk, report only

Three bounded parts (sets), each containing five France and five speech
trials, mixed in pairs with first-prompt order alternating across pairs and
sets. Each trial starts a new peer/session, takes a quiet reference, and
reuses the frozen long-reply trigger and bot-start+3 s near-injection method.
The capture stays bounded at 26 s; missing STT is a retained miss. Host
request overlap is reported as such, not as independently measured physical
speech overlap. No comparison gains, selection or threshold retuning.

```bash
for VALID_PART in double-talk-0 double-talk-1 double-talk-2; do
  aeclive hil/p4_aec_validation_capture.py capture \
    --plan "$VALID15D/plan.json" --part "$VALID_PART" --attempt 1 \
    --bot-dir "$BOT_DIR" --bot-repo "$HOME/src/dg-esp32" || exit $?
done
aecpy hil/p4_aec_validation_capture.py report --plan "$VALID15D/plan.json" \
  --out "$VALID15D/validation-report-01.json"
```

Each report replays checked raw dumps/NPZ/anchors and retained original
events, validates trial seals, and recomputes per-trial `reply_erle` using
1024 and that trial's quiet MIC3 reference. It reports the numerical ERLE
separately from single-long-turn acceptance; double-talk naturally contains
extra user turns. ERLE includes the double-talk mixture on echo-active
frames; it is not isolated echo power. Gain and clip checks are retained.
Interruption requires a DOWNSTREAM bot-active interruption between injection
request and the first bot stop; mirrored UPSTREAM frames never double-count.
Per trial retain normalized word accuracy, transcript, exact normalized word
match, ≥90% word-accuracy hit, interruption, ERLE and clip evidence.

For each set and pooled prompt (planned n=5 and n=15), report interruption
and STT trial-hit rates with **two-sided 95% exact Clopper–Pearson intervals**.
The mean WER-derived word accuracy is descriptive; a continuous mean does
not have an exact binomial interval. The exact intervals apply to trial
hits (≥90% accuracy and exact normalized word match), not to independent
words. These are individual intervals under an independent identical-trial
binomial model, not simultaneous or adjusted for within-set dependence,
fixed placement or timing uncertainty. Unavailable acquisitions stay in the
planned count, outside the observed-rate denominator; missing STT after a
valid injection is an observed zero. Inspect both denominators and every
unavailable reason before interpreting the rate.
Every prompt/set summary also reports `missed_injections` and
`sensitivity_missed_injections_as_failures`: the same hit counts divided by
observed trials plus authenticated missed injections, with exact intervals.
Those missed injections count as failures in the sensitivity line because
a bot already stopped at +3 s may reflect a self-interruption. Other unavailable
acquisitions remain explicitly excluded from that sensitivity denominator;
they are not silently called observed STT misses. A missing playback request or a request outside the first bot reply window
is a missed injection. Invalid duration with an otherwise in-window request
remains separately unavailable.

### Resume, exits and the 2.5 hour limit

A `trial-start.json` consumes a repetition **before** any trial quiet capture,
connect or stimulus. A process crash, missing capture, mismatch, missed
injection, failed cleanup/restore or STT miss cannot create a replacement.
Every sealed original uses exclusive creation and bound hashes; a journal
without a final seal remains consumed/unavailable. Replay additionally requires
`part-finalized.json`, written only after the frozen holder returns, binding
the canonical plan, board/image/part, final result/scored original, body
payload, room WAV/result/mixer evidence and trial seals. The result must be
acquired without error or cleanup failures, with clean room preflight and an
authenticated clean pinned `p4-product-of-record` restore. Each trial must be
`status=retained` with no error; V3 reaches that state only after `finish()`.

**Whole-attempt success is required.** Successfully completed prefix trials
from an attempt that later fails are ineligible, even if it restores cleanly.
They stay consumed and cannot be replaced; only the unstarted suffix can
resume. Failed, missing, unclean or factory restore/result evidence leaves
all observations of that attempt unavailable. This same centralized gate is
used by report replay, V1 summaries and the measured-different/V3 admission
check; retaining capture JSON alone never admits a score.

Resume only the unstarted
suffix of the same part in its original order, using a fresh `attempt-NN`.
Earlier parts must be fully consumed; no skipped/reordered sets. If a
one-repetition delay part fails after its journal is written, that part
stays consumed/unavailable: recover, report it, and continue with the next
declared delay part rather than requesting a replacement. A fully
consumed part refuses another attempt. Never delete or rename journals.

```bash
# Example: double-talk set 1 was interrupted after a started trial.
aeclive hil/p4_aec_validation_capture.py capture \
  --plan "$VALID15D/plan.json" --part double-talk-0 --attempt 2 \
  --bot-dir "$BOT_DIR" --bot-repo "$HOME/src/dg-esp32"
# Offline partial reports are always allowed; choose a new output each time.
aecpy hil/p4_aec_validation_capture.py report --plan "$VALID15D/plan.json" \
  --out "$VALID15D/validation-report-partial-02.json"
```

Capture exits: **0** acquired part (not a performance pass); **2** noisy
untouched preflight; **3** acquisition/cleanup/restore error; **4** measured
different delay. Validation/provenance refusals also exit nonzero. Stop on
any capture nonzero; inspect retained evidence and complete R7 recovery
before resuming any suffix. Report writes its output then exits **1** while
V2 is unexecuted and not physically validated (expected even with complete V1/V3), or **4** for a measured
different delay. Reports never declare full validation complete or mutate
historical labels. All outputs stay outside git; no commit until instructed.

Bench allocation (including normal flash/dump/restore): **5 min** setup,
**18 min** for three delay parts (≤6 min each), **75 min** for three double-talk
sets (≤25 min each), **7 min** final recovery/report, plus **20 min** contingency:
**125 min**, below **150 min / 2.5 h**. Reserve the remaining **25 min** for
the deferred two-condition V2 onset follow-up after its fixture/scorer is
supplied and reviewed. V2 is not part of this V1/V3 run; its re-arm boundary
remains unavailable pending controlled-gap tooling.
Offline report typically <1 min (budget 5 min).
Use a wall-clock timer from the first preflight. If a part exceeds its
allocation, stop before starting the next part and project the remaining
cost; do not spend recovery reserve on more trials. Start no new part after
125 elapsed minutes; allow 25 min for safe cleanup/restore. Recovery safety
always takes priority over the time estimate. No noisy-room/recovery retry
is promised to fit the budget.
The first cut is V2 (deferred by decision), then unstarted V3 sets
(report the resulting reduced n and incomplete planned counts). Never cut
individual observed failures, reduce the predeclared denominator, or claim
three sets from a partial run. V1 requires all three attempts to close its
predeclared label; an unfinished V1 stays unavailable. Budget cuts leave
validation incomplete rather than changing the pass rule.
