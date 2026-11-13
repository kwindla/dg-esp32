"""Decisions 114/115: smoke part 1's bot-alarm clause under the decision-113 tagged rule with the last-timely witness.

Fixture: hil/fixtures/smoke02_part1_liveness, a minimal excerpt of aec-promotion-smoke-02 part 1 (bot.log lines
without dg-project-id, connected.json pc_id, the evidence-derived disconnect bracket, device OK/STATE lines).
"""
import copy
import json
from pathlib import Path

import pytest

import echoear_aec_promotion_smoke as s

HERE = Path(__file__).resolve().parent
FIX = HERE/'fixtures/smoke02_part1_liveness'
RAW = (FIX/'bot-excerpt.log').read_text()          # the real pre-115 line (no silent_s): a negative since r11
RUN = json.loads((FIX/'smoke02-part1.json').read_text())
PC = RUN['pc_id']
EVENT = RUN['liveness_alarm_event']                    # the real evaluation event: silent_s 2.4335
BOT = RAW.replace('pc_id='+PC+'\n', 'pc_id='+PC+' silent_s=%.3f\n' % EVENT['silent_s'])   # the r11 line for that alarm
assert BOT != RAW
STAMP = dict(RUN['disconnect'], sent_mono=RUN['disconnect']['sent_wall']-RUN['clock']['offset_s'])
PLAN2 = json.loads((HERE/'plans/echoear-aec-reconnect-v1.json').read_text())
ALARM_TS = '2026-10-25 18:54:46.222'


def rule(bot=BOT, pc=PC, stamps=None, lines=None, events=None):
    return s.part1_liveness_rule(PLAN2, pc, [STAMP] if stamps is None else stamps, bot,
                                 RUN['device_lines'] if lines is None else lines, [EVENT] if events is None else events)


def moved(stamp):
    return BOT.replace(ALARM_TS, '2026-10-25 '+stamp).replace('2026-10-25 18:54:46.224', '2026-10-25 '+stamp)


def test_smoke02_part1_retirement_alarm_passes_the_tagged_rule():
    assert RUN['of_record']['part1'] == 'FAIL' and 'LIVENESS alarm' in BOT              # FAIL of record (absolute clause)
    r = rule()
    a = r['alarms'][0]
    assert r['passed'] and r['expected_alarms'] == 1 and a['pc_id'] == PC and a['epoch'] == 'part-1'
    assert a['wall'] > RUN['disconnect']['sent_wall'] and a['window_wall'][0] <= a['wall'] <= a['window_wall'][1]


def test_same_alarm_on_a_different_pc_id_fails():
    r = rule(BOT.replace('pc_id='+PC, 'pc_id=SmallWebRTCConnection#9-feed'))
    assert not r['passed'] and r['alarms'][0]['reason'] == 'alarm on an unknown peer'


def test_alarm_before_the_disconnect_send_fails():
    r = rule(moved('18:54:43.500'))
    assert not r['passed'] and r['alarms'][0]['reason'] == 'alarm on the live peer'


def test_alarm_early_after_the_send_fails():
    r = rule(moved('18:54:44.800'))       # < send + 1.25 s: a live liveness fault, not retirement
    assert not r['passed'] and 'outside' in r['alarms'][0]['reason']


def test_alarm_well_after_the_window_fails():
    r = rule(moved('18:54:49.000'))
    assert not r['passed'] and 'outside' in r['alarms'][0]['reason']


def test_untagged_alarm_fails():
    r = rule(BOT.replace(' pc_id='+PC+' silent_s=2.433', ''))
    assert not r['passed'] and r['alarms'][0]['reason'].startswith('untagged')


def test_the_real_pre115_smoke02_line_without_silent_s_fails_closed():
    r = rule(RAW)
    assert not r['passed'] and r['alarms'][0]['reason'].startswith('missing or malformed silent_s')


def test_two_alarms_fail():
    line = next(x for x in BOT.splitlines() if 'LIVENESS alarm' in x)
    r = rule(BOT+line.replace('46.222', '46.700')+'\n')
    assert not r['passed'] and r['alarms'][1]['reason'] == 'second alarm for the same peer'


def test_no_witnessed_disconnect_fails():
    assert not rule(stamps=[])['passed']
    assert not rule(stamps=[dict(RUN['disconnect'], ok=False)])['passed']


def test_unknown_part_peer_fails_closed():
    assert not rule(pc=None)['passed']


def test_device_missing_ping_stays_absolute():
    r = rule(BOT.replace(next(x for x in BOT.splitlines() if 'LIVENESS alarm' in x)+'\n', ''),
             lines=RUN['device_lines']+['I (1) dg-app: LIVENESS no ping for 15s gen=1 device_us=1'])
    assert not r['passed'] and r['device_missing_ping'] and not r['alarms']


