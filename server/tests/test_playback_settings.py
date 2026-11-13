from pathlib import Path
import pytest
from settings import vad_min_volume, fixture_paths, skip_greeting, FIXTURES
from bot import speech_vad_params, speech_stt_settings

def test_rtvi_burst_is_explicit_and_offline_only(monkeypatch):
    from settings import offline_rtvi_burst_enabled
    monkeypatch.delenv('OFFLINE_RTVI_BURST_SWEEP',raising=False)
    assert not offline_rtvi_burst_enabled('voice')
    for mode in ('voice','live-test','offline-test'):
        monkeypatch.setenv('OFFLINE_RTVI_BURST_SWEEP','0')
        assert not offline_rtvi_burst_enabled(mode)
    monkeypatch.setenv('OFFLINE_RTVI_BURST_SWEEP','1')
    assert offline_rtvi_burst_enabled('offline-test')
    with pytest.raises(ValueError):offline_rtvi_burst_enabled('voice')
    monkeypatch.setenv('OFFLINE_RTVI_BURST_SWEEP','true')
    with pytest.raises(ValueError):offline_rtvi_burst_enabled('offline-test')


@pytest.mark.parametrize('mode',['voice','offline-test','live-test'])
def test_production_vad_matches_pinned_pipecat_defaults(monkeypatch,mode):
    from pipecat.audio.vad.vad_analyzer import VADParams
    monkeypatch.delenv('BOT_VOICE_POLICY',raising=False)
    monkeypatch.delenv('BOT_VAD_MIN_VOLUME',raising=False)
    assert speech_vad_params(mode).model_dump()==VADParams().model_dump()

@pytest.mark.parametrize('value', ['0', '0.4', '1'])
def test_volume_reaches_vad_params(monkeypatch, value):
    monkeypatch.setenv('BOT_VAD_MIN_VOLUME', value)
    assert vad_min_volume() == float(value)
    params = speech_vad_params('live-test')
    assert params.min_volume == float(value)
    assert params.start_secs == .2 and params.stop_secs == .2
    assert params.confidence == .7

@pytest.mark.parametrize('value', ['-0.1', '1.1', 'nan', 'inf', '-inf', 'bad', ''])
def test_invalid_volume_rejected(monkeypatch, value):
    monkeypatch.setenv('BOT_VAD_MIN_VOLUME', value)
    with pytest.raises(ValueError): speech_vad_params('offline-test')

def test_defaults_and_voice_policy(monkeypatch):
    monkeypatch.delenv('BOT_VAD_MIN_VOLUME', raising=False)
    assert speech_vad_params('offline-test').min_volume == .6
    voice = speech_vad_params('voice')
    assert voice.start_secs == .2 and voice.stop_secs == .2 and voice.min_volume == .6
    assert speech_stt_settings('live-test').endpointing is False
    from pipecat.services.settings import is_given
    assert not is_given(speech_stt_settings('voice').endpointing)
    for policy in ('timeout', '8d'):
        monkeypatch.setenv('BOT_VOICE_POLICY', policy)
        params = speech_vad_params('voice')
        assert params.stop_secs == (.6 if policy == '8d' else .2)
        assert params.start_secs == (.1 if policy == '8d' else .2)
        assert params.min_volume == .6
        assert speech_stt_settings('voice').endpointing == 500
        assert speech_stt_settings('live-test').endpointing is False

@pytest.mark.parametrize('mode', ['offline-test', 'live-test', 'voice'])
def test_fixture_allowlist_and_bank_is_offline_only(monkeypatch, mode):
    monkeypatch.setenv('OFFLINE_BENCH_FIXTURE_DIR', '/bench/immutable')
    paths = fixture_paths(mode)
    expected = {'greeting.wav', 'reply_pineapple.wav', 'prompt_france.wav'}
    if mode == 'offline-test':
        expected.update({'tts_1.wav', 'tts_2.wav'})
        assert paths['tts_1.wav'] == Path('/bench/immutable/tts_1.wav')
        assert paths['tts_2.wav'] == Path('/bench/immutable/tts_2.wav')
        assert paths['prompt_france.wav'] == FIXTURES/'prompt_france.wav'
    assert set(paths) == expected
    assert paths['greeting.wav'] == FIXTURES/'greeting.wav'
    assert '../secret' not in paths and '/tmp/arbitrary.wav' not in paths

@pytest.mark.parametrize('flag', ['0', '1', 'true', ''])
def test_skip_greeting_only_explicit_offline_handoff(monkeypatch, flag):
    monkeypatch.setenv('OFFLINE_SKIP_GREETING', flag)
    assert skip_greeting('offline-test') == (flag == '1')
    assert not skip_greeting('live-test') and not skip_greeting('voice')

@pytest.mark.parametrize('value', ['nan','inf','0','-1','6'])
def test_invalid_smartturn_silence_bound_rejected(monkeypatch,value):
    from settings import smartturn_stop_secs
    monkeypatch.setenv('BOT_SMARTTURN_STOP_SECS',value)
    with pytest.raises(ValueError):smartturn_stop_secs()

def test_smartturn_bound_is_voice_only(monkeypatch):
    from voice_audit import voice_strategies,half_duplex_strategies
    monkeypatch.setenv('BOT_SMARTTURN_STOP_SECS','1')
    assert voice_strategies('smartturn').stop[0]._turn_analyzer.params.stop_secs==1
    assert half_duplex_strategies().stop[0]._turn_analyzer.params.stop_secs==3
