#!/usr/bin/env python3
"""Coordinator-only UART campaign; imports and verifier unit tests open no ports.

CRC32 is IEEE/zlib over the exact payload bytes (base64 text for DUMP).
F/R line_bytes includes the newline. A summary's attempted count exposes tail loss.
"""
import argparse
import base64
import json
import math
import re
import time
import zlib
from pathlib import Path

PORT = '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90169930-if00'
BAUDS = (2_000_000, 1_500_000, 921_600, 115_200)


def decode_dump_line(line):
    _, seq, checksum, payload = line.split(b' ', 3)
    if not re.fullmatch(rb'[0-9a-fA-F]{8}', checksum) or zlib.crc32(payload) != int(checksum, 16):
        raise ValueError('dump CRC mismatch')
    raw = base64.b64decode(payload, validate=True)
    if not raw or len(raw) % 8:
        raise ValueError('partial four-channel PCM frame')
    return int(seq), raw


class Verifier:
    def __init__(self, prefix=b'F'):
        self.prefix = prefix
        self.seen, self.valid = set(), set()
        self.corrupt = self.duplicates = self.reordered = self.bytes = self.pcm_bytes = 0
        self.highest = -1

    def feed(self, line):
        if not line.startswith(self.prefix + b' '):
            return
        self.bytes += len(line) + 1
        try:
            _, seq, checksum, payload = line.split(b' ', 3)
            if not re.fullmatch(rb'[0-9]+', seq): raise ValueError('invalid sequence')
            seq = int(seq)
            if seq < 0:
                raise ValueError('negative sequence')
            self.duplicates += seq in self.seen
            self.reordered += seq < self.highest and seq not in self.seen
            self.highest = max(self.highest, seq)
            self.seen.add(seq)
            if not re.fullmatch(rb'[0-9a-fA-F]{8}', checksum) or zlib.crc32(payload) != int(checksum, 16):
                raise ValueError('CRC mismatch')
            if self.prefix == b'DUMP':
                _, raw = decode_dump_line(line)
                if seq not in self.valid:
                    self.pcm_bytes += len(raw)
            self.valid.add(seq)
        except (ValueError, TypeError):
            self.corrupt += 1

    def result(self, expected):
        expected_set = set(range(expected))
        return dict(expected_lines=expected, received_lines=len(self.seen & expected_set),
                    valid_lines=len(self.valid & expected_set), lost_lines=len(expected_set - self.seen),
                    corrupt_lines=self.corrupt, missing_or_corrupt=len(expected_set - self.valid),
                    unexpected_sequences=len(self.seen - expected_set), duplicates=self.duplicates,
                    reordered=self.reordered, received_bytes=self.bytes, pcm_bytes=self.pcm_bytes)


class Board:
    def __init__(self, port=PORT, baud=115200, log=None):
        if port != PORT:
            raise ValueError('Only the pinned P4 by-id port is allowed')
        import serial
        self.s = serial.Serial()
        self.s.port = port; self.s.baudrate = baud; self.s.timeout = .1
        self.s.dtr = False; self.s.rts = False
        self.s.open()
        self.buf = b''; self.log = log; self.loop_ends = []; self.boots_seen = 0

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.s.close()

    def reset(self):
        self.s.baudrate = 115200
        self.s.reset_input_buffer(); self.buf = b''
        self.s.dtr = False; self.s.rts = True
        time.sleep(.15)
        self.s.rts = False

    def send(self, text):
        self.s.write(text.encode() + b'\n'); self.s.flush()

    def lines(self, seconds, slow=False):
        end = time.monotonic() + seconds
        pause_at = time.monotonic() + 1
        while time.monotonic() < end:
            if slow and time.monotonic() >= pause_at:
                time.sleep(2); pause_at = time.monotonic() + 1
            self.buf += self.s.read(max(1, min(8192, self.s.in_waiting)))
            while b'\n' in self.buf:
                line, self.buf = self.buf.split(b'\n', 1)
                line = line.rstrip(b'\r')
                if line == b'P4_PROBE boot': self.boots_seen += 1
                if line.startswith(b'LOOP END '): self.loop_ends.append(fields(line))
                if self.log:
                    self.log.write(line + b'\n')
                yield line

    def until(self, predicate, seconds):
        lines = []
        for line in self.lines(seconds):
            lines.append(line)
            if predicate(line): return lines
        raise TimeoutError('Expected console response missing')

    def command(self, text, seconds=15):
        self.send(text)
        lines = self.until(lambda line: line == b'OK', seconds)
        if any(line.startswith(b'ERR') for line in lines):
            raise RuntimeError('Device rejected command: ' + text)
        return lines

    def uptime(self, seconds=3):
        rows = self.command('uptime', seconds)
        row = next((line for line in rows if line.startswith(b'UPTIME us=')), None)
        if row is None: raise ValueError('Malformed uptime response')
        return int(row.split(b'=')[1])

    def resync_uptime(self, seconds=15):
        # A wrong-baud command may leave a partial line after an open reset.
        # End that line and ignore its stale ERR/OK while observing fresh uptime.
        self.send(''); self.send('uptime')
        rows = self.until(lambda line: line.startswith(b'UPTIME us='), seconds)
        value = int(rows[-1].split(b'=')[1])
        self.until(lambda line: line == b'OK', 3)
        return value

    def switch_baud(self, rate):
        self.send(f'baud {rate}')
        self.until(lambda line: line.startswith(f'BAUD rate={rate} '.encode()), 3)
        self.s.baudrate = rate
        self.until(lambda line: line == b'OK', 3)


