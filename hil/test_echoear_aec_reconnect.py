"""AEC promotion smoke part 2 (reconnect check): integration through the REAL AecReconnectLive cell(),
disconnect(), connect_timed(), snapshot()/command() console protocol, EventTail, seal and judge_aec.

Only hardware boundaries are faked: the device console (a scripted serial world that answers connect,
stats and disconnect with the firmware's line formats), the bot/recorder (rows written by the world as
the real recorder writes them), the Jabra (play_wav) and arecord (a recorder object with the real health
contract). A fake monotonic clock drives every wait. No bench, serial, socket, ALSA or bot access.
"""
import copy
import json
import re
from pathlib import Path

import numpy as np
import pytest

import echoear_mute_gate as g
import echoear_rollback as r

PLAN_PATH = r.ROOT/'hil/plans/echoear-aec-reconnect-v1.json'
PLAN = json.loads(PLAN_PATH.read_text())


class Clock:
    def __init__(self):
        self.t = 5000.0
    def monotonic(self):
        return self.t
    def monotonic_ns(self):
        return int(self.t*1e9)
    def sleep(self, s):
        self.t += max(s, 0.001)


class World:
    """The device console plus the bot recorder, as one scripted world."""
    def __init__(self, live, clock, faults):
        self.live, self.clock, self.faults = live, clock, faults
        self.cursor, self.lines, self.seq = 0, [], 0
        self.gen, self.pc, self.n = 0, None, 0
        self.prompts = []
        self.pending = []           # recorder rows not yet "happened" (the real recorder writes as time passes)
        self.log = live.out/'aec-serial.log'
        self.add('I (1) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+PLAN['image']['elf_sha256'])
        self.add('I (2) echoear-audio: UNQUALIFIED TDM MIC3_GAIN=0x15 analog_db=15 RX=stereo32')
        (live.out/'bot.log').write_text('')
    # -- serial --
    def add(self, text):
        self.cursor += 1
        self.lines.append((self.cursor, self.clock.t, text))
        with self.log.open('a') as f:
            f.write(text+'\n')
    def lines_since(self, cursor):
        return [x for x in self.lines if x[0] > cursor]
    def wait_for(self, pattern, timeout, since=None):
        for _, _, text in self.lines_since(since or 0):
            if re.search(pattern, text):
                return text
        raise TimeoutError(pattern)
    def send(self, text):
        self.seq += 1
        if text == 'connect':
            self.connect()
        elif text == 'stats':
            self.add('I (3) audio: AUDIO drops=0 read_errors=0 write_errors=0 clips=0 transitions=0 capture_stack=4000 playback_stack=5000 write_timeouts=0')
            self.add('PERF '+json.dumps(self.perf(post=self.pc is None)))
        elif text == 'disconnect':
            if self.faults.get('send_delay_s'):
                self.clock.t += self.faults['send_delay_s']
            self.disconnect()
        self.add(f'OK #{self.seq} {text}')
        return self.seq
    @property
    def cursor_value(self):
        return self.cursor
    # -- device state --
    def perf(self, post=False):
        self.n += 1
        tasks = [dict(name=n, stack_bytes=s, stack_free_bytes=f) for n, s, f in (
            ('audio-playback', 17152, 6000), ('audio-capture', 16384, 9000), ('audio-tx', 26624, 9000),
            ('peer-loop', 12800, 5000), ('peer-rx', 4096, 2000), ('app-control', 16384, 8000))]
        tasks += [dict(name='IDLE0', window_us=6000000, stack_bytes=0, stack_free_bytes=0),
                  dict(name='IDLE1', window_us=6000000, stack_bytes=0, stack_free_bytes=0)]
        rejected = {k: 0 for k in ('capacity', 'conflict', 'duplicate', 'duration', 'malformed', 'payload_type',
                                   'preconnect', 'source', 'window', 'late', 'generation')}
        p = dict(pc_id=self.last_pc if post else self.pc, generation=self.gen, device_us=int(self.clock.t*1e6), lost=0, received=50*self.n,
                 plc_frames=0, late=0, concealed=0, i2s_underflows=0, i2s_write_errors=0, i2s_write_timeouts=0, pcm_queue_drops=0,
                 failed_allocations=dict(total=0), console_output=dict(full=0, transport_drops=0), app_data_drops=0, queue_dropped=0,
                 playout_dropped=0, send_budget_cutoffs=0, playout=dict(post_plc_zeros=0, starvation=0, target_ms=140, ready_high_water=8, rejected=rejected),
                 playout_target_ms=140, target_depth=7, prefills=self.gen, output_volume_db=-6,
                 cancellation=dict(events=0, duplicate_notices=0),
                 display=dict(dma_errors=0, send_failures=0, gate_timeouts=0, display_gate_timeouts=0, lock_failures=0,
                              lock_timeouts=0, rendered=10*self.n, failed=False, dead=False),
                 drop_reasons=dict(admission_budget=0, admission_overflow=0, message_queue=0, send_cutoff=0),
                 data_admission=dict(received=10*self.n, dispatched=10*self.n),
                 liveness=dict(pings=4*self.n, pongs=4*self.n, timeouts=0), tasks=tasks,
                 heap=dict(internal=dict(minimum_free=90000, largest=40000), psram=dict(free=4000000, largest=3000000)),
                 encode_p95_us=7000, encode_max_us=9000, encode_frames=100, per_core_idle_percent=[60, 40], uplink_frame_ms=20,
                 aec=dict(enabled=True, errors=0, reference_clips=0, output_clips=0,
                          capture=dict(read_errors=0, read_timeouts=0, adc_clips=0, gain_clips=0)))
        if post:
            # Firmware-shaped retired census (rollback-01 lines 3658/4652): session workers joined and absent,
            # tasks_exited=true, their exit high-water in the retired transport stats.
            p['tasks'] = [t for t in p['tasks'] if t['name'] not in ('audio-tx', 'peer-loop', 'peer-rx')]
            p.update(tasks_exited=True, tx_stack=self.faults.get('tx_exit', 9000), owner_stack=5000, rx_stack=2000)
        if self.faults.get('teardown_i2s') and post and self.gen == self.faults['teardown_i2s']:
            p['i2s_write_errors'] = 1
        return p
    def event(self, pc, **row):
        row.setdefault('monotonic_ns', int(self.clock.t*1e9))
        if row['monotonic_ns'] > int(self.clock.t*1e9):
            self.pending.append((pc, row)); return
        d = self.live.out/f'sessions/session-{pc}'
        d.mkdir(parents=True, exist_ok=True)
        with (d/'events.jsonl').open('a') as f:
            f.write(json.dumps(dict(row, pc_id=pc))+'\n')
    def connect(self):
        self.clock.t += 2.0          # ICE/DTLS/data channel take real time before Ready
        self.gen += 1
        self.pc = f'SmallWebRTCConnection#0-{self.gen:04d}'
        with (self.live.out/'bot.log').open('a') as f:
            f.write(PLAN['mode']['bot_duplex_line']+'\n')
        self.event(self.pc, kind='session-start')
        self.event(self.pc, kind='stt-profile', config=dict(full_duplex=True, profile=PLAN['mode']['stt_profile']))
        if self.faults.get('stale') and self.gen == 2:
            self.event(self.pc, kind='tts-transport-write', non_silent=True, context_id='old', drop=False)
        self.add(f'I (4) dg-app: {PLAN["mode"]["device_capability_line"]}')
        self.add(f'I (5) dg-app: STATE Ready gen={self.gen} desired=1 t={int(self.clock.t*1e6)}')
        if self.faults.get('alarm') == 'live' and self.gen == 2:
            self.watchdog_reap(self.pc, 0.3)
    def flush(self):
        now = int(self.clock.t*1e9)
        due = [x for x in self.pending if x[1]['monotonic_ns'] <= now]
        self.pending = [x for x in self.pending if x[1]['monotonic_ns'] > now]
        for pc, row in sorted(due, key=lambda x: x[1]['monotonic_ns']):
            self.event(pc, **row)
    def bot_line(self, offset_s, text):
        import datetime
        stamp = datetime.datetime.fromtimestamp(r.wall_clock()+offset_s).strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
        with (self.live.out/'bot.log').open('a') as f:
            f.write(stamp+' '+text+'\n')
    def watchdog_reap(self, pc, offset_s):
        # demo_liveness.tick's tagged alarm (decision 113), then handle_disconnected's discard for that same peer
        self.bot_line(offset_s, '| ERROR    | demo_liveness:tick:58 - LIVENESS alarm: no timely pong for 2.0 s; closing peer pc_id='+pc
                      +' silent_s=%.3f' % self.faults.get('silent_s', 2.0))
        self.bot_line(offset_s+0.001, '| INFO     | pipecat.transports.smallwebrtc.request_handler:handle_disconnected:212 - '
                      'Discarding peer connection for pc_id: '+pc)
    def disconnect(self):
        if self.faults.get('alarm') == 'stale' and self.gen == 1:
            self.watchdog_reap(self.pc, 1.95)
        if self.faults.get('alarm') == 'delayed' and self.gen == 1:
            # Astra r10 shape at R1: last timely pong 3.0 s before the send, callback delayed to send + 1.5 s
            self.watchdog_reap(self.pc, 1.5)
        self.flush()
        if not (self.faults.get('zombie') and self.gen == 1):
            self.pending = [x for x in self.pending if x[0] != self.pc]      # a cut reply's future never happens
        # zombie (smoke-03 shape, decision 117): the bot keeps "successfully" writing the cut reply to dead peer A,
        # and its assistant turn lands inside B's cell window
        if self.faults.get('teardown_error') and self.gen == 2:
            self.event(self.pc, kind='pipeline-error', error='SmallWebRTCOutputTransport timed out after 10.0s writing audio')
        for p in self.prompts:
            p['end'] = min(p['end'], self.clock.t)
        self.add('PERF '+json.dumps(self.perf(post=True) if False else dict(self.perf(), pc_id=self.pc)))
        if self.faults.get('teardown_counter') and self.gen == self.faults['teardown_counter']:
            last = json.loads(self.lines[-1][2][5:]); last['i2s_underflows'] = 1
            self.lines[-1] = (self.lines[-1][0], self.lines[-1][1], 'PERF '+json.dumps(last))
        self.event(self.pc, kind='session-end')
        self.last_pc, self.pc = self.pc, None
    # -- the bot answering a spoken prompt --
    def prompt(self, file):
        t0 = self.clock.t
        prompt = Path(file).stem
        text = {'france': 'What is the capital of France?', 'long': 'Please count from one to twenty slowly.',
                'sky': 'What color is the sky on a clear day?'}.get(prompt, 'What is two plus three?')
        pc, ctx = self.pc, f'ctx-{self.gen}-{len(self.prompts)}'
        duration = 8.0 if prompt == 'long' else 2.5
        self.prompts.append(dict(t0=t0, prompt=prompt, start=t0+1.3, end=t0+1.3+duration))
        def at(dt, **row):
            self.event(pc, monotonic_ns=int((t0+dt)*1e9), **row)
        at(0.3, kind='aggregator-user-turn-started')
        if self.faults.get('live_error') and self.gen == 2:
            at(1.6, kind='pipeline-error', error='live peer pipeline error inside the cell')
        at(0.8, kind='aggregator-user-turn-stopped', text=text)
        at(1.2, kind='server-frame', frame='TTSStartedFrame', boundary='tts-written', context_id=ctx)
        for k in np.arange(1.3, 1.3+duration, 0.04):
            at(float(k), kind='tts-transport-write', non_silent=True, drop=bool(self.faults.get('drop')), context_id=ctx)
        at(1.3+duration+0.05, kind='server-frame', frame='TTSStoppedFrame', boundary='tts-written', context_id=ctx)
        at(1.3+duration+0.1, kind='server-frame', frame='BotStoppedSpeakingFrame', boundary='tts-written')
        at(1.3+duration+0.2, kind='aggregator-assistant-turn-stopped', text='Paris.')
        self.clock.t += 1.9         # the fixture's playback time
        return dict(duration_valid=True, elapsed_seconds=1.9, expected_seconds=1.9)


class Recorder:
    """arecord stand-in with the real result contract: samples carry the room floor plus the reply tone."""
    def __init__(self, world, path, device, owner):
        self.world, self.path = world, Path(path)
        owner.append(self)
        self.start = world.clock.t
        self.stopped = False
    def alive(self):
        return not self.stopped
    def stop(self):
        self.stopped, self.stop_s = True, self.world.clock.t
        return dict(healthy=True)
    def result(self):
        rate = 16000
        n = int((self.stop_s-self.start)*rate)
        t = self.start+np.arange(n)/rate
        s = np.random.default_rng(2).normal(0, 30, n)
        for p in self.world.prompts:
            on = (t >= p['start']) & (t <= p['end'])
            s[on] += 3000*np.sin(2*np.pi*1000*t[on])
        return dict(samples=s.astype('<i2'), rate=rate, start_interval_s=[self.start-0.05, self.start], healthy=True,
                    health=dict(healthy=True))


class WorldAec(r.AecReconnectLive):
    def __init__(self, tmp, monkeypatch, **faults):
        decl = dict(plan_sha256='plan', public_env=PLAN['public_env'], fixtures=r.mute_fixtures(PLAN), bot_commit='x')
        super().__init__(decl, PLAN, tmp)
        self.clock = Clock()
        monkeypatch.setattr(r, 'time', self.clock)
        monkeypatch.setattr(r, 'wall_clock', lambda: 1.79e9+self.clock.t)     # loguru's wall clock, on the fake timeline
        self.board = World(self, self.clock, faults)
        world = self.board
        import alsa
        monkeypatch.setattr(alsa, 'play_wav', lambda file, device: world.prompt(file))
        monkeypatch.setattr(r, 'RawRecording', lambda path, device, owner: Recorder(world, path, device, owner))
        self.jabra = 'hw:fake'
        tail = self.read_events      # the real EventTail; rows appear as the fake clock passes them
        self.read_events = lambda: (world.flush(), tail())[1]
    def alive(self):
        pass        # the owned bot process is a hardware boundary here



def run_world(tmp, monkeypatch, **faults):
    live = WorldAec(tmp, monkeypatch, **faults)
    try:
        verdict = live.smoke()
    except RuntimeError:
        verdict = None
    return live, json.loads((tmp/'gate.json').read_text()), verdict


def test_real_reconnect_path_passes_end_to_end(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch)
    assert verdict and verdict['outcome'] == 'PASS', {k: v for k, v in gate['verdict']['parts'].items() if v != 'PASS'}
    parts = gate['parts']
    trig = parts['reconnect-R1']['trigger']
    assert trig['kind'] == 'mid-reply' and trig['active'] and trig['active_at_send'] and trig['disconnect_lag_s'] < 0.5
    assert parts['reconnect-R1']['new_generation'] and parts['reconnect-R1']['new_peer'] and parts['reconnect-R1']['stale_writes'] == 0
    assert parts['terminal-A']['passed'] and parts['terminal-B']['passed'] and parts['terminal-resources-B']['passed']
    assert gate['cells']['A'][0]['mode'] == 'aec' and gate['cells']['B'][0]['status'] == 'PASS'
    sends = [t for _, _, t in live.board.lines if t.startswith('OK #')]
    k = next(i for i, t in enumerate(sends) if t.endswith(' disconnect'))
    assert sends[k+1].endswith(' stats') and sends[k+2].endswith(' audio 0')     # disconnect first, then terminal stats, then audio 0


def test_real_path_stale_write_on_the_new_peer_fails(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, stale=True)
    assert verdict is None and gate['verdict']['parts']['reconnect-R1'] == 'FAIL'


@pytest.mark.parametrize('fault,part', [(dict(teardown_counter=1), 'terminal-A'), (dict(teardown_counter=2), 'terminal-B'),
                                        (dict(teardown_i2s=2), 'terminal-resources-B')])
def test_real_path_teardown_faults_fail_their_generation(tmp_path, monkeypatch, fault, part):
    live, gate, verdict = run_world(tmp_path, monkeypatch, **fault)
    assert verdict is None and gate['verdict']['parts'][part] == 'FAIL'
    other = 'terminal-B' if part.endswith('A') else 'terminal-A'
    assert gate['verdict']['parts'][other] == 'PASS'                     # each generation against its own boundaries


def test_real_path_delayed_disconnect_send_denies_credit(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, send_delay_s=0.8)
    trig = gate['parts']['reconnect-R1']['trigger']
    assert verdict is None and gate['verdict']['parts']['reconnect-R1'] != 'PASS' and trig['disconnect_lag_s'] >= 0.8


def test_real_path_dropped_write_fails_the_reply(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, drop=True)
    assert verdict is None and gate['verdict']['parts']['reply-A'] == 'FAIL'


def test_acoustic_consequences_are_report_only_in_aec_mode():
    import test_echoear_mute_gate as m
    plan = copy.deepcopy(PLAN)
    rows = m.cell_rows()+[dict(kind='server-frame', frame='InterruptionFrame', monotonic_ns=m.ns(104.5), pc_id='peer'),
                          dict(kind='aggregator-assistant-turn-stopped', text='More.', monotonic_ns=m.ns(106.3), pc_id='peer')]
    c = g.score_echo_cell(rows, [], m.recording(), plan, 'france', 101.0, 103.0, 109.0)
    assert c['status'] == 'PASS' and c['mode'] == 'aec' and c['gate'] is None
    assert 'accepted false turn or interruption in the reply/tail window' in c['acoustic_report_only']
    dropped = m.cell_rows(); dropped[4]['drop'] = True
    assert g.score_echo_cell(dropped, [], m.recording(), plan, 'france', 101.0, 103.0, 109.0)['status'] == 'FAIL'


def test_aec_mode_check_and_resources_gate_the_aec_block():
    snaps = [dict(aec=dict(enabled=True))]
    device = ['x '+PLAN['mode']['device_capability_line']]
    rows = [dict(kind='stt-profile', config=dict(full_duplex=True, profile=PLAN['mode']['stt_profile']))]
    assert g.mode_check(PLAN['mode']['bot_duplex_line'], device, rows, snaps, PLAN)['passed']
    assert not g.mode_check(PLAN['mode']['bot_duplex_line'], device, rows, [dict()], PLAN)['passed']
    assert not g.mode_check('DEMO_DUPLEX aec_full_duplex=False', device, rows, snaps, PLAN)['passed']


def test_reconnect_plan_is_the_mute_machinery_with_aec_settings():
    mute = json.loads((r.ROOT/'hil/plans/echoear-mute-gate-v1.json').read_text())
    assert PLAN['reconnect']['triggers'] == {'R1': 'mid-reply'} and PLAN['reconnect']['max_disconnect_lag_s'] == 0.5
    assert PLAN['terminal'] == mute['terminal'] and PLAN['echo_cell']['playback_witness'] == mute['echo_cell']['playback_witness']
    assert not any(k.startswith('cancellation.') for k in PLAN['counters']['zero_delta_per_epoch'])
    assert 'ECHOEAR_AEC_FULL_DUPLEX' not in PLAN['public_env'] and PLAN['public_env']['ECHOEAR_STT_PROFILE'] == 'dev'


def test_real_path_disconnect_err_fails_the_generation(tmp_path, monkeypatch):
    live = WorldAec(tmp_path, monkeypatch)
    world = live.board
    original = world.send
    def send(text):
        if text == 'disconnect' and world.gen == 1:
            world.seq += 1
            world.disconnect()
            world.add(f'ERR #{world.seq} disconnect')
            return world.seq
        return original(text)
    world.send = send
    try:
        live.smoke()
    except RuntimeError:
        pass
    gate = json.loads((tmp_path/'gate.json').read_text())
    assert gate['verdict']['parts']['terminal-A'] == 'FAIL' and gate['verdict']['outcome'] == 'FAIL'


def test_real_path_retired_census_without_session_workers_passes(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch)
    for e in 'AB':
        t = gate['parts']['terminal-resources-'+e]
        assert t['status'] == 'PASS' and t['tasks_exited'] and t['checks']['stack_peer-rx']['basis'].startswith('worker exit')


def test_real_path_low_worker_exit_high_water_fails(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, tx_exit=100)
    assert verdict is None and gate['verdict']['parts']['terminal-resources-A'] == 'FAIL'


def test_r7_real_path_stale_peer_alarm_after_the_deliberate_disconnect_passes(tmp_path, monkeypatch):
    """Decision 112 through the real judge_aec: smoke-01's pattern (A reaped ~2 s after R1) is expected."""
    live, gate, verdict = run_world(tmp_path, monkeypatch, alarm='stale')
    ll = gate['parts']['liveness-logs']
    assert verdict and verdict['outcome'] == 'PASS', gate['verdict']['parts']
    assert ll['passed'] and ll['expected_alarms'] == 1 and ll['alarms'][0]['epoch'] == 'A'
    assert gate['epochs'][0]['disconnect']['sent_wall'] >= gate['epochs'][0]['disconnect']['send_lower_wall']


def test_r7_real_path_alarm_on_the_live_new_peer_fails(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, alarm='live')
    ll = gate['parts']['liveness-logs']
    assert verdict is None and gate['verdict']['parts']['liveness-logs'] == 'FAIL'
    assert ll['alarms'][0]['reason'] == 'alarm on the live peer' and ll['alarms'][0]['epoch'] == 'B'


def test_r11_real_path_delayed_watchdog_callback_owed_while_live_fails(tmp_path, monkeypatch):
    """Decision 115 through the real judge_aec: emitted inside R1's window, but silent_s 4.5 puts the last timely
    pong 3.0 s before the send - a failure owed while A was live."""
    live, gate, verdict = run_world(tmp_path, monkeypatch, alarm='delayed', silent_s=4.5)
    ll = gate['parts']['liveness-logs']
    assert verdict is None and gate['verdict']['parts']['liveness-logs'] == 'FAIL'
    assert ll['alarms'][0]['epoch'] == 'A' and 'owed while live' in ll['alarms'][0]['reason']


def test_r11_real_path_healthy_stale_peer_silence_passes(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, alarm='stale', silent_s=2.433)
    assert verdict and verdict['outcome'] == 'PASS' and gate['parts']['liveness-logs']['alarms'][0]['silent_s'] == 2.433


# -- decision 117: post-disconnect teardown rows of a deliberately disconnected peer ----------------------------

def test_r12_real_path_zombie_writes_to_dead_peer_a_inside_b_cell_pass_as_teardown(tmp_path, monkeypatch):
    """smoke-03 shape (1): after R1 the bot keeps writing to dead A; A's rows land in B's cell window."""
    live, gate, verdict = run_world(tmp_path, monkeypatch, zombie=True)
    cell = gate['cells']['B'][0]
    assert verdict and verdict['outcome'] == 'PASS', gate['verdict']['parts']
    assert cell['status'] == 'PASS' and cell['teardown_report_only']['rows'] > 0
    assert cell['teardown_report_only']['peers'] == [gate['epochs'][0]['first']['pc_id']]


def test_r12_real_path_pipeline_error_after_b_final_send_is_teardown(tmp_path, monkeypatch):
    """smoke-03 shape (2): the output transport times out after B's own deliberate final disconnect."""
    live, gate, verdict = run_world(tmp_path, monkeypatch, teardown_error=True)
    ri = gate['parts']['recorder-integrity']
    assert verdict and gate['verdict']['parts']['recorder-integrity'] == 'PASS'
    assert ri['teardown_report_only']['errors'][0]['kind'] == 'pipeline-error'


def test_r12_real_path_pipeline_error_on_the_live_peer_during_its_cell_fails(tmp_path, monkeypatch):
    live, gate, verdict = run_world(tmp_path, monkeypatch, live_error=True)
    assert verdict is None and gate['verdict']['parts']['recorder-integrity'] == 'FAIL'
    assert gate['verdict']['parts']['reply-B'] == 'FAIL' and 'fatal frame' in gate['cells']['B'][0]['cause']


def test_r12_real_path_without_the_principle_the_zombie_shape_fails(tmp_path, monkeypatch):
    """Control: with retirement disabled the same zombie run fails reply-B on 'more than one peer' (smoke-03 of record)."""
    monkeypatch.setattr(g, 'retired_peers', lambda epochs, max_bracket_s: {})
    live, gate, verdict = run_world(tmp_path, monkeypatch, zombie=True)
    assert verdict is None and gate['cells']['B'][0]['cause'] == 'more than one peer'


# -- decision 120: retirement starts at the acknowledged disconnect (OK receipt), bracket capped ----------------

def delayed_final_send(tmp_path, monkeypatch, fault_after_lower_s, wire_delay_s, ack_delay_s=0.0):
    """Astra r12 probe: at B's final disconnect the fake I/O boundary delays the actual wire write; a B
    pipeline-error is emitted fault_after_lower_s after the runner's lower stamp, while B is still connected."""
    live = WorldAec(tmp_path, monkeypatch)
    world = live.board
    original, original_wait = world.send, world.wait_for
    def send(text):
        if text == 'disconnect' and world.gen == 2:
            lower = world.clock.t
            world.clock.t = lower+fault_after_lower_s
            world.event(world.pc, kind='pipeline-error', error='live fault before the wire send')
            world.clock.t = lower+wire_delay_s
        return original(text)
    def wait_for(pattern, timeout, since=None):
        if 'disconnect' in str(pattern) or 'OK|ERR' in str(pattern):
            world.clock.t += ack_delay_s
        return original_wait(pattern, timeout, since)
    world.send, world.wait_for = send, wait_for
    try:
        verdict = live.smoke()
    except RuntimeError:
        verdict = None
    return live, json.loads((tmp_path/'gate.json').read_text()), verdict


def test_r13_astra_delayed_wire_live_fault_between_lower_and_upper_fails(tmp_path, monkeypatch):
    live, gate, verdict = delayed_final_send(tmp_path, monkeypatch, fault_after_lower_s=1.0, wire_delay_s=2.0)
    d = gate['epochs'][-1]['disconnect']
    assert d['done_s']-d['send_lower_s'] >= 2.0                                     # bracket wider than the cap
    assert verdict is None and gate['verdict']['parts']['recorder-integrity'] == 'FAIL'
    assert gate['verdict']['parts']['teardown-bracket'] == 'INCOMPLETE'              # the cascade's cause, named
    assert [b['credited'] for b in gate['parts']['teardown-bracket']['brackets']] == [True, False]


def test_r13_fault_inside_a_narrow_bracket_before_the_ok_receipt_is_live(tmp_path, monkeypatch):
    live, gate, verdict = delayed_final_send(tmp_path, monkeypatch, fault_after_lower_s=0.05, wire_delay_s=0.1)
    d = gate['epochs'][-1]['disconnect']
    assert d['done_s']-d['send_lower_s'] <= PLAN['teardown_bracket_max_s']
    assert verdict is None and gate['verdict']['parts']['recorder-integrity'] == 'FAIL'


def test_r13_healthy_control_and_teardown_after_the_ok_receipt_pass(tmp_path, monkeypatch):
    (tmp_path/'a').mkdir(); (tmp_path/'b').mkdir()
    live, gate, verdict = run_world(tmp_path/'a', monkeypatch)
    assert verdict and verdict['outcome'] == 'PASS' and gate['parts']['teardown-bracket']['passed']
    live, gate, verdict = run_world(tmp_path/'b', monkeypatch, teardown_error=True)
    assert verdict and gate['parts']['recorder-integrity']['teardown_report_only']['errors']


def test_r13_wide_bracket_gets_no_teardown_credit_even_after_the_ok(tmp_path, monkeypatch):
    """A slow OK (bracket > teardown_bracket_max_s) classifies the uncertain send conservatively: no credit."""
    epochs = [dict(id='B', first=dict(pc_id='p'), disconnect=dict(ok=True, send_lower_s=10.0, sent_s=10.0, done_s=10.6))]
    assert g.retired_peers(epochs, PLAN['teardown_bracket_max_s']) == {}
    epochs[0]['disconnect']['done_s'] = 10.3
    assert g.retired_peers(epochs, PLAN['teardown_bracket_max_s']) == {'p': int(10.3*1e9)}
