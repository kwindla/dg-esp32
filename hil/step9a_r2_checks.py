"""Byte-level driver stops, strict stat parsing and rendered-counter liveness."""
import json
import re
import threading
import time

RAW_ERROR = re.compile(rb'DMA TX underflow|E spi_master|E \(')
# Crash evidence that may print without any E-level companion or a second boot banner (Astra r3 #4): ESP-IDF
# panic handler, abort/assert, backtrace, task watchdog, brownout, and any ROM reset reason other than the two
# expected boots. r5 (Fable r4 N2): the reason is matched on its code alone, so a torn
# "rst:0x15 (USB_UART_CHIP_RE" is not a crash. Expected: 0x15 USB_UART_CHIP_RESET (every harness/esptool reset
# in the retained data) and 0x1 POWERON. 0x16 USB_JTAG_CHIP_RESET is NOT expected: no harness path issues a JTAG
# chip reset and no retained log shows one, so it fails closed. Shared by the raw monitor and the D6 segment
# check. r5 (Fable r4 N1): firmware free-text echoes (RTVI user/bot transcripts and SERVER messages, app.cpp:275/
# 276/285) are excluded; the ESP-IDF panic handler never prints under that prefix.
CRASH_EVIDENCE = re.compile(r"Guru Meditation|panic'ed|abort\(\) was called|assert failed|Backtrace:|"
                            r"Task watchdog got triggered|E \(\d+\) task_wdt:|[Bb]rownout|"
                            r"\brst:0x(?!15\b|1\b)[0-9a-fA-F]+")
FREE_TEXT_ECHO = re.compile(r'^I \(\d+\) dg-app: (?:RTVI (?:user|bot)\[|SERVER )')
# r7 (Astra r6 #1, Fable r6 N5): task_wdt counts only as the "Task watchdog got triggered" message or an E-level
# `E (..) task_wdt:` record; the bare tag on I/D/W lines (e.g. `I (1) task_wdt: initialized`, IDF retention notes)
# is informational and does not count.
# r6 (Astra r5 #4): conservative rule for echo-prefixed lines. A torn echo prefix can merge with a real panic
# header in the byte stream, so STRUCTURALLY SPECIFIC signatures count on every line, echo-prefixed or not:
# the ESP-IDF panic header, a register backtrace, abort-at-PC, assert with file:line, the brownout and task
# watchdog messages, the task_wdt tag form, and every reset-reason code (same code-only rule as above). Only bare
# generic words (brownout, Backtrace:, assert failed, panic'ed, watchdog) are ignored on an echo line; anywhere
# else they still count through CRASH_EVIDENCE.
STRUCTURAL_CRASH = re.compile(r"Guru Meditation Error: Core|panic'ed \(|Backtrace: ?0x[0-9a-fA-F]{8}:0x|"
                              r"abort\(\) was called at PC 0x|assert failed: .* [^ ]+\.c:\d+|"
                              r"Brownout detector was triggered|Task watchdog got triggered|E \(\d+\) task_wdt:|"
                              r"\brst:0x(?!15\b|1\b)[0-9a-fA-F]+")


def crash_match(line):
    structural = STRUCTURAL_CRASH.search(line)
    if structural: return structural
    return None if FREE_TEXT_ECHO.match(line) else CRASH_EVIDENCE.search(line)


def crash_lines(text):
    return [line for line in text.splitlines() if crash_match(line)]
BOT_LOSS_BODIES = ('AGENT: 0 Peer not response for binding after 30000',
                   'PEER_DEF: Peer disconnected')
BOT_LOSS_LINES = tuple(re.compile(rb'E \([0-9]+\) '+re.escape(body.encode())) for body in BOT_LOSS_BODIES)


