"""v9 scorer: false-event attribution, JSON safety and the retained-evidence dry run (no credit)."""
import json
from pathlib import Path

import numpy as np
import pytest

import echoear_aec_v9_score as s
import echoear_aec_v9_rules as rules

EVIDENCE = Path('/home/khkramer/src/evidence/echoear/aec-demo/step-9')


def ev(frame, t, **kw): return dict(frame=frame, t=t, event='frame', **kw)


def test_late_but_associated_response_is_not_a_false_event():
    # Sol re-review counterexample: near 0-0.6 s, its associated interruption at 3.0 s (> end + 2 s).
    own = [ev('UserStartedSpeakingFrame', 3.0), ev('InterruptionFrame', 3.05), ev('TranscriptionFrame', 3.4, text='stop')]
    assert s.attribute_accepted(own, 'stop', None, 0.0, 0.6) == []
    findings = [] ; scores = dict(acoustic=dict(result='miss', established=True), events=dict(result='pass'))
    assert rules.classify_attempt(findings, scores)['action'] == 'CONTINUE'      # an ordinary latency miss


def test_unsolicited_events_are_hard():
    own = [ev('InterruptionFrame', -1.0), ev('UserStartedSpeakingFrame', 0.3), ev('InterruptionFrame', 0.32),
           ev('UserStartedSpeakingFrame', 9.0), ev('TranscriptionFrame', 9.2, text='the weather today')]
    out = s.attribute_accepted(own, 'sentence', 'Please tell me the time in Paris.', 0.0, 2.0)
    assert [e['t'] for _, e in out] == [-1.0, 9.0]
    # A later turn that is the near text again is associated, not false.
    own[-1] = ev('TranscriptionFrame', 9.2, text='please tell me the time in paris')
    assert [e['t'] for _, e in s.attribute_accepted(own, 'sentence', 'Please tell me the time in Paris.', 0.0, 2.0)] == [-1.0]


def test_room_witness_explains_only_events_in_failed_windows():
    anchors = [dict(lower_s=100.0, upper_s=100.0), dict(lower_s=130.0, upper_s=130.0)]
    events = [('a', dict(t=101.0)), ('b', dict(t=115.0)), ('c', dict(t=129.0))]
    f = s.room_witness('noise.cell_quiet_windows', dict(pre=dict(passed=False), post=dict(passed=True)), events, anchors)
    assert f['explains'] == ['a']


def test_jsonable_numpy():
    value = s.jsonable(dict(a=np.float64(1.5), b=np.int64(2), c=np.bool_(True), d=np.array([1, 2]), e=float('nan')))
    assert json.dumps(value, allow_nan=False) == '{"a": 1.5, "b": 2, "c": true, "d": [1, 2], "e": null}'


def test_admission_findings_from_drain_records(tmp_path):
    drops = {k: 0 for k in rules.DROP_REASONS}
    perf = lambda r, d, o=0: dict(pc_id='pc', generation=1, data_admission=dict(received=r, dispatched=d, deferred=0),
                                  drop_reasons=dict(drops, admission_overflow=o))
    (tmp_path/'after-drain.json').write_text(json.dumps(dict(status='drain-timeout')))
    gates = [f['gate'] for f in s.admission_findings(tmp_path, perf(10, 10), perf(20, 18))]
    assert gates == ['admission.drain_timeout']
    assert [f['gate'] for f in s.admission_findings(tmp_path, perf(10, 10), perf(20, 20, 1))] == ['admission.control_loss']


@pytest.mark.skipif(not (EVIDENCE/'campaign-10').exists(), reason='retained evidence not on this host')
def test_dry_run_on_retained_witnesses_and_echo():
    out = s.dry_run(EVIDENCE/'campaign-10/qualification-01', limit=3)
    assert not out['errors'] and out['credit'] is False
    json.dumps(out, allow_nan=False)


# ---- joined scorer regressions on a retained barge capture (Astra blockers 1-3) -------------------

HOLD = EVIDENCE/'nova-live-02/qualification-01'
CELL = 'qualification-01-stop-01-a1'
needs_hold = pytest.mark.skipif(not (HOLD/CELL).exists(), reason='retained nova-live-02 not on this host')


