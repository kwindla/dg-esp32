from echoear_transport_analysis import gaps

def test_host_integer_ms_thresholds_and_phase_boundaries():
    rows=[dict(t=1000,seq=65535),dict(t=1060,seq=0),dict(t=1121,seq=1),dict(t=1272,seq=2)]
    result=gaps(rows,'t',.001,1,1.3)
    assert result['count']==3
    assert result['over60']==2
    assert result['over150']==1
    assert result['tails'][0]['seq_step']==1
    # A partial boundary interval cannot be counted as a within-arm gap.
    assert gaps(rows,'t',.001,1.061,1.3)['count']==1

def test_device_microseconds_do_not_round_away_a_real_gap():
    result=gaps([dict(t=0),dict(t=60001)],'t',1e-6,0,1)
    assert result['over60']==1
    assert result['over150']==0
