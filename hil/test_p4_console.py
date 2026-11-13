"""Host-only UART console diagnostics and end-to-end paced export integrity."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from dump_verify import verify_dump
from p4_headless import console_command, verify_network_dump
from wire_contract import ContractError

ROOT = Path(__file__).resolve().parents[1]


class Reader:
    def __init__(self, *, drops=True, perf=None, expired=False):
        self.rows = []
        if perf is not None: self.rows.append('PERF '+json.dumps(dict(console_output=perf)))
        self.rows.append('NETPROBE_DUMP END'); self.calls = []; self.drops = drops; self.expired = expired
    @property
    def cursor(self): return len(self.rows)
    def command(self, text, timeout):
        self.calls.append((text, timeout))
        if text != 'console-drops' or not self.drops: raise TimeoutError('missing ACK')
        self.rows.extend(['CONSOLE_DROPS full=3 transport_drops=1 ack_pending=0', 'OK #12 console-drops'])
        return 'OK #12 console-drops'
    def lines_since(self, cursor):
        if self.expired and cursor == 0: raise RuntimeError('cursor expired')
        return [(i+1, 0, line) for i, line in enumerate(self.rows) if i+1 > cursor]
    def latest_entry(self, pattern):
        if pattern == '^PERF ': return next(((i+1,0,row) for i,row in reversed(list(enumerate(self.rows))) if row.startswith('PERF ')), None)
        return (len(self.rows), 0, self.rows[-1])


def test_uart_timeout_names_last_line_and_fresh_drop_counts_without_retry(tmp_path):
    board = Reader()
    with pytest.raises(TimeoutError, match='NETPROBE_DUMP END') as error:
        console_command(board, 'netprobe dump', 60, uart=True, out=tmp_path)
    assert '"full":3' in str(error.value) and '"transport_drops":1' in str(error.value)
    assert board.calls == [('netprobe dump', 60), ('console-drops', 2)]
    assert json.loads((tmp_path/'console-ack-timeout.json').read_text())['last_line'] == 'NETPROBE_DUMP END'


@pytest.mark.parametrize('expired', [False, True])
def test_timeout_diagnostics_fall_back_to_last_stats_even_if_reader_cursor_expired(expired):
    board = Reader(drops=False, perf=dict(full=7, transport_drops=2), expired=expired)
    with pytest.raises(TimeoutError, match='last PERF') as error:
        console_command(board, 'netprobe dump', 60, uart=True)
    assert '"full":7' in str(error.value) and 'NETPROBE_DUMP END' in str(error.value)


def test_unknown_counters_and_usb_timeout_do_not_change_restore_errors():
    with pytest.raises(TimeoutError, match='unavailable'):
        console_command(Reader(drops=False), 'netprobe dump', 60, uart=True)
    board = Reader()
    with pytest.raises(TimeoutError, match='^missing ACK$'):
        console_command(board, 'netprobe dump', 60, uart=False)
    assert board.calls == [('netprobe dump', 60)]


@pytest.fixture(scope='module')
def exports(tmp_path_factory):
    directory = tmp_path_factory.mktemp('console-exports')
    for kind in ('uart', 'usb'):
        binary = ROOT/'build/host-diagnostics'/f'console_output_{kind}_test'
        subprocess.run([str(binary), '--gtest_filter=ConsoleOutput.PacedExportsAndChecksKeepUsbGoldenBytesAndNeverFillTheSmallRing'],
                       env=dict(os.environ, CONSOLE_FIXTURE_DIR=str(directory)), check=True, capture_output=True, text=True)
    return directory


@pytest.mark.parametrize('kind', ['uart','usb'])
@pytest.mark.parametrize('prefix', ['AEC_DUMP','MIC_DUMP','DUMP','ARRIVALS'])
def test_real_output_queue_paced_dumps_verify_with_the_frozen_check_reader(exports, kind, prefix):
    lines = (exports/f'{kind}-{prefix}.log').read_text().splitlines()
    assert verify_dump(lines, prefix=prefix) == 100
    damaged = list(lines); damaged.remove(next(row for row in damaged if row.startswith(dict(AEC_DUMP='AEC_PCM ',MIC_DUMP='MIC_PCM ',DUMP='PCM ',ARRIVALS='ARRIVAL ')[prefix])))
    with pytest.raises(ValueError): verify_dump(damaged, prefix=prefix)


@pytest.mark.parametrize('kind', ['uart','usb'])
def test_netprobe_record_count_and_uart_check_reject_truncated_or_changed_exports(exports, kind):
    text = (exports/f'{kind}-NETPROBE_DUMP.log').read_text()
    stats = dict(paths=[dict(received=2400, send_errors=0, overwritten=0)])
    assert verify_network_dump(text, stats, require_check=kind=='uart') == 2400
    lines = text.splitlines(); row = next(line for line in lines if line.startswith('NETPROBE_ROW '))
    with pytest.raises(ContractError, match='record count mismatch'):
        verify_network_dump(text.replace(row+'\n','',1), stats, require_check=False)
    if kind == 'uart':
        with pytest.raises(ContractError, match='missing netprobe CHECK'):
            verify_network_dump('\n'.join(line for line in lines if ' CHECK ' not in line), stats, require_check=True)
        with pytest.raises(ContractError, match='SHA256 mismatch'):
            verify_network_dump(text.replace(row,row[:-1]+'B',1), stats, require_check=True)
    else:
        assert ' CHECK ' not in text # Qualified S3 netprobe bytes retain the legacy format.
