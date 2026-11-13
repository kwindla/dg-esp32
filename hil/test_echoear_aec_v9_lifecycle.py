"""v9 host lifecycle test (freeze.lifecycle_test): the frozen schedule and the smoke schedule through
the real hold classes (V9Hold -> NovaHold -> EchoHold -> BenchHold: real __enter__/__exit__,
baseline, restore, mixer and lock paths) with fake board, Jabra, bot, serial, NIC and lock
boundaries, the real decision-71 drain boundary, the real ledger and rules, and fault injection.

The per-attempt acoustic scorer is replaced by a scenario table (its real implementation is
exercised on retained evidence by the analyzer dry run); admission findings come from the real
scorer function over the fake PERF boundaries. No hardware, network or bench lock is touched.
"""
import copy
import json
import types
from pathlib import Path

import numpy as np
import pytest

import echoear_aec_v9 as v9
import echoear_aec_v9_plan as plan_mod
import echoear_aec_v9_rules as rules
from echoear_aec_v9_ledger import Ledger, verdict, verify
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: v9 holds (campaign BenchHold) restore 9936da45

pytestmark = pytest.mark.skipif(not plan_mod.SOURCE.exists(), reason='frozen v8.7 plan not on this host')
REVIEWED = 'f'*64
EVIDENCE = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')


# The terminal task census and display fields of the retained v9.8-smoke-01/part-1/disconnected.json (subset).
TERMINAL_TASKS = [dict(name='app-control', stack_free_bytes=8884, stack_bytes=16384), dict(name='app-session', stack_free_bytes=8848, stack_bytes=16384),
                  dict(name='audio-playback', stack_free_bytes=4520, stack_bytes=17152), dict(name='audio-capture', stack_free_bytes=11568, stack_bytes=16384),
                  dict(name='audio-tx', stack_free_bytes=4524, stack_bytes=26624), dict(name='peer-rx', stack_free_bytes=1652, stack_bytes=4096),
                  dict(name='peer-loop', stack_free_bytes=5796, stack_bytes=12800), dict(name='IDLE0', stack_free_bytes=688, stack_bytes=1536),
                  dict(name='IDLE1', stack_free_bytes=696, stack_bytes=1536)]
TERMINAL_DISPLAY = dict(animation=False, dead=False, display_gate_timeouts=0, dma_errors=0, failed=False, gate_timeouts=0,
                        lock_failures=0, lock_timeouts=0, send_failures=0, rendered=725)


class Parked(Exception):
    """Raised by the fake sleep when BenchHold parks with the lock after a failed restore."""


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class FakeBudget:
    def __init__(self, clock, contract):
        self.clock, self.contract, self.total_expired = clock, contract, False
    def __enter__(self): self.started = self.clock(); return self
    def __exit__(self, *a): return False
    def remaining(self): return self.contract['active_s']-(self.clock()-self.started)
    def cleanup(self): pass


class Board:
    def __init__(self, env): self.env, self.lines, self.closed = env, [], False
    @property
    def cursor(self): return len(self.lines)
    def lines_since(self, cursor): return [(0, 0, line) for line in self.lines[cursor:]]
    def wait_for(self, *a, **k): return 0
    def close(self): self.closed = True


