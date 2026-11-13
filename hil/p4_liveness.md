# Step 14 demo, liveness and recovery run book

Build/test permission was granted after the coordinator's bench run ended.
Commands below are the reproducibility and coordinator procedure; the writer
report records which host/build gates actually ran. Hardware commands remain
coordinator-only; no outer flock or build lock
may surround `hil/bench.py`. Run from `/home/khkramer/src/dg-esp32-p4`.

The new canonical P4 entrypoint is `hil/p4_step14.py run`, which delegates
through `hil/p4_product.py`. It requires a restore record plus its separately
supplied SHA256 and binds every inherited restore hold to that normal image.
The direct frozen entrypoints keep their historical behavior. Do not use them
for post-step-14 P4 acquisition. Factory remains explicit deep recovery using
`spikes/p4_probe/host/restore_factory.py`; product restore failures retain the
bench lock for the existing recovery/retry procedure.

P4 preflight scopes both the fixture manifest and HTTP origin to
`hil/fixtures/echoear360`, including modules already imported by frozen tool
preflight. The shared `demo_session.prepare_board` checks display storage before
loading fixtures: unavailable storage uses `disp provision-empty`, then must
report `health="ready"`. Firmware permits this only in Idle with unmounted
storage and verifies the entire named partition is erased before formatting;
unknown data is refused. Already-ready storage is preserved. A `fetch bytes=0`
failure after ready storage can instead indicate an HTTP status/length mismatch;
check that the requested hash is in the served fixture pack before diagnosing
another storage failure. S3 keeps its existing fixture and provisioning path.

**Fix round 1:** do not rely on the initial dirty-tree pin below. The coordinator
will retain the accepted fix-1 diagnostic normal/demo images, commit, rebuild
with fresh configs, compare against that matching firmware baseline with the
documented metadata masks, and create a new pin from that clean
commit. No pin is created or changed in this fix round. Before *any* acquisition,
run the explicit boot proof below against that new record and require its passed
result. Existing frozen Python files stay unchanged; prominent restore banners
and migrated commands in all older P4 run books prevent copying a legacy launch.

## Fixed checks

F18 requires strictly increasing ping and pong sequences within each channel,
a matching earlier ping for every pong, and an increasing app generation on
each restart within a boot. Cold boots explicitly begin a new boot namespace.
No old-channel pong may arrive once the next generation is Ready. The bot tap
records **all raw pongs before validation**, so ignored duplicates cannot hide
a violation. The same unchanged demo bot and observer serve both boards.

F19 bounds are fixed before acquisition: `wifi-drop` to WiFi ready ≤30 s and
new app Ready ≤60 s; replacement bot startup to app Ready ≤60 s; exactly 20
successful connect/disconnect cycles; internal minimum ≥81,920 B, largest and
sampled-largest minimum ≥32,768 B, zero allocation failures. Settled heap is
sampled after hold at baseline and after each cycle. Settled means three
consecutive samples spanning at most 256 B, bounded at 20 s with a 0.5 s poll;
every raw convergence sample is retained. After cycle 20, hold Idle for a
configurable drain (`--drain-seconds`, default and minimum **150 s**), then take
one more settled sample before the existing six-minute idle/reconnect check.
The internal-heap gate is **post-cycle-1 settled free minus drained final free
≤4,096 B**; the numeric limit is unchanged. This implements
`decision-2026-11-01-liveness-heap-drain.md`: round-2 diagnosis showed bounded
lwIP TIME_WAIT retention that recovered after the configured 120 s lifetime.
The first-session allocation cost and temporary in-run retention are excluded
from this gate by the chosen sample endpoints.

