#!/usr/bin/env python3
"""CPU and block-level network decisions for step 8c; originals stay intact."""
import argparse
from collections import Counter
import json
from pathlib import Path
import numpy as np
from step8e0_analysis import trace_rows
from netprobe import sequence_extend


def distribution(values):
    values = list(values)
    if not values:
        return None
    return dict(n=len(values), p50=float(np.median(values)),
                p90=float(np.percentile(values, 90, method='lower')),
                minimum=min(values), maximum=max(values))


def cpu_windows(snapshots, minimum_seconds=20):
    # Several FreeRTOS workers share the name "pipecat". Task numbers are
    # stable in the fresh-boot census; combining their samples would hide
    # a regression in one worker behind another worker's idle time.
    duplicates = {name for snapshot in snapshots for name, count in
                  Counter(task['name'] for task in snapshot['tasks']).items() if count > 1}
    windows = []
    for before, after in zip(snapshots, snapshots[1:]):
        duration = after['device_us'] - before['device_us']
        if duration < minimum_seconds * 1e6:
            continue
        first = {task['id']: task for task in before['tasks']}
        tasks = []
        for task in after['tasks']:
            old = first.get(task['id'])
            if old is None or old['name'] != task['name']:
                continue
            delta = (task['runtime_us'] - old['runtime_us']) % 2**32
            if delta > duration * 1.05:
                raise ValueError('Impossible task runtime delta')
            name = task['name'] + '#' + str(task['id']) if task['name'] in duplicates else task['name']
            tasks.append(dict(name=name, core=task['core'], priority=task['priority'],
                              cpu=100 * delta / duration,
                              stack_margin_percent=task.get('stack_margin_percent')))
        windows.append(dict(duration_seconds=duration / 1e6, tasks=tasks,
                            sent=after['sent'] - before['sent'], received=after['received'] - before['received'],
                            send_errors=(after.get('send_errors',0)-before.get('send_errors',0))%2**32,
                            encoded=(after.get('encode_frames',after['sent'])-before.get('encode_frames',before['sent']))%2**32))
    return windows


def enqueue_interval(snapshots):
    """Remove warmup from rounded cumulative means when every send succeeds."""
    first,last=snapshots[0],snapshots[-1]
    count=last['sent']-first['sent']
    if count<=0 or any(row.get('send_errors')!=0 or 'enqueue_send_mean_us' not in row for row in (first,last)):
        return None
    total_delta=last['enqueue_send_mean_us']*last['sent']-first['enqueue_send_mean_us']*first['sent']
    return dict(mean_us_estimate=total_delta/count,rounding_error_bound_us=(last['sent']+first['sent'])/count,
                packets=count,basis='PERF interval; enqueue after encode to owner send, excluding PCM frame assembly',
                limitation='Counters are sampled separately; the bound covers integer mean rounding, not an in-flight send.')


def uplink_timestamp_check(records, frame_ms):
    """Opus RTP clock is 48 kHz; preserve skipped PCM/DTX time and seq wrap."""
    groups={}
    for row in records:
        groups.setdefault((row['pc_id'],row['ssrc']),[]).append(row)
    pairs=[]
    for group in groups.values():
        packets={}
        for row in sequence_extend(group,16):packets.setdefault(row['extended_seq'],row)
        ordered=[packets[key] for key in sorted(packets)]
        for left,right in zip(ordered,ordered[1:]):
            distance=right['extended_seq']-left['extended_seq']
            ticks=(right['timestamp']-left['timestamp'])%2**32
            pairs.append((ticks,distance))
    expected=frame_ms*48
    short=[dict(ticks=ticks,sequence_distance=distance) for ticks,distance in pairs if ticks<expected*distance]
    return dict(frame_ms=frame_ms,rtp_clock_hz=48000,expected_ticks_per_packet=expected,
                intervals=len(pairs),minimum_ticks_per_sequence=min((ticks/distance for ticks,distance in pairs),default=None),
                compressed_timeline_intervals=len(short),examples=short[:10],
                verified=not short if pairs else None)


