# Audio HIL

Place the Jabra SPEAK 410 about 10 cm from the board, speaker grille facing
the board. Keep the bench quiet. Both acoustic paths are required: Jabra speaker
to the board's right I2S microphone slot, then board PCM5101 speaker to Jabra mic.
ALSA resolves USB VID:PID 0b0e:0412 to its stable card ID; numeric card order is
irrelevant. PCM is set to 75% and Mic to 80%, both unmuted (ALSA uses `cap` for the Mic capture switch and `unmute`
for PCM playback). Actual rounded
percentages, dB and mute states are saved in mixer.log.
The setter verifies the qualified Step-7 readbacks: PCM 8/11 (73%, −4 dB)
and Mic 6/7 (86%, +3 dB), both enabled. A different readback fails the test.
During the step-8f acoustic hold, the operator must receive
`ACOUSTIC HOLD: <next arm>` and give explicit go-ahead before any microphone
capture, including room-baseline recording. Do code and host checks first.
The e2e, agent, and step-8f crossover/regression entry points record a three-second
Jabra room baseline before playback, retain WAV/mixer/JSON evidence, exclude the
first 200 ms of recording startup, and print its dBFS level. A baseline above
−50 dBFS marks that arm noise-contaminated. The hold is an orchestration rule;
do not launch a multi-arm campaign that would advance without each required
go-ahead. Retain interrupted and contaminated arms separately from qualification.
E2E originals and once-only repeats each restore and verify their own mixer
settings before playback; hardware-button volume changes do not carry over.

From the repository root:

```sh
uv run --project hil hil/flash.py --project hil/audio-loop-fw
cd hil
uv run audio_loop_test.py --firmware loop
```

Repeat the test three times for acceptance. Every run resets the device and
creates a unique repo-relative hil/out/<UTC timestamp>/ directory, with serial.log,
mixer.log, stimulus/recording WAVs, ALSA setup log, firmware.json and result.json.
The JSON records failures too, and the process exits nonzero on failure.
The firmware hashes describe local build artifacts; flash.py records the files
actually handed to esptool and esptool verifies the programmed data.

Host-to-board: collect at least three baseline reports, play a 1 kHz stimulus,
and evaluate reports in the stable interior of playback. At least three tone
reports and a majority (at least two) must simultaneously satisfy all thresholds.
Board-to-host: start a 5 s recording, capture baseline before sending the tagged
1.5 kHz/2 s tone at the amplitude qualified in `speaker_calibration.json`
(20% after step 7, for both firmwares), require its matching successful acknowledgment, and
timestamp recording readiness, command send and acknowledgment receipt with the
monotonic clock. Relative to recording readiness, baseline is [0.2 s,
command time - 0.1 s] and tone is [ack time + 0.3 s, ack time + 2 s - 0.2 s].
Both windows must be at least 0.5 s and fully inside the recorded WAV.
Both firmwares use the measured Jabra response correction and the unchanged
5% THD / −26 dBc H2/H3 gates. `--amplitude 50` reproduces the former loop
stimulus; that level is outside the step-7 clean range and its failures remain
in the evidence. Amplitude selection does not change frequency, RMS, SNR,
clipping or distortion acceptance thresholds.
The tone acknowledgment marks onset; a separate matching DONE reply confirms
successful I2S writes, and ERR fails the test. A failed or missing baseline,
serial acknowledgment, ALSA operation or recording is a hard failure.

Both directions require target-band power rise >=20 dB, harmonic-excluded band
SNR >=15 dB, and peak frequency within 40 Hz of the stimulus. Band SNR is power
within target +/-30 Hz divided by remaining power between 300 and 4000 Hz,
excluding +/-30 Hz around each harmonic k*target (k >=2) in that band. Harmonic
distortion is not counted as noise. Hann-window power is normalized by FFT
length and window energy, so baseline and tone durations may differ.
band_rise_db is tone target-band power minus baseline target-band power, in dB;
board reports use the median baseline target-band power. Wideband RMS remains
reported in dBFS but is not an acceptance gate (int32/2^31 on the board,
int16/32768 on the host). Host analysis also reports saturated-sample counts.
The loop firmware's target band is centered at the host's 1000 Hz stimulus.

The result separately records qualification_passed and per-criterion margins:
at least 5 dB above the SNR and band-rise gates on every analyzed report, at
least 5 Hz inside the frequency tolerance, and zero clipping. Frequency margin
is expressed in Hz, not dB. Three consecutive qualification passes establish
stimulus headroom. Ordinary passed retains the thresholds above.

Evidence bundles stay local under gitignored proj-*/evidence/. When preserving
a run there, omit tone_a.wav: make_wav regenerates this stimulus deterministically.
Preserve the recorded rec_b.wav, logs, metrics and firmware hashes.

Serial commands are '#<seq> <command>'; replies are 'OK #<seq>' or 'ERR #<seq>'.
Tone commands additionally return 'DONE #<seq>' after successful writes.
EspSerial uses monotonic deadlines, explicit cursors, a 20,000-line ring and a
full raw disk log. Use its context manager to release the port. Both flash.py
and scripts/idf.sh flash acquire /tmp/dg-esp32-serial.lock; use these wrappers
instead of invoking idf.py flash directly. The lock is advisory.

Restore the main application after testing:

```sh
uv run --project hil hil/flash.py
```

Configuration hygiene (run from the project directory): scripts/idf.sh sources
IDF v6.1 and adds sdkconfig.local to defaults when present. Run fullclean,
remove only the generated sdkconfig, then reconfigure and run
scripts/check-sdkconfig.py. The checker compares exact requested lines, with
local overrides taking precedence, and redacts local values in differences.
Empty credential defaults allow building without sdkconfig.local; no networking
is implemented in step 1. Do not save unredacted sdkconfig or build headers as
evidence.

Step 6 root firmware supports `audio_loop_test.py --firmware app` with the same
thresholds and tagged tone protocol. The transport console is available after
WiFi startup. `quiet 0` enables the proven FFT reports; `quiet 1` stops them.
`mic-stats` reports the last completed two-second PCM RMS, peak, clip count and
Goertzel powers at 1000/1500/2000 Hz, and enables FFT reporting. Mic statistics
are after the 50 Hz high-pass and configured gain, before the gate.

`audio 1` enables microphone uplink; `audio 0` disables it. Both commands
emit `AUDIO_STATE enabled=0|1 device_us=...` after the change; the serial reader
retains device and host receipt timestamps in `serial.audio.jsonl`. Link and
send intervals overlapping acknowledged audio-off windows classify as `idle`
and remain separately reported beside the raw population. Legacy captures
without those acknowledgements keep their original raw gaps.
Capture and playback hardware remain running in both modes. Microphone uplink defaults off so the
step-5 `tone-tx`/`speech-tx` exact-source tests remain deterministic. `gate auto`,
`gate open`, and `gate closed` select the policy; disconnect disables uplink.
`tone <hz> <ms> [percent]` is a local speaker diagnostic (default 50 percent).
Avoid local diagnostic tones during network playback: they serialize on the
speaker and can delay the network reader. `stats` includes audio drops/errors,
clip counts, task stack margins, transport counters and heap snapshots.

Run `uv run --project hil python hil/greeting_test.py --out <evidence-dir>`
after flashing root firmware. It owns an offline runner on 7861 and records
before connect. Correlation uses a fixed 300–1000 Hz fourth-order bandpass on
both waveforms, retaining the full greeting duration. This band was selected
from the measured board-speaker/Jabra response; broad-band phase response and
speaker harmonics substantially reduce raw waveform correlation. Acceptance
reports correlation >=0.6, unfiltered RMS rise >=20 dB, acoustic gate timing,
audio errors/drops/clips/active underruns, and the internal heap floors. Per the
step 6 scope update, an RMS-rise shortfall at unity gain remains a failing
acoustic criterion recorded for step 6b; do not increase gain to make it pass.
Network playback defaults to unity gain and is capped at 100% in Kconfig;
the digital dump retains pre-gain PCM, and local tones use their requested level.

Heap snapshots also print `reserve` and `excluding_reserve_lb`. The latter is
`max(0, free_internal - CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL)`: a conservative
lower bound that subtracts the entire configured reserve, even if part is in
use. IDF 6.1 actually permits normal malloc to fall back to this lower-priority
pool; the subtraction is deliberately stricter than malloc eligibility.
Use `transport_test.py --exclude-reserve --out <evidence-dir>` to require the
120 KiB floor at ready, channel-open and streaming snapshots even after this
subtraction. Ordinary 32 KiB largest-block checks remain enabled.

Phase 6a adds `main_largest`, `reserve_free`, `rtc_free` and
`general_internal = internal - reserve_free - rtc_free`, plus per-region
snapshots. Largest-block acceptance now uses `main_largest`. The legacy
`excluding_reserve_lb` remains for comparison; it does not subtract RTC and
should not be interpreted as general-purpose internal free. DTLS reports both
a 1 ms simultaneous-free sample minimum and a phase-local sum of heap minima.
The latter is still not a simultaneous measurement. Codec quantiles use
100 µs histogram bins and retain exact maxima.

