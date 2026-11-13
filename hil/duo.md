# Duo HIL run book

For repeatable end-to-end routing checks (prepared phrases into the web
simulator or the EchoEar, each turn scored against whom it addressed) see
[duo_e2e/README.md](duo_e2e/README.md).

The coordinator runs this book after step 6 supplies verified duo bundles and
the firmware includes `duo tap`, `taps_injected` and `taps_physical`. The writer
runs offline tests only. Use `~/src/dg-esp32-ad` on `angel-devil`; never substitute the frozen
legacy smoke or touch scorer. Both boards use AEC and autostart.

## Offline preparation

First complete the private deployment build, image gates and bundling below.
Step 7 uses those private bundles; the public-seed bundles are offline validation
artifacts. Fill in the deployment gate record before starting any bench part.

```bash
cd "$HOME/src/dg-esp32-ad"
umask 077
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export DUO_ROOT="$HOME/src/evidence/duo/step-7/run-1"
export DUO_DEPLOY="$HOME/src/evidence/duo/step-6/deployment-private-fix3"
mkdir -p "$TMPDIR" "$DUO_ROOT"
duopy() {
  uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python "$@"
}
export -f duopy
# Verify the private deployment bundles already built and gated below.
duopy hil/duo_bundle.py verify --board echoear --bundle "$DUO_DEPLOY/bundle-echoear-duo"
duopy hil/duo_bundle.py verify --board p4xc --bundle "$DUO_DEPLOY/bundle-p4xc-duo"
duopy hil/duo_smoke.py rules
TMPDIR="$TMPDIR" scripts/host-tests.sh --duo
```

Builds, when needed, use `DG_BOARD=echoear DG_VARIANT=duo scripts/idf.sh build`
or `DG_BOARD=p4xc DG_VARIANT=duo scripts/idf.sh build`. Build one at a time;
restore rewritten dependency locks. The bundle adapter verifies the image
checksum, chip ID, ELF SHA binding and duo symbols. It exports only approved
configuration counts, AEC gain and autostart metadata. Source/dependency
provenance is explicitly recorded-only: the verifier does not reconstruct
that provenance from firmware, and the CLI labels it `recorded-only`. It does not replace the
step-6 audited AEC placement, P4 headroom or nine-pair legacy image gates.

The restore pin remains the existing P4 R7 record under
`~/src/evidence/echoear/p4/step-15-pin-round/commit-52482495d1b859b5313cd8c76e4cd0cdd97ea507/restore-of-record.json`,
SHA-256 `a2c846832b43636fc73564d6a1d32c22ebcbc95458c8906de0d0550f028d653b`.
S3 restores `55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e`.
The driver reuses `p4_step14.restoring`, the frozen hold's signal protection,
recovery loop and named-storage policy. All cleanup and restore finish before
the inherited bench lock is released. A dirty restore fails the part. Recovery
keeps the lock and follows the existing R7 SIGUSR1/operator workflow.

## Fixture origin and console

The cat, status screens and both duo packs share `hil/fixtures/echoear360`.
The runner owns this fixture-only origin on 7874 during installation, closes
it, and only then reboots for the offline restore check. Its exact standalone
equivalent, for coordinator diagnosis while no runner owns 7874, is:

```bash
STEP9A_FIXTURES=hil/fixtures/echoear360 duopy -c \
  'from step9a_assets import asset_server; import threading; s=asset_server(); s.__enter__(); threading.Event().wait()'
```

The installer requests `/<sha256>.dgi`. In an already-owned console, use:

```text
demo hold
duo origin http://192.168.7.222:7874
duo mem
demo configure {"url":"http://192.168.7.222:7861","volume_db":-6,"enabled":true}
demo run
duo status
duo tap
```

`duo origin` requires Idle. `duo origin clear` restores the empty default.
`DUO_INSTALL result=ready` means every required hash was verified and pinned.
Error reasons include missing-origin, not-loaded, fetch, flash_full, hash,
invalid-dgi, ram_budget, catalog-full and storage_unavailable. Failed installs
release residency pins, retain successful flash files, and retry while idle
(5 seconds). Corrupt content requires explicit repair. Storage provisioning is
explicit; the installer never formats or evicts files. `screen=unavailable`
means the disconnected screen itself could not be verified.

