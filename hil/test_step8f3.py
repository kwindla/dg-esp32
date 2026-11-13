"""Fable 8f2 missing regressions, with retained real TTS and no hardware."""
import io
import json
import threading
from collections import deque
from pathlib import Path
import numpy as np
import pytest
from playback_quality import decoded_match,jabra_level_gate
from step7_support import pcm
from step8e2b_analysis import link_backpressure_windows,owner_gate,device_send_cadence
from uplink_continuity import audio_idle_windows,link_tail,recorded_audio_idle_windows
from netem import impairment_flow_gate,assert_selected_interface


def test_real_reference_with_400ms_dither_pause_passes():
    source=pcm(Path(__file__).parent/'fixtures/step8f3-tts-reference.wav')
    cut=16000;pause=np.random.default_rng(8).normal(0,1,6400)
    reference=np.r_[source[:cut],pause,source[cut:]]
    decoded=np.r_[np.zeros(500),source[:cut],np.zeros(6400),source[cut:],np.zeros(500)]
    gate=decoded_match(decoded,reference)
    assert gate['passed'] and gate['correlation']>=.95
    assert gate['quiet_blocks_excluded']>=3
    assert all(abs(r['offset_samples']-500)<=1 for r in gate['blocks'])


def test_counter_bracket_cannot_excuse_unrelated_half_second_owner_stall():
    samples=[dict(device_us=0,generation=1,peer_profile=dict(socket_send_enomem=0)),
             dict(device_us=30000000,generation=1,peer_profile=dict(socket_send_enomem=1))]
    slow=[dict(kind=8,device_us=20000000,value=500000)]
    assert not link_backpressure_windows(snapshots=samples)
    drop=dict(kind=7,flags=1,value=10,reserved=1,device_us=1000000)
    windows=link_backpressure_windows([drop],samples)
    assert len(windows)==1 and windows[0]['end_us']-windows[0]['start_us']==500000
    gate=owner_gate(slow,1000,trace=[drop],snapshots=samples)
    assert not gate['passed'] and gate['maximum_ms']==500
    assert gate['link_backpressure']['excused_span_s']==0
    for change in (dict(value=9),dict(flags=0),dict(reserved=2)):
        assert not link_backpressure_windows([dict(drop,**change)],samples)


def test_memory_schedule_rejects_20minute_reconnect_without_tail():
    from agent_conversation_test import soak_schedule
    with pytest.raises(AssertionError,match='Pooled heap growth'):
        soak_schedule(20,True,0)
    assert soak_schedule(20,True,180)==((360,'reconnect'),)
    assert soak_schedule(23,True,0)==((360,'reconnect'),)


def test_audio_acknowledged_between_turn_pause_is_idle():
    states=[dict(enabled=0,host_s=10,device_us=10000000),dict(enabled=1,host_s=20,device_us=20000000)]
    packets=[dict(pc_id='one',ssrc=2,seq=i,timestamp=i*960,observed_ms=t)
             for i,t in enumerate((9980,10000,20000,20020,20040))]
    gate=link_tail(packets,9.98,20.04,startup_seconds=0,excluded_windows=audio_idle_windows(states))
    assert gate['passed'] and gate['categories']['idle']['maximum_ms']==10000
    assert gate['eligible_maximum_ms']==20
    assert not link_tail(packets,9.98,20.04,startup_seconds=0)['passed']
    sends=[dict(kind=12,reserved=1,ssrc=2,seq=r['seq'],device_us=r['observed_ms']*1000) for r in packets]
    cadence=device_send_cadence(sends,9000000,20040000,audio_idle_windows(states,clock='device_us'))
    assert cadence['hard_ceiling_passed'] and cadence['idle']['count']==3
    assert cadence['raw_gaps_ms']['maximum']==10000


def test_serial_reader_preserves_raw_bytes_and_records_device_audio_ack(tmp_path):
    from esp_serial import EspSerial
    board=EspSerial.__new__(EspSerial)
    board._cv=threading.Condition();board._cursor=0;board._lines=deque();board._error=None
    board._stop=threading.Event();board._log=io.BytesIO()
    path=tmp_path/'serial.log';board._audio_log=path.with_suffix('.audio.jsonl').open('w')
    chunks=iter([b'AUDIO_STATE enabled=0 device_us=100',b'00000\nOK #1 audio\n',b'AUDIO_STATE enabled=1 device_us=20000000\n'])
    class Serial:
        in_waiting=1
        def read(self,n):
            chunk=next(chunks,b'')
            if not chunk:board._stop.set()
            return chunk
    board.s=Serial();board._reader();board._audio_log.close()
    assert board._error is None and b'\nOK #1 audio\n' in board._log.getvalue()
    assert recorded_audio_idle_windows(path,clock='device_us')==[dict(start_us=10000000,end_us=20000000,category='idle')]
    assert len(recorded_audio_idle_windows(path))==1


