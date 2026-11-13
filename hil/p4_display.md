# P4 step 13a display coordinator procedure

> **STEP 14 RESTORE GUARD:** Live commands now require `hil/p4_step14.py run`
> and the clean-commit restore record/hash plus its successful boot proof from
> [the step-14 procedure](p4_liveness.md). Do not invoke frozen `p4_product.py`
> directly: its historical factory/keep-tested semantics do not enforce R7.
> The live commands below have been migrated; export `RESTORE_RECORD` and
> `RESTORE_RECORD_SHA256` from the newly approved record before copying them.


The writer does not access hardware. Build/test permission was granted during
implementation; host tests, both P4 builds and the S3/Waveshare matrix pass.
The commands below
also document reproducibility and the coordinator work still owed. Keep acquired evidence
under `~/src/evidence/echoear/p4/step-13a/`; never commit logs, results or images.
No touch test or C6 write belongs to this step.

## Writer gates after build permission

Use one build lock and fresh generated configs; never read or remove
`sdkconfig.local`. Both P4 normal and demo images need regeneration, as in 12b.
These are future coordinator/writer commands, not evidence of execution:

```bash
cd /home/khkramer/src/dg-esp32-p4
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
P4_DISPLAY_EVIDENCE="$HOME/src/evidence/echoear/p4/step-13a"
mkdir -p "$P4_DISPLAY_EVIDENCE"
TMPDIR="$HOME/.cache/gap3-tmp" scripts/host-tests.sh
python3 scripts/check_board_neutral.py

scripts/build_locked.sh bash <<'P4_BUILDS'
set -euo pipefail
export TMPDIR="$HOME/.cache/gap3-tmp"
cmake -S components/dg_board/test_host -B build/host-board
cmake --build build/host-board --target p4_audio_profile -j 4
for P4_DISPLAY_VARIANT in normal demo; do
  if [ "$P4_DISPLAY_VARIANT" = demo ]; then
    P4_DISPLAY_BUILD=build-p4xc-demo
    P4_DISPLAY_AUTOSTART=1
  else
    P4_DISPLAY_BUILD=build-p4xc
    P4_DISPLAY_AUTOSTART=0
  fi
  rm -f "$P4_DISPLAY_BUILD/sdkconfig"
  DG_BOARD=p4xc DG_DEMO_AUTOSTART="$P4_DISPLAY_AUTOSTART" scripts/idf.sh build
  python3 scripts/sdkconfig_keydiff.py "$P4_DISPLAY_BUILD/sdkconfig" \
    --expect-keys LCD_DSI_ISR_HANDLER_IN_IRAM,LCD_DSI_ISR_CACHE_SAFE
  uv run --project hil --frozen --offline python scripts/p4_bundle.py \
    --build "$P4_DISPLAY_BUILD" \
    --out "$HOME/src/evidence/echoear/p4/step-13a/bundle-p4xc-$P4_DISPLAY_VARIANT"
done
P4_BUILDS

TMPDIR="$HOME/.cache/gap3-tmp" scripts/capture_s3_baselines.sh step-13a
```

The target manifest adds only JD9365 **2.0.2** and its existing pinned CMake
utilities dependency. Let the managed resolver regenerate
`dependencies.p4xc.lock`; shared component versions must remain unchanged.
Manifest hashes may change in the S3/Waveshare locks; restore S3/Waveshare
lock graphs per the existing capture scripts. Review all generated lock diffs
before accepting them. No hand-written lock proof substitutes for resolution.

The five-image capture rebuilds S3 normal/demo/AEC-normal/AEC-demo and
Waveshare, regenerates configs, runs `check-aec-image.py` on **both** AEC
images, and restores the root lock after each variant. Its snapshots include
canonical config fingerprints. Compare each S3 fingerprint with
`~/src/evidence/echoear/p4/baselines/step-12b/manifest.json`; all four must be
identical (allow-list empty). For direct config keydiffs use the approved
`scripts/sdkconfig_keydiff.py` with the corresponding retained generated
step-12b configs, never print/copy their contents. AEC-demo's `config-accept`
requires the existing `capture_s3_baselines.keydiff_name` temporary rename
under the build lock, as in 12b.

Build four S3 smoke bundles with `hil/make_bundle.py --board echoear --build
<matching-build> --out <new-bundle-path>` **immediately after each matching
variant build**, before another variant rewrites that directory. Reuse the
12b `build_and_bundle.py` approach or `capture_s3_baselines.capture_variant`
inside a build-locked driver. S3 normal/demo share `build/`; both AEC variants
share `build/accept/`, so bundling all four at the end is invalid. Restore S3
normal via `DG_BOARD=echoear DG_DEMO_AUTOSTART=0 scripts/idf.sh build`, then
restore `dependencies.lock` if it was rewritten. P4 bundles must use the
metadata adapter above, which checks **all** overlay values and records the
actual target lock and audio defaults. Preserve dirty-source hashes because
this writer was instructed not to commit.

