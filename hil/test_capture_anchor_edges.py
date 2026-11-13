import pytest
from echoear_aec_timing import require_bracketed_capture, mapped_sample_coverage
from echoear_aec_metrics import missing_playback

def anchors():
    return [dict(coordinate=i*1000,lower_s=i,upper_s=i+.01) for i in range(5)]

def test_any_block_before_first_anchor_fails_acquisition():
    rows=[[0,320,-100,100,0],[320,640,100,1000,0]]
    with pytest.raises(ValueError,match='precedes first'):require_bracketed_capture(rows,anchors())
    rows[0][2]=0
    assert require_bracketed_capture(rows,anchors())['passed']

def test_coverage_narrows_to_mapped_samples_without_inventing_silent_edges():
    rows=[[0,320,-100,100,0],[320,640,1000,1500,0],[640,960,2000,2500,0],[960,1280,3000,3500,0],[1280,1600,4000,4100,0]]
    coverage=mapped_sample_coverage(rows,anchors())
    assert coverage==[2.01,3]
    assert missing_playback([0,0],[],coverage)['outcome']=='timing-unscorable'
    rows[2][4]=1
    assert mapped_sample_coverage(rows,anchors()) is None

def test_runner_orders_delay_between_anchor_and_record(monkeypatch):
    import echoear_aec_bench as b
    calls=[]
    monkeypatch.setattr(b,'clock_anchor',lambda board:calls.append('anchor'))
    monkeypatch.setattr(b.time,'sleep',lambda seconds:calls.append(seconds))
    command=lambda board,text:calls.append(text)
    b.start_anchored_capture(None,command);b.stop_anchored_capture(None,command)
    assert calls==['anchor',.2,'aec-record 1','aec-record 0','anchor']
