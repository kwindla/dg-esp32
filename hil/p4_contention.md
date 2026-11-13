# Step 13b: touch and five-minute contention gate

> **STEP 14 RESTORE GUARD:** Live commands now require `hil/p4_step14.py run`
> and the clean-commit restore record/hash plus its successful boot proof from
> [the step-14 procedure](p4_liveness.md). Do not invoke frozen `p4_product.py`
> directly: its historical factory/keep-tested semantics do not enforce R7.
> The live commands below have been migrated; export `RESTORE_RECORD` and
> `RESTORE_RECORD_SHA256` from the newly approved record before copying them.


Writer work is offline only. The coordinator owns hardware, starts/stops the
bot, and runs every live command through `hil/bench.py`. Stop builds/tests
before starting a bench run. Evidence belongs under
`~/src/evidence/echoear/p4/step-13b/`; do not commit acquisitions.

## Limits fixed before acquisition

`p4_contention.py` fixes 300 seconds, with one intentional disconnect/connect
at 150 seconds while scene animation/DSI scan-out continues. Both generations
must have at least 130 seconds of resource samples, separately frozen once
with `uplink-intervals` before teardown. No restart gap is spliced out of a
histogram. The fixed restart start window remains [150,160) seconds; a persist
lasting more than 10 seconds under the 30 Hz scene can exhaust its scheduling
margin. Such a restart-window miss needs procedure diagnosis and is not by
itself a board defect. The helper still refuses the run; it never widens the
fixed limits or retries automatically. One unchanged `server/bot.py`, **BOT_MODE=live-test**, supplies STT,
TTS and display on 7861. Never run it alongside demo_bot from the same `--bot-repo`. The helper checks its
Python child PID/start token, source hashes, profile and command file; it never launches or
stops a bot. Both normal AEC-off boards can run this procedure sequentially
against the same process, for the R2 exact bench-display pair.

Pass checks:

- On P4, measured zero mic `capture.rx_overflows`; zero playback `i2s_underflows`, including
  the restart. Counters are absolute, not deltas that could hide an earlier fault.
  `PERF.capture` is required on the first Ready snapshot, before the timed window:
  rx_overflows/rx_completions are driver counters, and read_errors/read_timeouts/
  read_frames come from the microphone read path. Old AEC-off bundles without
  these keys fail immediately with a named rebuild error; AEC's historical
  `aec.capture` object remains unchanged. Send-queue drops are not DMA overflows.
  Per decision `decision-2026-11-01-capture-counter-liveness`, each measured
  generation must end with a greater `capture.rx_completions` than it began
  with before a zero overflow count is evidence. P4 (and future boards) fail
  with `mic capture counter not live` if callbacks are dead. EchoEar S3 normal
  registers neither RX callback: its dead counter produces
  `capture_overflows=null`, `capture_counter_live=false` and
  `capture_overflows_result="unmeasured (report-only)"`; this does not claim zero
  overflows. If an S3 counter does advance, its overflow gate applies normally.
  Raw counts, completion delta and the decision are retained in each generation;
  pair output retains both boards' capture evidence. A count inherited from a
  previous generation cannot prove the next one. No firmware/audio path changes.
  Pre-window samples retain the absolute resource/fault checks and prefix any
  failure with `pre-window:`. Their capture measurement stays pending until
  final per-generation liveness is established.
- Each generation passes `wire_contract.measured()` using **normal** bounds:
  max and trailing gap ≤ **68,411 µs**, coverage ≥ **0.95**, ≥500 intervals,
  unchanged 20 ms cadence. Report its p99 and all three absolute p99 differences
  against the hash-pinned step-11 S3 normal captures; each difference ≤ **7,000 µs**.
- Internal heap minimum ≥ **81,920 B**, largest block ≥ **32,768 B** (including
  sampled inter-query minima when available); zero failed allocations. This
  includes all actual internal driver and touch allocations. The helper derives
  display storage from the verified bundle ELF, grouping objects by section.
  The 13a recovery moves the large panel/history/table into external NOBITS BSS
  (17,632 B in its ELF), leaving a 4 B pointer and 8 B construction guard inside.
  Both internal and external totals, object sizes/sections and ELF hash are
  reported. Historical images are recognized as legacy internal layouts; their
  former static budget is not subtracted or transferred to the new image.
- Live STT final transcript and first TTS audio witness in both generations;
  DTLS duration reported in each. DISPLAY status immediately before disconnect
  and immediately after Ready brackets render progress across the restart.
  This demonstrates progress across that interval; it does not timestamp a
  particular scan inside the DTLS handshake. DSI scan-out itself is continuous.
