from copy import deepcopy
from step8_profile_policy import compare,distribution
import json
import pytest
import step8_profile_policy as policy
import step8_campaign as campaign

def measured():
    metrics={'counter:'+key:distribution([0]) for key in
        ('rebuffers','drift_drops','plc_frames','i2s_underflows','decode_errors','queue_dropped')}
    metrics.update({'cpu:'+name+'/core1':distribution([value]) for name,value in
        (('peer-loop',30),('audio-tx',40),('audio-playback',15),('peer-rx',0))})
    metrics['uplink:gap_max_ms']=distribution([100])
    return dict(captures=[dict(passed=True)],phases={name:deepcopy(metrics) for name in ('idle','duplex')})

def test_unobserved_task_is_not_a_zero_cpu_improvement():
    a=measured();b=deepcopy(a)
    assert compare(a,b,1)['passed']  # A measured zero is valid.
    del b['phases']['idle']['cpu:peer-rx/core1']
    assert not compare(a,b,1)['passed']

def test_peer_improvement_does_not_hide_encoder_regression():
    a=measured();b=deepcopy(a)
    b['phases']['duplex']['cpu:peer-loop/core1']=distribution([20])
    b['phases']['duplex']['cpu:audio-tx/core1']=distribution([45])
    assert not compare(a,b,1)['passed']

def test_partial_task_population_cannot_win_with_a_lower_cpu_median():
    a=measured();a['captures']*=10
    for phase in a['phases'].values():
        for value in phase.values():value['n']=10
    b=deepcopy(a)
    b['phases']['duplex']['cpu:peer-loop/core1']=distribution([20]*9)
    assert not compare(a,b,10)['passed']

def test_single_decode_error_remains_visible_beyond_the_median():
    a=measured();b=deepcopy(a)
    b['phases']['idle']['counter:decode_errors']=distribution([0,0,1])
    assert not compare(a,b,1)['passed']

@pytest.mark.parametrize('prior_control',[None,'C'])
def test_profile_repeat_cannot_replace_failed_original_population(tmp_path,monkeypatch,prior_control):
    for label in ('A','C','B'):
        folder=tmp_path/'profiles'/'boot1'/(label+'1');folder.mkdir(parents=True)
        (folder/'result.json').write_text(json.dumps(dict(passed=label!=prior_control,rebuffers=3 if label=='B' else 0)))
    if prior_control:
        folder=tmp_path/'profiles'/'boot1'/(prior_control+'2');folder.mkdir()
        (folder/'result.json').write_text(json.dumps(dict(passed=True,rebuffers=0)))

    def summaries(path):
        raw=json.loads(path.read_text())
        return [dict(name=phase,counters=dict(rebuffers=raw['rebuffers'],plc_frames=0,
            drift_drops=0,queue_dropped=0,decode_errors=0,i2s_underflows=0),
            network={'rtp.jsonl':dict(packets=100,loss=0,gap_max_ms=100)},
            tasks=[dict(name=name,core=1,cpu_percent=value) for name,value in
                (('peer-loop',30),('audio-tx',40),('audio-playback',15),('peer-rx',0))])
            for phase in ('idle','duplex')]

    repeats=[]
    def capture(name,label,image,directory,seconds,options,attempts):
        assert attempts==1
        repeats.append(label)
        folder=directory/(label+'1');folder.mkdir()
        (folder/'result.json').write_text(json.dumps(dict(passed=True,rebuffers=0)))

    monkeypatch.setattr(policy,'profile',summaries)
    monkeypatch.setattr(campaign,'capture_profile',capture)
    labels=('A','C','B')
    notes=policy.repeat_failed_profiles(tmp_path,'combined',dict.fromkeys(labels,tmp_path),
        dict.fromkeys(labels,()),labels)
    assert repeats==[label for label in labels if label!=prior_control]
    assert len(notes)==1 and not notes[0]['already_rerun']
    assert notes[0]['prior_capture_reruns']==([prior_control] if prior_control else [])
    original=policy.population(tmp_path,'B')
    assert original['phases']['idle']['counter:rebuffers']['p50']==3
    assert original['phases']['idle']['counter:rebuffers']['n']==1
    assert not policy.compare(policy.population(tmp_path,'A'),original,1)['passed']
