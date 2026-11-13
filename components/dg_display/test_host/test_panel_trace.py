#!/usr/bin/env python3
"""Compare real driver output with the pre-step-10 command/delay fixture.

Fixtures must be recorded from f5fc668 using compare_pre_refactor.py. Never
rebase them from the candidate under test. Every init-command failure is also
injected independently to prove init stops before backlight: returned errors
except the base EchoEar TE-enable ESP_ERROR_CHECK abort.
"""
from pathlib import Path
import signal
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def check(executable, board):
    expected = (HERE/'fixtures/panel-trace'/f'{board}.txt').read_text()
    result = subprocess.run([str(executable)], capture_output=True, text=True, check=True)
    if result.stdout != expected:
        import difflib
        raise AssertionError(''.join(difflib.unified_diff(expected.splitlines(True), result.stdout.splitlines(True), fromfile='before', tofile='after')))
    count = sum(line.startswith('CMD ') for line in expected.splitlines())
    for failure in range(1, count+1):
        result = subprocess.run([str(executable), str(failure)], capture_output=True, text=True)
        abort = board == 'st77916_qspi' and failure == count
        assert result.returncode == (-signal.SIGABRT if abort else 2), (failure, result.returncode)
        assert ' FAIL\n' in result.stdout, failure
        assert result.stdout.endswith(' FAIL\n' if abort else 'RESULT -1\n'), failure
        assert 'BACKLIGHT\n' not in result.stdout, failure
        assert sum(line.startswith('CMD ') for line in result.stdout.splitlines()) == failure, failure
    print(f'{board}: exact command/delay trace and {count} injected command failures pass')


if __name__ == '__main__':
    check(Path(sys.argv[1]), sys.argv[2])