def test_no_alarm_passes():
    assert rule(BOT.replace(next(x for x in BOT.splitlines() if 'LIVENESS alarm' in x)+'\n', ''), events=[])['passed']


# -- decision 115: last-timely witness and evaluation-event corroboration ----------------------------------

def test_smoke02_shape_last_timely_is_after_the_floor():
    a = rule()['alarms'][0]
    assert a['silent_s'] == 2.433 and a['last_timely_wall'] >= a['last_timely_floor_wall']
    assert STAMP['lower_wall']-a['last_timely_wall'] == pytest.approx(0.256, abs=0.01)


def test_astra_delayed_callback_trace_fails():
    """Last timely pong 3.0 s before the send; the callback runs at send + 1.5 s and reports silent_s 4.5."""
    line = next(x for x in BOT.splitlines() if 'LIVENESS alarm' in x)
    import datetime
    t = STAMP['sent_wall']+1.5
    stamp_ = datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
    bot = BOT.replace(line, stamp_+line[23:].replace('silent_s=2.433', 'silent_s=4.500'))
    ev = dict(EVENT, t=t-STAMP['sent_wall']+STAMP['sent_mono'], silent_s=4.5)
    r = rule(bot, events=[ev])
    assert not r['passed'] and 'owed while live' in r['alarms'][0]['reason']


@pytest.mark.parametrize('events,why', [
    ([], 'matching liveness-alarm events'),
    ([dict(EVENT, silent_s=2.6)], 'disagrees'),
    ([dict(EVENT, t=EVENT['t']+0.2)], 'event time'),
    ([dict(EVENT, pc_id='SmallWebRTCConnection#9-feed')], 'matching liveness-alarm events'),
    ([EVENT, dict(EVENT, t=EVENT['t']+5)], 'without an attributed alarm line'),
])
def test_evaluation_event_must_corroborate_the_line(events, why):
    r = rule(events=events)
    assert not r['passed'] and any(why in p for p in r['event_corroboration']['problems'])


def test_a_stamp_without_its_monotonic_pair_cannot_place_the_event():
    r = rule(stamps=[RUN['disconnect']])
    assert not r['passed'] and any('clock pair' in p for p in r['event_corroboration']['problems'])


# -- the promotion-wrapper stamp ----------------------------------------------------------------------------

class Board:
    def __init__(self, raise_on=None):
        self.sent, self.raise_on = [], raise_on
    def send(self, text):
        self.sent.append(text); return len(self.sent)


def test_stamp_brackets_only_the_disconnect_console_send_and_restores(tmp_path):
    import echoear_aec_campaign as C
    calls, clock = [], iter([10.0, 10.001, 99.0, 99.001])
    def original(board, text, timeout=8):
        board.send(text); calls.append(text)
        if text == 'disconnect' and board.raise_on: raise RuntimeError('ERR #1')
        return (0, 1)
    real = C.command
    C.command = original
    try:
        with s.stamp_disconnects(tmp_path/'stamps.json', wall=lambda: next(clock), mono=lambda: 500.0) as st:
            b = Board()
            C.command(b, 'audio 0'); C.command(b, 'disconnect', 40)
            assert 'send' not in vars(b)                                  # the instance is restored
            with pytest.raises(RuntimeError):
                C.command(Board(raise_on=True), 'disconnect', 40)
        assert C.command is original                                      # module attribute restored
    finally:
        C.command = real
    rows = json.loads((tmp_path/'stamps.json').read_text())
    assert rows[0] == dict(command='disconnect', ok=True, lower_wall=10.0, sent_wall=10.001, sent_mono=500.0)
    assert rows[1]['ok'] is False and calls == ['audio 0', 'disconnect', 'disconnect']


class EspLikeBoard:
    """The EspSerial boundary: send() is a CLASS method (as on EspSerial); 'wire' records time.time() at the write."""
    def __init__(self):
        self._seq, self._lines, self.sent, self.wire = 0, [], [], []
    @property
    def cursor(self):
        return len(self._lines)
    def lines_since(self, cursor):
        return [(i, 0.0, l) for i, l in enumerate(self._lines[cursor:], cursor)]
    def send(self, cmd):
        import time
        self._seq += 1; self.sent.append(cmd); self.wire.append((cmd, time.time()))
        if cmd == 'stats':
            self._lines.append('PERF '+json.dumps(PERF))
        self._lines.append(f'OK #{self._seq} {cmd}')
        return self._seq
    def wait_for(self, regex, timeout, since=None):
        import re
        for _, _, line in self.lines_since(since or 0):
            if re.search(regex, line):
                return line
        raise TimeoutError('no reply')
    def close(self):
        pass


