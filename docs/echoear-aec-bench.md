# EchoEar AEC measurement foundation, v1

This is the step-1 measurement contract for PLAN revision 3.1. It is not an
acceptance campaign. AEC remains default off. Run each witness independently;
a failed quiet, probe, speech, near, doubletalk or live attempt cannot prevent
another predeclared witness. Retain every failure. Never reuse an acquisition
identity or replace a failed run with a retake.

The old `echoear_aec_scored.py` / `echoear_aec_scoring.py` remain historical and
are unused by this runner. Their source-identity prerequisite is not a gate here.

## Running

Use the pinned server environment plus the HIL dependencies. Set
`ESPTOOL_PYTHON` to the IDF environment's Python (the HIL environment need not
contain esptool); acquisition checks that dependency before taking the bench.
For example:

```sh
uv run --project server --with pyserial python hil/echoear_aec_bench.py speech \
  --plan /absolute/path/speech-run.json \
  --out /home/khkramer/src/evidence/echoear/aec-demo/step-2/speech-001
```

The same command with `--capture retained.npz --quiet quiet-at-this-gain.npz`
only analyzes existing data. `--events events.jsonl --anchors clock-anchors.json`
adds recorded live events and timing. `--transcribe` requests prerecorded STT
for an offline doubletalk analysis. A fresh doubletalk acquisition always
transcribes the captured AFE output. Credential loading goes through
`server/settings.py`; credentials are never written to the run declaration.

A declaration has these fields (paths and hash are examples to replace):

```json
{
  "run_id": "speech-001",
  "mode": "speech",
  "afe_configuration": "esp-sr-2.4.7-MR-filter4-FD_LOW_COST-chunk512",
  "image": "/absolute/path/to/frozen-image",
  "elf_sha256": "full-frozen-ELF-SHA256",
  "ref_gain_db": 0,
  "volume_db": 0,
  "seconds": 18,
  "server_url": "http://192.168.7.222:7861"
}
```

Choose the actual host address; this is the device's runtime application URL,
not a network/AP change. The runner never provisions NVS. Supply `near_fixture`
to override the existing fixed `prompt_france.wav`. Step 1 accepts only the
existing 0 dB reference setting; step 3 will add validated gain selection.
Every fresh acquisition takes its own settled quiet capture at the same gain.
Quiet acquisition is a measurement, never a raw/AFE equality test. The probe
is the firmware's 2 s source. Speech uses four copies of the fixed greeting
through the offline bot's normal WebRTC/decoder/DSP/speaker path. Doubletalk
uses that same sustained source and a near fixture starting 2 s later; the
offline bot cannot interrupt it. Live uses the real `demo_bot.py` and Flux.

Acquisition owns `/tmp/claude-1000/echoear-bench.lock` with `flock` through
normal-image restoration and full boot ELF-hash verification. The only board
is the serial-by-id B4:3A:45:16:2D:94 device. Jabra must be card 0 and USB
0b0e:0412. Mixer state is saved/restored, hardware is not moved, and only an
owned bot on 7861 is started/stopped. An occupied 7861 is an error. No 7860
operation occurs. Normal restoration is the frozen 9936da45 image.

An output directory must not exist. It retains the declaration, resolved
fixture settings, source and dependency identity, captures, clock anchors,
serial and bot logs, resources, mixer states, and restoration evidence. An
exception writes `failure.json`; it does not delete partial evidence. Step 1
itself uses host/synthetic/retained evidence; the plan's first hardware hold
is step 2's live resource measurement.

## Frozen metrics

All PCM is mono signed 16-bit, 16 kHz. Frames are nonoverlapping 320 samples
(20 ms). Incomplete frames are excluded explicitly, never padded. Reference
activity means frame mean-square MIC3 exceeds the larger of 4 LSB squared and
6 dB above the loudest complete frame of a separate settled quiet reference
at the same gain. This avoids the old -85 dBFS threshold's high-gain noise bug.
Known source windows further restrict the mask. Neither AFE output nor uplink
output chooses the activity mask or alignment.

For probe, the window begins at the independently detected MIC3 frame onset
and lasts exactly 32,000 samples. Speech uses known fixture duration and MIC3
onset. Onset is quantized to a 20 ms frame; it is not an exact sample arrival.
Retained analyses can provide explicit `windows_samples` for multiple replies.
Missing source activity is retained as missing playback, acoustically unscorable.
Do not silently analyze a different playback interval when a fixture is truncated.

The fixed mic-to-AFE delay is 1,024 samples, measured on independent near-only
controls with the unchanged ESP-SR 2.4.7 MR/filter4/FD_LOW_COST/512-sample adapter.
Any AFE, mode or framer change requires new independent delay measurement. No
search for an ERLE-maximizing delay is implemented. Missing delayed output
makes the measurement unscorable.

Report integrated active-energy reduction (`10 log10(sum(raw²)/sum(AFE²))`),
median per-frame reduction, and the same measurements in the first 500 ms
following EACH reply's MIC3 onset. The first-500-ms window includes active
frames within those 500 ms, not the first 25 active frames spread over time.
AFE residual is `10 log10(mean(AFE²)/32768²)`. A finite numerical power floor
of 1e-20 avoids JSON infinities; it is not a detection threshold.

Near-only control reports raw-to-AFE attenuation over the known near source
extent, with a frozen +/-1 dB preservation bound. If no explicit near window
is supplied, the span of raw-MIC1 activity above its independent quiet floor
locates the source inside the board-silent acquisition. MIC3 must stay quiet.
Doubletalk ratios are labelled **raw mixture/output power ratio**, never
near-end attenuation. Report the AFE STT transcript separately.

Post-Opus residual is a separate result. `uplink_wav` plus `uplink_mapping`
provides independently established ranges of delayed board-sample coordinates
to recorder PCM offsets. Each range has `board_start`, `board_end`,
`uplink_start`, `gap_free: true`, and `evidence` identifying the independent
calibration/transport continuity witness. No mapping is fitted against residual
energy. Missing/gapped/ambiguous coverage is unscorable; whole-file uplink RMS
can still be reported as a diagnostic. WAV concatenation is not a continuous
sample clock. No zero filling across a missing interval is allowed.

## Recorder and time mapping

`ECHOEAR_AEC_SESSION_DIR=/evidence/run/sessions` enables the recorder; unset
means no recorder import, allocations or files in the ordinary demo. Every
session gets its own directory. Direct audio taps record pre-Flux decoded
uplink and TTS PCM, frame IDs, monotonic arrival timestamps and sample ranges.
A direct after-STT tap records Flux proposals without adding a queue. Aggregator
turn callbacks, interruptions and RTVI output are retained too. WAV files and
event files close on setup failure, pipeline error, cancellation and exit.

`gap_suspected` means a frame-arrival interval exceeded preceding frame duration
plus 40 ms; it is not proof of lost media. Unsupported formats generate explicit
`gap/drop` events. Every audio range is marked continuity-unverified until an
independent packet/sample witness verifies it. `write_duration_ns` exposes disk
stalls. Observer timestamps are receipt timestamps and must not be treated as
source event timestamps. `server-frame` timestamps define the synchronous tap
boundary used for the live BotStarted denominator; duplicated traversals are
deduplicated by frame ID. This is a demo recorder, not a complete async recorder.

The frozen `interval-bracket-v1` method uses three coordinates:

1. Server and runner use Linux `time.monotonic_ns` on the same host/boot. The
   recorder and run identity retain the boot ID. Remote-host recordings require
   a separate measured host-clock mapping and otherwise remain unscorable.
2. `aec-clock` returns an ESP `esp_timer_get_time()` stamp. The host brackets it
   between command send and receipt. Any intermediate device coordinate maps
   to the outer bounds of its surrounding transactions. There is no assumption
   of a fixed offset, drift rate or symmetric serial latency, and no extrapolation.
3. Each board capture read exports an `AEC_TIME` row: sample range, conservative
   device-time bounds, and a gap flag. The lower bound is the previous read's
   start minus `(DMA descriptors + 1) * DMA frames / 16000`; the upper bound is
   this read's return. The extra descriptor covers partly consumed driver data.
   Bounds cover every sample in the block. They are read/DMA bounds, not claimed
   exact ADC timestamps. First read, short/error reads and observed RX queue
   overflows mark gaps. Legacy captures without these rows remain useful for
   diagnostic energy analysis but not cross-clock timing.

Every mapped interval retains its measured width; results report maximum
uncertainty. A device callback uses its device timestamp through the same
transaction anchors. Onset/last-active MIC3 frames use their enclosing read
bounds. A latency is `[end.lower - start.upper, end.upper - start.lower]`.
Use the upper bound against a latency limit. Unresolved mappings return
`scorable: false`. Serial-log receipt is never used as a sample timestamp.
Step 2 changed clock polling from 50 ms to 200 ms after live trials found
uplink drops with the faster polling. The mapping formula is unchanged: wider
anchor spacing widens its bounds and may make a short latency gate unscorable.
Do not assume that the 75 ms DMA residence is the entire mapping uncertainty.
Timing rows are paced in groups of four; PCM export uses 8 ms chunks and a
150 s host timeout to cover the full 30 s, five-channel buffer. Dump after
teardown when measuring live resource margins.

PERF now exposes RX completions (240 frames each), consumed frames and queue
overflows. For two snapshots, completed-frame delta minus consumed-frame delta
can differ by the declared DMA residue allowance (1,200 frames) without
indicating a skip. Excess beyond that allowance is a skip diagnostic even
without an overflow callback, but asynchronous snapshot skew must also be
bounded before calling it proof. A smaller difference does not prove sample
continuity; a bounded number of skips can be hidden by residue changes. Use run deltas: RX starts before capture and startup overflows
are separately retained. Never substitute these counters for the row gap flags.

Live reports every server BotStarted in the denominator. No MIC3 activity in
its following 1 s is missing playback only when complete, gap-free timing
coverage proves that absence. Timing ambiguity is a retained failed/unscorable
attempt, not a silent success or a denominator exclusion. Acoustic absence and
missing data are different outcomes.

## Obligations carried into step 9

Keep internal heap minimum >=81,920 B, largest block >=32,768 B, zero failed
allocations; playback stack >=max(4,096 B,25%), capture/TX/peer-loop >=4,096 B,
app-control >=2,048 B, and step-2 peer-rx/CPU margins. Keep 20 ms uplink frames
and explicit zero firmware uplink drops, AFE errors, ADC/reference/output clips,
I2S errors/timeouts/underflows and capture overruns. Retain independent RTP
coverage, loss/PLC/drop counts, RF evidence, reconnects, and soak obligations.

Character qualification uses `DISPLAY_CHARACTER.enabled`, increasing character
`posts`/activity, and zero character/display errors. `character_health()` tests
that contract. The old `animation` flag and 24 fps benchmark floor are not
character-mode obligations. Step 9 freezes campaign thresholds and the bounded
transport policy; this runner never silently relaxes them.

## Step-2 resource diagnostics

`PERF.aec.errors` remains the wrapper's existing process/error counter. AFE
output saturation is `PERF.aec.output_clips`; MIC1 ADC and post-gain saturation
are separate `PERF.aec.capture.adc_clips` / `gain_clips`. Capture read errors,
timeouts, consumed frames and RX completions/overflows are exported there too.
AFE duration buckets have inclusive microsecond upper bounds, with an unbounded
last bucket. These are elapsed call times, including preemption, not CPU cycles.
`timing_row_us` / `timing_rows` measure row-store elapsed time including timer
boundary cost, excluding the bookkeeping that follows the second timestamp.

