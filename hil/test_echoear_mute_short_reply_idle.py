"""Decision 118 (mute-01): (a) a short reply is scored over its whole playback by the mic-gate witness;
(b) the runner never idles past the bot's 300 s idle timeout, and a bot idle cancel is an explicit INCOMPLETE leaf.

Fixture: hil/fixtures/mute01_short_reply_idle, a minimal excerpt of step-9/mute-01 (A-1 recorder rows without
rtvi/audio rows, A-1 MIC_GATE/GATE serial lines, bot.log idle-cancel lines; the room recording is synthesized).
"""
import copy
import json
import re
from pathlib import Path

import numpy as np
import pytest

import echoear_mute_gate as g
import echoear_rollback as r

HERE = Path(__file__).resolve().parent
FIX = json.loads((HERE/'fixtures/mute01_short_reply_idle/mute01.json').read_text())
PLAN = json.loads((HERE/'plans/echoear-mute-gate-v1.json').read_text())
CELL = FIX['cell']
LINES = [tuple(x) for x in FIX['serial_lines']]


def recording():
    rate, lo = 16000, CELL['start_interval_s']
    n = int((CELL['end_s']+0.2-lo[1])*rate)
    t = lo[1]+np.arange(n)/rate
    s = np.random.default_rng(5).normal(0, 30, n)
    on = (t >= CELL['first_write_s']) & (t <= CELL['bot_stopped_s'])
    s[on] += 3000*np.sin(2*np.pi*1000*t[on])
    return dict(samples=s.astype('<i2'), rate=rate, start_interval_s=lo, healthy=True, health={})


def score(lines=LINES, plan=PLAN):
    return g.score_echo_cell(FIX['rows'], list(lines), recording(), plan, CELL['prompt'], CELL['prompt_start_s'],
                             CELL['prompt_end_s'], CELL['end_s'])


# -- (a) short reply (decisions 118/120) -----------------------------------------------------------------------

F = CELL['first_write_s']


def opened_early(lines, until=0.49):
    """Astra r12 probe: the MIC_GATE samples in [first write, first write + 0.49) made open and nonzero."""
    out = []
    for i, h, text in lines:
        if 'MIC_GATE' in text and F <= h < F+until:
            text = text.replace('closed=1', 'closed=0').split(' post=')[0]+' post=-30.0'
        out.append((i, h, text))
    return out


def without_close(lines):
    return [x for x in lines if 'GATE closed' not in x[2]]


CLOSE = next(x for x in LINES if 'GATE closed' in x[2])
CLOSE_T = int(re.search(r't=(\d+)', CLOSE[2])[1])
LATE = json.loads((HERE/'fixtures/rollback01_late_close/late-close.json').read_text())


def device_t(host_s):
    """Device time at a host receipt time, from the nearest MIC_GATE sample (coherent synthetic lines)."""
    s = min((x for x in LINES if 'MIC_GATE' in x[2]), key=lambda x: abs(x[1]-host_s))
    return int(re.search(r't=(\d+)', s[2])[1])+int((host_s-s[1])*1e6)


def with_close(receipt_s, dev_t=None):
    """The A-1 lines with the close RECEIVED at receipt_s; the device time stays the real one unless given."""
    t = CLOSE_T if dev_t is None else dev_t
    return sorted(without_close(LINES)+[(0, receipt_s, f'GATE closed t={t} source_lower_us=0 source_valid=0')], key=lambda x: x[1])


def sample_at(host_s, closed, post=-30.0):
    return (0, host_s, f'MIC_GATE t={device_t(host_s)} closed={closed} pre=-30.0 post={post}')


def test_mute01_a1_was_incomplete_of_record_and_still_passes():
    assert FIX['of_record']['a1_status'] == 'INCOMPLETE' and FIX['of_record']['a1_cause'] == 'reply shorter than the gate lead'
    res = score()
    assert res['status'] == 'PASS', (res['failures'], res['missing'])
    g_ = res['gate']
    assert g_['basis'] == 'close witnessed -> GATE open' and g_['interval_s'][0] == F and g_['close_receipt_s'] < F
    assert g_['interval_s'][1] > CELL['last_write_s'] and g_['samples'] >= 8


# -- decision 122: close-witness allowance --------------------------------------------------------------------

