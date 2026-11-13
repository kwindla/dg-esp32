#!/usr/bin/env python3
"""Independent AEC witnesses; no witness failure suppresses another subcommand.

See docs/echoear-aec-bench.md for run declarations, timing and metric contracts.
All outputs are immutable run directories under aec-demo/step-N; no retakes.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import time
import wave

import numpy as np
from echoear_aec_metrics import (RATE, FRAME, AFE_DELAY, activity, dbfs, echo_metrics,
                                 near_metrics, reduction, missing_playback, reference_threshold, post_opus_residual, interval_latency)
from echoear_aec_tts import tts_anchor, retained_tts_events
from echoear_aec_timing import sample_interval, transaction_anchor, map_interval, mapped_sample_coverage

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path.home()/'src/evidence/echoear/aec-demo'
PORT = '/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B4:3A:45:16:2D:94-if00'
# Bench restore-of-record (gap 3, after the step-10 requal-n1 acceptance r20 PASS of record): the qualified AEC-off normal
# image of master 16c45a3 (no autostart), the exact bytes the acceptance bound and boot-verified, kept in place in the
# frozen requal-n1 bundle directory (manifest-hashed; never rebuilt or copied).
NORMAL = Path.home()/'src/evidence/echoear/aec-demo/step-10/requal-n1-bundles/normal'
NORMAL_HASH = '55152f25dcec0e164c8a087d7f5962dd81191e2cdd3f41ac882a58a0a57a626e'
# The previous restore-of-record (9bc2d42 half-duplex normal), kept for frozen analyses of retained evidence only.
HISTORICAL_NORMAL_9936DA45 = Path.home()/'src/evidence/echoear/B/item2-master-9bc2d42-normal-image'
HISTORICAL_NORMAL_9936DA45_HASH = '9936da45a8c7dd6103c72f3fd3335e270659d4af6710bdc082a5d984a3feb758'
MODES = ('quiet', 'probe', 'speech', 'near', 'doubletalk', 'live')
# Step-2 live measurements: 50 ms polling caused uplink drops; 200 ms did not.
CLOCK_ANCHOR_PERIOD_S = .2


def json_scalar(value):
    if isinstance(value,np.generic):return value.item()
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


def write(path, data):
    Path(path).write_text(json.dumps(data, indent=2, allow_nan=False, default=json_scalar)+'\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_capture(path):
    with np.load(path, allow_pickle=False) as data:
        x = data['pcm'].astype(float)
        if int(data['rate']) != RATE or x.ndim != 2 or x.shape[1] not in (3, 5):
            raise ValueError('Expected complete EchoEar 16 kHz capture')
        timing = data['timing'].copy() if 'timing' in data else np.empty((0, 5))
    return x[:, 0], x[:, -1], x[:, 2 if x.shape[1] == 5 else 1], timing


def read_wav(path):
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, 2, 1):
            raise ValueError('Expected mono 16 kHz S16 fixture')
        return np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(float)


def write_wav(path, samples):
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        w.writeframes(np.rint(samples).astype('<i2').tobytes())


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def analyze(mode, capture, quiet, declaration, *, events=(), anchors=()):
    raw, afe, ref, blocks = load_capture(capture)
    qraw, qafe, qref, _ = load_capture(quiet)
    result = dict(mode=mode, qualification=False, definitions='echoear-aec-metrics-v1',
                  levels_dbfs=dict(raw=dbfs(raw), afe=dbfs(afe), mic3=dbfs(ref)),
                  clips=dict(raw=int(np.count_nonzero((raw<=-32768)|(raw>=32767))),
                             afe=int(np.count_nonzero((afe<=-32768)|(afe>=32767))),
                             mic3=int(np.count_nonzero((ref<=-32768)|(ref>=32767)))),
                  timing_blocks=len(blocks), gap_blocks=int(sum(row[4] for row in blocks)))
    mapped = [sample_interval(int(row[0]), blocks, anchors) for row in blocks]
    widths = [b-a for v in mapped if v is not None for a,b in [v]]
    result['timing'] = dict(mapped_blocks=len(widths), total_blocks=len(blocks),
                            max_uncertainty_s=max(widths) if widths else None,
                            method='interval-bracket-v1; no serial receipt/sample equivalence')
    result['latencies'] = []
    for event in declaration.get('latencies', []):
        start = (map_interval(event['device_us'], anchors) if 'device_us' in event
                 else event.get('server_interval_s'))
        end = sample_interval(event['end_sample'], blocks, anchors)
        result['latencies'].append(dict(name=event['name'], **interval_latency(start, end)))
    complete_stimulus = True
    windows = declaration.get('windows_samples')
    if windows is None and mode in ('probe', 'speech', 'doubletalk'):
        # Independent MIC3 onset and exact fixture extent. No AFE selection.
        mask = activity(ref, qref, [(0, len(ref))])
        onsets = np.flatnonzero(mask)
        if len(onsets):
            start = int(onsets[0]*FRAME)
            extent = 2*RATE if mode == 'probe' else declaration['far_samples']
            complete_stimulus = start+extent+AFE_DELAY <= len(ref)
            windows = [(start, min(len(ref), start+extent))]
        else:
            windows = []
    if mode == 'quiet':
        result['settled_quiet'] = dict(raw_dbfs=dbfs(raw), afe_dbfs=dbfs(afe), mic3_dbfs=dbfs(ref),
                                        equality_gate=False, reference_threshold_power=reference_threshold(ref))
    elif mode == 'near':
        if not windows:
            # Independent raw MIC1 activity inside the known, board-silent stimulus
            # capture; never select on processed output. Includes whole source span.
            active = np.flatnonzero(activity(raw, qraw, [(0, len(raw)-AFE_DELAY)]))
            windows = [(int(active[0]*FRAME), int((active[-1]+1)*FRAME))] if len(active) else []
        result['near_only'] = near_metrics(raw, afe, windows)
        result['board_silent'] = not bool(activity(ref, qref, [(0, len(ref))]).any())
        if not result['board_silent']:
            result['near_only'] = dict(scorable=False, reason='MIC3 active in near-only control')
    elif mode in ('probe', 'speech'):
        result['afe'] = echo_metrics(raw, afe, ref, qref, windows)
        if not windows:
            result['outcome'] = 'missing-playback'
            result['acoustically_scorable'] = False
    elif mode == 'doubletalk':
        # Mixture power is explicitly NOT near-end preservation.
        mask = activity(ref, qref, windows)
        result['raw_mixture_output_power_ratio'] = reduction(raw, afe, mask)
        result['stt'] = dict(scorable=False, reason='Run --transcribe or supply retained STT evidence')
        result['interruption_disabled'] = declaration.get('interruption_disabled') is True
    elif mode == 'live':
        server_boots = {e.get('host_boot_id') for e in events if e.get('kind') == 'session-start'}
        clock_verified = bool(declaration.get('host_boot_id')) and server_boots == {declaration['host_boot_id']}
        result['server_clock_verified'] = clock_verified
        mask = activity(ref, qref, [(0, len(ref))])
        active_intervals = [sample_interval(int(i*FRAME), blocks, anchors) for i in np.flatnonzero(mask)]
        all_active_mapped = all(x is not None for x in active_intervals)
        active_intervals = [x for x in active_intervals if x is not None]
        first = sample_interval(0, blocks, anchors)
        last = sample_interval(len(ref)-1, blocks, anchors)
        # Coverage must be proven complete and gap-free; no silence across missing data.
        coverage = mapped_sample_coverage(blocks, anchors) if not result['gap_blocks'] and all_active_mapped else None
        attempts = []
        starts = {e['frame_id']:e for e in reversed(events) if e.get('kind') == 'server-frame'
                  and e.get('frame') == 'BotStartedSpeakingFrame'
                  and e.get('boundary') != 'tts-written'}
        ordered_starts = sorted(starts.values(), key=lambda e: e['monotonic_ns'])
        for index, e in enumerate(ordered_starts):
            # Defined server boundary: synchronous direct-tap observation of the
            # speaking frame, on the same Linux monotonic clock as this runner.
            start = [e['monotonic_ns']/1e9, e['monotonic_ns']/1e9] if clock_verified else None
            onset = tts_anchor(events, e['monotonic_ns'],
                ordered_starts[index+1]['monotonic_ns'] if index+1 < len(ordered_starts) else None)
            onset_interval = onset['server_interval'] if clock_verified else None
            attempts.append(dict(frame_id=e['frame_id'], server_interval=start,
                **missing_playback(start, active_intervals, coverage),
                bot_started=dict(server_interval=start, **missing_playback(start, active_intervals, coverage)),
                non_silent_tts=dict(anchor=onset,
                    **missing_playback(onset_interval, active_intervals, coverage))))
        result['scoring_anchor'] = 'non_silent_tts'
        result['legacy_top_level_anchor'] = 'bot_started; server-to-DAC diagnostic only'
        result['non_silent_tts_counts'] = {name: sum(a['non_silent_tts']['outcome'] == name for a in attempts)
            for name in ('playback', 'missing-playback', 'timing-unscorable')}
        result['attempts'] = attempts
        result['denominator'] = len(attempts)
        result['missing_playback'] = sum(x['outcome']=='missing-playback' for x in attempts)
        result['unscorable_attempts'] = sum(x['outcome']=='timing-unscorable' for x in attempts)
        result['missing_playback_rate'] = result['missing_playback']/len(attempts) if attempts else None
        result['failed_attempts'] = sum(x['failed'] for x in attempts)
    result['post_opus'] = dict(scorable=False, reason='Requires recorder PCM with independent board/uplink sample mapping; missing media is not silence')
    result['windows_samples'] = windows
    if declaration.get('uplink_wav'):
        uplink = read_wav(declaration['uplink_wav'])
        mask = activity(ref, qref, windows or [(0, len(ref))])
        result['post_opus'] = post_opus_residual(uplink, mask, declaration.get('uplink_mapping', []))
        result['post_opus']['whole_recording_dbfs_diagnostic'] = dbfs(uplink)
        result['post_opus']['replies'] = []
        for start, end in windows or []:
            local = activity(ref, qref, [(start, end)])
            onset = np.flatnonzero(local)
            first = local.copy()
            if len(onset):
                first[onset[0]+25:] = False
            result['post_opus']['replies'].append(dict(
                whole=post_opus_residual(uplink, local, declaration.get('uplink_mapping', [])),
                first_500ms=post_opus_residual(uplink, first, declaration.get('uplink_mapping', []))))
    if mode == 'live' and not len(blocks):
        result['timing_reason'] = 'Legacy capture has no sample-time witnesses'
    if result['gap_blocks'] or not complete_stimulus:
        reason = 'Capture gaps' if result['gap_blocks'] else 'Incomplete known stimulus/delayed tail'
        result['continuity'] = reason+'; acoustic ratios diagnostic only'
        result['acoustically_scorable'] = False
        def invalidate(value):
            if isinstance(value, dict):
                if 'scorable' in value:
                    value.update(scorable=False, reason=reason)
                for child in value.values():
                    invalidate(child)
            elif isinstance(value, list):
                for child in value:
                    invalidate(child)
        for key in ('afe', 'near_only', 'raw_mixture_output_power_ratio', 'post_opus'):
            invalidate(result.get(key))
    return result


def transcribe_afe(capture, out):
    import sys
    sys.path.insert(0, str(ROOT/'server'))
    from settings import load_credentials
    import requests
    load_credentials(approved_only=True)
    _, afe, _, _ = load_capture(capture)
    wav = out/'afe-stt.wav'
    write_wav(wav, afe)
    response = requests.post('https://api.deepgram.com/v1/listen',
        params=dict(model='nova-3', smart_format='true'),
        headers={'Authorization': 'Token '+os.environ['DEEPGRAM_API_KEY'], 'Content-Type': 'audio/wav'},
        data=wav.read_bytes(), timeout=45)
    if response.status_code != 200:
        raise RuntimeError(f'STT HTTP {response.status_code}')
    payload = response.json()
    write(out/'stt.json', payload)
    return dict(scorable=True, model='nova-3', transcript=payload['results']['channels'][0]['alternatives'][0]['transcript'])


@contextmanager
def bench_lock():
    # Own the flock for acquisition AND restoration, inherited by serial helper.
    with open('/tmp/claude-1000/echoear-bench.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = os.environ.get('DG_BENCH_LOCK_FD')
        os.environ['DG_BENCH_LOCK_FD'] = str(lock.fileno())
        try:
            yield
        finally:
            if old is None:
                os.environ.pop('DG_BENCH_LOCK_FD', None)
            else:
                os.environ['DG_BENCH_LOCK_FD'] = old


# A boot must follow verification in this process, including repeated trial resets.
_VERIFIED_BOOT_GAINS = {NORMAL_HASH: None, HISTORICAL_NORMAL_9936DA45_HASH: None}


def declared_boot_gain(image, expected, expected_gain_db=None):
    if expected in (NORMAL_HASH, HISTORICAL_NORMAL_9936DA45_HASH):
        return None
    manifest = image/'manifest.json'
    data = json.loads(manifest.read_text()) if manifest.exists() else {}
    declared = data.get('boot_ref_gain_db', expected_gain_db)
    if type(declared) is not int or declared not in range(0, 34, 3):
        raise ValueError('AEC image requires declared boot_ref_gain_db (0..33 by 3)')
    if expected_gain_db is not None and declared != expected_gain_db:
        raise ValueError('Plan and image boot gain declarations disagree')
    return declared


def verify_bundle(image, expected, expected_gain_db=None):
    if sha(image/'dg-esp32.elf') != expected:
        raise ValueError('ELF identity mismatch')
    manifest = image/'manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text())
        for name, record in data.get('files', data.get('sha256', {})).items():
            wanted = record['sha256'] if isinstance(record, dict) else record
            if sha(image/name) != wanted:
                raise ValueError('Frozen image bundle mismatch: '+name)
    gain = declared_boot_gain(image, expected, expected_gain_db)
    config = json.loads((image/'flasher_args.json').read_text())
    hashes = {name:sha(image/name) for name in config['flash_files'].values()}
    _VERIFIED_BOOT_GAINS[expected] = gain
    return hashes


def jabra_card_zero():
    if Path('/proc/asound/card0/usbid').read_text().strip().lower() != '0b0e:0412':
        raise RuntimeError('Jabra is not ALSA card 0')


def flash(image, expected, out, label, expected_gain_db=None):
    from step9a_echoear import esptool_python
    hashes = verify_bundle(image, expected, expected_gain_db)
    write(out/(label+'-image.json'), dict(elf_sha256=expected, files=hashes, boot_ref_gain_db=_VERIFIED_BOOT_GAINS[expected]))
    config = json.loads((image/'flasher_args.json').read_text())
    args = [esptool_python(), '-m', 'esptool', '--chip', 'esp32s3', '--port', PORT,
            '--baud', '460800', '--after', 'no-reset', 'write-flash', *config['write_flash_args']]
    for address, name in config['flash_files'].items():
        args.extend([address, name])
    with (out/(label+'-flash.log')).open('w') as log:
        subprocess.run(args, cwd=image, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)


def boot(out, label, expected):
    from esp_serial import EspSerial
    if expected not in _VERIFIED_BOOT_GAINS:
        raise ValueError('Verify the image and declare its boot gain before boot')
    board = EspSerial(PORT, log_path=out/(label+'-serial.log'), reset_on_open=False)
    try:
        board.reset(timeout=20)
        board.wait_for('READY transport-console', 45, since=0)
        board.wait_for('elf_sha256='+expected, 5, since=0)
        gain = _VERIFIED_BOOT_GAINS[expected]
        if gain is not None:
            line = board.wait_for(r'MIC3_GAIN=0x[0-9a-fA-F]+ analog_db=\d+', 5, since=0)
            match = re.search(r'MIC3_GAIN=0x([0-9a-fA-F]+) analog_db=(\d+)', line)
            if int(match[2]) != gain or int(match[1], 16) != 0x10 | (gain // 3):
                raise ValueError('Boot analog_db/register disagrees with declared gain')
            write(out/(label+'-boot-gain.json'), dict(elf_sha256=expected,
                  declared_gain_db=gain, boot_line=line, verified=True))
        return board
    except BaseException:
        board.close()
        raise


def clock_anchor(board, *, timeout=3):
    cursor = board.cursor
    sent = time.monotonic_ns()
    seq = board.send('aec-clock')
    line = board.wait_for(rf'^AEC_CLOCK seq={seq} device_us=\d+$', timeout, since=cursor)
    received = time.monotonic_ns()
    board.wait_for(rf'^OK #{seq} aec-clock$', timeout, since=cursor)
    return transaction_anchor(int(line.rsplit('=', 1)[1]), sent, received)


def acquire(mode, plan, out):
    from alsa import resolve_jabra, play_wav
    from echoear_aec_capture import dump_capture
    from echoear_aec_resource_trial import command, snapshot
    from step7_support import BotServer
    from step9a_echoear import check_esptool
    check_esptool()  # Fail before bench ownership or attempting any flash/restore.
    result = dict(mode=mode, qualification=False, cleanup_errors=[], acquisition_complete=False)
    plan['host_boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    board = bot = None
    anchors = []
    with bench_lock():
        # Validate restoration bundle before touching the candidate.
        verify_bundle(NORMAL, NORMAL_HASH)
        card, device = resolve_jabra()
        jabra_card_zero()
        mixer_file = out/'mixer-before.state'
        subprocess.run(['alsactl', '-f', str(mixer_file), 'store', '0'], check=True, capture_output=True)
        try:
            flash(Path(plan['image']), plan['elf_sha256'], out, 'candidate', plan.get('boot_ref_gain_db'))
            board = boot(out, 'candidate', plan['elf_sha256'])
            for text in ('demo hold', 'audio 0', 'quiet 1', 'gate auto', f"playback-volume {plan['volume_db']}"):
                command(board, text, 40)
            command(board, f"aec-ref-gain {plan['ref_gain_db']}")
            time.sleep(1)
            anchors.append(clock_anchor(board))
            command(board, 'aec-record-slots 1')
            time.sleep(2)
            command(board, 'aec-record-slots 0')
            dump_capture(board, out/'quiet.npz')
            snapshot(board, out, 'quiet-perf')
            near = Path(plan.get('near_fixture', ROOT/'server/fixtures/prompt_france.wav'))
            if mode in ('speech', 'doubletalk'):
                bank = out/'fixtures'; bank.mkdir()
                far = read_wav(ROOT/'server/fixtures/greeting.wav')
                far = np.tile(far, 4)
                write_wav(bank/'tts_1.wav', far)
                write_wav(bank/'tts_2.wav', far)
                plan['far_samples'] = len(far)
                if plan['seconds'] < len(far)/RATE+1:
                    raise ValueError('Capture duration must include full fixture plus delayed AFE tail')
                plan['interruption_disabled'] = True  # offline-test has no Flux/user interruptions
                os.environ.update(OFFLINE_SKIP_GREETING='1', OFFLINE_BENCH_FIXTURE_DIR=str(bank))
            if mode in ('near', 'doubletalk', 'live'):
                near_samples = read_wav(near)
                if plan['seconds'] < len(near_samples)/RATE+3:
                    raise ValueError('Capture too short for near fixture and tail')
                if mode == 'doubletalk' and len(far)/RATE < len(near_samples)/RATE+3:
                    raise ValueError('Far fixture must sustain the full near utterance')
                result['near_fixture_sha256'] = sha(near)
            if mode in ('speech', 'doubletalk', 'live'):
                os.environ['ECHOEAR_AEC_SESSION_DIR'] = str(out/'sessions')
                os.environ.pop('ECHOEAR_AEC_FULL_DUPLEX', None)   # decision 141: removed; demo_bot refuses it
                os.environ.pop('ECHOEAR_AEC_EVALUATION_DIR', None)
                bot = BotServer(out/'bot', mode='offline-test', entrypoint='demo_bot.py' if mode=='live' else 'bot.py')
                bot.start()
                command(board, 'server-url '+plan['server_url'])
                cursor, _ = command(board, 'connect', 90)
                board.wait_for('STATE Ready', 15, since=cursor)
                command(board, 'audio 1')
            # Start anchor sufficiently before the first sample's DMA residence.
            anchors.append(clock_anchor(board)); time.sleep(.2)
            command(board, 'aec-record-slots 1'); time.sleep(.5)
            if mode == 'probe':
                command(board, 'aec-probe', 8)
            elif mode in ('speech', 'doubletalk'):
                command(board, 'play-fixture tts_1.wav')
            with ThreadPoolExecutor(max_workers=1) as pool:
                def near_play():
                    if mode == 'doubletalk':
                        time.sleep(2)
                    return play_wav(near, device)
                future = pool.submit(near_play) if mode in ('near', 'doubletalk', 'live') else None
                end = time.monotonic()+plan['seconds']
                while time.monotonic() < end:
                    anchors.append(clock_anchor(board))
                    time.sleep(CLOCK_ANCHOR_PERIOD_S)
                if future:
                    result['jabra'] = future.result()
            command(board, 'aec-record-slots 0')
            anchors.append(clock_anchor(board))
            dump_capture(board, out/'capture.npz')
            snapshot(board, out, 'capture-perf')
            if bot:
                command(board, 'audio 0'); command(board, 'disconnect', 40)
                bot.stop(); bot = None
            events = []
            for p in (out/'sessions').glob('session-*/events.jsonl'):
                events.extend(retained_tts_events(jsonl(p), p.with_name('tts.wav')))
            session_wavs = list((out/'sessions').glob('session-*/uplink.wav'))
            if not session_wavs:
                session_wavs = list((out/'bot'/'input').glob('*.wav'))
            if len(session_wavs) == 1:
                plan['uplink_wav'] = str(session_wavs[0])
            result.update(analyze(mode, out/'capture.npz', out/'quiet.npz', plan, events=events, anchors=anchors))
            result['acquisition_complete'] = True
        except BaseException as exc:
            result['error'] = type(exc).__name__+': '+str(exc)
            raise
        finally:
            # Independent cleanup: no bot/serial/mixer failure skips normal restore.
            for label, fn in [('bot', lambda: bot.stop() if bot else None),
                              ('serial', lambda: board.close() if board else None)]:
                try:
                    fn()
                except Exception as exc:
                    result['cleanup_errors'].append(label+': '+str(exc))
            try:
                flash(NORMAL, NORMAL_HASH, out, 'restore')
                restored = boot(out, 'restore', NORMAL_HASH)
                restored.close()
                result['restored_elf_sha256'] = NORMAL_HASH
            except Exception as exc:
                result['cleanup_errors'].append('RESTORE: '+str(exc))
            try:
                subprocess.run(['alsactl', '-f', str(mixer_file), 'restore', '0'], check=True, capture_output=True)
                subprocess.run(['alsactl', '-f', str(out/'mixer-after.state'), 'store', '0'], check=True, capture_output=True)
                if mixer_file.read_bytes() != (out/'mixer-after.state').read_bytes():
                    raise RuntimeError('Mixer restoration differs')
            except Exception as exc:
                result['cleanup_errors'].append('mixer: '+str(exc))
            write(out/'clock-anchors.json', anchors)
            write(out/'acquisition.json', result)
            write(out/'resolved-plan.json', plan)
    if result['cleanup_errors']:
        raise RuntimeError('Cleanup failed; see retained acquisition.json')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='mode', required=True)
    for name in MODES:
        sub = subs.add_parser(name)
        sub.add_argument('--plan', type=Path, required=True)
        sub.add_argument('--out', type=Path, required=True)
        sub.add_argument('--capture', type=Path, help='Analyze retained capture; no hardware')
        sub.add_argument('--quiet', type=Path)
        sub.add_argument('--events', type=Path)
        sub.add_argument('--anchors', type=Path)
        sub.add_argument('--tts-wav', type=Path, help='Retained TTS PCM for legacy chunk anchors')
        sub.add_argument('--transcribe', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to(EVIDENCE) or not re.fullmatch(r'step-\d+', out.relative_to(EVIDENCE).parts[0]):
        parser.error('Output must be under evidence/echoear/aec-demo/step-N/')
    plan = json.loads(args.plan.read_text())
    if plan.get('mode') != args.mode or not re.fullmatch(r'[A-Za-z0-9_-]+', plan.get('run_id', '')):
        parser.error('Predeclare matching mode and unique run_id')
    if len(out.relative_to(EVIDENCE).parts) != 2 or out.name != plan['run_id']:
        parser.error('Use step-N/run_id as the immutable output directory')
    if plan.get('afe_configuration') != 'esp-sr-2.4.7-MR-filter4-FD_LOW_COST-chunk512':
        parser.error('Changed AFE/framer configuration requires independent delay remeasurement')
    if args.capture and args.mode != 'quiet' and not args.quiet:
        parser.error('A separate per-gain settled quiet capture is required')
    if not args.capture:
        if plan.get('volume_db') not in (0, -6) or type(plan.get('ref_gain_db')) is not int or plan['ref_gain_db'] not in range(0, 34, 3):
            parser.error('Reference gain must be 0..33 by 3 dB; volume 0/-6')
        if not 3 <= plan.get('seconds', 0) <= 20:
            parser.error('Acquisition duration must be 3..20 seconds')
        if args.mode in ('speech', 'doubletalk', 'live') and not re.fullmatch(r'http://[A-Za-z0-9.\-]+:7861', plan.get('server_url', '')):
            parser.error('Bot URL must use port 7861')
    out.mkdir(parents=True, exist_ok=False)
    write(out/'declaration.json', plan)
    write(out/'identity.json', dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
        host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        sources={str(p.relative_to(ROOT)):sha(p) for p in [Path(__file__), ROOT/'hil/echoear_aec_metrics.py', ROOT/'hil/echoear_aec_timing.py', ROOT/'server/aec_session_recorder.py', ROOT/'hil/echoear_aec_tts.py']},
        dependencies={name:importlib.metadata.version(name) for name in ('pipecat-ai','aiortc','numpy','scipy')},
        input_sha256={str(p):sha(p) for p in (args.plan,args.capture,args.quiet,args.events,args.anchors,args.tts_wav) if p}))
    try:
        if args.capture:
            result = analyze(args.mode, args.capture, args.quiet or args.capture, plan,
                             events=(retained_tts_events(jsonl(args.events), args.tts_wav) if args.events and args.tts_wav
                                     else jsonl(args.events) if args.events else []),
                             anchors=json.loads(args.anchors.read_text()) if args.anchors else [])
        else:
            result = acquire(args.mode, plan, out)
        if args.mode == 'doubletalk' and (args.transcribe or not args.capture):
            result['stt'] = transcribe_afe(args.capture or out/'capture.npz', out)
        write(out/'result.json', result)
    except BaseException as exc:
        write(out/'failure.json', dict(error=type(exc).__name__+': '+str(exc), retained=True))
        raise
    print(json.dumps(dict(output=str(out), qualification=False)))


if __name__ == '__main__':
    main()


# Shared by new integrated holds. The delay protects the first read's preceding
# DMA-completion bound; the final transaction follows record-stop.
def start_anchored_capture(board, command):
    anchor = clock_anchor(board)
    time.sleep(.2)
    command(board, 'aec-record 1')
    return anchor


def stop_anchored_capture(board, command):
    command(board, 'aec-record 0')
    return clock_anchor(board)
