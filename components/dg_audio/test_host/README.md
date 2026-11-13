# Audio host tests

Run `scripts/host-tests.sh` from the repository root. The audio tests need the
pinned, generated `managed_components/espressif__esp_codec_dev` sources. On a
fresh checkout, run **`scripts/idf.sh reconfigure`** first to fetch them. Missing
vendor code is a fatal configure error with that command; the gate never skips
codec tests. No hardware is used.

`trace.*` compiles the current audio source with the pinned vendor ES8311/ES7210
register implementations and deterministic hardware fakes, then compares its
ordered stdout and exit status to `fixtures/trace/*.txt`. The original 11 fixtures are
recorded, identical before/after traces from the step-9 refactor (base e27e428):
AEC off/on startup, startup REG44 NACK/retry, final REG0D/REG44/MIC1 readback
faults, and Waveshare startup. Fault cases must exit 1. No Git history or old
headers are needed by the permanent gate. A deliberate audio change updates
its fixture in the same commit, with a reviewed fixture diff; a regression is
never repaired by blindly refreshing a fixture. This policy also appears in
`test_codec_trace.py`.

An optional historical one-shot witness freezes the old source and headers:

```bash
TMPDIR="$HOME/.cache/gap3-tmp" python3 components/dg_audio/test_host/compare_pre_refactor.py \
  --build build/host-audio --out "$HOME/src/evidence/echoear/p4/step-9/historical-check"
```

It requires the e27e428 object and is never run by CMake/CTest's normal gate.
To run the two focused, isolated mutations manually:

```bash
TMPDIR="$HOME/.cache/gap3-tmp" python3 components/dg_audio/test_host/test_codec_mutations.py \
  --build build/host-audio --out "$HOME/src/evidence/echoear/p4/step-9/mutations"
```

The mutation helper defaults TMPDIR to `~/.cache/gap3-tmp` if unset. It requires
successful compilation and a golden-trace diff to count a mutation as killed.
The usual host suite separately exercises actual pipeline −6 dB/Q15 output,
mic RX-clock descriptor boundaries, stereo-DMA/selected-PCM slot semantics,
slot bounds, and paired-codec RX/TX DMA equality.

Step 12b adds five `p4xc-*` traces: AEC-off TDM startup, the one retryable
REG44 NACK and all three final readback faults. These use the same pinned
vendor source and capture P4's no-rail GPIO53 PA, RX GPIO13 external 256fs,
TX internal 512fs, stereo32 capture, word0-high MIC1, and the spike's
PA-gain=0 DAC volume register 0xba. Existing S3 fixtures are never rebased.
`slots_p4xc` additionally checks the electrical MIC3 and absent MIC4 map.
The null backend uses an independent test table, so it remains link-tested
without GPIO/I2S even though no product board currently selects it.

P4 summary fixtures explicitly include register −2.5 dB (0xba), a +6 dB
register offset from S3. Board table tests pin every paired clock limit; the
host-only `p4_audio_profile` executable exports table defaults for bundle
metadata, avoiding a second operative analog/volume default.
