# EchoEar AEC acceptance, specification v9 (step 9)

Status: specification revision 2 (2026-10-20) plus the re-review resolutions in section 14; runner in section 15. It supersedes 752bdda after the Sol and Astra REVISE reviews
(`sol-step-9-acceptance-v9-spec-review.md`, `astra-step-9-acceptance-v9-spec-review.md`). The v9 runner is
described in section 15. The machine-readable gate registry is `hil/echoear_aec_gate_registry.py`. The verdict
rules are pure functions in `hil/echoear_aec_v9_rules.py`. Both are checked by
`hil/test_echoear_aec_gate_registry.py` against the reviewed inventory `hil/plans/echoear-aec-v9-gate-inventory.json`.

Authority: `coord-step-9-acceptance-replan.md` (the plan) and decisions 70, 71 and 72 in
`step-9-user-decisions-2026-10-19-b.md`. Where the reviews differ, this spec takes the stricter or clearer
reading and says so in section 13. Section 14 maps every review item to its resolution.

## 1. Claim

| item | v9 |
|---|---|
| Claim | Room-inclusive fixed benchmark (decision 72). On this bench and in this room, as heard including room noise, candidate aec-v87 meets the per-cell echo thresholds with at most 2 failures in each 20-cell volume stratum (0 and -6 dB). It produces no accepted false turn or false interruption. It meets the three barge-in groups at >= 9/10. It shows no candidate fault. |
| Not claimed | Isolated canceller leakage, since no floor is subtracted and the room is part of the result. A population failure rate with confidence: the exact one-sided 95 % upper bound at 2/20 is 28 %. Rare-event reliability. Unguarded engine performance: results are conditional on the 400 ms guard and 2 s rearm. |
| Candidate | aec-v87 = 0e44051 (aec-v86, +40 ms playout) + admission carry-over a503d2b (decision 71). Its own ELF hash and review build. Inbound allocation is 1 MiB: 64 x max_message_bytes 16384, pre-existing. The retained payload cap is 128 KiB, which is a cap, not the total allocation. |
| Bot/profile | Nova dev profile and vendor model as in v8.7 (`general-nova-3` 2025-04-17.21547), pinned by hash. A change refuses resume and the result is INCONCLUSIVE. |
| Prior results | nova-live-02 stays a recorded FAIL. Historical cells get no v9 credit. |
| Release | All four are required, then 9(d) merge and 9(e) rollback: (1) acoustic PASS; (2) integration/resource/control PASS of this campaign, meaning no CANDIDATE-FAIL in any part; (3) the freeze preconditions in section 10, including the decision-71 regressions and burst smoke; (4) the **separate 9(d) integration/reconnect result** (reconnect cycles and mixed-load control on aec-v87), which v9 does not schedule. A transport-qualification PASS (section 5) does not substitute for any of these. |

### Outcomes (every outcome ends the campaign)

| outcome | when | next |
|---|---|---|
| PASS | All required cells complete and scored, no pending score, every bar met, no CANDIDATE-FAIL | Release prerequisites above |
| FAIL | An irreversible acoustic failure (section 7, early stop) or any CANDIDATE-FAIL | A re-run requires a stated change |
| INCONCLUSIVE | Missing support: caps, parts, recovery capacity or the 150 min ceiling exhausted with cells missing, fewer than 10 eligible in a group, or a refused resume | Fix the named blocker. The record names the missing cells |

There is no early PASS. A computed verdict closes the plan. An offline analyzer repair cannot reopen a finalized
FAIL, and it cannot acquire replacement data.

## 2. Echo

| rule | value |
|---|---|
| Cells | 20 live echo cells per volume, at 0 dB and at -6 dB: 10 declared texts (T01-T10, baker = T08) x 2 live TTS generations. Baker and the control texts are inside the bank. Generation 1 runs in part 1 and generation 2 in part 2, in a seeded, declared order that alternates volume |
| Per-cell thresholds (unchanged, judged as heard including room) | Whole reply >= 20 dB. First 500 ms >= 10 dB when the source first 500 ms > -30 dBFS (otherwise N/A). Post-guard AFE residual <= -50 dBFS full band, with no HPF substitution and no floor subtraction. Guard closed at onset after >= 2 s silence: a witnessed wrong state is a miss; an unknown state is unscorable |
| Bar | <= 2 valid failures per volume. Allowance does not transfer between volumes |
| Valid miss | Counts. It is never retaken, whatever the room contributed (decision 72) |
| Hard events (`hard=True` in the registry) | `echo.hard_floor`: whole < 15 dB or post-guard > -40 dBFS on a valid measurement. `echo.false_turns`: any accepted false turn or false interruption. Either gives acoustic FAIL. These are policy limits, not empirical safety limits |
| Event coverage | Credit for the events endpoint requires coverage from reply start through >= 2 s of declared post-reply quiet, with matching peer/context identity and complete recorder coverage (no gap, truncation or rotation). Zero event rows alone are not coverage. Missing coverage is INVALID-CELL on that endpoint only |
| Validity | The same independently witnessed noise-gate and clip checks apply at both volumes. There is no special 0 dB floor screen: the -58 dBFS full-band check is MONITOR (decision 72). An applicable but unscorable endpoint is INVALID-CELL. A transport fault never invalidates a measurable echo. With no usable exposure the endpoint is `echo.unscorable` |
| Recorded | Raw MIC1 and pre-guard AFE full-band (and HPF) floors for every part baseline and every cell pre/post quiet window, plus drift by volume. Medians per volume, failures by text, and texts failing both generations |
| Not gated | Pooled median. Floor-relative metrics. HPF residual |

Operating characteristic, for illustration only (independent constant-rate model): P(accept a 20-cell stratum
with <= 2 failures) = 98 % at a true failure rate of 3 %, 88 % at 6 %, 68 % at 10 % and 21 % at 20 %.

Historical floors, recorded only: the full-band raw MIC1 part baselines were -60.9 to -64.7 dBFS in campaigns
07-10 and -57.5 / -57.4 dBFS in nova-live-01/02. The corresponding pre-guard AFE baselines in nova-live-01/02 were
-59.1 / -58.7 dBFS. In nova-live-02 the first 0 dB cell's raw pre/post quiet windows read -54.1 / -49.6 dBFS while
the HPF gates passed. In a room like that, the room-inclusive -50 dBFS clause can produce legitimate misses, and
those count.

### Known limitation: baker vowel-tail burst (decision 70b)

The burst is documented and counted as an ordinary miss within the <= 2 allowance. It is not exempt from the
hard limits.

| evidence | value |
|---|---|
| Live scored baker cells (c07-c10, nova-01/02) | 8 cells, 2 FAIL: c10 q01-echo-08 at -6 dB (19.32 dB / -48.39 dBFS) and nova-02 q02-echo-08 at 0 dB (18.90 / -43.25). Baker median 28.4 dB against 32.8 dB for all 50 live cells. Every other text has a minimum >= 23.6 dB |
| Overshoot event (>= 10 dB AFE-over-mic, about 860-925 Hz, 1.32-1.40 s after onset) | 3 of about 46 retained baker captures, including reproducers and 9 clean aec-arms-04 arm-A replays. Identical-PCM replay reproduced it 1 of 6 times |
| Attribution | Leading hypothesis is narrowband drift of the FD_LOW_COST linear filter. This is inferred: no reproducer carries the linear channel. The aec-arms diagnostic is parked |
| Effect on v9 | Two baker generations per volume. A recurrence costs one allowed miss, or fails acceptance if it crosses a hard limit |

## 3. Barge-in

| rule | value |
|---|---|
| Groups (-6 dB) | Firm Stop, firm sentence, normal sentence |
| Scheduled | 12 per group, with frozen logical ranks 1-12 (the 10 v8.7 cases then the first 2 v8.7 spares, same fixtures and offsets), 6 per planned part |
| Membership | The first 10 eligible **by frozen logical rank**, not acquisition order. Scores of later ranks are provisional until every lower rank is resolved. Cells beyond the tenth are reported only, but their hard events and candidate faults count |
| Bar | >= 9 successes of the 10 |
| Early FAIL | Only when two misses are in the final ten under every legal completion of the pending and missing ranks (`barge_group`). Example: rank 1 pending, ranks 2-11 eligible with misses at ranks 10 and 11 is OPEN, not FAIL |
| Success (unchanged) | Recognition (whole-word "stop" / WER <= 0.20) AND accepted interruption after near onset AND eligible or ongoing-reply-lull opportunity with verified future speech AND latency <= 1.2 s Stop / <= 2.0 s sentence (conservative upper bound) AND flush (callback to last old MIC3 <= 100 ms, server interruption to silence <= 250 ms, next non-silent TTS <= 1 s, pre-callback tail witness, both interrupt copies under healthy observation) |
| False events | `barge.false_interruption` (hard): an accepted turn or interruption that is not positively associated with the declared near utterance. Any accepted event before the mapped near-onset lower bound is unsolicited; after the first associated response, a further accepted turn outside [mapped near-onset lower bound, near-end upper bound + 2 s] whose transcript does not match the near text is unsolicited. A late accepted response that is associated with the near utterance (event/transcript association) is an ordinary latency/outcome SCORE miss, never a hard event. The same rule applies to witnesses and report-only exposures. Intended barge-in is not false |
| Stale audio | Any stale old-reply frame after mute is a CANDIDATE-FAIL |
| Opportunity classes | eligible / lull: scored. no-opportunity (witnessed silence): not eligible, no refill. in-window, unscorable-*, undelivered: INVALID-CELL with refill. Cancelled lull: scored as eligible when an eligible ongoing reply is independently established (pre-near source and retained future speech); otherwise INVALID-CELL with refill. Cancelled-lull counts are reported |
| Bot/STT | Misrecognition, late or absent turns and late next TTS stay SCORE. Only an independently logged service outage overlapping the cell is INVALID-CELL. A long TTS lead is diagnostic unless it removes the stimulus opportunity (then undelivered/unscorable) |
| Support | Fewer than 10 eligible reachable: the group is INCONCLUSIVE |

## 4. Report-only

Near-end preservation (<= 1 dB) on the near-only witnesses. Floor-relative echo (69b3d8c module). HPF residual
and stimulus levels. The -58 dBFS floor screen. Burst <= 3 and the missing-run histogram. Export-interval
transport. The normal-voice Stop, host-interrupt and sentence-split exposures are not scheduled unless the freeze
declares them; they never use spares, and their candidate faults and false events still count. The 0 dB barge
exposures are not scheduled.

## 5. Transport

Separate qualification bar, which is not an AEC gate: a network-only run in the diag-04 form, 75 min, with no ended
missing run >= 6, no post-PLC zeros and loss <= 2 %. diag-04 meets it for +40 ms on its own image. It is not a
mixed-load admission qualification of aec-v87.

Inside acceptance, a transport fault never invalidates a **measurable** echo, because the AEC reference is the DAC
output including PLC. An echo with no usable exposure is `echo.unscorable`. A barge-in or flush cell is invalidated
only by interval evidence of a fault overlapping its stimulus window; a whole-part delta is not assigned to every
cell. Starvation and post-PLC zeros are playout counters, not I2S faults. A concurrent local fault keeps its own
class.

## 6. Gate classes and validity

| class | n | meaning |
|---|---|---|
| SCORE | 20 | Counts toward the bar (pass, fail, eligibility). `hard=True` marks hard events |
| INVALID-CELL | 22 | The listed endpoints (`invalidates`) of one attempt have no credit either way; bounded re-attempt |
| PAUSE-PART | 41 | Close the part, restore, resumable. The open block earns no acoustic credit |
| CANDIDATE-FAIL | 21 | Sticky. Ends the attempt and blocks release |
| MONITOR | 23 | Recorded only |

Every attempt is classified by `classify_attempt` in this order:

1. **Aggregates dispatch.** Legacy composites (`resources.part_audit`, `capture.interval_checks`, `retry.clean_witness`, `delivery.counters_dispatch`) resolve to their failing leaves, and each leaf keeps its own class. An AFE clip stays SCORE and a room clip stays INVALID-CELL. `completed == false` without a leaf, or any unresolved composite, is PAUSE-PART. Missing or malformed resource data is `resources.coverage` (PAUSE-PART), never a measured breach.
2. **Endpoint validity.** Endpoints are `acoustic` (attenuation, opportunity, outcome) and `events` (false-event harm).
   - Independently predetermined invalidity applies equally to would-be passes and misses.
   - Outcome-dependent invalidity (`capture.cap`) never erases an established miss.
   - Room witnesses invalidate `events` only when time-relevant to the event.
   - An unclosed block invalidates `acoustic` only.
