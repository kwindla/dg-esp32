# Campaign-04 prospective freeze (v7)

Coordinator decisions12 adopts F1b (user decision 2026-10-17 13:15). This is a
host-only preparation, NOT EXECUTED; a separate bench GO is required. Earlier
campaigns and frozen declarations retain their verdicts. Candidate ELF
76e29175ec089e47cedbfba8911ac8ba8fabec8ab975b3a50f1ce2f752c223e2, firmware,
bot, matrix, thresholds, voice blocks, soak and normal-image restoration stay
unchanged. The four-minute probe uses its own epoch transport rule.

F1b device alive requires zero starvation, intact liveness and resources.
Post-PLC zeros remain reported. Nonzero zeros qualify for environmental recovery
only with a no-speech-due witness or a wholly late interval. When arrival tracing
is enabled, complete Render coverage and a later matching Ingress for **every**
missing slot are mandatory; missing/overwritten trace never falls back to counters.
Tracing stays off in this campaign, so the approved fallback is late == missing
with positive missing delta. The no-speech witness is deliberately conservative:
a recorder start before the attempt, uplink within 100ms of its end, and no TTS PCM/write, gap or session
end anywhere in the epoch. Earlier queued TTS is never assumed drained. Missing
recorder evidence is not silence. All other environmental witnesses still apply.
Caps remain 3/hold and 6/campaign; per-cell burst bound remains <=3. A transport
failure is retained and excluded, not credited as a passed cell.

The launch and each hold require the saved NM profile BSSID lock to equal
b4:20:46:f4:ea:e5. Queries use `nmcli --escape no`. Every acquisition boundary,
connected boundary, hold entry and hold end retains `iw dev wlp8s0 link`
BSSID/frequency/signal and cumulative NetworkManager supplicant state changes
since hold entry. Signal must be >= -60 dBm, association must match the lock,
and state changes must be zero. Missing/failed queries refuse credit. The cached
BSS count is polled at 2 Hz without triggering scans; changes, errors and sampling
gaps are diagnostic only. A host failure is sticky, stops the campaign, grants
no hold credit, and uses normal-image/hash/mixer restoration and lock release.
Checks continue through cleanup. It is recorded as an environment pause.

The first hold checks recurrence before subsequent scoring, using the unchanged
qualifying uplink-run criteria (>=3 consecutive seconds with max delay >=120 ms,
>=25 packets >100 ms in a five-second span). Delay baseline is the peer epoch's
5th percentile. The fixed soak-01 lattice is phase 3656270.038243419 seconds,
period 304.4058455722058 seconds on host boot
676ff572-1edc-4588-9651-129d4054bd87. Two distinct adjacent cycles in the retained
[-5,+20] second onset/tail windows stop the hold. These windows retain the
previous report's scan-tail inspection range; they are not fitted to campaign-04.
A host reboot refuses the old monotonic lattice and requires re-examination.
With tracing off there is no measured consecutive-run counter. Four or more
late/missing slots summed across retained PERF intervals overlapping one lattice window
are therefore conservatively treated as a **possible** late run for the stop
only. This can pause on isolated slots; it never asserts a measured burst or
relaxes the cell gate. A wide interval overlapping multiple cycles cannot supply
two events. Raw intervals and matches are retained in periodicity.json.

Freeze with `hil/freeze_aec_campaign_v7.py` from a clean committed checkout.
The bundle binds code, dependencies, fixtures, prior source continuity and review
inputs. The launch script exports ESPTOOL_PYTHON, PYTHONUNBUFFERED and PYTHONPATH,
checks the declaration hash and absent output, checks live host conditions, then
execs the frozen command. The guard-check copy replaces **only** the final exec
with an echo and is the only script executed during freeze. Anchored pgrep stop
instructions select the Python child, excluding uv/nohup. SIGTERM invokes normal
cleanup; never SIGKILL or force-release flock.
