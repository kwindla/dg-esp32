"""Mute-image gate: pure scoring and the half-duplex adapter, with fakes only.

No bench, serial, socket, ALSA or bot access. Synthetic recorder rows, serial lines
(index, host_s, text), recordings, fake arecord processes and PERF snapshots drive every rule,
including the compound/adversarial probes from the r2 reviews (Astra R1-R6, Fable 2.2.1-2.2.2).
"""
import copy
import datetime
import json
import os
import re
import signal
from pathlib import Path

import numpy as np
import pytest

import echoear_mute_gate as g
import echoear_rollback as r

PLAN, PLAN_SHA = g.load_plan()
REAL_RECORDING = r.RawRecording
NS = 1_000_000_000


def ns(s):
    return int(s*NS)


def cell_rows():
    base = [dict(kind='aggregator-user-turn-started', monotonic_ns=ns(103.0)),
            dict(kind='aggregator-user-turn-stopped', text='What is the capital of France?', monotonic_ns=ns(103.5)),
            dict(kind='server-frame', frame='TTSStartedFrame', boundary='tts-written', context_id='ctx', monotonic_ns=ns(103.9)),
            dict(kind='tts-transport-write', non_silent=True, drop=False, context_id='ctx', monotonic_ns=ns(104.0)),
            dict(kind='tts-transport-write', non_silent=True, drop=False, context_id='ctx', monotonic_ns=ns(105.0)),
            dict(kind='tts-transport-write', non_silent=True, drop=False, context_id='ctx', monotonic_ns=ns(106.0)),
            dict(kind='server-frame', frame='TTSStoppedFrame', boundary='tts-written', context_id='ctx', monotonic_ns=ns(106.05)),
            dict(kind='server-frame', frame='BotStoppedSpeakingFrame', boundary='tts-written', monotonic_ns=ns(106.1)),
            dict(kind='aggregator-assistant-turn-stopped', text='Paris.', monotonic_ns=ns(106.2))]
    return [dict(e, pc_id='peer') for e in base]


def cell_lines(post=-120.0, reopen=True, gate_open=True, extra=(), skip=(), close=True):
    lines = []
    if close:      # the device closes on bot-started-speaking, before the first write (decision 122 close witness)
        lines.append((99, 103.95, f'I (1) audio: GATE closed t={int(103.95e6)} source_lower_us=0 source_valid=1'))
    for i, t in enumerate(np.arange(104.1, 106.35, 0.1)):
        t = round(float(t), 2)
        if any(a <= t <= b for a, b in skip):
            continue
        lines.append((i, t, f'MIC_GATE t={int(t*1e6)} closed=1 pre=-20 post={post}'))
    if gate_open:
        lines.append((100, 106.5, 'I (1) audio: GATE open t=1 source_lower_us=0 source_valid=1'))
    if reopen:
        lines.append((101, 106.7, f'MIC_GATE t={int(106.7e6)} closed=0 pre=-50 post=-50'))
    lines += list(extra)
    return sorted(lines, key=lambda x: x[1])


def recording(reply_level=3000, start=100.0, seconds=10, healthy=True, interval=(100.0, 100.05)):
    rate = 16000
    rng = np.random.default_rng(1)
    s = rng.normal(0, 30, rate*seconds)
    t = np.arange(rate*seconds)/rate+start
    reply = (t >= 104.0) & (t <= 106.2)
    s[reply] += reply_level*np.sin(2*np.pi*1000*t[reply])
    return dict(samples=np.clip(s, -32768, 32767).astype('<i2'), rate=rate, start_interval_s=list(interval) if interval else None,
                healthy=healthy, health=dict(healthy=healthy))


def score(rows=None, lines=None, rec='default', end_s=109.0, prompt='france', stimulus_error=None):
    return g.score_echo_cell(cell_rows() if rows is None else rows, cell_lines() if lines is None else lines,
                             recording() if rec == 'default' else rec, PLAN, prompt, 101.0, 103.0, end_s,
                             stimulus_error=stimulus_error)


def with_row(**e):
    return cell_rows()+[dict(e, pc_id=e.get('pc_id', 'peer'))]


# -- basic cell ------------------------------------------------------------------------------

def test_complete_echo_only_cell_passes_with_every_witness():
    c = score()
    assert c['status'] == 'PASS', c
    assert c['contexts_ok'] and c['gate']['samples'] >= 3 and c['playback']['rise_db'] >= 10
    assert c['tail_end_s'] == pytest.approx(108.5) and not c['failures'] and not c['missing']


@pytest.mark.parametrize('event,cause', [
    (dict(kind='aggregator-user-turn-started', monotonic_ns=ns(107.0)), 'false turn'),
    (dict(kind='server-frame', frame='UserStartedSpeakingFrame', monotonic_ns=ns(105.5)), 'false turn'),
    (dict(kind='flux-frame', frame='TranscriptionFrame', text='paris', monotonic_ns=ns(108.0)), 'false turn'),
    (dict(kind='server-frame', frame='InterruptionFrame', monotonic_ns=ns(104.5)), 'false turn'),
    (dict(kind='tts-transport-write', non_silent=True, drop=False, context_id='ctx2', monotonic_ns=ns(107.5)), 'after reply closure'),
])
def test_any_accepted_turn_interruption_or_unscripted_output_fails(event, cause):
    c = score(rows=with_row(**event))
    assert c['status'] == 'FAIL' and any(cause in f for f in c['failures'])


def test_interim_and_vad_events_are_report_only():
    extra = [dict(kind='flux-frame', frame='InterimTranscriptionFrame', text='pa', monotonic_ns=ns(107.0), pc_id='peer'),
             dict(kind='server-frame', frame='VADUserStartedSpeakingFrame', monotonic_ns=ns(107.1), pc_id='peer')]
    c = score(rows=cell_rows()+extra)
    assert c['status'] == 'PASS' and c['report_only_events'] == 2


# -- R2: latched failures win over any missing witness --------------------------------------

def test_r2_false_transcript_without_recording_is_fail_not_retake():
    c = score(rows=with_row(kind='flux-frame', frame='TranscriptionFrame', text='paris', monotonic_ns=ns(107.0)), rec=None)
    assert c['status'] == 'FAIL' and 'ALSA recording failed' in c['missing']
    v = g.block_verdict([c]+[score()]*10, 10, 2, PLAN)
    assert v['status'] == 'FAIL'


@pytest.mark.parametrize('stim', ['jabra playback duration invalid', 'ALSA recording failed'])
def test_r2_stimulus_error_never_hides_a_witnessed_failure(stim):
    bad = with_row(kind='server-frame', frame='InterruptionFrame', monotonic_ns=ns(104.5))
    assert score(rows=bad, stimulus_error=stim)['status'] == 'FAIL'
    ok = score(stimulus_error=stim)
    assert ok['status'] == 'INCOMPLETE' and ok['cause'] == stim


def test_r2_provider_error_before_exposure_is_the_only_provider_retake():
    early = with_row(kind='server-frame', frame='ErrorFrame', monotonic_ns=ns(103.6))
    early = [e for e in early if e['kind'] != 'tts-transport-write']
    c = score(rows=early)
    assert c['status'] == 'INCOMPLETE' and 'provider ErrorFrame before the first write' in c['missing']
    late = score(rows=with_row(kind='server-frame', frame='ErrorFrame', monotonic_ns=ns(105.5)))
    assert late['status'] == 'FAIL' and 'provider error after exposure began' in late['failures']
    for kind in (dict(kind='server-frame', frame='FatalErrorFrame', monotonic_ns=ns(103.6)),
                 dict(kind='gap', monotonic_ns=ns(103.6)), dict(kind='pipeline-error', monotonic_ns=ns(107.0))):
        assert score(rows=with_row(**kind))['status'] == 'FAIL'


