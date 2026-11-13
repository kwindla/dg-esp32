import copy
import numpy as np
import pytest
from soak_memory import transient_memory
from test_soak_memory import rows
from step8d2_campaign_summary import details


def test_drop_across_settlement_gap_is_in_pre_settle_partition():
    data=rows([0,4,5,10],[100000,93728,89664,89316])
    result=transient_memory(data,data[-1]['perf'],[])
    generation=result['transient_generations']['1']
    assert generation['pre_settle_minimum_decrease_bytes']['psram']==10336
    assert generation['minimum_decrease_bytes']['psram']==348
    assert generation['settlement_sample_gap_seconds']==60
    assert generation['settlement_sample_step_bytes']['psram']==4064
    assert 10336+348==100000-89316


def test_one_unsettled_generation_makes_transient_gate_unknown():
    data=rows([0,5,10,15],[100000]*4)+rows([16,17],[90000,89000],generation=2)
    for row in data[-2:]:row['ready_elapsed_s']=16*60
    result=transient_memory(data,data[-1]['perf'],[])
    assert result['transient_generations']['1']['passed'] is True
    assert result['transient_generations']['2']['passed'] is None
    assert result['heap_transient_depth_passed'] is None
    assert result['unsettled_generations']==['2']


def test_only_model_calls_are_verdicts_including_legacy_evidence():
    event=dict(event='smartturn-inference',verdict='INCOMPLETE',probability=.1,inference_time_ms=50)
    row=dict(prompt='pause',passed=False,checks={},failures=[],events=[
        dict(event,kind='model',model_metrics_available=True),
        dict(event,kind='stop_secs_timeout',verdict=None,model_metrics_available=False),
        dict(event,model_metrics_available=False),dict(event,model_metrics_available=True)])
    result=details([row])
    assert result['smartturn']['INCOMPLETE']['count']==2
    assert result['smartturn_timeout_calls']==2


def test_pause_sweep_never_selects_a_policy_even_when_all_one_second_turns_pass():
    from step8d3_pause import definitions,decision
    turns=[]
    for level in (65,58):
        for fixture in definitions():
            for bound in (1,3):
                turns.append(dict(spl=level,fixture=fixture['id'],bound=bound,
                    pause=dict(correct_audible_reply=True,continuation_dropped_by_mute=False)))
    result=decision(turns,1)
    assert result['informational_only'] and not result['automated_selection_allowed']
    assert result['production_stop_secs']==3 and 'winner' not in result
    turns[0]['pause']['continuation_dropped_by_mute']=None
    assert decision(turns,1)['production_stop_secs']==3
    turns[0]['pause']['continuation_dropped_by_mute']=True
    assert decision(turns,1)['production_stop_secs']==3
    assert not decision(turns[:-1],1)['complete']


def test_pause_first_fragment_cancel_and_vad_miss_are_distinct():
    from step8d3_pause import pause_observation
    row=dict(prompt_start=0,events=[dict(event='user-turn-stopped',turn=1,text='What is the capital',t=1.),
        dict(event='llm-start',turn=1,response_id=1,t=1.1),dict(event='llm-cancelled',response_id=1,t=2.1),
        dict(event='final-transcript',text='of France?',t=2.2)],checks={},tts_segments=[])
    r=pause_observation(row,dict(continuation_offset_s=2,continuation_duration_s=.5))
    assert r['first_fragment_started_generation'] and r['cancelled_before_first_tts_byte']
    assert r['vad_missed_continuation_onset'] and r['continuation_dropped_by_mute'] is False
    row['events'].append(dict(event='first-tts-byte',response_id=1,t=1.7))
    row['mute_evidence']=dict(bot_intervals=[(1.8,3)])
    r=pause_observation(row,dict(continuation_offset_s=2,continuation_duration_s=.5))
    assert r['continuation_dropped_by_mute'] is True
    assert not r['cancelled_before_first_tts_byte']


def test_reanalysis_removes_old_whole_turn_transport_label():
    from step8d2_analysis import restore_reply_window_classification
    row=dict(reply_window_transport_damaged=False,transport_damaged=True,
             failures=[dict(cause='transport',underlying_cause='acoustic/transport')])
    restore_reply_window_classification(row)
    assert row['failures']==[dict(cause='acoustic/transport')]