@pytest.fixture
def retained(tmp_path):
    import copy, shutil
    plan = copy.deepcopy(json.loads((HOLD/'declaration.json').read_text()))
    plan['timing']['timer_lateness_max_s'] = .1
    folder = tmp_path/CELL; shutil.copytree(HOLD/CELL, folder)
    view = s.HoldView(HOLD, plan, retain=True)
    entry = dict(kind='barge-firm-stop', logical_id=CELL)
    return plan, folder, view, entry


def gates(value): return [f['gate'] for f in value['findings']]


@needs_hold
def test_retained_barge_baseline_and_next_reply_endpoint(retained, monkeypatch):
    plan, folder, view, entry = retained
    base = s.score_attempt(view, folder, plan, entry)
    assert base['scores']['acoustic']['result'] == 'pass' and base['record']['next_reply']['result'] == 'pass'
    original = s.context_playback
    def second(result):
        calls = []
        def patched(*a, **k):
            calls.append(1); out = original(*a, **k)
            return dict(out, **result) if len(calls) == 2 else out
        return patched
    monkeypatch.setattr(s, 'context_playback', second(dict(passed=False)))       # next reply later than 1 s
    late = s.score_attempt(view, folder, plan, entry)
    assert late['scores']['acoustic']['result'] == 'miss' and late['record']['next_reply']['result'] == 'miss'
    monkeypatch.setattr(s, 'context_playback', second(dict(passed=False, reason='Unscorable source/MIC3 match')))
    unsupported = s.score_attempt(view, folder, plan, entry)
    assert 'barge.unscorable' in gates(unsupported)
    assert rules.classify_attempt(unsupported['findings'], unsupported['scores'])['credited']['acoustic'] is None


@needs_hold
def test_retained_output_clip_becomes_miss(retained):
    plan, folder, view, entry = retained
    for name in ('after.json', 'capture-end.json'):
        v = json.loads((folder/name).read_text()); v['aec']['output_clips'] += 1; (folder/name).write_text(json.dumps(v))
    out = s.score_attempt(view, folder, plan, entry)
    assert 'clip.afe_output' in gates(out) and out['scores']['acoustic']['result'] == 'miss'


@needs_hold
def test_retained_transport_window_inside_vs_outside(retained, monkeypatch):
    plan, folder, view, entry = retained
    c = json.loads((folder/'result.json').read_text())['cases'][0]
    view.poll(0)
    window = s.barge_window(view, plan, c)
    pc = c['events'][0]['pc_id'] if c['events'][0].get('pc_id') else next(e['pc_id'] for e in c['events'] if e.get('pc_id'))
    original_poll = s.HoldView.poll
    def cut(at):
        def poll(self, keep):
            original_poll(self, keep)
            self.downlink = [e for e in self.downlink if not (at <= e['send_ms']/1000 <= at+.3)]
        return poll
    monkeypatch.setattr(s.HoldView, 'poll', cut((window[0]+window[1])/2))
    inside = s.score_attempt(view, folder, plan, entry)
    assert 'transport.downlink_barge' in gates(inside)
    assert rules.classify_attempt(inside['findings'], inside['scores'])['credited']['acoustic'] is None
    monkeypatch.setattr(s.HoldView, 'poll', cut(window[0]-2.0))
    view = s.HoldView(HOLD, plan, retain=True)              # fresh streams: the cut above removed rows
    outside = s.score_attempt(view, folder, plan, entry)
    assert 'transport.downlink_barge' not in gates(outside) and outside['scores']['acoustic']['result'] == 'pass'
    monkeypatch.setattr(s.HoldView, 'poll', original_poll)
    # SCTP T3 inside the window without clean same-peer recovery invalidates; one recovered before it does not.
    t3 = lambda t: dict(pc_id=pc, monotonic_ns=int(t*1e9), event='t3-expired')
    ok = lambda t: dict(pc_id=pc, monotonic_ns=int(t*1e9), event='receive-sack-complete', flight_size=0, sent_queue=0, outbound_queue=0)
    view.sctp = view.sctp+[t3(window[0]+.5)]
    assert 'transport.sctp_recovery' in gates(s.score_attempt(view, folder, plan, entry))
    view.sctp = [e for e in view.sctp if e.get('event') != 't3-expired' or e['monotonic_ns'] != int((window[0]+.5)*1e9)]
    view.sctp = view.sctp+[t3(window[0]-5), ok(window[0]-4)]
    assert 'transport.sctp_recovery' not in gates(s.score_attempt(view, folder, plan, entry))


