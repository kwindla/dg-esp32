from echoear_tts_proactive_analysis import connection_windows


def test_acknowledged_interruption_ends_window_and_inflight_overlap_fails():
    events=[dict(event='tts-generation-metric-start',t=1,context_id='g'),
            dict(event='tts-interruption-stopped',t=2,context_id='g'),
            dict(event='tts-reconnect-start',t=2.01),dict(event='tts-reconnect-ready',t=2.2)]
    assert connection_windows(events)['passed']
    events[2]['t']=1.9
    assert not connection_windows(events)['passed']
    assert not connection_windows(events[:1]+events[2:])['passed']
