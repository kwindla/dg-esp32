from sctp_recovery import check_recovery


def row(event, ns, **kw):
    return dict(pc_id='p', event=event, monotonic_ns=ns, **kw)


def test_each_t3_needs_later_quiescent_ack_in_same_peer():
    timer = row('t3-expired', 10)
    ack = row('receive-sack-complete', 20, flight_size=0, sent_queue=0, outbound_queue=0)
    assert check_recovery([timer, ack])['passed']
    assert not check_recovery([dict(ack, monotonic_ns=5), timer])['passed']
    for key, value in [('pc_id', 'other'), ('flight_size', 1186), ('outbound_queue', 1), ('sent_queue', 1), ('event', 'send-chunk')]:
        assert not check_recovery([timer, dict(ack, **{key:value})])['passed']
    assert not check_recovery([timer, ack, row('t3-expired', 30)])['passed']
    result = check_recovery([timer, row('t3-expired', 15), ack])
    assert result['passed'] and result['t3_events'] == 2
    assert check_recovery([])['naturally_exercised'] is False


def test_intentional_teardown_is_separate_and_cannot_cure_active_failure():
    from sctp_recovery import check_active_recovery
    timer=lambda t:dict(event='t3-expired',pc_id='a',monotonic_ns=t)
    ack=lambda t:dict(event='receive-sack-complete',pc_id='a',monotonic_ns=t,flight_size=0,sent_queue=0,outbound_queue=0)
    check=check_active_recovery([timer(1),ack(2),timer(11)],10)
    assert check['passed'] and check['t3_events']==1 and check['teardown_t3_events']==1
    check=check_active_recovery([timer(1),ack(12)],10)
    assert not check['passed'] and check['teardown_t3_events']==0
