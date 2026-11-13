# First scored AEC protocol — preparation

The population is fixed by `hil/plans/echoear-aec-scored-v1.json`: ten distinct
short scripted bot replies with no user sound, then five original barge-ins
using the same short spoken **“Stop.”** stimulus. This intentionally limited
first evaluation cannot establish barge-in accuracy across diverse phrases.
No scored campaign has started. Freeze the reviewed harness, source, approved
plan copy, image, stimulus and calibration hashes before execution.

Use frozen experimental ELF52a61f23 and actual `demo_bot.py` Flux STT/TTS with
`ECHOEAR_AEC_FULL_DUPLEX=1`. AEC remains default off regardless of an individual
gate result. Only a completely passing campaign can support changing it.
The live peer, Opus workers and cat display must be active. No-peer results
are not admissible evidence for the81,920-byte heap floor.

## Pipeline and stimulus

An explicitly enabled evaluation hook will queue `TTSSpeakFrame` commands for
the predeclared texts through the real Flux TTS service. Flux STT, its external
turn strategies and `should_interrupt=True` remain active with no user-mute
strategy. Automatic LLM responses to recognized speech are suppressed only in
this test mode, after user-turn logging; this prevents a new response from
obscuring cessation of the interrupted reply. It must not suppress STT,
turn-start proposals, interruption frames or input audio. This is a live
audio/transport/AEC test with scripted assistant text, not an LLM accuracy test.
Default demo behavior must be unchanged.

The script control accepts known IDs only. It cannot inject a transcript,
user audio, user-turn event or successful gate. Structured logs retain peer ID,
process identity, mode, turn starts/stops, raw Flux transcripts, mute frames,
interruptions, TTS contexts/PCM and output speaking events. Missing or extra
peers, service errors or a mode mismatch fail. No prerecorded STT output is
accepted as a barge-in transcript.

Prepare the Jabra stimulus off bench through the existing approved TTS
credential path. Apply fixed1.25x tempo, source-defined silence trimming and
80/100ms leading/trailing silence. Active duration must be0.15..0.55s before
calibration; otherwise preparation fails. Hash the source and result. The
Jabra mixer remains PCM8/11(-4dB), Mic6/7(+3dB), with no geometry changes.
Unscored DAC-silent calibration sets the stimulus-file amplitude to the
existing nominal-39dBFS board speech level; never alter board gain to make a
scored case pass. Freeze the calibrated file before campaign consumption.

## Resource and measurement prerequisites

After quiet-room/NIC eligibility, consume the sole campaign attempt before
opening the full live peer. A heap/driver/resource failure is a retained real
failure, not a quiet-room deferral or permission to replace a scored attempt.
Preallocate the existing30s schema2 capture before connecting, avoiding heap
allocation on the scored audio path. Warm-up is diagnostic only and cannot
exclude any unexplained counter changes after scored-session start.

Room floor must be<=-55dBFS at both the Jabra and raw board mic. Gate total
host wlp8s0 bytes on20MB/300s, record wired enp7s0 without gating; no host/AP
changes. A pre-start noisy/busy deferral consumes no scored observation and
must release the bench. Every original after consumption remains, including
failures, unavailable timing, provider timeouts and partial aborts.

Remeasure independent raw-MIC1-to-pre-gain AFE delay with DAC silent, using
the already reviewed near-end verification. Prior1024-sample/64ms results
are context, not permission to optimize a scored ERLE delay. No NS/AGC.
Reference identity/gain/channel order must match the reviewed MIC3 geometry.

## ERLE and false turns

The old-45dBFS reference activity cutoff is not calibrated for this electrical
divider: measured active MIC3 was-69.1dBFS but25.6dB above quiet. The new
scorer explicitly versions its activity rule and does not rescore old results.
For20ms frames, reference RMS must exceed both2LSB and independently measured
quiet RMS by10dB. Require at least five active frames in each reply and actual
TTS/physical output evidence. Select activity from raw reference only, never
from AFE residual or an ERLE-maximizing alignment.

Align raw microphone and AFE output by the independently measured delay.
Compute each active-frame10log10(Pmic/Pout), flooring powers at1LSB squared,
then the median for each complete reply and the median of all ten reply
medians. Target>=20dB. Preserve all frame scores and activity decisions.
Count ADC/reference/output saturation over the entire capture, including
alignment-trimmed prefixes and tails. Muted, incomplete or unaligned captures
cannot qualify. The waveform is raw MIC1 versus pre-HPF/gain AFE output;
the soft limiter must not inflate measured ERLE.

The zero-false-turn region covers the entire quiet ten-reply phase, including
its inter-reply silence. Count actual Flux/user-aggregator turn starts and
final nonempty transcripts, not merely VAD or only events during active TTS.
Any spontaneous user turn fails, even if its transcript is empty or the
automatic LLM response was suppressed. No Jabra playback occurs in this phase.

## Physical barge-in stop timing

