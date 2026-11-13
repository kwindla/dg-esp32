from agent_conversation_test import content_matches, summarize, latency_summary
from agent_prompts import validate, TURNS


def test_fixture_bank_is_immutable_and_different_voice():
    assert len(validate()['turns'])==8
    assert len(TURNS)>=6


def test_full_fixture_validation_rejects_keyword_correct_italy_defect(tmp_path,monkeypatch):
    import json
    import shutil
    import pytest
    import agent_prompts
    shutil.copytree(agent_prompts.BANK,tmp_path/'bank')
    monkeypatch.setattr(agent_prompts,'BANK',tmp_path/'bank')
    path=agent_prompts.BANK/'manifest.json';manifest=json.loads(path.read_text())
    italy=next(r for r in manifest['turns'] if r['id']=='italy')
    assert content_matches('End of Italy.',italy['stt'])
    italy['validation']['text']='End of Italy.'
    path.write_text(json.dumps(manifest))
    with pytest.raises(AssertionError):agent_prompts.validate()


def test_keyword_checks_accept_number_words_and_reject_substrings():
    assert content_matches('Two plus three is 5.', [['five','5']])
    assert content_matches('Your name is Sam!', [['sam']])
    assert not content_matches('A sample of Parisian culture.', [['sam'],['paris']])
    assert not content_matches('', [['paris']])


def test_missing_observations_are_failures_and_not_latency_zeroes():
    result=summarize([dict(prompt='france',checks={'vad':True},failures=[{'cause':'acoustic'}],
        passed=False,latency_ms={'vad_stop':-100,'audible_reply':None}),
        dict(prompt='italy',checks={'vad':True},failures=[],passed=True,
        latency_ms={'vad_stop':100,'audible_reply':500})])
    assert result['checks']['acoustic_content']=={'passed':0,'unavailable':0,'total':0}
    assert result['acoustic_scored_turns']==0  # no transport evidence, excluded
    assert result['latency_ms']['audible_reply']['n']==1
    assert result['latency_ms']['audible_reply']['max']==500
    assert result['latency_ms']['vad_stop']['p50']==-100
    assert result['passes']==1 and len(result['failures'])==1


def test_playback_evidence_uses_actual_perf_counters_and_requires_all_three():
    import pytest
    from agent_conversation_test import playback_counter_deltas
    before={'received':100,'decode_frames':95,'pulled_frames':94}
    after={'received':200,'decode_frames':195,'pulled_frames':194}
    assert playback_counter_deltas(before,after)==dict(received=100,decode_frames=100,pulled_frames=100)
    with pytest.raises(KeyError):playback_counter_deltas(before,{'received':200})


def test_one_reply_is_independent_of_missing_vad_and_counts_generations():
    from agent_conversation_test import one_reply_matches
    events=[dict(event='final-transcript',turn=1), dict(event='llm-start',turn=1), dict(event='llm-response',turn=1)]
    assert one_reply_matches(events)  # No VAD: that separate check must fail.
    assert not one_reply_matches(events+[dict(event='llm-start',turn=1)])
    assert not one_reply_matches(events+[dict(event='llm-response',turn=1)])
    assert one_reply_matches(events+[dict(event='final-transcript',turn=2)])


def test_upstream_stt_miss_is_not_misclassified_as_llm_or_acoustic_variance():
    from agent_conversation_test import failure_cause
    assert failure_cause('vad',{})=='VAD miss'
    assert failure_cause('llm_content',{'stt':False})=='STT miss (upstream)'
    assert failure_cause('acoustic_content',{'stt':False,'llm_content':False})=='STT miss (upstream)'
    assert failure_cause('acoustic_content',{'stt':False,'llm_content':True})=='acoustic'
    assert failure_cause('acoustic_content',{'stt':True,'llm_content':False})=='LLM wording (upstream)'
    assert failure_cause('acoustic_content',{'stt':True,'llm_content':True})=='acoustic'


