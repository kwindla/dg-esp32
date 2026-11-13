"""Integrity and bounded-deadline checks for console evidence exports.

CHECK hashes exact record text joined with LF (no terminal LF). Unrelated log
lines are ignored. ``read_dump`` accepts a reader taking a timeout in seconds;
it must return one line or None within that timeout. No serial port is opened
here. UART callers supply the total wire-byte budget (base64/framing included)
and measured bytes/second R. USB-JTAG uses record count times firmware pacing:
8 ms for AEC PCM, 4 ms for other dumps. PCM counts are derived from BEGIN;
ARRIVALS uses the caller's count or the firmware maximum of 512 batches.
"""
import argparse
import base64
import hashlib
import re
import time


RECORDS = {'AEC_DUMP': 'AEC_PCM', 'MIC_DUMP': 'MIC_PCM',
           'DUMP': 'PCM', 'ARRIVALS': 'ARRIVAL'}


def deadline_seconds(wire_bytes=None, rate=None, *, console_kind='uart', line_count=None, pace_ms=8):
    if console_kind == 'usb_serial_jtag':
        if line_count is None or line_count < 0 or pace_ms <= 0:
            raise ValueError('Invalid USB line count/pace')
        return line_count * pace_ms / 1000 + 30
    if console_kind != 'uart' or wire_bytes is None or rate is None or wire_bytes < 0 or rate <= 0:
        raise ValueError('Invalid console/wire budget/rate')
    return wire_bytes / (0.8 * rate) + 30


class DumpVerifier:
    def __init__(self, prefix='AEC_DUMP'):
        self.prefix = prefix
        self.record = RECORDS[prefix]
        self.begun = self.checked = self.ended = False
        self.lines = self.samples = self.timing_end = self.timing_count = 0
        self.expected_samples = self.frames = None
        self.digest = hashlib.sha256()

    def feed(self, line):
        line = line.rstrip('\r\n')
        if line.startswith(self.prefix + ' BEGIN'):
            if self.begun:
                raise ValueError('Duplicate BEGIN')
            self.begun = True
            if self.prefix == 'AEC_DUMP':
                match = re.fullmatch(
                    r'AEC_DUMP BEGIN (schema=2 )?frames=(\d+) channels=(3|5) rate=16000 '
                    r'order=([^ ]+) adapter_delay=\d+ measured_delay=unknown truncated=0 ns=0 agc=0', line)
                if not match:
                    raise ValueError('Invalid AEC BEGIN')
                schema, frames, channels, order = match.groups()
                self.frames, channels = int(frames), int(channels)
                if bool(schema) != (channels == 5) or order != (
                        'mic1,mic2,mic3,mic4,processed' if channels == 5 else 'mic,ref,processed'):
                    raise ValueError('Invalid AEC channels')
                self.expected_samples = self.frames * channels
            elif self.prefix in ('MIC_DUMP', 'DUMP'):
                match = re.fullmatch(self.prefix + r' BEGIN samples=(\d+)' + (
                    r' rate=16000 raw=1 dsp_bypass=1' if self.prefix == 'MIC_DUMP' else ''), line)
                if not match:
                    raise ValueError('Invalid PCM BEGIN')
                self.expected_samples = int(match[1])
            elif line != 'ARRIVALS BEGIN version=1 record_bytes=32':
                raise ValueError('Invalid ARRIVALS BEGIN')
            return
        relevant = line.startswith((self.prefix + ' ', self.record, 'AEC_TIME' if self.prefix == 'AEC_DUMP' else '\0'))
        if not relevant:
            return
        if not self.begun or self.ended:
            raise ValueError('Record outside dump')
        if line.startswith(self.prefix + ' CHECK'):
            match = re.fullmatch(self.prefix + r' CHECK lines=(\d+) sha256=([0-9a-f]{64})', line)
            if self.checked or not match:
                raise ValueError('Missing/malformed/duplicate CHECK')
            if int(match[1]) != self.lines or match[2] != self.digest.hexdigest():
                raise ValueError('Dump count/SHA256 mismatch')
            self.checked = True
        elif line.startswith(self.prefix + ' END'):
            if not self.checked:
                raise ValueError('Missing CHECK')
            if self.prefix == 'AEC_DUMP':
                match = re.fullmatch(r'AEC_DUMP END frames=(\d+)(?: timing_blocks=(\d+))?', line)
                if not match or int(match[1]) != self.frames:
                    raise ValueError('Invalid AEC END')
                if match[2] is not None and int(match[2]) != self.timing_count:
                    raise ValueError('Missing/duplicate timing rows')
                if self.timing_count and self.timing_end != self.frames:
                    raise ValueError('Incomplete timing extent')
            elif self.prefix == 'ARRIVALS':
                if line != 'ARRIVALS END':
                    raise ValueError('Invalid ARRIVALS END')
            elif line != f'{self.prefix} END samples={self.expected_samples}':
                raise ValueError('Invalid PCM END')
            if self.expected_samples is not None and self.samples != self.expected_samples:
                raise ValueError('Incomplete PCM extent')
            self.ended = True
        elif line.startswith(self.record):
            if self.checked:
                raise ValueError('Record after CHECK')
            pattern = self.record + (r' ([A-Za-z0-9+/=]+)' if self.prefix == 'ARRIVALS'
                                     else r' (\d+) ([A-Za-z0-9+/=]+)')
            match = re.fullmatch(pattern, line)
            if not match:
                raise ValueError('Malformed record')
            payload = base64.b64decode(match.groups()[-1], validate=True)
            if self.prefix == 'ARRIVALS':
                if not payload or len(payload) % 32 or len(payload) > 512:
                    raise ValueError('Invalid arrival extent')
            else:
                if int(match[1]) != self.samples or not payload or len(payload) % 2 or len(payload) > 384:
                    raise ValueError('Missing/duplicate/corrupt PCM record')
                self.samples += len(payload) // 2
            if self.lines:
                self.digest.update(b'\n')
            self.digest.update(line.encode('ascii'))
            self.lines += 1
        elif line.startswith('AEC_TIME') and self.prefix == 'AEC_DUMP':
            match = re.fullmatch(r'AEC_TIME start=(\d+) end=(\d+) lower_us=(-?\d+) upper_us=(-?\d+) gap=([01])', line)
            if self.checked or not match:
                raise ValueError('Malformed timing row')
            start, end, lower, upper, _ = map(int, match.groups())
            if start != self.timing_end or not start < end <= self.frames or lower > upper:
                raise ValueError('Invalid timing extent')
            self.timing_end = end
            self.timing_count += 1
        else:
            raise ValueError('Malformed dump line')

    def finish(self):
        if not self.ended:
            raise ValueError('Missing dump trailer')
        return self.lines