`transport_test.py --campaign --sessions 3 --out <directory>` collects all
sessions after acceptance assertions while preserving each failure; fatal
protocol errors still stop the run. It exits nonzero if acceptance failed.
`audio_loop_test.py --firmware app --out <directory>` supports an explicit
evidence destination. `console_throughput_test.py --out <directory>` checks
128 tagged ping commands and complete contiguous PCM transfer without a reset.

`phase1_campaign.py <unique-name> --boots 5 --extras --settings '<JSON>'`
is a local step-6a bench runner. It changes defaults, performs the fresh-config
procedure, checks exact settings, builds and flashes root firmware, and saves
non-secret configuration/map/hash evidence before the 15 media sessions,
two greetings, application loopback and console check. It requires the recorded
`evidence/step6a/defaults-instrumented.txt` baseline and refuses to overwrite a
measurement directory. `--boot-only` is for isolated allocation measurements.
Use `phase1_summary.py <directory> --out <summary.json>` and
`phase1_compare.py <baseline.json> <candidate.json> --out <comparison.json>`
to retain distributions and apply the brief's bounds. The comparison JSON is
authoritative; an exit-zero summary command does not mean acceptance passed.
Full campaign comparisons also require both greetings, loopback and console
results. `--claimed-heap-bytes` checks the minimum ready general-internal gain;
standalone three-session screens use `--sessions 3`.

### Step 6b speaker and consumer-paced playback

Network audio is decoded by the internal-stack `audio-playback` task on core 1
(priority 13). Each pull drains the bounded packet queue, prefills the configured 20 ms
frame reserve (fixed at seven frames, 140 ms, since aec-v86 0e44051; it was five), and advances once per I2S write. Five PLC frames precede a rebuffer;
the contiguous READY backlog is capped at ten frames (previously eight). Persistent
overfill drops the oldest frame after one second above target + 3. There is no
decode worker, PCM playback queue, arrival deadline, or active-stream age drop.

`PLAYBACK` reports the clean ceiling, high-pass, limiter samples, active DMA
underflows and driver completion-queue overflows. Audible gating uses a threshold scaled with
the selected gain so clean low-level speech retains the original gate behavior. Intentional startup DMA silence
and idle are excluded from underflows. TX has four 240-frame stereo PCM16 DMA
buffers (3,840 bytes / 60 ms). The capture diagnostics use the existing float/
integer option; microphone PCM is unchanged. `PERF` JSON uses buffered console
output because the added counters exceed the reliable per-character log path.

Run `speaker_sweep_test.py --out <evidence>` for two raw-tone sweeps at the fixed
Jabra mixer settings. Optional `--amplitudes` and `--frequencies` add boundary
measurements; `--elf` verifies an archived image. All WAVs, windows, harmonic
powers, noise measurements, JSON and Markdown rows are retained. Diagnostic tones
bypass the network DSP so their requested amplitude remains meaningful. Capture
uses the Jabra's native 16 kHz rate; harmonics above its 8 kHz Nyquist limit
are unavailable, and their THD is marked as a lower bound. Older ALSA-interpolated
48 kHz recordings are analyzed after recovering their native sample phase.

`audio_loop_test.py --firmware app` reads `speaker_calibration.json` for the
3% raw tone and distortion bounds. `greeting_test.py` records acoustic and decoded
PCM acceptance; `--rms-rise-db` explicitly records a calibration probe override.
`play-long` requests sixty seconds of repeated reply PCM from the offline server;
`playout_long_test.py --out <evidence>` records depth trend, estimated clock ratio,
decoder timing and counters. Long requests are restricted to offline-test mode.

`step6b_campaign.py` interleaves archived images with the phase-1 transport,
summary and comparison tooling. It retains failed boots and repeats each once in
a separate outliers directory. It refuses to overwrite measurements and restores
the candidate image. Comparisons use session p95 distributions and the committed
phase-1 baseline; repeated outliers remain visible alongside the formal cohort.

Step 6a phases 2–4 use `phase2_campaign.py` with a saved, fresh committed
control under `../evidence/step6a-p2/images/control/`. Each lever gets its own
fullclean/reconfigure/settings check and archived BIN/ELF/map. The runner
alternates A and B for three boots per image, each with three formal media
sessions. `--concurrent-sctp` adds a 15 KiB echo during audio for TX-buffer
trials; `--performance` requires an encoder-p95 improvement instead of a
1 KiB ready-heap gain. `--boots 10` performs the combined campaign. Failed
sessions are repeated once in the same boot under `reruns/`; original samples
remain in all formal distributions. The session p90 is the lower empirical
90th percentile (second-worst with nine samples).
`phase2_outliers.py <lever-names...>` repeats isolated statistical tails once
in separate directories and recomputes decisions without replacing samples.
`phase2_cpu.py <serial-logs...>` measures task runtime deltas during formal
duplex trials on both cores, using task IDs to distinguish workers with the same name.

`phase2_endurance.py --out <evidence-dir>` runs the final root firmware for
30 minutes with mic capture enabled and `play-long 30` (thirty repetitions
of the identical 60 s fixture), then 50 connect/disconnect cycles and five
reboot-based WiFi reassociations. It records heap regions, playout counters
and task high-water/runtime snapshots each minute and after every cycle.
The soak explicitly opens the microphone gate to exercise continuous duplex
traffic. Both runners own only port 7861.
The soak raises the opt-in RTP recorder cap to 100,000 packets and the
offline worker idle timeout to 2,100 seconds; ordinary sessions retain their
10,000-packet recorder cap and default timeout. Long fixture output is fed
a few seconds ahead of playback by a session-owned task, canceled at teardown.
Each minute also checks that sent, received and pulled counters advance.
If the soak completed but lifecycle capture was interrupted,
`--resume-lifecycle` preserves the original result and logs, resumes once on
the same boot with a new owned bench-server process, and retains the failed
attempt separately from successful disconnect samples.

`codec_benchmark_test.py --elf <archived-elf> --out <new-directory>` invokes
the disconnected `codec-bench` console command. Media workers are parked;
a temporary internal-stack worker on core 1 at priority 24 times individual
codec calls with interrupts excluded and yields between frames. Each of four
cases (FEC off/on, native state in PSRAM/internal RAM) measures 500 recorded
speech encodes/decodes, 500 zero-frame encodes and 500 server-silence decodes.
Raw cycle counts, clock frequency, allocation placement and high-water marks
are retained. FEC applies to the corresponding encoder; decoding is normal,
without lost-packet FEC recovery. `codec_inputs.py`, run with the server Python,
records and checks the installed server encoder's actual silence payload.

`payload_histogram_test.py --elf <archived-elf> --out <new-directory>` records
received payload-size histograms during a three-second idle window and a
four-second reply-fixture window including its idle tail. The `PERF` histogram
counts accepted in-session packets before queue insertion, including packets
subsequently dropped by the queue. It changes no encode, decode or send policy.

Step 6c compares server encoders on the unchanged committed e185e9e image.
`BOT_OPUS=aiortc` retains the stock encoder (the historical fallback after acoustic
acceptance failed; the current default is lean); `BOT_OPUS=lean` installs the mono, 8 kHz cutoff,
30 kbps encoder before peer creation. `BOT_OPUS_BITRATE` and
`BOT_OPUS_CUTOFF_HZ` configure that encoder. The cutoff caps bandwidth but
does not guarantee SILK coding for every packet.

`step6c_campaign.py --out ../evidence/step6c/<new-name>` alternates modes
over five boots each, three formal transport sessions per boot, retaining
same-boot failure reruns. It then runs two acoustic greetings per mode,
lean application loopback, and once-only statistical-tail reruns. It never
builds, flashes, or changes configuration; the running board must already
match `--elf` (default `build/dg-esp32.elf`). `--outliers-only` resumes just
the tail-repeat phase in an existing campaign.

`server/.venv/bin/python hil/step6c_codec_reference.py <new-directory>`
records clean, loss-free fixture round trips for both encoders, including
idle prefix/tail, waveform correlation, TOC and packet sizes. This diagnostic
does not replace board acceptance or lower its thresholds.

For the matched long comparison, run `phase2_endurance.py --soak-only`
once with `BOT_OPUS=aiortc` and then with `BOT_OPUS=lean`, using separate
new `--out` directories. This keeps the 30-minute physical-mic/fixture
duplex session and minute snapshots but skips lifecycle cycles/reboots.
Ten-minute counter deltas are saved even when the stricter historical
zero-interruption checks fail. These long results are evidence rather than
an adoption gate. `step6c_summary.py <campaign-directory> --soaks <soak-parent>`
retains formal populations, task CPU coverage, exact received payload-size
histograms, phase-2 decisions and ten-minute soak summaries. The soak parent
contains `aiortc/` and `lean/` captures.
The collector retries incomplete heap/task census snapshots. In soak-only
mode it retains valid buffered PERF counters after three unsuccessful census
attempts, sets `census_complete=false`, and leaves missing heap fields absent.
The original strict lifecycle collector still rejects incomplete censuses.

Opt-in `RTP_EVIDENCE_DIR` also captures `downlink.jsonl` with actual
successfully sent RTP payload sizes, TOC, timestamps and send times.
`downlink-rtcp.jsonl` records matching receiver loss reports when available.
Sender gaps and board PLC/lost counters alone do not prove downlink wire loss.


