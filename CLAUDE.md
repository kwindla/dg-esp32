# dg-esp32

Shared ESP-IDF 6.1 Pipecat client for EchoEar (ESP32-S3) and P4-XC (ESP32-P4
rev 1.3, 3.4-inch 800×800). Retired Waveshare-1.46 is build-only. See
[board interfaces/gates](docs/boards.md), [P4](docs/p4xc.md), and
[display contracts](docs/display.md). The target is smoke parity, not qualification.

Evidence belongs under `~/src/evidence`, not git. Never print private configs,
environment files or generated sdkconfig headers. Use approved key-diff tools.

## Toolchain

- ESP-IDF **v6.1** lives at `~/esp/esp-idf-v6.1` (tools in `~/.espressif`).
- An older v5.4.2 checkout remains at `~/esp/esp-idf` and is not used by this project.
- Nothing is sourced automatically. Activate per shell with `get_idf` (alias in `~/.bashrc`),
  or `. ~/esp/esp-idf-v6.1/export.sh`.

## Common commands

```sh
# Only after bench-free permission. The wrapper sources IDF and takes the build lock.
export TMPDIR="$HOME/.cache/gap3-tmp"
DG_BOARD=echoear DG_DEMO_AUTOSTART=0 scripts/idf.sh build
DG_BOARD=echoear DG_DEMO_AUTOSTART=1 scripts/idf.sh -B build-s3-demo build
# S3 AEC uses the explicit legacy overlay (DG_AEC alone does not select it):
DG_BOARD=echoear DG_DEMO_AUTOSTART=1 scripts/idf.sh -B build-s3-aec-demo \
  -DSDKCONFIG="$PWD/build-s3-aec-demo/sdkconfig" \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local;sdkconfig.defaults.echoear;sdkconfig.defaults.demo;sdkconfig.defaults.echoear-aec' build
DG_BOARD=p4xc DG_AEC=0 DG_DEMO_AUTOSTART=0 scripts/idf.sh build
DG_BOARD=p4xc DG_AEC=0 DG_DEMO_AUTOSTART=1 scripts/idf.sh build
DG_BOARD=p4xc DG_AEC=1 DG_DEMO_AUTOSTART=0 scripts/idf.sh build
DG_BOARD=p4xc DG_AEC=1 DG_DEMO_AUTOSTART=1 scripts/idf.sh build
DG_BOARD=waveshare_146 scripts/idf.sh build
```

Never identify a board by tty enumeration names. The pinned ports are:

- EchoEar: `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00`,
  eFuse MAC `b4:3a:45:16:2d:94`.
- P4: `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00`,
  eFuse MAC `e8:f6:0a:e0:b8:76`.

Coordinator-only developer flash/monitor, after building and arranging the R7
restore hold; these raw commands do not replace a product runner's lifecycle:

```bash
DG_BOARD=echoear python3 hil/bench.py scripts/idf.sh -B "$PWD/build" \
  -p /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00 flash monitor
DG_BOARD=p4xc python3 hil/bench.py scripts/idf.sh -B "$PWD/build-p4xc" \
  -p /dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00 flash monitor
```

P4 hardware commands require an absolute already-built directory. No C6 writes.
Product runners require the selected restore-of-record and retain the signal-safe
recovery hold on failed restoration; no outer flock. Factory bring-up remains
documented in [spikes/p4_probe](spikes/p4_probe/README.md). Campaign instructions
and current pins live in [the parity run book](hil/p4_parity.md).

All builds go through `scripts/idf.sh` or `scripts/build_locked.sh` to serialize shared
managed components; `menuconfig` holds the build lock until it exits. Run legacy direct
builders as `scripts/build_locked.sh python hil/build_echoear_aec_candidate.py …` after
activating IDF. Build separately before flash/monitor, which use only the bench lock.

## Layout

- `main/` — application component (`main.c`, `CMakeLists.txt`, `idf_component.yml`).
- `sdkconfig.defaults` — checked-in config overrides; `sdkconfig` itself is generated and gitignored.
- `cmake/dg_select.py` — the one place that turns `DG_BOARD`/`DG_VARIANT`/`DG_AEC`/`DG_DEMO_AUTOSTART`/
  `DG_PUBLIC_DEMO` into a defaults chain, target, config path and lock. `scripts/idf.sh` and the
  top-level `CMakeLists.txt` both ask it; `scripts/test_dg_select.py` spells out every matrix image.
- `build*/`, `managed_components/` — generated, gitignored.
- `dependencies.lock`, `dependencies.p4xc.lock`, `dependencies.waveshare146.lock` — tracked target pins.
  Restore resolver-induced root/Waveshare changes after recording the actual graph.
  Shared versions agree except approved P4 esp_audio_codec 2.5.0 / S3 2.6.2.

## Conventions

- Add external components via the IDF Component Registry in `main/idf_component.yml`, not vendored copies.
- Put persistent Kconfig choices in `sdkconfig.defaults`, never commit `sdkconfig`.

## Gotchas

- `idf_build_set_property(MINIMAL_BUILD ON)` only builds components `main` depends on. A component's
  Kconfig options are silently dropped from `sdkconfig` if it isn't in the graph. That's why `main`
  lists `esp_psram` in `PRIV_REQUIRES`: without it every `CONFIG_SPIRAM_*` default vanished and PSRAM
  reported 0 bytes. Add any component whose Kconfig you set in `sdkconfig.defaults`.
- EchoEar: 16 MB flash, 16 MB octal PSRAM ([docs/echoear.md](docs/echoear.md)). P4: 32 MB flash/hex PSRAM;
  revision Kconfig selects less-v3/minimum 1.00 for physical rev 1.3.
  Shared partition table has a 4 MB app and named 4 MB display storage.
- Shared firmware/build changes need host tests and the S3/P4/Waveshare matrix,
  AEC image gates, P4 headroom and R10 restoration. Run `scripts/host-tests.sh`
  only after permission; keep configs/secrets out of artifacts. It runs in lanes:
  `--list` names them, `scripts/host-tests.sh peer app` runs a subset, and `--duo`
  runs the duo lanes, which need no IDF environment.

## Direction (2026-10-08)

Goal is a Pipecat client on this board, started from scratch. The plan is to reuse the core of
`pipecat-client-cxx` (RTVI protocol, client state machine, `Transport` interface) and write an
ESP32-specific `Transport` on top of Espressif's `esp_peer`/`esp_webrtc` rather than trying to build
the upstream `SmallWebRTCTransport`, which depends on libdatachannel (usrsctp, libjuice, OpenSSL)
and is not portable to ESP-IDF. The core's only non-portable piece is `src/http.cpp` (libcurl),
which maps onto `esp_http_client`.
