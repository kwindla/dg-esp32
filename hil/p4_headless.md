# Headless P4 network acquisition

> **STEP 14 RESTORE GUARD:** Live commands now require `hil/p4_step14.py run`
> and the clean-commit restore record/hash plus its successful boot proof from
> [the step-14 procedure](p4_liveness.md). Do not invoke frozen `p4_product.py`
> directly: its historical factory/keep-tested semantics do not enforce R7.
> The live commands below have been migrated; export `RESTORE_RECORD` and
> `RESTORE_RECORD_SHA256` from the newly approved record before copying them.


This is the current launch form for `p4_headless.py`, superseding historical
step-12a report commands. The runner owns the bench lock; release build locks
first. Use an existing unchanged bot process and its recorded identity. No bot
is started by these commands. S3 restores its image of record; P4 keeps a
successful acquisition and restores factory only on acquisition failure/abort.
Offline analysis failures leave a successfully acquired P4 installed.

```bash
cd "$HOME/src/dg-esp32-p4"
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
P4_HEADLESS_PYTHON="$HOME/.espressif/python_env/idf6.1_py3.12_env/bin/python"
: "${BOT_DIR:?existing unchanged bot process directory}"
: "${BOT_IDENTITY:?recorded bot identity JSON}"
: "${S3_BUNDLE:?verified normal AEC-off S3 bundle}"
: "${P4_BUNDLE:?verified normal AEC-off P4 bundle}"
: "${HEADLESS_OUT:?new evidence parent directory}"

# Dry dependency imports/version checks, before bench entry; no device access.
uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 \
  python hil/board_smoke.py --preflight-tools

"$P4_HEADLESS_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 \
  python hil/p4_headless.py --board echoear --bundle "$S3_BUNDLE" \
  --out "$HEADLESS_OUT/s3" --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY"
"$P4_HEADLESS_PYTHON" hil/bench.py uv run --project server --frozen --offline --group dev \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 \
  python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" hil/p4_headless.py --board p4xc --bundle "$P4_BUNDLE" \
  --out "$HEADLESS_OUT/p4" --pair-left "$HEADLESS_OUT/s3" \
  --bot-dir "$BOT_DIR" --bot-identity "$BOT_IDENTITY"
```

The server environment supplies Pipecat/demo_policy as well as the explicitly
pinned device tools. A plain `--project hil` environment is insufficient for
this shared preflight. This is network/headless evidence; audio measurement
and the full product-profile voice pair use [p4_audio.md](p4_audio.md).

## P4 storage preparation

P4 live commands must use [p4_step14.py run](p4_step14.py), which applies the pinned restore policy and the p4_product storage adapter
shown above; frozen helpers called directly do not perform this step. See
[p4_product.md](p4_product.md) for the named-partition checks, erase/readback
evidence and coordinator decision. S3 behavior and offline analysis are unchanged.