@pytest.mark.parametrize('reply', LATE['replies'], ids=lambda r: r['cell'])
def test_fable_retained_late_close_replies_pass(reply):
    """rollback-01 warm-2 and talk-over: GATE closed received 171/180 ms after the first non-silent write (healthy).
    These are long replies, scored by gate_check on the fixture lines only; r12's and r13's gate_check are also clean
    on them, so this test alone would NOT have caught the r13 blocker - test_r13_blocker_pinned_on_the_real_a1 does."""
    lines = [tuple(x) for x in reply['lines']]
    plan = PLAN['echo_cell']
    assert 0.1 < reply['close_receipt_s']-reply['first_write_s'] < plan['gate_closed']['close_witness_allowance_s']
    f, m, rec = g.gate_check(lines, reply['first_write_s'], reply['last_write_s'], reply['gate_open_s'], reply['gate_open_s']+2.0, plan)
    assert f == [] and m == [] and rec['pre_close_report_only']['open'] >= 1


@pytest.mark.parametrize('after_s', [0.022, 0.171, 0.180, 0.307, 0.49])
def test_short_reply_with_a_close_received_after_the_first_write_passes(after_s):
    """Fable's shape on the real A-1: the device closed on bot-started-speaking (real device time) but its line
    reaches the host after the first write; the latency range is the retained 22-307 ms."""
    res = score(with_close(F+after_s))
    assert res['status'] == 'PASS', (res['failures'], res['missing'])


def test_a_close_beyond_the_allowance_is_incomplete():
    res = score(with_close(F+0.6))
    assert res['status'] == 'INCOMPLETE' and 'gate close not witnessed within the allowance' in res['missing']


def test_missing_close_is_incomplete_never_pass():
    res = score(without_close(LINES))
    assert res['status'] == 'INCOMPLETE' and 'gate close not witnessed within the allowance' in res['missing']


def test_close_at_plus_0_2_then_an_open_sample_at_plus_0_3_fails():
    lines = sorted(with_close(F+0.2, device_t(F+0.2))+[sample_at(F+0.3, 0)], key=lambda x: x[1])
    res = score(lines)
    assert res['status'] == 'FAIL' and 'open mic sample during witnessed playback' in res['failures']


def test_an_open_sample_before_the_close_is_report_only():
    lines = sorted(with_close(F+0.2, device_t(F+0.2))+[sample_at(F+0.1, 0)], key=lambda x: x[1])
    res = score(lines)
    assert res['status'] == 'PASS' and res['gate']['pre_close_report_only']['open'] >= 1


def test_a_nonzero_closed_sample_after_the_close_fails():
    lines = sorted(with_close(F+0.2, device_t(F+0.2))+[sample_at(F+0.3, 1, post=-40.0)], key=lambda x: x[1])
    assert 'nonzero closed mic sample during playback' in score(lines)['failures']


def test_without_a_close_an_open_sample_beyond_the_allowance_fails():
    lines = sorted(without_close(LINES)+[sample_at(F+0.55, 0)], key=lambda x: x[1])
    res = score(lines)
    assert res['status'] == 'FAIL' and 'open mic sample during witnessed playback' in res['failures']


# -- Astra's r12 negatives, rechecked against the allowance --------------------------------------------------

def test_astra_r12_late_close_probe_with_early_open_samples_fails():
    """Astra's probe line 'GATE closed t=1' (device time before the samples) + open samples: they lie after the close
    on the device clock, so they are positive evidence."""
    late = sorted(opened_early(without_close(LINES))+[(0, F+0.49, 'GATE closed t=1 source_lower_us=0 source_valid=0')], key=lambda x: x[1])
    res = score(late)
    assert res['status'] == 'FAIL' and 'open mic sample during witnessed playback' in res['failures']


def test_astra_r12_shapes_with_coherent_device_times_follow_the_declared_allowance():
    """Declared cost of decision 122, stated by the test: samples received before a close that arrives within the
    allowance, with device times before it, are pre-close and report-only - the healthy late-close shape - so
    Astra's late-close shape with COHERENT device times PASSes with the five open samples retained as report-only;
    with NO close the open samples inside the allowance are report-only and the cell is INCOMPLETE (never PASS)."""
    coherent_late = sorted(opened_early(without_close(LINES))+[(0, F+0.49, f'GATE closed t={device_t(F+0.49)} source_lower_us=0 source_valid=0')],
                           key=lambda x: x[1])
    r1 = score(coherent_late)
    assert r1['status'] == 'PASS' and r1['gate']['pre_close_report_only']['open'] == 5
    r2 = score(opened_early(without_close(LINES)))
    assert r2['status'] == 'INCOMPLETE' and r2['gate']['pre_close_report_only']['open'] == 5


