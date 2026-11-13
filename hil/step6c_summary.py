#!/usr/bin/env python3
"""Retain formal A/B populations and apply phase-2 bounds to server Opus modes."""
from collections import Counter
import argparse
import json
from pathlib import Path

import numpy as np
from phase2_campaign import cohort, compare, distribution, write_json
from phase2_cpu import summarize as cpu_summary
from transport_test import records
from phase2_endurance import uplink_summary


def population(directory):
    pop = cohort(directory)
    cpu = cpu_summary(sorted(directory.glob('boot*/serial.log')))
    pop['cpu'] = cpu
    for task in ['audio-playback/core1', 'audio-tx/core1', 'peer-loop/core0', 'peer-rx/core1']:
        pop['metrics']['cpu_'+task] = cpu['task_cpu'].get(task)
    hist = Counter()
    wire_sessions = []
    for sample in pop['samples']:
        hist.update(sample['closed']['received_payload_size_histogram'])
        sent = [p for p in records(directory/sample['boot']/'downlink.jsonl')
                if p['pc_id'] == sample['pc_id']]
        gaps = np.diff([p['send_ms'] for p in sent])
        speech_sized = [p for p in sent if p['payload_bytes'] > 12]
        wire_sessions.append(dict(boot=sample['boot'], index=sample['index'],
            packets=len(sent), payload_bytes=sum(p['payload_bytes'] for p in sent),
            kbps=sum(p['payload_bytes'] for p in sent)*8/(len(sent)*.02)/1000,
            speech_sized_kbps=sum(p['payload_bytes'] for p in speech_sized)*8/(len(speech_sized)*.02)/1000,
            max_gap_ms=float(max(gaps)), p95_gap_ms=float(np.percentile(gaps, 95)),
            toc=dict(Counter(f'{p["toc_config"]}/{p["stereo"]}' for p in sent))))
    pop['received_payload_histogram'] = dict(sorted(hist.items(), key=lambda kv: int(kv[0])))
    pop['wire_sessions'] = wire_sessions
    for key in ['max_gap_ms', 'p95_gap_ms']:
        pop['metrics']['downlink_send_'+key] = distribution(s[key] for s in wire_sessions)
    pop['wire_silk_wb_mono'] = all(set(s['toc']) <= {f'{i}/False' for i in range(8, 12)} for s in wire_sessions)
    pop['wire_rate_le_36_kbps'] = all(s['kbps'] <= 36 and s['speech_sized_kbps'] <= 36 for s in wire_sessions)
    write_json(directory/'summary.json', pop)
    return pop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--soaks', type=Path, help='Also summarize the matched endurance captures')
    args = parser.parse_args()
    a, b = [population(args.directory/mode) for mode in ['aiortc', 'lean']]
    result = compare(a, b, expected=15)
    # CPU percentages have no millisecond/packet floor; allow 10% in the
    # median and separately require playback CPU to decrease for adoption.
    for key in (k for k in a['metrics'] if k.startswith('cpu_')):
        bound = a['metrics'][key]['p50'] * 1.1
        result['checks'][key] = b['metrics'][key]['p50'] <= bound
        result['metrics'][key]['p50_bound'] = bound
    # The 6a ready-memory-gain criterion does not apply to a server codec change.
    del result['checks']['ready_gain']
    result['checks'].update(
        decode_p95_drop=all(b['metrics']['decode_p95_us'][q] < a['metrics']['decode_p95_us'][q] for q in ['p50', 'p90']),
        playback_cpu_drop=b['metrics']['cpu_audio-playback/core1']['p50'] < a['metrics']['cpu_audio-playback/core1']['p50'],
        wire_silk_wb_mono=b['wire_silk_wb_mono'],
        wire_rate_le_36_kbps=b['wire_rate_le_36_kbps'],
        acoustic_greetings=all(b['extras'][f'greeting{i}'] for i in [1, 2]),
        app_loop=bool(b['extras']['loop']))
    result['passed'] = all(result['checks'].values())
    write_json(args.directory/'comparison.json', result)
    if args.soaks:
        soaks = {}
        for mode in ['aiortc', 'lean']:
            directory = args.soaks/mode
            soak = json.loads((directory/'result.json').read_text())
            previous = soak['base']
            blocks = []
            downlink_blocks = []
            packets = records(directory/'rtp.jsonl')
            downlinks = records(directory/'downlink.jsonl')
            for row in soak['minutes']:
                if row['minute'] % 10:
                    continue
                start_ms = (previous['observed_unix_s']+2208988800)*1000
                end_ms = (row['observed_unix_s']+2208988800)*1000
                uplink = [p for p in packets if p['pc_id'] == soak['base']['perf']['pc_id']
                          and start_ms < p['arrival_ms'] <= end_ms]
                blocks.append(dict(start_minute=previous.get('minute', 0), end_minute=row['minute'],
                    server_uplink=uplink_summary(uplink)))
                sent = [p for p in downlinks if previous['observed_monotonic_s']*1000 < p['send_ms']
                        <= row['observed_monotonic_s']*1000]
                gaps = np.diff([p['send_ms'] for p in sent])
                downlink_blocks.append(dict(start_minute=previous.get('minute', 0), end_minute=row['minute'],
                    packets=len(sent), max_gap_ms=float(max(gaps)) if len(gaps) else None,
                    p95_gap_ms=float(np.percentile(gaps, 95)) if len(gaps) else None))
                previous = row
            soaks[mode] = dict(ten_minute_blocks=soak.get('ten_minute_blocks'),
                uplink_blocks=blocks, soak_deltas=soak.get('soak_deltas'),
                downlink_send_blocks=downlink_blocks,
                downlink_receiver_reports=records(directory/'downlink-rtcp.jsonl'),
                complete_minutes=len(soak['minutes']), checks=soak.get('checks'), error=soak.get('error'))
        write_json(args.soaks/'comparison.json', soaks)
    print(json.dumps(dict(passed=result['passed'], failed=[k for k, v in result['checks'].items() if not v]), indent=2))


if __name__ == '__main__':
    main()
