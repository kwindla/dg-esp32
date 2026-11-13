"""Step 8c2 decisions from retained task identities and whole capture blocks."""
from collections import Counter
import json
from pathlib import Path
import numpy as np
from step8_summary import profile
from step8c_analysis import distribution, uplink_payload_summary


def pair_tasks(path):
    data=json.loads(Path(path).read_text())
    if not data.get('completed'):raise ValueError('Incomplete CPU pair campaign')
    names={t['name'] for run in data['runs'] for phase in run['task_profiles'] for t in phase['tasks']}
    duplicates={name for run in data['runs'] for phase in run['task_profiles']
                for name,count in Counter(t['name'] for t in phase['tasks']).items() if count>1}
    phases={}
    for phase_name in ('idle','duplex'):
        groups={arm:{} for arm in ('old','new')}
        for run in data['runs']:
            phase=next(p for p in run['task_profiles'] if p['name']==phase_name)
            for task in phase['tasks']:
                name=task['name']+('#'+str(task['id']) if task['name'] in duplicates else '')
                groups[run['label']].setdefault(name,[]).append(task['cpu_percent'])
        if set(groups['old'])!=set(groups['new']):raise ValueError('Incomplete task coverage')
        phases[phase_name]={name:dict(A=distribution(groups['old'][name]),B=distribution(groups['new'][name]),
            delta_pp=float(np.median(groups['new'][name])-np.median(groups['old'][name]))) for name in groups['old']}
    return phases


def aa_floor(path):
    """Use the larger aggregate median difference and paired absolute median.

    The latter keeps alternating-order cancellation from hiding pair noise.
    A/A contains eight fresh boots per arm, with both closed/open mic phases.
    """
    phases=pair_tasks(path)
    data=json.loads(Path(path).read_text())
    for phase_name,tasks in phases.items():
        for name,row in tasks.items():
            if min(row['A']['n'],row['B']['n'])<8:raise ValueError('Fewer than eight A/A windows per arm')
            paired=[]
            for pair in sorted({r['pair'] for r in data['runs']}):
                values={}
                for run in (r for r in data['runs'] if r['pair']==pair):
                    phase=next(p for p in run['task_profiles'] if p['name']==phase_name)
                    matches=[t for t in phase['tasks'] if t['name']==name or t['name']+'#'+str(t['id'])==name]
                    if len(matches)!=1:raise ValueError('Ambiguous A/A task')
                    values[run['label']]=matches[0]['cpu_percent']
                paired.append(abs(values['new']-values['old']))
            row.update(paired_absolute_deltas_pp=paired,
                noise_floor_pp=max(abs(row['delta_pp']),float(np.median(paired))))
    return phases


def guard(tasks, floors, minimum_windows=8):
    if set(tasks)!=set(floors):raise ValueError('A/A and A/B task populations differ')
    rows={}
    for name,row in tasks.items():
        if min(row['A']['n'],row['B']['n'])<minimum_windows:raise ValueError('Fewer than eight windows per arm')
        floor=floors[name]['noise_floor_pp'];idle=name.startswith('IDLE')
        threshold=max(floor,5.0 if idle else 2.0)
        regression=-row['delta_pp'] if idle else row['delta_pp']
        win=row['delta_pp'] if idle else -row['delta_pp']
        rows[name]=dict(**row,AA_noise_floor_pp=floor,threshold_pp=threshold,
            material_regression=regression>threshold,measured_win=win>floor)
    return dict(tasks=rows,passed=not any(r['material_regression'] for r in rows.values()))


def outage_verdict(blocks):
    """Predeclared rule: any >=1-second QIO outage and none on DIO rejects.

    Incomplete blocks never return QIO to qualification. Outage observations
    include leading/trailing absence, not only adjacent received-packet gaps.
    """
    if Counter(b['arm'] for b in blocks)!=Counter(qio=3,dio=3) or not all(b.get('complete') for b in blocks):
        return dict(complete=False,verdict='incomplete',requalify=False)
    outages={arm:sum(b['outages_ge_1s'] for b in blocks if b['arm']==arm) for arm in ('qio','dio')}
    rejected=outages['qio']>0 and outages['dio']==0
    return dict(complete=True,outages=outages,verdict='reject QIO' if rejected else
        'environmental outages; requalify QIO' if all(outages.values()) else
        'no QIO-only outage; requalify QIO',requalify=not rejected,
        rule='Any outage >=1 s on QIO with none on DIO rejects QIO; otherwise full gates required')


