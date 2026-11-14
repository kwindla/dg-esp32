#!/usr/bin/env python3
"""Run one board/bot campaign under the shared EchoEar bench flock."""
import fcntl
import os
import sys

if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: bench.py COMMAND [ARG ...]')
    os.makedirs(f'/tmp/claude-{os.getuid()}', exist_ok=True)
    with open(f'/tmp/claude-{os.getuid()}/echoear-bench.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(lock.fileno(), True)
        os.environ['DG_BENCH_LOCK_FD'] = str(lock.fileno())
        os.execvp(sys.argv[1], sys.argv[1:])