Step 7 uses `speaker_dual_sweep_test.py --out ../evidence/step7/<new-name>`
for simultaneous native board-mic/Jabra measurement. `mic-record 1|0` captures
30 seconds maximum of native microphone PCM before gain, filtering, and gating;
`dump-mic` exports ordered base64 chunks. The existing `tone` command uses the
normal I2S write/gate path with PlaybackDsp bypassed (unity gain, no high-pass,
no limiter); harmonic calibration covers 500–7500 Hz. Every raw recording,
repeat and once-only failed-point repeat is retained. The exact brief gates
can be recomputed with `step7_speaker_summary.py <result.json> <new-json>`.
`speaker_speech_test.py --amplitudes 15 20 30 --out <new-directory>` stages the
production DSP and checks fixture words, loudness, limiter counts and leakage.
`playback-settings <gain-percent> <ceiling-percent>` is diagnostic-only.

`e2e_offline_test.py --out ../evidence/step7/<new-name>` and
`e2e_voice_test.py --out ../evidence/step7/<new-name>` each run five acoustic
turns, with the voice test explicitly skipping missing credentials. Recordings
start before the prompt. The immutable `hil/fixtures/prompt_speech.wav` is
resampled into an evidence WAV for **48 kHz USB playback**: this bench Jabra
advertises 16 kHz playback but drains it at three times the intended speed.
Capture remains native 16 kHz. The prompt and reply are matched separately;
reply matching searches only after the prompt, and gate leakage is checked
against the audible reply window. Original failures and once-only repeats
remain separate; p50/p90 use original runs, with missing stages marked N/A.
Offline has fixture-byte timestamps, with transcript/TTS stages N/A.

`app_reconnect_test.py --cycles 50 --out ../evidence/step7/<new-name>` checks
user-intent disconnect, at least fifty cycles, one injected channel-open
failure (`fault-channel-next`), owned server kill/restart and simulated WiFi
loss (`wifi-drop`). Connect retries run through the app policy, so each
failure and its retry outcome remain visible. The control task has an 8 KiB
stack, core 0, priority 4; callbacks enqueue immutable session generations.
The session worker performs blocking WiFi/HTTP/teardown operations. The boot
joins WiFi and waits in Idle for console `connect`; `disconnect` clears intent
before teardown. Capture intent survives automatic reconnects.

Prompt-end timing uses host monotonic ALSA playback completion, since Jabra
AEC suppresses its own playback in its microphone recording. A negative
VAD-stop offset means VAD became quiet before playback completion. First
audible reply is measured by Jabra/reference correlation, with the unchanged
0.6 correlation, 3 dB room-noise rise, gate timing and digital-silence checks.

`step7_transport_campaign.py --out <new-directory>` captures three sessions
on each of three boots of the unchanged final image. Compare the preserved
15-session lean baseline with `step7_transport_summary.py <directory>`; the
phase-2 timing/count floors and 10% CPU bound remain unchanged. The app loop
test uses the measured Jabra response in `speaker_calibration.json` to correct
harmonic levels while retaining the raw observations and distortion thresholds.

For suspected bench drift, `step7_matched_transport.py --control <saved-image>
--out <new-directory>` interleaves three boots each of the exact saved control
and final firmware. It snapshots and restores the final image. Preserve the
historical comparison as well as the matched comparison; do not replace old
samples or change bounds. The step-6c control BIN and ELF hashes must match its
saved identity. `step7_transport_summary.py` accepts
`--expected-baseline-sessions 9 --baseline <control-summary.json>` for this
matched population, with the same performance and media-failure rules.

Step 8 adds `perf_test.py --elf <saved-elf> --out <new-directory>` for separate
20-second connected idle and duplex windows. Idle closes the mic gate while
uplink zero frames continue; duplex opens the mic gate and streams the reply
fixture. `quiet 1` suppresses FFT/reporting diagnostics while retaining
physical speaker output, playback DSP, decode and I2S work. Forced-open duplex
profiles set the offline bench's interruption override so physical speaker
feedback cannot cancel the fixture. Device and server traffic in both directions
must meet the existing 2,900-frames/minute floor; an open gate alone is insufficient.
A profile's `passed` flag means collection completed;
`step8_profile_policy.py <formal-directory> --boots <count>` applies the CPU,
network and counter bounds separately. Missing task observations remain absent.
Each primary task must have the full requested observation count in each phase.
Detailed nested timing and arrival tracing default off in the final step-8 image.
`--option call-timing=0` keeps iteration, frame and task
measurements while disabling detailed nested call timers; `call-timing=1`
enables those timers for investigation. `--option arrival-trace=1` enables packet
arrival and policy records; collectors that need them must opt in. Use
`soak_test.py --no-trace` to validate final compiled defaults. Disabled parts have null measurements,
not measured zeros. This option requires a firmware image supporting it.

`step8_campaign.py --screen <knob>` archives the cheap fresh A/B screens and
once-only repeats. `step8_formal.py <knob> --original <starting-image>
--observer <matched-image> --candidate <one-lever-image> --boots 4
--out <new-directory>` interleaves original A, matched observer C and candidate
B, with three digital media sessions and idle/duplex profiles per boot.
Failure and statistical-tail reruns are supplemental; original samples remain
in the numeric population. Failed idle/duplex windows also get one fresh matched
A/C/B repeat under `profile-reruns/`, with their failed checks in `source.json`.
These repeats never replace original CPU, network or counter observations.
`--no-observer --candidate-defaults --boots 10` runs the final 30/30 A/B
comparison after the individual matched comparisons. Saved image directories
contain BIN/ELF, bootloader, partition table and public identity/config records.

`step8_latency.py --control <starting-image> --candidate <final-image>
--pairs 10 --out <new-directory>` measures cold and genuinely connected warm
turns in both offline and live modes. It requires live credentials, uses the
step-7 speech/acoustic/leakage assertions, and reports each stage's median/p95
and observed count. At least ten observations are required for every applicable
latency stage and for cold connection timings, separately by image and mode.
Warm observations must retain the preceding successful peer session through
the reply; an unknown or replaced connection is kept as a failed, unverified
sample and excluded from actual cold/warm coverage.
`--b-auto-silence 0` selects a qualified silence-suppressed
server for B; A retains the starting server silence/chunk settings. All
originals and their once-only repeats are kept separately. The ALSA prompt-end
timing limitation above still applies.

`soak_test.py --elf <saved-elf> --out <new-directory>` runs the final 30-minute
physical-prompt campaign: one prompt every 20 seconds, reconnects at minutes
10/20 and an owned server restart at minute 15. Fault-overlapping stimuli are
marked. `--continuous` instead runs the continuous duplex fixture for arrival
investigation or QIO qualification. The continuous fixture disables user
interruptions only in this bench mode;
the final physical-turn soak retains normal speech behaviour.
Snapshots retain heap minima and task
margins; finalized worker high-water marks include teardown. Every playout
policy event is correlated with socket, authentication, native callback and
server send boundaries, and grouped by ten-minute block. `dump-arrivals`
drains 32-byte records; a nonzero overwritten counter invalidates complete
attribution. The final closed ring's cumulative written count must also match
the total records captured across generations. PERF identifies observed session
generations; retries between snapshots require a unique authenticated RTP
SSRC/server-session match. Socket-only or ambiguous matches cannot identify a
retry, and unattributed policy events fail the capture.
Socket observations follow WiFi/lwIP queues, so absent socket
reads alone do not establish radio loss. Deferred diagnostics remain outside
media callbacks. Use new evidence directories under `../evidence/step8/`.

Step 8b: speech loudness uses `bench_calibration.py` before every acoustic E2E
turn. `--spl 65` (default) and `--spl 58` on either E2E script scale the host
48 kHz prompt using native pre-gain mic PCM and the MSM261S4030H0R's nominal
−26 dBFS/94 dB SPL sensitivity. The calibration JSON records input reference,
output reference on both mics, room floor, digital scale and achieved estimated
SPL. Step-7 ALSA mixer readbacks are restored and checked. This is a sensitivity
based SPL estimate rather than calibrated SPL metrology.

`step8b_tts_capture.py <evidence-bank>` captures two fixed utterances once;
`step8b_speech_sweep.py --out <evidence-dir> --bank <evidence-bank> --ceiling 26`
records four utterances per target on both microphones, transcribes Jabra PCM,
and records active limiter duty plus closed-gate silence and VAD events.
`playback-loudness <negative-dBFS> <ceiling-percent>` stages runtime speech DSP;
`playback-settings 33 26` selects the legacy level for comparison.
`play-fixture <name>` plays an existing fixture or a host-enabled offline TTS
bank name. Fixed-utterance capture continues through internal sentence pauses;
gate checks apply to active speech and the final reopening.

`speech_accuracy.py` preserves word edit distance while canonicalizing numeric
STT formatting, including `03:30` versus `three thirty`; raw scores/transcripts
remain evidence. `acoustic_response.py` captures the unscored `prompt_france.wav` at known
legacy gain 50%, ceiling 26%, and 500 Hz high-pass to identify a fixed 129-tap
response for each microphone. Alternating held-out temporal blocks must
correlate at least 0.6 before the model predicts any scored audio. One fresh
calibration repeat is allowed before scoring; rejected captures are retained.
The known legacy soft knee is included in the calibration source. Scored
utterances never train the response. The modeled FIR gate explicitly compares
against a fitted linear response: it measures waveform agreement and can respond to nonlinear faults and
wrong content, while linear faults can be absorbed by calibration. Transport
dropouts require the explicit counter gate described below. Both the
board-mic and Jabra chains must pass 0.6; raw Jabra correlation is reported
without gating. This differs from the original raw-reference gate.
Raw and placement-corrected per-band SDR/correlation are both retained;
nonlinear residuals remain visible separately from calibrated tone THD.