def uplink_payload_summary(records, start_ns, end_ns):
    """Received codec bytes only; RTP/auth/IP/radio overhead is excluded."""
    packets={}
    for row in records:
        if start_ns<=row['observed_ms']*1_000_000<=end_ns:
            packets.setdefault((row['pc_id'],row['ssrc'],row['seq'],row['timestamp']),row)
    sizes=[row['payload_bytes'] for row in packets.values() if row.get('payload_bytes') is not None]
    complete=bool(packets) and len(sizes)==len(packets)
    seconds=(end_ns-start_ns)/1e9
    return dict(received_unique_packets=len(packets),packets_with_size=len(sizes),observable=complete,
                received_packets_per_second=len(packets)/seconds,
                codec_payload_bytes_total=sum(sizes) if complete else None,
                codec_payload_bytes_per_second=sum(sizes)/seconds if complete else None,
                bytes_per_packet=distribution(sizes),
                limitation='Received codec bytes within the decision window; lost payloads and transport/radio overhead are unobservable here.')


def summarize_cpu(directory):
    directory=Path(directory)
    arm=directory/'webrtc-ethernet'
    snapshots=json.loads((arm/'cpu-snapshots.json').read_text())
    windows=cpu_windows(snapshots)
    task_names=sorted({task['name'] for window in windows for task in window['tasks']})
    tasks={name:distribution(task['cpu'] for window in windows for task in window['tasks'] if task['name']==name)
           for name in task_names}
    metadata=json.loads((arm/'metadata.json').read_text())
    declared_frame=next((int(option.split('=')[1]) for option in metadata['options'] if option.startswith('frame-ms=')),None)
    frame_ms=snapshots[-1].get('uplink_frame_ms',declared_frame or 20)
    declared_dtx=next((bool(int(option.split('=')[1])) for option in metadata['options'] if option.startswith('dtx=')),None)
    dtx=snapshots[-1].get('opus_dtx',declared_dtx or False)
    option_match=(declared_frame is None or declared_frame==frame_ms) and (declared_dtx is None or declared_dtx==dtx)
    coverage=[dict(sent=window['sent'],encoded=window['encoded'],received=window['received'],send_errors=window['send_errors'],
                   minimum_sent=int(window['duration_seconds']*1000/frame_ms*.9667),
                   minimum_received=int(window['duration_seconds']*2900/60)) for window in windows]
    valid=bool(windows) and option_match and all(row['encoded']>=row['minimum_sent'] and
            (row['encoded']-row['send_errors']>=row['minimum_sent'] if dtx else row['sent']>=row['minimum_sent']) and
            row['received']>=row['minimum_received'] for row in coverage)
    return dict(path=str(directory),tasks=tasks,cpu_windows=windows,traffic_coverage=coverage,stimulus_complete=valid,
                actual_uplink_frame_ms=frame_ms,actual_opus_dtx=dtx,declared_options_match=option_match,
                encode_p50_us=snapshots[-1]['encode_p50_us'],encode_p95_us=snapshots[-1]['encode_p95_us'],
                enqueue_send_mean_us=snapshots[-1]['enqueue_send_mean_us'],
                enqueue_send_max_us=snapshots[-1]['enqueue_send_max_us'],heap=snapshots[-1]['heap'],
                steady_enqueue_send=enqueue_interval(snapshots),
                peer_profile=snapshots[-1]['peer_profile'])


