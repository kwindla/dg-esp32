"""Diagnostic only: trim leading near-digital silence of a TTS context.

Disabled by default (DEMO_TTS_TRIM_LEADING=0). Flux streams the silence at
real time; removing already received samples cannot advance speech arrival.

20 ms mean-square <=1024 LSB² is the existing recorder's silence rule.
Keep 40 ms before the first active frame; never alter speech or interior pauses.
This changes PCM lead-in only, not user turns, interruption or guard policy.
"""
from array import array
from copy import copy
import sys

from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame, InterruptionFrame, CancelFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


class LeadingSilence:
    FRAME_BYTES=640
    PREROLL_BYTES=1280

    def __init__(self):
        self.pending=b''
        self.preroll=b''
        self.active=False
        self.removed_samples=0

    def feed(self,audio):
        if len(audio)%2:raise ValueError('PCM must contain whole 16-bit samples')
        if self.active:return audio
        self.pending+=audio
        while len(self.pending)>=self.FRAME_BYTES:
            part,self.pending=self.pending[:self.FRAME_BYTES],self.pending[self.FRAME_BYTES:]
            samples=array('h',part)
            if sys.byteorder!='little':samples.byteswap()
            if sum(v*v for v in samples)>320*1024:
                self.active=True
                result=self.preroll+part+self.pending
                self.preroll=self.pending=b''
                return result
            self.preroll+=part
            if len(self.preroll)>self.PREROLL_BYTES:
                drop=len(self.preroll)-self.PREROLL_BYTES
                self.removed_samples+=drop//2
                self.preroll=self.preroll[drop:]
        return b''

    def finish(self):
        result=self.preroll+self.pending
        self.preroll=self.pending=b''
        return result


class TTSLeadingSilence(FrameProcessor):
    def __init__(self,enabled=False,event=None):
        super().__init__(enable_direct_mode=True)
        self.enabled=enabled
        self.event=event or (lambda *a,**k:None)
        self.trim=LeadingSilence()
        self.current=False
        self.template=None
        self.logged=False

    def _audio(self,frame,audio):
        result=copy(frame)
        result.audio=audio
        result.num_frames=len(audio)//(2*frame.num_channels)
        return result

    async def process_frame(self,frame,direction):
        await super().process_frame(frame,direction)
        if isinstance(frame,(InterruptionFrame,CancelFrame)):
            self.trim=LeadingSilence();self.current=False;self.template=None
        if direction==FrameDirection.DOWNSTREAM:
            if isinstance(frame,TTSStartedFrame):
                self.trim=LeadingSilence();self.template=None;self.logged=False
                self.current=bool(self.enabled() if callable(self.enabled) else self.enabled)
                self.event('tts-leading-trim-start',enabled=self.current,context_id=frame.context_id)
            elif isinstance(frame,TTSAudioRawFrame) and self.current:
                if frame.sample_rate!=16000 or frame.num_channels!=1:
                    raise ValueError('Leading trim is qualified only for 16 kHz mono PCM')
                self.template=frame
                audio=self.trim.feed(frame.audio)
                if not audio:return
                if not self.logged:
                    self.event('tts-leading-trim',removed_samples=self.trim.removed_samples,
                               preroll_ms=40,context_id=frame.context_id)
                    self.logged=True
                frame=self._audio(frame,audio)
            elif isinstance(frame,TTSStoppedFrame) and self.current:
                tail=self.trim.finish()
                if tail and self.template:
                    await self.push_frame(self._audio(self.template,tail),direction)
                self.current=False;self.template=None
        await self.push_frame(frame,direction)
