# P4 spike A: coordinator run book

These are independent IDF 6.1 projects, not product firmware. `p4_probe` keeps the
coordinator's production-equivalent ES8311/ES7210 audio seed and adds UART tests.
`../p4_net` isolates hosted dependencies, credential Kconfig and network tasks;
its only shared source is this spike's UART implementation. This keeps network
initialization and Wi-Fi logging out of the audio/console rate measurements.

All commands touching hardware below are **for the coordinator**. Build before
entering `hil/bench.py`. The guarded runner owns no lock: the outer bench process
holds it, and the runner restores the pinned factory image on failure or abort.
Never put a guarded run inside the build wrapper. No command updates the C6.
The S3 board is not used by this run book.

## Environment and builds (host only)

```bash
cd /home/khkramer/src/dg-esp32-p4
export TMPDIR="$HOME/.cache/gap3-tmp"
mkdir -p "$TMPDIR"
. "$HOME/esp/esp-idf-v6.1/export.sh"
export ESPTOOL_PYTHON="$IDF_PYTHON_ENV_PATH/bin/python"
P4_ROOT="$PWD"
P4_BACKUP="$HOME/src/evidence/echoear/p4/factory-20261031T060646Z"
P4_OUT="$HOME/src/evidence/echoear/p4/step-1a/coordinator-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$P4_OUT"
# Once when adopting fix round 2: regenerate the ignored network config from defaults.
rm -f "$P4_ROOT/spikes/p4_net/build/sdkconfig"
scripts/build_locked.sh idf.py -C "$P4_ROOT/spikes/p4_probe" -B "$P4_ROOT/spikes/p4_probe/build" -DSDKCONFIG="$P4_ROOT/spikes/p4_probe/build/sdkconfig" -DIDF_TARGET=esp32p4 build
scripts/build_locked.sh idf.py -C "$P4_ROOT/spikes/p4_net" -B "$P4_ROOT/spikes/p4_net/build" -DSDKCONFIG="$P4_ROOT/spikes/p4_net/build/sdkconfig" -DIDF_TARGET=esp32p4 build
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s spikes/p4_probe/host -p 'test_*.py' -v
python spikes/p4_probe/host/restore_factory.py --backup "$P4_BACKUP" --dry-run
python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_probe/build" --dry-run
python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_net/build" --dry-run
```

Use absolute build paths: `idf.py -C ... -B build` resolves `build` from the caller,
which can accidentally select the main project's S3 cache. Only `-DSDKCONFIG=<build>/sdkconfig` builds are supported. Both CMake projects
refuse a root `sdkconfig` before IDF can read it, and the flash helper refuses
stale root configurations or a manifest pointing outside `<build>/sdkconfig`.
Do not recreate the root files deleted by the coordinator. Generated configs
stay under each spike's ignored build directory. The network defaults list includes
`../../sdkconfig.local` after the checked-in defaults and reuses `DG_WIFI_SSID`,
`DG_WIFI_PASSWORD`, and `DG_SERVER_URL`. The build consumes that file; do not open,
print, copy, or include it in evidence. Our code never logs the password. Hosted 2.12.4 may log the SSID (not the
password) in WARN-level `Event: SSID ...` lines, for example from a future
`esp_wifi_get_config` response or a DPP event; do not assume serial logs exclude
the SSID or add that API casually.
Both projects select P4 rev<3/min v1.00, 32 MB flash/QIO, hex PSRAM 200 MHz/XIP,
UART0 TX37/RX38, and 115200 baud at boot. The main project is unchanged.

## Restore proof, before the remaining experiments

The backup pair was already captured by the coordinator. Each file must be
33,554,432 bytes with SHA256
`37db1cc6fcc1da5956d76dfb4100cff5a06b1112bbda862b6c4b98c644bacdcd`.
`restore_factory.py` validates both against `sha256.txt` and this pinned identity
before any write. Do not redo the backup or write the C6.

```bash
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_probe/build"
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/console_rate.py --boot-only --out "$P4_OUT/restore-proof-probe"
python hil/bench.py python spikes/p4_probe/host/restore_factory.py --backup "$P4_BACKUP" > "$P4_OUT/restore-proof.log" 2>&1
```

The flash helper reads offsets/files/options from the generated
`flasher_args.json`, checks the target via `project_description.json`, and runs
esptool **from that build directory**. It uses only chip `esp32p4`, baud 921600 and
`/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00`. For the current
builds the generated offsets are bootloader `0x2000`, partitions `0x8000`, app
`0x10000`; the helper reads them rather than assuming them. It leaves the chip in
the bootloader; the following host test explicitly resets it.

