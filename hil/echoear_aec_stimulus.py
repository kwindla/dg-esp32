"""Bounded stimulus scheduling; callers prime readers before sending requests."""
import heapq
import threading
import time

from echoear_aec_event_tail import JsonlTail, EventStreamChanged


class StimulusPreconditionFailure(RuntimeError):
    """A declared stimulus could not be delivered; never an automatic retry."""


class StimulusTimingFailure(RuntimeError):
    """Retained invocation differs from its requested deadline by over 100 ms."""


class TransportEvents:
    def __init__(self, paths, start_ns, *, at_end=False):
        self.tail = JsonlTail(paths, at_end=at_end)
        self.start_ns = start_ns
        self.pending = []; self.sequence = 0; self.states = {}

    def consume(self, clock=time.monotonic):
        for row in self.tail.poll():
            if (row.get('kind') == 'tts-transport-write' and row.get('drop') is not True
                    and row['monotonic_ns'] >= self.start_ns):
                self.sequence += 1
                heapq.heappush(self.pending, (row['monotonic_ns'], self.sequence, row))
        now = clock()  # after I/O; future replay rows remain queued
        while self.pending and self.pending[0][0] <= now*1e9:
            _, _, row = heapq.heappop(self.pending)
            key = (row.get('pc_id'), row.get('context_id'))
            state = self.states.setdefault(key, dict(anchor=None, latest=None))
            if row.get('non_silent') is True and state['anchor'] is None:
                state['anchor'] = row['monotonic_ns']/1e9
            state['latest'] = row
        return now

    def state(self, owner=None):
        if owner is not None:
            return self.states.get((owner['pc_id'], owner['context_id']), dict(anchor=None, latest=None))
        anchors = [s['anchor'] for s in self.states.values() if s['anchor'] is not None]
        latest = [s['latest'] for s in self.states.values() if s['latest'] is not None]
        return dict(anchor=min(anchors) if anchors else None,
                    latest=max(latest, key=lambda e:e['monotonic_ns']) if latest else None)


def active_transport_witness(paths, start_ns, now_s=None, max_age_s=.12, *, reader=None,
                             owner=None, clock=time.monotonic):
    """Incremental latest write including silence; explicitly timed offline calls supported."""
    if not 0 < max_age_s <= .2:
        raise ValueError('Active TTS freshness must be declared within 200 ms')
    reader = reader or TransportEvents(paths, start_ns)
    now = reader.consume(clock if now_s is None else lambda:now_s)
    latest = reader.state(owner)['latest']
    age = now-latest['monotonic_ns']/1e9 if latest else None
    return dict(active=bool(not reader.tail.backlog and latest and
                latest.get('non_silent') is True and 0 <= age <= max_age_s),
                latest=latest, age_s=age, max_age_s=max_age_s)


def _poll(reader, identity, clock, polls):
    start = time.monotonic(); cpu = time.thread_time()
    owner = identity() if identity else None
    now = reader.consume(clock)
    parts = [reader.tail.last_poll]
    if hasattr(identity, 'tail'): parts.append(identity.tail.last_poll)
    polls.append(dict(start_s=start, end_s=time.monotonic(),
                      wall_s=time.monotonic()-start, cpu_s=time.thread_time()-cpu,
                      bytes_consumed=sum(p.get('bytes_consumed', 0) for p in parts),
                      rows_consumed=sum(p.get('rows_consumed', 0) for p in parts)))
    backlog = reader.tail.backlog or bool(getattr(getattr(identity, 'tail', None), 'backlog', False))
    return now, owner, backlog


def _timing(value, witness, due, *, window_end=None):
    actual = witness['request_monotonic_s'] if witness else None
    error = (actual-due)*1000 if actual is not None else None
    value.update(jabra=witness, actual_request_s=actual,
                 actual_minus_requested_ms=error, timing_limit_ms=100,
                 timing_passed=bool(error is not None and abs(error) <= 100 and
                                   (window_end is None or actual <= window_end)))
    if actual is not None:
        value['actual_invocation_offset_s'] = actual-value['anchor_s']
    if not value['timing_passed']:
        value['harness_failure'] = 'stimulus-timing'
    return value


def _invoke(value, play, due, clock, cancel, *, require_active=False, window_end=None):
    # Build diagnostics before the final clock sample; no file I/O before callback.
    fired = clock()
    value.update(timer_fired_s=fired, timer_lateness_ms=(fired-due)*1000)
    active = value.get('active_tts_witness')
    if active and active.get('latest'):
        active['age_s'] = fired-active['latest']['monotonic_ns']/1e9
        active['active'] = bool(active['latest'].get('non_silent') is True and
                                0 <= active['age_s'] <= active['max_age_s'])
    if cancel.is_set():
        value.update(skipped=True, reason='cancelled'); return value
    if window_end is not None and fired > window_end:
        value.update(skipped=True, reason='no fresh active TTS in declared window'); return value
    if abs(fired-due) > .1:
        value.update(skipped=True, reason='stimulus invocation deadline missed')
        return _timing(value, None, due)
    if require_active and not active['active']:
        value.update(skipped=True, reason='no active TTS at declared deadline'); return value
    return _timing(value, play(), due, window_end=window_end)