@needs_hold
def test_retained_perf_transport_fault_is_unresolved_and_rf_total_has_no_effect(retained):
    plan, folder, view, entry = retained
    v = json.loads((folder/'capture-end.json').read_text()); v['lost'] += 500; (folder/'capture-end.json').write_text(json.dumps(v))
    r = json.loads((folder/'rf.json').read_text()); r['passed'] = False; (folder/'rf.json').write_text(json.dumps(r))
    out = s.score_attempt(view, folder, plan, entry)
    assert out['record']['transport_unlocalized'] and out['record']['rf']['effect']
    assert not {'transport.downlink_barge', 'rf.capture_window'} & set(gates(out))
    # The device-playout fault cannot be placed inside or outside the window: unresolved -> PAUSE-PART.
    assert 'unknown.unclassified' in gates(out)
    assert rules.classify_attempt(out['findings'], out['scores'])['action'] == 'PAUSE-PART'


@needs_hold
def test_retained_partial_capture_deadline_descriptor(retained):
    plan, folder, view, entry = retained
    c = json.loads((folder/'result.json').read_text())['cases'][0]
    (folder/'result.json').unlink(); (folder/'after.json').unlink()
    desc = {k: c[k] for k in ('id', 'kind', 'volume_db', 'fixture', 'expected_text', 'run_id', 'start_ns', 'end_ns', 'events',
                              'jabra', 'stimulus_timer', 'stop_offset_s')}
    out = s.score_attempt(view, folder, plan, entry, partial=desc)
    assert out['record']['partial'] is True and out['record']['established'] is False and 'acoustic' not in out['scores']
    late = dict(desc, events=[e for e in desc['events'] if e.get('frame') != 'InterruptionFrame'])
    eligible = s.score_attempt(view, folder, plan, entry, partial=late)
    assert eligible['record']['opportunity'] in ('eligible', 'ongoing-reply-lull')
    assert eligible['scores']['acoustic'] == dict(result='miss', established=True)
    # Astra counterexample: the same timed-out cell with MIC3 silenced is no-opportunity, never a miss.
    import numpy as np
    with np.load(folder/'capture.npz', allow_pickle=False) as d: data = {k: d[k] for k in d.files}
    pcm = data['pcm'].copy(); pcm[:, 2 if pcm.shape[1] == 5 else 1] = 0; data['pcm'] = pcm
    np.savez(folder/'capture.npz', **data)
    silent = s.score_attempt(s.HoldView(HOLD, plan, retain=True), folder, plan, entry, partial=late)
    assert silent['record']['opportunity'] == 'no-opportunity'
    assert silent['scores']['acoustic'] == dict(result='ineligible', established=True)
    # capture.cap keeps only an established MISS (registry); a timed-out ineligible exposure is re-attempted.
    assert rules.classify_attempt(silent['findings']+[dict(gate='capture.cap')], silent['scores'])['credited']['acoustic'] is None


SMOKE = EVIDENCE/'v9-smoke-01/part-2'


@pytest.mark.skipif(not (SMOKE/'burst-02/serial.log').exists(), reason='v9-smoke-01 not on this host')
def test_v9_smoke_01_burst_2_shape_is_complete_in_the_part_log():
    """Read-only replay of the shakedown: the per-burst window ended at id 37, the part log holds 0-39."""
    import echoear_aec_v9 as v9
    window = v9.server_ids((SMOKE/'burst-02/serial.log').read_text(errors='replace').splitlines(), 2)
    whole = v9.server_ids((SMOKE/'candidate-serial.log').read_text(errors='replace').splitlines(), 2)
    assert window == list(range(38)) and whole == list(range(40))


