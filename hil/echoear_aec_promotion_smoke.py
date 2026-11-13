#!/usr/bin/env python3
"""AEC promotion smoke (hil/plans/echoear-aec-promotion-smoke-v1.json; decisions 109(a), 110).

An equivalence/promotion check of the merged AEC bundle under the inherited q1-aec-v87-01
qualification. Not a qualification run; freeze-03 evidence is only read.

Part 1 is one v9 smoke part (echoear_aec_v9.run_part, mode='smoke') on the q1 run's own
plan-effective.json (sha256-pinned) with ONLY the candidate identity replaced: no reconnect, no plan
injection, the unmodified qualified hold (V9Hold -> NovaHold -> BenchHold) and the unmodified
part_audit over one generation. The promotion gate policy is applied through the Runner's acoustic
hook (promotion_acoustic): acoustic SCORE findings are retained as evidence but cannot STOP or fail
the smoke; candidate faults, hard false-event findings and the gated flush/false-turn checks stay
authoritative. No v9 rule module is changed.

Part 2 is a separate reconnect check in its own hold (echoear_rollback.AecReconnectLive with
hil/plans/echoear-aec-reconnect-v1.json): the mute gate's reviewed reconnect machinery on the AEC
bundle, each generation audited against its own start and terminal boundaries.
"""
import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT/'hil/plans/echoear-aec-promotion-smoke-v1.json'
DRAIN_ALLOWANCE_S = 4.0          # echoear_aec_q.Q default, the value q1-aec-v87-01 ran with
SCHEDULED = ('v9-echo-m6-t01-g1', 'v9-echo-m6-t02-g1', 'v9-stop-firm-r01')
SMOKE_FAIL_GATES = {
    'echo': ('echo.false_turns', 'echo.event_coverage', 'echo.unscorable'),
    'barge-firm-stop': ('barge.false_interruption', 'barge.cancelled_lull', 'flush.callback', 'flush.next_reply',
                        'flush.interrupt_copies', 'flush.stale_audio'),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_spec(path=SPEC):
    return json.loads(Path(path).read_text()), sha(path)


def verify_bundle_manifest(spec, manifest_path, manifest_sha):
    """The launcher passes the bundle manifest and its sha; both must match the spec and the files."""
    manifest_path = Path(manifest_path)
    if sha(manifest_path) != manifest_sha:
        raise ValueError('Bundle manifest sha256 mismatch')
    m = json.loads(manifest_path.read_text())
    image = spec['image']
    bundle = manifest_path.parent
    if str(bundle) != image['bundle'] or m.get('elf_sha256') != image['elf_sha256']:
        raise ValueError('Bundle is not the declared promotion target')
    if m.get('aec_enabled') is not True or m.get('boot_ref_gain_db') != image['boot_ref_gain_db'] \
            or m.get('firmware_commit') != image['firmware_commit']:
        raise ValueError('Bundle mode/gain/commit differ from the spec')
    cfg = m.get('public_config', {})
    want = dict(CONFIG_DG_ECHOEAR_AEC=1, CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15, CONFIG_DG_ECHOEAR_AEC_GUARD_MS=400,
                CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS=2000, CONFIG_DG_DEMO_AUTOSTART=1, CONFIG_DG_WIFI_OBSERVE=1)
    if any(cfg.get(k) != v for k, v in want.items()):
        raise ValueError('Bundle public config differs from the qualified settings')
    for name, h in m['sha256'].items():
        if sha(bundle/name) != h:
            raise ValueError('Bundle file changed: '+name)
    proof = json.loads(Path(image['proof_of_record']).read_text())
    app = proof['images']['app']['right']
    if not (proof['masked_equal'] is True and proof['unmasked_differing_bytes'] == 0 and proof['flash_options_equal'] is True
            and app['sha256'] == m['sha256']['dg-esp32.bin'] and app['descriptor']['elf_sha256'] == image['elf_sha256']
            and proof['images']['app']['left']['descriptor']['elf_sha256'] == spec['qualified']['elf_sha256']
            and proof['images']['bootloader']['right']['sha256'] == m['sha256']['bootloader/bootloader.bin']
            and proof['images']['partition-table']['raw_equal'] is True):
        raise ValueError('Masked-compare proof does not bind this bundle to the qualified image')
    return m


def profile_config_sha256(profile):
    """demo_policy.profile_manifest's aggregate: sha256 of the sorted-key JSON of config/versions/models/code."""
    payload = {k: profile[k] for k in ('config', 'versions', 'local_model_sha256', 'code_sha256')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def derive_runtime_profile(spec, base_profile):
    """Decision 113 / Astra r8: the q1 profile with ONLY the reviewed bot delta.

    The inherited NovaHold.connect/capture/finish compare the live stt-profile event with p['profile'] by exact
    equality (echoear_aec_nova_prestep.profile_witness). The decision-113 bot (alarm-line pc_id label) changes
    code_sha256['demo_bot.py'] and therefore config_sha256; nothing else may differ. The derivation is checked
    against the spec's pinned values; the live bot is then compared to the derived profile exactly."""
    pin = spec['runtime_profile']
    if profile_config_sha256(base_profile) != base_profile['config_sha256']:
        raise ValueError('Qualified profile is not self-consistent')
    if base_profile['code_sha256'].get('demo_bot.py') != pin['qualified_demo_bot_sha256'] \
            or base_profile['config_sha256'] != pin['qualified_config_sha256']:
        raise ValueError('Qualified profile differs from the pinned qualified bot')
    derived = copy.deepcopy(base_profile)
    derived['code_sha256']['demo_bot.py'] = pin['demo_bot_sha256']
    derived['config_sha256'] = profile_config_sha256(derived)
    if derived['config_sha256'] != pin['config_sha256']:
        raise ValueError('Derived runtime profile config_sha256 differs from the pinned decision-113 profile')
    changed = sorted(k for k in set(base_profile) | set(derived) if base_profile.get(k) != derived.get(k))
    if changed != ['code_sha256', 'config_sha256'] or \
            {k for k in derived['code_sha256'] if derived['code_sha256'][k] != base_profile['code_sha256'].get(k)} != {'demo_bot.py'}:
        raise ValueError('Runtime profile delta is not exactly the decision-113 bot hash')
    return derived


def check_live_profile(p, live=None):
    """The checked-out bot must produce exactly the plan's runtime profile (validate and run time).
    Any other change - another code file, a setting, a package version or a model hash - is an error."""
    if live is None:
        from demo_policy import profile_manifest
        live = profile_manifest('dev')
    if live != p['profile']:
        diff = sorted(k for k in set(live) | set(p['profile']) if live.get(k) != p['profile'].get(k))
        raise ValueError('Checked-out bot profile differs from the pinned runtime profile: '+', '.join(diff))
    return True


def build_plan(spec, manifest):
    """The q1 plan with ONLY the candidate identity replaced and the declared smoke schedule set."""
    base = Path(spec['base_plan']['path'])
    if sha(base) != spec['base_plan']['sha256']:
        raise ValueError('Base plan changed')
    p = json.loads(base.read_text())
    qualified = spec['qualified']['elf_sha256']
    if p['elf_sha256'] != qualified or p['candidate']['elf_sha256'] != qualified:
        raise ValueError('Base plan is not the qualified run')
    p = copy.deepcopy(p)
    image = spec['image']
    p['image'] = image['bundle']
    p['elf_sha256'] = image['elf_sha256']
    if p['boot_ref_gain_db'] != image['boot_ref_gain_db'] or p['gain_db'] != image['boot_ref_gain_db']:
        raise ValueError('Base plan gain differs')
    p['candidate'].update(elf_sha256=image['elf_sha256'], firmware_commit=image['firmware_commit'],
                          image_bundle_sha256=sha(Path(image['bundle'])/'manifest.json'),
                          review_artifact_manifest=str(Path(image['bundle'])/'manifest.json'),
                          review_build_record=dict(path=image['proof_of_record'], sha256=sha(image['proof_of_record'])),
                          promotion_of=qualified)
    if 'playback_config' in p['candidate']:
        p['candidate']['playback_config']['elf_sha256'] = image['elf_sha256']
    cells = {c['logical_id'] for c in p['v9']['cells']}
    missing = [lid for lid in SCHEDULED if lid not in cells]
    if missing:
        raise ValueError('Scheduled cells missing from the base plan: '+', '.join(missing))
    p['v9']['smoke_schedule'] = [dict(part=1, burst=False, blocks=[dict(id='promo-b1', kind='mixed', attempts=list(SCHEDULED))])]
    if p.get('bot_config') != spec['bot']['bot_config']:
        raise ValueError('Bot settings differ from the pinned public bot_config')
    pin = spec['stop_fixture']
    stop = next(c for c in p['v9']['cells'] if c['logical_id'] == pin['cell'])['cell']
    if (stop.get('fixture') != pin['path'] or stop.get('fixture_sha256') != pin['sha256']
            or stop.get('stop_offset_s') != pin['offset_s'] or sha(pin['path']) != pin['sha256']):
        raise ValueError('Stop fixture differs from the pinned fixture')
    if p.get('reconnects'):
        raise ValueError('Part 1 declares no reconnect')
    p['profile'] = derive_runtime_profile(spec, p['profile'])
    p['outputs'] = dict(smoke=spec['output'])
    p['promotion_smoke'] = dict(spec_id=spec['id'], qualified_elf_sha256=qualified)
    return p


def report_only_gates():
    """Acoustic SCORE gates: retained as evidence, never a STOP or a smoke failure. Every other class
    (CANDIDATE-FAIL, PAUSE-PART, INVALID-CELL, MONITOR) and the gated SCORE checks pass through."""
    from echoear_aec_gate_registry import BY_ID
    gated = {g for gates in SMOKE_FAIL_GATES.values() for g in gates}
    return frozenset(k for k, v in BY_ID.items() if v['cls'] == 'SCORE' and k not in gated)


def promotion_acoustic(base=None):
    """Runner acoustic hook. The qualified scorer runs unchanged; its report-only acoustic findings are
    moved into the record (promotion_report_only) and an acoustic 'miss' score is retained there and
    passed on as 'ineligible', so no acoustic outcome reaches classify_attempt, the hard-event list or
    the stratum/group bars. Integrity findings (computed before this hook) are untouched."""
    report_only = report_only_gates()
    def acoustic(view, plan, entry, a):
        if base is None:
            import echoear_aec_v9_score as score
            sc, findings, record = score.acoustic(view, plan, entry, a)
        else:
            sc, findings, record = base(view, plan, entry, a)
        kept = [f for f in findings if f['gate'] not in report_only]
        moved = [f for f in findings if f['gate'] in report_only]
        record = dict(record, promotion_report_only=moved)
        sc = {k: dict(v) for k, v in (sc or {}).items()}
        if sc.get('acoustic', {}).get('result') == 'miss':
            record['promotion_acoustic_score'] = sc['acoustic']
            sc['acoustic'] = dict(sc['acoustic'], result='ineligible', promotion_report_only=True)
        return sc, kept, record
    return acoustic


def runtime_sha256():
    """Code identity of the run, as q1/v9 record it: the v9 runtime files plus the promotion files."""
    import echoear_aec_v9_plan as plan_mod
    files = set(plan_mod.runtime_files()) | {'hil/echoear_aec_promotion_smoke.py', 'hil/echoear_rollback.py',
        'hil/echoear_mute_gate.py', 'hil/plans/echoear-aec-promotion-smoke-v1.json', 'hil/plans/echoear-aec-reconnect-v1.json'}
    return {f: sha(ROOT/f) for f in sorted(files) if (ROOT/f).exists()}


def reconnect_plan(spec):
    part2 = spec['reconnect_part']
    path = ROOT/part2['plan']
    if sha(path) != part2['plan_sha256']:
        raise ValueError('Reconnect plan changed')
    plan = json.loads(path.read_text())
    if plan['image'] != dict(plan['image'], bundle=spec['image']['bundle'], elf_sha256=spec['image']['elf_sha256'],
                             firmware_commit=spec['image']['firmware_commit'], boot_ref_gain_db=spec['image']['boot_ref_gain_db']):
        raise ValueError('Reconnect plan image differs from the promotion target')
    import echoear_mute_gate as g
    g.check_idle_budget(plan)          # decision 118: no planned runner wait reaches the bot idle timeout
    return plan, sha(path)


def serial_lines(hold_dir, label='candidate'):
    path = Path(hold_dir)/f'{label}-serial.log'
    return path.read_text(errors='replace').splitlines() if path.exists() else []


def perf(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def get(p, dotted):
    for key in dotted.split('.'):
        if not isinstance(p, dict) or key not in p:
            return None
        p = p[key]
    return p


CANCEL = re.compile(r'AUDIO_CANCEL callback_us=-?\d+ device_us=(\d+) gen=\d+ applied=([01])')


def stop_accounting(stop, attempts, lines, out):
    """Stop accounting scoped to attempt windows (device clock, before.json -> after.json).

    The Stop attempt's window holds exactly one AUDIO_CANCEL, applied=1, with cancellation.events +1 and
    duplicate notices >= 0 (redundant copies are dropped by the transport and never reach the callback).
    A near-witness window may hold its own expected Stop cancel(s): attributed to it and counted. A
    cancel inside an echo window, or outside every attempt window, is unattributed and fails."""
    out = Path(out)
    windows = []
    for a in attempts:
        folder = out/a.get('folder', '')
        b, f = perf(folder/'before.json'), perf(folder/'after.json')
        if b and f and type(b.get('device_us')) is int and type(f.get('device_us')) is int:
            windows.append(dict(attempt_id=a.get('attempt_id'), logical_id=a['logical_id'], kind=a.get('kind'),
                                lo=b['device_us'], hi=f['device_us'], before=b, after=f))
    cancels = []
    for line in lines:
        m = CANCEL.search(line)
        if m:
            cancels.append(dict(device_us=int(m[1]), applied=m[2] == '1', line=line))
        elif 'AUDIO_CANCEL' in line:
            cancels.append(dict(device_us=None, applied=None, line=line))
    by = {}
    unattributed = []
    for c in cancels:
        owner = next((w for w in windows if c['device_us'] is not None and w['lo'] <= c['device_us'] <= w['hi']), None)
        if owner is None or owner['kind'] == 'echo':
            unattributed.append(dict(c, owner=owner and owner['attempt_id']))
        else:
            by.setdefault(owner['attempt_id'], []).append(c)
    stop_windows = [w for w in windows if w['logical_id'] == SCHEDULED[2]]
    w = stop_windows[-1] if stop_windows else None
    own = by.get(w['attempt_id'], []) if w else []
    events = dupes = None
    if w:
        ev = [get(x, 'cancellation.events') for x in (w['before'], w['after'])]
        du = [get(x, 'cancellation.duplicate_notices') for x in (w['before'], w['after'])]
        if all(type(v) is int for v in ev+du):
            events, dupes = ev[1]-ev[0], du[1]-du[0]
    witness = {k: len(v) for k, v in by.items() if not w or k != w['attempt_id']}
    # Fable r4 item 4: every attributed non-Stop window is bounded: its cancel lines equal its
    # cancellation.events delta and every one is applied (q1 hold-1: 10/10 windows satisfy this).
    unbounded = []
    for win in windows:
        if win['kind'] == 'echo' or (w and win['attempt_id'] == w['attempt_id']):
            continue
        mine = by.get(win['attempt_id'], [])
        ev = [get(x, 'cancellation.events') for x in (win['before'], win['after'])]
        delta = ev[1]-ev[0] if all(type(v) is int for v in ev) else None
        if (mine or delta) and (delta is None or len(mine) != delta or not all(c['applied'] for c in mine)):
            unbounded.append(dict(attempt_id=win['attempt_id'], cancels=len(mine), events_delta=delta,
                                  applied=[c['applied'] for c in mine]))
    stop = dict(stop, stop_window_cancels=len(own), stop_window_applied=sum(1 for c in own if c['applied']),
                cancellation_events_delta=events, duplicate_notices_delta=dupes, witness_cancels=witness,
                unattributed_cancels=unattributed, unbounded_windows=unbounded, total_cancel_lines=len(cancels),
                rule='per attempt window: Stop = exactly one applied cancel, events +1, duplicates no-op; witnesses attributed; anything else unattributed fails')
    stop['passed'] = bool(stop.get('passed') and w is not None and len(own) == 1 and own[0]['applied'] and events == 1
                          and dupes is not None and dupes >= 0 and not unattributed and not unbounded)
    return stop


DISCONNECT_STAMPS = 'part1-disconnect-stamps.json'


class stamp_disconnects:
    """Promotion-wrapper-only stamp of part 1's deliberate device disconnect (decision 114).

    The qualified v9/bench hold sends its final 'disconnect' through echoear_aec_campaign.command (BenchHold.finish
    calls the module global; V9Hold.finish_peer_lost calls C.command). For the duration of run_part this wrapper
    replaces that one module attribute with a pass-through that, for a 'disconnect' only, brackets the console
    board.send with time.time() - loguru's clock - exactly as MuteLive.disconnect does. No v9/bench module is
    edited; every other command is the original call. The stamps are written to <out>/part1-disconnect-stamps.json
    after each disconnect (ok=False if the command raised)."""
    def __init__(self, path, wall=None, mono=None):
        import time
        self.path, self.wall, self.mono, self.rows = Path(path), wall or time.time, mono or time.monotonic, []
    def __enter__(self):
        import echoear_aec_campaign as C
        self.module, self.original = C, C.command
        original, rows, wall, mono, path = self.original, self.rows, self.wall, self.mono, self.path
        def command(board, text, timeout=8):
            if text != 'disconnect':
                return original(board, text, timeout)
            row = dict(command='disconnect', ok=False)
            send, own = board.send, 'send' in vars(board)
            def stamped(t):
                if t != 'disconnect' or 'sent_wall' in row:      # only this command's own console send
                    return send(t)
                row['lower_wall'] = wall(); seq = send(t); row['sent_wall'] = wall()
                row['sent_mono'] = mono()          # clock pair with sent_wall: maps evaluation event t (monotonic) to wall
                return seq
            board.send = stamped
            try:
                value = original(board, text, timeout)
                row['ok'] = True
                return value
            finally:
                if own: board.send = send
                else: del board.send
                rows.append(row)
                path.write_text(json.dumps(rows, indent=1))
        C.command = command
        return self
    def __exit__(self, *exc):
        self.module.command = self.original
        return False


EVENT_SILENT_TOLERANCE_S = 0.0015     # the line rounds silent_s to 3 decimals
EVENT_TIME_TOLERANCE_S = 0.05        # event t (monotonic) vs alarm line (wall, ms-truncated) through one clock pair


def part1_liveness_rule(plan2, pc, stamps, bot_log, lines, events=None):
    """Pure core: the part's one peer `pc`, its stamped deliberate disconnects `stamps` (the final one counts).

    events: the part's evaluation events.jsonl rows (the q1 bot runs evaluation in part 1). Decision 115: every
    'liveness-alarm' event must be for the part's peer and pair with exactly one attributed alarm line whose
    silent_s agrees (rounding) and whose wall time agrees with the event's monotonic t mapped through the final
    stamp's (sent_wall, sent_mono) pair; a missing event, an extra event, another peer or a disagreement FAILs.
    events=None skips this corroboration (callers without an evaluation directory)."""
    import echoear_mute_gate as g
    stamps = [r for r in (stamps or []) if r.get('command') == 'disconnect']
    final = stamps[-1] if stamps else None
    peers = [dict(id='part-1', pc_id=pc, live_from_wall=0.0,
                  disconnect=dict(lower_wall=final.get('lower_wall'), sent_wall=final.get('sent_wall'), ok=final.get('ok')) if final else None)]
    result = g.liveness_logs_attributed(bot_log, lines, peers if pc else [], plan2)
    out = dict(result, part_peer=pc, disconnect_stamps=stamps,
               rule='decision-114/115: part 1 bot alarm under the tagged rule with the last-timely witness')
    if events is None:
        return out
    alarm_events = [e for e in events if e.get('event') == 'liveness-alarm']
    problems = []
    offset = (final['sent_wall']-final['sent_mono']) if final and type(final.get('sent_mono')) in (int, float) \
        and type(final.get('sent_wall')) in (int, float) else None
    used = set()
    for a in result['alarms']:
        if a.get('status') != 'EXPECTED':
            continue
        mine = [i for i, e in enumerate(alarm_events) if e.get('pc_id') == pc and i not in used]
        if len(mine) != 1:
            problems.append('alarm line %d: %d matching liveness-alarm events' % (a['line'], len(mine))); continue
        e = alarm_events[mine[0]]; used.add(mine[0])
        a['event'] = dict(t=e.get('t'), silent_s=e.get('silent_s'))
        if type(e.get('silent_s')) not in (int, float) or abs(e['silent_s']-a['silent_s']) > EVENT_SILENT_TOLERANCE_S:
            problems.append('alarm line %d: event silent_s %r disagrees with %r' % (a['line'], e.get('silent_s'), a['silent_s']))
        if offset is None or type(e.get('t')) not in (int, float):
            problems.append('alarm line %d: no clock pair to place the event' % a['line'])
        elif abs(e['t']+offset-a['wall']) > EVENT_TIME_TOLERANCE_S:
            problems.append('alarm line %d: event time %.3f s from the line' % (a['line'], e['t']+offset-a['wall']))
    for i, e in enumerate(alarm_events):
        if i not in used:
            problems.append('liveness-alarm event without an attributed alarm line (pc %s)' % e.get('pc_id'))
    out.update(event_corroboration=dict(passed=not problems, problems=problems, events=len(alarm_events),
                                        clock_offset_s=offset))
    out['passed'] = bool(result['passed'] and not problems)
    return out


def check_part1_liveness_period(spec):
    """Preflight (validate and run, before any acquisition; Fable r10 should-fix 3 - never inside judge, where it
    could mask the part's own exception): the q1 bot's liveness period must be the declared liveness_logs period."""
    plan2, _ = reconnect_plan(spec)
    if spec['bot']['bot_config'].get('ECHOEAR_LIVENESS_SECONDS') != str(plan2['liveness']['period_s']):
        raise ValueError('Part 1 bot liveness period differs from the declared liveness_logs block')
    return True


def disconnect_stamp_check(out, completed):
    """Fable r10 should-fix 1: a completed part must have exactly one ok, fully stamped final disconnect; otherwise
    a missed stamp with no alarm would pass silently."""
    rows = [r for r in (perf(Path(out)/DISCONNECT_STAMPS) or []) if r.get('command') == 'disconnect']
    good = [r for r in rows if r.get('ok') is True and all(type(r.get(k)) in (int, float) for k in ('lower_wall', 'sent_wall', 'sent_mono'))]
    if not completed:
        return dict(passed=True, applicable=False, stamps=len(rows), basis='part did not complete')
    return dict(passed=len(rows) == 1 and len(good) == 1, applicable=True, stamps=len(rows), ok_stamps=len(good))


def part1_bot_liveness(spec, hold, out, bot_log, lines, stamps=None, events=None):
    """Decision 114: part 1's bot alarm clause under the reviewed decision-113 tagged rule (mute_gate code, the
    reconnect plan's declared liveness/liveness_logs block; the q1 bot runs liveness at the same period). The
    part has one peer (connected.json pc_id); its only deliberate disconnect is the final one, stamped by
    stamp_disconnects. An alarm is allowed only on that peer, after that send, inside [send + tolerance - period
    - early_s, send + tolerance + margin]; at most one. Untagged, another peer, outside the window, a second
    alarm, or no witnessed disconnect FAIL. Device 'LIVENESS no ping' stays absolute."""
    plan2, _ = reconnect_plan(spec)
    pc = (perf(Path(hold)/'connected.json') or {}).get('pc_id')
    if stamps is None:
        stamps = perf(Path(out)/DISCONNECT_STAMPS) or []
    if events is None:
        path = Path(hold)/'evaluation/events.jsonl'
        events = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    return part1_liveness_rule(plan2, pc, stamps, bot_log, lines, events)


def judge(spec, p, rows, out):
    """Part 1 verdict from the retained ledger and hold evidence. Each spec step maps to one check."""
    out = Path(out)
    hold = out/'part-1'
    checks = {}
    ends = [r for r in rows if r['type'] == 'part-end']
    attempts = [r for r in rows if r['type'] == 'attempt']
    end = ends[-1] if ends else {}
    restore = end.get('restore') or {}
    result = perf(hold/'result.json') or {}
    lines = serial_lines(hold)
    bot_log = '\n'.join(path.read_text(errors='replace') for path in sorted((hold/'bot').glob('server-*.log')))
    elf = spec['image']['elf_sha256']
    boot = perf(hold/'candidate-boot-gain.json') or {}
    checks['flash'] = dict(passed=any('elf_sha256='+elf in line for line in lines)
                           and (perf(hold/'candidate-image.json') or {}).get('elf_sha256') == elf)
    checks['gain'] = dict(passed=boot.get('verified') is True and boot.get('declared_gain_db') == 15
                          and 'MIC3_GAIN=0x15 analog_db=15' in (boot.get('boot_line') or ''), boot_line=boot.get('boot_line'))
    connected = perf(hold/'connected.json') or {}
    checks['capability'] = dict(
        passed=any('AUDIO_CAPABILITY aec=1 ref_gain_db=15 guard_ms=400' in line for line in lines)
        and 'DEMO_DUPLEX aec_full_duplex=True user_mute=False source=capability-or-safe-fallback' in bot_log
        and get(connected, 'aec.enabled') is True)
    by_lid = {}
    for a in attempts:
        by_lid.setdefault(a['logical_id'], []).append(a)
    def attempt_check(lid, kind):
        rows_ = by_lid.get(lid, [])
        if not rows_:
            return dict(passed=False, reason='not exercised')
        a = rows_[-1]
        gates = sorted({f['gate'] for f in a.get('findings', [])})
        bad = [g for g in gates if g in SMOKE_FAIL_GATES[kind]]
        return dict(passed=not a['error'] and not a['pending'] and not bad, attempts=len(rows_), error=a['error'],
                    failing_gates=bad, report_only_gates=[g for g in gates if g not in bad], folder=a.get('folder'))
    checks['cold'] = attempt_check(SCHEDULED[0], 'echo')
    checks['warm'] = attempt_check(SCHEDULED[1], 'echo')
    stop = attempt_check(SCHEDULED[2], 'barge-firm-stop')
    checks['stop'] = stop_accounting(stop, attempts, lines, out)
    checks['recovery'] = dict(passed=checks['stop']['passed'] and 'flush.next_reply' not in stop.get('failing_gates', []),
                              basis='v9 flush.next_reply gate of the stop cell (real_bot_reply)')
    settled = perf(hold/'settled-end.json') or {}
    checks['display'] = dict(passed=type(get(connected, 'display.rendered')) is int and type(get(settled, 'display.rendered')) is int
                             and get(settled, 'display.rendered') > get(connected, 'display.rendered')
                             and get(settled, 'display.failed') is False and get(settled, 'display.dead') is False)
    aec_keys = ('aec.errors', 'aec.reference_clips', 'aec.output_clips', 'aec.capture.read_errors',
                'aec.capture.read_timeouts', 'aec.capture.adc_clips', 'aec.capture.gain_clips')
    zero = all(get(settled, k) == 0 for k in aec_keys)
    live = get(settled, 'liveness') or {}
    # Decision 114: the bot clause takes the decision-113 tagged rule (part1_bot_liveness); the device liveness
    # counters (pongs > 0, timeouts 0) and device 'LIVENESS no ping' stay absolute.
    bot_liveness = part1_bot_liveness(spec, hold, out, bot_log, lines)
    checks['counters'] = dict(passed=bool(end.get('audit', {}).get('clean')) and zero and type(live.get('pongs')) is int
                              and live['pongs'] > 0 and live.get('timeouts') == 0 and bot_liveness['passed'],
                              part_audit_clean=end.get('audit', {}).get('clean'), aec_counters_zero=zero, liveness=live,
                              bot_liveness=bot_liveness, part_gates=end.get('gates'))
    checks['restore'] = dict(passed=restore.get('clean') is True and restore.get('restored_elf_sha256') == spec['normal_elf_sha256'],
                             restore=restore)
    lifecycle = dict(passed=end.get('reason') == 'complete' and not end.get('error') and not end.get('over_budget'),
                     reason=end.get('reason'), error=end.get('error'))
    checks['lifecycle'] = lifecycle
    checks['disconnect_stamp'] = disconnect_stamp_check(out, end.get('reason') == 'complete')
    faults = [r for r in rows if r['type'] == 'campaign-close' and r.get('reason') in ('FAIL', 'STOP')]
    infra = not lifecycle['passed'] and not faults and end.get('reason') in ('PAUSE-PART', 'INCONCLUSIVE')
    passed = all(c['passed'] for c in checks.values())
    outcome = 'PASS' if passed else ('INCOMPLETE' if infra and checks['restore']['passed'] else 'FAIL')
    return dict(outcome=outcome, checks=checks, spec_id=spec['id'], qualification=False,
                claim=spec['claim'])


def launch_checks(spec, out, now=None):
    """Fresh output, a terminal, and the whole smoke (part-1 smoke ceiling + restore reserve, part-2
    acquisition deadline + cleanup reserve) clear of the 03:00-04:10 exclusion."""
    import echoear_mute_gate as g
    from datetime import datetime
    out = Path(out)
    b = spec['budget']
    if g.exclusion_conflict(now or datetime.now(), b['worst_case_s'], b['exclusion_local_minutes']):
        raise ValueError('Promotion smoke refused: the worst case would reach the 03:00-04:10 exclusion')
    if out != Path(spec['output']):
        raise ValueError('Output must be the spec output '+spec['output'])
    if out.exists() or out.is_symlink():
        raise FileExistsError('Output exists: '+str(out))
    if not sys.stdin.isatty():
        raise ValueError('A terminal is required (interactive, or a persistent PTY: tmux + script)')


def part2_declaration(spec, plan2, plan2_sha):
    import echoear_rollback as r
    return dict(schema='aec-reconnect-01-v1', plan_sha256=plan2_sha, public_env=plan2['public_env'],
                fixtures=r.mute_fixtures(plan2), bot_commit=r.head(), runner_commit=r.head())


def run(manifest, manifest_sha, out, *, spec_path=SPEC, run_part=None, ledger_type=None, part2=None,
        acoustic=None, hold_factory=None, budget_factory=None, execute=None, live_profile=None):
    """Part 1 (v9 smoke part, one generation), then - only after a part-1 PASS - part 2 (reconnect check,
    own hold). Both retain their evidence and verdict; the promotion verdict is their conjunction."""
    spec, spec_sha = load_spec(spec_path)
    launch_checks(spec, out)
    m = verify_bundle_manifest(spec, manifest, manifest_sha)
    p = build_plan(spec, m)
    check_live_profile(p, live_profile)
    check_part1_liveness_period(spec)
    plan2, plan2_sha = reconnect_plan(spec)
    if run_part is None:
        import echoear_aec_q
        echoear_aec_q.preflight()
        import echoear_aec_v9 as v9
        run_part, ledger_type = v9.run_part, v9.Ledger
    out = Path(out)
    out.mkdir(parents=True)
    effective = json.dumps(p, sort_keys=True, default=str).encode()
    (out/'plan-effective.json').write_bytes(effective)
    (out/'spec.json').write_text(json.dumps(dict(spec_sha256=spec_sha, bundle_manifest=str(manifest),
                                                 bundle_manifest_sha256=manifest_sha, reconnect_plan_sha256=plan2_sha), indent=1))
    ledger = ledger_type(out/'ledger.jsonl', create=True)
    ledger.append(dict(type='campaign', mode='smoke', plan_sha256=hashlib.sha256(effective).hexdigest(),
                       runtime_sha256=runtime_sha256(), drain_allowance_s=DRAIN_ALLOWANCE_S, promotion_smoke=spec['id']))
    kwargs = dict(mode='smoke', allowance_s=DRAIN_ALLOWANCE_S, acoustic=promotion_acoustic(acoustic))
    if hold_factory is not None: kwargs['hold_factory'] = hold_factory
    if budget_factory is not None: kwargs['budget_factory'] = budget_factory
    verdict = {}
    try:
        with stamp_disconnects(out/DISCONNECT_STAMPS):
            run_part(p, out, ledger, 1, **kwargs)
    finally:
        verdict['part1'] = judge(spec, p, ledger.rows, out)
        verdict['part2'] = dict(outcome='NOT-RUN', reason='part 1 did not PASS')
        (out/'promotion-verdict.json').write_text(json.dumps(verdict, indent=1, default=str))
    if verdict['part1']['outcome'] == 'PASS':
        import echoear_rollback as r
        try:
            r.mute_start_allowed(plan2)
        except ValueError as exc:
            verdict['part2'] = dict(outcome='INCOMPLETE', reason=str(exc))
            return finish(verdict, spec, out)
        out2 = out/'part-2'
        out2.mkdir()
        decl = part2_declaration(spec, plan2, plan2_sha)
        (out2/'declaration.json').write_text(json.dumps(decl, indent=1))
        live = (part2 or r.AecReconnectLive)(decl, plan2, out2)
        try:
            result = r.protected_execute(live, out2, plan2['budget']['acquisition_deadline_s'], execute_fn=execute)
        except BaseException as exc:
            # Fable r5 should-fix 1: a crash escaping the wrapper (e.g. release() after a verified restore)
            # must not leave "part 1 did not PASS" in the verdict. Record it, write the verdict, re-raise.
            # Fable r6 should-fix 4: a witnessed gate FAIL/INVALID stays authoritative, as in part2_outcome.
            gate_outcome = ((perf(out2/'gate.json') or {}).get('verdict') or {}).get('outcome')
            verdict['part2'] = dict(outcome=gate_outcome if gate_outcome in ('FAIL', 'INVALID') else 'INCOMPLETE',
                                    reason='part 2 raised out of protected_execute',
                                    error=f'{type(exc).__name__}: {exc}', gate_outcome=gate_outcome)
            finish(verdict, spec, out)
            raise
        verdict['part2'] = part2_outcome(result, perf(out2/'gate.json'), spec['normal_elf_sha256'])
    return finish(verdict, spec, out)


def part2_outcome(result, gate, normal):
    """Compose part 2 from execute()'s result AND the gate; the execution result is authoritative.

    PASS only if execute() passed (no execution error and a clean FIRST cleanup), the gate is PASS, the
    final cleanup restored the normal image and the lock was released. A gate FAIL/INVALID is that
    outcome. Everything else is INCOMPLETE, with the cause retained:
      - gate PASS but execute() failed (an error after the gate, or a failed first cleanup that a retry
        later repaired): the acquisition may be sound but the protected lifecycle did not complete
        cleanly, so it cannot be credited (not FAIL: no candidate defect was witnessed);
      - no gate (a pre-gate exception: bot start, preload, ALSA, connect) with a clean final restore;
      - a gate INCOMPLETE.
    execute() only returns after a verified restore, so 'bench restored' is recorded either way."""
    attempts = result.get('cleanup_attempts') or [{}]
    last, first = attempts[-1], attempts[0]
    restored = last.get('restored_elf_sha256') == normal or last.get('device_untouched') is True
    gate_outcome = ((gate or {}).get('verdict') or {}).get('outcome')
    out = dict(gate_outcome=gate_outcome, execution_passed=bool(result.get('passed')), error=result.get('error'),
               cleanup_attempts=len(attempts), first_cleanup_errors=first.get('errors'), restored_elf_sha256=last.get('restored_elf_sha256'),
               lock_released=result.get('lock_released'), bench_restored=bool(restored and result.get('lock_released')))
    if gate_outcome in ('FAIL', 'INVALID'):
        return dict(out, outcome=gate_outcome)
    if result.get('passed') and gate_outcome == 'PASS' and restored and result.get('lock_released') is True and len(attempts) == 1:
        return dict(out, outcome='PASS')
    if gate_outcome == 'PASS':
        if result.get('error'):
            why = 'execution error after the gate'
        elif len(attempts) > 1:
            why = 'first cleanup failed; a retry restored the bench'
        elif not result.get('passed'):
            why = 'execute() not passed with one clean cleanup (result journal or lifecycle failure)'
        else:
            why = 'restore or lock release not witnessed'
        return dict(out, outcome='INCOMPLETE', reason=why)
    if gate_outcome is None:
        return dict(out, outcome='INCOMPLETE', reason='pre-gate exception' if result.get('error') else 'no gate written')
    return dict(out, outcome='INCOMPLETE', reason='gate '+str(gate_outcome))


def finish(verdict, spec, out):
    outcomes = [verdict['part1']['outcome'], verdict['part2']['outcome']]
    verdict['outcome'] = 'PASS' if outcomes == ['PASS', 'PASS'] else (
        'FAIL' if 'FAIL' in outcomes or 'INVALID' in outcomes else 'INCOMPLETE')
    verdict.update(spec_id=spec['id'], qualification=False, claim=spec['claim'])
    (Path(out)/'promotion-verdict.json').write_text(json.dumps(verdict, indent=1, default=str))
    print(json.dumps(dict(outcome=verdict['outcome'], part1=outcomes[0], part2=outcomes[1])), flush=True)
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='op', required=True)
    v = sub.add_parser('validate', help='host-only: verify bundle, proof and plan; no hardware')
    r = sub.add_parser('run', help='drive the bench (only via the launcher)')
    for s in (v, r):
        s.add_argument('--manifest', required=True)
        s.add_argument('--manifest-sha256', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--confirm-bench', action='store_true')
    a = parser.parse_args(argv)
    spec, spec_sha = load_spec()
    if a.op == 'validate':
        m = verify_bundle_manifest(spec, a.manifest, a.manifest_sha256)
        p = build_plan(spec, m)
        check_live_profile(p)
        check_part1_liveness_period(spec)
        plan2, plan2_sha = reconnect_plan(spec)
        import echoear_rollback as r
        r.mute_fixtures(plan2)
        print(json.dumps(dict(passed=True, spec_sha256=spec_sha, elf_sha256=p['elf_sha256'], cells=list(SCHEDULED),
                              reconnect_plan_sha256=plan2_sha, part2_id=plan2['id'], runtime_profile_config_sha256=p['profile']['config_sha256'],
                              hardware_access=False)))
        return 0
    if not a.confirm_bench:
        raise SystemExit('run drives the bench: pass --confirm-bench')
    return 0 if run(a.manifest, a.manifest_sha256, a.out)['outcome'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
