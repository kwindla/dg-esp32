# Board smoke A coordinator run book

The writer implements and host-tests this harness. Only the coordinator runs
these hardware commands. Run from `~/src/dg-esp32-p4` on `p4-port`; never use the
build lock and bench lock together. Every run flashes, verifies boot identity,
checks the selected mode, and restores EchoEar to bench image `55152f25` before
releasing the inherited bench lock. The harness never starts, stops, probes, or
reconfigures the bot on 7861. It never accesses 7860. Use one shell for the run
book: `ESPTOOL_PYTHON` is exported for the entire campaign in the setup below.
If resuming in a new shell, re-export it and the campaign/bot paths before any
preflight, calibration, reference or fixed-bound command. LiveIO also defaults
it from `checked_esptool_python()` for the frozen restore helpers.

## Freeze the four candidate bundles from the current worktree

Build each variant and package it immediately under the same build lock. Do not
package the old step-7 baseline as a build of the new HEAD. `make_bundle.py`
checks the build's worktree, target, Git project version and input freshness,
reads only approved config boolean/gain counts with `grep -c`, copies the flash map,
ELF and all flash inputs, and records file hashes, commit and dirty source
fingerprints. Config files and project descriptions are not copied. Do not
change firmware sources between building and bundling.

The supplied keydiff JSON currently names generated reference configs in the
gitignored `build/` directory. Before `fullclean` or moving worktrees, the
coordinator should preserve all five files in a private evidence directory
(mode 0700, configs mode 0600), retaining their `sdkconfig-<variant>` names and
recorded SHA256s, and write a **new** JSON whose `reference` paths name those
copies. Set `SMOKE_CONFIG_REFS` to that JSON; keep generated configs out of git
and never print their contents.

If those references have already been removed, the coordinator re-creates them
from the same R3 inputs before smoke builds: run
`TMPDIR="$HOME/.cache/gap3-tmp" scripts/capture_s3_baselines.sh step-8-config-recreate`
(use a fresh label), then compare each variant's `canonical_config_sha256` in
that new baseline manifest against
`~/src/evidence/echoear/p4/baselines/step-8/manifest.json`; require all
five to match. Do not accept regenerated configs if that digest gate differs.
The canonical source files then are `sdkconfig` (normal), `build/sdkconfig-demo`
(demo), `build/accept/sdkconfig-aec-normal`, `build/config-accept` (AEC demo), and
`build-ws146/sdkconfig`. Under the build lock, the coordinator copies those to
private evidence as `sdkconfig-<variant>`, writes a new JSON mapping each variant
to its preserved `reference` path and SHA256, and exports `SMOKE_CONFIG_REFS` to
that JSON before executing the build block below. The canonical digest gate
proves equivalence to the original R3 configs even if generated comments differ.
The writer does not copy or read these private generated configs.

```bash
cd "$HOME/src/dg-esp32-p4"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export SMOKE_CONFIG_REFS="${SMOKE_CONFIG_REFS:-$HOME/src/evidence/echoear/p4/step-7/step6-config-references.json}"
export SMOKE_A="$HOME/src/evidence/echoear/p4/step-8/smoke-A"
mkdir -p "$TMPDIR" "$SMOKE_A"
scripts/build_locked.sh bash <<'BUILD'
set -euo pipefail
. "$HOME/esp/esp-idf-v6.1/export.sh" >/dev/null
export DG_BOARD=echoear
trap 'git checkout -- dependencies.lock' EXIT
for variant in normal demo aec-normal aec-demo; do
    build_dir="$PWD/build/smoke-A-$variant"
    mkdir -p "$build_dir"
    defaults='sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear'
    case "$variant" in
        demo|aec-demo) defaults+=';sdkconfig.defaults.demo' ;;
    esac
    case "$variant" in
        aec-*)
            autostart=n
            if [[ "$variant" == aec-demo ]]; then autostart=y; fi
            cat > "$build_dir/public-profile.defaults" <<PROFILE
CONFIG_DG_ECHOEAR_AEC=y
CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15
CONFIG_DG_DEMO_AUTOSTART=$autostart
CONFIG_DG_WIFI_OBSERVE=y
PROFILE
            defaults+=";$build_dir/public-profile.defaults"
            ;;
    esac
    idf.py -B "$build_dir" -D IDF_TARGET=esp32s3 -D "SDKCONFIG=$build_dir/sdkconfig" \
        -D "SDKCONFIG_DEFAULTS=$defaults" build > "$SMOKE_A/build-$variant.log" 2>&1
    # R3: compare names only against the preserved config references whose
    # hashes matched the step-8 baseline matrix; any config drift stops here.
    python3 - "$variant" "$build_dir/sdkconfig" "$SMOKE_A" <<'PYCFG'
import json, subprocess, sys
from pathlib import Path
import os
refs = json.loads(Path(os.environ['SMOKE_CONFIG_REFS']).read_text())
variant, config, output = sys.argv[1:]
with (Path(output)/('keydiff-'+variant+'.json')).open('w') as log:
    subprocess.run(['python3', 'scripts/sdkconfig_keydiff.py', refs[variant]['reference'], config],
                   stdout=log, check=True)
PYCFG
    # The component manager rewrites membership on AEC flips. Restore BEFORE
    # packaging; the manifest fingerprints this canonical lock state.
    git checkout -- dependencies.lock
    python3 hil/make_bundle.py --board echoear --build "$build_dir" \
        --out "$SMOKE_A/bundles/$variant"
    case "$variant" in
        aec-*) python scripts/check-aec-image.py --elf "$build_dir/dg-esp32.elf" \
            --archive "$PWD/managed_components/espressif__esp-sr/lib/esp32s3/libesp_audio_processor.a" \
            --ar xtensa-esp32s3-elf-ar \
            --expected-sha256 670639c3d136c7e3909aa90bcaac573600b7e98b6850be8fd5484b4cdea96dcc \
            > "$SMOKE_A/aec-placement-$variant.log" ;;
    esac
    git checkout -- dependencies.lock
done
BUILD
```