class DisplayMonitor:
    def __init__(self, diagnostic=False, expected_boot_indices=()):
        self.diagnostic = diagnostic
        self.tail = b''
        self.offset = 0
        self.errors = []
        self.raw_matches = {}
        self.last_rendered = None
        self.snapshots = 0
        self.app_control_min_free = None
        self._lock = threading.RLock()
        self.bot_down_windows = []
        self.expected_losses = {}
        self.pending_losses = {}
        self.boot_epochs=[]
        self.expected_boot_indices=set(expected_boot_indices)
        self.pending_requested_boots=0
        self.perf_records=0

    def expect_boot_reset(self):
        """Host controller ledger; does not clear any failure or history."""
        with self._lock:self.pending_requested_boots+=1

    def fail_boot_reset(self,reason):
        with self._lock:
            self.pending_requested_boots=0
            self.fail(dict(requested_boot_failed=reason,origin='non-display',component='dg-esp32'))

    def begin_bot_down(self, label, maximum_s=180):
        with self._lock:
            self.check()
            if not 0 < maximum_s <= 180 or any(w['end_host_s'] is None for w in self.bot_down_windows):
                raise ValueError('One finite bot-down window at a time')
            now=time.monotonic()
            window=dict(label=label,start_host_s=now,start_byte_offset=self.offset,
                deadline_host_s=now+maximum_s,end_host_s=None,end_byte_offset=None,
                allowed_lines=[p.pattern.decode() for p in BOT_LOSS_LINES],
                close_rule='first subsequent complete STATE Ready, explicit close, or deadline')
            self.bot_down_windows.append(window)
            return window

    def end_bot_down(self, reason='arm ended'):
        with self._lock:
            for window in self.bot_down_windows:
                if window['end_host_s'] is None:
                    window.update(end_host_s=time.monotonic(),end_byte_offset=self.offset,end_reason=reason)
            for detail in self.pending_losses.values():
                self.fail(detail)
            self.pending_losses.clear()

    def _loss_window(self, offset, now):
        return next((w for w in self.bot_down_windows
            if w['start_byte_offset']<=offset and
            (w['end_byte_offset'] is None or offset<w['end_byte_offset']) and
            w['start_host_s']<=now<=w['deadline_host_s'] and
            (w['end_host_s'] is None or now<=w['end_host_s'])),None)

    @staticmethod
    def _loss_prefix(raw_line):
        # Only incomplete prefixes of the two exact permitted records may wait
        # for a newline. Arbitrary E-level records still stop immediately.
        if re.fullmatch(rb'E \([0-9]*',raw_line) or re.fullmatch(rb'E \([0-9]+\) ?',raw_line):return True
        prefix=re.match(rb'E \([0-9]+\) ',raw_line)
        return bool(prefix and any(body.encode().startswith(raw_line[prefix.end():]) for body in BOT_LOSS_BODIES))

    def fail(self, reason):
        self.errors.append(reason)

    def feed(self, data):
        with self._lock:
            self._feed(data)

    def _feed(self, data):
        merged = self.tail + data
        now=time.monotonic()
        base=self.offset-len(self.tail)
        # Close at the Ready record's BYTE boundary, not when the caller later
        # observes it. An E line after Ready in this same USB chunk must fail.
        for ready in re.finditer(rb'(?m)^(?:I \([0-9]+\) dg-app: )?STATE Ready gen=[0-9]+ desired=1 t=[0-9]+\r?\n',merged):
            for window in self.bot_down_windows:
                if window['end_host_s'] is None and base+ready.start()>=window['start_byte_offset']:
                    window.update(end_host_s=now,end_byte_offset=base+ready.start(),end_reason='STATE Ready')
        for match in RAW_ERROR.finditer(merged):
            offset = self.offset-len(self.tail)+match.start()
            begin = merged.rfind(b'\n', 0, match.start())+1
            end = merged.find(b'\n', match.end())
            raw_line = merged[begin:end if end >= 0 else len(merged)]
            line = raw_line.decode(errors='replace').rstrip()
            tag = re.search(r'E (?:\(\d+\) )?([^:\s]+):', line)
            component = tag[1] if tag else None
            origin = ('display' if component and component.startswith(('display','lcd','spi','gdma','dg_display'))
                      else 'non-display' if component else 'unknown')
            detail = dict(raw_error=match.group().decode(errors='replace'), byte_offset=offset,
                          matched_line=line[:4096], component=component, origin=origin,
                          line_complete=end >= 0, line_truncated=len(line)>4096)
            seen=offset in self.raw_matches
            if seen:
                if not self.raw_matches[offset]['line_complete']:
                    self.raw_matches[offset].update(detail)
            else:
                self.raw_matches[offset] = detail
            detail=self.raw_matches[offset]
            if offset in self.expected_losses or (seen and offset not in self.pending_losses):continue
            window=self._loss_window(offset,now)
            exact=raw_line.rstrip(b'\r')
            if window and end>=0 and any(p.fullmatch(exact) for p in BOT_LOSS_LINES):
                detail.update(window=window['label'],received_host_s=now)
                self.expected_losses[offset]=detail;self.pending_losses.pop(offset,None)
            elif window and end<0 and self._loss_prefix(exact):
                self.pending_losses[offset]=detail
            else:
                self.pending_losses.pop(offset,None);self.fail(detail)
        self.offset += len(data)
        self.tail = merged[-4096:]

    def line(self, line):
        crash=crash_match(line)
        if crash:
            self.fail(dict(crash_evidence=crash.group(),matched_line=line[:512],origin='non-display',component='crash'))
        boot=re.fullmatch(r'I \([0-9]+\) dg-esp32: ESP32-S3 step 7 audio elf_sha256=([0-9a-f]{64})',line)
        if boot:
            with self._lock:
                index=len(self.boot_epochs)
                requested=self.pending_requested_boots>0 or index in self.expected_boot_indices
                if self.pending_requested_boots:self.pending_requested_boots-=1
                self.boot_epochs.append(dict(index=index,elf_sha256=boot[1],requested=requested))
                if (index or self.perf_records) and not requested:
                    self.fail(dict(unexpected_boot=index,elf_sha256=boot[1],origin='non-display',component='dg-esp32',matched_line=line))
                self.last_rendered=None
            return
        stack = re.search(r'STACK task=app-control free_bytes=(\d+)', line)
        if stack:
            free = int(stack[1])
            self.app_control_min_free = min(free, self.app_control_min_free or free)
            if free < 2048:
                self.fail({'app_control_stack_free': free, 'floor': 2048})
        # ROM errors can occur in the middle of JSON; syntax failure is retained.
        match = re.match(r'^(PERF|DISPLAY(?:_[A-Z0-9_]+)?) ', line)
        if match:
            prefix = match[0]
            try:
                value = json.loads(line[match.end():])
                if not isinstance(value, dict):
                    raise ValueError('stat is not an object')
            except (ValueError, TypeError) as exc:
                self.fail({'malformed_stat': prefix.strip(), 'reason': str(exc)})
                return
            if prefix not in ('PERF ', 'DISPLAY '):
                return
            health = value.get('display') if prefix == 'PERF ' else value
            if health is None:
                self.fail({'missing_display_health': True})
                return
            # Versioned R4 firmware marks initialized LCDs explicitly; legacy
            # captures remain legacy and are not rescored as having this field.
            if health.get('lcd_initialized'):
                if health.get('lcd_tx_captured') is not True or type(health.get('priority_applies')) is not int or health['priority_applies']<1 or type(health.get('last_priority_written')) is not int or health['last_priority_written']!=1 or type(health.get('tx_pri')) is not int or health['tx_pri']!=1:
                    self.fail({'display_failure':'lcd_tx_priority_applied',
                        'captured':health.get('lcd_tx_captured'),'priority_applies':health.get('priority_applies'),
                        'last_priority_written':health.get('last_priority_written'),'tx_pri':health.get('tx_pri')})
            dma=health.get('dma')
            if prefix=='DISPLAY ' and isinstance(dma,list) and len(dma)==2 and dma[1]:
                self.fail({'display_failure':'dma_errors','value':dma[1]})
            for key in ('display_recovery_attempts', 'display_recoveries',
                        'gate_timeouts', 'display_gate_timeouts', 'dma_errors', 'send_failures', 'late_callbacks', 'dead', 'failed', 'diagnostic'):
                if health.get(key, 0) and not (key=='diagnostic' and self.diagnostic):
                    self.fail({'display_failure': key, 'value': health[key]})
            if prefix == 'PERF ':
                self.perf_records+=1
                rendered = health.get('rendered')
                if not isinstance(rendered, int):
                    self.fail({'missing_rendered_counter': True})
                elif health.get('animation'):
                    if self.last_rendered is not None and rendered <= self.last_rendered:
                        self.fail({'display_liveness': False, 'previous': self.last_rendered,
                                   'rendered': rendered})
                    self.last_rendered = rendered
                    self.snapshots += 1
                else:
                    self.last_rendered = None

    def check(self):
        with self._lock:
            if self.pending_losses and not all(self._loss_window(offset,time.monotonic()) for offset in self.pending_losses):
                self.end_bot_down('incomplete expected line at boundary/deadline')
        if self.errors and not self.diagnostic:
            error = self.errors[0]
            origin = error.get('origin', 'display')
            raise RuntimeError(f'Strict {origin} stop rule: '+repr(error))

    def result(self):
        with self._lock:
            return {'diagnostic': self.diagnostic, 'errors': list(self.errors),
                'animation_snapshots': self.snapshots, 'raw_bytes': self.offset,
                'app_control_min_free': self.app_control_min_free,
                'bot_down_windows': [dict(w) for w in self.bot_down_windows],
                'expected_bot_losses': list(self.expected_losses.values()),
                'incomplete_bot_losses': list(self.pending_losses.values()),
                'boot_epochs':[dict(b) for b in self.boot_epochs],
                'pending_requested_boots':self.pending_requested_boots,
                'passed': not self.errors and not self.pending_losses and not self.pending_requested_boots}


def audit(data, diagnostic=False, expected_boot_indices=()):
    monitor = DisplayMonitor(diagnostic,expected_boot_indices)
    monitor.feed(data)
    for line in data.splitlines():
        monitor.line(line.decode(errors='replace'))
    if monitor.expected_boot_indices-set(range(len(monitor.boot_epochs))):
        monitor.fail(dict(missing_requested_boot_records=sorted(monitor.expected_boot_indices-set(range(len(monitor.boot_epochs))))))
    monitor.check()
    return monitor.result()
