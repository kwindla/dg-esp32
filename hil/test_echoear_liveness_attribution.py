"""Decisions 112/113: bot LIVENESS alarms are expected only for a deliberately disconnected peer, and identity
comes only from the pc_id tag on the alarm line itself.

Fixture: hil/fixtures/smoke01_liveness, a minimal excerpt of aec-promotion-smoke-01 part 2 (bot.log lines
without the header, the recorder anchor rows, gate.json epoch stamps and the device STATE/LIVENESS lines).
That excerpt is UNTAGGED (pre-113 bot) and is the negative case; TAGGED is the same excerpt with the r8 line.
"""
import copy
import datetime
import json
from pathlib import Path

import pytest

import echoear_liveness_rejudge as rj
import echoear_mute_gate as g

HERE = Path(__file__).resolve().parent
FIX = HERE/'fixtures/smoke01_liveness'
RECONNECT = json.loads((HERE/'plans/echoear-aec-reconnect-v1.json').read_text())
MUTE = json.loads((HERE/'plans/echoear-mute-gate-v1.json').read_text())
BOT = (FIX/'bot-excerpt.log').read_text()
ROWS = [json.loads(x) for x in (FIX/'recorder-anchors.jsonl').read_text().splitlines()]
RUN = json.loads((FIX/'smoke01-part2.json').read_text())
PC_A = 'SmallWebRTCConnection#0-0fac7ccbe12140bca907af26dc18547d'
PC_B = 'SmallWebRTCConnection#1-0adb6529f53145bf94db78b57931f171'
UNTAGGED = '| ERROR    | demo_liveness:tick:58 - LIVENESS alarm: no timely pong for 2.0 s; closing peer'
DISCARD = '| INFO     | pipecat.transports.smallwebrtc.request_handler:handle_disconnected:212 - Discarding peer connection for pc_id: '


def alarm(pc, silent='2.000'):
    """The r11 bot line (decision 115): pc_id tag plus the measured silence."""
    return UNTAGGED+' pc_id='+pc+(' silent_s='+silent if silent is not None else '')


assert BOT.count(UNTAGGED+'\n') == 1
TAGGED = BOT.replace(UNTAGGED+'\n', alarm(PC_A)+'\n')


def judge(bot=TAGGED, device=None, plan=RECONNECT, epochs=None):
    m = rj.clock_map(ROWS, bot)
    peers, p, _ = rj.mapped_inputs(epochs or RUN['epochs'], m, plan)
    return g.liveness_logs_attributed(bot, RUN['device_lines'] if device is None else device, peers, p)


def at(stamp, text):
    return '2026-10-25 '+stamp+' '+text


def ordered(lines):
    return '\n'.join(sorted(lines, key=lambda x: x[:23]))+'\n'


def replace_alarm_pair(bot, lines):
    """Drop the real alarm + A discard; add `lines`, then keep file order by timestamp."""
    kept = [x for x in bot.splitlines() if 'LIVENESS alarm' not in x and PC_A not in x]
    return ordered(kept+lines)


def test_untagged_smoke01_excerpt_fails_closed_under_the_r8_rule():
    assert RUN['of_record']['gate_outcome'] == 'FAIL' and RUN['of_record']['liveness_logs']['bot_alarm'] is True
    assert not g.liveness_logs(BOT, RUN['device_lines'])['passed']                      # of record: absolute rule
    r = judge(BOT)
    assert not r['passed'] and r['alarms'][0]['reason'] == 'untagged alarm: identity not bound at emission'


def test_tagged_smoke01_shape_passes():
    r = judge()
    assert r['passed'] and r['expected_alarms'] == 1 and r['rule'] == 'decision-113 tagged'
    a = r['alarms'][0]
    assert a['status'] == 'EXPECTED' and a['pc_id'] == PC_A and a['epoch'] == 'A' and a['live_peers_at_alarm'] == ['B']
    assert a['discard_line'] == a['line']+2 and a['orphan_after_discard_s'] is None
    # Fable r7 should-fix 1: lower bound send + tolerance - period - 0.25 s = send + 1.25 s; smoke-01 at +1.957 s
    assert 1.9 < a['after_send_s'] < 2.0 and a['window_wall'][0]-(a['wall']-a['after_send_s']) == pytest.approx(1.25, abs=0.01)


