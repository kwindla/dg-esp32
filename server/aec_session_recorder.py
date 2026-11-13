"""Opt-in, per-session AEC evidence. No files are opened merely by importing.

Direct taps preserve Flux ordering. Small synchronous writes are intentional
for this demo recorder; disk stalls are measured, not hidden behind a queue.
WAV offsets are storage coordinates, NOT an uninterrupted acoustic clock.
"""
from collections import deque
from array import array
import sys
import json
import os
from pathlib import Path
import time
import uuid
import wave
from importlib.metadata import version

from pipecat.frames.frames import InputAudioRawFrame, TTSAudioRawFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

EVENTS = {
    'VADUserStartedSpeakingFrame', 'VADUserStoppedSpeakingFrame',
    'ProposedUserStartedSpeakingFrame', 'ProposedUserStoppedSpeakingFrame',
    'UserStartedSpeakingFrame', 'UserStoppedSpeakingFrame', 'InterruptionFrame',
    'BotStartedSpeakingFrame', 'BotStoppedSpeakingFrame', 'TranscriptionFrame',
    'InterimTranscriptionFrame', 'ErrorFrame', 'FatalErrorFrame',
    'EndFrame', 'CancelFrame', 'StopFrame', 'TTSStartedFrame', 'TTSStoppedFrame',
    'LLMFullResponseStartFrame', 'LLMTextFrame', 'LLMFullResponseEndFrame',
}


class SessionRecorder:
    @classmethod
    def from_environment(cls):
        directory = os.getenv('ECHOEAR_AEC_SESSION_DIR')
        return cls(directory) if directory else None

    def __init__(self, directory):
        self.directory = Path(directory) / ('session-' + uuid.uuid4().hex)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.closed = False
        self.pc_id = None
        self.written_context_id = None
        self.streams, self.offsets, self.previous = {}, {}, {}
        self.events = None
        try:
            self.events = (self.directory / 'events.jsonl').open('x')
            for name in ('uplink', 'tts'):
                stream = wave.open(str(self.directory / (name + '.wav')), 'wb')
                self.streams[name] = stream
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                self.offsets[name] = 0
            self.log('session-start', transport_write_witness=True, pipecat=version('pipecat-ai'), aiortc=version('aiortc'),
                     clock='time.monotonic_ns', host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                     continuity='unknown until independently verified; WAV offsets omit missing media')
            self.observer = SessionObserver(self)
        except BaseException:
            self.close('setup-error')
            raise

    def log(self, kind, **data):
        if self.closed:
            return
        self.events.write(json.dumps(dict(kind=kind, monotonic_ns=time.monotonic_ns(), pc_id=self.pc_id,
                                         **data), allow_nan=False) + '\n')
        self.events.flush()

    def audio(self, name, frame):
        if self.closed:
            return
        now = time.monotonic_ns()
        if frame.sample_rate != 16000 or frame.num_channels != 1 or len(frame.audio) % 2:
            self.log('gap', stream=name, drop=True, reason='unsupported-audio-format',
                     bytes=len(frame.audio), sample_rate=frame.sample_rate, channels=frame.num_channels)
            return
        n = len(frame.audio)//2
        previous = self.previous.get(name)
        # Arrival spacing is a gap suspicion, never proof of PCM loss or silence.
        gap = previous is not None and now-previous[0] > previous[1]/16000*1e9 + 40_000_000
        start = self.offsets[name]
        self.streams[name].writeframesraw(frame.audio)
        self.offsets[name] += n
        self.previous[name] = (now, n)
        self.log('audio', stream=name, frame_id=frame.id, sample_start=start, sample_end=start+n,
                 arrival_monotonic_ns=now, pts=getattr(frame, 'pts', None),
                 gap_suspected=gap, drop=False, continuity='unverified',
                 write_duration_ns=time.monotonic_ns()-now)

    def transport_write(self, frame):
        # Called by the direct tap AFTER output transport successfully wrote PCM.
        if frame.sample_rate != 16000 or frame.num_channels != 1 or len(frame.audio) % 2:
            self.log('gap', stream='tts-written', drop=True, reason='unsupported-audio-format')
            return
        samples = array('h', frame.audio)
        if sys.byteorder != 'little':
            samples.byteswap()
        active = any(sum(v*v for v in samples[i:i+320]) > 320*1024
                     for i in range(0, len(samples)-319, 320))
        self.log('tts-transport-write', frame_id=frame.id, non_silent=active,
                 context_id=self.written_context_id,
                 samples=len(samples), drop=False,
                 anchor_boundary='successful transport write observation',
                 rule='20ms PCM mean-square > 1024 LSB^2')

    def close(self, reason='exit'):
        if self.closed:
            return
        errors = []
        try:
            if self.events:
                self.log('session-end', reason=reason, samples=self.offsets)
        except Exception as exc:
            errors.append(exc)
        finally:
            self.closed = True
            for stream in [*self.streams.values(), self.events]:
                if stream:
                    try:
                        stream.close()
                    except Exception as exc:
                        errors.append(exc)
        if errors:
            raise errors[0]

    def tap(self, name):
        return AudioTap(self, name)


