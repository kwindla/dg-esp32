# Diag-01 host preparation — not frozen or launchable

Coordinator decisions 16 select a diagnostic hold with unchanged v8.2 candidate
ELF `61aa38d58559abe87bc54397fa8df0cec2411c1d37070d809b98d9d4e3b17242`.
No firmware rebuild, acceptance credit, jitter change, wired-host change or
scoring-policy change is authorized. Fable must review the completed freeze
before the coordinator launches. This preparation has not touched the bench.

## Unresolved acquisition constraint

The existing arrival ring holds 8,192 records (262,144 B PSRAM), approximately
28 s at the observed rate. The soak policy drains every 15 s, maximum gap 25 s.
The same single console command task executes both `dump-aec` and
`dump-arrivals`. An ordinary campaign PCM export takes 40–50 s. Host threading
cannot run the latter command while the former is executing. A second console
writer does not fix this and would break command ownership.

The coordinator was asked whether to permit explicit export trace gaps, with
capture overflow still fatal, or revise the firmware/export authorization to
obtain continuous trace. No such exception has been assumed. The diagnostic
runner, declaration and launch bundle remain unfinished pending this decision.
There is no runnable freeze or launch authorization in this document.

With an export-gap exception, the proposed sequence would be pre-capture drain,
capture (no drains), `aec-record 0`, exactly one PERF snapshot, end clock anchor,
capture trace drain, PCM export, export trace drain and after boundary. Capture
overflow must stop; export gaps must invalidate export slot attribution and
arrival distributions while preserving histogram deltas and ICMP evidence.
Without that exception the existing 25 s policy cannot cover a typical export.

## Host tools completed

`hil/echoear_aec_diag_analysis.py` provides a host-only portable cell analyzer.
It consumes raw trace/send rows, before/capture-end/after PERF snapshots,
explicit trace-coverage witnesses, device clock anchors, raw ping output,
wall/monotonic clock pairs and data-channel RTT observations. Run it with:

```sh
PYTHONPATH=hil:server python hil/echoear_aec_diag_analysis.py cell-input.json analysis.json
```

The output must not exist. `analyze_cell` defines the input schema and
`test_aec_diag_analysis.py` supplies synthetic end-to-end input. This portable
analyzer is not yet connected to a bench acquisition adapter.

For each missing Render slot, it retains full source/timestamp identity,
Wire/Authenticated/Ingress times and a temporal Plc witness. Retransmissions
retain the first observed arrival. Ambiguous identities, coverage gaps, counter
mismatches and missing packets remain unknown. Render's `seq` field is queue
depth, so it is never used as an RTP sequence number.

**The unchanged firmware does not record the exact deadline.** Both Render and
Plc records follow decoding. Arrival after Render completion proves lateness at
that boundary. Arrival before completion does not prove on-time admission and
must remain indeterminate; PERF exposes sample counts but not the missing
`RenderTick.deadline_us`. Consequently this image can discriminate some late
events, but cannot guarantee a binary upstream/playout answer for every slot.
Wire follows socket read and cannot separate air/AP from driver/lwIP buffering.

Arrival-delay distributions subtract each source/phase's fifth-percentile
device-minus-host offset. They are relative delay variation, not absolute
one-way latency; clock drift is not estimated. Capture/export histogram counts
are assigned at run termination. A three-slot run straddling the boundary can
end in export with only one new export missing slot; it must not become two
separate runs. Before-to-capture-end includes setup, and the export phase also
includes boundary commands. Actual capture anchors remain necessary for exact
speech-time correlation.

Ping correlation uses bracketed wall-to-monotonic mappings and device clock
intervals. An observed clock offset change over 10 ms makes that mapping
unavailable. The report-only excursion threshold is 100 ms, within one second
of the bounded device event. Missing replies do not exonerate either hop.
ICMP and data-channel co-occurrence suggest where to look, not causation.

`hil/echoear_aec_diag_probes.py` supplies an unprivileged process owner for
`ping -n -i 0.2 -D 192.168.4.1` and `192.168.4.50`, retaining raw stdout/stderr
and one-second wall/monotonic anchor pairs. It stops only its own children,
including on partial startup failure. These processes were mocked in tests;
no probes have been launched. The deadline helper reserves ten minutes for
restore within a 65-minute maximum hold; it rejects launches after 01:40 until
04:10 local. It is not yet wired to a running hold watchdog.

`hil/echoear_aec_pacing_draft.py` is a **proposal only**. No acceptance classifier
imports it. It selects first transmissions from the entire retained history
before selecting the scored window, deduplicates full `(pc_id, ssrc, seq,
timestamp)` identities, and requires consecutive original sequence/timestamp
increments (20 ms / 960 RTP ticks), edge coverage, maximum original gap 60 ms,
complete observer evidence and no SCTP T3/abort in the window. Retransmission
does not repair a real original-send gap. Tests cover wraps, repetitions,
out-of-window originals, real gaps, identity changes and truncated rings.

## Work remaining after the acquisition decision

Integrate a separate diagnostic adapter with the existing BenchHold ownership
and normal-image restoration path. Use the unchanged qualification-01 firm
−6 dB echo/Stop/sentence block (30 logical cells / 28 captures), same bot and
fixtures, plus the 240 s probe. Record and continue transport-quality failures;
resource floors, session-breaking device faults, host-gate and harness failures
stop. Do not inherit acceptance retry, denominator or periodic link-quality
stop rules. Keep host BSSID/signal/supplicant gates and no network mutations.

Add the capture-end snapshot, drain coverage accounting, raw probes over the
whole hold and their failure-safe cleanup, a resource-overhead measurement
protocol, and a runtime deadline watchdog that initiates cleanup with the
declared reserve. Retain stack/internal heap/idle/encode results with trace on;
any comparison with campaign-06 must be labelled observational, not a paired
causal overhead measurement. A paired trace-off/on comparison needs separate
sessions because arrival-trace is fixed at transport creation.

On every exit stop owned audio/bot/probe work, restore and boot-hash-verify normal
ELF `9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`,
compare the restored mixer, and release the hold lock. Preserve the existing
restore-failure lock retention for operator recovery. Finish the fresh
declaration, bound code/input hashes, guarded launch script and exact Python
stop pattern only when the acquisition policy is settled. Do not launch it.
