#!/usr/bin/env python3
"""Measure received Opus sizes during known server idle and fixture output."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
from esp_serial import EspSerial, ROOT
from transport_test import perf, wait_until


def delta(before, after):
    a = before['received_payload_size_histogram']
    b = after['received_payload_size_histogram']
    bins = {k: b.get(k, 0)-a.get(k, 0) for k in a.keys() | b.keys()}
    assert all(v >= 0 for v in bins.values())
    bins = {k: v for k, v in bins.items() if v}
    return dict(histogram=bins, packets=sum(bins.values()),
                payload_bytes=sum(int(k)*v for k, v in bins.items()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    result = dict(passed=False); process = None
    try:
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', 7861)) != 0, '7861 already occupied'
        env = {k:v for k,v in os.environ.items() if not k.endswith('_API_KEY')}
        env.update(BOT_MODE='offline-test', SERVER_PORT='7861', EVENTS_PATH=str(out/'events.jsonl'),
                   PIPECAT_SCTP_MAX_CHUNK_SIZE='1100')
        with (out/'server.log').open('w') as log:
            process = subprocess.Popen([str(ROOT/'server/.venv/bin/python'), 'bot.py', '--host', '0.0.0.0', '--port', '7861'],
                                       cwd=ROOT/'server', env=env, stdout=log, stderr=subprocess.STDOUT)
        def up():
            try: return urllib.request.urlopen('http://127.0.0.1:7861/', timeout=.5).status < 500
            except Exception: return False
        wait_until(up, 30)
        with EspSerial('/dev/ttyACM0', log_path=out/'serial.log') as board:
            time.sleep(.2); cursor=board.cursor; board.reset()
            board.wait_for('READY transport-console', 40, since=cursor)
            identity=board.wait_for('elf_sha256=', 3, since=cursor)
            result['elf_sha256']=re.search(r'elf_sha256=([0-9a-f]{64})', identity)[1]
            assert result['elf_sha256']==hashlib.sha256(args.elf.read_bytes()).hexdigest()
            cursor=board.cursor; board.command('connect', timeout=30)
            board.wait_for('on_bot_ready', 30, since=cursor)
            time.sleep(5)  # greeting completed before the known-idle window
            baseline=perf(board); time.sleep(3); idle=perf(board)
            result['idle_3s']=delta(baseline, idle)
            cursor=board.cursor; board.command('play', timeout=15)
            board.wait_for('PLAY ACK', 5, since=cursor)
            time.sleep(4); speech=perf(board)
            result['reply_fixture_and_idle_tail_4s']=delta(idle, speech)
            cursor=board.cursor; board.command('disconnect', timeout=15)
            closed=json.loads(board.wait_for(r'PERF \{', 5, since=cursor).split('PERF ', 1)[1])
            result['snapshots']=dict(baseline=baseline, idle=idle, speech=speech, closed=closed)
            total=delta({'received_payload_size_histogram':{}}, closed)
            assert total['packets']==closed['received'], 'Histogram packet total mismatch'
            assert total['payload_bytes']==closed['bytes_received'], 'Histogram byte total mismatch'
            assert result['idle_3s']['packets']>=100, 'Incomplete idle window'
            assert result['reply_fixture_and_idle_tail_4s']['packets']>=150, 'Incomplete fixture window'
            result['passed']=True
    except Exception as error:
        result['error']=f'{type(error).__name__}: {error}'
    finally:
        if process:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='snapshots'}, indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
