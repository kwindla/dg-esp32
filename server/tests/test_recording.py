import wave
import numpy as np
import pytest
from pipecat.frames.frames import InputAudioRawFrame
from bot import InputRecording

class Log:
    pc_id = 'recording-test'
    def write(self, event, **data):
        self.event, self.data = event, data

def test_recording_preserves_samples_and_finalizes_header(tmp_path, monkeypatch):
    monkeypatch.setenv('RECORD_INPUT', '1')
    monkeypatch.setenv('RECORD_INPUT_DIR', str(tmp_path))
    log = Log()
    recording = InputRecording(log)
    pcm = np.arange(640, dtype='<i2').tobytes()
    recording.write(InputAudioRawFrame(pcm[:640], 16000, 1))
    recording.write(InputAudioRawFrame(pcm[640:], 16000, 1))
    recording.close()
    recording.close()
    with wave.open(str(tmp_path / 'recording-test.wav')) as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2)
        assert stream.getnframes() == 640
        assert stream.readframes(640) == pcm
    assert log.data['samples'] == 640

def test_recording_disabled_and_wrong_format(tmp_path, monkeypatch):
    monkeypatch.delenv('RECORD_INPUT', raising=False)
    monkeypatch.setenv('RECORD_INPUT_DIR', str(tmp_path))
    recording = InputRecording(Log())
    recording.write(InputAudioRawFrame(b'\x00\x00', 16000, 1))
    recording.close()
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv('RECORD_INPUT', '1')
    recording = InputRecording(Log())
    try:
        with pytest.raises(ValueError):
            recording.write(InputAudioRawFrame(b'\x00\x00', 48000, 1))
    finally:
        recording.close()
