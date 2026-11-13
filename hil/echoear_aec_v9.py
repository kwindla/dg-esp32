"""v9 acceptance runner (docs/echoear-aec-acceptance-v9.md, "Runner and operator").

prepare/validate/analyze/dry-run/benchmark are file-only. `run` (part 1) and `resume` (part 2 and at
most one recovery part) open the bench and require the reviewed plan SHA256, a USER-GO file holding
it and an interactive TTY; `smoke-run`/`smoke-resume` need the reviewed SHA and a TTY only (standing
smoke permission, decision 70d). Every part is one process: fresh boot, runner-owned bench lock
(BenchHold; never an outer flock), normal-image restore and mixer restore on every exit. Each
acquired attempt is scored once (O(1)) and appended to the hash-chained ledger; the verdict is a pure
fold of the ledger.
"""
import argparse
import contextlib
import copy
import json
import re
import math
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import echoear_aec_v9_plan as plan_mod
import echoear_aec_v9_rules as rules
import echoear_aec_gate_registry as registry
from echoear_aec_v9_ledger import Ledger, verdict, fold
from echoear_aec_bench import ROOT, NORMAL_HASH, sha, write
from echoear_aec_bench import HISTORICAL_NORMAL_9936DA45_HASH as NORMAL_HASH   # gap 3: step-9 AEC family stays bound to its declared 9936da45 restore

REVIEW_ENV = 'AEC_V9_REVIEWED_PLAN_SHA256'
MODULE = 'hil/echoear_aec_v9.py'
LAUNCH_ENV = ['OPENBLAS_NUM_THREADS=1', 'PYTHONPATH=hil:server']
UV_RUN = ['uv', 'run', '--project', 'server', '--frozen', '--offline',
          '--with', 'pyserial', '--with', 'pyelftools', '--with', 'idf-component-manager==3.1.2']
ESPTOOL_PYTHON = '/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'
TERM_OWNER = {t: g['id'] for g in registry.REGISTRY for t in g['terms']}
# Exceptions raised by BenchHold.capture after the attempt's evidence is complete: the attempt is
# scored from its folder and the composite is dispatched to its leaves.
POST_ACQUISITION = {'Capture-interval or integrity failure; v8.5 has no link retake': None,
                    'Invalid capture/quiet windows; acquisition failure': None,
                    'Observer health failure': None,
                    'Incomplete long-TTS generation witnesses': 'tts.generation_witness',
                    'Missing or changed Nova profile/vendor': 'bot.profile_binding'}


class Stop(Exception):
    """Controlled end of acquisition in this part (PAUSE-PART, STOP or INCONCLUSIVE)."""
    def __init__(self, reason, gates=()):
        super().__init__(reason); self.reason = reason; self.gates = list(gates)


# ---- launch / identity --------------------------------------------------------------------------

def launch_argv(*args):
    return ['env', *LAUNCH_ENV, *UV_RUN, 'python', MODULE, *args]


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def check_launch(plan, reviewed_sha, *, mode, tty=None):
    """Before any validation, output creation, lock or hardware."""
    plan = Path(plan).resolve()
    if plan != (plan_mod.DESTINATION/'plan.json').resolve(): raise ValueError('Plan is not the declared v9 freeze bundle')
    if sha(plan) != reviewed_sha: raise ValueError('Reviewed plan SHA required')
    if mode == 'acceptance':
        marker = plan_mod.DESTINATION/'USER-GO'
        if not marker.is_file() or marker.read_text().strip() != reviewed_sha:
            raise ValueError('USER-GO containing the reviewed plan SHA required')
    if not (interactive() if tty is None else tty): raise ValueError('Interactive user terminal required')
    return json.loads(plan.read_text())


# ---- decision-71 drain boundary -----------------------------------------------------------------

QUIESCENT_SPACING_S = 0.25      # frozen: two quiescent observations at least this far apart (request times)
CONFIRMATION_S = 1.0             # frozen: the confirming observation must be received by deadline + this


def drain_boundary(board, folder, label, *, allowance_s, snapshot=None, clock=time.monotonic, sleep=time.sleep,
                   spacing_s=QUIESCENT_SPACING_S, ready=None):
    """Quiescent admission boundary with the frozen drain allowance (smoke finding S2).

    Quiescence is SUSTAINED: received == dispatched at two observations whose PERF requests are at least
    spacing_s apart, with no backlog between them (the single-instant equality of v9 is withdrawn). The
    deadline applies to reaching the first observation of the final pair. ready(observation) is an extra
    condition (the burst passes the sender's declared count). Never raises on a slow drain: every
    observation (request/receipt host time, device_us, counters) and the outcome are retained."""
    if snapshot is None:
        from echoear_aec_resource_trial import snapshot
    observations, samples, deadline, status, run_start = [], [], None, None, None
    identity = None
    while True:
        requested = clock()
        value = snapshot(board, folder, f'{label}-boundary-{len(samples)}'); now = clock()
        samples.append(value)
        da = value.get('data_admission', {}) if isinstance(value, dict) else {}
        current = (value.get('pc_id'), value.get('generation'))
        identity = identity or current
        obs = dict(t=now, requested_s=requested, device_us=value.get('device_us'), pc_id=current[0], generation=current[1],
                   received=da.get('received'), dispatched=da.get('dispatched'), deferred=da.get('deferred'),
                   deferred_depth=da.get('deferred_depth'))
        observations.append(obs)
        if deadline is None: deadline = now+allowance_s
        r, d = da.get('received'), da.get('dispatched')
        if current != identity or None in current or type(r) is not int or type(d) is not int or d > r:
            status = 'schema'; break
        quiet = r == d and (ready is None or ready(obs))
        # Deadlines use the observation RECEIPT time (a request before the deadline does not prove the
        # counters were sampled before it): the first member of the pair by the allowance deadline, the
        # confirming member by deadline + CONFIRMATION_S (Astra v9.1 blocker 4).
        if quiet and run_start is not None and requested-run_start >= spacing_s:
            if now <= deadline+CONFIRMATION_S: status = 'quiescent'; break
            status = 'drain-timeout'; break
        if quiet and run_start is None:
            if now <= deadline: run_start = requested
            else: status = 'drain-timeout'; break
        elif not quiet:
            run_start = None
            if now >= deadline: status = 'drain-timeout'; break
        sleep(max(0, spacing_s-(now-run_start)) if run_start is not None else min(.02, max(0, deadline-now)))
    arrivals = [o['t'] for a, o in zip(observations, observations[1:]) if o['received'] != a['received']]
    quiescent = [o for o in observations if o['received'] == o['dispatched']]
    record = dict(label=label, allowance_s=allowance_s, spacing_s=spacing_s, confirmation_s=CONFIRMATION_S,
                  deadline_s=deadline, status=status, observations=observations,
                  first_s=observations[0]['t'], first_requested_s=observations[0]['requested_s'],
                  backlog_observed=any(o['received'] != o['dispatched'] for o in observations),
                  received_unchanged=len({o['received'] for o in observations[-2:]}) == 1,
                  quiescent_s=observations[-1]['t'] if status == 'quiescent' else None,
                  first_quiescent_s=quiescent[0]['t'] if quiescent else None,
                  last_observed_arrival_s=max(arrivals) if arrivals else None)
    write(Path(folder)/f'{label}-drain.json', record)
    write(Path(folder)/(label+'.json'), samples[-1])
    print('BOUNDARY', label, status, json.dumps(observations[-1]), flush=True)
    return samples[-1], samples


def drain_latency(d, recorded=()):
    """Conservative last-arrival-to-quiescence bound for the drain calibration (Sol 2). No backlog seen:
    0. Backlog seen: quiescence minus a witnessed lower anchor of the last arrival, which includes PERF and
    polling I/O: the last host-side RTVI send before the first PERF request (a send precedes its arrival),
    else the observation preceding the last observed receive change. Without an anchor the bound is
    unbounded (None) and readiness is withheld; zero is never substituted for an unknown arrival."""
    out = dict(status=d['status'], backlog_observed=d.get('backlog_observed'))
    if d['status'] != 'quiescent':
        return dict(out, latency_s=None, unbounded=d['status'] != 'quiescent')
    if not d.get('backlog_observed', True):
        # No backlog witnessed: the measured value is the polling/PERF I/O of the sustained-quiescence pair.
        return dict(out, latency_s=d['quiescent_s']-d.get('first_requested_s', d['first_s']),
                    basis='no backlog; polling/PERF I/O of the quiescence pair')
    first_request = d.get('first_requested_s', d['first_s'])
    sends = [e['monotonic_ns']/1e9 for e in recorded if e.get('kind') == 'rtvi-output' and e.get('source') == 'RTVIProcessor'
             and e.get('direction') == 'DOWNSTREAM' and e.get('monotonic_ns', 0)/1e9 <= first_request]
    if sends: return dict(out, latency_s=d['quiescent_s']-max(sends), basis='last recorder send before the first PERF request')
    obs = d['observations']
    changes = [i for i in range(1, len(obs)) if obs[i]['received'] != obs[i-1]['received']]
    if changes: return dict(out, latency_s=d['quiescent_s']-obs[changes[-1]-1].get('requested_s', obs[changes[-1]-1]['t']),
                            basis='observation preceding the last receive change')
    return dict(out, latency_s=None, unbounded=True, basis='backlog without an arrival anchor')


@contextlib.contextmanager
def patched_campaign(allowance_s, burst=None, *, clock=time.monotonic, sleep=time.sleep):
    """Route the inherited acquisition through the v9 drain boundary (the withdrawn 0.25 s rule is
    never used) and, for the smoke, declare the bounded burst in the bot protocol."""
    import echoear_aec_campaign as campaign
    old = (campaign.admission_boundary, campaign.protocol)
    def boundary(board, folder, label): return drain_boundary(board, folder, label, allowance_s=allowance_s, clock=clock, sleep=sleep)
    def protocol(cells, reconnects=()):
        value = old[1](cells, reconnects)
        if burst: value['burst'] = dict(messages=burst['messages'], max_bursts=burst['max_bursts'], path=burst['path'])
        return value
    campaign.admission_boundary, campaign.protocol = boundary, protocol
    try:
        yield
    finally:
        campaign.admission_boundary, campaign.protocol = old


def hold_type(budget):
    """The v8.7 Nova hold (NovaHold -> EchoHold -> BenchHold) with the campaign baseline policy and the
    30 s part RF pre-gate; restore, mixer and lock exactly as in v8.7."""
    import echoear_aec_nova_prestep as n
    from echoear_aec_campaign import BenchHold
    base = n.hold_type(budget)
    class V9Hold(base):
        rf_pre_gate_seconds = 30
        baseline_capture = BenchHold.baseline_capture
        def finish(self):
            # NovaHold skips the final audit after an exception; v9 always attempts it (the settle path and
            # exception integrity audit of BenchHold.finish), so faults through the active end are observed.
            lost = peer_loss_witness(self)
            if lost: return self.finish_peer_lost(lost)         # v9.11: the peer is gone; device-side stages only
            if self.result.get('error'):
                return super(base, self).finish()
            return super().finish()

        def finish_peer_lost(self, lost):
            """Final audit after a positively witnessed peer loss (v9.11, v9.10-acceptance-01 part 1). The
            bot-side stages (settle/seal of the in-flight attempt, health, observer and active-error checks) need
            the lost peer: they are not applicable, and an unacknowledged evaluation control is attributed to the
            already-classified transport exit. Every serial/device-side stage still runs and records its
            completion witness: settled-end snapshot (no drain poll), display/character reads, the phase-partitioned
            exception-integrity / resource audit (lost_peer_audit: device leaves over EVERY retained observation,
            incl. reconnected generations; session leaves over the lost epoch's live phase), audio off and
            disconnect, the disconnected snapshot (terminal inventory; never raised here), host RF gate, bot stop,
            and the SCTP recovery audit up to the loss (scoped to the lost peer and exit interval in part_audit).
            Any exception here is a genuine finish_error and leaves the audit unresolved."""
            import echoear_aec_campaign as C
            from echoear_aec_resources import check_snapshots
            self.result.update(peer_lost=lost, bot_stages='not applicable: peer lost before the final audit')
            self.save()
            C.snapshot(self.board, self.out, 'settled-end')
            C.write(self.out/'display-end.json', C.read(self.board, 'status', 'DISPLAY'))
            C.write(self.out/'character-end.json', C.read(self.board, 'character-status', 'DISPLAY_CHARACTER'))
            audit = lost_peer_audit(self.out, lost)
            self.result['exception_integrity_audit'] = audit
            self.result['resources'] = dict(checks=audit['checks'], all_passed=audit['all_passed'], basis=audit['basis'])
            gate_file = json_or_none(self.out/'rf-pre-gate.json')
            if gate_file: self.result['rf_active'] = C.host_nic.gate(gate_file['after'], C.host_nic.snapshot())
            loss_ns = int(min([e['t']-(e.get('silent_s') or 0) for e in lost['bot']] or [time.monotonic()])*1e9)
            self.result['active_end_ns'] = loss_ns; self.save()
            C.command(self.board, 'audio 0'); C.command(self.board, 'disconnect', 40)
            C.snapshot(self.board, self.out, 'disconnected')
            if hasattr(self, 'host_guard'): self.host_guard.boundary(self.out, 'disconnected')
            if getattr(self, 'bot', None) is not None: self.bot.stop(); self.bot = None
            self.result['sctp_recovery'] = C.check_active_recovery(C.rows(self.out/'rtp/sctp.jsonl'), loss_ns)   # scoped in part_audit
            self.result['completed'] = False; self.save()
    return V9Hold


DEVICE_LEAVES = ('internal_minimum_free', 'internal_largest', 'failed_allocations.total', 'pcm_queue_drops', 'i2s_underflows',
                 'i2s_write_errors', 'i2s_write_timeouts', 'audio.drops', 'audio.read_errors', 'audio.write_errors',
                 'audio.write_timeouts', 'console_output.full', 'console_output.transport_drops', 'aec.errors',
                 'aec.capture.read_errors', 'aec.capture.read_timeouts', 'aec.reference_clips', 'aec.output_clips',
                 'aec.capture.adc_clips', 'aec.capture.gain_clips', 'snapshots_present')
