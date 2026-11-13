# Campaign-05 prospective freeze (v8)

Coordinator decisions13 Part B authorizes host-only fixes and user-approved
acceptance changes. Campaign-05 is NOT EXECUTED. A separate bench GO is required.
The candidate ELF, firmware, bot policy, host guard, F1b recovery, periodicity,
primary cells, noise gate, near-onset eligibility, 9/10 rule and restoration
remain as v7. Historical acquisitions retain their declarations and credit.

Retry admission uses attempt_started_ns through the timestamp immediately before
the retry stats transaction, minus 200 ms transit allowance. Both endpoints and
the allowance are retained. Periodicity parsing runs after this endpoint, outside
the counted interval; no +/-1 count tolerance. Normal capture accounting remains
inside its device interval. Device received/dispatched and drop checks still apply.

Jabra wall-duration error is inclusively [-0.10,+0.25] seconds. Calibration binds
33 unique step-8 timers (three above +0.1s) and 12 campaign-04 timers. Failed
campaign-04 values were not saved and are not invented. Every future successful
play result is now saved before checking its duration; rejection includes numeric
values. Independent waveform/onset witnesses remain mandatory. This timer alone
cannot prove a fixture was audible.

JSON writes normalize NumPy scalars, still reject NaN/unknown objects, and the
stale-frame count is a native int. Final analysis failure records both errors
and preserves a pending acquisition/cleanup exception. TTS metrics pair only
ProactiveConnectionTTS processor/context/model; LLM TTFB cannot fail completeness.

Stop accepted-interruption upper bound is 1.2s; sentences remain 2s. User rationale:
23 retained finite Stop bounds, max1.095157s; Fable's audit attributes variance to
Flux's turn decision with a stable ~0.3-0.45s device-plus-transport stage. Always
report full intervals. Do not convert historical diagnostics into credit.

Callback witness is pre-callback-active-written-tail-v1: the last block
whose upper device bound precedes callback must be gap-free and active (do not skip a later
inactive/gap block). A complete active source frame beyond the last old MIC3
sample must be covered by successful non-silent writes of that same old context
by callback host upper bound. Alignment and full source-write coverage are required;
unwritten future PCM alone and natural source ending cannot pass. Legacy
all-overlapping-block activity is diagnostic. Callback100ms, server250ms, zero
stale frames after150ms mute and both interruption copies remain mandatory.

Four Stop spares per gated -6dB voice block (firm/normal); two sentence spares and
report-only0dB two-per-kind spares unchanged. Extra IDs03/04 use offsets1.8/2.4s.
No performance retakes. Four extra possible captures add6min at90s each:206min
(~3.43h) planning, before noise/recovery overhead.

Freeze via hil/freeze_aec_campaign_v8.py after committing. The bundle binds code,
calibration, fixtures, source preflight and review decisions. Launch includes env
exports, declaration hash check, existing/dangling-output refusal, live host guard,
and exec. Only check-live-host-guard.sh runs during freeze; its final exec is an
echo. Stop instructions use anchored pgrep on the frozen Python child. SIGTERM
allows cleanup; never signal wrappers, SIGKILL or force-release flock.
