"""Owned raw Jabra capture, complete 20ms blocks with AFTER-read receipts.

Receipt times upper-bound samples; they never establish freshness or tail
coverage. The AEC scorer requires independently matched acoustic witnesses.
No ALSA, subprocess or hardware work occurs on import.
"""
from pathlib import Path
import json
import re
import subprocess
import threading
import time
import wave
import numpy as np


class StreamCapture:
    def __init__(self, path, device, *, maximum_seconds=28):
        self.path = Path(path)
        self.maximum = maximum_seconds
        self.chunks, self.receipts = [], []
        self.error = None
        self.stopping = False
        self.log = self.path.with_suffix('.alsa.log').open('w')
        self.started_s = time.monotonic()
        self.process = subprocess.Popen([
            'arecord', '-v', '-D', device, '-f', 'S16_LE', '-r', '16000', '-c', '1',
            '-t', 'raw', '--period-size=320', '--buffer-size=1280'],
            stdout=subprocess.PIPE, stderr=self.log, bufsize=0)
        self.thread = threading.Thread(target=self._read, name='jabra-raw-capture', daemon=True)
        self.thread.start()
        deadline = time.monotonic()+5
        while len(self.receipts)<10 and self.error is None:
            if time.monotonic()>deadline or self.process.poll() is not None:
                self.close()
                raise RuntimeError('Streaming Jabra did not provide ten complete blocks')
            time.sleep(.01)

    def _read(self):
        data = bytearray()
        try:
            while True:
                part = self.process.stdout.read(640-len(data))
                received = time.monotonic()
                if not part:
                    if not self.stopping:
                        self.error = 'Unexpected raw capture EOF'
                    break
                data.extend(part)
                if len(data)==640:
                    self.chunks.append(bytes(data))
                    self.receipts.append((len(self.chunks)*320, received))
                    data.clear()
                    if len(self.chunks)*.02 > self.maximum:
                        self.error = 'Raw capture exceeded predeclared bound'
                        break
        except Exception as exc:
            self.error = type(exc).__name__+': '+str(exc)
        # A stop may cut the final uncommitted partial block; retain its size,
        # never pad it or let the scorer claim coverage of these samples.
        self.partial_tail_bytes = len(data)

    def samples(self):
        return np.frombuffer(b''.join(self.chunks), dtype='<i2').copy()

    def close(self):
        if self.stopping:
            return
        already_exited=self.process.poll()
        if already_exited is not None:
            self.error='Raw capture exited before owned stop: '+str(already_exited)
        self.stopping = True
        owned_termination=False
        if self.process.poll() is None:
            self.process.terminate()
            owned_termination=True
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
        code=self.process.poll()
        # Normal owned SIGTERM may be handled as success or terminate with15.
        # A nonzero child failure cannot disappear in the EOF/close race.
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.error = 'Raw capture reader did not stop'
        self.process.stdout.close()
        self.log.close()
        log = self.path.with_suffix('.alsa.log').read_text()
        # alsa-utils1.2.13's signal_handler prints this line and exits1 even
        # for our requested SIGTERM. Accept only that proven owned shutdown,
        # never an earlier failure, xrun, unknown nonzero exit or forced kill.
        normal_alsa_abort=(code==1 and already_exited is None and owned_termination
            and self.error is None and log.strip().endswith('Aborted by signal Terminated...')
            and not re.search(r'error|failed|overrun|underrun|xrun|suspend',log,re.I))
        if code not in (0,-15) and not normal_alsa_abort:
            self.error='Raw capture failed during stop: '+str(code)
        if any(s in log.lower() for s in ('overrun', 'underrun', 'xrun', 'read error', 'suspend')):
            self.error = 'ALSA discontinuity: see retained log'
        with wave.open(str(self.path), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b''.join(self.chunks))
        self.path.with_suffix('.receipts.json').write_text(json.dumps(dict(
            method='after-read-monotonic-v1', receipts=self.receipts,
            started_s=self.started_s, error=self.error,
            child_returncode=code,owned_termination=owned_termination,
            normal_alsa_sigterm_exit1=normal_alsa_abort,
            partial_tail_bytes=getattr(self, 'partial_tail_bytes', None)), indent=2)+'\n')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