3. **Sticky findings.** Every CANDIDATE-FAIL survives. Positive false-event evidence survives alignment, observer or coverage loss; a room witness erases only the specific events it is time-associated with (findings carry event ids and the witness carries the ids it explains), and any unmatched event still STOPs. Numeric hard limits require a final valid acoustic measurement: while the block is open they are provisional, and block closure re-classifies every attempt from its retained findings so validity, hard events and action stay consistent (a candidate fault or positive false event forces STOP; a failed closure drops numeric hard limits).
4. **Action.** STOP (candidate fault or hard event) > PAUSE-PART > RETAKE (an endpoint lacks support and the exposure is not a valid acoustic miss) > CONTINUE. The credit unit is one exposure: a logical cell is credited only from an attempt whose acoustic and harm results are both valid, or from a valid acoustic miss, which is final and never replaced (`cell_result`). Endpoints are never combined across attempts.

Attribution rules:
- **Unknown cause** is PAUSE-PART.
- **Clipping is attributed time-locally.**
  - The clipped samples, matched to the counter interval, lie outside every declared stimulus or inside a failed quiet window that contains them: room, INVALID-CELL.
  - Positive causation by the declared, calibrated stimulus waveform: CANDIDATE-FAIL.
  - Otherwise (including a clip inside the stimulus with clean bookends): PAUSE-PART.
  - An AFE output clip, or a verified level clip of the declared reference with intact mapping: SCORE miss.
  - Reference clipping outside a scored cell, or with broken mapping: reference health / unknown.
- **Admission (decision 71).**
  - Both boundaries need a non-empty `pc_id`, an integer generation, the four admission counters and the complete aec-v87 `drop_reasons` schema, each a non-negative integer. Absent evidence is never zero, and one field's reset never nets against another field's drop: a positive drop delta is `admission.control_loss` even beside a reset; otherwise a reset or malformed interval is `admission.counter_schema`.
  - `admission_overflow` delta > 0 or drop/rejection deltas: CANDIDATE-FAIL. (v9.10, Sol v9.9: the aec-v87 firmware exposes no device-side witness of permanent non-dispatch, only received/dispatched/deferred/backpressure counters, so the former 'permanent non-dispatch with forward progress' branch is withdrawn; a permanent shortfall stays `admission.drain_timeout` PAUSE-PART.)
  - A live quiescent `received != dispatched` after the frozen drain allowance, with zero drops: PAUSE-PART.
  - Missing counters/schema, a peer change or a counter reset: `admission.counter_schema` PAUSE-PART.
  - Drain allowance (v9.9): a fixed declared **4.0 s** for the smoke and the acceptance, replacing the per-smoke 2x calibration (v9.4-v9.8: 2x the largest smoke attempt boundary, rounded up to 0.25 s, <= 3 s). Reason: v9.8-smoke-01 was lifecycle-ready but not READY because two ordinary boundaries caught a backlog (1.517 / 1.538 s; 2x rounds to 3.25 s > 3 s), and a 1.5 s allowance calibrated from a backlog-free smoke would have paused an acceptance on the roughly 5 backlog boundaries expected in its ~190 boundaries. The value comes from the pooled smoke distribution below (worst 1.54 s, 2.6x headroom) and stays within the 5 s burst receipt timeout (allowance + 1 s confirmation <= 5 s). Smoke readiness requires every ordinary attempt boundary <= 2.0 s (half the allowance) and none unbounded; the measured values are recorded in the smoke summary. `admission.drain_timeout` stays PAUSE-PART (v9.10: also for a permanent shortfall; no device-side forward-progress witness exists). The burst latency is MONITOR (within the 5 s receipt timeout), not a calibration source.

    | smoke (ordinary attempt boundaries) | boundaries | no-backlog max | backlog boundaries |
    |---|---|---|---|
    | v9.1-smoke-01 | 18 | 0.754 s | none |
    | v9.2-smoke-01 | 18 | 0.756 s | none |
    | v9.3-smoke-01 | 18 | 0.734 s | 1.405 s |
    | v9.4-smoke-01 | 18 | 0.741 s | none |
    | v9.8-smoke-01 | 18 | 0.702 s | 1.517 s, 1.538 s |
    | pooled | 90 | 0.756 s | 3 (3.3 %), worst 1.538 s |

    (v9-smoke-01 predates the measured-latency boundary record and is not pooled.) Survival check (N-1 targeted, fake hardware): about 5 backlog boundaries at 1.6 s in a stress campaign PASS without a pause; every after-attempt boundary (95 of 190) at 1.6 s or at 3.0 s still PASS at both timings (stress 6891 s / 7028 s). Classification therefore tolerates any number of boundary drains up to 4.0 s; their cost is wall time only (about 1.6 s each, within the ceiling slack). A drain beyond the allowance is a `admission.drain_timeout` PAUSE-PART: 5 such boundaries at 4.5 s end INCONCLUSIVE after three paused parts.
  - Recorder reconciliation: a recorder-vs-device shortfall alone is always `unknown.unclassified` (PAUSE-PART); host SCTP evidence never resolves delivery (v9.3). CANDIDATE-FAIL for control loss comes only from device-side positive witnesses: overflow/drop/rejection deltas or a duplicate/misordered application receipt. Otherwise there is a readiness review and no launch. It is never changed during the campaign. The inherited 0.25 s loss rule is withdrawn.
- **Delivery.** Proven internal drops, send-budget cutoffs, uplink gap, worker exception and live rx overflow: CANDIDATE-FAIL. `send_errors` alone: PAUSE-PART, unless network-only cause is established, in which case it follows the transport rule. A send_errors split never waives a simultaneous internal drop.
- **Missing playback.** Positive device receipt of a non-silent write without MIC3 render within 1 s: CANDIDATE-FAIL. Barge with a witnessed overlapping downlink fault: INVALID-CELL. Unknown: PAUSE-PART. Echo: section 5.
- **Capture gaps.** The v8.7 exactly-one-gap retry is withdrawn. Unknown attribution: PAUSE-PART. A demonstrated host/export defect: INVALID-CELL. A proven firmware PCM discontinuity: CANDIDATE-FAIL.
- **Stimulus lateness.** A single authoritative 100 ms bound (|error| <= 0.100 s, equality allowed). Above it the cell is INVALID-CELL. The v9 plan sets `timing.timer_lateness_max_s = 0.100`. Near-onset mapping uncertainty is separate.
- **Active end.** It is the final settled-end boundary of a part, before audio off and disconnect. After it, only expected transport teardown effects (T3, liveness alarms) are MONITOR (`post_seal.errors`). Heap, crash, I2S, admission-loss and restore faults count whenever they are observed, through restore. `transport.export` never hides a candidate fault.
- **Liveness / peer loss.** PAUSE-PART. Positive reboot, admission-loss or stall evidence takes its candidate gate; absence of a reboot banner is not proof of health.

## 7. Parts, blocks, ledger, caps

| rule | value |
|---|---|
| Parts | 2 planned parts, each <= 60 min including its restore, plus <= 1 recovery part that may use the ceiling left up to 7800 s (v9.6, decision 81; v9.8 RTP-cap bound; v9.5 capped it at 60 min). 150 min ceiling counted over part wall time, including restoration; gaps between parts do not count. No fourth part |
| Per part | Fresh boot. Same image, profile, plan and runtime hashes (except an approved analyzer-only provenance amendment, section 9). Own 30 s RF pre-gate, settled baseline, noise gate and 240 s link probe. 6 min restore reserve, which is mandatory |
| Blocks | A block holds <= 5 **acquired** scored-cell attempts between two near-only alignment witnesses. Each witness offset must be within 1024 +- 20 samples, and the mutual spread <= 20 samples. A block's end witness is the next block's start witness. v9.10: an invalid witness (noise-contaminated, or ambiguous / out-of-tolerance alignment) is retaken up to 2 more times per position (opening and closing) while the retake fits the part budget and admission; see section 15 |
| Block admission | A block opens only if every attempt in it, plus the closing witness, starts with >= 210 s of active time left and finishes inside the active budget (`block_fits`). Otherwise it stays unattempted for the recovery part. An unopened block is not an attempt |
| Closure | Only closed blocks earn acoustic credit. An unclosed block (witness failure is PAUSE-PART) loses acoustic credit for all its attempts, and each counts once toward the invalid cap. Its faults and false events remain |
| Ledger | Hash-chained attempt ledger: logical cell and rank, attempt id, part/boot, source hash, block, per-endpoint validity and reason, endpoint scores (provisional/final/pending), faults. Chain and restore are verified before every resume. Per-attempt scoring and ledger append are O(1), with no whole-hold work. The verdict is a pure function of the ledger |
| Caps (never reset) | <= 2 acquired attempts per logical cell. <= 8 invalid acquired scored-cell attempts per campaign, each counted once. An attempt counts as invalid when any applicable endpoint lacks support, including event-only loss (`echo.event_coverage`). 3 consecutive invalid attempts give PAUSE-PART |
| Re-attempts | Only for invalid endpoints, never for a valid scored miss. Appended at the end of the current part if a whole block fits, else in the recovery part. A re-attempt is a whole new exposure that decides the cell by itself: acoustic and harm evidence come from the same attempt, a valid acoustic miss stays final (even with lost event coverage), and the follow-up does not reconstruct the original event interval. A cell still unsupported after 2 attempts is `support-exhausted`, which makes its stratum INCONCLUSIVE (`cell_result`) |
| Early stop (FAIL) | A 3rd valid closed-block echo miss in a volume. A guaranteed 2nd miss in a barge group's final ten. Any hard event. Any CANDIDATE-FAIL |
| Capacity | Before each block, and at each part end, check that the remaining required support fits the remaining campaign wall time under the ceiling, spread over the entered parts still allowed (each net of its setup and 6 min restore reserve; planned parts <= 60 min, the last allowed part up to the ceiling left; v9.5/v9.6). A part that paused early cost only the time it used. If it cannot fit, the result is INCONCLUSIVE, not a fourth part |

## 8. Schedule and duration estimate

Per planned part: 20 echo cells (10 at -6 dB, 10 at 0 dB), 6 per barge group (18 in total) and 9 witnesses, in 8
blocks ordered E B E B E B E B. Echo blocks hold 5 cells; barge blocks hold 5, 5, 4 and 4. That is 47 attempts.

The per-cell RF check is the existing ~5 s pre-cell observation at an elapsed-time-scaled 20 MB/300 s limit (about
333 kB per 5 s). It is already inside the retained attempt windows. The 30 s window (about 2 MB) is per part only.
A literal 30 s per cell would add about 19.6 min per part.

Timings come from retained step-9 evidence. Single-echo statistics use attempts whose `result.json` holds exactly
one echo case, including c09/c10 a2 retakes. Pair captures and attempts with no cases are excluded.

| item | nova-live-02 | campaign-10 | used |
|---|---|---|---|
| Setup: part start to first cell (30 s RF pre-gate, flash, boot, display, baseline, connect, link probe) | 330 s | 330 s | 5.5 min |
| Finish, disconnect, restore, mixer (observed; the 6 min reserve still applies) | 58 s | 90 s | 1.5 min |
| Echo window, median / p90 | 43.9 / 46.8 s | 42.0 / 56.8 s | 43 / 57 s |
| Stop window | 52.8 / 55.8 s | 54.1 / 69.2 s | 54 / 69 s |
| Sentence window | 60.3 / 62.4 s | 58.4 / 64.6 s | 60 / 65 s |
| Near-only (witness) window | - | 42.2 / 42.5 s | 42 / 43 s |
| Gap between attempts (v8.7 whole-hold re-analysis) | 0.87 s x index | 0.76 s x index | O(1) target: 2 s, to be benchmarked before freeze (`freeze.scoring_cost`) |

The stress column sums the per-kind p90 windows. It is a planning stress scenario, not a measured campaign 90th
percentile.

| per planned part | typical windows | stress windows |
|---|---|---|
| Cells + witnesses, plus 2 s each | 39.6 min | 46.9 min |
| Active time (setup + cells), cap 54 min | 45.1 min | 52.4 min |
| Block admission (last witness must start by 50.5 min) | Last block fits (last witness at about 44.4 min) | The last barge block (4 cells, witness at about 51.7 min) does not fit and stays unattempted. The part ends at about 47.2 min active, and 4 cells per part go to recovery |
| Wall time including the observed restore | 46.6 min | 48.7 min |
| With v8.7 whole-hold re-analysis (+0.8 s x n(n-1)/2, n = 47: +14.4 min) | 57.9 min active: does not fit | - |

Recovery part (v8/v9.4 planning; v9.5 allowed up to 60 min, v9.6 the whole ceiling left): 30 min wall time, 24 min active, 18.5 min after setup. The last start is at 20.5 min, which leaves
about 15.75 min of acquisition including the final witness.

| recovery load | blocks / witnesses | time | fits |
|---|---|---|---|
| Typical: 8 invalid re-attempts (sentence-length worst case) | 2 / 3 | 10.5 min | yes |
| Stress: 8 spilled cells | 2 / 3 | 11.2 min | yes |
| Stress: 8 spilled + 3 re-attempts | 3 / 4 | 15.3 min | yes |
| Stress: 8 spilled + 4 or more re-attempts | 3 / 4 | >= 16.4 min | no: INCONCLUSIVE |

| campaign | estimate |
|---|---|
| Two planned parts | 93 min (typical), 97 min (stress) |
| With the recovery part | <= 128 min against the 150 min ceiling |