`heap_decline.report_only_metrics` explicitly labels baseline-to-cycle-20 net
decline, baseline-to-drained net decline, in-run running-peak decline (including
upticks), and internal drain recovery bytes as **report-only**. These are useful
diagnostics and do not change the drained 4 KiB bound. All live P4 samples,
including the drain's convergence samples, still pass the existing heap floors.
On the qualified S3 reference, the same drain and rule run, while heap floors,
convergence and the 4 KiB result remain **report-only**, per
`decision-2026-11-01-step14-reference-leg.md`; behaviour bounds remain identical.
Missing, short, reordered or mismatched drain evidence is **unavailable**, fails
acquisition/scoring on either board and never yields a pass. The runner saves
`heap-drain-start.json` before the wait and `heap-drain.json` with the wait/sample
timestamps, held-state witness and every final convergence sample. The score
checks that the complete drain lies after cycle 20 and before the later idle
check. Existing attempt-2 FAIL evidence remains unchanged; it has no drain sample
and cannot be rescored as passing under this rule. A fresh liveness run is owed.

The reconnect after ≥360 s held/disconnected idle
must become Ready within 60 s of its next request. Unsolicited C6 reset lines
are report-only. A reset disrupting a run produces an explicitly inconclusive
report, with no behavioural pass or failure inferred. It retains the original
`error` and returns **exit code 2** after clean restoration, with no cleanup
error. Every shell chain must stop on 2; the coordinator decides what to do next.
Only timestamped/cursor-bounded reset observations preceding the failure in its
current Ready window qualify. Old provisioning/reset lines, later cleanup output,
and a reset after a failure in the same polling batch cannot change that failure
to inconclusive. `Reset slave using GPIO[54]` is ignored only within recorded
boot-reset-to-READY windows; the same action mid-run is reset evidence.
Resets outside Ready windows (including boot, induced outages and rejoin waits)
are unclassified; any resulting failure remains an ordinary failure.
`Slave_Reset[54]` is a configuration table entry, never a reset event.
Recovery failures without such causal reset evidence still fail. There is no
real unsolicited-C6-reset capture yet: unrecognized slave output may still
surface as an ordinary session loss. Do not infer reset recovery from the
synthetic positive controls.

F20 uses the existing demo provision/hold/status/resume/cold-boot checks. Each
active session lasts at least 13 s and freezes peer-submit intervals once,
before intentional disruption. Assets are provisioned with the fixture server,
then that server exits before proof resets. Stored asset inventory and settings
must restore. The 6-minute idle is held/disconnected, with no connect request
until its end. The process startup marker is emitted immediately after uvicorn
startup, using the same host monotonic clock as serial receipt timestamps.

The HIL bot observer sends one diagnostic `display.fill` with a previous
epoch after a new channel advertises display-ready. Its result must be
`accepted=false,error=stale-epoch`, with the exact probe id and old epoch;
0 applied is tolerated. An old-epoch fill cannot change pixels. This is test
traffic, with no bot policy/source modification. Ping/pong generation safety is
also covered by the real firmware mailbox/state-machine host tests. No claim
of unsolicited C6 recovery or real physical touch is made by synthetic probes.

The liveness wire profile is explicitly the union of product evidence and
optional stale-probe result evidence. Product fields are validated with the
unchanged product validator; any additional result is validated independently
with the bench-display schema and a `step14-stale-N` request id. Nothing is
added to either frozen profile. Every candidate session preserves its wire
capture; offline score rehashes and revalidates it. The fixed demo timing
measurement and any violation are **report-only in this liveness campaign**.
Full product smokes still gate timing. There is no additional `stats` or
`disp status` inside a liveness interval window: epoch/channel come from the
observed display-ready; PERF/profile sampling happens after the interval freeze.
S3 image `5bbd868a` is reference-only and is never a wire-contract input.
Full product smoke below retains the ordinary product profile without probes.

## Build and host gates, after permission

Use fresh output labels and no firmware edits between building and bundling.
The five-image baseline script contains all four S3 builds, both AEC placement
checks and the Waveshare build. Its canonical config digests must equal step
13b; the allow-list is empty. Retain the private generated 13b config references
before rebuilding, without reading/printing their contents. `$STEP13B_REFS`
names a coordinator-supplied JSON mapping each variant to its retained private
`reference` config path. Use the preserved manifest from the actual accepted
13b build, not an assumed older baseline.

