import copy
import json
from pathlib import Path
import pytest
from echoear_aec_campaign_analysis import transport


def bounds(missing=4,buckets=None):
    a=dict(generation=1,pc_id='p',device_us=100,received=100,lost=10,playout_dropped=10,
        playout=dict(missing_run_ended=[2,0,0,1,0,1]))
    b=copy.deepcopy(a);b.update(device_us=200,received=4100,lost=10+missing,playout_dropped=10+missing)
    b['playout']['missing_run_ended']=[x+y for x,y in zip(a['playout']['missing_run_ended'],buckets or [0]*6)]
    return a,b


@pytest.mark.parametrize('missing,buckets,burst,passed',[
    (4,[4,0,0,0,0,0],1,True), (4,[0,0,0,1,0,0],4,False),
    (4,[1,0,1,0,0,0],3,True), (4,[0]*6,4,False),
    (6,[0,0,0,0,0,1],6,False), (9,[0,0,0,0,0,1],6,False),
    (3,[0]*6,3,True), (0,[0]*6,0,True),
    # A run opened before the first boundary is conservatively counted whole.
    (1,[0,0,0,1,0,0],4,False),
])
def test_histogram_window(missing,buckets,burst,passed):
    result=transport(*bounds(missing,buckets))
    assert result['max_burst_upper']==burst and result['passed']==passed


def test_absent_histogram_and_lifetime_maximum_never_waive_window():
    a,b=bounds();a.pop('playout');b.pop('playout')
    assert not transport(a,b)['passed']
    a['max_consecutive_playout_dropped']=b['max_consecutive_playout_dropped']=0
    assert not transport(a,b)['passed'] # lifetime max zero cannot waive 4 drops
    b['lost']=a['lost'];b['playout_dropped']=a['playout_dropped']
    a['max_consecutive_playout_dropped']=b['max_consecutive_playout_dropped']=11
    assert transport(a,b)['passed'] # historical max cannot poison a clean window


@pytest.mark.parametrize('bad',['generation','peer','swapped','decrease','short','bool','missing','null'])
def test_bad_histogram_or_epoch_fails_closed(bad):
    a,b=bounds(4,[4,0,0,0,0,0])
    if bad=='generation':b['generation']=2
    if bad=='peer':b['pc_id']='q'
    if bad=='swapped':a,b=b,a
    if bad=='decrease':b['playout']['missing_run_ended'][0]=0
    if bad=='short':b['playout']['missing_run_ended'].pop()
    if bad=='bool':b['playout']['missing_run_ended'][0]=True
    if bad=='missing':a.pop('playout')
    if bad=='null':b['playout']['missing_run_ended']=None
    assert not transport(a,b)['passed']


def test_histogram_does_not_waive_loss_ratio():
    a,b=bounds(40,[40,0,0,0,0,0])
    assert transport(a,b)['max_burst_upper']==1 and not transport(a,b)['passed']


def test_campaign05_v81_retains_device_fault():
    root=Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9/campaign-05/qualification-01')
    if not root.exists():pytest.skip('retained evidence unavailable')
    from echoear_aec_transport_environment import classify
    from echoear_aec_campaign import exception_class
    w=json.loads((root/'qualification-01-echo-pair-2-a1/transport-environmental-witness.json').read_text());r=w['raw']
    assert transport(r['before'],r['after'])==r['checks']['transport']
    replay=classify(r['before'],r['after'],r['checks'],r['pre_rf'],r['events'],r['downlink'],r['sctp'],r['uplink'],w['window_s'],r['cells'],r['attempt_timing'],device_alive_rule='f1b-v1',recorded=w['post_plc_zero_witness']['raw_recorded'])
    assert replay['conditions']==w['conditions'] and not replay['passed']
    assert exception_class(RuntimeError('Resource/admission/RF/delivery/transport failure'))=='device-fault'
