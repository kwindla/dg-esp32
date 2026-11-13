# Predeclared EchoEar item 2 requalification

Machine-readable plan: [echoear-item2-requalification.json](../hil/plans/echoear-item2-requalification.json).
This is preparation only. No final master commit or ELF has been bound, and no
scored execution is authorized. Claude must explicitly announce the final
master/ELF after the restart merge review and A's drift/late-frame diagnosis
outcome. An unchanged threshold remains a threshold even if a diagnostic
explains its failure.

The integration image must first complete one combined item1 proof on its exact
announced SHA: persisted autonomous boot and three no-console turns, three
injected reconnects with completed status frames and an ignored Connecting tap,
then bounded bot-restart recovery. Apply strict transport and heap gates to that
whole acquisition. Earlier partial A/B results cannot be stitched into this
proof. B does not acquire the bench until A posts `INTEGRATION CANDIDATE <sha>`.

Before scoring, freeze an evidence-only binding with the plan hash, full master
commit, ELF SHA256, build configuration readbacks, harness/bot/fixture/budget
hashes, and Claude's release announcement. Verify AEC off, microphone analog
30 dB/digital125%, the existing limiter profile, and playback volume0. All
stages use that one ELF. A new image requires a new binding and declaration;
earlier acquisitions do not count toward it.
Build metadata alone is insufficient: require the corrected artifact validator's
embedded ESP app version, matching descriptor inside the ELF, and binary's
embedded ELF SHA256. A failed build plus an old local ELF is not a candidate.
Normal EchoEar, demo and legacy configurations must successfully build/link.

The stage order and populations are fixed:

1. B: strict live voice+cat, exactly three original turns, all three must pass.
   One cold then two warm turns on one peer, cat30fps target. Retakes disabled.
2. A: A6 echo stress, hot-prompt STT/WER with limiter engagement, and analog
   saturation/THD probe, using the existing scorers and frozen banks. B releases
   the bench and supplies the exact ELF/hash and strict evidence before A starts.
3. A coordinated with B: five original quiet offline turns, then five original
   quiet live turns. Each population must pass5/5 with retakes disabled. Each
   cohort has one cold and four warm turns on one peer. Short separate holds.
4. Astra independently rederives gates from raw evidence; only then update the
   qualification label and caveats. No soak, unplug repeat or persistent service.

Before every original prompt, both Jabra RMS and native board-mic room RMS must
be finite and at or below−55dBFS. Preserve both WAVs and actual values. The
existing room recorder's−50dBFS default does not satisfy this plan. The opt-in
production `preflight` callback checks the Jabra recording and calibration's
native `room-board.wav` before the scored prompt. Calibration/probes remain
outside the scored population; warm calibration retains the existing omission
of a direct playback tone during the live session.

Record at least a30-second NIC eligibility observation before each cohort and per-arm
before/after counters. Gate only total `wlp8s0` RX+TX at20,000,000bytes/300s,
scaled to duration. Record `enp7s0` without gating it. Missing/reset WiFi evidence
cannot establish eligibility; no unsupported board-flow subtraction.
The opt-in callback requires the full30seconds; a shorter clean sample defers.
For otherwise quiet measurements, the production turn completes that interval
before the prompt without issuing full PERF queries or diagnostic playback.

A noisy, missing or unavailable room/NIC observation defers the original
prompt. Append every deferral to `deferrals.jsonl`: UTC/monotonic time, stage,
next original slot, both room values/WAVs, NIC snapshots/deltas/policy, reasons,
and `scored=false`. Release the bench and wait at least30seconds before another
eligibility attempt. Deferrals consume no original slot. The production loop
returns2 with a separate deferral record and retains any completed originals.
After any scored prompt, its failure remains in the original population. Do
not restart a partial cohort as a clean population. Resuming unscored slots
requires proof of the original peer's continuity; otherwise retain an
incomplete result for the coordinator. No automatic resume or retake is enabled.

Strict gates remain those in the existing scorers: full-turn transport deltas
zero including drift/drop/concealment and playback write errors; gate-open and
gate-closed clips both zero; unprotected echo headroom≥3dB with protected and
unprotected peaks reported; Jabra reply−22±3dBFS at volume0 and≥10dB above room;
internal minimum≥81,920bytes/largest≥32,768bytes, failed allocations0,
app-control free≥2,048bytes; DMA/recovery/lock errors0 and display alive.
The internal minimum includes cold boot and task-census/PERF allocations. Keep
the81,920-byte floor even if current free has recovered; do not reset the
historical minimum or exclude the census. The host observation gate recomputes
memory from all raw PERF records and rejects unknown engine snapshots on an
initialized LCD, nonzero
lock counters, retakes, extra originals, or failed per-arm WiFi evidence despite
stored pass flags. It also audits raw bytes without restart-loss exceptions.
Pre-LCD samples may legitimately have no engine snapshot; their historical
heap minima still count. Drift inserts and latency trims must also be zero,
even where a general transport scorer classifies them as edited audio.
Require boot/PSRAM/GDMA assertions, a wire-delivered show and actual render
proof during each reply, reply FPS≥24 including the full first-TTS-to-gate-reopen
bracket (at least5seconds), quiet1 and no full PERF dump during prompt/reply.
Keep existing uplink-tail thresholds and coverage rules. No intentional bot
restart is part of scoring, so no expected-loss exception window is opened.

Run only the host rehearsal now:

```sh
/home/khkramer/src/dg-esp32-echoear-display/build/hil-env/bin/python \
  /home/khkramer/src/dg-esp32-echoear-display/hil/echoear_requalification.py \
  --plan /home/khkramer/src/dg-esp32-echoear-display/hil/plans/echoear-item2-requalification.json \
  --dry-run --out /home/khkramer/src/evidence/echoear/B/item2-host-dry-run-NEW
```

This CLI has no scored execution option. It rehearses the production cohort
loop with synthetic board/bot turns, runs the real aggregate merge/acoustic/
uplink gates on synthetic fixtures, tests failed-original and deferral paths,
and installs tripwires on serial, subprocess, socket and bench-lock access.
It never reads credentials. Its results explicitly say `synthetic=true`,
`hardware_qualified=false` and `scored_campaign_started=false`. Future scored
launch wiring must pass the plan's preflight callback; invoking the older
five-turn CLI with only its default room threshold cannot qualify this plan.