Use fresh build/output directories for a new campaign. Bundle mode is selected
from verified `autostart`, `aec_enabled` and profile metadata, never its path or
name. An explicit boot ELF hash check precedes provisioning and every proof
boot. Candidates also require the matching `DG_BOOT` board/chip record.

## Coordinator-owned bot and identity

One unchanged `~/src/dg-esp32/server/demo_bot.py` process serves both sides of
each pair. The ordinary bot's existing opt-in **session recorder** must be
enabled; `ECHOEAR_AEC_EVALUATION_DIR` is not used (that scored mode requires AEC
and can change the acquisition protocol). User-start/interruption counts come
from the bot recorder's `frame` events, not a substring search or device VAD.
`bot.log` must be uncoloured. Both AEC modes use the same bot policy/profile.

If the coordinator has not already started a suitable bot, the exact launch is
below. Do not run this over an occupied 7861, stop someone else's process, or
run `server/tests` during the campaign. A pre-existing suitable process can be
used by supplying its capture directory and identity instead.

```bash
export MAIN_REPO="$HOME/src/dg-esp32"
export BOT_CAPTURE="$SMOKE_A/bot"
mkdir -p "$BOT_CAPTURE"
ECHOEAR_AEC_SESSION_DIR="$BOT_CAPTURE" ECHOEAR_STT_PROFILE=demo \
    PIPECAT_SCTP_MAX_CHUNK_SIZE=1100 LOGURU_COLORIZE=NO PYTHONUNBUFFERED=1 \
    SERVER_PORT=7861 "$MAIN_REPO/server/.venv/bin/python" -u \
    "$MAIN_REPO/server/demo_bot.py" --host 0.0.0.0 --port 7861 \
    > "$BOT_CAPTURE/bot.log" 2>&1 &
echo "$!" > "$BOT_CAPTURE/bot.pid"

PYTHONPATH="$PWD/hil:$MAIN_REPO/server" "$MAIN_REPO/server/.venv/bin/python" - <<'PY'
import json, os, subprocess
from pathlib import Path
from board_smoke import process_token
from demo_policy import profile_manifest
repo = Path(os.environ['MAIN_REPO'])
output = Path(os.environ['BOT_CAPTURE'])
pid = int((output/'bot.pid').read_text())
identity = dict(bot_instance=process_token(pid),
    bot_source_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD:server/demo_bot.py'], cwd=repo, text=True).strip(),
    policy_source_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD:server/demo_policy.py'], cwd=repo, text=True).strip(),
    profile_manifest=profile_manifest('demo'))
(output/'identity.json').write_text(json.dumps(identity, indent=2)+'\n')
PY
```

