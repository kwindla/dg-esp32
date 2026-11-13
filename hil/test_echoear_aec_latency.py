import numpy as np
from echoear_aec_latency import pcm_lead, counter_deltas


def test_stored_pcm_lead_is_not_wall_clock_latency():
    x = np.concatenate([np.full(32000, 12.), np.full(16000, 1000.)])
    r = pcm_lead(x)
    assert r['first_non_silent_pcm_s'] == 2
    assert r['stored_seconds'] == 3
    assert r['active_frames'] == 50
    assert -70 < r['prefix_dbfs'] < -68


def test_missing_and_reset_counters_are_not_zero_filled():
    a = dict(generation=1, pc_id='p', device_us=1, lost=3, received=100)
    b = dict(generation=1, pc_id='p', device_us=2, lost=3, received=200)
    d = counter_deltas(a,b)['delta']
    assert d['lost'] == 0 and d['received'] == 100 and d['late'] is None
    b['generation'] = 2
    assert all(v is None for v in counter_deltas(a,b)['delta'].values())
    b['generation'] = 1; b['lost'] = 0
    assert counter_deltas(a,b)['delta']['lost'] is None
