#!/usr/bin/env python3
"""Compare current ordered hardware calls to reviewed golden text fixtures."""
from pathlib import Path
import difflib
import subprocess
import sys


FIXTURES = Path(__file__).resolve().parent/'fixtures/trace'
# Rebase policy: a deliberate audio change regenerates the affected fixture
# in the same commit as that change, with an explicitly reviewed fixture diff.
# Never refresh fixtures merely to make a failing regression test pass.


def compare(after, board, args=(), rejected=False):
    case = args[0].removeprefix('--') if args else 'startup'
    expected_trace = (FIXTURES/f'{board}-{case}.txt').read_text()
    right = subprocess.run([after, *args], capture_output=True, text=True)
    expected = 1 if rejected else 0
    if right.returncode != expected:
        raise AssertionError(f'Exit mismatch: {right.returncode}, expected {expected}\n'
                             f'{right.stdout}\n{right.stderr}')
    if expected_trace != right.stdout:
        raise AssertionError(''.join(difflib.unified_diff(expected_trace.splitlines(True),
                             right.stdout.splitlines(True), fromfile='golden', tofile='candidate')))
    trace = right.stdout.splitlines()
    writes = [line for line in trace if line.startswith('write ')]
    if board == 'waveshare146':
        assert not writes
        assert 'new_channel 0 role=1 dma=3/256 clear=0 tx=0 rx=1' in trace
        assert 'new_channel 1 role=1 dma=4/240 clear=1 tx=1 rx=0' in trace
    else:
        assert len(writes) > 80, 'Must exercise real codec register programming'
        assert any('startup verified REG0D=0x01 REG44=0x58 MIC1_GAIN=0x1a' in s for s in trace) != rejected
    if board == 'echoear_aec' and not rejected:
        assert any('MIC3_GAIN=0x15 analog_db=15' in s for s in trace)
        assert 'afe mic=291 ref=-292' in trace
        assert 'afe mic=-32768 ref=32767' in trace
    if board == 'p4xc':
        assert not any('echoear' in line for line in trace)
        assert 'std RX rate=16000 mclk=256 data=32 mode=2 slot=32 ws=32 pins=13,12,10,9,11' in trace
        assert 'std TX rate=16000 mclk=512 data=16 mode=2 slot=32 ws=32 pins=-1,12,10,9,11' in trace
        assert 'info RX role=1 mclk=4096000 bclk=1024000' in trace
        assert 'info TX role=2 mclk=8192000 bclk=1024000' in trace
        assert 'callbacks RX sent=0 overflow=0 recv=1 overflow=1' in trace
        assert all(line.startswith(('gpio 53 ', 'direction 53 ')) for line in trace
                   if line.startswith(('gpio ', 'direction '))), 'No rail or hosted reset writes'
        if not rejected:
            assert 'write 18 32 ba timeout=20' in trace, 'Spike PA gain profile'
            assert 'sample 356 raw=291' in trace, 'MIC1 must use word0 high in AEC-off too'
            assert 'gpio 53 1' in trace and 'gpio 53 0' in trace
    print(f'{board} {list(args)}: identical ({len(trace)} calls/logs, {len(writes)} register writes)')


if __name__ == '__main__':
    after, board = sys.argv[1:]
    compare(after, board)
    if board != 'waveshare146':
        compare(after, board, ['--nack'])
        for register in ('0d', '44', '43'):
            compare(after, board, [register], rejected=True)
