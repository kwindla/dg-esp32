from echoear_tts_ab_analysis import summarize


def rows():
    return [dict(arm=arm,cold=i==0,clean=True,false_turns=0,guard_on_real_speech=True,
                 bracketed_playback=True,hardware_resources_passed=True,raw_leading_s=1.4,
                 output_leading_s=.04 if arm=='B' else 1.4,
                 written_leading_s=0 if arm=='B' else 1.36,
                 request_to_non_silent_write_s=.4 if arm=='B' else 1.8,
                 server_ttfa_s=1.6,server_leading_s=1.4)
            for i,arm in enumerate('ABBA'*5)]


def test_selection_requires_measured_counts_timing_guard_and_zero_false_turns():
    r=rows();assert summarize(r,True)['select_trim']
    incomplete=summarize(r,False)
    assert not incomplete['select_trim'] and incomplete['selected'] is None and incomplete['retained_default']=='A'
    assert not summarize(r[:-1],True)['select_trim']
    for key,value in [('false_turns',1),('guard_on_real_speech',False),('bracketed_playback',False),
                      ('hardware_resources_passed',False),('server_ttfa_s',None),('clean',False)]:
        r=rows();r[2][key]=value
        assert not summarize(r,True)['select_trim'],key
    r=rows()
    for c in r:
        if c['arm']=='B':c['request_to_non_silent_write_s']=1.5
    assert not summarize(r,True)['select_trim']


def test_speech_guard_ignores_minus75_dbfs_lead():
    import numpy as np
    from echoear_tts_ab_analysis import speech_onset
    from echoear_aec_metrics import frame_power
    lead=np.full(320*20,32768*10**(-75/20))
    speech=np.full(320*10,32768*10**(-35/20))
    ref=np.concatenate([np.zeros(320*5),lead,speech])
    assert np.flatnonzero(frame_power(ref)>4)[0]*320 == 320*5
    assert speech_onset(ref)==320*25
    assert speech_onset(lead) is None


def test_streaming_evidence_requires_every_generation():
    from echoear_tts_ab_analysis import streaming_findings
    x=streaming_findings([(.6,.4),(.7,.5)],[.2,.2])
    assert x['silence_streams_at_real_time'] and not x['hold_entered_long_state']
    assert not streaming_findings([(.6,.4)],[])['silence_streams_at_real_time']
    assert streaming_findings([(1.4,1.2)],[.2])['hold_entered_long_state']
