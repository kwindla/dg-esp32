"""Independent synthetic delay, predeclared outcomes and repeated-trial statistics."""
import numpy as np
import pytest
from scipy.stats import binomtest

import p4_aec_validation as v


def passing(lag):return dict(available=True,passed=True,measured_delay_samples=lag,correlation=.8,separation=.2)


def test_schedule_three_delay_reps_then_balanced_interleaved_sets():
    rows=v.schedule();assert len(rows)==33 and len({r['id'] for r in rows})==33
    assert [r['part'] for r in rows[:3]]==['delay-0','delay-1','delay-2']
    for group in (1,2,3):
        trials=[r for r in rows[3:] if r['set']==group]
        assert len(trials)==10
        assert sum(r['prompt']=='france' for r in trials)==5
        for i in range(0,10,2):assert {r['prompt'] for r in trials[i:i+2]}=={'france','speech'}
    assert rows[3]['prompt']!=rows[13]['prompt']


@pytest.mark.parametrize('lags,outcome,median',[
    ([1024,1024,1024],'measured-confirmed',1024),([1008,1040,None],'measured-confirmed',1024),
    ([1008,1008,None],'measured-confirmed',1008),([1040,1040,None],'measured-confirmed',1040),
    ([1007,1007,1024],'measured-different',1007),([1041,1041,None],'measured-different',1041),
    ([1024,None,None],'unavailable',None),([None,None,None],'unavailable',None),
    ([1023,1024,None],'measured-confirmed',1023.5)])
def test_delay_predeclared_median_rules(lags,outcome,median):
    rows=[passing(n) if n else dict(available=False) for n in lags];r=v.delay_summary(rows)
    assert r['outcome']==outcome and r['delay_samples']==median
    assert r['stop_for_user_decision']==(outcome=='measured-different')


@pytest.mark.parametrize('bad',[dict(measured_delay_samples=True),dict(measured_delay_samples=0),
    dict(measured_delay_samples=4096),dict(correlation=.59),dict(separation=.049),dict(correlation=float('nan')),
    dict(passed=False)])
def test_invalid_cached_passing_scores_fail_closed(bad):
    with pytest.raises(ValueError):v.delay_summary([dict(passing(1024),**bad),passing(1024),dict(available=False)])


@pytest.mark.parametrize('n',[0,2,4])
def test_exactly_three_observations_required(n):
    with pytest.raises(ValueError):v.delay_summary([passing(1024)]*n)


def signals(lag=1024):
    rng=np.random.default_rng(1515)
    source=np.r_[np.zeros(3200),rng.normal(0,800,16000),np.zeros(3200)].astype('<i2')
    near=rng.integers(-1,2,size=(64000,5),dtype='<i2');near[8000:8000+len(source),0]=source
    near[:,4]=0;near[lag:,4]=near[:-lag,0]
    from echoear_aec_alignment import probe_pcm
    probe=probe_pcm();dac=np.zeros((64000,5),dtype='<i2')
    dac[9000:9000+len(probe),2]=probe;dac[:,0]=dac[:,2]//2
    return dac,near,source


@pytest.mark.parametrize('lag',[1024,1300])
def test_full_independent_s3_method_on_synthetic_common_clock(lag):
    dac,near,source=signals(lag);r=v.delay_trial(dac,near,source)
    assert r['passed'] and r['available'] and r['measured_delay_samples']==lag
    assert r['analysis']['known_source_to_reference']['available']
    assert r['analysis']['reference_silent_for_independent_afe']


def test_longer_capture_does_not_bypass_failed_source_gate():
    dac,near,source=signals();r=v.delay_trial(dac,near,source[::-1])
    assert not r['available'] and not r['passed']
    assert not r['analysis']['known_nearend_source_to_mic']['available']


def test_reference_excitation_veto_retained():
    dac,near,source=signals();near[:,2]=near[:,0]
    assert not v.delay_trial(dac,near,source)['available']


def test_short_capture_unavailable_before_relaxing_full_template_gate():
    dac,near,source=signals();r=v.delay_trial(dac[:46400],near,source)
    assert not r['available'] and 'full source' in r['reason']


@pytest.mark.parametrize('hits,total',[(0,15),(15,15),(9,15),(0,0),(3,5)])
def test_exact_binomial_intervals_against_scipy(hits,total):
    r=v.exact_rate(hits,total)
    if total:
        reference=binomtest(hits,total).proportion_ci(.95,method='exact')
        assert r['interval_95']==pytest.approx([reference.low,reference.high],abs=1e-10)
    else:assert r['rate'] is None and r['interval_95'] is None


def observed(hit=True):
    return dict(observed=True,interruption=hit,stt_hit=hit,exact_transcript=hit,word_accuracy=float(hit))


def test_missing_is_not_reclassified_as_a_transcript_miss_or_dropped_from_plan():
    r=v.double_talk_summary([observed(),observed(False),dict(observed=False)],3)
    assert not r['complete'] and r['planned']==3 and r['observed']==2 and r['unavailable']==1
    assert r['interruption']['hits']==1 and r['interruption']['total']==2
    assert r['mean_word_accuracy']==.5 and r['word_accuracy_interval'] is None
    assert not r['reselection'] and r['report_only']


def test_guard_design_names_existing_onset_hooks_and_rearm_gap_without_claiming_a_run():
    r=v.guard_design();assert r['available'] is False and not r['physically_validated_in_window']
    assert r['outcome']=='partially-available-not-run'
    assert all(hook in r['reason'] for hook in ('In-guard','outside-guard','AEC_GUARD','Jabra','uplink.wav','re-arm','scheduled'))
    assert [c['id'] for c in r['conditions']]==v.RULES['guard']['conditions']
    assert all(c['repetitions']==5 for c in r['conditions'])
    assert '2000 ms' in r['evidence'] and 'post-gate' in r['reason']


