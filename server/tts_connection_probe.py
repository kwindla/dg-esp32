"""Pinned Flux reconnect policy; no interaction-policy changes.

Re-verify this file FIRST on a Pipecat upgrade. The full private cycle stops
metrics, so it is allowed only between generations, before the next metric
window. Unsupported versions/sources/interfaces fail closed without reconnecting.

Re-verified for 1.15.0 (from 1.12.0): flux/tts.py differs only in how a failed
handshake is reported (Deepgram's refusal reason, when there is one), and
flux/tts_base.py only in a docstring. _disconnect, _connect and
_transport_is_active are unchanged, and so are WebsocketService._connect and
_disconnect. One thing under them did change: _websocket_connect now returns a
connection whose sends time out after five seconds, so a blocked send closes
the transport (ConnectionClosedError) instead of waiting (Sol WP8 note 1).

Re-verified for 1.16.0 (from 1.15.0): flux/tts.py and flux/tts_base.py differ
only by the new normalize_volume setting (unset here, so the query string is
the same). _disconnect, _connect and _transport_is_active are unchanged, and so
are WebsocketService._connect and _disconnect. Under them, _websocket is now a
property: clearing or replacing it aborts the connection it held if that
connection is not closed yet, so a disconnect cancelled before its close
finished no longer leaves the provider connection open.
"""
import asyncio
import hashlib
from pathlib import Path
import time
import pipecat
from loguru import logger
from pipecat.services.deepgram.flux import tts_base, tts
from pipecat.services.deepgram.flux.tts import DeepgramFluxTTSService
from pipecat.frames.frames import TTSStoppedFrame, EndFrame, CancelFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection

EXPECTED_VERSION = '1.16.0'
EXPECTED_HASHES = {
    'tts_base': '79ad5130d1b2a011016b336cb5d5713de2ff8f9481e1122fabca7f49af1bf892',
    'tts': 'f89a43c3a89997fe2121578094776ee4511ebcdf4b14f563ee349e69f25c9a43',
}
PRIVATE_NAMES = ('_disconnect', '_connect', '_transport_is_active')


def source_hash(module):
    try:
        return hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    except (OSError, TypeError, AttributeError):
        return None


SOURCE_HASHES = {name: source_hash(module) for name, module in
                 (('tts_base', tts_base), ('tts', tts))}


def supported(service, version, hashes):
    return (version == EXPECTED_VERSION and hashes == EXPECTED_HASHES
            and all(hasattr(service, name) and callable(getattr(service, name))
                    for name in PRIVATE_NAMES))


