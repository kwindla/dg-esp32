# EchoEar AEC: known issues

Candidate aec-v87 (esp-sr 2.4.7 AFE_AEC, FD_LOW_COST, filter length 4, reply-start echo gate 400 ms).
These are documented limitations. Each one scores as an ordinary miss under the echo bar
(≤ 2 misses per 20 at each volume). None blocks qualification on its own.

## 1. Baker vowel-tail burst (decision 70b)

A sustained vowel at the end of a reply can leave a short residual above the post-guard limit. The leading
hypothesis is linear adaptive-filter drift. It counts as an ordinary miss.

## 2. Tonal burst with a 4 kHz mirror image (decision 89)

**Symptom:** for 75–95 ms the AEC output rises to about −23 dBFS while the mic signal is decaying and the
reference is near silence. The output exceeds its own input by up to about 20 dB. Spectrally it is a tone
(e.g. 662 Hz) plus its mirror around 4 kHz (7337 Hz; the two sum to 8000 Hz). There is no click and no capture gap.

**Where seen:** v9.10-acceptance-01 −6 dB T08 (post-guard −43.7 dBFS against −50, a miss); v9.10 0 dB T03 (−50.2, a
near miss); campaign-10 echo-08 (T08) and q02-echo-03. It did not appear in about 50 clean cells.

**Effect on transcription: none observed.** Every echo-cell window was checked for interim transcripts, final
transcripts, VAD and user-turn events: 40 windows in v9.10-acceptance-01 (Deepgram nova-3) and 14 echo cases in
campaign-10 (Deepgram Flux). The total was zero, including all four burst cells. Under decision 88 it is
therefore a known issue, not a blocker.

**Likely mechanism (inferred, not established):** a brief cancellation lapse on a sustained tone in the low-cost
AEC mode. A replay through FD_HIGH_PERF and longer filter lengths would test this; it is parked.

**Re-open if** the burst ever shows up in a transcript or user turn, or causes more than the echo bar's allowed misses.

Diagnosis: proj-2026-10-15-0645/codex-jobs/coord-step-9-v9.10-miss-diagnosis.md.
