"""Step-3-only runner: bounded artifact uploads and DTLS/RTP instrumentation."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from bot import bot
from fastapi import HTTPException, Request
from pipecat.runner.run import app, main

OUT = Path(os.environ['SPIKE_EVIDENCE_DIR']).resolve()
OUT.mkdir(parents=True, exist_ok=True)

@app.post('/spike-evidence/{attempt}/{name}')
async def evidence(attempt: int, name: str, request: Request):
    if not 1 <= attempt <= 10 or name not in {'offer.sdp','offer-active.sdp','answer.sdp','answer-original.sdp','device.json','rx.pcm'}:
        raise HTTPException(400)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 800000:
            raise HTTPException(413)
    directory = OUT / str(attempt)
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(data)
    return {'ok': True}

# Observe the pinned aiortc transport without altering negotiation or media.
import json
from itertools import count
import logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("aiortc.rtcdtlstransport").setLevel(logging.DEBUG)
logging.getLogger("aiortc.rtcsctptransport").setLevel(logging.DEBUG)
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtcrtpreceiver import RTCRtpReceiver
_setup_srtp = RTCDtlsTransport._setup_srtp
def setup_srtp(self):
    result = _setup_srtp(self)
    with (OUT / 'dtls.jsonl').open('a') as stream:
        stream.write(json.dumps({'cipher': self._ssl.get_cipher_name(),
            'srtp': self._ssl.get_selected_srtp_profile().decode()}) + '\n')
    return result
RTCDtlsTransport._setup_srtp = setup_srtp
_receiver_ids = count(1)
_handle_rtp = RTCRtpReceiver._handle_rtp_packet
async def handle_rtp(self, packet, arrival_time_ms):
    if not hasattr(self, '_spike_rx_id'):
        self._spike_rx_id = next(_receiver_ids)
    with (OUT / 'rtp.jsonl').open('a') as stream:
        stream.write(json.dumps({'receiver': self._spike_rx_id, 'ssrc': packet.ssrc, 'seq': packet.sequence_number,
            'timestamp': packet.timestamp, 'arrival_ms': arrival_time_ms}) + '\n')
    await _handle_rtp(self, packet, arrival_time_ms)
RTCRtpReceiver._handle_rtp_packet = handle_rtp

if __name__ == '__main__':
    main()
