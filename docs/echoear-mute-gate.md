# Mute-image gate (mute-01) and AEC promotion smoke

Step 9(d), decision 109(a) and 109(e). Both are predeclared host specs. Neither
has been run. The AEC smoke now has a runner, `hil/echoear_aec_promotion_smoke.py` (decision 110). Astra and Fable review both, the mute adapter included, before
any bench use.

## Why the mute image needs its own gate

The merged AEC-off image (`build-mute`, ELF `a9b61556…` since r6, bundle
`step-9/merge-9d-r3/bundle-mute`) is not 9936da45 rebuilt. The resolved
config differs from the 9bc2d42 normal build in playout (5/8 frames became
7/10) and, since r6, in the peer owner stack (10752 became 12800, decision 111(a)). The code differs as well: it carries the AEC line's general
changes, which are playback drain and retire, bot-interrupt cancel, liveness
pongs, the capability in `/start`, and admission carry-over. The AEC
qualification exercised those changes only with AEC on. The gate is functional
and bounded. It makes no acoustic barge-in, STT-accuracy, strict late-RTP or
general-reliability claim.

## Gate spec

The frozen contract is `hil/plans/echoear-mute-gate-v1.json`; the declaration
pins its sha256. In summary:

- **Identity and mode.** These items are checked:
  - The pinned mute bundle, AEC off and autostart off.
  - The bot runs from the pinned checkout with `ECHOEAR_AEC_FULL_DUPLEX` unset,
    so the capability route applies, and with liveness at 0.5 s.
  - The bot logs `DEMO_DUPLEX aec_full_duplex=False user_mute=True source=capability-or-safe-fallback`.
  - The device logs `AUDIO_CAPABILITY aec=0`.
  - The recorder's `stt-profile` has `full_duplex=false`.
  - No PERF snapshot has an `aec` block.

  Any mismatch makes the run **INVALID**, never a pass.
- **Stimulus.** The Jabra plays git-tracked, hash-checked prompts from
  `hil/fixtures/agent_conversation`, cycling france, sky, addition, long and
  italy. The Jabra is silent from the end of each prompt until its tail closes,
  so every reply is echo-only. The mixer is stored before the run, restored
  after it, and byte-compared.
- **Sequence.** The runner works through these steps in order:
  1. **A**, at −6 dB: 10 replies at 60 s spacing in a connected epoch of at
     least 600 s, with PERF snapshots every 10 s. This is the soak; its first
     reply is cold.
  2. **R1**: a mid-reply disconnect, 1.0 s after the first non-silent write.
  3. **B**: one recovery reply.
  4. **R2**: a disconnect 0.5 s after GATE open. The volume is set to 0 dB
     while disconnected.
  5. **C**, at 0 dB: 10 replies, the first of them cold.
  6. **R3**: 10 s idle, then a disconnect.
  7. **D**: one recovery reply.

  The replies cut short by R1 and R2 are report-only.
- **No false turns.** The observable is the tail after `GATE open`. The window
  runs from the first non-silent `tts-transport-write` to the first device
  `GATE open` after the reply's last write, plus 2.0 s. Inside it there must
  be no accepted turn:
  - no `aggregator-user-turn-started` or `aggregator-user-turn-stopped`;
  - no `UserStartedSpeakingFrame` or `InterruptionFrame`;
  - no final `TranscriptionFrame` with text;
  - no new bot output.

  Interim and VAD events are retained as report-only. The tolerance is zero
  across all 20 echo cells and both recovery replies.
- **Witnesses: a muted mic or a silent speaker cannot pass.**
  - During playback there are at least 3 `MIC_GATE closed=1` samples, every
    one at or below −119.9 dBFS, and a `closed=0` sample after GATE open.
  - The Jabra band level from 300 Hz to 4 kHz during the reply is at least
    10 dB above the room floor measured just before the prompt.
  - The prompt's transcript contains at least one keyword group, which
    witnesses uplink viability after every reconnect and through the soak.
- **Per-epoch counters.** Each connected session is one epoch, and epochs are
  never compared across a reconnect. These counters must show zero deltas:
  - I2S underflows, write errors and timeouts;
  - PCM queue drops, app data drops, queue drops and playout drops;
  - post-PLC zeros and starvation;
  - every `drop_reasons.*` field;
  - console output full and transport drops;
  - failed allocations;
  - cancellation events and duplicate notices;
  - display error counters.

  Loss must stay at or below 2 % of received packets. A missing, invalid or
  decreasing value fails.
- **Playout.** The checks are:
  - `target_ms` is 140 and `target_depth` is 7;
  - `ready_high_water` is at most 10;
  - each new generation shows prefills;
  - `output_volume_db` equals the block's declared volume.
- **Admission.** Each epoch starts and ends at a quiescent boundary, using the
  existing 0.25 s drain. `admission_counters` must pass, and the received delta
  must be at least the device's liveness-pings delta. Equal zero counters fail.
- **Liveness.** Over each epoch, pings and pongs both rise, pongs reach at
  least pings − 1, timeouts stay at 0, and at most one pong is outstanding at a
  boundary. The device never logs `LIVENESS no ping`. The bot logs
  `LIVENESS alarm` only for a peer the harness deliberately disconnected,
  attributed by pc_id and inside that disconnect's window (decision 112, see
  the r7 section; liveness is on at 0.5 s in this gate).
- **Reconnect (×3).** Each cycle must show:
  - a new `STATE Ready` generation within 10 s;
  - a peer id new relative to every earlier one;
  - no non-silent write on the new peer before its first user turn;
  - the old session's `session-end` logged before the new peer's first write;
  - a complete first reply.
