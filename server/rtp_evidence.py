"""Opt-in pinned-aiortc RTP observation; no changes to negotiation or media.

Bind receivers to pc_id, never to creation order. File records prove timestamp,
sequence and arrival timing independently of ESP send return codes.
"""
import sctp_flight_fix
from loguru import logger
import hashlib
import json
import os
from pathlib import Path
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtcrtpsender import RTCRtpSender
from aiortc.rtcsctptransport import RTCSctpTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from aiortc.rtp import RtpPacket, RtcpRrPacket, RtcpSrPacket, RtcpRtpfbPacket, is_rtcp
import time
import struct
from aioice.ice import StunProtocol

DIRECTORY = Path(os.environ["RTP_EVIDENCE_DIR"]) if os.getenv("RTP_EVIDENCE_DIR") else None
MAX_PACKETS = int(os.getenv("RTP_EVIDENCE_MAX_PACKETS", "10000"))
PAYLOAD_BYTES = os.getenv("RTP_EVIDENCE_PAYLOAD_BYTES", "0") == "1"
_original = RTCRtpReceiver._handle_rtp_packet
_original_send = RTCDtlsTransport._send_rtp
_original_feedback = RTCRtpSender._handle_rtcp_packet
_original_datagram = StunProtocol.datagram_received
_original_udp_send = StunProtocol.send_data
_original_sctp_send = RTCSctpTransport._send_chunk
_original_sctp_receive = RTCSctpTransport._receive_chunk
_original_t3 = sctp_flight_fix._t3_expired
_original_app_send = SmallWebRTCConnection.send_app_message


def _sctp_record(association, event, **fields):
    """Bounded metadata only; observation failure must never interrupt SCTP.

    Negotiation may precede pc_id binding. Buffer those rows on the association
    (not a global creation-order map), then flush when bind supplies its identity.
    """
    if not sctp_flight_fix.SUPPORTED or not DIRECTORY or getattr(association, '_dg_sctp_unavailable', False):
        return
    try:
        count = getattr(association, '_dg_sctp_count', 0)
        if count >= MAX_PACKETS:
            association._dg_sctp_truncated = True
            return
        row = dict(monotonic_ns=time.monotonic_ns(), event=event,
                   flight_size=association._flight_size, cwnd=association._cwnd,
                   outbound_queue=len(association._outbound_queue),
                   sent_queue=len(association._sent_queue), rto_s=association._rto,
                   **fields)
        association._dg_sctp_count = count + 1
        if hasattr(association, '_dg_pc_id'):
            _write(association._dg_pc_id, row, filename='sctp.jsonl')
        else:
            if not hasattr(association, '_dg_sctp_pending'):
                association._dg_sctp_pending = []
            association._dg_sctp_pending.append(row)
    except Exception:
        association._dg_sctp_unavailable = True


def _chunk_fields(chunk):
    fields = dict(chunk_type=type(chunk).__name__, chunk_type_id=chunk.type,
                  flags=chunk.flags)
    for name in ('tsn', 'cumulative_tsn', 'stream_id', 'stream_seq', 'protocol',
                 'advertised_rwnd'):
        if hasattr(chunk, name): fields[name] = getattr(chunk, name)
    if hasattr(chunk, 'gaps'): fields['gap_ack_blocks'] = list(chunk.gaps)
    if hasattr(chunk, 'user_data'):
        fields.update(user_data_length=len(chunk.user_data),
                      user_data_sha256=hashlib.sha256(chunk.user_data).hexdigest())
    if hasattr(chunk, '_sent_count'): fields['sent_count'] = chunk._sent_count
    return fields


async def _observe_sctp_send(self, chunk):
    try:
        await _original_sctp_send(self, chunk)
    except Exception as exc:
        _sctp_record(self, 'send-failed', error_type=type(exc).__name__, **_chunk_fields(chunk))
        raise
    _sctp_record(self, 'send-chunk', **_chunk_fields(chunk))


async def _observe_sctp_receive(self, chunk):
    # Pre-handler snapshot: the handler may immediately send SACK/DATA.
    _sctp_record(self, 'receive-chunk', **_chunk_fields(chunk))
    await _original_sctp_receive(self, chunk)
    if chunk.type == 3:
        _sctp_record(self, 'receive-sack-complete', **_chunk_fields(chunk))


def _observe_t3(self):
    _sctp_record(self, 't3-expired')
    return _original_t3(self)


_observe_t3._dg_sctp_tap = True


def _observe_app_send(self, message):
    association = self.pc.sctp
    channel = self._data_channel
    before = channel.bufferedAmount if channel else None
    result = _original_app_send(self, message)
    if association is not None:
        _sctp_record(association, 'send-app-message',
            buffered_amount_before=before,
            buffered_amount_after=channel.bufferedAmount if channel else None,
            channel_state=channel.readyState if channel else None,
            message_type=message.get('type') if isinstance(message, dict) else None,
            message_bytes=len(json.dumps(message).encode('utf-8')),
            message_sha256=hashlib.sha256(json.dumps(message).encode('utf-8')).hexdigest())
    return result


