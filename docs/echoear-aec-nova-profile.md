# Nova development profile (implementation, not a freeze)

`ECHOEAR_STT_PROFILE=demo` (also the unset default) retains Flux STT and its
provider turn strategies. `ECHOEAR_STT_PROFILE=dev` selects Nova-3-general with
interim results, default Silero VAD (`0.7 / 0.6 / 0.2s start / 0.2s stop`), VAD
and interim-transcription starts, and Smart Turn V3 stops. Invalid profile names
fail. The versioned firmware AEC capability and explicit duplex override still
control muting and interruptions in both profiles. Flux TTS is unchanged.

The prospective v8.5 template and `make_plan()` explicitly select `dev`. The
runner takes the declared profile rather than an ambient environment value;
older declarations without the field select `demo`. No declaration, launch
bundle, firmware or existing evidence is changed by this implementation. A new
reviewed freeze is required before acquiring with the changed source/template.

## Turn completion and provenance

The retained normal sentence04 delivered `Please change the subject to` with a
manual Nova Finalize acknowledgement before delivering `cooking.`. Pipecat marks
the acknowledgement's `TranscriptionFrame.finalized=True`, which short-circuits
the Smart Turn transcript wait. Raising the wait budget alone does not fix this.

The dev-only `NovaTranscriptBoundary` clears **utterance** finalization on final
transcript frames, keeping text, ordering and interim starts intact. Smart Turn
still decides completeness; Pipecat's documented `STTMetadataFrame` safety timer
waits for trailing text. The configured `ttfs_p99_latency=1.0` is a conservative
safety budget, **not a newly measured population p99**. The observed sentence04
tail arrived about 0.534s after speech end; the stock budget is 0.35s. Short words
without a VAD decision use the framework's transcript-inactivity fallback. This
adds turn-end wait, while interruption can still begin on an interim. It is not
a guarantee against arbitrarily late provider finals or semantically separate
speech within the wait window.

