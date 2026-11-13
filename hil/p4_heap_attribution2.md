# Heap diagnosis round 2 (coordinator only)

Round 1 did not support the SDIO buffer hypothesis: hosted/RPC/SDIO retained
zero traced bytes in all six cycles (ISR coverage remains excluded). P4's steady
decline was about 428 B/cycle; two `tcp_alloc` PCBs totalled 416 B at each stop.
Another 488 B/11 C++ allocations had callers hidden by non-frame-pointer
libstdc++. S3's internal heap was nearly flat after cycle 1 while PSRAM declined.
These are retained-at-stop windows, not sums of leaked bytes.

Use this additive round-2 entrypoint, not the historical six-cycle runner.
Shipping defaults, the production pin and the original 20-cycle/4 KiB gate stay
unchanged. This tool reports diagnosis only (`passed=false`, `qualification=false`).
The native option still builds only on P4. Its new/delete wrappers call the real
standard operators and store the wrapper's return PC in a fixed 8192-entry PSRAM
table. Array, sized, aligned and nothrow variants are covered; existing allocator
placement, new-handler and exception behavior are preserved. Caller-table overflow
invalidates C++ caller attribution for that window; the run continues to preserve
the heap series, malloc stacks, other windows and restoration. Those blocks carry
`new_caller_status=invalid-table-overflow` and their PCs do not inform component
hints. A PC inside libstdc++/libsupc++ or the C++ runtime is `runtime-internal`;
it does not identify the requester. Zero caller means unavailable/not a wrapped
new, never a C-allocation proof. Vendor calls resolved within the same object may
bypass linker wrapping.

Each cycle exports records only after it is closed. With `--settle-seconds 150`,
the **last** trace stays open during the final 150-second Idle tail, with heap
and TCP snapshots every five seconds. No stop/restart gap is introduced. Earlier
cycles retain their immediate traces. The final trace is labelled after-settle
only when a nonzero tail was requested (`after-cycle` with `--settle-seconds 0`),
separate from the final cycle's immediate heap/TCP snapshot. Raw points and
start/stop/pages survive errors; missing points, a short tail, heap-trace overflow, replay
or an ELF mismatch invalidates analysis. The usual retained R7 hold applies.

The Idle-only `heap-attribution tcp` census reads active/TIME_WAIT/bound lists
in the lwIP core context. Immediately after init, before the baseline heap sample
and every trace, the runner warms up this command at window 0 and saves
`tcp-warmup.json`. This places the console thread's one-time tcpip semaphore/TLS
allocations outside the measured series. Constants in both the bundle and census
are **compile-time values**; their equality checks build configuration consistency.
The list counts are runtime observations. Listening PCBs and PCBs on none of the
three lists (never bound/connected, or already unlinked) are omitted. Zero counts
do not rule out retained TCP allocations. TIME_WAIT expires when the slow timer exceeds 2×MSL;
150 s is beyond the configured 120 s threshold. A drained list and recovered
heap support deferred-free attribution; they do not prove every byte's owner.
Reported heap recovery subtracts the final-cycle `settled_heap` sample from the
last tail `io.snapshot`; with no tail both endpoints use the same settled sample.

## Reuse the delivered round-2 diagnostic demo

Run from `/home/khkramer/src/dg-esp32-p4`. Keep an existing ordinary demo bot and
its identity from [board_smoke.md](board_smoke.md); the writer never runs it.
All run output directories must be fresh. Fix 5b changes the host runner/analyzer
only. Keep the delivered bundle unchanged; do not rebuild or repin this image.

```bash
export HD="$HOME/src/evidence/echoear/p4/step-14/heap-diagnosis-2"
mkdir "$HD"
export TMPDIR="$HOME/.cache/gap3-tmp"
export DIAG_BUNDLE="$HOME/src/evidence/echoear/p4/step-14/fix-5/bundle-p4xc-diagnostic"
sha256sum "$DIAG_BUNDLE/dg-esp32.elf"
```

Expected ELF SHA256:
`04e2573c0d768daf5fb399f67bf0710935dfa19c2f89cfa0311082bcd1d3d815`.
The existing bundle records its original fix-5 source snapshot; the fix-5b report
records the updated host sources separately. Its diagnostic overlay retains the
prior frame-pointer/size-optimization settings and does not alter TCP lifetimes,
PCB limits or shipping options. Its numeric TCP constants came from a compile-only
probe using the build's own flags, without exporting config text.

## P4: 20 cycles, then a 150-second tail

Set BOT_CAPTURE to the existing ordinary bot capture that you own (the prior
round used the directory below); its process/identity must still match. Export
the fixture pack before any frozen preflight import. Use the existing verified
normal-image restore record, not the diagnostic bundle.

```bash
export MAIN_REPO="$HOME/src/dg-esp32"
export BOT_CAPTURE="$HOME/src/evidence/echoear/p4/step-14/heap-diagnosis-1/bot"
export STEP9A_FIXTURES="$PWD/hil/fixtures/echoear360"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_heap_attribution2.py live --board p4xc --bundle "$DIAG_BUNDLE" --out "$HD/p4xc" --cycles 20 --active-seconds 5 --settle-seconds 150 --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" --factory-backup "$HOME/src/evidence/echoear/p4/factory-20261031T060646Z" || exit $?
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server uv run --project server --frozen --offline --group dev python hil/p4_heap_attribution2.py analyze "$HD/p4xc" --elf "$DIAG_BUNDLE/dg-esp32.elf" --out "$HD/p4xc-analysis.json"
```

For a shorter first check, change only `--cycles 20` to `--cycles 6`. Supported
limits are 1..20 cycles, 1..30 active seconds and 0..600 settle seconds (default
zero). The original qualification bounds are untouched. Inspect each of the 11
C++ blocks' `new_caller_symbols`; these are distinct from the old malloc frames.
The report separates internal/PSRAM/other retained blocks and reports both full
per-cycle free series. Plateau status is descriptive: the exact last-five values,
range and deltas are shown; there is no new tolerance or qualification pass.

## S3 contrast, unchanged reference image

The same loop and 150-second heap sampling tail run without any new S3 firmware
or native command. Allocation ownership/TCP census remain explicitly unavailable
on this frozen reference. Compare its PSRAM series as well as internal free.

```bash
export S3_DEMO_RECORD="$HOME/src/evidence/echoear/aec-demo/step-10/requal-n1-bundles/demo"
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_heap_attribution2.py live --board echoear --reference-only --bundle "$S3_DEMO_RECORD" --out "$HD/echoear" --cycles 20 --active-seconds 5 --settle-seconds 150 --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" || exit $?
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server uv run --project server --frozen --offline --group dev python hil/p4_heap_attribution2.py analyze "$HD/echoear" --out "$HD/echoear-analysis.json"
```

Any exit 1 stops the chain. The existing factory-selection/retry and boot-proof
procedures still apply before acquiring again after failed recovery. No shipping
fix is chosen automatically. ISR/pre-window allocations, allocator overhead,
bounded depth and opaque vendor code remain attribution limits. Preserve cycle 1
and all raw evidence; do not sum the per-window surviving allocations as leaks.
The delivered firmware also retains the reviewed P3 limits: tombstones can cause
long PSRAM probe chains under the global critical section, each window clears
64 KiB inside that section, and `tcpip_api_call` has no timeout. The caller-table
lock serializes new/delete across cores and can perturb timing. A wedged tcpip
thread can wedge the console; host timeout/cleanup and the existing R7 restoration
hold remain the recovery path. No firmware P3 is changed by fix 5b.