def verify_dump(lines, *, prefix='AEC_DUMP'):
    verifier = DumpVerifier(prefix)
    for line in lines:
        verifier.feed(line)
    return verifier.finish()


def read_dump(readline, *, wire_bytes=None, rate=None, prefix='AEC_DUMP', console_kind='uart',
              line_count=None, clock=time.monotonic):
    """Bound the whole export by UART wire time or USB line pacing, plus 30 s.

    USB gets 30 s to receive BEGIN. Its absolute deadline is then derived from
    BEGIN's PCM extent (192 samples/record), or an explicit upper record count.
    Timing/header/trailer overhead uses the 30-s margin; log traffic never
    extends either deadline. USB intentionally ignores the physical link rate.
    """
    started = clock()
    pace_ms = 8 if prefix == 'AEC_DUMP' else 4
    usb = console_kind == 'usb_serial_jtag'
    budget_known = not usb or line_count is not None
    deadline = started + (30 if not budget_known else deadline_seconds(
        wire_bytes, rate, console_kind=console_kind, line_count=line_count, pace_ms=pace_ms))
    verifier = DumpVerifier(prefix)
    lines = []
    while not verifier.ended:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('Missing dump trailer within console deadline')
        line = readline(remaining)
        if clock() >= deadline:
            raise TimeoutError('Missing dump trailer within console deadline')
        if line is not None:
            verifier.feed(line)
            lines.append(line)
            if usb and not budget_known and verifier.begun:
                count = ((verifier.expected_samples + 191) // 192
                         if verifier.expected_samples is not None else 512)
                deadline = started + deadline_seconds(console_kind=console_kind, line_count=count, pace_ms=pace_ms)
                budget_known = True
    verifier.finish()
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture')
    parser.add_argument('--prefix', choices=RECORDS, default='AEC_DUMP')
    args = parser.parse_args()
    with open(args.capture, encoding='ascii') as stream:
        count = verify_dump(stream, prefix=args.prefix)
    print(f'CHECK verified lines={count}')


if __name__ == '__main__':
    main()