def fields(line):
    return {key.decode(): int(value) for key, value in re.findall(rb'([a-z_]+)=([0-9]+)', line)}


class ConsoleReset(RuntimeError): pass


def collect(board, prefix, seconds, slow=False, verifier=None):
    verifier = verifier or Verifier(b'R' if prefix == b'RING' else b'F')
    for line in board.lines(seconds, slow=slow):
        if line == b'P4_PROBE boot' or line.startswith(b'READY '): raise ConsoleReset('Board rebooted during stream')
        verifier.feed(line)
        if line.startswith(prefix + b' END '):
            summary = fields(line)
            result = verifier.result(summary['lines'])
            result.update(summary=summary, bytes_per_second=result['received_bytes'] * 1e6 / max(1, summary['elapsed_us']))
            board.until(lambda line: line == b'OK', 3)
            return result
    raise TimeoutError('Missing stream summary: ' + prefix.decode())


def dump_check(board, baud):
    board.command('rec 5000 1000', 10)
    board.send('dump')
    verifier = Verifier(b'DUMP'); expected = None
    for line in board.lines(640000 * 10 / baud * 2 + 30):
        if line.startswith(b'DUMP BEGIN '): expected = fields(line)['frames']
        elif line.startswith(b'DUMP END '):
            if expected is None or fields(line)['frames'] != expected:
                raise ValueError('Dump frame metadata mismatch')
            result = verifier.result(math.ceil(expected / 160))
            result['expected_pcm_bytes'] = expected * 8
            result['pcm_size_ok'] = verifier.pcm_bytes == expected * 8
            board.until(lambda row: row == b'OK', 3)
            return result
        else: verifier.feed(line)
    raise TimeoutError('Dump trailer missing')


def boot_check(board, result=None):
    result = {} if result is None else result
    board.reset()
    boot = board.until(lambda line: line.startswith(b'READY '), 15)
    ready = fields(boot[-1])
    seen = set()
    for line in boot:
        match = re.fullmatch(rb'BOOT ([0-7]) uptime_us=[0-9]+', line)
        if match: seen.add(int(match[1]))
    if ready['boot_lines'] != 8: raise ValueError('Unexpected boot record declaration')
    result['cold_reset'] = dict(ready=True, boot_lines_expected=ready['boot_lines'],
                               boot_lines_lost=len(set(range(ready['boot_lines'])) - seen),
                               scope='numbered driver BOOT records; ROM output has no loss oracle')
    # One command, no warm-up retry: directly tests the original first-line defect.
    result['first_command_uptime_us'] = board.uptime()
    return result


def campaign(board, args, result):
    boot_check(board, result)
    result['baud_runs'] = []
    for rate in BAUDS:
        row = dict(baud=rate, passed=False)
        result['baud_runs'].append(row)
        try:
            board.switch_baud(rate)
            loop_count = len(board.loop_ends)
            board.command(f'loop {args.seconds}')
            board.send(f'flood {args.seconds} {args.line_bytes}')
            flood = collect(board, b'FLOOD', args.seconds + 15)
            row['flood'] = flood
            time.sleep(1) # Let the final audio read finish before taking capture ownership.
            dump = dump_check(board, rate)
            loop = board.loop_ends[-1] if len(board.loop_ends) > loop_count else None
            row.update(dump=dump, loop=loop)
            row['passed'] = not (flood['missing_or_corrupt'] or flood['corrupt_lines'] or flood['duplicates'] or
                flood['summary'].get('tx_wait_rc', 0) or dump['missing_or_corrupt'] or dump['corrupt_lines'] or
                flood['unexpected_sequences'] or flood['reordered']) and dump['pcm_size_ok'] and loop is not None and loop['errors'] == 0
        except (TimeoutError, ValueError, RuntimeError) as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            # A failed candidate baud must not prevent measuring slower rates.
            # Reset the probe at 115200; failure to recover aborts the guarded run.
            row['recovery_boot'] = boot_check(board)
    good = [row for row in result['baud_runs'] if row['passed']]
    chosen = max(good, key=lambda row: row['baud'], default=None)
    result['highest_passing_baud'] = chosen['baud'] if chosen else None
    result['measured_bytes_per_second'] = chosen['flood']['bytes_per_second'] if chosen else None
    if chosen is None: raise RuntimeError('No baud passed the CRC/sequence campaign')
    board.switch_baud(chosen['baud'])
    post_sweep(board, args, result, chosen['baud'])
    return result


