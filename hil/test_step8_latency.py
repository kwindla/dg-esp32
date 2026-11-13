"""Actual warm latency requires the same established peer connection."""
from step8_latency import verify_session_continuity

def captured(temperature,before,after):
    return dict(passed=True,temperature=temperature,pc_id=before,
        before_perf={'pc_id':before},after_perf={'pc_id':after},
        latency_ms={'audible_reply':750})

def test_reconnected_turn_cannot_count_as_warm():
    sample=captured('warm','replacement','replacement')
    assert not verify_session_continuity(sample,'preceding')
    assert not sample['passed'] and sample['temperature']=='unverified-warm'
    assert sample['turn_assertions_passed'] and sample['latency_ms']['audible_reply']==750

def test_warm_turn_retains_preceding_session_through_reply():
    sample=captured('warm','established','established')
    assert verify_session_continuity(sample,'established')
    assert sample['passed'] and sample['temperature']=='warm'

def test_cold_turn_allows_new_session_but_not_midturn_replacement():
    sample=captured('cold','new','new')
    assert verify_session_continuity(sample,'preceding')
    replaced=captured('cold','new','replacement')
    assert not verify_session_continuity(replaced)
    assert replaced['temperature']=='unverified-cold'