SESSION_LEAVES = ('session_snapshots_present', 'encode_p95_us', 'encode_max_us', 'core_0_steady_idle_percent',
                  'core_1_steady_idle_percent', 'uplink_frame_ms')


def all_observations(out):
    """Every retained PERF observation of the part (any peer/generation), by device time."""
    snaps = []
    for f in Path(out).rglob('*.json'):
        try: v = json.loads(f.read_text())
        except (ValueError, OSError): continue
        if isinstance(v, dict) and isinstance(v.get('tasks'), list) and 'device_us' in v:
            snaps.append(v)
    return sorted(snaps, key=lambda v: v['device_us'])


def lost_peer_audit(out, lost):
    """Phase-partitioned exception-integrity / resource audit after a witnessed peer loss (v9.12, Sol/Astra v9.11).
    Device leaves (heap minimum and largest block, stacks of tasks present in each observation, whole-session
    absolute-zero hardware counters, PERF display fault fields, live capture overflows) are checked over EVERY
    retained observation through teardown, including reconnected generations (e.g. settled-end in the new
    generation). Session leaves (session presence, encode, CPU windows, uplink frame) are checked over the lost
    epoch's live phase. Dispatch equality (data_admission_counters_only) is not applicable here: it is validated
    at the last genuinely drained live boundary in the part audit. Audit completion is separate from the leaf
    outcomes: completed=True means every applicable leaf was evaluated; failing leaves dispatch as findings."""
    from echoear_aec_resources import check_snapshots
    out = Path(out)
    every = all_observations(out)
    epoch = [v for v in every if v.get('pc_id') == lost['pc_id'] and v.get('generation') == lost['generation']]
    device = check_snapshots(every) if every else {}
    session = check_snapshots(epoch) if epoch else {}
    checks = {k: v for k, v in device.items() if k in DEVICE_LEAVES or (k.startswith('stack_') and v.get('value') is not None)}
    checks.update({k: v for k, v in session.items() if k in SESSION_LEAVES})
    base = json_or_none(out/'connected.json') or (epoch[0] if epoch else {})
    a, b = _path(base, ('aec', 'capture', 'rx_overflows')), _path(every[-1] if every else {}, ('aec', 'capture', 'rx_overflows'))
    checks['live_rx_overflows'] = dict(value=(b-a) if type(a) is int and type(b) is int and b >= a else None, startup=a,
                                       passed=type(a) is int and type(b) is int and b == a)
    faults = [dict(device_us=v['device_us'], field=k, value=v['display'].get(k)) for v in every if isinstance(v.get('display'), dict)
              for k in TERMINAL_DISPLAY_FAULTS if v['display'].get(k) not in (False, 0, None)]
    start, end = json_or_none(out/'character-start.json') or {}, json_or_none(out/'character-end.json') or {}
    checks['cat'] = dict(details=end, passed=bool(end.get('enabled')) and (end.get('posts') or 0) > (start.get('posts') or 0) and end.get('errors') == 0)
    d = json_or_none(out/'display-end.json') or {}
    checks['display'] = dict(details=d, perf_faults=faults[:20],
                             passed=not faults and d.get('dead') is False and d.get('failed') is False and d.get('send_failures') == 0
                             and (d.get('dma') or [None, None])[1] == 0 and (d.get('errors') or [None])[0] == 0
                             and d.get('gate_timeouts') == 0 and d.get('display_gate_timeouts') == 0)
    leaf_failures = sorted(k for k, v in checks.items() if not v['passed'])
    return dict(completed=bool(every) and bool(epoch), checks=checks, failed_leaves=leaf_failures,
                all_passed=bool(every) and bool(epoch) and not leaf_failures,
                observations=dict(every=len(every), lost_epoch=len(epoch)),
                applicability=dict(device='every retained observation through teardown (all generations)',
                                   session='lost epoch live phase',
                                   dispatch_equality='not applicable here; validated at the last drained live boundary'),
                basis='v9.12 phase-partitioned lost-peer audit')


def epoch_snapshots(out, pc_id, generation):
    """PERF snapshots of one peer epoch (same pc_id and generation), by device time."""
    snaps = []
    for f in Path(out).rglob('*.json'):
        try: v = json.loads(f.read_text())
        except (ValueError, OSError): continue
        if isinstance(v, dict) and isinstance(v.get('tasks'), list) and 'device_us' in v and \
                v.get('pc_id') == pc_id and v.get('generation') == generation:
            snaps.append(v)
    return sorted(snaps, key=lambda v: v['device_us'])


EXIT_ASSOCIATED = ('drop_reasons.send_cutoff', 'send_budget_cutoffs')   # PENDING USER CONFIRMATION (v9.12)


def lost_exit_interval(hold_dir, lost):
    """Exit interval of a witnessed peer loss (v9.12, Sol/Astra v9.11). The failed acquisition is the attempt
    (or link-probe) folder holding the lost epoch's newest observation; its exit interval runs from the newest of
    its own before / before-boundary observations in the epoch (a drained live boundary) to that newest
    observation, the last device evidence before the peer-loss witness. Everything up to the interval start is
    strict. Returns dict(strict_end, start, end, folder) (start/end None when no failed acquisition is identified:
    then nothing is exit-associated and the strict end is the newest drained after/before boundary or connected)."""
    hold_dir = Path(hold_dir)
    ident = lambda v: isinstance(v, dict) and v.get('pc_id') == lost['pc_id'] and v.get('generation') == lost['generation'] and 'device_us' in v
    newest, folder = None, None
    for f in hold_dir.rglob('*.json'):
        v = json_or_none(f)
        if ident(v) and isinstance(v.get('tasks'), list) and (newest is None or v['device_us'] > newest['device_us']):
            newest, folder = v, f.parent
    befores = []
    if folder is not None and folder != hold_dir:
        befores = [v for g in [folder/'before.json', *folder.glob('before-boundary-*.json')] if g.exists()
                   for v in [json_or_none(g)] if ident(v)]
    if befores:
        start = max(befores, key=lambda v: v['device_us'])
        return dict(strict_end=start, start=start, end=newest, folder=str(folder.relative_to(hold_dir)))
    drained = [v for g in list(hold_dir.glob('*/after.json'))+list(hold_dir.glob('*/before.json'))+[hold_dir/'link-probe/after.json']
               if g.exists() for v in [json_or_none(g)] if ident(v)]
    end = max(drained, key=lambda v: v['device_us']) if drained else json_or_none(hold_dir/'connected.json')
    return dict(strict_end=end, start=None, end=None, folder=None)


def exit_interval_findings(start, end):
    """The narrow exit-associated rule (v9.12; PENDING USER CONFIRMATION): within the failed acquisition's own exit
    interval only drop_reasons.send_cutoff and send_budget_cutoffs deltas are exit-associated (reported, not
    dispatched: described as associated with the exit, not proven network-caused). Every other drop reason and
    admission_overflow stays admission.control_loss CANDIDATE-FAIL; send_errors keeps delivery.send_errors; a
    malformed / reset / non-monotonic counter or a different epoch is admission.counter_schema. Dispatch equality is
    not applicable at the non-drained end observation. Returns (findings, report of every delta)."""
    import echoear_aec_v9_score as score
    out, report = [], {}
    same = start.get('pc_id') == end.get('pc_id') and start.get('generation') == end.get('generation')
    da, db = start.get('drop_reasons'), end.get('drop_reasons')
    schema = not same or not isinstance(da, dict) or not isinstance(db, dict) or set(rules.DROP_REASONS)-set(da) or set(rules.DROP_REASONS)-set(db)
    pairs = [('drop_reasons.'+k, (da or {}).get(k), (db or {}).get(k)) for k in rules.DROP_REASONS]
    pairs += [(k, _path(start, path), _path(end, path)) for k, path in
              (('send_budget_cutoffs', ('send_budget_cutoffs',)), ('send_errors', ('send_errors',)),
               ('data_admission.received', ('data_admission', 'received')), ('data_admission.dispatched', ('data_admission', 'dispatched')),
               ('data_admission.deferred', ('data_admission', 'deferred')))]
    for name, a, b in pairs:
        ok = rules._counter(a) and rules._counter(b)
        report[name] = dict(before=a, after=b, delta=(b-a) if ok else None)
        if not ok or b < a: schema = True; continue
        if b == a: continue
        if name in EXIT_ASSOCIATED: report[name]['class'] = 'exit-associated (waived, reported)'
        elif name.startswith('drop_reasons.'):
            out.append(dict(gate='admission.control_loss', counter=name, delta=b-a, interval='exit interval'))
        elif name == 'send_errors':
            out.append(dict(gate='delivery.send_errors', counter=name, delta=b-a, interval='exit interval'))
    if schema: out.append(dict(gate='admission.counter_schema', interval='exit interval'))
    return out, report


SESSION_FAIL = re.compile(r'^[IWE] \((\d+)\) dg-app: SESSION FAIL gen=(\d+) reason=remote disconnected(?: retry_ms=\d+)?\s*$')
STATE_CONNECTED = re.compile(r'^[IWE] \((\d+)\) dg-app: STATE Connected gen=(\d+) ')


def peer_loss_witness(hold):
    """Positive witness that the part's peer epoch has ended (v9.11, hardened v9.12). Either source suffices:
    - bot: an explicit client-disconnected event for the connected pc_id, bound to that peer's owner/start
      (evaluation-start for the same pc_id, and evaluation/session-owner.json naming it); evaluation-end only
      corroborates (it is emitted from the runner's finally block);
    - device: the complete dg-app record 'SESSION FAIL gen=<connected generation> reason=remote disconnected',
      anchored at the start of the line (never a substring of a logged payload), after that generation's
      'STATE Connected' record in the same boot (no later reset line between them).
    None without positive evidence (a live peer keeps the unresolved finish path)."""
    conn = json_or_none(Path(hold.out)/'connected.json')
    if not isinstance(conn, dict) or not conn.get('pc_id') or type(conn.get('generation')) is not int: return None
    pc, gen = conn['pc_id'], conn['generation']
    try: events = hold.events()
    except Exception: events = []
    mine = [e for e in events if e.get('pc_id') == pc]
    owner = json_or_none(Path(hold.out)/'evaluation/session-owner.json') or {}
    bound = owner.get('pc_id') == pc and any(e.get('event') == 'evaluation-start' for e in mine)
    disconnect = [e for e in mine if e.get('event') == 'client-disconnected']
    start = next((e['t'] for e in mine if e.get('event') == 'evaluation-start'), None)
    disconnect = [e for e in disconnect if start is not None and e['t'] >= start]
    bot = [dict(event=e['event'], t=e['t'], silent_s=e.get('silent_s')) for e in mine
           if e.get('event') in ('client-disconnected', 'evaluation-end', 'liveness-alarm')] if bound and disconnect else []
    device = []
    log = Path(hold.out)/'candidate-serial.log'
    if log.exists():
        connected_at = None
        for line in log.read_text(errors='replace').splitlines():
            if REBOOT_LINE.search(line) and 'rst:0x' in line: connected_at = None        # a reset ends the boot
            m = STATE_CONNECTED.match(line)
            if m and int(m.group(2)) == gen: connected_at = int(m.group(1))
            m = SESSION_FAIL.match(line)
            if m and int(m.group(2)) == gen and connected_at is not None and int(m.group(1)) >= connected_at:
                device.append(line)
    if not bot and not device: return None
    return dict(pc_id=pc, generation=gen, bot=bot, device=device[:3],
                sources=[x for x, v in (('bot', bot), ('device', device)) if v], owner_bound=bound)


# ---- classification of exceptions -----------------------------------------------------------------

def gate_of_exception(exc):
    """Registry gate for an exception: typed classes first, then the reviewed raise-site inventory
    (innermost scanned frame: file, qualname, message literal), else unknown.unclassified."""
    from echoear_aec_campaign import HarnessFailure, EnvironmentalPause, LinkProbeFailed
    from echoear_aec_echo_dropout import BudgetExpired
    from echoear_aec_event_tail import EventStreamChanged
    if isinstance(exc, HarnessFailure): return TERM_OWNER.get('harness:'+exc.kind, 'unknown.unclassified')
    if isinstance(exc, BudgetExpired): return 'operator.stop' if 'Operator' in str(exc) else 'parts.time_caps'
    if isinstance(exc, KeyboardInterrupt): return 'operator.stop'
    if isinstance(exc, EventStreamChanged): return 'observer.event_stream'
    if isinstance(exc, LinkProbeFailed): return 'link_probe.part'
    try:
        import serial
        if isinstance(exc, serial.SerialException): return 'host.serial_loss'
    except ImportError:
        pass
    if isinstance(exc, TimeoutError) and str(exc).startswith('Serial timeout waiting for '): return 'host.serial_loss'
    claims = registry.literal_claims()
    for frame, _ in reversed(list(traceback.walk_tb(exc.__traceback__))):
        path = Path(frame.f_code.co_filename)
        rel = 'hil/'+path.name
        if rel in registry.SCANNED_FILES:
            gate = registry.resolve(rel, frame.f_code.co_qualname, str(exc), claims)
            if gate and not gate.startswith('EXCLUDED'): return gate
            break
    if isinstance(exc, EnvironmentalPause): return 'host.environment'
    if isinstance(exc, OSError): return 'host.serial_loss'
    return 'unknown.unclassified'