Step 8b3 uses `step8b2_speech_sweep.py` for legacy 33% and delivered
-25/-22 targets at 26% ceiling, two cold and two warm draws per program.
Warm runs preserve normalizer state between programs. The script builds its
renderer from production DSP and resolved sdkconfig. The limiter has a 2 ms
attack ramp, 4 ms delay, 2 ms hold and 75 ms release. Above-ceiling fraction
must be <=1%; limiter duty and original-source SDR report compression depth.
The binding distortion-only SDR predicts rendered decoded PCM through the
physical FIR (without applying DSP a second time), with board floors
12/10/5/0 dB at 500-1000/1000-2000/2000-4000/4000-7000 Hz. Acoustic board gain
over matching legacy must agree with rendered digital gain within +/-2 dB.
Board microphone correlation must reach 0.6; Jabra correlation/STT are
informational, with the declared Jabra level gate retained. Fractional alignment is declared
before scoring: one shared delay +/-1 sample fitted over 500-3500 Hz. Integer
SDR remains diagnostic; `--rescore-from` preserves historical floors, original
checks and hashes. Historical 8b2 -25 passed 3/4 pre-declared integer SDR versus
4/4 after fractional correction (minimum corrected margin +0.81 dB).

Each program/reply bracket gates lost, concealed, plc_frames, drift_drops,
rebuffers and playout_dropped at zero delta. Missing counters fail closed.
Transport-damaged sweep attempts remain on disk, excluded from acoustic
scoring, with at most three capture attempts. Agent/E2E original turns remain
in the strict total; damaged replies are separated from acoustic denominators.
Zero I2S underflows only excludes starvation while source monitoring is active.
The modeled correlation gate alone cannot guarantee continuous playback.

```sh
hil/.venv/bin/python hil/step8b2_speech_sweep.py --out ../evidence/step8b2/new-sweep --bank ../evidence/step8b/tts-bank --ceiling 26
hil/.venv/bin/python hil/agent_conversation_test.py --out ../evidence/step8b2/new-comparison --runs 3 --compare-old-bin ../evidence/step8b2/start.bin --compare-old-elf ../evidence/step8b2/start.elf
hil/.venv/bin/python hil/step8b2_listen.py --out ../evidence/step8b2/listen-morning
```

The comparison interleaves three cold conversations per image, saves failures,
and restores the current image. The listen command announces and records the
same greeting at step 8 legacy 33%, step 8b -25 with the archived old limiter,
then the chosen step 8b2 setting from `selection.json`. Only its optional
`--leave-connected` handoff detaches the owned bot. `step8b2_spl.py` reports nominal SPL at the board microphone only, with
several-dB uncertainty. Tone transfer is a spectral estimate at that position;
it does not measure the Jabra pickup position. A ruler and SPL meter are
required for a figure at 1 m.

`agent_conversation_test.py` exercises **BOT_MODE=voice** on an owned runner at
7861: physical calibrated prompts -> board mic -> Deepgram STT -> OpenAI ->
Deepgram TTS -> board speaker -> board microphone prerecorded STT. Its six-turn script
checks Paris, the contextual Italy/Rome follow-up, addition, introduction,
blue sky and name recall. Immutable prompt WAVs/text/hashes are in
`fixtures/agent_conversation/`; the user voice is Orpheus, the bot is Thalia.
Credentials come only from `../pipecat/.env` and never enter evidence.

```sh
uv run --project hil python hil/agent_conversation_test.py --out ../evidence/step8d/quick
uv run --project hil python hil/agent_conversation_test.py --runs 10 --out ../evidence/step8d/qualification
uv run --project hil python hil/agent_conversation_test.py --runs 2 --spl 58 --out ../evidence/step8d/quiet
uv run --project hil python hil/agent_conversation_test.py --interruptions --out ../evidence/step8d/interruptions
uv run --project hil python hil/agent_conversation_test.py --soak-minutes 45 --out ../evidence/step8d/soak
```

Use a fresh output directory. Every acoustic turn checks the USB identity and
ALSA readbacks. Initial calibration measures each prompt separately at the
native board mic to within 1 dB of nominal 65 or 58 dB SPL, using the step-8b
microphone sensitivity method (a broadband estimate, not a certified SPL meter).
`--calibration-from <previous-output>` may reuse qualified measurements for the
same image, level and unchanged placement; an independent speaker response
calibration is still captured for every campaign. Never regenerate the WAV bank
for ordinary runs. `agent_prompts.py --generate` is its one-time creation tool.

Each original turn preserves server events, PERF before/after, recording,
reply-only crop/transcript, all eight checks, classified failures, cumulative
prompt-end latency and individual stage durations. Summaries include p50,
p90 and maximum with actual counts; missing timestamps stay missing. Host ALSA
completion supplies prompt end; Jabra AEC suppresses its own prompt, so negative
VAD offsets are possible and are retained. First audible reply is located by
live TTS PCM correlation using the independently qualified room response.
Board modeled correlation must be >=0.6, with Jabra RMS rise >=3 dB,
the declared absolute transferred SPL window and digital-silence checks.
Jabra correlation and Jabra STT are informational.

Ten runs use five cold boots followed by five conversations on the last cold
session. Warm turns retain the same peer ID and generation. Qualification never
replaces failed originals with reruns. LLM wording and VAD/STT misses are reported
separately from firmware playback/gate failures. A missing VAD edge cannot
qualify a turn even if STT and its audible answer are correct.

The 45-minute mode sends 135 real scripted turns at roughly 20-second intervals
and captures PERF every minute. `--soak-minutes 20 --fault-free` runs the
60-turn growth qualification with no faults. Memory checks use a five-minute
settle after each Ready; every generation's first post-settle sample supplies
its own 4 KiB transient baseline. Pre-settle drops are reported separately.
Faults retain their own 16 KiB budget. Growth uses a common slope with per-generation
intercepts, at least ten total fitted minutes, and conservative OLS/Newey-West
uncertainty. Minimum detectable growth (64 + 2 SE B/min) and largest sample drop
make the verdict's resolution visible. Unknown windows remain listed.

Historical 8d2 voice mode (superseded by the final 8d3 defaults below) used
the Pipecat default 0.2 s VAD start/stop, minimum volume 0.6,
explicit SmartTurn v3 with a 1 s incomplete-turn silence bound and both default start
strategies with interruptions enabled. The board gain is 1600% (+18.06 dB);
SPL and modeled response calibration use native pre-gain mic dumps. Playback
Production uses the user-selected −22 dBFS delivered target / 26% ceiling. Deepgram
endpointing and TTFS are provider defaults. `BOT_VOICE_POLICY=timeout` and `8d`
select the comparison controls; `mic-gain <1..1600>` changes input gain only
with capture disabled and resets to the image default on boot.

`step8d2_campaign.py --out <fresh-directory>` rotates four arms across five
six-turn 65 dB conversations and two six-turn 58 dB conversations per arm.
Every child uses a new boot/context and independent unscored response fit.
Contemporaneous native-SPL calibration is shared within a level. The unchanged
France fixture tests its existing short dip; the extra `pause` fixture inserts
0.8 s of digital silence between separately generated fragments. Every clean
fixture must transcribe as its complete text (WER 0 with explicit number
normalization); rejected generation attempts stay in the requested evidence
folder. The old Italy waveform is retained in step8d2/original-fixtures.

Each TTS segment must independently match the board acoustic chain and
pass the Jabra level gate; audible onset
and end, transport loss/PLC/drift/rebuffer deltas and clean-source control ASR
on content failures are recorded. A cancelled generation is logged even when
its LLM already ended but output was pending. A cancelled partial is tolerated
only below 300 ms measured audible duration; unknown duration fails.
`stt_final` is the last final before the first LLM start; later finals are separate.

The interruption mode asks for a long count, plays the arithmetic prompt wholly
inside its closed gate, checks that no new VAD/STT/reply is produced, then asks a
fresh France question after reopen. With no AEC, speech over the speaker is
ignored; the user must repeat it afterward. Barge-in would require aligned
speaker-reference PCM, an AEC implementation qualified for the board's acoustic
path/CPU/RAM, ungated clean capture, server interruption strategies, and explicit
playout cancellation/queue flushing with new overlap/recovery qualification.

`agent_conversation_summary.py <output>` writes a separate `analysis.json`;
raw `result.json` is preserved. It records corrections to the early reply-count
checker (which incorrectly coupled reply count to VAD) and classifies wrong
answers caused by current or earlier STT misses at their upstream boundary.

`agent_silence_test.py --out <fresh-output>` verifies 120 seconds of physical
room noise in voice mode after greeting, with no spurious VAD/STT/LLM turns.
It qualifies the faster VAD start separately from conversation pass rates.

A faulted soak injects reconnects at minutes 2 and 8 and a server restart at
minute 5, between scripted turns. This keeps faults in the first ten minutes
and provides a long settled final generation. `--fault-free` disables all
faults for the step 8d2 growth verdict. Actual starts and recoveries remain
visible against the original 20-second schedule.

