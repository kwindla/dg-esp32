# Board tooling and host baselines

Step 2 adds board-aware host modules while keeping the frozen EchoEar
`hil/board_port.py`, `hil/esp_serial.py` and existing campaigns unchanged.
Step-1b spike sources and docs are a separate uncommitted change set.

| Board | Hardware policy | Serial policy |
|---|---|---|
| `echoear` | Existing pinned USB MAC/by-id identity; delegates to `board_port` | Returns the unchanged `EspSerial`; defaults and explicit `reset_on_open=False` stay unchanged |
| `p4xc` | CH343 USB serial `5B90169930`, eFuse MAC `e8:f6:0a:e0:b8:76`, chip `esp32p4`, pinned by-id only | Product 2,000,000 baud; ROM 115200; every open is a reset, with no passive attach |
| `waveshare_146` | Build-only; hardware access refused before port lookup | No serial factory |

P4 facts come from [Bring-up facts A](p4xc.md#bring-up-facts-a). APIs preserve
old positional port semantics:

```python
from boards import resolve_port, verify_flash_mac
from board_serial import open_board
resolve_port()                        # delegates to frozen EchoEar resolver
resolve_port(requested, board='p4xc')  # USB identity only, no serial open
verify_flash_mac(board='p4xc')         # runner-owned bench context required
open_board('echoear', reset_on_open=False)  # same legacy object/behavior
open_board('p4xc')                     # waits for DG_BOOT board=p4xc
```

EchoEar's existing `DG_BOARD` environment guard is preserved by delegation.
P4 read-mac uses `ESPTOOL_PYTHON`, else the exported IDF Python, else the known
IDF 6.1 Python path, and requires esptool 5.5.0. Both USB serial and eFuse MAC
are verified; a tty number is not an identity. P4 MAC/serial operations require
the inherited runner bench FD and refuse a held build-lock flag. The new tools
take no outer bench lock. The existing serial guard continues to own/reuse its
inner serial lock.

## P4 boot baud decision

IDF 6.1 `components/bootloader_support/src/bootloader_console.c:49–105` configures
UART pins and `CONFIG_ESP_CONSOLE_UART_BAUDRATE`, including the second-stage
bootloader. `components/esp_stdio/Kconfig:150–153` makes the baud configurable
with `ESP_CONSOLE_UART_CUSTOM`. The planned product therefore prints its
bootloader and app at 2 Mbaud; only the earlier ROM bytes remain at 115200.
This requires `CONFIG_PM_ENABLE=n`: IDF 6.1 `esp_stdio/Kconfig` caps
`ESP_CONSOLE_UART_BAUDRATE` at 1,000,000 when power management is enabled.
The factory opens directly at app baud, tolerates undecodable ROM text and
waits for `DG_BOOT board=p4xc` before returning.

Step 1a's highest-passing-baud evidence measured a runtime switch from a
115200 boot to 2 Mbaud. It did not test a bootloader or cold-reset readiness
at 2 Mbaud. The main-project step-3 overlay applies 2 Mbaud from the
second-stage bootloader onward. Step 12a must check cold-reset acquisition,
first-command acknowledgement and tolerance of the ROM's 115200 bytes at the
product baud; the coordinator owns that hardware check. Recheck both PM-off
and sleep-clock-ICG-off settings after enabling hosted Wi-Fi.

The current spike uses 115200 throughout. Supply `baud=115200` and a
`boot_pattern` override for that image. Only with an overridden boot pattern
and no explicit baud, a failed app-baud acquisition falls back once to 115200 with an explicit RTS reset and
waits again. That fallback may take one boot timeout; it is a second reset,
not passive attach. A missing boot pattern closes serial/logs and fails.
`reset_on_open=False` is refused for P4. The P4 subclass reuses the frozen
reader/cursor/command methods but initializes its own port state, so no global
EchoEar resolver or serial behavior is patched.
P4 error exit closes without running the frozen S3-specific failure snapshot.
P4 command-ack timeouts also propagate directly without issuing the inherited
S3 `perf`, `wifi-stats` or `netprobe stats` snapshot commands. Product-pattern
acquisition must observe the table's app baud; it never falls back to 115200,
and an explicit wrong baud with the product pattern is refused before port I/O.
The sequenced command methods target the product console from step 6; the
current plain-console spikes support this live check's boot witness only.

Coordinator-only live check (no flash):

```bash
. "$HOME/esp/esp-idf-v6.1/export.sh"
export ESPTOOL_PYTHON="$IDF_PYTHON_ENV_PATH/bin/python"
P4_LIVE_OUT="$HOME/src/evidence/echoear/p4/step-2/live"
mkdir -p "$P4_LIVE_OUT"
python hil/bench.py python hil/board_live_check.py --board p4xc --baud 115200 --boot-pattern 'P4_NET READY|READY' --log "$P4_LIVE_OUT/serial.log" > "$P4_LIVE_OUT/result.json"
```

The checker refuses a missing/inactive/wrong inherited bench FD before any
port access. Successful output contains the pinned identities, observed baud,
reset policy and boot witness. The product boot pattern lands in step 6;
until then an override is required against a spike. No hardware live check was
performed by the writer.

## idf.sh routing

EchoEar's config/defaults and hardware resolver stay unchanged. `DG_BOARD=p4xc`
hardware actions use `hil/boards.py --board p4xc` under the same single
`hil/bench.py` runner, with eFuse verification. An explicit absolute `-B` and
project/config arguments can still select an already-built external P4 project;
the target metadata must say esp32p4. Port-only P4 commands do not inject
defaults/config/lock overrides into that cache. Build first through the build
wrapper, then use hardware commands only after that build finishes. For
example, with the chosen spike already built:

```bash
DG_BOARD=p4xc scripts/idf.sh -C "$PWD/spikes/p4_net" -B "$PWD/spikes/p4_net/build" -DSDKCONFIG="$PWD/spikes/p4_net/build/sdkconfig" monitor
```

For P4 and Waveshare, non-port actions (including build, reconfigure and
menuconfig) require the main project: an external `-C`/`--project-dir`, or an
external working directory without `-C`, is refused before any defaults or
lock injection. Build other projects directly through `scripts/build_locked.sh`.
An explicit `-C` naming the main project remains supported.

The classifier consumes option values, so `-B build-flash`, `-D SDKCONFIG=...`
and attached options cannot accidentally request hardware. `efuse-common-table`
and `efuse-custom-table` are offline actions under the build lock.
IDF 6.1 `-f`/`-t` are flags, so `diag -f flash` still routes through the bench;
`coredump-info`/`coredump-debug` also route through the bench because they may
read flash when no core file is supplied. This intentionally over-detects
hardware access even for an explicit offline core file. Serial
hardware with the build-lock flag remains refused. Waveshare hardware is
always refused with a build-only error. Stub-based routing tests assert both
`python` and `idf.py` resolve to their private test stubs.

Waveshare `DG_DEMO_AUTOSTART=1` and `DG_AEC=1` refusals are deliberate plan
constraints (R3W). P4 verification requires `DG_BENCH_LOCK_FD` even if
`DG_SERIAL_LOCK_HELD=1` is set; the serial-lock flag alone cannot authorize a
P4 action. EchoEar's legacy serial-lock convention remains unchanged.

## Main-project config chains (step 3)

```bash
DG_BOARD=p4xc scripts/idf.sh reconfigure
DG_BOARD=waveshare_146 scripts/idf.sh build
```

The P4 chain is `boards/common/sdkconfig.defaults`, optional private
`sdkconfig.local`, then `boards/p4xc/sdkconfig.defaults`. The common file is
the exact board-neutral subset of the unchanged legacy root defaults; a
drift test classifies every excluded S3 hardware key explicitly. P4 adds only
IDF-core settings, including rev<3, 32 MB/QIO, hex PSRAM 200 MHz, XIP,
UART0/2 Mbaud with PM disabled, 360 MHz and the ICG fix. Main-project P4 is
config-only at this milestone: its first build stops at codec 2.6.x's rev>=3
check. The target-specific codec policy is implemented later; no guard is
bypassed and this main configuration is not a flashable P4 product.

The common chain still carries S3-derived mic/playback tuning, IRAM and cache
policy. These are configuration carry-overs, not P4 measurements; steps 9 and
14 must validate tuning and placement against the P4 hardware. The Wi-Fi-coupled
`CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP` remains explicitly deferred until hosted
Wi-Fi integration rather than entering the IDF-core PSRAM allowlist.

P4 uses `build-p4xc/sdkconfig` and `dependencies.p4xc.lock`. `DG_AEC=1`
selects `boards/p4xc/sdkconfig.defaults.aec` and adds `-aec` to
the build directory. Step 4 stages only the legacy `CONFIG_DG_ECHOEAR_AEC=y`
enable; gain/guard calibration and the first P4 AEC product build belong to
step 15. P4 off/on are reconfigure-only checks at this milestone. The existing
S3 coefficient-placement audit still hashes the S3 archive and must not be
treated as a P4 AEC image audit. `DG_DEMO_AUTOSTART=1` appends `sdkconfig.defaults.demo` and adds
`-demo`, giving `build-p4xc-aec-demo` when both are selected. These environment
flags leave the documented S3 AEC builder path unchanged.

Waveshare retains its legacy defaults/overlay, with `build-ws146/sdkconfig`
and a separate `dependencies.waveshare146.lock`; its builds never rewrite
the EchoEar lock. Both board paths pass the target and lock explicitly.
IDF 6.1 needs `DEPENDENCIES_LOCK` bound as an IDF build property in addition
to the cache argument. Direct CMake configuration derives the same defaults
from `DG_BOARD` when no explicit defaults list is given. EchoEar keeps its
original root-config/normal and build-config/demo paths and defaults chain.
That direct-CMake fallback uses configure-time variables: supply `DG_BOARD` on
every configure, or provide explicit target/config/defaults/lock cache arguments.
The supported `idf.sh` entry point supplies those arguments on every invocation.

Step 4 makes the board choice target-specific: S3 defaults to EchoEar and
retains Waveshare; P4 defaults to P4XC. The existing `DG_DEMO_AUTOSTART` and
`DG_ECHOEAR_AEC*` symbols are authoritative for EchoEar/P4XC and remain
unavailable on Waveshare. The header `dg_audio/aec_config.h` exposes constexpr
enabled/gain/guard/rearm values from those legacy inputs; inactive values are
zero. Runtime code has not migrated to this header yet. No P4 calibration or
codec-version policy is inferred from the widened configuration.

The esp-sr/dl_fft/cjson manifest guard is
`(target == esp32p4 || $CONFIG{DG_BOARD_ECHOEAR} == True) && $CONFIG{DG_ECHOEAR_AEC} == True`.
The target test skips the absent EchoEar choice on P4; the false EchoEar choice
skips the absent AEC key on Waveshare. IDF's component manager treats an omitted
Kconfig key as an error, so the coordinator approved this short-circuit guard
in PLAN R5. `scripts/check_aec_manifest_guard.py` tests the actual three rules with
IDF's expression parser as part of the host suite.

## Private config comparison

```bash
python3 scripts/sdkconfig_keydiff.py build/sdkconfig-a build/sdkconfig-b
python3 scripts/sdkconfig_keydiff.py build/sdkconfig-a --expect-keys IDF_TARGET,DG_BOARD_ECHOEAR
```

Only generated files named `sdkconfig*` are accepted, including the resolved
symlink target; directories, private local sources and arbitrary files are
refused. Output is JSON key names/statuses only (`equal`, `differ`, `added`,
`removed`); values and input lines never appear on stdout/stderr. Disabled and
explicit `n` compare equal. Deprecated duplicate aliases use the final
assignment, matching Kconfig. `--expect-keys` checks presence with any value
(including disabled/empty); with two paths it checks the right file. Exit
status is 0 for equality/all expected keys, 1 for differences/missing keys,
2 for invalid input.

## Five-image baseline capture

```bash
TMPDIR="$HOME/.cache/gap3-tmp" scripts/capture_s3_baselines.sh step-2
# Only to continue a failed/interrupted matrix; successful images are hash-checked.
TMPDIR="$HOME/.cache/gap3-tmp" scripts/capture_s3_baselines.sh step-2 --resume
```

The entry point exports IDF and holds the global build lock across the whole
matrix. It builds normal, demo, AEC normal, AEC demo, then Waveshare last.
Every new variant removes its generated config before building, regenerating
from the defaults chain rather than retaining cached values. This policy is
applied in the step-3 fix capture and is required for config proof from step 4
onward. Normal/demo use unchanged idf.sh config paths. AEC normal uses the documented
`build/accept` recipe with a late `autostart=n` overlay; the frozen candidate
builder has no normal-mode option. AEC demo invokes that frozen builder
unchanged under hil's offline environment; a private idf.py launcher keeps its
child on IDF's Python. Both AEC ELFs pass the audited coefficient-placement
check. No frozen builder is edited.

After each build, even on failure, the shared dependency lock is restored with
`git checkout -- dependencies.lock`; its before/after/restored SHA256 is
recorded. The script refuses an initially modified lock. Failed attempts are
retained separately; `--resume` checks commit/lock identity and hashes of any
already-complete images. Interrupt cleanup stops the build process group before
restoring/releasing the lock. The output manifest points to immutable attempt
folders through per-variant links under
`~/src/evidence/echoear/p4/baselines/<label>/`.
The manifest records `config_policy=fresh-defaults-v1`; resume rejects captures
without that policy, requiring a new label rather than mixing cached and fresh
config proofs.
Variant links are replaced atomically, so a crash after link publication but
before the manifest write can be resumed; the incomplete variant is rebuilt
into a new attempt and the stale link is replaced. Completed variants remain
hash-checked and skipped. Restoration failures are journaled separately and
added as notes to the original build exception; they do not replace that
exception. A restoration failure after a successful build still fails capture.

Waveshare-last (and S3 AEC-off) dependency resolution prunes AEC components
such as `espressif__esp-sr`, `esp-dsp`, `dl_fft` and `cjson` from
`managed_components/`. Restoring `dependencies.lock` does not restore those
directories. The next S3 AEC build must re-fetch them online before any archive
audit that needs them.

Each variant stores app/bootloader/partition bins and ELF, raw config SHA256,
and a canonical config SHA256 over the final key/value mapping (sorted compact
JSON, normalized disabled `n`, last assignment wins). Only the aggregate hash
is emitted: the canonical mapping stays in memory. It ignores generated
comments, ordering and disabled-value spelling while detecting value changes.
The bundle also stores
profile enabled-key counts and names-only key-diff manifests. No sdkconfig file
is copied into evidence. The frozen builder's `build/config-accept` is renamed
inside the ignored build directory only for the strict key-diff tool, then
renamed back under the build lock. S3 normal/demo are compared with step-0
`after` images using the existing masked-image tool; this does not mask any new
field. Baselines and all runtime evidence stay outside git.

The committed `dependencies.p4xc.lock` pins the AEC-on superset graph (13
entries), like the S3 root lock. P4 AEC-off configuration prunes it to nine
entries; restore the committed lock after an off-mode build/reconfigure unless
the step deliberately changes dependencies. Host tests require the IDF 6.1
Python environment via `IDF_PYTHON_ENV_PATH` or the pinned local fallback.
