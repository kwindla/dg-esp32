# ESP32 SmallWebRTC transport

`EspPeerTransport` implements the vendored Pipecat C++ Transport interface with
native `esp_peer` and `esp_audio_codec` calls. There is no alternate-backend API.
The current target is the local Pipecat 1.15.0 runner on port 7861 in **plain**
mode, with `BOT_MODE=offline-test` for digital-media acceptance.

The start request's session ID derives the offer URL, retaining headers and query
parameters; explicit `webrtcRequestParams` takes precedence. One deadline covers
local SDP, HTTP signaling, native DTLS/ICE and reliable ordered `chat` opening.
Only IPv4 UDP host candidates on the station subnet or matching the signaling
host survive answer filtering. The native binary has a ten-remote-candidate cap;
zero survivors or more than ten usable candidates fail rather than silently
losing the intended route. The channel's actual stream ID is used for every send.

Two interoperability workarounds are required for this pinned pairing:

- `rewrite_aiortc_active_offer` rewrites complete `a=setup:actpass` lines to
  `a=setup:active`, returning the new byte length. This is aiortc-specific and
  violates RFC 5763 section 5's actpass-offer requirement. It resolves the tested
  mismatch between aiortc's ICE-derived SCTP role and esp_peer's DTLS-derived
  initiator behavior. Other answerers may reject it.
- `--wrap=lwip_sendto` suppresses only size-zero sends and increments an atomic
  counter. No logging occurs inside the wrapper. There is no public mapping from
  peer handles to sockets, so this narrowly conditioned wrapper is image-wide.
  aiortc 1.15.0 otherwise indexes `data[0]` and closes the receive task.

Issue filing was attempted through the connected GitHub integration on
2026-10-08, but both repositories returned HTTP 403, “Resource not accessible by
integration.” **No issue was created.** Reproduction drafts are in
[upstream-issues.md](upstream-issues.md), with the corresponding submission links.

## Ownership and teardown

A fresh session owns four workers. Every esp_peer call after open is on the
owner task, including new_connection, channel creation, answer application,
send_audio, send_data, disconnect and close. Debug assertions also check callback
ownership. Callbacks copy into bounded queues; no HTTP or observer call occurs
inside one. Native callbacks return zero even when a drop is counted.

Disconnect rejects work and advances generation, wakes PCM readers, joins encode,
decode and message-dispatch workers, then asks the owner to disconnect and close
after its main loop ends. Joining the owner precedes codec and queue destruction.
An owner-creation failure is the only case where ownership never transfers from
the connecting task. Session references keep buffers alive for concurrent public
read/send calls. Remote terminal states and peerLeft are deduplicated per session;
intentional shutdown suppresses remote-disconnect notifications. Never send pings.

## Audio and allocation

The SDP is Opus/48000/2. Actual encoding and decoding are 16 kHz mono, 20 ms,
320 PCM16 samples. Native PTS uses milliseconds; the sender advances 20, which
becomes 960 RTP ticks. The PCM framer holds four frames; encoded packets use
an eight-entry (160 ms) queue to the owner, covering native SCTP send stalls.
Receive metadata uses PTS because esp_peer's
callback exposes no RTP sequence number. Decode follows the host-tested playout
clock, with up to 100 ms PLC, silence/resync for longer gaps, 500 ms idle detection
and a 100 ms idle read wait. The vendor PLC wrapper requires a non-null dummy
buffer even when the recovery payload length is zero.

aiortc's idle RTP can arrive before SCTP opens. These packets are counted as
`preconnect_packets` and discarded before decode: the C++ core cannot consume
PCM until transport connect returns. The RTVI greeting begins after client-ready
on the open channel, so it is retained. This prevents startup silence from
overflowing the playback queue during a slower SCTP handshake.

PCM, packet and message backing storage is explicitly in PSRAM. The 32-slot
message store costs 512 KiB at the 16 KiB cap; ordinary traffic cannot occupy its
reserved control slot. Native SCTP handles fragmentation; hardware verifies the
1/15/17 KiB acceptance matrix. The encoder/decoder have 32/16 KiB stacks, the peer
owner 10 KiB, and receive dispatch 8 KiB. Core threads retain their 12 KiB budgets.
Native codec allocation hooks are resolved from
[Espressif's media_lib_os.h](https://github.com/espressif/esp-adf-libs/blob/master/media_lib_sal/include/media_lib_os.h)
and the linked 2.6.2 archive's weak `audio_codec_mem.c` symbols; large codec state
uses explicit SPIRAM allocation rather than assuming the malloc threshold.
Peer cache budget is 112 KiB and 64 RTP queue entries.

## Verification

`bash scripts/host-tests.sh` runs upstream and local pure-logic tests.
`hil/flash.py` builds/flashes root firmware under the shared serial lock.
`hil/transport_test.py --out <ignored-evidence-directory>` starts an owned runner,
checks the running ELF hash, captures decoded PCM, verifies fixture correlation
and duration, tests message limits, observes server RTP sequence/timestamp/arrival
coverage, and performs two reconnects in one boot. It does not test I2S/acoustics.
Console commands use `#<sequence>` acknowledgments: stats, play, message <bytes>,
tone-tx, speech-tx, rtp-stats, dump-rx, disconnect, connect. The console announces
readiness before connecting; the harness starts each session explicitly.

The 1/15 KiB matrix validates the entire echo on the device as well as server
receipt; 17 KiB is rejected locally. Complete these boundary tests before the
duplex media trial: concurrent 15 KiB SCTP reassembly disturbed incoming RTP in
the pinned native stack. Ordinary telemetry continues during duplex audio.
Silero correctly rejects a pure 1 kHz tone as speech. Tone delivery is proved by
RTP and recorded PCM; a separate committed speech fixture must produce the
server's `speaking=true` telemetry.

The chosen owner receive timeout is 10 ms with a 1 ms loop delay. A 32 KiB
instruction cache and 1 kHz FreeRTOS tick are required by the measured duplex
workload; these settings are recorded in `sdkconfig.defaults`. Data cache stays
at 32 KiB to retain the internal heap floor. The synthetic source precomputes
its sine frame so it does not starve the codec workers with repeated `sin` calls.
The upstream process-lifetime message-type table is initialized before the
per-session heap baseline; session allocations must still return within 4 KiB.
WiFi uses a full scan and signal-strength ordering for multiple APs with the same
SSID, plus deadline-bounded association retries. Association logs RSSI/channel;
password-protected networks require WPA2 or stronger and PMF capability.

Server RTP observation is opt-in via `RTP_EVIDENCE_DIR`. It observes the pinned
aiortc receiver without changing packets and associates records with `pc_id`,
not receiver creation order. Device stats separately identify whether server loss
is known; local send success is not a delivery assertion. The final `rtp-stats`
request avoids treating an older periodic report as the end-of-stream total.
`queue_dropped` must remain zero in acceptance, including playout capacity
overflow. `playout_dropped` also counts correctly rejected late/duplicate/expired
packets and remains visible alongside PLC and loss counters; it is not evidence
of queue overflow. Active-stream PCM read stalls increment `underruns`; normal
idle waits do not.
