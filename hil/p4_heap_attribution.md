# Step 14 heap-decline diagnosis (coordinator only)

Historical round-1 procedure. Its capture did not support the SDIO hypothesis.
Use [round 2](p4_heap_attribution2.md) for new builds, C++ caller recovery,
20-cycle series and the deferred-free settle tail; retain the round-1 evidence.

The original 20-cycle P4 failure and 4 KiB rule remain unchanged. This is a
separate, bounded six-cycle diagnostic; it never reports qualification/pass.
The original serial confirms SDIO streaming mode. The pinned hosted driver
keeps two receive buffers, grows each to the largest observed transfer and
allocates them from internal DMA memory (`sdio_rx_get_buffer` →
`hosted_malloc_align`). This is a strong candidate for the stepped plateaus,
not measured attribution. Look for those callers first; do not change buffer
policy or the baseline until the trace identifies the retained allocations.
Use an existing ordinary demo bot and its captured identity as in
[board_smoke.md](board_smoke.md). The writer does not run hardware or a bot.

P4 tracing is compiled only with the explicit overlay below. Default builds
and S3 have no new command, buffer or trace overhead. The diagnostic image uses
frame pointers, eight frames, size optimization and warning-level bootloader
logs to fit the unchanged partition layout and startup-headroom gate. Its
performance/heap costs differ from shipping images; do not score its timing.
The first Idle `heap-attribution init` allocates 4096 records in PSRAM, before
the baseline. Each window starts before `demo run`, stops after `demo hold`,
PHASE closed and the existing three-sample settled check. No allocation records
are exported during a session. All console commands enforce Idle. Overflow or
missing/reordered pages invalidates attribution. Raw serial/start/stop/page
records remain available on failure.

## Build the opt-in P4 demo, never repin it

Run from `/home/khkramer/src/dg-esp32-p4`. Use fresh evidence/output directories.
The build consumes sdkconfig.local without displaying or copying it.

```bash
export HD="$HOME/src/evidence/echoear/p4/step-14/heap-diagnosis-1"
mkdir "$HD"
export TMPDIR="$HOME/.cache/gap3-tmp"
scripts/build_locked.sh bash <<'BUILD'
set -euo pipefail
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
export TMPDIR="$HOME/.cache/gap3-tmp"
cmake -S components/dg_board/test_host -B build/host-board
cmake --build build/host-board --target p4_audio_profile -j 4
rm -f build-p4xc-heapdiag-demo/sdkconfig
DG_BOARD=p4xc DG_DEMO_AUTOSTART=1 DG_AEC=0 scripts/idf.sh -B build-p4xc-heapdiag-demo '-DSDKCONFIG_DEFAULTS=boards/common/sdkconfig.defaults;sdkconfig.local;boards/p4xc/sdkconfig.defaults;sdkconfig.defaults.demo;boards/p4xc/sdkconfig.defaults.heap-attribution' build
uv run --project hil --frozen --offline python scripts/p4_heap_bundle.py --build build-p4xc-heapdiag-demo --out "$HD/bundle-p4xc-diag"
BUILD
```

The bundle emitter authenticates the resolved diagnostic keys, normal P4 audio
metadata and artifact hashes. The runner rejects a default P4 bundle before
constructing live IO. Keep the previously verified normal restore pin below;
the diagnostic demo is never an image of record. The ordinary step-14 recovery
and select-factory procedure still applies; keep the runner-owned bench lock
until its verified restore completes.

## Six P4 cycles and offline attribution

Set BOT_CAPTURE to the existing ordinary bot capture whose source/profile you
own. MAIN_REPO identifies that unchanged bot checkout. STEP9A_FIXTURES is set
before frozen preflight imports for both commands, including S3 contrast.

```bash
export MAIN_REPO="$HOME/src/dg-esp32"
export BOT_CAPTURE="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/smoke-bot"
export RESTORE_RECORD="$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/restore-of-record.json"
export RESTORE_RECORD_SHA256=730d7b2bf342427ca460400bd32053b3dd837004b8c98fc2b568256050d6d84a
export STEP9A_FIXTURES="$PWD/hil/fixtures/echoear360"
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_heap_attribution.py live --board p4xc --bundle "$HD/bundle-p4xc-diag" --out "$HD/p4xc" --cycles 6 --active-seconds 5 --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" --factory-backup "$HOME/src/evidence/echoear/p4/factory-20261031T060646Z" || exit $?
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server uv run --project server --frozen --offline --group dev python hil/p4_heap_attribution.py analyze "$HD/p4xc" --elf "$HD/bundle-p4xc-diag/dg-esp32.elf" --out "$HD/p4xc-analysis.json"
```

Inspect each cycle's candidate component bytes/blocks and full symbolized
callers. The decoder checks hosted/wifi_remote RPC/SDIO, lwIP PCB/pbuf,
mbedTLS/DTLS, esp_peer, display/touch and console/performance paths explicitly.
Component hints are inferred from bounded callers, with opaque/prebuilt code
kept unclassified. PCs are decoded against the exact ELF hash in image.json;
never use the current build directory's ELF instead. All raw samples, trace
pages, window identifiers and restore evidence stay in the run directory.

The PSRAM trace buffer excludes ISR allocations. The trace also excludes
allocations made before a window, allocator metadata, and frames beyond eight;
prebuilt vendor code may lack frame pointers/debug information. A zero bucket
does not exonerate a component. Requested retained bytes need not equal the
free-heap delta, and independent windows must not be summed as a net leak.
The report includes the native start/stop free delta and its difference from
retained requested bytes, alongside the PERF settled delta. Startup/first-session
costs in this short run differ from the original campaign's earlier recovery
phases; preserve cycle 1 separately when interpreting lazy initialization.
Overflow requires a new diagnostic design/window, not a relaxed pass bound.

## Same six-cycle contrast on the unchanged S3 reference

This command makes no attribution firmware change on S3. It captures the same
settled heap/task census after the same six lifecycle cycles; allocation
ownership is explicitly unavailable on that reference, never reported as zero.
Use a fresh run directory and the same ordinary bot/profile.

```bash
export S3_DEMO_RECORD="$HOME/src/evidence/echoear/aec-demo/step-10/requal-n1-bundles/demo"
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_heap_attribution.py live --board echoear --reference-only --bundle "$S3_DEMO_RECORD" --out "$HD/echoear" --cycles 6 --active-seconds 5 --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" || exit $?
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server uv run --project server --frozen --offline --group dev python hil/p4_heap_attribution.py analyze "$HD/echoear" --out "$HD/echoear-analysis.json"
```

Both runners keep their armed restore hold through acquisition, parser and
cleanup failures. A P4 diagnostic succeeds only after a clean pinned-normal
restore; S3 keeps its frozen normal-restore policy. Any exit 1 stops the chain.
Follow the step-14 factory-state/boot-proof procedure before another acquisition
if recovery selected factory. No production fix is selected by this tool.

To re-derive the original failed/passing captures without hardware:

```bash
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server uv run --project server --frozen --offline --group dev python hil/p4_heap_attribution.py retained "$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/liveness-p4xc" "$HOME/src/evidence/echoear/p4/step-14/pin-round/commit-2b2b04ee/liveness-echoear" --out "$HD/original-comparison.json"
```