Expect `READY boot_lines=8 uptime_us=...`, `P4_PROBE ready`, then the first single
`uptime` command's acknowledgement, with no retry. The restore log must show
`write-flash --flash-mode keep --flash-freq keep --flash-size keep 0x0 factory-a.bin`,
then `verify-flash 0x0 factory-a.bin`, then
`Factory boot verified: Project name:     esp_brookesia_demo`.
There is no `write-flash --verify` option in esptool 5.5.0. Restore uses 115200
baud for the banner witness, sets DTR/RTS low before opening, and pulses RTS
high for 150 ms then low for hard reset. Its deadline for the banner is 15 s.
It takes no lock and refuses alternate `--port` values, including tty aliases.
Restore writes and verifies each have separate 1800 s limits, and flash-project
writes also allow 1800 s. Each logs an elapsed time even on failure; record the
measured write/verify times in the restore proof. Full 32 MB erase/program or a
verify read-back can take minutes even when the compressed transfer is small.
All esptool subprocesses use `ESPTOOL_PYTHON`, else the IDF environment's Python,
else `~/.espressif/python_env/idf6.1_py3.12_env/bin/python`. That interpreter must
report **esptool 5.5.0** before a write. The guarded runner checks it before
starting the child and uses it for rollback; the restore banner witness also
runs there with IDF's pyserial. A uv/hil or uv/server launcher cannot change the
rollback's interpreter. Non-dry-run restore/flash require the inherited bench
FD, a contended fresh open of the actual bench-lock file, and no build lock.
The guarded runner preserves that FD in children and rolls back on SIGHUP,
SIGTERM or Ctrl-C; rollback is shielded from a second such signal. Before starting
any child it opens a rollback log at
`<backup-parent>/rollbacks/restore-<UTC>-<PID>.log` (override with
`--rollback-log "$P4_OUT/restore.log"`, before the `--` child separator). The
restore process and all nested esptool stdout/stderr go to that regular file,
not the controlling terminal, so terminal hang-up cannot break progress output.
An unusable log destination is refused before the hardware child starts.

Do not proceed if the probe banner, image verification or factory banner fails.
The proven restore command is the abort path for later runs. Success in this
proof restores factory; later successful probe runs leave the tested P4 image in
place per PLAN R7. A rollback failure is fatal and must be reported to the user.

## Console and audio campaign

Reflash the probe after the restore proof, then run the rate campaign:

```bash
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_probe/build"
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/console_rate.py --out "$P4_OUT/console" --seconds 60 --line-bytes 1800
```

`result.json` and `serial.log` contain each 2,000,000 / 1,500,000 / 921,600 /
115,200 baud run. Each switches rate, starts a 16 kHz tone/capture worker,
streams for 60 seconds, then captures and dumps all four channels for five
seconds. `baud` acknowledges the new rate at the **old** rate, waits for TX drain
and 200 ms, switches, then emits `BAUD READY` and `OK` at the new rate. The host
switches immediately after the old-rate acknowledgement. A failed candidate baud
is recorded and reset to 115200 before trying the remaining rates; inability to
recover aborts the guarded run. Every boot starts at
115200. A cold reset and first-command test run before the baud sweep; attach
without reset and close/reopen mid-flood compare uptime afterward. The sweep's
highest passing baud and its measured R are saved immediately after the baud
loop, and all attach/reopen/ring/slow-reader tests run at that selected baud,
recorded in each JSON row. Reset-on-open is a measurement: `passed=False` is
recorded and the remaining tests continue. If an open reset changes the rate to
115200, the host observes uptime there without forcing a reset, then restores
the selected baud. Missing trailers after an open reset are recorded as an
incomplete reopen measurement. An unusable port or failed recovery remains a
hardware error. No passing baud still fails the campaign. Malformed numbered
BOOT records count as missing records rather than aborting a boot check.

An ordinary-reader ring baseline precedes a second ring run with two-second
read pauses; both last six seconds and include I2S load. The producer is paced
at about **1.2 x baud/10 bytes per second** (8N1), and reports that target rate.
There is no UART flow control: host read pauses cannot directly back-pressure
the firmware. The 20% producer surplus can itself fill the 64 KB ring at high
baud even with a healthy reader, so ring drops alone do not prove a host-pause
stall. Compare the baseline with slow-reader sequence/CRC counts and
`unexplained_missing`; these observe end-to-end missing/corrupt lines, not the
physical location of loss. Driver write/format failures are separate diagnostics
and are not a rate-pass criterion. TX-drain timeouts report an error/result
instead of rebooting the probe. A missing/corrupt `RING END` or an observed
reboot in either ring run is also a measurement: JSON records
`summary_received=false`, observed sequence/CRC counts and `passed=false`, with
unknown rate/loss denominators left null. The host resynchronizes with
newline/uptime, tries 115200 if a reset changed the baud, returns to the selected
rate and continues. Only a link that cannot be recovered aborts the guarded run.

Wire records are LF-terminated:

- `F <seq> <crc32> <payload>` / `R <seq> <crc32> <payload>`; `line_bytes` includes
  the entire header, payload and LF. IEEE CRC32 is over payload bytes only.
- `FLOOD END` / `RING END` report attempted lines/bytes, total elapsed microseconds
  including final drain, production duration, driver write/format failures, dropped ring
  lines, sent lines, and RX FIFO/buffer/frame/parity error events. Counts include
  the tail, so a final missing line is visible. The 64 KB ring is bounded;
  producer drops count as intentionally missing sequence numbers, separately
  from corruption and unexplained loss. Driver write failures are not a measure
  of electrical UART loss; CRC/sequence checks observe that boundary.
- `DUMP BEGIN frames=N channels=4 rate=16000`, followed by
  `DUMP <seq> <crc32> <base64>`, then `DUMP END frames=N`. CRC covers the exact
  base64 text. Decoded PCM is MIC1..MIC4, interleaved signed little-endian int16,
  up to 160 frames per line. The host checks CRC, sequence and exact PCM bytes.
