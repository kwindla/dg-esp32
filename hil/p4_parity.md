# Step 16 parity run

**Pin verification passed; host validation authorized.** The coordinator's
`step-15-pin-round/commit-52482495d1b859b5313cd8c76e4cd0cdd97ea507/coordinator-run-1`
contains the passed boot proof, P4 normal AEC ERLE report (**21.2176 dB**,
report-only), normal AEC smokes on both boards and their pair, and normal
AEC-off smokes on both boards. These unlock plan preparation; step-16 demo,
liveness, touch and display evidence remains pending. Pass host tests and
review the new adapters before live use.
Historical host validation (before fix 1) passed 46 focused tests under
`~/src/evidence/echoear/p4/step-16/host-validation-1/`. Fix-1 validation passed
**125 focused tests**, then the complete `scripts/host-tests.sh`: **1,993 pytest
and 715 CTest cases**, plus the standalone gates, with no skips. Those logs and the historical plan/table round-trip are retained under
`~/src/evidence/echoear/p4/step-16/fix-1/`. Fix-2 validation passed **230 focused
tests**, then **2,098 pytest and 715 CTest cases** plus standalone host gates,
with no skips. Its current logs, retained-restore positives and fresh offline
plan/table round-trip are under `~/src/evidence/echoear/p4/step-16/fix-2/`.
Fix-3 validation passed **271 focused tests**, then the full host script with
**2,139 pytest and 715 CTest cases**, plus standalone gates, with no skips.
Current fix-3 logs, four untouched candidate/restore replay positives and a
fresh offline plan/table round-trip are under
`~/src/evidence/echoear/p4/step-16/fix-3/`.
Earlier plans cannot use the changed tools; freeze a fresh plan before live
acquisition. No new step-16 live acquisition was performed during validation.
The writer never opens serial, takes the bench lock, starts a bot or flashes.
Keep all evidence outside git. Do not commit this work until told.

Work only in `~/src/dg-esp32-p4-s16`, branch `p4-step16`, from `5248249`.
Do not touch the coordinator's `~/src/dg-esp32-p4` worktree. Bench busy means
code/docs only; host builds/tests require an explicit bench-free go. The
coordinator owns serial, bench and bot lifecycle. Never run `server/tests`
while the service is live. Existing committed HIL Python and server source
stay frozen; add adapters. Do not commit until told, push or open PRs.

The [pre-declared run rules](../../dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-step16-run-rules.md)
are binding: both candidates gate internal minimum 81,920 B and largest
32,768 B. S3's pin minimum was 84,088 B; a floor failure is a real FAIL for a
user decision, with no later waiver. All six bot handoffs are attended.
Before each, have the next launcher and identity script ready. Missing the
180 s handoff cap is a scored failure, retained with no retake.

Use only the six step-15 images at commit
`52482495d1b859b5313cd8c76e4cd0cdd97ea507`: S3/P4 **demo**, **aec-demo**, and
**normal**. Demo profiles run in autonomous demo mode; normal runs manually.
AEC-normal is a pin-verification prerequisite, not a seventh parity image.
All live commands below inherit the runner-owned bench lock through `bench.py`;
never use an outer flock or hold the build lock during a live command. P4
always restores the newly pinned normal AEC-off image. S3 keeps the unchanged
`55152f25` restore policy. Use only the pinned by-id ports; no C6 writes.

The run is smoke parity, not qualification. Do not promote S3 images of record.
LLM answer content is report-only **only on AEC product smokes** under the
existing decision. STT, completed turns, 10 long replies, talk-over, stack,
heap, wire, cleanup and restore remain strict. AEC-off smokes keep their
existing content gates. Liveness timing is independently reported under the
step-14 policy; full product timing remains gated.

## Parts, budget and bot order

| Part | Evidence units | Estimated bench time | Resume boundary |
|---|---|---:|---|
| Pin verification | Existing boot proof and four normal smokes | Already coordinator-owned | All must pass before plan |
| A0 P4 AEC-demo ERLE | Four same-image report parts, if not already available | 16–40 min | Each clean restored part |
| A1 product | Three matching S3/P4 pairs; six full smokes | 48–90 min | Each restored smoke; keep the same bot alive |
| A2 physical touch | One real double-tap per board on demo pins | 4–8 min | Each restored board |
| B recovery | Six F18/F19 runs; F20 on four demo runs | 120–210 min | Each whole image run, including all 20 cycles |
| C display | Three pairs on six images, audio held off | 12–24 min | Each restored display capture |