Wait for `Uvicorn running on …:7861` in `bot.log` before the first smoke. The
harness checks PID/start-time/host-boot identity, the exact demo_bot command,
unchanged source blobs, and the session's actual `stt-profile` recorder event.
A process restart requires a new identity; never pair captures across it.
No credentials or process environment are read by the harness. The fixed server
URL is `http://192.168.7.222:7861`, playback volume is -6 dB, and Jabra routing /
mixer / 48 kHz playback use the existing qualified ALSA helpers.

## Smoke A and the measurement acquisitions

Use the **p4 worktree's** server environment via cwd and `uv --project server`;
`server/` is byte-identical to master by R1. The reviewed launch recipe keeps
Pipecat/HIL imports in that environment and explicitly pins the independent
esptool interpreter for the frozen restore path. Run this dry preflight **before
any `hil/bench.py` invocation**, with no bench/device access:

```bash
cd "$HOME/src/dg-esp32-p4"
export ESPTOOL_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
export PYTHONPATH=hil:server
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/board_smoke.py --preflight-tools
```

If dependencies are absent from the uv cache, run the same host-only preflight
once without `--offline` to warm it before bench time. It checks esptool 5.5.0
and serial in both interpreters, imports the serial/audio/provision/restore
modules and checks the frozen helper's interpreter. The live runner repeats
this before `hold()` or `touch()` so dependency failures cannot park restoration.
EchoEar MAC reads use the checked interpreter and `--after no-reset`.

The bounds fixture is initially `pending-coordinator-measurement`. That state
must not qualify a candidate. Use `--calibration` for the first acquisitions;
exit 0 means acquisition/behavior/exact evidence/resource checks succeeded,
while `result.json.passed` remains **false** until measured bounds are fixed.
`acquisition_passed` is separate. No fixture numbers from host tests are live
bounds.

PLAN step 8 requires “at least three S3-vs-S3 runs per variant”. Collect three
captures per variant from one unchanged bot process and compare all three pairs
(1–2, 1–3, 2–3). The following loop collects 12 runs, including the demo F20
sessions in each run. EchoEar is restored after every invocation.
Host idle, board/Jabra placement and room conditions are the coordinator's
bench responsibility; retain the standing bench/time exclusions.

```bash
cd "$HOME/src/dg-esp32-p4"
export PYTHONPATH=hil:server
for variant in normal demo aec-normal aec-demo; do
    for capture in 1 2 3; do
        python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
            hil/board_smoke.py --board echoear --bundle "$SMOKE_A/bundles/$variant" \
            --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" \
            --bot-repo "$MAIN_REPO" --calibration --pair-id "$variant-cohort" \
            --out "$SMOKE_A/calibration/$variant-$capture" || exit 1
    done
done
```

The loop exports `PYTHONPATH=hil:server`; keep it for the commands below.
The existing server environment is used for pytest/HIL dependencies; only `demo_policy` is imported by the checker,
which opens no service.