def test_a_duplicate_close_never_moves_the_start_forward():
    dup = sorted(opened_early(LINES, until=0.3)+[(0, F+0.49, 'GATE closed t=1 source_lower_us=0 source_valid=0')], key=lambda x: x[1])
    res = score(dup)
    assert res['gate']['close_device_t'] == CLOSE_T and res['status'] == 'FAIL'   # the run starts at the real close
    clean_dup = sorted(LINES+[(0, F+0.49, f'GATE closed t={device_t(F+0.49)} source_lower_us=0 source_valid=0')], key=lambda x: x[1])
    r = score(clean_dup)
    assert r['gate']['close_device_t'] == CLOSE_T and r['status'] == 'PASS'


def test_a_mid_reply_open_fails():
    mid = sorted(LINES+[(0, F+0.2, 'GATE open t=1 source_lower_us=0 source_valid=0')], key=lambda x: x[1])
    assert score(mid)['status'] == 'FAIL'


def test_no_gate_open_after_the_short_reply_is_incomplete():
    assert score([x for x in LINES if 'GATE open' not in x[2]])['status'] == 'INCOMPLETE'


def test_fable_nit3_gate_flap_after_the_first_open_fails():
    opened = next(h for _, h, text in LINES if 'GATE open' in text)
    flap = sorted(LINES+[(0, opened+0.05, 'GATE closed t=1 source_lower_us=0 source_valid=0'),
                         (0, opened+0.3, 'GATE open t=2 source_lower_us=0 source_valid=0')], key=lambda x: x[1])
    res = score(flap)
    assert res['status'] == 'FAIL' and 'mic gate re-closed after the reply GATE open (flap)' in res['failures']


def long_reply_case():
    """A synthetic long reply: the fixture's writes and lines shifted so the write span exceeds the lead."""
    rows = copy.deepcopy(FIX['rows'])
    writes = [e for e in rows if e['kind'] == 'tts-transport-write' and e.get('non_silent')]
    last = max(e['monotonic_ns'] for e in writes)
    for k in range(1, 15):
        rows.append(dict(writes[-1], monotonic_ns=last+k*int(0.05e9)))
    rows = [dict(e, monotonic_ns=e['monotonic_ns']+int(0.7e9)) if e.get('frame') in ('BotStoppedSpeakingFrame', 'TTSStoppedFrame')
            or e.get('kind') == 'aggregator-assistant-turn-stopped' else e for e in rows]
    lines = [(i, h+0.7 if h > CELL['last_write_s'] else h, t) for i, h, t in LINES]
    extra = [(0, h, 'MIC_GATE t=1 closed=1 pre=-30.0 post=-120.0') for h in np.arange(CELL['last_write_s']+0.05, CELL['last_write_s']+0.75, 0.1)]
    return rows, sorted(lines+extra, key=lambda x: x[1])


def long_cases():
    """Astra r13 adversarial cases on the synthetic long reply (MIC_GATE device t coherent with host time)."""
    import re
    rows, base = long_reply_case()
    last = max(e['monotonic_ns'] for e in rows if e['kind'] == 'tts-transport-write' and e.get('non_silent'))/1e9
    base = [(i, h, re.sub(r'MIC_GATE t=\d+', 'MIC_GATE t='+str(int((h-F+20)*1e6)), t)) for i, h, t in base]
    cases = {'long_control': base,
             'long_early_open_transition': sorted(base+[(0, F+.2, 'GATE open t=1'), (0, F+.3, 'GATE closed t=2')], key=lambda x: x[1])}
    for name, window in [('long_early_open_sample', (F, F+.49)), ('long_drain_nonzero', (last, last+.5))]:
        lines = []
        for i, h, t in base:
            if 'MIC_GATE' in t and window[0] <= h < window[1]:
                t = re.sub(r'post=\S+', 'post=-30.0', t)
                if 'early' in name:
                    t = t.replace('closed=1', 'closed=0')
            lines.append((i, h, t))
        cases[name] = lines
    return rows, cases


