# P4 product-image storage preparation

> **STEP 14 RESTORE GUARD:** Live commands now require `hil/p4_step14.py run`
> and the clean-commit restore record/hash plus its successful boot proof from
> [the step-14 procedure](p4_liveness.md). Do not invoke frozen `p4_product.py`
> directly: its historical factory/keep-tested semantics do not enforce R7.
> The live commands below have been migrated; export `RESTORE_RECORD` and
> `RESTORE_RECORD_SHA256` from the newly approved record before copying them.


Coordinator decision: [decision-2026-11-01-p4-storage-erase.md](../proj-2026-10-30-2143/codex-jobs/decision-2026-11-01-p4-storage-erase.md)
(the authoritative copy is in the main repository's job directory).
Factory storage caused LittleFS `Corrupted dir pair` after the fixed 13a image
booted. Each full factory restore restores that data too.

In the documented checked server environment, inside `hil/bench.py`, insert
`hil/p4_product.py` before the existing helper path, retaining every argument:

```text
python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_display.py live --board p4xc --bundle ... --out ...
```

Supported helper paths are `hil/p4_display.py`, `hil/p4_headless.py`,
`hil/p4_audio.py`, `hil/p4_audio_fix4.py`, `hil/board_smoke.py`,
`hil/board_smoke_bounds.py` and `hil/p4_contention.py`. This includes demo image
paths that provision assets. Direct frozen entrypoints lack this policy; use
the adapter for every P4 product-image acquisition. Continue to supply the
factory backup and any approved product restore pin required by the helper.
From step 14, use the outer `hil/p4_step14.py run` entrypoint with the explicit
restore record and its SHA256; it calls this storage adapter internally. See
[the step-14 run book](p4_liveness.md). The frozen direct entrypoints retain
their historical pre-step-14 restore behavior and are not the current P4 launch
path. The P4 normal image pin is created only after the step-14 build; an absent
pin fails before flashing. Factory restore is the explicit deep-recovery path.

Before entering the helper, the adapter verifies the bundle's hashes and its
partition-table binary, including the IDF MD5 record. Exactly one partition
named `storage`, data subtype LittleFS/SPIFFS, unencrypted, must match the
approved layout (offset `0x410000`, size `0x400000`), without overlapping another
partition. A missing name or a different offset/size refuses before live IO.
The erase addresses are taken from that checked entry.

The helper still owns the armed R7 restore hold. After its candidate flash,
before its first `serial.log` open (which can reset/boot), the adapter rechecks
the bundle, verifies the P4 eFuse MAC and pinned by-id path, then issues pinned
esptool 5.5.0 commands with `--chip esp32p4 --after no-reset`:

1. `verify-flash` for every bundle flash input, including the partition table.
2. `erase-region` for exactly the named storage range.
3. `read-flash` for that full range; require exactly 4 MiB of `0xFF`.

Only then may serial open/boot proceed and empty-only provisioning run. No C6
or other flash range is erased. Errors propagate inside the helper's existing
R7 hold; failure/abort restores its full verified 32 MB factory backup (or the
coordinator's approved product restore). Restore serial logs bypass the adapter.
The step-14 outer adapter binds success, failure and abort to the pinned normal
bundle. Its restore flash is followed by the same named-partition erase and
full FF readback in `restore-storage-NNN/`, before opening the restore console.
Thus storage is deliberately empty after restore; normal mode needs no demo
assets. Provision assets/settings for the next demo run through the usual smoke
path. Candidate hold/resume/cold proof boots do not erase storage. NVS is
preserved; only the named `storage` partition is erased. If the pinned normal
image fails to boot, the outer adapter selects factory deep recovery and the
hold retains the bench lock. The coordinator's SIGUSR1 resumes factory recovery;
it must never retry that unbootable pin. The one-shot proof and exact recovery
commands are in the step-14 run book. Use the clean-commit pin and successful
proof required by fix round 1; the original dirty-tree pin is historical only.
The backup pair is verified before writes even when a product pin is configured.
A transient write/verify/erase error retries the pin under the retained hold;
an open/boot failure clears the pin **and** marks the hold unsuccessful so its
next SIGUSR1 action is factory. Check `restore.json` action as well as `clean`:
`p4-factory` is deep recovery, never proof of a restored product image.

Each run records `candidate-storage-erase.json`: decision, ELF/manifest/table
hashes, partition fields, pinned port, exact commands and phase completion flags,
plus a readback hash and verified byte count. The full readback is retained as
`candidate-storage-readback.bin`, with verify/erase/readback tool logs alongside.
Any phase failure is recorded and prevents candidate boot. Existing storage
evidence is refused rather than overwritten. This record supplements the
helper's acquisition/result evidence; a host-only pass is not a bench result.

The adapter is scoped to a single invocation. S3 delegates directly without
storage parsing or device commands; offline analysis also retains its original
entrypoint. Frozen helper sources stay unchanged.