Each main session includes France/Paris, the count-to-twenty prompt, Italy
playback at **+3 s from the bot's user-transcript event**, then ten complete long
replies with no talk-over. A missed scheduling deadline or stimulus outside bot
speech fails. AEC-off must ignore Italy and complete counting; AEC must produce
an accepted user start/interruption during bot speech, hear Italy, and answer
Rome. Extra accepted user starts or interruptions during any of the ten quiet
replies fail. Only DOWNSTREAM frame rows count; UPSTREAM copies have distinct ids.
The frozen recording helper uses `-d 15` for France and `-d 30` for long replies
and talk-over. arecord finishes naturally; no signal interrupts a successful
recording. In qualification mode the bot's completion must arrive before `recording start + duration
- 2 s`, reserving the two-second tail inside the capture. Playback and readiness
time consume that budget, and timeouts name the duration. The helper checks the
natural process exit; nonzero exit still fails. Bot evidence through the **whole**
recording, including its remaining idle time, decides pass/fail.

In `--calibration` mode only, bot answer content is report-only. Missing Paris,
missing Rome, off-script count text and obvious off-script/negated city answers
are retained under `bot_content_findings` in each turn JSON and in `result.json`
(with session and turn identifiers). User transcript matching, AEC interruption
and ignore behavior, zero quiet-turn self-interruptions, session continuity,
wire evidence, uplink intervals and resource checks remain mandatory. Qualification
keeps its existing answer checks and timing deadline.

Coordinator decision (fix round 5b): in AEC-off calibration, the count reply's
words are report-only. Completion is gated structurally: bot speech and the
assistant turn must stop after the user turn, without interruption, and Italy
must be ignored. The count-to-twenty text check remains mandatory in qualification.

Calibration permits completion through the end of the same 15/30-second capture.
If bot speech continues past that boundary, the WAV still closes naturally at its
original duration. The harness then requires a clean speech/assistant-turn stop
within the existing 60-second safety cap before playing another prompt, and
records `reply_exceeded_recording_window` as a finding. A missing in-window reply
start, safety-cap timeout, session loss or recording failure still fails the run.
After a drain or a finish in the last two seconds of the recording, calibration
waits a full two-second settle before the next prompt so device playout and the
mic gate can recover. Session guards remain active during this wait, and the WAV
duration remains unchanged.

These durations follow the coordinator's fix-2 decision and the mute-03 reference
evidence (`~/src/evidence/echoear/aec-demo/step-9/mute-03/sessions/`): recorded
count replies were 7.9–11 s, user-stop to speech 0.13–0.33 s, with a 2 s tail and
scheduling margin. The sanitized mute-03 event fixture retains one real reply's
timestamps. France has the shorter 15 s window.
An interrupted assistant-stop is insufficient: the reply must follow the latest
user-stop and bot speech must have stopped. Bot frame/event records, per-turn verdicts, Jabra recordings and
raw serial/bot logs are retained.

Demo mode provisions through the existing path, persists assets/settings, ends
the fixture origin before the proof reset, then waits for autostart. After the
main session it verifies held state/no reconnect, resumes with `demo run`,
checks a live France turn, holds again, hard-resets and verifies cold restoration
(`RESTORED assets=16 no_http=1`), autostart and another France turn.
`no_http=1` is a firmware claim; the host witness is that the provisioning asset
server context has already exited before the reset. Each F20
session stays active at least 13 s so it has >=500 measured intervals after
warm-up. `--headless` skips display/asset setup/restoration; it selects the
headless wire profile and retains the shared turn/resource checks.

For every candidate session, `uplink-intervals` is sent **exactly once immediately
at the end of measured turns**. The end PERF `stats` dump follows this freeze,
then the resource gate and intentional stop/hold; PERF never extends the window. Even an acknowledgement timeout does not retry
that command. `UPLINK_INTERVALS_NONE` fails as unavailable evidence. Captures
contain one session/generation each and extend through `PHASE closed`, including
queued `msg=` lines printed after intervals.

Coordinator decision 2026-10-31 (`step-8-fix-3`): each SDP m-line port must be a
decimal integer **1–65535**, including placeholder **9**. Only that port token is
excluded from S3/P4 pair equality; media type, protocol, format list, line order,
spacing outside the port, and all other sanitized attributes stay exact. Raw
captures retain their original ports. With ICE the m-line describes the default
candidate's destination, so the port can differ per session; it is not a bot
policy input. See [RFC 8839 section 3](https://www.rfc-editor.org/rfc/rfc8839.html#section-3)
and [section 4.3.1](https://www.rfc-editor.org/rfc/rfc8839.html#section-4.3.1).
The first normal-1 run recorded audio port 9 and application port 50712. This
explicit decision supersedes the earlier placeholder-9 finding rule. Bench-display work in later steps needs the same
known display request id on both boards and an uncoloured bot log.

Keep heap and stack gates on all four variants. The harness requires the existing
memory gate (internal minimum >=81,920 B, largest block >=32,768 B, zero failed
allocations), peer-loop free stack >=4096 B and positive/recorded PSRAM capacity.
PERF files preserve free/largest PSRAM with the additional approximately 15 KB
per-session instrumentation cost; do not discard these while setting timing
bounds. **Verify on the first live calibration run** that the initial `stats`
dumps complete inside the firmware's two-second warm-up; this is currently an
assumption, not a measured fact. Compare the start PERF host times/ACKs against
`UPLINK_INTERVALS start_us` with `aec-clock` anchors (start_us is the end of
warm-up), or prove the stats ACK precedes first-frame + 2 s. When retained, the firmware's
`CONSOLE_WINDOW command=stats start_us=... end_us=...` records give a stronger
same-device-clock comparison: end_us is sampled after ACK and diagnostic flush;
compare it directly with `UPLINK_INTERVALS start_us`. Demo capture can
start before Ready, and both initial dumps must fit. If either overlaps the
measured window, stop calibration and correct the snapshot acquisition before
freezing bounds; do not absorb the console-induced gap into the stall envelope.
The final snapshot is after the freeze. Capture a current-firmware PERF during
smoke A, sanitize it, and promote it to a positive gate fixture afterwards: the
historical mute-03 PERF lacks output_volume_db and peer-loop witnesses.
PSRAM free/largest across all three demo
sessions remain available for leak review; a per-session leak threshold has not
been measured and is not invented here.

The main session's recording floor is `15 + 11*30 = 345 s` (5 min 45 s).
Demo adds two France captures (`2*15 = 30 s`), for 375 s of recording. Budget
about **8 minutes per manual run and 9–10 minutes per demo run**, including
flash/MAC/boot, setup, provision and restore. Twelve calibration runs, two demo
references and four fixed-bound reruns total **18 runs: 8 manual and 10 demo**.
Recording alone takes `8*345 + 10*375 = 6510 s` (**108.5 minutes**); the total
estimate is `8*8 + 10*(9–10)` = **154–164 minutes (2.6–2.7 hours)**. Reserve
roughly **3 hours**, plus offline builds/review and timeout/recovery costs.
The 03:00–04:10 exclusion still applies. This replaces fix-1's event-terminated
recording estimate; every recording now runs for its declared 15/30 s duration.

## Offline recheck of a captured run

Run outside the bench wrapper; no device, live bot process, serial, audio or
network access is performed. The existing run's result/identity and per-session
`<label>-serial.log` / `<label>-bot.log` supply the metadata/evidence:

```bash
cd "$HOME/src/dg-esp32-p4"
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server \
    uv run --project server --frozen --offline --group dev python hil/board_smoke.py \
    --recheck "$SMOKE_A/calibration/normal-1" \
    --out "$SMOKE_A/calibration/normal-1/result-recheck.json"