def long_recording():
    rec = recording()
    times = rec['start_interval_s'][1]+np.arange(len(rec['samples']))/rec['rate']
    on = (times >= F) & (times <= CELL['bot_stopped_s']+.7)
    rec['samples'] = (np.random.default_rng(5).normal(0, 30, len(times))+on*3000*np.sin(2*np.pi*1000*times)).astype('<i2')
    return rec


# Verdicts of the r12 gate (aec-merge-r12 cc38e0a, hil/echoear_mute_gate.py) on these cases, recorded with that
# exact source; test_long_reply_verdicts_equal_r12_live re-derives them from git when the commit is available.
R12_LONG_VERDICTS = {
    'long_control': ('PASS', []),
    'long_early_open_transition': ('FAIL', ['premature GATE open during playback']),
    'long_early_open_sample': ('FAIL', ['open mic sample during witnessed playback']),
    'long_drain_nonzero': ('FAIL', ['nonzero closed mic sample during playback']),
}


def score_long(module, rows, lines):
    return module.score_echo_cell(rows, lines, long_recording(), PLAN, CELL['prompt'], CELL['prompt_start_s'],
                                  CELL['prompt_end_s'], CELL['end_s']+1)


@pytest.mark.parametrize('name', sorted(R12_LONG_VERDICTS))
def test_long_reply_verdicts_equal_the_r12_gate(name):
    """Decision 121: long replies keep r12's VERDICTS (Astra's three negatives beside a healthy control)."""
    rows, cases = long_cases()
    res = score_long(g, rows, cases[name])
    status, failures = R12_LONG_VERDICTS[name]
    assert res['status'] == status and [f for f in failures if f not in res['failures']] == [], (res['failures'], res['missing'])
    if name == 'long_control':
        assert res['failures'] == [] and res['missing'] == [] and res['gate']['basis'] == 'close witnessed -> GATE open'


def test_long_reply_verdicts_equal_r12_live():
    """The same comparison against the r12 source itself, extracted read-only from git (skipped if unavailable)."""
    import importlib.util, subprocess, sys, tempfile
    try:
        src = subprocess.check_output(['git', 'show', 'cc38e0a:hil/echoear_mute_gate.py'], cwd=HERE, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        pytest.skip('r12 commit not available')
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)/'gate_r12.py'; path.write_text(src)
        spec = importlib.util.spec_from_file_location('gate_r12', path); old = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(HERE)); spec.loader.exec_module(old)
    rows, cases = long_cases()
    for name, lines in cases.items():
        a, b = score_long(old, rows, lines), score_long(g, rows, lines)
        assert a['status'] == b['status'] == R12_LONG_VERDICTS[name][0], name
        assert set(a['failures']) <= set(b['failures']), name          # r14 latches at least what r12 did


def test_every_mute_prompt_and_reconnect_cell_is_scorable_by_the_criterion():
    """R1 (mid-reply) and R2 (after gate open) are report-only trigger cells; every scored cell (A, B, C, D) uses the
    cycle prompts, all scored by the same criterion."""
    seq = {s['id']: s for s in PLAN['sequence']}
    assert seq['R1']['kind'] == seq['R2']['kind'] == 'reconnect'
    assert set(PLAN['stimulus']['cycle']) == {'france', 'sky', 'addition', 'long', 'italy'}


# -- (b) bot idle timeout -------------------------------------------------------------------------------------

def test_the_mute01_bot_log_shows_an_idle_cancel_and_the_leaf_is_incomplete():
    leaf = g.bot_idle_timeout('\n'.join(FIX['bot_lines']))
    assert leaf['status'] == 'INCOMPLETE' and 'infrastructure' in leaf['reason'] and leaf['lines']
    assert g.bot_idle_timeout('2026-10-25 23:02:39.188 | INFO | x:y:1 - Transcript: assistant: Paris.')['passed']


def test_the_idle_cancel_was_300_s_after_the_last_bot_speech():
    from datetime import datetime
    ts = lambda line: datetime.strptime(line[:23], '%Y-%m-%d %H:%M:%S.%f')
    last = ts(next(x for x in FIX['bot_lines'] if 'Transcript: assistant' in x))
    cancel = ts(next(x for x in FIX['bot_lines'] if 'Idle timeout detected' in x))
    assert 299.5 < (cancel-last).total_seconds() < 300.5


def test_fable_nit2_a_block_without_replies_is_a_value_error():
    bad = copy.deepcopy(PLAN); next(s for s in bad['sequence'] if s['id'] == 'A').pop('replies')
    with pytest.raises(ValueError, match='replies'):
        g.check_idle_budget(bad)