- Eight numbered `BOOT` records precede `READY`. Cold-reset loss counts refer to
  these known driver records, not unnumbered ROM output.

Commands retained: `tone <Hz> <ms> <dBFS>`, `rec <ms> <analysis Hz>`,
`duplex <Hz> <ms> <dBFS>`, `dump`, `gain <mic 1..4> <dB>`, `vol <dB>`.
New commands: `baud <rate>`, `flood <seconds> <line_bytes>`,
`ring <seconds> <line_bytes>`, `loop <seconds>`, and `uptime`.
`loop` owns both I2S channels until its `LOOP END`; conflicting audio capture or
play commands return `ERR loop busy`. Flood/ring continue concurrently with it.

The seed's console had no installed UART driver: IDF's default VFS reads the
hardware FIFO by polling, returns no-data through stdio, and has no RX RAM queue
while codec initialization or other work runs. It also advertised readiness
before entering that polling loop. This explains the firmware-side input-loss
window; attributing the coordinator's exact first-line loss to FIFO timing
versus stdio error state still needs the new first-command hardware witness.
The new driver is installed before codec initialization, with 8 KB RX / 16 KB TX
buffers; the loop uses `uart_read_bytes` directly, and `READY` follows RX setup.
The seed's dump corruption remains an open measurement: classify it from CRC and
sequence counts at each rate, ring drops, and driver events rather than guessing.

Choose the highest passing baud and measured R (bytes/s). Target R >= 130 KB/s;
if only 921600 baud passes, record that result and scale future dump pacing and
deadlines as `bytes / (0.8 * R)`, plus the plan's fixed deadline margin.

Optional repeat of the coordinator's earlier acoustic smoke (requires its
placement/quiet-room arrangements; the writer does not run this):

```bash
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:spikes/p4_probe/host uv run --project hil --frozen --offline python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/audio_smoke.py "$P4_OUT/audio"
```

The hil project declares pyserial, NumPy, SciPy and the other smoke dependencies;
the server venv does not provide pyserial. The exported `ESPTOOL_PYTHON` above
keeps esptool/rollback in IDF even though this command uses hil's Python. The seed
accepts the dump CRC column and attempts its first command once.

## Network campaign

