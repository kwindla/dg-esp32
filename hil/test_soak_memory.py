import numpy as np
import pytest
from soak_memory import heap_trends,growth_verdict,transient_memory,unknown_heap_windows,ready_times_from_device_log


def rows(minutes, values, generation=1):
    data=[dict(elapsed_s=float(m*60),ready_elapsed_s=0,perf=dict(generation=generation,
        heap={c:dict(free=float(v),minimum_free=float(v),window_minimum_free=float(v)) for c in ('internal','psram','dma')},
        heap_window=dict(schema=1,start_us=int((minutes[i-1] if i else 0)*60e6),end_us=int(m*60e6),samples=1)))
        for i,(m,v) in enumerate(zip(minutes,values))]
    return data



def test_ready_mapping_is_conservative_keeps_originals_and_rejects_reboots():
    observations=[dict(elapsed_s=20,perf=dict(generation=1,device_us=2_000_000)),
                  dict(elapsed_s=21,perf=dict(generation=1,device_us=3_000_000)),
                  dict(elapsed_s=30,perf=dict(generation=2,device_us=4_000_000))]
    mapped=ready_times_from_device_log(observations,[1_000_000,3_500_000])
    assert [r['ready_elapsed_s'] for r in mapped]==[19.05,19.05,29.55]
    assert all('ready_elapsed_s' not in r for r in observations)
    with pytest.raises(ValueError,match='No prior Ready'):
        ready_times_from_device_log(observations,[5_000_000])
    with pytest.raises(ValueError,match='reset'):
        ready_times_from_device_log(observations+[dict(elapsed_s=31,perf=dict(generation=3,device_us=1))],[1_000_000])


def test_realistic_noise_and_five_minute_fit_are_unknown():
    t=np.arange(0,10.5,.5);rng=np.random.default_rng(17)
    r=heap_trends(rows(t,100000-90*t+rng.normal(0,300,len(t))))
    assert all(v['passed'] is None for v in r['1'].values())
    assert growth_verdict(r) is None and len(unknown_heap_windows(r))==3
    assert r['1']['psram']['span_minutes']==5


def test_settling_then_plateau_does_not_become_a_leak():
    t=np.arange(0,21,.5);values=100000-400*np.minimum(t,5)
    r=heap_trends(rows(t,values))['1']['psram']
    assert r['passed'] and r['free_slope_bytes_per_minute']==pytest.approx(0)
    assert r['settle_samples_excluded']==10 and r['span_minutes']==15.5


def test_noisy_marginal_slope_needs_significance_but_clear_leak_fails():
    t=np.arange(0,21,.5);rng=np.random.default_rng(8)
    noise=rng.normal(0,300,len(t));selected=t>=5
    x=t[selected]-t[selected].mean()
    noise[selected]-=np.dot(x,noise[selected])/np.dot(x,x)*x
    for rate,expected in [(70,True),(150,False)]:
        r=heap_trends(rows(t,100000-rate*t+noise))['1']['psram']
        assert r['passed'] is expected
        assert r['free_slope_bytes_per_minute']==pytest.approx(-rate)
        assert r['residual_sd_bytes']>200 and r['slope_se_bytes_per_minute']>0
        assert (r['growth_lower_two_se_bytes_per_minute']<=64) is expected


def test_short_and_single_sample_generations_are_listed_not_failures():
    data=rows([0,1,2],[100000,99000,98000])+rows([100],[10000],generation=2)
    r=heap_trends(data)
    assert growth_verdict(r) is None and len(unknown_heap_windows(r))==6
    assert all(v['passed'] is None for g in r.values() for v in g.values())


def recovery_rows(final=85500):
    before = rows([0,5,10], [100000,98000,98000])
    after = rows([11,16,20], [86224,85896,final], generation=2)
    for r in after:r['ready_elapsed_s']=660
    return before+after


def test_reconnect_peak_has_separate_budget_then_four_kib_gate():
    data=recovery_rows();fault=dict(kind='reconnect',started_s=601,recovered_s=659)
    r=transient_memory(data,data[-1]['perf'],[fault])
    assert r['faults'][0]['minimum_free_step_bytes']['psram']==11776
    assert r['heap_minimum_decrease_bytes']['psram']==396
    assert r['heap_transient_depth_passed'] and r['reconnect_transient_passed']
    assert 'minimum_free_step_bytes' not in fault
    data[3]['perf']['heap']['psram']['minimum_free']=70000
    assert not transient_memory(data,data[-1]['perf'],[fault])['reconnect_transient_passed']