def summarize(directory, *, attribution_file='rtp-attribution.json'):
    directory = Path(directory)
    arm = directory / 'webrtc-ethernet'
    snapshots = json.loads((arm / 'cpu-snapshots.json').read_text())
    trace = json.loads((arm / attribution_file).read_text())
    probe = json.loads((arm / 'analysis.json').read_text())
    before = json.loads((arm / 'device-before.json').read_text())
    after = json.loads((arm / 'device-after.json').read_text())
    samples=json.loads((arm/'device-samples.json').read_text())
    tx = json.loads((arm / 'wifi-tx-delta.json').read_text())
    minutes = trace['duration_minutes']
    windows = cpu_windows(snapshots)
    task_names = sorted({task['name'] for window in windows for task in window['tasks']})
    tasks = {name: distribution(task['cpu'] for window in windows for task in window['tasks'] if task['name'] == name)
             for name in task_names}
    uplink = trace['uplink']['streams']
    expected = sum(s['expected_sequence_span'] for s in uplink)
    lost = sum(s['lost_in_span'] for s in uplink)
    uplink_records=[json.loads(line) for line in (arm/'rtp/rtp.jsonl').read_text().splitlines()]
    # Use exactly the RTP decision window for the network rate. Keep the
    # wider PERF brackets separately, including their setup/teardown edges.
    bracket_rebuffers = snapshots[-1]['rebuffers'] - snapshots[0]['rebuffers']
    rebuffers = trace.get('playout_trace',{}).get('rebuffers',bracket_rebuffers)
    window_drift = trace.get('playout_trace', {}).get('drift_markers')
    def counter_delta(key):
        a = snapshots[0]['peer_profile']; b = snapshots[-1]['peer_profile']
        return b.get(key, 0) - a.get(key, 0)
    return dict(path=str(directory), minutes=minutes, tasks=tasks, cpu_windows=windows,
                plc_per_minute=trace['plc_frames'] / minutes, plc_frames=trace['plc_frames'],
                plc_boundaries=trace['plc_boundaries'], absent_socket=trace['rtp_absent_wire'],
                uplink_loss_rate=lost / expected if expected else None,
                uplink_missing=lost, uplink_sequence_span=expected,
                uplink_payload=uplink_payload_summary(uplink_records,*trace['window_host_ns']),
                rebuffers_per_hour=rebuffers * 60 / minutes,
                rebuffers=rebuffers, perf_bracket_rebuffers=bracket_rebuffers,
                rebuffers_boundary='RTP decision window' if 'playout_trace' in trace else 'PERF brackets (legacy analysis)',
                mailbox_full=after['udp_recvmbox_full'] - before['udp_recvmbox_full'],
                mailbox_by_port={str(row['local_port']):row['full']-next((old['full'] for old in before.get('udp_recvmbox_ports',[]) if old['local_port']==row['local_port']),0) for row in after.get('udp_recvmbox_ports',[])},
                mailbox_tracking_complete=after.get('udp_mailbox_untracked',0)==0 and after.get('udp_mailbox_untracked_ports',0)==0,
                path_stalls={name:{direction:record.get(direction,{}).get('stalls') for direction in ('downlink','uplink')} for name,record in probe.items()},
                tx_fail_fc={key: row['fail_fc'] for key, row in tx['buffer_deltas'].items()},
                probe_rtt_p99_ms=probe['esp-1']['rtt_ms']['p99'],
                peer_socket_enomem=counter_delta('socket_send_enomem'),
                peer_socket_errors=counter_delta('socket_send_errors'),
                heap=snapshots[-1]['heap'], rssi=[row.get('wifi') for row in [before]+[sample['stats'] for sample in samples]+[after]],
                trace_integrity=trace['trace_integrity_verified'],
                native_pts_calibration=trace['native_pts_calibration'],
                selected_pairs=trace['selected_pairs'],
                owner_slow_calls=trace.get('slow_calls'),
                auth_to_callback_ms=trace.get('auth_to_callback_ms'),
                authenticated_no_callback=trace['late_frames']['frames']-trace['late_frames']['callback_observed'],
                all_authenticated_callback_delivery=trace.get('callback_delivery'),
                callback_after_plc=trace['late_frames']['callback_observed'],
                playout_target_ms=[row.get('playout_target_ms') for row in snapshots],
                playout_drops=snapshots[-1]['playout_dropped']-snapshots[0]['playout_dropped'],
                playout_late=snapshots[-1]['late']-snapshots[0]['late'],
                playout_drift_drops=snapshots[-1]['drift_drops']-snapshots[0]['drift_drops'],
                window_drift_drops=window_drift,
                # PLC and intentional drift drops arise from distinct poll
                # actions. Late packet drops may duplicate earlier PLC and
                # must not be added again as newly damaged output frames.
                conceal_or_drift_per_minute=(trace['plc_frames']+window_drift)/minutes if window_drift is not None else None,
                playout_age_ms=distribution(row['playout_age_ms'] for row in snapshots),
                playout_depth=distribution(row['playout_queue'] for row in snapshots),
                i2s_underflows=snapshots[-1]['i2s_underflows']-snapshots[0]['i2s_underflows'],
                drift_events=sum(row['kind']==6 for row in trace_rows(arm/'trace-serial.log')),
                limitation='CPU uses device runtime/elapsed deltas. Uplink loss is within the received sequence span; endpoint loss is unobservable. Inclusive call timing is not exclusive CPU.')


