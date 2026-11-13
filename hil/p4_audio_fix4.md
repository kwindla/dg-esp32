# Step 12b fix round 4: voice evidence and stack measurements

> **STEP 14 RESTORE GUARD:** Live commands now require `hil/p4_step14.py run`
> and the clean-commit restore record/hash plus its successful boot proof from
> [the step-14 procedure](p4_liveness.md). Do not invoke frozen `p4_product.py`
> directly: its historical factory/keep-tested semantics do not enforce R7.
> The live commands below have been migrated; export `RESTORE_RECORD` and
> `RESTORE_RECORD_SHA256` from the newly approved record before copying them.


Use `hil/p4_audio_fix4.py` for **voice-smoke, pair and costs** going forward.
This is an additive adapter; the frozen `p4_audio.py`, `board_smoke.py`, bot,
firmware and original captures stay unchanged. Acoustic acquisition/reanalysis
still use `p4_audio.py`. The step-13a display helper is separate.

## Voice acquisition and pair

Keep the exact 12b launch environment, existing unchanged demo-bot identity,
acoustic eligibility and verified bundle arguments in [p4_audio.md](p4_audio.md),
replacing only the voice/pair entrypoint with `hil/p4_audio_fix4.py` and using
**new** output paths. `--headless --calibration` remain required: headless skips
UI/assets/touch checks while S3 still initializes its display runtime before
connect. It does not fetch assets, show demo screens or change firmware. An
already-initialized S3 runtime is reused; missing initialization after `disp
init` fails acquisition. P4's boot audio readback and acoustic/reanalysis guards
are inherited unchanged.

Both legs validate the **product** evidence profile, including actual
`DEMO_CAPABILITY`, `msg=display-ready`, `opus_enc` and `UPLINK_INTERVALS`, inside
the existing acquisition/restore hold. Frozen execute also retains its headless
check; that check cannot substitute for the adapter's strict product check.
The result declares `voice_evidence_profile=product`, `ui_checks_skipped=true`,
`display_initialized` and `product_evidence_verified`. Calibration still does
not confer qualification or infer a production stall limit from one run.

Offline `pair` delegates to the already-strict frozen `product_pair` unchanged:
full product schema, same bot process/source/profile, fixed normal-image
interval bounds, exact media/encoder records and advertised display limits.
No missing record is synthesized or removed. Full coordinator commands follow.
Release the build lock first; the bench runner owns the bench lock. Use one
existing unchanged demo-bot process for both legs. Do not run these live
commands as the writer.

```bash
cd /home/khkramer/src/dg-esp32-p4
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
P4_AUDIO_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
: "${FIX5_OUT:?new evidence parent directory}"
: "${S3_BUNDLE:?verified normal AEC-off S3 bundle}"
: "${P4_BUNDLE:?verified normal AEC-off P4 bundle}"
: "${BOT_DIR:?existing demo bot directory with bot.pid and uncoloured bot.log}"
: "${BOT_IDENTITY:?identity JSON for that unchanged demo bot}"
: "${ACOUSTIC_REANALYSIS:?verified P4 decision reanalysis for this bundle ELF}"

"$P4_AUDIO_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_fix4.py voice-smoke --board echoear --bundle "$S3_BUNDLE" \
  --out "$FIX5_OUT/voice-echoear" --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" \
  --bot-repo /home/khkramer/src/dg-esp32 --pair-id step12b-product-fix5 \
  --headless --calibration
"$P4_AUDIO_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_audio_fix4.py voice-smoke --board p4xc --bundle "$P4_BUNDLE" \
  --out "$FIX5_OUT/voice-p4xc" --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" \
  --bot-repo /home/khkramer/src/dg-esp32 --pair-id step12b-product-fix5 \
  --acoustic-reanalysis "$ACOUSTIC_REANALYSIS" --headless --calibration
# Offline, outside hil/bench.py:
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_fix4.py pair "$FIX5_OUT/voice-echoear" "$FIX5_OUT/voice-p4xc" \
  --out "$FIX5_OUT/product-pair.json"
```

`VoiceIO.finish` forwards `hold=hold` to the frozen keyword-only API. Tests
inspect the real method signatures and use matching stubs. Aborted-session
cleanup still drains/freezes the capture, but skips secondary product/stack
validation so it cannot obscure the original failure. Both boards verify
`PERF.display.lcd_initialized`; P4 must already be initialized at boot. The
product validator uses the actual bundle's AEC setting, including in tests of
AEC-on evidence, while the step-12b voice CLI remains normal AEC-off only.

The saved S3 `attempt1-voice-echoear-missing-display-ready` remains a failed
acquisition: its 12 passing turns cannot create the absent wire record. Offline
`replay` reports that precise failure, while the saved P4 capture passes product
evidence. A new S3 acquisition is still required for the successful pair.

## What stack zero means

Firmware source confirms, without a firmware edit:

- `components/pipecat_esp_peer/src/transport.cpp`: Session stack atomics start at
  zero (line 244); tx_run writes tx_stack only at exit (597), owner_run writes
  owner_stack at exit (556), and rx_run writes rx_stack at exit (759). Active
  zero fields are unavailable measurements. Decoder pull samples the actual
  audio-playback caller at line 658.