Allow roughly 3.5–6.5 hours plus coordinator setup/recovery. These are scheduling
estimates, not relaxed acceptance or a reason to truncate cycles or idle time.
No new part starts when its full estimated time plus recovery reserve is
unavailable. A single liveness part includes 20 cycles, a **150 s** drain,
**360 s** held idle and reconnect; do not split or resume inside it. Existing
per-operation deadlines and the 180 s coordinator restart handoff stay bounded.
Interrupted parts remain incomplete and retained. Resume only at completed,
clean-restored boundaries, with frozen tools and the same plan.

Bot order is explicit:

1. One recorder-enabled, unchanged master `demo_bot.py` process for A0/A1 and
   all three product pairs, with one recorded identity. A2 can use it too.
   Do not enable stale probes during product smokes: product wire profile
   forbids those extra results. Freeze its process/profile in the plan.
2. After all product pairs, switch once to the observed fault-test demo bot
   with `--stale-probes`. Each B run deliberately requests exactly one bot
   replacement. Keep source and profile identical, record each process identity
   and reuse the last replacement as the next run's initial bot. Those fault
   processes are distinct from the one product-pair process. The observer only
   wraps the unchanged master bot; no server source is edited.
3. After B, switch once to unchanged `server/bot.py` for **all** C display pairs.
   Its identity is anchored at the first display run and must remain unchanged
   through all six captures. No further product run is mixed into this phase.

If the product bot exits before A1 completes, keep every original. Do not merge
new-process captures into that campaign. A later campaign needs its own plan
and root. A noisy unscored AEC room preflight can have **one** fresh-path retry
after quiet-room go, only if the device was untouched and no scored turn began.
All scored failures remain failures; no best-of selection or general retry
loop. C6 reset/inconclusive and missing observations remain explicit; a later
attempt does not erase them. Recovery follows [p4_liveness.md](p4_liveness.md),
including the retained SIGUSR1/select-factory hold. Never kill a restore hold
to meet a scheduling estimate.

## Host validation after bench-free go

Work only in `~/src/dg-esp32-p4-s16`, branch `p4-step16`. Do not change the
coordinator's `~/src/dg-esp32-p4` tree. The source-only changes do not rebuild
or replace the pins. Existing `hil/*.py` and all server/firmware sources stay
frozen; new modules carry the step-16 behavior.

```bash
cd "$HOME/src/dg-esp32-p4-s16"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
# Execute only AFTER explicit bench-free permission:
scripts/build_locked.sh bash -c '. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null && scripts/host-tests.sh'
git diff --check
```

The new liveness/display modules preserve the previous acquisition procedures
as new source files, removing old reference/profile restrictions for the exact
new pins. Both S3 and P4 are candidates: the old S3 reference-only resource
waiver does not apply. Manual liveness connects/disconnects explicitly and
does not claim F20. Demo still requires hold, resume, cold boot and persisted
assets. The display adapter holds demo/audio off, validates actual AEC intent
and records both product and bench-display wire profiles independently.

## Freeze plan after pin verification

Use a fresh external root. `PIN_VERIFY` below selects the completed coordinator
verification. The input receipt authenticates boot proof, all four normal
smokes, the same-image ERLE report and the AEC pair, including their retained
source hashes. The plan embeds those results and the exact six canonical
bundle manifests. It refuses the old restore and any dirty image.
The fixed restore record SHA256 is
`a2c846832b43636fc73564d6a1d32c22ebcbc95458c8906de0d0550f028d653b`.
The populated host input is
`~/src/evidence/echoear/p4/step-16/parity-inputs.json` (29 source bindings),
SHA256 `0748f42e67db3e50e0207f84fc8608d89da8c2fd4e42c59e4fda2f1c629dcb10`.
Plan creation re-authenticates the raw prerequisites and embeds these results.
Supply the identity of the actual product bot selected for the new campaign;
the recorded pin-run identity alone does not prove a bot is still running.

