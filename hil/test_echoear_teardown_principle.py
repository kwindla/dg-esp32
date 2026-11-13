"""Decision 117: after a peer's deliberate disconnect send, its bot-side rows are teardown evidence (report-only,
excluded from every other cell's peer set and reply checks); before the send every rule is unchanged.

Fixture: hil/fixtures/smoke03_part2_teardown, a minimal excerpt of aec-promotion-smoke-03 part 2 (recorder rows of
the B-1 cell window without rtvi/audio rows, 3 of A's 53 post-send writes plus its other post-send rows, B's
teardown pipeline-error, gate.json epoch stamps). The room recording is synthesized from the cell timing.
"""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

import echoear_mute_gate as g

HERE = Path(__file__).resolve().parent
FIX = json.loads((HERE/'fixtures/smoke03_part2_teardown/smoke03-part2.json').read_text())
PLAN = json.loads((HERE/'plans/echoear-aec-reconnect-v1.json').read_text())
EPOCHS = FIX['epochs']
PC_A, PC_B = EPOCHS[0]['first']['pc_id'], EPOCHS[1]['first']['pc_id']
CELL = FIX['cell']
MAXB = PLAN['teardown_bracket_max_s']
SEND_A = EPOCHS[0]['disconnect']['done_s']      # decision 120: retirement starts at the OK receipt
SEND_B = EPOCHS[1]['disconnect']['done_s']


def recording():
    rate, lo = 16000, CELL['start_interval_s']
    n = int((CELL['end_s']+0.2-lo[1])*rate)
    t = lo[1]+np.arange(n)/rate
    s = np.random.default_rng(3).normal(0, 30, n)
    on = (t >= CELL['first_write_s']) & (t <= CELL['bot_stopped_s'])
    s[on] += 3000*np.sin(2*np.pi*1000*t[on])
    return dict(samples=s.astype('<i2'), rate=rate, start_interval_s=lo, healthy=True, health={})


def score(rows=None, epochs=None):
    return g.score_echo_cell(FIX['cell_rows'] if rows is None else rows, [], recording(), PLAN, CELL['prompt'],
                             CELL['prompt_start_s'], CELL['prompt_end_s'], CELL['end_s'],
                             retired=g.retired_peers(EPOCHS if epochs is None else epochs, MAXB))


def all_rows():
    return FIX['cell_rows']+FIX['a_post_send_rows']+FIX['pipeline_error_rows']


def test_smoke03_b_cell_failed_of_record_and_passes_under_the_principle():
    assert FIX['of_record'] == dict(outcome='FAIL', reply_B='FAIL', reply_B_cause='more than one peer', recorder_integrity=False)
    old = g.score_echo_cell(FIX['cell_rows'], [], recording(), PLAN, CELL['prompt'], CELL['prompt_start_s'],
                            CELL['prompt_end_s'], CELL['end_s'])
    assert old['status'] == 'FAIL' and old['failures'] == ['more than one peer']
    new = score()
    assert new['status'] == 'PASS' and new['pc_ids'] == [PC_B]
    td = new['teardown_report_only']
    assert td['peers'] == [PC_A] and td['by_kind'].get('aggregator-assistant-turn-stopped') == 1      # A's "One, two, ..."


def test_smoke03_recorder_integrity_passes_with_the_teardown_error_retained():
    assert not g.recorder_integrity(all_rows())['passed']
    r = g.recorder_integrity(all_rows(), g.retired_peers(EPOCHS, MAXB))
    assert r['passed'] and r['teardown_report_only']['errors'][0]['pc_id'] == PC_B
    assert 'timed out' in FIX['pipeline_error_rows'][0]['error']


def test_the_same_rows_timestamped_before_the_send_fail():
    late = copy.deepcopy(EPOCHS); late[0]['disconnect']['send_lower_s'] = CELL['end_s']+1     # A's rows now precede A's send
    assert score(epochs=late)['failures'] == ['more than one peer']
    early_err = [dict(e, monotonic_ns=int((SEND_B-0.5)*1e9)) for e in FIX['pipeline_error_rows']]
    assert not g.recorder_integrity(FIX['cell_rows']+early_err, g.retired_peers(EPOCHS, MAXB))['passed']


def test_a_pipeline_error_on_b_before_its_final_send_fails_the_cell_and_integrity():
    err = dict(FIX['pipeline_error_rows'][0], monotonic_ns=int((CELL['first_write_s']+0.2)*1e9))
    r = score(FIX['cell_rows']+[err])
    assert r['status'] == 'FAIL' and 'fatal frame, pipeline error or recorder gap' in r['failures']
    assert not g.recorder_integrity(FIX['cell_rows']+[err], g.retired_peers(EPOCHS, MAXB))['passed']