class Env:
    """Fake bench boundaries and the injected faults for one test."""
    def __init__(self, tmp, monkeypatch, *, scenario=None, clock=None, stress=False, census=False):
        self.tmp, self.mp, self.clock = Path(tmp), monkeypatch, clock or Clock()
        self.scenario = scenario or {}
        self.stress, self.census = stress, census
        self.perf = dict(pc_id='pc-1', generation=1, received=100, dispatched=100, deferred=0, overflow=0, rtp=1000,
                         output_clips=0, reference_clips=0, adc_clips=0, read_errors=0, send_errors=0)
        self.restore_delay_s = 0
        self.slow_drain_s, self.slow_drain_at, self.drain_until = 0, (), None
        self.restore_fails = False
        self.flashes = []
        self.lock_events = []
        self.attempts = []
        self._patch()

    # Full-schema PERF snapshots (admission, delivery, transport, render ledger, clips, reference health),
    # so the real integrity classification runs on them.
    def snapshot(self, board, folder, label):
        if label == 'display-init' and getattr(self, 'display_init', None) is not None:   # a real-schema pre-connect snapshot
            Path(folder).mkdir(parents=True, exist_ok=True)
            (Path(folder)/f'{label}.json').write_text(json.dumps(self.display_init)); self.clock.advance(.1)
            return self.display_init
        p = self.perf
        if self.drain_until is not None and self.clock() >= self.drain_until:
            p['dispatched'] = p['received']; self.drain_until = None
        p['rtp'] += 50
        drops = {k: 0 for k in rules.DROP_REASONS}; drops['admission_overflow'] = p['overflow']; drops['send_cutoff'] = p.get('send_cutoff', 0)
        value = dict(pc_id=p['pc_id'], generation=p['generation'], device_us=int(self.clock()*1e6),
                     data_admission=dict(received=p['received'], dispatched=p['dispatched'], deferred=p['deferred'],
                                         deferred_depth=0, deferred_max=0, capacity=64),
                     drop_reasons=drops, console_output=dict(full=0, transport_drops=p.get('transport_drops', 0)),
                     heap=dict(psram=dict(free=4000000, largest=3000000), internal=dict(minimum_free=90000, largest=45056)),
                     send_errors=p['send_errors'], i2s_underflows=0, i2s_write_errors=p.get('i2s_write_errors', 0),
                     i2s_write_timeouts=0, pcm_queue_drops=0, failed_allocations=dict(total=0),
                     audio=dict(drops=0, read_errors=0, write_errors=0, write_timeouts=0),
                     send_budget_cutoffs=p.get('send_budget_cutoffs', 0), received=p['rtp'], lost=0, playout_dropped=0, post_plc_zeros=0,
                     playout=dict(missing_run_ended=[0]*6, post_plc_zeros=0, starvation=0,
                                  clock=dict(completed_samples=3200, submitted_samples=3840)),
                     aec=dict(errors=p.get('aec_errors', 0), output_clips=p['output_clips'], reference_clips=p['reference_clips'],
                              reference_health=dict(low_windows=0),
                              capture=dict(rx_overflows=p.get('rx_overflows', 0), adc_clips=p['adc_clips'], gain_clips=0,
                                           read_errors=p['read_errors'], read_timeouts=0)))
        if label == 'disconnected':      # real-schema terminal census and display fields (v9.10 terminal inventory)
            tasks = getattr(self, 'terminal_tasks', None) or copy.deepcopy(TERMINAL_TASKS)
            value.update(tasks=tasks, display=dict(TERMINAL_DISPLAY, **getattr(self, 'terminal_display', {})),
                         send_budget_cutoffs=p.get('send_budget_cutoffs', 0))
        elif getattr(self, 'census', False):   # v9.11 peer-loss cases: every snapshot carries the real-schema census
            tasks = [dict(t, window_us=6000000) if t['name'].startswith('IDLE') else dict(t) for t in TERMINAL_TASKS]
            value.update(tasks=tasks, display=dict(TERMINAL_DISPLAY), uplink_frame_ms=20, tasks_exited=False,
                         encode_frames=50, encode_p95_us=6000, encode_max_us=9000, per_core_idle_percent=[60, 60])
        if label == 'settled-end' and getattr(self, 'peer_lost', False) and getattr(self, 'settled_override', None):
            self.settled_override(value)            # v9.12: an observation of the reconnected generation
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder)/f'{label}.json').write_text(json.dumps(value))
        self.clock.advance(.1)
        return value

    def _patch(self):
        import echoear_aec_campaign as C
        import echoear_aec_resource_trial as RT
        import echoear_aec_host_gate as HG
        env = self
        class Lock:
            def __enter__(s): env.lock_events.append('acquire'); return s
            def __exit__(s, *a): env.lock_events.append('release')
        class NIC:
            @staticmethod
            def snapshot(): return dict(monotonic_s=env.clock(), interfaces={})
            @staticmethod
            def gate(a, b): return dict(passed=True, before=a, after=b)
        class Gate:
            failed = False
            def __init__(s, hold): s.hold = hold
            def start(s): pass
            def stop(s): pass
            def boundary(s, *a, **k): pass
            def periodicity(s): pass
        class Sub:
            @staticmethod
            def run(args, **k):
                if args[0] == 'alsactl':
                    Path(args[2]).write_bytes(b'mixer-state\n') if args[3] == 'store' else None
                return types.SimpleNamespace(returncode=0, stdout='', stderr='')
            check_output = staticmethod(__import__('subprocess').check_output)
        def sleep(s):
            if s == 30 and env.flashes and env.flashes[-1][0] == 'restore' and env.restore_fails: raise Parked('parked with lock')
        def flash(image, expected, out, label, gain=None):
            env.flashes.append((label, expected))
            if label == 'restore': env.clock.advance(env.restore_delay_s)
            if label == 'restore' and env.restore_fails: raise RuntimeError('restore flash failed')
        def dump(board, path, timeout=150):
            rng = np.random.default_rng(1)
            loud = env.scenario.get(('loud', getattr(env, '_part', None))) and 'baseline' in str(path)
            n = 97600; pcm = (rng.normal(0, 3000 if loud else 3, (n, 3))).astype('<i2')
            timing = np.array([[i*320, (i+1)*320, i*20000, i*20000+20000, 0] for i in range(n//320)])
            np.savez(path, pcm=pcm, rate=16000, timing=timing)
        self.mp.setattr(C, 'bench_lock', lambda: Lock())
        self.mp.setattr(C, 'host_nic', NIC)
        self.mp.setattr(C, 'resolve_jabra', lambda: (0, 'hw:0'))
        self.mp.setattr(C, 'jabra_card_zero', lambda: None)
        self.mp.setattr(C, 'subprocess', Sub)
        self.mp.setattr(C, 'set_volumes', lambda *a, **k: None)
        self.mp.setattr(C, 'flash', flash)
        self.mp.setattr(C, 'boot', lambda out, label, expected: Board(env))
        self.mp.setattr(C, 'command', lambda *a, **k: (0, 1))
        self.mp.setattr(C, 'snapshot', lambda board, folder, label: env.snapshot(board, folder, label))
        self.mp.setattr(C, 'request', lambda *a, **k: None)
        self.mp.setattr(C, 'terminal', lambda *a, **k: dict(error='none'))
        def read(board, op, tag=None, *a, **k):         # DISPLAY status / character status reads
            if tag == 'DISPLAY':
                return dict(dead=False, failed=False, send_failures=0, dma=[0, 0], errors=[0, 0, 0], gate_timeouts=0, display_gate_timeouts=0)
            env._posts = getattr(env, '_posts', 0)+1
            return dict(enabled=True, posts=env._posts, errors=0)
        self.mp.setattr(C, 'read', read)
        self.mp.setattr(C, 'dump_capture', dump)
        self.mp.setattr(C, 'clock_anchor', lambda board: dict(lower_s=env.clock(), upper_s=env.clock(), coordinate=0))
        self.mp.setattr(C, 'time', types.SimpleNamespace(sleep=sleep, monotonic=env.clock, monotonic_ns=lambda: int(env.clock()*1e9)))
        self.mp.setattr(RT, 'snapshot', lambda board, folder, label: env.snapshot(board, folder, label))
        self.mp.setattr(HG, 'HostGate', Gate)
        self.mp.setattr(v9.time, 'sleep', lambda s: None)

    # -- the hold: real classes, fake bot/capture boundary --
    def hold_factory(self, budget):
        env = self
        base = v9.hold_type(budget)
        class FakeHold(base):
            def connect(self, cells, reconnects):
                self.evaluation = self.out/'evaluation'; self.evaluation.mkdir(exist_ok=True)
                self.bot = types.SimpleNamespace(stop=lambda: None)
                owner = getattr(env, 'owner_pc', 'pc-1')
                self._events = [dict(event='evaluation-start', pc_id=owner, t=env.clock())]
                (self.evaluation/'session-owner.json').write_text(json.dumps(dict(pid=1, pc_id=owner)))
                with (self.out/'candidate-serial.log').open('a') as w:
                    w.write('I (35808) dg-app: STATE Connected gen=1 desired=1 t=34464248\n')
                env.snapshot(self.board, self.out, 'connected')
            def link_probe(self):
                env.clock.advance(env.setup_s-env.clock_setup_spent())
                gate = env.scenario.get(('link', env.part()))
                if gate:                                          # as the real link probe: LinkProbeFailed
                    from echoear_aec_campaign import LinkProbeFailed
                    raise LinkProbeFailed('LINK PROBE FAILED, retry later')
            def events(self, case_id=None): return list(self._events)
            def recorded(self): return []
            def send(self, op, cid, *, timeout_s=10, **kw):
                self.number += 1
                if op == 'burst':
                    k = kw['burst']
                    # the bot and the runner share the host monotonic clock (the fake clock here)
                    self._events.append(dict(event='burst-sent', number=self.number, burst=k, t=env.clock(),
                                             first_send_s=env.clock(), last_send_s=env.clock()))
                    ids = env.scenario.get(('burst-ids', k), list(range(40)))
                    log = env.scenario.get(('part-log-ids', k), ids)
                    stamp = env.scenario.get('stamps', 'small')
                    def prefix(i):
                        if stamp == 'none': return 'dg-app: '
                        if stamp == 'offset': return f'I ({int(env.clock()*1000)+1344+i}) dg-app: '   # app_log ms = esp_timer + 1.344 s
                        return f'I ({1000+i}) dg-app: '
                    for i in ids: self.board.lines.append(prefix(i)+'SERVER '+json.dumps({'d': {'b': k, 'id': i}, 't': 'burst'}))
                    with (self.out/'candidate-serial.log').open('a') as w:
                        for i in log: w.write(prefix(i)+'SERVER '+json.dumps({'d': {'b': k, 'id': i}, 't': 'burst'})+'\n')
                    env.perf['received'] += 40; env.perf['dispatched'] += 40
                    import hashlib
                    for tag, dt in (('burst-pre-ping', -.5), ('burst-inflight', .05)):
                        if env.scenario.get((tag, k)):
                            m = dict(type='server-message', data=dict(ping=tag, b=k)); h = hashlib.sha256(json.dumps(m).encode()).hexdigest()
                            t = env.clock()+dt
                            sess = self.out/'sessions/s1'; sess.mkdir(parents=True, exist_ok=True)
                            with (sess/'events.jsonl').open('a') as w:
                                w.write(json.dumps(dict(kind='rtvi-output', source='RTVIProcessor', direction='DOWNSTREAM',
                                                        monotonic_ns=int(t*1e9), message=m))+'\n')
                            if tag == 'burst-pre-ping':     # sent, acked and already inside the device baseline
                                rtp = self.out/'rtp'; rtp.mkdir(exist_ok=True)
                                with (rtp/'sctp.jsonl').open('a') as w:
                                    for row in (dict(event='send-app-message', message_sha256=h, message_bytes=len(json.dumps(m)), monotonic_ns=int((t+.01)*1e9)),
                                                dict(event='send-chunk', chunk_type='DataChunk', protocol=51, tsn=900+k, flags=3, user_data_sha256=h, monotonic_ns=int((t+.02)*1e9)),
                                                dict(event='receive-chunk', chunk_type='SackChunk', cumulative_tsn=900+k, monotonic_ns=int((t+.05)*1e9))):
                                        w.write(json.dumps(dict(row, pc_id='pc-1'))+'\n')
                    env.perf['deferred'] += env.scenario.get(('burst-deferred', k), 12)
                    env.perf['overflow'] += env.scenario.get(('burst-overflow', k), 0)
            def capture(self, group, attempt, **kw):
                import echoear_aec_campaign as C
                c = group['cells'][0]
                folder = self.out/f"{group['run_id']}-a{attempt}"; folder.mkdir()
                env.attempts.append((env.part(), c['id'], attempt))
                if getattr(env, 'inject_at', None) == len(env.attempts):
                    if getattr(env, 'inject_delayed', False):      # mid-capture: the capture time is spent first
                        kind = env.kind_of(c['id'])
                        env.clock.advance(plan_mod.WINDOWS_S[kind][1 if env.stress else 0]/2)
                    raise RuntimeError('injected N-1 pause')          # unknown.unclassified -> PAUSE-PART
                fault = env.scenario.get((c['id'], attempt))
                if isinstance(fault, BaseException): raise fault
                self.attempt_started_ns = int(env.clock()*1e9)
                C.boundary(self.board, folder, 'before')
                if fault == 'peer-loss':
                    # v9.10-acceptance-01 part 1 shape: link outage mid-acquisition; the bot logs liveness-alarm,
                    # client-disconnected and evaluation-end for the epoch; the device logs SESSION FAIL and
                    # reconnects (new pc_id/generation, refused by the bot); the next control is unacknowledged.
                    env.clock.advance(20)
                    env.perf['send_cutoff'] = 240          # stale uplink audio dropped while the link stalls
                    env.perf['send_budget_cutoffs'] = env.perf.get('send_budget_cutoffs', 0)+139
                    for k, v in getattr(env, 'loss_capture_end', {}).items(): env.perf[k] = env.perf.get(k, 0)+v
                    env.snapshot(self.board, folder, 'capture-end')
                    t = env.clock()
                    bot = [dict(event='liveness-alarm', pc_id='pc-1', t=t, silent_s=2.46),
                           dict(event='client-disconnected', pc_id='pc-1', t=t+.01),
                           dict(event='evaluation-end', pc_id='pc-1', t=t+.08)]
                    witness = getattr(env, 'loss_witness', 'both')
                    if witness in ('both', 'bot'): self._events += bot
                    if witness == 'evaluation-end-only': self._events += bot[2:]
                    with (self.out/'candidate-serial.log').open('a') as w:
                        if witness in ('both', 'device'):
                            w.write('I (1782809) dg-app: SESSION FAIL gen=1 reason=remote disconnected retry_ms=1000\n')
                        if witness == 'spoof':
                            w.write('I (9) dg-app: SERVER {"text":"SESSION FAIL gen=1 reason=remote disconnected"}\n')
                        w.write('I (1782809) dg-app: STATE Disconnected gen=2 desired=1 t=1781465062\n')
                    if getattr(env, 'reconnect', True):    # a new session: its admission counters start balanced
                        env.perf.update(pc_id='pc-2', generation=3); env.perf['dispatched'] = env.perf['received']
                    env.peer_lost = True
                    from echoear_aec_campaign import HarnessFailure
                    raise HarnessFailure('evaluation-ack-timeout', 'Evaluation command not acknowledged')

                t0 = env.clock()
                kind = env.kind_of(c['id'])
                windows = plan_mod.WINDOWS_S[kind]
                env.clock.advance(windows[1 if env.stress else 0]+plan_mod.SCORING_S)
                if fault == 'overflow' or fault == 'composite': env.perf['overflow'] += 1
                if fault == 'send-cutoff': env.perf['send_cutoff'] = env.perf.get('send_cutoff', 0)+5
                if fault == 'output_clip': env.perf['output_clips'] += 1
                if fault == 'reference_clip': env.perf['reference_clips'] += 1
                if fault == 'drain': env.perf['received'] += 3
                if fault == 'drain-recovers': env.perf['received'] += 3; env.perf['dispatched'] += 3
                if fault == 'slow-drain' or (env.slow_drain_s and len(env.attempts) in env.slow_drain_at):
                    env.perf['received'] += 3; env.drain_until = env.clock()+env.slow_drain_s   # backlog at the 'after' boundary
                run_id = f"{c['id']}-a{attempt}"
                jabra = dict(request_monotonic_s=t0+3, elapsed_seconds=2.0, expected_seconds=2.0) if kind != 'echo' else None
                C.dump_capture(self.board, folder/'capture.npz')
                (folder/'anchors.json').write_text(json.dumps([dict(coordinate=0, lower_s=t0, upper_s=t0),
                                                               dict(coordinate=10**12, lower_s=env.clock(), upper_s=env.clock())]))
                if fault in ('late-seal', 'late-seal-aec-error'):   # decision 92: the seal ack fails after a complete capture
                    if fault == 'late-seal-aec-error': env.perf['aec_errors'] = env.perf.get('aec_errors', 0)+1
                    env.snapshot(self.board, folder, 'capture-end')    # as the real capture's finally (v85)
                    (folder/f'timer-{run_id}.json').write_text(json.dumps(dict(jabra=jabra, timer_lateness_ms=1.0)))
                    from echoear_aec_campaign import HarnessFailure
                    raise HarnessFailure('evaluation-ack-timeout', 'Evaluation command not acknowledged')
                if fault == 'deadline':
                    (folder/f'timer-{run_id}.json').write_text(json.dumps(dict(jabra=jabra, timer_lateness_ms=1.0)))
                    from echoear_aec_campaign import HarnessFailure
                    raise HarnessFailure('capture-deadline', 'Capture deadline; incomplete reply/post-roll retained')
                env.snapshot(self.board, folder, 'capture-end')
                C.boundary(self.board, folder, 'after')
                noise = env.scenario.get(('score', c['id'], attempt)) != 'noisy'
                (folder/'noise-gate.json').write_text(json.dumps(dict(pre=dict(passed=noise), post=dict(passed=True), board_silent=True)))
                (folder/'capture-duration.json').write_text(json.dumps(dict(elapsed_s=20, limit_s=28)))
                (folder/'rf.json').write_text(json.dumps(dict(passed=True)))
                health = dict(event='rtp-observer-health', t=env.clock(), passed=True,
                              sctp=dict(supported=True, flight_fix_enabled=True, records=5, truncated=False, unavailable=False))
                case = dict(c, run_id=run_id, attempt=attempt, start_ns=int(t0*1e9), end_ns=int(env.clock()*1e9), events=[health],
                            noise_gate_passed=noise, quiet_windows_valid=True, jabra=jabra, stimulus_timer=dict(timer_lateness_ms=1.0),
                            resources=dict(all_passed=True, checks={}))
                (folder/'result.json').write_text(json.dumps(dict(cases=[case], checks={})))
                if fault in ('recorder-short', 'recorder-short-acked'):
                    import time as real
                    import hashlib
                    sess = self.out/'sessions/s1'; sess.mkdir(parents=True, exist_ok=True)
                    msgs = [dict(type='server-message', data=dict(n=k, run=run_id)) for k in range(2)]
                    with (sess/'events.jsonl').open('a') as w:
                        for k, m in enumerate(msgs):
                            w.write(json.dumps(dict(kind='rtvi-output', source='RTVIProcessor', direction='DOWNSTREAM',
                                                    monotonic_ns=int((t0+1+k)*1e9), message=m))+'\n')
                    if fault == 'recorder-short-acked':     # every recorder message fully sent and acknowledged
                        rtp = self.out/'rtp'; rtp.mkdir(exist_ok=True)
                        with (rtp/'sctp.jsonl').open('a') as w:
                            for k, m in enumerate(msgs):
                                h = hashlib.sha256(json.dumps(m).encode()).hexdigest(); t = t0+1+k
                                w.write(json.dumps(dict(pc_id='pc-1', event='send-app-message', message_sha256=h,
                                                        message_bytes=len(json.dumps(m)), monotonic_ns=int((t+.01)*1e9)))+'\n')
                                w.write(json.dumps(dict(pc_id='pc-1', event='send-chunk', chunk_type='DataChunk', protocol=51,
                                                        tsn=7+k, flags=3, user_data_sha256=h, monotonic_ns=int((t+.02)*1e9)))+'\n')
                                w.write(json.dumps(dict(pc_id='pc-1', event='receive-chunk', chunk_type='SackChunk',
                                                        cumulative_tsn=7+k, monotonic_ns=int((t+.05)*1e9)))+'\n')
                if fault == 'composite': raise RuntimeError('Capture-interval or integrity failure; v8.5 has no link retake')
                return True, [case]
            def recover_harness(self, group, attempt, exc):
                if getattr(env, 'peer_lost', False):           # as BenchHold.settle_attempt with the bot gone
                    raise RuntimeError('Evaluation control remains unacknowledged')
                failure = env.scenario.get(('recover', group['run_id']))
                if failure:
                    folder = self.out/f"{group['run_id']}-a{attempt}"
                    (folder/'acquisition-failure.json').write_text(json.dumps(failure if isinstance(failure, dict) else {}))
                    raise RuntimeError('Device counters/resources not clean; harness retry forbidden')
                return dict(retry_safe=True)
            def settle_attempt(self): pass
            def finish(self):
                import echoear_aec_campaign as C
                if v9.peer_loss_witness(self):
                    return base.finish(self)                   # v9.11: the real peer-lost final audit
                if not hasattr(self, 'evaluation'):             # as EchoHold.finish: setup never reached connect
                    self.result.update(completed=False, final_audit=dict(status='not-started')); self.save(); return
                final = env.scenario.get(('final', env.part()), {})
                late = env.scenario.get('late-dup')
                if late:
                    with (self.out/'candidate-serial.log').open('a') as w:
                        w.write('I (9) dg-app: SERVER '+json.dumps({'d': {'b': late, 'id': 3}, 't': 'burst'})+'\n')
                for k, v in final.get('perf', {}).items(): env.perf[k] += v
                C.boundary(self.board, self.out, 'settled-end')
                checks = dict(internal_minimum_free=dict(passed=env.scenario.get(('heap', env.part())) is None, value=90000))
                checks['aec.capture.read_errors'] = dict(passed=env.perf['read_errors'] == 0, value=env.perf['read_errors'])
                self.result.update(resources=dict(all_passed=all(c['passed'] for c in checks.values()), checks=checks),
                                   active_end_ns=0)
                if self.result.get('error_class'):            # as BenchHold.finish after an exception
                    self.result['exception_integrity_audit'] = dict(all_passed=True)
                self.save()
                if env.scenario.get(('finish-raise', env.part())):   # Astra v9.6 3: fails after resources are populated
                    raise TimeoutError('Serial timeout waiting for disconnect')
                if env.scenario.get(('late-i2s', env.part())):      # Astra v9.7 2: positive fault at disconnect
                    env.perf['i2s_write_errors'] = 1
                    from echoear_aec_accept import require_zero_i2s
                    require_zero_i2s([env.snapshot(self.board, self.out, 'disconnected')])
                for k, v in env.scenario.get(('late', env.part()), {}).items(): env.perf[k] = env.perf.get(k, 0)+v   # after settled-end
                env.snapshot(self.board, self.out, 'disconnected')
                self.result.update(sctp_recovery=dict(passed=True),
                                   completed=final.get('completed', all(c['passed'] for c in checks.values())))
                self.bot = None; self.save()
        return FakeHold

    setup_s = 330
    def clock_setup_spent(self): return 0
    def part(self): return self._part
    def kind_of(self, cid):
        if cid.startswith('v9-witness'): return 'near-witness'
        return plan_mod.cell_index(self.p)[cid]['kind']

    def acoustic(self, view, plan, entry, a):
        """Scripted acoustic stage (the integrity stage before it is the real one)."""
        lid = entry['logical_id']; attempt = int(Path(a['folder']).name.rsplit('-a', 1)[1])
        spec = self.scenario.get(('score', lid, attempt), 'pass')
        if spec == 'raise': raise ValueError('injected analyzer exception')
        record = dict(mapping_intact=spec != 'broken_map')
        if entry['kind'] == 'near-witness':
            if self.scenario.get(('witness-invalid', lid)):   # v9.7 ambiguous witness: offset None
                record['witness'] = dict(valid=False, offset=None, method='ambiguous')
            else: record['witness'] = dict(valid=True, offset=self.scenario.get(('offset', lid), 1024))
            return {}, ([dict(gate='hardware.aec_errors')] if spec == 'fault' else []), record
        if a['partial']:
            return (dict(acoustic=dict(result='miss', established=True)) if spec == 'established' else {}), [], dict(partial=True)
        extra = dict(miss=[], fault=[dict(gate='hardware.aec_errors')],
                     false=[dict(gate='echo.false_turns', events=[dict(id='e1', t=1.0)])],
                     room_clip=[dict(gate='clip.mic_room')], unattributed=[dict(gate='clip.mic_unattributed')],
                     observer=[dict(gate='observer.health')], in_window=[dict(gate='barge.in_window')],
                     coverage=[dict(gate='echo.event_coverage')], hard_floor=[dict(gate='echo.hard_floor')],
                     undelivered=[dict(gate='barge.undelivered')], ineligible=[]).get(spec, [])
        result = 'miss' if spec in ('miss', 'hard_floor') else 'ineligible' if spec == 'ineligible' else 'pass'
        events = 'miss' if spec == 'false' else 'pass'
        return dict(acoustic=dict(result=result), events=dict(result=events)), extra, record

    def run_part(self, out, ledger, part, *, mode):
        self._part = part
        allowance = getattr(self, 'allowance_s', None) or (2.0 if mode == 'smoke' else 1.0)
        return v9.run_part(self.p, out, ledger, part, mode=mode, allowance_s=allowance,
                           hold_factory=self.hold_factory, budget_factory=lambda contract: FakeBudget(self.clock, contract),
                           acoustic=self.acoustic, clock=self.clock, sleep=lambda s: self.clock.advance(s))


@pytest.fixture(scope='module')
def plan():
    p = plan_mod.plan_from()
    return p


def campaign(tmp_path, monkeypatch, plan, *, mode='acceptance', parts=3, **kw):
    env = Env(tmp_path, monkeypatch, **kw); env.p = plan
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode=mode, plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0,
                       drain_basis={}, credit=mode == 'acceptance'))
    for part in range(1, parts+1):
        if ledger.state.closed: break
        if part > 1:
            # Resume preconditions as in v9.resume: previous part restored, no pending, evidence unchanged.
            ends = [r for r in ledger.rows if r['type'] == 'part-end']
            if not ends[-1]['restore']['clean'] or ledger.state.pending: break
            for r in ends: assert v9.files_manifest(out/f"part-{r['part']}") == r['files_sha256']
            ledger = Ledger(out/'ledger.jsonl')         # fresh process: re-read and verify the chain
        env.run_part(out, ledger, part, mode=mode)
    return env, out, ledger


def ends(ledger): return [r for r in ledger.rows if r['type'] == 'part-end']
def attempts(ledger, lid=None): return [r for r in ledger.rows if r['type'] == 'attempt' and (lid is None or r['logical_id'] == lid)]


def assert_restored(env, ledger):
    for r in ends(ledger):
        assert r['restore'] == dict(restored_elf_sha256=NORMAL_HASH, mixer_equal=True, lock_released=True, touched=True,
                                    cleanup_errors=[], completed=r['restore']['completed'], clean=True)
    assert env.lock_events.count('acquire') == env.lock_events.count('release') == len(ends(ledger))
    assert [f for f in env.flashes if f[0] == 'restore'] == [('restore', NORMAL_HASH)]*len(ends(ledger))


# ---- full frozen schedule ---------------------------------------------------------------------------

def test_full_schedule_two_parts_pass(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan)
    verify(ledger.rows)
    assert [r['reason'] for r in ends(ledger)] == ['complete', 'complete']
    assert ledger.state.closed['reason'] == 'PASS'
    v = verdict(ledger.state, plan)
    assert (v['outcome'], v['acoustic'], v['integration'], v['completeness']['complete']) == ('PASS', 'PASS', 'PASS', True)
    scored = [r for r in attempts(ledger) if r['kind'] != 'near-witness']
    assert len(scored) == 76 and len({r['logical_id'] for r in scored}) == 76
    assert len([r for r in attempts(ledger) if r['kind'] == 'near-witness']) == 18          # 9 per part
    assert [len([r for r in scored if r['part'] == p]) for p in (1, 2)] == [38, 38]
    gens = {part: {plan_mod.cell_index(plan)[r['logical_id']]['generation'] for r in scored if r['kind'] == 'echo' and r['part'] == part}
            for part in (1, 2)}
    assert gens == {1: {1}, 2: {2}}
    closes = [r for r in ledger.rows if r['type'] == 'block-close']
    assert len(closes) == 16 and all(r['closed'] for r in closes)
    starts = [r for r in ledger.rows if r['type'] == 'part-start']
    assert [s['budget']['total_s'] for s in starts] == [3600, 3600]
    assert_restored(env, ledger)


def test_stress_spill_goes_to_part_two_and_recovery(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, stress=True)
    notes = [r.get('note') for r in ledger.rows if r['type'] == 'drain' and r.get('note')]
    assert any('not admitted' in n for n in notes)              # whole-block admission refused the last block
    assert len(ends(ledger)) == 3 and ledger.state.closed['reason'] == 'PASS'
    starts = [r for r in ledger.rows if r['type'] == 'part-start']
    used = sum(e['wall_s'] for e in ends(ledger)[:2])
    assert starts[2]['role'] == 'recovery' and starts[2]['budget']['total_s'] == min(9000-used, 7800)   # v9.6 ceiling left, v9.8 RTP bound
    assert sum(e['wall_s'] for e in ends(ledger)) <= 9000
    # No scored attempt was acquired and lost at a witness boundary; every block closed.
    assert all(r['closed'] for r in ledger.rows if r['type'] == 'block-close')


# ---- invalid, re-attempt, scored misses -------------------------------------------------------------

def test_invalid_cell_is_reattempted_and_valid_miss_is_not(tmp_path, monkeypatch, plan):
    p1 = plan['v9']['schedule'][0]['blocks']
    noisy, missed = p1[0]['attempts'][1], p1[0]['attempts'][2]
    scenario = {('score', noisy, 1): 'noisy', ('score', missed, 1): 'miss'}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario=scenario)
    assert [r['attempt'] for r in attempts(ledger, noisy)] == [1, 2]
    assert attempts(ledger, noisy)[1]['part'] == 1 and attempts(ledger, noisy)[1]['block_key'].startswith('p1/x')
    assert [r['attempt'] for r in attempts(ledger, missed)] == [1]          # a scored miss is never a spare
    assert ledger.state.cell(missed) == 'miss' and ledger.state.invalid == 1
    assert ledger.state.closed['reason'] == 'PASS'                           # one miss is within the allowance