The display manifest is attached with `if: target == esp32p4` and
`idf_component_optional_requires(PRIVATE ...)` under P4 after registration.
No CONFIG-dependent REQUIRES is introduced. The two new overlay keys belong
to the already-transitive `esp_lcd` component; HANDLER_IN_IRAM resolves to y
(the spike/IDF default); CACHE_SAFE stays n,
so user callbacks may remain cached while the DMA re-arm path retains IRAM
placement. Flash operations still mask the interrupt. `--expect-keys` proves presence;
the bundle's overlay keydiff proves values. Inspect the P4 link command/map
for **both** `--wrap=mipi_dsi_hal_host_gen_read_dcs_command` and
`--undefined=__wrap_mipi_dsi_hal_host_gen_read_dcs_command` and the wrapper's
defined ELF symbol before flashing.

Shared changes in `dg_display/runtime.cpp`, `src/runtime_state.h`, the public
presentation/runtime headers and build files require S3 behavioural smokes.
Run the existing `board_smoke.py` procedure on all four matching S3 bundles,
with display checks enabled, using the unchanged demo bot and established
variant bounds/restore. These are additional to the display-only pair below.

## Existing bench bot and hardware entry

Release the build lock before entering `hil/bench.py`; never acquire the bench
lock with an outer flock. The helper reuses `p4_audio.HardwareIO` /
`board_smoke.LiveIO` flash and signal-safe restore holds, verifies the flashed
bytes separately, and uses pinned eFuse/chip/by-id board identity. P4 retains
the tested image on successful acquisition; failure/abort restores and verifies
factory with its boot banner. S3 always restores `55152f25` with boot proof.
An offline wire/histogram failure leaves a successfully acquired P4 installed.
Keep the verified factory backup available. No audio playback/recording occurs.

Use one **already-running unchanged** `server/bot.py` on 7861 for both sides
of each pair. The helper never starts/restarts a bot. The coordinator starts
that process under the existing bench service procedure, with:

```text
BOT_MODE=offline-test
SERVER_PORT=7861
OFFLINE_SKIP_GREETING=1
DISPLAY_RETRY_BUDGET=0
LOGURU_COLORIZE=NO
DISPLAY_HIL_COMMANDS=<absolute coordinator-owned command JSON path>
EVENTS_PATH=<bot-dir>/events.jsonl
```

Record `bot.pid`, uncoloured `bot.log`, and the existing wire identity schema:
`bot_instance` from `board_smoke.process_token(pid)`, `bot_source_sha` from
`git rev-parse HEAD:server/bot.py`, `policy_source_sha` from
`HEAD:server/demo_policy.py`, and `profile_manifest` containing the exact
settings above plus `entrypoint: "server/bot.py"`. The profile manifest is a
record of the launched process, not settings to apply to an unrelated process.
The helper checks process token, command entrypoint and committed/working bot
source hashes on both captures. Never use a demo-bot identity for this pair.

```bash
: "${BOT_DIR:?existing bench bot directory}"
: "${BOT_IDENTITY:?identity JSON for that bot.py process}"
: "${DISPLAY_COMMANDS:?absolute path matching DISPLAY_HIL_COMMANDS}"
P4_DISPLAY_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"

for P4_DISPLAY_VARIANT in normal demo; do
  for P4_DISPLAY_BOARD in echoear p4xc; do
    if [ "$P4_DISPLAY_BOARD" = echoear ]; then
      P4_DISPLAY_BUNDLE="$P4_DISPLAY_EVIDENCE/bundle-s3-$P4_DISPLAY_VARIANT"
    else
      P4_DISPLAY_BUNDLE="$P4_DISPLAY_EVIDENCE/bundle-p4xc-$P4_DISPLAY_VARIANT"
    fi
    "$P4_DISPLAY_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
      --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 \
      python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_display.py live --board "$P4_DISPLAY_BOARD" --bundle "$P4_DISPLAY_BUNDLE" \
      --out "$P4_DISPLAY_EVIDENCE/coordinator-$P4_DISPLAY_BOARD-$P4_DISPLAY_VARIANT" \
      --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY" --commands "$DISPLAY_COMMANDS" \
      --bot-repo /home/khkramer/src/dg-esp32 --seconds 30
  done
  uv run --project server --frozen --offline --group dev python hil/p4_display.py pair \
    "$P4_DISPLAY_EVIDENCE/coordinator-echoear-$P4_DISPLAY_VARIANT" \
    "$P4_DISPLAY_EVIDENCE/coordinator-p4xc-$P4_DISPLAY_VARIANT" \
    --out "$P4_DISPLAY_EVIDENCE/display-pair-$P4_DISPLAY_VARIANT.json"
done
```