def capacity(p, ledger, remaining_logical, *, part, current_s=0, current_total_s=0):
    """Section 7 capacity: remaining required support at typical windows (attempts plus one witness per
    block and one opening witness) against the current part's remaining active time and the parts that may
    still follow, each net of setup and restore reserve, all inside the 150 min ceiling."""
    v9 = p['v9']; parts = v9['parts']; cells = plan_mod.cell_index(p)
    w = lambda kind: v9['windows_s'][kind][0]+v9['scoring_s']
    need = sum(w(cells[i]['kind']) for i in remaining_logical)
    if remaining_logical:
        need += (math.ceil(len(remaining_logical)/v9['blocks']['max_acquired_attempts'])+1)*w('near-witness')
    used = sum((r.get('wall_s') or 0) for r in ledger.rows if r['type'] == 'part-end')
    ceiling_left = parts['ceiling_s']-used-current_total_s
    have = current_s
    # v9.5: capacity is the remaining CAMPAIGN wall time under the ceiling, spread over the entered parts still
    # allowed (max 3), each up to the per-part cap and net of its own setup and restore reserve; a part that
    # paused early cost only the time it used. v9.6 (decision 81): the last allowed part is not capped at
    # 60 min; it may use the whole ceiling left (part_budget below).
    future = [parts['planned_total_s'] for n in range(part+1, parts['planned']+parts['recovery']+1)]
    for i, total in enumerate(future):
        take = min(parts['last_part_total_max_s'], max(0, ceiling_left)) if i == len(future)-1 else min(total, max(0, ceiling_left))
        have += max(0, take-parts['restore_reserve_s']-parts['setup_s']); ceiling_left -= take
    return need <= have, dict(need_s=need, have_s=have)


# ---- the part runner ------------------------------------------------------------------------------

class Runner:
    """One part: blocks with whole-block admission, witnesses, O(1) scoring, ledger appends."""
    def __init__(self, p, out, ledger, *, part, role, schedule, budget, hold, acoustic=None, clock=time.monotonic, slot=None):
        self.p, self.out, self.ledger, self.part, self.role = p, Path(out), ledger, part, role
        self.slot = part if slot is None else slot      # planned part 1, 2 or recovery 3 (setup-only parts excluded)
        self.schedule, self.budget, self.hold, self.clock = schedule, budget, hold, clock
        self.cells = plan_mod.cell_index(p)
        self.v9 = p['v9']
        self.witness_index = 0
        self.view = None
        import echoear_aec_v9_score as score
        self.score = score
        self.acoustic = acoustic or score.acoustic      # tests inject a scripted acoustic stage only

    def _view(self, cls=None):
        if self.view is None: self.view = (cls or self.score.HoldView)(self.hold.out, self.p)
        return self.view

    # -- timing / admission --
    def elapsed(self):
        return self.budget.contract['active_s']-self.budget.remaining()

    def window(self, kind, which):
        return self.v9['windows_s'][kind][which]+self.v9['scoring_s']

    def fits(self, kinds):
        """Whole-block admission over every attempt and the closing witness (stress windows)."""
        durations = [self.window(k, 1) for k in kinds]+[self.window('near-witness', 1)]
        starts, t = [], self.elapsed()
        for d in durations: starts.append(t); t += d
        return rules.block_fits(starts, durations, active_s=self.budget.contract['active_s'],
                                min_start_remaining_s=self.v9['parts']['minimum_start_remaining_s'])

    def capacity_ok(self, remaining_logical):
        return capacity(self.p, self.ledger, remaining_logical, part=self.slot,
                        current_s=max(0, self.budget.contract['active_s']-self.elapsed()),
                        current_total_s=self.budget.contract['total_s'])

    # -- attempts --
    def entry(self, logical_id):
        if logical_id.startswith('v9-witness-'):
            return dict(logical_id=logical_id, kind='near-witness', cell=dict(self.v9['witness'], id=logical_id))
        return self.cells[logical_id]

    def next_witness(self):
        self.witness_index += 1
        return f'v9-witness-p{self.part}-{self.witness_index:02d}'

    def partial_descriptor(self, entry, group, attempt, folder):
        """Usable descriptor of an attempt the inherited capture ended early (capture deadline): the
        exported capture, anchors and stimulus timer are retained; result.json is not written."""
        if not ((folder/'capture.npz').exists() and (folder/'anchors.json').exists() and (folder/'before.json').exists()):
            return None
        run_id = f"{group['run_id']}-a{attempt}"
        timer = json.loads((folder/f'timer-{run_id}.json').read_text()) if (folder/f'timer-{run_id}.json').exists() else {}
        c = dict(entry['cell'], run_id=run_id, capture_dir=folder.name, attempt=attempt,
                 start_ns=getattr(self.hold, 'attempt_started_ns', 0), end_ns=time.monotonic_ns(),
                 events=[e for e in self.hold.events() if e.get('case_id') == run_id],
                 stimulus_timer=timer, jabra=timer.get('jabra'), stimulus_skipped=timer.get('skipped', False))
        return c

    def failure_findings(self, folder):
        """Recovery-failure path: dispatch the retained leaves of acquisition-failure.json (resources,
        delivery, admission) instead of an unresolved composite."""
        path = folder/'acquisition-failure.json'
        if not path.exists(): return [dict(gate='retry.clean_witness', reason='no acquisition-failure record')]
        f = json.loads(path.read_text()); out = []
        out += self.score.leaf_findings((f.get('resources') or {}).get('checks'), skip=self.score.CLIP_CHECKS+('data_admission_counters_only',))
        for key, value in ((f.get('delivery') or {}).get('delta') or {}).items():
            if value is None: out.append(dict(gate='resources.coverage', counter=key))
            elif value > 0: out.append(dict(gate='delivery.send_errors' if key == 'send_errors' else 'delivery.firmware_drops', counter=key, delta=value))
        if (folder/'before.json').exists() and (folder/'retry-check.json').exists():
            b = json.loads((folder/'before.json').read_text()); a = json.loads((folder/'retry-check.json').read_text())
            gate = rules.admission_interval(self.score.admission_observation(b), self.score.admission_observation(a))
            if gate: out.append(dict(gate=gate, boundary='retry-check'))
        return out or [dict(gate='retry.clean_witness', reason='recovery witness unclean; no failing leaf retained')]

    def acquire(self, logical_id, block_key):
        """Acquire, classify (live integrity, then fallible acoustic analysis) and append one attempt."""
        from echoear_aec_campaign import HarnessFailure
        entry = self.entry(logical_id)
        # every attempt row of the cell in this part, whatever its kind (aec-q hold A: single-talk is not a v9 SCORED
        # kind, so attempts_of() never counted it and every retake reused folder -a1 on the stale capture)
        prior = sum(1 for a in self.ledger.state.attempts.values() if a.get('logical_id') == logical_id)
        attempt = prior+1
        if entry['kind'] != 'near-witness' and attempt > self.v9['caps']['attempts_per_logical_cell']:
            raise ValueError('Per-cell attempt cap: '+logical_id)
        group = dict(run_id=entry['cell']['id'], cells=[entry['cell']])
        folder = self.hold.out/f"{group['run_id']}-a{attempt}"
        attempt_id = f'p{self.part}/{folder.name}'
        findings, scores, record, pending, error, partial = [], {}, {}, False, None, None
        started = self.clock()
        try:
            self.hold.capture(group, attempt)
            complete = True
        except Exception as exc:
            error = exc
            message = str(exc)
            complete = message in POST_ACQUISITION and (folder/'result.json').exists()
            if complete and POST_ACQUISITION[message]: findings.append(dict(gate=POST_ACQUISITION[message]))
            if isinstance(exc, HarnessFailure) and exc.kind == 'long-tts':
                complete = (folder/'result.json').exists(); findings.append(dict(gate='tts.long_lead'))
            elif not complete:
                findings.append(dict(gate=gate_of_exception(exc), error=f'{type(exc).__name__}: {exc}'))
                if isinstance(exc, HarnessFailure):
                    try: self.hold.recover_harness(group, attempt, exc)
                    except Exception as recover:
                        findings += [dict(f, error=f'{type(recover).__name__}: {recover}') for f in self.failure_findings(folder)]
                    if exc.kind in getattr(self, 'partial_kinds', ('capture-deadline',)):   # aec-q: also a late seal ack
                        partial = self.partial_descriptor(entry, group, attempt, folder)
                        if partial is not None: write(folder/'partial-descriptor.json', partial)   # offline repair input
                elif getattr(self.hold, 'attempt_cases', None):
                    try: self.hold.settle_attempt()
                    except Exception as settle:
                        findings.append(dict(gate=gate_of_exception(settle), error=f'{type(settle).__name__}: {settle}'))
        acquired_s = self.clock()
        if complete or partial is not None:
            view, a = self._view(), None
            try:
                a = self.score.load_attempt(view, folder, partial=partial)
                with self.score.fast_timing():
                    ifind, irec = self.score.integrity(view, self.p, entry, a)
                findings += ifind; record.update(irec)
            except Exception as exc:
                findings.append(dict(gate='unknown.unclassified', error=f'integrity: {type(exc).__name__}: {exc}'))
            if a is not None:
                try:
                    with self.score.fast_timing():
                        sc, afind, arec = self.acoustic(view, self.p, entry, a)
                    record.update(arec)
                    scores, combined = self.score.combine(entry, [f for f in findings if f['gate'] in ('clip.afe_output', 'clip.reference')],
                                                          sc, afind, record)
                    findings = [f for f in findings if f['gate'] not in ('clip.afe_output', 'clip.reference')]+combined
                except Exception as exc:
                    # Section 9: acoustic endpoints pending; the live findings above stand; never reacquired.
                    pending, scores = True, {}
                    findings.append(dict(gate='analyzer.exception', error=f'{type(exc).__name__}: {exc}'))
                    write(folder/'analysis-failure.json', dict(error=f'{type(exc).__name__}: {exc}',
                                                             traceback=traceback.format_exc()[-4000:]))
            record['source_sha256'] = {n: sha(folder/n) for n in ('capture.npz', 'anchors.json', 'result.json') if (folder/n).exists()}
        row = dict(type='attempt', part=self.part, block_key=block_key, logical_id=logical_id, kind=entry['kind'],
                   attempt=attempt, attempt_id=attempt_id, folder=str(folder.relative_to(self.out)),
                   source_sha256=record.get('source_sha256', {}), findings=self.score.jsonable(findings),
                   scores=self.score.jsonable(scores), pending=pending, partial=partial is not None,
                   record=self.score.jsonable({k: v for k, v in record.items() if k != 'source_sha256'}),
                   timing=dict(started_s=started, acquired_s=acquired_s, scored_s=self.clock()),
                   error=f'{type(error).__name__}: {error}' if error else None)
        appended = self.ledger.append(row)
        drains = {}
        for label in ('before', 'after'):
            path = folder/f'{label}-drain.json'
            if path.exists():
                drains[label] = drain_latency(json.loads(path.read_text()), self.view.recorded if self.view else [])
        if drains: self.ledger.append(dict(type='drain', attempt_id=attempt_id, boundaries=drains))
        write(self.out/'state.json', verdict(self.ledger.state, self.p))       # O(1) checkpoint
        return appended

    def check_stop(self, row):
        a = self.ledger.state.attempts[row['attempt_id']]
        c = a['classification']
        if c['action'] == 'STOP': raise Stop('STOP', c['candidate_faults']+c['hard_events'])
        if c['action'] == 'PAUSE-PART': raise Stop('PAUSE-PART', c['pause'])
        if a['kind'] != 'near-witness':
            if self.ledger.state.streak >= self.v9['caps']['consecutive_invalid_pause']:
                raise Stop('PAUSE-PART', ['caps.invalid_attempts'])
            state = self.ledger.state
            provisional = sum(state.attempts[i]['classification']['counts_invalid_attempt']
                              for ids in state.open_blocks.values() for i in ids)
            if state.invalid+provisional > self.v9['caps']['invalid_attempts_per_campaign']:
                raise Stop('INCONCLUSIVE', ['caps.invalid_attempts'])
        if a['pending'] and a['kind'] == 'near-witness':
            raise Stop('PAUSE-PART', ['analyzer.exception'])

    def witness(self, block_key, fit_kinds=()):
        """One witness position (v9.10): up to witness_acquisitions (3) acquisitions. An invalid witness
        (noise-contaminated: action RETAKE; or ambiguous / out-of-tolerance alignment) is retaken while the
        retake, plus fit_kinds and the closing witness, fits the part budget and admission. STOP-class findings
        stop at once; PAUSE-PART-class findings and a pending score pause with their own gates; nothing else is
        retaken. Every acquisition is an attempt row (block_key '<position>', '<position>#2', '#3'); retakes and
        refusals are noted. Witnesses never count toward the invalid-attempt cap. Returns (attempt_id, offset or
        None)."""
        limit = self.v9['blocks']['witness_acquisitions']
        row = None
        for n in range(1, limit+1):
            if self.witness_index >= MAX_WITNESSES:
                self.ledger.append(dict(type='drain', attempt_id=None, boundaries={}, note=f'{block_key}: witness pool exhausted'))
                break
            row = self.acquire(self.next_witness(), block_key if n == 1 else f'{block_key}#{n}')
            a = self.ledger.state.attempts[row['attempt_id']]
            c = a['classification']
            if c['action'] == 'STOP': raise Stop('STOP', c['candidate_faults']+c['hard_events'])
            if c['action'] == 'PAUSE-PART': raise Stop('PAUSE-PART', c['pause'])
            if a['pending']: raise Stop('PAUSE-PART', ['analyzer.exception'])
            w = row['record'].get('witness') or {}
            offset = w.get('offset') if w.get('valid') else None      # v9.7: an ambiguous witness never closes a block
            valid = (c['action'] == 'CONTINUE' and offset is not None and
                     abs(offset-self.v9['blocks']['witness_delay_samples']) <= self.v9['blocks']['witness_tolerance_samples'])
            if valid: return row['attempt_id'], offset
            reason = 'noise' if c['action'] == 'RETAKE' else 'alignment'
            if n == limit: break
            if not self.fits(['near-witness']+list(fit_kinds)):
                self.ledger.append(dict(type='drain', attempt_id=None, boundaries={},
                                        note=f'{block_key}: witness retake not admitted ({reason})'))
                break
            self.ledger.append(dict(type='drain', attempt_id=None, boundaries={},
                                    note=f'{block_key}: witness retake {n+1}/{limit} ({reason})'))
        return (row['attempt_id'] if row else None), None

    def run_block(self, block, *, start_witness=None):
        """Witness, <=5 attempts, closing witness; close_block finalizes. Returns the closing witness."""
        key = f"p{self.part}/{block['id']}"
        if start_witness is None:
            start_witness = self.witness(key+'/open', [self.entry(lid)['kind'] for lid in block['attempts']])
            if start_witness[1] is None: raise Stop('PAUSE-PART', ['alignment.near_witness'])
        for logical_id in block['attempts']:
            row = self.acquire(logical_id, key)
            self.check_stop(row)
        # The block's cells stay pending (open) until a valid closing witness exists or the retakes are spent.
        end = self.witness(key+'/close')
        offsets = [start_witness[1], end[1]]
        closed = None not in offsets and rules.witnesses_ok(offsets)
        self.ledger.append(dict(type='block-close', part=self.part, block_key=key, witnesses=[start_witness[0], end[0]],
                                offsets=offsets, closed=closed))
        write(self.out/'state.json', verdict(self.ledger.state, self.p))
        state = self.ledger.state
        if state.candidate or state.hard: raise Stop('STOP', [x['gate'] for x in state.candidate+state.hard])
        v = verdict(state, self.p)
        if v['acoustic'] == 'FAIL' and not state.pending: raise Stop('STOP', ['echo.stratum_bar' if any(s['result'] == 'FAIL' for s in v['echo'].values()) else 'barge.group_bar'])
        if not closed: raise Stop('PAUSE-PART', ['alignment.near_witness'])
        return end

    def remaining(self, logical_ids):
        """Logical cells still needing an exposure (unattempted or re-attempt), in the given order;
        never a cell with a final or pending result, never beyond the per-cell cap."""
        return [lid for lid in logical_ids if self.ledger.state.cell(lid) in ('unattempted', 'retake')]

    def all_required(self):
        return [c['logical_id'] for c in self.p['v9']['cells']]

    def extra_pool(self):
        """Work appended after this part's planned blocks, as whole blocks while they fit: re-attempts in
        every part; part 2 also takes part-1 blocks left unattempted; the recovery part takes all."""
        order = [lid for part in self.p['v9']['schedule'] for b in part['blocks'] for lid in b['attempts']]
        state = self.ledger.state
        if state.pending: return []     # section 9: no score-dependent retake while any score is pending
        retakes = [lid for lid in order if state.cell(lid) == 'retake']
        if self.slot == 1: return retakes
        allowed = order if self.slot >= 3 else \
            [lid for part in self.p['v9']['schedule'][:self.slot] for b in part['blocks'] for lid in b['attempts']]
        return retakes+[lid for lid in allowed if state.cell(lid) == 'unattempted']

    def run(self, *, smoke=False):
        """Planned blocks of this part, then whole blocks of remaining work while they fit."""
        end, queue, extra = None, [dict(b) for b in self.schedule['blocks']], 0
        size = self.v9['blocks']['max_acquired_attempts']
        while True:
            if not queue:
                todo = [] if smoke else self.extra_pool()
                if not todo: break
                extra += 1
                queue.append(dict(id=f'x{extra}', kind='extra', attempts=todo[:size]))
            block = queue.pop(0)
            attempts = self.remaining(block['attempts'])
            if not attempts: continue
            if not smoke:
                ok, need = self.capacity_ok(self.remaining(self.all_required()))
                if not ok:
                    self.ledger.append(dict(type='drain', attempt_id=None, boundaries={}, note='capacity', capacity=need))
                    raise Stop('INCONCLUSIVE', ['parts.time_caps'])
            if not self.fits([self.entry(lid)['kind'] for lid in attempts]):
                self.ledger.append(dict(type='drain', attempt_id=None, boundaries={}, note=f"block {block['id']} not admitted"))
                break
            end = self.run_block(dict(block, attempts=attempts), start_witness=end)
        return end


