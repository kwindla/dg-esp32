"""Run the production C++ CHECK generator and unchanged frozen AEC parser."""
import hashlib
import os
from pathlib import Path
import subprocess
import unittest

from dump_verify import RECORDS, deadline_seconds, read_dump, verify_dump
from echoear_aec_capture import decode_capture


FIXTURE = Path(os.environ.get('DG_DUMP_FIXTURE', 'build/host-diagnostics/dump_fixture')).resolve()


def fixture(prefix='AEC_DUMP', channels=3):
    return subprocess.check_output([str(FIXTURE), prefix, str(channels)], text=True).splitlines()


class DumpTests(unittest.TestCase):
    def test_production_check_matches_independent_sha256(self):
        for prefix, record in RECORDS.items():
            with self.subTest(prefix=prefix):
                lines = fixture(prefix)
                records = [line for line in lines if line.startswith(record + ' ')]
                digest = hashlib.sha256('\n'.join(records).encode('ascii')).hexdigest()
                self.assertEqual(lines[-2], f'{prefix} CHECK lines={len(records)} sha256={digest}')
                self.assertEqual(verify_dump(lines, prefix=prefix), len(records))

    def test_frozen_parser_ignores_check_both_s3_schemas(self):
        for channels in (3, 5):
            with self.subTest(channels=channels):
                lines = fixture(channels=channels)
                self.assertGreater(verify_dump(lines), 0)
                decoded = decode_capture(lines)
                old = decode_capture([line for line in lines if not line.startswith('AEC_DUMP CHECK')])
                self.assertEqual(decoded['pcm'].tobytes(), old['pcm'].tobytes())
                expected = bytes(i % 251 for i in range(130 * channels * 2))
                self.assertEqual(decoded['pcm'].tobytes(), expected)
                self.assertEqual(decoded['pcm'].shape, (130, channels))
                self.assertEqual(decoded['timing'].tolist(), [[0, 130, 100, 200, 0]])

    def test_dropped_duplicated_corrupted_truncated_each_dump(self):
        for prefix, record in RECORDS.items():
            lines = fixture(prefix)
            index = next(i for i, line in enumerate(lines) if line.startswith(record + ' '))
            bad = lines.copy()
            bad[index] = bad[index][:-2] + ('A' if bad[index][-2] != 'A' else 'B') + bad[index][-1]
            mutations = {
                'dropped': lines[:index] + lines[index+1:],
                'duplicate': lines[:index] + [lines[index]] + lines[index:],
                'corrupt': bad,
                'truncated-record': lines[:index] + [lines[index][:15]] + lines[index+1:],
                'truncated-trailer': lines[:-1],
                'missing-check': lines[:-2] + lines[-1:],
                'duplicate-check': lines[:-1] + lines[-2:],
                'duplicate-end': lines + lines[-1:],
                'record-after-check': lines[:-1] + [lines[index]] + lines[-1:],
                'duplicate-begin': [lines[0]] + lines,
            }
            for kind, mutated in mutations.items():
                with self.subTest(prefix=prefix, kind=kind):
                    with self.assertRaises(ValueError):
                        verify_dump(mutated, prefix=prefix)

    def test_count_and_digest_corruption(self):
        lines = fixture()
        for check in ('AEC_DUMP CHECK lines=999 sha256=' + '0'*64,
                      lines[-2].replace('sha256=', 'sha256=f'),
                      lines[-2].replace('lines=', 'lines=x')):
            with self.assertRaises(ValueError):
                verify_dump(lines[:-2] + [check] + lines[-1:])

    def test_missing_corrupt_timing_and_wrong_extent(self):
        lines = fixture()
        for mutated in (lines[:1] + lines[2:],
                        lines[:2] + [lines[1]] + lines[2:],
                        [line.replace('start=0 end=130', 'start=1 end=130') for line in lines],
                        [line.replace('END frames=130', 'END frames=129') for line in lines]):
            with self.assertRaises(ValueError):
                verify_dump(mutated)

    def test_deadline_scales_and_bounds_even_unrelated_log_traffic(self):
        self.assertEqual(deadline_seconds(160000, 200000), 31)
        self.assertEqual(deadline_seconds(160000, 100000), 32)
        for wire, rate in ((-1, 1), (0, 0), (1, -1)):
            with self.assertRaises(ValueError):
                deadline_seconds(wire, rate)
        for traffic in (None, 'unrelated log', 'AEC_DUMP CHECK lines='):
            now = [0.0]
            waits = []
            def reader(timeout):
                waits.append(timeout)
                now[0] += timeout
                return traffic
            with self.assertRaises(TimeoutError):
                read_dump(reader, wire_bytes=160000, rate=200000, clock=lambda: now[0])
            self.assertEqual(waits, [31])

    def test_truncated_capture_times_out_and_success_returns_before_deadline(self):
        for truncated in (False, True):
            lines = fixture()
            now = [10.0]
            pending = iter(lines[:-1] if truncated else lines)
            def reader(timeout):
                line = next(pending, None)
                now[0] += 0.1 if line is not None else timeout
                return line
            if truncated:
                with self.assertRaises(TimeoutError):
                    read_dump(reader, wire_bytes=160000, rate=200000, clock=lambda: now[0])
            else:
                self.assertEqual(read_dump(reader, wire_bytes=160000, rate=200000, clock=lambda: now[0]), lines)

    def test_logs_crlf_and_blank_lines_do_not_change_digest(self):
        lines = ['log\r\n']
        for line in fixture():
            lines += [line + '\r\n', '', 'log']
        self.assertGreater(verify_dump(lines), 0)

    def test_usb_budget_uses_paced_lines_instead_of_link_rate(self):
        self.assertEqual(deadline_seconds(console_kind='usb_serial_jtag', line_count=12500), 130)
        self.assertEqual(deadline_seconds(console_kind='usb_serial_jtag', line_count=12500, pace_ms=4), 80)
        for count in (None, -1):
            with self.assertRaises(ValueError):
                deadline_seconds(console_kind='usb_serial_jtag', line_count=count)
        # A full 30s five-channel AEC export takes 100s in paced PCM lines.
        # A fast USB R must not reduce that to its ~20s wire budget.
        header = fixture(channels=5)[0].replace('frames=130', 'frames=480000')
        now, waits = [0.0], []
        pending = iter((header, 'unrelated log', None))
        def reader(timeout):
            waits.append(timeout)
            line = next(pending)
            now[0] += 0 if line == header else 100 if line is not None else timeout
            return line
        with self.assertRaises(TimeoutError):
            read_dump(reader, console_kind='usb_serial_jtag', wire_bytes=6500000, rate=400000,
                      clock=lambda: now[0])
        self.assertEqual(waits, [30, 130, 30])

    def test_usb_pcm_extent_and_explicit_arrival_count(self):
        for prefix in RECORDS:
            for truncated in (False, True):
                with self.subTest(prefix=prefix, truncated=truncated):
                    lines = fixture(prefix)
                    pending = iter(lines[:-1] if truncated else lines)
                    now = [0.0]
                    def reader(timeout):
                        line = next(pending, None)
                        now[0] += 0.001 if line is not None else timeout
                        return line
                    options = dict(console_kind='usb_serial_jtag', prefix=prefix, clock=lambda: now[0])
                    if prefix == 'ARRIVALS':
                        options['line_count'] = 3
                    if truncated:
                        with self.assertRaises(TimeoutError):
                            read_dump(reader, **options)
                    else:
                        self.assertEqual(read_dump(reader, **options), lines)


if __name__ == '__main__':
    unittest.main()