@pytest.mark.parametrize('spec', ['noisy', 'room_clip', 'observer', 'in_window', 'coverage', 'undelivered'])
def test_invalid_cell_kinds_retake(tmp_path, monkeypatch, plan, spec):
    cell = 'v9-stop-firm-r01' if spec in ('in_window', 'undelivered') else plan['v9']['schedule'][0]['blocks'][0]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', cell, 1): spec}, parts=1)
    assert [r['attempt'] for r in attempts(ledger, cell)] == [1, 2]
    assert ledger.state.invalid == 1 and ends(ledger)[0]['reason'] == 'complete'


def test_ineligible_barge_has_no_refill(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', 'v9-stop-firm-r02', 1): 'ineligible'})
    assert [r['attempt'] for r in attempts(ledger, 'v9-stop-firm-r02')] == [1]
    assert verdict(ledger.state, plan)['barge']['barge-firm-stop']['states'][1] == 'ineligible'
    assert ledger.state.closed['reason'] == 'PASS'         # ranks 1 and 3-11 form the final ten


def test_three_consecutive_invalid_pause_and_caps_survive_resume(tmp_path, monkeypatch, plan):
    # Late in part 1, so the remaining work still fits part 2 + recovery (an early pause is INCONCLUSIVE
    # by the capacity rule; see test_early_pause_is_inconclusive_by_capacity).
    b = plan['v9']['schedule'][0]['blocks'][6]['attempts']
    scenario = {('score', b[0], 1): 'noisy', ('score', b[1], 1): 'noisy', ('score', b[2], 1): 'noisy'}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario=scenario, parts=3)
    first = ends(ledger)[0]
    assert first['reason'] == 'PAUSE-PART' and first['gates'] == ['caps.invalid_attempts']
    # The open block lost acoustic credit: its three attempts count once each, never reset by the reboot.
    starts = [r for r in ledger.rows if r['type'] == 'part-start']
    assert len(starts) >= 2 and ledger.state.invalid == 3
    assert [r['attempt'] for r in attempts(ledger, b[0])] == [1, 2]          # re-attempt carried across the reboot
    assert attempts(ledger, b[0])[1]['part'] in (2, 3)                        # appended in part 2 if a block fits, else recovery
    assert ledger.state.closed['reason'] == 'PASS'


def test_early_pause_survives_under_campaign_wall_time_capacity(tmp_path, monkeypatch, plan):
    # v9.4-acceptance-01 shape: an early part-1 pause. Under v9.5 capacity (remaining campaign wall time, every part
    # up to 60 min) parts 2 and 3 can still hold the support, so the campaign continues and completes.
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][1]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={(cell, 1): RuntimeError('injected unknown')})
    assert ends(ledger)[0]['reason'] == 'PAUSE-PART' and len(ends(ledger)) == 3
    assert ledger.state.closed['reason'] == 'PASS' and sum(e['wall_s'] for e in ends(ledger)) <= 9000


def test_capacity_still_closes_when_the_ceiling_cannot_hold_the_work(plan):
    from echoear_aec_v9_ledger import Ledger as L
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        led = L(Path(tmp)/'l.jsonl', create=True)
        led.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED))
        for n, wall in ((1, 3600), (2, 3000)):
            led.append(dict(type='part-start', part=n, slot=n, budget=dict(total_s=3600)))
            led.append(dict(type='part-end', part=n, slot=n, entered=True, wall_s=wall, restore=dict(clean=True)))
        everything = [c['logical_id'] for c in plan['v9']['cells']]
        ok, info = v9.capacity(plan, led, everything, part=2)
        assert not ok and info['have_s'] == 9000-6600-360-330


def test_reboot_cannot_reset_per_cell_cap(tmp_path, monkeypatch, plan):
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][0]
    scenario = {('score', cell, 1): 'noisy', ('score', cell, 2): 'noisy'}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario=scenario)
    assert len(attempts(ledger, cell)) == 2 and ledger.state.cell(cell) == 'support-exhausted'
    assert len(ends(ledger)) == 2                         # nothing left to acquire after part 2
    v = verdict(ledger.state, plan)
    assert ledger.state.closed['reason'] == 'INCONCLUSIVE' and cell in v['completeness']['missing_cells']


def test_invalid_cap_exhaustion_is_inconclusive(tmp_path, monkeypatch, plan):
    cells = [lid for b in plan['v9']['schedule'][0]['blocks'] for lid in b['attempts']]
    noisy = cells[0:2]+cells[3:5]+cells[6:8]+cells[9:11]+cells[12:13]          # 9 invalid, never 3 in a row
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', c, 1): 'noisy' for c in noisy})
    assert ends(ledger)[0]['reason'] == 'INCONCLUSIVE' and ends(ledger)[0]['gates'] == ['caps.invalid_attempts']
    assert ledger.state.closed['reason'] == 'INCONCLUSIVE'


# ---- early FAIL, hard events, candidate faults ------------------------------------------------------

def test_third_closed_block_echo_miss_stops_early(tmp_path, monkeypatch, plan):
    minus6 = [lid for b in plan['v9']['schedule'][0]['blocks'] if b['kind'] == 'echo' for lid in b['attempts'] if '-m6-' in lid]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', c, 1): 'miss' for c in minus6[:3]})
    assert ends(ledger)[0]['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'
    assert len(ends(ledger)) == 1
    assert_restored(env, ledger)


def test_guaranteed_second_barge_miss_stops_early_but_beyond_ten_does_not(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan,
                                scenario={('score', 'v9-stop-firm-r01', 1): 'miss', ('score', 'v9-stop-firm-r02', 1): 'miss'})
    # Two eligible misses at ranks 1-2 fail only once ten eligible ranks are guaranteed (an all-ineligible
    # completion would be INCONCLUSIVE, not FAIL): the stop comes in part 2 at the block holding rank 10.
    assert [r['reason'] for r in ends(ledger)] == ['complete', 'STOP'] and ends(ledger)[1]['gates'] == ['barge.group_bar']
    assert ledger.state.closed['reason'] == 'FAIL'
    stop_block = [r for r in ledger.rows if r['type'] == 'block-close'][-1]['block_key']
    assert 'v9-stop-firm-r10' in [r['logical_id'] for r in attempts(ledger) if r['block_key'] == stop_block]
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan,
                                scenario={('score', 'v9-stop-firm-r11', 1): 'miss', ('score', 'v9-stop-firm-r12', 1): 'miss'})
    assert ledger.state.closed['reason'] == 'PASS'        # ranks 11-12 are outside the first ten eligible


def test_barge_group_counterexample_stays_open_until_rank_one_resolves(plan):
    S, M, P = 'success', 'miss', 'pending'
    assert rules.barge_group([P]+[S]*8+[M, M]+[S]) == 'OPEN'


@pytest.mark.parametrize('spec,gate', [('false', 'echo.false_turns'), ('fault', 'hardware.aec_errors'),
                                       ('unattributed', None), ('hard_floor', 'echo.hard_floor')])
def test_hard_events_faults_and_unknown(tmp_path, monkeypatch, plan, spec, gate):
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][1]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', cell, 1): spec})
    first = ends(ledger)[0]
    if spec == 'unattributed':
        assert first['reason'] == 'PAUSE-PART' and first['gates'] == ['clip.mic_unattributed']
        assert ledger.state.closed['reason'] == 'PASS' and not ledger.state.candidate   # never a candidate fault; recovered
    else:
        assert first['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'
        assert gate in [x['gate'] for x in ledger.state.candidate+ledger.state.hard]
        if spec == 'hard_floor':      # numeric hard limit acted on only at block closure
            row = attempts(ledger, cell)[0]
            assert ledger.state.attempts[row['attempt_id']]['classification']['provisional_hard'] == ['echo.hard_floor']
            assert any(r['type'] == 'block-close' for r in ledger.rows)
    assert_restored(env, ledger)


def test_hard_floor_on_an_unclosed_block_is_dropped(tmp_path, monkeypatch, plan):
    blocks = plan['v9']['schedule'][0]['blocks']
    cell = blocks[0]['attempts'][1]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1,
                                scenario={('score', cell, 1): 'hard_floor', **{('offset', f'v9-witness-p1-0{i}'): 1200 for i in (2, 3, 4)}})   # v9.10: 3 closing acquisitions
    assert ends(ledger)[0]['reason'] == 'PAUSE-PART' and ends(ledger)[0]['gates'] == ['alignment.near_witness']
    a = ledger.state.attempts[attempts(ledger, cell)[0]['attempt_id']]
    assert a['final']['hard_events'] == [] and a['final']['credited']['acoustic'] is None
    assert not ledger.state.hard and ledger.state.invalid == 5            # each unclosed attempt counts once


# ---- decision 71 ----------------------------------------------------------------------------------

def test_admission_overflow_is_candidate_fail(tmp_path, monkeypatch, plan):
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={(plan_mod.cell_index(plan)[cell]['cell']['id'], 1): 'overflow'})
    assert ends(ledger)[0]['reason'] == 'STOP' and 'admission.control_loss' in [x['gate'] for x in ledger.state.candidate]
    assert ledger.state.closed['reason'] == 'FAIL'
    assert_restored(env, ledger)


def test_drain_timeout_alone_pauses_and_drain_within_allowance_is_clean(tmp_path, monkeypatch, plan):
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={(cell, 1): 'drain'})
    assert ends(ledger)[0]['reason'] == 'PAUSE-PART' and 'admission.drain_timeout' in ends(ledger)[0]['gates']
    drain = json.loads((out/'part-1'/f'{cell}-a1'/'after-drain.json').read_text())
    assert drain['status'] == 'drain-timeout' and drain['allowance_s'] == 1.0 and len(drain['observations']) > 1
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, parts=1, scenario={(cell, 1): 'drain-recovers'})
    assert ends(ledger)[0]['reason'] == 'complete'


# ---- pause, resume, cleanup, analyzer, timeouts ---------------------------------------------------

def test_unknown_exception_pauses_resume_retains_ledger(tmp_path, monkeypatch, plan):
    blocks = plan['v9']['schedule'][0]['blocks']
    cell = blocks[6]['attempts'][2]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={(cell, 1): RuntimeError('injected unknown')})
    first = ends(ledger)[0]
    assert first['reason'] == 'PAUSE-PART' and first['gates'][0] in ('unknown.unclassified',)
    rows_before = [r for r in ledger.rows if r['seq'] <= first['seq']]
    assert ledger.rows[:len(rows_before)] == rows_before and verify(ledger.rows)
    # Closed blocks 1-2 keep credit; the open block's acquired attempts count invalid once and are re-attempted.
    assert all(r['closed'] for r in ledger.rows if r['type'] == 'block-close' and r['part'] == 1)
    assert ledger.state.closed['reason'] == 'PASS'
    assert_restored(env, ledger)


def test_harness_timeout_is_invalid_and_unclean_recovery_pauses(tmp_path, monkeypatch, plan):
    from echoear_aec_campaign import HarnessFailure
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1,
                                scenario={(cell, 1): HarnessFailure('evaluation-ack-timeout', 'Evaluation command not acknowledged')})
    assert [r['attempt'] for r in attempts(ledger, cell)] == [1, 2] and ends(ledger)[0]['reason'] == 'complete'
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, parts=1,
                                scenario={(cell, 1): HarnessFailure('capture-deadline', 'Capture deadline'), ('recover', cell): True})
    assert ends(ledger)[0]['reason'] == 'PAUSE-PART' and 'resources.coverage' in ends(ledger)[0]['gates']  # no retained leaves