def test_four_kib_post_fault_growth_still_fails():
    data=recovery_rows(81000)
    r=transient_memory(data,data[-1]['perf'],[dict(started_s=601,recovered_s=659)])
    assert not r['heap_transient_depth_passed'] and r['reconnect_transient_passed']


def test_missing_recovery_snapshot_cannot_pass_fault_budget():
    data=rows([0,10,11],[100000,98000,86224])
    faults=[dict(started_s=601,recovered_s=659),dict(started_s=700,recovered_s=750)]
    r=transient_memory(data,data[-1]['perf'],faults)
    assert r['faults'][0]['minimum_free_step_bytes'] is not None
    assert r['faults'][1]['minimum_free_step_bytes'] is None
    assert r['reconnect_transient_passed'] is None


def test_drop_before_first_fault_is_gated_and_settling_is_reported():
    data=rows([0,3,5,7,10],[100000,95000,95000,90000,90000])
    later=rows([11,16,20],[80000,80000,80000],generation=2)
    for r in later:r['ready_elapsed_s']=660
    data+=later
    r=transient_memory(data,data[-1]['perf'],[dict(started_s=601,recovered_s=659)])
    assert r['transient_generations']['1']['baseline_elapsed_s']==300
    assert r['transient_generations']['1']['pre_settle_minimum_decrease_bytes']['psram']==5000
    assert r['transient_generations']['1']['minimum_decrease_bytes']['psram']==5000
    assert not r['heap_transient_depth_passed']


def test_no_fault_uses_identical_post_settle_rule_and_includes_closed_sample():
    data=rows([0,3,5,10],[100000,90000,90000,89900])
    closed=rows([11],[85000])[-1]['perf']
    r=transient_memory(data,closed,[])
    assert r['transient_generations']['1']['baseline_elapsed_s']==300
    assert r['heap_minimum_decrease_bytes']['psram']==5000
    assert not r['heap_transient_depth_passed']


def test_common_slope_uses_per_generation_intercepts_and_detectability():
    data=rows(np.arange(0,12),100000-100*np.arange(0,12))
    later=rows(np.arange(20,32),80000-100*np.arange(0,12),generation=2)
    for r in later:r['ready_elapsed_s']=1200
    trend=heap_trends(data+later)['common']['psram']
    assert trend['free_slope_bytes_per_minute']==pytest.approx(-100)
    assert trend['generations']==2 and trend['span_minutes']==12
    assert trend['largest_single_sample_drop_bytes']==100
    assert trend['minimum_detectable_growth_bytes_per_minute']==pytest.approx(64)
    assert not trend['passed']


def test_qualification_schedule_observes_each_session_without_weakening_memory_limits():
    from soak_memory import SOAK_FAULT_SCHEDULE, QUALIFICATION_MEMORY_TAIL_SECONDS
    def simulate(schedule,tail):
        faults=[dict(started_s=due,recovered_s=due+30) for due,_ in schedule]
        samples=[]
        for elapsed in range(0,1800+tail+1,30):
            if any(f['started_s']<elapsed<f['recovered_s'] for f in faults):continue
            ready=[0]+[f['recovered_s'] for f in faults if f['recovered_s']<=elapsed]
            sample=rows([elapsed/60],[100000],len(ready))[0]
            sample['ready_elapsed_s']=ready[-1]
            sample['perf']['heap_window']['start_us']=samples[-1]['perf']['heap_window']['end_us'] if samples else 0
            samples.append(sample)
        return transient_memory(samples,samples[-1]['perf'],faults),heap_trends(samples)
    original,_=simulate(((600,'reconnect'),(900,'server-restart'),(1200,'reconnect')),90)
    assert original['heap_transient_depth_passed'] is None
    fixed,trends=simulate(SOAK_FAULT_SCHEDULE,QUALIFICATION_MEMORY_TAIL_SECONDS)
    assert fixed['heap_transient_depth_passed'] is True
    assert fixed['reconnect_transient_passed'] is True
    assert growth_verdict(trends) is True
    assert all(r['minimum_decrease_bytes'] is not None for r in fixed['transient_generations'].values())
    assert all(r['span_minutes']>=10 for r in trends['common'].values())
