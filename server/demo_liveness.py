"""Bounded control-channel watchdog; deadlines never await SCTP sends."""
import asyncio
import math
import time
from loguru import logger


def cadence(value='1'):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0 or (seconds and not .1 <= seconds <= 10):
        raise ValueError('ECHOEAR_LIVENESS_SECONDS must be 0 (off) or 0.1..10')
    return seconds


class Liveness:
    def __init__(self, send, close, *, period=1, event=lambda *a, **kw: None, clock=time.monotonic, label=None):
        self.send, self.close, self.period, self.event, self.clock = send, close, period, event, clock
        self.label = label  # the peer's pc_id, appended to the alarm line (decision 113); None: line unchanged
        self.seq = self.pongs = self.misses = self.alarms = self.recoveries = 0
        self.pending = {}
        self.tolerance = max(2.0, 2*period)
        self.last_timely = None
        self.queue = asyncio.Queue(maxsize=1)
        self.tasks = []

    def start(self):
        if self.period and not self.tasks:
            self.tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._watchdog())]
            self.event('liveness-start', period_s=self.period, tolerance_s=self.tolerance)

    def pong(self, data):
        if not isinstance(data, dict) or type(data.get('seq')) is not int:
            return False
        seq = data['seq']
        deadline = self.pending.pop(seq, None)
        if deadline is None:
            return False
        self.pongs += 1
        timely = self.clock() < deadline
        if timely:
            self.misses = 0
            self.last_timely = self.clock()
        else: self.misses += 1
        self.event('liveness-pong', seq=seq, timely=timely, pongs=self.pongs)
        return True

    async def tick(self):
        """One monotonic deadline check; send is only enqueued, never awaited."""
        if self.alarms or not self.period: return
        now = self.clock()
        if self.last_timely is None: self.last_timely = now
        expired = [seq for seq, deadline in self.pending.items() if now >= deadline]
        for seq in expired:
            del self.pending[seq]
            self.misses += 1
            self.event('liveness-missed', seq=seq, consecutive=self.misses)
        if now-self.last_timely >= self.tolerance:
            self.alarms += 1
            if self.label is None:
                logger.error('LIVENESS alarm: no timely pong for {} s; closing peer', self.tolerance)
            else:
                logger.error('LIVENESS alarm: no timely pong for {} s; closing peer pc_id={} silent_s={:.3f}',
                             self.tolerance, self.label, now-self.last_timely)  # measured silence (decision 115)
            self.event('liveness-alarm', alarms=self.alarms, failed=True, silent_s=now-self.last_timely)
            self.recoveries += 1  # latch before awaiting; never renegotiate over the channel
            await self.close()
            return
        if not self.queue.full():
            self.seq += 1
            self.pending[self.seq] = now + self.tolerance
            self.queue.put_nowait(self.seq)
            self.event('liveness-ping', seq=self.seq, deadline=now+self.tolerance)

    async def _watchdog(self):
        while not self.alarms:
            await self.tick()
            await asyncio.sleep(self.period)

    async def _sender(self):
        while True:
            seq = await self.queue.get()
            try:
                await self.send({'t':'ping', 'seq':seq})
            except Exception as exc:
                self.event('liveness-send-error', error_type=type(exc).__name__)

    async def stop(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        self.event('liveness-summary', pings=self.seq, pongs=self.pongs,
                   alarms=self.alarms, recoveries=self.recoveries)
