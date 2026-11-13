# v8.5 prospective campaign 10

User decisions of 2026-10-18; host implementation on `aec-v85`. Candidate ELF remains
`61aa38d58559abe87bc54397fa8df0cec2411c1d37070d809b98d9d4e3b17242`.
No rebuild, default change, launch or retrospective qualification is authorized by freezing.
The evidence-paired `aec-demo` branch remains at `2f674e8`.

One restorable hold: one candidate flash, one idle link probe, one final normal-image
9936da45 hash verification and mixer restore, then flock release. The gated −6 dB
firm block contains ten echo replies, ten eligible Stop and ten eligible sentence
opportunities (nine successes per kind), seven/six opportunity spares, four near-only
controls, one silent cancel, two reconnect cycles and one canonical probe. Three
0 dB echo replies and five normal-voice Stop/sentence acquisitions per kind are
report-only. Device, transport, observer and liveness failures remain hard even in
report-only exposure. The existing 600-second display-active soak follows in the
same hold, with no reflash. Canonical excitation remains after transport disconnect.

The exact reviewed `ongoing-reply-lull-v84` classifier is adopted: measured span at
most five frames, horizon L+120 ms, origin tolerance two samples; cancelled count
must be zero. Strict and lull strata are reported separately. Firm strict+lull
opportunities jointly fill ten per kind; report-only normal voice never supplies a
firm denominator. No sentence-overlap redefinition is adopted.

## Boundaries and transport

A single explicit PERF read follows capture stop, before export. Cell transport and
delivery use `[before, capture-end]`: the conservative `(lost + playout_dropped) /
received <= 0.02` gate is unconditional, including cells under 1,500 received.
`[capture-end, after]` is export: loss, ended ≥6 runs, post-PLC zeros, starvation
and delivery counters are reported as disjoint event intervals and events/hour;
they are excluded from every scored transport/delivery denominator. Resources,
admission, observer health, peer close and liveness alarms remain hard everywhere.
Raw whole-epoch diagnostics are retained without reintroducing export loss as a gate.

Only non-cell/non-export piecewise intervals accumulate forward within the same
peer epoch until 1,500 received. An epoch-end residual merges backward into its
last qualifying aggregate; an entire piecewise epoch below 1,500 reports its ratio
without gating it. Every original scored interval still gates ended ≥6 runs, zeros,
starvation and delivery. Missing actual capture-end snapshots cannot be interpolated.
Retired reconnect PERF closes the old cell; the fresh new epoch's strict zero-loss
checks end at capture-end. No cross-epoch subtraction.

This is the **coordinator interpretation of user decisions 3–5, 2026-10-18**.

## Retakes and reconnect lifecycle

A complete TTS generation with lead ≥0.8 seconds can admit at most two retakes
campaign-wide, each also consuming the existing shared two-retry lifetime budget
of its cell. Retake the whole quick pair, both scripted/real response generations
of a barge-in, or whole reconnect cycle. Retain all original evidence. Device,
transport, liveness, admission and observer witnesses must be clean; both reconnect
epochs must be clean. Incomplete generation evidence or any unrelated hard failure
stops. The analyzer rechecks the journal against retained events and integrity records.
Long-TTS retries are neutral for the consecutive-noise streak. Soak retakes run only
after the original 600-second schedule, without shifting its original offsets.
No link-event retake or liveness relaxation is adopted.

Reconnect disconnects at the first fresh non-silent transport write in onset
+1.0…+1.5 seconds, bound to the requested context and peer, no older than 120 ms.
The latest write must still be speech. Missing/ambiguous/stale evidence stores a
skip witness before a `stimulus-precondition` stop, without retry. Exception cleanup
seals the actual open reconnect case before health. Boundaries never reached are
reported as such; attempted boundaries without evidence still fail.

Coordinator answer 3 authorizes waiting for the requested new reply's
BotStoppedSpeaking and last transport write, then 2.3 seconds quiet, within the
unchanged 28-second capture cap. This replaces the obsolete eight-second wait;
it is an acquisition fix, with no threshold change. A cap hit is a capture-deadline
failure, never a pass; absent clean evidence for both epochs cannot permit a retry.

## Host and planning envelope

Every boundary checks active NetworkManager profile `daily-633`, UUID
`fb56e504-5ac1-4e83-8e79-1a9884889dbd`, and that profile's BSSID lock
`b4:20:46:f4:ea:e5`, plus current association, signal and supplicant continuity.
The guard only reads network state. Existing uplink periodicity and host boot checks
remain unchanged. Snapshot intent is persisted before the query.

Nominal estimate: 45 ordinary capture groups ×65 seconds + two reconnects ×90 +
45-second probe +600-second soak +240-second flash/restore +300-second link probe
=4,290 seconds (71.5 minutes). Including all shared retries and opportunity spares,
213 attempts at a conservative 283-second acquisition/export/recovery allowance,
plus fixed overhead, yields 61,902 seconds (17.2 hours). This is a planning envelope,
not a hard deadline: analysis, host I/O and failed-restore lock retention are unbounded.
The per-association RTP/downlink/SCTP cap is 6,200,000 records (100 records/second
rounded upward over that envelope); truncated/overflowed observer evidence still
fails. Some receiver lists retain entries in memory; there is no drained-ring claim.

Freeze with `hil/freeze_aec_campaign_v8_5.py` into `step-9/freeze-v8.5/`.
`launch-campaign-10.sh` is generated only, guarded by exact declaration/code/fixture
hashes and absent output. `check-live-host-guard.sh` cannot execute the runner.
Stop instructions match the anchored Python child, never the uv/nohup wrapper.
Wait for hash-verified restoration, mixer equality and released lock; never SIGKILL.