# ---- decision-71 burst check (smoke) ------------------------------------------------------------

def server_ids(lines, burst):
    """Ordered burst ids from `SERVER <json>` application-log lines (decision 71 receipt witness)."""
    out = []
    for line in lines:
        at = line.find('SERVER {')
        if at < 0: continue
        try:
            value = json.loads(line[at+7:].strip())
        except ValueError:
            continue
        stack = [value]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                if v.get('t') == 'burst' and isinstance(v.get('d'), dict) and v['d'].get('b') == burst:
                    out.append(v['d'].get('id')); break
                stack.extend(v.values())
            elif isinstance(v, list): stack.extend(v)
    return out


def app_receipts(lines, burst):
    """(id, device ms) of `SERVER <json>` application-log receipts of one burst, in log order."""
    out = []
    for line in lines:
        ids = server_ids([line], burst)
        if ids:
            m = re.match(r'\s*[IWE] \((\d+)\)', line)
            out.append((ids[0], int(m.group(1)) if m else None))
    return out


BURST_RESERVE_S = 30.0  # a burst starts only with this much active time left (worst case ~15 s per burst)


def reaudit_bursts(p, burst_rows, part_dir):
    """Re-audit EVERY attempted burst over the complete part serial log (Astra v9.2/v9.3): returns structured
    findings -- admission.control_loss for a duplicate or misordered application receipt whatever the coverage,
    hardware.console for a covered burst that is incomplete over the log."""
    log = Path(part_dir)/'candidate-serial.log'
    text = log.read_text(errors='replace').splitlines() if log.exists() else []
    out = []
    for r in burst_rows:
        whole = server_ids(text, r['burst'])
        if whole != sorted(set(whole)) or len(whole) != len(set(whole)):
            out.append(dict(gate='admission.control_loss', burst=r['burst'], part=r['part'],
                            reason='re-audit: duplicate/misordered application receipt over the part log'))
        elif r['result'] == 'covered' and whole != list(range(p['v9']['burst']['messages'])):
            out.append(dict(gate='hardware.console', burst=r['burst'], part=r['part'],
                            reason='re-audit: covered burst incomplete over the part log'))
    return out


def burst_check(hold, ledger, p, part, *, allowance_s, clock=time.monotonic, sleep=time.sleep, view=None, budget=None):
    """Decision-71 burst check (v9.3). Application receipt first: the window stays open until every declared
    id has been applied (app_log `SERVER` lines) or the receipt timeout from the first receipt expires; the
    complete receipt sequence is the whole-part serial log for that burst, duplicates and order kept. Device
    drain second: a sustained quiescent pair (received == dispatched, >= 0.25 s apart, receipt-time deadlines)
    requested only after the host had received every application receipt (causal barrier, v9.4): host receipt of
    the app_log record follows the device application, and each PERF snapshot is a fresh, acknowledged stats
    request, so no device/host clock comparison and no app_log timestamp is needed.
    Findings: overflow/drop/schema by admission_interval; a duplicate or misordered receipt is control loss
    whatever the coverage; missing ids with a quiescent device and zero overflow are evidence loss
    (hardware.console); a drain that never becomes quiescent is admission.drain_timeout. The burst latency
    (last send -> sustained quiescence) is MONITOR and must fall within the receipt timeout; it is not a
    calibration source."""
    import echoear_aec_campaign as campaign
    from echoear_aec_v9_score import admission_observation
    cfg = p['v9']['burst']
    timeout = cfg['receipt_timeout_s']
    part_log = hold.out/'candidate-serial.log'
    def whole_receipts():
        return app_receipts(part_log.read_text(errors='replace').splitlines(), k) if part_log.exists() else []
    for k in range(1, cfg['max_bursts']+1):
        if budget is not None and budget.remaining() < BURST_RESERVE_S:
            ledger.append(dict(type='drain', attempt_id=None, boundaries={}, note=f'burst {k} not started: active time'))
            return None
        folder = hold.out/f'burst-{k:02d}'; folder.mkdir()
        before, _ = campaign.boundary(hold.board, folder, 'before')
        cursor = hold.board.cursor
        hold.send('burst', None, burst=k)
        sent = [e for e in hold.events() if e.get('event') == 'burst-sent' and e.get('burst') == k]
        first_send = sent[-1]['first_send_s'] if sent else None
        last_send = sent[-1]['last_send_s'] if sent else None
        deadline = None
        while True:
            window = app_receipts([line for _, _, line in hold.board.lines_since(cursor)], k)
            if {i for i, _ in window} >= set(range(cfg['messages'])): break
            now = clock()
            if deadline is None or (window and deadline > now+timeout): deadline = now+timeout+(0 if window else 2.0)
            if now >= deadline: break
            sleep(.05)
        lines = [line for _, _, line in hold.board.lines_since(cursor)]
        (folder/'serial.log').write_text('\n'.join(lines)+'\n')
        receipts = whole_receipts() or window
        ids = [i for i, _ in receipts]
        applied = set(ids) >= set(range(cfg['messages']))
        last_ms = max((ms for _, ms in receipts if ms is not None), default=None)   # recorded only, never compared
        barrier = clock()          # every receipt above was already received by the host before this instant
        ready = lambda obs: obs['requested_s'] >= barrier
        after, _ = drain_boundary(hold.board, folder, 'after', allowance_s=timeout, clock=clock, sleep=sleep, ready=ready)
        drain = json.loads((folder/'after-drain.json').read_text())
        b, a = admission_observation(before), admission_observation(after)
        ok_ints = all(type(x.get(f)) is int for x in (a, b) for f in ('deferred', 'admission_overflow', 'received'))
        overflow = a['admission_overflow']-b['admission_overflow'] if ok_ints else None
        deferred = a['deferred']-b['deferred'] if ok_ints else None
        quiescent = drain['status'] == 'quiescent'
        latency = drain['quiescent_s']-last_send if quiescent and last_send is not None else None
        console = [((after.get('console_output') or {}).get(f, 0) or 0)-((before.get('console_output') or {}).get(f, 0) or 0)
                   for f in ('full', 'transport_drops')]
        gate = rules.admission_interval(b, a)
        ordered = ids == sorted(set(ids)) and len(ids) == len(set(ids))
        complete = ids == list(range(cfg['messages']))
        result = rules.burst_coverage(overflow_delta=overflow if overflow is not None else 1, deferred_delta=deferred or 0,
                                      sent_ids=list(range(cfg['messages'])), received_ids=ids)
        findings = []
        if gate: findings.append(dict(gate=gate))
        if not ordered: findings.append(dict(gate='admission.control_loss', reason='duplicate/misordered application receipt'))
        elif not complete and not gate:
            findings.append(dict(gate='hardware.console' if quiescent and overflow == 0 else 'unknown.unclassified',
                                 reason='missing ids '+str([i for i in range(cfg['messages']) if i not in ids])))
            result = 'evidence-incomplete'
        if not quiescent and not gate: findings.append(dict(gate='admission.drain_timeout', reason='no sustained quiescence after the last application'))
        elif complete and latency is not None and latency > timeout:
            findings.append(dict(gate='admission.drain_timeout', reason=f'burst latency {latency:.2f} s over the receipt timeout'))
        row = ledger.append(dict(type='burst', part=part, burst=k, result=result, overflow_delta=overflow,
                                 deferred_delta=deferred, received_ids=ids, window_ids=[i for i, _ in window],
                                 last_application_ms=last_ms, receipt_barrier_s=barrier, first_send_s=first_send, last_send_s=last_send,
                                 drain_status=drain['status'], latency_s=latency, latency_class='MONITOR',
                                 console_delta=console, findings=findings, folder=folder.name))
        write(folder/'burst.json', row)
        if any(registry.BY_ID[f['gate']]['cls'] == 'CANDIDATE-FAIL' for f in findings):
            raise Stop('STOP', [f['gate'] for f in findings])
        if findings: raise Stop('PAUSE-PART', [f['gate'] for f in findings])
        if result == 'covered' and cfg['stop_when_covered']: return row
    return None


# ---- parts --------------------------------------------------------------------------------------

def part_budget(p, ledger, part, *, mode):
    v9 = p['v9']
    used = sum((r.get('wall_s') or 0) for r in ledger.rows if r['type'] == 'part-end')
    if mode == 'smoke':
        ceiling, cap, reserve = v9['smoke']['ceiling_s'], v9['smoke']['part_total_s'], v9['smoke']['restore_reserve_s']
    else:
        ceiling, reserve = v9['parts']['ceiling_s'], v9['parts']['restore_reserve_s']
        cap = v9['parts']['planned_total_s']          # v9.5: every entered part up to the per-part cap
        if part >= v9['parts']['planned']+v9['parts']['recovery']:
            cap = min(ceiling-used, v9['parts']['last_part_total_max_s'])   # v9.6 ceiling left; v9.8 RTP cap bound
    total = min(cap, ceiling-used)
    return dict(active_s=total-reserve, restore_reserve_s=reserve, total_s=total,
                minimum_cell_remaining_s=v9['parts']['minimum_start_remaining_s'])


def files_manifest(root):
    return {str(f.relative_to(root)): sha(f) for f in sorted(Path(root).rglob('*')) if f.is_file()}


def schedule_for(p, part, *, mode):
    if mode == 'smoke': return p['v9']['smoke_schedule'][part-1]
    if part <= 2: return p['v9']['schedule'][part-1]
    return dict(part=part, blocks=[])            # recovery: remaining required work only


def entered_parts(ledger):
    """Bench parts entered (flash started): each consumes a slot; a pure host preflight refusal never
    reaches run_part and consumes none (Astra 5(b) rejected)."""
    return sum(1 for r in ledger.rows if r['type'] == 'part-end' and r.get('entered', True))