Fit: the schedule fits only with O(1) per-attempt scoring, which must be measured before freeze. Whole-block
admission keeps the stress spill unattempted rather than acquiring cells and losing them at the witness boundary.

Smoke: one real two-part smoke with a resume boundary. The ceiling is **30 min total including both restores**.
There is no automatic repetition and no credit. The schedule:
- Part 1: witness, echo -6, echo 0, firm Stop, witness, then restore.
- Part 2: fresh boot and resume, witness, firm sentence, normal sentence, witness, then the decision-71 burst check (<= 3 predeclared burst attempts), then restore.

That is about 10.9 + 11.3 = 22 min. The resume must show the ledger carried over with prior rows retained, caps not
reset and hashes unchanged. A burst with deferred == 0 is coverage not achieved; it is not a candidate failure and
not a pass. A noisy smoke can prove lifecycle execution, but it cannot prove acoustic witness validity.
Exhaustive resume and fault permutations belong to the host lifecycle test, not to more bench smokes.

## 9. Analyzer exceptions

A scorer exception leaves that attempt's endpoints **pending**. Pending scores withhold credit, the verdict and
every score-dependent decision: retry slot, group completion, early stop and resume. Acquisition continues only
on the already declared schedule, and only while live fault checks stay healthy. Raw captures are retained and are
not reacquired.

Repair is offline. A reviewed, analyzer-only diff is applied (no change to firmware, fixtures, thresholds or the
schedule). Its hash is recorded in the ledger as an approved provenance amendment, and it re-scores every retained
attempt. This is the only exception to the unchanged-runtime-hash resume rule. The repaired analyzer is used for
offline scoring and for any later part. A repair never reopens a finalized FAIL.

## 10. Freeze preconditions (registry `freeze.*`, PAUSE-PART: launch withheld)

| precondition | content |
|---|---|
| `freeze.admission_regression` | aec-v87 host regressions: 20- and 64-message bursts, byte-budget exhaustion, ordered exactly-once dispatch, `admission_overflow` observable |
| `freeze.burst_smoke` | Decision-71 smoke burst: overflow 0, live reconciliation, deferred > 0, ordered exactly-once **application** receipt of burst IDs. deferred == 0 is coverage not achieved, and the bounded attempts are predeclared |
| `freeze.lifecycle_test` | Frozen schedule through the real hold classes with a fake board, Jabra and bot. Covers every cell kind, witnesses and block admission/closure, invalid and re-attempt, pause, a real resume (hash chain, fresh boot, carried ledger), analysis and restore. Fault injection: admission overflow, drain timeout, noisy cell, room/unattributed clip, missing observer, cleanup failure, in-window cell, analyzer exception. It must cover the `barge_group` counterexample and assert that a scored miss is not retaken and a reboot does not reset a cap |
| `freeze.analyzer_dry_run` | v9 scorer over retained campaign-10, nova-live-02 (including the fixtureless host-interrupt cells) and arms-04 evidence, with no exception and with NumPy serialization covered |
| `freeze.scoring_cost` | Measured per-attempt scoring plus ledger append/checkpoint <= 2 s, independent of ledger length |
| Registry | The gate-registry tests pass, and the v9 plan carries `V9_PLAN_VALUES` with its gate leaves added to the reviewed inventory |
| Reviews | Two reviewers and the user's quiet-room go. Standing smoke permission (decision 70d) covers only the smoke |

## 11. Not in v9

Reconnect cycles, soak, the canonical DAC probe and quick-gap pairs. Their entries stay inactive, with
applicability `reconnect`/`soak`/`probe`; they create no acoustic obligation and waive no release prerequisite.
Reconnect belongs to the separate 9(d) integration result. Parked: the aec-arms diagnostic, floor-relative
live-noise validation, and the wired host NIC for barge-in.

## 12. Dispositions of the 752bdda open items

| item | resolution |
|---|---|
| 12.1 zeros/starvation | Keep INVALID-CELL (barge interval overlap) / MONITOR (measurable echo). Playout counters, not I2S. Local faults keep their class |
| 12.2 send_errors | Split: `delivery.firmware_drops` CANDIDATE-FAIL for proven internal loss; `delivery.send_errors` PAUSE-PART unless network-only cause is established |
| 12.3 console | PAUSE-PART when attribution is unknown; proven candidate fault under the frozen logging load is CANDIDATE-FAIL; a bounded local observer defect follows observer INVALID-CELL |
| 12.4 reference clip | SCORE miss for a verified level/config clip with intact mapping; otherwise reference health / unknown |
| 12.5 MIC clip from stimulus | CANDIDATE-FAIL only with positive time-local causation; ambiguity is PAUSE-PART; `clip.mic_room` amended to the time-local test |
| 12.6 drain timeout | Proven loss CANDIDATE-FAIL; timeout alone PAUSE-PART; v9.9: fixed declared 4.0 s allowance from the pooled smoke distribution, smoke readiness <= 2.0 s per attempt boundary |
| 12.7 missing playback | CANDIDATE-FAIL on positive receipt without render; barge with overlapping fault INVALID-CELL; echo per section 5; unknown PAUSE-PART |
| 12.8 cancelled lull | Sol's rule: scored when an eligible ongoing reply is independently established, else INVALID-CELL |
| 12.9 timer lateness | 100 ms single authoritative threshold; v9 plan field 0.100 |

## 13. Deviations and reviewer differences

| topic | plan / reviews | here | why |
|---|---|---|---|
| 0 dB floor | Plan: -58 dBFS precondition. Sol/Astra: drop or replace | MONITOR, room-inclusive (decision 72); deferral machinery removed | User decision 72 |
| Link probe | Plan lists it with INVALID-CELL | PAUSE-PART part precondition | Both reviews approve |
| Host RF | Plan: INVALID-CELL | ~5 s per-cell pre-acquisition check INVALID-CELL; 30 s part pre-gate PAUSE-PART; during-capture follows the transport rule | Both reviews approve; the 5 s window matches the code |
| Liveness / peer close | Not listed | PAUSE-PART; positive local evidence is CANDIDATE-FAIL | Both reviews approve |
| CPU 20-25 % | Fable: cell failure | CANDIDATE-FAIL on valid windows; missing windows are coverage | Both reviews approve |
| Barge membership | Plan: ">= 10 eligible" | First ten by frozen rank, guaranteed-two-miss early FAIL | Astra counterexample, Sol rank rule |
| Blocks | Not stated | <= 5 acquired attempts, whole-block admission | Both reviews |
| 12.8 cancelled lull | Sol: score if opportunity established; Astra: INVALID-CELL | Sol | Closes outcome-dependent censoring (coordinator) |
| 12.6 drain allowance | Sol: 2x observed policy; Astra: 2 s smoke then frozen measured | Both: 2 s smoke, frozen by Sol's 2x rule, <= 2 s (v9.4: <= 3 s; v9.9: fixed 4.0 s from the pooled smokes, see section 6) | Stricter combination; cap raised for spaced-pair sampling |
| Long TTS lead | Sol: diagnostic when complete; Astra: INVALID-CELL with caps | Sol (MONITOR) | No escape route from a measurable failure |
| Firmware capture gap | Sol: unknown PAUSE / proven CANDIDATE-FAIL; Astra: keep INVALID-CELL for one gap | Sol | Stricter; the one-gap exception has no v9 basis |
| Witness compatibility | Astra: individual compatibility, no number | +- 20 samples of 1024 | Declared value for review |
| Fable floor drift, alternative n/k, Astra burst hard-fail | Not adopted | Unchanged | Decisions 70a/b, plan |
| Recovery part length (v9.6, review item) | v9.5: every part <= 60 min | The recovery part may run up to the ceiling left: 9000 s minus the wall time used, e.g. 97 min after a setup-only part 1 and a full stress part 2, at most 130 min (7800 s, v9.8 RTP-cap bound) after early pauses (section 15) | Decision 81. The longest observed live hold is 3078 s (nova-live-02); heap margin is about 2-3 kB above the 81920 B floor; link and thermal drift over a 60-130 min session are unmeasured. The gates are unchanged and apply over the whole part |

## 14. Changes from 752bdda

| review item | resolution |
|---|---|
| Sol class audit: machine-readable hard events | `hard` field; `echo.hard_floor`, `echo.false_turns`, `barge.false_interruption`; SCORE-HARD removed (6) |
| Sol 1 / Astra 6: validity before precedence, sticky faults/false events, valid misses kept | `classify_attempt`, section 6 steps 1-4, tests |
| Sol 2: false events beyond echo | `barge.false_interruption`, declared legitimate window (3) |
| Sol 3 / Astra 2: aggregates | Aggregates dispatch; `resources.part_audit` PAUSE-PART; `delivery.counters_dispatch`; coverage rule (6) |
| Sol 4: long TTS, capture cap, firmware gap | `tts.long_lead` MONITOR; `capture.cap` outcome-dependent; gap split (6) |
| Sol / Astra section 12 (12.1-12.9) | Section 12 table, registry text |
| Sol / Astra 1: decision 71 | `admission.*` split, `admission.counter_schema`, `admission_interval`, `burst_coverage`, `freeze.burst_smoke`, memory statement (1, 6, 10) |
| Astra 3 / Sol 13: barge membership | `barge_group`, counterexample test (3) |
| Astra 4 / Sol 8: RF window | 5 s scaled per cell, 30 s per part (8, registry) |
| Sol 8 / Astra duration: block admission, block definition, witness compatibility, recovery accounting, capacity | Section 7, `block_fits`, `close_block`, `witnesses_ok`, recovery table (8) |
| Astra duration: stress label, scoring cost | Stress column; `freeze.scoring_cost` (8, 10) |
| Astra: campaign-10 selection | Stated (8) |
| Sol / Astra smoke | Two-part smoke, 30 min ceiling, no repetition, burst check (8) |
| Astra 5 / Sol floor | Decision 72 (2, 13) |
| Astra: event coverage | Reply + >= 2 s quiet with identity/recorder coverage (2) |
| Astra / Sol: analyzer exceptions | Pending scores, provenance amendment, resume reconciliation (9) |
| Astra / Sol: post_seal, transport.export | Active end defined; faults count through restore (6) |
| Astra: freeze entries | `freeze.*` entries (10) |
| Sol: integration definition | 9(d) integration/reconnect result named as a release prerequisite (1) |
| Sol / Astra tests | Docstring narrowed; reviewed inventory with mutation tests; AST harness kinds (no serial import); table-driven rule tests |
| Sol: `playback.transport_witnessed` echo applicability | Barge only |
| Sol / Astra 13.x dispositions | Section 13 |
| Sol 1 / Astra 1 re-review: event-only invalidity | Counts once toward the invalid cap; RETAKE (whole new exposure) unless the exposure is a valid acoustic miss, which stays final; no combining endpoints across exposures; support-exhausted after 2 attempts; positive false events still STOP (6, 7, `cell_result`, rules tests) |
| Sol 2 re-review: room witness scope | Per-event association: a witness explains listed event ids only; unmatched events STOP (6, two-event test) |
| Sol 3 re-review: late associated barge response | Ordinary SCORE miss; only unsolicited (unassociated) events are `barge.false_interruption` (3, registry) |
| Sol 4 re-review: block finalization | `close_block` re-classifies from retained inputs; canonical STOP; numeric hard limits provisional until closure (6, tests) |
| Sol 5 / Astra 2 re-review: admission schema | Non-null live identity, complete aec-v87 drop schema, non-negative ints, per-counter monotonicity; proven positive drop is loss even beside a reset; absent evidence never zero (6, tests) |
| Astra freeze review 1-7 (v9.1) | Clip/next-reply misses; interval transport (5(e) reverted); deadline/pending/live-findings; rescore-derived state; full leaf audits; structural smoke readiness; entered parts consume slots (5(b) reverted), wall-time caps in the verdict; baseline floor report (section 15, lifecycle and scorer tests) |
| Sol freeze review 1-3 (v9.1) | Exhausted cap and terminal closure precede PASS; drain calibration never zero for an unanchored backlog; per-reply recorder reconciliation (section 15, tests) |
| v9-smoke-01 shakedown S1-S4 (v9.1) | Receipt window and part-log lookup; sustained quiescence and measured calibration; synchronous data-channel burst; wifi-stats explicit MONITOR (section 15, tests incl. the exact burst-2 shape) |
| Sol/Astra v9.11 reviews (v9.12) | Lost-peer audit partitioned by phase (device leaves over every observation incl. reconnected generations; session leaves over the lost epoch); completion separate from leaf outcomes; dispatch equality at the drained boundary; narrow exit rule (send_cutoff / send_budget_cutoffs only, PENDING USER CONFIRMATION); SCTP scoped; hardened witness (section 15) |
| v9.10-acceptance-01 part 1 peer loss (v9.11) | Final audit after a positive peer-loss witness: device-side stages run with witnesses, bot-side stages not applicable, unacknowledged control attributed to the transport exit; lost epoch audited to its last attempt boundary; reconnect refusal report-only (section 15) |
| Sol/Astra v9.9 reviews (v9.10) | Terminal audit from an explicit applicability inventory with a completeness test (present-task stacks, display faults, send_errors, send_budget_cutoffs, admission/drops, RX); shared interval validation; the unwired permanent-non-dispatch CANDIDATE-FAIL branch withdrawn (no device-side witness in aec-v87) |
| v9.9-smoke-01 noisy witness (v9.10) | Witness retake: up to 3 acquisitions per opening/closing position before PAUSE-PART, bounded by budget and admission, outside the invalid cap, STOP immediate; noisy-witness N-1 at every position (section 15) |
| v9.8-smoke-01 readiness, Sol/Astra v9.8 reviews (v9.9) | Fixed declared 4.0 s drain allowance from the pooled smoke distribution (worst 1.54 s); smoke readiness requires every ordinary boundary <= 2.0 s; slow-drain survival cases; v9.8-smoke-01 replay READY (section 6). Terminal-snapshot reconciliation of every applicable leaf (late RX, console, admission). Within-budget connected-time wording (section 15) |
| Sol/Astra v9.7 reviews (v9.8) | Witness: relative AFE-vs-raw estimator with a declared 1 % ambiguity margin; three-way agreement; fixture/envelope agreement-only (their aliases sit within 0.4 %); 120-case noisy grid. Reset exemption by cause (USB_UART_CHIP_RESET) and phase (first line after the ROM banner). Late disconnected-snapshot faults dispatched to their gates. RTP phase accounting corrected; last part capped at 7800 s (section 15) |
| Sol/Astra v9.6 reviews (v9.7) | Witness: unconstrained primary with a polarity-free envelope cross-check; half-pitch alias only with its own checks and envelope agreement; else ambiguous. Setup-only audit: candidate serial crash scan first, explicit applicability. completed=false after an exception needs every final-audit completion witness. N-1 points from each baseline's own trace (stress 98), targeted cases. Part-3 envelope, heap margin and RTP cap stated (section 15) |
| N-1 stress mechanism / decision 81 (v9.6) | The last allowed part (recovery) is not capped at 60 min: its total is the 150 min ceiling minus campaign time used, and capacity counts it that way. Stress N-1 86/96 (was 58/96); long-hold risk listed in section 13 |
| v9.4-acceptance-01 / decision 79 (v9.5) | Same-polarity witness matcher (offset constant 1024 by construction; retrospective over 16 boots); capacity from remaining campaign wall time, every part up to 60 min; N-1 survival test |
| v9.3 reviews (v9.4) | Causal receipt barrier instead of the cross-clock guard; structured re-audit findings in the ledger; allowance cap 3 s; up to 10 smoke bursts |
| Astra/Sol v9.2 re-reviews (v9.3) | SCTP delivery proof removed: recorder shortfall always PAUSE-PART; burst readiness from applied ids + post-application sustained quiescence (no pipeline counting, no request/sample race); calibration from attempt boundaries only (burst latency MONITOR, calibration-source change for approval); every burst re-audited; up to 3 bursts per part (replays of v9.1/v9.2-smoke-01 burst-01 become ready) |
| Astra/Sol v9.1 re-reviews (v9.2) | Closed PASS withdraws on lost support; complete message->DATA->SACK coverage for recorder shortfalls; partial captures establish the opportunity and are repaired offline; drain receipt-time deadlines; burst pipeline baseline alignment; whole-part receipt sequence kept with duplicates and re-audited; barge device transport attribution unresolved pending policy (section 15, tests incl. the v9.1-smoke-01 burst-01 replay) |
| Not adopted | Astra's numeric -57 dBFS same-path floor (superseded by decision 72). Value/applicability assertions over the v9 plan are deferred to the harness step because no v9 plan exists yet; `V9_PLAN_VALUES` and the inventory are the hooks |