PERF = dict(pc_id=PC, generation=1, device_us=1000, i2s_underflows=0, i2s_write_errors=0, i2s_write_timeouts=0,
            liveness=dict(pings=10, pongs=10, timeouts=0), aec=dict(enabled=True, errors=0, reference_clips=0, output_clips=0,
            capture=dict(read_errors=0, read_timeouts=0, adc_clips=0, gain_clips=0)), heap=dict(psram=dict(free=1, largest=1)),
            tasks=[], audio=dict(drops=0))


def test_executed_real_benchhold_finish_is_stamped_by_the_wrapper(tmp_path, monkeypatch):
    """Fable r10 S1: the REAL V9Hold -> ... -> BenchHold.__init__/finish, the real console command
    (echoear_aec_resource_trial.command via the wrapped campaign attribute) and the real snapshot writing
    disconnected.json; only the serial/bot/host/evaluation boundary is fake. The stamp brackets the actual wire write."""
    import types
    import echoear_aec_campaign as C
    import echoear_aec_nova_prestep as n
    import echoear_aec_resource_trial as RT
    import echoear_aec_v9 as v9
    import echoear_aec_v9_plan as plan_mod
    if not plan_mod.SOURCE.exists():
        pytest.skip('frozen inputs not on this host')
    spec, _ = s.load_spec()
    manifest = Path(spec['image']['bundle'])/'manifest.json'
    p = s.build_plan(spec, s.verify_bundle_manifest(spec, manifest, s.sha(manifest)))
    assert C.command is RT.command                                   # the real console command
    def boundary(board, folder, label, **kw):
        C.write(Path(folder)/(label+'.json'), PERF); return PERF, [PERF]
    for name, value in dict(boundary=boundary, observer_ok=lambda events: True, read=lambda board, a, b: {},
                            check_directory=lambda out: dict(all_passed=True), check_active_recovery=lambda rows, t: dict(passed=True),
                            rows=lambda path: [], host_nic=types.SimpleNamespace(gate=lambda a, b: dict(passed=True), snapshot=lambda: {})).items():
        monkeypatch.setattr(C, name, value)
    monkeypatch.setattr(n, 'profile_witness', lambda p, events, require_vendor=True: dict(passed=True))
    class Budget:
        total_expired = False
        def remaining(self): return 1000.0
        def cleanup(self): pass
    class Bot:
        stopped = 0
        def stop(self): Bot.stopped += 1
    board = EspLikeBoard()
    hold_dir = tmp_path/'part-1'; hold_dir.mkdir()
    stamps = tmp_path/s.DISCONNECT_STAMPS
    with s.stamp_disconnects(stamps) as st:
        H = v9.hold_type(Budget())
        h = H(p, p['sessions'][0], hold_dir)                         # real BenchHold.__init__
        h.board, h.bot, h.touched = board, Bot(), False
        h.evaluation = hold_dir/'evaluation'; h.evaluation.mkdir()
        (hold_dir/'rf-pre-gate.json').write_text(json.dumps(dict(after={})))
        h.events = lambda case_id=None: []
        h.send = lambda op, cid, **kw: None
        h.finish()                                                   # the real finish chain
        assert C.command is not st.original
    assert C.command is RT.command and 'send' not in vars(board)    # attribute and instance restored
    rows = json.loads(stamps.read_text())
    wire = [t for c, t in board.wire if c == 'disconnect']
    assert len(rows) == 1 and len(wire) == 1 and rows[0]['ok'] is True
    assert rows[0]['lower_wall'] <= wire[0] <= rows[0]['sent_wall'] and type(rows[0]['sent_mono']) is float
    assert board.sent.index('audio 0') < board.sent.index('disconnect') and (hold_dir/'disconnected.json').exists()
    assert Bot.stopped == 1 and s.disconnect_stamp_check(tmp_path, True)['passed']


# -- end to end through the real promotion judge --------------------------------------------------------------