P4 setup reuses the named-storage erase/readback helper and explicitly runs
`disp provision-empty` on that erased, unmounted storage. The S3 **install**
part now runs a scripted pre-step under its armed restore hold: it boots and
authenticates the pinned normal S3 `55152f25` image, uses its idle display
console to delete only duo hashes present in the first inventory (from the
26 hashes in `duo-pack.json`), and records present/absent sets plus before/
after inventory proving every duo hash absent and every other entry unchanged.
Using normal firmware avoids the duo image's residency pins; `demo hold` alone
would leave those pins and make deletion fail. It then flashes/authenticates
the declared duo candidate. Other parts may reuse cached assets.

The exact existing console operations used by that pre-step are:

```text
demo hold
disp init
disp delete {"id":"duo-clean-00","hash":"<first sorted present duo SHA-256>"}
disp result duo-clean-00
```

The script repeats delete/result for the sorted intersection of the duo hash
set and first inventory, with at most 26 requests. A fresh S3 issues zero
deletes; a partially installed S3 deletes only the present subset. Already
absent hashes are recorded without requesting deletion. It waits for each
terminal `error=none`, and pages `disp inventory 0 0`
(and the returned cursor/revision) before and after. Evidence is in the
attempt's `fresh-storage/` directory. It never erases or re-provisions S3
storage, and leaves cat, status and all non-duo entries intact.

Every part then resets the **candidate's NVS partition on both boards**, before
flashing the duo bundle, so an earlier persisted devil cannot change the initial
angel witness. `duo_nvs.py` verifies the bundle and partition-table digest/MD5,
finds the unique named `nvs` data/NVS entry, and derives its offset and size from
that table. Invalid, overlapping or encrypted entries and flash inputs that
would repopulate NVS are refused. No NVS address or size is hard-coded.
Under the already-armed restore hold and the same inherited bench-lock fd, it
checks the board identity, runs esptool `erase-region`, then `read-flash` over
the entire region and requires the exact size and all `0xFF` bytes before the
candidate flash can proceed. Both commands leave the board in ROM; serial
capture opens before the explicit reset into the candidate app.

Each attempt retains `candidate-nvs-erase.json` (bundle/table identities,
partition offset/size, commands, completion/error and readback checks),
`candidate-nvs-{erase,readback}.log` and `candidate-nvs-readback.bin`. Erase or
readback failure is final, never a retryable missed injection window. Only NVS
is erased here: the S3 pinned-normal asset-deletion pre-step runs first and is
unchanged; the P4 named-storage policy and frozen restore lifecycle are unchanged.
The offline reboot within an install part does not erase NVS again.

After every candidate boot, setup reapplies `demo configure` with the chosen
server URL, volume -6 dB and disabled autoconnect, then `duo origin` before
installation. Non-install parts enable the configured demo before connecting.
NVS-backed demo settings, origin and last character are deliberately reset for
each part; within-part persistence behavior is unchanged. Wi-Fi credentials
come from the deployment image: `main/main.cpp` passes the compiled config
symbols to `dg_net_init`, and `components/dg_net/dg_net.c` selects
`WIFI_STORAGE_RAM`. NVS initialization and settings readers accept an empty
store and use their defaults, including angel. The restore image therefore
uses its compiled network/default settings rather than candidate NVS settings;
the coordinator retains the usual restore boot/identity checks.

## One board and one resumable part

Each part has a fresh directory and its own declaration, identity, raw bot
and serial evidence, result and restore. Existing directories are refused.
Resume by running the next part; failed attempts remain on disk. The driver
registers `attempt-01` and, only after a clean, proven missed injection window,
permits `attempt-02` under the same campaign/board/part. Every other failure
is final, as is a success. A phase never reached before injection is final;
only a valid toggle witnessed at wire receipt outside its requested generating
or playback window can earn the sole retry. An ErrorFrame, FatalErrorFrame,
cancellation or pipeline failure between stimulus and wire receipt is final,
even if the switch succeeds. An interruption of that response before receipt is not a clean
miss; the toggle's own interruption after receipt is allowed. Only ordinary
response completion or playback progressing beyond the window earns retry.
Cleanup, restore or panic failure
cancels that eligibility. `attempt-03`, missing/changed earlier results and
unregistered attempt directories are refused. There are no automatic retries.
`attempts.json` and `attempts-summary.json` retain and report both attempts,
including latency and the first result digest. Run the following block
separately for each table entry, choosing
`DUO_BOARD=echoear` first, then `p4xc`.