def test_the_clock_mapping_is_tight_and_counts_every_anchor():
    m = rj.clock_map(ROWS, TAGGED)
    assert m['n'] == 9 and m['spread_s'] < 0.003


def test_interleaved_unrelated_lines_do_not_change_identity():
    lines = TAGGED.splitlines()
    i = next(k for k, x in enumerate(lines) if 'LIVENESS alarm' in x)
    noise = [at('15:02:44.772', '| DEBUG    | pipecat.transports.smallwebrtc.connection:on_ended:392 - Track audio ended')]*3
    assert judge('\n'.join(lines[:i+1]+noise+lines[i+1:])+'\n')['passed']


# -- Astra r7 blocker: a live-peer alarm whose own close is slow ---------------------------------------------

BASE = datetime.datetime(2026, 10, 25, 15, 0, 0).timestamp()


def stamp(t, text):
    return datetime.datetime.fromtimestamp(BASE+t).strftime('%Y-%m-%d %H:%M:%S.%f')[:23]+' '+text


ASTRA_PEERS = [dict(id='A', pc_id=PC_A, live_from_wall=BASE+5.0, disconnect=dict(lower_wall=BASE+10.0, sent_wall=BASE+10.0, ok=True)),
               dict(id='B', pc_id=PC_B, live_from_wall=BASE+10.1, disconnect=dict(lower_wall=BASE+20.0, sent_wall=BASE+20.0, ok=True))]


@pytest.mark.parametrize('tag', [PC_B, None])
def test_astra_delayed_own_close_live_peer_alarm_fails(tag):
    """B's watchdog alarms at 12.25 and awaits B's close; stale A is discarded at 12.35; B's discard at 12.85.
    r7's proximity pairing credited this to A. Tagged it is B (live): FAIL. Untagged it FAILs closed."""
    bot = '\n'.join([stamp(12.25, alarm(tag) if tag else UNTAGGED), stamp(12.35, DISCARD+PC_A), stamp(12.85, DISCARD+PC_B)])+'\n'
    r = g.liveness_logs_attributed(bot, [], ASTRA_PEERS, RECONNECT)
    a = r['alarms'][0]
    assert not r['passed'] and a['status'] == 'FAIL'
    if tag:
        assert a['epoch'] == 'B' and a['reason'] == 'alarm on the live peer' and a['discard_line'] == 3
    else:
        assert a['reason'].startswith('untagged')


def test_tagged_a_in_the_astra_shape_is_expected_and_identity_ignores_the_nearby_b_discard():
    bot = '\n'.join([stamp(11.95, alarm(PC_A)), stamp(12.0, DISCARD+PC_B), stamp(12.5, DISCARD+PC_A)])+'\n'
    r = g.liveness_logs_attributed(bot, [], [ASTRA_PEERS[0], dict(ASTRA_PEERS[1], disconnect=dict(lower_wall=BASE+11.9, sent_wall=BASE+11.9, ok=True))], RECONNECT)
    assert r['passed'] and r['alarms'][0]['epoch'] == 'A' and r['alarms'][0]['discard_line'] == 3


# -- negatives -----------------------------------------------------------------------------------------------

def test_alarm_on_the_live_session_b_peer_fails():
    bot = ordered(TAGGED.splitlines()+[at('15:02:47.000', alarm(PC_B)), at('15:02:47.001', DISCARD+PC_B)])
    r = judge(bot)
    assert not r['passed'] and r['alarms'][1]['status'] == 'FAIL' and r['alarms'][1]['reason'] == 'alarm on the live peer'


def test_late_alarm_outside_the_disconnect_window_fails():
    r = judge(replace_alarm_pair(BOT, [at('15:02:46.200', alarm(PC_A)), at('15:02:46.201', DISCARD+PC_A)]))
    assert not r['passed'] and 'outside' in r['alarms'][0]['reason']