def test_analyzer_exception_is_pending_and_withholds_resume(tmp_path, monkeypatch, plan):
    cell = plan['v9']['schedule'][0]['blocks'][0]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', cell, 1): 'raise'})
    assert len(ends(ledger)) == 1 and ends(ledger)[0]['reason'] == 'complete'     # acquisition continued
    assert ledger.state.pending == {attempts(ledger, cell)[0]['attempt_id']}
    v = verdict(ledger.state, plan)
    assert v['outcome'] == 'OPEN' and v['completeness']['pending_attempts']
    assert (out/'part-1'/f'{cell}-a1'/'analysis-failure.json').exists()
    assert [r['attempt'] for r in attempts(ledger, cell)] == [1]                  # never reacquired


def test_cleanup_failure_parks_and_refuses_resume(tmp_path, monkeypatch, plan):
    env = Env(tmp_path, monkeypatch); env.p = plan; env.restore_fails = True; env._part = 1
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    env.run_part(out, ledger, 1, mode='acceptance')
    end = ends(ledger)[0]
    assert end['restore']['clean'] is False and 'RESTORE: restore flash failed' in end['restore']['cleanup_errors']
    assert env.lock_events == ['acquire']                    # parked with the lock (never released)
    assert ledger.state.closed['reason'] == 'INCONCLUSIVE'


def test_operator_stop_and_part_level_candidate_fault(tmp_path, monkeypatch, plan):
    from echoear_aec_echo_dropout import BudgetExpired
    cell = plan['v9']['schedule'][0]['blocks'][1]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={(cell, 1): BudgetExpired('Operator stop')})
    assert ends(ledger)[0]['reason'] == 'PAUSE-PART' and ends(ledger)[0]['gates'][0] == 'operator.stop'
    assert_restored(env, ledger)
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, parts=1, scenario={('heap', 1): True})
    assert 'resources.heap' in [x['gate'] for x in ledger.state.candidate] and ledger.state.closed['reason'] == 'FAIL'


def test_setup_failure_restores_and_consumes_its_slot(tmp_path, monkeypatch, plan):
    import echoear_aec_campaign as C
    env = Env(tmp_path, monkeypatch); env.p = plan; env._part = 1
    calls = []
    def dump(board, path, timeout=150):        # loud room: every baseline attempt fails the noise gate
        n = 97600; pcm = (np.random.default_rng(2).normal(0, 3000, (n, 3))).astype('<i2')
        timing = np.array([[i*320, (i+1)*320, i*20000, i*20000+20000, 0] for i in range(n//320)])
        np.savez(path, pcm=pcm, rate=16000, timing=timing); calls.append(path)
    monkeypatch.setattr(C, 'dump_capture', dump)
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    env.run_part(out, ledger, 1, mode='acceptance')
    end = ends(ledger)[0]
    assert len(calls) == 3 and end['reason'] == 'PAUSE-PART' and end['restore']['clean'] and end['entered']
    assert end['restore']['restored_elf_sha256'] == NORMAL_HASH and env.lock_events == ['acquire', 'release']
    # Astra/Sol 5(b) rejected: the entered part used slot 1; under v9.5 capacity the campaign stays resumable.
    assert end['slot'] == 1 and ledger.state.closed is None


def test_witness_failure_at_part_start_pauses_without_attempts(tmp_path, monkeypatch, plan):
    # v9.10: three out-of-tolerance acquisitions at the opening position (one plus two retakes) pause the part.
    bad = {('offset', f'v9-witness-p1-0{i}'): 1100 for i in (1, 2, 3)}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario=bad)
    assert ends(ledger)[0]['gates'] == ['alignment.near_witness']
    assert [r for r in attempts(ledger) if r['kind'] != 'near-witness'] == []
    assert [r['block_key'] for r in attempts(ledger)] == ['p1/p1-b1/open', 'p1/p1-b1/open#2', 'p1/p1-b1/open#3']


# ---- smoke ----------------------------------------------------------------------------------------

def test_smoke_two_parts_resume_boundary_and_burst(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2)
    assert [r['reason'] for r in ends(ledger)] == ['complete', 'complete']
    assert ledger.state.closed['reason'] == 'SMOKE-COMPLETE'
    p1 = [r['logical_id'] for r in attempts(ledger) if r['part'] == 1]
    assert p1 == ['v9-witness-p1-01', 'v9-echo-m6-t01-g1', 'v9-echo-0-t08-g1', 'v9-stop-firm-r01', 'v9-witness-p1-02']
    p2 = [r['logical_id'] for r in attempts(ledger) if r['part'] == 2]
    assert p2 == ['v9-witness-p2-01', 'v9-sentence-firm-r01', 'v9-sentence-normal-r01', 'v9-witness-p2-02']
    bursts = [r for r in ledger.rows if r['type'] == 'burst']
    assert [b['result'] for b in bursts] == ['covered'] and bursts[0]['received_ids'] == list(range(40))
    s = v9.smoke_summary(plan, ledger.rows)
    assert s['resume_boundary'] and s['burst_covered'] and not s['candidate_faults'], s
    assert s['largest_last_arrival_to_quiescence_s'] == pytest.approx(bursts[0]['latency_s'])
    assert s['acceptance_drain_allowance_s'] == plan['v9']['admission']['drain_allowance_s'] == 4.0 and s['ready'], s
    assert sum(e['wall_s'] for e in ends(ledger)) <= plan['v9']['smoke']['ceiling_s']
    assert verdict(ledger.state, plan)['credit'] is False
    assert_restored(env, ledger)


def test_smoke_burst_coverage_not_achieved_and_loss(tmp_path, monkeypatch, plan):
    scenario = {('burst-deferred', k): 0 for k in range(1, 11)}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2, scenario=scenario)
    bursts = [r for r in ledger.rows if r['type'] == 'burst']
    assert [b['result'] for b in bursts] == ['coverage-not-achieved']*10     # bounded, predeclared attempts (v9.4: 10)
    assert not v9.smoke_summary(plan, ledger.rows)['ready'] and not ledger.state.candidate
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, mode='smoke', parts=2, scenario={('burst-ids', 1): [1, 0]+list(range(2, 40))})
    assert [b['result'] for b in ledger.rows if b['type'] == 'burst'] == ['fail']          # misordered application receipt
    assert 'admission.control_loss' in [x['gate'] for x in ledger.state.candidate]


def test_server_ids_parses_application_log():
    lines = ['I (5) dg-app: SERVER {"label":"rtvi-ai","type":"server-message","data":{"t":"burst","d":{"b":2,"id":0}}}',
             'I (5) dg-app: SERVER ignored message truncated=0',
             'I (6) dg-app: SERVER {"t":"burst","d":{"b":2,"id":1}}', 'I (6) dg-app: SERVER {"t":"burst","d":{"b":1,"id":9}}']
    assert v9.server_ids(lines, 2) == [0, 1]


# ---- launch gates (no hardware) ------------------------------------------------------------------

def test_launch_requires_reviewed_sha_user_go_and_tty(tmp_path, monkeypatch):
    plan = tmp_path/'plan.json'; plan.write_text('{}')
    monkeypatch.setattr(plan_mod, 'DESTINATION', tmp_path)
    digest = v9.sha(plan)
    with pytest.raises(ValueError, match='Reviewed plan SHA'): v9.check_launch(plan, 'x'*64, mode='smoke', tty=True)
    with pytest.raises(ValueError, match='USER-GO'): v9.check_launch(plan, digest, mode='acceptance', tty=True)
    (tmp_path/'USER-GO').write_text('nope\n')
    with pytest.raises(ValueError, match='USER-GO'): v9.check_launch(plan, digest, mode='acceptance', tty=True)
    (tmp_path/'USER-GO').write_text(digest+'\n')
    with pytest.raises(ValueError, match='terminal'): v9.check_launch(plan, digest, mode='acceptance', tty=False)
    assert v9.check_launch(plan, digest, mode='acceptance', tty=True) == {}
    (tmp_path/'USER-GO').unlink()
    assert v9.check_launch(plan, digest, mode='smoke', tty=True) == {}             # standing smoke permission
    with pytest.raises(ValueError, match='freeze bundle'): v9.check_launch(tmp_path/'other.json', digest, mode='smoke', tty=True)


def test_gate_of_exception_uses_registry():
    from echoear_aec_campaign import HarnessFailure
    assert v9.gate_of_exception(HarnessFailure('rf-pre-gate', 'x')) == 'rf.cell_pre_gate'
    assert v9.gate_of_exception(KeyboardInterrupt()) == 'operator.stop'
    assert v9.gate_of_exception(RuntimeError('anything')) == 'unknown.unclassified'
    import echoear_aec_campaign as C
    try:
        C.BenchHold.capture.__wrapped__ if False else None
        C.check_capture_timing(np.array([[0, 320, 0, 20000, 1]]), [])
    except Exception as exc:
        assert v9.gate_of_exception(exc) in ('timing.single_gap', 'timing.capture_bracket')


# ---- v9.1 joined-path regressions (Astra freeze review blockers) ---------------------------------

def first_cell(plan, block=0, index=0, part=0):
    return plan['v9']['schedule'][part]['blocks'][block]['attempts'][index]


def test_b1_output_clip_is_an_acoustic_miss_through_run_part(tmp_path, monkeypatch, plan):
    cell = first_cell(plan, 0, 1)
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={(cell, 1): 'output_clip'})
    row = attempts(ledger, cell)[0]
    assert 'clip.afe_output' in [f['gate'] for f in row['findings']]
    assert row['scores']['acoustic']['result'] == 'miss' and row['scores']['acoustic']['clip_miss'] == 'clip.afe_output'
    assert ledger.state.cell(cell) == 'miss' and [r['attempt'] for r in attempts(ledger, cell)] == [1]


def test_b1_reference_clip_needs_intact_mapping(tmp_path, monkeypatch, plan):
    cell = first_cell(plan, 0, 1)
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={(cell, 1): 'reference_clip'})
    assert attempts(ledger, cell)[0]['scores']['acoustic']['result'] == 'miss'
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, parts=1,
                                scenario={(cell, 1): 'reference_clip', ('score', cell, 1): 'broken_map'})
    row = attempts(ledger, cell)[0]
    assert 'unknown.unclassified' in [f['gate'] for f in row['findings']] and ends(ledger)[0]['reason'] == 'PAUSE-PART'


def test_b3_capture_deadline_keeps_an_established_miss(tmp_path, monkeypatch, plan):
    cell = 'v9-stop-firm-r01'
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1,
                                scenario={(cell, 1): 'deadline', ('score', cell, 1): 'established'})
    row = attempts(ledger, cell)[0]
    assert row['partial'] and 'capture.cap' in [f['gate'] for f in row['findings']]
    assert row['scores']['acoustic'] == dict(result='miss', established=True)
    assert ledger.state.cell(cell) == 'miss' and len(attempts(ledger, cell)) == 1       # not retaken
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, parts=1, scenario={(cell, 1): 'deadline'})
    assert [r['attempt'] for r in attempts(ledger, cell)] == [1, 2]                   # nothing established: retake


def test_b3_failed_composite_with_scorer_exception_keeps_live_fault(tmp_path, monkeypatch, plan):
    cell = first_cell(plan, 0, 0)
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1,
                                scenario={(cell, 1): 'composite', ('score', cell, 1): 'raise'})
    row = attempts(ledger, cell)[0]
    gates = [f['gate'] for f in row['findings']]
    assert row['pending'] and 'analyzer.exception' in gates and 'admission.control_loss' in gates
    assert ends(ledger)[0]['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'


def test_b3_pending_blocks_dependent_retakes(tmp_path, monkeypatch, plan):
    b = plan['v9']['schedule'][0]['blocks'][6]['attempts']
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1,
                                scenario={('score', b[0], 1): 'raise', ('score', b[1], 1): 'noisy'})
    assert ledger.state.pending and ledger.state.cell(b[1]) == 'retake'
    assert [r['attempt'] for r in attempts(ledger, b[1])] == [1]          # retake withheld while a score is pending
    assert not [r for r in attempts(ledger) if r['block_key'].startswith('p1/x')]


def _pending_ledger(tmp_path, monkeypatch, plan, cell, **kw):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', cell, 1): 'raise'}, **kw)
    return env, out, ledger


def test_b4_rescore_pending_to_candidate_fault_fails_and_replays(tmp_path, monkeypatch, plan):
    cell = first_cell(plan, 0, 0)
    env, out, ledger = _pending_ledger(tmp_path, monkeypatch, plan, cell, parts=1)
    aid = attempts(ledger, cell)[0]['attempt_id']
    (tmp_path/'amend.diff').write_text('analyzer fix\n')
    fault = lambda view, folder, p, entry: dict(findings=[dict(gate='admission.control_loss')], scores=dict(
        acoustic=dict(result='pass'), events=dict(result='pass')), record={})
    good = lambda view, folder, p, entry: dict(findings=[], scores=dict(acoustic=dict(result='pass'), events=dict(result='pass')), record={})
    v9.rescore(plan, out, tmp_path/'amend.diff', check_runtime=False,
               scorer=lambda view, folder, p, entry: (fault if folder.name.startswith(cell) else good)(view, folder, p, entry))
    ledger = Ledger(out/'ledger.jsonl')
    assert 'admission.control_loss' in [x['gate'] for x in ledger.state.candidate] and not ledger.state.pending
    assert ledger.state.closed['reason'] == 'FAIL'
    # Full-ledger replay equals the incremental state.
    from echoear_aec_v9_ledger import fold
    assert verdict(fold(ledger.rows), plan) == verdict(ledger.state, plan)
    assert ledger.state.attempts[aid]['final']['candidate_faults'] == ['admission.control_loss']


def test_b4_rescore_pending_to_invalid_counts_once_and_exhausts(tmp_path, monkeypatch, plan):
    cells = [lid for b in plan['v9']['schedule'][0]['blocks'] for lid in b['attempts']]
    noisy = cells[0:2]+cells[3:5]+cells[6:8]+cells[9:11]        # 8 invalid, never 3 in a row
    scenario = {('score', c, 1): 'noisy' for c in noisy}
    scenario[('score', cells[12], 1)] = 'raise'
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario=scenario, parts=1)
    assert ledger.state.invalid == 8 and ledger.state.pending
    (tmp_path/'amend.diff').write_text('analyzer fix\n')
    def scorer(view, folder, p, entry):
        if folder.name.startswith(cells[12]):
            return dict(findings=[dict(gate='echo.event_coverage')] if entry['kind'] == 'echo' else [dict(gate='barge.unscorable')],
                        scores=dict(acoustic=dict(result='pass'), events=dict(result='pass')), record={})
        old = [r for r in ledger.rows if r['type'] == 'attempt' and r['folder'].endswith(folder.name)][-1]
        return dict(findings=[f for f in old['findings']], scores=old['scores'], record={})
    for _ in range(2):          # repeated rescore never double-counts
        v9.rescore(plan, out, tmp_path/'amend.diff', check_runtime=False, scorer=scorer)
        state = Ledger(out/'ledger.jsonl').state
        assert state.invalid == 9 and not state.pending
    v = verdict(state, plan)
    assert v['caps']['exhausted'] and v['outcome'] == 'INCONCLUSIVE'