| PLAN check | `DUO_PART` values, in order |
|---|---|
| 1 | `install` |
| 2 | `connect` |
| 3 | `idle`, `generating`, `playback` |
| 4 | `drop-state-1`, `drop-state-4` |
| 5 | `tts-fail-next` |
| 6 | `quiet` |
| 7 | `turns` |
| 8 | `touch` |

```bash
# Set these for each invocation. Use 02 only when attempt-01 result says retryable=true.
export DUO_BOARD=echoear
export DUO_PART=install
export DUO_ATTEMPT=01
# Check outside the bench-owning shell, before every part; never evict a holder.
command -v fuser >/dev/null || { echo 'fuser is required for the pre-part checks' >&2; exit 1; }
if fuser -s 7861/tcp; then
  echo 'Port 7861 is still in use; wait for the previous bot to exit' >&2
  exit 1
fi
if test -e /tmp/claude-1000/echoear-bench.lock && fuser -s /tmp/claude-1000/echoear-bench.lock; then
  echo 'Bench lock still has an open holder; wait for cleanup/restore to finish' >&2
  exit 1
fi
python3 hil/bench.py bash --noprofile --norc <<'COORDINATOR'
set -euo pipefail
DUO_OUT="$DUO_ROOT/$DUO_BOARD/$DUO_PART/attempt-$DUO_ATTEMPT"
DUO_BOT="$DUO_ROOT/bots/$DUO_BOARD-$DUO_PART-$DUO_ATTEMPT"
DUO_FAULT_ARG=()
case "$DUO_PART" in
  drop-state-1|drop-state-4|tts-fail-next) DUO_FAULT_ARG=(--fault "$DUO_PART");;
esac
# One process group lets cleanup stop bash, uv and the bot, releasing inherited bench-lock fds.
setsid bash -c 'duopy hil/duo_identity.py launch --directory "$0" "${@}"' "$DUO_BOT" "${DUO_FAULT_ARG[@]}" &
DUO_LAUNCHER=$!
cleanup_bot() { kill -TERM -- "-$DUO_LAUNCHER" 2>/dev/null || true; wait "$DUO_LAUNCHER" || true; for i in $(seq 1 20); do pgrep -g "$DUO_LAUNCHER" >/dev/null || break; sleep 0.5; done; kill -KILL -- "-$DUO_LAUNCHER" 2>/dev/null || true; }
trap cleanup_bot EXIT
for DUO_WAIT in $(seq 1 120); do
  test ! -f "$DUO_BOT/ready.json" || break
  kill -0 "$DUO_LAUNCHER"
  sleep 1
done
test -f "$DUO_BOT/ready.json"
duopy hil/duo_identity.py check --directory "$DUO_BOT"
duopy hil/duo_smoke.py run --board "$DUO_BOARD" --part "$DUO_PART" \
  --bundle "$DUO_DEPLOY/bundle-$DUO_BOARD-duo" --out "$DUO_OUT" \
  --bot-dir "$DUO_BOT" --origin http://192.168.7.222:7874 \
  --server-url http://192.168.7.222:7861
COORDINATOR
```

The launcher uses `uv run --project server`, `load_credentials(approved_only=True)`,
port 7861, and the demo recorder/SCTP environment. Never source or inspect `.env`.
It records the bot's `identity()` manifest, including the three duo source
hashes, dependency source hashes, model, voices, endpoint, Pipecat version,
`DUO_TEST` and `DUO_FAULT`. The authenticated PID includes host boot and process
start time. The live session's own identity must match. Normal parts require
faults off; fault parts require exactly `DUO_TEST=1` and their named fault.
The wrapper adds passive wire and real Flux synthesis boundary witnesses.
A single bench-owning shell starts/stops the bot and runs the part, so all
children inherit the same lock. Do not launch a second bench owner alongside it.