@pytest.fixture
def prompt_rig(monkeypatch):
    spec=dict(expected='hello',id='france');events=[]
    for direction in ('DOWNSTREAM','UPSTREAM'):
        events.extend([dict(kind='frame',direction=direction,frame='UserStartedSpeakingFrame',monotonic_ns=1,bot_active=False),
            dict(kind='frame',direction=direction,frame='UserStartedSpeakingFrame',monotonic_ns=4e9,bot_active=True),
            dict(kind='frame',direction=direction,frame='InterruptionFrame',monotonic_ns=4e9+1,bot_active=True)])
    events.append(dict(kind='aggregator-user-turn-stopped',monotonic_ns=5e9,text='hello'))
    original=dict(events=events,near_playback=dict(request_monotonic_s=3,duration_valid=True),
        bot_started_ns=2e9,bot_stopped_ns=6e9,before={},after={},complete=True)
    monkeypatch.setattr(v.policy,'reference_gain',lambda *args:30)
    monkeypatch.setattr(v.policy,'clip_evidence',lambda *args:dict(passed=True))
    def echo(pcm,*,quiet_reference,delay_samples,reply_start,reply_end):
        assert delay_samples==1024 and reply_start==0 and reply_end==len(pcm)-1024
        assert np.array_equal(quiet_reference,np.zeros(len(pcm)))
        return dict(available=True,median_db=23)
    monkeypatch.setattr(v,'reply_erle',echo)
    return spec,original


def trial(rig):return v.double_talk_trial(*rig,np.zeros((8192,5),dtype='<i2'),np.zeros((8192,5),dtype='<i2'))


def test_double_talk_counts_mirrored_events_once_and_retains_per_trial_erle(prompt_rig):
    r=trial(prompt_rig);assert r['interruption'] and r['stt_hit'] and r['exact_transcript']
    assert r['word_accuracy']==1 and r['median_erle_db']==23 and r['prompt']['passed']


def test_stt_miss_is_an_observation_and_never_retryable(prompt_rig):
    spec,original=prompt_rig;original['events']=[e for e in original['events'] if e.get('kind')!='aggregator-user-turn-stopped']
    r=trial(prompt_rig);assert r['observed'] and r['interruption'] and not r['stt_hit'] and r['word_accuracy']==0


@pytest.mark.parametrize('change',['before-request','after-stop','not-bot-active','upstream-only'])
def test_unrelated_interruption_does_not_count(prompt_rig,change):
    for e in prompt_rig[1]['events']:
        if e.get('frame')=='InterruptionFrame':
            if change=='before-request':e['monotonic_ns']=2e9
            if change=='after-stop':e['monotonic_ns']=7e9
            if change=='not-bot-active':e['bot_active']=False
            if change=='upstream-only':e['direction']='UPSTREAM'
    assert trial(prompt_rig)['interruption'] is False


def test_missed_physical_request_protocol_unavailable(prompt_rig):
    prompt_rig[1]['near_playback']=None;r=trial(prompt_rig)
    assert r['observed'] is False and r['stt_hit'] is None and r['interruption'] is None


def test_two_different_passing_repetitions_do_not_stop_before_third_is_consumed():
    rows=[passing(1400),passing(1400),dict(available=False,started=False)]
    result=v.delay_summary(rows)
    assert result['outcome']=='unavailable' and result['consumed_repetitions']==2
    assert not result['stop_for_user_decision']
    rows[-1]['started']=True
    assert v.delay_summary(rows)['outcome']=='measured-different'


def test_two_passing_repetitions_cannot_confirm_while_third_is_unstarted():
    rows=[passing(1024),passing(1024),dict(available=False,started=False)]
    assert v.delay_summary(rows)['outcome']=='unavailable'
    rows[-1]['started']=True
    assert v.delay_summary(rows)['outcome']=='measured-confirmed'


def test_missed_injections_are_counted_and_sensitivity_includes_them_as_failures():
    rows=[observed(),observed(False),dict(observed=False,missed_injection=True),dict(observed=False)]
    result=v.double_talk_summary(rows,4)
    assert result['missed_injections']==1
    assert result['interruption']['hits']==1 and result['interruption']['total']==2
    sensitivity=result['sensitivity_missed_injections_as_failures']
    for key in ('interruption','stt_accuracy_ge_90_percent','exact_transcript'):
        assert sensitivity[key]==v.exact_rate(1,3)
    assert sensitivity['other_unavailable_excluded']==1


def test_all_missed_injections_have_zero_sensitivity_rate_without_fabricating_observations():
    result=v.double_talk_summary([dict(observed=False,missed_injection=True)]*5,5)
    assert result['missed_injections']==5 and result['interruption']['total']==0
    assert result['sensitivity_missed_injections_as_failures']['interruption']==v.exact_rate(0,5)


def test_bad_duration_is_protocol_unavailable_but_not_a_missed_injection(prompt_rig):
    prompt_rig[1]['near_playback']['duration_valid']=False
    result=trial(prompt_rig)
    assert not result['observed'] and result['missed_injection'] is False


def test_request_after_early_bot_stop_is_a_missed_injection_in_sensitivity(prompt_rig):
    prompt_rig[1]['near_playback']['request_monotonic_s']=7
    result=trial(prompt_rig)
    assert not result['observed'] and result['missed_injection']
    summary=v.double_talk_summary([result],1)
    assert summary['missed_injections']==1
    assert summary['sensitivity_missed_injections_as_failures']['interruption']==v.exact_rate(0,1)