def test_b4_resume_refused_after_rescore_fail(tmp_path, monkeypatch, plan):
    cell = first_cell(plan, 0, 0)
    env, out, ledger = _pending_ledger(tmp_path, monkeypatch, plan, cell, parts=1)
    (tmp_path/'amend.diff').write_text('x\n')
    v9.rescore(plan, out, tmp_path/'amend.diff', check_runtime=False, scorer=lambda *a: dict(
        findings=[dict(gate='hardware.aec_errors')], scores=dict(acoustic=dict(result='pass'), events=dict(result='pass')), record={}))
    p = dict(plan, outputs=dict(acceptance=str(out), smoke=str(out)))
    monkeypatch.setattr(v9, 'check_launch', lambda *a, **k: p)
    with pytest.raises(ValueError, match='Campaign closed: FAIL'):
        v9.resume('plan', out, REVIEWED, mode='acceptance', tty=True, skip_preflight=True)


def test_b5_final_settle_read_error_is_candidate_fail(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={('final', 1): dict(perf=dict(read_errors=1))})
    end = ends(ledger)[0]
    assert end['reason'] == 'STOP' and 'hardware.audio_io' in end['gates']
    assert 'hardware.audio_io' in [x['gate'] for x in ledger.state.candidate] and ledger.state.closed['reason'] == 'FAIL'


def test_b5_admission_overflow_after_last_attempt_is_caught_by_part_epoch(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={('final', 1): dict(perf=dict(overflow=1))})
    assert 'admission.control_loss' in ends(ledger)[0]['gates'] and ledger.state.closed['reason'] == 'FAIL'


def test_b5_recovery_failure_dispatches_retained_leaves(tmp_path, monkeypatch, plan):
    from echoear_aec_campaign import HarnessFailure
    cell = first_cell(plan, 0, 0)
    failure = dict(resources=dict(checks=dict(internal_minimum_free=dict(passed=False, value=70000))), delivery=dict(delta={}))
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1,
                                scenario={(cell, 1): HarnessFailure('jabra-duration', 'x'), ('recover', cell): failure})
    gates = [f['gate'] for f in attempts(ledger, cell)[0]['findings']]
    assert 'resources.heap' in gates and 'retry.clean_witness' not in gates and ledger.state.closed['reason'] == 'FAIL'


def test_b5_completed_false_cannot_pass(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('final', 2): dict(completed=False)})
    assert ends(ledger)[1]['reason'] == 'PAUSE-PART' and not ends(ledger)[1]['audit']['clean']
    v = verdict(ledger.state, plan)
    assert v['acoustic'] == 'PASS' and v['integration'] == 'INCOMPLETE' and ledger.state.closed['reason'] == 'INCONCLUSIVE'


def test_b6_smoke_readiness_requires_structural_completion(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2,
                                scenario={('offset', f'v9-witness-p1-0{i}'): 1100 for i in (1, 2, 3)})   # part 1 never opens its block
    s = v9.smoke_summary(plan, ledger.rows, out=out)
    assert s['burst_covered'] and not s['lifecycle_ready'] and not s['ready']
    assert any('not exercised' in x for x in s['problems'])
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, mode='smoke', parts=2, scenario={('score', 'v9-echo-0-t08-g1', 1): 'raise'})
    s = v9.smoke_summary(plan, ledger.rows, out=out)
    assert not s['ready'] and 'pending scores' in s['problems']
    tmp3 = tmp_path/'c'; tmp3.mkdir()
    env, out, ledger = campaign(tmp3, monkeypatch, plan, mode='smoke', parts=2)
    env.restore_delay_s = 0
    s = v9.smoke_summary(plan, ledger.rows, out=out)
    assert s['ready'], s['problems']
    rows = [dict(r, wall_s=1000) if r['type'] == 'part-end' else r for r in ledger.rows]
    assert any('ceiling' in x for x in v9.smoke_summary(plan, rows)['problems'])
    # Evidence identity: a changed part file is not ready.
    victim = next((out/'part-1').rglob('result.json')); victim.write_text(victim.read_text()+' ')
    assert any('evidence changed' in x for x in v9.smoke_summary(plan, ledger.rows, out=out)['problems'])


def test_b7_entered_setup_failure_consumes_a_slot_and_no_fourth_part(tmp_path, monkeypatch, plan):
    import echoear_aec_campaign as C
    env = Env(tmp_path, monkeypatch); env.p = plan
    def loud(board, path, timeout=150):
        n = 97600; pcm = (np.random.default_rng(2).normal(0, 3000, (n, 3))).astype('<i2')
        timing = np.array([[i*320, (i+1)*320, i*20000, i*20000+20000, 0] for i in range(n//320)])
        np.savez(path, pcm=pcm, rate=16000, timing=timing)
    monkeypatch.setattr(C, 'dump_capture', loud)
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for part in range(1, 5):
        if ledger.state.closed: break
        env._part = part; env.run_part(out, ledger, part, mode='acceptance')
    e = ends(ledger)
    assert [x['slot'] for x in e] == list(range(1, len(e)+1)) and 2 <= len(e) <= 3
    assert all(x['entered'] and x['reason'] == 'PAUSE-PART' for x in e)
    # Every entered setup failure used a slot; once the remaining parts cannot hold the support: INCONCLUSIVE.
    assert ledger.state.closed['reason'] == 'INCONCLUSIVE'
    # Direct count rule: three entered parts, then the fourth is refused before any lock or flash.
    rows = [r for r in ledger.rows if r['type'] != 'campaign-close']
    led2 = Ledger(tmp_path/'l2.jsonl', create=True)
    for r in rows: led2.append({k: v for k, v in r.items() if k not in ('seq', 'prev', 'sha256')})
    for _ in range(2):
        led2.append(dict(type='part-start', part=9, slot=2, role='planned', budget=dict(total_s=10)))
        led2.append(dict(type='part-end', part=9, slot=2, entered=True, reason='PAUSE-PART', wall_s=1, restore=dict(clean=True),
                         audit=dict(clean=True), files_sha256={}))
    flashes = len(env.flashes)
    v9.run_part(plan, out, led2, 4, mode='acceptance', allowance_s=1.0, hold_factory=env.hold_factory,
                budget_factory=lambda c: FakeBudget(env.clock, c), acoustic=env.acoustic, clock=env.clock)
    assert led2.state.closed['reason'] == 'INCONCLUSIVE' and len(env.flashes) == flashes


def test_b7_late_restore_over_part_budget_cannot_pass(tmp_path, monkeypatch, plan):
    env = Env(tmp_path, monkeypatch); env.p = plan
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    env._part = 1; env.run_part(out, ledger, 1, mode='acceptance')
    env.restore_delay_s = 2000                                # a clean but very slow final restore
    env._part = 2; env.run_part(out, ledger, 2, mode='acceptance')
    v = verdict(ledger.state, plan)
    assert v['acoustic'] == 'PASS' and not v['caps']['time_ok'] and 2 in v['caps']['over_budget_parts']
    assert v['outcome'] != 'PASS' and ledger.state.closed['reason'] == 'INCONCLUSIVE'
    assert ends(ledger)[1]['restore']['clean']                 # the restore itself still finished safely


def test_baseline_floor_report_is_persisted(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1)
    rows = [r for r in ledger.rows if r['type'] == 'floors']
    assert len(rows) == 1 and set(rows[0]['baseline']) == {'pre', 'post'}
    assert json.loads((out/'part-1'/'baseline-floors.json').read_text()) == rows[0]['baseline']
    assert set(rows[0]['baseline']['pre']) == {'raw_dbfs', 'afe_dbfs', 'raw_hpf_dbfs', 'afe_hpf_dbfs'}


# ---- Sol freeze review blockers ---------------------------------------------------------------------

def _rechain(rows):
    from echoear_aec_v9_ledger import digest, GENESIS
    out, prev = [], GENESIS
    for i, r in enumerate(rows):
        r = {k: v for k, v in r.items() if k not in ('seq', 'prev', 'sha256')}
        r.update(seq=i, prev=prev); r['sha256'] = digest(r); prev = r['sha256']; out.append(r)
    return out


def _with_extra_invalid(rows, cell):
    """Append a third entered part holding one more independently invalid exposure of an already-passed cell."""
    body = [r for r in rows if r['type'] != 'campaign-close']
    extra = [dict(type='part-start', part=3, slot=3, role='recovery', budget=dict(total_s=1800)),
             dict(type='attempt', part=3, block_key='p3/x1', logical_id=cell, kind='echo', attempt=2, attempt_id='p3/x',
                  folder='', source_sha256={}, findings=[dict(gate='noise.cell_quiet_windows')],
                  scores=dict(acoustic=dict(result='pass'), events=dict(result='pass')), pending=False, record={}, timing={}, error=None),
             dict(type='block-close', part=3, block_key='p3/x1', offsets=[1024, 1024], closed=True),
             dict(type='part-end', part=3, slot=3, entered=True, reason='complete', wall_s=10, restore=dict(clean=True),
                  audit=dict(clean=True), files_sha256={})]
    return _rechain(body+extra)


def test_sol1_eight_vs_nine_invalid_and_immutable_closure(tmp_path, monkeypatch, plan):
    from echoear_aec_v9_ledger import fold
    cells = [lid for b in plan['v9']['schedule'][0]['blocks'] for lid in b['attempts']]
    noisy = cells[0:2]+cells[3:5]+cells[6:8]+cells[9:11]           # eight invalid, each re-attempted and passed
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', c, 1): 'noisy' for c in noisy})
    assert ledger.state.invalid == 8 and ledger.state.closed['reason'] == 'PASS'
    assert verdict(fold(ledger.rows), plan)['outcome'] == 'PASS'
    nine = fold(_with_extra_invalid(ledger.rows, cells[20]))
    v = verdict(nine, plan, final=True)
    assert nine.invalid == 9 and v['caps']['exhausted'] and v['outcome'] == 'INCONCLUSIVE'
    # Immutable closure: a terminal INCONCLUSIVE stays INCONCLUSIVE in any later fold.
    closed = [dict(r, reason='INCONCLUSIVE') if r['type'] == 'campaign-close' else r for r in ledger.rows]
    assert verdict(fold(_rechain(closed)), plan)['outcome'] == 'INCONCLUSIVE'


def test_sol2_backlog_without_arrival_is_never_zero():
    t = iter([10.0, 10.0, 11.5, 11.5, 11.8, 11.8]); vals = iter([(10, 9), (10, 10), (10, 10)])
    def snap(board, folder, label):
        r, d = next(vals)
        return dict(pc_id='pc', generation=1, data_admission=dict(received=r, dispatched=d, deferred=0))
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        v9.drain_boundary(None, Path(tmp), 'after', allowance_s=2.0, snapshot=snap, clock=lambda: next(t), sleep=lambda s: None)
        d = json.loads((Path(tmp)/'after-drain.json').read_text())
    assert d['status'] == 'quiescent' and d['backlog_observed'] and d['last_observed_arrival_s'] is None
    unanchored = v9.drain_latency(d, [])
    assert unanchored['latency_s'] is None and unanchored['unbounded']
    anchored = v9.drain_latency(d, [dict(kind='rtvi-output', source='RTVIProcessor', direction='DOWNSTREAM', monotonic_ns=int(9.7e9))])
    assert anchored['latency_s'] == pytest.approx(2.1)                # sustained quiescence - last send, incl. PERF I/O
    # Readiness (v9.9): an unbounded drain withholds the allowance; a 2.1 s boundary exceeds the 2.0 s readiness bound.
    base = [dict(type='campaign', mode='smoke'), dict(type='burst', burst=1, result='covered', latency_s=.1, deferred_delta=3,
                                                      overflow_delta=0)]
    for boundaries, why in (({'after': unanchored}, 'unbounded'), ({'after': anchored}, 'too large')):
        rows = _rechain(base+[dict(type='drain', attempt_id='a', boundaries=boundaries)])
        s = v9.smoke_summary(dict(v9=plan_mod.plan_from()['v9']), rows)
        assert s['acceptance_drain_allowance_s'] is None and not s['ready'], why


def test_sol3_recorder_shortfall_is_always_unknown(tmp_path, monkeypatch, plan):
    cell = first_cell(plan, 0, 0)
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={(cell, 1): 'recorder-short'})
    row = attempts(ledger, cell)[0]
    rec = row['record']['reply_admission']
    assert rec['recorder_messages'] == 2 and rec['received_delta'] == rec['dispatched_delta'] == 0 and not rec['passed']
    assert 'unknown.unclassified' in [f['gate'] for f in row['findings']] and ends(ledger)[0]['reason'] == 'PAUSE-PART'
    # v9.3: even complete, acknowledged host SCTP evidence never resolves delivery: still PAUSE-PART, no candidate fault.
    tmp2 = tmp_path/'b'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, parts=1, scenario={(cell, 1): 'recorder-short-acked'})
    row = attempts(ledger, cell)[0]
    assert 'unknown.unclassified' in [f['gate'] for f in row['findings']] and not ledger.state.candidate
    assert ends(ledger)[0]['reason'] == 'PAUSE-PART'


# ---- shakedown smoke findings S1-S2 ---------------------------------------------------------------

def test_s1_late_application_ids_from_the_part_log_are_not_loss(tmp_path, monkeypatch, plan):
    # Exact v9-smoke-01 shape: the per-burst window held ids 0-37; 38 and 39 were applied 2-8 ms later and
    # are in the whole-part serial log.
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2,
                                scenario={('burst-ids', 1): list(range(38)), ('part-log-ids', 1): list(range(40))})
    b = [r for r in ledger.rows if r['type'] == 'burst'][0]
    assert b['window_ids'] == list(range(38)) and b['received_ids'] == list(range(40))
    assert b['result'] == 'covered' and not ledger.state.candidate


def test_s1_console_gap_with_complete_device_receipt_is_evidence_loss(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2,
                                scenario={('burst-ids', 1): list(range(38)), ('part-log-ids', 1): list(range(38))})
    b = [r for r in ledger.rows if r['type'] == 'burst'][0]
    assert b['result'] == 'evidence-incomplete' and [f['gate'] for f in b['findings']] == ['hardware.console']
    assert not ledger.state.candidate and ends(ledger)[1]['reason'] == 'PAUSE-PART'


def test_s2_single_instant_equality_is_not_quiescence():
    seq = iter([(10, 10), (12, 11), (12, 12), (12, 12)]); now = [0.0]
    def clock(): now[0] += .05; return now[0]
    def snap(board, folder, label):
        r, d = next(seq); return dict(pc_id='pc', generation=1, device_us=1, data_admission=dict(received=r, dispatched=d, deferred=0))
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        v9.drain_boundary(None, Path(tmp), 'after', allowance_s=2.0, snapshot=snap, clock=clock,
                          sleep=lambda s: now.__setitem__(0, now[0]+s))
        d = json.loads((Path(tmp)/'after-drain.json').read_text())
    assert d['status'] == 'quiescent' and len(d['observations']) == 4 and d['backlog_observed']
    q = [o for o in d['observations'] if o['received'] == o['dispatched']]
    assert d['observations'][-1]['requested_s']-d['observations'][-2]['requested_s'] >= v9.QUIESCENT_SPACING_S
    assert d['first_quiescent_s'] == q[0]['t'] and d['quiescent_s'] > d['first_quiescent_s']


