"""Read-only bench-NIC traffic gate; never changes routes or other processes."""
import json
import time
from pathlib import Path
from netem import command

BUDGET_BYTES = 20_000_000
REFERENCE_SECONDS = 300


def sample(interface='wlp8s0', ifb='ifb0'):
    stamp = time.monotonic()
    base = Path('/sys/class/net') / interface / 'statistics'
    counters = {key: int((base / key).read_text()) for key in ('tx_bytes', 'rx_bytes')}
    qdiscs = {dev: json.loads(command('-s', '-j', 'qdisc', 'show', 'dev', dev))
              for dev in (interface, ifb)}
    return dict(monotonic_s=stamp, interface=interface, **counters, qdiscs=qdiscs)


def board_bytes(snapshot, dev):
    # The owned netem child is selected only by the board-IP filter. Root prio
    # counts all egress and is retained for a second accounting cross-check.
    rows = snapshot['qdiscs'][dev]
    child = next((r for r in rows if r.get('handle') == '8e03:' and
                  r.get('kind') == 'netem' and r.get('parent') == '8e01:3'), None)
    root = next((r for r in rows if r.get('handle') == '8e01:' and
                 r.get('kind') == 'prio'), None)
    return (child.get('bytes') if child else None,
            root.get('bytes') if root else None)


def evaluate(before, after, *, seconds=None, conservative=False):
    actual_seconds=after['monotonic_s']-before['monotonic_s']
    seconds=actual_seconds if seconds is None else seconds
    allowance = BUDGET_BYTES * seconds / REFERENCE_SECONDS
    tx = after['tx_bytes'] - before['tx_bytes']
    rx = after['rx_bytes'] - before['rx_bytes']
    interface = before['interface']
    cb, rb = board_bytes(before, interface)
    ca, ra = board_bytes(after, interface)
    board_tx = ca - cb if cb is not None and ca is not None else None
    root_tx = ra - rb if rb is not None and ra is not None else None
    ifb = next(dev for dev in before['qdiscs'] if dev != interface)
    ib, _ = board_bytes(before, ifb)
    ia, _ = board_bytes(after, ifb)
    board_rx = ia - ib if ib is not None and ia is not None else None
    available = tx >= 0 and rx >= 0 and all(
        value is not None and 0 <= value <= total + 65536
        or conservative and value is None
        for value,total in ((board_tx,tx),(board_rx,rx)))
    non_board = max(0, tx - (board_tx or 0)) if available else None
    non_board_rx = max(0, rx - (board_rx or 0)) if available else None
    valid = non_board <= allowance and non_board_rx <= allowance if available else None
    return dict(before=before,after=after,elapsed_s=actual_seconds,budget_seconds=seconds,tx_bytes=tx,
                rx_bytes=rx, board_flow_tx_bytes=board_tx, board_flow_rx_bytes=board_rx,
                prio_root_tx_bytes=root_tx,
                non_board_rx_bytes=non_board_rx,
                non_board_egress_bytes=non_board, budget_bytes=allowance,
                accounting_available=available, valid=valid,
                basis='sysfs NIC byte deltas minus owned board-flow netem child; prio root retained. Without owned tc counters, preflight uses total TX and RX as conservative upper bounds.' if conservative else
                      'sysfs NIC byte deltas minus owned board-flow netem child; prio root retained. TX and RX subtraction require owned board counters in both directions.',
                sampling_note='Sequential sysfs/tc reads; L2 accounting and read skew are retained, not assumed byte-identical.')


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2)+'\n')


def wait_for_quiet(path, *, interval=30, maximum_wait=600, interface='wlp8s0',
                   sampler=sample, sleeper=time.sleep, clock=time.monotonic):
    began = clock()
    windows = []
    while clock()-began+interval <= maximum_wait+.001:
        before = sampler(interface)
        sleeper(interval)
        after = sampler(interface)
        row = evaluate(before, after, seconds=interval, conservative=True)
        windows.append(row)
        result = dict(windows=windows, waited_s=clock()-began,
                      maximum_wait_s=maximum_wait, passed=row['valid'] is True)
        write(path, result)
        if result['passed']:
            return result
        print('Bench NIC busy or counters unavailable; waiting for a quiet 30 s window', flush=True)
    raise RuntimeError('Bench NIC did not qualify within ten minutes; no HIL capture started')


class QuietNic:
    """Arm accounting, entered while the board-only tc hierarchy is owned."""
    def __init__(self, path, *, seconds=None, interface='wlp8s0'):
        self.path, self.seconds, self.interface = Path(path), seconds, interface
        self.result = None

    def __enter__(self):
        self.before = sample(self.interface)
        write(self.path, dict(before=self.before, valid=None, status='capturing'))
        return self

    def __exit__(self, *exc):
        self.result = evaluate(self.before, sample(self.interface), seconds=self.seconds)
        self.result['status'] = 'invalid' if self.result['valid'] is not True else 'valid'
        write(self.path, self.result)
        # Capture errors keep their original exception; invalid traffic is a
        # separate durable result and must never turn an incomplete arm valid.
        if exc[0] is None and self.result['valid'] is not True:
            raise RuntimeError('Invalid HIL arm: bench NIC non-board TX/RX exceeded budget or accounting unavailable')
