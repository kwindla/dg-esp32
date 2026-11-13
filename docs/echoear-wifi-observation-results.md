# WiFi telemetry observation results

One observation-only acquisition completed on the exact A observer-enabled
source `2a6acb223bc09d4b02ffe04beb3059f73f0592f1`, full ELF
`81054913f10a7c0d7f97dfe255e9f58ed6242bb982ced1de66a8f13683e339b0`.
B executed host commit `5f73fe8` after A released the bench. There was no
scoring, replacement acquisition or qualification claim. All earlier strict,
quiet-room and item1 failures remain; RF attempt3 remains unstarted after its
five failed admission probes.

The [prospective protocol](echoear-wifi-observation.md) was frozen before use.
The cat ran at30fps, quiet1, playback volume0, AEC off, SCTP chunks1100bytes.
The demo-profile image has autostart enabled: `demo hold` was acknowledged
with held=true, initializing=false and attempts=0 before bot startup. No
persisted settings changed. Hold evidence covers the controlled reset boot;
any pre-reset flash boot was not externally monitored. All three
predeclared24-second prompt windows
used one peer/generation. Each produced TTS; the third prompt split into two
final transcript fragments, “Please tell me the” and “of France.” Thus the
record supports three prompted windows/replies, rather than exactly three
complete STT-final turns. Transcript correctness was not scored.

## Receive and send timing

| Prompt window | Receive intervals | Gaps >60ms | Rate | Largest gap |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1200 | 15 | 1.250000% | 88ms |
| 2 | 1199 | 14 | 1.167640% | 177ms |
| 3 | 1195 | 58 | 4.853556% | 148ms |

Every one of the3594 retained receive intervals matched exact RTP identities
in the device send-return trace and a WiFi counter bin for its right-hand
send. Endpoint coverage and explicit nontruncated receive reports were
available. There were no in-window console commands or trace/history
overwrites. The wider surrounding device trace exports contain1364,1364
and1359 adjacent-send intervals, with maxima49.126,48.833 and51.181ms;
none exceeded60ms. These source summaries cover the surrounding exports,
which are broader than the24-second receive windows.

All87 tails were already present at the first host UDP callback: same-pair
UDP and authenticated receive spacing differed by at most1ms. Eighteen
third-window tails span intervening source sends, which are included in the
source-gap analysis. For example, the148ms receive gap spans four source
intervals over91.983ms; their largest adjacent gap was45.825ms. It is not a
91.983ms source stall. The177ms second-window gap corresponds to13.842ms
between consecutive source sends.

This places the observed spacing expansion after the device socket
send-return boundary and at or before the first host UDP callback. It does
not distinguish device queues/driver/RF/AP/host kernel or callback scheduling.
No absolute host/device clock offset or one-way latency is inferred. The
20ms host event-loop probe was active and reported no lag above20ms inside
these windows; that does not exclude earlier kernel queues or smaller stalls.
Tail starts after reply gate reopening numbered12,12 and55 respectively.

## WiFi samples and unavailable metrics

The sampler retained109 records without overwrite. The74 bins used by the
receive join had RSSI readings from−40 to−37dBm, PS mode0 (`WIFI_PS_NONE`)
and primary channel6 throughout, with no AP/PS read errors. These are discrete
API readings, not proof that each state held throughout its preceding counter
interval. Constant sampled PS mode/channel provide no comparison for their
effects. The following association assigns each receive interval to the
counter bin containing its right-hand source send:

| Bin RSSI reading | Used bins | Receive intervals | Tails >60ms |
| --- | ---: | ---: | ---: |
| −40dBm | 3 | 150 | 4 |
| −39dBm | 7 | 347 | 13 |
| −38dBm | 47 | 2345 | 49 |
| −37dBm | 17 | 752 | 21 |

The narrow RSSI range and these coarse, uneven populations do not establish
RSSI as the cause. Native all-STA TX completion deltas were4459 success/0
failed across those74 bins, or5972/0 across all109 retained samples. These
are not retry counts, per-RTP delivery, air/ACK timing or proof of complete
callback coverage. The before/after `wifi-stats` captures contained only
command acknowledgements, without usable driver fields. Retry counts,
send-to-air time and channel occupancy therefore remain null. There was no
observer-off control, so observer overhead and its effect remain unmeasured.

## Other observations and restoration

Across the whole before/after PERF bracket, drift drops increased0→1 and
drift inserts0→2. Late/lost/concealed/PLC/dropped, I2S write errors and
underflows stayed0. These counters are retained as observations, not a pass.
Display DMA errors and lock timeouts stayed0 and the display stayed alive.
Minimum internal heap was86020bytes, largest internal block47104bytes,
failed allocations0 and app-control minimum free stack8032bytes. Raw/resource
monitoring was clean. No acoustic qualification was performed.

Over112.037seconds, wlp8s0 RX/TX deltas were824627/702545bytes and enp7s0
were6126275/5772972bytes. Both interfaces were observed without scoring;
wired internet traffic does not define the board WiFi path.

The finally path verified the EchoEar MAC, reflashed the frozen normal image
and observed the exact boot hash
`9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758`.
Restoration was marked verified only after the serial reader closed and the
complete raw log passed audit. The controller exited0, both services closed
and the bench was released. No further acquisition is queued.

Evidence: `~/src/evidence/echoear/B/wifi-observation-1/`, including the
534-input `plan.json`, `acquisition/`, `result.json`,
`correlation-summary.json`, `restore-9936/` and independent `astra-review.md`.
The original result SHA-256 is
`4a23fb6f0df6349d14dce358ec1c9f33e3a1c677ecd90501041f165c0b4aeded`.
Astra independently replayed the raw decoder, identity joins, timing tables,
WiFi associations and complete restore log, and accepted this documentation
with its observational limits. All534 inputs,20 observer-package files and
40 captured-file hashes match. The seven protected prior failures remain
unchanged. The prospective plan and all captured files remain unchanged.
