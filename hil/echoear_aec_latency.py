#!/usr/bin/env python3
"""Host-only retained TTS lead-in and transport-counter diagnosis; no qualification.

PCM offsets measure stored audio duration, not a wall-clock onset. Transport
input, successful write, RTP send and DAC completion are distinct boundaries.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

import numpy as np
from echoear_aec_bench import jsonl, load_capture, read_wav, write
from echoear_aec_metrics import dbfs, frame_power, activity
from echoear_aec_tts import TTS_POWER_THRESHOLD, retained_tts_events

COUNTERS = ('received', 'lost', 'late', 'concealed', 'playout_dropped',
    'decode_errors', 'decode_frames', 'i2s_underflows', 'i2s_write_errors',
    'i2s_write_timeouts', 'pcm_queue_drops', 'audio.drops', 'audio.read_errors',
    'audio.write_errors', 'audio.write_timeouts', 'aec.capture.rx_overflows',
    'aec.capture.read_timeouts', 'aec.capture.read_errors', 'playout.accepted',
    'playout.ingress', 'playout.decoded_packets', 'playout.missing_slots',
    'playout.plc_calls', 'playout.starvation', 'playout.drift_drop',
    'playout.drift_insert', 'playout.clock.completed_samples',
    'playout.clock.submitted_samples', 'playout.clock.device_silence_samples',
    'playout.rejected.late', 'playout.rejected.capacity', 'playout.rejected.window',
    'drop_reasons.admission_budget', 'drop_reasons.admission_overflow',
    'drop_reasons.admission_shape',
    'drop_reasons.encoded_queue', 'drop_reasons.uplink_gap', 'dropped')


def counter_deltas(before, after):
    def get(row, key):
        for part in key.split('.'):
            row = row.get(part) if isinstance(row, dict) else None
        return row
    same = (before.get('generation') == after.get('generation') and
            before.get('pc_id') == after.get('pc_id') and
            before.get('device_us', 0) <= after.get('device_us', 0))
    result = {}
    for key in COUNTERS:
        a, b = get(before, key), get(after, key)
        valid = same and type(a) is int and type(b) is int and b >= a
        result[key] = b-a if valid else None
    return dict(same_session=same, delta=result,
                unavailable='null means absent/reset; never zero-filled')


def pcm_lead(pcm):
    power = frame_power(pcm)
    active = np.flatnonzero(power > TTS_POWER_THRESHOLD)
    onset = int(active[0])*320 if len(active) else None
    # Independent PCM witness. This offset cannot be added to host arrival time
    # as a sample-time mapping without proving uninterrupted paced delivery.
    return dict(stored_seconds=len(pcm)/16000, peak_lsb=float(np.max(np.abs(pcm))),
                first_non_silent_pcm_s=onset/16000 if onset is not None else None,
                first_half_second_dbfs=dbfs(pcm[:8000]),
                prefix_dbfs=dbfs(pcm[:onset]) if onset else None,
                complete_frames=len(power), active_frames=len(active))


def cadence(events, key):
    dt = np.diff([e[key] for e in events])
    return dict(chunks=len(events), median_gap_s=float(np.median(dt)) if len(dt) else None,
                max_gap_s=float(np.max(dt)) if len(dt) else None)


def read(path):
    return json.loads(path.read_text())


def sol_case(root):
    events = [e for e in jsonl(root/'evaluation/events.jsonl')
              if e.get('case_id') == 'nominal-stop-1']
    chunks = [e for e in events if e.get('event') == 'tts-pcm']
    start = min(e['t'] for e in events if e.get('frame') == 'BotStartedSpeakingFrame')
    interrupted = min(e['t'] for e in events if e.get('frame') == 'InterruptionFrame')
    pcm = np.fromfile(root/'evaluation/nominal-stop-1.pcm', '<i2').astype(float)
    lead = pcm_lead(pcm)
    index = int(round(lead['first_non_silent_pcm_s']*16000))
    delivery = next(e for e in chunks if e['samples_total'] > index)
    raw, afe, ref, timing = load_capture(root/'nominal-stop-1.npz')
    _, _, quiet, _ = load_capture(root/'quiet.npz')
    ttfa = [dict(ttfa_s=float(a), leading_silence_s=float(b)) for a, b in re.findall(
        r'TTFA: ([0-9.]+)s \(([0-9.]+)s leading silence\)', (root/'live-bot.log').read_text())]
    return dict(case='nominal-stop-1', tts=lead, ttfa_in_session_order=ttfa,
        first_non_silent_chunk_after_bot_started_s=delivery['t']-start,
        interruption_after_bot_started_s=interrupted-start,
        tts_chunk_cadence=cadence(chunks, 't'),
        mic3=dict(peak_lsb=float(np.max(np.abs(ref))), rms_dbfs=dbfs(ref),
                  active_frames=int(activity(ref,quiet,[(0,len(ref))]).sum())),
        capture_seconds=len(ref)/16000, timing_rows=len(timing),
        capture_window_counters=counter_deltas(read(root/'cat-echo-sealed.json'), read(root/'nominal-stop-1-after.json')),
        full_case_counters=counter_deltas(read(root/'cat-echo-sealed.json'), read(root/'nominal-stop-1-sealed.json')),
        rtp_observer_health=[e for e in events if e.get('event') == 'rtp-observer-health'],
        missing_witnesses=['No session tts.wav; equivalent per-case evaluation PCM retained',
            'No step-1 timing rows/clock anchors; host command receipts are not sample times',
            'RTP observer reports zero packets and failed health; no send-boundary trace',
            'No independent RF trace or successful transport-write audio witness'],
        inference='Long silent TTS lead-in plus server interruption before its paced audible onset explains the observation; exact queue-to-DAC causality remains unproved. No repaired-playback claim.',
        qualification=False)


def live_cases(root):
    sessions = []
    for path in sorted((root/'sessions').glob('*/events.jsonl')):
        events = jsonl(path)
        sessions.append((path, retained_tts_events(events, path.with_name('tts.wav')),
                         read_wav(path.with_name('tts.wav'))))
    analysis = read(root/'analysis.json')
    rows = []
    for row in analysis['rows']:
        case = row['case']; folder = root/case['id']
        anchors = read(folder/'anchors.json')
        lo, hi = min(a['lower_s'] for a in anchors), max(a['upper_s'] for a in anchors)
        matches = [(path, [e for e in events if lo<=e['monotonic_ns']/1e9<=hi], pcm)
                   for path, events, pcm in sessions
                   if any(e.get('stream')=='tts' and lo<=e['monotonic_ns']/1e9<=hi for e in events)]
        if len(matches) != 1:
            raise ValueError('Expected one TTS session per retained case')
        path, events, pcm = matches[0]
        chunks = [e for e in events if e.get('kind')=='audio' and e.get('stream')=='tts']
        a, b = chunks[0]['sample_start'], chunks[-1]['sample_end']
        if any(x['sample_end'] != y['sample_start'] for x,y in zip(chunks,chunks[1:])):
            raise ValueError('TTS storage ranges are discontinuous')
        metrics = [m for e in events if e.get('kind')=='rtvi-output'
                   for m in (e.get('message') or {}).get('data',{}).get('ttfa',[])]
        attempt = row['live']['attempts'][0]
        bot = attempt['server_interval'][0]
        tts = attempt['non_silent_tts']['anchor']['server_interval'][0]
        rows.append(dict(case=case['id'], quiet_eligible=row['eligible_for_gain_selection'],
            tts=pcm_lead(pcm[a:b]), ttfa=metrics,
            chunk_cadence=cadence([dict(t=e['monotonic_ns']/1e9) for e in chunks], 't'),
            first_non_silent_chunk_after_bot_started_s=tts-bot,
            mic3_after_bot_started_s=[v-bot for v in row['first_reference_interval']],
            mic3_after_non_silent_chunk_s=[v-tts for v in row['first_reference_interval']],
            outcomes=dict(bot_started=attempt['outcome'], non_silent_tts=attempt['non_silent_tts']['outcome']),
            live_counters=counter_deltas(read(folder/'before-capture.json'),read(folder/'active-perf.json')),
            export_counters=counter_deltas(read(folder/'active-perf.json'),read(folder/'export-perf.json')),
            source_session=str(path.parent)))
    return rows


def finalist_loss(root):
    rows, sources = [], []
    for gain in (15, 24):
        source = root/f'finalist-v2-{gain}-analysis.json'
        sources.append(source)
        for cell in read(source)['rows']:
            if not cell.get('transport', {}).get('lost'):
                continue
            folder = root/cell['folder']
            sources.extend([folder/'quiet-perf.json', folder/'capture-perf.json'])
            before, after = read(folder/'quiet-perf.json'), read(folder/'capture-perf.json')
            rows.append(dict(gain_db=gain, cell=cell['run_id'], folder=str(folder),
                retained_transport=cell['transport'], playout=after['playout'],
                pcm_queue_drops=after['pcm_queue_drops'],
                i2s_underflows=after['i2s_underflows'],
                capture_rx_overflow_delta=after['aec']['capture']['rx_overflows']-before['aec']['capture']['rx_overflows'],
                capture_read_errors=after['aec']['capture']['read_errors'],
                capture_read_timeouts=after['aec']['capture']['read_timeouts'],
                counter_window='whole connected trial through capture export; warm conditioning may be included'))
    return dict(rows=rows, lost_by_gain={str(g):sum(r['retained_transport']['lost'] for r in rows if r['gain_db']==g) for g in (15,24)},
                limitation='Late admission/PLC localized; sender, network and receive scheduling not separated without matched RTP/RF traces'), sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sol', type=Path, required=True)
    parser.add_argument('--live', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    sol = sol_case(args.sol)  # Deliberately first: coordinator-directed diagnostic order.
    live = live_cases(args.live)
    losses, loss_sources = finalist_loss(args.live.parent)
    sources = loss_sources + [Path(__file__), Path(__file__).with_name('echoear_aec_tts.py')]
    for root in (args.sol, args.live):
        sources.extend(p for p in root.rglob('*') if p.is_file() and (
                       p.suffix in ('.npz','.pcm','.wav','.json','.jsonl') or p.name=='live-bot.log'))
    result = dict(qualification=False, method='Independent retained PCM/TTFA plus conservative MIC3 timing and counter deltas',
        sol=sol, live=live, finalist_loss=losses, input_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    write(args.out,result)
    print(json.dumps(dict(sol=sol['tts'], live=[dict(case=r['case'],tts=r['tts'],
        ttfa=r['ttfa'],onset=r['mic3_after_non_silent_chunk_s'],outcomes=r['outcomes']) for r in live]),indent=2))


if __name__ == '__main__':
    main()
