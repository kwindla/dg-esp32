# v8.4 ongoing-reply-lull preparation (not adopted)

This isolated branch implements Fable's clarified option (a). A guarded v8.4 freeze draft is prepared separately; no adoption or campaign
launch is granted. Historical declarations retain the strict classifier. The
v8.4 draft template explicitly contains:

```json
"ongoing_reply_lull": {
  "rule": "ongoing-reply-lull-v84",
  "span_max_frames": 5,
  "horizon_after_latest_ms": 120,
  "alignment_origin_tolerance_samples": 2,
  "cancelled_expected": 0
}
```

The dictionary must match exactly and is part of the reviewed-template contract.
Adding it to an existing frozen template is rejected. The isolated v8.4 campaign template now includes the flag; this is draft
packaging, not adoption or authorization. See `echoear-aec-campaign-v8.4.md`.

## Measurement and classification

At 16 kHz, `E = near - 320`, `L = near + 320`; frames are 320 samples (20 ms).
Reference activity is still strictly above
`max(4 LSB², loudest quiet frame power × 10^0.6)`. Upstream exclusions, closed or
unknown guard, strict active onset and entirely silent onset follow the existing
paths. Only mixed/uncertain MIC3 candidates evaluate the new witness.

Find the maximal inactive runs touching `[E,L]`. Bound their **outer span**,
including any active middle between two runs, to five frames (100 ms). Require
complete active frames on both sides and gap-free coverage throughout. The right
closing frame must end no later than `L + 120 ms`; missing capture closure remains
`unscorable-mic3`. Frame reads are lazy and bounded, so no post-horizon waveform
can determine the result. `closing_frame_end_sample` records the exact right
closure. `horizon_end_sample` also includes the three onset-candidate frames
needed for the unchanged initial classification if their inspection extends
beyond an early closure; it is always ≤`L + 120 ms`. The read tracker makes that
extent auditable. The cancellation/tail tests use the exact closing-frame end.

The same-reply proof uses two disjoint 200 ms MIC3 windows ending at E, matched
against only the old context's source prefix successfully written before the
lower host bound of E. Their origins must agree within two samples. A complete
source frame with power >1,024 LSB², covered by successful non-silent writes of
the same context/peer before E, must cover or follow L and start no later than
`L + 120 ms` under both origin estimates. The last pre-written sample must map
strictly beyond the right closing-frame end. The source threshold is an existence
witness, not a prediction of MIC3 amplitude. Gaps, missing source/write evidence,
ambiguous matches or a natural end fail closed.

The earliest old-reply cancellation witness is a case/peer-scoped interrupt copy
or a device `AUDIO_CANCEL callback_us` record, conservatively mapped through the
existing anchors and raw block intervals. The minimum compatible sample is used;
missing relevant timing cannot pass. No witness means infinity (JSON `null`).
If cancellation can precede the closing frame, classify
`unscorable-lull-cancelled`, consume a spare, and never credit it. If cancellation
erases the closure, the fixed maximum horizon provides the conservative censor
bound; the muted tail cannot turn into an eligible lull or a witnessed pause.
A missing capture end remains unscorable MIC3, rather than being inferred from a
later cancellation. Events after cancellation are not used for source proof in a
censored case. Post-horizon cancellation and recognition outcomes do not select
eligible cases.

A passing mixed witness becomes `ongoing-reply-lull`, still subject to the
existing future-speech check. It counts toward the ten eligible in each existing
volume/voice/kind group, with exactly the existing recognition, accepted
interruption, latency and flush success checks. Strict active-at-onset remains a
separate diagnostic; a lull is never labelled simultaneous strict echo.

## Reporting and preparation-only overrides

Block and campaign summaries report strict/lull denominators, recognition,
interruption and success rates separately. They report `unscorable-lull-cancelled`
and enforce its expected count of zero, including replaced or report-only hold
rows so a spare cannot hide censoring. A nonzero count fails the campaign verdict
and calls for review of this witness on the faster pipeline. A materially higher
lull success rate also calls for review; no numerical materiality gate is added.

`main(..., lull_policy=RULE, output_path=...)` provides a diagnostic override
without editing retained declarations. `campaign_verdict(..., lull_policy=RULE)`
always denies actual credit/qualification, retaining `hypothetical_passed` for
host tests. Normal prospective scoring is selected by the declaration flag.
`Schedule` and the block/hold/campaign analyzers all propagate that selection.

The read-only replay re-derives all original sealed classifications without the
flag, preserves original rows verbatim, then applies the prepared option (a)
through the real block analyzer with an explicit override. It excludes the
unsealed control from denominators and records zero retrospective credit.

```sh
# From server/ in this isolated worktree:
OPENBLAS_NUM_THREADS=1 PYTHONPATH=../hil:. uv run --frozen --offline \
  --with pyserial --with pyelftools --with idf-component-manager==3.1.2 \
  python ../hil/echoear_aec_lull_replay.py \
  --evidence-base /home/khkramer/src/evidence/echoear/aec-demo/step-9 \
  --output /path/outside/evidence/replay.json
```

The output path must be outside evidence. Input hashes and analyzer hashes are
checked for changes. Synthetic tests cover span/closure/cancellation boundaries,
source-write identity/timing, threshold floors, declaration binding, denominator
behavior and horizon read limits. Actual unseen-speech, voice/volume and
board-silent prospective validation remains for a separately authorized campaign;
no bench work is part of this preparation.