def test_r2_block_precedence_across_attempts():
    passed = score()
    latched = dict(score(rec=None), status='INCOMPLETE', failures=['x'])   # any latched failure is final
    assert g.block_verdict([latched]+[passed]*10, 10, 2, PLAN)['status'] == 'FAIL'
    inc = dict(status='INCOMPLETE', cause='ALSA recording failed', failures=[])
    assert g.block_verdict([passed, inc, inc]+[passed]*9, 10, 2, PLAN, cold_first=True)['status'] == 'PASS'
    assert g.block_verdict([passed, inc, inc, inc]+[passed]*9, 10, 2, PLAN)['status'] == 'INCOMPLETE'
    assert g.block_verdict([dict(status='INCOMPLETE', cause='no reply output')]+[passed]*10, 10, 2, PLAN)['status'] == 'INCOMPLETE'


def test_cold_first_is_not_retakeable():
    passed = score()
    inc = dict(status='INCOMPLETE', cause='ALSA recording failed', failures=[])
    assert g.block_verdict([inc]+[passed]*10, 10, 2, PLAN, cold_first=True)['status'] == 'INCOMPLETE'
    assert g.block_verdict([inc]+[passed]*10, 10, 2, PLAN, cold_first=False)['status'] == 'PASS'


# -- R3: declared complete-reply contract ---------------------------------------------------

def test_r3_dropped_write_fails():
    rows = cell_rows()
    rows[4]['drop'] = True
    assert 'dropped transport write' in score(rows=rows)['failures']


def test_r3_missing_tts_lifecycle_fails():
    rows = [e for e in cell_rows() if e.get('frame') not in ('TTSStartedFrame', 'TTSStoppedFrame')]
    assert 'TTS context lifecycle incomplete' in score(rows=rows)['failures']
    rows = cell_rows()
    rows[2]['context_id'] = 'other'
    assert 'TTS context lifecycle incomplete' in score(rows=rows)['failures']


def test_r3_output_after_stop_but_before_gate_open_fails():
    c = score(rows=with_row(kind='tts-transport-write', non_silent=True, drop=False, context_id='ctx', monotonic_ns=ns(106.3)))
    assert c['status'] == 'FAIL' and 'bot output after reply closure' in c['failures']
    c = score(rows=with_row(kind='server-frame', frame='TTSStartedFrame', boundary='tts-written', context_id='n', monotonic_ns=ns(106.4)))
    assert 'bot output after reply closure' in c['failures']


def test_r3_second_assistant_or_peer_fails():
    assert 'more than one user or assistant turn' in score(rows=with_row(kind='aggregator-assistant-turn-stopped', text='More.', monotonic_ns=ns(106.3)))['failures']
    rows = cell_rows()
    rows[-1]['pc_id'] = 'other'
    assert 'more than one peer' in score(rows=rows)['failures']


# -- R4: full-interval mic-gate coverage ------------------------------------------------------

def test_r4_open_nonzero_sample_inside_playback_fails():
    open_sample = (50, 105.55, f'MIC_GATE t={int(105.55e6)} closed=0 pre=-20 post=-20')
    c = score(lines=cell_lines(extra=[open_sample]))
    assert c['status'] == 'FAIL' and 'open mic sample during witnessed playback' in c['failures']


def test_r4_nonzero_closed_sample_and_premature_open_fail():
    assert 'nonzero closed mic sample during playback' in score(lines=cell_lines(post=-80.0))['failures']
    early = (60, 105.2, 'I (1) audio: GATE open t=1 source_lower_us=0 source_valid=1')
    assert 'premature GATE open during playback' in score(lines=cell_lines(extra=[early]))['failures']


def test_r4_telemetry_gap_is_incomplete_and_stuck_gate_fails():
    c = score(lines=cell_lines(skip=[(104.8, 105.4)]))
    assert c['status'] == 'INCOMPLETE' and 'mic-gate telemetry gap' in c['missing']
    stuck = cell_lines(reopen=False, extra=[(102, 106.8, f'MIC_GATE t={int(106.8e6)} closed=1 pre=-50 post=-120.0')])
    assert 'mic gate did not reopen after playback' in score(lines=stuck)['failures']
    assert 'no mic-gate sample after GATE open' in score(lines=cell_lines(reopen=False))['missing']


# -- R1: recording health and time bounds -----------------------------------------------------

def test_r1_unhealthy_or_unbounded_recording_is_incomplete_and_silent_speaker_fails():
    assert score(rec=recording(reply_level=0))['failures'] == ['playback not witnessed above the room floor']
    for rec in (recording(healthy=False), recording(interval=(100.0, 100.2)), recording(interval=None), None, recording(seconds=3)):
        c = score(rec=rec)
        assert c['status'] == 'INCOMPLETE' and 'ALSA recording failed' in c['missing']


def test_r1_windows_hold_for_every_admissible_origin():
    w = g.playback_witness(recording(), PLAN, 101.0, 103.9, 103.95, 106.1)
    assert w['active_window_s'][0] >= 103.9+0.2 and w['uncertainty_s'] == pytest.approx(0.05)
    # The floor ends at the prompt for the LATEST origin and the active window starts after it for the EARLIEST.
    rate = 16000
    assert w['floor_samples'] <= int((0.5-0.05)*rate)+1 and w['active_samples'] <= int((106.1-104.1-0.05)*rate)+1


class FakeProc:
    """arecord stand-in. wait() models the child: it keeps capturing for `handle_s` after SIGINT, then the
    parent's wait() returns `reap_s` later; the PCM written is decided by the model (gaps, lost tail)."""
    def __init__(self, behaviour='ok', log=None, clock=None, handle_s=0.0, reap_s=0.0, write=None):
        self.behaviour, self.returncode, self.signals, self.log = behaviour, None, [], log
        self.clock, self.handle_s, self.reap_s, self.write = clock, handle_s, reap_s, write
        if behaviour == 'early':
            self.returncode = 1
    def poll(self):
        return self.returncode
    def send_signal(self, sig):
        self.signals.append(sig)
        if self.behaviour in ('ok', 'bare-exit'):
            self.pending = True
    def wait(self, timeout=None):
        if self.behaviour in ('stuck', 'immortal') and self.returncode is None:
            raise r.subprocess.TimeoutExpired('arecord', timeout)
        if getattr(self, 'pending', False):
            self.pending = False
            if self.clock is not None:
                self.clock.t += self.handle_s
            if self.write:
                self.write()
            if self.clock is not None:
                self.clock.t += self.reap_s
            self.returncode = 1
            if self.behaviour == 'ok' and self.log is not None:
                with open(self.log, 'a') as f:
                    f.write('Aborted by signal Interrupt...\n')
        return self.returncode
    def kill(self):
        self.signals.append('kill')
        if self.behaviour != 'immortal':
            self.returncode = -9


class Clock:
    def __init__(self):
        self.t = 100.0
    def __call__(self):
        self.t += 0.0005
        return self.t


HWPARAMS = 'Plug PCM: Hardware PCM card 0\n  rate         : 16000\n  period_size  : 200\n  buffer_size  : 800\n'


