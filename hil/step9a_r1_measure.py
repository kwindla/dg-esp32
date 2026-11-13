#!/usr/bin/env python3
"""Display allocation/push measurements; no flash filesystem or acoustic arms."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

from esp_serial import EspSerial
from step7_support import BotServer
from transport_test import perf
from step8e2b_analysis import lcd_memory_gate


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def camera(path):
    with path.with_suffix('.log').open('w') as log:
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '1280x720',
            '-framerate', '30', '-i', '/dev/video0', '-vf', 'select=gte(n\\,20)',
            '-frames:v', '1', '-update', '1', '-y', str(path)],
            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=20)


def measure(out, elf, placement, size, host):
    out.mkdir(parents=True, exist_ok=False)
    result = {'placement': placement, 'staging_bytes': size, 'complete': False,
              'filesystem': 'unmounted read-only stub; successful mount not measured'}
    try:
        with EspSerial('/dev/ttyACM0', log_path=out/'serial.log') as board:
            at = board.cursor
            board.reset()
            board.wait_for('READY transport-console', 60, since=at)
            identity = board.wait_for('elf_sha256=', 3, since=at)
            assert hashlib.sha256(elf.read_bytes()).hexdigest() in identity
            board.command('server-url http://' + host + ':7861')
            board.command('quiet 1')
            board.command(f'disp init {placement} {size}', 30)
            at = board.cursor
            board.command('disp fetch http://' + host + ':7874/census.bin')
            fetch = board.wait_for('fetch_complete', 45, since=at)
            assert 'ok=1 bytes=307200 ' in fetch
            result['fetch'] = fetch
            snapshots = []
            try:
                for _ in range(2):
                    board.command('connect', 90)
                    snapshots.append(perf(board))
                    time.sleep(10)
                    snapshots.append(perf(board))
                    board.command('disconnect', 45)
                    result['snapshots'] = snapshots
                    save(out/'result.json', result)
            finally:
                board.command('audio 0')
                board.command('disconnect', 45)
            result['memory'] = lcd_memory_gate(snapshots, reconnects=1)
            result['push'] = []
            for mhz in (20, 40, 80):
                at = board.cursor
                try:
                    board.command(f'disp push {mhz} 30', 60)
                    lines = [entry[2] for entry in board.lines_since(at)
                             if 'lcd-push: rect=' in entry[2]]
                    assert len(lines) == 2
                    rows = []
                    for line in lines:
                        fields = dict(re.findall(r'(\w+)=([^ ]+)', line))
                        rows.append({key: int(value) if value.isdigit() and key != 'crc'
                                     else value for key, value in fields.items()})
                    capture = out/f'pattern-{mhz}MHz.png'
                    camera(capture)
                    result['push'].append({'mhz': mhz, 'metrics': rows,
                                           'camera': str(capture)})
                except Exception as exc:
                    result['push'].append({'mhz': mhz, 'error': f'{type(exc).__name__}: {exc}'})
                    save(out/'result.json', result)
            result['complete'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        save(out/'result.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--elf', type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect(('192.168.4.29', 1234))
    host = sock.getsockname()[0]
    sock.close()
    os.environ['OFFLINE_SKIP_GREETING'] = '1'
    os.environ['OFFLINE_IDLE_TIMEOUT_SECS'] = '1200'
    save(a.out/'plan.json', {'buffers': [['internal', 20480], ['internal', 16384],
         ['external', 20480]], 'elf_sha256': hashlib.sha256(a.elf.read_bytes()).hexdigest(),
         'memory': 'fresh boot, fetch, two Ready/+10s snapshot pairs, reconnect',
         'throughput': 'after disconnect; 30 full and patch frames at 20/40/80 MHz',
         'storage': 'never mount/provision/format; unmounted registration only'})
    with BotServer(a.out/'bot', port=7861):
        for placement, size in [('internal', 20480), ('internal', 16384), ('external', 20480)]:
            print('START', placement, size, flush=True)
            measure(a.out/f'{placement}-{size}', a.elf, placement, size, host)
            print('DONE', placement, size, flush=True)


if __name__ == '__main__':
    main()