def bootstrap_difference(a, b, seed=8, samples=20000):
    """Independent resampling of whole blocks, never individual RTP packets."""
    if not a or not b:
        raise ValueError('Both block populations are required')
    rng = np.random.default_rng(seed)
    left, right = np.asarray(a), np.asarray(b)
    differences = right[rng.integers(len(right), size=(samples, len(right)))].mean(axis=1) - \
                  left[rng.integers(len(left), size=(samples, len(left)))].mean(axis=1)
    return dict(mean_difference=float(right.mean() - left.mean()),
                ci95=list(map(float, np.percentile(differences, [2.5, 97.5]))),
                blocks_A=len(a), blocks_B=len(b), seed=seed, bootstrap_samples=samples)


def compare_cpu(ca, cb, cx, cy, *, spread_floor=None):
    """Independent fixed-stimulus windows, with explicit fresh-boot A/A spread."""
    cpu = {}
    populations = (ca, cb, cx, cy)
    expected = set.union(*[set(row['tasks']) for row in ca + cb + cx + cy])
    common = set.intersection(*[set(row['tasks']) for row in ca + cb + cx + cy])
    for name in sorted(common):
        values = lambda rows: [task['cpu'] for row in rows for window in row['cpu_windows']
                               for task in window['tasks'] if task['name'] == name]
        av, bv, x, y = map(values, (ca, cb, cx, cy))
        observed_spread = abs(float(np.median(x) - np.median(y)))
        spread = max(observed_spread,(spread_floor or {}).get(name,0))
        reduction = float(np.median(av) - np.median(bv))
        idle=name.startswith('IDLE')
        cpu[name] = dict(A=distribution(av), B=distribution(bv), AA_left=distribution(x), AA_right=distribution(y),
                         AA_p50_spread=spread, p50_reduction=reduction,
                         AA_observed_p50_spread=observed_spread,
                         idle_task=idle,measured_win=(-reduction if idle else reduction) > spread)
    windows = all(sum(len(row['cpu_windows']) for row in pop) >= 8 for pop in (ca, cb, cx, cy))
    incomplete = sorted(expected - common | {name for name, row in cpu.items()
        if any(row[key]['n'] < 8 for key in ('A', 'B', 'AA_left', 'AA_right'))})
    stimulus=all(row.get('stimulus_complete',True) for row in ca+cb+cx+cy)
    return dict(cpu=cpu,cpu_windows_sufficient=windows and bool(cpu) and not incomplete,
                cpu_task_coverage_complete=bool(cpu) and not incomplete,
                cpu_tasks_with_incomplete_coverage=incomplete,cpu_stimulus_complete=stimulus)


def quality_cpu_guard(result):
    """Apply the orchestrator's corrected materiality guard, retaining the old verdict.

    Non-idle increases need to exceed both matched A/A and 2 pp; idle
    decreases need to exceed both matched A/A and 5 pp. Coverage is required.
    """
    tasks = {}
    for name, row in result['cpu'].items():
        regression = row['p50_reduction'] if row['idle_task'] else -row['p50_reduction']
        matched=row.get('matched_AA_p50_spread',row.get('AA_observed_p50_spread',row['AA_p50_spread']))
        threshold=max(matched,5.0 if row['idle_task'] else 2.0)
        tasks[name] = dict(regression_pp=regression,matched_AA_p50_spread=matched,materiality_threshold_pp=threshold,
                          beyond_AA=regression > row['AA_p50_spread'],
                          material_regression=regression > threshold)
    bad = sorted(name for name, row in tasks.items() if row['beyond_AA'])
    material = sorted(name for name, row in tasks.items() if row['material_regression'])
    complete=bool(tasks) and result['cpu_windows_sufficient'] and result['cpu_stimulus_complete'] and result.get('cpu_task_coverage_complete',False)
    return dict(tasks=tasks,CPU_regressions_beyond_AA=bad,
                CPU_material_regressions=material,
                old_any_task_CPU_guard=complete and not bad,
                quality_adoption_CPU_guard=complete and not material)