```bash
cd /home/khkramer/src/dg-esp32-p4
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export STEP14="$HOME/src/evidence/echoear/p4/step-14/clean-commit"
mkdir -p "$TMPDIR" "$STEP14"
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
scripts/host-tests.sh > "$STEP14/host-tests.log" 2>&1
scripts/check_board_neutral.sh > "$STEP14/r4.log" 2>&1
scripts/build_locked.sh bash <<'BUILD'
set -euo pipefail
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
cmake -S components/dg_board/test_host -B build/host-board
cmake --build build/host-board --target p4_audio_profile -j 4
for variant in normal demo; do
  autostart=0; build_dir=build-p4xc
  if [ "$variant" = demo ]; then autostart=1; build_dir=build-p4xc-demo; fi
  rm -f "$build_dir/sdkconfig"
  DG_BOARD=p4xc DG_AEC=0 DG_DEMO_AUTOSTART="$autostart" scripts/idf.sh build > "$STEP14/build-p4xc-$variant.log" 2>&1
  python scripts/check_p4_headroom.py --elf "$build_dir/dg-esp32.elf" --out "$STEP14/headroom-$variant.json"
  uv run --project hil --frozen --offline python scripts/p4_bundle.py --build "$build_dir" --out "$STEP14/bundle-p4xc-$variant"
done
BUILD
scripts/build_locked.sh bash <<'S3_BUILDS'
set -euo pipefail
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
python scripts/step14_s3.py --previous "$STEP13B_MANIFEST" --references "$STEP13B_REFS" --out "$STEP14" --label step-14-clean-commit > "$STEP14/r3-r3w.log" 2>&1
git diff --exit-code HEAD -- dependencies.lock
DG_BOARD=echoear DG_DEMO_AUTOSTART=0 DG_AEC=0 scripts/idf.sh build > "$STEP14/s3-normal-restored.log" 2>&1
git checkout -- dependencies.lock dependencies.waveshare146.lock
S3_BUILDS
git diff --exit-code 672aba5 -- server dependencies.p4xc.lock dependencies.waveshare146.lock dependencies.lock
```

The matrix adapter verifies the coordinator's retained reference hashes before
any build, compares each canonical config digest to `$STEP13B_MANIFEST`, runs
the names-only keydiff against `$STEP13B_REFS` while each config still exists,
and packages each S3 variant before its build directory is reused. Logs are
under `~/src/evidence/echoear/p4/baselines/step-14-clean-commit/<variant>/`, including
`keydiff-step13b.json` and both AEC placement logs. It restores root
`dependencies.lock` before each bundle and after each leg. No manifest,
component version or Kconfig change is declared in this step. Export both
`STEP13B_MANIFEST` and `STEP13B_REFS` to the actual accepted 13b artifacts before
running the block; absent or changed references fail before building.

After the clean commit/rebuild, pin only its newly built normal bundle; this command does no hardware
IO and does not claim a boot pass. Headroom evidence is bound to the exact ELF:

```bash
uv run --project server --frozen --offline --group dev python hil/p4_step14.py pin --bundle "$STEP14/bundle-p4xc-normal" --headroom "$STEP14/headroom-normal.json" --out "$STEP14/restore-of-record.json"
export RESTORE_RECORD="$STEP14/restore-of-record.json"
export RESTORE_RECORD_SHA256="$(sha256sum "$RESTORE_RECORD" | cut -d' ' -f1)"
```

Store the emitted record hash with the coordinator evidence. There is no
placeholder ELF hash or guessed pin in git. Every restore verifies record,
bundle, manifest, headroom and partition-table hashes. After verified flash it
erases **only the named storage partition** and verifies full FF readback,
then checks normal boot ELF identity. Storage is deliberately empty afterward;
normal mode has no asset requirement. Provision again for the next demo run.
NVS is preserved. Hold/resume/cold proof boots within a run never erase storage.
Restore erase/readback failures stay inside the inherited lock-retaining loop.