def test_a_row_from_an_unknown_peer_fails():
    w = next(e for e in FIX['cell_rows'] if e['kind'] == 'tts-transport-write' and e.get('non_silent'))
    r = score(FIX['cell_rows']+[dict(w, pc_id='SmallWebRTCConnection#9-feed')])
    assert r['failures'] == ['more than one peer']
    err = dict(FIX['pipeline_error_rows'][0], pc_id='SmallWebRTCConnection#9-feed')
    assert not g.recorder_integrity(FIX['cell_rows']+[err], g.retired_peers(EPOCHS, MAXB))['passed']


@pytest.mark.parametrize('mutate', [lambda d: d.pop('send_lower_s'), lambda d: d.update(ok=False), lambda d: d.update(send_lower_s=None)])
def test_an_a_row_whose_send_stamp_is_missing_or_not_ok_fails(mutate):
    epochs = copy.deepcopy(EPOCHS); mutate(epochs[0]['disconnect'])
    assert PC_A not in g.retired_peers(epochs, MAXB)
    assert score(epochs=epochs)['failures'] == ['more than one peer']


def test_rows_without_a_pc_id_are_never_teardown():
    live, teardown = g.split_teardown([dict(kind='gap', monotonic_ns=int((SEND_B+1)*1e9))], g.retired_peers(EPOCHS, MAXB))
    assert live and not teardown


def test_the_boundary_is_the_ok_receipt_stamp():
    cut = int(SEND_A*1e9)
    live, teardown = g.split_teardown([dict(kind='x', pc_id=PC_A, monotonic_ns=cut-1), dict(kind='x', pc_id=PC_A, monotonic_ns=cut)],
                                      g.retired_peers(EPOCHS, MAXB))
    assert [e['monotonic_ns'] for e in live] == [cut-1] and [e['monotonic_ns'] for e in teardown] == [cut]


def test_interruption_frames_split_but_device_cancels_stay_authoritative():
    snaps = [[dict(cancellation=dict(events=0, duplicate_notices=0))]]
    retired_frame = dict(kind='server-frame', frame='InterruptionFrame', pc_id=PC_A, monotonic_ns=int((SEND_A+1)*1e9))
    assert g.interrupt_absent([retired_frame], [], snaps, g.retired_peers(EPOCHS, MAXB))['passed']
    live_frame = dict(retired_frame, pc_id=PC_B, monotonic_ns=int((SEND_A+1)*1e9))
    assert not g.interrupt_absent([live_frame], [], snaps, g.retired_peers(EPOCHS, MAXB))['passed']
    assert not g.interrupt_absent([retired_frame], ['I (1) app: AUDIO_CANCEL callback_us=1'], snaps, g.retired_peers(EPOCHS, MAXB))['passed']


def test_part2_teardown_rejudge_refuses_to_write_inside_the_run(tmp_path):
    import echoear_liveness_rejudge as rj
    run = tmp_path/'aec-promotion-smoke-03'
    with pytest.raises(SystemExit, match='inside'):
        rj.main(['--part2-teardown', str(run), str(run/'x.json')])


def test_r13_smoke03_brackets_fit_the_cap_and_teardown_rows_fall_after_the_ok_receipt():
    for e in EPOCHS:
        d = e['disconnect']
        assert 0 < d['done_s']-d['send_lower_s'] <= MAXB
    ret = g.retired_peers(EPOCHS, MAXB)
    assert ret == {e['first']['pc_id']: int(e['disconnect']['done_s']*1e9) for e in EPOCHS}
    assert all(r['monotonic_ns'] >= ret[PC_B] for r in FIX['pipeline_error_rows'])
    a_rows = [r for r in FIX['cell_rows'] if r.get('pc_id') == PC_A]
    assert a_rows and all(r['monotonic_ns'] >= ret[PC_A] for r in a_rows)       # A's rows in B's cell: after A's OK


def test_r13_a_row_between_lower_and_upper_is_live():
    d = EPOCHS[0]['disconnect']
    mid = int(((d['send_lower_s']+d['done_s'])/2)*1e9)
    err = dict(FIX['pipeline_error_rows'][0], pc_id=PC_A, monotonic_ns=mid)
    assert not g.recorder_integrity([err], g.retired_peers(EPOCHS, MAXB))['passed']


def test_fable_nit_named_teardown_bracket_leaf():
    ok = g.teardown_brackets(EPOCHS, MAXB)
    assert ok['passed'] and all(b['credited'] for b in ok['brackets'])
    wide = copy.deepcopy(EPOCHS); wide[1]['disconnect']['done_s'] = wide[1]['disconnect']['send_lower_s']+0.6
    leaf = g.teardown_brackets(wide, MAXB)
    assert leaf['status'] == 'INCOMPLETE' and [b['credited'] for b in leaf['brackets']] == [True, False]
    assert g.gate_verdict({'teardown-bracket': leaf, 'recorder-integrity': dict(passed=False)})['outcome'] == 'FAIL'
