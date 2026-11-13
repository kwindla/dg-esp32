import numpy as np
import pytest
from echoear_aec_noise_gate import quiet_gate, validate_noise_gate, NoiseRetryQueue


@pytest.fixture
def policy():
    return dict(version=1,frame_ms=20,level_tolerance_db=3,transient_max_dbfs=-52,
                max_retries=2,pause_consecutive_cells=3,quiet_seconds=2,
                baseline_seconds=6,baseline_max_attempts=3,settle_seconds=5,baseline_max_dbfs=-53)


def tone(level, samples=32000):
    return np.full(samples,32768*10**(level/20))


def test_level_and_transient_are_independent(policy):
    assert quiet_gate(tone(-60),-60,policy)['passed']
    assert not quiet_gate(tone(-56.9),-60,policy)['level_passed']
    assert not quiet_gate(tone(-63.1),-60,policy)['level_passed']
    raw=tone(-62);raw[320:640]=tone(-51,320)
    result=quiet_gate(raw,-62,policy)
    assert result['level_passed'] and not result['transient_passed']
    assert not result['passed']


def test_incomplete_window_is_not_silence(policy):
    with pytest.raises(ValueError):quiet_gate(tone(-60,31999),-60,policy)


@pytest.mark.parametrize('key,value',[('max_retries',3),('transient_max_dbfs',-50),
    ('level_tolerance_db',4),('frame_ms',100),('pause_consecutive_cells',4)])
def test_gate_cannot_silently_relax(policy,key,value):
    validate_noise_gate(policy);policy[key]=value
    with pytest.raises(ValueError):validate_noise_gate(policy)


def test_retries_are_bounded_and_preserve_identity(policy):
    q=NoiseRetryQueue([dict(run_id='a')],policy)
    for expected in (1,2,3):
        cell,attempt=q.next();assert cell['run_id']=='a' and attempt==expected
        action=q.finish(cell,attempt,False)
    assert action=='retries-exhausted' and q.exhausted==['a']
    assert q.next() is None and not q.paused


def test_three_distinct_noisy_cells_pause_before_any_retry(policy):
    q=NoiseRetryQueue([dict(run_id=x) for x in 'abcd'],policy)
    for x in 'abc':
        cell,attempt=q.next();assert cell['run_id']==x
        q.finish(cell,attempt,False)
    assert q.paused and q.next() is None
    assert [(c['run_id'],a) for c,a in q.pending]==[('d',1),('a',2),('b',2),('c',2)]


def test_clean_gate_resets_streak_even_when_acoustic_result_fails(policy):
    q=NoiseRetryQueue([dict(run_id=x) for x in 'abc'],policy)
    c,a=q.next();q.finish(c,a,False)
    c,a=q.next();assert q.finish(c,a,True)=='accepted'
    assert q.noisy_cells==[]
    c,a=q.next();q.finish(c,a,False)
    assert not q.paused and q.noisy_cells==['c']


def test_acquisition_failure_does_not_gain_retry_permission(policy):
    q=NoiseRetryQueue([dict(run_id='a')],policy)
    c,a=q.next();assert q.finish(c,a,None)=='acquisition-failed'
    assert q.next() is None and not q.exhausted


@pytest.fixture
def policy_v2(policy):
    policy.pop('level_tolerance_db')
    return dict(policy,version=2,level_max_dbfs=-55,level_rise_max_db=6,transient_max_dbfs=-46)


def test_coordinator_v2_limits_are_independent(policy_v2):
    validate_noise_gate(policy_v2)
    assert quiet_gate(tone(-58.5),-61.6,policy_v2)['passed']
    assert quiet_gate(tone(-70),-61.6,policy_v2)['passed']
    assert not quiet_gate(tone(-54.9),-60,policy_v2)['level_passed']
    assert not quiet_gate(tone(-57.9),-64,policy_v2)['level_passed']
    raw=tone(-62);raw[320:640]=tone(-45.9,320)
    gate=quiet_gate(raw,-62,policy_v2)
    assert gate['level_passed'] and not gate['transient_passed']


@pytest.mark.parametrize('key,value',[('level_max_dbfs',-54),('level_rise_max_db',7),('transient_max_dbfs',-45)])
def test_v2_limits_cannot_silently_relax(policy_v2,key,value):
    policy_v2[key]=value
    with pytest.raises(ValueError):validate_noise_gate(policy_v2)


