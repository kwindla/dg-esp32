import numpy as np
import pytest
from playback_quality import transport_gate,TRANSPORT_COUNTERS,acoustic_level_gate,classify_turn

def test_short_realistic_concealment_is_excluded_even_with_good_correlation():
    before=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS});after=dict(before,lost=10,concealed=10,plc_frames=10)
    source=np.sin(np.arange(48000)*2*np.pi*1000/16000)
    concealed=source.copy();concealed[16000:19200]*=.8
    assert np.corrcoef(source,concealed)[0,1]>.99
    gate=transport_gate(before,after)
    assert not gate['passed'] and gate['classification']=='transport-damaged'
    row=dict(before_perf=before,after_perf=after,checks=dict(acoustic_content=True))
    classify_turn(row);assert not row['acoustic_scoring_eligible']

def test_transport_unknown_and_counter_reset_fail_closed():
    assert not transport_gate({}, {})['passed']
    assert not transport_gate({k:10 for k in TRANSPORT_COUNTERS},dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS}))['passed']

@pytest.mark.parametrize('error,passed',[(0,True),(2,True),(-2,True),(2.01,False),(-2.01,False),(6,False)])
def test_level_gate_detects_scale_invariant_quiet_failure(error,passed):
    assert acoustic_level_gate(8+error,8)['passed'] is passed

def test_reply_bracket_takes_precedence_over_unrelated_whole_turn_loss():
    clean=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS});loss=dict(clean,lost=39,concealed=39,plc_frames=39)
    row=dict(before_perf=clean,after_perf=loss,reply_window_transport=dict(before=loss,after=loss))
    assert classify_turn(row)['passed']

def test_direct_cpu_counter_uses_frame_and_device_deltas():
    from step8b3_cpu_pair import direct_cost
    phase=dict(before=dict(playback_cost=dict(frames=100,total_us=20000),perf=dict(device_us=1000000)),
        after=dict(playback_cost=dict(frames=600,total_us=120000,max_us=300),perf=dict(device_us=11000000)))
    result=direct_cost(phase)
    assert result['frames']==500 and result['mean_us']==200 and result['dsp_cpu_percent']==1
    phase['before']['playback_cost']=None
    with pytest.raises(RuntimeError,match='unavailable'):direct_cost(phase)

def test_counter_equality_cannot_hide_session_change():
    clean=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS})
    assert transport_gate(dict(clean,pc_id='one',generation=1),dict(clean,pc_id='two',generation=2))['classification']=='session-changed'


def test_jabra_only_gates_level_and_absolute_transferred_window():
    from playback_quality import jabra_level_gate
    model=dict(chains={c:dict(fir=[1.]) for c in ('board','jabra')})
    # -48 dBFS maps to nominal 72 dB SPL at the board microphone.
    audio=np.full(16000,32768*10**(-48/20))
    assert jabra_level_gate(audio,10,model)['passed']
    assert not jabra_level_gate(audio,2.99,model)['passed']
    assert not jabra_level_gate(audio*.1,20,model)['passed']
    assert not jabra_level_gate(audio*10,20,model)['passed']


def test_decoded_pcm_requires_content_and_complete_reference():
    from playback_quality import decoded_match
    rng=np.random.default_rng(87);source=rng.normal(0,2000,16000)
    assert decoded_match(np.r_[np.zeros(800),source,np.zeros(800)],source)['passed']
    assert not decoded_match(source[:8000],source)['passed']
    assert not decoded_match(rng.normal(0,2000,32000),source)['passed']


