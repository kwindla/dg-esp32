from types import SimpleNamespace
import asyncio
import rtp_evidence
import rtp_evidence as evidence
import json
import pytest
from aiortc.rtp import RtpPacket, RtcpRrPacket, RtcpReceiverInfo


def test_udp_callback_trace_precedes_delivery_and_excludes_control(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, 'DIRECTORY', tmp_path)
    seen = []
    def deliver(protocol, data, addr):
        path = tmp_path/'socket-rtp.jsonl'
        seen.append((data, len(path.read_text().splitlines()) if path.exists() else 0))
    monkeypatch.setattr(evidence, '_original_datagram', deliver)
    protocol = SimpleNamespace(receiver=SimpleNamespace(_dg_pc_id='peer'))
    packet = RtpPacket(payload_type=111, sequence_number=65535, timestamp=960, ssrc=42)
    packet.payload = b'ciphertext'
    for data in (b'\x16'+bytes(20), bytes(RtcpRrPacket(ssrc=42)), packet.serialize()):
        evidence._observe_datagram(protocol, data, ('127.0.0.1', 1234))
    rows = [json.loads(line) for line in (tmp_path/'socket-rtp.jsonl').read_text().splitlines()]
    assert [count for _, count in seen] == [0, 0, 1]
    assert len(rows) == 1 and rows[0]['seq'] == 65535 and rows[0]['ssrc'] == 42
    assert rows[0]['timestamp'] == 960 and rows[0]['boundary'] == 'asyncio UDP callback'
    assert rows[0]['rtp_bytes']==len(packet.serialize())
    assert 'payload' not in rows[0]


def test_udp_trace_cap_and_disk_failure_keep_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, 'DIRECTORY', tmp_path)
    monkeypatch.setattr(evidence, 'MAX_PACKETS', 1)
    delivered = []
    monkeypatch.setattr(evidence, '_original_datagram', lambda *args: delivered.append(True))
    packet = RtpPacket(payload_type=111, sequence_number=7, timestamp=960, ssrc=42).serialize()
    connection = SimpleNamespace(_dg_pc_id='peer')
    protocol = SimpleNamespace(receiver=connection)
    for _ in range(3): evidence._observe_datagram(protocol, packet, ('127.0.0.1', 1234))
    assert len(delivered) == 3 and connection._dg_socket_truncated
    assert len((tmp_path/'socket-rtp.jsonl').read_text().splitlines()) == 1
    assert json.loads((tmp_path/'socket-status.jsonl').read_text())['reason'] == 'packet cap'
    def fail(*args, **kwargs): raise OSError('disk full')
    monkeypatch.setattr(evidence, '_write', fail)
    protocol.receiver = SimpleNamespace(_dg_pc_id='other')
    evidence._observe_datagram(protocol, packet, ('127.0.0.1', 1234))
    assert len(delivered) == 4 and protocol.receiver._dg_socket_truncated

def test_rtp_summary_sequence_wrap_and_gap(monkeypatch, tmp_path):
    monkeypatch.setattr(rtp_evidence, 'DIRECTORY', tmp_path)
    receiver = SimpleNamespace(_dg_rtp=[
        dict(seq=65534,arrival_ms=0), dict(seq=65535,arrival_ms=20),
        dict(seq=1,arrival_ms=60), dict(seq=1,arrival_ms=61)])
    connection = SimpleNamespace(pc=SimpleNamespace(getReceivers=lambda: [receiver]))
    assert rtp_evidence.summary(connection) == dict(received=3,lost=1,max_gap_ms=40,truncated=False)

def test_soak_sequence_wrap_with_gap_reorder_and_duplicate(monkeypatch, tmp_path):
    monkeypatch.setattr(rtp_evidence, 'DIRECTORY', tmp_path)
    seqs=[(65530+i) % 65536 for i in range(90000) if i != 42000]
    seqs[65534],seqs[65535]=seqs[65535],seqs[65534]
    seqs.append(seqs[-1])
    receiver=SimpleNamespace(_dg_rtp=[dict(seq=seq,arrival_ms=i*20) for i,seq in enumerate(seqs)])
    connection=SimpleNamespace(pc=SimpleNamespace(getReceivers=lambda:[receiver]))
    assert rtp_evidence.summary(connection)==dict(received=89999,lost=1,max_gap_ms=20,truncated=False)

