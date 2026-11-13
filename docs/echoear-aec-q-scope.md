# EchoEar AEC-only qualification: near-end scope and detection limits

This is the qualification-scope note for the aec-q runner (`hil/echoear_aec_q*.py`) under decisions 88–101. When aec-q
merges, `docs/echoear-aec-known-issues.md` should point here.

## What the near-end bar claims

The claim is that content is preserved during real double talk, as the product produces it: the bot yields about
0.9 s after the near-end onset. It covers the onset and early content, and it is measured on the local AFE output
against a paired single-talk baseline of the same fixture. A cell counts only if its eligibility rule holds
(decisions 93/94).
- **Stop:** at least 12 of the 15 source-active frames are verified double talk.
- **Sentences:** at least 75 % of the frames inside the far end's measured exposure are verified, and the first
  verified source-active frame falls within the first 5.

The sentence remainder after the far end stops is single talk. The baselines witness it, and it is not part of the
double-talk claim.

An eligible cell passes the content bar when all of these hold:
- **b-whole:** |corr| between the AFE output and the template, 100 Hz–4 kHz, over the verified frames, is at least 0.50.
- **Clipping:** no AFE clipping inside the scored frames.
- **Local rule 'level-hole' (decision 97):**
  - Sliding 200 ms windows of the scored frames, each counted with at least 5 verified frames.
  - Each window's AFE band level minus the template's is taken relative to the same difference over the whole
    overlap. The minimum must be at least the frozen level threshold.
  - Splice clause (decision 98; decision 100: lag-tolerant): every counted 200 ms segment's correlation, maximised
    over a declared window of ±2 samples (0.125 ms) around the cell's selected lag, must be at least 0.17. The window
    covers the segment-lag jitter of 201 of the 204 identifiable intact development segments (A + A-supp; the other 3
    at 3–4 samples). Any window raises the correlation level-matched local leakage can reach, so the threshold moved
    from 0.12 to the declared midpoint 0.17. That keeps every required catch the zero-window rule made: the
    level-matched local-leakage constructions and the 400 ms splices. Two incidental catches of already-declared
    detection-limit classes were lost: the 200 ms splice on sentence-09-48k t1 and the 120 ms onset mute on
    sentence-06-normal t1. The development margins are thin (0.019 intact side, 0.017 leakage side).
  - Thresholds are set from the development data (hold A + A-supp). Hold B became development evidence (decision 100);
    hold C under q1-freeze-02 is the next prospective validation.
- **Loose level floor** (decision 91, Fable d97 B1): the whole-overlap level deficit must be at least −25 dB. This
  value is declared, not tuned. It fails total suppression of the near end, which the relative measures cannot see,
  and leaves the declared attenuation controls (down to −22 dB on hold A) and the real 5–17 dB range alone.

## What the local rule is declared to catch

- holes of 400 ms or more: internal mutes, ending mutes, and splices of other speech at full level;
- 200 ms ending mutes.

## Detection limit: NOT caught beyond b-whole ≥ 0.50

- **200 ms internal holes.** A 200 ms window over a 200 ms hole still contains neighbouring speech and band-filter
  spill. On hold A these edits reach −3.3 dB relative level, while intact cells go down to −8.5 dB.
- **Onset mutes of 80–200 ms.** Every window with at least 5 verified frames straddles unmuted frames (on A at most
  −0.7 dB relative). A 400 ms onset mute is caught by the level clause (37/39 on A); the two escapes barely touch
  scored frames.
- **200 ms splices.**
- **Splice lag window.** A local splice or leak that correlates with the template at ≥ 0.17 within ±2 samples of the
  cell's lag passes the splice clause. For the tested constructions (400 ms level-matched far-end echo and
  other-utterance speech at the overlap midpoint) the development data stay below that where the zero-window rule
  caught them: A + A-supp leak-echo 42/42, leak-other 41/42 caught (see the d100 design note). That is not a proof for
  every possible leakage. A near-end segment delayed by under 0.125 ms is by design not a loss. On hold B
  (development evidence), sentence-03-normal take 1, whose quiet first segment the AFE attenuated in double talk, IS
  rescued at W = 2: 0.048 at the cell lag, 0.312 within ±2 samples.
- **Whole-lag anchor.** The window is centred on the cell's whole-overlap lag. If the whole-overlap lag search picks a
  pitch-period alias instead of the true lag (as on hold A sentence-08-normal t2: −82 samples while every segment
  agrees near 0..+12), the ±2-sample window does not reach the true segment lags. This is a false-fail risk only: it
  can lower intact segment correlations, never raise an edited one.
- **Depth.** The level clause is calibrated against digital silence. A 400 ms partial hole in a ~1 s overlap shows
  −16.5 to −18.6 dB relative at −20 dB depth (27/39 caught at −20 dB, 37/39 at −25 dB, 1–2/39 at −15 dB), so the
  practical depth limit is about 21 dB.
- **Stop fixtures (300 ms overlap).** Any loss covering most of the overlap moves the whole-overlap reference with it,
  so the relative measure cannot see it. b-whole and the loose level floor are the only checks against a uniformly
  suppressed stop.

A cell with such a loss can still pass whenever its whole-overlap correlation stays at 0.50 or above. The qualification
verdict does not claim these losses are absent. Hold A catches some of them incidentally (see the rules artifact); that
is not a guarantee.

## Other declared limits

- **Uniform attenuation inside the loose floor is not content loss** (decision 91). Only a whole-overlap loss beyond
  −25 dB fails the gated floor.
- **Firm and normal fixtures of one sentence are the same utterance about 6 dB apart.** Raw-only delivery tells them
  apart only by the level clause.
- **The raw-only delivery guard accepts some null windows**: 29 of 7846 overlapping null windows on hold A (0.37 %).
  These windows are correlated, so this is not a per-cell rate. An accepted cell must still pass the independent AFE
  content bar (b-whole against the paired template, the local rule, the level floor) to count as preserved.
