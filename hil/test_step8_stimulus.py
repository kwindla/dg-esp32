from copy import deepcopy
from step8_summary import duplex_coverage

def continuous():
    return dict(name='duplex',duration_s=20,traffic_frames=dict(sent=1000,received=1000),
        network={'rtp.jsonl':dict(packets=1000),'downlink.jsonl':dict(packets=1000)})

def test_idle_suppression_and_continuous_duplex_are_distinct():
    idle=deepcopy(continuous());idle.update(name='idle',traffic_frames=dict(sent=1000,received=0))
    idle['network']['downlink.jsonl']['packets']=0
    assert duplex_coverage([idle,continuous()])['passed']

def test_interrupted_fixture_cannot_qualify_a_duplex_cpu_window():
    sample=continuous();sample['traffic_frames']['received']=76
    sample['network']['downlink.jsonl']['packets']=54
    result=duplex_coverage([sample])
    assert not result['passed'] and result['minimum_frames']==966

def test_idle_window_alone_cannot_qualify_duplex():
    sample=continuous();sample['name']='idle'
    assert not duplex_coverage([sample])['passed']
