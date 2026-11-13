"""Pinned-library flight accounting, including real peer loss recovery."""
import asyncio
from types import SimpleNamespace

import pytest
from aiortc import RTCConfiguration, RTCDataChannel, RTCDataChannelParameters, RTCPeerConnection
from aiortc import rtcsctptransport as sctp
from aiortc.rtcsctptransport import RTCSctpTransport, SackChunk
import sctp_flight_fix as fix


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(fix, 'ENABLED', False)
    monkeypatch.setattr(RTCSctpTransport, '_t3_expired', fix._original_t3)
    monkeypatch.setattr(sctp, 'USERDATA_MAX_LENGTH', 1100)
    assert fix.install()
    yield


class Association:
    def __init__(self):
        self.a = RTCSctpTransport(SimpleNamespace(state='connected'))
        self.a._local_tsn = 1000
        self.a._last_sacked_tsn = self.a._advanced_peer_ack_tsn = 999
        self.a._association_state = self.a.State.ESTABLISHED
        self.channel = RTCDataChannel(self.a, RTCDataChannelParameters(label='test', id=2, negotiated=True))
        self.sent = []
        async def wire(chunk): self.sent.append(chunk.tsn)
        self.a._send_chunk = wire

    async def burst(self):
        first = self.a._local_tsn
        for size in (47, 52, 474, 479, 181): self.channel.send('x'*size)
        await asyncio.sleep(0)
        return first

    async def ack(self, cumulative, gaps=()):
        chunk = SackChunk(); chunk.cumulative_tsn = cumulative; chunk.gaps = list(gaps)
        await self.a._receive_sack_chunk(chunk)

    async def timeout(self):
        self.a._t3_cancel()
        self.a._t3_expired()
        await asyncio.sleep(0)

    def close(self): self.a._t3_cancel()


async def test_reproduction_clears_flight_and_sends_both_stops(patched):
    h = Association()
    try:
        first = await h.burst()
        await h.ack(first-1, [(2, 5)])
        assert h.a._flight_size == 47
        await h.timeout()
        await h.ack(first+4)
        assert h.a._flight_size == 0 and h.a._cwnd == 2200
        assert not h.a._sent_queue and h.a._t3_handle is None
        h.channel.send('x'*47); h.channel.send('x'*52)
        await asyncio.sleep(0)
        assert len(h.sent) == 12 and h.a._t3_handle is not None
        assert h.channel.bufferedAmount == 0
        await h.ack(first+6)
        assert h.a._flight_size == 0 and h.a._t3_handle is None
    finally: h.close()


async def test_two_gap_patterns_two_t3_cycles_do_not_accumulate(patched):
    h = Association()
    try:
        for gaps in ([(2, 5)], [(2, 2), (4, 5)]):
            first = await h.burst()
            await h.ack(first-1, gaps)
            await h.timeout()
            # Second cycle can fill cwnd before all retransmits; ACK progress
            # drains those retransmits before acknowledging the entire burst.
            if any(c._retransmit for c in h.a._sent_queue):
                await h.ack(first+3)
            await h.ack(first+4)
            assert h.a._flight_size == 0 and not h.a._sent_queue
            assert h.a._t3_handle is None
    finally: h.close()


async def test_regap_ack_after_t3_subtracts_once(patched):
    h = Association()
    try:
        first = await h.burst()
        await h.ack(first-1, [(2, 5)])
        await h.timeout()
        assert h.a._flight_size == 1233
        await h.ack(first-1, [(2, 5)])
        assert h.a._flight_size == 47
        await h.ack(first-1, [(2, 5)])
        assert h.a._flight_size == 47
        await h.ack(first+4)
        assert h.a._flight_size == 0
        await h.ack(first+4)
        assert h.a._flight_size == 0
    finally: h.close()