def sctp_status(connection):
    association = getattr(connection.pc, "sctp", None)
    return dict(pc_id=connection.pc_id, supported=sctp_flight_fix.SUPPORTED,
                flight_fix_enabled=sctp_flight_fix.ENABLED,
                dtls_records=max((getattr(s.transport.transport._connection, '_dg_dtls_count', 0)
                                  for s in connection.pc.getSenders()), default=0),
                dtls_unavailable=any(getattr(s.transport.transport._connection, '_dg_dtls_unavailable', False)
                                     for s in connection.pc.getSenders()),
                records=getattr(association, '_dg_sctp_count', 0),
                truncated=getattr(association, '_dg_sctp_truncated', False),
                unavailable=getattr(association, '_dg_sctp_unavailable', False))



async def _observe_udp_send(self, data, addr):
    # Public DTLS identity after successful userspace UDP handoff; not an air tap.
    await _original_udp_send(self, data, addr)
    connection = self.receiver
    if not (sctp_flight_fix.SUPPORTED and DIRECTORY and hasattr(connection, '_dg_pc_id')
            and len(data) >= 13 and data[0] == 0x17):
        return
    try:
        count = getattr(connection, '_dg_dtls_count', 0)
        if count >= MAX_PACKETS:
            connection._dg_dtls_unavailable = True
            return
        _write(connection._dg_pc_id, dict(monotonic_ns=time.monotonic_ns(),
            epoch=int.from_bytes(data[3:5], 'big'),
            record_sequence=int.from_bytes(data[5:11], 'big'), bytes=len(data),
            boundary='successful asyncio UDP send_data'), filename='dtls-send.jsonl')
        connection._dg_dtls_count = count + 1
    except Exception:
        connection._dg_dtls_unavailable = True


def _observe_datagram(self, data, addr):
    """Record the asyncio UDP callback before ICE queuing or SRTP processing.

    Only public SRTP header identities are retained. These are unauthenticated
    until matched to rtp.jsonl; this is a userspace callback, not a kernel clock.
    """
    observed_ms = time.monotonic_ns() // 1_000_000
    connection = self.receiver
    try:
        if (hasattr(connection, '_dg_pc_id') and isinstance(data, bytes)
                and len(data) >= 12 and data[0] >> 6 == 2 and not is_rtcp(data)
                and not getattr(connection, '_dg_socket_truncated', False)):
            count = getattr(connection, '_dg_socket_count', 0)
            if count >= MAX_PACKETS:
                connection._dg_socket_truncated = True
                _write(connection._dg_pc_id, dict(truncated=True, reason='packet cap'),
                       filename='socket-status.jsonl')
            else:
                seq, timestamp, ssrc = struct.unpack_from('!HII', data, 2)
                _write(connection._dg_pc_id, dict(seq=seq, timestamp=timestamp,
                    ssrc=ssrc, observed_ms=observed_ms,rtp_bytes=len(data), boundary='asyncio UDP callback'),
                    filename='socket-rtp.jsonl')
                connection._dg_socket_count = count + 1
    except Exception:
        connection._dg_socket_truncated = True
    return _original_datagram(self, data, addr)


async def _observe_feedback(self, packet):
    await _original_feedback(self, packet)
    if hasattr(self.transport,"_dg_pc_id") and isinstance(packet,RtcpRtpfbPacket) and packet.fmt==1:
        try:
            _write(self.transport._dg_pc_id,dict(observed_ms=time.monotonic_ns()//1000000,
                media_ssrc=packet.media_ssrc,lost=list(packet.lost),boundary='aiortc authenticated NACK handler'),
                filename='downlink-nack.jsonl')
        except OSError:
            self._dg_nack_trace_unavailable=True
    if not hasattr(self.transport, "_dg_pc_id") or not isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
        return
    for report in packet.reports:
        if report.ssrc == self._ssrc:
            _write(self.transport._dg_pc_id, dict(
                observed_ms=time.monotonic_ns() // 1_000_000, ssrc=report.ssrc,
                packets_lost=report.packets_lost, fraction_lost=report.fraction_lost,
                highest_sequence=report.highest_sequence, jitter=report.jitter),
                filename="downlink-rtcp.jsonl")


async def _observe_send(self, data):
    await _original_send(self, data)
    if not hasattr(self, "_dg_pc_id") or is_rtcp(data):
        return
    ssl=getattr(self,"_ssl",None)
    if ssl is not None and not getattr(self,"_dg_crypto_reported",False):
        # Public negotiated names only: never export keys or SSL context data.
        profile=ssl.get_selected_srtp_profile()
        if isinstance(profile,bytes):profile=profile.decode('ascii')
        _write(self._dg_pc_id,dict(cipher=ssl.get_cipher_name(),srtp_profile=profile,
            protocol=ssl.get_protocol_version_name()),filename="dtls-info.jsonl")
        self._dg_crypto_reported=True
    packet = RtpPacket.parse(data)
    if not packet.payload:
        return
    # Capture the actual RTP payload supplied to SRTP, after a successful send.
    _write(self._dg_pc_id, dict(ssrc=packet.ssrc, seq=packet.sequence_number,
        timestamp=packet.timestamp, send_ms=time.monotonic_ns() // 1_000_000,
        payload_bytes=len(packet.payload),rtp_bytes=len(data), toc_config=packet.payload[0] >> 3,
        stereo=bool(packet.payload[0] & 4)), filename="downlink.jsonl")

