import copy
import pytest
from echoear_aec_f1b import zero_witness
from echoear_aec_transport_environment import classify
from test_echoear_aec_transport_environment import inputs
from test_aec_link_soak import snapshots,records


def counters(zeros=1,late=7,missing=7):
    a,b=snapshots()
    a.pop('peer_profile');b.pop('peer_profile')
    return a,b,dict(post_plc_zeros=zeros,late=late,missing_slots=missing)


@pytest.mark.parametrize('zeros,late,missing,passed',[(0,6,7,True),(1,7,7,True),(1,6,7,False),(1,8,7,False),(1,0,0,False),(None,7,7,False),(-1,7,7,False)])
def test_counter_fallback(zeros,late,missing,passed):
    assert zero_witness(*counters(zeros,late,missing))['passed']==passed


def test_trace_requires_every_slot_even_with_equal_counters():
    a,b=snapshots();p=dict(post_plc_zeros=2,late=7,missing_slots=7)
    assert zero_witness(a,b,p,arrivals=records())['wholly_late']
    for fault in ('one-ingress','all-ingress','render','overwritten','disabled','generation'):
        x,y=copy.deepcopy(a),copy.deepcopy(b);r=records()
        if fault=='one-ingress':r=[v for v in r if not(v['kind']==13 and v['timestamp']==2*960)]
        if fault=='all-ingress':r=[v for v in r if v['kind']!=13]
        if fault=='render':r=[v for v in r if not(v['kind']==14 and v['value']==2)]
        if fault=='overwritten':y['peer_profile']['trace']['overwritten']=1
        if fault=='disabled':y['peer_profile']['trace']['enabled']=False
        if fault=='generation':y['generation']=2
        assert not zero_witness(x,y,p,arrivals=r)['passed'],fault


def test_no_speech_requires_observed_recorder_epoch_not_absent_tts_events():
    args=counters(late=0);recorded=[dict(pc_id='pc',kind='session-start',transport_write_witness=True,monotonic_ns=0),dict(pc_id='pc',kind='audio',stream='uplink',monotonic_ns=200000000)]
    assert zero_witness(*args,recorded,window=[0,.2])['no_speech_packet_due']
    for r in ([],recorded[1:],recorded[:1],recorded+[dict(pc_id='pc',kind='tts-transport-write',non_silent=False)],recorded+[dict(pc_id='pc',kind='audio',stream='tts')],recorded+[dict(pc_id='pc',kind='gap')],recorded+[dict(pc_id='pc',kind='session-end')]):
        assert not zero_witness(*args,r,window=[0,.2])['passed']


def test_opt_in_zero_classification_never_changes_cell_transport_gate():
    v=inputs();v['after']['playout']['post_plc_zeros']=2
    assert not classify(**v)['passed'] # v6 unchanged
    r=classify(**v,device_alive_rule='f1b-v1')
    assert r['passed'] and r['playout']['post_plc_zeros']==2
    assert not v['checks']['transport']['passed'] and v['checks']['transport']['max_burst_upper']>=4
    for fault in ('starvation','resource','liveness','unresolved'):
        x=copy.deepcopy(v)
        if fault=='starvation':x['after']['playout']['starvation']=1
        if fault=='resource':x['checks']['resources']['all_passed']=False
        if fault=='liveness':x['after']['liveness']['timeouts']=1
        if fault=='unresolved':x['after']['late']=19
        assert not classify(**x,device_alive_rule='f1b-v1')['passed']


def test_classification_allows_witnessed_idle_but_not_unresolved_speech():
    v=inputs();v['after']['playout']['post_plc_zeros']=2;v['after']['late']=19
    recorder=[dict(pc_id='pc',kind='session-start',transport_write_witness=True,monotonic_ns=0),
              dict(pc_id='pc',kind='audio',stream='uplink',monotonic_ns=9950000000)]
    assert classify(**v,device_alive_rule='f1b-v1',recorded=recorder)['passed']
    assert not classify(**v,device_alive_rule='f1b-v1',recorded=recorder[:1])['passed']
    recorder.append(dict(pc_id='pc',kind='tts-transport-write',non_silent=True,monotonic_ns=100000000))
    assert not classify(**v,device_alive_rule='f1b-v1',recorded=recorder)['passed']
