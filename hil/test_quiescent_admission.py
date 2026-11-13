import pytest
from echoear_aec_resources import quiescent_admission_snapshot
from echoear_aec_checks import admission_counters


def sample(received, dispatched, generation=1):
    return dict(pc_id='peer', generation=generation,
                data_admission=dict(received=received, dispatched=dispatched),
                drop_reasons=dict(admission_budget=0))


class Clock:
    now=0.
    def clock(self):return self.now
    def sleep(self, seconds):self.now+=seconds


def test_live_ping_boundary_drains_and_keeps_failed_observation():
    c=Clock();observations=[sample(10,9),sample(10,10)]
    endpoint,retained=quiescent_admission_snapshot(lambda i:observations[i],clock=c.clock,sleep=c.sleep)
    assert retained==observations and endpoint==observations[-1]
    assert not admission_counters(sample(0,0),retained[0])['passed']
    assert admission_counters(sample(0,0),endpoint)['passed']
    assert c.now==.02


def test_persistent_backlog_is_bounded_not_waived():
    c=Clock();retained=[]
    def read(i):
        retained.append(sample(10,9))
        return retained[-1]
    with pytest.raises(TimeoutError):
        quiescent_admission_snapshot(read,clock=c.clock,sleep=c.sleep)
    assert .25<=c.now<.27 and len(retained)>1


def test_slow_initial_perf_transfer_does_not_consume_drain_window():
    c=Clock();observations=[sample(10,9),sample(10,10)]
    def read(i):
        c.sleep(.35)  # Serial transfer is separately bounded by the reader.
        return observations[i]
    endpoint,retained=quiescent_admission_snapshot(read,clock=c.clock,sleep=c.sleep)
    assert endpoint==observations[1] and retained==observations
    assert c.now==pytest.approx(.72)


@pytest.mark.parametrize('bad',[sample(10,10,2),sample(8,9),sample(None,9)])
def test_invalid_or_changed_session_fails_closed(bad):
    c=Clock();observations=[sample(10,9),bad]
    with pytest.raises(ValueError):
        quiescent_admission_snapshot(lambda i:observations[i],clock=c.clock,sleep=c.sleep)


def test_drained_endpoint_cannot_hide_intermediate_resource_failure():
    from echoear_aec_resources import check_reply_resources
    from test_echoear_aec_resources import snapshot
    before,middle,after=[snapshot() for _ in range(3)]
    for i,p in enumerate([before,middle,after]):
        p['device_us']=i*10000000
        p['liveness']=dict(pings=10+i,pongs=10+i,timeouts=0)
    middle['pcm_queue_drops']=1
    middle['data_admission']['received']=3
    after['data_admission']=dict(received=3,dispatched=3)
    result=check_reply_resources(before,after,intermediate=[middle])
    assert result['checks']['data_admission_counters_only']['passed']
    assert not result['checks']['pcm_queue_drops']['passed']
    assert not result['all_passed']