The long-count answer checker normalizes only the exact complete 1..20
concatenated digit sequence that Deepgram smart formatting can produce.
It keeps word/number token boundaries for all other answers. Separate analysis
records this correction for the first overlap capture without rewriting it.

`--restart-regression` verifies a question before a killed/restarted voice
server and France/Italy after recovery. Automatic recovery waits on the
existing `connect` command completion barrier before enabling capture: the
Ready log precedes publication of app readiness. It scopes greeting gate
events to the new Ready entry and requires increasing uplink counters.

The retained historical 8d two-second speech timeout also covers transcript-start fallback
without VAD: a captured arithmetic turn emitted “What's” and its trailing
“two plus three?” 1.33 seconds apart. A real pipeline regression reproduces
that delay; the former one-second policy fails it. The latency cost is
reported explicitly, and the new qualification is kept separate from earlier
settings and both interrupted soaks.

Reply cropping uses the final gate reopening after the last observed closure.
An intermediate opening during a rebuffer gap must not truncate later reply
words. Continuous-reply gate checks remain strict and still report any such
opening; this crop fix does not relax gate or PCM-silence assertions.

Step 8d2 compares the server's `AlwaysUserMuteStrategy` speech intervals with
board `GATE` edges. Each turn's `mute_evidence` includes server speech-end to
board gate-open delay, user turns and interruption frames during server speech,
and user turns in the remaining board playout tail. These are retained even
when acoustic content or transport checks fail. The pre-mute exploratory
captures remain separate from the final comparison with mute in every arm.

Transport evidence includes `lost`, `concealed`, `plc_frames`, `drift_drops`,
`rebuffers` and `playout_dropped` in both whole-turn and reply-window deltas.
Any nonzero available delta marks the turn transport-damaged; strict passes
retain that turn, and `turn_policy_results` records server behavior separately.
Reply-window snapshots prefer the last completed post-prompt snapshot before
TTS and finish after gate reopen plus two seconds. Early split replies and
later duplicate output use the conservative whole-turn bracket. Earlier
captures retain broader before/after brackets, explicitly labelled in derived
analysis. Missing counters stay null, including unsupported drift duplicates.

`step8d2_campaign.py --resume` keeps complete failed turns and failed
calibration attempts, then collects only missing conversations. It rejects a
pre-mute result. `step8d2_final.py --resume` similarly keeps completed final
observations. Both accept `--elf` to verify an archived image. Prompt SPL
calibration covers the six standard fixtures and requested extra cases;
reusing a bank for a new extra case calibrates that case separately. An unused
long-count fixture cannot block a six-turn quiet comparison.

### Step 8e0 raw network probe

`netprobe start HOST PORT [PPS BYTES ECHO [HOST2 PORT2]]` uses dedicated UDP
ports 47000/47001. Defaults are 50 pps and 130 bytes; 1200-byte payloads are
also supported. Both destinations must be configured in one start command.
`netprobe stop` joins the probe task, `netprobe stats` emits one `NETPROBE` JSON
line, and `netprobe dump` emits base64 batches of little-endian 16-byte
`<qIHH>` records: device arrival/send microseconds, sequence, path, send errno.
Errno zero denotes a downlink arrival; nonzero denotes a failed uplink send.
The per-path PSRAM ring holds 46,080 records and the bitmap 65,536 sequences.
Both paths together retain 1,490,944 bytes of PSRAM after their first start,
including the two bitmaps; these diagnostic buffers persist until reboot.
Failed uplink records share ring capacity. A wrapped ring or exhausted bitmap invalidates downlink loss; incomplete failure capture invalidates post-success uplink loss. Dumps
require a stopped probe. No packet logging or application allocation occurs
inside the probe task; lwIP retains its normal socket/pbuf allocation behavior.
The task runs on core 1 at priority 18, above playback's priority 13, and
records wake-up gaps, send overruns, send errors, and stack margin.
The receiver waits on both sockets with `lwip_select` (1 ms timeout) and
drains each ready socket until EAGAIN. Recorded timestamps are socket-drain
times. Mailbox-full counters are retained by local UDP port across socket
deletion/reuse; analysis must reject exhausted mailbox tracking.

`netprobe.py` is a standard-library host sender/receiver and reference client.
Host listeners learn the peer source from its first uplink packet before
sending downlink. This opens the Eero's NAT mapping for the ethernet path.
The optional echo appends device RX/TX timestamps; it adds a second 50 pps
uplink stream per destination. Independent uplink pacing continues regardless
of RX. Reports retain RTT, sender lateness, actual transmitted denominators,
loss bursts, per-minute loss events, and inter-arrival deviations from send
intervals. Echo midpoint mapping aligns device events to host monotonic time;
per-minute mapping estimates expose clock drift and path asymmetry.
Echo mode 0 disables echoes, 1 retains historical full-rate echoes, and 2
sends at most 5 pps on path 1 only. Use mode 2 for voice arms. Sequence-set
downlink loss and socket-stall durations remain valid without any echoes.
Stall summaries count gaps >=100 ms, group gaps separated by at most 1 s,
and report episode spans and periods between groups containing >=3 stalls.
The headline periodic episodes also report a >=10-stall threshold to separate
the long scan-like bursts from small background clusters; all gaps and groups
remain retained.

Run the ordered hardware campaign with runtime bench addresses:

```sh
hil/.venv/bin/python hil/step8e0_campaign.py \
  --wifi-host "$BENCH_WIFI_IP" --ethernet-host "$BENCH_ETHERNET_IP" \
  --spark-ip "$SPARK_WIFI_IP" --spark-host-key "$SPARK_SSH_HOST_IDENTITY" \
  --gateway "$AP_GATEWAY" --out ../evidence/step8e0/campaign-N
```

Each long arm lasts 610 seconds: baseline 130 B, baseline 1200 B, continuous
WebRTC over WiFi, continuous WebRTC over ethernet, and baseline repeat.
A final 60-second arm saves an unprivileged channel scan. Every arm includes
both probe paths, Spark bound to `wlP9s9`, and a 50 pps gateway ping bound to
boule's WiFi interface. Spark uses Python and `/tmp` files only. Startup waits
for the device console after reset; failed captures remain in their output
folders. The campaign owns only its offline bot on 7861 and never changes
network configuration, firewall rules, or port 7860.

`server-url URL` changes the bot endpoint in RAM while the app is idle.
`BOT_ICE_INTERFACE=enp7s0` restricts host candidates in the owned offline bot
process before ICE gathering. `ice-pairs.jsonl` records the actual nominated
pair at connection time and checks its local interface. Default server
behavior retains all interfaces. RTP arrival tracing records socket receipt,
authenticated receipt, sequence-gap notifications, callback, PLC, and drift
or queue drops; the campaign drains it every ten seconds.

After collection, analyze each completed arm:

```sh
hil/.venv/bin/python hil/step8e0_analysis.py ../evidence/step8e0/campaign-N/baseline
```

RTP loss uses server-sent packets absent from the complete socket trace;
reordering that later heals a gap is not final packet loss. Probe and RTP
loss can share stall/burst mailbox overflow; probe loss is not an RF witness.
Classify absent RTP by the preceding same-socket stall and drained burst size.
Historical stack counters are global and sampled about once per second;
IP drops can include unrelated background traffic. Pipeline
classification requires an authenticated packet before an explicit drop or
PLC decision. Retained examples show these boundaries and uncertainties.
The public station API does not expose a current PHY bitrate; the report
marks it unavailable and retains WiFi driver dumps and host bitrate snapshots.

Always compare the saved client frequencies and BSSIDs before attributing a
shared loss window to one radio channel. Spark can associate with another
Eero BSSID/band while remaining on the same LAN. The retained Spark reference
uses 5 GHz, another BSSID and power saving; it does not cover the ESP's
2.4 GHz hop. A radio witness requires a second 2.4 GHz client on that BSSID.
`SO_BINDTODEVICE` may require CAP_NET_RAW on older Linux kernels.

The ethernet WebRTC arm runs a temporary LAN STUN discovery responder on
boule's ethernet address, UDP 3478, to discover the ESP32's NAT candidate.
`BOT_ICE_REMOTE_NETWORK_ONLY=1` also filters remote SDP candidates to that
interface's subnet within the bot process: the private ESP candidate otherwise
causes checks to route through boule's WiFi. Plain STUN uses null native TURN
credentials. The actual nominated pair, rather than the offered candidates,
proves the selected path. `--only` and `--from-arm` resume with new output
folders, retaining failures. Port 7861 must be free before starting.

Analysis audits bot mic RTP by session/SSRC, extending 16-bit sequences across
wrap and reordering. Expected is the received sequence span; endpoint losses
require an independent ESP RTP-send ledger and are not claimed. It reports
burst histograms, RFC 3550 jitter and absolute transit variation, and associates
uplink gaps with reference/probe windows. aiortc's `arrival_ms` uses its NTP-era
clock; the retained monotonic `observed_ms` aligns it to the probe.

WiFi RX/TX/buffer dumps are saved before and after each run. Analysis retains
the vendor's numeric pool types and flow-control/allocation-failure deltas;
these are pressure indicators, not additive lost-packet counts. Peer socket
send-error/ENOMEM counters supplement native send return codes. Lateness
analysis retains authentication-to-PLC, later callback delay, missing callbacks,
and inclusive slow-loop overlaps. Boundary task/core identities come from the
transport code and PERF inventory; intervening scheduler execution is unknown.

