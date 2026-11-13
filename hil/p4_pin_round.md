# Step 15 pin round and step 16 handoff

The closing authority is
`proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-close-15-and-pin.md`
in the main repository. Delay 1024 is **measured-diagnostic**, bound to that
decision and the three V1 diagnostic correlations. Its near-end locator waiver
applies only to this value. Gain 30 dB remains **selected-single-trial**;
guard 400/2000 ms remains **provisional**. Both carry
"provisional: bench speaker not final hardware". Double-talk and near-end level
findings carry the same note. Re-measure those acoustic findings on final
speaker hardware. V2/re-arm work is parked on `p4-v2-parked`, outside `p4-port`.

All commands below are coordinator-only except offline `image plan` and
`image report-gain`. The writer builds/tests/pins without serial or bench
access. Use an already-running unchanged recorder-enabled product bot and
its verified identity, quiet-room go and the existing placement. Never wrap
these commands in an outer flock or hold the build lock during live work.

The writer creates immutable `commit-<full-sha>/restore-of-record.json` and
its separately retained `.sha256` file under
`~/src/evidence/echoear/p4/step-15-pin-round/`. `pin.env` selects that exact
commit's record, hash and final bundle directory; it contains public paths
and hashes only. Load it once and retain its hash with coordinator evidence.
This supersedes the step-14 restore record for step 16. Keep old evidence and
the historical command blocks in earlier run books unchanged. The new pin
has **no boot proof** until the first live command below passes.

Run from `/home/khkramer/src/dg-esp32-p4`:

```bash
set -euo pipefail
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
source "$HOME/src/evidence/echoear/p4/step-15-pin-round/pin.env"
sha256sum "$HOME/src/evidence/echoear/p4/step-15-pin-round/pin.env"
: "${BOT_DIR:?existing unchanged recorder-enabled demo bot directory}"
: "${BOT_IDENTITY:?verified identity JSON for that same process}"
export PIN15_RUN="$PIN15/coordinator-run-1"
mkdir -p "$PIN15_RUN"
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
# First prove the new pinned restore. Failure stops all following work.
aeclive hil/p4_step14.py boot-proof \
  --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" \
  --out "$PIN15_RUN/boot-proof"

# P4 AEC smoke requires authenticated ERLE evidence from this exact new ELF.
# Reacquire its four normal-image parts; old ELF reports cannot substitute.
export PIN15_RECOMMENDATION="$HOME/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json"
mkdir -p "$PIN15_RUN/p4-aec-report"
aecpy hil/p4_aec_close.py image plan \
  --bundle "$PIN15_BUNDLES/bundle-p4xc-aec-normal" \
  --recommendation "$PIN15_RECOMMENDATION" --out "$PIN15_RUN/p4-aec-report/plan.json"
for PIN15_PART in 0 1 2 3; do
  aeclive hil/p4_aec_close.py image gain --plan "$PIN15_RUN/p4-aec-report/plan.json" \
    --part "$PIN15_PART" --out "$PIN15_RUN/p4-aec-report/gain-30/part-$PIN15_PART/attempt-01" \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
    --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256"
done
aecpy hil/p4_aec_close.py image report-gain --plan "$PIN15_RUN/p4-aec-report/plan.json" \
  --root "$PIN15_RUN/p4-aec-report" --out "$PIN15_RUN/p4-aec-report/report.json"

# Exactly one normal AEC product smoke per board, same unchanged bot.
for PIN15_BOARD in echoear p4xc; do
  PIN15_EXTRA=()
  if [[ "$PIN15_BOARD" == p4xc ]]; then
    PIN15_EXTRA=(--applied "$PIN15_RECOMMENDATION" --erle-report "$PIN15_RUN/p4-aec-report/report.json")
  fi
  aeclive hil/p4_aec_close.py product smoke --board "$PIN15_BOARD" \
    --bundle "$PIN15_BUNDLES/bundle-$PIN15_BOARD-aec-normal" \
    --out "$PIN15_RUN/smoke-$PIN15_BOARD-aec-normal" --pair-id step15-pin-aec-normal \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32" \
    --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" \
    --llm-content report-only "${PIN15_EXTRA[@]}"
done
aecpy hil/p4_aec_close.py product pair "$PIN15_RUN/smoke-echoear-aec-normal" \
  "$PIN15_RUN/smoke-p4xc-aec-normal" --out "$PIN15_RUN/pair-aec-normal.json"

# Exactly one normal AEC-off product smoke per board.
for PIN15_BOARD in echoear p4xc; do
  aeclive hil/p4_step14.py run \
    --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" \
    smoke --board "$PIN15_BOARD" --bundle "$PIN15_BUNDLES/bundle-$PIN15_BOARD-normal" \
    --out "$PIN15_RUN/smoke-$PIN15_BOARD-normal" --pair-id step15-pin-normal \
    --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --bot-repo "$HOME/src/dg-esp32"
done
```

Stop on any failed boot/restore, unavailable ERLE report or failed smoke gate.
Retain all failed evidence; use a fresh output root for a later attempt. The
existing SIGUSR1/select-factory recovery hold in [p4_liveness.md](p4_liveness.md)
still governs failed restores. LLM content is report-only only for the AEC
smokes under the existing decision; STT, stack, timing and restoration gates
remain strict. A new build/pin or smoke does not promote the S3 qualification
images or claim qualification. Step 16 uses these bundles and this restore,
with `p4_step14.py run` for AEC-off/display/liveness entrypoints and
`p4_aec_close.py product` / `image` for the new AEC metadata.