- **Resources.** `check_snapshots` runs on every snapshot and applies the
  step-2 floors for heap, stacks, CPU idle, encode time and 20 ms uplink.
  AEC-only checks are N/A only when every snapshot lacks an `aec` block.
  Display: `rendered` advances over epoch A, and `failed` and `dead` stay
  false. No fatal error occurs, and the smoke serial log shows exactly one boot.
- **Outcome.** Precedence runs INVALID > FAIL > INCOMPLETE > PASS.
  - A FAIL is never retaken.
  - Each block allows at most 2 retakes, and only for INCOMPLETE cells whose
    cause is declared: Jabra duration, ALSA recording failure, no user turn
    within 15 s, or a provider ErrorFrame before the first write.
  - Every attempt is retained.
- **Restore.** The owned bot is stopped, normal 9936da45 is reflashed and its
  full boot hash verified, and the mixer is restored and byte-equal. The bench
  lock is retained if any cleanup step fails.

### Interrupt/flush: N/A in mute mode

The two reviewers disagreed on this. The code settles it:

- **Bot policy.** `server/demo_policy.py` `turn_policy` installs
  `AlwaysUserMuteStrategy` in half duplex. Flux runs with
  `should_interrupt=False`, and the dev profile's start strategies use
  `enable_interruptions=False`. User speech therefore cannot interrupt the bot.
- **Pipecat's other route.** Pipecat interrupts outside user audio only on an
  RTVI `send-text` with `run_immediately`, through
  `RTVIProcessor._handle_send_text` → `interrupt_bot()`. The firmware never
  sends one. Its only client messages are `pong`, `display-ready` and
  `display-result`, and a test asserts that `main/app.cpp` contains no
  `send_text`.
- **The only host stimulus.** `aec_evaluation` `script-interrupt` refuses half
  duplex (`enabled()`), so it cannot be used without a test-only path the
  product does not have.

Host interrupt and flush are therefore not product behaviour in mute mode, and
the gate does not fake them. The replacement check is a hard FAIL on any
`InterruptionFrame`, any `bot-interrupted` notice, any `AUDIO_CANCEL` line, or
any non-zero `cancellation.events` or `cancellation.duplicate_notices`. The
cancel/flush code is the same in the AEC image, which the promotion smoke
exercises with a real Stop.

## r2 review fixes (Astra R1-R6, Fable 2.2.1-2.2.2)

The plan and code now enforce these rules; each one has a fake-only regression test in
`hil/test_echoear_mute_gate.py`.

- **R1, recorder ownership and health.** `RawRecording` registers with the adapter as soon as
  it is spawned. `cell()` stops and reaps it in a `finally` on every path, and cleanup refuses
  to release the bench while any owned capture is alive. A capture counts as healthy only if:
  - it was stopped by SIGINT, not by an early exit or a forced kill;
  - the ALSA log shows no xrun or overrun;
  - its byte count is within 0.25 s of the time it ran.

  Health, time bounds, byte count, stop provenance and return status are written beside the
  PCM. Sample zero is known to within the arecord spawn-to-readiness interval, which must be at
  most 0.1 s. Windows are mapped conservatively, and the speaker window starts at least 0.2 s
  after the prompt ends.
- **R2, latched failures.** The scorer collects witnessed failures first, from whatever
  evidence exists, and they always win. An INCOMPLETE result is possible only when nothing
  failed, so a missing recording or a bad stimulus can never hide a false turn. A provider
  ErrorFrame before the first write is the only provider-related retake cause. These are all
  FAIL:
  - a FatalErrorFrame, pipeline error or recorder gap;
  - a provider error after exposure began.

  Across attempts, any latched failure makes the block FAIL.
- **R3, complete reply.**
  - All transport rows are kept, and any dropped write fails.
  - Each reply context needs a TTSStarted before its first write and a TTSStopped after its
    last, and the three context sets must be equal.
  - Any output or new context after the first BotStoppedSpeaking fails, through the end of the
    tail.
  - Exactly one assistant turn and one peer.
- **R4, mic-gate coverage.**
  - Every sample in [first write + 0.5 s, last reply write] must be closed and at or below
    −119.9 dBFS.
  - No GATE open may appear inside that interval.
  - The device-clock gap between samples is at most 0.3 s; a longer gap is INCOMPLETE.
  - After GATE open, a reopened sample is required. Samples that all stay closed mean a stuck
    gate, which fails.
- **R5, reconnect credit.**
  - R1 and R2 are credited only with a retained trigger: R1 fires while the reply is active
    (last write within 0.12 s, not yet stopped), R2 fires 0.5 s after GATE open.
  - The disconnect is issued inside the cell, at the trigger, before the recorder stops or any
    snapshot is taken. The lag is retained and must be at most 0.5 s.
  - With no trigger, the cycle is INCOMPLETE.
  - New-peer rows are selected by pc_id from every recorder row, so a write made before Ready
    still counts as stale.
- **R6, teardown telemetry.**
  - Every generation is retired with `disconnect`. Its single retired-session PERF line is that
    epoch's terminal record, and counters and liveness run from the first snapshot to it.
  - The last generation is retired, and its recorder session-end awaited, before the verdict.
    The verdict comes before cleanup.
  - A missing or foreign terminal record is INCOMPLETE.
- **Timing and retakes.**
  - The run refuses to start if the acquisition deadline (2700 s) plus a 600 s cleanup reserve
    would reach the 03:00–04:10 exclusion. That puts the latest start at 02:05.
  - B and D are single attempts.
  - The cold first attempt of A and C cannot be retaken.
