"""Epoch-aware bot display sender with bounded admission retries.

The 1023-byte bound covers the compact server-message data object. Admission
acks do not certify rendering. Timeout retries replay the identical request;
exhausting the shared retry budget leaves admission unknown.
"""
import asyncio
import json

class DisplayNotReady(RuntimeError):
    pass

class DisplayRejected(RuntimeError):
    pass

class DisplayClient:
    def __init__(self, processor, log, retry_budget=2, ack_timeout=3):
        self.processor, self.log = processor, log
        self.epoch = None
        self.sequence = 0
        self.limit = 0
        self.lock = asyncio.Lock()
        self.pending = None  # One in-flight admission; no unbounded pending map.
        self.retry_budget, self.ack_timeout = retry_budget, ack_timeout

    def ready(self, data):
        if not isinstance(data, dict) or data.get('v') != 1:
            raise ValueError('Unsupported display protocol')
        for key in ('epoch', 'next_seq', 'wire_bytes'):
            if type(data.get(key)) is not int or data[key] <= 0:
                raise ValueError('Invalid display-ready '+key)
        if self.pending and not self.pending[1].done():
            self.pending[1].set_exception(DisplayNotReady('Display session changed'))
        self.epoch, self.sequence = data['epoch'], data['next_seq']-1
        self.limit = min(1023, data['wire_bytes'])
        self.log.write('display-ready', **data)

    async def send(self, operation, data, request_id):
        async with self.lock:
            if operation not in ('load','preload','show','hide','fill','batch','delete','persist','cancel'):
                raise ValueError('Unknown display operation')
            if any(k in data for k in ('v','epoch','seq','id','any_host')):
                raise ValueError('Display protocol metadata is owned by DisplayClient')
            epoch = self.epoch
            packet = None
            for attempt in range(self.retry_budget + 1):
                if self.epoch is None or self.epoch != epoch:
                    raise DisplayNotReady('Device not ready or display session changed')
                if packet is None:
                    seq = self.sequence + 1
                    encoded = json.dumps({'t':'display.'+operation, 'd':dict(data, v=1, epoch=epoch, seq=seq, id=request_id)}, separators=(',', ':'), ensure_ascii=False)
                    size = len(encoded.encode())
                    if size > self.limit:
                        raise ValueError(f'Display request {size} bytes exceeds wire limit {self.limit}')
                    # Snapshot nested caller data too: an ambiguous retry must
                    # satisfy Engine::post's exact-content/sequence replay check.
                    packet = json.loads(encoded)
                    self.sequence = seq
                future = asyncio.get_running_loop().create_future()
                self.pending = (packet['d'], future)
                try:
                    await self.processor.send_server_message(packet)
                    self.log.write('display-sent', packet=packet, bytes=size, attempt=attempt)
                    try:
                        ack = await asyncio.wait_for(future, self.ack_timeout)
                    except asyncio.TimeoutError as exc:
                        self.log.write('display-ack-timeout', epoch=epoch, seq=seq, id=request_id, attempt=attempt)
                        if attempt == self.retry_budget:
                            raise DisplayRejected('Display admission unknown: ack timeout retry budget exhausted') from exc
                        self.log.write('display-retry', epoch=epoch, seq=seq, id=request_id, attempt=attempt+1, reason='ack-timeout')
                        await asyncio.sleep(.02 * (attempt+1))
                        continue
                finally:
                    self.pending = None
                    if not future.done():
                        future.cancel()
                if self.epoch != epoch:
                    raise DisplayNotReady('Display session changed')
                if ack['accepted']:
                    self.log.write('display-admitted', packet=packet, attempts=attempt+1)
                    return packet
                if ack['error'] == 'stale-epoch':
                    self.epoch = None
                    raise DisplayNotReady('Stale display epoch; await next display-ready')
                if ack['error'] == 'queue-full' and attempt < self.retry_budget:
                    self.log.write('display-retry', epoch=epoch, seq=seq, id=request_id, attempt=attempt+1, reason='queue-full')
                    packet = None  # Explicit non-admission permits a fresh sequence.
                    await asyncio.sleep(.02 * (attempt+1))
                    continue
                raise DisplayRejected('Display rejected: '+ack['error'])

    def result(self, data):
        self.log.write('display-result', result=data)
        if not isinstance(data, dict) or not self.pending:
            return
        body, future = self.pending
        # Old-session or malformed correlation must not invalidate a new epoch.
        if type(data.get('epoch')) is not int or type(data.get('seq')) is not int or (data['epoch'], data['seq']) != (body['epoch'], body['seq']):
            self.log.write('display-ack-unmatched', result=data)
            return
        if future.done():
            return
        if data.get('parse_failed') is True:
            future.set_exception(DisplayRejected('Device could not parse display request'))
        elif data.get('id') != body['id'] or type(data.get('accepted')) is not bool or not isinstance(data.get('error'),str):
            future.set_exception(DisplayRejected('Malformed display acknowledgment'))
        else:
            # Invalidate immediately, before the awaiting sender is scheduled.
            if not data['accepted'] and data['error']=='stale-epoch':
                self.epoch = None
            future.set_result(data)

async def hil_commands(display, path):
    """Command bridge for explicit HIL modes; retry only known not-ready failures.

DisplayClient owns the bounded ack retries; the bridge does not restart them.
Permanent validation/rejection and exhausted send/ack failures are logged once.
A command is marked consumed only after success or an explicit terminal verdict.
"""
    from pathlib import Path
    path = Path(path)
    previous = None
    retry_command, retries = None, 0
    while True:
        await asyncio.sleep(.02)
        if display.epoch is None:
            continue
        try:
            command = json.loads(await asyncio.to_thread(path.read_text))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if command == previous:
            continue
        if command != retry_command:
            retry_command, retries = command, 0
        try:
            await display.send(command['op'], command['data'], command['id'])
        except DisplayNotReady as exc:
            retries += 1
            display.log.write('display-command-retry', reason=str(exc), command=command, attempt=retries)
            if retries < 3:
                continue
            display.log.write('display-command-failed', reason='Not-ready retry budget exhausted', command=command)
        except (ValueError, KeyError, RuntimeError) as exc:
            display.log.write('display-command-failed', reason=str(exc), command=command)
        previous = command