```

Choose a fresh `result-recheck*.json` filename in the same run directory for each
recheck. Exclusive file creation never overwrites `result.json` or an earlier
recheck. Exit 0 and `checks_passed=true` mean that the captured wire/timing checks
pass. The sidecar binds input hashes, retains the original error, and reports
intervals, ports, median/p99 and observed coverage. It does not replace the
original behavior/resource/restore verdict or qualify a board (`qualification`
remains false). Demo rechecks require main/resume/cold logs. Calibration checks
count, cadence and declared coverage without inventing fixed stall/p99 bounds;
a fixed-bound recheck requires `--bounds` pointing to measured bounds. Reference-
only images cannot supply candidate wire evidence. The original verdict and
calibration input list require coordinator review separately.

The normal-1 first-run recheck passed under this port rule. Its recorded stats
windows were 10,168,516–10,303,914 us and 10,373,921–10,505,795 us. The measured
window starts at 12,178,387 us (first successful submit + 2 s): both initial
dumps completed during warm-up, at least 1,672,592 us before measurement. This
uses same-clock CONSOLE_WINDOW evidence, including ACK/flush, and needs no host
clock interpolation. It establishes this **normal** run's placement; verify each
other variant, especially demo, independently. Acquisition is unchanged.

## Images of record: behavioral references only

These binaries predate instrumentation. They must not enter the wire-contract
pair or timing calibration. Their hashes are allow-listed by reference-only
mode, which runs behavior/F20/resource checks but sends no `uplink-intervals`:

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
    hil/board_smoke.py --board echoear --reference-only \
    --bundle "$HOME/src/evidence/echoear/aec-demo/step-9/merge-9d-r3/bundle-aec" \
    --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" \
    --bot-repo "$MAIN_REPO" --out "$SMOKE_A/reference-aec-69d5d764"

python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
    hil/board_smoke.py --board echoear --reference-only \
    --bundle "$HOME/src/evidence/echoear/aec-demo/step-10/requal-n1-bundles/demo" \
    --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" \
    --bot-repo "$MAIN_REPO" --out "$SMOKE_A/reference-demo-5bbd868a"
```

The exact reference ELF hashes are 69d5d7642d2c7650a236ada6ac5915a1ba5d3caaf88bcfc697efdab3156e4c07
and 5bbd868adceb8f4d8ff99cfe0e077d5b249a29d29a5aae766b8c61f664939057.
Both restore to 55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e.

## Fix and review the measured bounds

Apply declared margin rule v1 unchanged across variants: stall = observed maximum
of exact `max_us` or trailing gap over **all main/resume/cold sessions**, plus
max(10,000 us, ceil(10% of that maximum)); p99 pair bound = max(1,000 us,
maximum all-pairs p99 difference across matching session labels) + 1,000 us.
These floors cover quantization and allow ordinary scheduling variance without
per-sample tuning; CLI margins can only increase them. Record observed minimum
`ok / ((end_us-start_us)/20000)` coverage, session counts and all input hashes.
The fixture stays pending until real acquisitions are collected.