- **Fable 2.2.1.** Text checks read the persisted `mute-serial.log` instead of the
  20,000-line capture ring.
- **Fable 2.2.2.** `playout_dropped`, `playout.rejected.late` and `playout.rejected.generation`
  are report-only. The non-network rejection reasons stay at zero delta.
- **Fable 2.3.** Periodic PERF snapshots are held from prompt start until the first write.

## Adapter

`hil/echoear_rollback.py` adds profile mute-01; the rollback-01 code is
unchanged. The scoring functions are pure and live in `hil/echoear_mute_gate.py`.
Host tests use fakes: `hil/test_echoear_mute_gate.py`.

```sh
PYTHONPATH=hil:server python hil/echoear_rollback.py prepare-mute /home/khkramer/src/evidence/echoear/aec-demo/step-9/mute-01-draft
PYTHONPATH=hil:server python hil/echoear_rollback.py validate-mute /home/khkramer/src/evidence/echoear/aec-demo/step-9/mute-01-draft/declaration.json
```

`prepare-mute` requires a clean, committed tree. It pins:
- the plan's sha256;
- the mute bundle's file hashes and the normal final image;
- the prompt fixtures;
- every `hil/*.py` and `server/*.py`, plus `server/uv.lock`, this document and
  the plan.

The launch script and `run-mute` refuse to start without a regular `USER-GO`
marker, which only the coordinator writes, after the user's go and a quiet
room. Lock, cleanup, stop and retry rules are those of
[rollback-01](echoear-rollback-01.md).

## AEC promotion smoke (r3 restructure)

The spec is `hil/plans/echoear-aec-promotion-smoke-v1.json` and the runner is
`hil/echoear_aec_promotion_smoke.py` (decision 110, restructured per the r3 direction). It covers the
merged AEC bundle (`merge-9d-r3/bundle-aec`, ELF `69d5d764…` since r6; `4ff2a364…` before). Its proof of record is the masked
compare against cbabb650 (zero unmasked bytes). It is an equivalence/promotion check, not a
qualification run.

- **Part 1** is one v9 smoke part (`echoear_aec_v9.run_part`, mode smoke) on q1's own
  plan-effective.json, with **only** the candidate identity replaced.
  - No reconnect and no plan injection.
  - The qualified hold (V9Hold → NovaHold → BenchHold) and `part_audit` run unmodified, over the
    part's single generation.
  - The schedule is the cold and warm scripted playback cells (the bot speaks a declared text via
    TTSSpeakFrame; there is no user turn) and the firm Stop cell (the pinned Stop fixture at
    0.9 s, then a real bot reply as recovery).
  - The bot settings and the Stop fixture are pinned. Evaluation hooks are on, as in q1, and
    server/ equals the qualified pairing (aec-q 133f587), except the decision-113 alarm-line
    pc_id label (log text only, r8).
- **Promotion gate policy (Astra 3).** It is applied through the Runner's acoustic hook
  (`promotion_acoustic`), not by editing v9.
  - The qualified scorer runs unchanged. Its acoustic SCORE findings move into the attempt record
    (`promotion_report_only`), and an acoustic miss is passed on as ineligible, with the original
    score retained. These findings therefore cannot STOP or fail the smoke.
  - Candidate faults, the hard false-event findings (`echo.false_turns`,
    `barge.false_interruption`) and the gated flush checks stay authoritative.
  - A real `run_part` regression shows `echo.hard_floor` does not stop the part, while a hardware
    fault or a gated false turn does.
- **Stop accounting (Fable B2).** It is scoped to attempt windows on the device clock
  (`before.json` → `after.json`).
  - The Stop window holds exactly one AUDIO_CANCEL, applied=1, with `cancellation.events` +1.
  - Duplicate notices may be ≥ 0. They are dropped by the transport before the callback and never
    print a line.
  - Near-witness windows hold their own expected Stop cancels; they are attributed and counted.
  - A cancel in an echo window, or outside every attempt window, is unattributed and fails.
  - `flush.stale_audio` is a server-write check. It is not a measure of DAC silence or flush
    latency.
- **Part 2** runs only after a part-1 PASS: one reconnect in its own hold,
  `echoear_rollback.AecReconnectLive` with `hil/plans/echoear-aec-reconnect-v1.json`. It reuses the
  mute gate's reviewed reconnect machinery: a trigger recorded mid-reply, the disconnect sent at the
  trigger, the new peer matched by id across all recorder rows, a complete reply in the new session,
  no stale writes, and per-generation terminal counters, liveness and resources.
  - Mic-gate and mute-mode checks are off. Acoustic consequences (false turns, extra output) are
    report-only.
  - The bot uses q1's bot_config environment with evaluation hooks OFF.
  - Bench lock, owned bot, cleanup and the 9936da45 restore follow the rollback-01 `execute()`
    path.
  - Its real cell/disconnect/connect/stats/judge path is integration-tested with only the
    hardware boundaries faked (`hil/test_echoear_aec_reconnect.py`).
- **Unproven path.** BenchHold.reconnect has no passing precedent (campaign-10 reconnect-01 failed
  on a stimulus precondition), and it is not used. Part 2's machinery is host-reviewed but has never
  run on the bench; its first live run is the first exercise of this path.
- **Launcher.** `step-9/merge-9d-r2/launch-aec-promotion-smoke.sh`:
  - takes the bundle manifest and its sha256;
  - refuses an existing output, a dirty tree, or any HEAD other than its pinned `EXPECTED_HEAD`;
  - verifies both holds' 9936da45 restore, mixer and lock at exit.

  The ledger records `runtime_sha256`. The whole smoke (part-1 ceiling 1800 s plus part 2's
  900 s + 600 s) must clear the 03:00–04:10 exclusion.