def test_early_alarm_below_the_tightened_lower_bound_fails():
    """Fable r7 should-fix 1: 0.3 s after the send the bot had already gone ~1.9 s without a timely pong while the
    peer was live - a liveness fault the deliberate disconnect must not mask."""
    r = judge(replace_alarm_pair(BOT, [at('15:02:43.115', alarm(PC_A)), at('15:02:43.116', DISCARD+PC_A)]))
    assert not r['passed'] and 'outside' in r['alarms'][0]['reason'] and 0.25 < r['alarms'][0]['after_send_s'] < 0.35


def test_alarm_before_the_disconnect_send_fails():
    r = judge(replace_alarm_pair(BOT, [at('15:02:42.500', alarm(PC_A)), at('15:02:42.501', DISCARD+PC_A)]))
    assert not r['passed'] and r['alarms'][0]['reason'] == 'alarm on the live peer'


def test_tagged_alarm_never_corroborated_by_a_discard_fails():
    r = judge('\n'.join(x for x in TAGGED.splitlines() if 'Discarding' not in x or PC_A not in x)+'\n')
    assert not r['passed'] and r['alarms'][0]['reason'] == 'not corroborated: tagged peer never discarded'


def test_two_alarms_for_the_same_peer_fail():
    bot = replace_alarm_pair(BOT, [at('15:02:44.772', alarm(PC_A)), at('15:02:44.773', DISCARD+PC_A),
                                   at('15:02:44.900', alarm(PC_A))])
    r = judge(bot)
    assert not r['passed'] and r['alarms'][0]['status'] == 'EXPECTED' and r['alarms'][1]['reason'] == 'second alarm for the same peer'


def test_alarm_on_an_unknown_peer_fails():
    r = judge(replace_alarm_pair(BOT, [at('15:02:44.772', alarm('SmallWebRTCConnection#9-feed')),
                                       at('15:02:44.773', DISCARD+'SmallWebRTCConnection#9-feed')]))
    assert not r['passed'] and r['alarms'][0]['reason'] == 'alarm on an unknown peer'


def test_unparseable_alarm_text_fails():
    r = judge(TAGGED+'Traceback: LIVENESS alarm raised\n')
    assert not r['passed'] and r['alarms'][-1]['reason'] == 'unparseable LIVENESS alarm line'


def test_logged_tolerance_must_match_the_declared_one():
    assert not judge(TAGGED.replace('for 2.0 s', 'for 3.0 s'))['passed']


def test_peer_without_an_ok_deliberate_disconnect_fails():
    epochs = copy.deepcopy(RUN['epochs']); epochs[0]['disconnect']['ok'] = False
    r = judge(epochs=epochs)
    assert not r['passed'] and 'deliberate disconnect' in r['alarms'][0]['reason']


def test_device_missing_ping_on_b_fails():
    r = judge(device=RUN['device_lines']+['I (30000) dg-app: LIVENESS no ping for 15s gen=3 device_us=30000000'])
    assert not r['passed'] and r['device_missing_ping']


def test_tolerance_declaration_must_match_demo_liveness():
    bad = copy.deepcopy(RECONNECT); bad['liveness_logs']['tolerance_s'] = 3.0
    with pytest.raises(ValueError, match='tolerance'):
        judge(plan=bad)


# -- orphan signature (Fable r7 should-fix 3) ----------------------------------------------------------------

def test_orphan_tick_after_a_graceful_discard_is_credited_inside_the_window():
    r = judge(replace_alarm_pair(BOT, [at('15:02:44.770', DISCARD+PC_A), at('15:02:44.773', alarm(PC_A))]))
    a = r['alarms'][0]
    assert r['passed'] and a['status'] == 'EXPECTED' and 'orphan' in a['reason'] and a['orphan_after_discard_s'] == pytest.approx(0.003, abs=1e-4)


def test_orphan_tick_outside_the_window_fails():
    r = judge(replace_alarm_pair(BOT, [at('15:02:46.300', DISCARD+PC_A), at('15:02:46.303', alarm(PC_A))]))
    assert not r['passed'] and 'outside' in r['alarms'][0]['reason']


