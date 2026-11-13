# Campaign-08 v8.4 freeze draft — not adopted

This is packaging of Fable's approved option (a) classifier, prepared on `aec-v84`.
It does not adopt the lull rule or authorize acquisition. It retains the v8.3.1
candidate ELF `61aa38d58559abe87bc54397fa8df0cec2411c1d37070d809b98d9d4e3b17242`,
15 dB reference gain, fixtures, four holds, ordered spare pools, playback matching,
transport policy, lattice and every numerical acceptance gate. No rebuild.

The template adds exactly the pinned `ongoing_reply_lull` policy from
`echoear_aec_lull.POLICY`. The two eligible strata are strict `eligible` and
`ongoing-reply-lull`; they jointly count toward ten eligible per kind per voice
class at -6 dB (five exposures reported at 0 dB). Each group still requires nine
successes. Firm and normal groups and their spares remain independent. Report
recognition, interruption and success rates for each stratum separately; a
materially higher lull success rate requires review.

The bounded five-frame witness, pre-near source/write proof and cancellation
censor are described in `echoear-aec-lull-v84-prep.md`. Cancellation before the
closing frame produces `unscorable-lull-cancelled`, consumes an existing spare
and earns no credit. The count across **all hold rows**, including replaced and
report-only rows, must be zero. One occurrence fails even if a spare succeeds.
Strict/silent/upstream paths, >=100 ms verified future speech, recognition,
latency, flush and integrity requirements remain unchanged. No old campaign is
rescored for acceptance. Fable's §5 clarification says no separate unseen-speech
bench validation is required before adoption; the prospective campaign reports
the two strata. User adoption and reconciliation with campaign-07 remain pending.

## Host-only packaging

From a clean committed `aec-v84` worktree, in `server/` with `PYTHONPATH=../hil:.`:

```sh
uv run --frozen --offline --with pyserial --with pyelftools \
  --with idf-component-manager==3.1.2 python ../hil/freeze_aec_campaign_v8_4.py \
  /home/khkramer/src/evidence/echoear/aec-demo/step-9/freeze-v8.4-draft \
  --output /home/khkramer/src/evidence/echoear/aec-demo/step-9/campaign-08 \
  --connection NM_PROFILE
```

The helper creates a fresh bundle and requires absent output. It binds code,
fixtures, candidate config/build evidence, reviews and source preflight, and
verifies the normal-image bundle read-only. The public playback config is reused
from the v8.3.1 frozen declaration (SHA-256
`3b4ff2cb4e5aaf63b0d93affcb6e3bfe9c3ff2b407258577b251a985aa8a6cae`),
checked against its config file, this template, the retained candidate ELF and
the playback implementation hash. No local build directory is read. It generates the launch script,
anchored Python-child stop pattern and separate host guard. Validation checks
shell syntax and exact generated content; it does not run network queries or
acquisition. `--validate BUNDLE` rechecks the frozen bundle host-only.

The launch script's **first action after shell options** requires a regular,
non-symlink `USER-ADOPTED` file at the fixed bundle path. Without it, exit 2 occurs
before changing directory, checking output/hash, querying NM/iw or executing
Python/uv. The host-guard script retains the same adoption check and replaces only
the final runner exec with an echo. The helper never creates the marker. Only the
coordinator creates it after the user's adoption decision; a marker in the
caller's working directory does not count. Do not invoke the runner directly or
edit/remove the guard. The frozen declaration's `declared`, `qualification` and
`campaign_authorized` flags encode the conditional prospective runner contract,
not user adoption or permission to bypass the launch script. `DRAFT.md` and the
freeze result explicitly record that adoption has not been granted.

After adoption, every existing hash/output/live BSSID/signal/host gate still runs.
The marker is necessary, not a replacement for packaging review, scheduling,
quiet-room confirmation or reconciliation with campaign-07. Code/declaration
changes require a fresh reviewed bundle. No launch or live host-guard execution
is part of this preparation. No builds after 04:00 and no access to the isolated
campaign worktree, board or Jabra.

The stop instructions target only the anchored Python child; SIGTERM allows the
existing per-hold cleanup to restore/hash-verify normal ELF 9936da45, restore the
mixer and release flock. Never signal wrappers, SIGKILL or force-unlock. No hold
may span 03:00–04:10 local; no automatic scheduling cutoff is added here.