Reference-health diagnostics now require accepted post-volume speaker power
above 1,024 LSB squared at **both** conservative capture bounds. Gap-free MIC3
block power is compared to the smaller endpoint playback power, with a 1 ppm
power-ratio floor at 0 dB scaled by `10^(reference_gain_db/10)`. The integer
power ladder is rounded to Q1000; no floating point or allocation enters the
capture path. This replaces the absolute reference threshold, whose meaning
changed with gain. A window is one consecutive 1,600-sample (100 ms) crossing;
changing gain resets the current run, while cumulative counts remain visible.

These are conservative diagnostic settings, not proof of DAC activity,
acceptance thresholds, or a runtime fallback. The speaker timeline still uses
its midpoint estimate and 31 ms hold. Step 3 measures the probe loopback ratio
and per-gain quiet noise to contextualize the floor; step 9 freezes the pass
interpretation. Low signals, noise and timing uncertainty limit detection.

The step-2 resource floors are peer-rx >=1,024 B free (25% of its
4,096 B stack) and >=25% idle on each core in steady live windows of at least
5 seconds. Below 20% is an absolute CPU-floor failure; 20–25% still fails
the required steady margin. Every session snapshot must also have
`encode_p95_us < 20000` and `encode_max_us < 40000` (strict bounds).
Run `python hil/echoear_aec_resources.py HOLD_DIRECTORY` on the retained
PERF snapshots; missing required measurements fail closed. The existing playback/capture/TX/peer-loop/app-control and heap floors
remain unchanged. Retain short setup/teardown windows separately; never hide
read errors or uplink drops in those transitions. These measurements do not
replace step 8 revalidation or the exact-candidate step 9 campaign.

Playback's 320-sample PCM scratch buffer now lives in its pipeline heap object,
leaving the 17,152 B playback stack for the decoder call chain. Peer-loop uses
12,800 B in AEC builds; non-AEC builds retain 10,752 B. AEC builds reuse
transport workers across sessions and use a 26,624 B encoder stack; non-AEC
EchoEar builds retain 28,672 B and per-session workers. AEC builds default the Opus encoder to core 0; capture/AEC and playback
remain on core 1. Non-AEC builds keep encoder core 1. The resource trial on core
1 observed a real capture-to-encoder queue drop and a 52 ms encoder call; average
idle alone was insufficient to show this scheduling problem. Frame size remains
20 ms. This affinity change requires the later integrated transport/acoustic
revalidation just like the other step-2 resource changes.


## Step-3 reference gain trials

`aec-ref-gain <dB>` accepts exactly one integer on the 0..33 dB ladder in
3 dB steps, only while idle with uplink disabled. Invalid input returns `ERR`
without reboot or codec mutation. A successful change reads back ES7210 REG45
before publishing the selected gain. `aec-ref-gain-status` verifies and logs the
selected dB and register without changing it. Codec IO failures return `ERR`.
The board-lifetime selection survives audio reinitialization and reconnect;
a reboot returns to `CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB` (product default 0; campaign gain is explicit). Non-ladder Kconfig values fail compilation. Boot readback checks the
configured value. MIC1 gain, DAC volume and AFE configuration remain unchanged.

`hil/echoear_aec_gain_sweep.py --plan PLAN.json --out EVIDENCE_DIRECTORY`
runs a predeclared screen/finalist/control grid under one bench lock and restores
the normal image before release. Each trial hard-resets the board, selects gain
and volume, records settled quiet at that gain, then records the stimulus.
Warm trials explicitly play one whole conditioning fixture plus a one-second
tail before the measured reply. A speech fixture is four copies of the pinned
2.6399375 s greeting in one 10.55975 s normal transport/DSP playback. All failed
attempts remain in the result and denominator. Capture gaps make acoustic
metrics unscorable; they do not become silence. Post-Opus qualification remains
unscorable until the independent mapping producer in step 8.

The screen has gains 0/15/24/30, volumes 0/-6 and two speech repetitions, plus
probes at 0/30 and both volumes, in a seeded shuffled order. Finalists require
one or two gains, both volumes, cold/warm and three repetitions each. Selection
uses sustained and first-500ms reduction together, subject to clipping/headroom,
quiet noise, near-only preservation, double-talk STT and live Stop controls.
No delay optimization or listening judgment is used.

### Exploratory room-noise retries (coordinator-authorized)

A `noise_gate` declaration enables the frozen v1 gate. This is an exploratory
screening exception to the no-retakes policy, not an acceptance-campaign retry
rule. All attempts have unique `cell_id-a1/a2/a3` paths and retained captures,
noise measurements, outcomes and retry decisions. Only measured noise-gate
failures can be retried, at most twice per cell. STT, acoustic, transport and
incomplete-capture failures never grant retry permission.

Before cells start, record a six-second settled session baseline after five
seconds of settling. Require raw average at most −53 dBFS and every 20 ms RMS
frame at most −52 dBFS; retain up to three declared baseline attempts if a
transient prevents obtaining a baseline. If none passes, restore and pause.

For every cell, require each raw MIC1 quiet window's average within **±3 dB**
of that baseline and its maximum **20 ms RMS** at most **−52 dBFS**. A transient
is defined by frame RMS, not an individual PCM sample peak. Check the separate
two-second per-gain quiet capture after five seconds of settling, plus the
first and last two seconds of the stimulus capture. The stimulus follows at
least 2.5 seconds of capture pre-roll. The existing 14-second speech / 5-second
probe or near observation retains at least two seconds of post-roll. Both
capture quiet windows must also have no MIC3 activity against the independently
measured per-gain quiet threshold. Own playback in a quiet window is a failed
acquisition, not a room-noise retry. Gaps never become silence.

Noise retries go to the queue's end. Three consecutive **distinct cells** whose
attempts fail the noise gate pause the hold; a clean gate resets that streak,
even when the acoustic or STT result fails. Repeating one noisy cell cannot
count as three different cells. An exhausted cell remains unresolved. Every
pause still stops the owned bot, restores and boot-verifies the normal image,
restores the mixer, then releases the bench flock. Thresholds, durations, retry
limits and this scheduling interpretation are declared before acquisition.


The coordinator subsequently replaced the exploratory gate prospectively with
**v2**: each raw MIC1 quiet-window average must be **at most −55 dBFS** and
**at most baseline + 6 dB**; the maximum 20 ms RMS must be **at most −46 dBFS**.
This is a coordinator correction to an overly tight room-noise gate, not a
result-driven selection change. Normal morning quiet was −56 to −60 dBFS;
an unusually low baseline must not automatically reject that range. Quieter
windows are allowed. All other durations, baseline attempts, retry limits and
three-distinct-cell auto-pause remain unchanged. The baseline also passes the
v2 cell limits (so its effective average limit is −55 dBFS).

Prior v1 failures stay excluded and are never rescored under v2. A resumed
hold declares a hashed checkpoint and preserves its pending cell order and
absolute attempt numbers: attempt 2 has only attempt 3 left. The coordinator's
explicit resumption begins a new session/streak, not a new per-cell retry budget.
Retained earlier clean cells remain identified by their original protocol;
they are not represented as prospectively v2-gated captures.


### Step-3 exploratory gain choice

The completed clean screen and 15/24 dB finalist comparison select **15 dB for
further AEC experiments**. Product `CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB` remains
**0 dB**, as the user explicitly required, and AEC remains default off. Select
15 dB explicitly with the idle `aec-ref-gain 15` command or a declared campaign
build override. This is not demo qualification.

Both finalists had zero clips and 3/4 correct double-talk transcripts. Worst
integrated reduction was 22.30/22.74 dB and worst first-500-ms reduction was
17.93/16.16 dB at 15/24 dB respectively. Neither gain is measurably superior; 15 dB remains the exploratory choice and
24 dB is the fallback. Pin the AEC demo volume range to **{0, -6} dB**; the
-12 dB reference-level check is deferred to step 9's matrix. Results vary by
volume/adaptation state: 24 dB was stronger at volume 0, while 15 dB was stronger
at -6 dB. All transport losses and clean recognition failures remain included.
The 15 dB volume-0 near-only control exhausted its noise budget and remains
unresolved; its volume -6 control passed the +/-1 dB preservation bound.

The declared six-case 15 dB live sample retained one noisy control and one noisy
Stop as exclusions. Three clean Stops produced the expected transcript and
subsequent interruption events, but **no qualified barge-in success rate** is
claimed. These are recognition observations; at most one is a real barge-in
through echo. The original BotStarted diagnostic remains 1 playback / 2 missing /
3 timing-unscorable across all six cases; all six show playback against the
coordinator-approved non-silent-TTS anchor (all four quiet-eligible cases pass). Post-Opus residuals remain
unscorable without the independent mapping assigned to step 8. The normal image
was restored and boot-verified after every hold.

The acquisition runner captures 2.1 s for the separate quiet precheck and gates
exactly its first 2 s, avoiding an asynchronous-framing edge that yielded 1.98 s.
The two original short-capture failures remain retained; their fresh replacements
were explicitly authorized as acquisition-failure replacements, not noise retries.
Full declarations, source hashes, exclusions, per-repeat metrics, live timing,
resource checks and the selection record are retained under
`~/src/evidence/echoear/aec-demo/step-3/`; see `step-3-gain-selection.json` and
`step-3-final-bench-status.json`.


### Step-3 review contracts

Every AEC image must declare `boot_ref_gain_db` in its manifest or run plan.
`verify_bundle` rejects absent/invalid/conflicting declarations before flashing;
`boot` requires verification in the same process and checks both the boot
`analog_db` and MIC3 register against that declaration on every reset. The frozen
normal AEC-off image is exempt by its full ELF hash. Runtime sweep gains are
separate from the image's declared boot gain. Remove stale generated gain overrides
before building the product default; the clean AEC config and boot now verify 0 dB.

Steps 4 and 9 score initial playback in the existing one-second window from the
**first non-silent TTS chunk written to transport**. Keep the BotStarted anchor
and its original outcome as a server-to-DAC diagnostic. `analysis.json` reports
both per attempt; the legacy top-level counts remain explicitly labelled.
The independent PCM rule is a complete 20 ms, 16 kHz S16 mono window with power
>1,024 LSB squared (32 LSB RMS), the existing audibility diagnostic threshold.
It is not selected using AFE or MIC3 outcomes. No witnessed chunk means unscorable,
not silence. Retained recordings expose the synchronous transport-input tap;
that boundary is labelled rather than invented as an RTP send timestamp. Future
recordings add a direct tap after successful transport writes. Future Stop offsets
must start from its first non-silent event; never fall back to BotStarted.
The updated future live runner is retained under `step-3/review-fixes/` pending
promotion and tests before step 9 (review note 6).

Playback's effective floor is max(4,096, ceil(stack allocation / 4)): **4,288 B**
for the 17,152 B allocation. The measured 4,508 B minimum leaves **220 B**.
Reports include the minimum per-snapshot margin. Acquisition failures leave the
noise streak unchanged and grant no retry permission. Review note 7's relative
reference-health acceptance statement remains due before step 9.


### Step-4 retained-evidence diagnosis

The host-only investigation found the same silent-TTS symptom in Sol's retained
`cat-nominal/nominal-stop-1`: TTFA leading silence 2.371 s, PCM below the fixed
non-silent rule until 2.16 s of stored audio, and server interruption at 1.911 s
after BotStarted. MIC3 stayed at its quiet floor. The board received/decoded
330 packets over the bracketing window without loss, PLC, starvation or I2S
errors. Silent TTS queued ahead of speech and interrupted before audible onset
is the supported explanation; the missing legacy send/sample-time witnesses
prevent proving the entire queue-to-DAC path. Retain the failed attempt.

In the six step-3 replies, Flux TTS (Pipecat 1.12.0, `flux-kelsey-en`) reports
0.105 s leading silence first, then **1.367 / 1.173 / 1.184 / 1.611 / 1.297 s**.
Stored PCM independently contains near-silent prefixes around -69 dBFS. These
are demo-latency findings on the bot/TTS side; step 4 changes no bot policy or TTS
settings. TTFA is TTFB plus detected PCM silence duration, not a direct wall-clock
DAC measurement. Chunk arrival timestamps and stored PCM offsets stay separate.