The initial writer produced a **historical dirty-tree pin, not the pin to use
for acquisition after fix round 1**, at
`/home/khkramer/src/evidence/echoear/p4/step-14/restore-of-record.json`:
record SHA256 `47431e0bdb29e64a6dec897421f009eeaaf06be459067a82e0535bca2f61b18c`,
normal ELF `f60a960a1f3142cb67965dde2b5013bbced85a9b9cfe480cc0d45d2fc6b72ca2`.
Use that exact hash with the existing record; do not re-run the exclusive pin
command into the same path. The demo ELF is
`7a71ee8cef7096845ca2c4506bc696853f81b4bc2be161a8216cfba4dbe3b8d0`.
Both headroom reports pass at 116,160 B (1,232 B above the floor), and both
bundles contain the P4 target-lock provenance. Boot/restore hardware proof is
still coordinator-owned. Keep this evidence immutable; the clean-commit rebuild
uses the fresh `clean-commit/` output root and emits a new record/hash.

## Required one-shot boot proof and deep recovery

Run this once after creating the clean-commit pin and **before any** smoke,
demo or liveness acquisition. It flashes/verifies/erases through the ordinary
candidate storage path and checks the normal boot identity. Its hold uses the
verified factory backup for failure recovery, so it cannot retry an unbootable
normal image. Successful proof leaves the pinned normal image installed.
Require `$STEP14/boot-proof/restore-boot-proof.json` to have `passed=true`, the
new record SHA/ELF, and a clean restore record before proceeding. This is the
**candidate flash-path** half of the proof; it does not certify the outer R7
restore path. The first normal acquisition below must prove that second half.
Preflight/authentication/IO-construction failures also write a failed proof
record with `device_untouched=true`.

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py boot-proof --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" --factory-backup "$HOME/src/evidence/echoear/p4/factory-20261031T060646Z" --out "$STEP14/boot-proof" || exit $?
```

**If the pinned image fails to boot, go to factory deep recovery; do not retry
the pinned image.** This rule also applies to later restore boot failures. The
step-14 adapter prints `PINNED IMAGE BOOT FAILED`, switches the retained hold's
next action to factory, and parks before a coordinator SIGUSR1. Identify the
actual Python runner PID printed as `Coordinator runner PID=...` **in the bench
shell's log**, not the uv parent; in the recovery shell set `RECOVERY_PID` to
that printed PID, then:

```bash
kill -USR1 "$RECOVERY_PID"
```

That command resumes **factory** recovery in the runner which already owns the
bench lock. Do not start another bench wrapper while that runner retains it;
never force-unlock or SIGKILL. If a factory attempt itself fails, investigate and
send the same SIGUSR1 to retry factory recovery only. A later explicit factory
restore with no runner holding the bench uses exactly:

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python spikes/p4_probe/host/restore_factory.py --backup "$HOME/src/evidence/echoear/p4/factory-20261031T060646Z" --port /dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00
```

The factory helper checks both pinned full backups, writes and separately
verifies the 32 MB image, hard-resets and requires the factory banner. No C6
write occurs. Rebuild/review/repin the normal image before trying acquisitions
again; a failed boot proof is not repaired by retrying the same image.
After factory recovery the acquisition's `restore.json` has
`action="p4-factory"`; even `clean=true` would mean factory restoration, not
that the pinned image was restored. A transient write/verify/storage IO failure
before boot is different: the hold retains the pin and retries it after SIGUSR1.
For a persistent pre-boot failure, select factory explicitly in the retained
runner. In the recovery shell, set `OUT` to that acquisition's exact `--out`
directory and `RECOVERY_PID` to its printed Python runner PID:

```bash
touch "$OUT/select-factory"
kill -USR1 "$RECOVERY_PID"
```

This first SIGUSR1 checks the sentinel before another pinned write, clears the
pin, marks the hold unsuccessful and parks again with `factory selected by
coordinator` in `restore.json`. Wait for `FACTORY SELECTED BY COORDINATOR` and
the subsequent recovery-wait prompt in that runner's log, then send the second
SIGUSR1:

```bash
kill -USR1 "$RECOVERY_PID"
```

