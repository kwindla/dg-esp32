#!/usr/bin/env python3
"""Coordinator-only TCP sink, port 47011; writes throughput JSON."""
import argparse
import json
import socket
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='192.168.7.222')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.bind, 47011)); listener.listen(1); listener.settimeout(90)
        print('TCP_SINK READY port=47011', flush=True)
        conn, peer = listener.accept()
        total = 0; start = time.monotonic()
        with conn:
            conn.settimeout(10)
            while True:
                data = conn.recv(65536)
                if not data: break
                total += len(data)
        elapsed = time.monotonic() - start
    result = dict(peer=peer, bytes=total, seconds=elapsed, bits_per_second=total*8/max(elapsed, .000001))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result))


if __name__ == '__main__': main()
