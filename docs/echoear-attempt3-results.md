# Strict attempt3: RF eligibility exhausted

Attempt3 remained **unstarted**, with zero scored turns. All five prospective
RF eligibility opportunities failed the frozen **<0.5%** admission gate.
The launcher stopped after the initial probe and four retries. No sixth probe,
scored retake, changed threshold or replacement for attempt2 was performed.
The earlier strict, quiet and combined-item1 failures remain recorded as FAIL.

Every probe used the independently flashed and verified normal ELF
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`,
with cat30fps, quiet1 and enabled continuous uplink. The full fixed60-second
window followed the greeting, gate reopening and settling. There were no
startup, disabled-audio or console exclusions. The original campaign limits
remain >60ms rate≤1%, >150ms rate≤0.05%, maximum≤2000ms; those limits were
never used to relax the separate admission condition.

| Probe | Start UTC, 2026-10-15 | >60ms intervals | Tail rate | Maximum gap | Admission |
| --- | --- | ---: | ---: | ---: | --- |
| 1 | 08:12:44.208 | 105/3001 | 3.498834% | 150ms | DEFER |
| 2 | 08:44:27.278 | 59/3001 | 1.966011% | 189ms | DEFER |
| 3 | 09:16:10.129 | 25/3001 | 0.833056% | 112ms | DEFER |
| 4 | 09:47:52.850 | 62/2998 | 2.068045% | 203ms | DEFER |
| 5 | 10:19:35.611 | 72/3000 | 2.400000% | 146ms | DEFER |

Probe3's rate below1% still fails the strict0.5% admission gate. The complete
individual windows are the populations; no pooled rate selects admission.
Probe4 also shows reordering and three sequence numbers absent from the
retained capture. Passing endpoint coverage, nontruncation and the minimum
interval count does not establish loss-free media. No source-send trace was
collected in these probes, so they do not identify a unique lower-path cause.

Each retry started at least1800seconds after the previous probe completed:
1800.245928,1800.230317,1800.229997 and1800.232436seconds. The background
timer remained active throughout. Bench, serial and bot/asset services were
released during every wait. A used its two specifically authorized short
holds during waits1 and2, returning before the retry deadlines. B restored
and verified9936da45 for every subsequent probe. There was no admitted
campaign to interrupt.

All five probes passed the raw monitor, display/resource, identity, WiFi NIC
and cleanup checks. Historical internal minima were86584,87264,85764,86776
and86588bytes; corresponding minimum largest blocks were47104,49152,47104,
47104 and47104bytes. Failed allocations were zero, and app-control minimum
free stack was8040bytes in each probe. WiFi RX+TX was1.10–1.16MB over roughly
86seconds per arm, below the unchanged proportional20MB/300s gate. Wired
traffic was retained without gating. These health observations are separate
from media transport validity and cannot qualify firmware.

The durable launch-consumption marker and strict attempt3 output directory
are both absent. Execution ended after the fifth deferral with the declared
exhaustion reason. The bench is released, no retry remains queued, and the
last installed image is the frozen normal9936da45.

Protocol and pure helpers were committed atd7bb689; the collector and one-use
launcher are at47fbeb1. Astra reviewed both before use. Host validation passed
129 related tests, including75 focused protocol/controller tests. The frozen
542-input execution binding chains the earlier526-input binding, preserves
all protected prior failures, and records the exact source/image identity.
No firmware source or build changed for this acquisition.

Evidence: `~/src/evidence/echoear/B/item2-attempt3-rf-precheck/`, including
`execution-binding.json`, `probes.jsonl`, `deferrals.jsonl`, `summary.json`,
all five raw acquisition directories and their independent Astra audits.
The declaration is [documented separately](echoear-attempt3-rf-precheck.md).
