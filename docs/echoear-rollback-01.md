# Rollback-01 draft: previous half-duplex firmware with the pinned current bot

This is host-only preparation for step 9(e), not authorization to use the bench.
The coordinator must obtain the user's go and campaign handoff, then create a
regular, non-symlink `USER-GO` in the declaration bundle. Both launch script and
Python runner refuse without it. Do not create that marker during preparation.

The rollback and **final installed image** are both the frozen normal bundle
`~/src/evidence/echoear/B/item2-master-9bc2d42-normal-image`, ELF
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`, firmware
commit `9bc2d428cd3ca0abbfdaf9763d0feb88363b069d`, autostart off. The declaration
records hashes for every manifest-listed file and the manifest, bot/runner
commit, `server/uv.lock`, all Python helpers and the public environment. No
reconstruction build is needed. Installing a future shipped image at the end
requires a separate reviewed declaration; this draft leaves normal installed. This manual/normal variant requires a console
`connect` after boot and does not autostart a demo session. A demo-day autostart
variant of the same commit is not retained and would need its own build/hash.

`ECHOEAR_AEC_FULL_DUPLEX=0` forces user mute and disables Flux interruption, but
alone is insufficient: the old image has no pong handler. The explicit
`ECHOEAR_LIVENESS_SECONDS=0` disables the new bot's watchdog. This compatibility
finding is source-derived; the future ten-second idle and reconnect witness must
verify it. Proactive TTS reconnect stays on, per-generation reconnect and leading
trim stay off; local LLM, Flux Kelsey voice, speed 1.5, expressivity 2 and four
10 ms output chunks are pinned in the public environment. Campaign evaluation
and RTP hooks are removed from the owned bot environment. Credentials are
inherited/resolved privately and never dumped. No bot code is changed.

## Preparation (host-only)

From the clean committed `merge-prep` worktree:

```sh
python hil/echoear_rollback.py prepare \
  /home/khkramer/src/evidence/echoear/aec-demo/step-9/rollback-01-draft
python hil/echoear_rollback.py validate \
  /home/khkramer/src/evidence/echoear/aec-demo/step-9/rollback-01-draft/declaration.json
