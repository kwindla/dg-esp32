from echoear_aec_pacing_draft import server_pacing


def packets():
    return [dict(pc_id='pc', ssrc=9, seq=(65534+i)%65536,
                 timestamp=(2**32-1920+i*960)%2**32, send_ms=i*20)
            for i in range(11)]


def check(p, **kw):
    return server_pacing(p, 'pc', kw.pop('lo', 0), kw.pop('hi', .2),
                         complete=kw.pop('complete', True),
                         sctp=kw.pop('sctp', [dict(pc_id='pc', monotonic_ns=100000000)]), **kw)


def test_wrap_and_unsorted_retransmissions():
    p=packets(); p += [dict(p[2],send_ms=150),dict(p[2],send_ms=170)]
    r=check(list(reversed(p)))
    assert r['passed'] and r['repeated_observations']==2 and r['original_count']==11


def test_missing_original_cannot_be_repaired_by_resend():
    p=packets();p[3]['send_ms']=160
    assert not check(p)['passed']


def test_duplicates_with_different_timestamp_are_not_resends():
    p=packets();p.append(dict(p[2],timestamp=12345,send_ms=150))
    assert not check(p)['passed']


def test_first_send_outside_window_is_deduped_before_selection():
    p=packets();p.append(dict(p[0],send_ms=150))
    r=check(p,lo=.08)
    assert r['passed'] and r['repeated_observations']==1 and r['original_count']==7


def test_truncation_edges_source_change_and_bad_sctp_fail_closed():
    assert not check(packets(),complete=False)['passed']
    assert not check(packets(),lo=-.1)['passed']
    assert not check(packets(),hi=.3)['passed']
    assert not check(packets(),sctp=[])['passed']
    assert not check(packets(),sctp=[dict(pc_id='pc',monotonic_ns=100000000,event='t3-expired')])['passed']
    p=packets();p[5]['ssrc']=10
    assert not check(p)['passed']


def test_real_pacing_gap_and_invalid_records():
    p=packets()
    for r in p[5:]:r['send_ms']+=80
    assert not check(p,hi=.28)['passed']
    p=packets();p[0]['seq']=None
    assert not check(p)['passed']