These are public [TranscriptionFrame fields](https://docs.pipecat.ai/api-reference/server/frames)
and the documented [turn strategy transcript wait](https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-turn-strategies).
Pipecat Context Hub source for
[Deepgram finalization](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/deepgram/stt.py)
and [STT metadata/finalized handling](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/services/stt_service.py)
was checked against installed Pipecat 1.12.0 signatures and a real-aggregator
regression. No library file was patched. The regression retains original PCM,
arrival cadence, transcription timing and vendor `from_finalize` flags.

Each recorded/evaluation session emits `stt-profile` with effective turn settings,
package versions, SHA-256 of local ONNX models and bot/profile code, and a
canonical configuration hash. `stt-model-observed` records the vendor's reported
model/version rather than pretending the requested alias pins cloud weights.
The metadata adapter uses the pinned Nova `_on_message` hook; framework upgrades
need review. Cloud model weights are unavailable for local hashing.

VAD starts, transcription-strategy starts, accepted turns, interruptions and bot
state are explicit fields/events. Flux proposals remain raw diagnostics, not the
acceptance definition. Echo scoring requires zero accepted turns and zero
interruptions. Final transcript recognition and application end-to-end success
remain separate from firmware-direct measurements.

## STT-independent preservation (report-only)

`echoear_aec_preservation.py` compares the fixture's active extent with delayed
post-AEC PCM using bands 300–600, 600–1200, 1200–2400, 2400–4000 and 4000–7000 Hz.
It uses the existing timed fixture waveform witness: post-AEC correlation for
barge-in rows, raw MIC1 correlation for near-only controls, and the fixed 1024
sample AFE source delay. Neither transcription nor spectral score selects the
extent. Missing/incomplete witnesses are unscorable, not passing. Waveform-match
conditioning can bias the measurable population and is reported.

Spectral power uses a mean-removed, Hann-windowed FFT over the whole extent;
each band's power is relative to the total of those five bands. The diagnostic
reports post-minus-fixture relative dB. The envelope uses corresponding 20ms RMS
windows after a single whole-extent RMS level match, with a log floor 40dB below
the fixture envelope peak. Reported values include correlation, log-RMS error,
original levels and gain needed for matching; attenuation is not hidden.
Residual echo and room/microphone coloration remain in post-AEC PCM. This does
not isolate the AEC's near-end suppression or internal vendor filter response.

`echoear_aec_preservation_report.py CAMPAIGN NEW_OUTPUT` reads retained captures
and writes per-row diagnostics, source hashes and an **in-sample proposal** from
clean firm rows. Proposed band limits are observed absolute maxima rounded up
plus 1dB; the envelope RMSE proposal is the maximum rounded up to 0.5dB plus 0.5dB.
These margins are explicit engineering proposals, not confidence bounds. All
values remain `gate=false` until the user sets tolerances. Firm/normal and
Stop/sentence/near-only groups remain separate in reporting. Retained c10 has
firm near-only controls; normal near-only coverage still needs future evidence.

## Host-controlled firmware interruption cell

`echoear_aec_host_interrupt.cell(id, old_text, next_text, offset_s=1.0)` constructs
an optional `host-interrupt` control. Its exact texts and offset must be declared
and reviewed before acquisition; no cells were added to a frozen campaign here.
The runner starts its incremental transport reader before the scripted reply,
then reserves one command number. The existing independent timer issues the
command at the declared offset from the first non-silent TTS transport write,
with the existing fresh-active-TTS witness and 100ms invocation-error ceiling.
No Jabra stimulus, transcript injection or STT turn is needed.

Evaluation queues an `InterruptionFrame` through the documented
[PipelineWorker.queue_frame API](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/pipeline/worker.py).
The [RTVI observer](https://github.com/pipecat-ai/pipecat/blob/1559a684b1ee9771b36454b72418d7364b518e7f/src/pipecat/processors/frameworks/rtvi/observer.py)
converts that frame to `bot-interrupted`; the existing two-copy notice policy
is preserved. After the declared 1.0s delay it queues the declared next TTS reply.
This reply probes recovery, not recognition or LLM responsiveness.

Analysis reports host issue→server interruption→device callback→flush-call entry,
callback→last old MIC3, and callback/last old MIC3→next reply write and MIC3 onset.
`AUDIO_CANCEL.device_us` timestamps entry to `cancel_bot_audio`; it is **not** a
measurement of flush completion. The applied flag, old-tail witness, last MIC3,
stale-output check, two RTVI copies and next-context playback provide the rest
of the existing evidence chain. Device/transport/resource/guard gates remain in
force. Missing/ambiguous timing, STT interference, an unapplied cancel, missing
next playback or missing notice copies fails this cell. No recognition or live
barge-in credit is assigned to it.

## Host validation

`server/test_nova_profile.py` replays the retained sentence04 entirely offline
through the real user aggregator: old finalization splits it into two messages;
the dev boundary produces one. The fixture embeds provenance and original timing.

`hil/echoear_aec_nova_replay.py --clips CLIPS_JSON --output NEW_DIRECTORY`
uses fresh Nova STT connections for retained c10 PCM, at original arrival cadence,
with the same bot profile, VAD, boundary and aggregator. It does not open a board,
Jabra, WebRTC transport, LLM or TTS service. It retains every request/response and
sample count, appends a separately identified six-second silence tail, and stops
on replay integrity failure without automatic retries. Run only when retained-
audio cloud replay is authorized. `echoear_aec_nova_replay_report.py OUTPUT`
checks eligible firm recognition, turn splits and echo false starts. Historical
bot-state feedback cannot establish a counterfactual flush or next reply, and
these replays never qualify a live campaign.

The tolerance report also emits separate firm Stop, sentence and near-only
proposals. Prefer reviewing these strata over borrowing a pooled limit: the
largest sentence spectral deviation should not automatically relax the Stop
comparison. No proposed values are copied into campaign acceptance gates.
