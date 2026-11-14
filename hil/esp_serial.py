"""Cursor-based serial capture and a shared, cross-process flash/serial lock."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
from board_port import resolve_port
import json
import re
import threading
import time
import serial

ROOT = Path(__file__).resolve().parents[1]

@contextmanager
def port_lock():
    # A campaign wrapper may own the bench lock across bot + serial activity.
    # Reuse its inherited open-file description, otherwise acquire locally.
    inherited = os.environ.get('DG_BENCH_LOCK_FD')
    bench = None
    if inherited:
        fd = int(inherited)
        if os.fstat(fd).st_ino != os.stat(f'/tmp/claude-{os.getuid()}/echoear-bench.lock').st_ino:
            raise RuntimeError('Invalid inherited bench lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        os.makedirs(f'/tmp/claude-{os.getuid()}', exist_ok=True)
        bench = open(f'/tmp/claude-{os.getuid()}/echoear-bench.lock', 'a')
        try:
            fcntl.flock(bench, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            bench.close()
            raise RuntimeError('EchoEar bench is busy')
    try:
        with _serial_lock():
            yield
    finally:
        if bench:
            bench.close()

@contextmanager
def _serial_lock():
    with open("/tmp/dg-esp32-serial.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Serial port is in use by capture or flash") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

class SerialDisconnected(RuntimeError):
    """Serial I/O failed; a demo may retain its independent network services."""


class EspSerial:
    def __init__(self, port=None, baud=115200, log_path=None, display_checks=False, display_diagnostic=False, reset_on_open=True):
        port = resolve_port(port)
        self.display_monitor = None
        self.display_cleanup = False
        if display_checks:
            from step9a_r2_checks import DisplayMonitor
            self.display_monitor = DisplayMonitor(diagnostic=display_diagnostic)
        self._guard = port_lock()
        self._guard.__enter__()
        self._cv = threading.Condition()
        self._lines = deque(maxlen=20000)
        self._cursor = 0
        self._seq = 0
        self._send_lock = threading.Lock()
        self._error = None
        self._stop = threading.Event()
        self.log_path = Path(log_path or ROOT / "hil/out" / f"serial-{time.time_ns()}.log")
        if not self.log_path.is_absolute():
            self.log_path = ROOT / self.log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._log = self.log_path.open("wb")
            self._audio_log = self.log_path.with_suffix('.audio.jsonl').open('w')
            if reset_on_open:
                self.s = serial.Serial(port, baud, timeout=0.1, write_timeout=2)
            else:
                # Linux CDC ACM asserts both lines on open. Keep them asserted:
                # pyserial clears DTR before RTS when configured False/False,
                # briefly producing RTS=1,DTR=0, the USB-JTAG reset command.
                # ESP32-S3 TRM 33.4.2, tables 33.4-3/4. No explicit reset here.
                self.s = serial.Serial(None, baud, timeout=0.1, write_timeout=2)
                self.s.dtr = True
                self.s.rts = True
                self.s.port = port
                self.s.open()
        except BaseException:
            if hasattr(self, "_log"):
                self._log.close()
            if hasattr(self, "_audio_log"):
                self._audio_log.close()
            self._guard.__exit__(None, None, None)
            raise
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    @property
    def cursor(self):
        with self._cv:
            return self._cursor

    def _reader(self):
        pending = b""
        try:
            while not self._stop.is_set():
                data = self.s.read(self.s.in_waiting or 1)
                if not data:
                    continue
                self._log.write(data)
                self._log.flush()
                if self.display_monitor:
                    self.display_monitor.feed(data)
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    received=time.monotonic()
                    decoded=line.decode(errors="replace").rstrip()
                    if self.display_monitor:
                        self.display_monitor.line(decoded)
                    state=re.fullmatch(r'AUDIO_STATE enabled=([01]) device_us=(\d+)',decoded)
                    if state:
                        self._audio_log.write(json.dumps(dict(enabled=int(state[1]),device_us=int(state[2]),host_s=received))+'\n')
                        self._audio_log.flush()
                    with self._cv:
                        self._cursor += 1
                        self._lines.append((self._cursor, received,decoded))
                        self._cv.notify_all()
                if len(pending) > 65536:
                    raise RuntimeError("Serial line exceeds 64 KB")
        except BaseException as exc:
            with self._cv:
                self._error = exc
                self._cv.notify_all()

    def lines_since(self, cursor):
        if self.display_monitor and not self.display_cleanup:
            self.display_monitor.check()
        with self._cv:
            if self._error:
                kind = SerialDisconnected if isinstance(self._error, (OSError, serial.SerialException)) else RuntimeError
                raise kind("Serial reader failed") from self._error
            if self._lines and cursor < self._lines[0][0] - 1:
                raise RuntimeError("Serial cursor expired from capture ring")
            return [entry for entry in self._lines if entry[0] > cursor]

    def wait_for(self, regex, timeout, since=None):
        return self.wait_for_entry(regex, timeout, since)[2]

    def latest_entry(self, regex):
        """Latest matching retained line, without requesting an expired cursor."""
        pattern = re.compile(regex)
        with self._cv:
            if self._error:
                kind = SerialDisconnected if isinstance(self._error, (OSError, serial.SerialException)) else RuntimeError
                raise kind("Serial reader failed") from self._error
            return next((entry for entry in reversed(self._lines)
                         if pattern.search(entry[2])), None)

    def wait_for_entry(self, regex, timeout, since=None):
        """Return cursor, monotonic receive timestamp and matched line."""
        since = self.cursor if since is None else since
        pattern = re.compile(regex)
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for entry in self.lines_since(since):
                    if pattern.search(entry[2]):
                        return entry
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Serial timeout waiting for {regex!r}")
                self._cv.wait(remaining)

    def reset(self, timeout=10):
        cursor = self.cursor
        if self.display_monitor:self.display_monitor.expect_boot_reset()
        try:
            self.s.dtr = False
            self.s.rts = True
            time.sleep(0.1)
            self.s.rts = False
            pattern=(r'^I \([0-9]+\) dg-esp32: ESP32-S3 step 7 audio elf_sha256=[0-9a-f]{64}$'
                     if self.display_monitor else r'READY|dg-esp32: ESP32-S3')
            return self.wait_for(pattern, timeout, since=cursor)
        except BaseException as exc:
            if self.display_monitor:self.display_monitor.fail_boot_reset(type(exc).__name__+': '+str(exc))
            raise

    def send(self, cmd):
        if "\n" in cmd or "\r" in cmd:
            raise ValueError("One command per send")
        self.lines_since(self.cursor)  # Surface reader failures before writing.
        with self._send_lock:
            self._seq += 1
            self.s.write(f"#{self._seq} {cmd}\n".encode())
            return self._seq

    def command(self, cmd, timeout=5):
        cursor = self.cursor
        seq = self.send(cmd)
        try:
            line = self.wait_for(rf"^(OK|ERR) #{seq}(?:\s|$)", timeout, since=cursor)
        except TimeoutError:
            if not getattr(self, '_snapshot_in_progress', False):
                self._snapshot_in_progress = True
                try:
                    from failure_snapshot import capture_failure
                    safe_cmd=' '.join(cmd.split(' ',2)[:2]) if cmd.startswith('disp ') else cmd
                    capture_failure(self, self.log_path.parent/f'ack-timeout-{seq}', f'ack timeout: {safe_cmd}')
                except Exception as exc:
                    self.snapshot_error=f'{type(exc).__name__}: {exc}'
                finally:
                    self._snapshot_in_progress = False
            raise
        if line.startswith("ERR"):
            raise RuntimeError(line)
        return line

    def close(self):
        if getattr(self, '_closed', False):
            return
        self._closed = True
        self._stop.set()
        self._thread.join(2)
        try:
            self.s.close()
        finally:
            self._log.close()
            self._audio_log.close()
            self._guard.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            if args and args[0] is not None:
                self._snapshot_in_progress=True
                try:
                    from failure_snapshot import capture_failure
                    capture_failure(self,self.log_path.parent,args[1])
                except Exception as exc:
                    self.snapshot_error=f'{type(exc).__name__}: {exc}'
                finally:
                    self._snapshot_in_progress=False
        finally:
            self.close()