def test_plan_idle_budget_holds_and_rejects_waits_past_the_bot_timeout():
    assert g.check_idle_budget(PLAN) and PLAN['bot_idle']['pipecat_idle_timeout_s'] == 300
    bad = copy.deepcopy(PLAN); next(s for s in bad['sequence'] if s['id'] == 'A').update(spacing_s=250)
    with pytest.raises(ValueError, match='bot_idle'):
        g.check_idle_budget(bad)
    bad = copy.deepcopy(PLAN); next(s for s in bad['sequence'] if s['id'] == 'A').update(min_epoch_s=900)
    with pytest.raises(ValueError, match='bot_idle'):
        g.check_idle_budget(bad)
    bad = copy.deepcopy(PLAN); next(s for s in bad['sequence'] if s['id'] == 'R3').update(idle_s=260)
    with pytest.raises(ValueError, match='bot_idle'):
        g.check_idle_budget(bad)
    bad = copy.deepcopy(PLAN); bad['bot_idle']['max_runner_idle_s'] = 300
    with pytest.raises(ValueError, match='below the bot idle timeout'):
        g.check_idle_budget(bad)


def test_pipecat_default_idle_timeout_is_300_s_and_demo_bot_does_not_override_it():
    import pipecat.pipeline.worker as w
    assert w.IDLE_TIMEOUT_SECS == 300
    src = (HERE.parent/'server/demo_bot.py').read_text()
    assert 'idle_timeout' not in src


class Clock:
    t = 1000.0
    def monotonic(self): return self.t
    def monotonic_ns(self): return int(self.t*1e9)


def block_with(statuses, monkeypatch, tmp_path, plan=PLAN, cause='reply shorter than the gate lead'):
    """The real MuteLive.block with fake cells on a fake clock; a PASS cell writes bot activity rows."""
    live = r.MuteLive.__new__(r.MuteLive)
    live.plan, live.cells, live.out = plan, {}, tmp_path
    clock = Clock(); monkeypatch.setattr(r, 'time', clock)
    live.last_bot_activity, live.idle_budget_stops = clock.t, []
    live.epochs = [dict(id='A', first=dict(pc_id='peer-1'))]
    events, waits = [], []
    live.read_events = lambda: list(events)
    def wait_until(deadline):
        cap = live.idle_deadline()
        deadline = min(deadline, cap) if cap is not None else deadline
        waits.append((clock.t, deadline)); clock.t = max(clock.t, deadline)
    live.wait_until = wait_until
    it = iter(statuses)
    def cell(label):
        clock.t += 20.0
        st = next(it)
        if st == 'PASS':
            events.append(dict(kind='server-frame', frame='TranscriptionFrame', pc_id='peer-1', monotonic_ns=clock.monotonic_ns()))
        return dict(label=label, status=st, failures=[], cause=cause)
    live.cell = cell
    monkeypatch.setattr(r, 'write', lambda *a, **k: None)
    seq = next(s for s in plan['sequence'] if s['id'] == 'A')
    live.block('A', seq['replies'], seq['spacing_s'], seq['min_epoch_s'])
    return live, waits, events


def test_an_early_cold_stop_does_not_soak(monkeypatch, tmp_path):
    """mute-01: cold A-1 INCOMPLETE broke the block, then the runner waited out min_epoch_s (600 s) and the bot
    cancelled at 300 s idle. Now an incomplete block returns without the soak tail."""
    live, waits, _ = block_with(['INCOMPLETE'], monkeypatch, tmp_path)
    assert all(d <= t for t, d in waits)                 # only the zero-length k=0 spacing wait; no soak tail


def test_a_complete_block_soaks_within_the_idle_budget(monkeypatch, tmp_path):
    live, waits, events = block_with(['PASS']*10, monkeypatch, tmp_path)
    assert waits and all(d-max([e['monotonic_ns']/1e9 for e in events if e['monotonic_ns']/1e9 <= t] or [1000.0])
                         <= PLAN['bot_idle']['max_runner_idle_s'] for t, d in waits)