The campaign keeps host sockets open through Spark’s full transmitter lifetime.
Analysis invalidates uncovered reference tails and omits echoes read during the
serial-stop collector pause from RTT statistics. Failed/invalid smoke captures
remain in evidence; they do not enter the long-arm comparison.

### Step 8c crossover

`step8c_campaign.py build` creates immutable images from production defaults
plus `hil/step8c-sdkconfig.defaults` (lwIP statistics) and local credentials.
Only public settings and hashes enter evidence; generated sdkconfig remains
under ignored `build/step8c`. Build outside measurement windows. `block`
flashes an archived image, verifies the ELF identity and captures a fresh boot.
Runtime `--voice-path wifi` selects the intentional two-hop stall stimulus;
`ethernet` is the final bench. Addresses remain CLI/RAM settings.
`build --production final-control` creates the pre-8c performance control
with lwIP statistics disabled. After the final production root build,
`archive-root` archives that actual root ELF/binaries as `final`, validates
the requested settings and requires statistics disabled. Qualifying this
archive preserves the identity of the build the orchestrator verifies.

`--cpu-only` disables raw probes and arrival tracing, opens the mic gate and
uses a repeating fixed PCM fixture after microphone gain, before level/gate
processing. Its SHA256 is recorded. Downlink speech is continuously paced.
For DTX CPU comparisons, pass `--gate closed` in both arms and their A/A
controls. Continuous encoding/downlink coverage remains required; failed
sends do not count as successful DTX suppression. Uplink audits retain the
48 kHz RTP timeline for both 20 ms (960 ticks) and 40 ms (1,920 ticks) frames.
CPU captures set `RTP_EVIDENCE_PAYLOAD_BYTES=0`; network captures set it to
`1` to retain uplink codec-payload lengths. Byte rates count unique received
packets within the RTP decision window and exclude lost payloads and
transport/radio overhead. Old records without sizes have an unobservable
byte rate. Keep this observer setting equal across comparison arms.
Keep CPU and network populations separate, start with A/A, and retain failed
stimuli. Network blocks retain per-port overflow, paired stall episodes, PLC
boundaries, drift/rebuffer, RSSI, ENOMEM and heap. Nested call timings are
inclusive wall intervals; `call-timing=1` with arrival tracing also records
calls >=40 ms (kind 11, value duration us, aux PeerPart).

Same-name FreeRTOS workers are reported separately by task number (for
example `pipecat#17` and `pipecat#18`). Every compared task needs at least
eight measured windows in each A/B and A/A population; missing task evidence
does not qualify. `quality_cpu_guard` retains the old any-task verdict and
applies the corrected materiality rule: non-idle p50 increases must exceed
max(matched A/A, 2 pp), and idle p50 decreases must exceed max(matched A/A,
5 pp), to count as regressions. Other candidates still need a CPU win beyond
A/A and acceptable long network quality. XIP-from-PSRAM and CPU data-cache
experiments belong to the separate 8c2 step.

`compare_quality_network` requires the long populations and a boundary or
PLC improvement whose whole-block confidence interval exceeds A/A variation.
It also checks all authenticated frames for missing callbacks with enough
post-window trace, and rejects regression in disjoint PLC+drift output
actions. Lower callback-loss or PLC counts alone cannot conceal extra drift
or rebuffer damage. RAM cost and the independent materiality CPU guard remain
separate adoption requirements.

The orchestrator's final 18:40 user decision overrides the long network
campaign for 8c: compare production DIO80/mailbox6/cache100/VFS select with
QIO80/mailbox32/cache200/direct select in one 310-second two-hop block per
arm, back to back. `compare_network_screen` records raw counts and retained
A/A point variation, without bootstrap confidence intervals. Report whether
each block contains a prominent stall episode; a single block cannot measure
the approximately 304-second recurrence. The CPU comparison still needs
eight independent windows per arm. The later 18:55 decision adds recv20 and
40 ms uplink frames with a 32768-byte TX stack to the final candidate. Both
audio-tx and peer-loop savings must exceed retained CPU-win floors; report
encode wall time, enqueue-to-owner-send and TX stack margin. Compare baseline
and candidate decoded-uplink STT accuracy and reply latency in the offline
voice checks. The user's latency gate is candidate reply p50 <= baseline
p50 +100 ms, with p50/p90 and per-turn values reported; candidate transcript
errors may not exceed baseline's. Bisect failures by dropping QIO80, frame40, recv20, then direct
select. The final Ethernet voice gates below remain unchanged.

`step8c_qualify.py --image <archive> --ethernet-host <host> --mode
offline|reconnect|soak --out <new-directory>` flashes the archived image,
owns a temporary STUN responder and restricts the bot's nominated ICE pair
to Ethernet. It runs the existing gates unchanged: five calibrated offline
turns, 50 reconnects with failure recovery, or the full 30-minute soak with
90 scheduled physical prompts and three recovery events. Options remain
RAM-only `--option name=value` settings. Each invocation retains raw logs,
checks the nominated path and stops audio/probes before returning.

For the full soak, `--memory-tail-seconds 90` adds observation time after
the original 30-minute, 90-prompt, three-fault schedule. It preserves the
five-minute settlement and ten-minute aggregate heap-fit requirements.
Settlement uses the actual host receive timestamp of each Ready transition;
the serial cursor advances so a long capture cannot expire its Ready lookup.
Short individual generation fits remain unknown; the common settled fit
must pass. The default tail is zero. Keep a failed qualification under its
original label and use a new label for a prescribed candidate bisect.

## Step 8d3 correction to the 8d2 pause interpretation

The production SmartTurn v3 silence bound is restored to Pipecat's **3 s**
default; `BOT_SMARTTURN_STOP_SECS` remains an experiment override. The previous
1 s pause success ended the turn early on “What is the capital”; the continuation
interrupted that generation 84–243 ms after LLM start, before the first TTS byte.
It did not show SmartTurn holding a complete utterance through the pause. Each
trace contains one INCOMPLETE model verdict plus a silence-timeout no-op, rather
than two INCOMPLETE verdicts. In the 3 s control VAD missed the continuation,
so the model never evaluated the complete question. At the corrected gain the
standard prompts yielded COMPLETE 130/130 times; the bound does not delay that
path. The firmware gate and AlwaysUserMuteStrategy are unchanged. Continuations
arriving during bot speech may be lost, so 1 s cannot be selected without the
predeclared pause sweep. New audit events distinguish `kind: model` from
`kind: stop_secs_timeout`; only model calls count as verdicts.

Step 8d3 uses `hil/step8d3_pause.py` to generate and WER-validate a separate pause
bank, then collect the 96-turn interleaved sweep. `step8d3_readout.py` exports
per-turn CSV and informational cell counts; it cannot select a policy. The latest
user addendum stopped pause collection and withdrew the automated rule.
`step8d3_final.py` now collects the loud/silence checks, exactly ten nominal
turns, five quiet turns and a fault-free twenty-minute soak on 20 ms, using
Pipecat SmartTurn/VAD defaults. It runs no further pause or alternate-bound arms. Passes
are never manufactured by dropping damaged or failed turns. `ethernet_bench.py`
owns the LAN STUN responder, restricts ICE to enp7s0, and verifies the selected
pairs through `server/ice_path.py`; no host routing changes are made.

`agent_silence_test.py --mode offline-test` and `--mode live-test` each record
120 s with gain read back from the board. `step8d3_loud.py` calibrates the
original France waveform at 75/80 dB estimated SPL using the temporary loud-only
Jabra PCM 11/11 (+8 dB) setting, retains the Mic setting, and restores standard
PCM 8/11 (−4 dB). It logs device saturation and decoded-input clipped counters
separately and checks STT. Native SPL remains a datasheet sensitivity estimate
(±1 dB tolerance), not a traceable sound-level-meter measurement. Host playback
clipping is refused at every calibration adjustment.

The immutable WER-0 pause bank is in `hil/fixtures/agent_pause/`. The manifest
records each zero-gap length, continuation offset, generated continuation style,
full-text STT validation, and WAV hash. Failed generation attempts remain only in
the step evidence. `step8d3_pause.py --bank hil/fixtures/agent_pause --out <new-dir>`
runs the sweep; `--winner 3` runs the one-per-cell winner repeat.

Offline/live speech modes now also default to Pipecat's 0.6 volume floor, per
the latest user instruction to keep VAD defaults. The earlier 0.4-floor silence
checks are preserved as historical evidence; final 20 ms checks repeat both
modes at the current default. `BOT_VAD_MIN_VOLUME` remains an explicit experiment
override. New archived production images verify the selected public uplink
default (20 ms), and keep the TX stack at 32768 bytes.

The current-default final sequence is complete: nominal 8/10, quiet 4/5 and
soak 47/60 strict passes. Its original failed results stay failed; the soak's
corrected resource gates all pass. `completed` distinguishes collected runs
from passing qualification. See the
[step 8d3 report](../proj-2026-10-08-0949/codex-jobs/step-8d3-report.md)
for the preserved e2e wrapper-path failure and separate 5/5 reconciliation.