def fake_recording(tmp, behaviour='ok', ready=True, log_extra='', seconds=1.0, owner=None, startup_s=0.03,
                   handle_s=0.005, reap_s=0.01, gaps_s=(), tail_lost_s=0.0, drift_ppm=0.0, hw=HWPARAMS):
    """Model: Popen at t_spawn; sample zero `startup_s` later; the parent sends SIGINT `seconds` after readiness;
    the child captures `handle_s` more, loses `tail_lost_s` at abort (silent pending overrun) and `gaps_s`
    elsewhere (each a silent gap), and the device clock runs `drift_ppm` off. Returns the recorder and the
    true time of written samples 0 and N/2."""
    path = tmp/'room.raw'
    log = tmp/'room.alsa.log'
    clock = Clock()
    state = {}
    def write_pcm():
        if 'origin' not in state:
            return
        span = clock.t-tail_lost_s-state['origin']
        written = span/(1+drift_ppm*1e-6)-sum(gaps_s)
        n = max(0, int(16000*written))
        # arecord 1.2.13 semantics: open(O_WRONLY|O_CREAT) WITHOUT O_TRUNC (Fable r6 probe A2)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.write(fd, b'\0\0'*n)
        finally:
            os.close(fd)
        # true time of the middle written sample: the first gap lies before it
        mid = n//2
        state['mid_k'], state['mid_t'] = mid, state['origin']+(mid/16000)*(1+drift_ppm*1e-6)+(gaps_s[0] if gaps_s else 0)
    proc = FakeProc(behaviour, log, clock, handle_s, reap_s, write_pcm)
    def popen(args, stderr):
        assert '-B' in args and args[args.index('-B')+1] == '50000'
        state['spawn'] = clock.t
        stderr.write((hw if ready else '')+log_extra); stderr.flush()
        return proc
    owner = [] if owner is None else owner
    rec = REAL_RECORDING(path, 'dev', owner, popen=popen, clock=clock, sleep=lambda s: None)
    state['origin'] = state['spawn']+startup_s
    rec.true_origin, rec.state = state['origin'], state
    clock.t = rec.ready_s+seconds
    if behaviour in ('early', 'stuck', 'immortal'):
        write_pcm()
    return rec, proc, owner


def contains(h, k, t):
    lo, hi = h['start_interval_s']
    return lo+k/16000 <= t <= hi+k/16000


def test_r1_healthy_owned_recording(tmp_path):
    rec, proc, owner = fake_recording(tmp_path)
    assert owner == [rec]
    h = rec.stop()
    assert h['healthy'] and h['stopped_by'] == 'sigint' and proc.signals == [signal.SIGINT] and h['aborted_by_sigint']
    assert contains(h, 0, rec.true_origin) and h['width_ok'] and h['uncertainty_s'] <= 0.15
    assert h['diagnostic']['buffer_s'] == pytest.approx(0.05)          # diagnostic only
    assert json.loads(json.dumps(rec.result()['health']))['healthy'] and (tmp_path/'room.health.json').exists()


@pytest.mark.parametrize('behaviour,log,why', [('early', '', 'exited_early'), ('stuck', '', 'stopped_by'),
                                               ('ok', 'overrun!!! (at least 1 ms long)\n', 'alsa_errors'),
                                               ('ok', 'arecord: pcm_read:2221: read error: Input/output error\n', 'alsa_errors'),
                                               ('bare-exit', '', 'aborted_by_sigint')])
def test_r1_early_exit_forced_kill_alsa_error_or_unexplained_status_is_unhealthy(tmp_path, behaviour, log, why):
    rec, proc, _ = fake_recording(tmp_path, behaviour, log_extra=log)
    h = rec.stop()
    assert not h['healthy']
    assert {'exited_early': h['exited_early'], 'stopped_by': h['stopped_by'] == 'kill', 'alsa_errors': bool(h['alsa_errors']),
            'aborted_by_sigint': not h['aborted_by_sigint']}[why]


@pytest.mark.parametrize('case', [
    dict(startup_s=0.010, handle_s=0.001, reap_s=0.006, tail_lost_s=0.1),     # Astra r5: silent pending overrun at abort
    dict(startup_s=0.08, handle_s=0.09, reap_s=0.005),                          # Astra r4: late start, child handles late
    dict(startup_s=0.02, handle_s=0.0, reap_s=0.12),                            # slow reap
])
def test_r6_review_counterexamples_contain_the_truth_or_are_incomplete(tmp_path, case):
    rec, proc, _ = fake_recording(tmp_path, **case)
    h = rec.stop()
    assert contains(h, 0, rec.true_origin)                    # the bound holds even when health rejects the width
    assert h['healthy'] == h['width_ok']
    if not h['healthy']:
        assert h['uncertainty_s'] > 0.15
        w = g.playback_witness(dict(rec.result(), samples=np.zeros(16000*2, '<i2')), PLAN, 101.0, 101.5, 101.6, 102.0)
        assert w['missing']


def test_r6_randomized_lost_samples_late_start_and_slow_reap_never_mislocate(tmp_path):
    rng = np.random.default_rng(11)
    healthy = 0
    for k in range(300):
        d = tmp_path/str(k); d.mkdir()
        gaps = tuple(float(x) for x in rng.uniform(0, 0.03, int(rng.integers(0, 3))))
        case = dict(startup_s=float(rng.uniform(0, 0.12)), handle_s=float(rng.uniform(0, 0.06)), reap_s=float(rng.uniform(0, 0.08)),
                    tail_lost_s=float(rng.uniform(0, 0.12)), gaps_s=gaps, drift_ppm=float(rng.uniform(-400, 400)))
        rec, proc, _ = fake_recording(d, **case)
        h = rec.stop()
        assert contains(h, 0, rec.true_origin), case
        assert contains(h, rec.state['mid_k'], rec.state['mid_t']), case
        healthy += h['healthy']
        if h['healthy']:
            assert h['uncertainty_s'] <= 0.15
    assert 0 < healthy < 300


def test_r1_width_over_the_limit_is_flagged_by_health_itself(tmp_path):
    rec, proc, _ = fake_recording(tmp_path, startup_s=0.12, reap_s=0.05)
    h = rec.stop()
    assert not h['width_ok'] and not h['healthy'] and h['uncertainty_s'] > 0.15


def test_r7_stale_longer_target_is_unlinked_before_popen(tmp_path):
    """Fable r6 should-fix 1 / probe A2: a pre-existing longer file must not survive into the capture."""
    (tmp_path/'room.raw').write_bytes(b'\1\1'*int(16000*1.005))
    rec, proc, _ = fake_recording(tmp_path, seconds=0.95, startup_s=0.03)
    h = rec.stop()
    assert h['replaced_existing'] and h['healthy']
    assert h['bytes'] < 2*int(16000*1.005) and contains(h, 0, rec.true_origin)


@pytest.mark.parametrize('hw', [HWPARAMS.replace('16000', '48000'),
                                'Plug PCM: Rate conversion PCM (48000, sformat=S16_LE)\n' + HWPARAMS,
                                'Plug PCM: Hardware PCM card 0\n  period_size  : 200\n  buffer_size  : 800\n'])
def test_r7_hw_rate_other_than_16000_or_resampled_or_unreported_is_unhealthy(tmp_path, hw):
    """Fable r6 should-fix 3: the subsequence premise needs the device's native 16 kHz."""
    rec, proc, _ = fake_recording(tmp_path, hw=hw)
    h = rec.stop()
    assert not h['rate_ok'] and not h['healthy']


def test_r7_hw_rate_16000_is_reported(tmp_path):
    rec, proc, _ = fake_recording(tmp_path)
    h = rec.stop()
    assert h['rate_ok'] and h['hw_rates'] == [16000] and not h['replaced_existing']


def test_r1_constructor_failure_after_spawn_is_owned_and_stopped(tmp_path):
    owner = []
    with pytest.raises(RuntimeError, match='did not become ready'):
        fake_recording(tmp_path, ready=False, owner=owner)
    assert len(owner) == 1 and owner[0].stopped_by is not None


