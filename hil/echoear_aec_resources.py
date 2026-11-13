"""Step-2 resource gates over retained PERF snapshots (no hardware access)."""
import argparse
import json
import time
from pathlib import Path
from echoear_aec_checks import admission_counters

STACK_FLOORS = {'audio-playback': 4096, 'audio-capture': 4096,
                'audio-tx': 4096, 'peer-loop': 4096, 'peer-rx': 1024,
                'app-control': 2048}


def check_snapshots(snapshots):
    """Check all sessions, retaining short CPU windows without qualifying on them."""
    checks = {}
    def add(name, value, passed, **details):
        checks[name] = dict(value=value, passed=bool(passed), **details)
    add('snapshots_present', len(snapshots), bool(snapshots))
    for task, floor in STACK_FLOORS.items():
        rows = [t for p in snapshots for t in p['tasks'] if t['name'] == task]
        floors = [max(floor, (t['stack_bytes'] + 3) // 4)
                  if task == 'audio-playback' else floor for t in rows]
        margins = [t['stack_free_bytes'] - f for t, f in zip(rows, floors)]
        add('stack_' + task, min((t['stack_free_bytes'] for t in rows), default=None),
            bool(rows) and min(margins) >= 0, floor=max(floors, default=floor),
            minimum_margin_bytes=min(margins, default=None))
    for key, floor in [('minimum_free', 81920), ('largest', 32768)]:
        value = min((p['heap']['internal'][key] for p in snapshots), default=0)
        add('internal_' + key, value, value >= floor, floor=floor)
    sessions = [p for p in snapshots if p.get('generation', 0) > 0]
    add('session_snapshots_present', len(sessions), bool(sessions))
    groups = {}
    for p in sorted(sessions, key=lambda p: p.get('device_us', 0)):
        groups.setdefault((p.get('pc_id'), p.get('generation')), []).append(p)
    admission = [admission_counters(rows[0], rows[-1]) for rows in groups.values()]
    add('data_admission_counters_only', admission, bool(admission) and all(c['passed'] for c in admission))
    for key, limit in [('encode_p95_us', 20000), ('encode_max_us', 40000)]:
        values = [p.get(key) for p in sessions]
        valid = bool(values) and all(isinstance(v, (int, float)) for v in values)
        add(key, max(values) if valid else None, valid and max(values) < limit,
            strict_upper_bound=limit)
    steady = [p for p in sessions if p.get('encode_frames', 0) > 0
              and not p.get('tasks_exited', False) and all(
        any(t['name'] == 'IDLE' + str(i) and t.get('window_us', 0) >= 5000000
            for t in p['tasks']) for i in (0, 1))]
    for core in (0, 1):
        values = [p['per_core_idle_percent'][core] for p in steady]
        valid = bool(values) and all(isinstance(v, (int, float)) for v in values)
        value = min(values) if valid else None
        add(f'core_{core}_steady_idle_percent', value, valid and value >= 25,
            floor=25, absolute_floor=20, absolute_floor_passed=valid and value >= 20,
            windows=len(values))
    for path in [('failed_allocations', 'total'), ('pcm_queue_drops',),
                 ('i2s_underflows',), ('i2s_write_errors',), ('i2s_write_timeouts',),
                 ('audio', 'drops'), ('audio', 'read_errors'), ('audio', 'write_errors'),
                 ('audio', 'write_timeouts'), ('console_output', 'full'),
                 ('console_output', 'transport_drops'), ('aec', 'errors'),
                 ('aec', 'reference_clips'), ('aec', 'output_clips')]+[
                     ('aec', 'capture', k) for k in ('read_errors', 'read_timeouts', 'adc_clips', 'gain_clips')]:
        values = []
        session_counter = path in [('pcm_queue_drops',), ('i2s_underflows',),
                                   ('i2s_write_errors',), ('i2s_write_timeouts',)]
        for p in (sessions if session_counter else snapshots):
            value = p
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            values.append(value)
        valid = bool(values) and all(v is not None for v in values)
        add('.'.join(path), max(values) if valid else None, valid and max(values) == 0)
    add('uplink_frame_ms', [p.get('uplink_frame_ms') for p in sessions],
        bool(sessions) and all(p.get('uplink_frame_ms') == 20 for p in sessions))
    return checks


def liveness_reply(before, after):
    """One in-flight pong is allowed at a PERF boundary; stopped pongs fail."""
    a,b=before.get('liveness',{}),after.get('liveness',{})
    keys=('pings','pongs','timeouts')
    valid=all(type(x.get(k)) is int and x[k]>=0 for x in (a,b) for k in keys)
    same=before.get('pc_id') is not None and before.get('pc_id')==after.get('pc_id') and before.get('generation')==after.get('generation')
    delta={k:b[k]-a[k] for k in keys} if valid else {}
    passed=(valid and same and all(v>=0 for v in delta.values()) and
        delta['pings']>0 and delta['pongs']>0 and delta['pongs']>=delta['pings']-1 and
        0<=b['pings']-b['pongs']<=1 and b['timeouts']==0)
    return dict(passed=bool(passed),before=a,after=b,delta=delta,same_session=same)


def quiescent_admission_snapshot(read_snapshot, *, timeout_s=.25,
                                 clock=time.monotonic, sleep=time.sleep):
    """Bounded queue drain for a quiescent boundary, not a dropped-message waiver.

    Retain every observation for resource checks. A live ping may be between
    admission and dispatch at the first snapshot. Persistent backlog, malformed
    counters or a session change fail closed; the strict counter checker is
    unchanged. Call before/after a cell, never retry the acoustic acquisition.
    """
    # PERF serialization/USB transfer may itself exceed this polling budget.
    # Start the drain window after the initial observation, so it cannot expire
    # before even one follow-up. Each reader call retains its own I/O timeout.
    deadline=None
    samples=[]
    identity=None
    while True:
        sample=read_snapshot(len(samples))
        samples.append(sample)
        if deadline is None:deadline=clock()+timeout_s
        current=(sample.get('pc_id'),sample.get('generation'))
        if identity is None:identity=current
        counters=sample.get('data_admission',{})
        received,dispatched=(counters.get(k) for k in ('received','dispatched'))
        if (current!=identity or None in current or
                any(type(v) is not int or v<0 for v in (received,dispatched)) or
                dispatched>received):
            raise ValueError('Invalid admission boundary; all observations retained by reader')
        if received==dispatched:return sample,samples
        if clock()>=deadline:
            raise TimeoutError('Admission queue did not drain within polling budget')
        sleep(min(.02,max(0,deadline-clock())))


def check_reply_resources(before, after, *, intermediate=()):
    checks=check_snapshots([before,*intermediate,after])
    checks['liveness']=liveness_reply(before,after)
    return dict(checks=checks,all_passed=all(c['passed'] for c in checks.values()))


def check_directory(root):
    snapshots = []
    for path in root.glob('*.json'):
        p = json.loads(path.read_text())
        if isinstance(p, dict) and isinstance(p.get('tasks'), list) and 'device_us' in p:
            snapshots.append(p)
    checks = check_snapshots(snapshots)
    initial = json.loads((root / 'connected.json').read_text())['aec']['capture']
    last = max(snapshots, key=lambda p: p['device_us'])['aec']['capture']
    delta = last['rx_overflows'] - initial['rx_overflows']
    checks['live_rx_overflows'] = dict(value=delta, startup=initial['rx_overflows'], passed=delta == 0)
    b = json.loads((root / 'character-start.json').read_text())
    e = json.loads((root / 'character-end.json').read_text())
    checks['cat'] = dict(details=e, passed=e['enabled'] and e['posts'] > b['posts'] and e['errors'] == 0)
    d = json.loads((root / 'display-end.json').read_text())
    checks['display'] = dict(details=d, passed=not d['dead'] and not d['failed'] and
        d['send_failures'] == 0 and d['dma'][1] == 0 and d['errors'][0] == 0 and
        d['gate_timeouts'] == 0 and d['display_gate_timeouts'] == 0)
    return dict(checks=checks, all_passed=all(c['passed'] for c in checks.values()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hold_directory', type=Path)
    args = parser.parse_args()
    result = check_directory(args.hold_directory)
    (args.hold_directory / 'resource-checks-v2.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['all_passed'] else 1)
