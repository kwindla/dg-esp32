# User-paced F11 touch verification

This is a new, separate physical-touch check for the **refactored demo pins**
at `5248249` on S3 and P4. It does not reuse the S3 restore image as the
candidate. The prior step-16 F11 outcomes remain unverified and unchanged.
A host test or a detected contact alone cannot establish F11.
The retained touch-verify run-1 passed on both boards; see the cited
[F11 parity addendum](../docs/p4xc-parity.md#f11-verified-touch-verify-run-1).
Keep that completed run immutable and use a fresh plan/root for future runs.

The coordinator runs hardware commands through `hil/bench.py`. The writer
has no bench/serial access. Use one unchanged recorder-enabled product demo
bot, with its current identity file. This helper does not start or stop a bot,
change firmware, inject touch, or access service ports on the writer's behalf.
It flashes the already-pinned candidate during the coordinator-owned run.

The frozen step-16 physical witness is unchanged: one real double-tap counter
increment, one accepted demo request without a new rejection, unchanged
injection count, and a new Live/Ready generation witnessed in the serial log.
The run stops immediately on that first witness. Multiple double taps cannot
be selected into a best trial. Singles/contact detection is reported separately.

## Plan and host checks

Freeze the code before creating a plan. Choose a fresh directory strictly
below `~/src/evidence/echoear/p4/touch-verify/`. The plan pins both demo images,
the bot identity, source/brief hashes, P4 R7 restore record, and S3 `55152f25`
restore. It fixes the GO wait at 900 s, observation at 600 s and polling at 1 s.
Plan/check/status are offline and never acquire the bench lock.

```bash
cd "$HOME/src/dg-esp32-p4"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export TOUCH_ROOT="$HOME/src/evidence/echoear/p4/touch-verify/run-2"
: "${PRODUCT_BOT_DIR:?coordinator-owned product demo bot recorder directory}"
: "${PRODUCT_IDENTITY:?its current identity JSON}"
touchpy() {
  nice -n 19 uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
touchpy hil/p4_touch_verify.py plan --root "$TOUCH_ROOT" \
  --product-identity "$PRODUCT_IDENTITY" --out "$TOUCH_ROOT/plan.json"
touchpy hil/p4_touch_verify.py check --plan "$TOUCH_ROOT/plan.json"
```

## Coordinator: establish Live and wait for the user

Run one board at a time. The helper owns the inherited bench lock through
candidate setup, waiting, observation, cleanup and restore. Do not open a
second serial connection to inspect touch while it runs. Neither GO nor
counter inspection requires another hardware command.

```bash
# Coordinator terminal A; choose echoear first, then p4xc in a separate run.
TOUCH_BOARD=echoear
python3 hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
  --with esptool==5.5.0 python hil/p4_touch_verify.py run \
  --plan "$TOUCH_ROOT/plan.json" --board "$TOUCH_BOARD" \
  --out "$TOUCH_ROOT/$TOUCH_BOARD/attempt-01" \
  --bot-dir "$PRODUCT_BOT_DIR" --bot-identity "$PRODUCT_IDENTITY" \
  --bot-repo "$HOME/src/dg-esp32"
```

The runner flashes the correct demo, connects Live, enables touch, then writes
`ready-for-go.json` and prints **READY FOR GO**. It waits up to **15 minutes**
for that attempt's `GO` file. The 600-second window has not started yet.
Contacts before GO remain in the serial/timeline but are excluded from the
fresh scored baseline. A pre-existing GO is refused.

Before writing GO, the coordinator:

1. Confirms the marker names the intended board and the runner is ready.
2. Explains the gestures and tells the user to wait for **WINDOW OPEN**.
3. Gets the user's explicit "ready" while they are at that board.
4. Creates GO immediately in a second terminal, then promptly relays the
   runner's **WINDOW OPEN** message. Do not pre-create the attempt directory
   or GO before the runner's readiness marker.

```bash
# Coordinator terminal B, after the ready marker and the user's "ready".
export TOUCH_ROOT="$HOME/src/evidence/echoear/p4/touch-verify/run-2"
TOUCH_BOARD=echoear
TOUCH_ATTEMPT="$TOUCH_ROOT/$TOUCH_BOARD/attempt-01"
if [[ -f "$TOUCH_ATTEMPT/ready-for-go.json" && ! -e "$TOUCH_ATTEMPT/GO" ]]; then
  touch "$TOUCH_ATTEMPT/GO"
else
  printf '%s\n' 'Runner is not ready, or GO already exists.'
fi
```

## The 600-second window

On GO, the runner holds the prior session, starts fresh Live with `resume`,
waits for Ready, enables touch and takes the scored baseline. It then prints
the window marker. Wait for **WINDOW OPEN** before touching. It polls
`touch-status` once per second and prints changes in taps, double taps,
ignored/rejected contacts, interrupts, reads and requests.

The product bot can close an idle session after 300 s. If Live closes or its
generation changes without a touch request, the runner starts fresh Live and
takes another baseline. This stays within the same attempt and the original
600 s deadline; reconnect time counts against that deadline. It retains every
baseline in `baseline-NNN.json`, `touch.json` and `status-timeline.jsonl`.
`before.json` stays the first GO baseline; `witness_before` and
`witness_serial_start` identify the current baseline. A pass must satisfy the
frozen witness from both the current baseline and the original GO baseline.
Earlier contacts, rejected requests, injections and errors cannot be erased
by renewal or turned into an absent-user retry.

Ask for **one short single tap in the middle of the scene**, then look for
**CONTACT PRE-CHECK**. Valid tap contacts, ignored/rejected contacts and a
standalone single observed before a double tap are reported separately.
Ignored/rejected contact evidence proves a contact reached the gesture path;
it does not prove an accepted F11 request. Poll reads alone on P4 are expected
and do not prove contact. S3 IRQ/read activity without a decoded contact is
reported as activity and conservatively blocks an absent-user retry.

After the pre-check, ask for **one double tap at the same spot**: two distinct
20–250 ms contacts, with the second release 80–399 ms after the first. Ask the
user to stop touching after that double tap. The 600-second window gives time
for coordinator relay; it does not relax the firmware's gesture timing.

A pass requires the original physical witness and new Ready generation;
contact/pre-check success alone remains unverified. An error, changed
injection counter, counter reset or dirty restore cannot pass. Idle renewal
also refuses an unaccepted double tap or app rejection. The helper always attempts touch-disable and
demo-hold before closing serial and restoring. P4 uses R7 and S3 restores
`55152f25`, including the existing lock-retaining recovery lifecycle.

## Outcomes, retained attempts and the sole retry

Exit codes: **0** witnessed F11, **2** clean acquisition but unverified,
**1** acquisition/cleanup/restore failure. `touch.json` reports the contact
pre-check independently of F11. A 15-minute GO timeout is unverified and does
not earn a retry, because no post-GO observation window occurred.

Every attempt is registered before setup and sealed afterwards in the
hash-chained/fsynced `attempts.jsonl`. All original files are kept, including
serial, `status-timeline.jsonl`, readiness/GO/window markers, before/after,
window serial slice, identity, result and restore witnesses. An unfinished,
malformed, modified, successful, contact-bearing, short-window or dirty
original cannot be replaced.

There is **one** permitted second attempt per board, only after the first
completed a clean 600-second post-GO window with no contact evidence at all.
The runner authenticates that original's sealed files, GO/timeline/brackets,
counter deltas and raw restore proof before registration. P4 reads alone do
not disqualify absence; read errors do. There is no automatic retry loop.
Zero observed counters are not a claim that the touch controller works. If
the user reports tapping but nothing arrives, keep the unverified result and
investigate the controller; do not report a physical pass.

```bash
touchpy hil/p4_touch_verify.py status --plan "$TOUCH_ROOT/plan.json"
# Only after inspecting an eligible no-contact result, use the same live
# command with attempt-02, and a fresh user-ready/GO handshake. No attempt-03.
```

New plans do not overwrite step-16 plans/receipts or completed touch runs.
The published F11 pass/pass addendum cites run-1's plan hash and each board's
physical witness; all earlier step-16 unverified attempts remain disclosed.
The offline `hil/p4_touch_close.py` reader authenticates run-1 against its
archived acquisition sources, sealed attempts, raw counters and clean restores.