## r3 review fixes (mute gate: Astra 4-6, Fable non-blocking)

- **Astra 4, terminal evidence.**
  - After every disconnect, a `stats` snapshot (no live session) is the generation's terminal
    device/resource record: stacks, internal heap, failed allocations, I2S, PCM queue, the lifetime
    AUDIO line, console and display flags are gated on it (`terminal_resources`).
  - A missing snapshot, or one without its AUDIO line, is INCOMPLETE.
  - Every retained recorder row of every session is scanned through the final seal for gap,
    pipeline-error and FatalErrorFrame (`recorder_integrity`). Positive evidence wins.
  - Regressions inject each of Astra's last-disconnect cases: a gap row, a FatalErrorFrame, and
    terminal heap `minimum_free=1`.
- **Astra 5, disconnect timing.**
  - `disconnect` is the first console command after a trigger. It is sent with `board.send`, and
    the lag is measured to the moment the send returns. `audio 0` follows the disconnect.
  - For R1, activity is re-checked at the send from the recorder rows.
  - A 0.8 s delay inside the disconnect command path is not credited (fake and real-path tests).
- **Astra 6, recorder origin.**
  - arecord runs with `-B 100000` and is stopped by SIGINT. Sample zero is bounded from the stop
    time minus the captured duration, widened by the ALSA buffer read from the hw-params dump. That
    bound is cross-checked against [spawn, readiness + 0.1 s]: sample zero follows readiness by a
    short lag. Fable noted the earlier wording had the direction inverted.
  - A capture whose bound disagrees with readiness is INCOMPLETE, and so is one with an ALSA error
    diagnostic other than arecord's "Aborted by signal Interrupt". There is no separate byte
    tolerance.
  - Windows keep only samples inside them for every admissible origin.
  - Astra's 0.8 s-of-1 s case gets the buffer's 0.1 s uncertainty, never 0.020. Shorter or
    delayed-start captures are INCOMPLETE.
- **Fable, teardown cutoffs.** In each last-boundary → terminal interval, `drop_reasons.send_cutoff`
  and `send_budget_cutoffs` are report-only. A deliberate teardown cuts queued uplink audio by
  design, which matches v9.12's exit-associated treatment. Both stay zero-delta from first to last
  quiescent boundary in every epoch.
- **Display (found while restructuring).** MuteLive never initialised the display, so
  `display.rendered` would have stayed 0, as rollback-01's snapshots show, and display-A could
  never pass. Setup now replays the qualified BenchHold display/character sequence.

## r4 review fixes (Astra 1-5, Fable should-fixes)

- **Restore dispatch (Astra 1).** The candidate is installed by `install_candidate()`.
  `Live.install(label)` is only the normal 9936da45 installer, and every cleanup attempt uses it. The
  r4 overrides broke `install('restore-N')`: the old code raised a TypeError, and the normal image was
  never restored. Regressions cover inherited cleanup and an end-to-end `execute()` for both
  subclasses. Each asserts the normal bundle and hash, the mixer restore, and the lock release.
- **Retired-task terminal semantics (Astra 2).** A post-disconnect census legitimately omits
  audio-tx, peer-loop and peer-rx (`tasks_exited=true`; rollback-01 serial lines 3658 and 4652).
  - Tasks present in the census are gated on their floors.
  - An absent session worker is gated on its exit high-water (`tx_stack`, `owner_stack`, `rx_stack`)
    when the firmware reports a positive value, and is reported otherwise. No rows are invented.
  - A worker absent without `tasks_exited` is INCOMPLETE.
  - A measured heap, stack, display or mode failure is FAIL even when the AUDIO line is missing.
  - The integration fakes now produce this firmware-shaped retired census.
- **Part-2 protection (Astra 3, Fable 1).** `protected_execute` is the single wrapper for mute-01 and
  for the smoke's part 2. Subprocess tests prove that SIGTERM and deadline expiry unwind into cleanup,
  restore normal and release the lock.
- **Outcome composition (Astra 4, Fable 2).** `part2_outcome` treats execute()'s result as
  authoritative:
  - PASS needs execute() passed with one clean cleanup, a gate PASS, a normal restore and a released
    lock.
  - A gate FAIL or INVALID keeps that outcome.
  - A gate PASS with an execution failure, or a failed-then-retried cleanup, is INCOMPLETE. No candidate
    defect was witnessed, but the protected lifecycle did not complete cleanly, and the bench was
    restored.
  - A pre-gate exception is INCOMPLETE.
- **Recorder endpoint (Astra 5, Fable 3; superseded in r6 by decision 111(b), below).**

- **Should-fixes.**
  - `send_budget_cutoffs` is now in `zero_delta_per_epoch`, so the declaration holds: zero-delta
    before teardown, report-only in the teardown interval.
  - A disconnect answered with ERR FAILS that generation.
  - Every near-witness window must hold as many AUDIO_CANCEL lines as its `cancellation.events` delta,
    all applied=1.
  - `MuteLive.setup` calls `jabra_card_zero()` before the card-0 mixer store.
  - The spec's quiet-room and INCOMPLETE wording is corrected.

## r6 changes (decision 111)

- **Owner stack (Fable r5 B1, decision 111(a)).**
  - `DG_PEER_OWNER_STACK_BYTES` now defaults to 12800 in every mode. Measured peak peer-loop use is
    6780–7004 B on both images, so 10752 left about 3.8 KB, under the qualified 4096 floor. The floor
    is unchanged.
  - The AEC build's resolved config is key-for-key unchanged (1173/1173), and its rebuild is again
    masked-equal to cbabb650.
  - Both bundles are rebuilt at the firmware commit and written to `step-9/merge-9d-r3/`. The
    `merge-9d-r2` bundles are superseded.
