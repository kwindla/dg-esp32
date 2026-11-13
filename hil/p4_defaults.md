# Step 12b default validation and playback seed derivation

**Default round closed (2026-11-01, user decision
`decision-2026-11-01-p4-mic-revert-30db`).** MIC1 stays at **30 dB**,
ES7210 register `0x43=0x1a`, matching the qualified P4 image. The 24 dB
candidate (`0x18`) passed standard-prompt STT (WER 0, zero clips), but its
dedicated board linearity captures provided no doubling step with both
endpoints at 10 dB SNR. The retained v2 acoustic band is therefore unavailable.
All 24 dB captures, analyses and validation/seed tools remain as evidence.

The encoder stack remains **26624 B**, digital gain stays at the bundle
default, and the playback seed remains **−289**. The derived **−257** candidate
is retained as unconfirmed evidence. The restored 30 dB setting preserves the
6–30% clean-band, voice/product-pair, display, contention and liveness baseline.
AEC-on echo reaches MIC1 about 6 dB hotter than at 24 dB; step 15b measures
clipping and ERLE and revisits gain only if those measurements warrant it.
No new default acquisition or seed adoption is authorized by this closure.

The procedures below document the historical **24 dB candidate round** and
retain its original paths and outstanding candidate-confirmation details;
they are not instructions to reopen the closed round or transfer its ELF
eligibility to the rebuilt 30 dB image.

Coordinator only for every live command below. The writer builds and tests;
hardware validation is still owed. Use the quiet-room/placement approval and
the existing bench runner; never hold a build lock around `hil/bench.py`.
Run from `/home/khkramer/src/dg-esp32-p4`. Nothing here repins production.

The measured analog rule selects **24 dB**, replacing the 30 dB placeholder:
ES7210 MIC1 `0x43: 0x1a → 0x18`. ES8311 REG0D/REG44 remain `0x01/0x58` and
volume remains −6 dB. The P4 encoder stack stays **26624 B**. Digital gain and
playback seed remain the bundle defaults. Read those values from the delivered
manifest, never from a guessed former default. This is a candidate image until
the following STT/clip/resource checks pass.

## Environment, restore and original source recording

Use the **already verified** step-14 normal restore record/hash and successful
boot proof from [p4_liveness.md](p4_liveness.md). Do not pin the candidate as a
convenience. The new standard-prompt helper calls the same `restoring`,
`candidate_storage` and signal-safe hold directly; the frozen step-14 dispatch
table does not list this additive helper. It prints the Python runner PID for
the existing SIGUSR1/factory-selection recovery procedure. Any nonzero exit
stops the chain. Original output paths must be fresh.

```bash
export TMPDIR="$HOME/.cache/gap3-tmp"
export OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=hil:server
export MAIN_REPO="$HOME/src/dg-esp32"
export DELIVERY="$HOME/src/evidence/echoear/p4/step-12b/fix-7"
export DEFAULT_RUN="$DELIVERY/coordinator"
mkdir "$DEFAULT_RUN"
export P4_BUNDLE="$DELIVERY/bundle-p4xc-normal"
export S3_BUNDLE="$DELIVERY/bundle-echoear-normal"
export STEP9A_FIXTURES="$PWD/hil/fixtures/echoear360"
: "${RESTORE_RECORD:?accepted normal restore record with successful boot proof}"
: "${RESTORE_RECORD_SHA256:?separately verified accepted record SHA256}"
: "${BOT_CAPTURE:?existing unchanged demo bot capture directory}"
```