class CellHarness(r.MuteLive):
    """Real cell() with fakes around it: recorder, Jabra, events, serial."""
    def __init__(self, tmp, raise_in_tick=False, behaviour='ok'):
        super().__init__(dict(plan_sha256=PLAN_SHA, public_env=PLAN['public_env'], fixtures={'france': dict(file='x')}), PLAN, tmp)
        self.board = type('B', (), dict(cursor=0, lines_since=lambda self, c: []))()
        self.raise_in_tick, self.behaviour, self.procs = raise_in_tick, behaviour, []
    def tick(self):
        if self.raise_in_tick:
            raise self.raise_in_tick('owned bot died')


@pytest.mark.parametrize('exc', [RuntimeError, KeyboardInterrupt])   # signal handlers raise into the cell
def test_r1_exception_during_cell_stops_and_reaps_the_recorder(tmp_path, monkeypatch, exc):
    h = CellHarness(tmp_path, raise_in_tick=exc)
    procs = []
    def make(path, device, owner, **kw):
        rec, proc, _ = fake_recording(tmp_path, owner=owner)
        procs.append(proc)
        return rec
    monkeypatch.setattr(r, 'RawRecording', make)
    monkeypatch.setattr(r.time, 'sleep', lambda s: None)
    import alsa
    monkeypatch.setattr(alsa, 'play_wav', lambda *a: dict(duration_valid=True))
    with pytest.raises(exc, match='owned bot died'):
        h.cell('A-1', prompt='france')
    assert h.recorders == [] and procs[0].signals == [signal.SIGINT]


def test_r1_cleanup_with_a_live_recorder_reports_an_error_so_the_lock_is_kept(tmp_path, monkeypatch):
    h = CellHarness(tmp_path)
    rec, proc, _ = fake_recording(tmp_path, behaviour='immortal', owner=h.recorders)
    proc.behaviour = 'immortal'
    proc.send_signal = lambda sig: None
    monkeypatch.setattr(r.Live, 'cleanup', lambda self, attempt: dict(errors=[], restored_elf_sha256=r.NORMAL_HASH, owned_bot_stopped=True))
    result = h.cleanup(1)
    assert any('live capture' in e for e in result['errors'])


# -- counters, terminal, liveness, admission, playout -----------------------------------------

def perf(**over):
    rejected = {k: 0 for k in ('capacity', 'conflict', 'duplicate', 'duration', 'malformed', 'payload_type',
                               'preconnect', 'source', 'window', 'late', 'generation')}
    p = dict(pc_id='peer', generation=1, lost=0, received=1000, plc_frames=0, late=0, concealed=0,
             i2s_underflows=0, i2s_write_errors=0, i2s_write_timeouts=0, pcm_queue_drops=0,
             failed_allocations=dict(total=0), console_output=dict(full=0, transport_drops=0),
             app_data_drops=0, queue_dropped=0, playout_dropped=0, send_budget_cutoffs=0,
             playout=dict(post_plc_zeros=0, starvation=0, target_ms=140, ready_high_water=8, rejected=rejected),
             playout_target_ms=140, target_depth=7, prefills=1, output_volume_db=-6,
             cancellation=dict(events=0, duplicate_notices=0),
             display=dict(dma_errors=0, send_failures=0, gate_timeouts=0, display_gate_timeouts=0,
                          lock_failures=0, lock_timeouts=0, rendered=10, failed=False, dead=False),
             drop_reasons=dict(admission_budget=0, admission_overflow=0, message_queue=0),
             data_admission=dict(received=10, dispatched=10),
             liveness=dict(pings=10, pongs=10, timeouts=0))
    for k, v in over.items():
        p[k] = v
    return p


def bump(p, path, by=1):
    p = copy.deepcopy(p)
    node = p
    keys = path.split('.')
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] += by
    return p


def test_counter_epoch_each_declared_counter_fails_and_missing_fails():
    a, b = perf(), perf(received=2000, lost=10)
    assert g.counter_epoch(a, b, PLAN)['passed']
    for path in PLAN['counters']['zero_delta_per_epoch']+['drop_reasons.admission_overflow']:
        assert not g.counter_epoch(a, bump(b, path), PLAN)['passed'], path


def test_fable_late_rejections_are_report_only_but_capacity_rejections_fail():
    a = perf()
    late = bump(bump(perf(received=2000), 'playout.rejected.late', 11), 'playout_dropped', 11)
    assert g.counter_epoch(a, late, PLAN)['passed']
    gen = bump(perf(), 'playout.rejected.generation')
    assert g.counter_epoch(a, gen, PLAN)['passed']
    assert not g.counter_epoch(a, bump(perf(), 'playout.rejected.capacity'), PLAN)['passed']
    assert 'playout_dropped' not in PLAN['counters']['zero_delta_per_epoch']


def test_counter_epoch_loss_and_session_change():
    a = perf()
    assert not g.counter_epoch(a, perf(received=2000, lost=21), PLAN)['passed']
    assert g.counter_epoch(a, perf(received=2000, lost=20), PLAN)['passed']
    assert not g.counter_epoch(a, perf(generation=2), PLAN)['passed']


def test_r6_terminal_telemetry_counts_teardown_faults_and_missing_is_incomplete():
    a = perf()
    term = perf(liveness=dict(pings=40, pongs=40, timeouts=0))
    assert g.terminal_epoch(a, term, PLAN)['passed']
    teardown_fault = bump(term, 'i2s_write_errors')       # only visible in the disconnect-time PERF
    assert g.gate_verdict(dict(t=g.terminal_epoch(a, teardown_fault, PLAN)))['outcome'] == 'FAIL'
    after_last_snapshot = bump(term, 'failed_allocations.total')
    assert not g.terminal_epoch(a, after_last_snapshot, PLAN)['passed']
    assert g.terminal_epoch(a, None, PLAN)['status'] == 'INCOMPLETE'
    assert g.terminal_epoch(a, perf(generation=2), PLAN)['status'] == 'INCOMPLETE'
    assert g.gate_verdict(dict(t=g.terminal_epoch(a, None, PLAN)))['outcome'] == 'INCOMPLETE'


def test_liveness_admission_and_playout_epochs():
    a = perf()
    b = perf(liveness=dict(pings=40, pongs=39, timeouts=0), data_admission=dict(received=60, dispatched=60))
    assert g.liveness_epoch(a, b)['passed'] and g.admission_epoch(a, b)['passed']
    assert not g.liveness_epoch(a, perf(liveness=dict(pings=40, pongs=37, timeouts=0)))['passed']
    assert not g.liveness_epoch(a, perf(liveness=dict(pings=40, pongs=40, timeouts=1)))['passed']
    assert not g.admission_epoch(a, perf(liveness=dict(pings=40, pongs=40, timeouts=0)))['passed']
    assert g.playout_epoch([a, b], PLAN)['passed']
    hw = perf(); hw['playout'] = dict(hw['playout'], ready_high_water=11)
    assert not g.playout_epoch([hw], PLAN)['passed']
    assert g.volume_epoch([a, b], -6)['passed'] and not g.volume_epoch([a, perf(output_volume_db=0)], -6)['passed']


def snapshot_for_resources(aec=False):
    tasks = [dict(name=n, stack_bytes=s, stack_free_bytes=f) for n, s, f in (
        ('audio-playback', 17152, 6000), ('audio-capture', 8192, 5000), ('audio-tx', 28672, 9000),
        ('peer-loop', 10752, 5000), ('peer-rx', 4096, 2000), ('app-control', 8192, 3000))]
    tasks += [dict(name='IDLE0', window_us=6000000, stack_bytes=0, stack_free_bytes=0),
              dict(name='IDLE1', window_us=6000000, stack_bytes=0, stack_free_bytes=0)]
    p = perf(tasks=tasks, heap=dict(internal=dict(minimum_free=90000, largest=40000)),
             encode_p95_us=7000, encode_max_us=9000, encode_frames=100, per_core_idle_percent=[60, 40],
             uplink_frame_ms=20, audio=dict(drops=0, read_errors=0, write_errors=0, write_timeouts=0))
    if aec:
        p['aec'] = dict(errors=0, reference_clips=0, output_clips=0, capture=dict(read_errors=0, read_timeouts=0, adc_clips=0, gain_clips=0))
    return p


