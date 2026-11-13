"""Retained guard control and negatives for the approved event-detection rule."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import p4_aec_guard_rescore as guard
import p4_aec_policy as policy
from make_bundle import digest
from wire_contract import ContractError


@pytest.fixture
def retained():
    return json.loads(guard.ATTEMPT.read_text())


def test_real_retained_attempt_passes_with_original_physical_failure_visible(retained):
    before=digest(guard.ATTEMPT);result=guard.score_events(retained)
    assert not retained['score']['passed'] and result['passed']
    assert result['observed_onset'] and result['rearmed'] and result['zero_self_interruptions']
    assert result['guard_ms']==400 and result['rearm_ms']==2000
    probe=result['after_rearm_probe']
    assert probe['detected'] and probe['after_rearm'] and probe['post_injection_bot_active_starts']==1
    assert probe['post_injection_interruptions']==1 and probe['transcript_count']==1
    assert probe['transcript']=="Where's the capital of France?"
    assert probe['request_after_bot_start_ms']==pytest.approx(3002.780366)
    assert result['inside_probe']['detected'] and not result['inside_probe']['after_rearm']
    assert digest(guard.ATTEMPT)==before


@pytest.mark.parametrize('change',['no-start','no-interruption','empty-transcript','two-transcripts',
    'earlier-start','earlier-interruption','upstream-only','bot-inactive','no-injection'])
def test_missing_detection_or_earlier_bot_active_turn_fails(retained,change):
    original=retained['after_original'];request=original['near_playback']['request_monotonic_s']*1e9
    events=original['events']
    if change in ('no-start','no-interruption'):
        frame='UserStartedSpeakingFrame' if change=='no-start' else 'InterruptionFrame'
        original['events']=[e for e in events if not(e.get('frame')==frame and e['monotonic_ns']>=request)]
    elif change in ('empty-transcript','two-transcripts'):
        transcript=next(e for e in events if e.get('kind')=='aggregator-user-turn-stopped' and e['monotonic_ns']>=request)
        if change=='empty-transcript':transcript['text']='  '
        else:events.append(copy.deepcopy(transcript))
    elif change in ('earlier-start','earlier-interruption'):
        frame='UserStartedSpeakingFrame' if change=='earlier-start' else 'InterruptionFrame'
        events.append(dict(kind='frame',frame=frame,direction='DOWNSTREAM',bot_active=True,monotonic_ns=int(request)-1))
    elif change=='no-injection':original['near_playback']=None
    else:
        for e in events:
            if e.get('kind')=='frame' and e.get('frame')=='UserStartedSpeakingFrame' and e['monotonic_ns']>=request:
                if change=='upstream-only':e['direction']='UPSTREAM'
                else:e['bot_active']=False
    assert not guard.score_events(retained)['passed']


@pytest.mark.parametrize('offset_ms,expected',[(2399,False),(2400,False),(2400.001,True)])
def test_injection_must_be_strictly_after_guard_plus_rearm(retained,offset_ms,expected):
    original=retained['after_original']
    original['near_playback']['request_monotonic_s']=(original['bot_started_ns']+offset_ms*1000000)/1e9
    score=guard.score_events(retained)
    assert score['after_rearm_probe']['after_rearm'] is expected and score['passed'] is expected


@pytest.mark.parametrize('change',['nine-turns','long-self-interruption','no-onsets','no-rearm'])
def test_other_guard_acceptance_terms_stay_hard(retained,change):
    if change=='nine-turns':
        retained['long_turns'].pop()
        with pytest.raises(ContractError,match='ten'):guard.score_events(retained)
        return
    if change=='long-self-interruption':
        turn=retained['long_turns'][0]
        active=next(e for e in turn['events'] if e.get('kind')=='frame' and e.get('bot_active') is True)
        turn['events'].append(dict(active,frame='UserStartedSpeakingFrame',direction='DOWNSTREAM'))
    elif change=='no-onsets':retained['onsets']=[]
    else:retained['onsets']=[line.split('previous_end_us=')[0]+'previous_end_us=-1' for line in retained['onsets']]
    assert not guard.score_events(retained)['passed']


def test_retained_hashes_and_decision_are_pinned_before_replay(tmp_path,monkeypatch):
    plan=tmp_path/'plan.json';plan.write_text('{}')
    with pytest.raises(ContractError,match='pinned retained'):guard.authenticate(plan,guard.GAIN,tmp_path/'result.json')
    decision=tmp_path/'decision.md';decision.write_text('changed')
    monkeypatch.setattr(guard,'DECISION',decision)
    with pytest.raises(ContractError,match='decision changed'):guard.context()


def test_rescore_authenticates_inputs_and_keeps_original_score_and_delay(retained,monkeypatch,tmp_path):
    plan=tmp_path/'plan.json';plan.write_text('{}')
    gain=tmp_path/'gain.json';gain.write_text('{}')
    attempt=tmp_path/'attempt/result.json';attempt.parent.mkdir();attempt.write_text(json.dumps(retained))
    proof=dict(kind='decision-backed',delay_samples=1024,measured_pass=False)
    authenticate=Mock(return_value=(dict(elf_sha256='a'*64),dict(rescored_selection=dict(delay_provenance=proof)),retained))
    monkeypatch.setattr(guard,'authenticate',authenticate)
    result=guard.rescore(plan,gain,attempt)
    authenticate.assert_called_once_with(plan,gain,attempt)
    assert result['score']['passed'] and not result['original_score']['passed']
    assert result['applied'] is False and result['delay_provenance']==proof
    assert result['source_sha256'][str(attempt.resolve())]==digest(attempt)
    assert result['scoring_context']['decision']['sha256']==guard.DECISION_SHA256


def test_changed_rescore_output_fails_replay(tmp_path,monkeypatch):
    expected=dict(kind='p4-aec-guard-event-rescore',plan_file='plan',gain_file='gain',attempt_file='attempt',score=dict(passed=False))
    out=tmp_path/'rescore.json';out.write_text(json.dumps(dict(expected,score=dict(passed=True))))
    monkeypatch.setattr(guard,'rescore',lambda *args:expected)
    with pytest.raises(ContractError,match='provenance or score'):guard.checked_rescore(out)


def test_existing_output_is_refused_without_replay(tmp_path,monkeypatch):
    out=tmp_path/'existing.json';out.write_text('original')
    run=Mock();monkeypatch.setattr(guard,'rescore',run)
    with pytest.raises(ContractError,match='fresh'):
        guard.main(['guard-rescore','--plan','plan','--gain-selection','gain','--attempt','attempt','--out',str(out)])
    assert not run.called and out.read_text()=='original'


def test_recommendation_keeps_delay_decision_and_applied_false(tmp_path,monkeypatch,retained):
    plan=tmp_path/'plan.json';plan.write_text(json.dumps(dict(elf_sha256='a'*64)))
    delay=tmp_path/'delay.json';delay.write_text('{}')
    gain=tmp_path/'gain.json';gain.write_text(json.dumps(dict(rescored_selection=dict(delay_file=str(delay)))))
    score=tmp_path/'score.json';score.write_text('{}')
    row=dict(plan_file=str(plan),gain_file=str(gain),score=guard.score_events(retained),source_sha256={'raw':'b'*64})
    monkeypatch.setattr(guard,'checked_rescore',lambda *args:row)
    proof=dict(kind='decision-backed',measured_pass=False,delay_samples=1024)
    monkeypatch.setattr(guard.measure,'delay_input',lambda *args:proof)
    result=guard.recommendation(plan,gain,[score])
    assert result['passed'] and result['applied'] is False and result['qualification'] is False
    assert result['selected']==dict(ref_gain_db=30,guard_ms=400,rearm_ms=2000,afe_delay_samples=1024)
    assert result['delay_provenance']==proof and result['source_sha256']['raw']=='b'*64