def test_reanalysis_preserves_vad_failure_and_does_not_mutate_originals():
    from agent_conversation_summary import analyze
    import copy
    events=[dict(event='final-transcript',turn=1),dict(event='llm-start',turn=1),dict(event='llm-response',turn=1)]
    checks={k:True for k in ('stt','llm_content','playback','acoustic_content','gate','session')}
    checks.update(vad=False,one_reply=False)
    raw=dict(summary={'old':True},turns=[dict(prompt='addition',events=events,checks=checks,passed=False,
        failures=[dict(check='vad',cause='STT miss'),dict(check='one_reply',cause='reply count')])])
    before=copy.deepcopy(raw);r=analyze(raw)
    assert raw==before
    assert r['summary']['passes']==0
    assert r['summary']['checks']['one_reply']['passed']==1
    assert r['summary']['checks']['vad']['passed']==0
    assert r['summary']['failures'][0]['failures']==[dict(check='vad',cause='VAD miss')]
    assert len(r['checker_corrections'])==1


def test_missing_input_is_not_reported_as_failed_device_playout():
    from agent_conversation_summary import analyze
    raw=dict(summary={},turns=[dict(prompt='italy',events=[],stt_text='',
        checks={k:False for k in ('stt','playback','gate','one_reply')},passed=False,
        failures=[dict(check=k,cause=k) for k in ('stt','playback','gate','one_reply')])])
    result=analyze(raw)
    failures=result['summary']['failures'][0]['failures']
    assert failures[0]['cause']=='STT miss'
    assert all(f['cause']=='STT miss (upstream; no generated reply)' for f in failures[1:])
    assert result['summary']['passes']==0
    assert result['summary']['checks']['playback']['passed']==0
    raw['turns'][0].update(before_perf=dict(sent=0,encode_frames=0),
                          after_perf=dict(sent=0,encode_frames=0))
    inactive=analyze(raw)['summary']['failures'][0]['failures']
    assert all(f['cause']=='capture/uplink inactive' for f in inactive)


def test_count_smart_format_requires_the_exact_complete_sequence():
    from agent_conversation_test import answer_matches
    from agent_prompts import EXTRA
    assert answer_matches('1234567891011121314151617181920.', EXTRA[0])
    assert answer_matches('1, 2, 3, ..., 20.', EXTRA[0])
    assert not answer_matches('1234567891011121314151617181921.', EXTRA[0])
    assert not answer_matches('12345678910111213141516171819200.', EXTRA[0])
    assert not answer_matches('The answer is 120.', EXTRA[0])


def test_restart_ready_log_is_not_a_capture_readiness_barrier(monkeypatch):
    import agent_conversation_test as harness
    class Board:
        published_ready = False
        capture = False
        def command(self, command, timeout=5):
            if command == 'connect': self.published_ready = True
            if command == 'audio 1': self.capture = self.published_ready
        def wait_for(self, pattern, timeout, since):
            assert since == 10  # Exclude stale gate events before new Ready.
    board = Board()
    board.command('audio 1')  # Former path: logged Ready, not yet published.
    assert not board.capture
    sent = 0
    def observed_perf(board):
        nonlocal sent
        if board.capture: sent += 10
        return dict(sent=sent, pc_id='new-session')
    monkeypatch.setattr(harness, 'perf', observed_perf)
    assert harness.resume_after_restart(board, (10, 12.0, 'STATE Ready')) == 'new-session'
    assert board.capture and board.agent_ready_at == 12.0 and sent > 10


def test_rebuffer_gap_is_not_the_end_of_the_recorded_reply():
    from agent_conversation_test import reply_gate_window
    gates=[(1,'closed'),(2,'open'),(2.15,'closed'),(3.5,'open')]
    assert reply_gate_window(gates)==(1,3.5)
    # Missing final reopen remains unavailable, not an earlier gap's reopen.
    assert reply_gate_window(gates[:-1])==(1,None)
    assert reply_gate_window([(1,'open')])==(None,None)


def test_correct_llm_answer_does_not_blame_acoustic_miss_on_input_ambiguity():
    from agent_conversation_summary import analyze
    from agent_conversation_test import CHECKS
    checks={k:True for k in CHECKS};checks['acoustic_content']=False
    raw=dict(summary={},turns=[dict(prompt='italy',checks=checks,stt_text='End of Italy.',
        events=[dict(event='final-transcript',turn=1),dict(event='llm-start',turn=1),dict(event='llm-response',turn=1)],
        failures=[dict(check='acoustic_content',cause='acoustic',detail='The capital is wrong')],passed=False)])
    assert analyze(raw)['summary']['failures'][0]['failures'][0]['cause']=='acoustic'


