# Campaign-06 freeze v8.2

Coordinator decisions15 selects the firmware missing-run histogram recommended
by Fable's campaign-05 review section 5. Campaign-06 is NOT EXECUTED; Fable reviews
this freeze, and the user gives a separate launch GO. Freeze-v8.1 and campaign-05
remain intact. All v8.1 contracts remain unchanged except the burst witness and
its new candidate image. No performance retake, PLC-coverage gate or arrival
trace is introduced.

`playout.missing_run_ended` contains six cumulative uint64 counters for ended
missing runs of 1, 2, 3, 4, 5 and >=6 slots. Successful decode, invalidation and
device retirement close an open run once. Stream resets preserve the counters.
The field extends the existing stats snapshot, without new console transactions
or media-path allocations.

Analysis requires same-generation, same-peer, increasing-device-time boundaries
and nonnegative bucket deltas. A positive >=6 delta fails. Let
`finished = sum(k * delta[k])` for buckets 1 through 6 (6 is a lower bound),
`open = max(0, lost_delta - finished)`, and
`burst = max(largest ended bucket, open)`. Require burst <=3 and the unchanged
`(lost_delta + playout_dropped_delta) / received_delta <=0.005`.
An ended run spanning the first boundary counts whole, conservatively. Missing
histograms retain the conservative total-drop bound; malformed or incomplete
histograms fail closed. Lifetime absolute maxima are never window witnesses.
This measures run length, not the time or speech exposure of each missing slot.

Tests cover four singles passing, a four-run failing, a three-run plus single
passing, open four failing, >=6 failing, absent/malformed/decreasing counters,
generation/peer mismatch and reversed snapshots. Campaign-05's retained v8.1
window still fails as device-fault; no historical result receives acceptance
credit or a new classification.

The target-compiler comparison records +48 B persistent internal heap, unchanged
pull/finish stack frames (80/32 B), +160 B in the diagnostics snapshot frame and
+16 B in disconnect. The encoder object is byte-identical. These are static
measurements, not runtime high-water or encode/CPU measurements. The review-build
record binds the detailed measurements and the clean-config candidate. Existing
stack, heap, encode and CPU gates remain required during the future campaign.
The board codec selftest was not run; its shared histogram accounting test runs
in the native suite. The candidate has playback bench mode disabled.

Use `hil/freeze_aec_campaign_v8_2.py` from a clean committed tree to create fresh
`step-9/freeze-v8.2/` with absent `step-9/campaign-06`. The declaration binds the
candidate, review-build record, analyzer, firmware source, fixtures and code.
Launch retains exports, declaration digest/output checks, BSSID lock, live host
guard and final exec. The host boot ID remains bound to the periodicity policy.
No hold may cross 03:00–04:10 local time; the coordinator must schedule the
unchanged 206-minute campaign accordingly. Only the separate host guard script,
with final exec replaced by echo, is run during this freeze.

Anchored pgrep instructions identify the Python child for SIGTERM and verified
cleanup. Wait for restored ELF 9936da45…, mixer equality and lock release; never
signal wrappers, SIGKILL or force-release the bench lock. The next authorized
hold verifies candidate boot gain 15 dB and restores the normal image afterward.