## 15. Runner and operator

Runner: `hil/echoear_aec_v9.py` (plan `echoear_aec_v9_plan.py`, ledger `echoear_aec_v9_ledger.py`, scorer
`echoear_aec_v9_score.py`, rules `echoear_aec_v9_rules.py`). The freeze bundle is
`step-9/freeze-v9.12-aec-v87/` (v9.12, not yet prepared; earlier bundles, including freeze-v9.10 and the unfinished freeze-v9.6/v9.7, are superseded); `launch-command.txt` there holds the exact commands with the plan hash.

| step | command (from any shell; the launchers `cd` to the repo) | gate |
|---|---|---|
| Smoke (both parts) | `AEC_V9_REVIEWED_PLAN_SHA256=<sha> .../freeze-v9-aec-v87/launch-v9-smoke.sh` | reviewed sha + TTY; no USER-GO (decision 70d); output `step-9/v9.12-smoke-01` must not exist |
| Acceptance part 1 | `AEC_V9_REVIEWED_PLAN_SHA256=<sha> .../launch-v9-acceptance.sh run` | reviewed sha + USER-GO holding the sha + TTY; smoke ledger ready (burst covered, clean restores); output `step-9/v9.12-acceptance-01` must not exist |
| Part 2 / recovery | `AEC_V9_REVIEWED_PLAN_SHA256=<sha> .../launch-v9-acceptance.sh resume` | same, plus the resume checks below |
| Result | `python hil/echoear_aec_v9.py analyze --plan <plan> --out <output>` | file-only |

- **Parts.** Each invocation is one part and one process: 30 s RF pre-gate, flash, fresh boot, baseline (3 attempts),
  connect, 240 s link probe, blocks, final audit, disconnect, normal-image restore, mixer restore, lock release. The
  runner owns the bench lock (never wrap it in `flock`). Part 1 runs planned part 1, part 2 planned part 2 plus any
  part-1 blocks left unattempted, part 3 (recovery, 30 min) everything still required. The smoke is two parts with
  a resume boundary (the launcher runs `smoke-run` then `smoke-resume`), 30 min ceiling, no credit.
- **Part slots.** Every entered bench part (flash started) uses a slot, including a setup failure after the
  flash; a host preflight refusal (before the lock) uses none. No fourth part (two for the smoke). Part wall
  time within its own budget, no recorded overrun, and the sum within the ceiling are verdict prerequisites:
  an overrun still restores safely but cannot PASS.
- **Classification per attempt.** Live integrity (admission and per-reply recorder reconciliation, delivery,
  resources, render ledger, reference health, observer, reboot, noise, RF, interval transport) runs first and
  is kept even when the acoustic analysis raises (that attempt is then pending; no score-dependent retake or
  early stop while anything is pending). Clip findings are ordinary acoustic misses on a valid measurement.
  A barge flush also requires the next reply to reach MIC3 within 1 s (absent or late: miss; unmeasurable:
  invalid). A capture-deadline attempt keeps an already established latency miss.
- **Interval transport (barge).** Invalid by host-timestamped evidence overlapping [Jabra request - 0.2 s,
  next-reply first write + 1 s]: a downlink send gap >= 120 ms or an SCTP T3 without clean same-peer
  recovery. The registry's device-playout gate (loss <= 2 %, ended run >= 6, post-PLC zeros, starvation,
  histogram) needs time-local device evidence that the acquisition does not record (PERF only at before /
  capture-end / after; no per-packet device trace): an attempt-interval device transport fault on a barge
  cell is therefore unresolved, PAUSE-PART (reviewed option iv). RF attribution inside the window is unmeasured
  (no per-second counters are acquired): a whole-capture total alone has no effect and is never a demonstrated
  `rf.capture_window` pass.
- **Part audit.** Every failing leaf of the final, setup and link-probe audits is dispatched; admission is
  reconciled over connected -> settled-end; clips outside scored cells are attributed (MIC: room, recorded;
  output/reference: unknown); an incomplete audit is unresolved and blocks PASS. Each part persists its
  decision-72 baseline floor report (`baseline-floors.json`, ledger `floors` row).
- **Quiescence (S2).** A boundary is quiescent only when received == dispatched at two PERF observations
  requested >= 0.25 s apart; a single equal observation is not enough. The first member must be received by
  the allowance deadline and the confirmation by deadline + 1 s (receipt times), else drain timeout. The measured value is always a
  measurement: sustained quiescence minus a witnessed arrival anchor (backlog), or the polling/PERF I/O of the
  quiescence pair when no backlog was seen; never 0. Only attempt-boundary values count for readiness (v9.3, v9.9).
- **Burst (S1, S3; v9.3).** The bench bot writes the 40 messages synchronously to the aiortc data channel
  (`send_app_message`, no await between sends), so they leave as one SCTP flight (host test: 38 of 40 DATA
  chunks before the first SACK; aiortc never bundles, so one packet never carries > 16 chunks; on-board
  deferral is opportunistic). Readiness: every declared id applied (app_log `SERVER` lines; the receipt window
  stays open until all 40 are logged or 5 s after the first receipt; the whole-part serial log, duplicates
  and order kept, is the witness), then a sustained quiescent pair of fresh, acknowledged stats requests
  issued only after the host received every application receipt (causal barrier, v9.4). The app_log
  millisecond (`esp_log_timestamp`, tick based) and PERF `device_us` (esp_timer) do NOT share an origin
  (about 1.344 s apart on this board), so no clock comparison is made and missing `I (ms)` stamps change
  nothing. No pipeline counting. Up to 10 bursts per part (v9.4), stopping at the first covered one; a burst
  starts only with >= 30 s of active time left. Re-audit findings are structured ledger findings. A duplicate or misordered receipt is
  control loss whatever the coverage; a remaining gap with a quiescent device and zero overflow is evidence
  loss (`hardware.console`). Readiness re-audits every attempted burst over the part log.
- **wifi-stats (S4).** Evidence only: a boundary whose `wifi-stats` returned no driver payload is recorded per
  attempt (`wifi_stats_payload`, finding `rf.diagnostics`, MONITOR) and never gates.
- **Smoke readiness.** Structural: every scheduled smoke cell attempted in its own part without error or
  pending score, blocks executed, two entered parts complete with clean restores and audits, total <= 30 min,
  evidence unchanged, no candidate fault; burst coverage and a bounded drain allowance separately. A drain
  with backlog but no arrival anchor is unbounded (never zero) and withholds the allowance.
- **Capacity (v9.5, v9.6).** Before every block and at every part end the remaining required support (typical
  windows) is compared with the remaining campaign wall time under the 150 min ceiling over the entered parts
  still allowed (max 3), each net of its setup and restore reserve: planned parts up to 60 min, the last
  allowed (recovery) part up to the whole ceiling left (v9.6, decision 81), so no part can exceed the ceiling. A
  part that pauses early costs only the time it used (v9.4-acceptance-01 closed INCONCLUSIVE after a 393 s part
  because part 2 plus a 30 min recovery part could not hold the work). Capacity is a typical-duration feasibility
  estimate, not a promise that every stress-duration block fits.
- **Part-3 budget envelope (v9.6-v9.8).** Part 3's total is min(9000 s minus the wall time parts 1-2 used, 7800 s), including
  setup and its 6 min restore reserve: 5807 s (97 min) after a setup-only part 1 (331 s) and a full stress part 2
  (2862 s); 8338 s (139 min) after two early 331 s parts under v9.6/v9.7, capped to 7800 s (130 min) in v9.8. It is a budget, not a characterized safe duration and
  not a prediction of how long the remaining schedule runs; the longest live hold observed is 3078 s
  (nova-live-02). Heap margin: nova-live-02 ended with internal minimum free 84072 B (2152 B above the 81920 B
  floor) on the earlier candidate; the v9.4 aec-v87 smoke parts show 84896 / 84852 B (about 3 kB above). Neither
  establishes a leak rate or long-hold survival. A measured breach late in a long part is still CANDIDATE-FAIL;
  missing resource evidence stays coverage. RTP evidence cap: 400000 packets
  per stream per connection (one connection per part). The busiest observer streams run at 50 packets/s
  (nova-live-02: 148602 rtp / 148716 downlink rows in 3078 s; sctp 75746, dtls 13408), so the cap covers 8000 s
  of connected media. Phase accounting (corrected, Sol/Astra v9.7): of the 330 s setup allowance only about 90 s
  is before connect; the 240 s link probe is connected (nova-live-02 probe 240.0 s), and finalization can keep
  the connection during part of the reserved cleanup, so the reserve is not guaranteed disconnected time. With
  the uncapped v9.6/v9.7 last part, the 8338 s example allowed about 8338 - 360 - 90 = 7888 s connected (394400
  packets, 98.6 % of the cap), and the cap could be exceeded once parts 1-2 together used less than about 550 s
  (e.g. two setup-only parts of about 180 s each). v9.8 therefore caps the last part's total at 7800 s
  (`last_part_total_max_s`). Within-budget parts have at most 7800 s connected time (390000 packets at the
  assumed 50 packets/s, 97.5 % of the cap); an overrun may still exhaust observer storage and cannot PASS
  (Budget.cleanup marks total expiry without interrupting restoration, and the ledger rejects an over-budget
  PASS); capacity counts the last part the same way, and the plan build refuses a
  value that would break the bound. Truncation would in any case be observer evidence loss (`observer.health`,
  PAUSE-PART), never a candidate result by inference.
