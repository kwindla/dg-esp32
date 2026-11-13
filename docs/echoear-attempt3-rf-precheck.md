# Prospective strict attempt3 RF eligibility

Claude authorized one later strict re-acquisition after the completed LIVE
originals2–5. Those four replies passed, but the LIVE aggregate remains FAIL:
original1 level, continuation uplink1.828%, and original2 late3. Strict
attempt2 and quiet-offline attempt2 also remain FAIL. No prior result is
replaced or counted toward attempt3.

The machine-readable declaration is
[echoear-attempt3-rf-precheck.json](../hil/plans/echoear-attempt3-rf-precheck.json).
It retains normal ELF9936da45, the reviewed OLD-image-only geometry band,
all existing fixtures/bot settings, and all strict gates. Attempt3 contains
exactly three original turns on one peer, no retakes, all three required.
Its uplink limits remain >60ms rate≤1%, >150ms rate≤0.05%, maximum≤2000ms.
Room floors≤−55dBFS, wlp8s0 eligibility, full-turn transport, clips, unprotected
headroom, display/render/FPS, memory, stack and raw error gates remain intact.
A failed or interrupted scored population is never restarted as a clean one.

A's frozen old/new/old comparison measured1.499/2.220/3.281% receive tails
with no source-send gaps above60ms. This establishes no observed9936 send
cadence regression in those measurements. The precise lower-path cause
remains unresolved among post-send device/network/host delivery stages.
The prospective gate selects a measured clean interval; passing it cannot
prove an RF cause or estimate reliability in an uncontrolled environment.

Before the ONE scored campaign, take a fixed60-second quiet uplink probe
following the greeting, mic-gate reopening and six seconds of settled
continuous uplink. Keep cat30fps, quiet1, audio1, volume0, AEC off and the same
1100-byte SCTP chunks. Here idle means no speech/playback, with capture and
uplink enabled. Retain the original receiver packet file, endpoint margins,
capture status, full serial/audio-state ledger, bot events and NIC counters.
Take PERF snapshots only before and after; no exports or full console dump
inside the window. Freeze boundaries before inspecting gaps. There are no
startup, disabled-audio, console or fault exclusions in this probe.

Admission requires full endpoint coverage, one peer/SSRC, no truncation,
at least2850 adjacent intervals validating nominal50Hz continuity, no speech
or session interruption, and a tail rate **strictly below0.5%**. Compare
`over60_count * 200 < eligible_intervals`; exactly0.5% fails without rounding.
Report maxima and extreme tails separately. Existing raw/resource/display/
identity/cleanup checks must also pass. A hardware/raw/resource/cleanup
failure is fatal and cannot be relabelled as an RF deferral.

One initial probe plus at most four retries gives five opportunities.
For each failed or unavailable eligibility observation, append a deferral
with UTC and monotonic clocks, raw paths/hashes, identity, fixed boundaries,
numerator/denominator/rate, health/NIC/status evidence, reasons and
`scored=false`. Release the board, services and bench lock; wait at least
1800seconds after that probe ends before retrying. Persist the deadline
across launcher restarts. Incomplete/crashed evidence is retained and stops
automatic continuation. Stop after five unsuccessful probes, with the
campaign unstarted. Once attempt3 starts, this retry schedule ends regardless
of its outcome; it never creates attempt4 or repeats a scored original.

After admission, start attempt3 immediately in the same short bench hold.
Separate probe and campaign outputs so no probe packet contributes to scored
results. If the image, hold or environment must change before launch, retain
an unstarted result rather than reuse admission silently. The bench remains
available to A between deferred probes; no queued lock is held while waiting.

[echoear_rf_precheck.py](../hil/echoear_rf_precheck.py) provides host-only raw
packet arithmetic and persisted-ledger scheduling. Its tests cover real
steady/burst traces, strict boundary handling, missing coverage, speech and
audio-state invalidity, retry timing, fatal failures and no scored replay.
This is a predeclaration, with no serial/service/scored execution entry point.
The eventual collector and launch integration must receive Astra review
against this protocol before use, and all inputs/prior failures must be frozen
in an evidence-only binding. Astra independently audits any completed campaign.
No firmware changes or new hardware measurements accompany this declaration.