def test_s2_burst_waits_for_the_declared_count(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2)
    drain = json.loads((out/'part-2'/'burst-01'/'after-drain.json').read_text())
    base = json.loads((out/'part-2'/'burst-01'/'before.json').read_text())['data_admission']['received']
    assert drain['status'] == 'quiescent' and drain['observations'][-1]['received']-base >= 40
    b = [r for r in ledger.rows if r['type'] == 'burst'][0]
    assert b['latency_s'] is not None and b['latency_s'] > 0



# ---- v9.1 re-review: burst accounting, receipt recovery, closure, partial, drain deadlines -----------

@pytest.mark.parametrize('log,gate', [(list(range(40))+[39], 'admission.control_loss'),
                                      ([0, 1, 3, 2]+list(range(4, 40)), 'admission.control_loss')])
def test_v92_part_log_recovery_keeps_duplicates_and_misorder(tmp_path, monkeypatch, plan, log, gate):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2,
                                scenario={('burst-ids', 1): list(range(38)), ('part-log-ids', 1): log})
    b = [r for r in ledger.rows if r['type'] == 'burst'][0]
    assert b['received_ids'] == log and b['result'] == 'fail' and gate in [x['gate'] for x in ledger.state.candidate]


def test_v92_readiness_reaudits_receipts(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2)
    assert v9.smoke_summary(plan, ledger.rows, out=out)['ready']
    log = out/'part-2'/'candidate-serial.log'
    log.write_text(log.read_text()+'I (2000) dg-app: SERVER {"d": {"b": 1, "id": 39}, "t": "burst"}\n')
    problems = v9.smoke_summary(plan, ledger.rows, out=out)['problems']
    assert any('re-audit' in x for x in problems)


def test_v92_closed_pass_withdraws_when_rescore_removes_support(tmp_path, monkeypatch, plan):
    cells = [lid for b in plan['v9']['schedule'][0]['blocks'] for lid in b['attempts']]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan)
    assert ledger.state.closed['reason'] == 'PASS'
    (tmp_path/'amend.diff').write_text('x\n')
    victims = {c: None for c in cells[:2]+cells[3:5]+cells[6:8]+cells[9:11]+cells[12:13]}
    def scorer(view, folder, p, entry):
        old = [r for r in ledger.rows if r['type'] == 'attempt' and r['folder'].endswith(folder.name)][-1]
        if old['logical_id'] in victims:
            return dict(findings=[dict(gate='noise.cell_quiet_windows')], scores=old['scores'], record={})
        return dict(findings=old['findings'], scores=old['scores'], record={})
    v9.rescore(plan, out, tmp_path/'amend.diff', check_runtime=False, scorer=scorer)
    state = Ledger(out/'ledger.jsonl').state
    v = verdict(state, plan)
    assert state.invalid == 9 and v['caps']['exhausted'] and not v['completeness']['complete']
    assert v['closed'] == 'PASS' and v['outcome'] == 'INCONCLUSIVE'               # withdrawn, not reopened
    p = dict(plan, outputs=dict(acceptance=str(out), smoke=str(out)))
    monkeypatch.setattr(v9, 'check_launch', lambda *a, **k: p)
    with pytest.raises(ValueError, match='Campaign closed'):
        v9.resume('plan', out, REVIEWED, mode='acceptance', tty=True, skip_preflight=True)


def test_v92_closed_pass_to_candidate_fail(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan)
    (tmp_path/'amend.diff').write_text('x\n')
    first = first_cell(plan)
    def scorer(view, folder, p, entry):
        old = [r for r in ledger.rows if r['type'] == 'attempt' and r['folder'].endswith(folder.name)][-1]
        extra = [dict(gate='hardware.aec_errors')] if old['logical_id'] == first else []
        return dict(findings=old['findings']+extra, scores=old['scores'], record={})
    v9.rescore(plan, out, tmp_path/'amend.diff', check_runtime=False, scorer=scorer)
    assert verdict(Ledger(out/'ledger.jsonl').state, plan)['outcome'] == 'FAIL'


def test_v92_partial_pending_is_repaired_offline(tmp_path, monkeypatch, plan):
    cell = 'v9-stop-firm-r01'
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario={(cell, 1): 'deadline', ('score', cell, 1): 'raise'})
    row = attempts(ledger, cell)[0]
    assert row['pending'] and row['partial'] and (out/row['folder']/'partial-descriptor.json').exists()
    (tmp_path/'amend.diff').write_text('x\n')
    seen = []
    def scorer(view, folder, p, entry, partial=None):
        seen.append(partial is not None)
        return dict(findings=[], scores=dict(acoustic=dict(result='miss', established=True)), record={})
    v9.rescore(plan, out, tmp_path/'amend.diff', check_runtime=False, scorer=scorer)
    state = Ledger(out/'ledger.jsonl').state
    assert True in seen and not state.pending and state.cell(cell) == 'miss'


@pytest.mark.parametrize('sequence,expected', [
    ([(1.0, (10, 9)), (2.5, (10, 10)), (5.0, (10, 10))], 'drain-timeout'),     # first equality after the allowance
    ([(0.5, (10, 10)), (3.0, (10, 10))], 'drain-timeout'),                      # confirmation after deadline + 1 s
    ([(0.1, (10, 9)), (0.5, (10, 10)), (0.9, (10, 10))], 'quiescent'),          # timely pair
])
def test_v92_drain_deadlines_use_receipt_times(sequence, expected):
    import tempfile
    seq = iter(sequence); state = dict(t=0.0, pending=None)
    def clock(): return state['t']
    def snap(board, folder, label):
        t, (r, d) = next(seq); state['t'] = t
        return dict(pc_id='pc', generation=1, data_admission=dict(received=r, dispatched=d, deferred=0))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            v9.drain_boundary(None, Path(tmp), 'x', allowance_s=1.0, snapshot=snap, clock=clock, sleep=lambda s: None)
            status = json.loads((Path(tmp)/'x-drain.json').read_text())['status']
        except StopIteration:
            status = 'exhausted'
    assert status == expected



# ---- v9.3: burst readiness from application receipts, re-audit, continuation -----------------------

def test_v93_burst_continues_until_covered(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2,
                                scenario={('burst-deferred', 1): 0, ('burst-deferred', 2): 0, ('burst-deferred', 3): 5})
    bursts = [r for r in ledger.rows if r['type'] == 'burst']
    assert [b['result'] for b in bursts] == ['coverage-not-achieved', 'coverage-not-achieved', 'covered']
    assert all(not b['findings'] and b['drain_status'] == 'quiescent' for b in bursts)
    assert v9.smoke_summary(plan, ledger.rows, out=out)['ready']


def test_v93_burst_readiness_needs_no_pipeline_accounting(tmp_path, monkeypatch, plan):
    # A pipeline message around the baseline (previously double counted) no longer affects readiness.
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2, scenario={('burst-pre-ping', 1): True,
                                                                                              ('burst-inflight', 1): True})
    b = [r for r in ledger.rows if r['type'] == 'burst'][0]
    assert b['drain_status'] == 'quiescent' and b['result'] == 'covered' and not b['findings']
    assert b['last_application_ms'] == 1039 and b['latency_class'] == 'MONITOR'


def test_v93_reaudit_flags_duplicates_on_an_uncovered_burst(tmp_path, monkeypatch, plan):
    scenario = {('burst-deferred', k): 0 for k in range(1, 11)}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2, scenario=scenario)
    assert [b['result'] for b in ledger.rows if b['type'] == 'burst'] == ['coverage-not-achieved']*10
    log = out/'part-2'/'candidate-serial.log'
    log.write_text(log.read_text()+'I (3000) dg-app: SERVER {"d": {"b": 2, "id": 5}, "t": "burst"}\n')
    problems = v9.smoke_summary(plan, ledger.rows, out=out)['problems']
    assert any('burst 2' in x and 'duplicate' in x for x in problems)


def test_v93_calibration_comes_from_attempt_boundaries_only(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2)
    s = v9.smoke_summary(plan, ledger.rows, out=out)
    boundary = [b['latency_s'] for r in ledger.rows if r['type'] == 'drain' for b in r['boundaries'].values() if b.get('latency_s') is not None]
    assert s['largest_last_arrival_to_quiescence_s'] == max(boundary)
    burst = [r for r in ledger.rows if r['type'] == 'burst'][0]
    rows = [dict(r, latency_s=4.5) if r['type'] == 'burst' else r for r in ledger.rows]       # slow burst: MONITOR only
    assert v9.smoke_summary(plan, rows)['largest_last_arrival_to_quiescence_s'] == max(boundary)
    rows = [dict(r, latency_s=6.0) if r['type'] == 'burst' else r for r in ledger.rows]       # beyond the receipt timeout
    assert any('latency not within' in x for x in v9.smoke_summary(plan, rows)['problems'])



# ---- v9.4 -------------------------------------------------------------------------------------

@pytest.mark.parametrize('stamps', ['none', 'offset'])
def test_v94_causal_barrier_needs_no_clock_comparison(tmp_path, monkeypatch, plan, stamps):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2, scenario={'stamps': stamps})
    b = [r for r in ledger.rows if r['type'] == 'burst'][0]
    assert b['result'] == 'covered' and b['drain_status'] == 'quiescent' and not b['findings']
    drain = json.loads((out/'part-2'/'burst-01'/'after-drain.json').read_text())
    assert all(o['requested_s'] >= b['receipt_barrier_s'] for o in drain['observations'])     # receipts before requests
    assert b['latency_s'] < plan['v9']['burst']['receipt_timeout_s']
    assert (b['last_application_ms'] is None) == (stamps == 'none')


def test_v94_late_duplicate_is_a_structured_reaudit_finding(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2, scenario={'late-dup': 1})
    re = [r for r in ledger.rows if r['type'] == 'burst-reaudit']
    assert re and re[-1]['findings'][0]['gate'] == 'admission.control_loss' and re[-1]['findings'][0]['burst'] == 1
    assert 'admission.control_loss' in [x['gate'] for x in ledger.state.candidate] and ledger.state.closed['reason'] == 'FAIL'
    s = v9.smoke_summary(plan, ledger.rows, out=out)
    assert not s['ready'] and any(f['gate'] == 'admission.control_loss' and f.get('burst') == 1 for f in s['findings'])


def test_v94_ten_bursts_fit_the_smoke_part(tmp_path, monkeypatch, plan):
    scenario = {('burst-deferred', k): 0 for k in range(1, 10)}       # covered only at burst 10
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, mode='smoke', parts=2, scenario=scenario)
    bursts = [r for r in ledger.rows if r['type'] == 'burst']
    assert len(bursts) == 10 and bursts[-1]['result'] == 'covered' and ends(ledger)[1]['reason'] == 'complete'
    assert sum(e['wall_s'] for e in ends(ledger)) <= plan['v9']['smoke']['ceiling_s']
    assert v9.smoke_summary(plan, ledger.rows, out=out)['ready']


# ---- v9.7: setup-only audit both directions; exception + incomplete final audit ------------------------

DISPLAY_INIT = EVIDENCE/'v9.4-acceptance-01/part-1/display-init.json'
needs_display_init = pytest.mark.skipif(not DISPLAY_INIT.exists(), reason='retained display-init.json not on this host')
CLEAN_BOOT = ['ESP-ROM:esp32s3-20210327', 'rst:0x15 (USB_UART_CHIP_RESET),boot:0xb (SPI_FAST_FLASH_BOOT)',
              'I (2131) dg-audio: AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000',
              'I (2151) dg-app: STATE Boot gen=0']


def setup_only_part(tmp_path, monkeypatch, plan, *, serial=CLEAN_BOOT, display=None, parts=1):
    """Part 1 pauses at the settled baseline (loud room) before connect; optional candidate serial lines and
    a real-schema display-init snapshot."""
    import echoear_aec_campaign as C
    env = Env(tmp_path, monkeypatch, scenario={('loud', 1): True}); env.p = plan
    if display is not None: env.display_init = display
    def boot(out, label, expected):
        if serial is not None: (Path(out)/'candidate-serial.log').write_text('\n'.join(serial)+'\n')
        return Board(env)
    monkeypatch.setattr(C, 'boot', boot)
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for part in range(1, parts+1):
        if ledger.state.closed: break
        if part > 1: ledger = Ledger(out/'ledger.jsonl')
        env._part = part; env.run_part(out, ledger, part, mode='acceptance')
    return env, out, ledger


def test_v97_clean_setup_only_pause_is_resumable(tmp_path, monkeypatch, plan):
    env, out, ledger = setup_only_part(tmp_path, monkeypatch, plan)
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['audit']['clean'] and 'device.reboot_crash' not in end['gates']
    assert end['restore']['clean'] and ledger.state.closed is None and not ledger.state.candidate


def test_v97_sol_setup_panic_fails_the_candidate(tmp_path, monkeypatch, plan):
    """Sol v9.6 blocker 2: banner, then a panic and reboot at the first setup command, then a setup exception."""
    serial = CLEAN_BOOT+['Guru Meditation Error: Core  1 panic\'ed (LoadProhibited). Exception was unhandled.',
                         'Backtrace: 0x4200abcd:0x3fc9e000', 'Rebooting...',
                         'rst:0xc (RTC_SW_CPU_RST),boot:0xb (SPI_FAST_FLASH_BOOT)']
    env, out, ledger = setup_only_part(tmp_path, monkeypatch, plan, serial=serial, parts=3)
    end = ends(ledger)[0]
    assert 'device.reboot_crash' in end['gates'] and end['reason'] == 'STOP'
    assert 'device.reboot_crash' in [x['gate'] for x in ledger.state.candidate]
    assert ledger.state.closed['reason'] == 'FAIL' and len(ends(ledger)) == 1


@pytest.mark.parametrize('lines', [['rst:0x15 (USB_UART_CHIP_RESET),boot:0xb', 'rst:0xc (RTC_SW_CPU_RST),boot:0xb']+CLEAN_BOOT[2:],
                                   ['rst:0x15 (USB_UART_CHIP_RESET),boot:0xb', 'abort() was called at PC 0x4200']])
def test_v97_crash_before_the_banner_is_a_crash(tmp_path, lines):
    (tmp_path/'candidate-serial.log').write_text('\n'.join(lines)+'\n')
    crash, _ = v9.candidate_crash_lines(tmp_path)
    assert crash


def test_v97_expected_boot_is_not_a_crash(tmp_path):
    (tmp_path/'candidate-serial.log').write_text('\n'.join(CLEAN_BOOT)+'\n')
    assert v9.candidate_crash_lines(tmp_path)[0] == []
    assert v9.candidate_crash_lines(tmp_path/'absent')[0] == []