- **Recorder (Astra r5 item 1, decision 111(b)).** The r5 interval assumed the last written sample
  lay within one buffer plus period of SIGINT. A silent pending overrun at abort breaks that: the
  installed arecord 1.2.13 aborts before the next read reports `-EPIPE`. The bound is now
  premise-free. With D = written frames / rate:

  `t_spawn + k/r ≤ t(k) ≤ t_reaped − D + k/r`

  with a declared 500 ppm × D clock allowance on each side (r7: this is an
  allowance for this fixed capture setup, not a USB-derived bound).
  - Width = startup (spawn to first sample) + samples lost at or before abort + the child's stop and
    reap latency.
  - `health()` itself flags a width over 0.15 s.
  - Readiness, buffer and period are diagnostic only.
  - Correction to the r5 text: the installed 1.2.13 does not write a stale final chunk.
  - *Superseded in r7 by measurement:* the coordinator measured 0.016–0.033 s capture-only
    (20 Jabra captures, 2026-10-25), and smoke-01 part 2 recorded 0.08 s under bot load.
  - *(r6 text)* **Expected nominal width: unmeasured.** It is roughly 30–80 ms of arecord exec and ALSA
    open/prepare/start plus the first period on the USB device, plus up to 50 ms of ring lost at
    abort, plus 10–40 ms of reap. That makes 0.1–0.15 s plausible and startup alone close to 0.1 s
    possible.
  - The risk is a deterministic INCOMPLETE, not a wrong verdict. A host-only arecord timing check on
    the Jabra, with no board and no bot, would settle it before the bench run. I cannot run it here
    (no ALSA access).

## r7 changes (decision 112, Fable r6 should-fixes)

- **Liveness attribution (decision 112).** aec-promotion-smoke-01 part 2 failed only on
  `liveness-logs`: the bot's watchdog reaped the abandoned session-A peer 2.0 s after R1's deliberate
  disconnect. The device disconnect does not close the bot's peer (report-only, firmware scope). The
  absolute rule came from rollback-01, where liveness is 0. Both the mute gate and the AEC reconnect
  check run the bot with liveness 0.5 s, so both now use `liveness_logs_attributed`:
  - Attribution: `demo_liveness.tick` logs the alarm, then awaits `close()` of its own peer, whose
    `handle_disconnected` logs `Discarding peer connection for pc_id`. The alarm pairs with the single
    discard that follows it in file order within 0.5 s, for a pc not already discarded. Zero or several
    candidates are unattributable.
  - Expected only for a declared epoch's peer with an OK deliberate disconnect, not live at the
    alarm, inside [send lower − 2 ms, send + 2.0 s tolerance + 1.0 s margin + 2 ms], with the logged
    tolerance equal to max(2, 2 × period) and at most one alarm per peer. Anything else FAILs.
  - The runner now stamps `time.time()` (loguru's clock) before connect and around the console
    disconnect send.
  - Device `LIVENESS no ping` stays absolute. Part 1's settled-end rule stays absolute (part 1 has no
    reconnect).
  - smoke-01 stays FAIL of record. `echoear_liveness_rejudge.py` writes a diagnostic-only rejudge beside
    it, mapping monotonic disconnect times to wall time through recorder/bot.log anchor pairs.
- **Recorder (Fable r6 should-fixes 1–3).** An existing target is unlinked before Popen (arecord
  opens without `O_TRUNC`); every hw `rate` in the `-v` dump must equal 16000 with no rate conversion;
  the 500 ppm is stated as a declared allowance. `playback_witness`'s docstring now describes the
  decision-111(b) interval.
- **Crash handler (should-fix 4).** A crash escaping part 2 after a witnessed gate FAIL/INVALID records
  that outcome, not INCOMPLETE.
- **Evidence location (should-fix 5).** New smoke runs write under `step-9/merge-9d-r3/`
  (`aec-promotion-smoke-02`, launcher `merge-9d-r3/launch-aec-promotion-smoke.sh`). smoke-01 stays in
  `merge-9d-r2/`.

## r8 changes (decision 113, Astra r7 blocker, Fable r7 should-fixes)