The retained finalist losses (34 at 15 dB, 36 at 24 dB) correspond to late
admission and PLC/missing slots. They are not proof of RF packet loss. No matched
RTP send/receive or independent RF trace exists for those cells; sender pacing,
network delay and receive scheduling remain unresolved. No repair is claimed.
The coordinator's host-only restriction deferred the planned fresh replication.
Step 9 must report missing-playback rate across all 20+ replies using the
non-silent-TTS anchor, retain the BotStarted diagnostic and all failed attempts,
and collect `rtp_evidence.py`, recorder, counters and independent RF evidence.
The legacy Sol case stays timing-unscorable under the new anchor; it is not
retroactively counted as a successful reply.

Reproduce the retained diagnosis without hardware:

```sh
uv run --project server --with pyserial python hil/echoear_aec_latency.py \
  --sol ~/src/evidence/echoear/aec-assess/sol/cat-nominal \
  --live ~/src/evidence/echoear/aec-demo/step-3/live-stop-selected-15-q3 \
  --out ~/src/evidence/echoear/aec-demo/step-4/retained-diagnosis.json
```

This is diagnosis, not acceptance. The output hashes its input evidence, keeps
noise exclusions visible, and separates active capture from export counter windows.


## Step-4 review: localized data-channel admission loss

Fable identified a separate device-side finding: nominal real-bot traffic was
shed by `MessageWorkBudget`. Across the six step-3 cases, cumulative
`admission_budget` was 0 → 4 → 7 → 13 → 18 → 21 → 28:

| Case | Admission-budget drops in case | Cumulative drops |
|---|---:|---:|
| v0-control | 4 | 4 |
| v-6-control | 3 | 7 |
| v0-stop-1 | 6 | 13 |
| v0-stop-2 | 5 | 18 |
| v-6-stop-1 | 3 | 21 |
| v-6-stop-2 | 7 | 28 |

That is **28 of 82 received RTVI messages (34%)**. Sol's missing-playback case
shed 2 of 15; offline-fixture finalist sessions shed 0 of 115–142. The real bot
emits 7–8 reply-start messages within about 80 ms (1.8–2.2 KiB: metrics,
bot-tts-started, bot-started-speaking, two 675 B bot-output messages,
bot-tts-text and metrics), plus interruption bursts of 3+4 messages. The budget
resets once per owner-loop iteration and rejects the fifth message; a native
receive pass can deliver the whole burst in that iteration. The 4 KiB byte cap
and 128 KiB/s bucket are not the binding limits for these bursts.

Admission is order-dependent. bot-interrupted and bot-stopped-speaking happened
to survive, while bot-output, bot-tts-text, user-transcription and
user-stopped-speaking did not; other ordering puts bot-ready/bot-interrupted at
risk. This is localized control-message loss, separate from audio late/PLC
attribution. The coordinator added **step 4b**: retain size/type in the drop
detail, raise the bounded message cap, preserve byte/rate limits, test bursts,
and require zero admission-budget delta plus received == dispatched in live
checks. The fresh tracing deferral is accepted: step 5 collects matched traces,
step 8 attributes transport lateness. Variable TTS lead-in (not a fixed cost
after the first reply) is now planned as bot-side A/B **step 8b**.

### Step-4b admission and observer checks

`MessageWorkBudget` now accepts at most 16 messages per owner-loop iteration,
while retaining 4 KiB per-iteration bytes, the 128 KiB/s bucket and the existing
bounded small-message reserve. AdmissionBudget trace details encode native
frame type in the high byte and byte size in the low 24 bits, with stream ID.
The shape bound is 80 conservative nodes: the retained nominal combined
TTFB/processing metrics envelope needs 66 and was rejected at 64. The wire-size
limit and per-message heap-reservation guard are unchanged. This additional
shape correction prevents the old startup drop from hiding behind the count fix.

Live resource checks now require zero admission-budget delta per connection
and received == dispatched at a quiescent endpoint. Missing/reset counters fail,
and echo/barge integrity requires the admission check too. Intermediate pending
dispatch is not a successful endpoint. Steps 5, 8 and 9 inherit these checks.

Real-bot evaluation explicitly activates `rtp_evidence` using its configured
path or an evaluation-local `rtp/` directory, even after an earlier disabled
import. Ordinary bots remain opt-in. A host test uses actual local aiortc peers,
with no STUN service, and proves authenticated receive, UDP-callback and send
records; observer health must also pass during the live hold.

The future live runner with explicit `stop_not_played: no non-silent write
witness` outcomes is retained at `step-4b/live_stop_hold_next.py`, superseding
the step-3 evidence copy. No Stop is injected without a witnessed non-silent
transport write. Promotion of that runner remains due before step 9.

The step-4b three-case real-bot hold passed at cumulative **12/12, 22/22 and
37/37 received/dispatched**, with all admission-budget, shape, heap and queue
rejection counters zero. Observer health passed in all cases; the final archive
contains 11,638 authenticated/UDP observations and 11,759 downlink sends.
The Stop followed non-silent transport output and was recognized; this was a
control-message check, without acoustic scoring or noise gating. Quiet raw MIC1
was -61.52 dBFS. All resource floors passed: internal minimum 84,380 B, largest
47,104 B, steady idle minima 48.77%/35.17%, encoder p95/max 6,899/9,663 us,
playback free stack 4,508 B (220 B margin). Normal firmware/hash and mixer were
restored and the bench lock released.

Five late/PLC events remain (one in an active reply window); this change does
not fix transport lateness. The optional 8,192-record device trace overwrote
43,227 records during long PCM exports, while server observers stayed complete.
WiFi observation was unavailable in this image. Step 5 must arrange complete
bounded device-trace windows and WiFi observation alongside its replies; step 8
still owns attribution. The 2,460 B internal-heap and 220 B playback-stack margins
require rechecks after subsequent code changes.

The heap low-water trajectory was 102,028 B before connect → 87,856 B at
Ready → 84,380 B two seconds later. The ~3.5 KB transient is attributable
to the admitted startup metrics envelope (405 B / 66 conservative nodes),
previously rejected at the 64-node shape limit. DOM parsing, message/callback
copies and logging consume temporary internal heap; later reply bursts did not
deepen the trough. The unchanged per-message reserve (~20.6 KB for this
envelope) protects the 81,920 B floor, and any admission-heap rejection fails
the received/dispatched live check. The original coordinator decision kept
device-bound metrics and the 80-node cap until a heap failure or step 8b.
The step-5 review supersedes that timing: **step 5b suppresses device-bound
metrics before step 6**, because heap margins fell from 4,176 to 2,460 to
1,920 B and capability JSON will allocate in the same connect window.
The 80-node cap and all resource thresholds remain unchanged.

### Step-5 reply-start guard

AEC Auto mode now reserves closure before an eligible post-volume speaker
write and confirms only accepted samples whose magnitude exceeds 32 PCM16
units. Zero volume, an unaccepted non-silent suffix, and zero-byte writes do
not start an onset. The upper DAC-onset estimate includes submitted minus
DMA-completed samples and the accepted prefix offset. A 2,000 ms gap between
accepted non-silent output intervals now rearms the guard; remote speaking events
neither trigger it nor restart it. Explicit gate policies retain precedence.

`CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS` / `aec-guard-rearm-ms 500..10000`
sets that gap (default 2,000 ms). Runtime changes require capture disabled,
survive reconnect and reset to Kconfig at reboot. Invalid, overflowing and
multi-argument input is rejected. The minimum retains the former 500 ms
setting and stays above the continuous-write queue/block/100 ms timeout
reservation lead. The upper bound limits an exploratory setting to 10 s.
The original step-5 hold below used 500 ms; its measured closure counts and
muted fractions are historical, not validation of the new default. Step 6
must witness re-arm count per reply, admission and heap trough on the combined
image. Freeze the selected re-arm value explicitly in the step-8 declaration.

`CONFIG_DG_ECHOEAR_AEC_GUARD_MS` / `aec-guard-ms 0..1000` selects the source
interval, initially 400 ms; console changes require uplink disabled. The
step-3 clean speech data commonly has its strongest startup residual in
200..400 ms, with some later outliers. This is an exploratory guard, not a
claim that all echo has converged by 400 ms. Expiry compares the deadline to
the conservative input-time lower bound of the actual delayed AFE output:
1,024 samples (64 ms) earlier in the input stream. The nominal 464..484 ms
interval is further extended by the existing conservative DMA/read bounds
(typically about 95 ms), and by any capture backlog. Unknown source timing
keeps the gate closed. Continuous AFE feeding and 20 ms uplink silence remain;
AEC mode does not inherit the half-duplex audible-tail mute.

`hil/plans/echoear-aec-guard-v1.json` declares 20 new varied echo-only replies
and two in-guard Stops, frozen v2 noise screening, bounded noise-only retries,
and the three-noisy-cell auto-pause. It requires WiFi observation enabled and
`available: true` before the first reply, all RTP observers and successful
non-silent transport-write anchors, per-case admission checks, and arrival
ring drains immediately after each reply window before exports (also at a
bounded intermediate point). That hold retained metrics forwarding and the
80-node cap; step 5b changes forwarding as described below.
The opt-in evaluation controller can skip the next declared unused/noisy
attempt without emitting audio; it cannot replay that identity or inject
speech/transcripts. Ordinary bot interaction policy is unchanged.

The step-5 frozen hold (`3ae2a64`, ELF `7801ce32…`) completed **20 clean new
replies, cold first included, with zero proposed or accepted false turns**.
Two noise-contaminated originals were retained; both passed at attempt 2 under
unchanged v2 thresholds. All 22 clean reply/Stop cases have witnessed initial
playback against the non-silent successful-write anchor. Admission ended at
232 received / 232 dispatched, all rejection counters zero. WiFi was available
before first playback; RTP observers remained complete. Every reply-window
arrival-ring overwrite delta was zero. The ring still overwrote silent export
periods, which are explicitly outside those bounded trace windows.

Measured guard closures were **602–657 ms** for the 400 ms source setting,
including conservative DMA/read bounds and capture quantization. All 90 retained
gate transitions match the source block exactly 1,024 samples earlier in the
capture. Fable's independent review found two closures in each of the 18
two-sentence replies (of 20 clean echo replies), with onsets 2.1–3.1 s apart.
Uplink muting overlapped an average 1,163 ms of 4,821 ms active playback:
about **24%**, ranging **21.8–29.8%** per reply. The review's 22 later-onset
closures had residual bins at or below −51.9 dBFS, with no measured benefit
from suppressing them. This motivates the coordinator's step-5b change from
500 ms to 2,000 ms re-arm silence; the 400 ms source guard stays unchanged.
The capture lower bound plus AFE delay also zeroes about **160 ms of user
audio captured before playback onset** when the guard arms. This is intended
conservatism, and a speech-loss limitation, not measured echo adaptation time.

Both clean early Stops were missed (no transcript or interruption),
at actual successful-write offsets 352 and 474 ms. The nominal requested
100/300 ms offsets were delayed by polling/scheduling. Respectively 116–640 ms
and 0–476 ms of source capture within the Jabra invocation were forcibly gated.
Those interval bounds include fixture silence and ALSA startup/drain; exact
voiced loss/physical near-end onset remains unscorable, not a claim that the
whole word was muted or that gating alone caused both misses. In-guard speech
loss is a material limitation; these attempts stay in the denominator.

