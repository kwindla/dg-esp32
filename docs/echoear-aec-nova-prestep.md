# Nova six-capture pre-step

Preparation is file-only. `echoear_aec_nova_prestep.py prepare --destination …`
requires committed source and a fresh destination. It does not launch a bot,
query the NIC, open serial, take the bench lock, or make vendor requests.
The existing blocked `freeze-v8.6-nova` bundle is immutable. The separate pre-step
is not acceptance or a main-campaign launch. Coordinator review/go is required.

One candidate boot (61aa38d5, gain 15, guard 400, cat), one connection, no retries:
red-kite echo −6 and 0 dB; firm Stop +0.9 s; firm sentence01 +1.2 s;
normal sentence04 +1.5 s; derived paused sentence04 while initially silent.
The three interruption offsets use the first non-silent transport write and the
unchanged active-source/invocation checks. Real bot replies must finish inside
the existing 28 s capture cap, with 2.3 s quiet tail. The paused control uses the
existing `soak-near` listening protocol, which waits for the real reply (it does
not use the old six-second near-only exit). No link probe or extra scored capture.

The generator inserts exactly 33,600 zero-valued 48 kHz samples into the calibrated
sentence04. It chooses the lowest-RMS complete 5 ms bin at least 0.6 s from each
end, then the nearest-to-zero sample in that bin. The manifest gives its exact
sample index, envelope witness, source/output/generator hashes and unchanged text.
The low-energy gap is consistent with “change / the”; lexical alignment is
approximate. This preserves source speech sample bytes and level calibration.
It changes the sentence duration, not its words; no new recording or synthesized
speech is used.

Both shell and Python require the reviewed plan digest; shell additionally pins
the emitted digest. Python validates all runtime/fixture/input/package/model
hashes before `check_esptool`, active UUID/BSSID guard and the runner-owned flock.
The launcher exports the flash interpreter. All BSSID comparison uses the
existing case-insensitive host guard. The owned bot binds 7861 and explicitly
selects dev. Port 7860 is never queried or managed. Candidate boot, five-second
RF pre-gates, settled noise, mixer save/byte comparison, admission/resources,
transport, observer, periodicity and liveness gates remain in force.

The pre-step active alarm is 840 s, reserving 360 s of a 1200 s total budget for
normal 9936da45 restoration/boot verification and mixer restoration. Export
timeouts are bounded by remaining active time; once exhausted, export yields to
restore. A second alarm records total-budget exhaustion during cleanup without
interrupting restoration. Expiry is never a pass. Restore/mixer/cleanup failure
parks with the lock. A parked failed restore can exceed the cap; this is a safety
exception, never successful completion. Repeated stop signals cannot kill cleanup.

Decision 25 accepts the ~45.9 minute estimate for the later main validation.
The supplemental main-session contract has a 3600 s total budget, 3240 s active
cutoff and 360 s restore reserve. Three separate groups still require 10 eligible
turns and ≥9 successes each; 7/6/6 spares are usable only under their declared
conditions. Thresholds, report-only controls and denominators are unchanged.
This prep does not implement or authorize the still-blocked main-session runner;
its final integration must use and test that cap before a separate freeze/go.

The recorder retains each provider transcript result (including word times)
before the boundary processor, and LLM start/text/end frames. Existing evaluation
and session logs retain turn strategy, Nova boundary, vendor model, context and
generation, PCM, transport-write and BotStarted/Stopped witnesses. This adds
observation only. The Nova metadata adapter also retains streaming `model_uuid`
(the existing observer omitted it). Reviewed effective settings, package and model
hashes stay unchanged. The policy code hash and composite profile digest therefore
change and are freshly bound in this pre-step; the old digest remains provenance.

Post-cleanup file-only analysis emits `nova-preflight.json`, binding all retained
inputs and runtime/profile/packages. It lists every accepted turn separately,
requires one complete expected turn/request for each speech fixture, checks
trailing final segments and complete reply/quiet tail, and reports incomplete
acquisitions explicitly. Summaries use count/median/maximum from complete captures,
never a six-observation population p99. The 1 s transcript budget remains chosen.
The unchanged continuity metric is reported for every retained context; interrupted
winding-path prefixes are censored and cannot establish a complete ≥4 s source.
Final source qualification must use complete evidence and review, not a fabricated
extension of an interrupted prefix. This pre-step cannot automatically authorize
the final Nova freeze.

Stop only the exact Python runner PID with TERM (not uv/nohup, SIGKILL or an outer
flock). Wait for normal ELF, mixer_equal=true, lock_released=true and no cleanup
errors. On restore failure leave the parked owner/lock intact for the coordinator.

## Pre-step 01 partial analysis and completion retake

Decision 27 permits retaining captures 1–4 of the specific, cleanly restored
pre-step 01 abort. `echoear_aec_nova_completion.py analyze --out …` performs only
file reads and writes its analysis outside the retained run. It recomputes
acquisition gates from retained witnesses and records each supported component.
The whole hold remains incomplete. Stop has a complete transcript/turn/reply but
no VAD start/stop, so its VAD-relative delays remain unavailable. The failed normal
capture remains visible with both final segments and its duplicated “cook cooking”
turn; its transport failure excludes every measurement from qualified summaries.
The paused fixture never ran. Interrupted source prefixes remain censored.

After committing source, `echoear_aec_nova_completion.py prepare --destination …
--prior-analysis …` prepares the separate `nova-prestep-02` plan. It acquires only
one conditioning echo at −6 dB, normal sentence04 at +1.5 s with real reply, and
the unchanged paused fixture with board initially silent. The echo warms AEC after
a fresh candidate boot; it cannot recreate the exact prior adaptive history and
earns no measurement credit. All original safety gates, images, budgets and
no-retry behavior are reused by the existing runner.

Neither live decision log is hashed or opened at launch. The committed code fixes
decision 27's scope; the old plan is bound as historical bytes without recursively
validating its embedded mutable decision hash. The new plan binds the full run-01
file manifest and partial analysis. Post-cleanup `combined-nova-preflight.json`
binds both runs, fixes the first four slots to run 01 and the last two to run 02,
and keeps the failed old normal and conditioning echo separate. New measurements
require a completed run-02 hold. No score selection, substitution, automatic retry,
denominator change, population p99 estimate or main-freeze authorization results.

Decision 28/29's post-run correction keys run-02 usability on the completed
acquisition contract, accepted schedule and recomputed acquisition gates. Split
turns, echo false turns and reply metrics interrupted by a subsequent turn remain
measurement findings; they cannot discard an otherwise valid normal capture.
Strict measurement completeness stays separate. The full generation audit is
retained, including contexts canceled before any PCM write, rather than used as
an acquisition exclusion. `echoear_aec_nova_prestep02_analysis.py --destination …`
writes a new combined analysis and exact retained-event timeline outside both run
directories. It binds original runner analyses too, keeps unavailable Smart Turn
probabilities/call-entry times null, and never changes the live profile.