def test_resources_mark_aec_checks_not_applicable_only_when_absent():
    ok = g.resources([snapshot_for_resources(), snapshot_for_resources()])
    assert ok['passed'] and all(v['status'] == 'N/A' for v in ok['aec_not_applicable'].values())
    assert not g.resources([snapshot_for_resources(), snapshot_for_resources(aec=True)])['passed']


def test_mode_mismatch_is_invalid_not_pass():
    log = PLAN['mode']['bot_duplex_line']
    device = ['I (1) app: '+PLAN['mode']['device_capability_line']]
    rows = [dict(kind='stt-profile', config=dict(full_duplex=False, profile='demo'))]
    assert g.mode_check(log, device, rows, [perf()], PLAN)['passed']
    assert not g.mode_check(log.replace('False', 'True'), device, rows, [perf()], PLAN)['passed']
    assert not g.mode_check(log, ['AUDIO_CAPABILITY aec=1 ref_gain_db=15 guard_ms=400'], rows, [perf()], PLAN)['passed']
    assert g.gate_verdict(dict(mode=dict(passed=False), other=dict(passed=True)))['outcome'] == 'INVALID'


def test_any_interruption_or_cancel_fails_the_na_replacement_check():
    snaps = [[perf()]]
    assert g.interrupt_absent([], [], snaps)['passed']
    assert not g.interrupt_absent([dict(frame='InterruptionFrame', monotonic_ns=1)], [], snaps)['passed']
    assert not g.interrupt_absent([], ['AUDIO_CANCEL callback_us=1 device_us=2 gen=1 applied=1 mute_ms=150'], snaps)['passed']
    assert not g.interrupt_absent([], [], [[bump(perf(), 'cancellation.events')]])['passed']


def test_interrupt_na_reason_is_the_device_gate():
    # Decision 141: the bot no longer mutes. In the AEC-off image only the firmware capture gate keeps
    # bot speech from reaching the bot, so an interruption in a mute run is a device failure.
    from demo_policy import turn_policy
    assert turn_policy().user_mute_strategies == []
    firmware = (Path(r.ROOT)/'main/app.cpp').read_text()
    assert 'send_text' not in firmware and 'send-text' not in firmware


# -- R5: reconnect credit requires the retained trigger ---------------------------------------

def reconnect_inputs():
    before, after = perf(pc_id='old', generation=1), perf(pc_id='new', generation=2)
    ready = [dict(generation=1, device_us=1), dict(generation=2, device_us=2)]
    rows = [dict(kind='aggregator-user-turn-started', pc_id='new', monotonic_ns=10),
            dict(kind='tts-transport-write', non_silent=True, pc_id='new', monotonic_ns=20)]
    return before, after, ready, rows


def trig(kind='mid-reply', lag=0.05, active=True):
    return dict(kind=kind, fired=True, fired_s=1.0, disconnect_lag_s=lag, active=active)


def test_r5_reconnect_requires_the_declared_trigger():
    before, after, ready, rows = reconnect_inputs()
    assert g.reconnect_rule('R1', before, after, ready, {'old'}, rows, 2.0, PLAN, trigger=trig())['passed']
    assert g.reconnect_rule('R1', before, after, ready, {'old'}, rows, 2.0, PLAN, trigger=None)['status'] == 'INCOMPLETE'
    assert not g.reconnect_rule('R1', before, after, ready, {'old'}, rows, 2.0, PLAN, trigger=trig(active=False))['passed']
    assert not g.reconnect_rule('R1', before, after, ready, {'old'}, rows, 2.0, PLAN, trigger=trig(lag=2.0))['passed']
    assert not g.reconnect_rule('R2', before, after, ready, {'old'}, rows, 2.0, PLAN, trigger=trig())['passed']
    assert g.reconnect_rule('R2', before, after, ready, {'old'}, rows, 2.0, PLAN, trigger=trig('after-gate-open'))['passed']
    assert g.reconnect_rule('R3', before, after, ready, {'old'}, rows, 2.0, PLAN)['passed']


def test_r5_pre_ready_stale_write_on_the_new_peer_fails():
    before, after, ready, rows = reconnect_inputs()
    stale = [dict(kind='tts-transport-write', non_silent=True, pc_id='new', monotonic_ns=5)]+rows
    r3 = g.reconnect_rule('R3', before, after, ready, {'old'}, stale, 2.0, PLAN)
    assert r3['status'] == 'FAIL' and r3['stale_writes'] == 1
    assert not g.reconnect_rule('R3', before, perf(pc_id='old', generation=2), ready, {'old'}, rows, 2.0, PLAN)['passed']
    assert not g.reconnect_rule('R3', before, after, ready, {'old'}, rows, 11.0, PLAN)['passed']


# -- timing --------------------------------------------------------------------------------

def test_exclusion_constrains_acquisition_plus_protected_cleanup():
    at = lambda h, m: datetime.datetime(2026, 10, 26, h, m)
    budget = PLAN['budget']['acquisition_deadline_s']+PLAN['budget']['cleanup_reserve_s']
    win = PLAN['budget']['exclusion_local_minutes']
    assert g.exclusion_conflict(at(2, 10), budget, win)        # 55 min crosses 03:00
    assert not g.exclusion_conflict(at(2, 0), budget, win)
    assert g.exclusion_conflict(at(3, 30), budget, win) and g.exclusion_conflict(at(4, 5), budget, win)
    assert not g.exclusion_conflict(at(4, 10), budget, win) and not g.exclusion_conflict(at(23, 50), budget, win)
    with pytest.raises(ValueError, match='exclusion'):
        r.mute_start_allowed(PLAN, at(2, 30))
    r.mute_start_allowed(PLAN, at(1, 0))


def test_single_attempts_and_cold_rules_are_declared():
    seq = {s['id']: s for s in PLAN['sequence']}
    assert seq['B']['attempts'] == 1 and seq['D']['attempts'] == 1
    assert seq['A']['cold_first_retakeable'] is False and seq['C']['cold_first_retakeable'] is False


def test_fixtures_and_keywords_are_bound_to_the_tracked_bank():
    fixtures = r.mute_fixtures(PLAN)
    assert set(fixtures) == set(PLAN['stimulus']['cycle'])
    changed = copy.deepcopy(PLAN)
    changed['stimulus']['keywords']['france'] = [['paris']]
    with pytest.raises(ValueError, match='keywords'):
        r.mute_fixtures(changed)


def test_mute_public_env_is_capability_routed_and_keeps_liveness():
    env = PLAN['public_env']
    assert 'ECHOEAR_AEC_FULL_DUPLEX' not in env and env['ECHOEAR_LIVENESS_SECONDS'] == '0.5'


# -- sequencing with fakes (R5 triggers, R6 terminal, Fable serial ring) -----------------------

