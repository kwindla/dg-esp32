import numpy as np
from echoear_aec_tts import non_silent, tts_anchor


def test_fixed_pcm_rule_rejects_digital_near_silence():
    assert not non_silent(np.full(1280, 12))
    assert not non_silent(np.full(320, 32))
    assert non_silent(np.full(320, 33))
    assert not non_silent(np.full(319, 1000))


def test_anchor_skips_silent_chunks_and_does_not_steal_next_reply():
    events = [dict(kind='audio', stream='tts', frame_id=i, monotonic_ns=t,
                   non_silent=active) for i, t, active in
              [(1, 999900000, False), (2, 2000000000, True)]]
    assert tts_anchor(events, 10**9)['server_interval'] == [2, 2]
    assert tts_anchor(events, 10**9, 1500000000)['server_interval'] is None
    events.append(dict(kind='tts-transport-write', frame_id=3,
                       monotonic_ns=2100000000, non_silent=True))
    assert tts_anchor(events, 10**9)['server_interval'] == [2.1, 2.1]


def test_new_recorder_cannot_fall_back_to_input_when_transport_drops_audio():
    events = [dict(kind='session-start', transport_write_witness=True),
              dict(kind='audio', stream='tts', frame_id=1, monotonic_ns=10**9, non_silent=True)]
    assert tts_anchor(events, 10**9)['server_interval'] is None