def test_rescore_credits_board_content_when_jabra_correlation_fails(tmp_path,monkeypatch):
    import json
    import step8b3_rescore as rescore
    from step7_support import write_pcm
    from agent_conversation_test import CHECKS
    campaign=tmp_path/'original';turn=campaign/'turn1';turn.mkdir(parents=True)
    out=tmp_path/'corrected';out.mkdir()
    source=turn/'source.wav';audio=32768*10**(-48/20)*np.sqrt(2)*np.sin(np.arange(16000)*2*np.pi*1000/16000)
    for name in ('source.wav','turn.wav','turn-board.wav'):write_pcm(turn/name,audio)
    clean=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS});before=dict(clean,received=0,decode_frames=0,pulled_frames=0)
    after=dict(clean,received=50,decode_frames=50,pulled_frames=50)
    segment=dict(source_path=str(source),response_id=1,cancelled=False,verified=False,complete=False,
        correlation=.1,offset_s=0,rms_rise_db=20,onset=1.,end=2.,board=dict(correlation=.98,offset_s=0))
    row=dict(prompt='france',passed=False,checks={k:True for k in CHECKS},failures=[],
        before_perf=before,after_perf=after,events=[dict(event='final-transcript',text='France'),dict(event='llm-start',response_id=1),dict(event='llm-response',response_id=1)],
        tts_segments=[segment],gate_events=[(.95,'closed'),(2.1,'open')],leakage=dict(maximum_post_dbfs=-120),acoustic_text='Paris',jabra_transcript_informational='Parrot')
    row['checks'].update(playback=False,one_reply=False,gate=False,acoustic_content=False)
    original=turn/'result.json';original.write_text(json.dumps(row));saved=original.read_bytes()
    model=dict(chains={c:dict(fir=[1.]) for c in ('board','jabra')})
    (campaign/'result.json').write_text(json.dumps(dict(response_calibration=model,turns=[row])))
    monkeypatch.setattr(rescore,'transcribe_acoustic',lambda path:'Paris')
    rescore.rescore_agent(campaign,out)
    corrected=json.loads((out/'result.json').read_text())['turns'][0]
    assert original.read_bytes()==saved
    assert corrected['passed'] and all(corrected['checks'].values())
    assert corrected['original_passed'] is False
    assert corrected['jabra_transcript_informational']=='Parrot'
    assert corrected['decoded_pcm_proof'].startswith('unavailable')


def test_complete_reply_keeps_low_energy_final_consonant_and_rejects_truncation(tmp_path):
    from agent_conversation_test import reference_audio
    from step7_support import write_pcm,pcm
    source=tmp_path/'reply.wav'
    x=np.sin(np.arange(16000)*2*np.pi*4000/16000)*6000
    x[-4000:]*=.03  # Quiet unvoiced tail below the timing activity threshold.
    write_pcm(source,x);x=pcm(source)
    recording=np.r_[np.zeros(1600),x,np.zeros(1600)]
    part=dict(source_path=str(source),offset_s=.1,board=dict(offset_s=.1),end=.85)
    for chain in ('board','jabra'):
        assert np.array_equal(reference_audio(recording,[part],chain),x)
        with pytest.raises(AssertionError,match='Truncated'):
            reference_audio(recording[:15000],[part],chain)


@pytest.mark.parametrize('target,center',[(-25,73),(-22,78),(-19,79)])
@pytest.mark.parametrize('fault_db',[-10,-8,8,10])
def test_reply_level_rejects_eight_to_ten_db_faults_in_both_directions(target,center,fault_db):
    from playback_quality import jabra_level_gate
    model=dict(chains={c:dict(fir=[1.]) for c in ('board','jabra')})
    audio=np.full(16000,32768*10**((center-120)/20))
    assert jabra_level_gate(audio,20,model,target_dbfs=target)['passed']
    assert not jabra_level_gate(audio*10**(fault_db/20),20+fault_db,model,target_dbfs=target)['passed']


def test_contaminated_control_cannot_produce_a_passing_sweep_level_gate():
    gate=acoustic_level_gate(8,8,control_room_rise_passed=False)
    assert not gate['passed'] and not gate['eligible']
    assert 'room-rise' in gate['reason']


def test_production_reply_level_defaults_to_user_selected_minus22():
    from playback_quality import jabra_level_gate
    model=dict(chains={c:dict(fir=[1.]) for c in ('board','jabra')})
    audio=np.full(16000,32768*10**((78-120)/20))
    for kwargs in ({},dict(target_dbfs=None)):
        gate=jabra_level_gate(audio,20,model,**kwargs)
        assert gate['passed'] and gate['target_dbfs']==-22
        assert gate['spl_window_db']==[71,85]
        assert not jabra_level_gate(audio*10**(-8/20),12,model,**kwargs)['passed']

@pytest.mark.parametrize('version',[None,1,3])
def test_complete_zero_counters_with_unknown_schema_remain_unavailable(version):
    clean={k:0 for k in TRANSPORT_COUNTERS}
    if version is not None:clean['playout_schema']=version
    result=transport_gate(clean,clean)
    assert not result['passed'] and not result['available']
    assert result['classification']=='transport-unavailable'
    assert all(v is None for v in result['deltas'].values())