The default device server/origin address is `192.168.7.222`; change both command
arguments together if the coordinator host address changes. `--origin` must
be `http://<local-interface>:7874` with no path or credentials. The runner
checks a local bind to that interface before starting its owned fixture server,
records its GET paths and source addresses, and requires observed fetches for
all 26 fresh duo hashes in the install part. `owned-origin.json` binds the
configured URL to that server and records its closure before offline reboot.
An arbitrary remote URL is rejected; closing an unrelated local server cannot
be called an origin outage. The LLM endpoint
is not written in this repository. Export `DUO_LLM_URL=http://<host>:<port>/v1`
(the OpenAI-compatible base URL; `LOCAL_LLM_URL` is read as a fallback) in the
coordinator's shell before `duo_identity.py launch`; the bot inherits it. With
neither set the launch stops before anything is recorded. The identity check
does not pin the endpoint: it requires an http(s) URL that came from one of
those two variables, and the authenticated bot record keeps the URL and the
variable's name (`llm_url`, `llm_url_source`). That record and the output of
`duo_identity.py check` are evidence and stay out of git. The model is pinned:
`phonellm-max-alpha-2-nvfp4`; temperature 0 and thinking disabled. Voices are
`flux-hannah-en` and `flux-rufus-en`. No request text is scored as an LLM gate.

## Pre-declared pass rules

Use only the verified private deployment bundles from
`$DUO_DEPLOY/bundle-{echoear,p4xc}-duo`, built in `build/duo-deploy-*`.
The coordinator fills every private field below before the first step-7 part.
Record hashes and gate results only; keep images and configs in their private
directories. Public results do not stand in for private-image checks.

| Board | Public-seed R10 | Public-seed headroom | Private deployment bundle / ELF SHA-256 | Private R10 result / comparison JSON | Private headroom result / evidence |
|---|---|---|---|---|---|
| S3 (`echoear`) | PASS, 0 unmasked bytes; `same-path/images/final/echoear-duo-r10.json` | Not a P4 gate | `$DUO_DEPLOY/bundle-echoear-duo`; **TODO: ELF SHA-256** | **TODO: PASS/FAIL, unmasked bytes, JSON path and SHA-256** | Not a P4 gate |
| P4 (`p4xc`) | PASS, 0 unmasked bytes; `same-path/images/final/p4xc-duo-r10.json` | PASS, 116,016 B retention; 114,928 B floor; 1,088 B above floor | `$DUO_DEPLOY/bundle-p4xc-duo`; **TODO: ELF SHA-256** | **TODO: PASS/FAIL, unmasked bytes, JSON path and SHA-256** | **TODO: PASS/FAIL, retention/floor/margin bytes, headroom JSON path and SHA-256** |

Public evidence paths in this table are relative to
`~/src/evidence/duo/step-6/step-6b/`; the public P4 headroom results for both
builds are in `same-path/images/final/p4xc-duo/{first,last}/headroom.json`.
For each private R10 entry, retain the first/last app and ELF hashes and require
zero unmasked differences with the default app-image masks. Bind the private
P4 headroom record to the deployed ELF hash. The hardware checks below remain
required; P4 AEC acoustic labels remain report-only and provisional.

Every candidate acquisition/reset first passes the frozen `boot_check` for
that attempt's exact ELF hash, board/chip and MIC3 gain, before session setup or
scoring. Each check is retained in `boot-NN.json` and the final result, including
the install part's offline reset and S3 pre-step boot.

1. **Install/offline reboot:** initial install fetched content; both packs have
   their complete hash inventory pinned, resident, verified and persisted.
   The owned origin is closed before reboot. `DUO_INSTALL result=ready` then
   reports restored content, zero fetches and the same pin count. `duo mem`
   agrees before/after (31 pins, used at most 368,640 bytes), and the reboot
   boot record matches the bundle ELF. Keep memory/inventory evidence.
2. **Connect:** current-session wire `hello`, matching `state`, then committed
   angel `DUO_PROOF`; match session, conn, seq, rev, pack identity, ordered
   layers/positions and hidden layers. This part requires saved angel state;
   if a previous run left devil saved, use a separately recorded idle toggle
   to return to angel before the connect part. Never relabel devil as angel.
