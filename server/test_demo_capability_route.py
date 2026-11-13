"""Real installed runner HTTP routing; replace only WebRTC negotiation/services."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from pipecat.runner import run
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

from demo_policy import capability_aec


@pytest.mark.parametrize('start,expected', [
    ({'transport': 'webrtc'}, False),  # legacy firmware
    ({'transport': 'webrtc', 'audio': {'v': 1, 'aec': True}}, False),  # wrong nesting
    *[({'transport': 'webrtc', 'body': {'audio': {'v': 1, 'aec': aec,
          'ref_gain_db': 15, 'guard_ms': 400}}}, aec) for aec in (False, True)],
    ({'transport': 'webrtc', 'body': {'audio': {'v': 1, 'aec': 'true'}}}, False),
    ({'transport': 'webrtc', 'body': {'audio': {'v': 2, 'aec': True}}}, False),
])
async def test_start_session_offer_reaches_bot_arguments(monkeypatch, start, expected):
    seen = []

    async def bot(args):
        seen.append(args)

    async def negotiate(self, request, webrtc_connection_callback):
        await webrtc_connection_callback(MagicMock())
        return {'sdp': 'host-test-answer', 'type': 'answer', 'pc_id': 'host-test'}

    monkeypatch.setattr(run, '_get_bot_module', lambda: SimpleNamespace(bot=bot))
    monkeypatch.setattr(SmallWebRTCRequestHandler, 'handle_web_request', negotiate)
    app = FastAPI()
    args = SimpleNamespace(transport=None, ice_servers=[], esp32=True, host='127.0.0.1', folder=None)
    sessions = {}
    run._setup_unified_start_route(app, args, sessions)
    run._setup_webrtc_routes(app, args, sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/start', json=start)
        assert response.status_code == 200
        sid = response.json()['sessionId']
        # Same sessionId offer path as firmware: no requestData in the offer.
        response = await client.post(f'/sessions/{sid}/api/offer',
                                     json={'sdp': 'host-test-offer', 'type': 'offer'})
        assert response.status_code == 200
    assert len(seen) == 1 and seen[0].session_id == sid
    assert seen[0].body == start.get('body', {})
    assert capability_aec(seen[0].body) is expected