class FakeBoard:
    """Console fake: send() returns a sequence number; disconnect prints the retired PERF and OK."""
    def __init__(self, live, label='mute', send_delay_s=0.0):
        self.live, self.label, self.send_delay_s = live, label, send_delay_s
        self.cursor = 0
        self.lines = []
        self.seq = 0
    def add(self, text):
        self.cursor += 1
        self.lines.append((self.cursor, 0.0, text))
        with (self.live.out/(self.label+'-serial.log')).open('a') as f:
            f.write(text+'\n')
    def lines_since(self, cursor):
        return [x for x in self.lines if x[0] > cursor]
    def send(self, text):
        self.seq += 1
        self.live.commands.append(text)
        if text == 'disconnect':
            self.live.clock.advance(self.send_delay_s)      # delay INSIDE the command path, before transmission
            self.live.on_disconnect()
        self.add(f'OK #{self.seq} {text}')
        return self.seq
    def wait_for(self, pattern, timeout, since=None):
        import re as _re
        for _, _, text in self.lines_since(since or 0):
            if _re.search(pattern, text):
                return text
        raise TimeoutError(pattern)


def retire(snapshot, tx=9000, owner=5000, rx=2000):
    """A post-disconnect stats census as rollback-01 shows it (serial lines 3658, 4652): tasks_exited=true and no
    audio-tx / peer-loop / peer-rx rows; the retired transport stats carry the workers' exit high-water."""
    snapshot['tasks'] = [t for t in snapshot['tasks'] if t['name'] not in ('audio-tx', 'peer-loop', 'peer-rx')]
    snapshot.update(tasks_exited=True, tx_stack=tx, owner_stack=owner, rx_stack=rx)
    return snapshot


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def monotonic(self):
        return self.t
    def monotonic_ns(self):
        return int(self.t*1e9)
    def sleep(self, s):
        self.t += s
    def advance(self, s):
        self.t += s


class FakeMute(r.MuteLive):
    plan_used = PLAN
    def __init__(self, tmp, monkeypatch, fail_at=None, no_trigger=None, teardown_fault=None, flood=0, send_delay_s=0.0,
                 last_disconnect=None):
        super().__init__(dict(plan_sha256=PLAN_SHA, public_env=self.plan_used['public_env']), self.plan_used, tmp)
        self.clock = FakeClock()
        monkeypatch.setattr(r, 'time', self.clock)
        self.board = FakeBoard(self, self.install_label, send_delay_s)
        self.commands = []
        self.board.add('I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+self.plan_used['image']['elf_sha256'])
        self.board.add('I (2) app: '+self.plan_used['mode']['device_capability_line'])
        for i in range(flood):
            self.board.add(f'MIC_GATE t={i} closed=0 pre=-50 post=-50')
        self.fail_at, self.no_trigger, self.teardown_fault = fail_at, no_trigger or set(), teardown_fault
        self.last_disconnect = last_disconnect or {}
        self.generation, self.n, self.retired = 0, 0, False
        (tmp/'bot.log').write_text(self.plan_used['mode']['bot_duplex_line']+'\n')
        (tmp/'sessions/session-x').mkdir(parents=True)
        self.events_path = tmp/'sessions/session-x/events.jsonl'
        fd = self.plan_used['mode']['recorder_stt_profile_full_duplex']
        self.event(kind='stt-profile', config=dict(full_duplex=fd, profile=self.plan_used['mode']['stt_profile']))
    def event(self, **row):
        row.setdefault('monotonic_ns', self.clock.monotonic_ns())
        with self.events_path.open('a') as f:
            f.write(json.dumps(row)+'\n')
    def alive(self):
        pass
    def read_events(self):
        return [json.loads(l) for l in self.events_path.read_text().splitlines()]
    def display_setup(self):
        self.commands.append('display-setup')
    def last(self):
        return self.generation == self.final_generation
    @property
    def final_generation(self):
        return 2 if self.install_label == 'aec' else 4
    def on_disconnect(self):
        term = self.snapshot('terminal')
        if self.teardown_fault == self.generation:
            term['i2s_write_errors'] += 1
        self.board.add('PERF '+json.dumps(term))
        if self.last() and self.last_disconnect.get('gap'):
            self.event(kind='gap', pc_id='peer-'+str(self.generation), stream='tts-written', drop=True)
        if self.last() and self.last_disconnect.get('fatal'):
            self.event(kind='server-frame', frame='FatalErrorFrame', pc_id='peer-'+str(self.generation))
        if self.last() and self.last_disconnect.get('pipeline'):
            self.event(kind='pipeline-error', pc_id='peer-'+str(self.generation), error='output transport timed out')
        if self.generation == 1 and self.last_disconnect.get('r1_interrupt'):
            self.event(kind='server-frame', frame='InterruptionFrame', pc_id='peer-1')
        self.event(kind='session-end', pc_id='peer-'+str(self.generation))
        self.retired = True
    def command(self, text, timeout=40):
        self.commands.append(text)
        return 0, 0
    def connect_timed(self):
        self.clock.advance(1.0)
        self.generation += 1
        self.retired = False
        self.board.add(f'STATE Ready gen={self.generation} desired=1 t={self.generation*1000}')
        self.commands.append('connect')
        return 1.5
    def snapshot(self, label):
        self.n += 1
        base = snapshot_for_resources(aec=self.install_label == 'aec')
        if self.install_label == 'aec':
            base['aec']['enabled'] = True
        base.update(pc_id='peer-'+str(self.generation), generation=self.generation, device_us=self.generation*1000+self.n,
                    output_volume_db=0 if 'playback-volume 0' in self.commands else -6,
                    liveness=dict(pings=self.n*4, pongs=self.n*4, timeouts=0),
                    data_admission=dict(received=self.n*10, dispatched=self.n*10))
        base['display']['rendered'] = self.n
        if self.retired and 'post-disconnect' in label:
            retire(base)                       # firmware-shaped: session workers joined, exit high-water reported
        if self.retired and self.last() and 'post-disconnect' in label and self.last_disconnect.get('heap'):
            base['heap']['internal']['minimum_free'] = 1
        return base
    def wait_until(self, deadline):
        cap = self.idle_deadline()                       # the real cap (cumulative idle account), on the fake clock
        self.clock.t = max(self.clock.t, min(deadline, cap) if cap is not None else deadline)
    def seal_recorder(self, timeout_s=10):
        return True
    def cell(self, label, trigger_rule=None, prompt=None):
        prompt = prompt or self.next_prompt()
        self.prompts_used += 1
        self.clock.advance(1.0)
        pc = 'peer-'+str(self.generation)
        self.event(kind='aggregator-user-turn-started', pc_id=pc)
        self.event(kind='server-frame', frame='TranscriptionFrame', pc_id=pc)      # a Pipecat idle-reset frame
        self.clock.advance(1.0)
        if not trigger_rule:
            self.event(kind='tts-transport-write', non_silent=True, pc_id=pc)
        if trigger_rule:
            trigger = None
            if label not in self.no_trigger:
                kind = 'mid-reply' if label == 'R1' else 'after-gate-open'
                self.event(kind='tts-transport-write', non_silent=True, pc_id=pc)
                trigger = dict(kind=kind, fired=True, fired_s=self.clock.monotonic(), first_write_s=self.clock.monotonic()-1.0,
                               active=True)
                self.disconnect(trigger)
            return dict(label=label, status='REPORT-ONLY', prompt=prompt, trigger=trigger)
        if label == self.fail_at:
            return dict(label=label, status='FAIL', cause='accepted false turn', failures=['x'])
        return dict(label=label, status='PASS', prompt=prompt, failures=[])


def run_fake(tmp, monkeypatch, cls=None, **kw):
    live = (cls or FakeMute)(tmp, monkeypatch, **kw)
    try:
        verdict = live.smoke()
    except RuntimeError:
        verdict = None
    return live, json.loads((tmp/'gate.json').read_text()), verdict


def test_sequence_and_judge_pass_with_fakes(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch)
    assert verdict and verdict['outcome'] == 'PASS', gate['verdict']
    assert live.commands.count('disconnect') == 4 and live.commands.count('connect') == 4
    i = live.commands.index('playback-volume 0')
    # Astra 5: disconnect is sent first after a trigger; audio 0 follows it; the volume is set while disconnected.
    assert live.commands[i-2:i+2] == ['disconnect', 'audio 0', 'playback-volume 0', 'connect']
    assert all(live.commands[k+1] == 'audio 0' for k, c in enumerate(live.commands) if c == 'disconnect')
    assert all(parts_ok in ('PASS',) for parts_ok in [gate['verdict']['parts']['recorder-integrity']])
    assert all(gate['verdict']['parts']['terminal-resources-'+e] == 'PASS' for e in 'ABCD')
    assert [len(live.cells[k]) for k in ('A', 'B', 'C', 'D')] == [10, 1, 10, 1]
    assert [c['prompt'] for c in live.extra] == ['long', 'france']
    parts = gate['verdict']['parts']
    assert parts['reconnect-R1'] == parts['reconnect-R2'] == parts['reconnect-R3'] == 'PASS'
    assert all(parts['terminal-'+e] == 'PASS' for e in 'ABCD')


def test_r5_absent_trigger_denies_reconnect_credit(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, no_trigger={'R1'})
    assert verdict is None and gate['verdict']['parts']['reconnect-R1'] == 'INCOMPLETE'
    assert gate['verdict']['outcome'] == 'INCOMPLETE'


def test_r6_fault_only_at_disconnect_fails_the_gate(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, teardown_fault=2)
    assert verdict is None and gate['verdict']['parts']['terminal-B'] == 'FAIL'
    (tmp_path/'x').mkdir()
    live, gate, _ = run_fake(tmp_path/'x', monkeypatch, teardown_fault=4)      # the last generation, after its last snapshot
    assert gate['verdict']['parts']['terminal-D'] == 'FAIL'


def test_fable_judge_reads_the_persisted_log_not_the_ring(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, flood=25000)
    assert verdict and verdict['outcome'] == 'PASS'
    assert gate['verdict']['parts']['mode'] == 'PASS' and gate['verdict']['parts']['fatal-boot'] == 'PASS'


def test_a_failed_cell_stops_the_block_and_fails_the_gate(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, fail_at='A-3')
    assert verdict is None and len(live.cells['A']) == 3 and live.commands.count('disconnect') == 1
    assert gate['verdict']['outcome'] == 'FAIL' and gate['verdict']['parts']['block-A'] == 'FAIL'
    assert gate['verdict']['parts']['terminal-A'] == 'PASS'      # the failing generation is still retired and audited


def test_event_tail_reads_incrementally_and_skips_audio(tmp_path):
    f = tmp_path/'sessions/session-a/events.jsonl'
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(dict(kind='audio', monotonic_ns=1))+'\n'+json.dumps(dict(kind='x', monotonic_ns=2))+'\n{"kind": "y"')
    tail = r.EventTail(tmp_path)
    assert [e['kind'] for e in tail()] == ['x']
    with f.open('a') as h:
        h.write(', "monotonic_ns": 3}\n')
    assert [e['kind'] for e in tail()] == ['x', 'y']