Resources passed: internal minimum 83,840 B (1,920 B margin), largest 45,056 B,
playback stack 4,504 B (216 B above the effective 4,288 B floor), TX stack
4,716 B, core idle minima 48.70%/33.58%, encoder p95/max 6,799/9,420 us.
Cat/display and 20 ms uplink checks passed, with zero allocation, clipping,
I2S, uplink-drop and console-ring/transport-drop failures. Transport remains
open: five clean echo replies had loss/lateness (12 lost/PLC, 13 late/dropped
in active windows; 21 each over the connected hold). This is not step-9
acceptance or a transport repair. Normal `9936da45…` was restored and fully
boot-hash verified; mixer byte-equal, owned bot stopped, lock released.

The unchanged AFE metrics on those 20 clean replies remain separate from gate
success: integrated reduction 28.43–42.33 dB, median-frame reduction
21.71–37.09 dB, active AFE residual −64.85..−52.76 dBFS. First-500 ms
reduction spans **1.30–40.71 dB**, with residual −64.12..−43.82 dBFS. The
1.30 dB case (`echo-03-a1`) contains just one active reference frame in that
window, at raw −56.47 / AFE −57.77 dBFS; it stays in the results under the
fixed activity definition. No threshold was adjusted. Post-Opus residual
remains unscorable without an independent board/uplink sample mapping, due
for integrated revalidation. Zero false turns is not a pass of all proposed
step-9 acoustic gates.

### Step-5b device metrics forwarding

`demo_bot.py` now passes `RTVIObserverParams(metrics_enabled=False)` to
`PipelineWorker`. This suppresses both startup and later RTVI `metrics`
envelopes. Server metrics and usage collection stay enabled; other RTVI
messages, including bot-started-speaking, bot-stopped-speaking and
bot-interrupted, retain their defaults. No TTS, turn or interruption policy
changes accompany this. The Pipecat context hub identified the observer
switch; the webinar reference bot disables only initial empty metrics,
which would not suppress populated envelopes. The forwarding behavior is
host-tested against the installed Pipecat 1.12.0.
Browser and other RTVI clients of `demo_bot.py` also lose their metrics panel.

Step 5b is host-only. No heap recovery or reduction in closures is yet
claimed. Step 6's live check must measure the connect/Ready/post-Ready heap
trough, verify received == dispatched with zero admission-budget drops and
all resource floors, and count guard onsets/re-arms per reply at 2,000 ms.
The capture-clock change, 300/400 ms guard comparison, timer-scheduled Stops
and `wlp8s0` capture are deferred to step 8. That capture may not use sudo or
change network settings; if privileges are needed, report and stop that item.

### Step-6 capability selection

Firmware sends `/start` an envelope with `transport: "webrtc"` and
`body.audio: {v: 1, aec: <boolean>, ref_gain_db: <integer>, guard_ms: <integer>}`.
The AEC flag comes from the initialized engine; gain and guard describe current
runtime settings. The server receives the nested body through the session offer.
With `ECHOEAR_AEC_FULL_DUPLEX` unset, a valid v1 AEC capability selects full
duplex. Missing, legacy, unknown-version or malformed capabilities select half
duplex. `0` forces half duplex; `1` forces full duplex and logs an experimental
override. Other override values are rejected. Old servers keep their existing
policy when they ignore the new body. This does not promote AEC to default-on.

`aec-guard-ms` now uses the same strict whole-argument parser as re-arm, with
its existing 0..1000 ms bounds. Boot logs guard and re-arm values; each new
onset logs the previous accepted non-silent end estimate for reply-gap analysis.

The step-6 hold (source `6f852fc`, ELF `feb43cfe…`, six two-sentence
replies at volumes 0/-6, override unset) selected full duplex from the firmware
capability. All six cells passed the frozen v2 noise gate at attempt 1; no
proposed or accepted false turns. All six initial-playback checks passed the
non-silent successful-write anchor. Baseline raw MIC1 was −57.02 dBFS.

Boot logged guard 400 ms / re-arm 2,000 ms and gain 0 dB; the manifest matches.
Runtime gain 15 dB appeared in the capability. There was exactly one onset and
one closure per reply: 602.9–651.8 ms. Muting intersected an average 600 ms of
4,660 ms MIC3-active playback (mean per-reply fraction 12.94%, range
11.40–13.90%). No possible onset lead above 100 ms was found. The accepted
DAC-onset upper estimate versus the first MIC3-active frame's retained bounds
spanned −129.7..+5.5 ms across cases; these intervals do not establish the
review's expected exact 0–50 ms pre-onset lead. No timing bound was tightened.

With the declared >=500 ms MIC3-inactive gap definition, two unguarded
re-onsets were found. Their first-400 ms AFE residuals were −38.15 and
−58.97 dBFS; maximum 20 ms frames were −26.52 and −54.72 dBFS. The louder
case had 19.27 dB integrated whole-reply reduction. These results remain in
the denominator; zero false turns does not establish uniformly quiet re-onsets
or step-9 acceptance. Reply gaps from previous accepted non-silent end to new
onset were 84.20–85.62 s because captures were exported between cells. This
sample does not cover spontaneous follow-ups below 2 s.

Heap low-water was 86,612 B in the Ready log, 86,564 B at the first PERF and
+2/+7 s, and 86,160 B at end (4,240 B above the floor, 2,320 B above step 5).
Largest internal block was 47,104 B. Required stack/CPU/encoder/display and
20 ms uplink checks passed. Playback stack remains only 216 B above its
4,288 B floor. The sixth reply had 4 lost/PLC and 4 late/dropped packets;
these are retained, with no RF exclusion or repair claim.

**Control delivery remains unresolved.** The recorder observed zero metrics
and 43 outgoing RTVI frames. Each reply's start phase contained five frames
spanning 77–142 ms (four of six fit in the declared first 100 ms window;
two had only the first frame in that window). But device admission advanced
only from 1/1 to 6/6 during the first reply and stayed at 6/6 for the other
five, with every rejection counter zero. Equality is a narrow admission
check, not proof of continued end-to-end control delivery. The recorder is
not an SCTP send/ACK tap, so the retained evidence does not localize the
stall or observability gap. Heap results after that point do not qualify the
missing control workload. This must be resolved before relying on step-7
interrupt delivery. The original check outputs remain intact, accompanied by
`step-6/live-2/delivery-audit.json`; the overall result is qualified by this
finding, despite their narrow pass booleans.

Normal `9936da45…` was restored and full boot-hash verified; mixer byte-equal,
owned bot stopped, flock independently reacquired/released. No step-7/8 work
or parameter tuning was performed.

### Step-6 review: exclusions and the step-6b hold

Step 7 is blocked on step 6b: diagnose the silent bot-to-device control stall,
then review a fix/workaround and liveness mechanism before interruption relies
on this channel. The retained step-6 data exclude the HTTP capability path,
device admission/worker backlog, application stalls, Pipecat's not-ready queue,
and association death (device-to-bot delivery continued at +552 s). RTP loss
is not a direct explanation: audio flowed in the missing-control windows.
These exclusions do not establish successful SCTP delivery.

Open hypotheses are device SCTP acknowledging DATA without delivering it to the
callback (1a), device SCTP failing to acknowledge DATA (1b), or an aiortc send
stall (2). No SCTP DATA/SACK tap existed in that hold. Metrics suppression is
only an A/B timing lever, with no evidence of causation; the capability body is
HTTP-only. Three COOKIE-ECHOs were benign duplicate handshakes in aiortc, but
future holds record the count. Equality of received/dispatched alone is
insufficient: per-reply device increments must cover the recorder's outgoing
RTVI count in the same window. Post-reply-1 heap qualification is limited by the
missing control workload.

Fable's frame-level recount found eight unguarded re-onsets after at least
500 ms of reference inactivity (six sentence pauses and two later onsets).
The cap-04 late onset had a 60 ms AFE leak about 300 ms later: three 20 ms
frames at approximately -35, -26 and -32 dBFS, returning to -54 to -58 dBFS
by 6.44 s. There was no capture gap, RX overflow, transport event, or abnormal
reference level in that window. This was not sustained slow convergence and
did not trigger Flux. Keep the 2 s re-arm; step 8 must measure maximum 20 ms
residual after every unguarded re-onset over 20+ replies. Investigate possible
brief AFE re-adaptation in step 8b/9 without claiming that mechanism established.

Step 6b adds an opt-in aiortc 1.15.0 SCTP chunk/T3 tap to the existing RTP
observer, including stream sequence, fragmentation flags, DATA hashes, SACK
gaps and send-side queue/buffer measurements. It is installed before evaluation
negotiation; early rows are buffered on their association until pc_id binding.
The bounded tap reports truncation/I/O failure without blocking delivery.
`live_admission` now requires an explicit recorder count and checks both device
deltas against it. Resource-only snapshots report `data_admission_counters_only`;
that check cannot qualify end-to-end delivery. The retained step-6 reply-2
regression is seven outputs versus zero received/dispatched increments.
The declared hold/read-out is in `hil/plans/echoear-aec-sctp-diagnosis-v1.json`.

### Step-6b diagnosis: aiortc flight accounting after a gap ACK

The short `step-6b/live-1` hold reused the exact step-6 image (`feb43cfe…`),
cat on, capability-selected full duplex, metrics forwarding off. It stopped
at reply 1: seven outgoing RTVI messages, five device receive/dispatch
increments, no admission rejection. The SCTP trace is complete and untruncated.
Two COOKIE-ECHOs were observed on both sides. No second metrics-on hold was
needed because the first reproduced the stall. No acoustic scoring was done.

The first start DATA TSN 556888654 initially remained unacknowledged; SACK
cumulative 556888653 with gap block [2,5] acknowledged the other four start
messages (1,186 bytes). T3 expired once, retransmitting all five chunks. The
next cumulative SACK acknowledged through 556888658, and all five messages
reached the device. However, aiortc retained flight_size=1,186 with cwnd=1,147
and an empty sent queue. Neither later Stop message generated DATA. The
channel stayed open; the first Stop occupied the SCTP outbound queue and the
second remained buffered by the data channel. This is host send-side failure,
not acknowledged-but-undelivered Stop messages or missing Stop SACKs.

The installed aiortc 1.15.0 source and two deterministic host reproductions
explain the accounting: T3 marks every sent-queue chunk for retransmission,
including gap-ACKed chunks, then resets flight. Retransmission counts their
bytes again without clearing `_acked`; the cumulative ACK pops those chunks
without subtracting their bytes. Empty sent_queue cancels T3, while phantom
flight above cwnd blocks later sends. A reproduction through the actual
RTCDataChannel ends with flight 1,186, cwnd 1,147, no sent chunks or T3 timer,
one outbound Stop and the other 52-byte Stop buffered. The original lost or
reordered first DATA's cause is not established. This explains the reproduced
hold; the earlier step-6 hold lacked SCTP evidence, so identical causation
there remains an inference.

The review's hypothesis 2 is selected with a more precise signature: phantom
flight **above** cwnd, not the originally suggested below-cwnd queue stall.
The two device-SCTP hypotheses do not explain the unsent Stop messages in
this hold. Restoring device-bound metrics is not an established remedy.

Proposed next reviewed change: correct T3/retransmit ACK/flight bookkeeping in
a pinned aiortc patch or verified upstream version, with gap-ACK + timeout +
cumulative-ACK tests, fragmentation/reordering/reneging cases, and a live
repeat proving later control delivery. Do not merely zero flight opportunistically.
Separately propose a periodic numbered server-message/device acknowledgement
with an independent host deadline and an alarm/connection recovery policy.
A recovery request sent only over the stalled channel cannot be relied on.
Neither fix nor liveness probe is implemented; step 7 remains blocked.

Resource snapshots passed (minimum internal heap 86,148 B, largest 47,104 B,
playback stack free 4,520 B against 4,288 B, idle minima 49.23/44.27%, encoder
p95/max 6,699/8,791 us), but the stalled control workload limits qualification.
Raw MIC1 pre/post RMS was -58.64/-58.65 dBFS; whole capture -29.66 dBFS.
The normal image was restored and full-hash verified, mixer restored byte-equal,
owned bot stopped and bench lock released. Retained evidence and reproducible
analysis are under `~/src/evidence/echoear/aec-demo/step-6b/`.