Step 8b3 scoring uses board-mic correlation and raw board-mic STT for answer
content, with per-turn `rx-clear` / `dump-rx` proving decoded pre-DAC content
(reference correlation ≥0.95). Jabra correlation/STT are informational. Jabra
level requires rise ≥10 dB and frozen-FIR 1 kHz transferred board-mic SPL
historically within ±5 dB of the frozen 8b3 midpoint (73 dB at −25,
76 dB at −22). The current step-8f3 −22 window is 78 ±7 dB (71–85), with
nominal sensitivity and several dB uncertainty, not Jabra-position
or 1 m SPL. `step8b3_rescore.py` writes separate retained-capture comparisons
and keeps original verdicts/hashes; absent historical decoded dumps stay unknown.

Offline e2e remains credential-free for decoded-content and board-correlation
acceptance. When ASR credentials exist, the board-mic transcription extension
is also gated; otherwise `board_stt` is explicitly skipped/unknown. Full 8b3
bench qualification includes that extension (credentials present), and does
not count its absence as verified near-field intelligibility.

Answer STT uses complete matched reference durations at the saved microphone
offsets. Activity onset/end thresholds are timing diagnostics and must not
truncate low-energy final consonants. `reference_audio` rejects truncated
captures; corrected retained-audio scores keep original verdicts alongside.


Step 8e scoring correction: agent/e2e checks content, presence and coarse level
only. E2E/agent reply correlations are not an independent guarantee against
every clipping fault. The Jabra level gate requires room rise ≥10 dB and
±5 dB around the per-level 8b3 midpoint. Historical 60–85 dB / 3 dB rules above
are superseded. Sweep level error is unknown/ineligible when its legacy control
fails room rise; the contaminated control remains in retained evidence.

The firmware `LOUDNESS lookahead_ms` field is deprecated (legacy 2L collector
field). Use `limiter_attack_ms` and `limiter_delay_ms`.


Step 8e fault campaigns use `netem.Netem(board_ip, args, direction, state=path)`.
Every classifier matches the board IPv4 address; unmatched traffic uses the
unimpaired priority band. Installing the owned prio root temporarily replaces the default root (`noqueue` or `fq_codel`); unmatched traffic uses prio's default FIFO leaf, so its queue discipline changes during the capture. Exact original state is restored afterward. Existing non-default tc hierarchies are refused.
The durable state file journals each intended mutation before applying it;
normal exit and SIGINT/SIGTERM remove only journalled rules and assert exact
original qdisc/class/filter state on both interfaces. Emergency cleanup:
`hil/.venv/bin/python hil/netem.py --cleanup --state <absolute-state-path>`.
Only `/usr/sbin/tc` uses passwordless sudo. Do not run two campaigns together.

`step8e_burst.py` retains all three N=1/5/20/50 trials, including failures.
`step8e_soak.py` runs 30 actual voice turns and separately reports the strict
60 ms socket/pipeline continuity gate; ten minutes do not qualify a longer
heap-growth window. Its diagnostic trace records successful uplink UDP sends
(kind 12) as well as receive/owner boundaries. `STEP8E_TRACE=1` enables this
trace in the agent harness, and `STEP8E_EVENT_LOOP_PROBE=1` records server
scheduling gaps. Both default off. The mid-turn PERF request adds console
traffic and remains part of the measured choreography.

Receive-boundary correction: historical server `rtp.jsonl` records are taken
after the aiortc RTP handler, following ICE queueing and SRTP processing.
They do not directly measure raw socket receipt. New captures also retain
`socket-rtp.jsonl` at asyncio's UDP callback before ICE queueing, with public
header identities matched to authenticated RTP. Agent turn results retain
both boundaries and keep the existing strict RTP/pipeline verdict. The UDP
callback is a userspace observation; it does not measure kernel arrival.

The console RX recording contains samples returned by the transport, before
speaker DSP. It includes active PLC and silence frames, but an idle read
returning zero adds no samples. Thus 30 seconds of returned PCM can span a
longer wall-clock interval. Correlation/STT describe the sampled content;
RTP gaps, state transitions and trace counters describe outage duration.

`step8e_matrix.py` runs five-minute arms in both individual directions and
together, plus calibrated real STT/LLM/TTS turns for static faults and 1/3 s
outages. Long-outage arms retain continuous speech/recovery evidence. The
full campaign starts with a separate 60 s bidirectional blackout to measure
actual consent expiry beyond the required 30 s arm. State transitions and
temporarily unavailable PERF during reconnect remain explicit in each capture;
console timeouts still fail the arm. `step8e_setup.py` selects STUN, DTLS handshake
or encrypted SCTP application packets inside the same board address filter.
`step8e_mute.py` injects 300/800 ms loss during five voice replies per arm;
`--long-replies` requests slow counting and injects after three seconds at an
active speaking interval. `step8e_rejoin.py` records five physical WiFi rejoins
and their active/closed resources; it does not administer DHCP leases.
`STEP8E_DISABLE_BOT_MUTE=1` is its explicit diagnostic comparison. Production
keeps AlwaysUserMuteStrategy, SmartTurn/VAD defaults, 20 ms uplink and −22 dBFS
playback. Detailed per-arm metrics and every failed capture remain in the
step-8e evidence bundle; completion of a capture alone is not qualification.

### Repeated-fixture phase correction

Continuous offline fixture scoring retains every nonoverlapping source-length
returned-PCM block and measures normalized 500–1500 Hz correlation against the
cyclic fixture, allowing source phase independently in each block. A later
linear best match cannot establish initial phase after native drift drops or
idle reads. The 0.95 threshold is unchanged; whole missing blocks and damage
within blocks still fail. This is repeated-content evidence, not wall-clock
continuity or source-time alignment. Existing captures retain their original
linear-window verdicts; separate cyclic-content audits apply the correction
uniformly. Actual voice turns retain the complete linear-reference scorer.


The 2026-10-11 user decision sets production playback to **−22 dBFS with the 26% ceiling**. Limiter and compression settings are unchanged. The bounded Step 8e −19 diagnostic uses one cold and one warm program draw and one six-turn conversation; it stops on distortion/exceedance failure and restores −22. The finished 8b3 comparison is historical evidence and is not repeated.

The uplink Opus encoder defaults to FEC off with CBR; aiortc decodes without LBRR recovery.
The 30-minute qualification soak takes about 33 minutes including its 180-second memory tail.


Step 8e2b gates every hardware arm with a 30 s quiet-NIC check (up to ten
minutes waiting) and sysfs `wlp8s0` TX/RX deltas, each ≤20,000,000 bytes per
five minutes (scaled). In the Ethernet/STUN bench, media goes through
`enp7s0` to the mapped board endpoint `192.168.42.93`; owned WiFi class counts
are zero and this measures total host WiFi traffic. Direct-WiFi arms subtract
owned board-flow counts in each direction. Missing directional counters
invalidate accounting; preflight uses total TX/RX conservatively. An
impairment arm is invalid if its owned impaired direction carries no board
bytes. Step 8f2 retains each impairment period and checks whole-arm byte deltas
before restoration (100% blackout phases can legitimately transmit zero bytes). An Ethernet media gate matching the selected mapped
endpoint/UDP flow is proposed, not applied. No routing/interface settings or
other project processes are changed.

`playback-selftest`, `playback-pause`, and the self-test Opus fixture compile only
with `CONFIG_DG_PLAYBACK_BENCH=y` in a separate bench build. The default is off.
Self-test requires Disconnected or Initialized transport and no active tone;
the guard runs before parking playback or allocating benchmark decoders.
Production qualification images keep this option off.

The step-8f device-send target p99 ≤45 ms is a regression guard, with a 60 ms
ceiling. Step 8f2 gates raw cadence after declared fault/reconnect and
`link-backpressure` intervals, publishing the raw quantiles and all categories.
Backpressure uses kind-11 send_audio/dtls_write/control SlowCalls ≥40 ms
with `call-timing=1`, or device-stamped kind-7 `send_cutoff` drops with a
±250 ms pad corroborated by same-session PERF increments. Counters alone
create no windows. `control` also wraps local events/heap work, so it is not
purely link evidence. Any overlap with a backpressure window excuses the
whole interval; this is bounded by the local SlowCall/cutoff pads. Category
counts, union excused seconds and maxima
remain explicit. Ordinary owner iterations obey 40 ms on what remains.
Only `stats`/`wifi-stats` ±120 ms windows remain, informational; dumps create
no windows. Continuous (pc_id, SSRC) link intervals >60 ms may occupy at most
1%, and >150 ms at most 0.05%; maximum is informational up to the 2 s death
cap. Startup and declared fault/reconnect windows remain separate.

Impaired concealment conformance is reported separately. Drift inserts and latency trims count as edited, and decoded content aligns successive 100 ms blocks. Acoustic STT mismatch is informational when decoded and board correlations pass. Firmware now exports loop-sampled heap minima since the preceding PERF read. Analysis merges every intermediate PERF reset; missing coverage yields unknown. Legacy boot-cumulative minima and censoring headroom remain informational. `CONFIG_DG_PLAYBACK_BENCH` defaults off and gates playback self-test fixtures and deliberate pause commands; self-test also requires no active session.

Arrival kind 18 records validated native terminal state: `value` is `esp_peer_state_t`, `aux=0` is the state callback and `aux=1` is channel-close. These records revoke media admission and wake the playback pacer; starvation is not a terminal event.


