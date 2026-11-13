# Campaign-07 freeze v8.3

Coordinator decisions18 pauses diag-01 and prospectively separates downlink
playback quality from echo acceptance. Commit 5bbb4d9 remains; the uncommitted
decisions17 diagnostic work is preserved in stash
`b6f55df8e5eb27d078169d6b235f6165c663c423` and was never launched.

The explicit `echo-transport-v8.3` policy requires
`(lost_delta + playout_dropped_delta) / received_delta <= 0.02`, with the same
conservative numerator and boundary checks. Missing-run burst length is
report-only. The six ended-run bucket deltas, finished-slot lower bound,
conservative open remainder and burst upper bound remain in every transport
record and in `analysis.json`'s `summary.transport_burst_summary`. Summary
windows overlap (including paired cells and epoch intervals); they are listed,
never summed. A positive >=6 bucket delta, post-PLC zeros delta or starvation
delta still fails. Missing or invalid histograms fail closed. Cumulative
counters are differenced within one ordered generation/peer window.

The AEC reference is the actual DAC output including PLC, so concealed runs do
not bias echo/barge-in metrics. Post-PLC silence would and remains a hard gate.
The PLAN no-burst>3 playback rule moves to separate downlink-quality work
(paused diag-01 / jitter depth), outside echo acceptance. This does not grant
retrospective acceptance to any previous campaign.

Runner acquisition and recovery, analyzer cells and non-probe epoch intervals
select this policy from the declaration. The 240-second probe retains its own
rule; reconnects retain their zero-loss checks. All resource, admission, RF,
delivery, I2S, liveness, host, noise, acoustic, barge-in, spare, cap, callback
witness and Stop 1.2-second checks are unchanged. Environmental classification
(including its existing F1b exception), retake budgets and clean recovery rules
are unchanged. The retransmission-aware pacing predicate remains draft-only.
The unversioned transport function retains exact v8.2 replay behavior.

The unchanged candidate ELF is
`61aa38d58559abe87bc54397fa8df0cec2411c1d37070d809b98d9d4e3b17242`;
there is no firmware rebuild or board check during this freeze.
`hil/freeze_aec_campaign_v8_3.py` creates fresh `step-9/freeze-v8.3` for absent
output `step-9/campaign-07`, binding code, review decisions, candidate and
fixtures. It emits the launch script, anchored Python-child stop pattern and
separate live guard whose final runner exec is replaced by echo. Only that
read-only guard runs during preparation. The coordinator launches about 04:15
America/Los_Angeles after Fable APPROVE. No hold may span 03:00–04:10; the
coordinator schedules the unchanged 206-minute campaign outside that interval.
Per-hold flock and signal-driven cleanup remain: restore/hash-verify 9936da45,
restore Jabra mixer and release flock. Never signal wrappers or force-unlock.

Stop-10 remains a qualification risk, not a demonstrated absent reply. The
campaign-06 second reply's frozen 250-ms source template is a weak precursor:
full-band -53.95 dBFS, high-passed -68.84 dBFS; corresponding MIC3 is -94.06
dBFS. Its correlation is 0.04805 versus alternate 0.04722. Searching the entire
capture does not rescue that prefix. Later source prefixes match strongly
(-0.699 to -0.881) and agree on source origin within two samples, including in
the original search window. They establish reply presence, not playback of the
missing precursor or an acceptance latency for it. The matcher is unchanged.
If the same weak opening recurs, the frozen next-playback gate will fail final
analysis; the runner does not immediately stop acquisition on this offline
matcher result. One retained waveform cannot estimate the recurrence rate of
new Flux synthesis, so there is no evidence that this alone is likely to stop
campaign-07 acquisition, nor evidence to promise qualification. This requires
separate reviewed matcher work if it recurs.