def test_fast_map_interval_equals_the_frozen_mapping():
    import random
    from echoear_aec_timing import map_interval
    rng = random.Random(7)
    for trial in range(200):
        n = rng.randint(1, 12)
        coords = sorted(rng.sample(range(0, 10**6, 1000), n))
        t = 0.0; anchors = []
        for c in coords:
            t += rng.uniform(.001, .1); anchors.append(dict(coordinate=c, lower_s=t, upper_s=t+rng.uniform(0, .02)))
        rng.shuffle(anchors)
        for value in [coords[0], coords[-1], -1, 10**6+5]+[rng.uniform(-10, 10**6) for _ in range(20)]:
            try: want = ('ok', map_interval(value, anchors))
            except ValueError as e: want = ('err', str(e))
            try: got = ('ok', s.fast_map_interval(value, anchors))
            except ValueError as e: got = ('err', str(e))
            assert got == want
    bad = [dict(coordinate=1, lower_s=2, upper_s=1)]
    with pytest.raises(ValueError, match='Invalid/duplicate'): s.fast_map_interval(1, bad)
    with pytest.raises(ValueError, match='Nonfinite clock'): s.fast_map_interval(float('nan'), bad)



def _msg(k, t): return dict(kind='rtvi-output', source='RTVIProcessor', direction='DOWNSTREAM', monotonic_ns=int(t*1e9),
                            message=dict(type='server-message', data=dict(n=k)))


def _send_rows(m, tsn, *, ack=True, fragments=None, t=None):
    import hashlib
    body = json.dumps(m['message']); h = hashlib.sha256(body.encode()).hexdigest(); t = t or m['monotonic_ns']/1e9
    rows = [dict(pc_id='pc', event='send-app-message', message_sha256=h, message_bytes=len(body), monotonic_ns=int((t+.01)*1e9))]
    if fragments:
        sizes = fragments
        for i, n in enumerate(sizes):
            flags = (2 if i == 0 else 0) | (1 if i == len(sizes)-1 else 0)
            rows.append(dict(pc_id='pc', event='send-chunk', chunk_type='DataChunk', protocol=51, tsn=tsn+i, flags=flags,
                             stream_id=1, stream_seq=5, user_data_length=n, monotonic_ns=int((t+.02+i*.001)*1e9)))
        last = tsn+len(sizes)-1
    else:
        rows.append(dict(pc_id='pc', event='send-chunk', chunk_type='DataChunk', protocol=51, tsn=tsn, flags=3,
                         user_data_sha256=h, monotonic_ns=int((t+.02)*1e9)))
        last = tsn
    if ack: rows.append(dict(pc_id='pc', event='receive-chunk', chunk_type='SackChunk', cumulative_tsn=last, monotonic_ns=int((t+.05)*1e9)))
    return rows


SMOKE91 = EVIDENCE/'v9.1-smoke-01/part-2'