def test_astra_cumulative_idle_trace_stops_inside_the_budget(monkeypatch, tmp_path):
    """Astra r12 limitation: with spacing 240 s (accepted by the static check) one PASS and then attempts without a
    user turn reached 720 s without bot activity. The cumulative account stops the block before the budget."""
    plan = copy.deepcopy(PLAN)
    st = next(s for s in plan['sequence'] if s['id'] == 'A'); st.update(spacing_s=240, min_epoch_s=0)
    plan['retakes']['allowed_causes'] = list(set(plan['retakes']['allowed_causes'])|{'no user turn within user_turn_timeout_s'})
    assert g.check_idle_budget(plan)
    live, waits, events = block_with(['PASS']+['INCOMPLETE']*5, monkeypatch, tmp_path, plan=plan,
                                     cause='no user turn within user_turn_timeout_s')
    last = max(e['monotonic_ns'] for e in events)/1e9
    assert r.time.t-last <= plan['bot_idle']['max_runner_idle_s']
    assert any(x['kind'] == 'block-stopped' for x in live.idle_budget_stops)


# -- decision 121: idle activity credits only Pipecat's idle-reset frames on the current peer ----------------------

def activity_live():
    live = r.MuteLive.__new__(r.MuteLive)
    live.last_bot_activity, live.plan = 1000.0, PLAN
    live.epochs = [dict(id='A', first=dict(pc_id='peer-1'))]
    return live


def test_idle_reset_frames_are_the_installed_pipecat_default():
    import inspect
    from pipecat.pipeline.worker import PipelineWorker
    installed = {c.__name__ for c in inspect.signature(PipelineWorker).parameters['idle_timeout_frames'].default}
    assert r.pipecat_idle_reset_frames() == installed
    assert set(r.IDLE_RESET_FALLBACK) <= installed                  # the fallback never credits a non-Pipecat frame


@pytest.mark.parametrize('frame,credited', [
    ('BotStartedSpeakingFrame', False), ('BotStoppedSpeakingFrame', False),           # Astra r13 probe: not idle frames
    ('TranscriptionFrame', True), ('InterimTranscriptionFrame', True), ('UserStartedSpeakingFrame', True),
    ('BotSpeakingFrame', True), ('UserSpeakingFrame', True),
])
def test_astra_activity_probe_frames(frame, credited):
    live = activity_live()
    live.note_bot_activity([dict(kind='frame', frame=frame, pc_id='peer-1', monotonic_ns=1239000000000)])
    assert (live.last_bot_activity == 1239.0) is credited
    assert live.idle_deadline() == (1239.0 if credited else 1000.0)+PLAN['bot_idle']['max_runner_idle_s']


@pytest.mark.parametrize('row', [
    dict(kind='tts-transport-write', non_silent=True, pc_id='peer-1'),          # a write is not a Pipecat frame
    dict(kind='aggregator-user-turn-started', pc_id='peer-1'),                   # an aggregator callback, not a frame
    dict(kind='frame', frame='TranscriptionFrame', pc_id='peer-0'),              # a retired peer's pending output
    dict(kind='frame', frame='TranscriptionFrame'),                              # no peer
])
def test_non_pipecat_rows_and_other_peers_are_never_credited(row):
    live = activity_live()
    live.note_bot_activity([dict(row, monotonic_ns=1239000000000)])
    assert live.last_bot_activity == 1000.0


def test_without_pipecat_the_fallback_is_the_conservative_observed_subset(monkeypatch):
    import builtins
    real = builtins.__import__
    def no_pipecat(name, *a, **k):
        if name.startswith('pipecat'):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', no_pipecat)
    assert r.pipecat_idle_reset_frames() == frozenset(r.IDLE_RESET_FALLBACK)


def test_fable_nit_a_retired_peers_writes_never_reset_the_idle_account():
    live = activity_live()
    live.note_bot_activity([dict(kind='tts-transport-write', non_silent=True, pc_id='peer-0', monotonic_ns=1239000000000),
                            dict(kind='server-frame', frame='TranscriptionFrame', pc_id='peer-0', monotonic_ns=1239000000000)])
    assert live.last_bot_activity == 1000.0


# -- r15 (decision 124): Fable r14 should-fixes, Astra idle note, Fable nit 3 --------------------------------------

def test_fable_a8_close_with_an_incoherent_future_device_t_fails_closed():
    """A close whose device t lies beyond every sample used to make every sample report-only (PASS); a sample received
    at or after the close line is now evidence regardless of t."""
    far = with_close(F-0.33, 10**12)
    res = score(opened_early(far))
    assert res['status'] == 'FAIL' and 'open mic sample during witnessed playback' in res['failures']


