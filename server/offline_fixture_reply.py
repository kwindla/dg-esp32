"""Opt-in USB-absence stimulus: one archived TTS reply per completed VAD turn."""
import wave
from pipecat.frames.frames import TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame


def enabled(mode, value):
    if value not in ('0', '1') or value == '1' and mode != 'offline-test':
        raise ValueError('OFFLINE_USB_ABSENCE_TURNS requires offline-test and a 0/1 value')
    return value == '1'


class OfflineFixtureReply:
    def __init__(self, path, log):
        with wave.open(str(path)) as stream:
            if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
                raise ValueError('USB-absence reply must be PCM16 mono 16 kHz')
            self.pcm = stream.readframes(stream.getnframes())
        self.log, self.last_turn = log, None

    async def stopped(self, turn, emit):
        if turn == self.last_turn:
            return
        self.last_turn = turn
        self.log.write('usb-fixture-reply', turn=turn, samples=len(self.pcm)//2)
        # Use the supported cached-TTS frame envelope so output transport, not
        # enqueue time, emits the observed BotStarted/StoppedSpeaking frames.
        await emit([TTSStartedFrame(),
                    *(TTSAudioRawFrame(self.pcm[i:i+640], 16000, 1)
                      for i in range(0, len(self.pcm), 640)), TTSStoppedFrame()])
