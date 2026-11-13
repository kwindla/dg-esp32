"""Explicit offline-only startup-burst injection; never a qualification bot.

Hold the first N audio RTP frames, then release that batch in original order.
Subsequent sender cadence is unchanged. No payload/sequence/timestamp edits.
"""
import os
import runpy
from pathlib import Path
from aiortc.rtcdtlstransport import RTCDtlsTransport
from aiortc.rtp import is_rtcp


def startup_burst(original, frames):
    async def send(transport, data):
        if is_rtcp(data) or getattr(transport,'_dg_startup_burst_done',False):
            return await original(transport,data)
        pending=getattr(transport,'_dg_startup_burst',None)
        if pending is None:
            pending=[];transport._dg_startup_burst=pending
        pending.append(data)
        if len(pending)<frames:return
        transport._dg_startup_burst_done=True
        for packet in pending:await original(transport,packet)
        transport._dg_startup_burst=[]
    return send


def main():
    if os.getenv('BOT_MODE')!='offline-test':
        raise RuntimeError('Startup burst injection is offline diagnostic only')
    frames=int(os.environ['DRIFT_PROBE_STARTUP_FRAMES'])
    if not 2<=frames<=4:raise ValueError('Startup batch must be 2..4 frames')
    # Load existing observation first, so its send records describe the actual
    # batch release, not the earlier frame production/hold time.
    import rtp_evidence
    if rtp_evidence.DIRECTORY is None:raise RuntimeError('RTP evidence required')
    RTCDtlsTransport._send_rtp=startup_burst(RTCDtlsTransport._send_rtp,frames)
    runpy.run_path(str(Path(__file__).with_name('bot.py')),run_name='__main__')


if __name__=='__main__':main()