### Step-6b review: upstream status and repair basis

Read-only check on 2026-10-16: [PyPI](https://pypi.org/project/aiortc/) still
lists 1.15.0 as latest. Upstream [main at 34b5fd34](https://github.com/aiortc/aiortc/blob/34b5fd3409220e1aeb8b62f2c56a9ae2085ca8c8/src/aiortc/rtcsctptransport.py)
retains the faulty T3 bookkeeping. [Issue #1342](https://github.com/aiortc/aiortc/issues/1342)
reported the same phantom-flight symptom and was closed by its reporter as an
application race, without a library fix. [Issue #1438](https://github.com/aiortc/aiortc/issues/1438)
and [PR #1443](https://github.com/aiortc/aiortc/pull/1443) concern abandoned
maxRetransmits chunks, a separate path that does not repair this reliable-channel
T3/gap-ACK bug. Pipecat 1.12.0 allows aiortc >=1.14.0,<2, so a pinned 1.15.0
local patch fits its dependency constraint. Nothing has been filed upstream.

[RFC 4960 §6.2.1 rule D](https://www.rfc-editor.org/rfc/rfc4960#section-6.2.1)
accounts for flight on transmission and acknowledgement; §6.3.3 covers T3
retransmission, including previously gap-ACKed data a receiver can renege on.
A chunk retransmitted after T3 must become outstanding again. The approved
minimal fix clears `_acked` for non-abandoned sent chunks before the original
T3 handler; it neither suppresses retransmission nor opportunistically zeros
flight when queues empty.

Exposure is per burst: a lost/late earlier DATA followed by gap-ACKed later
chunks before T3 can trigger the defect. Five-to-eight-chunk reply bursts offer
repeated opportunities; metrics suppression reduces exposure but does not fix
it. A phantom smaller than cwnd can survive without an immediate stall. Further
such events can accumulate phantom bytes until sending stops.

Step 6c freezes the version/file-hash guarded timer wrapper and guarded tap,
including a send-failed row. Tests cover the original sequence, two gaps/two
T3 cycles, a repeated gap ACK after T3, real local peers with one first-DATA
drop, and the same six-reply live hold checking device delivery and flight
recovery after every timer expiry. Step 6d separately implements the reviewed
liveness policy; step 7 remains blocked on both.

### Step-6c pinned repair and verification

`demo_bot.py` unconditionally installs `sctp_flight_fix.py` at import/startup.
On a Pipecat upgrade, **first re-verify `server/tts_connection_probe.py`** beside
this aiortc patch: its private Flux reconnect cycle requires exact Pipecat 1.12.0,
SHA-256 matches for `flux/tts_base.py` and `flux/tts.py`, and all three callable
private methods. Unsupported source/version/interface logs once and disables
reconnect without preventing pipeline startup. Active generations and shutdown
also refuse reconnect; the metric-start path shares the connection lock.

It accepts only aiortc 1.15.0 with `rtcsctptransport.py` SHA256
`d0d42fd9b568d44e687b564ca17bb384ea06c481bf8f38091ccb09d20be716c6`.
A mismatch logs ERROR and leaves the timer unpatched. The wrapper clears
`_acked` only on non-abandoned sent chunks, then calls the original T3 handler.
The installed package is unchanged. Installation is idempotent and preserves
the opt-in observer in either import order. The observer shares the startup
guard; every evaluation enables it before negotiation, including evaluations
that use the default evidence directory.

The tap adds `send-failed` with chunk identity and exception type (no exception
text), then re-raises the original exception. Post-handler `receive-sack-complete`
rows expose actual flight recovery; a timer's own flight reset is not counted
as proof. `hil/sctp_recovery.py` requires a later same-connection post-SACK row
with flight, sent queue and outbound queue all zero for every T3 row. Zero
natural T3 events is reported explicitly, without claiming live loss-recovery
coverage. The declared six-reply hold is
`hil/plans/echoear-aec-sctp-fix-v1.json`; no acoustic gate or score applies.

The frozen `step-6c/live-1` hold passed all six delivery checks: 7/7 messages
per reply, final device received/dispatched 43/43 including bot-ready, admission
rejections zero. **Twelve natural T3 expiries** occurred, two per reply. Six
followed gap [2,5] ACKs of the reply-start burst, exercising the original bug;
six were later single-chunk recoveries. Every expiry had a subsequent quiescent
post-SACK flight of zero, 6.69–190.71 ms after expiry. This excludes the timer's
1 s wait and is not end-to-end interruption latency. Send-failed rows: zero;
SCTP/RTP observer health complete, two COOKIE-ECHOs on both sides, no metrics
frames. The installed aiortc source hash remained unchanged.

Minimum internal heap was 85,456 B (3,536 B margin), largest block 47,104 B;
playback free 4,504 B against 4,288 B (216 B margin). Idle minima 48.89/43.67%;
encoder p95/max 6,799/9,526 us. All frozen resource checks passed. One temporary
SDK backpressure count in reply 1 retained queued data and all messages arrived;
this was not a zero-backpressure session. No acoustic score was made.

Audio transport remains outside this repair's qualification: reply 1 had four
lost/concealed packets; reply 3 had 15 lost/late/concealed/playout-dropped packets.
Session totals also include export/idle windows: lost 154, late 40, concealed 59,
playout-dropped 44, and 152 stale uplink frames skipped at send cutoff (all cutoff
increments outside the reply windows). No RF exclusion or retry was applied.
These are retained limitations, not evidence that this patch repairs transport
loss or meets step-9 acceptance. Raw MIC1 pre/post levels ranged -59.79 to
-57.81 dBFS, with no capture gaps. Normal `9936da45…` was restored and full-hash
verified; mixer byte-equal, owned bot stopped, bench lock released. Step 6d has
not started; step 7 remains blocked.

### Step-6c review: systematic first-DTLS-record loss

Fable's retained-trace audit found 14/14 lone datagrams after approximately
three seconds of bot-to-device DTLS silence lost on first transmission, versus
0/30 sent within 0.9 s. This is a measured rule for these holds, not a general
network guarantee. The 47-byte and 52-byte SCTP payloads both produce a 141-byte
DTLS record under AES256-CBC-SHA padding, so size alone does not explain it.
The socket-write-to-device-SCTP interval remains unlocalized among esp_peer's
DTLS/SCTP path, WiFi RX and air/AP.

The loss predates metrics suppression. Steps 4b/5 had three separately arriving
later groups giving new highest-TSN gap ACKs, allowing fast retransmission and
roughly 250–970 ms device delivery. Removing metrics left only one new gap-ACK
group: recovery required the 1 s T3 minimum, producing roughly 850–1,050 ms
first bot-output delivery in 6c. Step 6c fixes phantom flight, not that loss.
A lone bot-interrupted after similar idle would pay about one second without
a successful mitigation. Step 6d tests 1 s pings versus ping-off/socket tracing;
RTO changes and duplicate interruption sends remain coordinator decisions.

`DATA_DISPATCH` now logs explicit `device_us`, generation, ordinal, type and
wire bytes immediately before each SDK dispatch. Use this device clock and
measured host/device anchors. The retained step-6c serial log prefix was about
1,339 ms ahead of esp_timer; do not use it as device_us or assume that offset
is universal across boots. This fixes the ambiguity in per-message timestamps.

### Step 6d: control-channel liveness and socket witness

The demo bot defaults `ECHOEAR_LIVENESS_SECONDS` to `0.5`; `0` disables the
probe for a declared diagnostic arm, and evaluation accepts finite cadences
from 0.1 to 10 s. RTVI sends `server-message` data `{"t":"ping","seq":n}`.
The C++ SDK's canonical reply is `client-message` data
`{"t":"pong","d":{"seq":n}}`. The device control task admits a bounded
pong mailbox, and its session worker sends only for the current generation.
PERF exposes `liveness.pings`, `pongs`, and `timeouts`. A connected Ready
session without a ping for 15 s logs once, using explicit `device_us`.

The bot's monotonic watchdog runs separately from the sender. No timely pong for `max(2.0, 2 * period)` seconds logs ERROR, emit `liveness-alarm` with `failed=true`, and
increment the alarm/recovery counters before closing the public peer once.
A blocked send therefore cannot block deadline checks. Recovery closes the
peer directly; it does not send signalling or renegotiate over the stalled
channel. Late pongs do not reset the silence deadline. Evaluation retains start,
ping, pong, miss, alarm, and end-summary events; a campaign with an alarm fails.
The host test deliberately blocks the sender and checks one close after the silence deadline. This failure is not provoked on the bench.

`CONFIG_DG_PEER_DTLS_RX_TRACE` defaults off. The step-6d diagnostic image
alone enables it. Each successful `recvfrom` whose first byte is `0x17`
increments `peer_profile.dtls_app_datagrams`. With runtime `arrival-trace=1`,
it adds a kind-19 record to the existing bounded ring: device time, datagram
size, cumulative counter, socket, and the public DTLS epoch/48-bit record
sequence. Binary v1 fields are reused: `seq=epoch`, `timestamp=sequence low32`,
`ssrc=sequence high16`, `value=counter`, `aux=socket`. Flag 1 means a header
shorter than 13 bytes, without a usable identity. No payload or keys are saved.

The opt-in host tap writes `dtls-send.jsonl` after successful aioice UDP handoff,
with the same public record identity. It is a userspace handoff witness, not
proof of transmission over the air. SCTP DATA is associated with its immediately
preceding same-peer DTLS write; the analysis retains the time difference and
requires less than 10 ms. Device presence is then matched by exact epoch and
record sequence. Absence is usable only for reply windows whose device counter
range is fully retained. The ring can overwrite during long PCM exports and
idle gaps; those intervals cannot prove absence. Step 6d drains every four
seconds within the reply windows and retains count-based completeness checks.
SCTP retransmission has a new DTLS record sequence, so it is separately visible.

The first step-6d hold completed arm A: three replies, 23 recorder messages per
reply and 24 received/dispatched per window (the final 400 ms drain can include
another ping), no admission drops, 261 matched pings/pongs before teardown,
zero active T3 expiries or retransmitted pings. This supports the one-second
keepalive for this hold. It does not locate the original idle-path loss.

The runner incorrectly applied its final recovery check after deliberate
teardown. Two subsequent pings were unacknowledged and one T3 fired 1.46 s after
the retained `AUDIO enabled=0` receipt. It therefore restored the normal image
and stopped before arm B. `live-1/result.json` retains that failure unchanged;
`analysis-including-teardown.json` and `analysis.json` show the full trace and
explicit active/teardown split. `check_active_recovery` and the frozen v2 runner
now put the boundary before teardown; an unresolved active timer still fails.
Arm B has no evidence yet and requires a coordinator exception to the one-hold
limit. Do not infer a socket-path result or advance to step 7 from arm A alone.

### Step 6d review: device-side loss and demo mitigation

The demo now defaults to **0.5 s** liveness, leaving margin between the observed
1.001 s maximum successful arm-A send gap and arm B's 1.104 s first-send loss.
`DATA_DISPATCH` skips ping messages; PERF and bot liveness events retain their
counts. `check_reply_resources` checks the PERF liveness triple per reply:
monotonic same-session counters, ping and pong progress, zero timeouts, at most
one in-flight pong at the boundary. A silent stop of pongs fails this check once the boundary tolerance is exhausted.
Commit 8909cc2 also inadvertently halved the watchdog tolerance from about two
seconds to one. The step-7 review fix restores a two-second minimum independently
of cadence; tests cover a pong at period + 0.1 s and two lost 0.5 s-spaced pings.

Arm B completed under the explicit B-only continuation authorization:

| Message | Idle before first send | First-send result | Socket witness (epoch:sequence) |
|---|---|---|---|
| TTS started ×3 | 11.1 / 84.1 / 84.3 s | lost, T3 | 1:9 / 1:22 / 1:35, 129 B |
| Four-chunk burst ×3 | 0.08–0.10 s | gap-acked | all twelve reached socket |
| TTS stopped ×3 | 3.26 / 3.14 / 3.72 s | lost, T3 | 1:19 / 1:32 / 1:45, 129 B |
| Speaking stopped ×2 | 0.906 / 0.863 s | first-send ACK | 1:21 / 1:34, 145 B |
| Speaking stopped, b-3 | 1.104 s | lost, T3 | 1:47, 145 B |
| Seven retries | about 1 s | ACK | 1:14/20/27/33/40/46/48 |

All seven first sends reached `__real_lwip_recvfrom` and were returned unchanged
by its wrapper; exact epoch/sequence and size agree with the host UDP witness.
The reply windows retained all 13/13, 13/13, 14/14 counted DTLS records. Thus air,
AP, WiFi and lwIP loss cannot explain these seven missing first transmissions.
SACK gap blocks exclude the first TSN while acknowledging later chunks, proving
absence from SCTP's received set, so an SDK dispatcher delay is also excluded.
The finding is a drop inside esp_peer's DTLS/SCTP receive path after socket read.
The trace does not identify the responsible closed-library function.

A roughly one-second idle/read timer is an **inference**, not an identified
cause. A consumed wake-up datagram is a hypothesis. Arm A's cold first ping
was delayed 507 ms but delivered without retransmission: cold datagrams can be
delayed or lost, not invariably lost. That counterexample also argues against
lowering `SCTP_RTO_MIN` to 0.5 s; it remains 1 s. The wire sizes here are measured
129/145 B, superseding the earlier inferred identical-141-B argument for this hold.
Step 7 tests duplicate `bot-interrupted` messages about 100 ms apart with the
0.5 s keepalive. The cipher-preference hold is deferred. Nothing has been filed
with Espressif; that is the user's decision.

Step 7 interruption uses a dedicated app-control cancellation event. Under the
transport reader lock and Playout mutex it invalidates the current media generation,
retires queued packet leases, enters Empty, and starts a 150 ms mute. The consumer
clears partial decoded PCM and resets Opus/PLC; existing media-token checks reset
playback DSP before another write. No codec or DSP runs on the callback owner.
BotStarted ends the mute early. Muted RTP is discarded; render intervals are zero
without advancing the old media watermark or counting missing slots. The next
accepted packet reanchors the normal prefill. Committed I2S DMA drains normally.

The demo uses the public PipelineWorker `rtvi_processor` injection and RTVIProcessor
`push_transport_message` hook to send each bot-interrupted notice twice, 100 ms
apart, with the same session-local `interrupt_seq`. The receive owner suppresses
repeated/older IDs even across BotStarted. Legacy notices without an ID use a
redundant-cancel latch until BotStarted or a new RTP re-anchor. `AUDIO_OUT_10MS_CHUNKS=4` is pinned in the
hold; liveness remains 0.5 s and SCTP_RTO_MIN remains 1 s. PERF `cancellation`
separates events, duplicate notices, retired slots and discarded arrivals, suppressed render
intervals and discarded decoded PCM samples from network loss/PLC/drop counters.

This is a best-effort local flush. Late old RTP after mute expiry can reanchor;
new audio arriving before BotStarted while muted is discarded. Host tests exercise
both orderings explicitly, along with outstanding reader/writer leases, duplicate
notices crossing a new reply, silent cancellation, and real Opus reset recovery.
There is no 60 ms stale-audio bound or strict sender fence. Live measurements and
any remaining limitations are recorded in the step-7 report.

The declared step-7 hold (`aec-demo/step-7/live-1`, candidate 8cc3311) completed
six clean cells at the first attempt: two controls and four recognized Stops,
each followed by a new reply. Both interrupt copies were first-send ACKed in
all four pairs (5.66–18.97 ms ACK latency, 97.65–98.81 ms between DATA sends).
The device applied four cancellations and suppressed four duplicate notices;
53 packet leases/arrivals and 32 render intervals were counted as cancellation.
There was one active-window liveness miss and one teardown liveness alarm
(+1.2 s); the per-reply one-in-flight-pong rule passed the boundary despite a
pong that never arrived. There were no active T3 events; the one teardown T3 is retained separately.
This warm-path result does not distinguish the two cold-path loss hypotheses.

Conservative upper bounds were 188.60 ms server-event-to-last-active MIC3 sample
and 67.67 ms callback-to-last-active sample. No old-reply activity was observed
after the mute window. Provisional engineering targets are 250 ms server-to-MIC3
and 100 ms callback-to-MIC3; these do not replace the frozen one-second demo gate
or establish the expected 80–120 ms total latency. All ten replies have positive,
fully bracketed MIC3 activity witnesses within one second of non-silent TTS
(maximum onset upper bound 312.1 ms). The whole-capture scorer still reports ten
timing-unscorable attempts because several capture-edge blocks lack clock anchors;
that result is retained, not converted into a qualification pass. Future holds
must bracket both capture endpoints. New-reply prefix correlation and cutoff
sample-step diagnostics are retained; they do not prove every phoneme intact or
subjective click-free playback. Adverse RTP/SCTP ordering remains a host-test
limitation, not a live fault-injection result.

Minimum internal heap was 84,808 B (2,888 B margin); playback stack 4,504 B versus
the effective 4,288 B floor (216 B margin). Across all per-reply and session
snapshots, steady idle minima were 44.47%/35.59%; encode p95/max 6,799/10,678 us.
Every per-reply liveness check passed; final active PERF was 1,196/1,196 pings/pongs,
zero timeouts. Normal ELF 9936da45… was restored and boot-hash verified; mixer
restored, owned bot stopped, bench lock released. See the step-7 review report
and retained analysis for interval bounds and limitations; this is exploratory,
not step-9 acceptance or a qualified barge-in claim.

### Step 7 review corrections and host regression inventory

All four server-to-callback and server-to-last-MIC3 bounds share the same
~210 ms host bracket. The provisional 250 ms target rests on the 47–68 ms
device-clock upper bounds plus 5.7–19 ms first-copy SCTP ACKs, not the coarse
188.6 ms host upper bound. The next-reply prefix correlations (-0.51 to -0.71)
are consistent with inverted MIC3 polarity. The original whole-capture verdict
is unchanged: only the leading 5–7 blocks were unbracketed; every trailing edge
was bracketed. New holds anchor, wait 0.2 s, then record, and explicitly reject
any block outside the endpoint anchors. Coverage uses first/last mapped samples
and still requires each scoring window to lie within that coverage.

The ninth pending interrupt repeat is now dropped and logged after its first
copy has been sent. An interrupt while silent opens the speaker path to write
zeros for 150 ms; `ready()` is true during this interval. Cancellation counts
include a partly rendered Reading slot, hence “retired slots and discarded
arrivals.” A new RTP anchor clears the redundant-cancel latch, so a higher-ID
interrupt can cancel before BotStarted arrives.

The four revived host tests cover the production inbound callback/session,
recvfrom wrapper gating and exhaustion, 4 ms receive-select budget, and transport
delta counters. The other 14 same-environment failures predate this plan and
remain stale older-project tests: DgiGolden's five tests (all_modes_odd_width_and_
middle_crop, every_truncation_and_random_inputs, one_pixel_edges_and_canaries,
runs_indexes_deltas_and_transparency, structural_and_row_errors) require an
ignored binary; BindingTests' two approval/protocol-binding tests require a Git
checkout; HelpersTest's lock_contention and stale_reply_and_reader_error use old
serial attributes; test_hil_sources_do_not_embed_a_users_home_directory encodes
an obsolete path policy; speaker_empty_queue_credit and dma_deficit_accounting
use obsolete credit shims; native_logging_wrappers and serial_reader_preserves_
raw_bytes_and_records_device_audio_ack use obsolete logging/serial shims.
The separate voice-audit failure in Fable's broad run passed 3/3 isolated on
both revisions and is load-sensitive. The local missing idf_component_tools
import is an environment dependency, outside those 14.

Sampled minimum largest internal block was 43,008 B (the final snapshot was
45,056 B). Step 8 must attribute the 1,288 B heap reduction and core-1 idle
35.6–38.1% versus arm A's 43.6%; controls also show the change, so the evidence
does not isolate the flush as its cause.

### Step 8 capture clock

The RX clock now relates consumed frames and discarded whole DMA buffers to the
ISR completion count. Eight timestamp/count observations bound the preceding
DMA boundary without extrapolating the nominal sample rate or adding ISR work.
The timestamp is taken before loading the counter. A discarded partial driver
buffer only makes the inferred source older. The previous five-descriptor bound
is retained as a conservative fallback; overflow and short-read gap handling and
the independently measured 1,024-sample AFE delay are unchanged. The steady host
model bounds source age by 40 ms rather than the old roughly 95 ms. Live guard
closure remains to be measured, not inferred from this model.

A live ping can be admitted between the received/dispatched reads in a PERF
snapshot. For new step-8 cells, collect a bounded quiescent endpoint: retain
all observations with a 250 ms drain polling budget after the initial read,
with 20 ms polling. Serial reads have their own I/O timeout and are additional
to this budget.
Persistent backlog fails; counter equality and recorder delivery requirements
are unchanged. Include every intermediate snapshot in resource checks, so a
later endpoint cannot hide a drop, low stack, or CPU failure. This drains a
measurement boundary; it never repeats an acoustic cell. The earlier failed
`dry-v0` hold remains failed and retained.

### Step 8 exploratory findings and limits

The combined AEC image is `a3e29b6e1928a7d164088251b364b071562b6311098a3e1cfa9c945ab419630c`
(firmware 6643bca). Boot analog reference gain is 0 dB; holds explicitly select
15 dB exploratory gain and volumes 0/-6. Product defaults remain unchanged.
The four declared 300/400 ms TTS controls pass their capture-edge checks and
resource gates. Median source-block interval width is 43.8 ms. Measured
close/open durations are 463–483 ms for 300 ms source guard, and vary more
widely with 400 ms; the latter remains the default. The host model's 40 ms
bound is not a universal prediction of live closure duration.

Canonical probes give integrated reductions of 33.11/25.21 dB at 0/-6, with
zero clips or capture gaps. Near-only attenuation is 0.079/0.077 dB, inside
±1 dB. The 0 dB independent near-only control confirms 1,024 samples of AFE
delay (correlation 0.9998). Source-to-MIC3 correlation is 0.9676 at both
volumes. Acoustic MIC3-to-MIC1 peak isolation fails; the -6 near fixture's
strict source-match threshold also fails. These remain unscorable, with no
physical-lag claim or threshold relaxation.

The step-3 greeting versus step-5 TTS comparison is not a matched causal A/B:
the first 500 ms of the volume-0 greeting has raw MIC1 near -15 to -14 dBFS,
versus -18.38 dBFS for the step-5 cold TTS. Envelopes, phonetics and adaptation
history differ. A step-5 quiet early-onset window has only 1.30 dB reduction;
it is a quiet-window ratio, not evidence of an algorithm regression.

The apparent CPU drop versus 6d arm A largely compares unlike windows. Arm A's
active reply windows have core-1 idle 36.73–37.63%, versus step-7 controls
35.59–36.48%; step-7 export windows have 42.24–42.71%. Capture/playback task
load explains most remaining active-window difference; cancellation is zero
in those controls. This is observational attribution, not a matched causal
profile. Heap minimum also differs from settled free heap: the latter differs
by only 140 B between the compared 6d/7 snapshots, versus 1,288 B in historical
minimum. Session/Playout grew 32/40 B, but their allocations exceed the 1,024 B
internal-preference cutoff and prefer PSRAM. Those sizes do not explain the
internal-heap delta directly. Transient allocation peaks remain incompletely
localized; no allocation trace identifies every byte.

Every original dry-run cell and failed hold remains in the evidence ledger.
A single PCM discontinuity stopped one hold; the runner now avoids redundant
audio-reset commands when the guard is unchanged. A later received/dispatched
snapshot mismatch stopped another hold; the subsequent snapshot caught up,
and future boundaries use the bounded drain above. Neither failed hold is
retrospectively passed. The executable reply window was frozen at 18 seconds;
an inherited declaration prose field incorrectly says 23 seconds. Retained
blocks/anchors and the final two-second quiet gate determine actual coverage.

The auxiliary board-silent Stop was recognized by real Flux, but its hold
subsequently failed: one hardware I2S underflow and 3,356 write timeouts, with
the first timeout log 291 ms after silent cancellation and before the planned
disconnect. Loss/concealment/playout-drop counters are zero; host send gaps
reach only 24 ms. This does not identify the triggering device operation or
prove a transport cause. The silent-cancellation fault must be resolved before
step 9. Resource checks now enforce the hardware I2S counters as well as the
separately sampled pipeline AUDIO counters.

The dry run is not acceptance. Recognition, actual echo overlap, verified
future speech, guard overlap and hold integrity are reported separately in
`step-8/dry-summary.json` and the step-8 implementation report. Stops missed
outside the guard at volume 0 also require a demonstrated fix before step 9.
No result-driven retakes or noise-threshold changes are permitted to turn
these observations into a pass. The default guard remains 400 ms; two warm
300 ms controls do not justify changing it. Step 8b and step 9 remain unstarted.

Final dry-run counts (all original acquisitions, including retained failed
holds):

| Volume | Stop recognition | Stop acoustic credit | Sentence recognition | Sentence acoustic credit |
|---|---:|---:|---:|---:|
| -6 dB | 9/10 | 5/10 | 5/5 | 4/5 |
| 0 dB | 6/10 | 2/10 | 4/5 | 2/5 |

There are 12 Stops and 7 sentences with both active-MIC3 and future-speech
witnesses. Natural pauses receive no acoustic credit. Outside the guard,
Stop recognition is 9/9 at -6 and 6/8 at 0; all three in-window Stops were
missed. The earliest volume-0 sentence has WER 0.25: “Tell me” is omitted.
All 30 cells have clean v2 noise windows; no noise retakes were needed.

All 59 dry-run non-silent-TTS attempts have bracketed playback witnesses, with
no missing/unscorable attempt and a maximum positive-onset upper bound of
457.72 ms. ALSA invocation lateness is 0.218–10.392 ms, p95 4.275 ms; physical
near onset uses a separate acoustic witness. For 12 cuts with MIC3 active
across every candidate callback frame, maximum callback/server-to-last-MIC3
upper bounds are 76.80/178.97 ms. All 50 interrupt copies were first-send ACKed.
No original-reply activity was observed after the 150 ms mute in these captures;
this is not a strict late-RTP fence. Across 18 echo-only unguarded re-onsets,
the maximum 20 ms AFE residual is -44.08 dBFS. Recorded 400 ms gate closures
span 502.34–614.24 ms; the earlier 520–560 ms expectation is not a bound.

Across retained resource snapshots, minimum internal heap is 84,508 B
(2,588 B margin), largest sampled block 43,008 B, and playback stack 4,504 B
(216 B above its effective 4,288 B floor). Core idle minima are 44.29%/30.98%;
encode p95/max maxima are 6,899/11,318 us. These passing margins do not erase
the PCM, admission-boundary or auxiliary I2S failures. Timing-row wall-time
maximum is 5,004 us (final cumulative mean 5.47 us); this includes scheduling,
and no capture gap or live RX overflow accompanies the observation.

A distinct reconnect-only diagnostic also stopped before its planned
disconnect: the initial PERF transfer exhausted the original 250 ms drain
budget while one message remained in flight. Commit 4193508 starts the drain
budget after that first read; serial I/O retains its own timeout. A slow-read
host test covers this, but the correction is not live-validated. No capture
from this failed diagnostic is promoted, and reconnect revalidation remains
incomplete. The implementation brief's blocked-plan rule applies: stop for
review with these blockers, without starting step 8b/9 or repeating failed
cells to manufacture a pass. Every hold restored and boot-hash verified full
normal ELF 9936da45…, restored the mixer and released the bench lock.

### Step 8c review work and step-9 runner preparation

`hil/echoear_aec_accept.py` acquires the declared barge-in block; its paired
`hil/echoear_aec_accept_analysis.py` retains source/near-end/MIC3 witnesses and
reports each volume and stimulus kind separately. The draft specification is
`hil/plans/echoear-aec-accept-v1.json`. It is not a frozen acceptance campaign:
Astra review, real-talker level measurement, candidate/fixture hashes, eligible
counts and the other step-9 blocks remain prerequisites. Step 9 has not run.

The reply is a single continuous sentence intended to stay active beyond four
seconds. The timer still anchors to the first successful non-silent TTS transport
write. At its original deadline it also requires a recent active write (120 ms);
a newer silent write supersedes an older active one. A failed scheduling
precondition is retained without moving the deadline. Physical near onset still
needs the independent capture witness. A witnessed MIC3 pause is `no-opportunity`,
excluded from eligible counts. Unknown near onset, mixed MIC3 activity across the
onset uncertainty, missing guard state or unverified future speech stay unscorable.
In-window words and sentences have their own line, including the step-8 volume-0
0.3 s sentence with WER 0.25. They do not lower the out-of-window recognition score.

The step-8 `i2s_underflows=1` observation denotes one expired render interval and
refill, not proof of a DAC underrun. The step-8c TX fix keeps taking scheduled
opportunities and writing silence through short source-Idle gaps. Once the
committed tail (including DSP tail) has played, TX can idle; newly written drain
silence does not extend that tail indefinitely. The ring fake models IDF's four
240-frame descriptors, three-entry EOF queue, partial-buffer early pop and timeout.
PERF snapshots carry the **pre-write** RenderTick ledger: steady in-flight is
`640 - completed % 320`; after a complete 320-frame write it is
`960 - completed % 320`. Both phases are checked after an induced 60 ms gap.

Guard remains 400 ms. No post-AFE gain or Jabra-level change is made before the
user's real-talker measurement. The 5 ms render-write timeout is retained: the
ring regression passes without enlarging it. First timeout/refill diagnostics
include device time and the physical completion/submission ledger.

Step-8c bench evidence is under `~/src/evidence/echoear/aec-demo/step-8c/`.
The separate silent-cancel hold on firmware `69e0eaf` passed: board-silent Stop,
following reply, and a Stop during a reply; all cumulative hardware I2S counters
stayed zero through disconnect. MIC3 and independent source matching credit the
during-reply Stop. Playback stack minimum was 4,520 B (232 B above the effective
4,288 B floor); internal heap minimum 84,872 B; minimum steady idle 45.91/33.45%.
The unchanged reconnect declaration completed with healthy resources and playback
in both epochs, but does **not** establish active playback at disconnect: its
stats/disconnect/post-roll interval lacks dense anchors and spans both active and
quiet MIC3. No retake was performed. This witness and the user real-talker level
measurement remain open before acceptance. Both holds restored and boot-hash
verified normal `9936da45`, restored the mixer, stopped their bot and released flock.

### Step 8d: Flux connection-state diagnostic

The default remains untrimmed (`DEMO_TTS_TRIM_LEADING=0`). Client trimming is
only a diagnostic: Flux streams its leading silence at real time, so discarding
received silent samples cannot advance first-speech arrival.

`hil/echoear_tts_connection_trial.py` runs a declared server-metric experiment
with the board as client. Alternate sessions A (ordinary Flux service) and B
(`DEMO_TTS_RECONNECT_EACH_GENERATION=1`, evaluation only), at least eight per arm
and four identical ordered texts per session. B reconnects before queuing each
TTSSpeakFrame, including the first. The connection cycle is outside Pipecat's
per-generation metric window because disconnect stops active metrics. Keep its
wall time and request-to-transport-write latency alongside server TTFA, TTFB and
leading silence; do not describe service TTFB as including connection setup.
The hub exposes only private unchanged-settings reconnect primitives in 1.12.0;
the diagnostic subclass uses the same full disconnect/connect cycle as Flux's
own inactive-transport recovery. It changes no turn, mute or interrupt policy.

Freeze the text set, order, tolerances and denominator before acquisition. A
session enters the long state if any generation has server leading silence
>=0.8 s. Pass requires zero such B sessions and unchanged server TTFB across the
complete denominator; the declaration supplies numeric median/p95 tolerances.
Report reconnect-inclusive cost and whether A actually exhibited the state.
No acoustic scoring or noise gate applies. Keep raw MIC1 for every generation,
failed/incomplete sessions, all server metrics and the original declaration.
No automatic retakes. Own the bench flock through normal-image restoration and
boot hash verification, preserve the Jabra mixer, and stop the owned 7861 bot.
A passing diagnostic does not automatically change the product default.

### Step 8e: proactive reconnect policy

`DEMO_TTS_PROACTIVE_RECONNECT` defaults to 1 in `demo_bot.py`; explicit 0
restores the ordinary connection policy. The coordinator authorized this switch
after the two server-only A/Bs and the local-cleanup fixes; step 9 must qualify the
exact bot revision. Per-generation reconnect and trimming remain default-off
and diagnostic-only. The per-generation probe requires proactive reconnect 0.
The pinned version, source-hash and callable guards remain fail-closed. Refusals
emit an evidence event on every occurrence while the warning is logged once per
reason. Re-verify `server/tts_connection_probe.py` first on a Pipecat upgrade.

A normal TTSStoppedFrame followed by the public audio-context-completed callback
schedules one reconnect task. After interruption, completion of the base service's
local cancellation/context cleanup schedules it immediately. Provider
SpeechInterrupted/NO_ACTIVE_SPEECH is recorded separately and never gates a new
context, changes active-generation state, or schedules reconnect. A late cleanup
cannot reconnect a newer context. No synthetic stop frame is injected and
InterruptionFrame itself never triggers reconnect before cleanup.

A fast next turn and its metric start await only an existing reconnect task;
they neither cancel it nor start another one. Failure leaves recovery to Pipecat's
ordinary inactive-transport path. End/Cancel suppresses new work and awaits any
existing task before teardown. No shutdown-drain fix is included.

The historical 8-by-8 comparison requested g2 **after TTS stream end, before
playback end**, and g4 after the then-acknowledged interruption stop. The g2
1.1–1.2 s request-to-first-write median includes preceding queued playback and is
not a user-response latency. All 24 B normal stream stops preceded playback end by 0.922–1.351 s;
reconnect finished 0.651–1.274 s before playback end (retained-event re-derivation). g4 exposed the remaining reconnect: 21–52 ms
in fast sessions, 246–256 ms in slow sessions. Starting after local cleanup removes
the roughly 100 ms ack delay; a one-word follow-up can still encounter a slow
reconnect. Step 9 reports request-to-first-write after every Stop separately.

The comparison retained all attempts and post-seal errors, used context-attributed
transport writes, and passed the aggregate +40 ms median rule and zero-long-B
rule. It did not establish a per-fast-turn +40 ms guarantee. Defaults are enabled
by coordinator decision, not by changing the retained experiment's configuration.

## Step 9 campaign-02 host-only refreeze

Coordinator decisions of 2026-10-16 (`step-9-coordinator-decisions-2.md`) replace
only the quiet measurement with noise gate v3. Independently filter each full
settled-baseline capture and each 2 s cell quiet window using a fourth-order
Butterworth 100 Hz high-pass at 16 kHz, SciPy SOS forward/backward filtering
(`sosfiltfilt`, odd padding, padlen 15). Do not concatenate windows, carry state,
trim samples, or change the absolute/transient limits. Coordinator decisions 3
references the +6 dB rise to **max(filtered baseline, −61 dBFS)**: RMS ≤−55 dBFS
and ≤that reference +6 dB, maximum 20 ms frame ≤−46 dBFS. This restores the
effective −55 dBFS ceiling instead of tightening it to −61.5 when the filtered
baseline is −67.5 dBFS. Baseline attempts and two-retry /
three-consecutive-failure rules stay unchanged. This is a host measurement;
firmware, bot audio and acoustic scoring signals are unchanged.

`raw_dbfs` remains full-band MIC1 RMS; `gate_dbfs` is the v3 filtered RMS;
`max_20ms_dbfs` uses the gated signal. Retain full-band maximum 20 ms and MIC1/MIC3
fourth-order 100 Hz low-pass RMS diagnostics using the same forward/backward
padding. The low-pass diagnostics are not an FFT brick-wall band integral.
Baseline and live/probe/reconnect quiet windows all use the same definition.
The scheduler's new journal field is `noise_gate_passed`; legacy journals remain
readable, but a declaration with changed gates cannot resume an old campaign.

Run `hil/echoear_aec_noise_validation.py EVIDENCE DECLARATION OUTPUT` for the
retained-window check: 13 campaign-01 windows (including its settled baseline),
step-8b cell-17 post-roll against its own filtered baseline, seeded −45 dBFS
broadband noise, real speech at −52 dBFS, and fixed 200 ms speech-band bursts
in a 2 s window at −56 (fail) / −58 dBFS (pass). Burst levels are RMS over the
whole 2 s window; their 20 ms peaks exercise the unchanged transient gate. A
stationary −56 dBFS speech-band control passes, as it must below the restored
−55 dBFS RMS ceiling. All control definitions and peaks are retained. This validates the new measurement; it does
not retrospectively admit excluded cells or reuse campaign-01 acceptance data.

Every capture now applies the same transport check online as the analyzer, before
noise scheduling, with the verdict saved in capture `result.json` and hold
`hard_failure` on failure. The conservative burst upper-bound policy remains
unchanged; no RF outage exception/resume class has been added.

Each campaign admission boundary requests `wifi-stats` before sampling PERF and
retains `LABEL-wifi-stats.log` plus command timing/status JSON. This is evidence
only; a diagnostic error is recorded, and ordinary admission/resource checks
still run. It records driver RX/TX/buffer statistics, not the separate
`wifi-observe` TX-completion ring. The host NIC traffic-volume gate observes the
host's 5 GHz link, not the board's 2.4 GHz channel; a pass does not prove RF health
or distinguish AP/air from device radio/driver. The RF policy is unchanged.

Candidate ELF remains `76e29175ec089e47cedbfba8911ac8ba8fabec8ab975b3a50f1ce2f752c223e2`.
The new declaration must be reviewed before a separately authorized fresh
`step-9/campaign-02`; no campaign-01 clean cells carry forward. This refreeze
performs no bench access or firmware rebuild.

### Step 9 correction of record, 2026-10-16 (decisions 3)

Fable corrected a channel-order error in the campaign-01 review: three-channel
NPZ order is MIC1, MIC3 reference, processed AFE. The sub-100 Hz component is
on MIC1 and passes through the AFE; **MIC3 is clean (about −94 dBFS, maximum
2 LSB)**. Thus it is not a component shared with the electrical reference, and
neither ADC/supply coupling nor a board-electrical origin has been established.
Its origin remains unresolved. Large pulses are state-dependent: observed in
silence, not during playback; this is not evidence of audible-band room noise.
Keep the MIC3 diagnostics because they establish the distinction. No new bench
localization is part of campaign-02 preparation.

Whole, first-500-ms and post-guard echo results now include
`highpass100_residual_dbfs` beside the original full-band residuals. Filter the
contiguous AFE capture once using the same fixed 100 Hz SOS filter, then use the
original reference-derived masks and 1024-sample delayed output selection.
Unscorable windows get null diagnostics. The filtered residuals never affect
qualification, the full-band metrics, guard state or activity masks.
Declaration-v3 binds the current Fable fixes review and coordinator decisions 3
by path and SHA256; validation checks both. Old reports receive append-only
errata and old run verdicts remain immutable.

### Step 9 normal-voice block, coordinator decisions 4

The user selected −6 dB demo playback. Qualification now requires **both firm
and normal** blocks to reach at least 9/10 successes for each of Stops and
sentences, with ten independently eligible opportunities per kind/class. The
existing −6 dB firm cells, offsets, fixtures and gates are unchanged; 0 dB remains
5+5 report-only and cannot qualify. Each class has two independent ordered
opportunity spares per kind; neither spare pools nor success counts may cross
classes. Existing v3 noise, retry, recognition, overlap, latency, flush and hard
failure rules apply unchanged.

Eleven normal fixtures are derived from the exact frozen PCM sources by digital
attenuation only: Stop −10 dB (amplitude 0.3162277660), each sentence −6 dB
(0.5011872336), nearest-even signed16 rounding, no dither/normalization/resampling.
Source/output hashes, scale, samples/rate and no-clipping checks are retained in
`step-9/normal-voice-fixtures/manifest.json`; declaration validation checks the
actual sample equation. The Jabra mixer remains PCM75%, Mic80%.

The retained real-talker observation is from normal image raw MIC1 with DSP
bypassed, 300–3400 Hz, active 20 ms frames above quiet+15 dB. It is **not a
processed-uplink level** despite the legacy `real_talker_uplink` field name.
Normal Stops median −47.05 dBFS versus frozen Jabra −37.1; normal sentences
median −44.9 versus sentence-01 −39.7. Firm user Stops −35.2..−34.3. One talker,
one position. Full arrays, measurement provenance/hash and coordinator decision
are bound in declaration-v4. These observations motivate the scaling; they are
not measurements of the newly scaled playback, which has not run.

Campaign hold order is firm −6 (`qualification-01`), normal −6
(`qualification-normal`), report-only 0 (`qualification-02`), then −6 soak.
The normal block has its own restored hold and settled baseline, twenty primary
cells and four spares, and adds no echo/reconnect/probe/soak denominators. Firm
controls and reconnects complete before the normal hold. Planning estimate:
184 minutes (about3.1h), from the previous138 plus30 for twenty90s cells,
6 for four90s spares and10 for added setup/restore. Noise/harness retries can
extend it; this estimate changes no capture cap or fixed soak deadline.

Analysis reports `voice_class` and separate `volume:voice:kind` barge-in groups.
Every played stimulus with a matched near-source extent gets 300–3400 Hz raw
MIC1 and pre-gate AFE (+1024 samples) active RMS, using20ms frames above each
channel's filtered quiet baseline+15dB, along with active counts and extent.
Skipped/unmatched/incomplete windows are explicitly unscorable, never zero.
During double-talk the raw window includes far-end echo and the AFE window may
include residual echo: these measured levels are **not isolated near-end
estimates**. Per-class lists retain this limitation and noise validity. No
stimulus level affects eligibility or qualification; no new bench measurement
is claimed by this host-only preparation.

### Decisions 5–7: environmental transport and idle link probe

The runner retries an environmental attempt only when all six independent witnesses
pass: transport alone failed; delivery/resources/admission/I2S/observer are clean;
liveness and TTS lifecycle are intact; no post-PLC zeros or starvation; downlink
sequence/pacing and SCTP are clean; both RF byte gates pass; and the socket-RTP ring
contains the declared delay excursion inside the attempt counter window. The 0.5% double-count transport gate
and burst bound 3 stay unchanged. `unique_unusable_fraction` is diagnostic only.

For the link witness, unwrap RTP timestamps (48kHz) and subtract the window-local
5th percentile of `observed_ms - timestamp/48`. A qualifying excursion has at least
three consecutive integer host seconds with maximum delay >=120ms, containing at
least25 packets with delay >100ms in an inclusive sliding five-second span within
that run. It must lie inside the before/after attempt window, including its export
tail; scored-cell overlap is diagnostic only. A gap >=1s (including
missing endpoint coverage) cannot satisfy it. Retain delay p50/p99/max, per-second
table, unique missing/expected-range ratio, reorder depth, max inter-arrival gap and
the naive forward-gap sum. The latter counts reorder and is never a loss gate.

Completed proactive TTS reconnects between context generation windows are allowed
and reported. Failed/refused, incomplete or overlapping cycles fail. Reply overlap
uses the existing context metric start/stop lifecycle; queued PCM may still play
after generation stops, so delayed BotStopped is not the generation endpoint.
Actual Jabra request/duration brackets the near stimulus. The wider scored cell
start/end interval supplies only the reply-overlap diagnostic.

Excluded attempts retain raw inputs in `acquisition-failure.json`, receive no score,
credit or opportunity spare, and share the lifetime two-retry budget with noise.
Three consecutive noisy/environmental cells pause. Exceeding three environmental
attempts per physical hold or six over the campaign/resume chain also pauses after
saving the journal. New holds reset the streak, not lifetime attempts/campaign
counts. A second recovery boundary at least five seconds later must show clean
transport; resources, delivery, admission and liveness span the entire recovery.
Epoch transport checks consecutive snapshots outside only the witnessed exclusion
spans; retain raw first-to-last transport diagnostically. Other checks are unchanged.

Before scheduled stimuli in each physical hold, run the connected idle bot for at
least 240 seconds (the declaration pins exactly 240). Gate only on unique missing-sequence loss <=0.5% and no
inter-arrival gap >=1s, with complete observers and normal device integrity checks.
Report delay p50/p99/max and excursion count, but do not gate on excursions. This
sample does not predict the next three hours. Failure records `LINK PROBE FAILED,
retry later`, stops and uses normal restoration/mixer/unlock; no automatic overnight
restart. Four probes add sixteen planning minutes (200 total before retries).
Wifi-stats retry columns are not maintained in this build and cannot witness retries.

Fable's original campaign-02 uplink loss/time-of-day claims are withdrawn: the hold
has seven unique missing IDs out of9,736, not308 missing packets. The review recheck
and decisions6 adopt delay/reorder evidence instead. Original campaign verdicts and
retained evidence are unchanged; replay uses new separately named output files.


The 240s duration avoids the pinned Pipecat worker's 300s idle timeout without a
bot override. A host regression imports `IDLE_TIMEOUT_SECS` and checks the declared
9s connect overhead + 240s probe + 45s margin < that constant. The former 300s probe
would deterministically cancel the idle worker before its after-boundary check.

`attempt-window.json` retains `capture_end_s` as soon as capture stops and
`after_boundary_s` immediately after the boundary completes. Each qualifying
excursion has `during_export` (overlap with capture-end through after-boundary),
plus the existing `overlaps_cell`/`overlapping_cells` diagnostics. Missing export
timing is null, not false. Reports include a prominent `EXPORT-LOAD HYPOTHESIS`
warning if every environmental attempt includes an export-tail excursion; re-examine
the class before further use in that case. This is a report-only flag, not a gate.
Two of the three reviewed transport events (campaign01 cells7/8) occurred during
export; campaign02 pair4 occurred during a reply. This pattern motivates the column
and does not establish that export causes radio delay. Cell7 failed the original
transport gate at0.8914%; under the corrected full-window witness it now classifies
environmental, while cell8 still hard-stops on independent device/liveness faults.

For an unattended launch, `campaign-03-launch.pid` identifies the nohup/uv wrapper,
not necessarily Python. To stop gracefully, use `pgrep -af` with the pattern
`python .*[/]hil/echoear_aec_campaign[.]py` to identify the Python process for this
declaration, then send SIGTERM to that process; do not signal the saved wrapper PID
or force-kill restoration. Launch instructions remain separate from bench GO.