- **Identity bound at emission.** The r7 pairing chose the peer by proximity: a live peer whose own
  `close()` takes over 0.5 s, with a stale peer discarded in between, was credited to the stale peer
  (Astra's probe; Fable P1). `demo_liveness.Liveness` now takes an optional `label`, and `demo_bot`
  passes `runner_args.webrtc_connection.pc_id`. The alarm line becomes
  `LIVENESS alarm: no timely pong for 2.0 s; closing peer pc_id=<id>`; with no label it is unchanged.
  This is the only server change: log text and the label, plus tests.
- **Scorer.** Each alarm is attributed only by its own tag. An untagged alarm or an unknown pc_id FAILs
  closed. The discard line corroborates the identity (the same pc discarded after the alarm, or the
  pre-declared orphan signature) and never chooses it. smoke-01's untagged alarm FAILs under r8; it
  stays FAIL of record.
- **Window.** The lower bound is now sent + tolerance − period − 0.25 s (send + 1.25 s), since a peer
  that answered while live cannot alarm earlier; smoke-01 alarmed at send + 1.957 s. The upper bound
  is unchanged (send + 3.0 s).
- **Orphan signature (declared).** A watchdog tick between a graceful discard and `liveness.stop()`
  logs a tagged alarm after that peer's discard. It is credited only when the tagged peer has an OK
  deliberate disconnect and the alarm is in that peer's window.
- **Record-only.** The stamps assume one non-stepping wall clock (chrony slews), and the naive local
  parse is ambiguous in the DST fall-back hour (2026-11-01 01:00–02:00).

## r9 changes (Astra r8 blocker)

- **Part-1 runtime profile.** `build_plan` keeps the q1 plan, and the inherited
  `NovaHold.connect/capture/finish` compare the live `stt-profile` event with `p['profile']` by exact
  equality (`profile_witness`). The decision-113 bot changes `code_sha256['demo_bot.py']` (c0cfd66b →
  43db5296) and so `config_sha256` (1d2c6dc8 → c793313e). Without a fix, every smoke-02 connect would
  raise. The spec now pins both (`runtime_profile`), and `build_plan` derives the profile from the
  frozen q1 profile with exactly those two deltas; any other difference is an error. `validate` and
  `run` require `profile_manifest('dev', True)` of the checked-out bot to equal the derived profile
  exactly. `profile_witness` is unchanged, and no historical plan or evidence is rewritten.
- **Part 2 and mute** have no code-hash profile comparison. `mode_check` reads only
  `config.full_duplex` and `config.profile` from the recorder's `stt-profile` rows, and their
  declarations bind `code_sha256` at the prepared commit.

## r10 changes (decision 114)

- **Part 1 bot-alarm clause.** smoke-02 part 1 failed only on its absolute "no LIVENESS alarm"
  clause. After the final deliberate disconnect (~18:54:44.0) the bot's graceful retirement drained
  for over 2 s with the watchdog running, which alarmed at 18:54:46.222 on the part's only peer, tagged
  `pc_id=…c47d6d3a`. The clause now uses the decision-113 tagged rule through the same
  `liveness_logs_attributed` code and the reconnect plan's declared block.
  - The alarm must be on the part's own peer (`connected.json` pc_id), after its final disconnect
    send, inside [send + 1.25 s, send + 3.0 s], with at most one alarm.
  - Device `LIVENESS no ping` and the device liveness counters stay absolute.
- **Send stamp.** The qualified hold doesn't stamp its disconnect. While `run_part` runs, the
  promotion wrapper replaces the `echoear_aec_campaign.command` module attribute with a pass-through
  that brackets the `disconnect` console send with `time.time()`. `BenchHold.finish` and
  `V9Hold.finish_peer_lost` resolve that attribute at call time, and no v9 or bench module is edited.
- **Rejudge.** smoke-02 stays FAIL of record. `echoear_liveness_rejudge.py --part1` brackets the
  send from retained evidence and writes a diagnostic beside the run:
  - lower: the last `audio 0` device record, mapped to wall time through the recorder/bot.log anchors;
  - upper: the `disconnected.json` mtime, written only after the disconnect's OK.
- **Next run:** smoke-03, output `merge-9d-r3/aec-promotion-smoke-03`.

## r11 changes (decision 115, Astra r10 blocker)

- **Problem.** The alarm line logged the 2.0 s threshold, not the measured silence. A watchdog
  callback delayed past the final disconnect could therefore report a failure owed while the peer was
  live, inside the allowed window. Astra's probe had the last timely pong at −3.0 s, the alarm at
  +1.5 s and silent_s 4.5, and it passed.
- **Server (log only).** The labelled alarm line now ends `pc_id=<id> silent_s=<now − last_timely,
  3 decimals>`. The unlabelled line is unchanged. `profile_manifest` hashes only `demo_policy.py` and
  `demo_bot.py`, so the runtime-profile pin is unchanged.
- **Shared rule** (part 1, part 2, mute):
  - last_timely = alarm wall − silent_s must be ≥ send lower − period − 0.25 s − slack;
  - silent_s missing, malformed (not exactly 3 decimals) or below the tolerance FAILs closed;
  - all r8–r10 identity, peer, window and single-alarm rules are kept.
- **Part 1 corroboration.** Every evaluation `liveness-alarm` event must be for the part's peer and
  pair with exactly one attributed line. Its silent_s must agree within 1.5 ms, and its monotonic `t`,
  mapped through the stamp's new (`sent_wall`, `sent_mono`) pair, must be within 50 ms of the line.
- **Fixtures.** The pre-115 smoke-01/02 lines have no silent_s and are now negatives. The positives are
  synthetic r11 lines. smoke-02's real event (silent 2.433) puts its last timely pong 0.26 s before the
  send, with the floor at 0.75 s.

## r12 changes (decision 117)

- **smoke-03 part 2** failed only on bot-side teardown of deliberately disconnected peers:
  - after R1, dead peer A's "successful" writes and its assistant turn landed in B's cell window
    ("more than one peer");
  - after B's final send, the watchdog closed B during graceful drain, and a 10 s output-transport
    timeout became a pipeline-error on B.
- **Principle (shared code, `echoear_mute_gate`).** `retired_peers(epochs)` maps each pc_id whose
  console `disconnect` was sent and answered OK to its send's lower stamp, on the recorder's host
  monotonic clock. `split_teardown` marks a row as teardown evidence if and only if its peer is retired
  and the row is at or after that stamp.
  - Teardown rows are report-only and retained as `teardown_report_only`. They are excluded from
    `score_echo_cell` (peer set and every reply check), `recorder_integrity` and `interrupt_absent`.
  - `old_session_closed` (the old peer's session-end) becomes report-only. Only the decision-113/115
    liveness rule applies to a retired peer.
  - Before the send nothing changes: pre-Ready stale writes, the R1 trigger, and a live peer's pipeline
    error all still fail.
  - A missing stamp, an ERR answer, an unknown peer or a row with no pc_id is never teardown.
  - Device evidence stays authoritative: counters, admission/generation, playout, terminal stats and
    AUDIO_CANCEL.
- **Part 1** has one peer, retired only at the final disconnect after every cell. Its v9 consumers of
  bot rows run before that send, so only the liveness rule sees post-send rows.
- **Reversal (cost stated).** Astra r3 #4's bot-side half no longer gates: a recorder gap or
  FatalErrorFrame emitted during the last peer's own disconnect is now teardown, so it is report-only
  but retained. That check protected against a bot-side fault at the final teardown going unnoticed.
  Its device-side half (terminal resources and counters) still gates.
- **Runs.** smoke-04 is next (`merge-9d-r3/aec-promotion-smoke-04`). The mute plan's run id becomes
  `mute-02`: mute-01 is retained under its own id, and outputs are written once.

## r12 addendum (decision 118, mute-01 findings)

- **(a) Short reply versus the gate lead.** mute-01's cold A-1 ("What is the capital of France?" →
  "Paris.", write span 0.44 s) was INCOMPLETE: the old interval [first write + 0.5 s, last write] was
  empty.
  - The device had closed the gate 0.33 s before the first write (on bot-started-speaking) and opened
    it 0.42 s after the last write.
  - Retained bot logs give one-word replies to the exact fixture prompts, so the criterion changes,
    not the prompts.
  - **New interval.** lo = max(device GATE closed, first write) if the device's latest transition at
    or before first write + lead is a close, else first write + lead as before. hi = just before the
    GATE open that ends the reply. This is a superset of the old interval.
  - A GATE open anywhere in [first write, hi] is premature.
  - mute-01 A-1 rescores PASS over the whole playback (9 samples). R1 and R2 are report-only trigger
    cells; every scored cell uses the same interval.
- **(b) Bot idle timeout.**
  - **Cause.** Pipecat's PipelineWorker default `idle_timeout_secs` = 300 (`IDLE_TIMEOUT_SECS`;
    demo_bot doesn't override it) cancelled peer #0 exactly 300 s after the last bot speech.
  - **Why the runner idled.** The cold A-1 INCOMPLETE ended block A, and `block()` then waited out
    `min_epoch_s` (600 s, the soak) with only periodic snapshots.
  - **Fix (runner only).** The soak tail runs only for a complete block. A declared `bot_idle` budget
    (max runner idle 240 s < 300 s) is validated at plan load for every planned wait (spacing, soak
    tail, reconnect idle), and the soak wait is capped by it. A `bot-idle-timeout` leaf records any
    idle cancel in bot.log as INCOMPLETE (infrastructure).
  - **AEC smoke.** Part 2's sequence has no waits (budget validated through the reconnect plan).
    Part 1's v9 schedule runs cells back to back; no idle cancel appears in smoke-01/02/03 part 1 or
    the q1 holds.
- **Next mute run:** `mute-02` (draft r13).

## r13 changes (decision 120, Astra r12 blockers; Fable r12 nits 2-3)

- **Short-reply interval.** Astra showed that a late or missing GATE closed left a short reply's early
  playback unexamined, so explicit open samples there passed. The rule now:
  - **Long replies** (last write > first write + lead) keep the original interval [first write + lead,
    last write].
  - **Short replies** are scored over [first write, just before GATE open]. The mute image logs no
    device-side playback-onset line, so the first write is the start. Any open or nonzero MIC_GATE
    sample, or any GATE open, in that interval FAILs.
  - The device's latest GATE transition at or before the start must be a close. Otherwise (late close,
    missing close) the cell is INCOMPLETE, never PASS. The start is fixed, so a duplicate close can't
    move it. A missing GATE open after the reply is INCOMPLETE.
  - Fable nit 3: a GATE re-close in (GATE open, tail end] is a flap and FAILs.