def absent_intervals(records,start_ns,end_ns,time_key):
    stamps=sorted({r[time_key] for r in records if start_ns<=r[time_key]<=end_ns})
    boundaries=[start_ns,*stamps,end_ns]
    return [dict(start_ns=a,end_ns=b,gap_ms=(b-a)/1e6,endpoint=i in (0,len(boundaries)-2))
            for i,(a,b) in enumerate(zip(boundaries,boundaries[1:])) if b-a>=1_000_000_000]


def crossover_decision(path,floor_path):
    data=json.loads(Path(path).read_text());floor=json.loads(Path(floor_path).read_text())
    if not data.get('completed'):raise ValueError('Incomplete crossover')
    samples={arm:{} for arm in ('A','B')};payload={arm:[] for arm in samples};heaps={arm:[] for arm in samples}
    for run in data['runs']:
        summary=run['summary'];arm=run['arm']
        if not summary['stimulus_complete']:raise ValueError('Incomplete fixed PCM coverage')
        for window in summary['cpu_windows']:
            for task in window['tasks']:samples[arm].setdefault(task['name'],[]).append(task['cpu'])
        payload[arm].append(summary['uplink_payload']);heaps[arm].append(summary['heap'])
    if set(samples['A'])!=set(samples['B']):raise ValueError('Task census changed between arms')
    rows={name:dict(A=distribution(samples['A'][name]),B=distribution(samples['B'][name]),
        delta_pp=float(np.median(samples['B'][name])-np.median(samples['A'][name]))) for name in samples['A']}
    extra=set(rows)-set(floor['duplex']);missing=set(floor['duplex'])-set(rows)
    if extra or missing:raise ValueError('A/A census mismatch: added '+', '.join(sorted(extra))+'; missing '+', '.join(sorted(missing)))
    decision=guard(rows,floor['duplex'])
    decision.update(name=data['name'],payload=payload,heap=heaps,arm_image_sets={arm:sorted({run['image'] for run in data['runs'] if run['arm']==arm}) for arm in samples})
    return decision


def cache_summary(snapshots):
    """Total shared miss rates; never mistake address classes for physical RAM."""
    rows=[];overflow=[]
    # The first PERF ends warmup. Later snapshots bracket scored CPU windows.
    for snapshot in snapshots[1:]:
        counter=snapshot.get('cache_counters')
        if counter is None:continue
        if counter['overflow_status']:
            overflow.append(counter);continue
        ia=counter['ibus_accesses'];da=counter['dbus_accesses']
        rows.append(dict(window_us=counter['window_us'],
            icache_miss_percent=100*counter['ibus_misses']/ia if ia else None,
            dcache_miss_percent=100*(counter['dbus_flash_misses']+counter['dbus_psram_misses'])/da if da else None,
            icache_misses_per_second=1e6*counter['ibus_misses']/counter['window_us'],
            dcache_misses_per_second=1e6*(counter['dbus_flash_misses']+counter['dbus_psram_misses'])/counter['window_us']))
    return dict(windows=rows,overflow_windows=overflow,complete=bool(rows) and not overflow,
        basis='Both cores combined; DBUS counters summed across address classes; no per-task or physical-backing attribution')


def device_payload_summary(snapshots):
    """Successful device codec sends; excludes RTP/SRTP/IP/radio overhead."""
    first,last=snapshots[0],snapshots[-1]
    duration=(last['device_us']-first['device_us'])/1e6
    packets=(last['sent']-first['sent'])%2**32
    byte_count=(last['bytes_sent']-first['bytes_sent'])%2**32
    return dict(seconds=duration,successful_codec_packets=packets,codec_bytes=byte_count,
        codec_bits_per_second=8*byte_count/duration if duration>0 else None,
        codec_bytes_per_packet=byte_count/packets if packets else None,
        send_errors=(last['send_errors']-first['send_errors'])%2**32,
        basis='Device PERF counter delta; successful codec sends only, with in-flight endpoint uncertainty; transport/radio overhead excluded')