```

The fresh bundle contains the declaration, guarded launch script, stop instructions
and host validation record. Output `step-9/rollback-01` stays absent. A later
checkout, code, dependency or image change fails validation and needs a new draft.
The launch and bot use `uv run --frozen --offline`; dependencies must already be
available before the bench slot. Preparation performs no socket, serial, mixer,
board or bot operations and no builds.

## Future authorized hold: about 10–15 minutes

Use an attending operator and a quiet room after the campaign owner has reported
normal restore/full boot hash, mixer equality and lock release. No other bench
owner, no hold across 03:00–04:10 local, free port 7861, working credentials/local
LLM. The runner refuses starts from 02:30 through 04:09 to allow additional cleanup margin;
the 720-second acquisition deadline leaves cleanup time but is not a hard bound
on recovery. All checks have finite normal-operation timeouts. Never run detached
without the operator terminal.

The fixed sequence is:

1. Validate marker, declaration digest, code/dependency/image hashes and operator
   handoff confirmation. Take the shared bench flock nonblocking. Verify the pinned
   USB/MAC, flash the full normal bundle and verify the complete boot ELF line.
2. Set demo hold, audio off, diagnostic logging on, gate auto and playback -6 dB.
   Start only a new owned bot process group on port 7861; retain its PID/argv/log.
   Connect, wait Ready and enable microphone capture.
3. Operator performs the six declared prompts: one cold and two warm ordinary
   turns; a longer reply with two spoken Stops during audible playback; a near-only
   recovery turn; and, after ten seconds connected idle and one disconnect/reconnect,
   another reply. The operator types `PASS <observations>` only after each complete
   audible answer. Use `PASS ` plus at least five characters of observations, or
   `FAIL <reason>` to abort. No automatic retries.
4. Corroborate each note with session user/assistant transcripts, successful
   non-silent transport writes, completed playback and closed-gate zeroed PCM
   followed by reopening. No interruption during the reply; the talk-over must not
   create a second accepted user turn. Require two override-0 policy lines, a new
   peer after reconnect, no liveness alarm/reset/fatal, and no new legacy fault
   counters in either epoch. Retain PERF, all serial/session logs and PCM.
5. On completion, failure, EOF, timeout, SIGINT or SIGTERM: stop only the owned bot
   group with TERM, close serial, reflash normal and verify its entire boot hash
   again. Require the owned group and port to be clear before releasing flock.

The speaker and talk-over witness is operator-observed, corroborated by console
and bot evidence; serial receipt time is not a calibrated acoustic boundary.
This is a functional rollback smoke, not AEC/barge-in qualification. Missing or
contaminated evidence fails, and boot success alone cannot pass. An interruption
at the start of the user's prompt, before TTS, is not counted as an interruption
of playback. Legacy resource snapshots are retained; no new AEC fields/floors are
required. Counter comparisons are per peer epoch so reconnect cannot erase a
first-epoch fault. The declaration pins exactly seven raw 9bc2d42 PERF paths:
`failed_allocations.total`, `console_output.full`, `console_output.transport_drops`,
`i2s_underflows`, `i2s_write_errors`, `i2s_write_timeouts`, `pcm_queue_drops`.
Each must be a non-negative integer and unchanged within its epoch; missing,
invalid or decreasing values fail. The four `AUDIO` serial counters remain in the
retained serial/snapshot evidence but are not required as nonexistent `audio.*`
PERF fields. A regression uses the hash-pinned retained old-image PERF and gate
lines; synthetic bot events in that test are not historical smoke evidence.

The smoke boot-count check uses only the current smoke serial reader and runs
before cleanup. Do not move it after final restoration or combine the restore
reader's boot log with it: the expected final boot is a separate witness, not an
unexpected smoke reset. After a failed USB/MAC identity check, no flash or restore is
attempted against an unidentified board.

This draft uses **operator speech only**: no Jabra playback/recording, mixer
commands or geometry changes. Mixer state is declared untouched, not falsely
claimed byte-compared. Existing speaker output is used at declared -6 dB.

## Cleanup failure and stop

Signal the exact Python `echoear_rollback.py run` child with the declared digest,
not its uv wrapper. TERM/INT are ignored during cleanup. Every independent cleanup
step is attempted even if bot stop fails. A restore/hash failure, surviving owned
bot/group, serial-close failure or occupied port retains the bench flock and
records failure. Investigate while the runner remains alive; send SIGUSR1 to that
same runner to request another cleanup attempt. It never retries acquisition or
turns a failed attempt into a successful smoke. Never SIGKILL, force-release flock,
kill unknown services or claim cleanup from a stale result file. Recovery failure
can outlast the estimate. Process/host loss cannot be made cleanup-safe by Python;
that requires operator recovery before any subsequent bench use.

## Reconnect identity and report-only rejudging

Reconnect identity comes from each turn's user transcript, assistant transcript,
and transport-write events. All three must carry the same nonempty peer ID. The
reconnected peer must differ from **every** earlier turn's peer. Unrelated old
peer teardown events inside an operator window do not identify the reply.
The retained board `STATE Ready` lines must also establish a new generation
between the before/after-reconnect PERF snapshots, whose peer IDs must match the
turn witnesses. Missing or contradictory peer/generation evidence fails.

Every user turn inside an operator window is evaluated separately, bounded by
the next user-turn start. It needs its own complete assistant response, matching
TTS contexts, successful non-silent transport writes, end-of-playback witness,
no playback interruption and closed/reopened microphone gate. A successful second
reply cannot rescue a failed first one. The operator note remains scoped to the
whole window; two replies under one note do not have independent per-reply human
attestations. Prompt deviations and extra turns must be reported separately
from functional transport/playback success.

`PYTHONPATH=hil python hil/echoear_rollback_rejudge.py /path/to/retained/rollback-01`
is file-only. It creates only a new `rejudge.json`, refuses overwriting one,
hashes all original files before/after, and leaves the original failure result
and declaration untouched. It reports each turn, protocol deviations, new peer
and board generation, and the status of audits skipped by the original raise.
Retained PERF snapshots are bound to raw serial records before counters are
recomputed. `functional_claims_supported` and `all_declared_checks_passed` are
separate: an off-script or extra turn can preserve functional evidence while
violating the declared window sequence. This report does not authorize a rerun
or re-pin a historical launch bundle to a newer checkout.
