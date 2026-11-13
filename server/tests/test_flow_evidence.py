import json
import struct
from types import SimpleNamespace
import pytest
import flow_evidence as f


def test_board_only_userspace_capture_has_exact_payload_and_bounded_population(monkeypatch,tmp_path):
    monkeypatch.setattr(f,'DIRECTORY',tmp_path);monkeypatch.setattr(f,'LIMIT',1);monkeypatch.setattr(f,'_counts',{})
    protocol=SimpleNamespace(receiver=SimpleNamespace(_dg_pc_id='p'),transport=SimpleNamespace(get_extra_info=lambda _:('192.168.7.222',1234)))
    data=b'\x15\xfe\xfd'+bytes(10)
    f.packet(protocol,data,('192.168.4.30',5000),'receive')
    assert not (tmp_path/'board-flow.pcap').exists()
    f.packet(protocol,data,(f.BOARD,5000),'receive');f.packet(protocol,data,(f.BOARD,5000),'receive')
    raw=(tmp_path/'board-flow.pcap').read_bytes();assert struct.unpack_from('<I',raw,20)[0]==101
    captured=raw[40:];assert captured[28:]==data
    assert struct.unpack_from('!HH',captured,20)==(5000,1234)
    assert json.loads((tmp_path/'endpoint-events.jsonl').read_text())['event']=='flow-truncated'
    assert len((tmp_path/'board-flow.jsonl').read_text().splitlines())==1


def test_capture_failure_preserves_native_receive(monkeypatch,tmp_path):
    monkeypatch.setattr(f,'DIRECTORY',tmp_path);calls=[]
    monkeypatch.setattr(f,'packet',lambda *args:(_ for _ in ()).throw(OSError('disk full')))
    monkeypatch.setattr(f,'_original_receive',lambda *args:calls.append(args))
    protocol=SimpleNamespace(receiver=SimpleNamespace(_dg_pc_id='p'))
    f.observe_receive(protocol,b'packet',(f.BOARD,9));assert len(calls)==1


@pytest.mark.asyncio
async def test_failed_close_flush_retains_error_and_endpoint_evidence(monkeypatch,tmp_path):
    monkeypatch.setattr(f,'DIRECTORY',tmp_path)
    async def fail(_):raise ConnectionError('ICE closed')
    monkeypatch.setattr(f,'_original_write_ssl',fail)
    transport=SimpleNamespace(_dg_pc_id='p',_ssl=SimpleNamespace(get_shutdown=lambda:1),transport=SimpleNamespace(state='closed'))
    with pytest.raises(ConnectionError):await f.observe_write_ssl(transport)
    event=json.loads((tmp_path/'endpoint-events.jsonl').read_text())
    assert event['event']=='dtls-close-flush' and event['succeeded'] is False and event['ice_state']=='closed'


@pytest.mark.asyncio
async def test_actual_dtls_stop_can_complete_without_emission_when_ice_is_closed(monkeypatch,tmp_path):
    from aiortc.rtcdtlstransport import State
    monkeypatch.setattr(f,'DIRECTORY',tmp_path)
    shutdown=[]
    async def closed_ice(_):raise ConnectionError('ICE closed')
    monkeypatch.setattr(f,'_original_write_ssl',closed_ice)
    transport=SimpleNamespace(_dg_pc_id='p',_task=None,_state=State.CONNECTED,state='connected',
        _ssl=SimpleNamespace(shutdown=lambda:shutdown.append(True),get_shutdown=lambda:int(bool(shutdown))),
        transport=SimpleNamespace(state='closed'),_RTCDtlsTransport__tx_packets=0,
        _RTCDtlsTransport__log_debug=lambda *args:None)
    async def flush():return await f.observe_write_ssl(transport)
    transport._write_ssl=flush
    # Calls the pinned aiortc implementation, whose ConnectionError handling
    # allows stop() to finish even though the encrypted alert was not emitted.
    await f.observe_dtls_stop(transport)
    events=[json.loads(line) for line in (tmp_path/'endpoint-events.jsonl').read_text().splitlines()]
    assert shutdown and transport._RTCDtlsTransport__tx_packets==0
    assert events[-1]['event']=='dtls-stop-end' and events[-1]['ssl_shutdown']==1
    assert any(e['event']=='dtls-close-flush' and e['succeeded'] is False for e in events)