def test_loud_mixer_has_explicit_readback_and_preserves_microphone(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import alsa
    calls=[]
    def command(args,**kwargs):
        calls.append(args)
        control=args[4]
        text=('Mono: Playback 11 [100%] [8.00dB] [on]' if control=='PCM'
              else 'Mono: Capture 6 [86%] [3.00dB] [on]')
        return SimpleNamespace(stdout=text)
    monkeypatch.setattr(alsa.subprocess,'run',command)
    alsa.set_volumes('USB',tmp_path/'mixer.log',pcm_percent=100)
    assert ['amixer','-c','USB','sset','PCM','100%'] in calls
    assert ['amixer','-c','USB','sset','Mic','80%'] in calls


def test_immutable_pause_bank_has_exact_zero_gaps_and_full_text_validation():
    import hashlib,json
    from step8d3_pause import definitions
    from esp_serial import ROOT
    from step7_support import pcm
    from speech_accuracy import speech_accuracy
    bank=ROOT/'hil/fixtures/agent_pause'
    manifest=json.loads((bank/'manifest.json').read_text())
    assert len(manifest['turns'])==8
    for fixture,expected in zip(manifest['turns'],definitions()):
        assert all(fixture[k]==v for k,v in expected.items())
        path=bank/fixture['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==fixture['sha256']
        assert fixture['wer']==0 and speech_accuracy(expected['text'],fixture['transcript'])==1
        end=round(fixture['continuation_offset_s']*16000)
        count=round(expected['pause_s']*16000)
        assert fixture['digital_silence_samples']==count
        assert np.all(pcm(path)[end-count:end]==0)


def test_real_recorded_full_reply_cannot_be_measured_as_short_cancelled_prefix():
    import hashlib,json
    from esp_serial import ROOT
    from agent_conversation_test import cancelled_segment,one_reply_matches
    bank=ROOT/'hil/fixtures/cancelled_prefix'
    manifest=json.loads((bank/'manifest.json').read_text())
    for name,digest in manifest['hashes'].items():
        assert hashlib.sha256((bank/name).read_bytes()).hexdigest()==digest
    assert manifest['known_audible_ms']>1000
    model=json.loads((bank/'response-model.json').read_text())
    measured=cancelled_segment(bank/'full-audible.wav',bank/'reference.wav',
        manifest['sent_at'],manifest['ready_at'],manifest['prompt_start'],model,[])
    # A genuine >1 s recording must yield >=300 ms or unknown, never credit
    # a short cancellation from an accidentally matching early prefix.
    assert measured['audible_ms'] is None or measured['audible_ms']>=300
    events=[dict(event='llm-start',response_id=1),dict(event='first-tts-byte',response_id=1),
            dict(event='llm-cancelled',response_id=1),dict(event='llm-start',response_id=2),
            dict(event='llm-response',response_id=2)]
    segments=[dict(measured,response_id=1),dict(response_id=2,verified=True,complete=True)]
    assert not one_reply_matches(events,segments)


def test_bot_launch_preserves_wrapper_ice_evidence_paths(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import step7_support as support
    monkeypatch.setenv('BOT_ICE_INTERFACE','enp7s0')
    rtp=str(tmp_path/'wrapper-rtp');debug=str(tmp_path/'wrapper-ice.log')
    monkeypatch.setenv('RTP_EVIDENCE_DIR',rtp)
    monkeypatch.setenv('BOT_ICE_DEBUG_PATH',debug)
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def connect_ex(self,address):return 1
    class Process:
        def poll(self):return None
        def terminate(self):pass
        def wait(self,**kwargs):pass
    launched=[]
    def launch(command,**kwargs):
        launched.append(kwargs['env']);return Process()
    monkeypatch.setattr(support.socket,'socket',Socket)
    monkeypatch.setattr(support.subprocess,'Popen',launch)
    monkeypatch.setattr(support.urllib.request,'urlopen',lambda *a,**k:SimpleNamespace(status=200))
    with support.BotServer(tmp_path/'bot'):
        assert launched[0]['RTP_EVIDENCE_DIR']==rtp
        assert launched[0]['BOT_ICE_DEBUG_PATH']==debug


def test_soak_checks_keeps_unsettled_generation_unknown_and_cannot_pass():
    from agent_conversation_test import soak_checks
    data=rows([0,5,10,15],[100000]*4)+rows([16,17],[90000,89000],generation=2)
    for row in data[-2:]:row['ready_elapsed_s']=16*60
    for row in data:
        row['perf'].update(decode_errors=0,i2s_underflows=0,tasks=[dict(name='worker',stack_margin_percent=50)],
            tx_stack=5000,tx_stack_bytes=8192,owner_stack=5000,owner_stack_bytes=8192,
            rx_stack=5000,rx_stack_bytes=8192,decode_stack=10000,playback_stack_bytes=17408)
    result=dict(observations=data,closed=data[-1]['perf'],elapsed_s=17*60,
        fault_free=True,faults=[],turns=[dict(passed=True)]*51)
    checks=soak_checks(result,17)
    assert checks['heap_transient_depth'] is None
    assert result['unsettled_generations']==['2']
    assert not all(checks.values())