- **N-1 survival test** (`hil/test_echoear_aec_v9_n1.py`, full sweeps at freeze): *single immediate injected
  exception, clean audit/restore*. One non-candidate PAUSE-PART at every schedule point of each timing baseline's
  own fault-free trace: typical 2 parts / 94 captures (96 points), stress 3 parts / 95 captures (98 points,
  including the part-3 setup and the final witness). Injection is at acquisition entry; a setup injection is a
  link-probe failure; restoration costs 0 s. Typical: every point PASS within the ceiling and 3 entered parts
  (asserted). Stress (reported): 86/98 PASS (v9.5 had 58/96 at the typical points); the 12 INCONCLUSIVE are pauses inside part 3 itself (its setup and captures 85-95, incl. the final witness) with no fourth part; worst completing wall time 7090 s, at most 3 entered parts. Targeted cases, asserted PASS at typical timings and reported at
  stress: 90 s and 360 s restoration on every part (setup:1, capture:48), a failure after half the capture window
  (capture 24, 70), an ambiguous opening witness of a mid-part block (parts 1 and 2), a loud room at the part-1
  baseline; all PASS at both timings (stress worst 8062 s, 360 s restores with a capture-48 pause). These are
  conditional test counts, not completion probabilities; a single pause inside the last entered part (incl. its
  setup and final witness) still ends INCONCLUSIVE.
- **Setup-only audit (v9.7, v9.8).** Before the setup-only exemption, candidate-serial.log is scanned for
  positive panic/reboot evidence across the candidate boot/setup/live interval. Only a positively identified
  expected host-triggered reset is exempt, by cause and phase: `rst:0x15 (USB_UART_CHIP_RESET)` as the first log
  line after nothing but the ROM banner (ESP-ROM:/Build:), i.e. before any candidate output; all 43 retained
  candidate logs start exactly so. Any other reset reason (watchdog, software restart, brownout), a reset after
  candidate output, Guru Meditation, abort(), Rebooting... or Backtrace is `device.reboot_crash`
  (CANDIDATE-FAIL) whatever the immediate exit was, including a watchdog reset that is the first reset line
  captured (Astra v9.7). AEC_GUARD_CONFIG lines are not boots (configuration setters print them). The restore
  boot is in restore-serial.log. With no live peer, session leaves (session snapshots, admission, encode, CPU
  windows, I2S/PCM session counters, uplink_frame_ms) are not applicable, nor is the stack of a task that does
  not exist yet; only measured applicable leaves (internal heap minimum and largest block, failed allocations,
  stack of existing tasks, audio/console/AEC hardware counters) can fail the candidate.
- **Exception-ended part (v9.7).** completed == false after an acquisition exception resolves only when every
  applicable final-audit stage has its completion witness (resource audit, exception integrity audit, disconnected
  snapshot, SCTP recovery audit) and finish raised nothing; otherwise `resources.part_audit` stays unresolved and
  the campaign cannot PASS. v9.8-v9.10 (Astra v9.7, Sol v9.8, Sol/Astra v9.9): the terminal disconnected
  snapshot is audited from an explicit applicability inventory (`TERMINAL_INVENTORY` in `hil/echoear_aec_v9.py`)
  that covers every leaf of the live checker (check_snapshots, check_directory, delivery counters, admission)
  and every `resources:*` registry term; a test fails when a new leaf has no entry. APPLICABLE leaves run through
  the existing rule functions: check_snapshots for the internal heap floors, the stack floors of tasks still
  present (playback formula preserved; an exited task is not applicable) and the whole-session absolute-zero
  counters (I2S, PCM queue, allocations, audio, console -> `hardware.console` PAUSE-PART, AEC errors, capture
  read errors/timeouts); the PERF display fault fields (dead, failed, send_failures, dma_errors, gate and
  display-gate timeouts, lock failures/timeouts -> `resources.display`); and one shared interval validation
  against the connected baseline (the `rules.admission_interval` rules: same peer epoch, non-negative integers,
  monotonic) for drop_reasons/admission_overflow (`admission.control_loss`), send_errors
  (`delivery.send_errors` PAUSE-PART), send_budget_cutoffs (`delivery.firmware_drops`) and live rx_overflows
  (`hardware.audio_io`; startup counts stay MONITOR). A reset, null or decrease is `admission.counter_schema`,
  never zero; an independently positive loss is kept beside a malformed field. Clip counters go through the
  part-epoch residual. NOT_APPLICABLE after teardown, with reasons: session presence, CPU windows, encode
  activity, uplink-frame identity, dispatch equality (no drain-polled terminal boundary; aec-v87 does not count
  signalling messages as dispatched), character post growth, peer liveness. a serial timeout or missing stage stays unresolved and the
  generic error_class is never promoted to a candidate failure.