def test_soak_observer_allows_more_than_short_trial_limit(monkeypatch):
    monkeypatch.setattr(rtp_evidence, 'MAX_PACKETS', 100000)
    async def receive(*args): pass
    monkeypatch.setattr(rtp_evidence, '_original', receive)
    receiver=SimpleNamespace()
    async def observe():
        for i in range(10001):
            packet=SimpleNamespace(ssrc=1,sequence_number=i % 65536,timestamp=i*960)
            await rtp_evidence._observe(receiver,packet,i*20)
    asyncio.run(observe())
    assert len(receiver._dg_rtp)==10001
    assert all(isinstance(r['observed_ms'],int) for r in receiver._dg_rtp)
    assert all(a['observed_ms']<=b['observed_ms'] for a,b in zip(receiver._dg_rtp,receiver._dg_rtp[1:]))


@pytest.mark.asyncio
async def test_sent_payload_capture_excludes_rtcp_and_failed_sends(monkeypatch, tmp_path):
    monkeypatch.setattr(rtp_evidence, 'DIRECTORY', tmp_path)
    sent = []
    async def send(transport, data):
        sent.append(data)
    monkeypatch.setattr(rtp_evidence, '_original_send', send)
    transport = SimpleNamespace(_dg_pc_id='peer')
    packet = RtpPacket(payload_type=111, sequence_number=7, timestamp=960, ssrc=1)
    packet.payload = bytes([9 << 3]) + b'payload'
    await rtp_evidence._observe_send(transport, packet.serialize())
    await rtp_evidence._observe_send(transport, bytes(RtcpRrPacket(ssrc=1)))
    rows = [json.loads(line) for line in (tmp_path/'downlink.jsonl').read_text().splitlines()]
    assert len(rows) == 1 and len(sent) == 2
    assert rows[0]['toc_config'] == 9 and rows[0]['stereo'] is False
    assert rows[0]['payload_bytes'] == len(packet.payload)
    assert rows[0]['seq'] == 7 and rows[0]['timestamp'] == 960
    async def fail(transport, data):
        raise ConnectionError('closed')
    monkeypatch.setattr(rtp_evidence, '_original_send', fail)
    with pytest.raises(ConnectionError):
        await rtp_evidence._observe_send(transport, packet.serialize())
    assert len((tmp_path/'downlink.jsonl').read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_receiver_feedback_records_only_matching_downlink_ssrc(monkeypatch, tmp_path):
    monkeypatch.setattr(rtp_evidence, 'DIRECTORY', tmp_path)
    seen = []
    async def receive(sender, packet):
        seen.append(packet)
    monkeypatch.setattr(rtp_evidence, '_original_feedback', receive)
    sender = SimpleNamespace(_ssrc=42, transport=SimpleNamespace(_dg_pc_id='peer'))
    def report(ssrc):
        return RtcpReceiverInfo(ssrc=ssrc, fraction_lost=2, packets_lost=3,
            highest_sequence=90000, jitter=960, lsr=0, dlsr=0)
    packet = RtcpRrPacket(ssrc=5, reports=[report(42), report(99)])
    await rtp_evidence._observe_feedback(sender, packet)
    rows = [json.loads(line) for line in (tmp_path/'downlink-rtcp.jsonl').read_text().splitlines()]
    assert seen == [packet] and len(rows) == 1
    assert rows[0]['packets_lost'] == 3 and rows[0]['highest_sequence'] == 90000
    assert rows[0]['ssrc'] == 42

@pytest.mark.asyncio
async def test_negotiated_crypto_names_are_recorded_once_without_context(monkeypatch,tmp_path):
    monkeypatch.setattr(rtp_evidence,'DIRECTORY',tmp_path)
    async def send(*args):pass
    monkeypatch.setattr(rtp_evidence,'_original_send',send)
    ssl=SimpleNamespace(get_selected_srtp_profile=lambda:b'SRTP_AES128_CM_SHA1_80',
        get_cipher_name=lambda:'ECDHE-ECDSA-AES128-GCM-SHA256',get_protocol_version_name=lambda:'DTLSv1.2')
    transport=SimpleNamespace(_dg_pc_id='peer',_ssl=ssl)
    packet=RtpPacket(payload_type=111,sequence_number=1,timestamp=960,ssrc=1);packet.payload=b'payload'
    await rtp_evidence._observe_send(transport,packet.serialize())
    await rtp_evidence._observe_send(transport,packet.serialize())
    rows=[json.loads(line) for line in (tmp_path/'dtls-info.jsonl').read_text().splitlines()]
    assert len(rows)==1
    assert set(rows[0])=={'pc_id','cipher','srtp_profile','protocol'}
    assert rows[0]['srtp_profile']=='SRTP_AES128_CM_SHA1_80'



async def test_capture_cap_never_stops_media_and_marks_truncation(monkeypatch,tmp_path):
    delivered=[]
    async def receive(receiver,packet,arrival):delivered.append(packet.sequence_number)
    monkeypatch.setattr(evidence,'_original',receive)
    monkeypatch.setattr(evidence,'MAX_PACKETS',2)
    monkeypatch.setattr(evidence,'DIRECTORY',tmp_path)
    receiver=SimpleNamespace(_dg_pc_id='test')
    for seq in range(7):
        await evidence._observe(receiver,SimpleNamespace(ssrc=1,sequence_number=seq,timestamp=seq*960),seq*20)
    assert delivered==list(range(7))
    assert len(receiver._dg_rtp)==2
    connection=SimpleNamespace(pc=SimpleNamespace(getReceivers=lambda:[receiver]))
    assert evidence.summary(connection)['truncated']
    assert len((tmp_path/'rtp.jsonl').read_text().splitlines())==2

async def test_capture_io_failure_never_drops_media(monkeypatch):
    delivered=[]
    async def receive(*args):delivered.append(True)
    def fail(*args,**kwargs):raise OSError('disk full')
    monkeypatch.setattr(evidence,'_original',receive);monkeypatch.setattr(evidence,'_write',fail)
    receiver=SimpleNamespace(_dg_pc_id='test')
    await evidence._observe(receiver,SimpleNamespace(ssrc=1,sequence_number=1,timestamp=960),20)
    assert delivered==[True]
    assert receiver._dg_rtp_truncated


@pytest.mark.asyncio
async def test_payload_logging_is_opt_in_and_keeps_media_delivery(monkeypatch, tmp_path):
    delivered=[]
    async def receive(receiver,packet,arrival):delivered.append(packet.sequence_number)
    monkeypatch.setattr(evidence,'_original',receive)
    monkeypatch.setattr(evidence,'DIRECTORY',tmp_path)
    receiver=SimpleNamespace(_dg_pc_id='test')
    for seq,enabled in enumerate((False,True)):
        monkeypatch.setattr(evidence,'PAYLOAD_BYTES',enabled)
        packet=SimpleNamespace(ssrc=1,sequence_number=seq,timestamp=seq*960,payload=b'\x48abc')
        await evidence._observe(receiver,packet,seq*20)
    rows=[json.loads(line) for line in (tmp_path/'rtp.jsonl').read_text().splitlines()]
    assert delivered==[0,1]
    assert 'payload_bytes' not in rows[0] and 'toc_config' not in rows[0]
    assert rows[1]['payload_bytes']==4 and rows[1]['toc_config']==9

@pytest.mark.asyncio
async def test_authenticated_nack_trace_follows_retransmission_and_never_blocks_it(monkeypatch,tmp_path):
    from aiortc.rtp import RtcpRtpfbPacket
    monkeypatch.setattr(evidence,'DIRECTORY',tmp_path)
    seen=[]
    async def receive(sender,packet):seen.append(list(packet.lost))
    monkeypatch.setattr(evidence,'_original_feedback',receive)
    sender=SimpleNamespace(_ssrc=42,transport=SimpleNamespace(_dg_pc_id='peer'))
    packet=RtcpRtpfbPacket(fmt=1,ssrc=5,media_ssrc=42,lost=[65535,0])
    await evidence._observe_feedback(sender,packet)
    rows=[json.loads(line) for line in (tmp_path/'downlink-nack.jsonl').read_text().splitlines()]
    assert seen==[[65535,0]] and rows[0]['lost']==[65535,0] and rows[0]['media_ssrc']==42
    def fail(*args,**kwargs):raise OSError('disk full')
    monkeypatch.setattr(evidence,'_write',fail)
    await evidence._observe_feedback(sender,packet)
    assert seen==[[65535,0],[65535,0]] and sender._dg_nack_trace_unavailable

@pytest.mark.asyncio
async def test_explicit_bind_activates_preimported_observer_on_real_local_peer(monkeypatch, tmp_path):
    """Reproduce the old no-RTP_EVIDENCE_DIR import, then exercise actual hooks."""
    from aiortc import RTCPeerConnection, RTCConfiguration, AudioStreamTrack
    # Register original values with monkeypatch so direct install mutations unwind.
    for cls, name in [(evidence.StunProtocol,'send_data'), (evidence.StunProtocol,'datagram_received'),
                      (evidence.RTCRtpReceiver,'_handle_rtp_packet'),
                      (evidence.RTCDtlsTransport,'_send_rtp'),
                      (evidence.RTCRtpSender,'_handle_rtcp_packet'),
                      (evidence.RTCSctpTransport,'_send_chunk'),
                      (evidence.RTCSctpTransport,'_receive_chunk'),
                      (evidence.RTCSctpTransport,'_t3_expired'),
                      (evidence.SmallWebRTCConnection,'send_app_message')]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(evidence, 'DIRECTORY', None)
    a, b = [RTCPeerConnection(RTCConfiguration(iceServers=[])) for _ in range(2)]
    try:
        a.addTrack(AudioStreamTrack()); b.addTrack(AudioStreamTrack())
        channel = a.createDataChannel('test')
        got = []
        @b.on('datachannel')
        def remote_channel(remote):
            remote.on('message', lambda message: (got.append(message), remote.send('ack')))
        replies = []
        channel.on('message', replies.append)
        evidence.install(tmp_path)
        await a.setLocalDescription(await a.createOffer())
        await b.setRemoteDescription(a.localDescription)
        await b.setLocalDescription(await b.createAnswer())
        await a.setRemoteDescription(b.localDescription)
        connection = SimpleNamespace(pc_id='real-local-peer', pc=a, _data_channel=channel)
        evidence.bind(connection, directory=tmp_path)
        evidence.bind(connection, directory=tmp_path)  # no double wrapping
        for _ in range(100):
            if evidence.summary(connection) and evidence.summary(connection)['received'] >= 5:
                break
            await asyncio.sleep(.02)
        for _ in range(100):
            if channel.readyState == 'open': break
            await asyncio.sleep(.02)
        evidence._observe_app_send(connection, {'type': 'test-message'})
        for _ in range(100):
            if replies: break
            await asyncio.sleep(.02)
        await asyncio.sleep(.1)
        assert got == ['{"type": "test-message"}'] and replies == ['ack']
        rows = [json.loads(x) for x in (tmp_path/'sctp.jsonl').read_text().splitlines()]
        assert {r['pc_id'] for r in rows} == {'real-local-peer'}
        assert any(r.get('chunk_type') == 'CookieAckChunk' for r in rows)
        data = [r for r in rows if r['event'] == 'send-chunk' and r.get('protocol') == 51]
        assert len(data) == 1 and data[0]['user_data_length'] == len(got[0])
        assert any(r['event'] == 'receive-chunk' and r.get('cumulative_tsn') == data[0]['tsn'] for r in rows)
        app = next(r for r in rows if r['event'] == 'send-app-message')
        assert app['buffered_amount_before'] == 0 and app['buffered_amount_after'] > 0
        assert not evidence.sctp_status(connection)['unavailable']
        assert not evidence.sctp_status(connection)['dtls_unavailable']
        dtls = [json.loads(x) for x in (tmp_path/'dtls-send.jsonl').read_text().splitlines()]
        assert dtls and all(r['epoch']==1 and r['bytes']>=13 for r in dtls)
        assert len({r['record_sequence'] for r in dtls})==len(dtls)
        assert all(r['pc_id']=='real-local-peer' for r in dtls)
        assert evidence.summary(connection)['received'] >= 5
        assert not evidence.summary(connection)['truncated']
        assert a.getSenders()[0].transport.transport._connection._dg_socket_count >= 5
        for name in ('rtp.jsonl', 'socket-rtp.jsonl', 'downlink.jsonl'):
            rows = [json.loads(x) for x in (tmp_path/name).read_text().splitlines()]
            assert len(rows) >= 5
            assert {r['pc_id'] for r in rows} == {'real-local-peer'}
    finally:
        await a.close(); await b.close()


def test_bind_without_opt_in_remains_disabled(monkeypatch):
    monkeypatch.setattr(evidence, 'DIRECTORY', None)
    evidence.bind(SimpleNamespace())  # ordinary bot: no PC inspection or hooks


async def test_sctp_cap_disk_failure_and_t3_preserve_protocol(monkeypatch, tmp_path):
    from aiortc.rtcsctptransport import DataChunk, SackChunk
    monkeypatch.setattr(evidence, 'DIRECTORY', tmp_path)
    monkeypatch.setattr(evidence, 'MAX_PACKETS', 3)
    association = SimpleNamespace(_dg_pc_id='peer', _flight_size=10, _cwnd=100,
                                 _outbound_queue=[], _sent_queue=[], _rto=3)
    calls = []
    async def receive(*args): calls.append('receive')
    async def send(*args): calls.append('send')
    monkeypatch.setattr(evidence, '_original_sctp_receive', receive)
    monkeypatch.setattr(evidence, '_original_sctp_send', send)
    monkeypatch.setattr(evidence, '_original_t3', lambda self: calls.append('t3'))
    chunk = SackChunk(); chunk.cumulative_tsn = 42; chunk.gaps = [(2, 3)]
    await evidence._observe_sctp_receive(association, chunk)
    evidence._observe_t3(association)
    await evidence._observe_sctp_send(association, DataChunk())
    assert calls == ['receive', 't3', 'send'] and association._dg_sctp_truncated
    rows = [json.loads(x) for x in (tmp_path/'sctp.jsonl').read_text().splitlines()]
    assert len(rows) == 3 and rows[0]['gap_ack_blocks'] == [[2, 3]]
    assert rows[1]['event'] == 'receive-sack-complete'
    assert rows[2]['event'] == 't3-expired' and rows[2]['flight_size'] == 10
    monkeypatch.setattr(evidence, 'MAX_PACKETS', 20)
    def fail(*args, **kwargs): raise OSError('disk full')
    monkeypatch.setattr(evidence, '_write', fail)
    await evidence._observe_sctp_send(association, DataChunk())
    assert calls[-1] == 'send' and association._dg_sctp_unavailable


async def test_sctp_send_failure_records_type_and_reraises(monkeypatch, tmp_path):
    from aiortc.rtcsctptransport import DataChunk
    monkeypatch.setattr(evidence, 'DIRECTORY', tmp_path)
    association = SimpleNamespace(_dg_pc_id='peer', _flight_size=10, _cwnd=100,
                                 _outbound_queue=[], _sent_queue=[], _rto=3)
    error = ConnectionError('private error text must not enter evidence')
    async def fail(*args): raise error
    monkeypatch.setattr(evidence, '_original_sctp_send', fail)
    with pytest.raises(ConnectionError) as caught:
        await evidence._observe_sctp_send(association, DataChunk())
    assert caught.value is error
    rows = [json.loads(x) for x in (tmp_path/'sctp.jsonl').read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['event'] == 'send-failed'
    assert rows[0]['error_type'] == 'ConnectionError' and rows[0]['chunk_type'] == 'DataChunk'
    assert 'private error' not in (tmp_path/'sctp.jsonl').read_text()


def test_sctp_tap_shared_guard_does_not_read_private_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence.sctp_flight_fix, 'SUPPORTED', False)
    monkeypatch.setattr(evidence, 'DIRECTORY', tmp_path)
    evidence._sctp_record(object(), 'guarded')
    assert not (tmp_path/'sctp.jsonl').exists()

async def test_dtls_send_identity_success_only_and_bounded(monkeypatch,tmp_path):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(evidence,'DIRECTORY',tmp_path)
    monkeypatch.setattr(evidence,'MAX_PACKETS',1)
    original=AsyncMock();monkeypatch.setattr(evidence,'_original_udp_send',original)
    protocol=SimpleNamespace(receiver=SimpleNamespace(_dg_pc_id='peer'))
    data=bytes.fromhex('17fefd12340102030405060000')
    await evidence._observe_udp_send(protocol,b'\x16'+bytes(20),None)
    await evidence._observe_udp_send(protocol,data,None)
    rows=[json.loads(x) for x in (tmp_path/'dtls-send.jsonl').read_text().splitlines()]
    assert len(rows)==1 and rows[0]['epoch']==0x1234 and rows[0]['record_sequence']==0x010203040506
    assert rows[0]['bytes']==13 and 'payload' not in rows[0]
    await evidence._observe_udp_send(protocol,data,None)
    assert protocol.receiver._dg_dtls_unavailable and original.await_count==3
    original.side_effect=OSError('test')
    with pytest.raises(OSError):await evidence._observe_udp_send(protocol,data,None)
    assert len((tmp_path/'dtls-send.jsonl').read_text().splitlines())==1