def compare_network(a, b, aa_left, aa_right):
    """Judge whole duplex blocks independently of fixed-stimulus CPU windows."""
    network = {}
    for metric in ('plc_per_minute', 'uplink_loss_rate', 'rebuffers_per_hour'):
        get = lambda rows: [row[metric] for row in rows]
        if any(value is None or not np.isfinite(value)
               for rows in (a, b, aa_left, aa_right) for value in get(rows)):
            network[metric] = dict(observable=False,
                                   reason='At least one block has no observable rate')
            continue
        baseline = bootstrap_difference(get(aa_left), get(aa_right))
        candidate = bootstrap_difference(get(a), get(b))
        variation = max(abs(v) for v in baseline['ci95'])
        network[metric] = dict(observable=True, AA=baseline, AB=candidate, AA_variation=variation,
                               worse_beyond_AA=candidate['ci95'][0] > variation)
    enough = all(sum(row['minutes'] for row in pop) >= 60 for pop in (a, b))
    aa_blocks = all(len(pop)>=2 for pop in (aa_left,aa_right))
    observable = all(row['observable'] for row in network.values())
    return dict(network=network, network_duration_sufficient=enough,
                network_AA_blocks_sufficient=aa_blocks,
                network_rates_observable=observable,
                network_acceptable=enough and aa_blocks and observable and
                    not any(row['worse_beyond_AA'] for row in network.values()))


def compare_quality_network(a, b, aa_left, aa_right):
    """Qualify a mailbox/cache quality win without hiding displaced damage.

    A boundary improvement alone is insufficient: PLC, uplink loss,
    rebuffers and disjoint PLC+drift output actions must remain acceptable.
    The independent materiality CPU guard and reported RAM cost are separate
    requirements for adoption.
    """
    result=compare_network(a,b,aa_left,aa_right)
    metrics={}
    getters={
        'absent_socket_per_minute':lambda row:row.get('absent_socket')/row['minutes'] if row.get('absent_socket') is not None else None,
        'all_authenticated_no_callback_per_minute':lambda row:
            row['all_authenticated_callback_delivery']['without_callback']/row['minutes']
            if (row.get('all_authenticated_callback_delivery') or {}).get('observable') else None,
        'conceal_or_drift_per_minute':lambda row:row.get('conceal_or_drift_per_minute'),
    }
    for name,get in getters.items():
        populations=[[get(row) for row in rows] for rows in (a,b,aa_left,aa_right)]
        if any(value is None or not np.isfinite(value) for rows in populations for value in rows):
            metrics[name]=dict(observable=False)
            continue
        baseline=bootstrap_difference(*populations[2:])
        candidate=bootstrap_difference(*populations[:2])
        variation=max(abs(value) for value in baseline['ci95'])
        metrics[name]=dict(observable=True,AA=baseline,AB=candidate,AA_variation=variation,
                           worse_beyond_AA=candidate['ci95'][0]>variation,
                           improvement_beyond_AA=candidate['ci95'][1]<-variation)
    improvements=[name for name,row in metrics.items() if row.get('improvement_beyond_AA')]
    plc=result['network']['plc_per_minute']
    if plc['observable'] and plc['AB']['ci95'][1]<-plc['AA_variation']:
        improvements.append('plc_per_minute')
    output_ok=result['network_acceptable'] and all(row['observable'] and not row['worse_beyond_AA']
                                                   for row in metrics.values())
    result.update(quality_boundaries=metrics,output_quality_acceptable=output_ok,
                  measured_quality_improvements=improvements,
                  quality_network_qualified=output_ok and bool(improvements))
    return result