- `main/performance.cpp`: `uxTaskGetSystemState` already exports each task's
  actual `usStackHighWaterMark` as `tasks[].stack_free_bytes` (in bytes), together
  with task name/ID and allocated stack size. `main/console.cpp` also emits STACK
  records; PHASE tx-exit/owner-exit/rx-exit independently witness exit marks.
  No new firmware measurement is needed.

`costs` uses every snapshot from the measured session generation. It maps
tx_stack→audio-tx, decode_stack→audio-playback, owner_stack→peer-loop and
rx_stack→peer-rx. Positive direct fields and task-census marks both contribute;
the minimum wins. Snapshot selection is per session generation; the census
mark itself is a **task-lifetime high-water mark**, including earlier sessions
on the same PersistentWorker. It is conservative for gates. Reboot between
sessions when independent measurements are required for default derivation.
Each of the three encoder-stack derivation sessions must start from a fresh
boot; `voice-smoke` already flashes and boots once per invocation. Reconnecting
three times in one boot does not provide independent census minima.
Unknown/zero direct fields become null, not “0 B free”. A
zero **census** mark is a real measured low mark and must fail the stack check.
Missing/ambiguous task identities, invalid values or impossible free/allocated
sizes do not turn into valid margins. Source rows and hashes are retained.

Valid codec cost figures are written even if a gated stack measurement is
unavailable or below its floor. Voice acquisition records every task minimum
and applies the same board-specific policy before success.
`codec_costs_available=true` is separate from `checks_passed` and
`stack_checks_passed`; the latter remain false for unavailable gated
measurements or a per-task floor miss. The coordinator decision
`decision-2026-11-01-p4-task-stack-floors` and its
`decision-2026-11-01-p4-task-stack-floors-addendum` require encoder, decoder
caller and owner ≥4096 B free on both boards. On P4, peer-rx requires
≥max(1744 B, 25% of allocation), rounded up to whole bytes. On S3, peer-rx is
**report-only**: a low or unavailable peer-rx mark/allocation does not fail the
stack gate. Reports identify it in `stack_report_only` and use a null
`stack_floors_bytes.rx_stack`; its measurements and all task minima remain
visible. Invalid values or conflicting/changing allocations still fail closed.
On P4, missing peer-rx allocation cannot produce a passing floor.

The 1744 B reference is from **this step's uncommitted S3 normal bundle**
(`092e8cf4…`, step-12b fix-1), observed in
`step-12b/fix-1/attempt1-voice-echoear-missing-display-ready/perf-003.json`;
it is **not** from image of record `55152f25`. JSON records the decision,
addendum and corrected reference provenance. The CLI returns 1 for a gated
resource finding, with JSON still written.
Insufficient real work, wrong frame size, decoder errors, unclean restore,
invalid heap or missing peer-loop resource evidence remain hard failures.

Saved P4 run (`step-12b/fix-1/coordinator-voice-p4xc`), exact live wall µs:

| Metric | p50 | p95 | Exact max |
|---|---:|---:|---:|
| Encode, 17,298 frames | 3199 | 4399 | 6485 |
| Decode | 1099 | 1799 | 2491 |
| Speech decode, 17,119 frames | 1099 | 1799 | 2491 overall decode max |

Percentiles have the existing 100 µs histogram resolution; maxima are exact.
There is no p99 codec claim. Actual minimum free stack: audio-tx **6344 B**,
audio-playback **4772 B**, peer-loop **6616 B**, peer-rx **2264 B**. Per-task
floors are 4096/4096/4096/**1744 B** (peer-rx allocation 4096 B). All four
pass the adopted decision; the old blanket 4096 B dispatcher finding is
superseded, without changing firmware. `task_minima` reports every census task
with its ID, while `stack_measurements` retains the four gated measurements,
allocation provenance, `stack_floors_bytes` and `stack_report_only`. Saved-run results are still
not qualification or a successful S3/P4 product pair.

```bash
# Offline; retain the exports above and choose a fresh output path.
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
  hil/p4_audio_fix4.py costs \
  "$HOME/src/evidence/echoear/p4/step-12b/fix-1/coordinator-voice-p4xc" \
  --out "$FIX5_OUT/live-opus-cost.json"
```

Host acceptance is `scripts/host-tests.sh` (now includes the fix4 suite), plus isolated
`python hil/p4_audio_fix4_mutations.py --out <new-dir>` in the pinned server
environment. Tests replay both original real captures read-only, preserve the
historical S3 failure, recover real encoder census marks, and detect disabled
S3 init, weakened profile, zero-as-measurement, ignored census, maximum instead
of minimum, relaxed floors, positional hold forwarding, missing-snapshot and
foreign-generation omissions, dispatcher percentage/rounding errors, aborted
cleanup validation and ignored bundle AEC settings. Fix round 6 also covers
live finish generation filtering and its two-sample minimum, allocation changes,
direct marks exceeding allocation, minimum-vs-maximum in all-task reports,
S3 report-only/P4 gated peer-rx and board forwarding in both paths.
Acquisition inputs are never rewritten.

The two real-evidence tests skip with an explicit reason when their local
acquisition directories are absent. Synthetic signature, task-floor, missing
snapshot and foreign-generation tests always run, including in mutations.

## P4 storage preparation

P4 live commands must use [p4_step14.py run](p4_step14.py), which applies the pinned restore policy and the p4_product storage adapter
shown above; frozen helpers called directly do not perform this step. See
[p4_product.md](p4_product.md) for the named-partition checks, erase/readback
evidence and coordinator decision. S3 behavior and offline analysis are unchanged.
