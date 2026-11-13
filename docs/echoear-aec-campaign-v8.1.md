# Campaign-05 freeze v8.1

Coordinator decisions14 adopts Fable's freeze-v8 review section 2 amendment.
This is host-only. Campaign-05 is NOT EXECUTED; a separate bench GO is required.
The original freeze-v8 bundle remains preserved.

Only callback activity rule (a) changes. `pre-callback-active-written-tail-v2`
requires an active, gap-free block of the old reference whose upper device bound
is within 300 ms before callback: `callback - 300000 <= upper < callback`.
Select the last such active block, allowing a later phonetic lull. Analysis saves
the selected block, its upper-bound age and the declared window. The v8 single
block rule remains available when analyzing its original declaration.

The aligned old context must still have a complete active source frame beyond
the physical cut covered by successful non-silent transport writes by callback
host upper bound. Natural end, wrong context, late/silent/dropped writes and
gap-only evidence still fail. Callback-to-last-MIC3 <=100 ms, server-to-silence
<=250 ms, zero stale frames and both interruption copies remain required.

All other v8 contracts remain unchanged, including Stop 1.2 s / sentence 2 s,
independent 9/10 gates, near-onset eligibility, four Stop spares per gated voice
block, two sentence spares, 206-minute estimate, duration calibration, retry
accounting, host/periodicity checks, firmware, bot and restoration requirements.

The retained campaign-04 replay is report-only with no acceptance credit. Expect
8/8 eligible Stops and 2/2 eligible sentences to pass the prospective predicate;
the incomplete campaign remains unqualified. Stop-03/05 remain unscorable at
near onset. No original result or declaration is rewritten.

Use `hil/freeze_aec_campaign_v8_1.py` from a clean committed tree to create fresh
`step-9/freeze-v8.1/` with absent `step-9/campaign-05`. Launch includes exports,
declaration hash/output checks, live host guard and final exec. Only the guard
check script, with final exec replaced by echo, runs during this freeze. Anchored
pgrep stop instructions identify the exact Python child for SIGTERM and verified
cleanup. Never signal wrappers, SIGKILL or force-release the bench lock.