```bash
cd "$HOME/src/dg-esp32-p4-s16"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export S16="$HOME/src/evidence/echoear/p4/step-16/coordinator-run-1"
source "$HOME/src/evidence/echoear/p4/step-15-pin-round/pin.env"
export PIN_VERIFY="$PIN15/coordinator-run-1"
: "${PRODUCT_BOT_DIR:?one unchanged recorder-enabled master demo bot directory}"
: "${PRODUCT_IDENTITY:?verified identity JSON for that process}"
mkdir -p "$TMPDIR" "$S16"
paritypy() {
  uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
paritylive() {
  python3 hil/bench.py uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
paritypy hil/p4_parity.py pin-verification --root "$PIN_VERIFY" --out "$S16/pin-verification.json"
paritypy hil/p4_parity.py plan --boot-proof "$PIN_VERIFY/boot-proof/restore-boot-proof.json" \
  --pin-smokes "$PIN_VERIFY" --product-identity "$PRODUCT_IDENTITY" --root "$S16" --out "$S16/plan.json"
paritypy hil/p4_parity.py check --plan "$S16/plan.json"
```

Record the plan SHA256 and freeze all `p4_parity*.py` files through collection
and scoring. Each live runner checks that frozen source set and the images.
Host tests and offline input/generator work are now authorized. Live commands
remain coordinator-owned; no step-16 bench run is implied by a passing pin input.

## A0/A1: same-image ERLE, then six product smokes

If a same-image P4 AEC-demo ERLE report already exists on these pins, authenticate
it with the unchanged applied report reader under `p4_aec_close.adapters()`.
Otherwise acquire the four bounded report parts below. Never substitute the
old fix-2 report or the normal-image report. These report parts do not select
gain/guard/delay again. Keep the same product bot and placement throughout A.

```bash
export S16_REC="$HOME/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json"
mkdir -p "$S16/aec-demo-report"
paritypy hil/p4_aec_close.py image plan --bundle "$PIN15_BUNDLES/bundle-p4xc-aec-demo" \
  --recommendation "$S16_REC" --out "$S16/aec-demo-report/plan.json"
for S16_PART in 0 1 2 3; do
  paritylive hil/p4_parity_erle.py --plan "$S16/plan.json" \
    --image-plan "$S16/aec-demo-report/plan.json" \
    --part "$S16_PART" --out "$S16/aec-demo-report/gain-30/part-$S16_PART/attempt-01" \
    --bot-dir "$PRODUCT_BOT_DIR" --bot-identity "$PRODUCT_IDENTITY" \
    --bot-repo "$HOME/src/dg-esp32" || exit $?
done
paritypy hil/p4_aec_close.py image report-gain --plan "$S16/aec-demo-report/plan.json" \
  --root "$S16/aec-demo-report" --out "$S16/aec-demo-report/report.json" || exit $?
for S16_VARIANT in demo aec-demo normal; do
  for S16_BOARD in echoear p4xc; do
    S16_EXTRA=()
    if [[ "$S16_BOARD/$S16_VARIANT" == p4xc/aec-demo ]]; then
      S16_EXTRA=(--erle-report "$S16/aec-demo-report/report.json")
    fi
    paritylive hil/p4_parity_smoke.py --plan "$S16/plan.json" --board "$S16_BOARD" \
      --variant "$S16_VARIANT" --out "$S16/product/$S16_BOARD-$S16_VARIANT/attempt-01" \
      --bot-dir "$PRODUCT_BOT_DIR" --bot-identity "$PRODUCT_IDENTITY" \
      --bot-repo "$HOME/src/dg-esp32" "${S16_EXTRA[@]}" || exit $?
  done
  paritypy hil/p4_parity.py product-pair "$S16/product/echoear-$S16_VARIANT/attempt-01" \
    "$S16/product/p4xc-$S16_VARIANT/attempt-01" --out "$S16/product/pair-$S16_VARIANT.json" || exit $?
done
```

The pair calls `wire_contract.compare` for **every** main/resume/cold session
on demo images and the main session on normal. It checks exact schema, intended
AEC, platform exceptions, one process/profile identity, measured uplink bounds,
boot ELF, full session coverage and clean restore. There is no firmware rebuild.

## A2: ask the user for the physical double taps

Do this after all product pairs, before the fault bot is substituted. The
runner installs the new demo image, establishes Live, enables touch, then
prints **USER ACTION: double-tap the <board> screen ONCE within 45 seconds**.
The coordinator asks the user at that point, once per board. Do not inject a
touch or move a board. The witness requires the real double-tap counter,
accepted demo request, unchanged injection counter and a new Ready generation.
Exit 2 means clean acquisition/restoration but F11 remains unverified. Keep
that result; do not repeat taps until a passing count can be selected.

