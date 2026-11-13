# Echo dropout diagnostic preparation

This is an exploratory diagnostic, not a qualification campaign. The preparation
request authorizes host work only. The coordinator may run it after review; no
run, flash, bench lock, vendor request, Jabra playback or firmware build is part
of preparation.

`hil/echoear_aec_echo_dropout.py` has separate `prepare`, `validate`, `analyze`
and explicit `run` subcommands. Preparation copies the required configuration
from the retained campaign-10 declaration, binds its hash and the host Python
sources, and verifies both image bundles without opening hardware. It rejects
configuration or source changes at launch. Before acquiring the lock, `run` also
checks that the interpreter selected by `flash()` can import esptool; a missing
interpreter, import failure or timeout stops before bench access. This does not freeze or modify any
campaign declaration.

## Fixed design

One candidate boot, one connection and ten separate captures, all at −6 dB:

1. Baker target 1–4 (target 1 is cold).
2. Red-kite control.
3. Baker target 5–8.
4. Silver-spoon control.

The exact target is: “A friendly baker opens the shop before sunrise. The fresh
loaves cool beside the open window.” These are fresh TTS requests with identical
text, **not identical PCM replay**. The retained campaign's Flux voice/settings,
proactive reconnect behavior, 15 dB analog reference gain and display character
remain in effect. No manual AEC reset occurs between replies. No Jabra fixture,
near-end speech, transport reconnect, direct probe, soak or quick-gap pair is
scheduled. There are no retries, spares or performance-dependent decisions.

The ordinary `BenchHold` owns the shared nonblocking flock across acquisition
and restoration. It preserves/restores the Jabra mixer, verifies the candidate
ELF `61aa38d58559abe87bc54397fa8df0cec2411c1d37070d809b98d9d4e3b17242`
and boot gain, and restores/hash-verifies normal ELF
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758` before
releasing the lock. A restoration failure retains the lock. The runner additionally
requires exact mixer restoration, lock-release evidence and no cleanup errors.

One settled six-second baseline is recorded with clock anchors. Each reply has
its own anchored start/stop, 2.3-second pre/post-roll and complete
MIC1/MIC3/AFE PCM plus timing rows. Existing noise-v3, resource, CPU, liveness,
RF/profile, observer, delivery and transport checks remain active. The final
completed-case list distinguishes accepted acquisitions from unfinished exports
or failures. Any noise, acquisition, long-TTS or integrity failure ends the hold;
originals remain. No gate is relaxed because this is exploratory.

## Time and cleanup

The planning cap is **20 minutes including flash and restore**: at most 14 minutes
of active work (including setup/final audit) plus six reserved for cleanup.
Before each reply, at least 210 active seconds must remain for its capture,
export and boundaries. If not, the hold stops instead of starting another reply.
A one-shot process alarm also bounds active work; SIGINT/SIGTERM take the same
cleanup path. Normal captures retain their existing 24-second work deadline,
28-second stop cap and 150-second export timeout.

A timeout or operator stop retains an emergency stop anchor, stops audio, and
enters the existing final audit/restoration path. If evaluation/bot acquisition
never started, cleanup records the live audit as not-started and retains the
original error; restoration still runs. Started sessions keep their normal audit. The deadline is disarmed during
cleanup and repeated termination signals are ignored until restoration finishes.
The runner reports elapsed time and fails if a completed hold exceeds 20 minutes.
This is a bounded-work plan, not a hard real-time promise: failed restoration must
never be killed or release the bench merely to meet the timer. Missing replies
are reported as not-run, not synthesized or silently retried. Analysis runs after
the bench is released.

## Host-only commands

From the `aec-v85` worktree, use the existing frozen host environment:

```sh
OPENBLAS_NUM_THREADS=1 PYTHONPATH=hil:server uv run --project server --frozen --offline \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
  python hil/echoear_aec_echo_dropout.py prepare --source /path/to/campaign-10/declaration.json --out /new/prep-directory --run-out /new/echo-dropout-02
```

`--run-out` also generates `launch-after-review.sh`, which exports
`ESPTOOL_PYTHON=/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python`
separately from the frozen uv environment used for HIL/bot dependencies.
`validate --plan /new/prep-directory/plan.json` repeats file-only checks.
`analyze --capture /retained/capture.npz --quiet /retained/quiet.npz --out /new/analysis.json`
never changes the inputs or overwrites an existing output. For launch **only after
review**, the coordinator supplies `run --plan ... --out /new/run-directory
--reviewed-plan-sha256 <reviewed-plan-file-hash>` with the same Python prefix.
An existing output directory is refused. The preparation report supplies concrete
paths and a shell command file; it has not executed that file.

## Metric contract

The analyzer imports the existing `load_capture`, `activity`, `frame_power`,
`reference_threshold` and `reduction` helpers. It uses 320-sample/20 ms frames,
MIC3-only activity against the separately settled quiet reference, and the fixed
1024-sample raw-to-AFE measurement delay. No correlation/delay search, output
activity selection, waveform identity gate or new acceptance threshold is added.
Reference activity without delayed AFE coverage makes the result unscorable;
no padding or tail trimming grants credit.

Each reply's analysis includes raw/reference/AFE dBFS, reduction and residual
energy for every complete aligned frame; whole integrated/median-frame reduction;
and the share of total active AFE residual energy in the **five highest-energy
active frames, which need not be contiguous**. The selected frame indices are
retained. Reduction with those five removed is explicitly report-only and never
replaces the whole-reply value. A zero-energy denominator returns null. No active
reference, missing timing or a gap is unscorable. Input hashes are checked before
and after analysis. Failed/noisy captures retain descriptive numbers but are
marked excluded; the summary lists all ten scheduled replies, including not-run
and incomplete cases. No acoustic result decides whether another reply runs.