- **Retirement stamp.** Teardown credit now starts at the acknowledged disconnect's OK receipt
  (`done_s`), not the lower send stamp, and only if the bracket (lower stamp to OK) is at most
  `teardown_bracket_max_s` = 0.5 s.
  - Retained brackets are 0.225–0.292 s (smoke-01, smoke-03 part 2; mute-01).
  - A wider bracket gets no credit; rows before the OK are live and gate.
  - smoke-03's teardown rows all fall after the OK receipts. The rows inside the bracket are writes,
    frames and rtvi rows, with no errors.
- **Cumulative idle accounting** (should-fix). The runner tracks the last bot activity (non-silent
  writes, bot speaking frames, user turns, transcriptions).
  - Every wait is capped at last activity + `max_runner_idle_s`.
  - A block stops before a cell whose start plus `user_turn_timeout_s` would exceed that. This is
    recorded as an `idle-budget` INCOMPLETE leaf.
  - The static `check_idle_budget` remains as plan validation. A block missing `replies` now raises
    ValueError (Fable nit 2).

## r14 changes (decision 121, Astra r13 blocker and should-fix)

- **Long replies keep r12's coverage.** r13 narrowed long replies to [first write + lead, last write].
  Astra's three synthetic faults (an early premature open and close, early open samples, nonzero drain
  samples before the final GATE open) FAILed under r12 but PASSed under r13.
  - Long replies are now checked from max(device GATE closed, first write), when a close is witnessed
    at or before first write + lead (else first write + lead), to just before the final GATE open. The
    premature-open scan starts at the first write.
  - r13's positive-evidence and flap checks are added on top.
  - Regressions compare **verdicts** with the r12 gate on Astra's cases: from recorded values, and live
    against `git show cc38e0a:hil/echoear_mute_gate.py`.
  - ~~Short replies keep r13's rule; the serial-receipt boundary stays conservative (INCOMPLETE).~~
    Superseded in the same round by the decision-122 close-witness allowance below (one rule for short
    and long replies).
