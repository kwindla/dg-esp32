#!/usr/bin/env python3
"""Step 5: causal, digital PCM/RTP SmallWebRTC acceptance (no microphone/I2S).

Starts an owned offline runner on 7861 unless --external-server is supplied.
Run after hil/flash.py. Raw artifacts and failed attempts stay in --out.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request
import wave
import numpy as np
from scipy import signal
from esp_serial import EspSerial, ROOT
from step6b_checks import playout_checks
sys.path.insert(0, str(ROOT / 'server/tests'))
from audio_analysis import correlation


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_until(fn, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('Timed out waiting for server evidence')


def fixture(pcm, name):
    with wave.open(str(ROOT / 'server/fixtures' / name)) as w:
        reference = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(float)
    assert len(pcm) >= len(reference), 'Incomplete fixture capture'
    score = correlation(pcm, reference)
    at = int(np.argmax(signal.correlate(pcm, reference, mode='valid', method='fft')))
    def active(x):
        rms = np.sqrt(np.convolve(x*x, np.ones(320)/320, mode='valid'))
        edges = np.flatnonzero(rms > max(80, rms.max() * .08))
        return int(edges[-1] - edges[0] + 320) if len(edges) else 0
    expected = active(reference)
    observed = active(pcm[max(0, at-320):at+len(reference)+320])
    # Preserve a speech-onset window for the silence-suppression comparison.
    # It supplements, and does not replace, the existing whole-fixture gate.
    envelope=np.sqrt(np.convolve(reference*reference,np.ones(320)/320,mode='valid'))
    edges=np.flatnonzero(envelope>max(80,envelope.max()*.08))
    begin=int(edges[0]) if len(edges) else 0
    x=pcm[at+begin:at+begin+1600];y=reference[begin:begin+1600]
    onset=dict(samples=len(x),reference_offset_samples=begin,
        correlation=float(np.dot(x,y)/max(1,np.linalg.norm(x)*np.linalg.norm(y))),
        rms=float(np.sqrt(np.mean(x*x))),
        peak_step=float(np.max(np.abs(np.diff(x)))) if len(x)>1 else 0)
    return dict(correlation=float(score), duration=observed/16000, expected_duration=expected/16000,
                onset_100ms=onset, passed=bool(score >= .95 and abs(observed/expected - 1) <= .05))


def perf(serial):
    cursor = serial.cursor
    serial.command('stats')
    return json.loads(serial.wait_for(r'PERF \{', 3, since=cursor).split('PERF ', 1)[1])


def dump(serial, directory):
    cursor = serial.cursor
    serial.command('dump-rx', timeout=120)
    entries = [x[2] for x in serial.lines_since(cursor)]
    begin = next(re.fullmatch(r'DUMP BEGIN samples=(\d+)', l) for l in entries if l.startswith('DUMP BEGIN'))
    count = int(begin[1]); data = bytearray()
    for line in entries:
        if line.startswith('PCM '):
            _, at, encoded = line.split(' ', 2)
            assert int(at) == len(data)//2, 'Missing/reordered serial PCM chunk'
            data.extend(base64.b64decode(encoded, validate=True))
    assert len(data) == count * 2, 'Incomplete PCM dump'
    (directory/'rx.pcm').write_bytes(data)
    with wave.open(str(directory/'rx.wav'), 'wb') as w:
        w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed')); w.writeframes(data)
    return np.frombuffer(data, '<i2').astype(float)


READY_LOG = r'(?:STATE Ready gen=\d+ desired=1 t=\d+|on_bot_ready t=\d+)'


def ready_timestamp(lines, last=True):
    # STATE is the app's authoritative transition; retain compatibility with
    # saved pre-app control images. USB logging can omit an adjacent line.
    times = re.findall(r'(?:STATE Ready gen=\d+ desired=1 t=|on_bot_ready t=)(\d+)', lines)
    return int(times[-1 if last else 0]) if times else None


def session(serial, out, index, initial_cursor, concurrent_sctp=False, directory=None):
    directory = directory or out / str(index); directory.mkdir(parents=True)
    cursor = initial_cursor if index == 1 else serial.cursor
    serial.command('connect', timeout=30)
    serial.wait_for(READY_LOG, 30, since=cursor)
    ready = perf(serial)
    pc_id = ready['pc_id']
    # Correlate both sides by the peer ID from the actual answer.
    def events(): return [e for e in records(out/'events.jsonl') if e['pc_id'] == pc_id]
    def messages(kind): return [e['message'] for e in events() if e['event'] == kind]
    inbound = wait_until(lambda: [m for m in messages('rtvi-in') if m.get('type') == 'client-ready'])
    wait_until(lambda: any(m.get('type') == 'bot-ready' and m.get('id') == inbound[0]['id'] for m in messages('rtvi-out')))
    # Let the greeting finish, then complete the fragmented-message round trips
    # before the duplex media trial. Large reliable SCTP traffic can stall the
    # native peer's RTP receive path; the media test measures steady streaming.
    time.sleep(4)
    for n in (1024, 15360, 17408):
        message_cursor = serial.cursor
        serial.command(f'message {n}', timeout=15)
        serial.wait_for(f'MESSAGE {"echoed" if n <= 16384 else "rejected"} bytes={n}', 3, since=message_cursor)
        if n <= 16384:
            incoming = wait_until(lambda: [m for m in messages('rtvi-in') if m.get('type') == 'client-message'
                                          and m.get('data', {}).get('t') == 'echo'
                                          and m.get('data', {}).get('d', {}).get('bytes') == n])
            assert len(json.dumps(incoming[0], separators=(',', ':'), ensure_ascii=False).encode()) == n
            wait_until(lambda: any(m.get('id') == incoming[0]['id'] and m.get('type') == 'server-response' for m in messages('rtvi-out')))
    before_play = serial.cursor
    serial.command('play', timeout=15)
    serial.wait_for('PLAY ACK', 5, since=before_play)
    play_sample = int(re.search(r'PLAY BEGIN samples=(\d+)', serial.wait_for('PLAY BEGIN', 5, since=before_play))[1])
    tone_cursor = serial.cursor
    tone_start = time.monotonic()
    serial.command('tone-tx')
    concurrent_message = None
    if concurrent_sctp:
        time.sleep(1)
        message_cursor = serial.cursor
        sent_at = time.monotonic()
        serial.command('message 15360', timeout=15)
        serial.wait_for('MESSAGE echoed bytes=15360', 3, since=message_cursor)
        concurrent_message = dict(bytes=15360, tone_offset_s=sent_at-tone_start,
                                  roundtrip_s=time.monotonic()-sent_at)
    tone_done = serial.wait_for(r'TONE DONE frames=\d+ accepted=\d+', 20, since=tone_cursor)
    produced_frames, accepted_samples = map(int,re.search(r'frames=(\d+) accepted=(\d+)',tone_done).groups())
    time.sleep(.4)
    serial.command('rtp-stats', timeout=10)
    stats = perf(serial)
    close_cursor = serial.cursor
    serial.command('disconnect', timeout=15)
    closed = json.loads(serial.wait_for(r'PERF \{', 5, since=close_cursor).split('PERF ', 1)[1])
    pcm = dump(serial, directory)
    greeting = fixture(pcm[:play_sample], 'greeting.wav')
    reply = fixture(pcm[play_sample:], 'reply_pineapple.wav')
    packets = [p for p in records(out/'rtp.jsonl') if p['pc_id'] == pc_id]
    assert packets, 'Missing server RTP evidence'
    first = packets[0]['seq']
    ordered = sorted(packets, key=lambda p: (p['seq']-first) % 65536)
    seq = np.array([(p['seq']-first) % 65536 for p in ordered])
    ts = np.array([p['timestamp'] for p in ordered], dtype=np.int64)
    times = np.array([p['arrival_ms'] for p in packets])
    rtp = dict(received=len(set(seq.tolist())), missing=500-len(set(seq.tolist())),
               max_gap_ms=int(np.diff(times).max()), p95_gap_ms=float(np.percentile(np.diff(times),95)), duration_ms=int(times[-1]-times[0]),
               timestamp_steps_correct=bool(np.all(np.diff(ts) % 2**32 == 960*np.diff(seq))),
               wire_missing_lower_bound=int(seq.max()+1-len(set(seq.tolist()))))
    input_path = out/'input'/f'{pc_id}.wav'
    with wave.open(str(input_path)) as w:
        tx = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(float)
    (directory/'tx.wav').write_bytes(input_path.read_bytes())
    rms = np.sqrt(np.mean(tx[:len(tx)//320*320].reshape(-1,320)**2, axis=1))
    active = np.flatnonzero(rms > 1000)
    assert len(active), 'Tone absent from server PCM'
    tone = tx[(active[0]+5)*320:(active[-1]-5)*320]
    freq = float(np.fft.rfftfreq(len(tone), 1/16000)[np.argmax(abs(np.fft.rfft(tone)))])
    tone_result = dict(produced_frames=produced_frames, accepted_samples=accepted_samples,
                       rejected_samples=produced_frames*320-accepted_samples, hz=freq, duration=(active[-1]-active[0]+1)*.02, total_duration=len(tx)/16000)
    telemetry = [e['message'].get('data', {}) for e in events() if e['event'] == 'rtvi-out'
                 and e['message'].get('type') == 'server-message' and tone_start <= e['t'] <= tone_start+12]
    speaking = any(m.get('t') == 'user-audio' and m.get('d', {}).get('speaking') for m in telemetry)
    captured = '\n'.join(entry[2] for entry in serial.lines_since(cursor))
    def last_time(pattern):
        values = re.findall(pattern,captured)
        return int(values[-1]) if values else None
    opened = last_time(r'PHASE open t=(\d+)')
    connected = last_time(r'PHASE connected t=(\d+)')
    ready_time = ready_timestamp(captured)
    wifi_time = last_time(r'WIFI READY t=(\d+)')
    start_ready = last_time(r'START_READY .*duration_us=(\d+)')
    setup = dict(start_ready_us=start_ready, open_connected_us=connected-opened if connected and opened else None,
                 wifi_to_ready_us=ready_time-wifi_time if wifi_time and ready_time else None)
    heap_rows = [dict(phase=m[1], **{k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',m[2])})
                 for m in re.finditer(r'(?:HEAP phase=|PHASE )(\S+) (.*)',captured)]
    region_rows = [dict(phase=m[1], kind=m[2], start=m[3], length=int(m[4]), free=int(m[5]), largest=int(m[6]), minimum=int(m[7]))
                   for m in re.finditer(r'REGION phase=(\S+) .*kind=(\S+) start=(\S+) length=(\d+) free=(\d+) largest=(\d+) minimum=(\d+)',captured)]
    result = dict(index=index, pc_id=pc_id, setup=setup, heap_rows=heap_rows, region_rows=region_rows, greeting=greeting, reply=reply, rtp=rtp,
                  tone=tone_result, speaking=speaking, stats=stats, closed=closed,
                  concurrent_message=concurrent_message, server_cleanup=False)
    (directory/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    # Retain the complete media sample even if asynchronous server teardown
    # misses its deadline. Cleanup remains an unchanged acceptance gate.
    wait_until(lambda: any(e['event'] == 'session-end' for e in events()), 15)
    result['server_cleanup'] = True
    (directory/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    assert produced_frames==500 and accepted_samples==160000, 'Tone source PCM backpressure'
    assert greeting['passed'] and reply['passed'], 'Fixture correlation/duration failed'
    assert rtp['received'] == 500 and rtp['missing'] == 0 and rtp['timestamp_steps_correct'], 'Server RTP loss/timing'
    assert abs(rtp['duration_ms'] - 9980) < 500 and rtp['max_gap_ms'] < 200, 'Server RTP pacing'
    assert abs(freq-1000) < 10 and abs(tone_result['duration']-8) <= .4, 'Tone frequency/duration'
    assert abs(tone_result['total_duration']-10) <= .5, 'Recorded stream duration'
    assert not any(m.get('data', {}).get('d', {}).get('bytes') == 17408
                   for m in messages('rtvi-in')), '17 KiB message reached server'
    assert stats['server_loss_known'] and stats['server_received'] == 500 and stats['server_lost'] == 0
    assert closed['tasks_exited'] and closed['decode_errors'] == 0 and closed['send_errors'] == 0
    assert closed['queue_dropped'] == 0, 'Local queue overflow or invalid frames'
    if 'target_depth' in closed: playout_checks(closed)
    for key, size in [('tx_stack',closed.get('tx_stack_bytes',32768)), ('decode_stack',closed.get('playback_stack_bytes',16384)), ('owner_stack',closed.get('owner_stack_bytes',10240)), ('rx_stack',closed.get('rx_stack_bytes',8192))]:
        assert closed[key] >= size*.25, f'{key} margin below 25%'
    # Silero is a speech detector: retain the observed tone VAD value, then
    # test speaking=true independently with the committed speech fixture.
    print(f'Session {index}: PASS, RTP={rtp["received"]}, max gap={rtp["max_gap_ms"]} ms', flush=True)
    return result


def speech_vad(serial, out):
    cursor = serial.cursor
    serial.command('connect', timeout=30)
    serial.wait_for(READY_LOG, 5, since=cursor)
    pc_id = perf(serial)['pc_id']
    cursor = serial.cursor
    started = time.monotonic()
    serial.command('speech-tx')
    line = serial.wait_for('SPEECH DONE', 15, since=cursor)
    frames, accepted = map(int, re.search(r'frames=(\d+) accepted=(\d+)', line).groups())
    assert accepted == frames * 320
    time.sleep(.6)
    events = [e for e in records(out/'events.jsonl') if e['pc_id'] == pc_id and e['t'] >= started]
    telemetry = [e['message'].get('data', {}) for e in events if e['event'] == 'rtvi-out'
                 and e['message'].get('type') == 'server-message']
    speaking = any(m.get('t') == 'user-audio' and m.get('d', {}).get('speaking') for m in telemetry)
    cursor = serial.cursor
    serial.command('disconnect', timeout=15)
    closed = json.loads(serial.wait_for(r'PERF \{', 5, since=cursor).split('PERF ', 1)[1])
    assert speaking, 'Speech fixture never produced server speaking=true'
    assert closed['tasks_exited'] and closed['decode_errors'] == 0 and closed['send_errors'] == 0
    return dict(pc_id=pc_id, frames=frames, speaking=speaking, closed=closed)


def pair_connect_heaps(heaps, expected_completed):
    """Keep failed-setup snapshots when an app command retries internally."""
    before=[free for phase,free,_ in heaps if phase=='before-connect']
    after=[free for phase,free,_ in heaps if phase=='after-disconnect']
    assert len(after)==expected_completed and len(before)>=len(after), 'Incomplete heap census'
    pairs=[];pending=[]
    for phase,free,_ in heaps:
        if phase=='before-connect':pending.append(free)
        elif phase=='after-disconnect':
            assert pending, 'Disconnect without a connect heap snapshot'
            pairs.extend((value,free) for value in pending);pending=[]
    assert not pending and len(pairs)==len(before), 'Unpaired heap census'
    return pairs


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', default='/dev/ttyACM0')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--external-server', action='store_true')
    p.add_argument('--sessions', type=int, default=3)
    p.add_argument('--elf',type=Path,default=ROOT/'build/dg-esp32.elf',help='Expected saved image ELF for interleaved measurements')
    p.add_argument('--no-speech', action='store_true', help='Single-session statistical outlier reruns only')
    p.add_argument('--concurrent-sctp', action='store_true', help='Echo 15 KiB during tone uplink and reply playback')
    p.add_argument('--rerun-failed', action='store_true', help='Retain and rerun each failed session once in the same boot')
    p.add_argument('--campaign', action='store_true', help='Collect every session after acceptance failures, retaining failures')
    p.add_argument('--exclude-reserve', action='store_true', help='Also enforce free-minus-configured-reserve floor')
    p.add_argument('--option',action='append',default=[],help='Disconnected bench option name=value')
    p.add_argument('--arrival-trace',action='store_true',help='Drain the step-8 observer after each session')
    a = p.parse_args(); out = a.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    proc = None; result = {'passed':False, 'sessions':[]}
    try:
        if not a.external_server:
            with socket.socket() as probe:
                assert probe.connect_ex(('127.0.0.1',7861)) != 0, '7861 already occupied; use an owned external runner'
            env = {k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
            env.update(BOT_MODE='offline-test', SERVER_PORT='7861', RECORD_INPUT='1', RECORD_INPUT_DIR=str(out/'input'),
                       EVENTS_PATH=str(out/'events.jsonl'), RTP_EVIDENCE_DIR=str(out), PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
            log = (out/'server.log').open('w')
            proc = subprocess.Popen([str(ROOT/'server/.venv/bin/python'), 'bot.py', '--host', '0.0.0.0', '--port','7861'],
                                    cwd=ROOT/'server', env=env, stdout=log, stderr=subprocess.STDOUT)
            def up():
                try: return urllib.request.urlopen('http://127.0.0.1:7861/', timeout=.5).status < 500
                except Exception: return False
            wait_until(up,30)
        with EspSerial(a.port, log_path=out/'serial.log') as serial:
            time.sleep(.2)  # Drain bytes left by the preceding flash/boot.
            cursor = serial.cursor; serial.reset()
            serial.wait_for('READY transport-console',40,since=cursor)
            boot = serial.wait_for(r'elf_sha256=([0-9a-f]{64})',5,since=cursor)
            expected = hashlib.sha256(a.elf.read_bytes()).hexdigest()
            assert re.search(r'elf_sha256=([0-9a-f]{64})',boot)[1] == expected, 'Running firmware differs from local ELF'
            for option in a.option:
                name,value=option.split('=');serial.command(f'peer-option {name} {int(value)}')
            for i in range(1,a.sessions+1):
                try:
                    result['sessions'].append(session(serial,out,i,cursor,a.concurrent_sctp))
                except AssertionError as exc:
                    if not a.campaign: raise
                    path = out/str(i)/'result.json'
                    item=json.loads(path.read_text()) if path.exists() else dict(index=i, capture_complete=False)
                    item['acceptance_error']=str(exc)
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text(json.dumps(item,indent=2)+'\n')
                    result['sessions'].append(item)
                    if a.rerun_failed:
                        repeat_dir = out/'reruns'/str(i)
                        try:
                            repeat = session(serial,out,i,serial.cursor,a.concurrent_sctp,repeat_dir)
                        except AssertionError as repeated:
                            path = repeat_dir/'result.json'
                            repeat = json.loads(path.read_text()) if path.exists() else dict(index=i, capture_complete=False)
                            repeat['acceptance_error'] = str(repeated)
                            path.parent.mkdir(parents=True,exist_ok=True)
                            path.write_text(json.dumps(repeat,indent=2)+'\n')
                        result.setdefault('reruns', []).append(repeat)
                if a.arrival_trace:
                    from arrival_trace import drain
                    drain(serial,out/'arrivals.jsonl')
            if not a.no_speech: result['speech_vad'] = speech_vad(serial, out)
            time.sleep(2)
            lines = '\n'.join(x[2] for x in serial.lines_since(cursor))
            assert not re.search(r'Guru Meditation|assert failed|abort\(\)|TRANSPORT FAIL|Task watchdog got triggered', lines)
            assert len(re.findall(r'ESP32-S3 step \d+ (?:transport|audio)', lines)) == 1, 'Unexpected reboot'
            source_stacks = [int(n) for n in re.findall(r'STACK task=tone-source free_bytes=(\d+)', lines)]
            # A completed pthread remains visible in task snapshots until join.
            assert len(source_stacks) >= a.sessions + (0 if a.no_speech else 1) and min(source_stacks) >= 8192*.25
            wifi = int(re.search(r'WIFI READY t=(\d+)',lines)[1])
            ready = ready_timestamp(lines, last=False)
            assert ready is not None, 'Missing readiness transition timestamp'
            result['wifi_to_ready_ms'] = (ready-wifi)/1000
            assert result['wifi_to_ready_ms'] < 5000
            heaps = [(phase,int(free),int(largest)) for phase,free,largest in
                     re.findall(r'HEAP phase=(\S+).*?internal=(\d+) largest=(\d+)',lines)]
            main_heaps=[(phase,int(free),int(largest)) for phase,free,largest in
                        re.findall(r'HEAP phase=(\S+).*?internal=(\d+).*?main_largest=(\d+)',lines)]
            assert main_heaps, 'Missing main-heap measurements; instrumented firmware required'
            assert all(free>=120*1024 and largest>=32*1024 for phase,free,largest in main_heaps if phase in {'ready','stats'})
            result['heaps_main']=main_heaps
            result['phase_heaps']=[dict(phase=m[1], **{k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',m[2])})
                                   for m in re.finditer(r'(?:HEAP phase=|PHASE )(\S+) (.*)',lines)]
            result['regions']=[dict(phase=m[1], kind=m[2], start=m[3], length=int(m[4]), free=int(m[5]), largest=int(m[6]), minimum=int(m[7]))
                for m in re.finditer(r'REGION phase=(\S+) .*kind=(\S+) start=(\S+) length=(\d+) free=(\d+) largest=(\d+) minimum=(\d+)',lines)]
            result['start_ready_us']=[int(n) for n in re.findall(r'START_READY .*duration_us=(\d+)',lines)]
            opens=[int(n) for n in re.findall(r'PHASE open t=(\d+)',lines)]
            connected=[int(n) for n in re.findall(r'PHASE connected t=(\d+)',lines)]
            result['open_connected_us']=[b-a for a,b in zip(opens,connected)]
            result['audio_counters']=[list(map(int,m)) for m in re.findall(r'AUDIO drops=(\d+) read_errors=(\d+) write_errors=(\d+)',lines)]
            before = [free for phase,free,_ in heaps if phase=='before-connect']
            after = [free for phase,free,_ in heaps if phase=='after-disconnect']
            if a.exclude_reserve:
                unreserved = [int(n) for n in re.findall(r'(?:HEAP phase=(?:ready|stats|streaming)|PHASE channel-open) .*excluding_reserve_lb=(\d+)', lines)]
                assert len(unreserved) >= a.sessions * 4, 'Missing reserve-excluded heap measurements'
                assert min(unreserved) >= 120*1024, 'Conservative reserve-excluded internal floor'
                result['minimum_excluding_reserve_lb'] = min(unreserved)
            result['heap_before']=before; result['heap_after']=after
            expected_completed=a.sessions+len(result.get('reruns',[]))+(0 if a.no_speech else 1)
            heap_pairs=pair_connect_heaps(heaps,expected_completed)
            result['heap_return_pairs']=heap_pairs
            assert all(abs(x-y)<=4096 for x,y in heap_pairs), 'Heap did not return within 4 KiB'
            result['passed']=not any('acceptance_error' in item for item in result['sessions'])
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        if proc:
            proc.terminate()
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
    print(('PASS' if result['passed'] else 'FAIL') + ': transport acceptance')
    if not result['passed']: raise SystemExit(1)

if __name__ == '__main__':
    main()
