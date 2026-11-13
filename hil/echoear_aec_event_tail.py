"""Bounded, append-only JSONL polling for capture and stimulus critical paths.

Prime at EOF before sending a request to avoid parsing historical audio events.
Rotation/truncation is an acquisition failure, never a reason to reuse old state.
"""
import json
import threading
import time
from collections import defaultdict
from pathlib import Path


class EventStreamChanged(RuntimeError):
    pass


class JsonlTail:
    def __init__(self, paths, *, at_end=False, max_bytes=262144):
        self.paths = paths if callable(paths) else lambda: paths
        self.max_bytes = max_bytes
        self.files = {}
        self.last_poll = {}
        self.backlog = False
        if at_end:
            for path in self.paths():
                path = Path(path)
                if path.exists():
                    with path.open('rb') as stream:
                        self.files[path] = self._state(stream, at_end=True)

    @staticmethod
    def _state(stream, at_end=False):
        import os
        stat = os.fstat(stream.fileno())
        offset = stat.st_size if at_end else 0
        stream.seek(max(0, offset-64))
        mark = stream.read(offset-stream.tell())
        return dict(identity=(stat.st_dev, stat.st_ino), offset=offset,
                    mark=mark, partial=b'', discard=bool(mark and not mark.endswith(b'\n')))

    def poll(self):
        import os
        start = time.monotonic(); cpu = time.thread_time()
        consumed = 0; result = []; self.backlog = False
        paths = {Path(p) for p in self.paths()}
        if set(self.files)-paths:
            raise EventStreamChanged('JSONL path disappeared')
        for path in sorted(paths):
            if not path.exists():
                if path in self.files: raise EventStreamChanged('JSONL path disappeared')
                continue
            with path.open('rb') as stream:
                if path not in self.files:self.files[path] = self._state(stream)
                state = self.files[path]
                stat = os.fstat(stream.fileno())
                offset = state['offset']
                if (stat.st_dev, stat.st_ino) != state['identity'] or stat.st_size < offset:
                    raise EventStreamChanged('JSONL rotated or truncated')
                stream.seek(offset-len(state['mark']))
                if stream.read(len(state['mark'])) != state['mark']:
                    raise EventStreamChanged('JSONL rewritten at committed offset')
                stream.seek(offset)
                data = stream.read(max(0, self.max_bytes-consumed)); consumed += len(data)
                state['offset'] += len(data)
                state['mark'] = (state['mark']+data)[-64:]
                # Only unread bytes from the pre-read snapshot are backlog.
                # A writer appending after read() is ordinary concurrent progress.
                self.backlog |= state['offset'] < stat.st_size
                data = state['partial']+data
                lines = data.split(b'\n'); state['partial'] = lines.pop()
                if len(state['partial']) > self.max_bytes:
                    raise EventStreamChanged('Unbounded JSONL partial line')
                if state['discard'] and lines:
                    lines.pop(0); state['discard'] = False
                for line in lines:
                    if line: result.append(json.loads(line))
        self.last_poll = dict(start_s=start, end_s=time.monotonic(), bytes_consumed=consumed,
                              rows_consumed=len(result), cpu_s=time.thread_time()-cpu,
                              backlog=self.backlog)
        self.last_poll['wall_s'] = self.last_poll['end_s']-start
        return result


class EventCache:
    """One reader plus indexed case history; thread safe for acknowledgements."""
    def __init__(self, paths, *, at_end=False):
        self.tail = JsonlTail(paths, at_end=at_end)
        self.rows = []; self.cases = defaultdict(list); self.lock = threading.Lock()

    def case_rows(self, case_id):
        """Snapshot one already-polled key; None means case-less, not all rows."""
        with self.lock:
            return list(self.cases.get(case_id, ()))

    def read(self, case_id=None):
        with self.lock:
            while True:
                for row in self.tail.poll():
                    self.rows.append(row); self.cases[row.get('case_id')].append(row)
                if not self.tail.backlog:break
                if case_id is not None:
                    raise EventStreamChanged('Evaluation event backlog exceeds bounded capture poll')
                # Outside a timed case, catch up after idle probes/export. Never
                # drop unseen health/error events or impose a new idle-time gate.
            return list(self.rows if case_id is None else self.cases[case_id])


class ContextIdentity:
    """Cache a requested case's identity, but keep checking new conflicting starts."""
    def __init__(self, paths, case_id, *, at_end=True):
        self.tail = JsonlTail(paths, at_end=at_end)
        self.case_id = case_id; self.owner = None

    def __call__(self):
        for row in self.tail.poll():
            if row.get('case_id') != self.case_id or row.get('event') != 'tts-generation-metric-start':
                continue
            identity = dict(pc_id=row.get('pc_id'), context_id=row.get('context_id'))
            if not all(identity.values()) or (self.owner is not None and identity != self.owner):
                raise EventStreamChanged('Missing or ambiguous requested context')
            self.owner = identity
        return self.owner
