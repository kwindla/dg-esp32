"""Strict EchoEar voice/display acceptance. Missing evidence fails closed."""
import json
import re
import time
from pathlib import Path


def memory_gate(samples):
    try:
        result = dict(minimum_internal=min(s['heap']['internal']['minimum_free'] for s in samples),
                      largest_internal=min(s['heap']['internal']['largest'] for s in samples),
                      failed_internal_allocations=max(s['failed_allocations']['internal'] for s in samples),
                      failed_total_allocations=max(s['failed_allocations']['total'] for s in samples))
    except (KeyError, ValueError, TypeError):
        return dict(passed=False, reason='Missing memory samples')
    result['passed'] = result['minimum_internal'] >= 81920 and result['largest_internal'] >= 32768 and result['failed_internal_allocations'] == 0 and result['failed_total_allocations'] == 0
    return result


def census_gate(lines):
    rows = [dict((k, int(v)) for k,v in re.findall(r'(psram_chip|psram_heap_total|cache_line|half_bytes)=(\d+)', line))
            for line in lines if 'lcd-census: staging bytes=' in line]
    assert rows, 'Missing PSRAM/staging census'
    for row in rows:
        assert row.get('psram_chip') == 16777216, row
        assert row.get('psram_heap_total', 0) >= 14000000, row
        assert row.get('cache_line') in (32,64) and row.get('half_bytes',0) == 10240 and row['half_bytes'] % row['cache_line'] == 0, row
    return rows


def ready_gdma_record(line):
    """Require the complete five-pair hardware snapshot; never repair a splice."""
    prefix='DISPLAY_R4_GDMA_READY '
    assert line.startswith(prefix), 'Missing GDMA readiness record'
    record=json.loads(line[len(prefix):])
    assert type(record.get('device_us')) is int and record['device_us']>0, 'Missing readiness timestamp'
    pairs=record.get('pairs')
    assert type(pairs) is list and len(pairs)==5, 'Incomplete GDMA readiness pairs'
    for index,pair in enumerate(pairs):
        assert type(pair) is dict and type(pair.get('pair')) is int and pair['pair']==index, 'Invalid GDMA pair index'
        for key,limit in (('tx_peri_sel',63),('rx_peri_sel',63),('tx_priority',15),('rx_priority',15)):
            assert type(pair.get(key)) is int and 0<=pair[key]<=limit, 'Missing or invalid GDMA register '+key
    return record


def audio_gate(before, after):
    keys = ('drops','read_errors','write_errors','clips')
    delta = {k:after.get('audio',{}).get(k, -1)-before.get('audio',{}).get(k, -1)
             if k in after.get('audio',{}) and k in before.get('audio',{}) else None for k in keys}
    for key in ('i2s_underflows','decode_errors','pcm_queue_drops'):
        delta[key] = after[key]-before[key] if key in before and key in after else None
    before_gap,after_gap = before.get('drop_reasons',{}).get('uplink_gap'),after.get('drop_reasons',{}).get('uplink_gap')
    delta['uplink_queue_gaps'] = after_gap-before_gap if type(before_gap) is int and type(after_gap) is int else None
    return dict(deltas=delta, passed=all(v == 0 for v in delta.values()))


def reply_fps(before, after):
    try:
        seconds = (after['device_us']-before['device_us'])/1e6
        frames = after['display']['rendered']-before['display']['rendered']
        fps = frames/seconds if seconds > 0 else 0
        return dict(frames=frames, seconds=seconds, fps=fps, floor=24,
                    passed=seconds >= 1 and fps >= 24)
    except (KeyError, TypeError):
        return dict(passed=False, reason='Missing reply display bracket')


def perf_window_gate(windows, acquisition_start, gate_reopen):
    overlaps=[w for w in windows if w['start']<gate_reopen and w['end']>acquisition_start]
    return dict(passed=bool(windows) and acquisition_start<gate_reopen and not overlaps,
                start=acquisition_start,end=gate_reopen,overlaps=overlaps,
                basis='No full PERF query from prompt start through gate reopen; compact frame clocks retained')


def frame_clock(board):
    from step9a_support import read
    requested=time.monotonic()
    sample=read(board,'frame-clock','DISPLAY_FRAME_CLOCK')
    assert type(sample.get('device_us')) is int and type(sample.get('display',{}).get('rendered')) is int, 'Missing frame clock'
    return dict(requested_at=requested,completed_at=time.monotonic(),perf=sample)


