from step8e_analysis import recovery_bounds


def observations():
    rows=[dict(kind=12,reserved=g,device_us=t,ssrc=6,seq=0,timestamp=0)
          for g,t in ((1,500000),(3,1500000))]
    rows.append(dict(kind=1,reserved=3,device_us=1500000,ssrc=9,seq=0,timestamp=0))
    up=[dict(pc_id=p,observed_ms=t,ssrc=6,seq=0,timestamp=0)
        for p,t in (('old',50520),('new',51520))]
    down=[dict(pc_id='new',send_ms=51490,ssrc=9,seq=0,timestamp=0)]
    return rows,up,down


def test_restarted_native_rtp_identity_matches_its_own_peer_generation():
    result=recovery_bounds(*observations(),51.4,51.405,{'1':'old','3':'new'})
    assert result['clock_bounds_valid']
    assert result['uplink_matches']==2
    assert 49.98<result['clock_offset_lower_s']<50.02
    assert 50<result['clock_offset_upper_s']<50.03
    assert result['uplink_recovery_ms_bounds']==[115,120]


def test_unmapped_restarted_identity_is_excluded_instead_of_last_record_winning():
    result=recovery_bounds(*observations(),51.4,51.405)
    assert not result['clock_bounds_valid']
    assert result['uplink_matches']==0
    assert result['ambiguous_uplink_identities_excluded']==1
    assert 'downlink_recovery_ms_bounds' not in result


def test_two_distinct_send_times_for_one_mapped_identity_are_ambiguous():
    rows,up,down=observations()
    rows.append(dict(rows[1],device_us=1600000))
    result=recovery_bounds(rows,up[1:],down,51.4,51.405,{1:'old',3:'new'})
    assert not result['clock_bounds_valid'] and result['uplink_matches']==0
    assert result['ambiguous_uplink_identities_excluded']==1
