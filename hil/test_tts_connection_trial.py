import pytest
from echoear_tts_connection_analysis import parse_metrics,summarize
from echoear_tts_connection_trial import validate


def test_metrics_pairing_and_mismatched_counts():
    log='DeepgramFluxTTSService#0 TTFB: 0.2s\nDeepgramFluxTTSService#0 TTFA: 1.4s (1.2s leading silence)'
    assert parse_metrics(log)==[dict(ttfb_s=.2,ttfa_s=1.4,lead_s=1.2)]
    assert parse_metrics(log.replace('DeepgramFluxTTSService','ConnectionProbeTTS'))==parse_metrics(log)
    with pytest.raises(ValueError):parse_metrics(log.splitlines()[0])


def test_denominator_long_state_and_service_vs_connection_cost():
    plan=dict(long_state_s=.8,ttfb_tolerance_s=dict(median=.05,p95=.1))
    sessions=[dict(arm=arm,complete=True,metrics=[dict(ttfb_s=.2,ttfa_s=.5,lead_s=.3,reconnect_s=.2 if arm=='B' else 0,ttfb_plus_reconnect_s=.4 if arm=='B' else .2)]*4) for arm in 'AB'*8]
    x=summarize(sessions,plan,True)
    assert x['passed'] and not x['reconnect_inclusive_ttfb_unchanged']
    assert not summarize(sessions[:-1],plan,True)['passed']
    sessions[1]['metrics'][0]['lead_s']=.8
    assert not summarize(sessions,plan,True)['passed']


def test_teardown_error_is_retained_separately_from_completed_metrics():
    from echoear_tts_connection_analysis import classify_failures
    events=[dict(t=1,event='script-request'),dict(t=3,event='case-sealed'),
            dict(t=15,event='frame',frame='ErrorFrame',case_id=None)]
    failures,during,after=classify_failures(events)
    assert failures==after==events[-1:] and during==[]
    events[-1]['t']=2
    assert classify_failures(events)[1]==events[-1:]
    assert classify_failures(events[:1]+events[-1:])[1]==events[-1:]


def test_reconnect_must_finish_before_generation_window():
    from echoear_tts_connection_analysis import reconnect_windows
    events=[dict(t=0,event='script-request',case_id='g1'),dict(t=.1,event='tts-reconnect-start'),
            dict(t=.2,event='tts-reconnect-ready'),dict(t=.3,event='script-queued',case_id='g1'),
            dict(t=2,event='frame',frame='TTSStoppedFrame',case_id='g1')]
    assert reconnect_windows(events)['passed']
    events[2]['t']=.4
    assert not reconnect_windows(events)['passed']
    assert not reconnect_windows([e for e in events if e['event']!='tts-reconnect-ready'])['passed']


def test_bad_session_metrics_do_not_abort_other_sessions(tmp_path):
    import json
    from echoear_tts_connection_analysis import analyze
    plan=dict(sessions=[dict(id='bad',arm='A',texts=['x']),dict(id='good',arm='B',texts=['x'])],
              long_state_s=.8,ttfb_tolerance_s=dict(median=.05,p95=.1))
    (tmp_path/'declaration.json').write_text(json.dumps(plan))
    (tmp_path/'result.json').write_text(json.dumps(dict(completed=False,sessions=[])))
    for name in ('bad','good'):(tmp_path/name/'bot').mkdir(parents=True)
    (tmp_path/'bad/bot/server-1.log').write_text('DeepgramFluxTTSService#0 TTFB: 0.2s')
    (tmp_path/'good/bot/server-1.log').write_text('ConnectionProbeTTS#0 TTFB: 0.2s\nConnectionProbeTTS#0 TTFA: 0.4s (0.2s leading silence)')
    result=analyze(tmp_path)
    assert result['sessions'][0]['metric_error'] and not result['sessions'][0]['complete']
    assert len(result['sessions'][1]['metrics'])==1
    assert not result['summary']['passed']