The helper holds any configured demo and leaves audio off, then reuses the
fixture asset server, arena setup, explicit empty-only filesystem provisioning
and status-asset persistence. The P4 storage adapter first erases the authenticated named storage partition
under the coordinator decision below; empty-only provisioning then verifies it. It checks
disconnected/connecting/live `DISPLAY_DEMO_PROOF` records from newly committed
frames. Demo here tests the demo **image and screens**, not autonomous product
behaviour (step 14). Boot must match the bundle ELF and board/chip, with the
P4 DSI configuration banner. Touch stays off/stubbed.

The first bench-bot request is always `p4-display-load`; stale command JSON is
replaced **before** connect. `p4-display-show` follows on layer 7, with matching
bot admission, device terminal state and completed revision/layer evidence.
`wire.log` contains every record in the exact-wire window, from connect through
the measurement tail, before intentional disconnect/freeze. `serial.log` and
the single-boot `run-serial.log` retain cleanup and interval records as well.
The frozen `wire_contract` bench-display profile checks the known first request
ID and unchanged advertised wire limits/media. The pair records source/profile
identity; it does not claim product-profile voice parity.

## Border, latency and tearing observations

Inspect or capture all four 40-pixel black borders surrounding the centered
720×720 logical content. Record the bundle ELF, screen, placement and a photo
or webcam evidence path. Software commit proofs establish copy completion,
not optical appearance; the helper deliberately leaves border appearance
unverified. Pixel colors must be correct (red/blue must not be swapped).

`P4_SCAN` records use every 16th submission and actual frame-complete ISR
timestamps. `scan-latency.json` and the pair output contain 1 ms bins, exact
sampled max and p50/p95/p99, Presented sample count, Superseded count and
overwritten-timestamp count. `superseded_fraction` is all sampled Superseded
outcomes / total sampled records, including untimed outcomes in both counts.
Read it beside the quantiles: the Presented histogram describes surviving
submissions only. `underrun_lines` counts literal `underrun happens` lines in
the single-boot log, in acquisition and latency outputs, as a report-only
baseline for 13b. Only timed Presented samples enter the histogram.
An empty presented set or inconsistent timestamps fails analysis. Re-analysis:

```bash
python3 hil/p4_display.py latency \
  "$P4_DISPLAY_EVIDENCE/coordinator-p4xc-normal/run-serial.log" \
  --out "$P4_DISPLAY_EVIDENCE/scan-latency-reanalysis.json"
```

Expected timing at 54.112554 Hz is roughly one to two refresh periods after
msync (18.48–36.96 ms); report the actual distribution, superseded fraction
and late/untimed samples,
without turning that expectation into a qualification threshold. Preserve all
`esp_lcd` underrun diagnostics; Facts B already observed one per run at
≤60 MHz. Single-buffer tearing is **unverified** unless the user positions the
webcam and the coordinator captures moving content with adequate optical
evidence. Never claim tear-free from a fence, photograph or absent serial error.

Presented is an accounting fence on a single mutable framebuffer: a later
overlapping copy can affect the scan before that later copy commits. It is
not a preserved-image optical proof. Timestamps are taken when the ISR is
serviced; flash-op masking can overestimate latency and undercount completed
frames. Callback dispatch across IDs has no submission-order guarantee after
history wrap. A persistent Busy at the 256-entry bound indicates scan-complete
progress may have stalled. The static panel/history and init table consume
approximately 17.6 KB internal RAM; 13b must include that cost and driver
allocations in its measured heap gate. GPIO27 reset currently precedes all
touch probing; the RMW mutex does not exclude codec/touch transactions, so
13b must coordinate any subsequent reset and re-probe Goodix.

## P4 storage preparation

P4 live commands must use [p4_step14.py run](p4_step14.py), which applies the pinned restore policy and the p4_product storage adapter
shown above; frozen helpers called directly do not perform this step. See
[p4_product.md](p4_product.md) for the named-partition checks, erase/readback
evidence and coordinator decision. S3 behavior and offline analysis are unchanged.

## Display connection matching

After STATE Ready, the helper waits for a post-offset bot display-ready whose
epoch equals the device DISPLAY status epoch. It records this display_session
and matches sent packets by pc_id/epoch/request-id and acknowledgements by
pc_id/epoch/sequence. Closing connection bridges may still send commands from
the shared command file; stale_bridge_sends records those events as report-only
diagnostics. The active connection's send and acknowledgement remain required.
This uses the same pure selectors as the contention helper; the bot is unchanged.