3. **Injected switches:** one `duo tap` while idle, after observed
   `LLMFullResponseStartFrame` that is still unmatched at wire receipt (no
   response end, interruption, cancellation or error), before `TTSStartedFrame`/audio, and after
   `TTSStartedFrame` plus `BotStartedSpeakingFrame` while playback is active.
   A missed phase fails. Each witness requires injected +1, physical +0,
   toggle +1, answer +1, displayed +1; unchanged boot session/app generation/
   bot conn; exactly one verified switch and revision advance; matching sent
   state and new proof; then a completed turn from the new real voice service.
   Interrupt, retirement wait, mutation and publish timings are saved per
   phase in `result.json`. No LLM reply content gate is applied.
4. **Dropped state:** N=1 requires exactly one identical-seq retry; N=4 requires
   three identical-seq retries and one new-seq resync hello carrying stale
   `have`. The resync returns the committed target without re-adopting stale
   device state. Require exactly N recorded drops, one switch transaction and
   one display advance. N=4 binds the resync hello sequence exactly to state
   and proof. Before/after status character, revision and generation must agree
   with their proofs; verification transaction, mutation/publish transaction,
   operation, session, request sequence, connection and phase revisions must
   join to the authoritative outcome. N=4 expects exactly one firmware timeout counter
   increment; all other errors remain unchanged.
5. **Voice failure:** after the committed toggle, play France. Require a
   synthesis error and `state reason=reverted` at the next revision, with
   old-character proof on the same connection. The failed user reply is not
   re-spoken. Bracket the failed input through the actual playback start of the
   next explicitly stimulated turn, independently of revert publication time.
   Require the expected fault's error ID/context to match the first target
   synthesis, and reject any extra synthesis or PCM on either service within
   that bracket, including restored-service remainders. A new France turn must
   complete with the old voice and matching written-audio completion context.
6. **Quiet:** more than 300 seconds (driver uses 301) without a user/bot turn,
   session/generation/conn change, then a successful STT-strict France turn.
7. **Turns:** exactly one completed France turn per character. Use the frozen,
   hash-checked smoke stimuli and USB-identity-selected Jabra/mixer helpers.
   STT must contain the fixture's declared word alternatives. A completed
   speech interval, non-silent PCM written by the transport and matching
   synthesis voice/context/conn/rev are mandatory. Assistant text is retained
   **report-only**, including unexpected answers.
8. **Physical double tap:** follow the GO procedure below. Zero injection
   delta, exactly one physical double tap, toggle and answer, one new proof
   for the other character with unchanged session/generation/conn, and the
   matching bot request, verified commit and outcome are mandatory.

