# Candidate link soak (step 9, decisions 8–9)

This is a separate declared experiment, not acceptance or a v7 refreeze. No run
has been authorized by this implementation task. Use the frozen command only
after coordinator launch authorization; the freeze utility never starts hardware.

Candidate ELF `76e29175ec089e47cedbfba8911ac8ba8fabec8ab975b3a50f1ce2f752c223e2`,
reference gain 15 dB, playback −6 dB, cat on. Reuse the campaign BenchHold for
per-hold flock, radio pre-gate, Jabra mixer snapshot, flash/boot gain verification,
settled baseline, bot, normal `9936da45…` restoration and mixer comparison.
SIGINT/SIGTERM cancels the stimulus worker, waits for the bounded command, and
unwinds that same cleanup path. Restore failure retains the lock. No concurrent
board commands or second bench owner are introduced.
The frozen stop instructions use an anchored `pgrep -af` expression matching the
Python interpreter and exact frozen arguments. A saved nohup/uv wrapper PID must
never be used for the stop signal. Final analysis records missing evidence and
preserves the original acquisition exception if analysis itself fails.

Six target 300-second blocks alternate no-dump/dump, starting no-dump. Five short
scripted TTS replies per block start at absolute offsets 0/60/120/180/240 seconds.
The host stimulus worker does not access serial. It requires a non-silent context
write and completion, seals each case and never shifts a missed deadline. This
keeps Pipecat's 300-second idle timer fed. The existing proactive reconnect path
and strict bot/observer/resource/liveness/RF checks remain active.
Request lateness up to 1 s is allowed and recorded, including a failed deadline;
later requests retain their original absolute schedule. Sealing on the first
`BotStoppedSpeakingFrame` depends on the single-sentence text pinned by validation.
Multi-sentence text would require a different completion rule.

The sole acoustic recording is the settled baseline before connect. Dump blocks
re-export that buffer back-to-back, with trace drains between exports, verifying
PCM/timing equality on every export. A command begun before the 300-second target
may finish up to its 25-second timeout before the next block; no new export starts
after target. Per-block rates use actual PERF-to-PERF duration, not assumed 300 s.
All command start/end host times, PERF and wifi-stats boundaries, clocks, packet
rings and dumps are retained. No per-cell acceptance transport gate truncates this
link-measurement experiment. Failed integrity or acquisition stops and restores;
there are no retries or acceptance credits.

**Burst measurement needs instrumentation.** The unchanged ELF has cumulative
late/missing counts, not a count of ≥4-slot bursts. This review draft enables the
existing arrival trace before connection and applies the same drain policy in
both arms (15 s when idle; before/after each synchronous AEC export; maximum drain
gap 25 s). Thus “no-dump” means no bulk AEC export, not zero console traffic.
The trace allocates existing PSRAM storage and adds measurement traffic to both
arms. Its start/end spans are retained separately from AEC exports. This is an
explicit reviewable measurement choice, not an uninstrumented causal test.
The 8,192 × 32 B ring fills in about 28 s at the measured ~293 records/s. A 15 s
idle interval uses about 4,400 records; the 25 s maximum gap leaves only about 10%
margin. Any overflow invalidates coverage. The matched trace base rate is
comparable within this soak, but not directly to untraced campaigns 01–03.

Consecutive Render PLC/Zero decisions identify missing runs; a run ≥4 slots is
counted once, in the block containing its fourth slot, whether late or lost.
Late/unresolved attribution is diagnostic and does not invalidate covered runs.
At least four consecutive
slots must have matching later device Ingress timestamps to call it a late burst.
Missing/lost bursts without those witnesses are reported unresolved. Duplicates
are removed; generation changes, missing render ticks, overwritten trace,
incomplete endpoint coverage or disagreement with missing/zero counter deltas
invalidate the rate verdict. No total-drop upper bound is passed off as a burst
count. Complete rings are required in both arms. The analysis reports per block:
actual device missing-burst rate, late/unresolved burst counts, late/missing/zero increments, spike-seconds ≥120 ms
per minute, qualifying delay runs and unique uplink loss. Delays use each block's
fifth-percentile RTP/48kHz offset baseline; positive sequence gaps are not loss.

Declared decision: with complete measurements, dump rate ≥2× no-dump rate and at
least five pooled events means **export implicated**. Otherwise report **export
exonerated under the declared rule**, alongside event counts/exposure and the
sensitivity limitation. Zero no-dump events with ≥5 dump events qualifies as
implicated; zero events in both does not. Missing evidence gives **indeterminate**,
never exoneration. This verdict changes no acceptance rule or historical result.