def test_resume_preserves_lifetime_retry_budget(policy_v2):
    q=NoiseRetryQueue([dict(run_id='a',start_attempt=2)],policy_v2)
    for expected in (2,3):
        cell,attempt=q.next();assert attempt==expected
        action=q.finish(cell,attempt,False)
    assert action=='retries-exhausted' and q.exhausted==['a'] and q.next() is None


@pytest.mark.parametrize('attempt',[0,4,True,1.5])
def test_invalid_resume_attempt(policy_v2,attempt):
    with pytest.raises(ValueError):NoiseRetryQueue([dict(run_id='a',start_attempt=attempt)],policy_v2)


def test_acquisition_failure_preserves_noise_streak(policy):
    q = NoiseRetryQueue([dict(run_id=x) for x in 'abcd'], policy)
    for verdict in (False, None, False):
        cell, attempt = q.next()
        q.finish(cell, attempt, verdict)
    assert q.noisy_cells == ['a', 'c'] and not q.paused
    cell, attempt = q.next()
    assert q.finish(cell, attempt, False) == 'pause-three-noisy-cells'
    assert q.paused


@pytest.fixture
def policy_v3(policy_v2):
    from echoear_aec_noise_gate import V3_FILTER
    return dict(policy_v2, version=3, filter=V3_FILTER.copy(), rise_reference_min_dbfs=-61)


def test_v3_filters_dc_but_rejects_acoustic_noise_and_retains_full_band(policy_v3):
    from echoear_aec_noise_gate import baseline_level
    rng = np.random.default_rng(7)
    quiet = rng.normal(size=96000)*32768*10**(-67/20)
    baseline = baseline_level(quiet+200, policy_v3)
    assert baseline == pytest.approx(baseline_level(quiet, policy_v3), abs=1e-8)
    raw = quiet[:32000]+200
    gate = quiet_gate(raw, baseline, policy_v3, mic3=raw)
    assert gate['passed'] and gate['raw_dbfs'] > -55 and gate['gate_dbfs'] < -65
    assert gate['mic3_lowpass100_dbfs'] > -55
    loud = raw+rng.normal(size=32000)*32768*10**(-45/20)
    assert not quiet_gate(loud, baseline, policy_v3, mic3=raw)['passed']
    transient = raw.copy();transient[16000:16320] += np.tile([1000., -1000.],160)
    assert not quiet_gate(transient, baseline, policy_v3)['transient_passed']


@pytest.mark.parametrize('key,value',[('order',2),('cutoff_hz',90),('padlen',0),('application','sosfilt')])
def test_v3_filter_contract_cannot_drift(policy_v3,key,value):
    policy_v3['filter'][key]=value
    with pytest.raises(ValueError):validate_noise_gate(policy_v3)


def test_v3_retained_windows_and_positive_control(policy_v3):
    from pathlib import Path
    from echoear_aec_noise_validation import validate_retained
    evidence=Path.home()/'src/evidence/echoear/aec-demo'
    if not (evidence/'step-9/campaign-01').exists():
        pytest.skip('Retained bench evidence not installed on this host')
    result=validate_retained(evidence,policy_v3)
    assert len(result['windows'])==14 and result['passed']
    assert all(w['mic3_lowpass100_dbfs'] is not None for w in result['windows'])


@pytest.mark.parametrize('baseline,reference',[(-67.5,-61),(-61,-61),(-58,-58)])
def test_v3_rise_reference_floor_and_absolute_limit(policy_v3,baseline,reference):
    t=np.arange(32000)/16000
    raw=np.sin(2*np.pi*1000*t)*32768*10**(-56/20)*np.sqrt(2)
    gate=quiet_gate(raw,baseline,policy_v3)
    assert gate['rise_reference_dbfs']==reference
    assert gate['filtered_baseline_delta_db']==pytest.approx(gate['gate_dbfs']-baseline)
    assert gate['delta_db']==pytest.approx(gate['gate_dbfs']-reference)
    assert gate['passed']  # stationary -56 is below the restored -55 ceiling
    assert not quiet_gate(raw*10**(2/20),baseline,policy_v3)['passed']


@pytest.mark.parametrize('floor',[None,-62,-60])
def test_v3_floor_is_declared_and_pinned(policy_v3,floor):
    policy_v3['rise_reference_min_dbfs']=floor
    with pytest.raises(ValueError):validate_noise_gate(policy_v3)