The two SIGUSR1s after `touch` runs factory recovery; the bench lock stays held through
factory write, verification and banner. A transient failure with no sentinel
continues to retry the pin. A restore-console open or boot failure selects factory
automatically and needs only one resume. Keep the sentinel as recovery evidence;
use a fresh output directory for a new acquisition.
After **any exit 1**, inspect `restore.json` and check whether the board is on
factory. Before acquiring again, complete boot-proof for an authenticated normal
pin, or rebuild/review/repin and then boot-proof it; verify the pinned-restore
proof below before proceeding. A factory fallback is an unclean restore and
returns exit 1.
Product preflight verifies the factory backup pair even with a configured pin.

## Full product smoke and S3 shared-code debt

Use one unchanged ordinary demo bot process and identity launched as documented
in `hil/board_smoke.md` (without the observer/probes). `$BOT_CAPTURE` is its
directory; `$MAIN_REPO` is `/home/khkramer/src/dg-esp32`. Run the dry dependency
preflight first. Bot-content calibration is report-only, while `--bounds`
forces the previously fixed measured timing checks. Calibration results retain
`passed=false,qualification=false`; acquisition success is separate.

```bash
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/board_smoke.py --preflight-tools
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" smoke --board p4xc --bundle "$STEP14/bundle-p4xc-normal" --out "$STEP14/smoke-p4xc-normal" --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --calibration --pair-id step14-normal --bounds hil/fixtures/wire_contract_bounds.json || exit $?
uv run --project server --frozen --offline --group dev python - <<'RESTORE_PROOF' || exit $?
import json, os
from pathlib import Path
run=Path(os.environ['STEP14'])/'smoke-p4xc-normal'
result=json.loads((run/'result.json').read_text())
restore=json.loads((run/'restore.json').read_text())
storage=json.loads((run/'restore-storage-001/candidate-storage-erase.json').read_text())
assert result['acquisition_passed'] and not result.get('error')
assert restore==result['restore'] and restore['clean'] is True
assert restore['action']=='p4-product-of-record' and storage['readback_verified'] is True
RESTORE_PROOF
```

Stop if that restore-path proof fails; no demo or liveness acquisition may
follow. Before committing to the 20-minute liveness run, collect a short P4
held-idle jitter series and confirm the declared settle rule on the real board:

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py settle-check --bundle "$STEP14/bundle-p4xc-normal" --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" --factory-backup "$HOME/src/evidence/echoear/p4/factory-20261031T060646Z" --out "$STEP14/settle-check-p4xc" || exit $?
```

`settle-check.json` must pass all six convergence windows and report a clean
`p4-product-of-record` restore. It retains every raw PERF sample, per-window
start/end timestamps and held-free values; windows are separated by five seconds
(about 30 seconds total when stable). Review these data before starting liveness;
no P4 held-idle jitter validation is claimed by host tests. A non-converged S3
reference point is explicitly labelled `converged=false` in the decline report,
rather than being mixed silently into settled points.
These never-connected held-idle windows check convergence of the sampling rule;
they do not predict post-session teardown jitter or the 4 KiB decline.

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" smoke --board p4xc --bundle "$STEP14/bundle-p4xc-demo" --out "$STEP14/smoke-p4xc-demo" --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --calibration --pair-id step14-demo --bounds hil/fixtures/wire_contract_bounds.json || exit $?
for variant in normal demo aec-normal aec-demo; do
  python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py run smoke --board echoear --bundle "$STEP14/bundle-echoear-$variant" --out "$STEP14/smoke-echoear-$variant" --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --calibration --pair-id "step14-$variant" --bounds hil/fixtures/wire_contract_bounds.json || exit 1
done
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py run audio console-check --board echoear --bundle "$STEP14/bundle-echoear-normal" --out "$STEP14/console-echoear-normal"
```

The `normal`/`demo` legs of both boards must use the same bot process/identity.
Use these exact offline pair commands for `main` and demo's `resume`/`cold`
captures. They read fixed bounds and identical bot identities:

