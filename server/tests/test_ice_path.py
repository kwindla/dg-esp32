from types import SimpleNamespace as NS
import json
import pytest
import ice_path


def test_filter_rejects_zero_surviving_candidates():
    import ipaddress
    with pytest.raises(RuntimeError,match='mapped remote candidate'):
        ice_path.filter_remote_sdp('v=0\r\na=candidate:1 1 UDP 1 10.0.0.2 9000 typ host\r\n',ipaddress.ip_network('198.51.100.0/24'))


def test_record_creates_directory_and_rejects_wrong_pair(monkeypatch,tmp_path):
    pair=dict(local=['198.51.100.2',9000],remote=['198.51.100.3',9001])
    monkeypatch.setattr(ice_path,'selected_pairs',lambda connection:[pair])
    monkeypatch.setattr(ice_path,'interface_ipv4',lambda _: '198.51.100.2')
    monkeypatch.setenv('BOT_ICE_INTERFACE','test')
    directory=tmp_path/'not-created'/'rtp'
    monkeypatch.setenv('RTP_EVIDENCE_DIR',str(directory))
    connection=NS(pc_id='test-session');calls=[]
    ice_path.record(connection,NS(write=lambda *args,**kwargs:calls.append((args,kwargs))))
    record=json.loads((directory/'ice-pairs.jsonl').read_text())
    assert record['pairs']==[pair] and record['pc_id']=='test-session'
    pair['local'][0]='203.0.113.2'
    with pytest.raises(RuntimeError,match='requested interface'):
        ice_path.record(connection,NS(write=lambda *args,**kwargs:calls.append((args,kwargs))))
    assert len(calls)==1


def test_interface_filter_is_opt_in(monkeypatch):
    import aioice.ice
    original=aioice.ice.get_host_addresses
    monkeypatch.delenv('BOT_ICE_INTERFACE',raising=False)
    ice_path.install()
    assert aioice.ice.get_host_addresses is original
    monkeypatch.setenv('BOT_ICE_INTERFACE','bench-test')
    monkeypatch.setattr(ice_path,'interface_ipv4',lambda _: '198.51.100.2')
    # Restore aioice after this test: the override is process-local at runtime.
    monkeypatch.setattr(aioice.ice,'get_host_addresses',original)
    ice_path.install()
    assert aioice.ice.get_host_addresses(True,True)==['198.51.100.2']
    assert aioice.ice.get_host_addresses(False,True)==[]


def test_impairment_rejects_ethernet_pair_even_when_ice_requested_it(monkeypatch):
    monkeypatch.setenv('BOT_ICE_INTERFACE','enp7s0')
    monkeypatch.setenv('BOT_IMPAIRED_INTERFACE','wlp8s0')
    monkeypatch.setattr(ice_path,'selected_pairs',lambda _: [dict(local=['192.168.42.75',9000])])
    calls=[]
    with pytest.raises(RuntimeError,match='impaired interface'):
        ice_path.record(NS(pc_id='wrong-path'),NS(write=lambda *a,**k:calls.append(a)))
    assert not calls


def test_impairment_checks_route_of_nominated_pair_at_session_start(monkeypatch,tmp_path):
    pair=dict(local=['192.168.7.222',9000],remote=['192.168.4.29',9001])
    monkeypatch.setenv('BOT_ICE_INTERFACE','wlp8s0');monkeypatch.setenv('BOT_IMPAIRED_INTERFACE','wlp8s0')
    monkeypatch.setenv('RTP_EVIDENCE_DIR',str(tmp_path))
    monkeypatch.setattr(ice_path,'interface_ipv4',lambda _:pair['local'][0])
    monkeypatch.setattr(ice_path,'selected_pairs',lambda _:[pair])
    calls=[];log=NS(write=lambda *a,**k:calls.append(k))
    def wrong_route(argv,**kwargs):
        assert argv==['ip','-j','route','get','192.168.4.29','from','192.168.7.222']
        return '[{"dev":"enp7s0"}]'
    monkeypatch.setattr(ice_path.subprocess,'check_output',wrong_route)
    with pytest.raises(RuntimeError,match='route is not on impaired interface'):
        ice_path.record(NS(pc_id='current'),log)
    assert not calls
    monkeypatch.setattr(ice_path.subprocess,'check_output',lambda *a,**k:'[{"dev":"wlp8s0"}]')
    ice_path.record(NS(pc_id='current'),log)
    row=json.loads((tmp_path/'ice-pairs.jsonl').read_text())
    assert row['selected_interfaces']==['wlp8s0']


def test_selected_pair_is_nominated_pair_not_sdp_candidates():
    pair=NS(local_addr=('198.51.100.2',9000),remote_addr=('198.51.100.3',9001),
            local_candidate=NS(type='host'),remote_candidate=NS(type='prflx'))
    ice=NS(_connection=NS(_nominated={1:pair}))
    sender=NS(transport=NS(transport=ice))
    pc=NS(getTransceivers=lambda:[NS(sender=sender),NS(sender=sender)])
    assert ice_path.selected_pairs(NS(pc=pc))==[dict(component=1,local=['198.51.100.2',9000],
        remote=['198.51.100.3',9001],local_type='host',remote_type='prflx')]


def test_remote_filter_preserves_session_and_mapped_candidate():
    import ipaddress
    sdp='v=0\r\na=ice-ufrag:example\r\na=candidate:1 1 UDP 1 10.0.0.2 9000 typ host\r\na=candidate:2 1 UDP 2 198.51.100.3 9001 typ srflx\r\n'
    filtered,kept,dropped=ice_path.filter_remote_sdp(sdp,ipaddress.ip_network('198.51.100.0/24'))
    assert (kept,dropped)==(1,1)
    assert 'a=ice-ufrag:example\r\n' in filtered
    assert '198.51.100.3 9001 typ srflx' in filtered
    assert '10.0.0.2' not in filtered


def test_debug_and_remote_filter_delegate_independent_methods(monkeypatch,tmp_path):
    import aioice.ice
    from aiortc import RTCPeerConnection
    calls=[]
    monkeypatch.setenv('BOT_ICE_DEBUG_PATH',str(tmp_path/'ice.log'))
    monkeypatch.setenv('BOT_ICE_INTERFACE','test')
    monkeypatch.setenv('BOT_ICE_REMOTE_NETWORK_ONLY','1')
    monkeypatch.setattr(ice_path,'interface_ipv4',lambda _: '198.51.100.2')
    monkeypatch.setattr(aioice.ice,'get_host_addresses',aioice.ice.get_host_addresses)
    monkeypatch.setattr(aioice.ice.StunProtocol,'_dg_header_diagnostic',False,raising=False)
    monkeypatch.setattr(aioice.ice.StunProtocol,'datagram_received',lambda self,data,addr:calls.append((data,addr)))
    monkeypatch.setattr(RTCPeerConnection,'_dg_remote_network_filter',False,raising=False)
    monkeypatch.setattr(RTCPeerConnection,'setRemoteDescription',RTCPeerConnection.setRemoteDescription)
    ice_path.install()
    aioice.ice.StunProtocol.datagram_received(None,b'x',('198.51.100.3',9000))
    assert calls==[(b'x',('198.51.100.3',9000))]
