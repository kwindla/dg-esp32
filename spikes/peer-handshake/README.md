# esp_peer / Pipecat decision spike

Standalone ESP-IDF 6.1 project for the ESP32-S3. This is a synthetic network
probe; it does not exercise the microphone, DAC, or Jabra acoustic path.
The root application remains separate.

From this directory, `../../scripts/idf.sh build` automatically layers the
repository's gitignored `sdkconfig.local` after `sdkconfig.defaults`. Without
local credentials it builds and idles. Check requested values with
`python ../../scripts/check-sdkconfig.py --project spikes/peer-handshake`.

Flash using `uv run python flash.py --project spikes/peer-handshake --out
proj-2026-10-08-0949/evidence/step3/<flash-name>` from `hil/`. The flash helper
and capture script use the same serial lock. No interactive monitor is used.

From `hil/`, run:

```sh
uv run python ../spikes/peer-handshake/run_capture.py \
  --out ../proj-2026-10-08-0949/evidence/step3/<new-name> --esp32
```

Omit `--esp32` for the plain runner. The script starts its own offline Pipecat
runner on port **7861**, captures ten sessions in one boot, and stops only its
own server process. It refuses an occupied port and preserves existing evidence
directories. `server_evidence.py` adds bounded PCM/SDP upload routes and passive
DTLS/RTP instrumentation only to this spike runner. Production `server/bot.py`
only gains opt-in received-audio recording.

After capture, use the server environment for analysis:

```sh
server/.venv/bin/python spikes/peer-handshake/analyze.py \
  proj-2026-10-08-0949/evidence/step3/<new-name>
```

Run that command from the repository root. Five attempts are five pairs of
successive sessions: connect, transfer, disconnect, then reconnect without a
reset. Analysis requires every session to have the actual RTVI exchange, both
correlated speech fixtures, a continuous eight-second 1 kHz tone inside a
ten-second transmission, 960-tick RTP steps, paced arrival, and no device
queue/decode errors. RTP is sorted by sequence for timestamp checks; genuine
network reordering is not treated as packet loss. WAV duration excludes the
server jitter/resampling tail; fixture duration is compared using the same
20 ms RMS envelope threshold on reference and received PCM, within 5%.

Configuration probes:

- `CONFIG_SPIKE_PTS_MS=y` is the working PTS convention. The original tick
  experiment used `n`: 960 produced 46,080 RTP ticks, not 960.
- `CONFIG_SPIKE_ACTIVE_OFFER=y` offers active DTLS. aiortc initiates SCTP by ICE
  role, while esp_peer follows DTLS role. An unmodified actpass offer left both
  sides waiting for SCTP INIT. Original and transmitted offers are preserved.
- `udp_guard.c` suppresses zero-byte UDP sends in this isolated binary. esp_peer
  sends an empty probe that makes aiortc 1.15.0 raise `IndexError` in its DTLS
  receive loop. The guard leaves every nonempty payload untouched. It is linked
  explicitly; no managed dependency or Python package is patched.
- Set `CONFIG_SPIKE_MUNGE=y` in the generated config and reconfigure for the
  third matrix mode: plain runner with on-device answer munging. It removes
  SHA-384/SHA-512 fingerprints and non-host candidates, preserving CRLF. It
  does not filter host candidates by a hardcoded interface address.

When changing defaults, fullclean, remove only this project's generated
sdkconfig, reconfigure, and check exact settings before building. The runner
records firmware hashes and a redacted resolved-config difference.

Callback work is bounded copies into preallocated queues. HTTP/JSON belongs to
the signaling task; all peer calls after open, including teardown, belong to
the owner task. An esp_timer notifies the encoder every 20 ms; encoded packets
return to the owner queue. A separate decoder records 16 kHz mono PCM in PSRAM.
The host computes fixture FFT/RMS and normalized correlation from those exact
samples. Each session owns fresh queues/context, rejecting callbacks before
joining workers, disconnecting, and closing. The owner closes after its loop
has stopped; the control task waits for its completion before freeing context.

Explicit peer caches total 112 KiB: 32 KiB send + 32 KiB receive SCTP,
16 KiB audio jitter, 32 KiB RTP send pool, plus 64 RTP queue entries. PSRAM
holds callback queues, packet buffers, JSON allocations, and the bounded PCM
capture. The prebuilt codec owns its internal state allocations. Tasks are
core 0/priority 6/10 KiB (peer), core 0/5/8 KiB (signaling), core 1/11/32 KiB
(encode), and core 1/10/16 KiB (decode). Heap and stack evidence is logged by
phase; the larger codec stacks are required by measured library stack use.

After testing, restore the root application with `uv run python flash.py
--out proj-2026-10-08-0949/evidence/step3/restore-root` from `hil/`, then capture
its boot and periodic heap log under the serial lock. Findings and the gate
verdict live in `proj-2026-10-08-0949/spike-findings.md`.
