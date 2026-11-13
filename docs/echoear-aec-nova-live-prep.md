# Nova live v8.6 preparation

## Final short-session freeze (decisions 25–30)

`hil/echoear_aec_nova_live.py prepare` creates the fresh
`step-9/freeze-v8.6-nova-final` bundle; the historical blocked bundle below is
unchanged. Preparation never runs the bench. The new runner binds the e32eaff
rejudged combined pre-step file and both runs' retained files, including the
original runner analyses. Mutable user-decision files are not launch inputs.

The 39 nominal captures retain three separate ten-eligible groups (firm Stop,
firm sentence, normal sentence), each requiring nine successes, and conditional
opportunity spares 7/6/6. Existing noise/harness/long-TTS retry, resource, transport,
observer, RF, source/opportunity, recognition, flush and cleanup rules remain.
There is one four-minute link probe, one candidate boot, and no reconnect/soak.
Normal sentence04 remains gated. The local retained sentence04 regression also
tests that the current boundary adapter and budget yield one complete turn.

The 0.7-second paused sentence is report-only. Each accepted turn, request,
transcript segment and timing event remains visible; a canceled prefix with no
PCM cannot be lost by combining turns. Its application generation findings do not
gate, replenish groups or consume/create opportunity spares. Global connection,
observer, resource, transport and cleanup integrity still applies to every capture.
Host interruption physical timing and preservation remain report-only. Ordinary
normal single-word Stops remain report-only and never replenish a gated group.

The accepted nominal estimate remains approximately 46 minutes. The active alarm
is 3240 seconds, with 360 seconds reserved for restore and a 3600-second total cap.
An incomplete matrix or an overrun cannot pass. Restore is never interrupted by
the timer or repeat Ctrl-C/TERM; failed restore retains the lock, even past the cap.
Fresh Nova observations (three complete intended replies, maximum PCM duration
9.36 seconds) are descriptive, not a population p99 or a duration guarantee.
Interrupted winding prefixes remain censored; per-attempt source/opportunity
checks and the 24/28-second capture deadlines are unchanged.

Run only from an interactive user terminal, using the exact generated
`launch-command.txt`, after review and explicit user go. Both shell and Python
require `freeze-v8.6-nova-final/USER-GO` containing the reviewed plan SHA-256.
Preparation does not create that file. The shell refuses before uv/environment
queries when the marker or review hash is absent. The Python path checks the
marker before validating inputs or touching output, NIC, lock or hardware.
The live profile and observed vendor UUID/version must match at connection,
captures, final audit and scoring. Normal restore is 9936da45; candidate is
61aa38d5, gain 15, guard 400, cat, dev Nova, bot port 7861.

The live manifest excludes only the three explicitly named standalone offline
turn-sweep modules/tests, which the runner never imports. That permits the later
report-only sweep without changing this freeze or the live profile.

## Historical blocked handoff

This is a host-only blocked handoff, not an authorization or a completed freeze.
The existing v8.5 runner requires its full matrix, reconnects and soak. A shortened
Nova declaration must not bypass those checks or claim full firmware qualification.
The final short-session integration must preserve all per-cell firmware/resource,
transport, observer, timing and cleanup checks while using the new explicit matrix.

The proposed single hold has ten eligible firm Stops, ten firm sentences and ten
normal sentences at −6 dB. Each group independently requires at least nine
successes, using the existing opportunity, recognition, latency, flush, next-playback
and integrity rules. Opportunity spares are 7/6/6 respectively; scores never select
retakes. Two normal Stops are report-only. Four echo replies (two each at −6/0 dB)
require zero accepted turns and zero interruptions, with existing acoustic gates.
Two host-interrupt cells at 1.0/1.5 seconds from non-silent TTS write report physical
timing without recognition credit. Preservation has no approved tolerance and
remains report-only. A separate long-sentence/internal-pause control cannot fill
any of the three ten-case denominators. No reconnect or soak is proposed.

The profile binding includes effective dev settings, config SHA-256, installed
versions and local model hashes, reviewed boundary-processor hash, event fields,
retained vendor model/version/UUID and its provenance. The vendor must be observed
live; the requested alias does not pin cloud weights. NOVA_TTFS_P99_S=1.0 is a
chosen safety budget, not a measured p99. Missing/changed profile or vendor evidence
must block credit. Code and fixture manifests bind all retained inputs.

A fresh bot/reply preflight is required: the old Flux observation has a different
whole-bot code hash. It must not be relabelled as Nova or updated by changing a hash.
The coordinator pre-step must measure real replies, final-segment/turn boundaries,
TTS onset/completion, capture/export duration, and retain restored-normal-image,
mixer and lock witnesses. No pre-step is launched by this preparation.

The long single sentence with internal pause still needs an exact fixture, expected
text, pause timing and SHA-256. Existing sentence fixtures last only 1.56–2.20 seconds;
they are not silently substituted for the requested long sentence. Existing replay
regression evidence remains useful but is not live validation.

The executable launch stub exits 2 before environment queries. A non-executable
launcher template preserves ESPTOOL_PYTHON, the uppercase-safe active-profile/BSSID
guards, hash/output binding and the proposed Python-child stop anchor. The final
freeze must regenerate the declaration, launcher and anchor after resolving the
preflight/fixture inputs and completing short-runner validation. Never edit the
blocked bundle in place or execute its template as a campaign.