@pytest.mark.parametrize('direction,keys',[('both',('tx','rx')),('downlink',('tx',)),('uplink',('rx',))])
def test_owned_byte_floor_rejects_connectivity_checks_and_checks_rtp_fraction(direction,keys):
    before=dict(tx=0,rx=0);tiny=dict(tx=100,rx=100)
    assert not impairment_flow_gate(before,tiny,direction,session_seconds=60)['valid']
    assert impairment_flow_gate(before,dict(tx=100000,rx=100000),direction,session_seconds=60)['valid']
    rtp={key:1000000 for key in keys}
    assert not impairment_flow_gate(before,dict(tx=499999,rx=499999),direction,rtp_bytes=rtp)['valid']
    assert impairment_flow_gate(before,dict(tx=500000,rx=500000),direction,rtp_bytes=rtp)['valid']
    assert not impairment_flow_gate(before,tiny,direction)['valid']


def test_wrong_or_missing_selected_ice_pair_fails_before_impairment(tmp_path):
    path=tmp_path/'ice-pairs.jsonl'
    row=dict(pc_id='current',interface='enp7s0',pairs=[dict(local=['192.168.42.75',9000])],selected_interfaces=['enp7s0'])
    path.write_text(json.dumps(row)+'\n')
    with pytest.raises(RuntimeError,match='impaired interface'):
        assert_selected_interface(tmp_path,'current','wlp8s0',timeout=0)
    with pytest.raises(RuntimeError,match='Missing selected ICE pair'):
        assert_selected_interface(tmp_path,'new-session','wlp8s0',timeout=0)
    row['interface']='wlp8s0';path.write_text(json.dumps(row)+'\n')
    with pytest.raises(RuntimeError,match='impaired interface'):
        assert_selected_interface(tmp_path,'current','wlp8s0',timeout=0)
    row['selected_interfaces']=['wlp8s0'];path.write_text(json.dumps(row)+'\n')
    assert assert_selected_interface(tmp_path,'current','wlp8s0',timeout=0)==row


def test_predeclared_minus22_level_window_is_71_to_85():
    model=dict(chains={name:dict(fir=[1]) for name in ('board','jabra')})
    gate=jabra_level_gate(np.ones(16000)*1000,20,model,target_dbfs=-22)
    assert gate['spl_window_db']==[71,85]


def test_netem_restores_when_owned_bytes_are_below_recorded_rtp_floor(monkeypatch,tmp_path):
    import netem
    original={d:dict(qdisc=[dict(root=True,handle='0:',kind='noqueue')],filter=[],**{'class':[]}) for d in ('wlp8s0','ifb0')}
    restored=[];owned=[0];now=[0]
    monkeypatch.setattr(netem,'snapshot',lambda *a:original)
    monkeypatch.setattr(netem,'restore',lambda *a:restored.append(True))
    monkeypatch.setattr(netem.fcntl,'flock',lambda *a:None)
    monkeypatch.setattr(netem.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(netem,'command',lambda *a,**k:json.dumps([dict(handle='8e03:',parent='8e01:3',bytes=owned[0])]))
    rtp=tmp_path/'rtp';rtp.mkdir();state=tmp_path/'tc.json'
    with pytest.raises(RuntimeError,match='Invalid impairment arm'):
        with netem.Netem('192.168.4.29',['loss','3%'],'both',state=state,rtp_directory=rtp):
            for name in ('downlink.jsonl','socket-rtp.jsonl'):
                (rtp/name).write_text(json.dumps(dict(rtp_bytes=1000000))+'\n')
            owned[0]=499999;now[0]=60
    report=json.loads(state.read_text())
    assert restored and report['status']=='restored'
    assert report['impairment_flow']['minimum_board_bytes']==dict(tx=500000,rx=500000)
    assert report['impairment_flow']['valid'] is False
