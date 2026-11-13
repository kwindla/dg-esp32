"""Host-only prospective RF gate; no hardware/service launches."""
from unittest.mock import patch

import pytest

from echoear_rf_precheck import evaluate_probe, load_plan, next_action


def fixture():
    packets = [dict(pc_id='peer', ssrc=1, seq=i, timestamp=i*960,
                    observed_ms=94000+i*20) for i in range(3351)]
    events = [dict(pc_id='peer', t=130, event='input-level')]
    states = [dict(host_s=93, enabled=1)]
    return packets, events, states


def evaluate(data=None, **kwargs):
    packets, events, states = data or fixture()
    return evaluate_probe(packets, events, states, pc_id='peer', start=100,
                          end=160, **kwargs)


def test_real_continuous_silent_audio_is_eligible_without_qualifying_firmware():
    result = evaluate()
    assert result['eligible'] and not result['qualification'] and not result['scored']
    assert result['link_tail']['intervals'] == 3002
    assert result['link_tail']['categories'].keys() == {'eligible'}
    assert result['link_tail']['over_threshold'] == 0


def test_precheck_boundary_is_exclusive_and_not_rounded_or_the_scored_one_percent():
    real = evaluate()['link_tail']
    for count, eligible in [(14, True), (15, False), (16, False), (29, False)]:
        tail = dict(real, intervals=3000, over_threshold=count)
        with patch('echoear_rf_precheck.link_tail', return_value=tail):
            assert evaluate()['eligible'] is eligible
    # The scored production defaults have not been mutated.
    assert real['rate_limit'] == .01 and real['extreme_rate_limit'] == .0005
    assert real['hard_cap_ms'] == 2000


def test_actual_burst_tail_fails_and_all_gaps_remain_in_fixed_window():
    packets, events, states = fixture()
    for i in range(400, 2800, 80):
        # Create 80ms burst/recovery gaps without changing window/packet count.
        for j in range(3):
            packets[i+j]['observed_ms'] = packets[i+3]['observed_ms']
    result = evaluate((packets, events, states))
    assert not result['eligible']
    assert result['link_tail']['over_threshold'] == 30
    assert result['link_tail']['intervals'] == 3002


@pytest.mark.parametrize('kind', ['short', 'long', 'missing_endpoint', 'sparse',
                                 'truncated', 'unknown_status', 'second_peer',
                                 'second_ssrc', 'no_settle', 'missing_pipeline',
                                 'audio_disabled', 'no_audio_ack', 'clock_reset'])
def test_missing_or_noncontinuous_evidence_cannot_admit(kind):
    packets, events, states = fixture()
    options = {}
    if kind == 'short':
        assert not evaluate_probe(packets, events, states, pc_id='peer', start=100, end=159)['eligible'];return
    if kind == 'long':
        assert not evaluate_probe(packets, events, states, pc_id='peer', start=100, end=161)['eligible'];return
    if kind == 'missing_endpoint':packets = packets[:-100]
    if kind == 'sparse':packets = packets[::2]
    if kind == 'truncated':options['truncated'] = True
    if kind == 'unknown_status':options['truncated'] = None
    if kind == 'second_peer':packets.append(dict(packets[1000], pc_id='other'))
    if kind == 'second_ssrc':packets.append(dict(packets[1000], ssrc=2))
    if kind == 'no_settle':packets = packets[100:]
    if kind == 'missing_pipeline':events = []
    if kind == 'audio_disabled':states += [dict(host_s=120, enabled=0),dict(host_s=140, enabled=1)]
    if kind == 'no_audio_ack':states = []
    if kind == 'clock_reset':states += [dict(host_s=92, enabled=1)]
    assert not evaluate((packets, events, states), **options)['eligible']


@pytest.mark.parametrize('event', ['first-tts-byte', 'first-fixture-byte', 'vad-start',
                                 'final-transcript', 'transport-disconnected'])
def test_speech_or_session_activity_defers_without_gap_exclusions(event):
    packets, events, states = fixture()
    events.append(dict(pc_id='peer',t=120,event=event))
    assert not evaluate((packets, events, states))['eligible']


def row(index=1, end=100, eligible=False, fatal=False):
    return dict(probe=index, started_utc_s=end-70, ended_utc_s=end,
                eligible=eligible, fatal=fatal, scored=False, completed=True)


def test_retry_deadline_initial_plus_four_release_bench_and_no_sixth_probe():
    assert next_action([],100)['probe'] == 1
    history=[]
    for i in range(1,6):
        end=100+(i-1)*1870
        history.append(row(i,end))
        if i < 5:
            assert next_action(history,end)['action']=='wait'
            assert next_action(history,end)['release_bench']
            assert next_action(history,end+1799.9)['action']=='wait'
            assert next_action(history,end+1800)==dict(action='probe',probe=i+1)
        else:assert next_action(history,end)['action']=='stop'


def test_admission_runs_only_one_three_original_campaign():
    history=[row(eligible=True)]
    assert next_action(history,101)==dict(action='campaign',probe=1,runs=3,retakes=0)
    assert next_action(history,102,campaign_started=True)['action']=='stop'
    with pytest.raises(ValueError):next_action(history+[row(2,1970)],2000)


def test_device_or_cleanup_failure_is_fatal_not_environment_deferral():
    assert next_action([row(fatal=True)],101)['action']=='stop'
    with pytest.raises(ValueError):next_action([row(fatal=True),row(2,1970)],2000)


def test_restart_cannot_skip_deadline_replay_probe_or_relabel_scored_run():
    with pytest.raises(ValueError):next_action([row(),row(2,1800)],2000)
    with pytest.raises(ValueError):next_action([row(2)],101)
    scored=row();scored['scored']=True
    with pytest.raises(ValueError):next_action([scored],101)
    with pytest.raises(ValueError):next_action([row()],90)


def test_plan_preserves_scored_thresholds_and_frozen_failed_population():
    plan=load_plan()
    assert plan['campaign']['scored_uplink']==dict(threshold_ms=60,rate_limit=.01,
        extreme_threshold_ms=150,extreme_rate_limit=.0005,hard_cap_ms=2000)
    assert plan['campaign']['attempt']==3 and plan['campaign']['normal_elf_sha256'].startswith('9936da45')
    assert 'remain unchanged' in plan['campaign']['scope']


@pytest.mark.parametrize('field', ['eligible','fatal','completed'])
@pytest.mark.parametrize('invalid', [None,'false','true',0,1,'missing'])
def test_incomplete_or_nonboolean_persisted_disposition_cannot_retry_or_admit(field,invalid):
    entry=row()
    if invalid=='missing':entry.pop(field)
    else:entry[field]=invalid
    with pytest.raises(ValueError):next_action([entry],2000)


@pytest.mark.parametrize('field',['started_utc_s','ended_utc_s','now'])
@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),float('-inf'),None,'100',True])
def test_nonfinite_or_nonnumeric_persisted_clocks_fail_closed(field,invalid):
    entry=row();now=2000
    if field=='now':now=invalid
    else:entry[field]=invalid
    with pytest.raises(ValueError):next_action([entry],now)


def test_inconsistent_fatal_admission_and_sixth_probe_rejected():
    with pytest.raises(ValueError):next_action([row(eligible=True,fatal=True)],2000)
    history=[row(i,100+(i-1)*1870) for i in range(1,7)]
    with pytest.raises(ValueError):next_action(history,10000)
