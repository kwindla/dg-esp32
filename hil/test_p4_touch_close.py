"""Read-only replay of both retained run-1 originals and cited F11 update."""
import copy
from pathlib import Path
import shutil

import pytest
import p4_touch_close as close
from p4_parity import load,write_new

PLAN=Path.home()/'src/evidence/echoear/p4/touch-verify/run-1/plan.json'


def test_retained_run1_both_boards_pass_with_frozen_witness_and_restore():
    proof=close.verify(PLAN)
    assert proof['plan_sha256']=='337516a0f0dbae1a2a8ed18b257ff3055cb9a65784838b947db54fb0e63963f0'
    assert all(r['verified'] and r['delta']['double_taps']==1 and r['delta']['requests']==1 and r['delta']['injected']==0
        for r in proof['boards'].values())
    assert proof['boards']['echoear']['first_contact_taps']==2 and proof['boards']['p4xc']['first_contact_taps']==1
    text=close.document('| F11 Physical double tap | unverified | unverified | old attempts |\n',proof)
    assert '| pass | pass |' in text and str(PLAN) in text and proof['plan_sha256'] in text
    assert '45 s windows' in text and '600 s window' in text and 'remain **unverified**' in text
    assert 'first observed contact was the double tap itself (2 valid taps; no standalone single pre-check)' in text
    assert '10.06 s after WINDOW OPEN' in text and '10.06 s after GO' not in text
    assert 'pre-armed waiter' in text and 'user-ready GO step was skipped' in text and '8cb42b8' in text
    assert 'GO about 40 s' not in text and 'were defeated by coordinator relay latency' not in text
    assert 'mtimes are not hash-sealed' in text and 'poll observation time' in text
    with pytest.raises(ValueError):close.document(text,proof)


@pytest.mark.parametrize('board,expected',[
    ('echoear',(0.039947571,0.966587096,9.052360191)),
    ('p4xc',(0.110594729,0.897050326,8.033175504))])
def test_run1_timing_is_computed_from_markers_and_first_tap_sample(board,expected):
    proof=close.verify(PLAN);timing=proof['boards'][board]['timing'];run=PLAN.parent/board/'attempt-01'
    assert timing['ready_to_go_s']==pytest.approx(expected[0])
    assert timing['go_to_window_open_s']==pytest.approx(expected[1])
    assert timing['window_open_to_first_tap_observed_s']==pytest.approx(expected[2])
    assert timing['marker_mtime_ns']=={name:(run/name).stat().st_mtime_ns
        for name in ('ready-for-go.json','GO','window-opened.json')}
    opened=load(run/'window-opened.json');before=load(run/'before.json')
    import json
    samples=[json.loads(line) for line in (run/'status-timeline.jsonl').read_text().splitlines()]
    first=next(s for s in samples if s['phase']=='window' and s['status']['driver']['taps']>before['driver']['taps'])
    assert timing['first_tap_sample_index']==first['index']
    assert timing['window_open_to_first_tap_observed_s']==first['at']-opened['opened_at']
    # Formatter consumes the computed proof fields, rather than run-1 literals.
    timing.update(ready_to_go_s=1.234,go_to_window_open_s=2.345,window_open_to_first_tap_observed_s=3.456)
    text=close.document('| F11 Physical double tap | unverified | unverified | old attempts |\n',proof)
    assert '| '+board+' | 1.234 | 2.345 | 3.456 |' in text


def test_plan_hash_mismatch_is_rejected_before_any_replay(tmp_path,monkeypatch):
    changed=tmp_path/'plan.json';changed.write_bytes(PLAN.read_bytes()+b'\n')
    assert load(changed)==load(PLAN)  # Same inputs still require the pinned bytes.
    monkeypatch.setattr(close,'load',lambda *args:pytest.fail('mismatched plan was parsed'))
    with pytest.raises(ValueError,match='plan SHA-256 differs'):close.verify(changed)


@pytest.mark.parametrize('board',('echoear','p4xc'))
@pytest.mark.parametrize('change',('identity','request','serial-panic','go','restore'))
def test_replay_refuses_changed_or_incomplete_physical_witness(tmp_path,board,change):
    plan=load(PLAN);run=tmp_path/board;shutil.copytree(PLAN.parent/board/'attempt-01',run)
    row=load(run/'touch.json')
    if change=='identity':row['identity']={}
    elif change=='request':row['after']['requests']=2;write(run/'after.json',row['after'])
    elif change=='serial-panic':
        with (run/'serial.log').open('a') as stream:stream.write('Guru Meditation Error: late panic\n')
    elif change=='go':(run/'GO').unlink()
    elif change=='restore':row['restore']['clean']=False
    write(run/'touch.json',row)
    with pytest.raises((ValueError,OSError)):
        close.verify_board(PLAN,plan,board,run)


def write(path,row):
    import json
    path.write_text(json.dumps(row))


def test_archived_source_selection_cannot_be_changed():
    plan=copy.deepcopy(load(PLAN));plan['source_sha256']['p4_touch_verify.py']='f'*64
    with pytest.raises(ValueError,match='archived'):close.archived_sources(plan)