def _replay_burst(smoke):
    """Replay a retained burst-01 with the v9.4 causal barrier. The retained run did not record when the host
    received the last application line, so the replay places the barrier conservatively: the last app_log
    stamp is mapped to esp_timer with the per-boot offset measured in the same log (`STATE ... t=<us>` lines),
    and the barrier is the host receipt time of the first PERF observation sampled on the device after it.
    Only observations REQUESTED after that barrier are offered to the drain (receipt-before-request order)."""
    import re as _re, tempfile
    import echoear_aec_v9 as v9
    P = EVIDENCE/smoke/'part-2'
    lines = (P/'candidate-serial.log').read_text(errors='replace').splitlines()
    receipts = v9.app_receipts(lines, 1)
    offsets = [int(m.group(1))-int(m.group(2))/1000 for l in lines for m in [_re.match(r'\s*I \((\d+)\) dg-app: STATE \w+ .*t=(\d+)', l)] if m]
    offset = sorted(offsets)[len(offsets)//2]
    apply_us = (max(ms for _, ms in receipts)-offset)*1000
    obs = json.loads((P/'burst-01/after-drain.json').read_text())['observations']
    barrier = next(o['t'] for o in obs if o['device_us'] > apply_us)
    usable = [o for o in obs if o['requested_s'] >= barrier]
    times = iter(x for o in usable for x in (o['requested_s'], o['t'])); it = iter(usable)
    def snap(board, folder, label):
        o = next(it); return dict(pc_id=o['pc_id'], generation=o['generation'], device_us=o['device_us'],
                                  data_admission=dict(received=o['received'], dispatched=o['dispatched'], deferred=o['deferred']))
    with tempfile.TemporaryDirectory() as tmp:
        v9.drain_boundary(None, Path(tmp), 'after', allowance_s=5.0, snapshot=snap, clock=lambda: next(times),
                          sleep=lambda s: None, ready=lambda o: o['requested_s'] >= barrier)
        d = json.loads((Path(tmp)/'after-drain.json').read_text())
    return [i for i, _ in receipts], d, offset


@pytest.mark.parametrize('smoke', ['v9.2-smoke-01', 'v9.1-smoke-01'])
def test_retained_burst_01_becomes_ready_under_the_causal_barrier(smoke):
    """v9.2-smoke-01 and v9.1-smoke-01 burst-01 timed out under pipeline counting; with the v9.3 predicate (all 40
    ids applied, then a sustained quiescent pair sampled after the last application) both are ready."""
    if not (EVIDENCE/smoke/'part-2/burst-01/after-drain.json').exists(): pytest.skip('retained smoke not on this host')
    ids, d, offset = _replay_burst(smoke)
    assert ids == list(range(40)) and d['status'] == 'quiescent'
    assert 1300 < offset < 1400                     # app_log ms vs esp_timer: different origins on this board
    led = [json.loads(l) for l in open(EVIDENCE/smoke/'ledger.jsonl')]
    b = [r for r in led if r['type'] == 'burst'][0]
    import echoear_aec_v9_rules as r
    result = r.burst_coverage(overflow_delta=b['overflow_delta'], deferred_delta=b['deferred_delta'], sent_ids=list(range(40)), received_ids=ids)
    assert result == ('covered' if smoke == 'v9.1-smoke-01' else 'coverage-not-achieved')



@pytest.mark.parametrize('smoke,worst', [('v9.3-smoke-01', 1.405), ('v9.2-smoke-01', .756), ('v9.1-smoke-01', .754),
                                         ('v9.4-smoke-01', .741), ('v9.8-smoke-01', 1.538)])
def test_retained_boundaries_within_the_v9_9_readiness_bound(smoke, worst):
    """v9.9: the drain allowance is a fixed declared 4.0 s; every retained smoke's ordinary boundaries lie within the
    2.0 s readiness bound (pooled worst 1.54 s, backlog boundaries 1.40 / 1.52 / 1.54 s), so each yields 4.0 s."""
    path = EVIDENCE/smoke/'ledger.jsonl'
    if not path.exists(): pytest.skip('retained smoke not on this host')
    import echoear_aec_v9 as v9, echoear_aec_v9_plan as plan_mod
    rows = [json.loads(l) for l in open(path)]
    s = v9.smoke_summary(dict(v9=plan_mod.plan_from()['v9']), rows)
    assert s['largest_last_arrival_to_quiescence_s'] == pytest.approx(worst, abs=1e-3)
    assert s['acceptance_drain_allowance_s'] == 4.0 and not s['drain_boundaries_over_readiness'] and not s['drain_unbounded']


V98_SMOKE = EVIDENCE/'v9.8-smoke-01'


@pytest.mark.skipif(not (V98_SMOKE/'ledger.jsonl').exists(), reason='v9.8-smoke-01 not on this host')
def test_v9_8_smoke_readiness_replay_under_v9_9():
    """Readiness replay (test only; the ledger's plan sha is v9.8's, so it can never launch v9.9): the retained
    v9.8-smoke-01 ledger with its part evidence, under the v9.9 rule. Lifecycle-ready, burst covered, two backlog
    boundaries 1.517 / 1.538 s within the 2.0 s bound: READY with the fixed 4.0 s allowance."""
    import echoear_aec_v9 as v9, echoear_aec_v9_plan as plan_mod
    from echoear_aec_v9_ledger import verify
    rows = [json.loads(l) for l in open(V98_SMOKE/'ledger.jsonl')]
    verify(rows)
    s = v9.smoke_summary(plan_mod.plan_from(), rows, out=V98_SMOKE)
    assert s['lifecycle_ready'] and s['burst_covered'], s['problems']
    assert sorted(round(b['latency_s'], 3) for b in s['drain_backlog_boundaries']) == [1.517, 1.538]
    assert s['ready'] and s['acceptance_drain_allowance_s'] == 4.0


ACC94 = EVIDENCE/'v9.4-acceptance-01/part-1/v9-witness-p1-01-a1'


def _acc94():
    c = json.loads((ACC94/'result.json').read_text())['cases'][0]
    raw, afe, _, _ = s.load(ACC94/'capture.npz')
    return raw, afe, c['fixture']


def _shift(x, d):
    return np.concatenate([np.zeros(d), x[:-d]])


needs_acc94 = pytest.mark.skipif(not (ACC94/'capture.npz').exists(), reason='v9.4-acceptance-01 not on this host')


@needs_acc94
def test_witness_v9_4_acceptance_nominal_1001_resolves_to_1024():
    """Decision 79: raw and AFE broke a +/- correlation-peak tie (23 samples, half the pitch period) in opposite
    polarity and read 1001 for a true 1024. v9.7 accepts the same-polarity alternative only because its own
    checks pass, it is the half-period opposite-polarity alias, and the polarity-free envelope delay is 1024."""
    raw, afe, fixture = _acc94()
    w = s.witness_offset(raw, afe, fixture)
    assert w['valid'] and w['offset'] == 1024 and w['method'] == 'alternative'
    assert w['afe_unconstrained']['at']-w['raw']['at'] == 1001
    ch = w['checks']
    assert ch['strength'] and ch['alias'] and ch['envelope_agrees'] and ch['envelope']['lag'] == 1024
    assert ch['relative_agrees'] and ch['relative']['lag'] == 1024 and ch['relative']['competitor_ratio'] < .99
    assert (w['raw']['correlation'] > 0) == (w['afe']['correlation'] > 0)
    import echoear_aec_v9_rules as r
    assert r.witnesses_ok([w['offset'], 1024])


@needs_acc94
@pytest.mark.parametrize('invert', [True, False], ids=['inverted', 'plain'])
def test_witness_astra_plus_23_shift_is_ambiguous(invert):
    """Astra v9.6 review: the retained AFE delayed by 23 more samples (true delay 1047), inverted or not. The
    unconstrained / same-polarity peaks can land on 1024; the envelope delay reads 1047, so no estimate is valid."""
    raw, afe, fixture = _acc94()
    shifted = _shift(afe, 23)*(-1 if invert else 1)
    w = s.witness_offset(raw, shifted, fixture)
    assert not w['valid'] and w['offset'] is None and w['method'] == 'ambiguous'
    assert w['checks']['envelope']['lag'] == 1047


@needs_acc94
def test_witness_sol_inverted_1047_is_ambiguous_and_controls():
    """Sol v9.6 review: afe[1047:] = -raw[:-1047]. The same-polarity peak 1024 is contradicted by the envelope
    (1047). Controls: the uninverted copy at 1024 is valid; at 1001 and 1047 it is not."""
    raw, _, fixture = _acc94()
    afe = np.zeros_like(raw); afe[1047:] = -raw[:-1047]
    w = s.witness_offset(raw, afe, fixture)
    assert not w['valid'] and w['offset'] is None and not w['checks'].get('envelope_agrees')
    for d, ok in ((1024, True), (1001, False), (1047, False)):
        x = np.zeros_like(raw); x[d:] = raw[:-d]
        w = s.witness_offset(raw, x, fixture)
        assert w['valid'] is ok and (w['offset'] == 1024 if ok else w['offset'] is None), (d, w['offset'])


def _noise_fixture(tmp_path, n=8000, seed=42):
    import wave
    t = np.random.default_rng(seed).standard_normal(n)
    pcm = np.clip(np.round(t*3000), -32768, 32767).astype('<i2')
    path = tmp_path/'fixture.wav'
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())
    _, template, _, _ = s.fixture_template(path)
    return path, template