Positive margins do not imply a known false-fail probability. With three
exchangeable independent run blocks per variant, a new block exceeds the
unmargined observed stall maximum with probability at most 1/4; positive margins
reduce that by an unknown amount. Four independent variants give the conservative
unmargined estimate 1-(3/4)^4 = 68.36% for at least one exceedance. Pair deltas
share captures and are not independent samples. The report gives an explicit
scheduling model illustration; actual tail rates need held-out bench evidence.
Never describe three captures as a statistical qualification guarantee.
```bash
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/board_smoke.py \
    --calibrate-bounds "$SMOKE_A"/calibration/*/result.json \
    --coverage 0.95 --stall-margin-us 10000 --p99-margin-us 1000 \
    --out "$SMOKE_A/wire_contract_bounds.measured.json"
```

Review the JSON and evidence. If any chosen coverage is below 0.95, this is a
**coordinator decision**, with rationale recorded via
`--coverage-decision 'decision id and measured rationale'`; it cannot silently
weaken the default. The median requirement is fixed, including 22.0 ms; the
histogram resolves medians at 1 ms using the occupied bin's lower edge. P99
continues to use the conservative bin upper bound; stalls use exact maxima.

After coordinator review, copy the measured fixture into the named plan location, retain
its input result/capture hashes and decisions, and rerun the four variants with
fixed bounds (fresh output directories):

```bash
cp "$SMOKE_A/wire_contract_bounds.measured.json" hil/fixtures/wire_contract_bounds.json
for variant in normal demo aec-normal aec-demo; do
    python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python \
        hil/board_smoke.py --board echoear --bundle "$SMOKE_A/bundles/$variant" \
        --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" \
        --bot-repo "$MAIN_REPO" --bounds hil/fixtures/wire_contract_bounds.json \
        --out "$SMOKE_A/qualified-$variant" || exit 1
done
```

Each run preserves `<session>-wire.log`, `identity.json`, PERF/turn evidence and
`result.json`. To rerun an individual exact/measured S3 pair through the unchanged
checker CLI, for example normal captures 1 and 2:

```bash
read -r stall p99 coverage < <(python3 - <<'PY'
import json
r=json.load(open('hil/fixtures/wire_contract_bounds.json'))['variants']['normal']
print(r['stall_us'], r['p99_delta_us'], r['coverage'])
PY
)
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/wire_contract.py \
    "$SMOKE_A/calibration/normal-1/main-wire.log" \
    "$SMOKE_A/calibration/normal-2/main-wire.log" \
    --profile product --left-aec 0 --right-aec 0 \
    --left-target esp32s3 --right-target esp32s3 \
    --left-identity "$SMOKE_A/calibration/normal-1/identity.json" \
    --right-identity "$SMOKE_A/calibration/normal-2/identity.json" \
    --stall-us "$stall" --p99-delta-us "$p99" --coverage "$coverage"
```

The calibrator already performs this pair check for every pair. A final
qualification claim needs the coordinator's successful live records; host tests
and a pending fixture are never substitutes.

## P4 restore lifecycle (later steps)

`--board p4xc` uses the pinned P4 identity, reset-aware serial factory and its
app baud; it cannot select an EchoEar port. Until the step-14 product restore is
pinned, a successful run leaves the tested image; a failure/abort restores the
pinned factory pair using `spikes/p4_probe/host/restore_factory.py` and requires
`--p4-factory-backup` (default is the approved factory-20261031T060646Z directory).
The inherited bench lock remains held while restoration retries after SIGUSR1.
No C6 firmware is written. From step 14, pass both `--p4-restore-bundle` and
`--p4-restore-elf` for the coordinator-pinned normal image; it is flashed and
boot-hash verified after success or failure. Never fill that pin with an
unqualified image. The build lock must be released before any of these actions.

P4 product-image runs must launch through `python hil/p4_product.py
hil/board_smoke.py --board p4xc ...` (or `hil/board_smoke_bounds.py` in place
of `hil/board_smoke.py`). Keep the same checked server environment, bench
runner, arguments and R7 hold. See [p4_product.md](p4_product.md) for the
coordinator-approved storage erase before boot. S3 commands above are unchanged.
