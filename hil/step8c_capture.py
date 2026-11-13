#!/usr/bin/env python3
"""One freshly booted duplex block for the step-8c crossover.

No builds run during a block. Addresses and diagnostic options are runtime
arguments. The bot, STUN responder and probes belong only to this invocation.
Raw records use the existing 8e0 attribution format; no Spark dependency.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import traceback
import urllib.request

from esp_serial import EspSerial, ROOT
import netprobe
from step8e0_campaign import dump_json, probe_stats
from step8e0_analysis import attribute_rtp, wifi_counters
from step8c_analysis import uplink_timestamp_check
from transport_test import perf


def capture(a):
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    directory = out / 'webrtc-ethernet'
    directory.mkdir()
    voice_host=a.wifi_host if a.voice_path=='wifi' else a.ethernet_host
    voice_interface='wlp8s0' if a.voice_path=='wifi' else 'enp7s0'
    metadata = dict(started_utc=datetime.now(timezone.utc).isoformat(),
                    duration=a.duration, options=a.option, fixed_mic=a.fixed_mic,
                    gate=a.gate, board_moved=False, reference_client=False,
                    cpu_window_seconds=a.cpu_window, label=a.label, cpu_only=a.cpu_only,
                    voice_path=a.voice_path,probe_echo_pps=[0,5],
                    rtp_payload_sizes=not a.cpu_only or getattr(a,'payload_sizes',False),
                    fixture_sha256=hashlib.sha256((ROOT/'server/fixtures/prompt_france.wav').read_bytes()).hexdigest() if a.fixed_mic else None)
    dump_json(directory / 'metadata.json', metadata)
    platform_file=a.elf.parent/'platform-settings.json'
    platform=json.loads(platform_file.read_text()) if platform_file.exists() else None
    processes = []
    logs = []
    stop = threading.Event()
    worker = None
    errors = []
    samples, snapshots = [], []
    def cpu_perf(board):
        began=time.perf_counter_ns()
        row=perf(board)
        row['host_before_ns']=began;row['host_after_ns']=time.perf_counter_ns()
        return row
    try:
        with socket.socket() as check:
            if check.connect_ex(('127.0.0.1', 7861)) == 0:
                raise RuntimeError('Port 7861 occupied before owned bot launch')
        def launch(command, log, **kwargs):
            stream = (directory / log).open('w')
            logs.append(stream)
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, **kwargs)
            processes.append(process)
            return process
        stun = launch([str(ROOT / 'hil/.venv/bin/python'), str(ROOT / 'hil/netprobe.py'),
                       '--stun-bind', a.ethernet_host, '--duration', str(a.duration + 240),
                       '--out', str(directory / 'stun.jsonl')], 'stun-command.log')
        time.sleep(.3)
        if stun.poll() is not None:
            raise RuntimeError('STUN responder exited')
        env = {**os.environ, 'BOT_MODE': 'offline-test', 'SERVER_PORT': '7861',
               'BOT_ICE_INTERFACE': voice_interface, 'BOT_ICE_REMOTE_NETWORK_ONLY': '1' if a.voice_path=='ethernet' else '0',
               'BOT_ICE_DEBUG_PATH': str(directory / 'ice-debug.log'),
               'RTP_EVIDENCE_DIR': str(directory / 'rtp'), 'RTP_EVIDENCE_MAX_PACKETS': '200000',
               'RTP_EVIDENCE_PAYLOAD_BYTES': '1' if not a.cpu_only or getattr(a,'payload_sizes',False) else '0',
               'EVENTS_PATH': str(directory / 'events.jsonl'), 'OFFLINE_SKIP_GREETING': '1',
               'OFFLINE_BENCH_CONTINUOUS_AUDIO': '1', 'OFFLINE_IDLE_TIMEOUT_SECS': '2100'}
        bot = launch([str(ROOT / 'server/.venv/bin/python'), 'bot.py', '-t', 'webrtc',
                      '--host', '0.0.0.0', '--port', '7861', '--ice-servers',
                      'stun:' + a.ethernet_host + ':3478'], 'bot.log', cwd=ROOT / 'server', env=env)
        for _ in range(90):
            if bot.poll() is not None:
                raise RuntimeError('Owned bot exited')
            try:
                urllib.request.urlopen('http://127.0.0.1:7861/', timeout=1).close()
                break
            except OSError:
                time.sleep(.5)
        else:
            raise RuntimeError('Bot readiness timeout')
        with EspSerial(a.port, log_path=out / 'serial.log') as esp:
            cursor = esp.cursor
            reset_began=time.monotonic()
            esp.reset(timeout=15)
            esp.wait_for('READY transport-console', 60, since=cursor)
            ready_at=time.monotonic()
            dump_json(out/'boot-timing.json',dict(reset_to_console_ready_ms=1000*(ready_at-reset_began),
                basis='Host monotonic interval from reset command through console READY; includes reset USB/serial latency'))
            identity = esp.wait_for('elf_sha256=', 3, since=cursor)
            expected = hashlib.sha256(a.elf.read_bytes()).hexdigest()
            if re.search(r'elf_sha256=([a-f0-9]{64})', identity)[1] != expected:
                raise RuntimeError('Installed ELF identity differs from requested image')
            dump_json(out / 'identity.json', dict(elf_sha256=expected, elf=str(a.elf)))
            if platform:
                boot='\n'.join(row[2] for row in esp.lines_since(cursor))
                modes=re.findall(r'spi_flash: flash io: (\w+)',boot)
                speeds=re.findall(r'Boot SPI Speed\s*:\s*(\d+)MHz',boot)
                expected_mode='qio' if platform['CONFIG_ESPTOOLPY_FLASHMODE_QIO']=='y' else 'dio'
                expected_mhz=int(platform['CONFIG_ESPTOOLPY_FLASHFREQ'].strip('"m'))
                verification=dict(platform=platform,operating_flash_io=modes[-1] if modes else None,
                                  boot_flash_mhz=int(speeds[-1]) if speeds else None,
                                  expected_flash_io=expected_mode,expected_flash_mhz=expected_mhz)
                dump_json(out/'boot-verification.json',verification)
                if not modes or modes[-1]!=expected_mode or not speeds or int(speeds[-1])!=expected_mhz:
                    raise RuntimeError('Operating flash mode/frequency differs from requested image')
            try:
                esp.command('disconnect', timeout=45)
                esp.command('netprobe stop')
                esp.command('audio 0')
                esp.command('quiet 1')
                if getattr(a,'cache_counters',False):esp.command('cache-counters 1')
                esp.command('server-url http://' + voice_host + ':7861')
                for option in a.option:
                    name, value = option.split('=')
                    esp.command(f'peer-option {name} {int(value)}')
                esp.command('peer-option arrival-trace ' + ('0' if a.cpu_only else '1'))
                if a.fixed_mic:
                    esp.command('mic-fixture 1')
                esp.command('gate ' + a.gate)
                esp.command('audio 1')
                esp.command('connect', timeout=95)
                esp.command('play-long ' + str(min(30, int(a.duration // 60) + 2)), timeout=20)
                time.sleep(5)
                def wifi(name):
                    cursor = esp.cursor
                    esp.command('wifi-stats', timeout=10)
                    (directory / name).write_text('\n'.join(row[2] for row in esp.lines_since(cursor)) + '\n')
                def trace(stream):
                    cursor = esp.cursor
                    esp.command('dump-arrivals', timeout=15)
                    stream.write('\n'.join(row[2] for row in esp.lines_since(cursor)) + '\n')
                    stream.flush()
                dump_json(directory / 'host-before.json', netprobe.wifi_snapshot('wlp8s0'))
                before = probe_stats(esp)
                dump_json(directory / 'device-before.json', before)
                wifi('wifi-tx-before.log')
                snapshots.append(cpu_perf(esp))
                with (directory / 'trace-serial.log').open('w') as stream:
                    if not a.cpu_only:trace(stream)
                    def poll():
                        next_trace = time.monotonic() + 10
                        next_cpu = time.monotonic() + a.cpu_window
                        next_notice = time.monotonic() + 60
                        began = time.monotonic()
                        try:
                            while not stop.wait(1):
                                if not a.cpu_only:samples.append(dict(host_ns=time.perf_counter_ns(), stats=probe_stats(esp)))
                                if not a.cpu_only and time.monotonic() >= next_trace:
                                    trace(stream)
                                    next_trace = time.monotonic() + 10
                                if time.monotonic() >= next_cpu:
                                    snapshots.append(cpu_perf(esp))
                                    dump_json(directory / 'cpu-snapshots.json', snapshots)
                                    next_cpu = time.monotonic() + a.cpu_window
                                if time.monotonic() >= next_notice:
                                    print(f'{a.label}: {time.monotonic()-began:.0f}/{a.duration:.0f}s', flush=True)
                                    next_notice += 60
                        except BaseException as exc:
                            errors.append(str(exc))
                    def ready():
                        nonlocal worker
                        if not a.cpu_only:esp.command(f'netprobe start {a.wifi_host} 47100 50 130 2 {a.ethernet_host} 47101', timeout=10)
                        worker = threading.Thread(target=poll)
                        worker.start()
                    def finish():
                        stop.set()
                        worker.join(30)
                        if worker.is_alive():
                            raise RuntimeError('Serial poller failed to join')
                        esp.command('netprobe stop')
                    if a.cpu_only:
                        ready()
                        deadline=time.monotonic()+a.duration
                        while time.monotonic()<deadline:time.sleep(min(1,deadline-time.monotonic()))
                        finish()
                    else:
                        netprobe.run_streams([f'esp-0,{a.wifi_host},47100', f'esp-1,{a.ethernet_host},47101'],
                                             a.duration, 50, 130, directory / 'host.jsonl', ready=ready, finish=finish)
                    snapshots.append(cpu_perf(esp))
                    after = probe_stats(esp)
                    dump_json(directory / 'device-after.json', after)
                    dump_json(directory / 'device-samples.json', samples)
                    dump_json(directory / 'cpu-snapshots.json', snapshots)
                    wifi('wifi-tx-after.log')
                    if not a.cpu_only:
                        cursor = esp.cursor
                        esp.command('netprobe dump', timeout=90)
                        lines = '\n'.join(row[2] for row in esp.lines_since(cursor))
                        (directory / 'probe-dump.log').write_text(lines + '\n')
                        device_rows = netprobe.decode_dump(lines)
                        trace(stream)
                if errors:
                    raise RuntimeError('; '.join(errors))
                if not a.cpu_only:
                    analysis = netprobe.analyze_streams(directory / 'host.jsonl', device_rows=device_rows, device_stats=after)
                    dump_json(directory / 'analysis.json', analysis)
                    if after.get('udp_mailbox_untracked',0) or after.get('udp_mailbox_untracked_ports',0):
                        raise RuntimeError('Incomplete UDP mailbox tracking')
                    if not all(record['downlink']['capture_complete'] and record['uplink']['local_send_failure_capture_complete'] for record in analysis.values()):
                        raise RuntimeError('Incomplete probe receive/send-failure capture')
                esp.command('rtp-stats', timeout=15)
                esp.command('disconnect', timeout=45)
                esp.command('audio 0')
                dump_json(directory / 'host-after.json', netprobe.wifi_snapshot('wlp8s0'))
            finally:
                stop.set()
                if worker:
                    worker.join(30)
                for command in ('netprobe stop', 'audio 0', 'disconnect'):
                    try:
                        esp.command(command, timeout=45)
                    except Exception:
                        pass
        if re.search(r'Guru Meditation|assert failed|abort\(\)|Task watchdog|task_wdt: .*triggered',
                     (out/'serial.log').read_text()):
            raise RuntimeError('Device fault during capture; retain raw evidence')
        uplink_file=directory/'rtp/rtp.jsonl'
        uplink_records=[json.loads(line) for line in uplink_file.read_text().splitlines()] if uplink_file.exists() else []
        timestamp_check=uplink_timestamp_check(uplink_records,snapshots[-1]['uplink_frame_ms'])
        dump_json(out/'uplink-timestamp-check.json',timestamp_check)
        if timestamp_check['verified'] is False:
            raise RuntimeError('Uplink RTP timestamps compressed the configured PCM timeline')
        if a.cpu_only:
            from step8c_analysis import summarize_cpu
            cpu_summary=summarize_cpu(out)
            dump_json(out/'cpu-summary.json',cpu_summary)
            if not cpu_summary['stimulus_complete']:raise RuntimeError('Incomplete continuous duplex CPU stimulus')
            pairs=[json.loads(line) for line in (directory/'rtp/ice-pairs.jsonl').read_text().splitlines()]
            if not pairs or any(record['interface']!=voice_interface or not record['pairs'] or any(pair['local'][0]!=voice_host for pair in record['pairs']) for record in pairs):
                raise RuntimeError('CPU voice used an unexpected nominated ICE path')
            dump_json(out/'result.json',dict(passed=True,metadata=metadata))
            print(a.label+': CPU capture complete',flush=True)
            return
        attribution = attribute_rtp(directory, analysis)
        dump_json(directory / 'rtp-attribution.json', attribution)
        tx = wifi_counters(directory)
        dump_json(directory / 'wifi-tx-delta.json', tx)
        if not attribution['trace_integrity_verified'] or not attribution['native_pts_calibration']['exact']:
            raise RuntimeError('Incomplete RTP trace or PTS calibration')
        if not attribution['selected_pairs']:
            raise RuntimeError('No nominated ICE pair evidence')
        for record in attribution['selected_pairs']:
            if record['interface'] != voice_interface or not record['pairs'] or any(pair['local'][0] != voice_host for pair in record['pairs']):
                raise RuntimeError('Voice used an unexpected nominated ICE path')
        dump_json(out / 'result.json', dict(passed=True, metadata=metadata))
        print(a.label + ': capture complete', flush=True)
    except BaseException:
        (out / 'FAILED.txt').write_text(traceback.format_exc())
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for stream in logs:
            stream.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--elf', type=Path, required=True)
    p.add_argument('--wifi-host', required=True)
    p.add_argument('--ethernet-host', required=True)
    p.add_argument('--voice-path',choices=['wifi','ethernet'],default='ethernet')
    p.add_argument('--duration', type=float, default=610)
    p.add_argument('--cpu-window', type=float, default=30)
    p.add_argument('--option', action='append', default=[])
    p.add_argument('--fixed-mic', action='store_true')
    p.add_argument('--cpu-only', action='store_true', help='Disable probes/arrival recording for independent CPU measurement')
    p.add_argument('--payload-sizes',action='store_true',help='Retain codec byte counts in CPU captures')
    p.add_argument('--cache-counters',action='store_true',help='Enable shared hardware cache counters in the bench image')
    p.add_argument('--gate', choices=['auto', 'open', 'closed'], default='auto')
    p.add_argument('--label', default='baseline')
    p.add_argument('--port', default='/dev/ttyACM0')
    args = p.parse_args()
    if args.duration <= 0 or args.cpu_window <= 0:
        p.error('Durations must be positive')
    capture(args)
