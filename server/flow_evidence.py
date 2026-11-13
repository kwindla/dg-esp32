"""Opt-in board UDP callback capture and authenticated endpoint close events.

The pcap reconstructs IPv4/UDP headers around bytes seen at asyncio callbacks;
it is not a kernel capture and cannot prove physical delivery. No TLS keys.
"""
import json
import os
from pathlib import Path
import socket
import struct
import time
from aioice.ice import StunProtocol
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtcsctptransport import RTCSctpTransport, AbortChunk

DIRECTORY=Path(os.environ['FLOW_EVIDENCE_DIR']) if os.getenv('FLOW_EVIDENCE_DIR') else None
BOARD=os.getenv('FLOW_BOARD_IP','192.168.4.29')
LIMIT=int(os.getenv('FLOW_EVIDENCE_MAX_PACKETS','200000'))
_original_receive=StunProtocol.datagram_received
_original_send=StunProtocol.send_data
_original_dtls_stop=RTCDtlsTransport.stop
_original_write_ssl=RTCDtlsTransport._write_ssl
_original_sctp_chunk=RTCSctpTransport._send_chunk
_counts={}


def event(pc_id,**value):
    try:
        DIRECTORY.mkdir(parents=True,exist_ok=True)
        with (DIRECTORY/'endpoint-events.jsonl').open('a') as f:
            f.write(json.dumps(dict(pc_id=pc_id,monotonic_ns=time.monotonic_ns(),**value))+'\n')
    except OSError:
        pass # Observation failure must not change transport behavior.


def ipv4_udp(data,source,destination):
    udp=struct.pack('!HHHH',source[1],destination[1],8+len(data),0)+data
    header=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),0,0,64,17,0,
        socket.inet_aton(source[0]),socket.inet_aton(destination[0]))
    words=struct.unpack('!10H',header);checksum=sum(words)
    while checksum>>16:checksum=(checksum&65535)+(checksum>>16)
    header=header[:10]+struct.pack('!H',(~checksum)&65535)+header[12:]
    return header+udp


def packet(protocol,data,addr,direction):
    connection=protocol.receiver;pc=getattr(connection,'_dg_pc_id',None)
    if not pc or addr[0]!=BOARD:return
    count=_counts.get(pc,0)
    if count>=LIMIT:
        if count==LIMIT:event(pc,event='flow-truncated',packet_limit=LIMIT)
        _counts[pc]=count+1;return
    local=protocol.transport.get_extra_info('sockname')
    if not local or ':' in local[0]:return
    source,destination=(addr,local) if direction=='receive' else (local,addr)
    raw=ipv4_udp(data,source,destination);wall=time.time_ns();mono=time.monotonic_ns()
    DIRECTORY.mkdir(parents=True,exist_ok=True);path=DIRECTORY/'board-flow.pcap'
    new=not path.exists()
    with path.open('ab') as f:
        if new:f.write(struct.pack('<IHHIIII',0xa1b2c3d4,2,4,0,0,65535,101))
        f.write(struct.pack('<IIII',wall//10**9,(wall%10**9)//1000,len(raw),len(raw))+raw)
    with (DIRECTORY/'board-flow.jsonl').open('a') as f:
        f.write(json.dumps(dict(pc_id=pc,direction=direction,monotonic_ns=mono,wall_ns=wall,
            source=source,destination=destination,bytes=len(data),
            dtls_record_type=data[0] if data and 20<=data[0]<=63 else None,
            boundary='asyncio UDP callback/send_data; synthesized IPv4/UDP headers'))+'\n')
    _counts[pc]=count+1


def observe_receive(self,data,addr):
    try:packet(self,data,addr,'receive')
    except Exception as exc:event(getattr(self.receiver,'_dg_pc_id',None),event='flow-error',error=type(exc).__name__)
    return _original_receive(self,data,addr)


async def observe_send(self,data,addr):
    await _original_send(self,data,addr)
    try:packet(self,data,addr,'send')
    except Exception as exc:event(getattr(self.receiver,'_dg_pc_id',None),event='flow-error',error=type(exc).__name__)


async def observe_dtls_stop(self):
    pc=getattr(self,'_dg_pc_id',None)
    event(pc,event='dtls-stop-begin',dtls_state=self.state,ice_state=self.transport.state)
    try:return await _original_dtls_stop(self)
    finally:event(pc,event='dtls-stop-end',dtls_state=self.state,ice_state=self.transport.state,
        ssl_shutdown=self._ssl.get_shutdown() if self._ssl else None)


async def observe_write_ssl(self):
    pc=getattr(self,'_dg_pc_id',None)
    closing=bool(self._ssl and self._ssl.get_shutdown()&1)
    before=getattr(self,'_RTCDtlsTransport__tx_packets',None)
    try:
        result=await _original_write_ssl(self)
        if closing:event(pc,event='dtls-close-flush',succeeded=True,ice_state=self.transport.state,
            tx_packets_delta=getattr(self,'_RTCDtlsTransport__tx_packets',0)-before if before is not None else None)
        return result
    except BaseException as exc:
        if closing:event(pc,event='dtls-close-flush',succeeded=False,ice_state=self.transport.state,error=type(exc).__name__)
        raise


async def observe_chunk(self,chunk):
    closing=isinstance(chunk,AbortChunk);pc=getattr(self.transport,'_dg_pc_id',None)
    try:
        result=await _original_sctp_chunk(self,chunk)
        if closing:event(pc,event='sctp-abort-send',succeeded=True,ice_state=self.transport.transport.state)
        return result
    except BaseException as exc:
        if closing:event(pc,event='sctp-abort-send',succeeded=False,ice_state=self.transport.transport.state,error=type(exc).__name__)
        raise


if DIRECTORY:
    StunProtocol.datagram_received=observe_receive
    StunProtocol.send_data=observe_send
    RTCDtlsTransport.stop=observe_dtls_stop
    RTCDtlsTransport._write_ssl=observe_write_ssl
    RTCSctpTransport._send_chunk=observe_chunk
