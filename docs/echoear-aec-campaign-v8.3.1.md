# Campaign-07 freeze v8.3.1

Coordinator decisions19 A/B/C amends the prospective v8.3 declaration. The
candidate remains ELF `61aa38d5`; no firmware rebuild or bench work is required.
Earlier campaigns retain their original evidence and receive no acceptance credit.

The playback matcher detects the source onset and extracts its 4000-sample
prefix after the candidate's playback high-pass. The public resolved config is
extracted from the retained build whose ELF hash equals the candidate, then bound
into the declaration and `candidate-playback-config.json`. It specifies 500 Hz;
the algorithm is the firmware's causal second-order Butterworth biquad. Only
source PCM is filtered, with zero initial state per context. MIC3, activity
threshold (1024 LSB squared per 20 ms), correlation criteria, timing bounds and
the non-silent transport-write anchor are unchanged. This does not simulate
normalizer gain, compression, limiter or lookahead. Historical declarations
without the versioned template rule retain their original scorer.

Retained campaign-06 regression differs from the review's prediction: the exact
firmware filter leaves 44 of 52 source onsets unchanged and shifts eight later.
Stop-10's second reply now matches; echo-08 shifts 320 ms. Sentence-07's second
reply retains its 60 ms source onset and still matches. The other six changes
are documented in the freeze report. Even where source onsets are unchanged,
the filtered correlation peak moves 3–7 samples earlier; physical matches are
not byte-identical. Tests pin these measured results, not the predicted 49/52.
All 52 playback matches pass, but the campaign replay still fails and earns no
retrospective credit. No threshold or filter-order adjustment was made to force
the prediction; this discrepancy requires reviewer attention before launch.

The 304-second lattice stops only for two adjacent qualifying-uplink cycles.
Its phase, period, boot identity, window, witness and boot-change rejection are
unchanged. PERF late/missing sums and their flagged cycles/pairs remain explicit
report-only evidence, including pairs the old combined rule would have stopped.
Historical lattice-v1 replay retains that old rule. Transport acceptance remains
`echo-transport-v8.3`, including the 2% limit and >=6/post-PLC/starvation gates.

Each gated -6 dB voice block has seven Stop and six sentence opportunity spares.
The ordered existing spares are preserved, with new spares appended. The 0 dB
block retains two per kind. All eligibility witnesses, ten-eligible denominators,
noise/harness retries (two per ID), three-consecutive-cell pause, environmental
caps (three per hold/six per campaign), and recovery checks are unchanged. There
is no new lull class, performance retake or retrospective classification change.

Base planning rises from 206 to 227 minutes (14 extra spares at 90 s each).
`echoear_aec_campaign_budget.py` derives capture counts including all spares,
three attempts per ID, two legs per reconnect and ten soak prompts. All-attempt
planning totals 10 h 15 min. A conservative timeout/overhead envelope totals
30 h 03 min 10 s: 28 s capture, 150 s export, 5 s idle, 60 s boundary I/O and
30 s analysis per leg, plus explicit recovery, baseline, setup/restore, probe
and soak allowances. These are planning allowances, not new runner timeouts.
The available 04:15 to next-day 03:00 interval is 22 h 45 min; the conservative
envelope DOES NOT FIT. The runner has no finite guaranteed wall-clock maximum:
host I/O/analysis, environmental holds/resumes and restore-failure lock retention
are unbounded. A nominal schedule must not be presented as a worst-case guarantee.

The 294 s probe idle budget remains below the 300 s bot idle timer. Capture,
export, soak/resource and retry limits are unchanged; extra cells add session
duration, not longer individual idle windows. Lifetime RTP/DTLS/SCTP observation
caps (previously 200,000 per association) are derived at 100 records/s from each
hold's envelope and rounded upward: 4,600,000 / 3,000,000 / 2,400,000 / 1,100,000.
They do not preallocate memory; actual observer memory grows with received
records. No finite cap can cover unbounded external delay. Declaration validation
pins these derived budgets, and the runner exports the per-hold observer cap.

`hil/freeze_aec_campaign_v8_3_1.py` creates fresh `step-9/freeze-v8.3.1`, binding
code, config, reviews, fixtures and candidate for absent output `campaign-07`.
Keep `freeze-v8.3` with a SUPERSEDED marker. The new bundle contains launch and
anchored Python-child stop instructions plus a separate read-only host guard
whose final runner exec is replaced by echo. Only that guard runs in preparation.
Coordinator launch is about 04:15 after Fable APPROVE. No hold may span
03:00–04:10 local; these scripts do not add an automatic scheduling cutoff.
Per-hold flock and signal cleanup still restore/hash-verify 9936da45, restore
the Jabra mixer and release the lock. Never kill wrappers or force-unlock.