class AudioTap(FrameProcessor):
    def __init__(self, recorder, name):
        super().__init__(enable_direct_mode=True)
        self.recorder, self.stream = recorder, name

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        try:
            if direction == FrameDirection.DOWNSTREAM and (
                (self.stream == 'uplink' and isinstance(frame, InputAudioRawFrame)) or
                (self.stream == 'tts' and isinstance(frame, TTSAudioRawFrame))
            ):
                self.recorder.audio(self.stream, frame)
            if self.stream == 'tts-written' and direction == FrameDirection.DOWNSTREAM:
                if type(frame).__name__ == 'TTSStartedFrame':
                    self.recorder.written_context_id = frame.context_id
                elif type(frame).__name__ in ('TTSStoppedFrame','InterruptionFrame'):
                    self.recorder.written_context_id = None
            if self.stream == 'tts-written' and direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSAudioRawFrame):
                self.recorder.transport_write(frame)
            if type(frame).__name__ in EVENTS:
                self.recorder.log('server-frame', frame=type(frame).__name__, frame_id=frame.id,
                                  boundary=self.stream, direction=direction.name, context_id=getattr(frame,'context_id',None))
            # The after-STT tap timestamps proposals synchronously with delivery.
            if self.stream in ('flux','stt') and type(frame).__name__ in EVENTS:
                result = getattr(frame, 'result', None)
                # Retain provider word times/final-segment flags before the
                # Nova boundary processor. Recording only; no policy mutation.
                vendor_result = result.model_dump(mode='json') if hasattr(result, 'model_dump') else None
                self.recorder.log('flux-frame' if self.stream == 'flux' else 'stt-frame', frame=type(frame).__name__, frame_id=frame.id,
                                  direction=direction.name, text=getattr(frame, 'text', None), vendor_result=vendor_result)
            await self.push_frame(frame, direction)
        except BaseException:
            self.recorder.close('tap-error-or-cancel')
            raise


class SessionObserver(BaseObserver):
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder
        self.seen, self.order = set(), deque()
        self.bot_active = False

    async def on_push_frame(self, data):
        frame = data.frame
        name = type(frame).__name__
        message = getattr(frame, 'message', None)
        rtvi = isinstance(message, dict) and message.get('label') == 'rtvi-ai'
        if name not in EVENTS and not rtvi:
            return
        if frame.id in self.seen:
            return
        self.seen.add(frame.id)
        self.order.append(frame.id)
        if len(self.order) > 10000:
            self.seen.discard(self.order.popleft())
        if name == 'BotStartedSpeakingFrame':self.bot_active = True
        if name == 'BotStoppedSpeakingFrame':self.bot_active = False
        self.recorder.log('rtvi-output' if rtvi else 'frame', frame=name, frame_id=frame.id,
                          source=type(data.source).__name__, direction=data.direction.name,
                          bot_active=self.bot_active, vad_start=name == 'VADUserStartedSpeakingFrame',
                          accepted_turn=name == 'UserStartedSpeakingFrame', interruption=name == 'InterruptionFrame',
                          observer_timestamp=getattr(data, 'timestamp', None),
                          text=getattr(frame, 'text', None), message=message if rtvi else None,
                          timing='observer receipt; not source event time')
