"""Serial factory with frozen EchoEar behavior and reset-aware P4 acquisition.

IDF 6.1 bootloader_console.c:105 programs CONFIG_ESP_CONSOLE_UART_BAUDRATE.
With UART_CUSTOM, both the second-stage bootloader and product use 2 Mbaud;
open there, ignore undecodable 115200 ROM text and wait for DG_BOOT. A baud
explicitly supplied by the caller supports the existing 115200 spike. If the
app-baud acquisition times out with an overridden boot pattern, reset once at
ROM baud for legacy spikes. The product pattern requires the table's app baud;
never pretend this is passive attach. No global monkeypatch of frozen modules.
"""
from collections import deque
from pathlib import Path
import re
import threading
import time
import serial
import boards
from esp_serial import EspSerial, ROOT, port_lock


class P4Serial(EspSerial):
    def __init__(self, port=None, baud=None, log_path=None, reset_on_open=True,
                 boot_pattern=None, boot_timeout=30):
        if not reset_on_open: raise ValueError('P4 reset_on_open=true; no passive attach path')
        if boot_timeout <= 0: raise ValueError('Positive boot timeout required')
        info = boards.hardware_board('p4xc')
        self.boot_pattern = boot_pattern or info['boot_pattern']; re.compile(self.boot_pattern)
        spike_pattern = self.boot_pattern != info['boot_pattern']
        active_baud = info['app_baud'] if baud is None else baud
        if not spike_pattern and active_baud != info['app_baud']:
            raise ValueError('P4 product boot pattern requires app_baud=2000000')
        self.log_path = Path(log_path or ROOT/'hil/out'/f'p4-serial-{time.time_ns()}.log')
        if not self.log_path.is_absolute(): self.log_path = ROOT/self.log_path
        boards.require_bench()
        port = boards.resolve_port(port, board='p4xc')
        self.boot_timeout = boot_timeout
        self.reset_on_open = True
        self.observed_baud = active_baud
        # EspSerial's constructor resolves only EchoEar. Initialize its reader
        # state locally, preserving inherited cursor/command/log semantics.
        self.display_monitor = None; self.display_cleanup = False
        # Suppress inherited command-timeout S3 diagnostics until step 6
        # defines a P4 snapshot contract. Keep sequenced command semantics.
        self._snapshot_in_progress = True
        self._guard = port_lock(); self._guard.__enter__()
        self._cv = threading.Condition(); self._lines = deque(maxlen=20000)
        self._cursor = 0; self._seq = 0; self._send_lock = threading.Lock()
        self._error = None; self._stop = threading.Event()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = self.log_path.open('wb')
            self._audio_log = self.log_path.with_suffix('.audio.jsonl').open('w')
            self.s = serial.Serial(None, active_baud, timeout=.1, write_timeout=2)
            self.s.dtr = False; self.s.rts = False; self.s.port = port; self.s.open()
            self._thread = threading.Thread(target=self._reader, daemon=True); self._thread.start()
            try:
                self.boot_line = self.wait_for(self.boot_pattern, boot_timeout, since=0)
            except TimeoutError:
                if not spike_pattern or baud is not None or active_baud == info['rom_baud']: raise
                self.s.baudrate = info['rom_baud']; self.observed_baud = info['rom_baud']
                self.boot_line = self.reset(boot_timeout)
        except BaseException:
            self._stop.set()
            if hasattr(self, '_thread') and self._thread.is_alive(): self._thread.join(2)
            if hasattr(self, 's'): self.s.close()
            if hasattr(self, '_log'): self._log.close()
            if hasattr(self, '_audio_log'): self._audio_log.close()
            self._guard.__exit__(None, None, None)
            raise

    def reset(self, timeout=None):
        cursor = self.cursor
        self.s.reset_input_buffer()
        self.s.dtr = False; self.s.rts = True; time.sleep(.15); self.s.rts = False
        return self.wait_for(self.boot_pattern, self.boot_timeout if timeout is None else timeout, since=cursor)

    def __exit__(self, *args):
        # The frozen context exit collects S3-specific failure diagnostics.
        # P4 has no such contract yet; close without issuing those commands.
        self.close()


def open_board(board='echoear', **kw):
    boards.hardware_board(board)
    if board == 'echoear': return EspSerial(**kw)
    return P4Serial(**kw)
