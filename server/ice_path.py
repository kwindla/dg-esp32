"""Opt-in, process-local ICE host-address restriction and nominated-pair proof.

The pinned aioice API has no public interface selector. Override only its host
address discovery before any connection exists; default behavior is untouched.
"""
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time


def interface_ipv4(name):
    if sys.platform=='darwin':
        # SIOCGIFADDR (0x8915) is Linux's number; ask the system instead.
        address=subprocess.run(['ipconfig','getifaddr',name],capture_output=True,text=True).stdout.strip()
        if not address:
            raise OSError(f'no IPv4 address on {name}')
        return address
    import fcntl
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        return socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',name.encode()[:15]))[20:24])


def interface_network(name):
    import fcntl
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        mask=socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x891b,struct.pack('256s',name.encode()[:15]))[20:24])
    return ipaddress.ip_network(interface_ipv4(name)+'/'+mask,strict=False)


def filter_remote_sdp(sdp, network):
    lines=[];kept=dropped=0
    for line in sdp.splitlines():
        if line.startswith('a=candidate:'):
            parts=line.split()
            try:
                permitted=ipaddress.ip_address(parts[4]) in network
            except (ValueError,IndexError):
                permitted=False
            if permitted:kept+=1
            else:dropped+=1;continue
        lines.append(line)
    if not kept:raise RuntimeError('NAT path requires a mapped remote candidate; use LAN STUN discovery')
    return '\r\n'.join(lines)+'\r\n',kept,dropped


def install():
    debug=os.getenv('BOT_ICE_DEBUG_PATH')
    if debug:
        import logging
        handler=logging.FileHandler(debug)
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        log=logging.getLogger('aioice.ice');log.setLevel(logging.DEBUG)
        if not any(getattr(h,'baseFilename',None)==str(Path(debug).resolve()) for h in log.handlers):log.addHandler(handler)
        else:handler.close()
        import aioice.ice
        if not getattr(aioice.ice.StunProtocol,'_dg_header_diagnostic',False):
            datagram_original=aioice.ice.StunProtocol.datagram_received
            def observe(self,data,addr):
                if len(data)>=20 and data[4:8]==b'\x21\x12\xa4\x42':
                    try:
                        message=aioice.ice.stun.parse_message(data)
                        log.debug('STUN_HEADER source=%s class=%s method=%s bytes=%s attribute_names=%s',
                            addr,message.message_class.name,message.message_method.name,len(data),list(message.attributes))
                    except ValueError as exc:
                        log.debug('STUN_PARSE_FAILURE source=%s bytes=%s reason=%s',addr,len(data),str(exc))
                return datagram_original(self,data,addr)
            aioice.ice.StunProtocol.datagram_received=observe
            aioice.ice.StunProtocol._dg_header_diagnostic=True
    interface=os.getenv('BOT_ICE_INTERFACE')
    if not interface:
        return
    address=interface_ipv4(interface)
    ipaddress.IPv4Address(address)
    import aioice.ice
    aioice.ice.get_host_addresses=lambda use_ipv4,use_ipv6:[address] if use_ipv4 else []
    if os.getenv('BOT_ICE_REMOTE_NETWORK_ONLY')=='1':
        from aiortc import RTCPeerConnection,RTCSessionDescription
        if not getattr(RTCPeerConnection,'_dg_remote_network_filter',False):
            remote_original=RTCPeerConnection.setRemoteDescription
            async def set_remote(self,description):
                network=interface_network(os.environ['BOT_ICE_INTERFACE'])
                sdp,kept,dropped=filter_remote_sdp(description.sdp,network)
                import logging
                logging.getLogger('aioice.ice').info('REMOTE_CANDIDATE_FILTER kept=%s dropped=%s network=%s',kept,dropped,network)
                return await remote_original(self,RTCSessionDescription(sdp=sdp,type=description.type))
            RTCPeerConnection.setRemoteDescription=set_remote
            RTCPeerConnection._dg_remote_network_filter=True


def selected_pairs(connection):
    pairs=[]
    seen=set()
    for transceiver in connection.pc.getTransceivers():
        ice=transceiver.sender.transport.transport
        if id(ice) in seen:
            continue
        seen.add(id(ice))
        for component,pair in ice._connection._nominated.items():
            pairs.append(dict(component=component,local=list(pair.local_addr),remote=list(pair.remote_addr),
                local_type=pair.local_candidate.type,remote_type=pair.remote_candidate.type))
    return pairs


def pair_interface(pair):
    """Route the actual nominated endpoints; binding a local IP is insufficient."""
    local=str(ipaddress.IPv4Address(pair['local'][0]))
    remote=str(ipaddress.IPv4Address(pair['remote'][0]))
    routes=json.loads(subprocess.check_output(['ip','-j','route','get',remote,'from',local],text=True))
    if len(routes)!=1 or not routes[0].get('dev'):
        raise RuntimeError('Selected ICE pair route interface unavailable')
    return routes[0]['dev']


def record(connection, log):
    pairs=selected_pairs(connection)
    expected=os.getenv('BOT_ICE_INTERFACE')
    impaired=os.getenv('BOT_IMPAIRED_INTERFACE')
    if impaired and expected!=impaired:
        raise RuntimeError('ICE selected pair is not on impaired interface '+impaired)
    if expected and (not pairs or any(p['local'][0]!=interface_ipv4(expected) for p in pairs)):
        raise RuntimeError('ICE selected pair does not match requested interface')
    selected_interfaces=[pair_interface(p) for p in pairs] if impaired else None
    if impaired and (not selected_interfaces or any(dev!=impaired for dev in selected_interfaces)):
        raise RuntimeError('ICE selected pair route is not on impaired interface '+impaired)
    log.write('ice-selected-pairs',interface=expected,pairs=pairs,selected_interfaces=selected_interfaces)
    directory=os.getenv('RTP_EVIDENCE_DIR')
    if directory:
        path=Path(directory)
        path.mkdir(parents=True,exist_ok=True)
        with (path/'ice-pairs.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(pc_id=connection.pc_id,observed_ns=time.monotonic_ns(),interface=expected,pairs=pairs,selected_interfaces=selected_interfaces))+'\n')
