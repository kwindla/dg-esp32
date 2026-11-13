from pathlib import Path
from unittest.mock import Mock
import pytest
from pipecat.frames.frames import TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame
from offline_fixture_reply import OfflineFixtureReply, enabled


def test_opt_in_only_and_never_voice_policy():
    assert not enabled('offline-test', '0')
    assert not enabled('voice', '0')
    assert enabled('offline-test', '1')
    for mode,value in [('voice','1'), ('live-test','1'), ('offline-test','yes')]:
        with pytest.raises(ValueError):
            enabled(mode,value)


@pytest.mark.asyncio
async def test_fixture_envelope_once_per_turn_preserves_exact_pcm():
    log, output = Mock(), []
    reply = OfflineFixtureReply(Path(__file__).resolve().parents[1]/'fixtures/reply_pineapple.wav', log)
    async def emit(frames):
        output.append(frames)
    await reply.stopped(1, emit)
    await reply.stopped(1, emit)
    await reply.stopped(2, emit)
    assert len(output) == 2
    for frames in output:
        assert isinstance(frames[0], TTSStartedFrame)
        assert isinstance(frames[-1], TTSStoppedFrame)
        assert all(isinstance(f, TTSAudioRawFrame) and f.sample_rate==16000 and f.num_channels==1
                   for f in frames[1:-1])
        assert b''.join(f.audio for f in frames[1:-1]) == reply.pcm
    assert [call.kwargs['turn'] for call in log.write.call_args_list] == [1, 2]