The final candidate uses IDF 6.1's in-tree Wi-Fi remote API and **only** managed
`espressif/esp_hosted` **2.12.4** (registry source commit
`4adc668f71987a0f46ee7d040c91f8783688373f`). `../p4_net/dependencies.lock` pins
its component hash. A build with managed `esp_wifi_remote` 1.6.5 also passed, but
that version's CMake registers an empty component on this IDF; removing it and
its unused EPPP dependencies still builds the complete probe. An in-tree-only
minimal image builds with a weak `esp_wifi_remote_init`; IDF's source returns
`ESP_ERR_NOT_SUPPORTED` there. The final image instead links the strong hosted
implementation. Thus in-tree API alone supplies no SDIO/RPC transport; the
managed hosted transport is required, the extra managed remote shim is not.
These are build/source facts, not proof of compatibility with the factory C6.
See the [IDF station example](https://github.com/espressif/esp-idf/blob/v6.1/examples/wifi/getting_started/station/main/idf_component.yml)
and [hosted 2.12.4 source](https://github.com/espressif/esp-hosted-mcu/tree/4adc668f71987a0f46ee7d040c91f8783688373f)
for upstream context.

### Pre-app sleep-clock boot-loop fix (round 2)

The coordinator's old network image aborted before `app_main`, at
`0x4805b676` / `sleep_clock_icg_startup_init` with `ESP_ERR_NO_MEM` (0x101).
This is IDF's eager sleep-clock ICG retention allocation, not a failed Wi-Fi join.
`PM_SLEEP_CLK_ICG_ENABLE` defaults on independently of `PM_ENABLE` and tickless
idle; it was on in both resolved probe configs, although PM/tickless/peripheral
power-down were off. The Wi-Fi-remote C6 defaults additionally enable
`WIFI_RMT_SLP_IRAM_OPT`, selecting `PM_SLEEP_FUNC_IN_IRAM` in the network build.
Hosted's constructor initializes transport/RPC before the sleep startup hook.

IDF's `pmu_sleep_clock_icg.c` calls `sleep_retention_entries_create`, whose P4
REGDMA links allocate with `MALLOC_CAP_DEFAULT | MALLOC_CAP_RETENTION`. The
rev<3 retention region is only part of internal RAM; PSRAM and the deferred
startup-stack heap cannot satisfy that request at this startup point. Thus low
aggregate DIRAM use does not establish enough free retention-capable memory.
The log proves that allocation failed after hosted initialization; the exact
free/fragmentation state and which earlier allocation consumed it were not
measured, so this is not evidence of a chip erratum or C6 incompatibility.

This throughput-only spike never enters light sleep. Its defaults now set
`CONFIG_PM_SLEEP_CLK_ICG_ENABLE=n`, and a compile-time guard refuses an old
config that still enables it. The rebuilt ELF omits both the ICG startup entry
and its allocating function; the already-working audio probe is unchanged.
Regenerate **only** `spikes/p4_net/build/sdkconfig` using the removal/build
commands above when adopting this fix. Do not edit/read the generated config or
change `sdkconfig.local`. Chip revision, PSRAM/XIP, hosted version, SDIO pins and
reset policy are unchanged. The coordinator's subsequent rerun reached network
READY and joined Wi-Fi; the version RPC remained unavailable, as recorded below.

SDIO slot 1 uses CLK18 CMD19 D0..D3=14..17, four bits / 40 MHz. Hosted alone owns
C6 reset GPIO54; application code never drives it. The firmware logs hosted
slave version via RPC, then IP, AP BSSID/RSSI/channel/auth/cipher/PHY flags, plus
return codes for `statis_dump`, `set_ps_none` and `set_tx_done_cb` (or
`not linkable` for the private callback). The private callback is weak-probed;
it is absent in this linked candidate. Unsupported diagnostic calls are
reported, not replaced by fabricated success values.

The board-path host address is **192.168.7.222 on wlp8s0** (not a server
URL). UDP uses **47010**, TCP **47011**; neither uses 7860 or 7861.

```bash
P4_HOST_IP=192.168.7.222  # board subnet on wlp8s0
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_net/build"
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_net/host/run_net.py --host "$P4_HOST_IP" --out "$P4_OUT/network"
```

By default, an unavailable slave-version RPC aborts before UDP/TCP. The
coordinator's rerun at
`~/src/evidence/echoear/p4/step-1a/coordinator-net-20261031T085629Z/`
confirmed active hosted transport, Wi-Fi join, AP info (RSSI -47, 11ax), and
`set_ps_none` success. It reported `HOSTED FW rpc_rc=-1 version=0.0.0`,
`statis_dump` NOT_SUPPORTED and a non-linkable TX callback. UDP/TCP were not
run. The coordinator considers a factory C6 predating the firmware-version RPC
likely; the unavailable version alone is not evidence of incompatibility.

For that case, explicitly opt in to continue both transfer tests:

```bash
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_net/host/run_net.py --host "$P4_HOST_IP" --out "$P4_OUT/network-unknown-version" --allow-unknown-slave-version
```

The flag still requires a parseable firmware-version record plus active
transport and Wi-Fi IP records from this boot. `result.json` records
`slave_version: "unavailable (rpc_rc=-1)"`, `slave_version_override: true`,
and `connection_evidence` containing transport, join, AP and API records;
the complete `boot` transcript is retained. It never reports the failed RPC's
zero-initialized `0.0.0` as the measured slave version. This metadata survives
successful UDP/TCP completion and later test failures. Missing/malformed
version records and transfer failures still abort. No C6 write is authorized.

Expected: `HOSTED FW rpc_rc=0 version=...`, `WIFI IP=...`, `P4_NET READY`,
or the explicitly allowed unknown-version result above, followed by
`UDP END ...` (separate uplink `send_errors` and `echo_send_errors`), and `TCP END bytes=... errno=0`. For a transport/join failure, before concluding that the factory C6 is incompatible, check **GPIO54 reset
polarity** against the board's C6 EN wiring: hosted's `SDIO_RESET_ACTIVE_HIGH`
and reset-on-every-host-boot defaults are not yet electrically proved for this
board. A C6 held in reset can look like an RPC/version timeout. Hosted retains
ownership of GPIO54; application code must not add a second reset driver.
If transport/join compatibility remains unresolved, stop and ask the user; do not update C6. The guarded runner restores P4
factory on RPC/join/transfer failure or interruption.

`run_net.py` invokes the unchanged `hil/netprobe.py` `run_streams` with
`esp-0,192.168.7.222,47010` bound to `wlp8s0`, 60 seconds, 50 pps, 130 bytes. Its ready callback starts
independent NPU1 uplink; its finish callback stops the device while the UDP
sockets stay open for tail collection. NPD1 downlink echoes as NPE1 with device
RX/TX timestamps. Device receive rows and send-error rows use the tool's
`<qIHH>` format; CRC and count-checked serial dumps are passed to its unchanged
`decode_dump` / `analyze_streams`. `result.json` reports RTT, both directions'
loss, jitter and local send failures. It does not infer loss from a received
maximum. The 16000-row bound marks any overflow as incomplete capture.

TCP uses the included stdlib host command
`python spikes/p4_net/host/tcp_sink.py --bind 192.168.7.222 --out "$P4_OUT/tcp.json"` (port 47011);
`run_net.py` starts and cleans it up automatically and issues
`tcp <host IPv4> 47011 60`. Raw bytes are consumed by the sink; both sides report
bytes, duration and throughput. Other console commands are
`udp <host IPv4> <port> <seconds> [pps bytes]`, `stop`, `netstats`, `netdump`,
`status`, and `uptime`. `udp`/`tcp` have a 120 s firmware duration ceiling.

## Recording Bring-up facts A

The coordinator fills [docs/p4xc.md](../../docs/p4xc.md) from the logs/JSON:
factory identity/restore banner from `restore-proof.log`; first command and boot
loss from `cold_reset`; R and baud from `baud_runs`; selected-baud attach/reopen/ring-baseline/slow-reader
behavior from those JSON records; four-channel CRC counts from each `dump`;
slave version/AP/API status from the network boot log; 60 s RTT/loss/jitter from
`udp_host`; TCP throughput from `tcp.json` and `tcp_device`. Compare the network
numbers with the coordinator's last S3 record using the same host and load.
Record whether the driver-backed first-command witness removes the observed
boot loss, and any remaining source-versus-hardware root-cause uncertainty. No
table row is a hardware pass until the coordinator runs and records it.

## Coordinator observations received for round 2

Evidence: `~/src/evidence/echoear/p4/step-1a/coordinator-20261031T081809Z/`.
These describe the prior candidate; the writer did not perform these runs.

- Factory restore proof passed: write 196.522 s, verify 70.831 s, factory banner
  witnessed. The later network-failure rollback also passed.
- 2 Mbaud passed at 199997.8 bytes/s: 6676/6676 flood lines and 500/500 dump
  lines, zero CRC/loss and zero I2S errors. The three lower rates had 1–2 bad
  dump lines; 115200 also had one bad flood line. Use the clean 2 Mbaud result
  as R; `producer_bytes_per_second` is a ring target, while achieved ring
  production is `summary.bytes * 1e6 / summary.produced_us`.
- Cold boot lost zero numbered BOOT lines; the first command succeeded once.
  This confirms the driver-backed first-command fix for this campaign.
- **reset_on_open=true**: attach and mid-flood close/reopen rebooted the board
  despite low DTR/RTS. Boot baud is 115200; the selected throughput-test baud is 2 Mbaud.
  The network probe still boots and accepts commands at 115200.
  Step 2 must handle that measured reset policy rather than promise passive attach.
- Ring baseline had 87 device ring drops and two unexplained/corrupt lines;
  the slow reader had 89 device drops and 440 unexplained missing/corrupt lines.
  Those effects are separate from the clean flood result. Missing ring trailers
  in later runs are now retained as incomplete measurements without a restore.
- The network result was the pre-app sleep-clock failure described above.
  No Wi-Fi/C6 compatibility or throughput result exists yet. The new image's
  hardware rerun and the Bring-up facts A table remain with the coordinator.

## Step 1b: audio, AFE, peer, panel and touch

These extensions are disconnected spike measurements. They keep the step-1a
commands and wire records intact. Hardware measurements remain with the
coordinator; the writer's build/host checks do not qualify an AFE soak, touch
range or visually stable display. Fill the empty Bring-up facts B table in
[docs/p4xc.md](../../docs/p4xc.md) from the new serial/JSON records.

### Fix round 2 build freeze

Fix round 2 changes sources and host tests only. The existing reviewed
`p4_probe/build` and `p4_panel/build` are frozen for the coordinator's 04:10
campaign; neither was accessed or rebuilt in this round. The new AEC/Opus
telemetry requires a later rebuild after the coordinator releases the freeze.
Use the previously reviewed runners with the existing binaries for the frozen
campaign. The commands below are the run book for that later rebuild/run;
they were **not executed** during fix round 2. Prior archive/image evidence
identifies the earlier build, not these updated sources.

### Build and archive identity (host only)

```bash
cd /home/khkramer/src/dg-esp32-p4
export TMPDIR="$HOME/.cache/gap3-tmp"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR"
. "$HOME/esp/esp-idf-v6.1/export.sh"
export ESPTOOL_PYTHON="$IDF_PYTHON_ENV_PATH/bin/python"
P4_ROOT="$PWD"
P4_BACKUP="$HOME/src/evidence/echoear/p4/factory-20261031T060646Z"
P4_OUT="$HOME/src/evidence/echoear/p4/step-1b/coordinator-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$P4_OUT"
# Once on adoption: consume the new public defaults without reading the config.
rm -f "$P4_ROOT/spikes/p4_probe/build/sdkconfig"
scripts/build_locked.sh idf.py -C "$P4_ROOT/spikes/p4_probe" -B "$P4_ROOT/spikes/p4_probe/build" -DSDKCONFIG="$P4_ROOT/spikes/p4_probe/build/sdkconfig" -DIDF_TARGET=esp32p4 build
scripts/build_locked.sh idf.py -C "$P4_ROOT/spikes/p4_panel" -B "$P4_ROOT/spikes/p4_panel/build" -DSDKCONFIG="$P4_ROOT/spikes/p4_panel/build/sdkconfig" -DIDF_TARGET=esp32p4 build
scripts/build_locked.sh idf.py -C "$P4_ROOT/spikes/p4_net" -B "$P4_ROOT/spikes/p4_net/build" -DSDKCONFIG="$P4_ROOT/spikes/p4_net/build/sdkconfig" -DIDF_TARGET=esp32p4 build
python -m unittest discover -s spikes/p4_probe/host -p 'test_*.py' -v
python spikes/p4_probe/host/build_evidence.py --build "$P4_ROOT/spikes/p4_probe/build" --out "$P4_OUT/audio-build.json"
python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_probe/build" --dry-run
python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_panel/build" --dry-run
```

The audio project pins esp-sr 2.4.7, dl_fft 0.8.0, cjson 1.7.19~2 and esp_peer
1.5.7, matching the main lock. The actual linked AFE engine is
`lib/esp32p4_less_v3_idf6/libesp_audio_processor.a`, SHA256
`16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100`.
`build_evidence.py` checks archive-to-symbol bindings in the link map, including
the AFE front-end wrapper and peer default implementation, and hashes archives,
map, ELF and application image. Its metadata check reads
`project_description.json`, never a generated config. `run_audio_b.py` embeds
that identity before opening the board. The enlarged audio app uses a private
partition CSV; the flash helper still reads all three offsets from the manifest.
The network project is unchanged. No product/shared source or main lock changes.

**Opus version exception (user-confirmed 2026-10-31, R10):** P4 rev 1.3 pins
`espressif/esp_audio_codec` **2.5.0**, with `P4_PROBE_OPUS=y`; S3 retains the
qualified **2.6.2** pin. This follows the vendor's instruction to use <2.6 on
P4 rev<3, without bypassing any guard or changing chip revision. The main lock
and all production source files remain unchanged. The benchmark compiles and
uses the actual `components/pipecat_esp_peer/src/opus.cpp` and `codec_memory.c`
in the isolated spike, preserving its explicit PSRAM allocation hooks.

The used Opus encoder/decoder/frame/type headers and registration headers are
byte-identical between 2.5.0 and 2.6.2. A cross-compiled RV32 ABI check verifies
all used fields/offsets/sizes, seven function signatures and enum values; both
versions' weak module-allocation hook objects are byte-identical. There are
**no API/ABI differences in the wrapper's used surface**. Package version,
codec binary/hash and the vendor's rev<3 support/guard differ; wire-level
bitstream or performance equivalence is not inferred from API compatibility.
Evidence: `~/src/evidence/echoear/p4/step-1b/fix-1/api-abi-peer-check.json`.
The linked P4 2.5.0 codec archive SHA256 is
`1fda41dc09d46f1ed8bbe2d687fb6ccad3a5e2ac6726f5d15804f4f0c8a6f0c2`.

`opus 300` is now enabled and reports codec version, 640 PCM bytes per frame,
production settings (16 kHz mono PCM16, 20 ms/320 samples, 24 kbit/s,
complexity 0, FEC/DTX off, default VOIP/CBR), and per-frame encode/decode/PLC cycles.
It runs on a dedicated core-0, priority-10 task with 32768 internal stack bytes
(above production's 26624/28672-byte 20 ms defaults, and equal to its generic
32768-byte default). `opus_end.stack_high_water_bytes` records remaining stack
in IDF bytes after codec destruction. Every frame also calls production
`decode(nullptr,0,...,true)` and requires 320 PLC samples, exercising its
non-null-placeholder workaround. These PLC operations add a synthetic missing
frame after every decoded frame; this is an API/cycle probe, not packet-loss
quality scoring. `opus_begin.input` is `uploaded_speech` when a WAV was uploaded,
otherwise `synthetic_sine`. An Opus-only `--speech-wav` run uploads it too.
The unchanged decoder validates consumed bytes, 640 decoded bytes, rate and
channel count; the benchmark checks 320 samples. Host analysis rejects wrong
codec/frame metadata. Hardware cycles/round-trip results remain for the
coordinator; a successful build is not a runtime pass.

The peer 1.5.7 archive links with the rev<3 image and has ELF32 little-endian
RISC-V single-float ABI throughout. Its attributes advertise `xespv2p2`, while
the rev<3 compiler selects `xespv2p1`. A complete text disassembly contains
29,450 instructions using 69 base I/M/F/C mnemonics, with no ESP/PIE, unknown or
rev-3-only instruction, and its CMake has no silicon-revision gate. Thus the
requested quick static check found no rev>=3 instruction dependency; this is
not a hardware peer-session qualification. Architecture, symbols and raw
checks are recorded alongside the API/ABI evidence above.

### Coordinator audio commands

Flash only after the build lock has exited. All actual runners below inherit
the existing bench lock/guarded factory rollback; the new host runners also
refuse a missing bench context. No new board/port selection is introduced.

```bash
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_probe/build"
# Electrical/default campaign; no Jabra playback is requested.
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/run_audio_b.py --build "$P4_ROOT/spikes/p4_probe/build" --out "$P4_OUT/audio" --aec-seconds 600
# Optional external burst/gain discrimination, only when the board/Jabra are positioned.
PYTHONPATH=hil uv run --project hil --frozen --offline python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/run_audio_b.py --build "$P4_ROOT/spikes/p4_probe/build" --out "$P4_OUT/acoustic-slots" --stage slots --acoustic
# Optional speech through the DAC instead of the default tone; WAV must already
# be mono PCM16 at 16 kHz, between 20 ms and 30 s, peak <= -12 dBFS.
# It is repeated for the run; --ref-gain defaults to 15, or use a sweep choice.
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/run_audio_b.py --build "$P4_ROOT/spikes/p4_probe/build" --out "$P4_OUT/aec-speech" --stage aec --aec-seconds 600 --speech-wav "$P4_SPEECH_WAV"
```

The runner resets at 115200, witnesses READY/first command, then switches to
the measured clean 2 Mbaud. New records are `B <JSON> <crc32>`; CRC is over the
exact JSON bytes. Host parsing rejects corrupt/missing/duplicate/out-of-order
measurement rows and firmware ERR records. Original step-1a record formats are
unchanged. `result.json` survives failures with completed stages and an error;
the guard restores factory on failure/interruption. Successful runs leave the
spike image per R7. A short `--aec-seconds 10` is a smoke only;
`aec.ten_minute_soak` remains false until the full duration is measured.
Use `--stage slots|reference|aec|opus|touch` for targeted repeats.

Command/protocol details and evidence mapping:

- `slots <mode> <ms>`: modes 0=silence/DAC muted, 1=DAC-only chirp,
  2=external stimulus/DAC muted; duration 100–5000 ms. PA GPIO53 is held low
  during the capture to isolate the electrical reference from speaker pickup,
  then restored. The chirp sweeps 300–3000 Hz. Each slot reports sample count,
  AC RMS dBFS, peak, clipping and actual gain-register readback; `slots_end`
  reports RX queue-overflow delta. Wire mapping remains MIC1=word0 high,
  MIC2=word1 high, MIC3=word0 low, MIC4=word1 low. Compare chirp against silence;
  observe whether only MIC3 rises and report MIC4's actual floor/peak.
  The default campaign captures the muted-DAC noise floor at 30 dB, then
  separately drops MIC1 and MIC2 to 0 dB, captures each floor and restores 30 dB.
  Every `GAIN` witness must match the command and report `rc=0`; a missing,
  duplicate or failed write stops the campaign. Analysis reports all slot
  deltas and the largest drop of at least 6 dB; insufficient/ambiguous floor
  is recorded as no match, never a pass. Optional `--acoustic` adds a steady
  1 kHz Jabra tone (not noise) and a second per-mic discrimination result.
- `refgain <db>` writes MIC3 reg 0x45 using the production
  `reference_gain_register` formula (0–33 dB by 3) and verifies readback.
  `level <volume_db> <ref_gain_db>` measures a 1 kHz, -12 dBFS DAC tone with
  PA off. The host sweeps all 48 combinations of DAC volume -20/-12/-6/0 dB
  and reference gain 0..33 dB, reporting every channel's level/clips; it
  restores DAC -6 dB and reference 0 dB. These are probe settings, not P4
  production calibration values.
- `aec <seconds> <stimulus>`: 1–600 s; stimulus 1=1 kHz tone, 2=uploaded PCM
  repeated through the DAC. The exact call is
  `afe_aec_create("MR",4,AFE_TYPE_FD,AFE_MODE_LOW_COST)`; no NS/AGC/models.
  It validates nested factory state, one mic/one ref, 16 kHz, MR indices and
  FD_LOW_COST mode, and rejects an unsupported chunk or output byte count.
  Live stereo32 MIC1/MIC3 feed the AFE on core 1, priority 12, with a 16384-byte
  PSRAM stack like production `pipeline.cpp`; DAC feeding is on core 0.
  AEC recreates the duplex channels at 4 descriptors x 240 frames (960 frames,
  60 ms at 16 kHz), then restores the step-1a 6 x 320 geometry on completion.
  These are `DG_SPEAKER_DMA_*` defaults actually used by duplex
  `echoear_i2s.cpp`, not the standalone `DG_MIC_DMA_*` 3 x 160 defaults.
  `--ref-gain` defaults to production's 15 dB; choose another 0..33 dB/3 value
  from the sweep explicitly. A bare firmware `aec` also starts at 15 unless
  overridden by `refgain`. It verifies reg 0x45 and includes gain, readback,
  DMA geometry and stack placement in `aec_begin`; host analysis checks them.
  Each chunk has a sequence, CPU cycle delta, wall microseconds, core, output
  byte count and raw input/reference/output powers. Cycle deltas include
  interrupts/preemption; they are not an exclusive task CPU profiler.
  Heap snapshots before/after AFE creation, after buffers, at end and after
  destroy distinguish INTERNAL|8BIT from SPIRAM|8BIT. `before_create` is inside
  the allocated worker: its AFE factory delta already excludes stack/TCB and
  must not have 16 KB subtracted. Separate `before_worker`/`after_worker_create`
  snapshots isolate task allocation (PSRAM stack, internal TCB); the worker
  waits for this snapshot before creating the AFE. `after_worker_delete`
  follows suspended-worker deletion, before restoring DMA. DMA allocation is
  outside both deltas. Heap minima are process-wide historical low-water marks,
  not resettable per-stage measurements.
  RX DMA queue overflows are counted by `on_recv_q_ovf`; stale idle frames are
  drained before the run. Runtime overflow, format or sample-count errors fail
  closed. Heap allocation failures abort the image via the required IDF option.
  Output includes clipping, counts, core/CPU frequency, per-chunk p50/p99/max
  cycles/time, heap-floor comparison, and a crude far-end MIC1/output power
  ratio after excluding the first two adaptation seconds. This is not
  delay-aligned speech ERLE; record tone and speech runs separately.
- `pcm_begin <samples>`, `pcm_data <seq> <crc32> <base64>`, `pcm_end` are a
  bounded, sequence/CRC-checked optional speech upload (maximum 30 s).
  Host code validates WAV format and rejects peaks above -12 dBFS **before
  sending any upload command** (PCM16 full-scale 32768; max integer peak 8230).
  Firmware independently enforces the same bound at `pcm_end`. Both report
  peak/RMS dBFS (unscaled RMS including DC; silence uses a finite log floor),
  and `playback` records path and rejection policy. No implicit resampling or
  scaling occurs. Speech is never injected into live MIC1 capture.
- `peer`: references the actual esp_peer 1.5.7 default operations table and
  verifies its open/close pointers. No session or network connection is made.
- `touch <seconds> <codec_traffic>`: a background 50 Hz GT911 poll, probing
  0x5D then 0x14 on SDA7/SCL8 without driving touch INT/RST. If both fail,
  `touch_error` records unavailability without aborting; host reports
  `available=false`, not a touch pass. GPIO27 is shared LCD/touch reset: panel
  setup can relatch the GT911 address from INT. The product must serialize
  shared reset with touch reads and re-probe after reset; the post-panel address
  remains unverified until hardware evidence. It reads product
  identity and configured dimensions, prints `TOUCH x y`, and records every
  poll's read duration, result and start time plus error/event counts.
  Host JSON reports actual touched range, out-of-range coordinates, read
  p50/p99/max and actual inter-poll timing. No touches means range unmeasured.
  The campaign compares an idle poll with `loop 6` plus a separate 50 Hz codec
  register-reader task to exercise real shared-bus contention; codec operations
  and errors are counted. The host waits for exactly one `LOOP END`, checks
  reads > 0 and errors = 0, and records it even if touch is unavailable.
  Ask the user to sweep corners/edges during this
  portion if touch coordinates are to be qualified. Audio loop commands stay
  available while the background touch task runs.

Codec initialization failures emit `CODEC_INIT_FAILED`/`codec_unavailable`,
leave PA off and keep UART/uptime/baud/stream diagnostics usable. Audio commands
return an unavailable error instead of dereferencing absent codecs. This does
not relax the AFE/Opus runtime format and allocation fail-stop checks.

### Coordinator panel commands

The independent `spikes/p4_panel` project prevents a panel experiment from
changing the audio probe's live I2S setup. It pins JD9365 2.0.2 (BSP-compatible
^2) and uses IDF DSI directly; no BSP/LVGL dependency. Copied constants,
source commit/hashes and license are in
[the panel BSP notice](../p4_panel/BSP-NOTICE.md).

```bash
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/flash_project.py --build "$P4_ROOT/spikes/p4_panel/build"
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/run_panel.py --out "$P4_OUT/panel" --seconds 10
# Refine near the lowest passing requested clock; example grid, not a result.
python hil/bench.py python spikes/p4_probe/host/run_guarded.py --backup "$P4_BACKUP" -- python spikes/p4_probe/host/run_panel.py --out "$P4_OUT/panel-fine" --clocks 40 39 38 37 36 --seconds 10
```

`panel <clock_mhz> <seconds>` supports 30–80 MHz / 2–30 s. Each requested clock
recreates/reset-initializes JD9365 using the BSP's exact 3.4-inch sequence:
800x800, horizontal total 880 / vertical total 840, two 1500 Mbps DSI lanes,
LDO ch3/2500 mV, reset GPIO27, inverted LEDC GPIO26 backlight at 50%.
Both RGB565 framebuffers must be in PSRAM. A one-pixel white border, colour
bars and moving white bar are drawn. Two completion fences keep a raced IRQ
from releasing a framebuffer too early; animation is at most half the refresh
rate, while callback measurements cover the continuous display refresh.
The firmware captures bounded timestamp arrays in ISR context, then emits
CRC/sequence-checked `panel_vsync` and `panel_frame` records after the window.
It reports overflow rather than truncating silently. Host analysis handles
32-bit timestamp wrap, reports both callback-period distributions, measured
refresh Hz and an effective pixel clock derived from the fixed timing totals.
Requested clocks can quantize in the hardware divider; do not label requested
MHz as an independently measured clock.

The host selects the lowest **tested requested** clock with measured refresh
at least 50 Hz and no observed driver underrun logs. This is a timing candidate only. With a positioned user/webcam
witness, repeat and pass `--visual-stable-clocks <actually witnessed clocks>`
to annotate stability; otherwise the stable-clock field stays null/unmeasured.
No writer camera action or visual pass is implied. Check the saved serial log
for driver underruns and the border/bars/moving bar for visual integrity.
Callback timing alone does not prove a stable image. Reflash the audio project
through the guarded path before returning to audio/console measurements.

Panel reconfiguration dims the backlight, sends display-off (DCS 0x28),
sleep-in (0x10), waits 120 ms, then deletes the old panel before the next reset.
The selection JSON includes both requested MHz and the measured effective
pixel clock for the selected timing/visual rows. PLL divider quantization can
map several requests to one actual clock; record that achieved value in facts B.

### Panel watchdog follow-up (fix round 4, source only)

The completed hardware timing windows and the interrupted lower-clock test
are recorded in [Bring-up facts B](../../docs/p4xc.md#bring-up-facts-b).
The failed PC resolves to IDF's unbounded DSI read-busy poll during the
JD9365 ID read, before the next timing window starts. A framebuffer delay
would not repair that call path.

The panel target now wraps `mipi_dsi_hal_host_gen_read_dcs_command` locally.
[dsi_read.cpp](../p4_panel/main/dsi_read.cpp) retains the IDF transaction
sequence for this spike's byte-wide command bus, gives all read phases one
500 ms deadline and blocks for a tick while polling. A timeout emits a
`panel_io_error` record and fails closed, letting the existing guarded
rollback handle failure. The task watchdog remains enabled. This bounds an
unresponsive read; the underlying DSI response failure is not yet diagnosed.

Only native host tests of the shared wait helper were run for this change.
The existing panel ELF still identifies the measured, unpatched binary.
Do not infer a successful link or a panel rerun from the source fix; the
coordinator decides when to rebuild and repeat the interrupted sweep.