def test_prepare_and_validate_mute_are_host_only(tmp_path, monkeypatch):
    monkeypatch.setattr(r, 'refuse_pre_141', lambda: None)   # decision 141 refusal is tested in test_echoear_rollback
    monkeypatch.setattr(r, 'BASE', tmp_path/'evidence')
    monkeypatch.setattr(r, 'head', lambda: 'a'*40)
    monkeypatch.setattr(r, 'image_record', lambda: dict(elf_sha256=r.NORMAL_HASH))
    monkeypatch.setattr(r, 'mute_image_record', lambda plan: dict(elf_sha256=plan['image']['elf_sha256']))
    original = r.subprocess.check_output
    monkeypatch.setattr(r.subprocess, 'check_output', lambda args, **kw: '' if args[:2] == ['git', 'status'] else original(args, **kw))
    bundle = tmp_path/'bundle'
    r.prepare_mute(bundle)
    p, plan = r.validate_mute(bundle/'declaration.json')
    assert p['status'] == 'draft-not-authorized' and p['plan_sha256'] == PLAN_SHA
    with pytest.raises(ValueError, match='USER-GO'):
        r.validate_mute(bundle/'declaration.json', r.sha(bundle/'declaration.json'), True)
    monkeypatch.setattr(r, 'mute_code_manifest', lambda: {'changed': 'x'})
    with pytest.raises(ValueError, match='Code changed'):
        r.validate_mute(bundle/'declaration.json')


def test_mute_image_record_checks_identity_mode_and_files(tmp_path):
    plan = copy.deepcopy(PLAN)
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    (bundle/'dg-esp32.elf').write_bytes(b'elf')
    manifest = dict(elf_sha256=plan['image']['elf_sha256'], aec_enabled=False, autostart=False,
                    firmware_commit=plan['image']['firmware_commit'],
                    public_config=dict(CONFIG_DG_PLAYOUT_TARGET_FRAMES=7, CONFIG_DG_PLAYOUT_MAX_TARGET_FRAMES=10),
                    sha256={'dg-esp32.elf': r.sha(bundle/'dg-esp32.elf')})
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    plan['image']['bundle'] = str(bundle)
    assert r.mute_image_record(plan)['elf_sha256'] == plan['image']['elf_sha256']
    for key, value in (('aec_enabled', True), ('autostart', True)):
        (bundle/'manifest.json').write_text(json.dumps(dict(manifest, **{key: value})))
        with pytest.raises(ValueError, match='identity'):
            r.mute_image_record(plan)


def test_mute_install_uses_the_unchanged_flash_path_with_a_scoped_no_gain_declaration(tmp_path, monkeypatch):
    import echoear_aec_bench as bench
    plan = copy.deepcopy(PLAN)
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    (bundle/'manifest.json').write_text(json.dumps(dict(elf_sha256=plan['image']['elf_sha256'], aec_enabled=False)))
    plan['image']['bundle'] = str(bundle)
    seen = []
    monkeypatch.setattr(bench, 'verify_bundle', lambda image, h, g=None: seen.append(('verify', bench.declared_boot_gain(image, h))))
    monkeypatch.setattr(bench, 'flash', lambda image, h, out, label, g=None: seen.append(('flash', label)))
    monkeypatch.setattr(bench, 'boot', lambda out, label, h: seen.append(('boot', label)) or 'board')
    original = bench.declared_boot_gain
    live = r.MuteLive(dict(plan_sha256=PLAN_SHA, public_env=PLAN['public_env']), plan, tmp_path)
    live.install_mute()
    assert seen == [('verify', None), ('flash', 'mute'), ('boot', 'mute')] and live.board == 'board'
    assert bench.declared_boot_gain is original          # the patch is scoped to the install
    (bundle/'manifest.json').write_text(json.dumps(dict(elf_sha256=plan['image']['elf_sha256'], aec_enabled=True)))
    with pytest.raises(ValueError, match='AEC-off bundle'):
        live.install_mute()


@pytest.mark.parametrize('inject,part', [('heap', 'terminal-resources-D')])
def test_astra4_fault_only_at_the_last_disconnect_fails_the_gate(tmp_path, monkeypatch, inject, part):
    """Device-side teardown evidence (terminal resources) stays authoritative under decision 117."""
    live, gate, verdict = run_fake(tmp_path, monkeypatch, last_disconnect={inject: True})
    assert verdict is None and gate['verdict']['outcome'] == 'FAIL' and gate['verdict']['parts'][part] == 'FAIL'