- **Idle activity.** The cumulative account credits only Pipecat's idle-reset frames (the installed
  `PipelineWorker` `idle_timeout_frames` default), and only on the current epoch's peer. The epoch's
  StartFrame is credited at pre-connect.
  - The recorder observes TranscriptionFrame, InterimTranscriptionFrame and UserStartedSpeakingFrame.
  - Bot-speech proxies (writes, Bot started/stopped) and other peers are never credited, so the account
    is not advanced by anything Pipecat ignores. Qualified in r15: recorder timestamps are receipt times
    on an observer queue independent of Pipecat's IdleFrameObserver, so an observer backlog can stamp a
    credit slightly later than Pipecat's reset. Retained same-frame spreads are below 1 ms against the
    60 s planned margin, but that is not an absolute "never behind" bound.
- **Close-witness allowance (decision 122, Fable r13 blocker).** The mute image closes on RTVI
  bot-started-speaking. The `GATE closed` line reaches the host 22–307 ms later, and in 2 of 8 retained
  cells after the first non-silent write (rollback-01 warm-2 +171 ms, talk-over +180 ms). One rule now
  covers short and long replies:
  - **The close.** It must be the latest transition received by first write + 0.5 s (=
    `playback_lead_s`). The closed run starts at the first close after the last open.
  - **Positive evidence FAILs:** a sample at or after the close on the device clock that is open or
    nonzero; any GATE open in [first write, final GATE open); a flap.
  - **Pre-close samples** (received before the close, device time before it) are report-only.
  - **No close in time:** open or nonzero samples after the allowance FAIL; otherwise INCOMPLETE.
  - **Cost, as declared:** up to 0.5 s of the reply start can go unscored, the qualified pre-118
    exposure. Astra's r12 late-close shape with coherent device times is indistinguishable from the
    healthy late close and PASSes. Her literal probe (close at device t=1) still FAILs. Missing close plus
    open samples inside the allowance is INCOMPLETE. A long reply without any close is INCOMPLETE (r12
    used the lead fallback).
- **Fable nits.** Retired-peer writes never reset the idle account (current-peer filter). The rejudge
  key is `retired_ok_receipt_ns`. A named `teardown-bracket` leaf lists every bracket and is INCOMPLETE
  when one exceeds the cap; FAIL leaves still win.

## r15 changes (decision 124: mute-scorer should-fixes; smoke paths untouched)

- **Fail-closed evidence selection (Fable should-fix 1).** A MIC_GATE sample is evidence if its
  device time is at or after the close's **or** it was received at or after the close line. Only a
  sample that is pre-close on both clocks is report-only, so a close with an incoherent (future) device
  `t` can no longer silence the samples behind it (probe A8 now FAILs).
- **Witness window bounded by the reply (should-fix 2).** Transitions count only if received at or
  before min(first write + allowance, hi). The reply's own ending GATE open inside the allowance is no
  longer taken as "the latest transition", and positive evidence over [first write, hi] is latched.
- **Idle account (Astra r14 note).** Only the first observation per frame (`frame_id`) is credited,
  because Pipecat's observer sees only a frame's first push. The "never behind" claim is qualified
  above.
- **Fable nit:** the stale r14 sentence is marked superseded. The fixture-level late-close test's
  docstring now says what it proves, and a new test pins the r13 blocker on the real A-1 through
  `score_echo_cell`.
- **Scope:** only `gate_check` and `MuteLive.note_bot_activity` change. Neither is reached by the AEC
  promotion smoke: part 1 is v9, and part 2's AEC-mode cells skip `gate_check` and run no blocks. The
  reconnect plan and smoke spec are unchanged.

## r16 changes (decisions 129-131, firmware)

- **mute-02 A-4** (a 20 s "count from one to twenty slowly" reply) FAILed: the gate opened 2 s after the
  TTS faded below the device playback-activity threshold (−45 dBFS after the 500 Hz high-pass), while
  the bot was still speaking. See `impl-step-9-mute-02-diagnosis.md`.
- **Firmware** (`components/dg_audio/src/gate.cpp`, commit 8c84e05). In AEC-off builds (normal and
  mute) the RTVI speaking latch holds the capture gate closed from bot-started until bot-stopped, or a
  session stop or disconnect (the stop hook already calls `speaking(false)`). The 300 ms audible hold is
  unchanged. The self-clear is now only the fallback for a missing bot-stopped: 20 s without audible
  playback, previously 2 s.
  - Across 1498 retained replies the longest no-activity span inside a reply is 16.4 s, so 0 would
    reopen early at 20 s.
  - The AEC build keeps the qualified 2 s, selected with `#if CONFIG_DG_ECHOEAR_AEC` after a
    `__has_include` of sdkconfig.h. `gate.h` is unchanged.
- **Images** (`step-9/merge-9d-r4/`):
  - **bundle-mute** 3eaa629a: only 4 bytes of `.flash.text` (the constant) and the app descriptor
    differ from a9b61556.
  - **bundle-aec** 067ef9e0: loadable sections are identical to 69d5d764 except `.flash.appdesc`, and
    the masked flash-set compare against cbabb650 shows 0 unmasked bytes.
- **Scorer:** unchanged. The next mute run is `mute-03`.