- At least 20 real HTTP DGI fetch → LittleFS persist → delete cycles per
  generation, distributed across the window (5 s target, no ≥15 s holes).
  Noise DGI is run-owned; an HTTP GET and successful persisted terminal result
  are required. Cached load success alone cannot prove a fetch or a write.
- Report render deadline misses and per-core CPU busy percentages from PERF
  runtime deltas. No additional deadline/CPU threshold is invented. Lost
  diagnostics, absent CPU windows, dead display and unexpected session loss fail.

Per generation, after STATE Ready the helper matches the bot's post-offset
`display-ready` to the device DISPLAY epoch and checks its pc_id against PERF.
It records that `display_session` (pc_id, epoch, ready event), selects every
`display-sent` by pc_id/epoch/request-id, and every `display-result` by
pc_id/epoch/sequence. Device worker completions must carry the active epoch.
Old connection bridges can observe the republished command while closing;
`stale_bridge_sends` retains those sends as report-only diagnostics. They do
not satisfy a current request and do not fail the run. No bot teardown wait,
bot modification, restart-window widening or command retry is introduced.

LCD `underrun happens` lines are counted separately from audio underflows, as
in 13a. They remain a visible display observation. Tearing and actual native
coordinate orientation/range remain unverified until observed on hardware.
A successful gate is smoke evidence, **qualification=false**, **passed=false**;
`checks_passed` carries the contention result. F11 is a separate user test.

## Coordinator acquisition

Use normal bundles built from this step (P4 from a fresh generated sdkconfig).
The bundle pins target/audio provenance with `scripts/p4_bundle.py`; verify it
before taking the bench. The board restore hold verifies the factory backup
before any P4 flash, checks the board MAC, flashes and verifies each flash file,
then checks boot ELF identity. Before step 14, successful P4 runs keep the tested
image. Failure/abort, including contention gate failure, restores and verifies
factory under R7. S3 restores its image of record. Cleanup only deletes this
run's generated asset and clears the diagnostic persistence controls. A partial
failure stays under the restore hold. No C6 write is involved.

The coordinator starts the existing bot outside this helper. Pin its absolute
`server/bot.py` path and use the environment/profile fields below:

```text
entrypoint=server/bot.py
BOT_MODE=live-test
SERVER_PORT=7861
DISPLAY_RETRY_BUDGET=0
DISPLAY_HIL_COMMANDS=<absolute path to commands.json>
EVENTS_PATH=<bot-dir>/events.jsonl
```

Use the coordinator's credential-loading procedure; never print credentials.
Save its PID to `bot.pid`, uncoloured stdout to `bot.log`, and an identity JSON
with `bot_instance` from `board_smoke.process_token(pid)`, `bot_source_sha` and
`policy_source_sha` from `git rev-parse HEAD:server/{bot.py,demo_policy.py}`, and
`profile_manifest` containing the exact five public fields above (omit
EVENTS_PATH from those five). The helper checks working source hashes too. `bot.pid` must name the Python
child, not its `uv run` parent. Only the exact bot.py/demo_bot.py paths in this
`--bot-repo` server directory count (default `/home/khkramer/src/dg-esp32`); uv/shell launch wrappers are not extra Python
instances. Other projects' bots and their 7860/7871 ports are ignored and never
touched.
Record additional public STT/TTS/mixer settings in the run notes; no bot source
change is allowed. Keep the same process alive for both board legs.

Run from the P4 worktree, after the coordinator announces the bench hold:

```bash
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
# Set CONTENTION_BOT_DIR, CONTENTION_COMMANDS, CONTENTION_IDENTITY and BUNDLE
# to the coordinator's absolute paths. Repeat with --board echoear first.
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
  --with esptool==5.5.0 python hil/bench.py \
  python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_contention.py live --board p4xc --bundle "$BUNDLE" \
  --bot-dir "$CONTENTION_BOT_DIR" --commands "$CONTENTION_COMMANDS" \
  --bot-identity "$CONTENTION_IDENTITY" \
  --out "$HOME/src/evidence/echoear/p4/step-13b/run-p4xc"
```

It uses the verified Jabra long-prompt fixture every 30 s; coordinator acoustic
permission/placement applies. It serves immutable animation fixtures and an
18 KB noise DGI on 7874. Scene animation runs through `disp bench cat 30` (the existing 30 Hz scene
benchmark), remaining active through disconnect and DTLS reconnect.
The explicit `disp call-persistence 1` diagnostic permits in-session writes;
it is cleared at cleanup. This procedure requires enough free LittleFS space
for one noise image. The P4 product adapter erases only the authenticated named storage partition
before boot; see the storage preparation section. The acquisition does not
autoformat unknown data or erase individual unrelated files. The 300 s measured window starts after first Ready; setup,
flash, restoration and analysis are outside it. The restart handshake is inside.
F11 must remain disabled during this exact-one-restart contention window.