async def test_real_local_peer_first_data_drop_recovers_after_t3(patched, monkeypatch, tmp_path):
    import rtp_evidence as tap
    for cls, name in [(tap.StunProtocol,'send_data'), (tap.StunProtocol,'datagram_received'), (tap.RTCRtpReceiver,'_handle_rtp_packet'),
                      (tap.RTCDtlsTransport,'_send_rtp'), (tap.RTCRtpSender,'_handle_rtcp_packet'),
                      (RTCSctpTransport,'_send_chunk'), (RTCSctpTransport,'_receive_chunk'),
                      (RTCSctpTransport,'_t3_expired'), (tap.SmallWebRTCConnection,'send_app_message')]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(tap, 'DIRECTORY', None)
    tap.install(tmp_path)
    a, b = [RTCPeerConnection(RTCConfiguration(iceServers=[])) for _ in range(2)]
    received = []; dropped = []; armed = False
    original_send = RTCSctpTransport._send_chunk
    async def drop_once(self, chunk):
        if armed and self is a.sctp and getattr(chunk, 'protocol', None) == 51 and not dropped:
            dropped.append(chunk.tsn)
            return
        await original_send(self, chunk)
    monkeypatch.setattr(RTCSctpTransport, '_send_chunk', drop_once)
    channel = a.createDataChannel('loss-test')
    @b.on('datachannel')
    def remote_channel(remote): remote.on('message', received.append)
    async def until(predicate):
        async with asyncio.timeout(8):
            while not predicate(): await asyncio.sleep(.01)
    try:
        await a.setLocalDescription(await a.createOffer())
        await b.setRemoteDescription(a.localDescription)
        await b.setLocalDescription(await b.createAnswer())
        await a.setRemoteDescription(b.localDescription)
        connection = SimpleNamespace(pc=a, pc_id='loss-peer')
        tap.bind(connection)
        await until(lambda: channel.readyState == 'open' and a.sctp._flight_size == 0)
        armed = True
        # Three DATA chunks: lost 47 B + fragmented 1186 B. Two newly-acked
        # gaps cannot cause the three-miss fast retransmit; T3 must recover it.
        channel.send('a'*47); channel.send('b'*1186)
        await until(lambda: len(received) == 2 and a.sctp._flight_size == 0)
        channel.send('later-stop')
        await until(lambda: len(received) == 3 and a.sctp._flight_size == 0)
        assert received == ['a'*47, 'b'*1186, 'later-stop'] and len(dropped) == 1
        import json
        rows = [json.loads(x) for x in (tmp_path/'sctp.jsonl').read_text().splitlines()]
        assert any(r['event'] == 't3-expired' for r in rows)
        assert any(r['event'] == 'receive-sack-complete' and r['flight_size'] == 0 for r in rows)
        assert not tap.sctp_status(connection)['unavailable']
    finally:
        await a.close(); await b.close()


def test_version_hash_guard_and_idempotence(monkeypatch):
    assert fix.supported('1.15.0', fix.EXPECTED_SHA256)
    assert not fix.supported('1.15.1', fix.EXPECTED_SHA256)
    assert not fix.supported('1.15.0', '0'*64)
    monkeypatch.setattr(fix, 'ENABLED', False)
    monkeypatch.setattr(RTCSctpTransport, '_t3_expired', fix._original_t3)
    monkeypatch.setattr(fix, 'SUPPORTED', False)
    assert not fix.install() and RTCSctpTransport._t3_expired is fix._original_t3
    monkeypatch.setattr(fix, 'SUPPORTED', True)
    assert fix.install()
    installed = RTCSctpTransport._t3_expired
    assert fix.install() and RTCSctpTransport._t3_expired is installed


@pytest.mark.parametrize('tap_first', [True, False])
def test_fix_and_tap_install_order(monkeypatch, tmp_path, tap_first):
    import rtp_evidence as tap
    for cls, name in [(tap.StunProtocol,'send_data'), (tap.StunProtocol,'datagram_received'), (tap.RTCRtpReceiver,'_handle_rtp_packet'),
                      (tap.RTCDtlsTransport,'_send_rtp'), (tap.RTCRtpSender,'_handle_rtcp_packet'),
                      (RTCSctpTransport,'_send_chunk'), (RTCSctpTransport,'_receive_chunk'),
                      (RTCSctpTransport,'_t3_expired'), (tap.SmallWebRTCConnection,'send_app_message')]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(tap, 'DIRECTORY', None)
    monkeypatch.setattr(fix, 'ENABLED', False)
    monkeypatch.setattr(RTCSctpTransport, '_t3_expired', fix._original_t3)
    calls = []
    monkeypatch.setattr(fix, '_original_t3', lambda self: calls.append('original'))
    monkeypatch.setattr(RTCSctpTransport, '_t3_expired', fix._original_t3)
    if tap_first: tap.install(tmp_path)
    assert fix.install()
    if not tap_first: tap.install(tmp_path)
    chunks = [SimpleNamespace(_abandoned=False, _acked=True), SimpleNamespace(_abandoned=True, _acked=True)]
    association = SimpleNamespace(_sent_queue=chunks, _outbound_queue=[], _flight_size=10, _cwnd=100, _rto=3)
    RTCSctpTransport._t3_expired(association)
    assert calls == ['original'] and not chunks[0]._acked and chunks[1]._acked
    assert association._dg_sctp_pending[0]['event'] == 't3-expired'