def schedule_from_tts(paths, start_ns, offset_s, play, *, cancel=None, timeout_s=30,
                      require_active=False, active_max_age_s=.12, reader=None,
                      identity=None, clock=time.monotonic):
    if not 0 <= offset_s <= 3:
        raise ValueError('Declared near-end offset must be 0..3 seconds')
    cancel = cancel or threading.Event()
    reader = reader or TransportEvents(paths, start_ns)
    expires = clock()+timeout_s; polls = []; owner = None
    while not cancel.is_set() and clock() < expires:
        now, resolved, backlog = _poll(reader, identity, clock, polls)
        if resolved:
            if owner is not None and resolved != owner: raise EventStreamChanged('Ambiguous requested context')
            owner = resolved
        state = reader.state(owner)
        anchor = state['anchor'] if not identity or owner else None
        if anchor is not None and now >= anchor+offset_s and not backlog:
            break
        cancel.wait(.005)
    else:
        raise RuntimeError('No non-silent transport anchor before cancellation/deadline')
    due = anchor+offset_s
    # No file reads or historical scans between this clock and invocation.
    fired = clock()
    latest = state['latest']; age = fired-latest['monotonic_ns']/1e9
    active = dict(active=bool(latest.get('non_silent') is True and 0 <= age <= active_max_age_s),
                  latest=latest, age_s=age, max_age_s=active_max_age_s)
    value = dict(anchor_s=anchor, requested_offset_s=offset_s, requested_s=due,
                 deadline_s=due, due_s=due, timer_fired_s=fired, timer_lateness_ms=(fired-due)*1000,
                 active_tts_witness=active if require_active else None, polls=polls, skipped=False)
    return _invoke(value, play, due, clock, cancel, require_active=require_active)


def schedule_active_window(paths, start_ns, play, identity, *, cancel=None,
                           timeout_s=18, window_s=(1.0,1.5), max_age_s=.12,
                           clock=time.monotonic, reader=None):
    if tuple(window_s) != (1.0,1.5) or max_age_s != .12:
        raise ValueError('Unreviewed active-speech window')
    cancel = cancel or threading.Event(); expires = clock()+timeout_s
    reader = reader or TransportEvents(paths, start_ns)
    anchor = None; owner = None; latest = None; polls = []
    def result(skipped, reason, now):
        return dict(skipped=skipped, reason=reason, identity=owner, anchor_s=anchor,
                    window_s=list(window_s), deadline_s=anchor+window_s[1] if anchor is not None else None,
                    timer_fired_s=now, polls=polls, active_tts_witness=dict(active=not skipped,
                    latest=latest, age_s=now-latest['monotonic_ns']/1e9 if latest else None, max_age_s=max_age_s))
    while not cancel.is_set() and clock() < expires:
        try:
            now, resolved, backlog = _poll(reader, identity, clock, polls)
        except (StimulusPreconditionFailure, EventStreamChanged) as exc:
            return result(True, str(exc), clock())
        if resolved:
            if owner is not None and resolved != owner: return result(True, 'ambiguous requested context', now)
            owner = resolved
        if owner:
            state = reader.state(owner); anchor = state['anchor']; latest = state['latest']
        fired = clock()
        if anchor is not None:
            if fired > anchor+window_s[1]: return result(True, 'no fresh active TTS in declared window', fired)
            if (not backlog and not cancel.is_set() and latest and latest.get('non_silent') is True
                    and latest['monotonic_ns']/1e9 >= anchor+window_s[0]
                    and 0 <= fired-latest['monotonic_ns']/1e9 <= max_age_s):
                # Requested instant is the qualifying write within the declared window,
                # not +1.0 when speech legitimately resumes later in that window.
                due = latest['monotonic_ns']/1e9
                value = result(False, None, fired)
                value.update(requested_offset_s=due-anchor, requested_s=due, due_s=due,
                             timer_lateness_ms=(fired-due)*1000)
                return _invoke(value, play, due, clock, cancel, require_active=True,
                               window_end=anchor+window_s[1])
        cancel.wait(.005)
    return result(True, 'cancelled' if cancel.is_set() else 'no requested non-silent anchor before deadline', clock())


def set_guard_if_changed(board, command, current_ms, requested_ms):
    """Guard writes need capture disabled; unchanged settings must not reset PCM.

    The console's audio command always signals a framer discontinuity, including
    audio 1 when already enabled. Never issue it just to restate a fixed guard.
    """
    if type(requested_ms) is not int or not 0<=requested_ms<=1000:
        raise ValueError('Guard must be integer 0..1000 ms')
    if requested_ms != current_ms:
        command(board,'audio 0')
        command(board,f'aec-guard-ms {requested_ms}')
        command(board,'audio 1')
    return requested_ms
