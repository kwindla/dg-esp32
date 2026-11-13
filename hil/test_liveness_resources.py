from copy import deepcopy
from echoear_aec_resources import liveness_reply

def snapshots():
    return [dict(pc_id='p',generation=1,liveness=dict(pings=p,pongs=q,timeouts=0)) for p,q in [(10,10),(30,29)]]

def test_one_inflight_pong_allowed_but_stopped_or_missing_counters_fail():
    a,b=snapshots();assert liveness_reply(a,b)['passed']
    for change in ({'pongs':10},{'pings':10,'pongs':10},{'pongs':27},{'timeouts':1},{'pongs':31}):
        bad=deepcopy(b);bad['liveness'].update(change);assert not liveness_reply(a,bad)['passed']
    b.pop('liveness');assert not liveness_reply(a,b)['passed']

def test_reset_or_different_session_fails():
    a,b=snapshots();b['generation']=2;assert not liveness_reply(a,b)['passed']
    a,b=snapshots();b['liveness']['pings']=0;assert not liveness_reply(a,b)['passed']