def test_orphan_tick_for_a_peer_without_a_deliberate_disconnect_fails():
    epochs = copy.deepcopy(RUN['epochs']); epochs[0]['disconnect']['ok'] = False
    r = judge(replace_alarm_pair(BOT, [at('15:02:44.770', DISCARD+PC_A), at('15:02:44.773', alarm(PC_A))]), epochs=epochs)
    assert not r['passed']


# -- plans and wiring ----------------------------------------------------------------------------------------

def test_mute_plan_declares_the_same_rule_and_its_bot_runs_liveness():
    """Liveness is ON in the mute gate (public_env 0.5 s; MuteLive launches with plan public_env), so the
    tagged rule applies to its R1/R2/R3 cycles too. rollback-01 (PUBLIC_ENV '0') keeps its absolute rule."""
    import echoear_rollback as r
    assert MUTE['public_env']['ECHOEAR_LIVENESS_SECONDS'] == '0.5' and r.PUBLIC_ENV['ECHOEAR_LIVENESS_SECONDS'] == '0'
    assert MUTE['liveness_logs'] == RECONNECT['liveness_logs'] and 'pair_window_s' not in MUTE['liveness_logs']
    assert g.liveness_tolerance_s(MUTE['liveness']['period_s']) == MUTE['liveness_logs']['tolerance_s']
    assert judge(plan=MUTE)['passed'] and not judge(BOT, plan=MUTE)['passed']


def test_scorer_matches_the_bot_alarm_text():
    """The tag format is the server's (demo_liveness), not a harness invention."""
    src = (HERE.parent/'server/demo_liveness.py').read_text()
    assert "'LIVENESS alarm: no timely pong for {} s; closing peer pc_id={} silent_s={:.3f}'" in src
    m = g.ALARM_MESSAGE.match('LIVENESS alarm: no timely pong for 2.0 s; closing peer pc_id='+PC_A+' silent_s=2.433')
    assert m[2] == PC_A and m[3] == '2.433'


def test_both_runners_judge_with_the_attributed_rule():
    import inspect
    import echoear_rollback as r
    for fn in (r.MuteLive.judge, r.AecReconnectLive.judge_aec):
        src = inspect.getsource(fn)
        assert 'liveness_logs_attributed' in src and 'g.liveness_logs(' not in src


def test_runner_wall_stamps_map_directly_to_peers():
    epochs = [dict(id='A', first=dict(pc_id=PC_A), pre_connect_wall=10.0,
                   disconnect=dict(send_lower_wall=20.0, sent_wall=20.001, ok=True)),
              dict(id='B', first=dict(pc_id=PC_B), pre_connect_wall=20.5)]
    peers = g.liveness_peers(epochs)
    assert peers[0]['disconnect'] == dict(lower_wall=20.0, sent_wall=20.001, ok=True) and peers[1]['disconnect'] is None


def test_live_disconnect_records_wall_stamps(monkeypatch):
    import echoear_rollback as r
    class Board:
        cursor = 0
        def send(self, text): return 7
        def wait_for(self, pattern, timeout, since=0): return 'OK #7'
        def lines_since(self, cursor): return [(0, 0.0, 'PERF {"a": 1}')]
    live = r.MuteLive.__new__(r.MuteLive)
    live.board, live.epochs, live.out = Board(), [dict(id='A', snapshots=[])], Path('/nonexistent')
    monkeypatch.setattr(r, 'write', lambda *a, **k: None)
    live.snapshot = lambda label: {}
    live.command = lambda text: None
    live.disconnect()
    d = live.epochs[0]['disconnect']
    assert d['send_lower_wall'] <= d['sent_wall'] and abs(d['sent_wall']-__import__('time').time()) < 5