```bash
uv run --project server --frozen --offline --group dev python - <<'PAIRS'
import json, os, subprocess, sys
from pathlib import Path
root=Path(os.environ['STEP14'])
fixture=json.loads(Path('hil/fixtures/wire_contract_bounds.json').read_text())
for variant in ('normal','demo'):
    bound=fixture['variants'][variant]
    for label in (('main',) if variant=='normal' else ('main','resume','cold')):
        left=root/('smoke-echoear-'+variant);right=root/('smoke-p4xc-'+variant)
        command=[sys.executable,'hil/wire_contract.py',str(left/(label+'-wire.log')),str(right/(label+'-wire.log')),
            '--profile','product','--left-aec','0','--right-aec','0','--left-identity',str(left/'identity.json'),
            '--right-identity',str(right/'identity.json'),'--stall-us',str(bound['stall_us']),
            '--p99-delta-us',str(bound['p99_delta_us']),'--coverage',str(bound['coverage'])]
        with (root/('pair-'+variant+'-'+label+'.json')).open('x') as log:
            subprocess.run(command,check=True,stdout=log)
PAIRS
```

S3 smokes are owed because
`main/liveness.h`, `main/performance.cpp` and the host/build helper files change.
All four S3 variants are listed above to retain the broad R3 check.
The separate AEC-off console check closes the older step-11 cache-counters,
mic-analog-gain and explicit AEC-command rejection debt when the coordinator
runs it; host vocabulary checks do not close that live debt.

## Liveness acquisition, identical P4 and S3 reference checks

Run these after ordinary smokes, once the coordinator has ended its own earlier
bot process. The writer never starts/stops a bot. For each board use a fresh
observed bot directory and replacement directory. The adapter runs the unchanged
`server/demo_bot.py`; its command line still contains that absolute path and
the normal identity checker verifies its source and profile. Only metadata
ping/pong/display fields are recorded, with no audio, tokens or credentials.

```bash
export MAIN_REPO="$HOME/src/dg-esp32"
export S3_DEMO_RECORD="$HOME/src/evidence/echoear/aec-demo/step-10/requal-n1-bundles/demo"
export LIVE_BOARD=p4xc
export LIVE_BOT="$STEP14/liveness-$LIVE_BOARD-bot"
export NEXT_BOT="$STEP14/liveness-$LIVE_BOARD-restarted-bot"
mkdir "$LIVE_BOT"
ECHOEAR_AEC_SESSION_DIR="$LIVE_BOT" ECHOEAR_STT_PROFILE=demo PIPECAT_SCTP_MAX_CHUNK_SIZE=1100 LOGURU_COLORIZE=NO PYTHONUNBUFFERED=1 SERVER_PORT=7861 "$MAIN_REPO/server/.venv/bin/python" hil/p4_liveness_bot.py --events "$LIVE_BOT/dc-events.jsonl" --identity "$LIVE_BOT/identity.json" --ready-file "$LIVE_BOT/ready.json" --stale-probes "$MAIN_REPO/server/demo_bot.py" --host 0.0.0.0 --port 7861 > "$LIVE_BOT/bot.log" 2>&1 &
echo "$!" > "$LIVE_BOT/bot.pid"
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_liveness.py live --board p4xc --bundle "$STEP14/bundle-p4xc-demo" --out "$STEP14/liveness-p4xc" --drain-seconds 150 --bot-dir "$LIVE_BOT" --bot-identity "$LIVE_BOT/identity.json" --bot-repo "$MAIN_REPO" --restart-dir "$NEXT_BOT" --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" --factory-backup "$HOME/src/evidence/echoear/p4/factory-20261031T060646Z" || exit $?
```

Wait for the observed bot's `ready.json` before starting acquisition. A second
coordinator shell must already be polling for `liveness-p4xc/restart-request.json`
before the acquisition starts (about one minute
after provisioning). Verify its bot identity matches the process you own, then
stop **only that process**, and restart it in the requested new directory:

```bash
kill -TERM "$(cat "$LIVE_BOT/bot.pid")"
mkdir "$NEXT_BOT"
ECHOEAR_AEC_SESSION_DIR="$NEXT_BOT" ECHOEAR_STT_PROFILE=demo PIPECAT_SCTP_MAX_CHUNK_SIZE=1100 LOGURU_COLORIZE=NO PYTHONUNBUFFERED=1 SERVER_PORT=7861 "$MAIN_REPO/server/.venv/bin/python" hil/p4_liveness_bot.py --events "$NEXT_BOT/dc-events.jsonl" --identity "$NEXT_BOT/identity.json" --ready-file "$NEXT_BOT/ready.json" --stale-probes "$MAIN_REPO/server/demo_bot.py" --host 0.0.0.0 --port 7861 > "$NEXT_BOT/bot.log" 2>&1 &
echo "$!" > "$NEXT_BOT/bot.pid"
```