The new `probe-own-rule-v1` acceptance-template field makes epoch transport use
the probe's own raw uplink ratio ≤0.005 and absence of ≥1 s gap inside its exact
PERF boundary span. Capture/inter-capture rules remain unchanged. Old declarations
without the field still reproduce v6, including its probe burst failure. New
acceptance freeze waits for the user decision on F1b; `device_alive` still rejects
any post-PLC zero, and no silence exception has been added.

The candidate already calls `esp_wifi_set_ps(WIFI_PS_NONE)` in dg_net.c at b8a6fd9.
There is no proposed power-save A/B. The host 5 GHz / board 2.4 GHz AP path remains
an observation; no radio/network configuration changes belong to this soak.
The user reports that the Eero panel places board MAC `b4:3a:45:16:2d:94` on the
same node as the host; this host-only preparation has not independently verified it.
Every before/after boundary in both arms exports `wifi-observe` before wifi-stats
and PERF, retaining raw console output, parsed rows and host export times.
Analysis reports BSSID, RSSI range, primary channel, ps_mode and observed BSSID
changes per block and across the soak. Overlapping histories are deduplicated;
null readbacks remain null. A prior sample is retained as boundary context.
The 256-row ring covers about 256 s of each 300 s block. Sequence gaps and bounded
change times are reported; a roam and return entirely inside an uncovered gap
can remain unseen. This is not continuous roaming coverage.

Host-only freeze utility: `hil/freeze_aec_link_soak.py`. Validation can be rerun
with `hil/echoear_aec_link_soak.py DECLARATION --validate`; it checks all code,
inputs and candidate/normal bundles and requires the run output not to exist.
The explicit acquisition mode is `hil/echoear_aec_campaign.py --link-soak
--declaration-sha256 HASH DECLARATION OUTPUT`. A command without the frozen digest
cannot launch. Offline analysis is `hil/echoear_aec_link_soak.py DECLARATION
--analyze OUTPUT` and never opens a device.

For decisions 10, `freeze_aec_link_soak.py DESTINATION OUTPUT --connection PROFILE`
prepares run `link-soak-02` under the same `candidate-link-soak-01` experiment
protocol. Acquisition/analysis code, candidate, prompts, block schedule and
scoring remain unchanged. The declaration binds decisions 10 and records the
coordinator-applied host BSSID lock as an environment precondition.

Use the generated `launch.sh`: it exports the IDF `ESPTOOL_PYTHON`,
`PYTHONUNBUFFERED=1` and `PYTHONPATH`; checks the declaration digest and absent
output; then requires both the saved NM connection BSSID and the live `wlp8s0`
association to equal `b4:20:46:f4:ea:e5`. Query errors or either mismatch stop
before the runner. It reads network state without modifying it. These are
prelaunch checks, not a guarantee of association throughout acquisition.
The script execs uv; graceful stop still targets the Python runner using the
anchored pattern in `stop-instructions.txt`, never the uv wrapper.

`freeze_aec_link_soak.py DESTINATION --validate-bundle` validates the declaration
and exact generated launch script, including the required esptool export and
shell syntax, without running network queries or touching hardware. Guard tests
execute the shell against fake nmcli/iw/uv commands only. Preparing or validating
a bundle does not launch it.

Decisions 11 correct the NM query to `nmcli --escape no -g ...`: get-values mode
otherwise escapes the BSSID's colons. Guard tests emulate that default escaping
and prove the old query refuses even with the correct lock. The review bundle
retains a live host guard check performed only on a copy with `exec uv` replaced
by `echo`; it cannot launch the runner.

The soak-02 declaration also binds host Wi-Fi diagnostics. Before and after every
block, both arms save raw `iw dev wlp8s0 link` output plus parsed BSSID, frequency
and signal, and raw NetworkManager journal output plus cumulative wlp8s0
supplicant-state-change count since hold start. Per-block analysis reports both
boundaries and the count delta. Errors/permission warnings remain unavailable,
not a fabricated zero. Queries have a two-second timeout each.

A host-only worker polls `iw dev wlp8s0 scan dump` at a target 2 Hz from before
BenchHold entry through restoration/exit. It reads the existing cache and never
requests a scan. Samples retain monotonic start/end, cached BSS count and errors
in `host-wifi-scan.jsonl`; hold metadata records the start, end and journal wall
clock cutoff. Samples before output creation are buffered; BenchHold remains the
sole output-directory creator. Each scan-cache query has a 0.4 s timeout; actual
sample gaps and count changes are reported per block. Missed poll deadlines are
skipped, not emitted as synthetic samples. Cache-count changes are diagnostic
witnesses, not a reliable count of scans; a scan may leave the count unchanged.
These observations add host load but never change a transport or acceptance gate.
The hold id uses the declared `run_id`, with the historical id only as a fallback
for old declarations. No scoring thresholds, firmware or bot behavior changed.
