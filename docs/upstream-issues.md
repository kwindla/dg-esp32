# Pending upstream issue submissions

Both create-issue calls were rejected with HTTP 403 by the connected GitHub
integration on 2026-10-08. These are unfiled drafts, not issue references.

## aiortc: empty datagram aborts the DTLS receiver

[Submit to aiortc](https://github.com/aiortc/aiortc/issues/new)

Tested aiortc 1.15.0 / Pipecat 1.12.0 against esp_peer 1.5.7 on ESP32-S3,
ESP-IDF 6.1. The prebuilt peer emits a zero-byte UDP probe before DTLS. Once this
reaches `RTCDtlsTransport._recv_next`, indexing `data[0]` raises `IndexError`; the
receive task exits and the connection closes. Suppressing only zero-length
`lwip_sendto` calls allowed ten sessions and five reconnect pairs in one boot to
complete with real bidirectional Opus audio. Nonempty packets were unchanged.

Expected: ignore empty datagrams and continue receiving subsequent valid packets.
A guard before indexing data and a regression test injecting `b''` through the
ICE receive path would cover this case. No installed aiortc source was patched.

## esp_peer: SCTP initiator mismatch with aiortc

[Submit to esp-webrtc-solution](https://github.com/espressif/esp-webrtc-solution/issues/new)

Tested esp_peer 1.5.7, ESP32-S3 / IDF 6.1, aiortc 1.15.0 / Pipecat 1.12.0,
controlling ICE role, audio and an ordered reliable manually created `chat`
channel, no TURN, no auto reconnect. A normal actpass offer produces an aiortc
DTLS-client answer. esp_peer's DTLS-server role waits for SCTP INIT; aiortc's
ICE-controlled role also makes it SCTP server and wait for INIT.

Changing only complete actpass setup lines to active before signaling causes
aiortc to answer passive, esp_peer initiates SCTP, and the channel opens. Ten
plain-runner sessions / five same-boot reconnect pairs passed ready exchange and
paced real audio with this workaround. The empty UDP probe was independently
suppressed to avoid the aiortc bug described above.

This workaround violates RFC 5763 section 5 and is not proposed for general SDP.
Please confirm the prebuilt implementation's SCTP initiator policy and suggest a
standards-compatible configuration or fix. Changing the ESP ICE role to
controlled would conflict with the controlled answerer and does not solve it.

## aiortc / Pipecat: configurable outbound Opus parameters

Unfiled draft for [aiortc](https://github.com/aiortc/aiortc/issues/new) and
[Pipecat](https://github.com/pipecat-ai/pipecat/issues/new).

Tested aiortc 1.15.0 / Pipecat 1.12.0 with an ESP32-S3 receiver decoding
16 kHz mono. The installed aiortc `codecs/opus.py::OpusEncoder` sets 48 kHz,
stereo, 96,000 bit/s and the `voip` application. Its factory selects the encoder
from the codec MIME type without applying negotiated fmtp parameters such as
`stereo` and `maxaveragebitrate`. Pipecat's `SmallWebRTCTransport` exposes PCM
sample-rate/batching settings but no outbound Opus encoder configuration.
Request: an encoder configuration API with negotiated fmtp support in aiortc,
and a way for Pipecat applications to supply that configuration.

Our local workaround replaces `rtcrtpsender.get_encoder` before peer creation,
only for `audio/opus`, and uses mono, an 8 kHz cutoff and 30,000 bit/s while
retaining the required 48 kHz RTP clock. This preserves the existing SDP and
16 kHz mono fixture path. Actual outbound RTP payloads are captured before
SRTP encryption to verify TOC configuration 8–11 and a clear stereo bit.
The server can select the stock encoder with `BOT_OPUS=aiortc`.

Device-side measurements and the adoption decision are recorded in
[`step-6c-report.md`](../proj-2026-10-08-0949/codex-jobs/step-6c-report.md).
An interleaved five-boot/three-session-per-boot comparison measured median
device decode p95 **5.499 → 2.999 ms** (45.5% lower) and playback-task CPU
**24.568% → 15.332% of one core** (37.6% lower). Peer-loop CPU was
33.530% → 32.441%; peer-rx CPU did not fall (1.185% → 1.276%). Runtime
census coverage is 10 stock / 8 lean intervals; all 15 sessions per mode
remain in the codec/counter statistics. Speech-sized payloads averaged
100.436–102.071 kbps stock versus 26.603–27.841 kbps lean, excluding
headers and idle-sized packets.

The wire audit also exposes a limitation of this workaround: initial idle
packets are SILK wideband mono (TOC 9), but nine formal sessions contain
mono CELT wideband (TOC 23) during part of the greeting. An 8 kHz cutoff
does not force SILK mode. Lean is our default; acoustic greeting results were
the same for both modes (one pass, one bench-correlation failure each).
The same e185e9e firmware and speaker DSP are used for both modes; improvements
in codec cost do not by themselves establish fewer network interruptions.

## Pipecat: outbound audio catches up in a burst after an idle gap

Unfiled draft for [Pipecat](https://github.com/pipecat-ai/pipecat/issues/new).
Tested with Pipecat 1.12.0 and aiortc 1.15.0. The context hub confirms that
`RawAudioTrack` supports `auto_silence=False`, but its wall-clock scheduling
origin remains unchanged while its sample timestamp pauses awaiting data.
[Retrieved implementation](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/transports/smallwebrtc/transport.py#L126-L162).

In the API reproduction, leave a 16 kHz track idle for 250 ms, enqueue multiple
10 ms PCM chunks, then time consecutive `recv()` calls. The first three
returned in 0.016, 0.012 and 0.006 ms rather than being paced 10 ms apart.
The sample timestamps remain consecutive; their computed wall-clock deadlines
are already in the past. On a small receiver, a catch-up burst can overflow
queues or trigger drift and late-frame policies when speech resumes.

Request: re-anchor the scheduling origin after an intentional idle wait while
preserving the consecutive sample timestamps. Our opt-in `PacedIdleAudioTrack`
subclass waits for queued audio, moves the origin when overdue by more than
20 ms, then uses the native frame construction and pacing. Tests cover two
separate idle/resume transitions and preserve continuous-silence behavior.
This uses private fields and should be replaced by a supported framework fix.

Evidence: `../evidence/step8/silence-api.json`, `pipecat-hub-recv.json`,
`rtp-silence-tests2.log`, and `server-tests3.log` outside the repository.
Hardware adoption and audio-onset validation are recorded separately in the
step-8 report; this reproduction alone does not establish acoustic equivalence.

## esp_peer: socket-only select pays the general VFS routing cost

Unfiled draft for Espressif; no issue has been submitted. With esp_peer 1.5.7
on ESP-IDF 6.1, the native UDP loop calls general `select()` for socket-only
descriptor sets. At roughly 131 calls/s, the outer select interval exceeds
its nested lwIP interval by about 0.74 ms/call. These are inclusive wall
intervals, including preemption, rather than exclusive CPU measurements.
[The tested VFS implementation](https://github.com/espressif/esp-idf/blob/fff9895c82d744c7237be8847347bdd1b07c6643/components/vfs/vfs_calls.c#L547)
allocates routing arrays and visits the global descriptor table up to `nfds`,
including descriptors outside the selected set.

A local wrapper restricted to the peer owner and socket-only sets calls
`lwip_select()` directly. Two short paired screens reduced peer task runtime
by about 8–10 percentage points, while retaining the general VFS path for
mixed sets and other tasks. The first screen also regressed PLC and uplink
maximum gap; it and its repeat remain retained. Full qualification and the
final adoption decision are in the step-8 report.

Request: a supported socket-only path in the peer transport, or a VFS fast
path for socket-only sets, with documented descriptor and teardown semantics.
Native ICE/STUN, SCTP and jitter-cache internals are opaque in the supplied
binary; no claim is made that this change fixes their delivery delays.

## esp_peer: expose authenticated-audio cache delivery diagnostics

Unfiled diagnostic request for Espressif; no issue has been submitted. On
esp_peer 1.5.7, socket-read and successful SRTP-authentication hooks can be
correlated with the native audio callback using the measured relationship
`native_pts_ms = floor(extended RTP timestamp / 48) modulo 2^32`.

The corrected observer's 30-minute DIO capture contains 90,335 callbacks, all
matched to authenticated RTP, and 273,286 captured records exactly matching the
final ring count, with zero overwrites. Of 315 PLC records, 140 corresponding
packets were already authenticated at the recorded playout-event timestamp,
but had not yet crossed the native audio callback. Another 156 were sent by
the server and absent at the recorded socket boundary, 18 arrived after the
event, and one was read before authentication was observed. These distinguish
observable boundaries; they do not prove a native-cache defect or radio loss.
The event timestamp follows the consumer poll, so it is not a directly
measured native-cache deadline.

Request: supported diagnostics for audio jitter-cache depth, oldest packet
age, release/discard reason and callback scheduling time. Those would help
separate cache policy from owner scheduling without interpreting opaque
library state. Keep these diagnostics bounded and off the callback print path.

Evidence: `../evidence/step8/arrival-soak-rerun/`, including raw RTP/arrival
records, `attribution-*.json`, `final-trace-coverage.json` and the archived v11
ELF/config. The initial v3 trace and failed/incomplete repeats remain separate;
full tuning and acoustic acceptance decisions belong to the step-8 report.


## esp_peer: bounded receive work while audio is due

Unfiled draft for Espressif; no issue has been submitted. Step 8e used fixed
PCM uplink, continuous offline downlink and identical bursts of 1/5/20/50
RTVI messages, three repeats each. The original image produced 188–212 ms
maximum bot-side uplink inter-arrival gaps at N=20 and 275–442 ms at N=50.
The packets bordering most stalls had consecutive sequence numbers and
20 ms RTP timestamps. Deferring application JSON classification/dispatch to
the RX worker alone retained 167–180 ms and 247–435 ms gaps respectively.
Its owner callback averaged 56 microseconds, while individual DTLS reads
remained under 4 ms. These boundary measurements implicate cumulative native
receive work; they do not identify a particular opaque SCTP routine.

The original stress sweep also records 27 encoded-uplink queue drops and
25 ordinary application-message queue drops. Consecutive RTP identities
around a socket stall do not prove that every pre-RTP PCM/encoded frame was
retained. Application offload alone removes the message drops but retains
23 encoded queue drops. The receive-budget sweeps record zero of both;
these counters are separate from packet loss measured at the bot socket.

The local socket-select wrapper gives an established session's receive pass
a 12 ms deadline and returns an ordinary read timeout when it expires,
leaving queued packets for the next pass. Handshakes, socket writes and other
tasks retain their existing select behavior. A matched sweep then stayed at
32–48 ms for every burst. A later final-image repeat retained one failing
N=5 window (118 ms, including a missing packet), with no overlapping owner
interval over 40 ms. The report retains that failure and subsequent
send-boundary evidence separately; this is not a claim that the wrapper
eliminates WiFi/path stalls.

Request: a supported per-main-loop receive-work budget or an audio-deadline
yield API, with fairness and SCTP retransmission behavior documented. This
would avoid imposing receive scheduling through a link-time select wrapper.
Evidence: `../evidence/step8e/burst-before-01/`, `burst-after-01/`,
`burst-after-02/`, `burst-final03/`, and `burst-comparison.json`.

The final diagnostic repeat adds a successful-device-UDP-send boundary,
without changing Opus framing or production policy. Its 2,966 captured RTP
sends have a maximum device gap of 53.1 ms and no established-session owner
iteration over 40 ms. The one failing burst window has a 93 ms bot-arrival
gap versus 28.749 ms between the matching successful device sends. This
locates that remaining delay after the observed send boundary; radio TX
completion and AP/driver buffering remain unobserved. See
`../evidence/step8e/burst-final04/` and `burst-comparison-final.json`.

Receive-boundary correction: the historical server `rtp.jsonl` timestamps are
recorded after aiortc's RTP handler, following aioice queueing and SRTP
processing. They are not raw UDP socket timestamps. Thus the residual delay
after a successful device send includes possible server processing or ICE
queueing, as well as unobserved network/driver delay. New diagnostic runs add
`socket-rtp.jsonl` at asyncio's UDP callback before ICE queueing. Its public
header identities must be matched to authenticated RTP; that callback still
does not supply kernel arrival or radio TX-completion times. Earlier captures
and qualification misses remain retained.

## esp_peer: failed DTLS client handshake prevents bounded teardown

Unfiled draft for Espressif. Board-IP-scoped netem drops DTLS handshake
records for 25 seconds during setup. With uplink or bidirectional loss, the
native client first reports `MBEDTLS_ERR_SSL_TIMEOUT` after about 13 seconds.
The app's shared start deadline then expires, but joining the owner does not
finish; the board remains Starting after loss is released, and disconnect
also times out. ICE loss and the downlink-only DTLS arm do not show this hang.
The original failed captures remain in `../evidence/step8e/setup-final04/`.

The binary's `peer_on_connecting` returns without changing CONNECTING when
`dtls_srtp_handshake` fails, so the next native iteration retries the failed
SSL context. Its client handshake maps both transient and terminal results
to -1. The local adapter records the underlying mbedTLS terminal result,
stops further native iterations on that session, and checks stop/shared
deadline inside subsequent SSL handshake calls. WANT_READ, WANT_WRITE and
HELLO_VERIFY_REQUIRED remain transient. All native teardown still runs on
the owner. On the retained uplink fault, the new image closes both failed
attempts, backs off for one/two seconds and reaches Ready after release.
Evidence: `setup-final05/dtls-uplink/` and the retained `peer-*-disassembly.txt`.

Request: propagate terminal handshake failure as CONNECT_FAILED and expose
bounded cancellation of every native setup call. A connection deadline
cannot enforce bounded teardown if the owner cannot return from main_loop.


## Draft: aioice consent expiration on a high RTT path

Step 8e's board-scoped 300 ms delay / 60 ms normal jitter in both directions produces about 600 ms mean RTT. In the retained −22 capture `../evidence/step8e/matrix-final07-minus22-failed-attempts/delay-300-jitter-60-both-01/`, aioice 0.10.2 explicitly logs consent expiration and server transport disconnects 31.224 s after fault onset. The ESP32 later detects remote disconnection and retries with 1/2/4/8/16/30 s backoff. Its local HTTP adapter caps each blocking call at 500 ms to preserve cancellation; connection opens repeatedly fail in about 0.55–0.61 s while this fault remains.

This demonstrates an unsupported high-delay path and a failed keep-session expectation, not a lost app state callback. Consent transaction timeout/retransmission defaults and the local HTTP cancellation/connect tradeoff need independent matched experiments before proposing changed defaults. Step 8e retains the existing defaults and records the repeat separately. Closed PERF snapshots preserve their old PC identity and must not be scored as a surviving session. No upstream issue has been filed.

## Draft: native send-pressure path can still stall the audio owner

The Final07 −22 step-8e 10-second bidirectional blackout capture completes at `../evidence/step8e/matrix-final07-minus22/outage-10s-both/`. A separate episode **142.053–153.034 seconds after fault onset** (well after the blackout was released) increments socket-send ENOMEM by 213 and encoded-queue drops by 93. In that bracket, successful device RTP sends have a 1,839.687-ms gap. Established inclusive slow-call maxima are owner iteration 231.995 ms, control 175.675 ms, native main-loop 58.188 ms, audio-send 46.351 ms and DTLS-write 44.826 ms. Individual socket-send calls remain below 4.510 ms over the capture, and the copied application callback below 0.883 ms. The source sends up to four audio frames per control pass, so cumulative native send-call time can lengthen the owner iteration; an individual send still exceeds the 20-ms cadence.

The measured receive budget addresses the RTVI receive-drain stall but does not bound opaque send/write processing under transmit-memory pressure. These timers include preemption, and ENOMEM is a bracketed counter, so they do not identify an exclusive native retry routine or prove an AP/radio cause. The buffer briefly reaches depth 21 and drains to 7–8 after drift correction, with zero packet-pool drops; this is separate from the fixed sustained-overfill trap. Qualification remains open for transmit-pressure continuity.

Suggested upstream investigation: expose bounded/nonblocking native audio/DTLS send behavior and retry accounting, with explicit handling of ENOMEM/ENOBUFS and ownership-safe cancellation. Validate a proposed change with controlled send-pressure A/B and full device-send/UDP-callback traces; a queue-size or timer change without that evidence is not established. No issue was filed. Public raw/per-call evidence and a separate audit are retained in `send-pressure-audit.json` under the capture.

The final uniform 60-arm pressure audit also finds 361 ENOMEM errors and 93 encoded-queue drops in duplication 1% uplink, across 135.533–146.373 and 146.373–157.122-second observation brackets. Its first bracket has a 277.852-ms successful-send gap and 232.998-ms owner maximum. The other 58 arms have no observed ENOMEM increment in their matched snapshots; counter coverage and retired tails remain limited. Aggregate evidence: `../evidence/step8e/audit-final07-minus22/send-pressure-audit.json`.

## Draft: esp_peer authenticated pre-cache audio ingress and cache disable

Unfiled vendor request, step 8e2a. esp_peer 1.5.7 ESP32-S3
`libpeer_default.a` SHA256
`e6fa4ccb3256007105bf7eff1e313dc5c06669f7d3b6df192571ebdba4496285`
provides a public identity receiver RTP transformer after SRTP authentication
and native track routing, before native audio jitter insertion. Hardware
qualification retains 6,400 authenticated hook packets, including 177 reordered
packets; authenticated-to-hook p99 is 0.163 ms. The ordinary callback remains
behind the native loss hold. Evidence: `~/src/evidence/step8e2a/qualification/`.

Please document this boundary and the full authenticated RTP layout, or expose
an explicit pre-cache raw receive callback with negotiated track identity and
lifetime rules. Add a supported audio cache/NACK-disable option for applications
that own device-clock playout. An identity hook bypasses the native hold for
playback but leaves the configured 16,384-byte shadow budget, metadata and
feedback processing allocated. Neither zero timeout nor zero retransmission
count means pass-through in this binary. No private ABI, transform failure,
zero-size consumption flag or deliberate allocation failure is used locally.

## Draft addendum: aioice consent timeout under sustained 600 ms RTT (8e2b)

The pinned aioice consent task uses no transaction retransmissions, a 0.5 s
initial transaction timeout and six consecutive failures before closing ICE.
A board-only 300 ±60 ms delay in each direction puts the mean RTT above that
transaction timeout. Please expose/document a consent timeout appropriate to
supported high-RTT paths, while retaining bounded expiry and cancellation.
This request is about the consent policy; application starvation is not peer
death. Our HTTP adapter now gives synchronous open up to five seconds clipped
by the shared deadline, then restores 500 ms I/O slices, with cancellation and
deadline checks around every blocking operation. It never replays a POST.
The application retry boundary remains unchanged. Hardware follow-up is retained
in `~/src/evidence/step8e2b/candidate-01-faults02/delay-300-jitter-60-both/`.
This is a draft only; no upstream issue was filed.

### Teardown investigation evidence (8e2b; no additional issue filed)

The quiet-NIC-qualified 300 s uplink-only capture with a 30 s blackout kept
the original session and recovered uplink within 33.468 ms of fault release
(matched-clock bounds are retained in the arm result). No consent-expiry close
occurred during this run. At ordinary cleanup the authenticated server endpoint
recorded successful SCTP ABORT, then DTLS shutdown with one outbound encrypted
record flushed while ICE was completed. This is evidence that close traffic can
be emitted on a working path; it does not establish delivery or native handling
after ICE consent expiry. The historical delayed-close failure remains
unreproduced, rather than resolved by an audio-starvation timeout.

Evidence: `~/src/evidence/step8e2b/candidate-01-faults02/outage-30s-uplink/flow/`.
An unprivileged board-filtered tcpdump was refused by the host. No escalation
was attempted (sudo is restricted to tc). The fallback pcap contains the actual
encrypted bytes observed at the server UDP receive/send callbacks with
synthesized IPv4/UDP headers, alongside monotonic timestamps and endpoint
DTLS/SCTP events. It is explicitly not a kernel capture or proof of air delivery.