Retain live TTS **without a server edit**: launch the unchanged demo bot exactly
as in [board_smoke.md, Coordinator-owned bot and identity](board_smoke.md#coordinator-owned-bot-and-identity),
with `ECHOEAR_AEC_SESSION_DIR="$BOT_CAPTURE"` and `ECHOEAR_STT_PROFILE=demo`.
That documented command also writes the PID and identity. A suitable running
process can be reused; do not restart a process merely for one leg of a pair.
The existing `SessionRecorder` writes `session-*/tts.wav` (pre-transport TTS),
`uplink.wav` (actual received device uplink) and `events.jsonl` with audio storage
offsets, peer binding, actual STT profile and original turn boundaries.
The recorder closes on disconnect. Retain the whole directory, bot log and
identity. It does not require a wrapper, policy change, or new server mode.

## Fresh acoustic eligibility on the rebuilt ELF

The old 6–30% P4 band belongs to a different ELF. Do not pass its reanalysis to
the new voice runner: the same-ELF gate will reject it. Obtain the new original
acoustic run and reapply the existing user decision, keeping the originals:

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" audio acoustic --board p4xc --bundle "$P4_BUNDLE" --out "$DEFAULT_RUN/acoustic" || exit $?
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools python hil/p4_audio.py reanalyze "$DEFAULT_RUN/acoustic" --exclude-hz 500 --min-chain-snr-db 10 --out "$DEFAULT_RUN/clean-band.json" || exit $?
```

This retains 500 Hz as a reported speaker characteristic, applies the 10 dB
per-chain noise eligibility rule, and leaves THD≤5%, H2/available H3≤−26 dBc,
linearity and zero-clip limits unchanged. Require a nonempty band suitable for
the canonical −6 dB voice procedure per [p4_audio.md](p4_audio.md).

## Standard prompts, live STT and clips

The frozen standard mic set in `audio-plan.json` is France, Italy and
`prompt_speech.wav`. The expected texts come from the frozen conversation and
server fixture manifests. The helper plays their existing waveforms once,
retains native ADC PCM and pre/post PERF, then uses the live bot's received
transcript/profile. Every transcript row is saved first in `transcripts.json`
and retained in `prompt.json` and the run result. Multiple received transcript
rows for one original prompt are scored as **`split-transcript` failure**, even
if their joined text would pass accuracy. Joined text is diagnostic only. This
conservative choice keeps a split original as an STT failure eligible for the
frozen fallback sequence; it never turns it into an acquisition error inviting
a retake. No joining rule is added to the frozen scoring procedure.
It does **not** use prerecorded Nova transcription as a
substitute for the actual live STT profile. Bot completion and resource/stack
checks occur before leaving the inherited restore hold. Acquisition success
does not mean STT validation passed.

Candidate S3 reference evidence:
`~/src/evidence/echoear/p4/step-12b/fix-5-live/voice-echoear-2` (and sibling
`voice-echoear`) has successful original France and long conversation turns,
the same conversation waveform hashes, and the demo Flux profile
(`flux-general-en`, pipecat 1.12.0, Deepgram SDK 7.12.0). The coordinator must
confirm the **new run's actual profile** still matches before using it.
These are useful candidates for the France/long conversation comparison, but
they **do not establish a complete same-prompt band** for the standard mic set:
their Italy stimulus was talk-over context, and `prompt_speech.wav` differs in
bytes from conversation `france.wav` despite the same expected words. No complete
matching standard-set reference has been identified. No numerical S3 worst-WER
bound is invented here. Confirm other retained original references, or acquire
the exact set on S3 using the same bot/profile before scoring P4:

```bash
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_defaults_validate.py live --board echoear --bundle "$S3_BUNDLE" --out "$DEFAULT_RUN/mic-s3" --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" || exit $?
python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_defaults_validate.py live --board p4xc --bundle "$P4_BUNDLE" --out "$DEFAULT_RUN/mic-p4" --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" || exit $?
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_defaults_validate.py score "$DEFAULT_RUN/mic-p4" --s3 "$DEFAULT_RUN/mic-s3" --out "$DEFAULT_RUN/mic-validation.json" || exit $?
```

Pass each prompt at frozen `speech_accuracy` accuracy ≥0.90 and
`echoear_hot_stt.word_errors` WER no worse than the **worst original matching S3
prompt/profile** (multiple confirmed original reference directories can follow
`--s3`). ADC and gain-clip deltas, and native ADC clips, must be zero.
Missing/mismatched or wrong-schema reference (including a voice-smoke directory)
returns `verified=false` with an `unverified` disposition and exit 1; it is not a new
success threshold. Preserve every failed original and all actual received
`uplink.wav` files, frame offsets and STT events. No scored retakes.

Do not change digital gain during the default round. If live STT fails, stop
default adoption and record the failure. The frozen follow-up sequence is
100, 150, 200, 250 percent in that order, intersected with integer 1..1600 and
`worst_native_peak * percent/100 ≤ 0.9*32767`. Obtain the worst native peak from
all original prompt captures (not a conveniently quiet window). Begin with the
bundle's declared gain clamped to that allowable set; test the fallback sequence
only after its labelled original fails STT. Each fallback needs a fresh labelled
capture and the same gates; stop at the first passing candidate. This helper
deliberately performs no gain writes; a follow-up acquisition must explicitly
implement/record that selected runtime gain and readback. Do not silently edit
the bundle, board overlay or source gain from this offline score.

## Three independent live voice/resource sessions and seed source replies

Run the established voice smoke on the normal bundle three times, each with
its own flash/fresh boot. The bundle declares encoder allocation 26624 B.
Each run must retain ≥500 20 ms encoded frames and ≥100 real speech decodes;
encoder, decoder and owner free stack each ≥4096 B; P4 peer-rx free ≥
`max(1744, ceil(allocation/4))` B. Report every task minimum, idle/active/long
downlink/end heap, PSRAM and per-core CPU. Internal minimum ≥81920 B, largest
block ≥32768 B, zero failed allocations; unchanged −6 dB output and full product
wire/resource checks remain in force. The existing `costs` gate enforces these.

```bash
for trial in 1 2 3; do
  python3 hil/bench.py uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_step14.py run --restore-record "$RESTORE_RECORD" --restore-record-sha256 "$RESTORE_RECORD_SHA256" audio-fix4 voice-smoke --board p4xc --bundle "$P4_BUNDLE" --out "$DEFAULT_RUN/voice-$trial" --bot-dir "$BOT_CAPTURE" --bot-identity "$BOT_CAPTURE/identity.json" --bot-repo "$MAIN_REPO" --pair-id "p4-defaults-$trial" --acoustic-reanalysis "$DEFAULT_RUN/clean-band.json" --headless --calibration || exit $?
  uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools --with idf-component-manager==3.1.2 --with esptool==5.5.0 python hil/p4_audio_fix4.py costs "$DEFAULT_RUN/voice-$trial" --out "$DEFAULT_RUN/costs-$trial.json" || exit $?
done
```

For wire contrast, retain a fresh `audio-fix4 voice-smoke --board echoear` normal
capture under the same bot identity and use the existing `pair` command from
[p4_audio_fix4.md](p4_audio_fix4.md). The qualified S3 stack/reference rules do
not become P4 calibration values. There is no new S3 firmware behavior change
in this round; shared host checks and P4 register fixtures change only.
**No four-variant S3 R3 smokes are owed.** Both fix-7 reviewers independently
compared normal, demo, AEC-normal and AEC-demo flash sets: all are masked-equal,
with zero unmasked differing bytes using only the established metadata masks.
Fable compared against the `2b2b04ee` pins; Opus compared against step-14 fix-5
bundles, already proven equal to those pins. Canonical configs match 13b with
empty keydiffs. This supersedes the earlier conservative smoke debt; the fresh
same-prompt S3 STT reference above is still needed for microphone validation.

Choose **three original long replies**, e.g. turns 3, 4, 5 from `voice-1`.
These are independent original responses, not three copies of a saved waveform.
Find that run's recorder directory by its peer ID in `turn-03.json` and the
corresponding `session-*/events.jsonl` peer-binding. Do not choose by directory
mtime. The selector checks peer/profile, closed recorder, completed long turns,
ordered source offsets and clean acquisition; derivation rechecks source hashes,
rejects overlap/duplicate PCM, and replays each reply from a cold DSP instance.

```bash
: "${TTS_SESSION:?exact closed session directory for voice-1 peer, under BOT_CAPTURE}"
uv run --project server --frozen --offline --group dev python hil/p4_seed_derive.py build-probe --out "$DEFAULT_RUN/seed-probe" || exit $?
uv run --project server --frozen --offline --group dev python hil/p4_seed_derive.py select --run "$DEFAULT_RUN/voice-1" --session "$TTS_SESSION" --turns 3 4 5 --out "$DEFAULT_RUN/original-replies.json" || exit $?
uv run --project server --frozen --offline --group dev python hil/p4_seed_derive.py derive --selection "$DEFAULT_RUN/original-replies.json" --policy "$DELIVERY/playback-policy-normal.json" --probe "$DEFAULT_RUN/seed-probe" --out "$DEFAULT_RUN/seed-candidate" || exit $?
```

The delivered policy was extracted with the allow-listed resolved-config parser
and is bound to the built ELF/manifest and DSP source hashes. The host probe
uses `-fno-access-control` **only for offline observation** of unchanged
PlaybackDsp state; it adds no firmware field or getter. The actual product HPF,
normalizer/compressor, activity mask and peak limiter run unchanged. Effective
source is compressed pre-limiter output divided by its current normalizer gain;
limiter attenuation is excluded. Each active effective RMS is converted to dBFS;
take the median of the three, round `10*dBFS` half away from zero and clamp
−600..−100. `.active` files retain the exact product activity mask, while
`reply-*-product.pcm` retain latency-trimmed rendered PCM. The JSON is a candidate
only (`passed=false`, `qualification=false`). Kconfig is not written.

Probe limits: its per-sample activity flag changes at 320-sample (20 ms) window
boundaries and lags the estimator's same-window power decision; the first 319
samples are inactive. Opus reproduced −24.4761/−26.4504/−25.7468 dBFS with this
product-flag mask (candidate **−257**). Alternative same-window estimator gating
gave about −24.38/−26.32/−25.54 dBFS, median −25.54 and candidate **−255**.
Replaying from the derived seed also shifted the median to about −25.47 dBFS
(−255), because compression follows AGC. These 0.2–0.3 dB sensitivities are
limits, not a changed rule or an adopted seed. Cold/steady Jabra confirmation
remains required. The three retained originals are independent replies from one
session/peer, not three independent sessions. Delivered probe/policy/candidate
artifacts remain unchanged; this host-only follow-up is recorded separately.

## Cold first-word and steady-state Jabra confirmation

Keep original Jabra `reply-1.wav` from each fresh-boot voice run (a cold first
reply **only if no earlier TTS was delivered**), all selected long-reply Jabra WAVs, corresponding original TTS PCM/text,
product masks, mixer/volume readbacks, room baseline and clip/transport counters.
First-word means the actual first spoken word, not an arbitrary initial silence
window; annotate its source sample bounds from the original audio/text. Align
each original source to Jabra using the frozen `playback_quality.decoded_match`
block correlation procedure and retain offsets/scores. A truncated reply or
unavailable/poor alignment is deferred, never a level pass. Do not synthesize
or stretch missing content. Retain the raw Jabra WAV and selection bounds.

For each cold and steady measurement, put the observed source/Jabra alignment
offset and annotated first-word or steady source bounds in a **new**
`$DEFAULT_RUN/jabra-marks.json` list. Each row names `kind` (`cold-first-word` or
`steady`), `jabra` (original 16 kHz mono PCM16 WAV), `activity` (the product
`.active` file for that whole source reply), `source_first`, `source_end`,
`jabra_offset_samples`, and `alignment_passed=true` with an `alignment_evidence`
path. Cold masks must come from a separate cold replay of the complete original
first reply using the same seed-probe/policy; do not reuse a long reply's mask.
The probe command is its binary followed by input PCM, output PCM, then policy
fields in `p4_seed_derive.FIELDS` order. For example, replay the full original
first reply from `voice-1` below. Set `COLD_SESSION` by peer ID as above. This
rejects an earlier greeting/TTS; if that occurs, the harness's `reply-1.wav` is
not a cold-start recording. Retain that finding and acquire a fresh capture
with Jabra armed before connect in a later coordinator procedure; do not relabel
a warm word as cold.

```bash
: "${COLD_SESSION:?closed recorder directory matching voice-1's first turn}"
uv run --project server --frozen --offline --group dev python - <<'COLD_MASK'
import json, os, subprocess
from pathlib import Path
from p4_seed_derive import FIELDS,pcm,digest,write
root=Path(os.environ['DEFAULT_RUN']);session=Path(os.environ['COLD_SESSION'])
turn=json.loads((root/'voice-1/turn-01.json').read_text());events=turn['events']
first,last=min(e['monotonic_ns'] for e in events),max(e['monotonic_ns'] for e in events)
rows=[json.loads(line) for line in (session/'events.jsonl').read_text().splitlines()]
audio=[e for e in rows if e.get('kind')=='audio' and e.get('stream')=='tts']
selected=[e for e in audio if first<=e['monotonic_ns']<=last]
assert selected and audio[0]==selected[0], 'Cold evidence unavailable: earlier TTS or wrong session'
assert {e['pc_id'] for e in selected}=={e['pc_id'] for e in events if e.get('pc_id')}
assert all(a['sample_end']==b['sample_start'] for a,b in zip(selected,selected[1:]))
begin,end=selected[0]['sample_start'],selected[-1]['sample_end']
raw=root/'cold-first-source.pcm';raw.write_bytes(pcm(session/'tts.wav')[begin*2:end*2])
policy=json.loads((Path(os.environ['DELIVERY'])/'playback-policy-normal.json').read_text())
stats=json.loads(subprocess.check_output([str(root/'seed-probe'),str(raw),str(root/'cold-first-product.pcm'),*[str(policy['config'][field]) for field in FIELDS]],text=True))
write(root/'cold-first-replay.json',dict(source_sha256=digest(session/'tts.wav'),events_sha256=digest(session/'events.jsonl'),sample_start=begin,sample_end=end,stats=stats))
COLD_MASK
```

This offline command retains levels,
clips and comparisons to every measured P4 frequency-specific Jabra range:

```bash
uv run --project server --frozen --offline --group dev --with pyserial --with pyelftools python - <<'CONFIRM'
import json, os
from pathlib import Path
import numpy as np
from p4_seed_derive import pcm,digest,write
from p4_audio import reanalysis_gate
root=Path(os.environ['DEFAULT_RUN']);bundle=Path(os.environ['P4_BUNDLE'])
manifest=json.loads((bundle/'manifest.json').read_text())
band=reanalysis_gate(root/'clean-band.json',manifest)['level_band_dbfs']
marks=json.loads((root/'jabra-marks.json').read_text());rows=[]
for mark in marks:
    assert mark['alignment_passed'] and Path(mark['alignment_evidence']).is_file()
    samples=np.frombuffer(pcm(Path(mark['jabra'])),'<i2').astype(float)
    mask=np.fromfile(mark['activity'],dtype=np.uint8).astype(bool)
    first,end=mark['source_first'],mark['source_end'];offset=mark['jabra_offset_samples']
    assert 0<=first<end<=len(mask) and 0<=first+offset<end+offset<=len(samples)
    window=samples[first+offset:end+offset];active=window[mask[first:end]];assert len(active)>0
    level=float(20*np.log10(np.sqrt(np.mean(active*active))/32768));clips=int(np.count_nonzero(np.abs(window)>=32767))
    comparisons={hz:dict(measured_p4_jabra_dbfs=chains['jabra'],inside=chains['jabra'][0]<=level<=chains['jabra'][1]) for hz,chains in band.items()}
    rows.append(dict(mark=mark,active_rms_dbfs=level,clips=clips,comparisons=comparisons,jabra_sha256=digest(mark['jabra']),activity_sha256=digest(mark['activity']),alignment_sha256=digest(mark['alignment_evidence'])))
assert {'cold-first-word','steady'}<=set(r['mark']['kind'] for r in rows)
write(root/'jabra-confirmation.json',dict(qualification=False,passed=False,measurements=rows,clean_band_sha256=digest(root/'clean-band.json'),disposition='Review measured frequency-specific P4 ranges; any outside range or clip defers selection; no S3 band or tone-to-speech scalar threshold is invented'))
CONFIRM
```

The clean band contains frequency-specific tone ranges; preserve those individual
comparisons rather than inventing a single tone-to-speech threshold. Any outside
range or clip defers selection with all measurements retained. Current-image
captures characterize the **existing** bundle seed. If the newly derived seed
differs, its physical cold/steady confirmation remains pending a later approved
candidate image; this round does not claim that an unapplied seed was tested.
Do not compensate with an S3 band or modify target/ceiling/HPF/compression.
Applying a derived seed to Kconfig is explicitly a later measured/reviewed round.