@pytest.mark.parametrize('counter',['i2s_write_errors','i2s_underflows'])
def test_reply_transport_includes_i2s_failures(counter):
    before=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS})
    after=dict(before);after[counter]=1
    assert not transport_gate(before,after)['passed']
    del after[counter]
    assert not transport_gate(before,after)['available']

@pytest.mark.parametrize('level,expected',[(-22,True),(-24.99,True),(-19.01,True),(-25.01,False),(-18.99,False)])
def test_echoear_jabra_reply_gate_measures_direct_rms(level,expected):
    from playback_quality import jabra_level_gate
    samples=np.full(1600,32768*10**(level/20))
    gate=jabra_level_gate(samples,20,{},board='echoear')
    assert gate['passed'] is expected and gate['available']
    assert gate['board_mic_tone_transfer_spl'] is None and gate['spl_window_db'] is None
    assert gate['target_dbfs']==-22
    assert not jabra_level_gate(samples,9.99,{},board='echoear')['passed']

def test_echoear_missing_reply_cannot_use_unavailable_diagnostic_bypass():
    from playback_quality import jabra_level_gate
    gate=jabra_level_gate([],None,{},board='echoear')
    assert gate['available'] and not gate['passed']

@pytest.mark.parametrize('before,after,passed',[(0,0,True),(4,4,True),(0,1,False),(1,0,False),(None,0,False),(0,None,False)])
def test_in_turn_microphone_clip_gate_fails_missing_reset_and_nonzero(before,after,passed):
    from playback_quality import microphone_clip_gate
    assert microphone_clip_gate(dict(mic_in_turn_clips=before,mic_gate_open_clips=before,mic_gate_closed_clips=0),dict(mic_in_turn_clips=after,mic_gate_open_clips=after,mic_gate_closed_clips=0))['passed'] is passed

def test_microphone_clip_gate_rejects_different_session():
    from playback_quality import microphone_clip_gate
    counters=dict(mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0)
    assert not microphone_clip_gate(dict(counters,pc_id='a'),dict(counters,pc_id='b'))['passed']

@pytest.mark.parametrize('volume',[0,-6])
def test_echoear_measured_passing_reply_requires_reference_volume_max(volume):
    from playback_quality import jabra_level_gate
    # Post-isolation dma-live-1: measured RMS -24.335377 dBFS, rise36.351724 dB.
    audio=np.full(16000,32768*10**(-24.335377282295344/20))
    gate=jabra_level_gate(audio,36.35172370854939,{},board='echoear',volume_relative_db=volume)
    assert gate['passed'] is (volume==0)
    assert gate['available'] and gate['target_dbfs']==-22
    assert gate['rms_window_db']==[-25,-19]
    assert gate['board_mic_tone_transfer_spl'] is None and gate['spl_window_db'] is None
    assert gate['volume_relative_db']==volume


@pytest.mark.parametrize('opened,closed',[(1,0),(0,1),(2,3)])
def test_microphone_clips_in_either_gate_state_fail(opened,closed):
    from playback_quality import microphone_clip_gate
    before=dict(mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0)
    after=dict(mic_in_turn_clips=opened+closed,mic_gate_open_clips=opened,mic_gate_closed_clips=closed)
    result=microphone_clip_gate(before,after)
    assert result['available'] and result['consistent'] and not result['passed']
    assert result['gate_open_delta']==opened and result['gate_closed_delta']==closed

@pytest.mark.parametrize('key',['mic_gate_open_clips','mic_gate_closed_clips'])
def test_microphone_gate_split_is_required_and_monotonic(key):
    from playback_quality import microphone_clip_gate
    before=dict(mic_in_turn_clips=4,mic_gate_open_clips=2,mic_gate_closed_clips=2)
    after=dict(before);del after[key]
    assert not microphone_clip_gate(before,after)['available']
    after=dict(before);after[key]-=1
    assert not microphone_clip_gate(before,after)['passed']

def test_microphone_gate_split_must_match_total():
    from playback_quality import microphone_clip_gate
    before=dict(mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0)
    after=dict(before,mic_in_turn_clips=1)
    assert not microphone_clip_gate(before,after)['consistent']