def json_or_none(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


MAX_WITNESSES = 60          # declared witness ids per part (v9.10: retakes; at most 3 per position)
REBOOT_LINE = re.compile(r'rst:0x|Guru Meditation|abort\(\) was called|Rebooting\.\.\.|Backtrace:')
# Setup-only (no live peer) applicability (v9.7): snapshot-scoped measured leaves only. Session leaves
# (session snapshots, admission, encode, CPU windows, I2S/PCM session counters, uplink_frame_ms) need a live
# peer and are not applicable; a stack task that does not exist yet is not applicable either.
SETUP_APPLICABLE = ('internal_minimum_free', 'internal_largest', 'failed_allocations.total', 'audio.drops',
                    'audio.read_errors', 'audio.write_errors', 'audio.write_timeouts', 'console_output.full',
                    'console_output.transport_drops', 'aec.errors', 'aec.capture.read_errors', 'aec.capture.read_timeouts')


EXPECTED_RESET = re.compile(r'^rst:0x15 \(USB_UART_CHIP_RESET\)')
ROM_BANNER = re.compile(r'^(ESP-ROM:|Build:)')


def candidate_crash_lines(hold_dir):
    """Positive panic/reboot evidence in the candidate serial log (v9.8, Astra v9.7 1). Only a positively
    identified expected host-triggered reset is exempt: by cause, rst:0x15 (USB_UART_CHIP_RESET), the reset
    esptool/the USB-Serial-JTAG RTS line produces after flashing; and by phase, it must be the first line of the
    log after nothing but the ROM banner (ESP-ROM:/Build:), i.e. before any candidate output. All 43 retained
    candidate logs start exactly so. Any other reset reason (WDT, panic, brownout, software restart), a reset
    after candidate output, Guru Meditation, abort(), Rebooting... or Backtrace is a crash. AEC_GUARD_CONFIG
    lines are never counted as boots (configuration setters print them too). The normal-image restore boot is in
    restore-serial.log. Returns (lines, note); a missing log is no evidence (note), never a crash."""
    log = Path(hold_dir)/'candidate-serial.log'
    if not log.exists(): return [], 'candidate-serial.log absent: no boot/setup crash evidence either way'
    lines = [l for l in log.read_text(errors='replace').splitlines() if l.strip()]
    hits = [(i, l) for i, l in enumerate(lines) if REBOOT_LINE.search(l)]
    expected = None
    if hits and EXPECTED_RESET.search(hits[0][1].strip()) and all(ROM_BANNER.search(l.strip()) for l in lines[:hits[0][0]]):
        expected = hits[0][0]
    crash = [l for i, l in hits if i != expected]
    return crash, None if expected is not None else 'no expected host-triggered reset at the start of the log'


# ---- terminal-phase applicability inventory (v9.10) -------------------------------------------------------
# Every resource / delivery / hardware leaf the live audit (check_snapshots, check_directory, delivery_counters,
# admission_interval) and the gate registry know is either APPLICABLE to the terminal disconnected snapshot, with
# the rule that evaluates it there, or NOT_APPLICABLE with the reason. test_v910_terminal_inventory_is_complete
# fails when a new leaf appears without an entry.
A = lambda rule: ('APPLICABLE', rule)
NA = lambda reason: ('NOT_APPLICABLE', reason)
STACK_RULE = 'check_snapshots stack floor, tasks still present (an exited task is not applicable)'
ZERO_RULE = 'check_snapshots absolute zero over the terminal observation'
TERMINAL_INVENTORY = {
    'snapshots_present': NA('the terminal snapshot is one observation; its presence is a completion witness'),
    'session_snapshots_present': NA('session presence after teardown'),
    'data_admission_counters_only': NA('received == dispatched needs a drain-polled boundary (settled-end carries it); '
                                       'aec-v87 does not count signalling (peerLeft) messages as dispatched'),
    'encode_p95_us': NA('encode activity after teardown'), 'encode_max_us': NA('encode activity after teardown'),
    'core_0_steady_idle_percent': NA('CPU windows after teardown'), 'core_1_steady_idle_percent': NA('CPU windows after teardown'),
    'uplink_frame_ms': NA('uplink-frame identity after teardown'),
    'cat': NA('character post growth is not required after teardown'),
    'liveness': NA('peer liveness after disconnect'),
    'internal_minimum_free': A('check_snapshots heap floor'), 'internal_largest': A('check_snapshots heap floor'),
    **{'stack_'+t: A(STACK_RULE) for t in ('audio-playback', 'audio-capture', 'audio-tx', 'peer-loop', 'peer-rx', 'app-control')},
    **{k: A(ZERO_RULE) for k in ('failed_allocations.total', 'pcm_queue_drops', 'i2s_underflows', 'i2s_write_errors',
                                 'i2s_write_timeouts', 'audio.drops', 'audio.read_errors', 'audio.write_errors',
                                 'audio.write_timeouts', 'console_output.full', 'console_output.transport_drops',
                                 'aec.errors', 'aec.capture.read_errors', 'aec.capture.read_timeouts')},
    **{k: A('part-epoch clip residual connected -> terminal (DELTA_COUNTERS attribution)') for k in
       ('aec.reference_clips', 'aec.output_clips', 'aec.capture.adc_clips', 'aec.capture.gain_clips')},
    'display': A('terminal PERF display fault fields: dead, failed, send_failures, dma_errors, gate_timeouts, '
                 'display_gate_timeouts, lock_failures, lock_timeouts = false/0 (no animation/post growth required)'),
    'live_rx_overflows': A('interval connected -> terminal (shared validation): positive delta -> hardware.audio_io'),
    'send_errors': A('interval connected -> terminal (shared validation): positive delta -> delivery.send_errors'),
    'send_budget_cutoffs': A('interval connected -> terminal (shared validation): positive delta -> delivery.firmware_drops'),
    'drop_reasons': A('rules.admission_interval connected -> terminal: positive drop / admission_overflow delta -> '
                      'admission.control_loss (preserved beside a malformed field); reset/null/decrease -> admission.counter_schema'),
}
TERMINAL_DISPLAY_FAULTS = ('dead', 'failed', 'send_failures', 'dma_errors', 'gate_timeouts', 'display_gate_timeouts',
                           'lock_failures', 'lock_timeouts')
TERMINAL_INTERVAL = (('send_errors', ('send_errors',), 'delivery.send_errors'),
                     ('send_budget_cutoffs', ('send_budget_cutoffs',), 'delivery.firmware_drops'),
                     ('live_rx_overflows', ('aec', 'capture', 'rx_overflows'), 'hardware.audio_io'))


def _path(snap, path):
    v = snap
    for k in path: v = v.get(k) if isinstance(v, dict) else None
    return v


def terminal_interval(base, snap, device=None):
    """Shared interval validation (the admission_interval rules) for the terminal deltas: both observations in the
    same peer epoch (pc_id, generation), non-negative integer counters, monotonic. An independently positive
    delta is loss whatever another field shows; a reset, null or decrease is admission.counter_schema (never 0)."""
    import echoear_aec_v9_score as score
    out = []
    gate = rules.admission_interval(score.admission_observation(base), score.admission_observation(snap))
    if gate == 'admission.drain_timeout': gate = None            # dispatch equality: not applicable (inventory)
    if gate: out.append(dict(gate=gate, leaf='drop_reasons', interval='connected -> terminal'))
    same = (isinstance(base.get('pc_id'), str) and base.get('pc_id') == snap.get('pc_id')
            and type(base.get('generation')) is int and base.get('generation') == snap.get('generation'))
    schema = gate == 'admission.counter_schema'
    for leaf, path, owner in TERMINAL_INTERVAL:
        device_level = device is not None and leaf == 'live_rx_overflows'    # capture counter spans peer epochs
        a, b = _path(base, path), _path(device if device_level else snap, path)
        valid = rules._counter(a) and rules._counter(b)
        if valid and b > a:
            out.append(dict(gate=owner, leaf=leaf, delta=b-a, startup=a, interval='connected -> terminal'))
        elif not valid or b < a or (not same and not device_level):
            if not schema: out.append(dict(gate='admission.counter_schema', leaf=leaf, before=a, after=b, interval='connected -> terminal'))
            schema = True
    return out


def late_stage_findings(hold_dir, peer_lost=None):
    """Terminal-phase audit (v9.10, Sol/Astra v9.9): the disconnected snapshot is evaluated for every APPLICABLE
    leaf of TERMINAL_INVENTORY through the existing rule functions: check_snapshots (heap, present-task stack
    floors incl. the playback formula, absolute-zero counters), the display fault fields, and the shared interval
    validation for drop_reasons/admission (admission_interval), send_errors, send_budget_cutoffs and live
    rx_overflows against the connected baseline. Clip counters go through the part-epoch residual. NOT_APPLICABLE
    leaves are never evaluated. A missing value in a present snapshot is coverage; an absent snapshot is left to
    the completion witnesses."""
    from echoear_aec_resources import check_snapshots
    import echoear_aec_v9_score as score
    snap = json_or_none(Path(hold_dir)/'disconnected.json')
    if not isinstance(snap, dict): return []
    out = []
    if isinstance(snap.get('tasks'), list):
        checks = check_snapshots([snap])
        present = {t.get('name') for t in snap['tasks']}
        applicable = {}
        for name, check in checks.items():
            kind = TERMINAL_INVENTORY.get(name, ('MISSING',))[0]
            if kind == 'MISSING': raise KeyError('terminal inventory has no entry for leaf '+name)
            if kind != 'APPLICABLE' or name in score.CLIP_CHECKS: continue
            if name.startswith('stack_') and name[6:] not in present: continue     # exited task: not applicable
            applicable[name] = check
        out += [dict(f, interval='terminal') for f in score.leaf_findings(applicable)]
    else:
        out.append(dict(gate='resources.coverage', reason='terminal snapshot has no task census', interval='terminal'))
    display = snap.get('display')
    if not isinstance(display, dict):
        out.append(dict(gate='resources.coverage', reason='terminal display fields missing', interval='terminal'))
    else:
        for k in TERMINAL_DISPLAY_FAULTS:
            v = display.get(k)
            if k in ('dead', 'failed'): bad, ok = v is True, isinstance(v, bool)
            else: bad, ok = type(v) is int and v > 0, type(v) is int and v >= 0
            if bad: out.append(dict(gate=TERM_OWNER['resources:display'], check='display.'+k, value=v, interval='terminal'))
            elif not ok: out.append(dict(gate='resources.coverage', reason='display.'+k+' missing/malformed', interval='terminal'))
    base = json_or_none(Path(hold_dir)/'connected.json')
    if not isinstance(base, dict):
        out.append(dict(gate='resources.coverage', reason='connected baseline missing for terminal deltas', interval='terminal'))
    elif peer_lost:
        # v9.11: per-session counters of the lost epoch end with it: their interval is connected -> the last
        # observation of that epoch; the device-level capture counter runs through the terminal snapshot.
        end = lost_exit_interval(hold_dir, peer_lost)['strict_end']
        if not isinstance(end, dict): out.append(dict(gate='resources.coverage', reason='lost epoch has no snapshot', interval='terminal'))
        else: out += terminal_interval(base, end, device=snap)
    else:
        out += terminal_interval(base, snap)
    return out


def final_audit_witnesses(result, hold_dir):
    """Completion witnesses of every applicable final-audit stage of BenchHold.finish (v9.7, Astra 3):
    resource audit, exception integrity audit (after an exception), disconnected snapshot, SCTP recovery
    audit, and no finish_error. Returns the missing stages."""
    missing = []
    if result.get('finish_error'): missing.append('finish_error: '+str(result['finish_error']))
    if not isinstance((result.get('resources') or {}).get('checks'), dict): missing.append('resources')
    exc_audit = result.get('exception_integrity_audit') or {}
    if result.get('peer_lost') and exc_audit.get('completed') is not True:     # v9.12: completion, not leaf outcomes
        missing.append('exception_integrity_audit')
    elif not result.get('peer_lost') and result.get('error_class') and exc_audit.get('all_passed') is not True:
        missing.append('exception_integrity_audit')
    if not (Path(hold_dir)/'disconnected.json').exists(): missing.append('disconnected snapshot')
    if result.get('sctp_recovery') is None: missing.append('sctp_recovery')
    return missing


def part_audit(p, ledger, part, hold_dir, result):
    """Final/setup/probe audit leaf dispatch through the active end (Astra blocker 5).

    Every failing leaf of the final check_directory audit and of the link-probe audit goes to its gate;
    clip counters are attributed against the per-attempt deltas (an unexplained output/reference clip is
    unknown, a MIC clip outside every cell is a room event); admission is reconciled over the whole part
    epoch (connected -> settled-end) with the v87 checker; a missing or incomplete audit is unresolved
    (resources.part_audit), never a pass. Returns (gates, audit record)."""
    import echoear_aec_v9_score as score
    findings, notes = [], []
    res = result.get('resources')
    not_started = (result.get('final_audit') or {}).get('status') == 'not-started'
    # Positive candidate crash evidence across the candidate boot/setup/live interval, before any exemption
    # (Sol v9.6 blocker 2): sticky device.reboot_crash whatever the immediate exit was.
    crash, crash_note = candidate_crash_lines(hold_dir)
    if crash: findings.append(dict(gate='device.reboot_crash', lines=crash[:5], interval='candidate serial'))
    if crash_note: notes.append(dict(note=crash_note))
    if not res and not_started:
        # The part paused before connect (e.g. noise.part_baseline): no live interval exists to audit. Only
        # measured, applicable snapshot leaves can fail the candidate (v9.7, Astra blocker 2); session leaves
        # are not applicable without a live peer, and absent values are not breaches.
        from echoear_aec_resources import check_snapshots
        snaps = [json.loads(f.read_text()) for f in sorted(hold_dir.glob('*.json'))]
        snaps = [x for x in snaps if isinstance(x, dict) and isinstance(x.get('tasks'), list) and 'device_us' in x]
        checks = check_snapshots(snaps) if snaps else {}
        applicable = {k: v for k, v in checks.items()
                      if (k in SETUP_APPLICABLE or (k.startswith('stack_') and v.get('value') is not None))
                      and v.get('value') is not None}
        leaves = score.leaf_findings(applicable) if applicable else []
        findings += [f for f in leaves if registry.BY_ID[f['gate']]['cls'] == 'CANDIDATE-FAIL']
        notes.append(dict(note='setup-only part: no live interval; measured applicable snapshot leaves only',
                          applicable=sorted(applicable), not_applicable=sorted(set(checks)-set(applicable))))
        return [f['gate'] for f in findings], dict(clean=not findings, findings=findings, notes=notes)
    if not res: findings.append(dict(gate='resources.part_audit', reason='final audit not completed'))
    else: findings += score.leaf_findings(res.get('checks'), skip=score.CLIP_CHECKS+('data_admission_counters_only',))
    probe = json_or_none(hold_dir/'link-probe/result.json')
    if probe:
        findings += [dict(f, interval='link-probe') for f in score.leaf_findings((probe.get('resources') or {}).get('checks'),
                     skip=score.CLIP_CHECKS+('data_admission_counters_only',))]
        b, a = json_or_none(hold_dir/'link-probe/before.json'), json_or_none(hold_dir/'link-probe/after.json')
        if b and a:
            gate = rules.admission_interval(score.admission_observation(b), score.admission_observation(a))
            if gate: findings.append(dict(gate=gate, interval='link-probe'))
    first, last = json_or_none(hold_dir/'connected.json'), json_or_none(hold_dir/'settled-end.json')
    lost = result.get('peer_lost')
    exit_window = None
    if lost and first:      # v9.12: strict up to the failed acquisition's drained start; narrow rule in its exit interval
        exit_window = lost_exit_interval(hold_dir, lost)
        last = exit_window['strict_end']
        report = {}
        if exit_window['start'] is not None:
            xf, report = exit_interval_findings(exit_window['start'], exit_window['end'])
            findings += xf
        notes.append(dict(note='peer lost: strict audit connected -> the failed acquisition\'s drained start; in its exit '
                               'interval only send_cutoff / send_budget_cutoffs are exit-associated (PENDING USER CONFIRMATION); '
                               'bot-side final stages not applicable',
                          peer_lost=lost, exit_folder=exit_window['folder'],
                          exit_interval_device_us=[(exit_window['start'] or {}).get('device_us'), (exit_window['end'] or {}).get('device_us')],
                          strict_end_device_us=(last or {}).get('device_us'), exit_interval_deltas=report,
                          rule='EXIT_ASSOCIATED = drop_reasons.send_cutoff, send_budget_cutoffs (v9.12, pending user confirmation)'))
    if first and last:
        gate = rules.admission_interval(score.admission_observation(first), score.admission_observation(last))
        if gate: findings.append(dict(gate=gate, interval='part epoch connected -> settled-end'))
        drain = json_or_none(hold_dir/'settled-end-drain.json')
        if drain and drain['status'] == 'drain-timeout' and not gate: findings.append(dict(gate='admission.drain_timeout', interval='settled-end'))
        attributed = {}
        for r in ledger.rows:
            if r['type'] == 'attempt' and r['part'] == part:
                for f in r['findings']:
                    if f['gate'].startswith('clip.') and isinstance(f.get('delta'), int):
                        attributed[f['gate']] = attributed.get(f['gate'], 0)+f['delta']
        terminal = json_or_none(hold_dir/'disconnected.json') or last      # v9.9: through the terminal snapshot
        for path, kind in score.DELTA_COUNTERS.items():
            x, y = score.counter(first, path), score.counter(terminal, path)
            if type(x) is not int or type(y) is not int: findings.append(dict(gate='resources.coverage', counter='.'.join(path))); continue
            got = attributed.get('clip.afe_output' if kind == 'clip.afe_output' else 'clip.reference' if kind == 'clip.reference' else None, 0) \
                if kind != 'mic' else attributed.get('clip.mic_room', 0)+attributed.get('clip.mic_unattributed', 0)
            residual = y-x-got
            if residual > 0 and kind == 'mic': notes.append(dict(counter='.'.join(path), residual=residual,
                                                                  class_='clip.mic_room outside every cell: no cell to invalidate'))
            elif residual > 0: findings.append(dict(gate='unknown.unclassified', reason=kind+' outside a scored cell', residual=residual))
    elif res: findings.append(dict(gate='resources.coverage', reason='connected/settled-end snapshot missing'))
    findings += late_stage_findings(hold_dir, peer_lost=lost)   # positive final-stage faults before completion
    sctp = result.get('sctp_recovery')
    scoped = False
    if sctp is not None and not sctp.get('passed') and lost and exit_window and exit_window['folder']:
        drain = json_or_none(hold_dir/exit_window['folder']/'before-drain.json') or {}
        start_s = drain.get('quiescent_s') or drain.get('first_s')
        bad = [r for r in sctp.get('recoveries', []) if not r.get('passed')]
        scoped = bool(start_s) and bool(bad) and all(r.get('pc_id') == lost['pc_id'] and r['t3_ns'] >= start_s*1e9 for r in bad)
        if scoped: notes.append(dict(note='SCTP timers unrecovered inside the exit interval of the lost peer: associated with the exit',
                                     timers=bad[:10], exit_start_s=start_s))
    if sctp is not None and not sctp.get('passed') and not scoped:
        findings.append(dict(gate='resources.part_audit', reason='active SCTP T3 without clean recovery (check_active_recovery)'))
    if result.get('completed') is False:
        # An exception-ended part is completed=false because of its (separately classified) exception. It is
        # resolved only when every applicable final-audit stage has a completion witness and finish raised
        # nothing (v9.7, Astra blocker 3); otherwise the audit stays unresolved, alongside any positive finding
        # dispatched above (v9.8). The generic error_class is never promoted to a candidate failure.
        missing = final_audit_witnesses(result, hold_dir) if result.get('error') or result.get('peer_lost') else \
            ([] if findings else ['no exception explains completed=false'])
        if missing: findings.append(dict(gate='resources.part_audit', reason='final audit incomplete', missing=missing))
    gates = [f['gate'] for f in findings]
    clean = not any(registry.BY_ID[g]['cls'] in ('CANDIDATE-FAIL', 'PAUSE-PART') for g in gates)
    return gates, dict(clean=clean, findings=findings, notes=notes)


def run_part(p, out, ledger, part, *, mode, allowance_s, hold_factory=None, budget_factory=None, acoustic=None,
             clock=time.monotonic, sleep=time.sleep):
    """One process-level part: Budget -> hold (lock, flash, boot, baseline) -> connect -> link probe ->
    blocks -> finish -> restore. Every exit appends a part-end row with the restore witness and the audit."""
    import echoear_aec_nova_prestep as n
    slot = entered_parts(ledger)+1
    limit = 2 if mode == 'smoke' else p['v9']['parts']['planned']+p['v9']['parts']['recovery']
    if slot > limit:
        ledger.append(dict(type='campaign-close', reason='INCONCLUSIVE', gates=['parts.time_caps'], note='no further part'))
        return
    role = 'recovery' if slot == 3 else ('smoke' if mode == 'smoke' else 'planned')
    contract = part_budget(p, ledger, slot, mode=mode)
    if contract['active_s'] < p['v9']['parts']['setup_s']+p['v9']['parts']['minimum_start_remaining_s']:
        ledger.append(dict(type='campaign-close', reason='INCONCLUSIVE', gates=['parts.time_caps'],
                           note='ceiling cannot fit another part'))
        return
    schedule = schedule_for(p, slot, mode=mode)
    ledger.append(dict(type='part-start', part=part, slot=slot, role=role, mode=mode, budget=contract,
                       plan_sha256=ledger.rows[0]['plan_sha256'], drain_allowance_s=allowance_s,
                       host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
    started = clock(); reason, gates, error, hold, budget = 'complete', [], None, None, None
    audit = dict(clean=False, findings=[dict(gate='resources.part_audit', reason='part did not reach its audit')], notes=[])
    burst = p['v9']['burst'] if mode == 'smoke' and schedule.get('burst') else None
    budget_factory = budget_factory or n.Budget
    hold_dir = out/f'part-{part}'
    try:
        with patched_campaign(allowance_s, burst, clock=clock, sleep=sleep), budget_factory(contract) as budget:
            factory = hold_factory(budget) if hold_factory else hold_type(budget)
            hold = factory(p, p['sessions'][0], hold_dir)   # bound before __enter__ so its restore witness survives
            with hold:
                hold.environmental_counts = {'campaign': 0}
                cells = [c['cell'] for c in p['v9']['cells']]
                witnesses = [dict(p['v9']['witness'], id=f'v9-witness-p{part}-{i:02d}') for i in range(1, MAX_WITNESSES+1)]
                hold.connect(cells+witnesses, [])
                hold.link_probe()
                runner = Runner(p, out, ledger, part=part, role=role, schedule=schedule, budget=budget,
                                hold=hold, acoustic=acoustic, clock=clock, slot=slot)
                view = runner._view()          # warm the scorer (imports, quiet reference, stream tails)
                view.poll(time.monotonic_ns())
                floors = runner.score.jsonable(runner.score.baseline_floors(view))
                write(hold_dir/'baseline-floors.json', floors)             # decision 72 part report
                ledger.append(dict(type='floors', part=part, baseline=floors))
                try:
                    runner.run(smoke=mode == 'smoke')
                    if burst: burst_check(hold, ledger, p, part, allowance_s=allowance_s, clock=clock, sleep=sleep, view=view,
                                          budget=budget)
                except Stop as stop:
                    reason, gates = stop.reason, stop.gates
                hold.finish()
    except Stop as stop:
        reason, gates = stop.reason, stop.gates
    except BaseException as exc:
        error = exc
        gate = gate_of_exception(exc)
        reason = 'STOP' if registry.BY_ID[gate]['cls'] == 'CANDIDATE-FAIL' else 'PAUSE-PART'
        gates = [gate]
    finally:
        r = getattr(hold, 'result', {}) if hold is not None else {}
        restore = dict(restored_elf_sha256=r.get('restored_elf_sha256'), mixer_equal=r.get('mixer_equal'),
                       lock_released=r.get('lock_released'), cleanup_errors=r.get('cleanup_errors', []),
                       completed=r.get('completed'))
        touched = bool(getattr(hold, 'touched', False))
        mixer = (hold_dir/'mixer-before.state').exists()
        restore.update(touched=touched)
        restore['clean'] = bool((restore['restored_elf_sha256'] == NORMAL_HASH or not touched)
                                and (restore['mixer_equal'] is True or not mixer)
                                and restore['lock_released'] is True and not restore['cleanup_errors']
                                and not r.get('parked_with_lock'))
        if hold is not None and hold_dir.exists():
            try:
                agates, audit = part_audit(p, ledger, part, hold_dir, r)
            except Exception as exc:
                agates, audit = ['resources.part_audit'], dict(clean=False, findings=[dict(gate='resources.part_audit',
                                                               error=f'{type(exc).__name__}: {exc}')], notes=[])
            gates = gates+[g for g in agates if g not in gates]
        part_bursts = [x for x in ledger.rows if x['type'] == 'burst' and x['part'] == part]
        if part_bursts and hold_dir.exists():
            reaudit = reaudit_bursts(p, part_bursts, hold_dir)
            ledger.append(dict(type='burst-reaudit', part=part, findings=reaudit, bursts=[x['burst'] for x in part_bursts]))
            gates = gates+[f['gate'] for f in reaudit if f['gate'] not in gates]
            if any(registry.BY_ID[f['gate']]['cls'] == 'CANDIDATE-FAIL' for f in reaudit): reason = 'STOP'
            elif reaudit and reason == 'complete': reason = 'PAUSE-PART'
        carried = {x['gate'] for x in ledger.state.candidate}
        part_findings = [dict(gate=g) for g in gates if g in registry.BY_ID and registry.BY_ID[g]['cls'] == 'CANDIDATE-FAIL'
                         and g not in carried]
        if part_findings: reason = 'STOP'
        elif reason == 'complete' and not audit['clean']: reason = 'PAUSE-PART'
        acquired = any(x['type'] == 'attempt' and x['part'] == part for x in ledger.rows)
        over = bool(getattr(budget, 'total_expired', False))
        ledger.append(dict(type='part-end', part=part, slot=slot, entered=touched, started=acquired, reason=reason,
                           gates=gates, wall_s=clock()-started, over_budget=over, audit=audit,
                           error=f'{type(error).__name__}: {error}' if error else None, restore=restore,
                           part_findings=part_findings,
                           files_sha256=files_manifest(hold_dir) if hold_dir.exists() else {}))
        state = ledger.state
        v = verdict(state, p, final=False)
        close = None
        remaining = [c['logical_id'] for c in p['v9']['cells'] if state.cell(c['logical_id']) in ('unattempted', 'retake')]
        used = entered_parts(ledger)
        if v['outcome'] in ('PASS', 'FAIL') or reason in ('STOP', 'INCONCLUSIVE'):
            close = v['outcome'] if v['outcome'] in ('PASS', 'FAIL') else 'INCONCLUSIVE'
        elif mode == 'acceptance' and not remaining and not state.pending and restore['clean']:
            close = verdict(state, p, final=True)['outcome']   # nothing left to acquire
        elif mode == 'acceptance' and not capacity(p, ledger, remaining, part=used)[0]:
            ledger.append(dict(type='drain', attempt_id=None, boundaries={}, note='capacity',
                               capacity=capacity(p, ledger, remaining, part=used)[1]))
            close = 'INCONCLUSIVE'; gates = gates+['parts.time_caps']   # no further part can complete the support
        elif mode == 'smoke' and used >= 2: close = 'SMOKE-COMPLETE'
        elif mode == 'acceptance' and used >= limit: close = 'INCONCLUSIVE'
        if not restore['clean'] and close is None: close = 'INCONCLUSIVE'
        if close: ledger.append(dict(type='campaign-close', reason=close, gates=gates))
        write(out/'verdict.json', dict(verdict(ledger.state, p), ledger_head=ledger.head))
        if error is not None and not isinstance(error, Exception): raise error


# ---- smoke readiness / acceptance allowance -----------------------------------------------------

def smoke_summary(p, ledger_rows, *, out=None):
    """Lifecycle readiness of the smoke (Astra blocker 6) and, separately, decision-71 burst coverage.
    Ready only when the frozen smoke schedule was structurally exercised in two entered, resumed parts:
    every scheduled cell attempted in its own part without exception, error or pending score, both
    parts' blocks executed (block-close rows) with their witnesses, both parts complete with clean
    restores and audits, the 30 min total respected, no candidate fault, and (with `out`) the part
    evidence unchanged since its part-end manifest. Acoustic thresholds and witness validity are not
    required (a noisy smoke proves execution, not acoustics)."""
    s = fold(ledger_rows)
    bursts = [r for r in ledger_rows if r['type'] == 'burst']
    # v9.3 calibration source: ordinary attempt-boundary drain latencies only; the burst's last-send ->
    # sustained-quiescence latency is MONITOR and need only fall within the receipt timeout.
    latencies = [b['latency_s'] for r in ledger_rows if r['type'] == 'drain' for b in r['boundaries'].values()
                 if b.get('latency_s') is not None]
    unbounded = [r.get('attempt_id') for r in ledger_rows if r['type'] == 'drain' for b in r['boundaries'].values() if b.get('unbounded')]
    starts = [r for r in ledger_rows if r['type'] == 'part-start']
    ends = [r for r in ledger_rows if r['type'] == 'part-end']
    covered = any(r['result'] == 'covered' for r in bursts)
    measured = max(latencies) if latencies else None
    rule = p['v9']['admission']
    # v9.9: a fixed declared allowance; the smoke only has to show every ordinary boundary within half of it.
    boundaries = [dict(attempt_id=r.get('attempt_id'), boundary=k, latency_s=b.get('latency_s'),
                       backlog_observed=b.get('backlog_observed'), unbounded=bool(b.get('unbounded')))
                  for r in ledger_rows if r['type'] == 'drain' for k, b in r['boundaries'].items()]
    over = [b for b in boundaries if b['latency_s'] is not None and b['latency_s'] > rule['readiness_boundary_max_s']]
    allowance = rule['drain_allowance_s'] if measured is not None and not over else None
    problems, reaudit_findings = [], []
    for r in ledger_rows:
        if r['type'] == 'burst-reaudit':
            reaudit_findings += r['findings']
            problems += [f"burst {f['burst']} receipt re-audit (part end): {f['gate']}" for f in r['findings']]
    if [e.get('slot') for e in ends if e.get('entered', True)] != [1, 2] or len(starts) != len(ends):
        problems.append('two entered parts (slots 1, 2) required')
    if not all(e['reason'] == 'complete' for e in ends): problems.append('every part must end complete: '+str([e['reason'] for e in ends]))
    if not all(e['restore']['clean'] for e in ends): problems.append('restore not clean')
    if not all((e.get('audit') or {}).get('clean') for e in ends): problems.append('part audit unresolved')
    wall = sum(e['wall_s'] for e in ends)
    if wall > p['v9']['smoke']['ceiling_s'] or any(e.get('over_budget') for e in ends): problems.append(f'over the smoke ceiling ({wall:.0f} s)')
    attempts = [r for r in ledger_rows if r['type'] == 'attempt']
    slot_of = {e['part']: e.get('slot') for e in ends}
    for part in p['v9']['smoke_schedule']:
        for block in part['blocks']:
            for lid in block['attempts']:
                rows = [a for a in attempts if a['logical_id'] == lid and slot_of.get(a['part']) == part['part']]
                if not rows: problems.append('not exercised: '+lid)
                elif any(a['error'] or a['pending'] for a in rows): problems.append('error/pending: '+lid)
    for e in ends:
        if not any(r['type'] == 'block-close' and r['part'] == e['part'] for r in ledger_rows): problems.append(f"part {e['part']}: no block executed")
    if s.pending: problems.append('pending scores')
    if s.candidate: problems.append('candidate fault')
    if out is not None:
        for e in ends:
            folder = Path(out)/f"part-{e['part']}"
            if e['files_sha256'] and files_manifest(folder) != e['files_sha256']: problems.append(f"part {e['part']} evidence changed")
        # Re-audit EVERY attempted burst over the complete part log again at readiness (structured findings).
        for part_no in sorted({r['part'] for r in bursts}):
            for f in reaudit_bursts(p, [r for r in bursts if r['part'] == part_no], Path(out)/f'part-{part_no}'):
                reaudit_findings.append(f)
                problems.append(f"burst {f['burst']} receipt re-audit: {f['gate']} ({f['reason']})")
    for r in bursts:
        if r['result'] == 'covered' and (r.get('latency_s') is None or r['latency_s'] > p['v9']['burst']['receipt_timeout_s']):
            problems.append(f"burst {r['burst']} latency not within the receipt timeout")
    lifecycle = not problems
    allowance_ok = allowance is not None and not unbounded
    return dict(ready=bool(lifecycle and covered and allowance_ok), lifecycle_ready=lifecycle, problems=problems,
                findings=reaudit_findings+[dict(f, source='ledger') for f in s.candidate],
                burst_covered=covered, bursts=[dict(burst=r['burst'], result=r['result'], deferred_delta=r['deferred_delta'],
                overflow_delta=r['overflow_delta'], latency_s=r['latency_s']) for r in bursts],
                largest_last_arrival_to_quiescence_s=measured, acceptance_drain_allowance_s=allowance if allowance_ok else None,
                drain_boundaries=boundaries, drain_boundaries_over_readiness=over,
                drain_backlog_boundaries=[b for b in boundaries if b['backlog_observed']],
                readiness_boundary_max_s=rule['readiness_boundary_max_s'],
                drain_unbounded=unbounded, wall_s=wall,
                candidate_faults=s.candidate, parts=[dict(part=r['part'], slot=r.get('slot'), reason=r['reason'],
                restore_clean=r['restore']['clean']) for r in ends], resume_boundary=len(ends) == 2 and len(starts) == 2)


def acceptance_allowance(p):
    """Frozen before scored data from the hash-verified smoke ledger; otherwise no launch."""
    path = Path(p['outputs']['smoke'])/'ledger.jsonl'
    if not path.exists(): raise ValueError('Smoke ledger missing: readiness review, no launch')
    ledger = Ledger(path)
    if ledger.rows[0].get('plan_sha256') != sha(plan_mod.DESTINATION/'plan.json'):
        raise ValueError('Smoke ran a different plan')
    summary = smoke_summary(p, ledger.rows, out=path.parent)
    if not summary['ready']: raise ValueError('Smoke not ready (lifecycle/burst/allowance): readiness review, no launch: '
                                              +json.dumps(summary)[:800])
    return summary['acceptance_drain_allowance_s'], dict(smoke_ledger_head=ledger.head, summary=summary)


# ---- entry points -------------------------------------------------------------------------------

def preflight(p):
    from echoear_aec_accept import check_esptool
    from echoear_aec_host_gate import prelaunch
    check_esptool()
    if not prelaunch(p['host_environment'])['passed']: raise RuntimeError('Host UUID/BSSID prelaunch guard failed')


def start(plan, out, reviewed_sha, *, mode, tty=None, **kw):
    p = check_launch(plan, reviewed_sha, mode=mode, tty=tty)
    plan_mod.validate(p)
    out = Path(out).resolve()
    if out != Path(p['outputs'][mode]).resolve() or out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('Fresh declared output required')
    if mode == 'acceptance': allowance, basis = acceptance_allowance(p)
    else: allowance, basis = p['v9']['admission']['smoke_drain_allowance_s'], dict(rule='v9.9 fixed declared allowance')
    if kw.pop('skip_preflight', False) is False: preflight(p)
    out.mkdir()
    ledger = Ledger(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode=mode, plan_sha256=reviewed_sha, runtime_sha256=p['runtime_sha256'],
                       drain_allowance_s=allowance, drain_basis=basis, credit=mode == 'acceptance'))
    run_part(p, out, ledger, 1, mode=mode, allowance_s=allowance, **kw)
    return ledger


def amendment_hashes(ledger):
    approved = {}
    for r in ledger.rows:
        if r['type'] == 'amendment': approved.update(r['runtime_sha256'])
    return approved


def resume(plan, out, reviewed_sha, *, mode, tty=None, **kw):
    """Part 2 or the recovery part: chain, restore and part evidence verified first; same plan and
    runtime hashes (except an approved analyzer-only amendment); fresh boot; caps carried."""
    p = check_launch(plan, reviewed_sha, mode=mode, tty=tty)
    out = Path(out).resolve()
    if out != Path(p['outputs'][mode]).resolve() or not (out/'ledger.jsonl').is_file():
        raise ValueError('Declared output with a ledger required')
    try:
        ledger = Ledger(out/'ledger.jsonl')
        rows = ledger.rows
        if rows[0]['type'] != 'campaign' or rows[0]['plan_sha256'] != reviewed_sha or rows[0]['mode'] != mode:
            raise ValueError('Resume plan/mode mismatch')
        if ledger.state.closed: raise ValueError('Campaign closed: '+ledger.state.closed['reason'])
        ends = [r for r in rows if r['type'] == 'part-end']
        starts = [r for r in rows if r['type'] == 'part-start']
        if len(ends) != len(starts) or not ends: raise ValueError('Previous part has no part-end record')
        limit = 2 if mode == 'smoke' else 3
        if entered_parts(ledger) >= limit: raise ValueError('No further part allowed')
        if not ends[-1]['restore']['clean']: raise ValueError('Previous part restore not verified')
        if ledger.state.pending: raise ValueError('Pending scores: offline analyzer repair required before resume')
        for r in ends:
            folder = out/f"part-{r['part']}"
            if r['files_sha256'] and files_manifest(folder) != r['files_sha256']:
                raise ValueError(f"Part {r['part']} evidence changed since its part-end")
        plan_mod.validate(p, analyzer_amendment=amendment_hashes(ledger))
        part = len(ends)+1
        if (out/f'part-{part}').exists(): raise ValueError('Part directory exists; no attempt reuse')
    except Exception as exc:
        write(out/f'resume-refused-{int(time.time())}.json', dict(error=f'{type(exc).__name__}: {exc}',
              outcome='INCONCLUSIVE (refused resume)'))
        raise
    if kw.pop('skip_preflight', False) is False: preflight(p)
    run_part(p, out, ledger, part, mode=mode, allowance_s=rows[0]['drain_allowance_s'], **kw)
    return ledger


ANALYZER_FILES = ('hil/echoear_aec_v9_score.py', 'hil/echoear_aec_campaign_analysis.py', 'hil/echoear_aec_accept_analysis.py',
                  'hil/echoear_aec_lull.py', 'hil/echoear_aec_metrics.py')


def rescore(plan, out, amendment, *, scorer=None, check_runtime=True):
    """Offline analyzer repair (section 9): only analyzer files may differ from the plan's runtime hashes;
    the reviewed diff's hash is recorded as a provenance amendment, then every retained attempt is
    re-scored. A finalized FAIL is never reopened."""
    from echoear_aec_v9_score import score_attempt, HoldView
    p = json.loads(Path(plan).read_text()) if not isinstance(plan, dict) else plan
    ledger = Ledger(Path(out)/'ledger.jsonl')
    changed = {}
    if check_runtime:
        runtime = {f: sha(ROOT/f) for f in plan_mod.runtime_files()}
        changed = {k: v for k, v in runtime.items() if p['runtime_sha256'].get(k) != v}
        if set(changed) - set(ANALYZER_FILES) or set(p['runtime_sha256']) != set(runtime):
            raise ValueError('Only analyzer files may change in an amendment')
    if (ledger.state.closed or {}).get('reason') == 'FAIL': raise ValueError('A finalized FAIL is never reopened')
    ledger.append(dict(type='amendment', amendment_sha256=sha(amendment), runtime_sha256=changed))
    views = {}
    for aid, a in list(ledger.state.attempts.items()):
        folder = Path(out)/a['folder']
        partial = json.loads((folder/'partial-descriptor.json').read_text()) if (folder/'partial-descriptor.json').exists() else None
        if not (folder/'result.json').exists() and partial is None: continue
        part_dir = folder.parent
        views.setdefault(part_dir, HoldView(part_dir, p, retain=True))
        entry = plan_mod.cell_index(p).get(a['logical_id']) or dict(kind=a['kind'], logical_id=a['logical_id'])
        value = (scorer or score_attempt)(views[part_dir], folder, p, entry, **(dict(partial=partial) if partial else {}))
        kept = [f for f in a['findings'] if f['gate'] != 'analyzer.exception' and f.get('error')]
        ledger.append(dict(type='rescore', attempt_id=aid, findings=kept+value['findings'], scores=value['scores'], pending=False))
    v = verdict(ledger.state, p, final=ledger.state.closed is not None)
    if v['outcome'] == 'FAIL' and not ledger.state.closed:
        ledger.append(dict(type='campaign-close', reason='FAIL', gates=[x['gate'] for x in ledger.state.candidate+ledger.state.hard],
                           note='established by offline rescore'))
    write(Path(out)/'verdict.json', dict(verdict(ledger.state, p), ledger_head=ledger.head))


# ---- freeze -------------------------------------------------------------------------------------

def launcher(plan, digest, *, mode):
    q = shlex.quote; go = str(plan_mod.DESTINATION/'USER-GO')
    lines = ['#!/usr/bin/env bash', 'set -euo pipefail',
             f'test "${{{REVIEW_ENV}:-}}" = {q(digest)} || {{ echo "Reviewed plan hash required" >&2; exit 2; }}']
    if mode == 'acceptance':
        lines += ['# Acceptance: quiet-room USER-GO required. Usage: launch-v9-acceptance.sh run|resume',
                  'op="${1:-}"; [[ "$op" == run || "$op" == resume ]] || { echo "Usage: $0 run|resume" >&2; exit 2; }',
                  f'test -f {q(go)} || {{ echo "USER-GO required" >&2; exit 2; }}',
                  f'test "$(cat {q(go)})" = {q(digest)} || {{ echo "USER-GO must contain this plan hash" >&2; exit 2; }}']
    else:
        lines += ['# Smoke: standing smoke permission (decision 70d); no USER-GO, no credit, normal restore.']
    lines += ['test -t 0 && test -t 1 || { echo "Interactive user terminal required" >&2; exit 2; }',
              'export ESPTOOL_PYTHON='+q(ESPTOOL_PYTHON), 'cd '+q(str(ROOT))]
    if mode == 'acceptance':
        argv = launch_argv('"$op"', '--plan', str(plan), '--out', str(plan_mod.OUTPUTS['acceptance']), '--reviewed-plan-sha256', digest)
        lines.append('exec '+' '.join(a if a == '"$op"' else q(a) for a in argv))
    else:
        for op in ('smoke-run', 'smoke-resume'):
            argv = launch_argv(op, '--plan', str(plan), '--out', str(plan_mod.OUTPUTS['smoke']), '--reviewed-plan-sha256', digest)
            lines.append(shlex.join(argv))
    return '\n'.join(lines+[''])


def recorded_validation(plan):
    """validate in a clean environment with the launcher's exact argv (env -i HOME PATH ESPTOOL_PYTHON)."""
    import os
    # The launching terminal's PATH: drop entries injected by prepare's own uv environment.
    path = ':'.join(x for x in os.environ['PATH'].split(':') if x and '.venv' not in x and '/.cache/uv/' not in x)
    argv = ['env', '-i', 'HOME='+os.environ['HOME'], 'PATH='+path, 'ESPTOOL_PYTHON='+ESPTOOL_PYTHON,
            *launch_argv('validate', '--plan', str(plan))[1:]]
    done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    return dict(validate_command=shlex.join(argv), cwd=str(ROOT), exit_code=done.returncode,
                stdout=done.stdout[-4000:], stderr=done.stderr[-4000:])


def preconditions(destination):
    """Freeze preconditions (section 10) recorded with their outputs."""
    import os
    env = dict(os.environ, OPENBLAS_NUM_THREADS='1', PYTHONPATH='hil:server')
    out = {}
    for name, files in (('registry_tests', ['hil/test_echoear_aec_gate_registry.py']),
                        ('lifecycle_test', ['hil/test_echoear_aec_v9_lifecycle.py']),
                        ('n1_survival', ['hil/test_echoear_aec_v9_n1.py']),
                        ('bot_burst_tests', ['server/test_aec_evaluation.py'])):
        run_env = dict(env, V9_N1_FULL='1', V9_N1_REPORT=str(Path(destination)/'n1-stress.json')) if name == 'n1_survival' else env
        done = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-s', '-p', 'no:cacheprovider', *files], cwd=ROOT, env=run_env,
                              capture_output=True, text=True)
        out[name] = dict(files=files, exit_code=done.returncode, tail=done.stdout[-1500:],
                         **({'stress_report': 'n1-stress.json'} if name == 'n1_survival' else {}))
    holds = [plan_mod.EVIDENCE/x for x in ('campaign-10/qualification-01', 'nova-live-02/qualification-01',
                                           'nova-live-01/qualification-01', 'aec-arms-04/visit-01-A')]
    from echoear_aec_v9_score import dry_run
    runs = [dry_run(h) for h in holds]
    write(destination/'analyzer-dry-run.json', runs)
    out['analyzer_dry_run'] = dict(holds=[r['hold'] for r in runs], attempts=sum(len(r['attempts']) for r in runs),
                                   exceptions=sum(len(r['errors']) for r in runs), output='analyzer-dry-run.json',
                                   passed=all(not r['errors'] for r in runs))
    bench = benchmark(holds[0])
    write(destination/'scoring-cost.json', bench)
    out['scoring_cost'] = dict(passed=bench['passed'], max_s=bench['max_s'], output='scoring-cost.json')
    out['admission_regression'] = admission_regression_record(destination)
    return out


GTEST_SOURCES = ('/home/khkramer/src/dg-esp32-nlp-diag/build/nlp-host/_deps/googletest-src',
                 '/home/khkramer/src/dg-esp32-echoear-display/build/host-peer/_deps/googletest-src')


def admission_regression_record(destination):
    """freeze.admission_regression: build and run the aec-v87 esp_peer host tests from `git archive` of the
    candidate commit (offline googletest source), retaining the log in the freeze bundle."""
    import tempfile
    from echoear_aec_v9_plan import CANDIDATE
    gtest = next((g for g in GTEST_SOURCES if Path(g).is_dir()), None)
    if gtest is None: return dict(passed=False, reason='No offline googletest source')
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = subprocess.run(f'git archive {CANDIDATE["firmware_commit"]} components third_party/pipecat-client-cxx/include'
                                 f' | tar -x -C {shlex.quote(str(tmp))}', shell=True, cwd=ROOT, capture_output=True, text=True)
        steps = [archive]
        if archive.returncode == 0:
            steps.append(subprocess.run(['cmake', '-S', str(tmp/'components/pipecat_esp_peer/test_host'), '-B', str(tmp/'build'),
                                         '-DFETCHCONTENT_SOURCE_DIR_GOOGLETEST='+gtest, '-DFETCHCONTENT_FULLY_DISCONNECTED=ON'],
                                        capture_output=True, text=True))
        if steps[-1].returncode == 0:
            steps.append(subprocess.run(['cmake', '--build', str(tmp/'build'), '-j8'], capture_output=True, text=True))
        if steps[-1].returncode == 0:
            steps.append(subprocess.run([str(tmp/'build/esp_peer_logic_tests')], capture_output=True, text=True))
        log = steps[-1].stdout+steps[-1].stderr
    (Path(destination)/'admission-host-regression.log').write_text(log)
    names = [l.split('] ', 1)[1].split(' (')[0] for l in log.splitlines() if l.startswith('[       OK ] MessageCarryover.')]
    return dict(candidate_commit=CANDIDATE['firmware_commit'], googletest_source=gtest, exit_code=steps[-1].returncode,
                passed=steps[-1].returncode == 0 and len(names) >= 9, carryover_tests=names,
                summary=[l for l in log.splitlines() if l.startswith('[  PASSED  ]') or l.startswith('[  FAILED  ]')],
                log='admission-host-regression.log')


def benchmark(hold, *, lengths=(1, 10, 25, 50)):
    """Per-attempt scoring + ledger append/checkpoint cost against ledger length (freeze.scoring_cost)."""
    import tempfile
    from echoear_aec_v9_score import score_attempt, HoldView, retained_kind, read
    hold = Path(hold)
    p = plan_mod.plan_from() if plan_mod.SOURCE.exists() else None
    declaration = read(hold/'declaration.json')
    view = HoldView(hold, declaration, retain=True)     # conservative: the whole hold stays in memory
    cases = sorted(read(hold/'result.json')['cases'], key=lambda c: c['start_ns'])
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp)/'ledger.jsonl', create=True)
        ledger.append(dict(type='campaign', mode='benchmark', plan_sha256='0'*64, drain_allowance_s=2.0))
        ledger.append(dict(type='part-start', part=1, role='planned', mode='benchmark'))
        i = 0
        for n in range(1, max(lengths)+1):
            c = cases[i % len(cases)]; i += 1
            started = time.perf_counter()
            entry = dict(kind=retained_kind(c), logical_id=c['id'])
            value = score_attempt(view, hold/c.get('capture_dir', c['run_id']), declaration, entry, case=c)
            ledger.append(dict(type='attempt', part=1, block_key=f'p1/b{n//5}', logical_id=f'bench-{n}', kind=entry['kind'],
                               attempt=1, attempt_id=f'p1/bench-{n}', folder='', source_sha256={}, findings=value['findings'],
                               scores=value['scores'], pending=False, record={}, timing={}, error=None))
            write(Path(tmp)/'state.json', verdict(ledger.state, p) if p else {})
            rows.append(dict(ledger_rows=len(ledger.rows), attempt=c['run_id'], seconds=time.perf_counter()-started))
    by_length = {n: rows[n-1]['seconds'] for n in lengths}
    # The first call pays imports and the one-time read of the retained hold's whole streams (a live part
    # reads them incrementally); it is reported separately, the runner warms the scorer at connect.
    warm = rows[1:]
    first, last = sum(r['seconds'] for r in warm[:10])/10, sum(r['seconds'] for r in warm[-10:])/10
    mx = max(r['seconds'] for r in warm)
    return dict(hold=str(hold), attempts=len(rows), cold_first_s=rows[0]['seconds'], max_s=mx, mean_s=sum(r['seconds'] for r in rows)/len(rows),
                mean_first10_s=first, mean_last10_s=last, by_ledger_length_s=by_length, rows=rows,
                passed=mx <= 2.0 and last <= first*1.5+0.1, limit_s=2.0,
                note='Same retained captures cycled; independence of ledger length = last-10 mean within 1.5x first-10 mean')