def test_witness_sol_weak_same_polarity_replacement_rejected(tmp_path):
    """Sol v9.6 review: a dominant inverted copy at true delay 1450 plus a weak same-polarity copy at 1024."""
    path, t = _noise_fixture(tmp_path)
    raw = np.zeros(40000); raw[10000:10000+len(t)] = t
    afe = np.zeros(40000); afe[11450:11450+len(t)] -= t; afe[11024:11024+len(t)] += .13*t
    afe += np.random.default_rng(7).standard_normal(len(afe))*1e-3
    w = s.witness_offset(raw, afe, path)
    assert not w['valid'] and w['offset'] is None


def test_witness_astra_validity_inheritance_rejected(tmp_path):
    """Astra v9.6 review: negative template at AFE 5000 in 20000 samples; the positive maximum in [4360, 5641) and
    a positive raw template 1024 samples before it. True displacement is not 1024; the weak peak must not inherit
    validity."""
    path, t = _noise_fixture(tmp_path)
    afe = np.zeros(20000); afe[5000:5000+len(t)] = -t
    curve = s._signed_curve(afe, t)
    at = 4360+int(np.argmax(curve[4360:5641]))
    raw = np.zeros(20000); raw[at-1024:at-1024+len(t)] = t
    w = s.witness_offset(raw, afe, path)
    assert not w['valid'] and w['offset'] is None