def judged(tmp_path, alarm_pc=PC, stamps=True, extra='', bot=None, events=None):
    import test_echoear_aec_promotion_smoke as t
    spec, _ = t.make_world(tmp_path)
    out = tmp_path/'o'
    rows = t.write_hold(out, stamps=False)
    hold = out/'part-1'
    c = json.loads((hold/'connected.json').read_text()); c['pc_id'] = PC
    (hold/'connected.json').write_text(json.dumps(c))
    lines = [x for x in (bot or BOT).splitlines() if 'DEMO_DUPLEX' not in x]
    (hold/'evaluation').mkdir()
    (hold/'evaluation/events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in ([EVENT] if events is None else events)))
    log = (hold/'bot/server-1.log').read_text()+'\n'.join(lines).replace('pc_id='+PC, 'pc_id='+alarm_pc)+'\n'+extra
    (hold/'bot/server-1.log').write_text(log)
    if stamps:
        (out/s.DISCONNECT_STAMPS).write_text(json.dumps([STAMP]))
    return s.judge(spec, {}, rows, out)


def test_judge_passes_the_smoke02_shape_end_to_end(tmp_path):
    v = judged(tmp_path)
    assert v['outcome'] == 'PASS', {k: c for k, c in v['checks'].items() if not c['passed']}
    assert v['checks']['counters']['bot_liveness']['expected_alarms'] == 1


@pytest.mark.parametrize('kw', [dict(alarm_pc='SmallWebRTCConnection#9-feed'), dict(stamps=False)])
def test_judge_fails_another_peer_or_an_unstamped_disconnect_end_to_end(tmp_path, kw):
    v = judged(tmp_path, **kw)
    assert v['outcome'] == 'FAIL' and not v['checks']['counters']['passed']
    assert not v['checks']['counters']['bot_liveness']['passed']


def test_rejudge_part1_writes_beside_and_never_inside(tmp_path):
    import echoear_liveness_rejudge as rj
    run = tmp_path/'aec-promotion-smoke-02'
    with pytest.raises(SystemExit, match='inside'):
        rj.main(['--part1', str(run), str(run/'x.json')])


def test_judge_fails_the_astra_delayed_callback_trace_end_to_end(tmp_path):
    import datetime
    line = next(x for x in BOT.splitlines() if 'LIVENESS alarm' in x)
    t = STAMP['sent_wall']+1.5
    bot = BOT.replace(line, datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
                      +line[23:].replace('silent_s=2.433', 'silent_s=4.500'))
    v = judged(tmp_path, bot=bot, events=[dict(EVENT, t=t-STAMP['sent_wall']+STAMP['sent_mono'], silent_s=4.5)])
    assert v['outcome'] == 'FAIL' and not v['checks']['counters']['passed']
    assert 'owed while live' in v['checks']['counters']['bot_liveness']['alarms'][0]['reason']


def test_judge_fails_a_missing_evaluation_event_end_to_end(tmp_path):
    v = judged(tmp_path, events=[])
    assert v['outcome'] == 'FAIL' and not v['checks']['counters']['bot_liveness']['event_corroboration']['passed']


def test_completed_part_without_a_stamp_fails_even_with_no_alarm(tmp_path):
    """Fable r10 should-fix 1: a missed stamp with no alarm must not pass silently."""
    import test_echoear_aec_promotion_smoke as t
    spec, _ = t.make_world(tmp_path)
    v = s.judge(spec, {}, t.write_hold(tmp_path/'o', stamps=False), tmp_path/'o')
    assert v['outcome'] == 'FAIL' and not v['checks']['disconnect_stamp']['passed'] and v['checks']['counters']['passed']
    (tmp_path/'o'/s.DISCONNECT_STAMPS).write_text(json.dumps([STAMP, STAMP]))
    assert not s.judge(spec, {}, t.write_hold(tmp_path/'p', stamps=False), tmp_path/'o')['checks']['disconnect_stamp']['passed']


def test_an_incomplete_part_does_not_require_a_stamp():
    assert s.disconnect_stamp_check(Path('/nonexistent'), False)['passed']


def test_period_guard_runs_at_validate_not_in_judge(tmp_path, monkeypatch):
    """Fable r10 should-fix 3: the guard is preflight; judge (inside run's finally) never raises on it."""
    import test_echoear_aec_promotion_smoke as t
    spec, _ = t.make_world(tmp_path)
    bad = copy.deepcopy(spec); bad['bot']['bot_config']['ECHOEAR_LIVENESS_SECONDS'] = '1'
    with pytest.raises(ValueError, match='liveness period'):
        s.check_part1_liveness_period(bad)
    assert s.judge(bad, {}, t.write_hold(tmp_path/'o'), tmp_path/'o')['outcome'] == 'PASS'
    real, _ = s.load_spec()
    assert s.check_part1_liveness_period(real)
    monkeypatch.setattr(s, 'load_spec', lambda path=s.SPEC: (bad, 'x'))
    monkeypatch.setattr(s, 'verify_bundle_manifest', lambda *a: {})
    monkeypatch.setattr(s, 'build_plan', lambda spec, m: dict(elf_sha256='e', profile={}))
    monkeypatch.setattr(s, 'check_live_profile', lambda p, live=None: True)
    with pytest.raises(ValueError, match='liveness period'):
        s.main(['validate', '--manifest', 'm', '--manifest-sha256', 'x'])