Step 8f adds `step8f_campaign.py` (six adjacent A/B boot pairs, busy/quiet strata and a predeclared CPU/cadence adoption rule), `step8f_regression.py` (quiet-0/stats collision and opt-in sampler comparison), and `step8f_qualify.py` (offline 5/5, reconnect 50/50, sustained overflow, and 20-minute voice soak with a forced reconnect). Completed tails and acquisition failures remain in evidence; qualification proceeds through completed failing arms without selecting retakes.

LCD memory gates name their basis: active-media owner-loop window minimum internal free ≥96 KiB; boot-cumulative internal minimum ≥80 KiB after the reconnect soak; owner-sampled largest internal free block ≥32 KiB; failed internal allocation count zero from boot. DMA values accompany internal values. All PERF resets in the durable serial log participate. `window_minimum_free` includes connection/setup; `window_session_minimum_free` samples only while the channel admits media. These are sampled allocator observations, not allocation-event minima. A clean transient-depth failure takes precedence over disturbed/unknown windows. The callback counts requested capability classes (generic and DMA requests conservatively count internal); last size/caps are published as one coherent atomic pair. The IDF 64-bit atomic implementation uses its ISR-safe critical-section primitive; the callback itself contains only atomics and arithmetic.

Audio and owner diagnostics enqueue bounded 192-byte records without waiting; a priority-1 worker prints them. Full/truncated records increment `diagnostic_log_drops`. `heap-sample-compare 1` temporarily enables the former audio-loop sampling as a shadow measurement; normal operation uses owner sampling only, and cleanup turns comparison off. Sparse pipeline input-clock reports after aiortc jitter-buffer prefetch/decoding remain informational; damage flags use the link gate and retain missing coverage as unknown. Jabra level uses the reviewed ±7 dB window, with correlation and decoded checks retained. A baseline above −50 dBFS makes acoustic timing/rise unavailable while independent content and digital-silence checks remain active.


`step8f2_qualify.py --out <new-directory>` acquires quiet-0, offline 5/5 and
one 20-minute voice soak without retakes. It preserves per-interface counters
and stops the entire campaign on `ACOUSTIC HOLD: noise`. The forced reconnect
is scheduled at 360 s; the soak configuration asserts that a generation can
settle and provide a complete window. `step8f2_analysis.py` exports all gates,
raw/backpressure owner/send tails, the historical flow audit, and the Fable
owner residual comparison against both 8f and 8e2b.

Adopted LCD floors remain 96/80/32 KiB (98,304/81,920/32,768 B). Step 9's
whole display stack must cost ≤27,108 B internal, measured as the
boot-cumulative minimum drop. Staging is budgeted at 20 KiB, leaving 6,628 B
for driver/compositor objects; remeasure all floors and fragmentation with the
stack allocated. The diagnostic queue uses 128 × 192 B of PSRAM, and owner
largest-block walks run at 1 Hz with a PERF timer; free minima stay per loop.

Step 8f3 predeclares the −22 dBFS reply level window as **78 ±7 dB (71–85)**,
using the pooled 8f/8f2 transferred-level mean of 78.1 dB. This window is fixed
before step 9; per-turn measurements cannot move it. The −25 centre remains
73 dB. The matcher excludes reference blocks below −30 dB of segment RMS and
requires each excluded block at the running offset to have decoded power
≤−20 dB of decoded segment RMS. A violation contributes a zero-score failed
block. Within the 320-sample truncation tolerance, short aligned quiet blocks
are judged on samples present; empty blocks are marked `unavailable` and
excluded. Quiet blocks never steer alignment; voiced blocks move local alignment
only on correlations ≥0.5. Borderline pause tails can cross the −30 dB reference
threshold between references; insert/replacement
regressions remain. `fixtures/step8f3-tts-reference.wav` is the real retained
8f2 turn-1 TTS reference used by the 400 ms dither-pause regression.

Voice soaks include a connected 180 s memory tail, sampled every 30 s without
additional prompts. `--memory-tail-seconds` defaults to
`QUALIFICATION_MEMORY_TAIL_SECONDS` (180). Fault-free and forced-reconnect
qualification scheduling asserts
pooled settled time: duration + tail − 10 s per fault recovery −
(faults + 1) × 300 s settle ≥ 600 s fit + 120 s sampling margin. A 20-minute
arm with a six-minute reconnect and no tail is rejected (590 <720 s); with the
tail it passes (770 ≥720 s). The first step-9 voice qualification is the next
new voice soak. The three-fault transport soak skips the pooled growth
feasibility assertion because it makes no growth qualification claim;
20 minutes works with or without the tail. Its settle/window and fault-time
assertions still apply. RTP capture caps include both duration and memory tail.
The default tail fits inside the voice pipeline's 300 s idle timeout; a longer
tail needs a corresponding pipeline timeout configuration.

An adjacent host/device send interval is declared idle only if it is within
the acknowledged `audio 0` → `audio 1` window, with at most 100 ms before/after
for in-flight packets. RTP `seq_step` must be 1 when recorded. A boundary
violation stays eligible with its excess gap (including all excess on a side
that exceeds 100 ms); contained sequence loss stays eligible at
`max((seq_step-1)*20, timestamp_step/48)` ms. The device trace uses the sequence
estimate alone if timestamps are absent. Raw gaps and maxima remain available.

Fault-injection media arms run **without `ethernet_bench`**, on the direct-WiFi
path (`BOT_ICE_INTERFACE=wlp8s0`, `BOT_IMPAIRED_INTERFACE=wlp8s0`). At session
start, the route of each server nominated pair (`ip route get <remote> from
<local>`) must match the impaired interface, and
the matrix checks that record before injecting faults, including reconnects.
Owned bytes in every impaired direction must be ≥50% of the corresponding
server-recorded full RTP bytes when supplied (incoming UDP includes SRTP
overhead, conservatively raising that floor); without complete RTP byte evidence,
the enforced fallback is **100,000 bytes per minute of the whole arm**. Phase
and whole-arm verdicts are retained; a blackout phase can be empty when the
session deliberately stops sending, but the whole arm must meet the floor.
`loss 0%` accounting arms may still use the Ethernet bench. All owned tc state
must restore exactly even when validity fails.

### AEC campaign stimulus timers (prospective repair after campaign-10)

`echoear_aec_event_tail.py` provides bounded append-only JSONL readers. Construct
`TransportEvents(..., at_end=True)` and `ContextIdentity` **before** sending the
case request, then pass them to the scheduler. Capture error checks use indexed
`EventCache` case rows rather than reparsing or filtering the entire hold. An
incomplete final line waits for completion. Rotation, truncation, a rewritten
committed tail, or ambiguous requested identity fails closed; it never reuses an
old speech witness. Each poll consumes at most 256 KiB across its input paths.

`timer-<case>.json` (ordinary capture) / `timer.json` (reconnect) retain requested
offset/time, due, fired, callback/ALSA request time, actual invocation offset,
`actual_minus_requested_ms`, and wall/CPU/byte/row poll measurements. A difference
whose absolute value exceeds **100 ms** is `stimulus-timing`, a harness acquisition
failure. An already-late timer does not invoke; a callback whose retained request
time reveals lateness fails the cell. Ordinary cells use the existing bounded
harness recovery and shared attempt budget, with originals retained. Reconnect
expiry still stops without retry. No acoustic miss becomes a harness retake.

The reconnect target is the qualifying requested-context write inside the
unchanged **+1.0 to +1.5 s** window; its upper deadline and **120 ms** freshness are
also enforced. `requested_s` / `due_s` identify that target, while `deadline_s`
retains the window end. Ordinary due time is first non-silent write + declared
offset. Clocks are sampled after reads and immediately before invoking. Python
is not a hard real-time scheduler: retained invocation times enforce the gate
when host scheduling slips. `capture-polls.json` records capture-reader cost.
Analysis rows expose near-onset bracket widths both with and without onset
uncertainty; scoring continues to use the existing conservative bounds.

An acceptance-boundary `wifi-stats` response without driver payload is retained
with `payload_present=false` and a **WARNING**, including ACK-only responses.
This observation does not change acceptance or retry gates.

This is a host-only implementation, not a freeze or authorization to launch.
Existing frozen declarations and c09/c10 evidence remain unchanged. A subsequent
reviewed freeze must document the user-decided 100 ms gate and harness class and
bind the new source hashes. For read-only diagnostics, run
`echoear_aec_timer_replay.py --root <step-9-evidence> --out <new-directory>`;
virtual-time selections never grant qualification credit. The real-clock tests
in `test_aec_event_tail.py` cover 0/90 MB history with concurrent writes, expiry,
partial lines, wrong identities, cancellation, and delayed reads/callbacks.

The capture error check also inspects the cache's case-less key for the same
alarm/error events as settle/finish; those rows do not participate in the current
case's speaking/stop timing. `EventCache.case_rows(None)` snapshots that key
without another poll, while `read(None)` still means all retained events.
`BenchHold.recorded()` uses a persistent tail-backed cache for session logs,
including new sessions and completed partial lines. It decodes each appended row
once. Returning the full list and downstream audits still scale with row count;
retained cache memory grows with the hold. Changed/disappearing files fail closed.
