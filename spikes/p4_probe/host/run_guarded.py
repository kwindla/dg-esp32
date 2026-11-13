#!/usr/bin/env python3
"""Coordinator runner: on command failure/interrupt, restore verified factory P4.

Owns no lock; requires the outer hil/bench.py lock. Never wrap this in a build lock.
"""
import argparse
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from restore_factory import verify_backup
from hardware import checked_esptool_python, require_bench


def rollback_log_path(args):
    if args.rollback_log is not None: return args.rollback_log
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return args.backup.resolve().parent / 'rollbacks' / f'restore-{stamp}-{os.getpid()}.log'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--rollback-log', type=Path, help='default: <backup-parent>/rollbacks/restore-UTC-PID.log')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command: parser.error('command required')
    bench_fd = require_bench()
    verify_backup(args.backup)
    python = checked_esptool_python()
    log_path = rollback_log_path(args)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('ab') as rollback_log:
        rollback_log.write(f'Guarded run pid={os.getpid()} rollback log\n'.encode()); rollback_log.flush()
        def interrupted(signum, frame): raise InterruptedError(f'signal {signum}')
        for sig in (signal.SIGHUP, signal.SIGTERM): signal.signal(sig, interrupted)
        child = None
        try:
            child = subprocess.Popen(command, start_new_session=True, pass_fds=(bench_fd,))
            code = child.wait()
            if code: raise subprocess.CalledProcessError(code, command)
            rollback_log.write(b'Child completed; rollback not needed\n'); rollback_log.flush()
        except BaseException:
            # Shield cleanup as well as rollback from a second terminal signal.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): signal.signal(sig, signal.SIG_IGN)
            if child is not None and child.poll() is None:
                try: os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: child.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait()
            rollback_log.write(b'Child failed/interrupted; starting factory restore\n'); rollback_log.flush()
            subprocess.run([python, str(Path(__file__).with_name('restore_factory.py')),
                            '--backup', str(args.backup)], check=True, pass_fds=(bench_fd,),
                            stdout=rollback_log, stderr=subprocess.STDOUT)
            rollback_log.write(b'Factory rollback completed\n'); rollback_log.flush()
            raise


if __name__ == '__main__': main()
