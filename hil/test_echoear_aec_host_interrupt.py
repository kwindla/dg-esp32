import json
import pytest
from echoear_aec_host_interrupt import cell,validate,issue
from echoear_aec_campaign import protocol
from echoear_aec_campaign_analysis import host_interrupt_measure,turn_events


def test_declared_host_cell_protocol_and_number(tmp_path):
    c=cell('host-01','Long old reply.','Next reply.')
    p=protocol([c]);assert len(p['interrupt_ids'])==3
    assert p['host_interrupts']['host-01-a1']==dict(next_text='Next reply.',next_delay_s=1.)
    assert not p['real_bot_ids'] and not p['listen_ids']
    path=tmp_path/'command.json';timing=issue(path,7,'host-01-a1')
    cmd=json.loads(path.read_text())
    assert cmd['op']=='interrupt' and cmd['number']==7
    assert cmd['host_issued_s']==timing['request_monotonic_s']<=timing['write_complete_s']
    with pytest.raises(ValueError):validate(dict(c,fixture='stop.wav'))


def test_missing_timed_interruption_never_scores_as_success():
    assert not host_interrupt_measure(dict(events=[]),{},None,None,None,None,'',[],{})['passed']


def test_profile_neutral_turn_fields():
    events=[dict(event='user-turn-started',transcription_start=True),
            dict(event='frame',frame='UserStartedSpeakingFrame'),
            dict(event='frame',frame='InterruptionFrame'),
            dict(event='frame',frame='BotStartedSpeakingFrame',t=1)]
    assert turn_events(events)==dict(vad_starts=0,transcription_starts=1,accepted_turns=1,interruptions=1,bot_state=[dict(t=1,active=True)])


def test_timer_invokes_reserved_host_command_without_ack_wait(tmp_path):
    import time
    from echoear_aec_stimulus import schedule_from_tts
    now=time.monotonic_ns();path=tmp_path/'events.jsonl'
    path.write_text(json.dumps(dict(kind='tts-transport-write',non_silent=True,monotonic_ns=now))+'\n')
    target=tmp_path/'command.json'
    timer=schedule_from_tts(lambda:[path],now,0,lambda:issue(target,3,'host-a1'),require_active=True)
    assert timer['timing_passed'] and not timer['skipped']
    assert json.loads(target.read_text())['op']=='interrupt'
    assert timer['jabra']['number']==3  # Generic timer callback-result slot.


def test_host_chain_does_not_borrow_stt_or_unapplied_flush(monkeypatch):
    import echoear_aec_campaign_analysis as a
    c=dict(host_interrupt_offset_s=1,host_interrupt_timer=dict(command=dict(request_monotonic_s=1,number=2),timing_passed=True,requested_offset_s=1,active_tts_witness=dict(active=True)),events=[
        dict(event='script-interrupt',t=1.01,host_issued_s=1,number=2,frame_id=42),dict(event='frame',frame='InterruptionFrame',t=1.02,frame_id=42),
        dict(event='host-next-reply-queued',t=2.01)])
    flush=dict(passed=True,callbacks=[dict(host_interval=[1.03,1.04],applied=True)],last_mic3_host_interval=[1.05,1.06])
    monkeypatch.setattr(a,'flush_measure',lambda *args:flush)
    monkeypatch.setattr(a,'sample_interval',lambda *args:[2.1,2.12])
    playback=[dict(passed=True),dict(passed=True,write_s=2.05,speech_onset_sample=32000)]
    r=a.host_interrupt_measure(c,{},None,None,None,None,'',playback,{})
    assert r['passed'] and not r['recognition_credit']
    assert r['callback_to_next_mic3']['lower_s']==pytest.approx(1.06)
    flush['callbacks'][0]['applied']=False
    assert not a.host_interrupt_measure(c,{},None,None,None,None,'',playback,{})['passed']
    c['events'].append(dict(event='frame',frame='UserStartedSpeakingFrame',t=1.015))
    assert not a.host_interrupt_measure(c,{},None,None,None,None,'',playback,{})['passed']


def test_fixtureless_host_cell_overlap_is_unscorable_not_keyerror(tmp_path):
    from echoear_aec_campaign_analysis import near_end_interval,overlap_bounds
    c=cell('nova-host-interrupt-01','Long old reply.','Next reply.');assert 'fixture' not in c
    assert near_end_interval(c,{},None,None) is None
    assert overlap_bounds(None,near_end_interval(c,{},None,None),[1.,1.1])==dict(scorable=False,reason='Missing independent overlap endpoint')
    with pytest.raises(FileNotFoundError):near_end_interval(dict(c,fixture=str(tmp_path/'missing.wav')),{},None,None)