@needs_display_init
def test_v97_astra_retained_display_init_baseline_pause_is_resumable(tmp_path, monkeypatch, plan):
    """Astra v9.6 blocker 2: the unchanged retained display-init.json (26 tasks, no peer/uplink) with a baseline
    pause. uplink_frame and the other session leaves are not applicable without a live peer."""
    snap = json.loads(DISPLAY_INIT.read_text())
    env, out, ledger = setup_only_part(tmp_path, monkeypatch, plan, display=snap)
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['audit']['clean'] and ledger.state.closed is None
    assert 'hardware.uplink_frame' not in end['gates'] and not ledger.state.candidate
    note = [n for n in end['audit']['notes'] if 'applicable' in n][0]
    assert 'uplink_frame_ms' in note['not_applicable'] and 'internal_minimum_free' in note['applicable']


@needs_display_init
def test_v97_measured_setup_heap_breach_fails(tmp_path, monkeypatch, plan):
    snap = json.loads(DISPLAY_INIT.read_text())
    snap['heap']['internal']['minimum_free'] = 70000            # measured below the 81920 floor during setup
    env, out, ledger = setup_only_part(tmp_path, monkeypatch, plan, display=snap, parts=3)
    end = ends(ledger)[0]
    assert end['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'
    assert any(x['gate'].startswith('resources.') for x in ledger.state.candidate)


def test_v97_astra_exception_with_incomplete_final_audit_stays_unresolved(tmp_path, monkeypatch, plan):
    """Astra v9.6 blocker 3: LinkProbeFailed, then finalization raises after passing resources are populated."""
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('link', 1): True, ('finish-raise', 1): True})
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and 'resources.part_audit' in end['gates'] and not end['audit']['clean']
    missing = [f for f in end['audit']['findings'] if f['gate'] == 'resources.part_audit'][0]['missing']
    assert any(m.startswith('finish_error') for m in missing) and 'sctp_recovery' in missing
    assert ledger.state.closed is None or ledger.state.closed['reason'] != 'PASS'
    assert verdict(ledger.state, plan)['outcome'] != 'PASS'


def test_v97_exception_with_complete_final_audit_recovers(tmp_path, monkeypatch, plan):
    """The intended recovery: acquisition fails (link probe), completed=false, but every final-audit stage has its
    completion witness and finish raised nothing -> the part audit is clean and the campaign can PASS."""
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('link', 1): True, ('final', 1): dict(completed=False)})
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['gates'] == ['link_probe.part'] and end['audit']['clean']
    assert ledger.state.closed['reason'] == 'PASS'


def test_v97_ambiguous_witness_pauses(tmp_path, monkeypatch, plan):
    # v9.10: an ambiguous witness is retaken; three ambiguous acquisitions at one position pause the part.
    bad = {('witness-invalid', f'v9-witness-p1-0{i}'): True for i in (1, 2, 3)}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario=bad)
    assert ends(ledger)[0]['gates'] == ['alignment.near_witness'] and ends(ledger)[0]['reason'] == 'PAUSE-PART'
    tmp2 = tmp_path/'one'; tmp2.mkdir()
    env, out, ledger = campaign(tmp2, monkeypatch, plan, scenario={('witness-invalid', 'v9-witness-p1-01'): True})
    notes = [r['note'] for r in ledger.rows if r['type'] == 'drain' and r.get('note')]
    assert 'p1/p1-b1/open: witness retake 2/3 (alignment)' in notes and ledger.state.closed['reason'] == 'PASS'


# ---- v9.8: reset exemption by cause and phase; late final-stage faults ---------------------------------------

@needs_display_init
def test_v98_astra_wdt_first_reset_after_candidate_output_fails(tmp_path, monkeypatch, plan):
    """Astra v9.7 1: the initial USB reset is missing from the capture, candidate startup is captured, then a
    TG1WDT reset before AEC init. The first reset line is not the expected boot: sticky device.reboot_crash."""
    serial = ['I (1336) app_init: Application information:', 'I (1337) app_init: Project name:     dg-esp32',
              'I (1338) app_init: ELF file SHA256:  cbabb650b...', 'ESP-ROM:esp32s3-20210327',
              'rst:0x8 (TG1WDT_SYS_RST),boot:0xb (SPI_FAST_FLASH_BOOT)',
              'I (2131) dg-audio: AEC_GUARD_CONFIG guard_ms=400 afe_delay_samples=1024 rearm_ms=2000']
    env, out, ledger = setup_only_part(tmp_path, monkeypatch, plan, serial=serial, display=json.loads(DISPLAY_INIT.read_text()), parts=3)
    end = ends(ledger)[0]
    assert 'device.reboot_crash' in end['gates'] and end['reason'] == 'STOP'
    assert ledger.state.closed['reason'] == 'FAIL' and len(ends(ledger)) == 1


@pytest.mark.parametrize('first', ['rst:0x8 (TG1WDT_SYS_RST),boot:0xb (SPI_FAST_FLASH_BOOT)',
                                   'rst:0xc (RTC_SW_CPU_RST),boot:0xb (SPI_FAST_FLASH_BOOT)',
                                   'rst:0xf (BROWN_OUT_RST),boot:0xb (SPI_FAST_FLASH_BOOT)'])
def test_v98_non_host_reset_as_first_line_is_a_crash(tmp_path, first):
    (tmp_path/'candidate-serial.log').write_text('\n'.join(['ESP-ROM:esp32s3-20210327', 'Build:Mar 27 2021', first]+CLEAN_BOOT[2:])+'\n')
    assert v9.candidate_crash_lines(tmp_path)[0] == [first]


def test_v98_repeated_guard_config_lines_are_not_boots(tmp_path):
    lines = ['ESP-ROM:esp32s3-20210327', 'Build:Mar 27 2021', CLEAN_BOOT[1]]+CLEAN_BOOT[2:]+[CLEAN_BOOT[2]]*3
    (tmp_path/'candidate-serial.log').write_text('\n'.join(lines)+'\n')
    assert v9.candidate_crash_lines(tmp_path)[0] == []


@pytest.mark.skipif(not (EVIDENCE/'v9.4-acceptance-01/part-1/candidate-serial.log').exists(), reason='retained logs not on this host')
def test_v98_retained_candidate_logs_have_the_expected_boot_only():
    logs = sorted(EVIDENCE.glob('*/part-*/candidate-serial.log'))+sorted(EVIDENCE.glob('*/qualification-01/candidate-serial.log'))
    assert len(logs) >= 16
    for log in logs:
        crash, note = v9.candidate_crash_lines(log.parent)
        assert crash == [] and note is None, (log, crash, note)


def test_v98_astra_late_i2s_write_error_fails(tmp_path, monkeypatch, plan):
    """Astra v9.7 2: LinkProbeFailed; exception and resource audits pass; the disconnected snapshot records
    i2s_write_errors=1 and require_zero_i2s raises in finish. hardware.audio_io is CANDIDATE-FAIL, not merely an
    incomplete audit."""
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('link', 1): True, ('late-i2s', 1): True})
    end = ends(ledger)[0]
    assert 'hardware.audio_io' in end['gates'] and end['reason'] == 'STOP'
    assert ledger.state.closed['reason'] == 'FAIL' and 'hardware.audio_io' in [x['gate'] for x in ledger.state.candidate]


def test_v98_timeout_after_resources_stays_unresolved_not_fail(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('link', 1): True, ('finish-raise', 1): True})
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and 'resources.part_audit' in end['gates'] and not ledger.state.candidate


# ---- v9.9: terminal-snapshot reconciliation (Sol v9.8) -----------------------------------------------------

@pytest.mark.parametrize('late,gate,cls', [({'rx_overflows': 1}, 'hardware.audio_io', 'FAIL'),
                                           ({'transport_drops': 1}, 'hardware.console', 'PAUSE'),
                                           ({'overflow': 1}, 'admission.control_loss', 'FAIL')])