def observe_reopen(board, baud, row, before, boots):
    observed_baud = baud
    try:
        after = board.uptime()
    except (TimeoutError, ValueError, RuntimeError) as exc:
        row['response_error'] = f'{type(exc).__name__}: {exc}'
        # A reset starts the app at 115200. Observe it without forcing a reset,
        # then restore the measured rate for the following experiment.
        board.s.baudrate = 115200; board.buf = b''
        after = board.resync_uptime(15); observed_baud = 115200
    row.update(baud=baud, observed_baud=observed_baud, before_us=before, after_us=after,
               passed=after > before and board.boots_seen == boots and observed_baud == baud and
                      'response_error' not in row and 'stream_error' not in row)
    if observed_baud != baud: board.switch_baud(baud)


def post_sweep(board, args, result, baud):
    before, boots = board.uptime(), board.boots_seen
    row = result['attach_without_reset'] = dict(baud=baud)
    board.s.close(); time.sleep(.3); board.s.open(); board.buf = b''
    observe_reopen(board, baud, row, before, boots)

    before, boots = board.uptime(), board.boots_seen
    row = result['reopen_mid_flood'] = dict(baud=baud)
    board.command('loop 6'); board.send(f'flood 6 {args.line_bytes}')
    verifier = Verifier()
    for line in board.lines(1): verifier.feed(line)
    board.s.close(); time.sleep(.5); board.s.open(); board.buf = b''
    try:
        row.update(collect(board, b'FLOOD', 20, verifier=verifier))
    except (TimeoutError, ValueError, ConsoleReset) as exc:
        # An open-triggered reset destroys the flood trailer; that is a measured
        # reopen failure, not a reason to skip the ring experiment.
        row.update(stream_error=f'{type(exc).__name__}: {exc}', summary_received=False,
                   observed_sequences=len(verifier.seen), corrupt_lines=verifier.corrupt)
    observe_reopen(board, baud, row, before, boots)

    for name, slow_reader in (('ring_baseline', False), ('slow_reader', True)):
        time.sleep(1)
        board.command('loop 6'); board.send(f'ring 6 {args.line_bytes}')
        measurement = result[name] = dict(baud=baud)
        verifier = Verifier(b'R')
        try:
            measurement.update(collect(board, b'RING', 30, slow=slow_reader, verifier=verifier))
            measurement['summary_received'] = True
            measurement['unexplained_missing'] = max(0, measurement['missing_or_corrupt'] - measurement['summary']['ring_drops'])
        except (TimeoutError, ValueError, ConsoleReset) as exc:
            measurement.update(passed=False, stream_error=f'{type(exc).__name__}: {exc}',
                               summary_received=False, observed_sequences=len(verifier.seen),
                               corrupt_lines=verifier.corrupt, valid_lines=len(verifier.valid),
                               unexplained_missing=None, bytes_per_second=None)
            # The deliberate read pause may lose END/OK too. End any partial
            # command and observe uptime before resuming; do not invent a loss
            # denominator or abort a usable link just because its trailer vanished.
            try:
                measurement['recovery_uptime_us'] = board.resync_uptime()
            except (TimeoutError, ValueError, RuntimeError):
                board.s.baudrate = 115200; board.buf = b''
                measurement['recovery_uptime_us'] = board.resync_uptime()
                measurement['recovery_baud'] = 115200
                if baud != 115200: board.switch_baud(baud)
            else:
                measurement['recovery_baud'] = baud



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--boot-only', action='store_true')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--line-bytes', type=int, default=1800)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 64 <= args.line_bytes <= 2048: parser.error('invalid duration or line size')
    args.out.mkdir(parents=True, exist_ok=True)
    result = {}
    try:
        with (args.out / 'serial.log').open('wb') as log, Board(log=log) as board:
            if args.boot_only: boot_check(board, result)
            else: campaign(board, args, result)
    except BaseException as exc:
        result.update(error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