def test_fable_a7_sample_received_after_the_close_with_an_earlier_t_is_evidence():
    lines = sorted(with_close(F+0.2, device_t(F+0.2))+[(0, F+0.25, f'MIC_GATE t={device_t(F+0.2)-1000} closed=0 pre=-30.0 post=-30.0')],
                   key=lambda x: x[1])
    res = score(lines)
    assert res['status'] == 'FAIL' and 'open mic sample during witnessed playback' in res['failures']


def own_open_at(after_s, open_sample_at=None):
    """A-1 lines with the reply's ending GATE open received at first write + after_s (close at -0.33 s); samples
    received after it are reopened; optionally one open sample inside the reply."""
    out = []
    for i, h, text in LINES:
        if 'GATE open' in text:
            continue
        if 'MIC_GATE' in text and h > F+after_s:
            text = text.replace('closed=1', 'closed=0').split(' post=')[0]+' post=-60.0'
        out.append((i, h, text))
    out.append((0, F+after_s, f'GATE open t={device_t(F+after_s)} source_lower_us=0 source_valid=0'))
    if open_sample_at is not None:
        out = [x for x in out if not ('MIC_GATE' in x[2] and abs(x[1]-(F+open_sample_at)) < 0.02)]
        out.append(sample_at(F+open_sample_at, 0))
    return sorted(out, key=lambda x: x[1])


@pytest.mark.parametrize('after_s', [0.30, 0.45, 0.50])
def test_fable_a14_reply_open_inside_the_allowance_is_the_replys_own_open(after_s):
    lines = own_open_at(after_s)
    f, m, rec = g.gate_check(lines, F, F+0.1, F+after_s, F+after_s+2.0, PLAN['echo_cell'])
    assert f == [] and m == [], (f, m, rec)
    assert rec['close_receipt_s'] < F


def test_fable_d3_open_sample_inside_a_reply_whose_open_lies_inside_the_allowance_fails():
    lines = own_open_at(0.45, open_sample_at=0.2)
    f, m, rec = g.gate_check(lines, F, F+0.1, F+0.45, F+2.45, PLAN['echo_cell'])
    assert 'open mic sample during witnessed playback' in f


def test_r13_blocker_pinned_on_the_real_a1():
    """Fable r14 nit 3: the r13 blocker lived in the close-vs-first-write rule. On the real A-1 with its close RECEIVED
    171 ms after the first write (retained warm-2 latency, real device t), r14+ PASSes; the r13 gate (a940bc9) said
    INCOMPLETE. The r13 comparison runs from git when the commit is available."""
    assert score(with_close(F+0.171))['status'] == 'PASS'
    import importlib.util, subprocess, sys, tempfile
    try:
        src = subprocess.check_output(['git', 'show', 'a940bc9:hil/echoear_mute_gate.py'], cwd=HERE, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        pytest.skip('r13 commit not available')
    with tempfile.TemporaryDirectory() as d:
        path = Path(d)/'gate_r13.py'; path.write_text(src)
        spec = importlib.util.spec_from_file_location('gate_r13', path); old = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(HERE)); spec.loader.exec_module(old)
    r13 = old.score_echo_cell(FIX['rows'], with_close(F+0.171), recording(), PLAN, CELL['prompt'], CELL['prompt_start_s'],
                              CELL['prompt_end_s'], CELL['end_s'])
    assert r13['status'] == 'INCOMPLETE'


def test_astra_idle_first_observation_per_frame_only():
    """Pipecat's observer sees a frame's first push only: two taps of the same frame (same frame_id) at 1000.5 and
    1001.0 credit 1000.5, not 1001.0; distinct frames still take the latest."""
    live = activity_live()
    live.note_bot_activity([dict(kind='server-frame', frame='UserStartedSpeakingFrame', frame_id=7, pc_id='peer-1', monotonic_ns=1000500000000),
                            dict(kind='frame', frame='UserStartedSpeakingFrame', frame_id=7, pc_id='peer-1', monotonic_ns=1001000000000)])
    assert live.last_bot_activity == 1000.5
    live.note_bot_activity([dict(kind='frame', frame='TranscriptionFrame', frame_id=8, pc_id='peer-1', monotonic_ns=1002000000000)])
    assert live.last_bot_activity == 1002.0