```bash
for S16_BOARD in echoear p4xc; do
  if paritylive hil/p4_parity_touch.py --plan "$S16/plan.json" --board "$S16_BOARD" \
    --out "$S16/touch/$S16_BOARD/attempt-01" --bot-dir "$PRODUCT_BOT_DIR" \
    --bot-identity "$PRODUCT_IDENTITY" --bot-repo "$HOME/src/dg-esp32"; then
    S16_TAP_EXIT=0
  else
    S16_TAP_EXIT=$?
  fi
  if [[ "$S16_TAP_EXIT" != 0 && "$S16_TAP_EXIT" != 2 ]]; then exit "$S16_TAP_EXIT"; fi
done
```

The conditional retains exit 2 as unverified even when the shell uses `set -e`.

## B: six whole-image F18/F19 runs and demo-only F20

Use the existing [observed demo-bot launch/restart procedure](p4_liveness.md),
now from this worktree with the unchanged main-repository bot entrypoint.
The source/profile and observer hash are checked at startup and replacement.
Set `LIVE_BOT` to its fresh directory containing `bot.pid`, `bot.log`,
`identity.json`, `ready.json`, `dc-events.jsonl` and recorder sessions. The
observer must use `--stale-probes`. The previous product bot has been stopped
by the coordinator after A; these new identities are fault-test evidence.

One run at a time, from the coordinator shell:

```bash
: "${LIVE_BOT:?current observed fault-test bot directory}"
export S16_VARIANT=demo
export S16_BOARD=echoear
export S16_LIVE_OUT="$S16/liveness/$S16_BOARD-$S16_VARIANT/attempt-01"
export NEXT_BOT="$S16_LIVE_OUT/replacement-bot"
paritylive hil/p4_parity_liveness.py live --plan "$S16/plan.json" --board "$S16_BOARD" \
  --bundle "$PIN15_BUNDLES/bundle-$S16_BOARD-$S16_VARIANT" --out "$S16_LIVE_OUT" \
  --bot-dir "$LIVE_BOT" --bot-identity "$LIVE_BOT/identity.json" --restart-dir "$NEXT_BOT" \
  --bot-repo "$HOME/src/dg-esp32" --restore-record "$RESTORE_RECORD" \
  --restore-record-sha256 "$RESTORE_RECORD_SHA256" --drain-seconds 150
```

In the separate coordinator bot-control shell, wait for the **actual**
`$S16_LIVE_OUT/restart-request.json` and the runner's restart message. Only
then stop that exact recorded old bot and launch the same observer/profile
in `NEXT_BOT`; the existing procedure records startup monotonic time and new
identity. Do not create `NEXT_BOT` before the runner's preflight. No runner
controls processes. The scored reconnect deadline is 60 seconds from the
replacement startup, not from coordinator dead time (handoff cap 180 seconds).
After the whole run and clean restore, retain `NEXT_BOT` as `LIVE_BOT` for the
next part. Execute these six parts in this fixed order:

| Order | Board | Variant |
|---:|---|---|
| 1 | echoear | demo |
| 2 | p4xc | demo |
| 3 | echoear | aec-demo |
| 4 | p4xc | aec-demo |
| 5 | echoear | normal |
| 6 | p4xc | normal |

Set `S16_BOARD`/`S16_VARIANT` and recompute the output/replacement paths for
each part; do not rerun the example's first output. All 20 cycles, 150 s drain
and 360 s idle are required. No step-14 `--reference-only` option is allowed.
F20 is explicitly not applicable for normal; the normal runner starts manually
and uses disconnect/idle rather than pretending to run a held demo.

## C: six display captures on one display bot