def test_rejudge_writes_beside_the_run_never_inside_and_never_overwrites(tmp_path):
    run = tmp_path/'aec-promotion-smoke-01'; part = run/'part-2'; (part/'sessions/session-x').mkdir(parents=True)
    parts = {'liveness-logs': RUN['of_record']['liveness_logs'], 'reply-A': dict(passed=True)}
    (part/'gate.json').write_text(json.dumps(dict(verdict=dict(outcome='FAIL'), parts=parts, epochs=RUN['epochs'])))
    (part/'bot.log').write_text(BOT)
    (part/'aec-serial.log').write_text('\n'.join(RUN['device_lines'])+'\n')
    (part/'sessions/session-x/events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in ROWS))
    before = {p: p.read_bytes() for p in run.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit, match='inside'):
        rj.main([str(part), str(run/'x.json')])
    out = tmp_path/'smoke-01-rejudge-diagnostic.json'
    rj.main([str(part), str(out)])
    d = json.loads(out.read_text())
    assert d['diagnostic'] is True and d['rescore'] is False and d['of_record']['gate_outcome'] == 'FAIL'
    assert 'tagged' in d['note'] and not d['liveness_logs']['passed']                     # untagged: fails closed
    assert d['liveness_logs']['alarms'][0]['reason'].startswith('untagged')
    assert d['hypothetical_gate_outcome_if_only_liveness_logs_replaced'] == 'FAIL'
    with pytest.raises(FileExistsError):
        rj.main([str(part), str(out)])
    assert before == {p: p.read_bytes() for p in run.rglob('*') if p.is_file()}


# -- decision 115: last timely pong witnessed by silent_s ------------------------------------------------------

def test_tagged_alarm_without_silent_s_fails_closed():
    r = judge(BOT.replace(UNTAGGED+'\n', alarm(PC_A, None)+'\n'))
    assert not r['passed'] and r['alarms'][0]['reason'].startswith('missing or malformed silent_s')


@pytest.mark.parametrize('bad', ['abc', '2.0', '2.4335', '-2.000'])
def test_malformed_silent_s_fails_closed(bad):
    r = judge(BOT.replace(UNTAGGED+'\n', alarm(PC_A, bad)+'\n'))
    assert not r['passed'] and r['alarms'][0]['status'] == 'FAIL'


def test_astra_delayed_callback_owed_while_live_fails():
    """Astra r10: last timely pong 3.0 s before the send, callback delayed to send + 1.5 s, silent_s 4.5. Inside
    the emission window, but the failure was owed while the peer was live."""
    send = BASE+10.0
    peers = [dict(id='A', pc_id=PC_A, live_from_wall=BASE+1.0, disconnect=dict(lower_wall=send, sent_wall=send+0.001, ok=True))]
    bot = '\n'.join([stamp(11.5, alarm(PC_A, '4.500')), stamp(11.502, DISCARD+PC_A)])+'\n'
    r = g.liveness_logs_attributed(bot, [], peers, RECONNECT)
    a = r['alarms'][0]
    assert not r['passed'] and 'owed while live' in a['reason'] and a['last_timely_wall'] == pytest.approx(send-3.0)
    healthy = '\n'.join([stamp(12.2, alarm(PC_A, '2.433')), stamp(12.202, DISCARD+PC_A)])+'\n'
    assert g.liveness_logs_attributed(healthy, [], peers, RECONNECT)['passed']


def test_silent_s_below_tolerance_is_inconsistent():
    r = judge(BOT.replace(UNTAGGED+'\n', alarm(PC_A, '1.500')+'\n'))
    assert not r['passed'] and 'below the tolerance' in r['alarms'][0]['reason']


def test_the_last_timely_floor_is_send_lower_minus_period_minus_early():
    send = BASE+10.0
    peers = [dict(id='A', pc_id=PC_A, live_from_wall=BASE+1.0, disconnect=dict(lower_wall=send, sent_wall=send, ok=True))]
    def at_silent(silent):     # alarm at send + 2.2 s
        bot = '\n'.join([stamp(12.2, alarm(PC_A, silent)), stamp(12.201, DISCARD+PC_A)])+'\n'
        return g.liveness_logs_attributed(bot, [], peers, RECONNECT)['passed']
    assert at_silent('2.940') and not at_silent('2.960')       # floor: send - 0.5 - 0.25 - 0.002 (+1 ms line truncation)