def compare_network_screen(a, b, aa_left, aa_right):
    """User-requested single five-minute A/B; raw counts, without bootstrap CIs.

    Retained baseline A/A point differences describe variation. One block
    cannot establish a confidence interval or a recurring stall period.
    """
    getters={
        'plc_per_minute':lambda r:r.get('plc_per_minute'),
        'uplink_loss_rate':lambda r:r.get('uplink_loss_rate'),
        'rebuffers_per_hour':lambda r:r.get('rebuffers_per_hour'),
        'conceal_or_drift_per_minute':lambda r:r.get('conceal_or_drift_per_minute'),
    }
    metrics={}
    for name,get in getters.items():
        values=[get(r) for r in (a,b,aa_left,aa_right)]
        observable=all(v is not None and np.isfinite(v) for v in values)
        if not observable:
            metrics[name]=dict(observable=False)
            continue
        av,bv,x,y=values
        spread=abs(y-x)
        metrics[name]=dict(observable=True,A=av,B=bv,AA_point_spread=spread,
                           difference_B_minus_A=bv-av,worse_beyond_retained_AA=bv-av>spread)
    complete=all(r['observable'] for r in metrics.values())
    duration=all(r['minutes']>=5 for r in (a,b))
    return dict(method='User decision 18:40: one 310-second block per arm, no bootstrap CIs',
                metrics=metrics,duration_sufficient=duration,
                network_acceptable=duration and complete and not any(r.get('worse_beyond_retained_AA',False) for r in metrics.values()),
                raw_A=a,raw_B=b,AA_sources=[aa_left['path'],aa_right['path']],
                limitation='Single-block comparison; stall presence and time-of-day can confound differences. No confidence interval or long-run quality claim.')


def compare_voice(a, b, stt_a, stt_b):
    """Five original offline turns; user permits at most 100 ms p50 reply rise."""
    def arm(voice,stt):
        values=[r.get('latency_ms',{}).get('audible_reply') for r in voice['runs']]
        scores=[r['word_accuracy'] for r in stt['runs']]
        complete=len(values)==len(scores)==5 and all(v is not None and np.isfinite(v) for v in values+scores)
        return dict(complete=complete,latency_per_turn_ms=values,
                    latency=distribution(v for v in values if v is not None),
                    STT_word_accuracy_per_turn=scores,
                    STT_mean_word_accuracy=float(np.mean(scores)) if scores else None,
                    transcripts=[r['transcript'] for r in stt['runs']])
    left,right=arm(a,stt_a),arm(b,stt_b)
    complete=left['complete'] and right['complete'] and a['passed'] and b['passed']
    latency_ok=complete and right['latency']['p50']<=left['latency']['p50']+100
    stt_ok=complete and right['STT_mean_word_accuracy']>=left['STT_mean_word_accuracy']
    return dict(A=left,B=right,reply_latency_p50_allowance_ms=100,
                reply_latency_acceptable=latency_ok,STT_accuracy_acceptable=stt_ok,
                passed=latency_ok and stt_ok,
                basis='User decision: candidate p50 audible reply <= baseline p50 +100 ms; no additional transcript errors. Five original turns, no rerun substitution.')


def compare(a, b, aa_left, aa_right, *, cpu_a=None, cpu_b=None, cpu_aa_left=None, cpu_aa_right=None,cpu_spread_floor=None):
    cpu=compare_cpu(a if cpu_a is None else cpu_a,b if cpu_b is None else cpu_b,
                    aa_left if cpu_aa_left is None else cpu_aa_left,
                    aa_right if cpu_aa_right is None else cpu_aa_right,spread_floor=cpu_spread_floor)
    return dict(**cpu, **compare_network(a, b, aa_left, aa_right))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture', type=Path)
    p.add_argument('--out',type=Path,help='New derived summary; defaults to summary-reanalysis.json in the capture')
    a = p.parse_args()
    result = summarize(a.capture)
    target=a.out or a.capture/'summary-reanalysis.json'
    with target.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:result[key] for key in ('path','minutes','plc_per_minute','uplink_loss_rate',
                                               'rebuffers_per_hour','mailbox_full','trace_integrity')},indent=2))
