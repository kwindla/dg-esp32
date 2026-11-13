"""Keep Pipecat's non-silence track paced after a long idle gap.

RawAudioTrack's timestamps pause with auto_silence=False, but its wall-clock
origin does not. Without reanchoring, queued resumed speech is sent in a burst.
The RTP sample clock stays continuous; only the scheduling origin moves. A
bounded 160 ms quiet tail flushes receiver buffering before output stops.
"""
import asyncio
import time
from pipecat.transports.smallwebrtc import transport

_original=transport.RawAudioTrack

class PacedIdleAudioTrack(_original):
    idle_tail_ms=160

    def __init__(self,sample_rate,auto_silence=True):
        super().__init__(sample_rate,auto_silence)
        self._tail_remaining=0
        self._audio_available=asyncio.Event()

    def add_audio_bytes(self,audio_bytes):
        completion=super().add_audio_bytes(audio_bytes)
        if self._chunk_queue:self._audio_available.set()
        return completion

    async def recv(self):
        if not self._auto_silence:
            while not self._chunk_queue and not self._tail_remaining:
                self._audio_available.clear()
                # No await between checking the queue and clearing the event:
                # producers on this loop cannot pass through that window.
                await self._audio_available.wait()
            now=time.time()
            due=self._start+self._timestamp/self._sample_rate
            # Ordinary producer handoffs retain the sample-clock cadence.
            if now-due>.020:
                self._start=now-self._timestamp/self._sample_rate
            if self._chunk_queue:
                self._tail_remaining=self.idle_tail_ms//10
            else:
                # A bounded tail flushes receiver/decoder buffering. Immediate
                # RTP cessation left the independent greeting recording shorter
                # than its reference; eight Opus packets allow its footer out.
                self._tail_remaining-=1
                self._auto_silence=True
                try:return await super().recv()
                finally:self._auto_silence=False
        return await super().recv()

def install():
    transport.RawAudioTrack=PacedIdleAudioTrack