class ConnectionProbeTTS(DeepgramFluxTTSService):
    def __init__(self, *args, probe_event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._probe_event = probe_event or (lambda *args, **kwargs: None)
        self._connection_lock = asyncio.Lock()
        self._generation_active = False
        self._probe_closing = False
        self._refusal_logged = set()
        self._probe_context_id = None
        # Provider acknowledgments are evidence only, never a lifecycle gate.
        self._cancel_context_id = None

    def reconnect_allowed(self, event):
        reason = None
        if not supported(self, pipecat.__version__, SOURCE_HASHES):
            reason = 'unsupported-version-source-or-interface'
        elif self._generation_active:
            reason = 'generation-active'
        elif self._probe_closing or getattr(self, '_disconnecting', False):
            reason = 'service-closing'
        if reason:
            if reason not in self._refusal_logged:
                self._refusal_logged.add(reason)
                logger.warning('TTS_RECONNECT disabled: {}', reason)
            event('tts-reconnect-refused', reason=reason)
            return False
        return True

    async def prepare_generation(self, event):
        async with self._connection_lock:
            if not self.reconnect_allowed(event):
                return False
            started = time.monotonic()
            event('tts-reconnect-start')
            await self._disconnect()
            # End/Cancel may arrive while the private disconnect awaits I/O.
            if self._probe_closing:
                event('tts-reconnect-aborted', reason='shutdown')
                return False
            await self._connect()
            if not self._transport_is_active():
                raise RuntimeError('TTS reconnect did not establish an active transport')
            event('tts-reconnect-ready', elapsed_s=time.monotonic()-started)
            return True

    async def start_ttfb_metrics(self, *args, **kwargs):
        async with self._connection_lock:
            self._generation_active = True
            self._probe_event('tts-generation-metric-start', context_id=self._probe_context_id)
            return await super().start_ttfb_metrics(*args, **kwargs)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if type(frame).__name__ == "MetricsFrame":
            for metric in frame.data:
                if type(metric).__name__ in ("TTFBMetricsData", "TTFAMetricsData"):
                    self._probe_event("tts-context-metric", context_id=self._probe_context_id,
                                      metric_type=type(metric).__name__, data=metric.model_dump())
        await super().push_frame(frame, direction)
        if isinstance(frame, TTSStoppedFrame):
            self._generation_active = False
            self._probe_event('tts-generation-stopped', context_id=frame.context_id)

    async def on_turn_context_created(self, context_id):
        self._probe_context_id = context_id
        await super().on_turn_context_created(context_id)

    def _on_interruption_stopped(self, context_id):
        pass

    async def _handle_message(self, msg):
        await super()._handle_message(msg)
        # The provider may acknowledge late or never. It cannot stop a newer
        # context, schedule reconnect, or hold up a new generation.
        if (msg.get('type') == 'SpeechInterrupted' or
                (msg.get('type') == 'Warning' and msg.get('code') == 'NO_ACTIVE_SPEECH')):
            self._probe_event('tts-interruption-provider-stop',
                              context_id=self._cancel_context_id)
            self._cancel_context_id = None

    async def process_frame(self, frame, direction):
        if isinstance(frame, (EndFrame, CancelFrame)):
            self._probe_closing = True
        cancelled = isinstance(frame, InterruptionFrame) and self._generation_active
        context_id = self._probe_context_id
        if cancelled:
            self._cancel_context_id = context_id
        await super().process_frame(frame, direction)
        if cancelled:
            self._probe_event('tts-interruption-local-cleanup', context_id=context_id)
            self._probe_event('tts-interruption-stopped', context_id=context_id,
                              boundary='local-cleanup')
            # A new context may have started while base cleanup yielded. Never
            # clear its active flag or reconnect on behalf of that newer context.
            if self._probe_context_id == context_id:
                self._generation_active = False
                self._on_interruption_stopped(context_id)


class ProactiveConnectionTTS(ConnectionProbeTTS):
    """Prepare the next connection after a stopped, fully cleaned audio context.

    One owned task serializes with both the next turn and metric-start boundary.
    It is never cancelled by a fast turn; a failed attempt falls through to the
    library's ordinary inactive-transport recovery on the next run_tts call.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proactive_task = None
        self._stopped_contexts = set()
        self._last_scheduled_context = None

    async def _wait_reconnect(self):
        task = self._proactive_task
        if task is not None:
            started = time.monotonic()
            if not task.done():
                self._probe_event('tts-reconnect-wait-start')
            await asyncio.shield(task)
            self._probe_event('tts-reconnect-wait-end', elapsed_s=time.monotonic()-started)

    async def _attempt_reconnect(self):
        try:
            return await self.prepare_generation(self._probe_event)
        except Exception as exc:
            # Keep Pipecat's ordinary on-demand recovery; don't create a second
            # reconnect here, retry in a loop, or turn a fast turn into a cancel.
            self._probe_event('tts-reconnect-failed', error=type(exc).__name__)
            return False

    def _schedule_reconnect(self, context_id):
        if self._last_scheduled_context == context_id:
            return
        if self._proactive_task is not None and not self._proactive_task.done():
            return
        if not self.reconnect_allowed(self._probe_event):
            return
        self._last_scheduled_context = context_id
        self._probe_event('tts-reconnect-scheduled', context_id=context_id)
        self._proactive_task = asyncio.create_task(self._attempt_reconnect())

    def _on_interruption_stopped(self, context_id):
        self._schedule_reconnect(context_id)

    async def on_turn_context_created(self, context_id):
        self._probe_event("tts-context-request", context_id=context_id)
        await self._wait_reconnect()
        await super().on_turn_context_created(context_id)

    async def start_ttfb_metrics(self, *args, **kwargs):
        await self._wait_reconnect()
        return await super().start_ttfb_metrics(*args, **kwargs)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        await super().push_frame(frame, direction)
        if isinstance(frame, TTSStoppedFrame):
            self._stopped_contexts.add(frame.context_id)

    async def on_audio_context_completed(self, context_id):
        await super().on_audio_context_completed(context_id)
        # The public completion hook follows TTSStoppedFrame and removal from
        # the audio-context map. Never start merely on an InterruptionFrame.
        if context_id in self._stopped_contexts:
            self._stopped_contexts.discard(context_id)
            self._schedule_reconnect(context_id)

    async def stop(self, frame):
        self._probe_closing = True
        await self._wait_reconnect()
        await super().stop(frame)

    async def cancel(self, frame):
        self._probe_closing = True
        await self._wait_reconnect()
        await super().cancel(frame)

    async def cleanup(self):
        self._probe_closing = True
        await self._wait_reconnect()
        await super().cleanup()
