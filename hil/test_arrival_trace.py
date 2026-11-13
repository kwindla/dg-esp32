import base64
import pytest
from arrival_trace import RECORD,decode_line,sequence_gaps,attribute,generation_sessions

def row(seq,time=0,kind=1,stamp=None):
    return dict(device_us=time,timestamp=stamp if stamp is not None else seq*960,
        ssrc=7,value=0,aux=0,seq=seq,bytes=24,kind=kind,flags=0,reserved=0)

def test_inter_snapshot_retry_is_identified_by_unique_rtp_source():
    rows=[dict(row(1),reserved=1),dict(row(2,kind=2),reserved=3,ssrc=9),
        dict(row(3,kind=4),reserved=3)]
    server=[dict(pc_id='observed',ssrc=7),dict(pc_id='retry',ssrc=9)]
    mapped,basis=generation_sessions(rows,server,{'observed':1})
    assert mapped=={1:'observed',3:'retry'}
    assert basis[3]=='Unique authenticated RTP SSRC/server session match'

@pytest.mark.parametrize('server',[[],[dict(pc_id='one',ssrc=7),dict(pc_id='two',ssrc=7)]])
def test_unmapped_or_ambiguous_retry_cannot_drop_playout_events(server):
    with pytest.raises(ValueError,match='Unmapped or ambiguous'):
        generation_sessions([row(1,kind=2),row(2,kind=4)],server,{})

def test_socket_packet_alone_cannot_identify_a_retry_session():
    with pytest.raises(ValueError,match='Unmapped or ambiguous'):
        generation_sessions([row(1),row(2,kind=4)],[dict(pc_id='old',ssrc=7)],{})

def test_binary_record_and_truncation():
    r=row(3,120000)
    encoded=base64.b64encode(RECORD.pack(*r.values())).decode()
    assert decode_line(encoded)==[r]
    with pytest.raises(ValueError):decode_line(base64.b64encode(b'partial').decode())

def test_wrap_reorder_duplicates_are_not_loss():
    rows=[row(65534),row(0),row(65535),row(0),row(1)]
    assert not sequence_gaps(rows)
    rows.append(row(3))
    assert sequence_gaps(rows)[0]['first_missing']==2

def test_boundary_loss_and_stall_are_distinct():
    rows=[row(0,0),row(1,20000),row(2,150000),row(3,151000)]
    rows+=[row(0,0,2),row(1,20000,2),row(2,150000,2),row(3,151000,2)]
    rows.append(row(0,100000,4))
    server=[dict(ssrc=7,seq=i,timestamp=i*960,send_ms=i*20) for i in range(4)]
    result=attribute(rows,server)
    assert not result['wire_sequence_gaps']
    assert not result['wire_not_authenticated']
    assert result['events'][0]['attribution']=='arrival stall'
    assert result['arrival_stalls'][0]['server_send_gap_ms']==20

def test_server_gap_is_not_attributed_to_radio():
    rows=[row(0,0),row(1,200000)]
    server=[dict(ssrc=7,seq=i,timestamp=i*960,send_ms=i*200) for i in range(2)]
    assert attribute(rows,server)['arrival_stalls'][0]['source']=='server pacing'

def test_authenticated_frame_missing_native_callback_is_localized():
    rows=[row(1,0),row(1,100,2),dict(row(1,200,3),value=20),
          row(2,20000),row(2,20100,2),dict(row(2,40000,4),value=40)]
    result=attribute(rows)
    assert result['native_pts_calibration']['exact']
    assert result['events'][0]['attribution']=='native cache / callback scheduling: authenticated before PLC decision'

def test_packet_read_after_plc_deadline_is_a_late_arrival():
    rows=[row(1,0),row(1,100,2),dict(row(1,200,3),value=20),
          dict(row(2,40000,4),value=40),row(2,60000),row(2,60100,2),dict(row(2,60200,3),value=40)]
    result=attribute(rows)
    assert result['events'][0]['attribution']=='late arrival at socket boundary'

def test_native_timestamp_calibration_survives_rtp_wrap():
    rows=[]
    for seq,stamp in [(1,2**32-960),(2,0),(3,960)]:
        rows.extend([row(seq,seq*20000,stamp=stamp),row(seq,seq*20000+100,2,stamp)])
        if seq<3:
            rows.append(dict(row(seq,seq*20000+200,3),value=(2**32+(seq-2)*960)//48))
    rows.append(dict(row(3,80000,4),value=(2**32+960)//48))
    result=attribute(rows)
    assert result['native_pts_calibration']['exact']
    assert result['events'][0]['attribution']=='native cache / callback scheduling: authenticated before PLC decision'

def test_callback_does_not_prove_consumer_delivery_after_queue_rejection():
    rows=[row(1,0),row(1,100,2),dict(row(1,200,3),value=20),
          dict(row(0,201,7),flags=1,value=1,aux=20),
          dict(row(0,40000,4),value=20)]
    result=attribute(rows)
    assert result['queue_drops'][0]['reason']=='received_queue'
    assert result['events'][0]['attribution']=='device incoming queue / payload pool rejected frame'

def test_late_wire_duplicate_preserves_first_read_before_plc_decision():
    rows=[row(1,0),row(1,100,2),dict(row(1,200,3),value=20),
          row(2,20000),dict(row(0,40000,4),value=40),
          row(2,60000),row(2,60100,2),dict(row(2,60200,3),value=40)]
    event=attribute(rows)['events'][0]
    assert event['frame_boundary']['wire_us']==20000
    assert event['attribution']=='native receive / authentication: socket read before PLC decision'


def test_cleaned_drop_enum_and_archived_drop_enum_keep_distinct_names():
    from arrival_trace import attribute
    def row(kind,value,flags=0):
        return dict(kind=kind,value=value,flags=flags,reserved=1,timestamp=0,ssrc=0,
                    aux=0,seq=0,bytes=0,device_us=1)
    # Epoch boundary records mark new sessions; old captures retain old IDs.
    current=attribute([row(17,0),row(7,0,1)])
    archived=attribute([row(7,0,1)])
    assert current['queue_drops'][0]['reason']=='invalid_data'
    assert archived['queue_drops'][0]['reason']=='invalid_audio'


def test_admission_drop_details_and_legacy_missing_detail():
    epoch = row(0, kind=17)
    drop = dict(row(0, kind=7), flags=1, value=11, aux=(2<<24)|675, timestamp=3)
    old = dict(drop, aux=0)
    results = attribute([epoch,drop,old])['queue_drops']
    assert results[0]['reason']=='admission_budget'
    assert results[0]['native_frame_type']==2 and results[0]['message_bytes']==675
    assert results[0]['stream_id']==3
    assert 'message_bytes' not in results[1]