Use a streaming Jabra recorder with20ms raw blocks and a monotonic receipt
timestamp **after each read**. Take the user-onset lower bound immediately
**before** starting the owned `aplay` process. The acoustic-stop upper bound
is the receipt time of the last block needed to confirm200ms of quiet. This
is deliberately conservative: user sound starts after its request and
captured sound precedes its receipt. Do not infer capture start from process
startup, subtract USB latency, or use a server interruption timestamp as
the physical stop.

The short user word ends soon enough for both sources to become quiet within
one second. Require quiet RMS<=independent room+6dB, no renewed activity
through2s after the request, verified pre-barge bot audibility, and hardware
MIC3 cessation. Play a fixed300ms aperiodic non-speech marker no earlier
than request+2.1s. Require unique, ordered complete word and marker matches
in both continuous Jabra and rawMIC1 recordings. These acoustic witnesses
prove post-trigger freshness and capture coverage despite arbitrary pipe
backlog. Receipt times alone never prove freshness. Missing witnesses or
ALSA overruns/read errors fail. Quiet begins only after the verified word and
continues through the marker onset; exclude the marker itself. Preserve all
Flux events including delayed finals and marker effects, with one final user
turn per original by its3s post-marker deadline; conflicting/missing finals fail. Require a real Flux interruption during the reply and a TTS
source with at least2s future PCM already generated before the trigger,
attributed to the actual DAC offset at verified word onset using rawMIC3/source
and common-clock rawMIC1 word matches. That source window must contain no
>=200ms natural quiet interval (fixed source-only-45dBFS activity). Missing
PCM, provider errors/stalls, early EOF and natural completion fail. A
pause followed by resumed speech, Jabra self-echo suppression while MIC3
continues, or a provider failure cannot pass as a successful interruption.
If any evidence is unavailable, timing is unavailable/failed, not zero.

All five upper-bound stop latencies must be<=1s. At least four of five final
Flux transcripts must equal normalized “stop” (case/punctuation only). Retain
all five originals. The stop bound includes output process/USB/host receipt
and quiet-confirmation overhead; it may conservatively fail a fast physical
stop. Do not raise the1s gate or retake it for that reason.

## Whole-session integrity and execution

Record each of the ten echoes and five barge-ins in its own continuous
schema2 capture, each bounded to28s to stay inside the existing30s storage.
Each original has a12s execution deadline; all15 originals remain required.
If a case overruns, retain failure; do not extend capacity or drop cases. Bulk
exports and PERF occur between originals, with the peer and capture uplink still
active. They remain inside whole-session integrity scoring. No mid-reply
PERF, intentional uplink disable, dump-window exemption or WiFi observer.

Every original and the whole session require all13 transport-counter deltas
zero, no ADC/front-end/AEC saturation or audio errors (including write timeouts),
unchanged microphone gain and per-PERF output-volume0 readback, no failed allocation, internal minimum>=81,920 and largest
block>=32,768 bytes. Include sampled largest-block minima where available.
The peer/audio-TX tasks must actually exist in the resource evidence. Keep
existing stack/display/GDMA guards. Uplink>60ms rate<=1%,>150ms rate<=0.05%,
maximum<=2000ms, with complete authenticated RTP identities/coverage matched to socket
receipts, explicit positive observer health after the final endpoint, and no
hidden windows. The final counter bracket starts at the last PERF request;
all prior PERF/dumps/gain readbacks are inside the scored window. Require
cat rendering>=24fps over every complete>=1s bracket, current stack floors,
and complete live-peer GDMA priority proof. Child capture failures, provider
errors or missing final transcripts fail. Each owned resource is cleaned up
even if another cleanup fails, with failure retained.

One finite campaign only, not a soak. Maximum hold900s and at least1020s
remaining in a new explicit B grant, reserving restoration time. B owns its
probe/campaign schedule; never interrupt an admitted acquisition. Stop on
fatal exhaustion/reset/resource fault or deadline; missing originals prevent
qualification. Always release services/serial/lock, report the installed image,
and let B independently restore its frozen image. Default AEC stays off.

## Review status

Astra reviewed the initial draft9ac0f78 and required the marker freshness
witness, measured future-PCM proof and per-original captures above. Concrete
implementation/tests and final source review remain pending.
It does not authorize hardware or claim that any gate has passed. Framework
basis was retrieved through Pipecat Context Hub: [Flux STT and external turn
management](https://docs.pipecat.ai/api-reference/server/services/stt/deepgram.md)
and [PipelineWorker queued speech](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/pipeline/worker.py#L983-L1011).

The concrete ff6be245 implementation review also found a false credit for a
bot already silent before the user. The corrected scorer requires active
MIC3 during three20ms blocks before and one after the source-defined active
word onset, then native cessation through the late marker. A dedicated
regression retains the old falsely passing fixture as a failure. The two-second
future-PCM proof uses this same active-word coordinate. Exact consumed protocol,
source, dependency versions and copied approval are bound; an unreviewed draft
cannot run. A global sole-campaign marker prevents changing output directories
to repeat scored originals. Final source re-review remains required.