After B finishes with clean restore, the coordinator switches to one unchanged
`server/bot.py` process using the [13b display-pair setup](p4_display.md#existing-bench-bot-and-hardware-entry).
Profile: `BOT_MODE=offline-test`, port 7861, `OFFLINE_SKIP_GREETING=1`,
`DISPLAY_RETRY_BUDGET=0`, no coloured logs, exact `DISPLAY_HIL_COMMANDS` path.
Record its verified identity and keep this one process for all three pairs.
`DISPLAY_COMMANDS` is coordinator-owned; each helper replaces the known first
load/show command before connect. Touch and audio are off for these captures.

**Operator warning:** keep this exact display-bot process running for the
whole campaign, across all six captures and all three pairs. Its process
identity is anchored in `display-identity.json`. If it exits or restarts,
retain the originals and stop: a replacement cannot continue the current
plan. Arrange a user decision and a fresh campaign/plan before acquiring more.

```bash
: "${DISPLAY_BOT_DIR:?one unchanged server/bot.py process directory}"
: "${DISPLAY_IDENTITY:?verified identity JSON for that process}"
: "${DISPLAY_COMMANDS:?absolute DISPLAY_HIL_COMMANDS path for that process}"
for S16_VARIANT in demo aec-demo normal; do
  for S16_BOARD in echoear p4xc; do
    paritylive hil/p4_parity_display.py live --plan "$S16/plan.json" --board "$S16_BOARD" \
      --bundle "$PIN15_BUNDLES/bundle-$S16_BOARD-$S16_VARIANT" \
      --out "$S16/display/$S16_BOARD-$S16_VARIANT/attempt-01" --bot-dir "$DISPLAY_BOT_DIR" \
      --bot-identity "$DISPLAY_IDENTITY" --commands "$DISPLAY_COMMANDS" \
      --bot-repo "$HOME/src/dg-esp32" --seconds 30 --restore-record "$RESTORE_RECORD" \
      --restore-record-sha256 "$RESTORE_RECORD_SHA256" || exit $?
  done
  paritypy hil/p4_parity_display.py pair "$S16/display/echoear-$S16_VARIANT/attempt-01" \
    "$S16/display/p4xc-$S16_VARIANT/attempt-01" --out "$S16/display/pair-$S16_VARIANT.json" || exit $?
done
```

The bench-display and product wire checks are complementary; neither substitutes
for the other. The actual AEC flag is checked even while capture audio is off.
Scan callback latency is reported; optical border/tearing stay unverified
without a separately retained physical observation. Unsolicited C6 recovery
is still report-only. No synthetic result proves touch or hardware radio recovery.

## Evidence receipts and parity table

Smoke/liveness host-only checks (arguments, plan/restore selection, bot
readiness, tools and the inherited bench lock) finish before registration.
A host-only refusal leaves the ledger unchanged. Once acquisition begins,
its original is sealed even when the result is missing or malformed;
`abandon` preserves and hashes the raw bytes using the registered identity.

Every live entrypoint registers an acquisition in `$S16/attempts.jsonl` before
creating its attempt directory. The append-only JSONL is plan-bound and
hash-chained, with a final disposition and hashes of the retained result.
Never edit it, delete a directory, or move attempts outside the campaign root.
A new source set requires a fresh plan/root; the previous host-validation plan
is historical and cannot accept the fix-1 tools.

Only a raw `preflight-noisy` result proving untouched hardware, no score and
room noise permits one `attempt-02` in the same unit/parent. Nothing else
permits a retake, including a missed bot handoff or a resource failure.
Retain the noisy attempt and its receipt too. ERLE parts use the ledger-aware
`p4_parity_erle.py`; any reused same-image historical ERLE report is cited as
historical rather than relabelled as a campaign acquisition.

If a process dies before writing its result/disposition, first finish any
coordinator-owned restore recovery. Then seal the retained attempt offline:

```bash
paritypy hil/p4_parity.py abandon --plan "$S16/plan.json" \
  --run "$S16/liveness/echoear-demo/attempt-01" --reason 'interrupted; retain as incomplete'
```

After all parts, `collect-all` enumerates **every** registered original,
including ERLE, noisy preflights and failures. It also derives product/display
pairs from the ledger's final permitted originals. It writes a fresh receipt
directory; do not select only passing receipts. The table authenticates the
plan, inventory, dispositions and receipts, and refuses any missing attempt,
unregistered retained attempt directory, changed result or unfinished part.

```bash
paritypy hil/p4_parity.py collect-all --plan "$S16/plan.json" --out "$S16/receipts"
S16_RECEIPTS=()
for S16_RECEIPT in "$S16"/receipts/*.json; do S16_RECEIPTS+=(--receipt "$S16_RECEIPT"); done
paritypy scripts/p4_parity_table.py --plan "$S16/plan.json" \
  "${S16_RECEIPTS[@]}" --out "$S16/p4xc-parity.md"
# Review the generated Markdown, then copy only the prose table into docs:
# cp "$S16/p4xc-parity.md" docs/p4xc-parity.md
```

Single-display and pair receipts replay exact boot identity, committed screen
proofs, frozen fixture hashes, persisted status inventory, actual HTTP body
hash/length, session load/show requests and acknowledgements, terminal results,
and rendered revision/layer. The command and HTTP journals are retained next
to serial/events. `serial-entries.jsonl` retains original reader cursors and
receive timestamps. Display `wire.log` must equal the exact connect-to-cleanup
window of the full serial capture; liveness windows bind the actual boot,
Ready/close, interval generation, PERF peer/generation and control epoch.
Persisted assets must be in flash; RAM eviction is allowed. Fatal checks cover the complete closed
candidate `serial.log`, including context teardown and raw tails beyond saved
run/active windows or the journal. Display success is set only after the
reader and asset-server contexts have exited and this check passes. Liveness replays all 21 convergence witnesses plus drain,
both ready/identity markers, the restart request, process-specific control
extents and source hashes against the plan's declared fault-bot policy.
Every family binds raw restore/summary equality, restored boot and record;
P4 also binds candidate/restore storage erase and full FF readback.

Receipts recompute coverage and rehash their inputs. A missing profile/category
cannot become pass from another image's success. Report-only evidence remains
marked. Features not actually exercised by these helpers (the complete console
surface, codec/netprobe diagnostics, export integrity, battery instrumentation,
WiFi observe/cache/PHY, DSP transfer curves and contention) remain unverified
for step 16 until their specific earlier evidence is reviewed and cited; a smoke
does not manufacture that coverage. Keep the historical sources in the board
docs alongside the new table. The writer's initial table deliberately records
pending coverage rather than claiming a run while the bench is busy.

## Step 16 AEC-demo follow-up

The [prerequisite retry decision and addenda](../../dg-esp32/proj-2026-10-30-2143/codex-jobs/decision-2026-11-03-step16-prereq-retry.md)
were made **after** the main A0 part-0 dump error and the main S3 demo failure.
The first error was unscored/retryable (`Missing/reordered/duplicate AEC PCM
block`). The S3 demo failed during the user-reported noisy interval: its
France reply was off script and later room speech was transcribed as
"Twenty. Sorry. Twenty million." Keep both originals and their receipts.

The follow-up now covers **all product cells**: both boards' demo, normal and
AEC-demo smokes, all three product pairs, and the P4 AEC-demo report parts.
It also includes one disclosed F11 physical-touch re-run per board.
It runs only after the main campaign finishes. Liveness and display remain
exclusively in the main plan. The original `s16` tree stays frozen;
use `~/src/dg-esp32-p4-s16b`, branch `p4-step16-aecdemo`, for this follow-up.
The writer has no serial/bench/bot access; writer host tests must run under
`nice -n 19`, and no firmware build is needed or authorized.

The new modules wrap the frozen HIL files. Main-plan replay authenticates the
28 original runtime hashes against Git commit `42106560112ce5e43dc344c2a1336c51a87f4e65`
using this worktree's Git objects and identical local copies. It does not
open the frozen worktree. The main ledger is opened read-only during merge.

Only A0 `erle` parts 0–3 get the additional retry: at most two attempts for a
part, after a matching raw acquisition-error result with typed
`score_produced=false`, `retryable=true`, the same registered measurement plan,
part/ELF/gain/product identity, no scored marker/payload and clean raw R7
recovery. A noisy untouched preflight still gets the existing one retry.
Every attempt remains in its ledger and report. All parity outcomes retain
the old no-retake rule, except the one explicitly authorized and disclosed
main-plan S3 demo re-run and one F11 re-run per board in this separate plan.
The main F11 attempts stay unverified: S3 had no touch reads/interrupts
(the prompt arrived late); P4 saw one tap and no double-tap. Both originals
remain disclosed. F11 uses the same pinned demo images and strict physical
witness/restore checks. A second follow-up touch attempt is refused.

AEC-off now uses the same room-baseline acquisition and the same underlying
pure content checker as AEC. `--llm-content report-only` softens LLM answer
content alone. France/count STT, AEC-off talk-over suppression, completed and
uninterrupted replies, timing/wire, resource floors, cleanup and restoration
stay strict. No executor/calibration flag or actual AEC intent is changed.
An off-script LLM answer is retained in `llm_content_findings` and yields a
report-only smoke receipt. Room speech or missed STT still fails.

Set up a **fresh external root** and one current recorder-enabled master
`demo_bot.py` for A0 and all six product smokes. Its source and profile must
match the main product bot; its process may be new, and its identity is frozen
within this follow-up. Keep that exact process throughout all product pairs.
The coordinator supplies `MAIN_PLAN` and the already collected immutable
`MAIN_RECEIPTS` directory after finishing the main campaign.

```bash
cd "$HOME/src/dg-esp32-p4-s16b"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export S16F="$HOME/src/evidence/echoear/p4/step-16/product-follow-up-run-1"
export PIN15_BUNDLES="$HOME/src/evidence/echoear/p4/step-15-pin-round/commit-52482495d1b859b5313cd8c76e4cd0cdd97ea507/final-bundles"
: "${MAIN_PLAN:?completed frozen main campaign plan}"
: "${MAIN_RECEIPTS:?retained main receipts collected by the coordinator}"
: "${PRODUCT_BOT_DIR:?one unchanged current recorder-enabled master demo bot}"
: "${PRODUCT_IDENTITY:?its verified identity JSON}"
followpy() {
  nice -n 19 uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
followlive() {
  python3 hil/bench.py uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
followpy hil/p4_parity_followup.py plan --main-plan "$MAIN_PLAN" \
  --root "$S16F" --product-identity "$PRODUCT_IDENTITY" --out "$S16F/plan.json"
followpy hil/p4_parity_followup.py check --plan "$S16F/plan.json"
```

Freeze the new source set and plan hash before acquisition. The following are
**coordinator commands**, requiring the runner-owned bench lock, room go-ahead
and the existing R7 recovery procedure. No writer runs them.

```bash
followpy hil/p4_aec_close.py image plan --bundle "$PIN15_BUNDLES/bundle-p4xc-aec-demo" \
  --recommendation "$HOME/src/evidence/echoear/p4/step-15b-fix-5/recommendation.json" \
  --out "$S16F/aec-demo-report/plan.json"
for S16F_PART in 0 1 2 3; do
  followlive hil/p4_parity_followup.py erle --plan "$S16F/plan.json" \
    --image-plan "$S16F/aec-demo-report/plan.json" --part "$S16F_PART" \
    --out "$S16F/aec-demo-report/gain-30/part-$S16F_PART/attempt-01" \
    --bot-dir "$PRODUCT_BOT_DIR" --bot-identity "$PRODUCT_IDENTITY" \
    --bot-repo "$HOME/src/dg-esp32" || exit $?
done
```

On an eligible first A0 acquisition error, stop the loop, verify recovery and
retain the original. Run the **same** command for that part with
`attempt-02`, once, then continue the not-yet-acquired parts. Registration
checks raw eligibility and refuses a scored, dirty, changed or third attempt.
Do not restart the entire loop or select a best trial. After all four parts:

```bash
followpy hil/p4_aec_close.py image report-gain --plan "$S16F/aec-demo-report/plan.json" \
  --root "$S16F/aec-demo-report" --out "$S16F/aec-demo-report/report.json" || exit $?
for S16F_VARIANT in demo normal aec-demo; do
  for S16F_BOARD in echoear p4xc; do
    S16F_EXTRA=()
    if [[ "$S16F_BOARD/$S16F_VARIANT" == p4xc/aec-demo ]]; then
      S16F_EXTRA=(--erle-report "$S16F/aec-demo-report/report.json")
    fi
    followlive hil/p4_parity_followup.py smoke --plan "$S16F/plan.json" \
      --board "$S16F_BOARD" --variant "$S16F_VARIANT" --llm-content report-only \
      --out "$S16F/product/$S16F_BOARD-$S16F_VARIANT/attempt-01" \
      --bot-dir "$PRODUCT_BOT_DIR" --bot-identity "$PRODUCT_IDENTITY" \
      --bot-repo "$HOME/src/dg-esp32" "${S16F_EXTRA[@]}" || exit $?
  done
done
# Give these instructions to the user BEFORE opening either touch window:
# Two short taps on the same spot, each <0.25 s; second ends 0.08–0.4 s after first.
# Relay TAP NOW immediately when printed; do not wait for a later status update.
for S16F_BOARD in echoear p4xc; do
  followlive hil/p4_parity_followup.py touch --plan "$S16F/plan.json" \
    --board "$S16F_BOARD" --out "$S16F/touch/$S16F_BOARD/attempt-01" \
    --bot-dir "$PRODUCT_BOT_DIR" --bot-identity "$PRODUCT_IDENTITY" \
    --bot-repo "$HOME/src/dg-esp32" || exit $?
done
followpy hil/p4_parity_followup.py collect-all --plan "$S16F/plan.json" --out "$S16F/receipts"
S16F_RECEIPTS=()
for S16F_RECEIPT in "$MAIN_RECEIPTS"/*.json "$S16F"/receipts/*.json; do
  S16F_RECEIPTS+=(--receipt "$S16F_RECEIPT")
done
followpy scripts/p4_parity_table.py --plan "$MAIN_PLAN" --follow-up-plan "$S16F/plan.json" \
  "${S16F_RECEIPTS[@]}" --out "$S16F/merged-parity.md"
```

`collect-all` derives all three pairs, replays every original and includes
failed/unscored A0 attempts. Merge authenticates both plans and every receipt,
reconciles each complete ledger, cites plans on every row, and prints the
post-failure decision and all retained attempts. Main liveness/display
cannot be replaced. F11 may replace only each board’s one unverified demo
original, with the original explicitly retained in the disclosure. Duplicate receipt paths, third plans, duplicate cells and
all other product conflicts are refused. The product overlap exception is the
one main S3 demo FAIL; it remains explicitly cited as failed. Its verdict is
replaced in coverage only after the one follow-up original has an outcome;
noise-only or incomplete follow-up evidence cannot conceal the main failure.

## Step 16 close-out replay

The 17:05 addendum records the completed campaign outcomes. Close-out is
host-only: do not reacquire a failed product pair or touch attempt. The demo
pair remains FAIL under the unchanged two-sided 6 ms p99 bound (resume S3
47 ms, P4 34 ms; P4 was better), and F11 remains unverified on both boards.
Normal and AEC-demo product pairs passed. Carry the F11 redesign/controller
check, AEC-specific bounds, final-hardware near-end measurement and parked V2
work forward.

`hil/p4_step16_close.py` adds read-only collection over the frozen acquisition
sources. It authenticates the exact 17:05 addendum and the original decision
prefix, so neither the acquired plan nor its recorded decision hash changes.
ERLE identity comes from the retained `result.json`, matched to the planned
product bot and identical `scored-original.json`; no `identity.json` is
manufactured. All raw restore, ledger, source, image, part and measurement-plan
checks remain. Repaired ERLE receipts include the close-out collector hash and
decision hash. Main and follow-up ledgers are read-only during replay.

Keep the first receipts and table. Regenerate into fresh paths:

```bash
cd "$HOME/src/dg-esp32-p4-s16b"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
S16_MAIN="$HOME/src/evidence/echoear/p4/step-16/coordinator-run-1"
S16_FOLLOW="$HOME/src/evidence/echoear/p4/step-16/product-follow-up-run-2"
closepy() {
  nice -n 19 uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
closepy hil/p4_step16_close.py collect-all --plan "$S16_FOLLOW/plan.json" \
  --out "$S16_FOLLOW/receipts-2"
S16_CLOSE_RECEIPTS=()
for S16_CLOSE_RECEIPT in "$S16_MAIN"/receipts/*.json "$S16_FOLLOW"/receipts-2/*.json; do
  S16_CLOSE_RECEIPTS+=(--receipt "$S16_CLOSE_RECEIPT")
done
closepy scripts/p4_parity_table.py --plan "$S16_MAIN/plan.json" \
  --follow-up-plan "$S16_FOLLOW/plan.json" "${S16_CLOSE_RECEIPTS[@]}" \
  --out "$S16_FOLLOW/merged-parity-2.md"
```

The generator adds the demo-pair explanation from raw resume intervals,
F11 counters from both original and follow-up touch results, and ERLE receipt
regeneration/report provenance. Copy that generated output to
`docs/p4xc-parity.md`; do not hand-edit evidence outcomes.
