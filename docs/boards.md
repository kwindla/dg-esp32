# Board porting and image lifecycle

The shared application supports EchoEar (ESP32-S3), P4-XC (ESP32-P4) and the
retired build-only Waveshare-1.46. The project target is **smoke parity**, not
qualification of new images. [The parity table](p4xc-parity.md) records evidence
coverage; [the step-16 run book](../hil/p4_parity.md) defines the current campaign.

| Board selector | Target | Display / logical canvas | Audio | Hardware policy |
|---|---|---|---|---|
| `echoear` | esp32s3 | ST77916 QSPI 360×360 / 360×360 | ES8311 + ES7210, electrical MIC3 reference | Bench under pinned identity |
| `p4xc` | esp32p4, rev 1.3 | JD9365 DSI 800×800 / 360×360 | ES8311 + ES7210, electrical MIC3 reference | Pinned normal restore required |
| `waveshare_146` | esp32s3 | SPD2010 QSPI 412×412 / 412×412 | Direct I2S MEMS + PCM5101 DAC | Build-only, no AEC/demo |

## Adding a board

1. Add the board Kconfig choice in `main/Kconfig.projbuild`, a defaults branch
   and a selected backend in `components/dg_board/CMakeLists.txt`. Keep the
   established `DG_ECHOEAR_AEC*` and `DG_DEMO_AUTOSTART` authoritative inputs;
   widen their dependencies only when the hardware supports them. Do not create
   aliases that could diverge. Preserve existing board defaults.
2. Implement the `dg_board` interfaces for identity, early rail/backlight
   initialization, I2C, audio hardware tables, power, console, touch and canvas
   geometry. `BoardInfo` declares battery/reference/console/touch capabilities;
   unsupported hardware must return an explicit unavailable/unsupported result.
   Hardware-specific register programs and pins belong behind these interfaces.
3. Add display source selection under `dg_display/backends/` and board-specific
   audio selection under `dg_audio/src/codec/`. Shared application/UI/engine code
   must not grow board `#if` branches. Use the existing host fakes and
   [presentation contract](display.md#presentation-seam-p4-port-step-10).
4. Add target-conditional component manifest entries and an independent target
   lock. With `MINIMAL_BUILD`, every component whose Kconfig is set must be in
   `main`'s transitive graph in that same change. Component discovery cannot
   depend on CONFIG values that do not exist until dependency discovery runs.
   Prove public overlay keys survive with `sdkconfig_keydiff.py --expect-keys`.
5. Add the board to `cmake/dg_select.py`, which selects the target, defaults,
   config/build directory and dependency lock for `scripts/idf.sh` and for a
   plain `idf.py` alike, and its expected selection to `scripts/test_dg_select.py`.
   Add `hil/boards.py` identity/port support via
   a new adapter when that file is frozen. A new physical board needs a pinned
   USB identity, eFuse MAC and matching chip check before any write. Enumeration
   names such as ttyACM0 are not identities. A build-only board must reject all
   hardware actions.
6. Measure board-specific values: mic and playback levels, AEC reference gain,
   delay and guard, encoder cost/stacks and runtime heap floors. Do not transfer
   S3 calibration labels onto another board. Preserve evidence outside git and
   distinguish selected, diagnostic, provisional and fully measured findings.

## Build and bundle gates

Every build is serialized by `scripts/build_locked.sh` or `scripts/idf.sh`,
including direct legacy Python builders. `managed_components/` is shared
between target builds within a worktree. Never build while the coordinator
requires an idle host. Restore resolver-induced tracked lock changes after
S3 AEC-off and Waveshare builds; preserve the measured actual graph before
restoring the canonical pin. Shared versions must agree except the approved
P4 audio-codec 2.5.0 / S3 2.6.2 revision exception.

Run `scripts/host-tests.sh` after permission. For shared firmware/build changes,
build four S3 variants, four P4 variants and Waveshare; check both targets' AEC
archives/images and P4 static headroom. Use fresh generated configs, compare
all effective keys with the approved names-only parser, and run the R10
switch/reconfigure matrix. Final S3 graph/config/versions must match its first
build; compare complete flash sets with established metadata masks, exact
offsets and ELF/image binding. A source/config equality claim does not imply
old image bytes are identical when build paths change.

Freeze each image **immediately after its build**, before another variant can
reuse its build directory. `hil/make_bundle.py` copies public flash inputs and
the ELF, hashes every artifact and records commit/dirty-source provenance.
P4 uses `scripts/p4_bundle.py` for target-lock/public audio metadata and
authenticated calibration labels. Never copy a resolved config, private
defaults or environment file into a bundle. Preserve historical evidence;
fresh output directories must not overwrite prior bundles or results.

## Restore lifecycle

Before the first P4 write, retain and verify the full factory backup and
its explicit recovery recipe. A product restore-of-record must be normal
AEC-off, hash-bound to a verified bundle/headroom/partition table and exact
commit. `p4_step14.py pin` is offline and **does not prove boot**. The coordinator
must run boot proof and normal-image smokes before acquisitions use the pin.

The current record is selected by
`~/src/evidence/echoear/p4/step-15-pin-round/pin.env`, under immutable commit
`52482495d1b859b5313cd8c76e4cd0cdd97ea507`. Its SHA256 is
`a2c846832b43636fc73564d6a1d32c22ebcbc95458c8906de0d0550f028d653b`.
Every P4 run authenticates record, bundle, manifest, headroom and partition
table before restoring, erases only named storage, verifies FF readback and
checks the restored boot ELF. NVS is preserved. Keep the inherited recovery
hold armed through acquisition and cleanup errors; failed restore retains the
bench lock. Factory fallback is a recovery outcome, not a clean product restore.
See [the recovery procedure](../hil/p4_liveness.md#required-one-shot-boot-proof-and-deep-recovery).

## Compatibility and evidence

Do not modify the bot or frozen HIL readers to make a port pass. Add adapters.
`wire_contract.py` validates independent schemas before comparing two boards;
only the declared calibration and platform fields may differ. Product and
bench-display evidence are separate required profiles. Physical touch needs a
user tap; synthetic gestures cannot prove the physical controller. Report-only
results, missing measurements and hardware exceptions must remain visible in
the feature table. Old qualified S3 images stay reference-only and are never
silently promoted by smoke parity.