def test_cancelled_generation_requires_short_verified_audible_evidence():
    from agent_conversation_test import one_reply_matches
    events=[dict(event='final-transcript',turn=1),dict(event='llm-start',turn=1,response_id=1),
        dict(event='llm-cancelled',turn=1,response_id=1),dict(event='final-transcript',turn=2),
        dict(event='llm-start',turn=2,response_id=2),dict(event='llm-response',turn=2,response_id=2)]
    assert not one_reply_matches(events)
    segments=[dict(response_id=1,audible_ms=200,complete=False,verified=True),
              dict(response_id=2,audible_ms=1000,complete=True,verified=True)]
    assert one_reply_matches(events,segments)
    segments[0]['audible_ms']=300
    assert not one_reply_matches(events,segments)
    segments[0]['audible_ms']=None
    assert not one_reply_matches(events,segments)


def test_stt_boundary_excludes_late_finals_after_first_generation():
    from agent_conversation_test import final_before_inference
    events=[dict(event='final-transcript',t=1),dict(event='llm-start',t=2),
            dict(event='final-transcript',t=3),dict(event='llm-start',t=4)]
    assert final_before_inference(events)==(1,[events[2]])


def test_cancellation_without_audible_evidence_is_not_reported_as_zero():
    from agent_conversation_test import cancelled_audio_duration
    assert cancelled_audio_duration([dict(event='first-tts-byte',response_id=1)],[],1) is None
    assert cancelled_audio_duration([],[],1)==0
    assert cancelled_audio_duration([], [dict(response_id=1,audible_ms=None)],1) is None
    assert cancelled_audio_duration([], [dict(response_id=1,audible_ms=120)],1)==120


def test_transport_deltas_include_all_review_counters_without_inventing_missing_values():
    from agent_conversation_test import transport_counter_deltas
    before=dict(playout_schema=2,lost=4,concealed=4,plc_frames=4,drift_drops=1,rebuffers=2,playout_dropped=6)
    after=dict(playout_schema=2,lost=7,concealed=7,plc_frames=7,drift_drops=3,rebuffers=3,playout_dropped=10)
    assert transport_counter_deltas(before,after)==dict(lost=3,concealed=3,plc_frames=3,
        drift_drops=2,rebuffers=1,playout_dropped=4,drift_inserts=None,
        post_plc_zeros=None,device_silence_blocks=None,discontinuity=None,latency_trim=None,
        i2s_underflows=None,i2s_write_errors=None)


def test_unavailable_transport_counter_is_null_in_campaign_summary():
    from step8d2_campaign_summary import counter_details
    result=counter_details([dict(transport_deltas=dict(lost=0,concealed=2))])
    assert result['lost']==dict(available=1,turns_nonzero=0,total=0,max=0)
    assert result['concealed']==dict(available=1,turns_nonzero=1,total=2,max=2)
    assert result['drift_inserts']==dict(available=0,turns_nonzero=0,total=None,max=None)


def test_calibration_requires_only_scored_prompts_and_requested_extra_cases():
    from agent_conversation_test import calibration_definitions
    assert [d['id'] for d in calibration_definitions()]==[d['id'] for d in TURNS]
    assert [d['id'] for d in calibration_definitions(pause_case=True)][-1]=='pause'
    assert [d['id'] for d in calibration_definitions(interruptions=True)][-1]=='long'


def test_raw_board_stt_survives_transport_damage_and_unverified_jabra(tmp_path,monkeypatch):
    import numpy as np
    import agent_conversation_test as harness
    from playback_quality import TRANSPORT_COUNTERS,classify_turn
    from step7_support import write_pcm
    source=tmp_path/'source.wav';write_pcm(source,np.full(1600,1000))
    segment=dict(response_id=1,cancelled=False,verified=False,source_path=str(source),
        board=dict(offset_s=.1),offset_s=.1,onset=.1,end=.2)
    clean=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS})
    row=dict(before_perf=clean,after_perf=dict(clean,lost=1,concealed=1,plc_frames=1))
    assert not classify_turn(row)['passed']
    monkeypatch.setattr(harness,'transcribe_acoustic',lambda path:'Paris.')
    definition=next(d for d in TURNS if d['id']=='france')
    result=harness.transcribe_board_responses(tmp_path,np.r_[np.zeros(1600),np.full(1600,1000)],
        [segment],definition)
    assert result['available'] and result['passed'] and result['responses'][0]['text']=='Paris.'
    assert not row['reply_transport_gate']['passed']