def test_witness_peak_checks_reapplied_to_the_selected_lag():
    curve = np.zeros(5000); curve[1000] = .9; curve[2500] = .2; curve[4000] = .19
    assert s._peak_checks(curve, 1000)['valid']
    assert not s._peak_checks(curve, 2500)['valid']           # |c| < 0.25
    curve[4000] = .9; curve[2500] = .85
    assert not s._peak_checks(curve, 2500)['valid']           # a competing peak beyond +-640 is not 1.1x weaker


NOISY_DELAYS = (1001, 1003, 1004, 1024, 1044, 1045, 1047)


@needs_acc94
def test_witness_noisy_grid_never_accepts_a_wrong_delay():
    """Sol v9.7 finding 1, generalized: the retained raw channel copied into AFE at a known delay (both polarities;
    gate boundaries 1003/1004 and 1044/1045, the half-period aliases 1001/1047, the nominal 1024), with independent
    noise at twice the raw speech level added only during the fixture interval, rounded to int16. Seeds 1-12:
    120 cases incl. Sol's seed-8 inverted 1047 and seeds 5 and 9. No accepted offset may lie outside 1024 +- 20 or
    more than 20 samples from the true delay."""
    raw, _, fixture = _acc94()
    _, t, _, _ = s.fixture_template(fixture)
    at = s.match(raw, t)['at']; scale = 2*np.std(raw[at:at+8000])
    wrong, accepted, cases = [], 0, 0
    for seed in range(1, 13):
        for d in NOISY_DELAYS:
            for sign in (1, -1):
                if d in (1003, 1004, 1044, 1045) and sign < 0: continue
                x = np.zeros_like(raw); x[d:] = sign*raw[:-d]
                x[at+d:at+d+8000] += np.random.default_rng(seed).standard_normal(8000)*scale
                x = np.clip(np.rint(x), -32768, 32767).astype(np.int16).astype(float)
                w = s.witness_offset(raw, x, fixture); cases += 1
                if w['valid']:
                    accepted += 1
                    if abs(w['offset']-1024) > 20 or abs(w['offset']-d) > 20 or abs(d-1024) > 20: wrong.append((seed, d, sign, w['offset']))
    assert cases == 120 and wrong == [], wrong
    assert accepted > 0          # the estimator still accepts some correct noisy witnesses


