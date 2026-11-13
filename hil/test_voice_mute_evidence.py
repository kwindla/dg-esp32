from voice_mute_evidence import mute_evidence


def test_jitter_tail_and_self_interruption_are_separate_from_pending_response():
    events=[dict(event='interruption',t=1),dict(event='bot-audio-start',t=2),
        dict(event='user-mute-start',t=2.001),dict(event='interruption',t=3),
        dict(event='user-turn-started',t=3.2),dict(event='bot-audio-end',t=4),
        dict(event='user-mute-stop',t=4.001),dict(event='user-turn-started',t=4.2)]
    evidence=mute_evidence(events,[(2.1,'closed'),(4.4,'open')])
    assert evidence['interruptions_during_bot_speech']==[events[3]]
    assert evidence['user_turns_during_bot_speech']==[events[4]]
    assert evidence['tail_user_turns']==[events[-1]]
    assert abs(evidence['board_tail_after_server_stop'][0]['tail_ms']-400)<1e-9
    assert len(evidence['mute_edges'])==2