def prepare(destination=plan_mod.DESTINATION):
    destination = Path(destination)
    if destination.exists() or any(Path(v).exists() for v in plan_mod.OUTPUTS.values()):
        raise FileExistsError('Fresh freeze and absent outputs required')
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, text=True).strip():
        raise ValueError('Commit source before freezing')
    p = json.loads(json.dumps(plan_mod.bind(plan_mod.plan_from())))   # exactly as written to plan.json
    plan_mod.validate(p)
    destination.mkdir()
    try:
        plan = destination/'plan.json'; write(plan, p); digest = sha(plan)
        for mode, name in (('acceptance', 'launch-v9-acceptance.sh'), ('smoke', 'launch-v9-smoke.sh')):
            path = destination/name; path.write_text(launcher(plan, digest, mode=mode)); path.chmod(0o755)
            subprocess.run(['bash', '-n', str(path)], check=True)
        (destination/'launch-command.txt').write_text(
            f'Smoke (standing permission, no USER-GO):\n  {REVIEW_ENV}={digest} {destination/"launch-v9-smoke.sh"}\n'
            f'Acceptance part 1 (after reviews, smoke readiness and quiet-room USER-GO):\n'
            f'  {REVIEW_ENV}={digest} {destination/"launch-v9-acceptance.sh"} run\n'
            f'Acceptance part 2 / recovery part:\n  {REVIEW_ENV}={digest} {destination/"launch-v9-acceptance.sh"} resume\n')
        (destination/'stop-instructions.txt').write_text(
            'Use Ctrl-C in the launching terminal, or identify the exact Python child argv:\n'
            'pgrep -af '+shlex.quote(f'python {MODULE} ')+' (then confirm --plan '+str(plan)+')\n'
            'After verifying argv, kill -TERM <python-pid>. Never SIGKILL, never signal uv, never release the lock by hand, '
            'never use an outer flock. The stop ends acquisition (operator.stop, PAUSE-PART); the part then settles, '
            'reflashes and boot-verifies normal ELF '+NORMAL_HASH+', restores and byte-compares the mixer and releases the '
            'lock. Wait for the part-end ledger row and part-N/result.json: restored_elf_sha256, mixer_equal=true, '
            'lock_released=true, no cleanup_errors. Restore failure parks with the lock held. A stopped part is resumable '
            'with `resume` (same plan hash, fresh boot) while parts and the 150 min ceiling remain.\n')
        record = recorded_validation(plan)
        if record['exit_code'] != 0: raise RuntimeError('Clean-environment validation failed: '+record['stderr'][-800:])
        pre = preconditions(destination)
        passed = all(v.get('exit_code', 0) == 0 for k, v in pre.items() if 'exit_code' in v) and \
            pre['analyzer_dry_run']['passed'] and pre['scoring_cost']['passed'] and pre['admission_regression']['passed']
        write(destination/'validation.json', dict(validated=passed, plan_sha256=digest, prepared_from_commit=p['prepared_from_commit'],
              clean_environment_validation=record, freeze_preconditions=pre, launch_executed=False, user_go_created=False,
              hardware_access=False, outputs={k: str(v) for k, v in plan_mod.OUTPUTS.items()},
              outputs_absent=all(not Path(v).exists() for v in plan_mod.OUTPUTS.values()),
              pending_preconditions=['freeze.burst_smoke (bench smoke, then acceptance run derives the drain allowance)',
                                     'Reviews: two reviewers', "User's quiet-room USER-GO"]))
    except BaseException:
        shutil.rmtree(destination); raise
    return dict(plan_sha256=digest, destination=str(destination), validated=passed)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__); sub = ap.add_subparsers(dest='op', required=True)
    sub.add_parser('prepare')
    v = sub.add_parser('validate'); v.add_argument('--plan', type=Path, required=True)
    for op in ('run', 'resume', 'smoke-run', 'smoke-resume'):
        s = sub.add_parser(op); s.add_argument('--plan', type=Path, required=True); s.add_argument('--out', type=Path, required=True)
        s.add_argument('--reviewed-plan-sha256', required=True)
    a = sub.add_parser('analyze'); a.add_argument('--plan', type=Path, required=True); a.add_argument('--out', type=Path, required=True)
    d = sub.add_parser('dry-run'); d.add_argument('holds', type=Path, nargs='+')
    b = sub.add_parser('benchmark'); b.add_argument('hold', type=Path)
    r = sub.add_parser('rescore'); r.add_argument('--plan', type=Path, required=True); r.add_argument('--out', type=Path, required=True)
    r.add_argument('--amendment', type=Path, required=True)
    args = ap.parse_args(argv)
    if args.op == 'prepare': print(json.dumps(prepare(), indent=2))
    elif args.op == 'validate':
        plan_mod.validate(json.loads(args.plan.read_text()))
        print(json.dumps(dict(validated=True, plan_sha256=sha(args.plan), python=sys.executable)))
    elif args.op in ('run', 'smoke-run'):
        start(args.plan, args.out, args.reviewed_plan_sha256, mode='smoke' if args.op == 'smoke-run' else 'acceptance')
    elif args.op in ('resume', 'smoke-resume'):
        resume(args.plan, args.out, args.reviewed_plan_sha256, mode='smoke' if args.op == 'smoke-resume' else 'acceptance')
    elif args.op == 'analyze':
        p = json.loads(args.plan.read_text()); ledger = Ledger(args.out/'ledger.jsonl')
        result = dict(verdict(ledger.state, p), ledger_head=ledger.head)
        if ledger.rows[0]['mode'] == 'smoke': result['smoke'] = smoke_summary(p, ledger.rows)
        print(json.dumps(result, indent=2))
    elif args.op == 'dry-run':
        from echoear_aec_v9_score import dry_run
        print(json.dumps([dry_run(h) for h in args.holds], indent=1))
    elif args.op == 'benchmark': print(json.dumps(benchmark(args.hold), indent=1))
    else: rescore(args.plan, args.out, args.amendment)


if __name__ == '__main__':
    sys.modules['echoear_aec_v9'] = sys.modules[__name__]
    main()