@pytest.mark.parametrize('inject,kind', [('gap', 'gap'), ('fatal', 'server-frame:FatalErrorFrame')])
def test_r12_bot_side_fault_after_the_last_disconnect_send_is_teardown_report_only(tmp_path, monkeypatch, inject, kind):
    """Decision 117 supersedes Astra r3 #4's bot-side half: a recorder gap / FatalErrorFrame of the last peer emitted
    during its own deliberate disconnect is teardown evidence - retained, report-only - not a gate FAIL."""
    live, gate, verdict = run_fake(tmp_path, monkeypatch, last_disconnect={inject: True})
    ri = gate['parts']['recorder-integrity']
    assert gate['verdict']['parts']['recorder-integrity'] == 'PASS' and ri['teardown_report_only']['by_kind'].get(kind) == 1
    assert ri['teardown_report_only']['errors']


def test_astra5_delay_inside_the_disconnect_command_path_denies_credit(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, send_delay_s=0.8)
    r1 = gate['verdict']['parts']['reconnect-R1']
    assert verdict is None and r1 != 'PASS'
    trig = gate['report_only_cells'][0]['trigger']
    assert trig['disconnect_lag_s'] == pytest.approx(0.8) and not trig['active']      # write is 0.8 s old at the real send


def test_terminal_send_cutoff_is_exit_associated_report_only(tmp_path):
    first = perf()
    term = bump(perf(liveness=dict(pings=40, pongs=40, timeouts=0)), 'drop_reasons.admission_budget', 0)
    term['drop_reasons']['send_cutoff'] = 5
    first['drop_reasons']['send_cutoff'] = 0
    t = g.terminal_epoch(first, term, PLAN)
    assert t['passed'] and t['exit_associated_report_only']['drop_reasons.send_cutoff'] == [0, 5]
    last = copy.deepcopy(first); last['drop_reasons']['send_cutoff'] = 1          # before the teardown it still gates
    assert not g.counter_epoch(first, last, PLAN)['passed']


def test_terminal_resources_need_the_post_disconnect_snapshot_and_its_audio_line():
    post = snapshot_for_resources()
    assert g.terminal_resources(post, PLAN)['passed']
    assert g.terminal_resources(None, PLAN)['status'] == 'INCOMPLETE'
    no_audio = dict(post); no_audio.pop('audio')
    assert g.terminal_resources(no_audio, PLAN)['status'] == 'INCOMPLETE'
    bad = copy.deepcopy(post); bad['tasks'][0]['stack_free_bytes'] = 100
    assert not g.terminal_resources(bad, PLAN)['passed']
    dead = copy.deepcopy(post); dead['display']['dead'] = True
    assert not g.terminal_resources(dead, PLAN)['passed']


def test_display_setup_replays_the_qualified_benchhold_sequence(tmp_path, monkeypatch):
    import step9a_support as sup
    live = r.MuteLive(dict(plan_sha256=PLAN_SHA, public_env=PLAN['public_env']), PLAN, tmp_path)
    seen = []
    monkeypatch.setattr(live, 'command', lambda text, timeout=40: seen.append(text) or (0, 0))
    monkeypatch.setattr(live, 'snapshot', lambda label: dict(heap=dict(psram=dict(free=4000000, largest=3000000))))
    monkeypatch.setattr(sup, 'request', lambda board, op, data, rid: seen.append(('request', op, rid)))
    monkeypatch.setattr(sup, 'terminal', lambda board, rid: dict(error='none'))
    monkeypatch.setattr(sup, 'read', lambda board, op, tag: dict(enabled=True))
    live.display_setup()
    assets = re.findall(r'"([0-9a-f]{64})"', (r.ROOT/'main/demo_assets.h').read_text())
    assert seen[0] == 'disp init' and seen[1] == 'disp arena 491520 4000000 1287760 3000000' and seen[-1] == 'disp character on'
    assert sum(1 for x in seen if isinstance(x, tuple)) == len(assets) > 0
    monkeypatch.setattr(sup, 'terminal', lambda board, rid: dict(error='missing'))
    with pytest.raises(RuntimeError, match='preload'):
        live.display_setup()


# -- Astra r4 item 2: retired-task terminal semantics and precedence ----------------------------------

def test_retired_census_gates_surviving_tasks_and_worker_exit_high_water():
    post = retire(snapshot_for_resources())
    t = g.terminal_resources(post, PLAN)
    assert t['status'] == 'PASS' and t['tasks_exited']
    assert t['checks']['stack_audio-tx']['basis'].startswith('worker exit high-water')
    assert t['checks']['stack_audio-playback']['basis'] == 'live census'
    low = retire(snapshot_for_resources(), rx=500)
    assert g.terminal_resources(low, PLAN)['status'] == 'FAIL'
    unreported = retire(snapshot_for_resources(), tx=0)
    u = g.terminal_resources(unreported, PLAN)
    assert u['status'] == 'PASS' and u['checks']['stack_audio-tx'].get('report_only')
    absent = retire(snapshot_for_resources()); absent['tasks_exited'] = False
    assert g.terminal_resources(absent, PLAN)['status'] == 'INCOMPLETE'


def test_measured_failure_beats_missing_audio_line():
    post = retire(snapshot_for_resources())
    post.pop('audio')
    assert g.terminal_resources(post, PLAN)['status'] == 'INCOMPLETE'
    post['heap']['internal']['minimum_free'] = 1
    assert g.terminal_resources(post, PLAN)['status'] == 'FAIL'
    aec = retire(snapshot_for_resources(aec=True)); aec.pop('audio')
    assert g.terminal_resources(aec, PLAN)['status'] == 'FAIL'           # an aec block in mute mode is a mode failure


def test_disconnect_err_fails_the_generation():
    first, term = perf(), perf(liveness=dict(pings=40, pongs=40, timeouts=0))
    assert g.terminal_epoch(first, term, PLAN, dict(ok=True))['passed']
    t = g.terminal_epoch(first, term, PLAN, dict(ok=False, error='ERR #9 disconnect'))
    assert t['status'] == 'FAIL' and g.gate_verdict(dict(t=t))['outcome'] == 'FAIL'


# -- decision 117 through the real mute judge -------------------------------------------------------------------

def test_r12_mute_judge_pipeline_error_after_the_final_send_is_teardown(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, last_disconnect={'pipeline': True})
    assert verdict and verdict['outcome'] == 'PASS', gate['verdict']['parts']
    assert gate['parts']['recorder-integrity']['teardown_report_only']['errors'][0]['kind'] == 'pipeline-error'


def test_r12_mute_judge_interruption_frame_of_the_retired_r1_peer_is_teardown(tmp_path, monkeypatch):
    live, gate, verdict = run_fake(tmp_path, monkeypatch, last_disconnect={'r1_interrupt': True})
    assert verdict and gate['verdict']['parts']['interrupts'] == 'PASS'
    assert gate['parts']['interrupts']['teardown_report_only']['by_kind'].get('server-frame:InterruptionFrame') == 1


def test_r12_mute_judge_without_the_principle_the_same_runs_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(g, 'retired_peers', lambda epochs, max_bracket_s: {})
    live, gate, verdict = run_fake(tmp_path, monkeypatch, last_disconnect={'pipeline': True, 'r1_interrupt': True})
    assert verdict is None and gate['verdict']['parts']['recorder-integrity'] == 'FAIL' and gate['verdict']['parts']['interrupts'] == 'FAIL'


def test_r14_a_long_reply_without_any_close_witness_is_incomplete():
    """Decision 122 (one rule for short and long replies): no GATE closed received by first write + allowance is
    INCOMPLETE, never PASS - the r11/r12 lead fallback is gone; clean samples cannot stand in for the close."""
    c = score(lines=cell_lines(close=False))
    assert c['status'] == 'INCOMPLETE' and 'gate close not witnessed within the allowance' in c['missing']
