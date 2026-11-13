"""Offline-only SCTP delay; ICE/DTLS and the original RTP sender stay intact."""
import asyncio
import os
from pathlib import Path
import runpy
from aiortc.rtcdtlstransport import RTCDtlsTransport


def delay_chat(original, seconds):
    async def send(transport, data):
        loop = asyncio.get_running_loop()
        if not hasattr(transport, '_dg_prechat_hold_lock'):
            transport._dg_prechat_hold_lock = asyncio.Lock()
            transport._dg_prechat_hold_until = loop.time()+seconds
        # Serialize only SCTP writes through their shared initial hold. RTP,
        # RTCP and the DTLS handshake use different methods and keep running.
        async with transport._dg_prechat_hold_lock:
            remaining = transport._dg_prechat_hold_until-loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            return await original(transport, data)
    return send


def main():
    if os.getenv('BOT_MODE') != 'offline-test':
        raise RuntimeError('Pre-chat injection is offline diagnostic only')
    milliseconds = int(os.environ['PRECHAT_PROBE_HOLD_MS'])
    if not 500 <= milliseconds <= 5000:
        raise ValueError('Pre-chat hold must be 500..5000 ms')
    RTCDtlsTransport._send_data = delay_chat(RTCDtlsTransport._send_data, milliseconds/1000)
    runpy.run_path(str(Path(__file__).with_name('bot.py')), run_name='__main__')


if __name__ == '__main__':
    main()