def test_v99_sol_late_terminal_fault_is_dispatched(tmp_path, monkeypatch, plan, late, gate, cls):
    """Sol v9.8: a counter advances after settled-end, before the terminal disconnected snapshot. Startup RX count
    123 (connected 123, settled-end 123, disconnected 123 + late). The late fault is dispatched with its class."""
    env = Env(tmp_path, monkeypatch, scenario={('late', 1): late}); env.p = plan; env.perf['rx_overflows'] = 123
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for part in (1, 2, 3):
        if ledger.state.closed: break
        if part > 1: ledger = Ledger(out/'ledger.jsonl')
        env.run_part(out, ledger, part, mode='acceptance')
    end = ends(ledger)[0]
    assert gate in end['gates'], end['gates']
    if cls == 'FAIL':
        assert end['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL' and gate in [x['gate'] for x in ledger.state.candidate]
    else:
        assert end['reason'] == 'PAUSE-PART' and not end['audit']['clean'] and not ledger.state.candidate
        assert verdict(ledger.state, plan)['outcome'] != 'PASS'


def test_v99_unchanged_startup_rx_and_clean_disconnect_pass(tmp_path, monkeypatch, plan):
    """Controls: startup rx_overflows 123 unchanged through the terminal snapshot stays MONITOR; a clean disconnect
    is not a failure (phase exclusions after teardown)."""
    env = Env(tmp_path, monkeypatch); env.p = plan; env.perf['rx_overflows'] = 123
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for part in (1, 2):
        if part > 1: ledger = Ledger(out/'ledger.jsonl')
        env.run_part(out, ledger, part, mode='acceptance')
    assert [e['reason'] for e in ends(ledger)] == ['complete', 'complete'] and all(e['audit']['clean'] for e in ends(ledger))
    assert ledger.state.closed['reason'] == 'PASS'
    assert json.loads((out/'part-1'/'disconnected.json').read_text())['aec']['capture']['rx_overflows'] == 123


@pytest.mark.skipif(not (EVIDENCE/'v9.4-smoke-01/part-1/disconnected.json').exists(), reason='retained parts not on this host')
def test_v99_retained_terminal_snapshots_reconcile_clean():
    """Real-schema control: every retained v9 part with a disconnected snapshot reconciles clean through it."""
    parts = [d for d in sorted(EVIDENCE.glob('v9*-*/part-*')) if (d/'disconnected.json').exists()]
    assert len(parts) >= 8
    for d in parts:
        assert v9.late_stage_findings(d) == [], d


# ---- v9.10: witness retake ------------------------------------------------------------------------------

def test_v910_noisy_opening_witness_is_retaken(tmp_path, monkeypatch, plan):
    """v9.9-smoke-01 shape: the opening witness aligned but its quiet windows were noisy (INVALID-CELL, RETAKE).
    v9.10 retakes it; the campaign completes PASS and the witness never counts toward the invalid cap."""
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', 'v9-witness-p1-01', 1): 'noisy'})
    rows = attempts(ledger)
    assert rows[0]['logical_id'] == 'v9-witness-p1-01' and rows[1]['block_key'] == 'p1/p1-b1/open#2'
    assert ledger.state.attempts[rows[0]['attempt_id']]['classification']['action'] == 'RETAKE'
    notes = [r['note'] for r in ledger.rows if r['type'] == 'drain' and r.get('note')]
    assert 'p1/p1-b1/open: witness retake 2/3 (noise)' in notes
    assert ledger.state.closed['reason'] == 'PASS' and ledger.state.invalid == 0


def test_v910_noisy_closing_witness_keeps_the_block_open_until_valid(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', 'v9-witness-p1-02', 1): 'noisy'})
    closes = [r for r in ledger.rows if r['type'] == 'block-close']
    first = closes[0]
    rows = {r['attempt_id']: r for r in attempts(ledger)}
    assert rows[first['witnesses'][1]]['block_key'] == 'p1/p1-b1/close#2' and first['closed']
    # the block's cells had no final classification before the valid closing witness row
    seq_close = first['seq']
    retake = [r for r in attempts(ledger) if r['block_key'] == 'p1/p1-b1/close#2'][0]
    assert retake['seq'] < seq_close and ledger.state.closed['reason'] == 'PASS'


def test_v910_three_noisy_closing_acquisitions_leave_the_block_unclosed(tmp_path, monkeypatch, plan):
    noisy = {('score', f'v9-witness-p1-0{i}', 1): 'noisy' for i in (2, 3, 4)}
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, parts=1, scenario=noisy)
    first = [r for r in ledger.rows if r['type'] == 'block-close'][0]
    assert first['closed'] is False and ends(ledger)[0]['gates'] == ['alignment.near_witness']
    keys = [r['block_key'] for r in attempts(ledger) if r['kind'] == 'near-witness']
    assert keys == ['p1/p1-b1/open', 'p1/p1-b1/close', 'p1/p1-b1/close#2', 'p1/p1-b1/close#3']
    # no credit without a valid closing witness: the block's scored attempts are finalized unclosed
    scored = [r for r in attempts(ledger) if r['kind'] != 'near-witness']
    assert scored and all(ledger.state.attempts[r['attempt_id']]['final']['block_closed'] is False for r in scored)


def test_v910_stop_class_finding_in_a_witness_stops_at_once(tmp_path, monkeypatch, plan):
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('score', 'v9-witness-p1-01', 1): 'fault'})
    assert len([r for r in attempts(ledger) if r['kind'] == 'near-witness']) == 1
    assert ends(ledger)[0]['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'


# ---- v9.10: terminal-phase inventory -------------------------------------------------------------------------

def test_v910_terminal_inventory_is_complete():
    """Every leaf of the live checker (check_snapshots, check_directory extras, delivery counters, admission) and
    every resources:* registry term has a terminal-phase entry (APPLICABLE with its rule, or NOT_APPLICABLE)."""
    from echoear_aec_resources import check_snapshots
    import echoear_aec_gate_registry as reg
    snap = json.loads((EVIDENCE/'v9.8-smoke-01/part-1/disconnected.json').read_text()) \
        if (EVIDENCE/'v9.8-smoke-01/part-1/disconnected.json').exists() else None
    leaves = set(check_snapshots([snap]) if snap else {}) | {'live_rx_overflows', 'cat', 'display', 'liveness',
                                                             'send_errors', 'send_budget_cutoffs', 'drop_reasons'}
    leaves |= {t.split(':', 1)[1] for g in reg.REGISTRY for t in g['terms'] if t.startswith('resources:')}
    missing = sorted(leaves-set(v9.TERMINAL_INVENTORY))
    assert not missing, missing
    assert all(v[0] in ('APPLICABLE', 'NOT_APPLICABLE') and v[1] for v in v9.TERMINAL_INVENTORY.values())
    # every delivery-counter path of the live delivery rule is covered
    from echoear_aec_campaign_analysis import delivery_counters
    paths = delivery_counters(dict(drop_reasons={'x': 0}), dict(drop_reasons={'x': 0}))['delta']
    alias = {'aec.capture.rx_overflows': 'live_rx_overflows'}
    assert {('drop_reasons' if k.startswith('drop_reasons') else alias.get(k, k)) for k in paths} <= set(v9.TERMINAL_INVENTORY)


def _terminal_campaign(tmp_path, monkeypatch, plan, **knobs):
    env = Env(tmp_path, monkeypatch, scenario=knobs.pop('scenario', {})); env.p = plan
    for k, v in knobs.items(): setattr(env, k, v)
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for part in (1, 2, 3):
        if ledger.state.closed: break
        if part > 1: ledger = Ledger(out/'ledger.jsonl')
        env.run_part(out, ledger, part, mode='acceptance')
    return env, out, ledger


def _with(task, free):
    t = copy.deepcopy(TERMINAL_TASKS)
    for x in t:
        if x['name'] == task: x['stack_free_bytes'] = free
    return t


@pytest.mark.parametrize('knobs,gate,cls', [
    (dict(terminal_tasks=_with('app-control', 1024)), 'resources.stacks', 'FAIL'),          # Sol v9.9
    (dict(terminal_tasks=_with('app-control', 2044)), 'resources.stacks', 'FAIL'),          # Astra v9.9
    (dict(terminal_display=dict(dma_errors=1)), 'resources.display', 'FAIL'),               # Sol v9.9
    (dict(scenario={('late', 1): {'send_budget_cutoffs': 1}}), 'delivery.firmware_drops', 'FAIL'),   # Astra v9.9
    (dict(scenario={('late', 1): {'send_errors': 1}}), 'delivery.send_errors', 'PAUSE'),             # Astra v9.9
], ids=['sol-stack', 'astra-stack', 'sol-display', 'astra-cutoff', 'astra-send-errors'])
def test_v910_late_terminal_leaf_is_dispatched(tmp_path, monkeypatch, plan, knobs, gate, cls):
    env, out, ledger = _terminal_campaign(tmp_path, monkeypatch, plan, **knobs)
    end = ends(ledger)[0]
    assert gate in end['gates'], end['gates']
    if cls == 'FAIL':
        assert end['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'
    else:
        assert end['reason'] == 'PAUSE-PART' and not end['audit']['clean'] and not ledger.state.candidate
        assert verdict(ledger.state, plan)['outcome'] != 'PASS'


def test_v910_clean_terminal_and_exited_task_controls_pass(tmp_path, monkeypatch, plan):
    # an exited task (peer-loop, peer-rx absent from the terminal census) is not applicable, not a failure
    exited = [t for t in TERMINAL_TASKS if t['name'] not in ('peer-loop', 'peer-rx')]
    env, out, ledger = _terminal_campaign(tmp_path, monkeypatch, plan, terminal_tasks=exited)
    assert all(e['audit']['clean'] for e in ends(ledger)) and ledger.state.closed['reason'] == 'PASS'
    assert v9.late_stage_findings(out/'part-1') == []


@pytest.mark.parametrize('edit,expected', [
    (lambda c, x: x['drop_reasons'].__setitem__('invalid_data', 0) or c['drop_reasons'].__setitem__('invalid_data', 7), ['admission.counter_schema']),
    (lambda c, x: x['drop_reasons'].__setitem__('invalid_data', None), ['admission.counter_schema']),
    (lambda c, x: x['drop_reasons'].__setitem__('message_queue', None) or x['drop_reasons'].__setitem__('admission_overflow', 1), ['admission.control_loss']),
    (lambda c, x: x.__setitem__('send_errors', None), ['admission.counter_schema']),
    (lambda c, x: c.__setitem__('send_budget_cutoffs', 3) or x.__setitem__('send_budget_cutoffs', 2), ['admission.counter_schema']),
    (lambda c, x: x.__setitem__('generation', 2), ['admission.counter_schema']),
], ids=['reset', 'null', 'loss-beside-malformed', 'send-errors-null', 'cutoff-decrease', 'peer-epoch'])
def test_v910_terminal_interval_shared_validation(tmp_path, edit, expected):
    """Astra 2 / Sol 3: terminal deltas use the admission_interval validity rules; a reset, null or decrease is
    admission.counter_schema (never zero); an independently positive loss is preserved beside a malformed field."""
    import echoear_aec_v9_rules as r
    base = dict(pc_id='pc-1', generation=1, data_admission=dict(received=10, dispatched=10, deferred=0),
                drop_reasons={k: 0 for k in r.DROP_REASONS}, send_errors=0, send_budget_cutoffs=0,
                aec=dict(capture=dict(rx_overflows=2)))
    snap = copy.deepcopy(base)
    edit(base, snap)
    gates = [f['gate'] for f in v9.terminal_interval(base, snap)]
    for g in expected: assert g in gates, gates
    if expected == ['admission.control_loss']: assert 'admission.control_loss' in gates


# ---- v9.11: peer loss mid-acquisition with an unacknowledged evaluation control ----------------------------

def test_v911_peer_loss_with_unacknowledged_control_resolves_and_campaign_passes(tmp_path, monkeypatch, plan):
    """v9.10-acceptance-01 part 1: a real link outage mid-acquisition. The part pauses (PAUSE-PART), the device-side
    final-audit stages run and record their witnesses, the bot-side stages are not applicable, the part audit is
    clean, and a 2-part-plus-recovery campaign can still PASS."""
    cell = plan['v9']['schedule'][0]['blocks'][1]['attempts'][0]
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={(cell, 1): 'peer-loss'}, census=True)
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['audit']['clean'], end['audit']
    r = json.loads((out/'part-1'/'result.json').read_text())
    assert r['peer_lost']['generation'] == 1 and r['peer_lost']['device'] and not r.get('finish_error')
    for k in ('resources', 'exception_integrity_audit', 'sctp_recovery'): assert r.get(k) is not None, k
    assert (out/'part-1'/'disconnected.json').exists() and r['exception_integrity_audit']['all_passed']
    note = [n for n in end['audit']['notes'] if 'exit_interval_deltas' in n][0]
    d = note['exit_interval_deltas']
    assert d['drop_reasons.send_cutoff']['delta'] == 240 and d['drop_reasons.send_cutoff']['class'].startswith('exit-associated')
    assert d['send_budget_cutoffs']['delta'] == 139 and d['send_errors']['delta'] == 0 and 'data_admission.received' in d
    assert ledger.state.closed['reason'] == 'PASS' and len(ends(ledger)) == 3


def test_v911_peer_loss_still_dispatches_a_positive_late_fault(tmp_path, monkeypatch, plan):
    """Peer loss does not weaken the terminal audit: a present-task stack breach in the disconnected snapshot
    still dispatches resources.stacks and fails the candidate."""
    cell = plan['v9']['schedule'][0]['blocks'][1]['attempts'][0]
    env = Env(tmp_path, monkeypatch, scenario={(cell, 1): 'peer-loss'}); env.p = plan
    env.terminal_tasks = _with('app-control', 1024); env.census = True
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    env.run_part(out, ledger, 1, mode='acceptance')
    end = ends(ledger)[0]
    assert 'resources.stacks' in end['gates'] and end['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'


def test_v911_unacknowledged_control_on_a_live_peer_stays_unresolved(tmp_path, monkeypatch, plan):
    """No positive peer-loss witness: the existing finish path; a finalization failure stays resources.part_audit."""
    env, out, ledger = campaign(tmp_path, monkeypatch, plan, scenario={('link', 1): True, ('finish-raise', 1): True})
    end = ends(ledger)[0]
    assert v9.peer_loss_witness(type('H', (), dict(out=out/'part-1', events=lambda self: []))()) is None
    assert 'resources.part_audit' in end['gates'] and not end['audit']['clean']


# ---- v9.12: lost-peer audit partitioned by phase; narrow exit rule; hardened witness ---------------------------

def _loss_campaign(tmp_path, monkeypatch, plan, *, cell_index=(1, 0), parts=3, **knobs):
    blocks = plan['v9']['schedule'][0]['blocks']
    cell = blocks[cell_index[0]]['attempts'][cell_index[1]]
    env = Env(tmp_path, monkeypatch, scenario={(cell, 1): 'peer-loss'}, census=True); env.p = plan
    for k, v in knobs.items(): setattr(env, k, v)
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    for part in range(1, parts+1):
        if ledger.state.closed: break
        if part > 1: ledger = Ledger(out/'ledger.jsonl')
        env._part = part; env.run_part(out, ledger, part, mode='acceptance')
    return env, out, ledger


def _settled(fn):
    def apply(value): fn(value)
    return apply


def test_v912_sol_exited_task_stack_breach_in_new_generation_settled_end_fails(tmp_path, monkeypatch, plan):
    """Sol v9.11: peer-loop stack 1024 B in the reconnected generation's settled-end; the task then exits before the
    disconnected census. The measured breach must survive: resources.stacks, FAIL."""
    def edit(v):
        for t in v['tasks']:
            if t['name'] == 'peer-loop': t['stack_free_bytes'] = 1024
    exited = [t for t in TERMINAL_TASKS if t['name'] != 'peer-loop']
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, settled_override=edit, terminal_tasks=exited)
    end = ends(ledger)[0]
    assert 'resources.stacks' in end['gates'] and ledger.state.closed['reason'] == 'FAIL', end['gates']


def test_v912_astra_largest_block_breach_in_new_generation_settled_end_fails(tmp_path, monkeypatch, plan):
    def edit(v): v['heap']['internal']['largest'] = 32767
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, settled_override=edit)
    end = ends(ledger)[0]
    assert 'resources.heap' in end['gates'] and ledger.state.closed['reason'] == 'FAIL', end['gates']


@pytest.mark.parametrize('extra', [{'received': 1}, {}], ids=['received-101-dispatched-100', 'balanced'])
def test_v912_undrained_capture_end_does_not_block_recovery(tmp_path, monkeypatch, plan, extra):
    """Sol/Astra v9.11: the final capture-end catches an in-flight message (received one ahead of dispatched), no loss.
    Dispatch equality is validated at the drained before boundary, not at the lost-peer observation."""
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, loss_capture_end=extra)
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['audit']['clean'], end['audit']['findings']
    r = json.loads((out/'part-1'/'result.json').read_text())
    assert r['exception_integrity_audit']['completed'] is True
    assert ledger.state.closed['reason'] == 'PASS'


def test_v912_admission_overflow_in_the_exit_interval_fails(tmp_path, monkeypatch, plan):
    """Sol/Astra v9.11: admission_overflow=1 in the final capture-end stays admission.control_loss beside a peer loss."""
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, loss_capture_end={'overflow': 1})
    end = ends(ledger)[0]
    assert 'admission.control_loss' in end['gates'] and end['reason'] == 'STOP' and ledger.state.closed['reason'] == 'FAIL'


def test_v912_earlier_positive_loss_stays_sticky_after_a_later_peer_loss(tmp_path, monkeypatch, plan):
    """A send_cutoff delta in an earlier, completed attempt is classified strictly by that attempt (and the strict
    part interval); a later peer loss does not waive it."""
    blocks = plan['v9']['schedule'][0]['blocks']
    early, cell = blocks[0]['attempts'][0], blocks[1]['attempts'][0]
    env = Env(tmp_path, monkeypatch, scenario={(cell, 1): 'peer-loss', (early, 1): 'send-cutoff'}, census=True); env.p = plan
    out = tmp_path/'out'; out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='acceptance', plan_sha256=REVIEWED, runtime_sha256={}, drain_allowance_s=1.0))
    env.run_part(out, ledger, 1, mode='acceptance')
    assert ledger.state.closed['reason'] == 'FAIL'
    assert any(x['gate'] in ('delivery.firmware_drops', 'admission.control_loss') for x in ledger.state.candidate)


def test_v912_loss_on_the_fifth_attempt_of_an_open_block_recovers(tmp_path, monkeypatch, plan):
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, cell_index=(0, 4))
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['audit']['clean'], end['audit']['findings']
    assert ledger.state.closed['reason'] == 'PASS'


def test_v912_loss_without_a_reconnect_recovers(tmp_path, monkeypatch, plan):
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, reconnect=False)
    end = ends(ledger)[0]
    assert end['reason'] == 'PAUSE-PART' and end['audit']['clean'], end['audit']['findings']
    assert ledger.state.closed['reason'] == 'PASS'


@pytest.mark.parametrize('witness,sources', [('bot', ['bot']), ('device', ['device']), ('both', ['bot', 'device'])])
def test_v912_single_source_witnesses_are_accepted(tmp_path, monkeypatch, plan, witness, sources):
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, parts=1, loss_witness=witness)
    r = json.loads((out/'part-1'/'result.json').read_text())
    assert r['peer_lost']['sources'] == sources and ends(ledger)[0]['audit']['clean']


@pytest.mark.parametrize('knobs', [dict(loss_witness='evaluation-end-only'), dict(loss_witness='spoof'),
                                   dict(loss_witness='bot', owner_pc='pc-9')],
                         ids=['evaluation-end-only', 'server-payload-spoof', 'wrong-owner'])
def test_v912_insufficient_witnesses_are_rejected(tmp_path, monkeypatch, plan, knobs):
    """evaluation-end alone, a SESSION FAIL text inside a logged SERVER payload, and bot-only evidence without the
    owner/start binding are not peer-loss witnesses: the existing (unresolved) finish path applies."""
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, parts=1, **knobs)
    r = json.loads((out/'part-1'/'result.json').read_text())
    assert not r.get('peer_lost')
    assert not ends(ledger)[0]['audit']['clean']


def test_v912_failure_of_a_device_side_stage_stays_unresolved(tmp_path, monkeypatch, plan):
    """A device-side final stage that fails (here the exception-integrity audit raises on a serial read error) is a
    genuine finish_error: the part audit stays unresolved, and a recovery cannot reach PASS."""
    def broken(out, lost): raise OSError('serial read failed during the final audit')
    monkeypatch.setattr(v9, 'lost_peer_audit', broken)
    env, out, ledger = _loss_campaign(tmp_path, monkeypatch, plan, parts=1)
    end = ends(ledger)[0]
    assert 'resources.part_audit' in end['gates'] and not end['audit']['clean']
    missing = [f for f in end['audit']['findings'] if f['gate'] == 'resources.part_audit' and f.get('missing')][0]['missing']
    assert any(m.startswith('finish_error') for m in missing)


def test_v912_live_peer_unacknowledged_control_through_the_real_settle_path(tmp_path, monkeypatch):
    """No peer-loss witness (live peer): V9Hold.finish keeps the BenchHold path, and the real settle_attempt raises
    'Evaluation control remains unacknowledged' when the in-flight control never gets its acknowledgement."""
    import echoear_aec_campaign as C
    clock = Clock()
    monkeypatch.setattr(C, 'time', types.SimpleNamespace(sleep=lambda s: clock.advance(s), monotonic=clock, monotonic_ns=lambda: int(clock()*1e9)))
    cls = v9.hold_type(FakeBudget(clock, dict(active_s=100, total_s=200)))
    hold = object.__new__(cls)
    out = tmp_path/'part'; out.mkdir()
    (out/'connected.json').write_text(json.dumps(dict(pc_id='pc-1', generation=1, device_us=1)))
    hold.out, hold.evaluation, hold.bot = out, out/'evaluation', types.SimpleNamespace(stop=lambda: None)
    hold.result = dict(error='HarnessFailure: Evaluation command not acknowledged', error_class='device-fault')
    hold.attempt_cases, hold.number, hold.attempt_command_number = ['c1'], 7, 6
    hold.events = lambda case_id=None: [dict(event='client-connected', pc_id='pc-1', t=1.0)]   # live: no disconnect
    assert v9.peer_loss_witness(hold) is None
    with pytest.raises(RuntimeError, match='Evaluation control remains unacknowledged'):
        hold.finish()