Offline commands (no bench access):

```bash
uv run --project server --frozen --offline --group dev python \
  hil/p4_contention.py score "$RUN" --out "$RUN/score-reviewed.json"
uv run --project server --frozen --offline --group dev python \
  hil/p4_contention.py pair "$S3_RUN" "$P4_RUN" --out "$PAIR_JSON"
```

The R2 pair validates both complete bench-display profiles with known load ID
`p4-contention-load`, exact wire fields and bot identity, plus the measured
per-generation checks. Historical S3 normal captures are timing references;
they do not replace the same-bot exact bench-display pair.

## Fixed bounds without bot-content gating

Existing `hil/*.py` remain frozen. Invoke the new adapter:

```bash
uv run --project server --frozen --offline --group dev python \
  hil/board_smoke_bounds.py --recheck "$RUN" \
  --bounds hil/fixtures/wire_contract_bounds.json \
  --out "$RUN/result-recheck-fixed-bounds.json"
# For a new calibration acquisition use the same board_smoke CLI through
# board_smoke_bounds.py, with --calibration --bounds <fixed JSON>.
```

Explicit bounds apply `measured()` even for calibration acquisitions.
Bot-content findings retain calibration semantics; `passed` and `qualification`
stay false. Rechecks are exclusive sidecars and retain the original result's
hash/error; they never overwrite it or upgrade its turn/resource/restore verdict.
Without explicit `--bounds`, the adapter preserves the old acquisition-only mode.

## F11: a real user double-tap

Do this separately from contention, on the candidate image and one existing
bench bot. Hold the bench throughout and use R7 restoration on failure/abort.
Enable `touch-enable 1` in a Ready live session. Save parsed
`touch-status` / `DEMO_TOUCH_STATUS` immediately before and after asking
the user to double-tap within the logical image: native [40,760) on each axis.
No injector command is permitted. Record the user observation time and the
Ready/reconnect generation lines. Save those status objects as JSON files.

```bash
uv run --project server --frozen --offline --group dev python \
  hil/p4_contention.py f11 before.json after.json --user-observed --out f11.json
```

Only a real driver double-tap increment, one delivered reconnect request, a
new session generation and unchanged injection count can produce `observed`.
No user tap, no session observation, or omission of `--user-observed` leaves
F11 **unverified**. Save raw coordinates/status if checking range/orientation;
no-tap polling cannot prove them. Disable touch after the separate test.


## Fix 1 build and smoke candidates

The final 13b fix-1 writer candidates are under
`step-13b/fix-1/resumed-after-fix3/`: `bundle-s3-normal`, `bundle-s3-demo`,
`bundle-s3-aec-normal`, `bundle-s3-aec-demo`, `bundle-p4xc-normal` and
`bundle-p4xc-demo`. Their exact ELF hashes and app sizes are in the latest
13b report. Select **bundle-s3-normal in that directory** for the S3 contention
leg and normal smoke, not the post-matrix restored build directory. Four S3
behavioral smokes are owed for the additive main/performance.cpp change.
The source/hash and guard audits do not substitute for those smokes.

Earlier `step-13b/` and `step-13b/fix-1/` P4 images predate the pre-main RAM fix;
the original step-13b images also lack PERF.capture. Do not use them for this
contention gate. The later `step-13a/fix-2/` combined images passed the
coordinator's P4 display checks; they remain that historical evidence's pins.
The new candidates rebuild the same runtime sources with the strengthened
constructor-source audit and current source provenance. They need their own
hardware evidence before any new qualification or restore pin.

Touch is polled at **≤50 Hz**, lower when its non-audio owner is delayed. A
successful reset re-probe deliberately cancels the old contact with one driver
read_errors increment; re-probe failures have a one-second backoff. Consequently
this touch diagnostic must not be mistaken for the board-neutral mic overflow
counter or given an unapproved zero-error gate.


The ELF reader supports both historical internal storage and current external
BSS layouts. It derives placement from the actual candidate before any flash.
Keep the declared 114,928 B pre-main regression floor until a coordinator
measurement establishes a replacement. The known fix-2 boot confirms the
113 KiB RETENT_RAM line; earliest internal-free/pre-main consumption remains a
separate measurement. Retain initial heap-region lines and the earliest PERF
snapshot, with its time relative to DG_BOOT and before asset provisioning.

## P4 storage preparation

P4 live commands must use [p4_step14.py run](p4_step14.py), which applies the pinned restore policy and the p4_product storage adapter
shown above; frozen helpers called directly do not perform this step. See
[p4_product.md](p4_product.md) for the named-partition checks, erase/readback
evidence and coordinator decision. S3 behavior and offline analysis are unchanged.