- **Peer loss before the final audit (v9.11).** v9.10-acceptance-01 part 1 had a real link outage mid-acquisition
  (bot: liveness alarm, client-disconnected, evaluation-end; device: `SESSION FAIL gen=1 reason=remote
  disconnected`, then automatic reconnects). The runner's next control was unacknowledged (HarnessFailure ->
  PAUSE-PART, correct), but BenchHold.finish then raised at its first bot-side stage (`health`, then
  `settle_attempt`: "Evaluation control remains unacknowledged"), so no serial/device-side stage ran and the part
  audit stayed unresolved (missing resources, exception integrity, disconnected snapshot, SCTP), which blocks
  PASS for the whole campaign. With a positive peer-loss witness (the bot's client-disconnected / evaluation-end
  for the connected pc_id, or the device's SESSION FAIL for the connected generation), the final audit now skips
  the bot-side stages (settle/seal, health, observer and active-error checks: not applicable without the peer;
  the unacknowledged control is attributed to the already-classified transport exit) and runs every device-side
  stage with its completion witness: settled-end snapshot (no drain poll), exception-integrity and resource audits
  over the lost epoch's own snapshots (no liveness leaf), display/character reads, audio off and disconnect, the
  disconnected snapshot (terminal inventory), the host RF gate, bot stop, and the SCTP recovery audit over the
  active interval up to the loss (an unrecovered timer there is attributed to the loss). v9.12 (Sol/Astra v9.11 reviews) replaces the v9.11 blanket exit waiver and
  partitions the audit by phase. Device leaves (heap minimum and largest block, stacks of tasks present in each
  observation, whole-session hardware counters, PERF display faults, live capture overflows) are checked over
  every retained observation through teardown, including reconnected generations (a breach in the new
  generation's settled-end, or in a task that exits before the disconnected census, still fails); session leaves
  (session presence, encode, CPU windows, uplink frame) over the lost epoch's live phase. Audit completion is
  separate from leaf outcomes (`completed` vs failing leaves, which dispatch as findings). Dispatch equality is
  validated at the last genuinely drained live boundary (the failed acquisition's before boundary), not at a
  capture-end / lost-peer observation. **Exit rule, PENDING USER CONFIRMATION:** within the failed acquisition's
  own exit interval (its newest drained before / before-boundary observation in the lost epoch -> the epoch's
  newest observation) only `drop_reasons.send_cutoff` and `send_budget_cutoffs` deltas are *exit-associated*
  (reported, not dispatched; associated with the exit, not proven network-caused). Every other drop reason and
  admission_overflow stays `admission.control_loss` CANDIDATE-FAIL beside a peer loss; send_errors keeps
  `delivery.send_errors`; malformed/reset counters or an unidentified epoch are `admission.counter_schema`.
  Earlier intervals are strict. The audit note records every delta of the exit interval (drop reasons,
  send_budget_cutoffs, send_errors, received/dispatched/deferred), both observations and the authorizing rule.
  SCTP timers unrecovered at the loss are associated with the exit only when they belong to the lost peer and
  start inside the exit interval; any other unrecovered timer stays a finding. Witness hardening: bot evidence
  needs an explicit client-disconnected for the connected pc_id, bound to its evaluation-start and
  session-owner.json (evaluation-end alone is insufficient); device evidence is the complete anchored `dg-app:
  SESSION FAIL gen=<n> reason=remote disconnected` record after that generation's `STATE Connected` in the same
  boot (a SERVER payload containing the text is rejected). In real part 1 the exit interval is
  `v9-echo-m6-t09-g1-a1` before -> capture-end: send_cutoff +240 and send_budget_cutoffs +139 exit-associated,
  everything else unchanged. Without a peer-loss witness (a live peer) an unacknowledged control keeps the existing path and an
  unresolved finish_error. The bot refusing the device's automatic reconnects (`FileExistsError` on
  `evaluation/session-owner.json`) is the intended single-owner guard ("a second peer/generation can never
  inherit a partly scored sequence"): it raises before evaluation-start, writes no evaluation events, and the
  part has already ended on the loss, so it is report-only and does not affect the audit. Replay on a copy of the
  real part 1: the witness is found (bot and device) and the epoch audit passes over its 217 snapshots, but the
  retained result stays unresolved, because the device-side stages were never captured in that run (no
  resources, exception audit, disconnected snapshot or SCTP record exist); only a new run under v9.11 records
  them.
- **Witness retake (v9.10).** v9.9-smoke-01 part 1 paused at its opening witness: the alignment was exact (1024 on
  all three estimators, relative competitor 0.96) but the quiet windows were noise-contaminated
  (`noise.cell_quiet_windows`, about -52 dBFS against a -55.6 dBFS baseline), and one noise burst during a witness
  cost a whole part although nothing scored was at stake. Each witness position (opening and closing) now allows
  3 acquisitions: an invalid witness, i.e. noise-contaminated (INVALID-CELL, action RETAKE) or ambiguous /
  out-of-tolerance alignment, is retaken up to 2 more times before `alignment.near_witness` PAUSE-PART. A retake
  must fit the part budget and whole-block admission (an opening retake together with the block's attempts and
  its closing witness; a closing retake by itself); otherwise the position fails. STOP-class findings during a
  witness stop at once; PAUSE-PART-class findings and a pending witness score pause with their own gates; a
  spread disagreement between two individually valid witnesses is not retaken. During a closing retake the
  block stays open, its cells pending; witness rules are unchanged (no credit without valid, agreeing opening and
  closing witnesses). Witness acquisitions are outside the 8-invalid cell cap. Every acquisition is an attempt
  row (block key `<position>`, `<position>#2`, `<position>#3`), and every retake or refused retake is a noted
  ledger row. Each part declares 60 witness ids (3 per position). N-1: a noise-contaminated first acquisition at
  every witness position of the fault-free traces (18 typical, 19 stress), retake valid, PASSes without a paused
  part (asserted). Cost (reported, fake hardware): one noisy acquisition adds one witness window, +45 s typical / +46 s stress; two consecutive noisy acquisitions at one position add +90 s / +92 s and still PASS at every position; three consecutive (the position fails, PAUSE-PART) still PASS at every typical position (+380-812 s) and at 16 of 19 stress positions; the 3 INCONCLUSIVE are the part-3 positions (no fourth part).
- **Witness estimator (v9.8).** The raw -> AFE delay is expected to be 512 (framer priming) + 512 (FD_LOW_COST)
  = 1024 samples; the expectation does not validate a contradictory witness. Three estimators must agree, each
  within +-20 of the chosen fixture offset and all inside 1004-1044:
  1. *Fixture match*: the unconstrained match in each channel (|c| >= 0.25, >= 1.1x any peak beyond +-640
     samples), or else an alternative AFE peak accepted only with its own |c| >= 0.25 / 1.1x checks, the raw
     peak's polarity with the primary AFE peak opposite, and half a fixture pitch period from it (+-25 %).
  2. *Envelope*: the lag maximizing the normalized correlation (>= 0.5) of the magnitude of the 200-3400 Hz
     analytic signals; polarity-free.
  3. *Relative*: AFE matched directly against raw MIC1's own fixture-anchored 8000-sample interval (Sol v9.7),
     valid by its own |c| >= 0.25 / 1.1x checks, with a declared ambiguity margin: any other local maximum of
     |c| within +-64 samples (about one pitch period) that lies outside 1004-1044 or more than 20 samples from
     the winner and scores >= 0.99 x the winner makes it ambiguous.
  The margin is declared from the retained data: on the 31 valid captures the relative estimator's closest such
  competitor is 0.982 of the winner (0.8 % headroom). Fixture match and envelope carry no margin: the witness
  fixture is strongly periodic (pitch period about 45 samples), and on every real capture their one-period
  aliases (979/1069 and 1002/1046) sit within 0.4 % of the winner (closest 0.996 for both), so no margin with
  headroom exists for them; they are agreement checks only. Anything unresolved is ambiguous
  (`alignment.near_witness`, PAUSE-PART). Real captures: all 31 valid captures stay valid (relative 1024 on
  every one); the 34-capture replay reproduces every earlier result except v9.4-acceptance-01's 1001, which
  becomes 1024 by the alternative path. Regressions: a 120-case noisy grid (the retained raw channel copied into
  AFE at 1001/1003/1004/1024/1044/1045/1047 samples, both polarities for the alias and nominal delays, with
  independent noise at twice the speech level during the fixture interval, int16, seeds 1-12, including Sol's
  seed-8 inverted-1047 and seeds 5 and 9) accepts no wrong or out-of-gate delay (17 correct accepts, 103
  ambiguous); the earlier inverted/plain +23, inverted-1047, weak-replacement and inheritance constructions stay
  rejected.
- **Resume checks** (`resume`, before any lock or hardware): ledger hash chain; same plan hash and mode; previous
  part has a part-end row with a verified restore (normal ELF, mixer byte-equal, lock released, no cleanup errors);
  every earlier part directory unchanged since its part-end manifest; no pending score; runtime hashes unchanged
  except an approved analyzer amendment; fewer than 3 parts (2 for the smoke); campaign not closed. A refusal is
  written as `resume-refused-*.json` and the campaign is INCONCLUSIVE for that reason.
- **Ledger.** `<output>/ledger.jsonl`: campaign, part-start, attempt (logical id, attempt id, part, block, source
  hashes, findings, scores, record incl. decision-72 floors), drain, block-close (witness offsets), burst,
  part-end (reason, gates, restore witness, wall time, file manifest), campaign-close. `verdict.json` and
  `state.json` are derived from it; `analyze` recomputes them.
- **Drain allowance (v9.9).** Smoke and acceptance use the fixed declared 4.0 s. `run` checks the hash-verified
  smoke ledger (every ordinary attempt boundary <= 2.0 s, none unbounded, lifecycle and burst readiness) and
  records the allowance in the campaign row; otherwise it refuses to launch (readiness review). A readiness
  replay of the retained v9.8-smoke-01 ledger under this rule (test only; its plan sha is v9.8's) is READY.
- **Closure.** FAIL and INCONCLUSIVE closures are final. A closed PASS keeps its record but the current
  outcome follows the evidence: a repair that removes support or exhausts the cap withdraws it to INCONCLUSIVE,
  a candidate fault to FAIL; acquisition never reopens. A recorder RTVI shortfall is always PAUSE-PART. Capture-deadline attempts first establish
  the barge opportunity like a complete attempt and are re-scored offline from their partial descriptor.
- **Analyzer repair.** `rescore --plan --out --amendment <reviewed diff>` records the amendment hash, accepts
  changed hashes only for analyzer files and re-scores every retained attempt; it never reopens a FAIL.
- **Stop safely.** Ctrl-C in the launching terminal, or `kill -TERM` the exact Python child
  (`pgrep -af 'python hil/echoear_aec_v9.py'`, check `--plan`). Never SIGKILL, never signal `uv`, never release
  the lock by hand. The stop is `operator.stop` (PAUSE-PART): the part settles, restores and releases the lock; a
  restore failure parks with the lock held. A stopped part can be resumed while parts and the ceiling remain.

## Appendix: gate registry

Generated by `echoear_aec_gate_registry.markdown_table()`. The test fails if this table drifts from the registry.
The source column is file:line at 69b3d8c, the line where the gate is enforced today.

<!-- registry:begin -->
| id | class | applies to | gate | evidence for classification | source today |
|---|---|---|---|---|---|
| `echo.whole_reply` | SCORE | echo | Integrated reduction over the reference-active whole reply >= 20 dB (unchanged). | Valid playback extent, fixed 1024-sample AFE delay, independent MIC3 activity mask. | echoear_aec_campaign_analysis.py:30 |
| `echo.first_500ms` | SCORE | echo | First 500 ms reduction >= 10 dB when source first 500 ms > -30 dBFS (unchanged). | Source first-500 ms level from aligned context PCM; complete frames only. | echoear_aec_campaign_analysis.py:31 |
| `echo.post_guard` | SCORE | echo | Post-guard AFE residual <= -50 dBFS full band (unchanged; never relaxed). | Guard-open reference-active frames mapped from the GATE log. | echoear_aec_campaign_analysis.py:32 |
| `echo.guard_closure` | SCORE | echo | Guard closed at speech onset after >=2 s silence; a witnessed wrong state is a scored miss; unknown gate state is echo.unscorable. | GATE log states mapped to every reference-active frame. | echoear_aec_campaign_analysis.py:156 |
| `echo.hard_floor` | SCORE | echo | Policy limit: whole reply < 15 dB or post-guard > -40 dBFS on any valid echo cell fails acceptance. | Same measurement as echo.whole_reply / echo.post_guard on a valid cell. | new in v9 |
| `echo.false_turns` | SCORE | echo | 0 accepted false turns and 0 false interruptions on every echo reply (harm gate). | Positive evidence (accepted UserStartedSpeaking / InterruptionFrame in the reply or its tail); survives alignment, observer or coverage loss; erased only by an independent room witness time-associated with that specific event (per-event ids). | echoear_aec_campaign_analysis.py:419 |
| `barge.false_interruption` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Unsolicited accepted turn or interruption: not positively associated with the declared near utterance (e.g. any before the mapped near-onset lower bound, or a further turn after the associated one outside [near onset, near-end upper bound + 2 s] whose transcript does not match the near text). A late accepted response associated with the near utterance is an ordinary latency/outcome SCORE miss, not a false event. Intended barge-in is not false. | Event timestamps mapped to the capture timeline with the declared onset uncertainty, plus the event/transcript association to the near utterance; same survival rule as echo.false_turns. | new in v9 |
| `echo.stratum_bar` | SCORE | echo | 20 valid echo cells per volume (0 and -6 dB; 10 texts x 2 generations); <=2 failures per volume; no transfer. | Ledger rows for all 40 logical echo cells; first valid attempt of each is the scored one. | echoear_aec_nova_live.py:276 |
| `echo.event_coverage` | INVALID-CELL | echo | Credit needs event coverage from reply start through >= 2 s of declared post-reply quiet, with matching peer/context identity and complete recorder coverage (no gap, truncation or rotation); zero rows alone are not coverage. | Event schema frames present (VAD/UserStarted/Interruption, frame_id, bot_active) for the cell window. | echoear_aec_campaign_analysis.py:418 |
| `echo.unscorable` | INVALID-CELL | echo | Applicable echo endpoint unscorable (playback extent, incomplete delayed tail, no active frames, unknown gate frames, no trustworthy playback extent) -> invalid, never a pass; a transport fault alone never makes a measurable echo unscorable. | Analyzer reason string; playback present (else playback.missing). | echoear_aec_campaign_analysis.py:120 |
| `echo.zero_db_floor` | MONITOR | echo, part | Decision 72: the -58 dBFS full-band part-baseline screen is recorded only. Both volumes use the same noise/clip validity gates; the benchmark is room-inclusive (thresholds judged as heard, room included). | Part baseline full-band level. | new in v9 |
| `echo.floor_record` | MONITOR | echo, part | Raw MIC1 and pre-guard AFE full band (and HPF) for each part baseline and each cell pre/post quiet window; drift by volume. Recorded with every verdict. | Baseline and quiet-window records. | new in v9 |
| `echo.residual_report` | MONITOR | echo | Median frame reduction, whole/first500/post-guard residuals, max-20 ms residual after every re-onset, guard reports. | Analyzer output. | echoear_aec_campaign_analysis.py:154 |
| `echo.median_by_volume` | MONITOR | echo | Median whole-reply reduction and post-guard residual per volume; per-text failures and double-replicate failures. | Scored ledger rows. | new in v9 |
| `echo.floor_relative` | MONITOR | echo | Floor-relative echo metrics (report-only module, asymmetric verdict); never a gate before live-noise validation. | Retained captures. | echoear_aec_floor_relative.py:441 |
| `echo.quick_gap` | MONITOR | echo | Quick-gap witness classification (no v9 quick-gap pairs are scheduled). | Independent MIC3 gap. | echoear_aec_campaign_analysis.py:531 |
| `barge.recognition` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Stop: normalized transcript contains the whole word "stop"; sentence: WER <= 0.20 (unchanged). Ordinary STT misses stay scored. | Final transcript events for the cell. | echoear_aec_accept_analysis.py:150 |
| `barge.credited` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Credited barge-in = recognition AND accepted interruption after near onset AND eligible/lull opportunity with verified future speech. | Opportunity class, InterruptionFrame after near onset, future-speech witness. | echoear_aec_accept_analysis.py:169 |
| `barge.latency` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Near onset to accepted interruption, conservative upper bound: <= 1.2 s Stop, <= 2.0 s sentence (unchanged). | Mapped near onset with +-320 sample uncertainty; missing bound = fail. | echoear_aec_campaign_analysis.py:433 |
| `barge.group_bar` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Three -6 dB groups (firm Stop, firm sentence, normal sentence): 12 scheduled, >=10 eligible, >=9 successes among the first 10 eligible by frozen logical rank (not acquisition order); membership is final only when every lower rank is resolved; early FAIL only when two misses are in the final ten under every legal completion. | Ledger in declared order; opportunity classes per attempt. | echoear_aec_nova_live.py:271 |
| `barge.eligibility` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Eligible / ongoing-reply-lull opportunity definitions and near-match rule (abs(r) >= 0.25, >= 1.1x competing peak), unchanged. | Fixed-delay correlation, guard open over onset uncertainty, active MIC3, >=100 ms future speech. | echoear_aec_accept_analysis.py:23 |
| `barge.no_opportunity` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Witnessed MIC3 silence over the onset uncertainty: scheduled but not eligible; no refill (the two extra slots absorb it). | Complete evidence with witnessed silence; partial coverage is barge.unscorable. | echoear_aec_accept_analysis.py:42 |
| `barge.in_window` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Guard-closed overlap of the onset uncertainty (in-window) -> invalid with refill (v8.7: unresolved, failed the session). | Gate state at near onset from the GATE log. | echoear_aec_accept_analysis.py:136 |
| `barge.unscorable` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Opportunity unscorable (missing witness, <100 ms verified future speech) -> invalid with refill. | Analyzer opportunity class unscorable-*. | echoear_aec_campaign.py:181 |
| `barge.cancelled_lull` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Lull cancelled after the stimulus: if an eligible ongoing reply is independently established (pre-near source and retained future speech), the cell is scored as eligible (barge.eligibility); only when opportunity cannot be established -> invalid with refill. Counts reported separately. | Lull cancellation witness plus independent pre-near/future-speech evidence. | echoear_aec_lull.py:54 |
| `barge.undelivered` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Stimulus not delivered: no fresh active TTS at the declared deadline, no non-silent transport anchor, or stimulus precondition failure (v8.7: stopped c09/c10). | Timer record with skip reason; transport-write events. | echoear_aec_stimulus.py:107; echoear_aec_campaign.py:125 |
| `flush.callback` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Flush: callback to last old MIC3 <= 100 ms; server interruption to silence <= 250 ms; pre-callback active tail within 300 ms (unchanged). | AUDIO_CANCEL callback, mapped MIC3 blocks, callback witness v2. | echoear_aec_campaign_analysis.py:206 |
| `flush.next_reply` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | After an accepted interruption the next non-silent TTS reaches MIC3 within 1 s (unchanged). | Second playback witness for the cell. | echoear_aec_campaign_analysis.py:440 |
| `flush.interrupt_copies` | SCORE | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Both interrupt copies observed in the SCTP ring (part of flush pass). Scored only with healthy observation; observer failure is observer.health, not a missing-copy miss. | SCTP ring with TSNs and first-send ACK. | echoear_aec_campaign_analysis.py:431 |
| `flush.stale_audio` | CANDIDATE-FAIL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Stale old-reply speech frames after mute: 0. | Blocks of the old reference after control_us + 150 ms. | echoear_aec_campaign_analysis.py:200 |
| `flush.diagnostics` | MONITOR | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Prefix loss, cut-step spike, signed correlation, sentence overlap duration: report-only. | Analyzer output. | echoear_aec_campaign_analysis.py:211 |
| `report_only.exposures` | MONITOR | report-only | Normal-voice Stop, host-interrupt, sentence-split regression and 0 dB barge exposures: report-only, no spares and no acoustic credit; candidate faults and false events still apply. | Their rows. | echoear_aec_nova_live.py:285 |
| `near.preservation` | MONITOR | near-witness | Near-end preservation (<=1 dB raw MIC1 vs pre-gate AFE): report-only, as in v8.7 Nova. | Near-only cells. | echoear_aec_campaign_analysis.py:424 |
| `diagnostics.report` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | 100 Hz high-pass residual and stimulus-level diagnostics; never gates. | Analyzer output. | echoear_aec_campaign_analysis.py:135 |
| `alignment.near_witness` | PAUSE-PART | near-witness | Near-only alignment witnesses at start and end of each block: each offset within 1024 +- 20 samples and mutual spread <= 20 samples, measured by the v9.8 witness estimator (fixture match, polarity-free envelope delay and relative AFE-vs-raw delay must all agree within +-20 inside 1004-1044; a half-pitch opposite-polarity alias is accepted only with its own checks; the relative estimate carries a declared 1 % ambiguity margin; anything unresolved is ambiguous). The fixed 512+512-sample framer/AFE delay makes 1024 the expectation, not a substitute for the witness. v9.10: an invalid witness (noise-contaminated or ambiguous/out-of-tolerance) is retaken up to 2 more times per position while it fits the part budget and admission; PAUSE-PART after the third. Failure leaves the block unclosed: its acoustic endpoints lose credit, faults and false events stay. | Witness match offsets; boot banner AEC_GUARD_CONFIG. | echoear_aec_campaign.py:441 |
| `parts.block_closure` | PAUSE-PART | part | Block = <= 5 acquired scored-cell attempts between two witnesses. A block opens only if all its attempts and the closing witness fit the active deadline and the 210 s start rule; otherwise it stays unattempted. Only closed blocks earn acoustic credit; each invalid attempt is counted once. | Ledger block records. | echoear_aec_campaign_analysis.py:524 |
| `caps.invalid_attempts` | PAUSE-PART | campaign | Caps: <=2 acquired attempts per logical cell, <=8 invalid acquired scored-cell attempts per campaign (each counted once, including unclosed-block attempts), 3 consecutive invalid -> PAUSE-PART; unopened blocks are not attempts; exhausted -> INCONCLUSIVE. | Hash-chained attempt ledger. | echoear_aec_noise_gate.py:124 |
| `parts.time_caps` | PAUSE-PART | part | At most 3 entered parts (2 planned + 1 recovery), planned parts each <=60 min incl. its own 6 min restore reserve, the recovery (last) part up to the ceiling left (v9.6), 150 min ceiling over part wall time; capacity is the remaining campaign wall time (v9.5/v9.6); >=210 s active time to start a cell. | Part monotonic clocks; restore reserve. | echoear_aec_nova_live.py:295; echoear_aec_campaign_analysis.py:609 |
| `parts.ledger` | PAUSE-PART | campaign | Resume integrity: hash-chained ledger, same image/profile/plan/runtime hashes (except an approved analyzer-only provenance amendment recorded in the ledger), fresh boot, no attempt reuse. Failure refuses resume -> INCONCLUSIVE. | Ledger chain, prior restore witness. | echoear_aec_campaign_resume.py:41 |
| `outcome.completeness` | SCORE | campaign | All required gates conjunctive; a missing required scored cell or metric at campaign end -> INCONCLUSIVE (not PASS). | Ledger. | echoear_aec_nova_live.py:279 |
| `launch.integrity` | PAUSE-PART | part | Freeze/launch preconditions: plan, runtime, profile, package, fixture, image and boot-gain hashes; USER-GO; source preflight. Refuse to start or continue a part. | Validation exception text. | echoear_aec_nova_live.py:167 |
| `freeze.admission_regression` | PAUSE-PART | campaign | aec-v87 host regressions pass at freeze: 20- and 64-message bursts and byte-budget exhaustion, ordered exactly-once dispatch, admission_overflow observable. | Host test log bound to the candidate commit. | new in v9 |
| `freeze.burst_smoke` | PAUSE-PART | campaign | Decision 71 smoke burst: admission_overflow == 0, live quiescent reconciliation, deferred delta > 0 and ordered exactly-once APPLICATION receipt of burst IDs; deferred == 0 is coverage not achieved (bounded, predeclared burst attempts). | Smoke ledger rows and serial/recorder burst records. | new in v9 |
| `freeze.lifecycle_test` | PAUSE-PART | campaign | Host lifecycle test of the frozen schedule through the real hold classes with fault injection and resume. | Test log bound to the runner hash. | new in v9 |
| `freeze.analyzer_dry_run` | PAUSE-PART | campaign | v9 scorer completes over retained campaign-10, nova-live-02 and arms-04 evidence without exception. | Dry-run output bound to the analyzer hash. | new in v9 |
| `freeze.scoring_cost` | PAUSE-PART | campaign | Measured per-attempt scoring + ledger append/checkpoint cost <= 2 s and independent of ledger length. | Benchmark over retained captures and synthetic ledgers of campaign length. | new in v9 |
| `operator.stop` | PAUSE-PART | part | Operator Ctrl-C/TERM: restore and close the part. | Signal record. | echoear_aec_campaign.py:1052 |
| `unknown.unclassified` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Any exception or failed check not mapped to another entry (v8.7 default: device-fault session stop). | Exception text retained; classification by review before resume. | echoear_aec_campaign.py:131 |
| `restore.integrity` | PAUSE-PART | part | Normal ELF restored and boot-verified, mixer byte-equal, lock released, no cleanup errors; failure parks with the lock. | restore-image.json, mixer states, lock record. | echoear_aec_campaign.py:927 |
| `host.serial_loss` | PAUSE-PART | part | Serial/host failure without device-reboot evidence (USB, host OSError). | No boot banner or reset reason in candidate-serial.log; device_us continuous on reconnect. | echoear_aec_campaign.py:127 |
| `device.reboot_crash` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Candidate reboot, panic, watchdog or crash during a part, including during setup before connect (v9.7/v9.8: scanned across the candidate boot/setup/live interval before any setup-only exemption; only rst:0x15 USB_UART_CHIP_RESET as the first log line after the ROM banner is the expected host-triggered boot; any other reset reason, a reset after candidate output, or panic text is a crash; AEC_GUARD_CONFIG lines are not boots; the restore boot is in restore-serial.log). | Boot banner/reset reason mid-part, device_us regression, or panic text in candidate-serial.log. | new in v9 |
| `bench.audio_device` | PAUSE-PART | part | Jabra not ALSA card 0 / mixer unavailable. | ALSA query. | echoear_aec_bench.py:281 |
| `noise.cell_quiet_windows` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Noise gate v3 (100 Hz HPF) on the 2 s pre/post quiet windows: <= -55 dBFS, rise <= +6 dB over max(baseline, -61), transient <= -46 dBFS. | Quiet-window gate record per attempt. | echoear_aec_campaign.py:663 |
| `noise.part_baseline` | PAUSE-PART | part | Settled part baseline <= -53 dBFS (HPF) within 3 attempts, else the part cannot start. | baseline-N/noise-gate.json. | echoear_aec_campaign.py:472 |
| `quiet.board_silent` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | MIC3 active in a pre/post quiet window (device playing outside the cell) or a gap block in the quiet window: cause unknown. | MIC3 frame power vs quiet reference. | echoear_aec_campaign.py:661 |
| `clip.mic_room` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | MIC ADC/gain clip witnessed as room sound (diag-02-r4 precedent). | Time-local: the clipped raw MIC1 samples matched to the counter interval lie outside every declared stimulus extent, or inside a failed quiet window that contains them; a failed quiet window elsewhere does not attribute the clip. | echoear_aec_resources.py:59 |
| `clip.mic_stimulus` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | MIC clip inside a declared stimulus extent at declared level with clean quiet windows (gain/level configuration fault). | Positive causation: clipped samples coincide with the declared, correctly leveled stimulus waveform (fixture or MIC3-aligned echo) at its calibrated level; clean bookend quiet windows alone are insufficient. | new in v9 |
| `clip.mic_unattributed` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | MIC clip with neither a room witness nor a stimulus attribution. | Neither time-local room attribution nor positive stimulus causation (e.g. a clip inside the stimulus with no independent evidence). | new in v9 |
| `clip.afe_output` | SCORE | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | AFE output clip under a declared stimulus: the cell fails (scored miss). | aec.output_clips delta inside the cell window with a declared stimulus; outside any stimulus -> unknown.unclassified. | echoear_aec_resources.py:58 |
| `clip.reference` | SCORE | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Verified level/config clip of the actual declared playback with intact reference mapping: the cell fails (ordinary miss). Clipping outside a scored cell, broken mapping or missing integrity evidence -> reference.health / unknown.unclassified. | aec.reference_clips delta inside the cell window. | echoear_aec_resources.py:58 |
| `hardware.aec_errors` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | aec.errors = 0. | PERF counter delta (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:57 |
| `hardware.audio_io` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | I2S/audio/capture faults = 0: capture read errors/timeouts, audio drops/read/write errors/timeouts, PCM queue drops, I2S underflow/write errors/timeouts, live rx_overflows, capture gap blocks beyond one. | PERF counter deltas (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_accept.py:42 |
| `hardware.startup_rx` | MONITOR | part | Startup rx_overflows before Ready: retained separately. | connected.json. | echoear_aec_resources.py:137 |
| `hardware.console` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Console output queue full / transport drops (PERF/evidence channel loss; attribution harness load vs firmware unknown). | PERF counters. | echoear_aec_resources.py:57 |
| `hardware.render_ledger` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Render ledger submitted - completed == 640 - completed % 320 at every active snapshot. | Boundary PERF snapshots (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_campaign_analysis.py:409 |
| `hardware.uplink_frame` | CANDIDATE-FAIL | part | Uplink frame 20 ms in every session snapshot (identity). | PERF uplink_frame_ms (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:70 |
| `resources.heap` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Internal heap minimum >= 81920 B and largest block >= 32768 B. | PERF heap (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:27 |
| `resources.failed_alloc` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Failed allocations = 0. | PERF failed_allocations.total (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:53 |
| `resources.stacks` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Stack floors: playback max(4096, stack/4) (4288), capture/tx/peer-loop 4096, peer-rx 1024, app-control 2048. | PERF tasks (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:8 |
| `resources.cpu` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Measured idle < 25 % per core in a qualifying >= 5 s steady window blocks resource PASS; < 20 % is the immediate stop. No qualifying window -> resources.coverage, not a low-idle result. | PERF steady windows. | echoear_aec_resources.py:50 |
| `resources.encode` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Opus encode p95 < 20 ms, max < 40 ms. | PERF encode stats (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:37 |
| `resources.display` | CANDIDATE-FAIL | part | Character enabled, posts increase, display errors/DMA errors/send failures/gate timeouts = 0; asset preload succeeds. | display-end.json, character-start/end.json (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_resources.py:142 |
| `resources.coverage` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | PERF snapshots, fields and steady windows present for the interval; missing, malformed or reset counters are coverage/identity failures, never a measured resource breach and never a pass. | Snapshot files. | echoear_aec_resources.py:18 |
| `resources.part_audit` | PAUSE-PART | part | Legacy hold-level composite audit (all snapshots, exception audit, final integrity, completed flag). Dispatches to its failing leaf gates (each keeps its own class, e.g. an AFE clip stays SCORE, a room clip INVALID-CELL); an unresolved composite or completed == false without a leaf (operator/host interruption) -> PAUSE-PART. v9.11/v9.12: after a positive (hardened) peer-loss witness the bot-side final stages are not applicable and the unacknowledged evaluation control is attributed to the classified transport exit; the device-side stages must still record their witnesses; device leaves are audited over every observation incl. reconnected generations; PENDING USER CONFIRMATION: only send_cutoff/send_budget_cutoffs in the exit interval of the failed acquisition are exit-associated, every other internal loss stays CANDIDATE-FAIL. v9.7: completed == false after an exception resolves only when every applicable final-audit stage has its completion witness (resources, exception integrity audit, disconnected snapshot, SCTP recovery) and there is no finish_error. v9.8/v9.9: every applicable counter and resource leaf is reconciled through the terminal disconnected snapshot and dispatched to its gate before completion is resolved (absolute-zero counters, heap floors, rx_overflows as a live delta from connected, drop_reasons/admission_overflow deltas -> admission.control_loss, console drops -> hardware.console, clip residuals); session presence, CPU windows, encode activity and uplink-frame identity do not apply after teardown. A setup-only part (no live peer) audits measured, applicable snapshot leaves only (heap, largest block, allocation, stack of existing tasks, hardware counters); session leaves are not applicable. | Leaf check results of check_directory / exception audit. | echoear_aec_campaign.py:850 |
| `admission.control_loss` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Proven control-message loss after the aec-v87 fix: admission_overflow delta > 0, admission/rejection/queue drop deltas > 0 (v9.10: the received-but-permanently-undispatched branch is withdrawn: the aec-v87 firmware has no device-side witness of it, so a permanent shortfall is admission.drain_timeout); recorder count must not exceed device deltas once transport delivery is resolved (v9.3: host evidence never resolves it, so a recorder-vs-device shortfall alone is unknown.unclassified / PAUSE-PART; CANDIDATE-FAIL comes only from device-side positive witnesses, incl. a duplicate or misordered application receipt). | data_admission (received, dispatched, deferred, admission_overflow) and drop_reasons.* deltas in one peer epoch; recorder RTVI count. | echoear_aec_campaign.py:678 |
| `admission.drain_timeout` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Live quiescent received == dispatched not reached within the drain allowance with zero drop/overflow deltas (cause unresolved). Allowance (v9.9): a fixed declared 4.0 s for smoke and acceptance, from the pooled v9 smoke boundary distribution (90 ordinary attempt boundaries, no-backlog <= 0.76 s, backlog boundaries 1.40 / 1.52 / 1.54 s; 2.6x headroom), within the 5 s receipt timeout, never changed during the campaign; smoke readiness requires every ordinary boundary <= 2.0 s and none unbounded (v9.4-v9.8: 2x the largest smoke boundary, rounded up to 250 ms, <= 3 s). v9.3: the burst last-send-to-sustained-quiescence latency is MONITOR (it must fall within the 5 s receipt timeout) because it is dominated by the device applying 40 test messages that each log two serial lines. | Retained drain observations; smoke calibration record. | echoear_aec_resources.py:117 |
| `admission.counter_schema` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Admission counters (received, dispatched, deferred, admission_overflow) present with the aec-v87 schema, same peer identity, no reset inside an interval. | PERF schema per snapshot. | new in v9 |
| `admission.boundary_identity` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Admission boundary invalid: peer identity changed or malformed counters during the drain poll. | Retained boundary observations. | echoear_aec_resources.py:114 |
| `delivery.firmware_drops` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Proven firmware-internal loss = 0: drop_reasons.* (queues, admission, send_cutoff, uplink_gap, worker_exception), send_budget_cutoffs, live rx_overflows. Never waived by a simultaneous send_errors split. v9.12 (PENDING USER CONFIRMATION): the one exception is send_cutoff / send_budget_cutoffs inside the failed acquisition's exit interval after a witnessed peer loss (exit-associated, reported); every other counter stays strict. | Per-counter deltas in one peer epoch. | echoear_aec_campaign_analysis.py:346 |
| `delivery.send_errors` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | send_errors delta without established cause. An independently established network-only failure follows the transport rule (barge overlap INVALID-CELL, echo MONITOR). | Counter delta plus any independent network witness. | new in v9 |
| `delivery.counters_dispatch` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Legacy delivery_counters composite (send_errors + internal drops): dispatches per counter to delivery.firmware_drops / delivery.send_errors; unresolved -> PAUSE-PART. | Per-counter deltas. | echoear_aec_campaign_analysis.py:352 |
| `transport.downlink_barge` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Downlink transport (loss <= 2 %, ge6 ended run 0, post-PLC zeros 0, starvation 0, valid histogram) overlapping the barge-in/flush stimulus window -> invalid; outside the window: no effect. | Interval evidence: ordered PERF snapshots bracketing the stimulus window, same pc_id/generation; a whole-part delta is not assigned to every cell. Starvation/post-PLC zeros are playout counters, not I2S faults; a concurrent local fault keeps its own class. | echoear_aec_campaign_analysis.py:318 |
| `transport.downlink_echo` | MONITOR | echo | Downlink transport faults never invalidate echo cells (AEC reference = DAC output incl. PLC); recorded per cell. | Same counters; if no usable stimulus exposure remains the echo is echo.unscorable, never PASS. | new in v9 |
| `transport.burst` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Burst <= 3 and missing-run histogram: report-only since v8.3. | Histogram deltas. | echoear_aec_campaign_analysis.py:324 |
| `transport.export` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Export-interval transport/delivery effects: report-only; never suppresses a candidate fault or capture-export evidence failure. | export-event.json. | echoear_aec_campaign.py:683 |
| `transport.sctp_recovery` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Active SCTP T3 without same-pc_id clean SACK recovery overlapping a barge stimulus window -> invalid (echo: recorded only); SCTP flight patch must be enabled. | SCTP ring. | echoear_aec_campaign.py:859 |
| `transport.environmental_classifier` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | v8.4 transport-environmental classifier and caps: disabled since v8.5; superseded by transport.downlink_barge. Witnesses may be retained. | Classifier witness. | echoear_aec_campaign.py:959 |
| `transport.qualification_bar` | MONITOR | campaign | Transport qualification (separate, network-only, diag-04 form): no >=6 missing run, no zeros, loss <= 2 % over 75 min. Not an AEC gate. | Separate network-only run. | new in v9 |
| `capture.interval_checks` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | v8.7 composite capture-interval stop (resources/admission/RF/delivery/transport). v9 reclassifies by the failing component; unresolved composite -> PAUSE-PART. | checks record in the attempt result. | echoear_aec_campaign.py:692 |
| `observer.health` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | SCTP/RTP/arrival observers healthy, supported, untruncated over the attempt. | rtp-observer-health events. | echoear_aec_campaign.py:176 |
| `observer.event_stream` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Evaluation/recorder event stream rotated, truncated, rewritten, over-backlogged or ambiguous context. | EventStreamChanged text. | echoear_aec_event_tail.py:58 |
| `liveness.peer` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, part | Liveness ping/pong (0.5 s cadence, 2 s timeout): alarm, device timeout, stopped pongs or peer close -> lost peer, recondition in a new part. Positive reboot, admission-loss or stall evidence takes its CANDIDATE-FAIL gate; absence of a reboot banner is not proof of health. | PERF liveness counters, liveness-alarm events. | echoear_aec_resources.py:82 |
| `bot.wire_metrics` | PAUSE-PART | part | No RTVI metrics messages forwarded to the device (bot configuration). | Recorder RTVI output. | echoear_aec_campaign_analysis.py:509 |
| `sctp.cookie_echo` | MONITOR | part | COOKIE ECHO count per epoch. | SCTP ring. | plan only |
| `bot.pipeline_error` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Bot pipeline errors: FatalErrorFrame, evaluation-error, TTS reconnect refused/failed, unattributed ErrorFrame, unacknowledged control, unfinished generation after a failure. | Evaluation events; bot log. | echoear_aec_campaign.py:601 |
| `bot.service_outage` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Independently logged STT/TTS/LLM service outage overlapping the cell. Ordinary misrecognition, late turns and long TTS lead are not outages. | Vendor error frame or connection failure logged by the bot with timestamp inside the cell window. | new in v9 |
| `bot.profile_binding` | PAUSE-PART | part | Live STT profile and vendor model (general-nova-3 2025-04-17.21547) match the plan; a change mid-campaign refuses resume (INCONCLUSIVE). | stt-profile / stt-model-observed events. | echoear_aec_campaign_analysis.py:527 |
| `tts.long_lead` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Proactive TTS leading silence >= 0.8 s: diagnostic when the scored interval is complete (Sol item 4). If it removes the stimulus opportunity the cell is barge.undelivered/unscorable; a late or absent response to a valid near stimulus stays SCORE. | Complete generation audit for the attempt. | echoear_aec_campaign.py:721 |
| `tts.generation_witness` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Generation lifecycle audit incomplete (missing metric context, write/metric mismatch, unclosed generation) -> evidence integrity unknown. | generation_audit sessions. | echoear_aec_campaign_analysis.py:83 |
| `tts.diagnostics` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | After-Stop request/first-write timing: diagnostic. | Events. | plan only |
| `playback.missing` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Positive device receipt (arrival/decode counters for the context) of a non-silent write but no MIC3 render within 1 s. Clean transport counters support but do not prove delivery; unknown attribution -> PAUSE-PART; no usable echo exposure -> echo.unscorable. | Write event, source/MIC3 match, clean transport over the window. | echoear_aec_campaign_analysis.py:103 |
| `playback.transport_witnessed` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Barge/flush: missing playback with a witnessed downlink fault overlapping the write window -> invalid, re-attempted. Echo never uses this entry. | Downlink fault in the same window. | new in v9 |
| `reference.health` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Reference health: playback power >= 1024 LSB^2, ratio floor 31.623 ppm at 15 dB, low_windows delta 0 (unexplained reference integrity). | PERF aec.reference_health (valid observations; missing or malformed data -> resources.coverage). | echoear_aec_campaign_analysis.py:412 |
| `reference.probe` | CANDIDATE-FAIL | probe | Canonical DAC probe calibration (not scheduled in v9 or v8.7 Nova). | Probe capture. | echoear_aec_campaign.py:893 |
| `timing.capture_bracket` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Capture blocks bracketed by clock anchors (interval-bracket-v1), mapped coverage of the scored interval, no timing gap. | anchors.json, capture timing rows. | echoear_aec_timing.py:81 |
| `timing.host_clock` | PAUSE-PART | part | Host monotonic clock regressed. | Clock samples. | echoear_aec_campaign.py:647 |
| `timing.single_gap` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Firmware-marked capture gap block with unknown attribution (the v8.7 exactly-one-gap retry is withdrawn). | Capture timing rows. | echoear_aec_campaign.py:170 |
| `capture.gap_host_export` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Capture gap demonstrated to be a host/export measurement defect (device PCM continuous). | Device-side continuity witness plus host export error. | new in v9 |
| `capture.gap_firmware` | CANDIDATE-FAIL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Proven firmware PCM discontinuity in a scored capture. | Device capture counters/serial attribute the gap to firmware. | new in v9 |
| `timing.stimulus_lateness` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness | Stimulus invocation abs(error) <= 100 ms (equality allowed), the single authoritative threshold: the v9 plan sets timing.timer_lateness_max_s = 0.100 (V9_PLAN_VALUES). Near-onset mapping uncertainty is separate. | Timer record. | echoear_aec_stimulus.py:103 |
| `capture.cap` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Capture <= 28 s (24 s workload deadline): invalidates only still-unresolved endpoints; an already established latency/recognition miss stands. | capture-duration.json. | echoear_aec_campaign.py:621 |
| `capture.export` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Console capture export within 2.6 s per capture second + 5 s and decodes completely. | Export record; decode error text. | echoear_aec_campaign.py:154 |
| `harness.jabra_duration` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness | Jabra playback duration error within [-0.10, +0.25] s. | jabra-outcome record. | echoear_aec_campaign.py:80 |
| `harness.evaluation_ack` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Evaluation command acknowledged within 10 s. | Event log. | echoear_aec_campaign.py:528 |
| `retry.clean_witness` | PAUSE-PART | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | A re-attempt needs clean device/observer witnesses over the failed attempt; an unclean composite is reclassified by its failing component, else PAUSE-PART. | acquisition-failure.json. | echoear_aec_campaign.py:791 |
| `post_seal.errors` | MONITOR | part | Expected transport teardown effects (T3, liveness alarms) after the active end = the final settled-end boundary of the part, before audio off/disconnect. Heap, crash, I2S, admission-loss and restore faults count whenever observed, through restore. | Events after the last seal. | plan only |
| `rf.cell_pre_gate` | INVALID-CELL | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only | Per-cell host RF observation over the existing ~5 s pre-cell wait on wlp8s0, limit scaled by actual elapsed time at 20 MB/300 s (about 333 kB per 5 s); missing/reset counters fail. Applies before acquisition at either volume. | rf-pre-gate.json. | echoear_aec_campaign.py:545 |
| `rf.part_pre_gate` | PAUSE-PART | part | Part-start 30 s RF pre-gate (about 2 MB at 20 MB/300 s) before any flash; the only 30 s window. | rf-pre-gate.json. | echoear_aec_campaign.py:431 |
| `rf.capture_window` | INVALID-CELL | barge-firm-stop, barge-firm-sentence, barge-normal-sentence | Host RF byte gate over the capture window: barge INVALID-CELL only when the declared evidence rule places the excess inside the stimulus window (per-second counters); whole-capture totals alone do not; echo recorded only. | rf.json. | echoear_aec_campaign_analysis.py:411 |
| `rf.hold_active` | MONITOR | part | Whole-hold RF byte total. | rf_active. | echoear_aec_campaign.py:853 |
| `rf.diagnostics` | MONITOR | part | wifi-stats at every boundary: evidence only. | wifi-stats logs. | echoear_aec_campaign.py:46 |
| `link_probe.part` | PAUSE-PART | part | 240 s idle link probe per part: uplink unique loss <= 0.5 %, no >= 1 s gap, observer healthy, idle, resources clean (resource failure -> its CANDIDATE-FAIL entry). | link-probe/result.json. | echoear_aec_campaign.py:512 |
| `host.environment` | PAUSE-PART | part | Host BSSID lock b4:20:46:f4:ea:e5 on profile fb56e504, signal >= -60 dBm, 0 supplicant changes; prelaunch refuses, in-run pauses. | host-gate records. | echoear_aec_host_gate.py:123 |
| `host.periodicity` | PAUSE-PART | part | Two adjacent qualifying 304.4 s host-scan lattice cycles in uplink -> pause (host boot id re-derived per freeze). | periodicity.json. | echoear_aec_host_gate.py:156 |
| `reconnect.cycle` | CANDIDATE-FAIL | reconnect | Reconnect cycle gates (two epochs, active disconnect, new reply). No reconnect cells in v9; integration result covers reconnect. | Reconnect records. | echoear_aec_campaign.py:828 |
| `soak.block` | CANDIDATE-FAIL | soak | Soak block gates (600 s resource coverage). No soak in v9. | Soak records. | echoear_aec_campaign.py:1040 |
| `analyzer.exception` | MONITOR | echo, barge-firm-stop, barge-firm-sentence, barge-normal-sentence, near-witness, report-only, campaign | Analyzer/scorer exception: the attempt score is pending; pending scores withhold credit, the verdict and every score-dependent decision (retry slot, group completion, early stop). Acquisition continues only on the declared schedule while live fault checks stay healthy. Repair offline with an approved provenance amendment; never re-run on hardware, never reopen a finalized FAIL. | analysis-failure.json and retained captures. | echoear_aec_campaign.py:88 |
<!-- registry:end -->
