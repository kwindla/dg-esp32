# One WiFi telemetry observation

This acquisition observes three ordinary live turns on A's exact frozen
observer-enabled source2a6acb2 image. It performs no acoustic or transport
scoring and makes no qualification claim. Earlier qualification failures and
RF deferrals remain unchanged. This is one acquisition with no replacements.

The bundle is
`~/src/evidence/echoear/A/wifi-observer-2a6acb2-enabled-image/`, ELF
`81054913f10a7c0d7f97dfe255e9f58ed6242bb982ced1de66a8f13683e339b0`.
Observer is on, AEC off, and demo autostart on. After the full-hash reset
record, the controller immediately queues `demo hold`, before waiting for
READY or starting a bot. It requires held=true, initializing=false and zero
automatic attempts. The existing boot worker's three-second grace interval
allows the queued command to run when the console starts. A missed hold
aborts this acquisition; no failure is exempted or repeated. No persisted
demo settings are changed.

The run starts after A releases its scored AEC hold. B verifies the EchoEar
MAC before flashing, verifies the complete observer boot hash, and keeps one
exclusive bench hold through capture and restoration. The cat runs at30fps,
console quiet1, volume0 and SCTP chunks1100bytes. The three frozen calibrated
prompts come from strict attempt2; the normal live-test bot returns its
ordinary short reply. Current Jabra mixer settings are recorded without
recalibration or mutation. All three turns share one peer and generation.

Each turn has a24-second window declared before prompt playback. Raw RTP,
first host UDP-callback observations, bot events and device send-return
identity traces are retained. PERF snapshots and trace exports occur only
outside those windows. WiFi driver statistics are retained before and after;
`wifi-observe` exports the buffered samples only after all three windows have
ended and audio is disabled/disconnected. Missing driver dump fields remain
unavailable; field names alone do not establish their units or semantics.

The offline join matches full RTP identity and generation between device
send-return records and authenticated host receive records. This maps receive
interval endpoints into the device timestamp domain without assuming a clock
offset or absolute one-way latency. For each tail, it retains the overlapping
WiFi counter intervals and the sample containing the right-hand send. RSSI,
PS mode and channel are discrete API reads within their own begin/end bounds;
they are not proven constant throughout a preceding one-second counter bin.
The optional UDP-callback join compares the same packet pair at two host
boundaries, preserving missing identities and receive reordering.

Native TX completion success/failure deltas cover all observed STA data over
coarse bins. They are **not retry counts**, per-RTP completion statuses,
independent proof of successful air transmission or ACK latency. Retries,
send-to-air time and channel occupancy remain null. A zero failure count does
not establish complete callback coverage. No observer-off control is run,
so this acquisition cannot measure observer cost or assign a causal RF effect.

Receive endpoint coverage and explicit nontruncated post-window status,
matching send identities, WiFi extent/clock integrity, retained history and
trace overwrites are checked separately. Incomplete evidence is labelled
INCONCLUSIVE while descriptive rows remain available. Resource/raw/identity
faults stop the acquisition; no extra probe is substituted. WiFi and wired NIC
counters are recorded as observations, without qualifying this run.

`hil/echoear_wifi_observation.py` freezes all runtime helpers, A's package,
source provenance, prompts, arena budget and coordinator release evidence
before execution. An exclusive acquisition directory prevents replay. Its
finally path independently flashes the frozen normal9936da45 bundle and
verifies the full boot hash. Restoration is reported verified only after the
serial reader closes and its complete raw log passes audit. A restoration
failure remains explicit and cannot become a success flag.

Astra reviews the capture/correlation method before use and independently
replays the raw correlations afterward. All evidence lives under
`~/src/evidence/echoear/B/`; no captures are committed to git.