`DUO_PROOF` is the display oracle on both boards; the S3 face is too small
for a webcam to resolve. A scene admission is never accepted as proof.
Fix-2 firmware emits a `DUO_PROOF` header with `layer_count` and `hidden`,
followed by one `DUO_PROOF_LAYER` per visible layer. Each layer carries the
header's `rev` and `scene_revision`; the parser admits the proof only when
every unique layer index is present. `DUO_STATUS` carries counters and heap;
two `DUO_ERRORS` fragments carry the same `generation`/`rev` plus `part`,
`parts` and all fault counts. A new status header cannot complete an older
partial sample. Every firmware record is at most 420 bytes including framing;
native worst-case coverage measures 417 bytes. Truncated, mismatched, duplicate
or missing fragments fail closed. The physical-touch adapter consumes these
same reassembled snapshots; use the fix-2 firmware and HIL parser together.
The fix-2 deployment bundles are in
`~/src/evidence/duo/step-6/deployment-private-fix2/bundle-{echoear,p4xc}-duo`,
from fresh `build/duo-deploy-{echoear,p4xc}-fix2` builds. Offline preparation
selects these bundles; fill the private gate table using their fix-2 records.
These directories contain Wi-Fi credentials and must never be committed,
shared or uploaded, just like the original private deployment directories.
Fix-3 adds `render_busy` to `DUO_STATUS` for zero-wait display-lock or queue
backpressure. These attempts retain the exact unshown animation frame for
retry, including when a blink phase elapses while busy. Fix-4 escalates a
continuous Busy period longer than 500 ms to one `render` fault and a
`DUO_RENDER` reason of `busy-timeout`, while continuing retries. Successful
admission re-arms that timeout; more Busy attempts alone do not flood faults.
Real admission and render failures still increment `render`.
`DUO_RENDER` records identify `phase` (`swap` or `animation`), `reason`
(`lock-busy`, `queue-full`, `busy-timeout`, or `rejected`) and the display `Error` enum value.
The quiet gate still requires zero fault-counter changes. Every part's
`result.json` includes a report-only `render_busy` observation: the counter
`delta`, elapsed `seconds`, and `rate_per_minute = delta * 60 / seconds`,
bracketed by status queries immediately before and after the part, before
cleanup/restore. No rate threshold is a gate, including a high rate. Missing
counters, a reboot (including the install part's offline reboot), a generation
change, a counter reset, or an empty time bracket yield null delta/rate and an
explicit observation instead of claiming zero. A failed part retains this
report when both samples are available. Reporting failures do not change the
verdict; the existing strict error, proof, retry and restore rules still apply.
Current deployment bundles are under `deployment-private-fix4/bundle-{echoear,p4xc}-duo`, from
fresh `build/duo-deploy-{echoear,p4xc}-fix4` directories; they carry the same
credential protections as fix 2.
All parts fail on candidate panic diagnostics, cleanup failures or dirty
restore. Large evidence and audio stay under `~/src/evidence/duo/step-7/`.

## User-paced physical check

Choose `DUO_PART=touch` in the same block, or invoke `hil/duo_touch_verify.py`
with the same run arguments except `run --part touch`. The runner enables
touch, writes `ready-for-go.json`, and waits up to 900 s. It retains early
contacts but excludes them from the scored baseline. Do not create GO until
the readiness file exists and the user explicitly says they are ready.
In a second terminal (no serial connection or bench acquisition):

```bash
DUO_TOUCH="$DUO_ROOT/$DUO_BOARD/touch/attempt-$DUO_ATTEMPT"
test -f "$DUO_TOUCH/ready-for-go.json" && test ! -e "$DUO_TOUCH/GO" && touch "$DUO_TOUCH/GO"
```

Relay **WINDOW OPEN** to the user. Ask for a short single tap in the middle,
wait for **CONTACT PRE-CHECK** and at least **1 second**, then one double tap at the same spot: each contact 20–250 ms; second release
80–399 ms after the first. Stop touching after that double tap. The fixed
600-second window begins at the fresh post-GO baseline. It ends early only on
the complete witness. Single/contact detection is reported separately.
There is no reconnect/idle renewal: duo must keep its session. Injected input,
read errors, counter resets or changed generation invalidate the window.
The legacy touch-status UI fields are not duo UI state; the driver counters
and `DUO_STATUS`/`DUO_PROOF` together are authoritative. GO timeout and an
unwitnessed full window remain unverified, with no automatic retry. Cleanup
attempts touch-disable and demo-hold independently, then restores R7/S3.


Every attempt reports `tap_latencies` in `result.json` and the attempts summary.
Injected latency is the host console-injection timestamp to the bot's wire
receipt, in milliseconds. Physical-tap timing is bracketed by the status polls
and reported as millisecond bounds, with that limitation explicit. An attempt
without an observed tap/wire bracket reports measurement unavailable; no
latency is invented. Both successful and missed injected windows retain their
latency. These measurements are report-only.

## Firmware installation, touch and deployment notes

For a corrupt-file error, hold the demo first (`demo hold`) and keep it idle.
Read `disp inventory 0 0`, following each returned `next` cursor with the same
inventory revision, to identify the rejected hash. Explicitly remove only that
file with `disp delete {"hash":"<rejected-hash>"}` and wait for its
`disp result <id>` terminal success (`id` comes from `DISPLAY_POST`). If it is
still resident, use `disp ram-evict <rejected-hash>` after the delete completes.
Set the trusted fixture origin with `duo origin http://host:port`, then
`demo run`; the installer refetches the missing file and verifies it. Other
verified files remain intact. A delete rejected as `in-use` needs a reboot into
hold/idle before retry; never format a mounted store to repair one bad hash.

Step 7 install evidence on each board must include `duo mem` (`DUO_MEM`) and
all pages of `disp inventory 0 0` (flash inventory), both after the initial
install and after reboot with the asset origin unavailable. Retain the inventory
revision while paging; restart at zero if it changes. Repeat `duo mem` through
the AEC reconnect workload, outside measurement windows.

`duo tap` injects one double-tap through app-control and the same duo UI handler
used by the physical gesture. `DUO_STATUS.taps_injected` counts console/driver
injections delivered to that handler; `taps_physical` counts physical gestures.
Both sources share pending/retry/resync logic and the existing counters,
including ignored pending taps. For the real-tap witness, require a physical
counter increment with zero injected-counter delta. Use `duo status` to sample.

Touch initialization and polling run on the dedicated `duo-touch` owner (4 KiB
PSRAM stack, 20 ms polls). Failed initialization backs off 1, 2, 4, 8, 16, then
30 seconds between attempts. The owner posts source- and generation-tagged tap
events; app-control performs no touch I2C. Its shared admission rejects console
and physical taps alike while touch is disabled or the connection is not live.

A hello normally receives a state, including `reason:"adoption-failed"` when
adoption was not possible. An unexpected hello error schedules a new-sequence
hello after 1, 2, then 4 seconds, at most three retries in that error chain.
Pending taps remain ignored during this backoff. Exhaustion waits for a new
connection; successful hello state clears the fallback budget.

For step-7 memory evidence, `duo status` includes byte counts
`internal_heap_free` and `internal_heap_minimum` (minimum since boot). Capture
both while idle, connected, and after toggles, alongside `DUO_MEM` and inventory.

## Step 6a images and deployment settings

Step 6a builds use the public seed `duo-build-test`, an empty Wi-Fi password,
and `http://duo.invalid:7874`. These inputs deliberately exercise the transport
and AEC code. They are validation images; real Wi-Fi credentials are absent.
The exact seed, build commands, raw SHA-256s, app comparisons and image checks
are retained under `~/src/evidence/duo/step-6/`.

The nine legacy reference/candidate pairs use identical absolute source and
build paths under this worktree: `build/step6-source` and
`build/step6-images/<board>-<profile>`. Each side starts with an empty build
directory and the same explicit defaults chain, excluding `sdkconfig.local`.
The reference is `c2d0aa3`; the candidate commit is recorded in
`build-summary.json`. Only the default app descriptor/checksum masks are used.
Step-15 shipped bundles are independently checked against their pin record,
without modifying any shipped file.

Final root-worktree duo builds remain at `build/step6-final/echoear-duo` and
`build/step6-final/p4xc-duo`. Their first/last flash inputs, ELF, map and hashes
are in `images/final/<board>-duo/{first,last}`. R10 compares the first and last
app images with the default masks and verifies each embedded ELF hash. AEC
archive/placement checks and the P4 headroom gate run on both builds. Additional
isolated-source duo builds and comparisons are retained in `images/`.
Bundling is step 6b, after the coordinator merges the corrected duo adapters.
A new merged HEAD needs a build/version refresh before the bundler can attest
that HEAD; retained step-6a artifacts keep their original identity.

Wi-Fi settings cannot be re-seeded in these binaries at flash time. The current
firmware passes the compiled `CONFIG_DG_WIFI_SSID` and
`CONFIG_DG_WIFI_PASSWORD` directly to `dg_net_init()` and selects
`WIFI_STORAGE_RAM`. There is no existing console or NVS Wi-Fi provisioning
path. The coordinator must build fresh deployment images from private defaults,
then bundle those images. This follows the demo image build/defaults path;
`demo configure` can replace the server URL, but cannot replace Wi-Fi settings.
Do not label a private rebuild as byte-identical to the public-seed image.

`~/src/evidence/duo/step-6/deployment-private/` and `build/duo-deploy-*`
contain Wi-Fi credentials inside the app image (and private build configs).
Never commit, share, or upload these directories or their credential-bearing
artifacts. Build and bundle with `umask 077`, including in a fresh shell.

The following commands are coordinator-only. Prepare the existing private
`sdkconfig.local` through the established private configuration process, without
printing or committing it. In a fresh shell at the merged worktree root:

```bash
set -euo pipefail
export TMPDIR="$HOME/.cache/gap3-tmp"
umask 077
mkdir -p "$TMPDIR"
# Both destinations must be fresh so saved public-seed config cannot win.
test ! -e build/duo-deploy-echoear
test ! -e build/duo-deploy-p4xc
mkdir -p build/duo-deploy-echoear build/duo-deploy-p4xc
# Public flags seed IDF dependency discovery; private defaults supply Wi-Fi.
printf 'CONFIG_DG_BOARD_ECHOEAR=y\nCONFIG_DG_ECHOEAR_AEC=y\nCONFIG_DG_DEMO_AUTOSTART=y\nCONFIG_DG_DUO=y\n' > build/duo-deploy-echoear/sdkconfig
printf 'CONFIG_DG_BOARD_P4XC=y\nCONFIG_DG_ECHOEAR_AEC=y\nCONFIG_DG_DEMO_AUTOSTART=y\nCONFIG_DG_DUO=y\n' > build/duo-deploy-p4xc/sdkconfig
trap 'git restore -- dependencies.lock dependencies.p4xc.lock' EXIT
DG_BOARD=echoear DG_VARIANT=duo scripts/idf.sh \
  -B "$PWD/build/duo-deploy-echoear" \
  -DSDKCONFIG="$PWD/build/duo-deploy-echoear/sdkconfig" \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear;sdkconfig.defaults.demo;sdkconfig.defaults.echoear-aec;sdkconfig.defaults.duo' build
git restore -- dependencies.lock
DG_BOARD=p4xc DG_VARIANT=duo scripts/idf.sh \
  -B "$PWD/build/duo-deploy-p4xc" \
  -DSDKCONFIG="$PWD/build/duo-deploy-p4xc/sdkconfig" \
  -D 'SDKCONFIG_DEFAULTS=boards/common/sdkconfig.defaults;sdkconfig.local;boards/p4xc/sdkconfig.defaults;boards/p4xc/sdkconfig.defaults.aec;sdkconfig.defaults.demo;sdkconfig.defaults.duo' build
git restore -- dependencies.p4xc.lock
```

Repeat the image/headroom and first/last R10 gates on the private deployment
images; retain only hashes and approved config counts in public evidence. After
the step-6b adapter merge, create and verify fresh deployment bundles:

```bash
set -euo pipefail
umask 077
export DUO_DEPLOY="$HOME/src/evidence/duo/step-6/deployment-private"
mkdir -p "$DUO_DEPLOY"
for board in echoear p4xc; do
  OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts \
    uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python hil/duo_bundle.py make --board "$board" \
    --build "$PWD/build/duo-deploy-$board" \
    --bundle "$DUO_DEPLOY/bundle-$board-duo"
  OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server:scripts \
    uv run --project server --frozen --offline --group dev \
    --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
    --with esptool==5.5.0 python hil/duo_bundle.py verify --board "$board" \
    --bundle "$DUO_DEPLOY/bundle-$board-duo"
done
```

The adapter takes the build lock. Flash only the newly verified private
bundle through the coordinator's bench-owned `duo_smoke.py run --board <board>
--bundle <deployment-bundle> ...` path after its step-6b merge and preflight.
Never patch image bytes or substitute flash inputs into a public-seed snapshot.

While idle, `demo hold`, `demo configure {"url":"http://<bot-host>:7861","volume_db":-6,"enabled":true}`,
`duo origin http://<asset-host>:7874`, then `demo run` select the actual bot and
asset origin. `demo configure` persists server settings through the existing
idle-only writer. Keep the
P4 AEC labels report-only and provisional, as for its AEC-demo predecessor.

## Step 6b integration and validation bundles

The corrected bot and HIL adapters are integrated on `angel-devil`. Refreshed
public-seed builds reuse `build/step6-final/{echoear,p4xc}-duo`; their first/last
snapshots, image gates, R10 comparisons and bundle manifests are retained under
`~/src/evidence/duo/step-6/step-6b/same-path/`. The bundle directories are
`bundle-echoear-duo` and `bundle-p4xc-duo`. They retain the step-6a public network
seed and are for offline validation. Use the private deployment rebuild above
for the coordinator's step-7 campaign.

The absolute build paths match step 6a because the HTTP component embeds its
generated source path in an assertion string. A different build directory
changes image bytes outside the permitted descriptor masks. The initial
`step6b-final` builds and their path-difference evidence remain in the parent
`step-6b` evidence directory; the `same-path` builds provide the comparison and
bundle handoff.

Each bundle's manifest identifies the merged firmware commit and records source
and dependency hashes. The adapter verifies flash-input hashes, image integrity,
ELF binding, target chip, duo symbols, AEC, autostart and reference-gain metadata.
Its source/dependency provenance remains `recorded-only`; boot and bench checks
remain pending until the coordinator measures the corresponding deployment
bundle on hardware.