Wait for the old process to exit and 7861 to be released before restarting; do
not replace another process. The acquisition allows 180 s for coordinator
handoff, then scores ≤60 s from the replacement server's actual startup marker.
Unexpected process changes elsewhere fail. The device remains inside its armed
restore hold throughout; cleanup or acquisition failure restores the pinned
normal image (S3 always restores `55152f25`). The new drain adds about 2.5 minutes
per run; allow about 22.5 minutes with the default 150 s tail. Longer configured
drains add their extra duration. The inherited hold stays armed through the
drain, scoring and cleanup; failed/missing drain evidence restores as usual.

Repeat the initial observed launch with `LIVE_BOARD=echoear` and fresh paths,
then run this exact S3 reference command. `$S3_DEMO_RECORD` must name the already
verified image-of-record bundle with ELF `5bbd868a…`; do not substitute a current
candidate. Repeat the same coordinator stop/start handoff above when requested:

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_liveness.py live --board echoear --reference-only --bundle "$S3_DEMO_RECORD" --out "$STEP14/liveness-echoear" --drain-seconds 150 --bot-dir "$LIVE_BOT" --bot-identity "$LIVE_BOT/identity.json" --bot-repo "$MAIN_REPO" --restart-dir "$NEXT_BOT" || exit $?
uv run --project server --frozen --offline --group dev python hil/p4_liveness.py score "$STEP14/liveness-p4xc" --out "$STEP14/liveness-p4xc/score-recheck.json" || exit $?
uv run --project server --frozen --offline --group dev python hil/p4_liveness.py score "$STEP14/liveness-echoear" --out "$STEP14/liveness-echoear/score-recheck.json" || exit $?
```

For a contention recheck with an explicit top-level unmeasured summary, use
`hil/p4_step14.py run contention score RUN --out NEW_JSON` (or `pair S3 P4`).
It is offline, needs no restore record or bench wrapper, and never rewrites
the original result. A dead S3 overflow counter yields
`capture_overflows_summary="unmeasured (report-only)",all_checks_measured=false`
while preserving the scoped `checks_passed` field.

Real touch F11, calibrated AEC and unsolicited C6 reset recovery remain separate
coordinator work. Step-12b acoustic eligibility edge cases and old documentation
API cleanups are not changed here. The 13b worker-epoch and PERF pc_id negative
controls are included in the new host tests. The 12b private-defaults temporary
config issue is closed by omitting `sdkconfig.local` from audio-only re-resolution.

## Carry-over to step 16

The current new-pin campaign is described below; older commands remain scoped
to their original step-14 reference run.

## Step 16 parity run

Use [p4_parity.md](p4_parity.md) from the separate `dg-esp32-p4-s16` worktree
for the six new-pin candidate images. Its additive liveness runner covers
normal/manual, demo and AEC-demo on both boards, with F20 only on demo.
The frozen runner above remains limited to its original reference campaign.
Do not pass a new S3 pin through `--reference-only` or inherit that reference's
resource waiver. Builds/tests wait for explicit bench-free go; live parity
waits for successful new-pin verification and coordinator execution.

The step-15 pin round supersedes all earlier restore/image selections for
step 16. Load the exact new record and SHA256 from
`~/src/evidence/echoear/p4/step-15-pin-round/pin.env`, then run the boot proof
and four normal-image smokes in [p4_pin_round.md](p4_pin_round.md) before parity.
The historical step-14 pin commands above remain evidence of that round.

Historical step-14 product/contention entrypoints **must use `p4_step14.py run`**
for the negated-city requirement and contention unmeasured summary. The new
parity smoke wrapper delegates through it; the new display/liveness runners
apply the same restoring/candidate-storage lifecycle directly. Never invoke
the old frozen live readers directly for this campaign.