RETRO_34 = [
    ('aec-arms-04/visit-01-A', 'visit-01-A-near-01-a1', 1024),
    ('campaign-09/qualification-01', 'qualification-01-near-01-a1', 1024),
    ('campaign-09/qualification-01', 'qualification-01-near-02-a1', 1024),
    ('campaign-09/qualification-01', 'qualification-01-near-03-a1', 1024),
    ('campaign-09/qualification-01', 'qualification-01-near-04-a1', 1024),
    ('campaign-10/qualification-01', 'qualification-01-near-01-a1', 1024),
    ('campaign-10/qualification-01', 'qualification-01-near-02-a1', 1024),
    ('campaign-10/qualification-01', 'qualification-01-near-03-a1', 1024),
    ('campaign-10/qualification-01', 'qualification-01-near-04-a1', 1024),
    ('near-normal-stop-01', 'near-group-01-a1', 1024),
    ('near-normal-stop-01', 'near-group-02-a1', None),
    ('near-normal-stop-01', 'near-group-03-a1', None),
    ('nlp-diag-01', 'aggr-near-normal-a1', None),
    ('v9-smoke-01/part-1', 'v9-witness-p1-01-a1', 1024),
    ('v9-smoke-01/part-1', 'v9-witness-p1-02-a1', 1024),
    ('v9-smoke-01/part-2', 'v9-witness-p2-01-a1', 1024),
    ('v9-smoke-01/part-2', 'v9-witness-p2-02-a1', 1024),
    ('v9.1-smoke-01/part-1', 'v9-witness-p1-01-a1', 1024),
    ('v9.1-smoke-01/part-1', 'v9-witness-p1-02-a1', 1024),
    ('v9.1-smoke-01/part-2', 'v9-witness-p2-01-a1', 1024),
    ('v9.1-smoke-01/part-2', 'v9-witness-p2-02-a1', 1024),
    ('v9.2-smoke-01/part-1', 'v9-witness-p1-01-a1', 1024),
    ('v9.2-smoke-01/part-1', 'v9-witness-p1-02-a1', 1024),
    ('v9.2-smoke-01/part-2', 'v9-witness-p2-01-a1', 1024),
    ('v9.2-smoke-01/part-2', 'v9-witness-p2-02-a1', 1024),
    ('v9.3-smoke-01/part-1', 'v9-witness-p1-01-a1', 1024),
    ('v9.3-smoke-01/part-1', 'v9-witness-p1-02-a1', 1024),
    ('v9.3-smoke-01/part-2', 'v9-witness-p2-01-a1', 1024),
    ('v9.3-smoke-01/part-2', 'v9-witness-p2-02-a1', 1024),
    ('v9.4-acceptance-01/part-1', 'v9-witness-p1-01-a1', 1024),
    ('v9.4-smoke-01/part-1', 'v9-witness-p1-01-a1', 1024),
    ('v9.4-smoke-01/part-1', 'v9-witness-p1-02-a1', 1024),
    ('v9.4-smoke-01/part-2', 'v9-witness-p2-01-a1', 1025),
    ('v9.4-smoke-01/part-2', 'v9-witness-p2-02-a1', 1024),
]


@pytest.mark.skipif(not (EVIDENCE/'v9.4-smoke-01').exists(), reason='retained witness captures not on this host')
def test_witness_retrospective_34_captures_unchanged_except_the_1001():
    """witness-offsets retrospective (decision 79): 34 near-only captures, 16 boots. v9.7 reproduces every
    earlier result (30x 1024, 1x 1025, 3 invalid) and changes only v9.4-acceptance-01's 1001 to 1024."""
    assert len(RETRO_34) == 34
    for hold, attempt, expected in RETRO_34:
        folder = EVIDENCE/hold/attempt
        c = [c for c in json.loads((folder/'result.json').read_text())['cases'] if c.get('kind') == 'near'][0]
        raw, afe, _, _ = s.load(folder/'capture.npz')
        w = s.witness_offset(raw, afe, c['fixture'])
        assert w['offset'] == expected and w['valid'] is (expected is not None), (hold, attempt, w['offset'])
        if expected is not None:          # the declared 1 % relative margin has headroom on every real capture
            assert w['checks']['relative']['lag'] == 1024 and w['checks']['relative']['competitor_ratio'] < .985