def full_reply_fps(samples, first_tts, gate_host, gate_device_us):
    """Lower-bound FPS over the FULL first-TTS-to-reopen host interval.

    Only count frames between device snapshots provably inside the interval.
    Exclude edge frames rather than interpolating or crediting them. This lower
    bound must itself clear24fps, so a clean short slice cannot qualify.
    """
    duration = gate_host-first_tts
    inside = [s for s in samples if s['requested_at'] >= first_tts
              and s['completed_at'] < gate_host and s['perf']['device_us'] < gate_device_us]
    if len(inside)<2 or duration<5:
        return dict(passed=False, seconds=duration, reason='Need >=5s full reply and two interior snapshots')
    before, after = inside[0], inside[-1]
    frames = after['perf']['display']['rendered']-before['perf']['display']['rendered']
    elapsed = (after['perf']['device_us']-before['perf']['device_us'])/1e6
    rate = frames/duration
    return dict(passed=frames>=0 and elapsed>0 and rate>=24, frames_lower_bound=frames,
                seconds=duration, fps_lower_bound=rate, floor=24,
                first_tts=first_tts, gate_reopen=gate_host, gate_device_us=gate_device_us,
                before=dict(requested_at=before['requested_at'],completed_at=before['completed_at'],device_us=before['perf']['device_us'],rendered=before['perf']['display']['rendered']),
                after=dict(requested_at=after['requested_at'],completed_at=after['completed_at'],device_us=after['perf']['device_us'],rendered=after['perf']['display']['rendered']),
                edge_seconds=max(0,duration-elapsed),
                basis='Interior rendered frames / full first_tts-to-gate-reopen interval; excludes edge frames')


def publish_command(path, operation, data, request_id):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(op=operation, data=data, id=request_id)))
    temp.replace(path)


def rendered_show(completed, status, layers, body):
    # A cat revision can finish after the show was applied, so the engine may
    # mark its older revision Superseded while preserving the untouched layer.
    # Require an applied revision, a finished frame at/after it, and exact layer
    # identity/position. Layer 7 is reserved for this wire probe; cat uses 0..3.
    revision = completed.get('revision', 0)
    layer = next((x for x in layers.get('layers', []) if x[0]==body['layer']), None)
    return bool(completed.get('error')=='none' and completed.get('state') in (3,7)
                and revision>0 and status.get('revisions',[0,0,0])[2]>=revision
                and layer and layer[1:4]==[body['hash'],body['x'],body['y']] and layer[-1] is True)


def wire_show(board, bot, pc_id, path, request_id):
    from step9a_support import pack, terminal, read
    from transport_test import wait_until
    started = time.monotonic()
    # Each turn makes a visible change, rather than re-showing identical pixels.
    index = int(re.search(r'(\d+)$',request_id)[1])
    body = dict(hash=pack()[1]['hash'], layer=7, x=8*(index%8), y=0)
    publish_command(path, 'show', body, request_id)
    outcome = wait_until(lambda:next((e for e in bot.events() if e.get('pc_id')==pc_id and
        ((e['event']=='display-admitted' and e['packet']['d']['id']==request_id) or
         (e['event']=='display-command-failed' and e['command'].get('id')==request_id))),None),5)
    assert outcome is not None, 'Missing bot display admission verdict for '+request_id
    assert outcome['event']=='display-admitted', outcome
    body_meta=outcome['packet']['d']
    sent=next(e for e in bot.events() if e.get('pc_id')==pc_id and e['event']=='display-sent' and
              e['packet']['d']['epoch']==body_meta['epoch'] and e['packet']['d']['seq']==body_meta['seq'])
    ack=next(e for e in bot.events() if e.get('pc_id')==pc_id and e['event']=='display-result' and
             e['result'].get('epoch')==body_meta['epoch'] and e['result'].get('seq')==body_meta['seq'])
    assert ack['result'].get('accepted') is True and not ack['result'].get('parse_failed'), ack
    completed = terminal(board, request_id, timeout=3)
    proof = {}
    def visible():
        proof['status'] = read(board,'status','DISPLAY')
        proof['layers'] = read(board,'status layers','DISPLAY_LAYERS')
        return rendered_show(completed,proof['status'],proof['layers'],body)
    wait_until(visible,3)
    return dict(sent=sent, ack=ack, terminal=completed, render_proof=proof,
                started=started, completed_at=time.monotonic())


def merge_gate(result, serial_text):
    runs = result.get('runs', [])
    samples = []
    for line in serial_text.splitlines():
        if line.startswith('PERF '):
            samples.append(json.loads(line[5:]))
    memory = memory_gate(samples)
    monitor = result.get('display_monitor', {})
    from playback_quality import transport_gate
    # Independently recompute from raw snapshots, so old harness results cannot
    # qualify merely by omitting the whole-turn gate.
    whole_turn_transport=[transport_gate(r.get('before_perf',{}),r.get('after_perf',{})) for r in runs]
    checks = dict(whole_turn_transport=bool(runs) and all(g['passed'] for g in whole_turn_transport),three_runs=len(runs)>=3 and all(r.get('passed') for r in runs),
                  memory=memory['passed'], stack=(monitor.get('app_control_min_free') or 0)>=2048,
                  monitor=monitor.get('passed') is True,
                  reply_checks=bool(runs) and all(r.get('display_merge',{}).get('passed') and r.get('display_merge',{}).get('full_reply_fps',{}).get('passed') for r in runs),
                  uplink=result.get('link_tail',{}).get('passed') is True,
                  single_peer=len({r.get('pc_id') for r in runs})==1)
    return dict(passed=all(checks.values()), checks=checks, memory=memory, whole_turn_transport=whole_turn_transport)