async def _observe(self, packet, arrival_time_ms):
    # Media delivery always precedes observation, even with a full capture or
    # an unavailable evidence filesystem. Keep the retained sample bounded.
    await _original(self, packet, arrival_time_ms)
    if not hasattr(self, "_dg_rtp"):
        self._dg_rtp = []
    if len(self._dg_rtp) >= MAX_PACKETS or getattr(self, "_dg_rtp_truncated", False):
        self._dg_rtp_truncated = True
        return
    try:
        record = dict(ssrc=packet.ssrc, seq=packet.sequence_number,
                      timestamp=packet.timestamp, arrival_ms=arrival_time_ms,
                      observed_ms=time.monotonic_ns() // 1_000_000)
        if PAYLOAD_BYTES:
            payload = getattr(packet, 'payload', None)
            record.update(payload_bytes=len(payload) if payload is not None else None,
                          toc_config=payload[0] >> 3 if payload else None)
        self._dg_rtp.append(record)
        if hasattr(self, "_dg_pc_id"):
            _write(self._dg_pc_id, record)
    except Exception:
        self._dg_rtp_truncated = True

def _write(pc_id, record, filename="rtp.jsonl"):
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    with (DIRECTORY / filename).open("a") as stream:
        stream.write(json.dumps(dict(pc_id=pc_id, **record)) + "\n")

def install(directory):
    """Activate the opt-in observer even if this module was imported earlier.

    Evaluation used to import/bind with no RTP_EVIDENCE_DIR, silently leaving all
    hooks off. Callers now supply an explicit evaluation-local directory.
    """
    global DIRECTORY
    DIRECTORY = Path(directory)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    StunProtocol.datagram_received = _observe_datagram
    RTCRtpReceiver._handle_rtp_packet = _observe
    RTCDtlsTransport._send_rtp = _observe_send
    RTCRtpSender._handle_rtcp_packet = _observe_feedback
    if sctp_flight_fix.SUPPORTED:
        StunProtocol.send_data = _observe_udp_send
        RTCSctpTransport._send_chunk = _observe_sctp_send
        RTCSctpTransport._receive_chunk = _observe_sctp_receive
        RTCSctpTransport._t3_expired = _observe_t3
        SmallWebRTCConnection.send_app_message = _observe_app_send
    else:
        logger.error('SCTP tap disabled: unsupported aiortc version/source hash')


if DIRECTORY:
    install(DIRECTORY)


def bind(connection, directory=None):
    if directory is not None:
        install(directory)
    if not DIRECTORY:
        return  # Ordinary bots remain uninstrumented unless explicitly enabled.
    association = getattr(connection.pc, "sctp", None)
    if association is not None and sctp_flight_fix.SUPPORTED:
        association._dg_pc_id = connection.pc_id
        try:
            for row in getattr(association, '_dg_sctp_pending', []):
                _write(connection.pc_id, row, filename='sctp.jsonl')
            association._dg_sctp_pending = []
        except Exception:
            association._dg_sctp_unavailable = True
    for sender in connection.pc.getSenders():
        sender.transport._dg_pc_id = connection.pc_id
        sender.transport.transport._connection._dg_pc_id = connection.pc_id
    for receiver in connection.pc.getReceivers():
        receiver._dg_pc_id = connection.pc_id
        for record in getattr(receiver, "_dg_rtp", []):
            _write(connection.pc_id, record)


def summary(connection):
    if not DIRECTORY:
        return None
    records = [p for r in connection.pc.getReceivers() for p in getattr(r, "_dg_rtp", [])]
    if not records:
        return None
    # Extend against the newest sequence: the 30-minute soak wraps 16 bits.
    highest = records[0]["seq"]
    seqs = set()
    for record in records:
        extended = highest + (record["seq"]-highest % 65536+32768) % 65536-32768
        seqs.add(extended)
        highest = max(highest, extended)
    gaps = [b["arrival_ms"] - a["arrival_ms"] for a, b in zip(records, records[1:])]
    return dict(received=len(seqs), lost=max(seqs)-min(seqs)+1-len(seqs),
                max_gap_ms=max(gaps, default=0),
                truncated=any(getattr(r,"_dg_rtp_truncated",False) for r in connection.pc.getReceivers()))

# Separate opt-in teardown capture; RTP observer hooks above remain unchanged.
import flow_evidence  # noqa: F401
