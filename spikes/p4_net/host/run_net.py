#!/usr/bin/env python3
"""Coordinator-only network campaign using the unchanged hil/netprobe.py wire v1 tool."""
import argparse
import json
import re
import subprocess
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'spikes/p4_probe/host'))
from console_rate import Board, fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='192.168.7.222', help='LAN IPv4 address of this host, reachable by P4')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-unknown-slave-version', action='store_true',
                        help='continue after a failed firmware-version RPC when boot confirms transport and WiFi join')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    children = []; result = {}
    try:
        with (args.out/'serial.log').open('wb') as log, Board(log=log) as board:
            board.reset()
            boot = board.until(lambda row: row.startswith(b'P4_NET READY'), 60)
            result['boot'] = [row.decode('utf-8','replace') for row in boot]
            result['connection_evidence'] = {
                'transport': [row for row in result['boot'] if 'TRANSPORT_TX_ACTIVE' in row or 'H_API: Transport active' in row],
                'join': [row for row in result['boot'] if row.startswith('WIFI IP=')],
                'ap': [row for row in result['boot'] if row.startswith('AP ')],
                'api': [row for row in result['boot'] if row.startswith('API ')],
            }
            version = next((match for row in boot if (match := re.match(
                rb'HOSTED FW rpc_rc=(-?[0-9]+) version=([0-9]+\.[0-9]+\.[0-9]+)(?: |$)', row))), None)
            result['slave_version_override'] = False
            if version is None:
                result['slave_version'] = 'unavailable (missing or malformed firmware-version record)'
                raise RuntimeError('Missing or malformed slave firmware-version record')
            rpc_rc = int(version[1])
            result['slave_version'] = version[2].decode('ascii') if rpc_rc == 0 else f'unavailable (rpc_rc={rpc_rc})'
            if rpc_rc != 0:
                if not args.allow_unknown_slave_version:
                    raise RuntimeError('Slave firmware RPC unavailable; coordinator must inspect compatibility, no C6 write; '
                                       'use --allow-unknown-slave-version to continue with transport/join evidence')
                if not result['connection_evidence']['transport'] or not result['connection_evidence']['join']:
                    raise RuntimeError('Unknown slave version requires active transport and WiFi join evidence')
                result['slave_version_override'] = True
            raw = args.out/'udp.jsonl'
            sys.path.insert(0, str(ROOT/'hil'))
            from netprobe import run_streams, analyze_streams, decode_dump
            stopped = []
            def ready(): board.command(f'udp {args.host} 47010 120 50 130')
            def finish(): stopped.extend(board.command('stop', 10))
            # Unchanged tool callbacks keep RX sockets alive while stopping the
            # transmitter, so neither leading nor terminal loss is hidden.
            run_streams([f'esp-0,{args.host},47010'], 60, 50, 130, raw, interface='wlp8s0', ready=ready, finish=finish)
            udp_device = fields(next(row for row in stopped if row.startswith(b'UDP END')))
            stats_rows = board.command('netstats')
            stats = json.loads(next(row.split(b' ',1)[1] for row in stats_rows if row.startswith(b'NETPROBE ')))
            dump = board.command('netdump', 45)
            for row in dump:
                if row.startswith(b'NETPROBE_ROW '):
                    _, payload, crc = row.split()
                    if zlib.crc32(payload) != int(crc.split(b'=')[1],16): raise ValueError('Network record dump CRC mismatch')
            device_rows = decode_dump(b'\n'.join(dump).decode('ascii'))
            trailer = next(row for row in dump if row.startswith(b'NETPROBE_DUMP END'))
            if fields(trailer)['records'] != len(device_rows): raise ValueError('Network record dump loss')
            summary = analyze_streams(raw, device_rows=device_rows, device_stats=stats)
            result.update(udp_device=udp_device, udp_host=summary)
            if not udp_device['received'] or summary['esp-0']['rtt_ms']['p50'] is None:
                raise RuntimeError('No UDP/echo exchange')
            with (args.out/'tcp-host.log').open('w') as tcp_log:
                tcp = subprocess.Popen([sys.executable, str(Path(__file__).with_name('tcp_sink.py')),
                    '--bind', args.host, '--out', str(args.out/'tcp.json')], stdout=tcp_log, stderr=subprocess.STDOUT)
                children.append(tcp); time.sleep(.5)
                board.command(f'tcp {args.host} 47011 60')
                tcp_rows = board.until(lambda row: row.startswith(b'TCP END'), 75)
                tcp.wait(15)
                if tcp.returncode: raise RuntimeError('TCP sink failed')
            result.update(tcp_device=fields(tcp_rows[-1]))
            if result['tcp_device']['errno'] or not result['tcp_device']['bytes']:
                raise RuntimeError('TCP device transfer failed')
    except BaseException as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (args.out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        for child in children:
            if child.poll() is None:
                child.terminate()
                try: child.wait(5)
                except subprocess.TimeoutExpired: child.kill(); child.wait()


if __name__ == '__main__': main()
