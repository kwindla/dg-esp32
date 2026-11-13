"""Real-shaped fixtures and offline replay; no hardware, audio or bot access."""
import copy
import json
import os
from pathlib import Path

import pytest
import p4_aec_policy as policy
import p4_aec_schema as schema
from wire_contract import ContractError

REAL=Path(os.environ.get('DG_P4_AEC_RECORDS',Path.home()/'src/evidence/echoear/p4/step-15b-early'))


def perf_record(board,variant='normal'):
    """Trim schema-bearing fields from actual records; fallback is the same shape.

    Modeled counters can vary in unit tests. Field names/nesting may not.
    Raw captured PERF and provenance stay outside git in the evidence archive.
    """
    source=REAL/f'smoke-{board}-aec-{variant}'/'perf-001.json'
    if source.exists():
        row=json.loads(source.read_text())
        return {**{k:row[k] for k in ('generation','pc_id','mic_in_turn_clips','mic_gate_open_clips','mic_gate_closed_clips')},
            'aec':{**{k:row['aec'][k] for k in ('enabled','errors','reference_clips','output_clips')},
                'reference_health':dict(gain_db=row['aec']['reference_health']['gain_db'])}}
    return dict(generation=1,pc_id='modeled-peer',mic_in_turn_clips=0,mic_gate_open_clips=0,mic_gate_closed_clips=0,
        aec=dict(enabled=True,errors=0,reference_clips=0,output_clips=0,
            reference_health=dict(gain_db=0 if board=='p4xc' else 15)))


@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('variant',['normal','demo'])
def test_gain_and_clip_paths_match_real_perf_shape(board,variant):
    import numpy as np
    row=perf_record(board,variant);gain=0 if board=='p4xc' else 15
    assert 'reference_gain_db' not in row['aec']
    assert policy.reference_gain(row,row,gain)==gain
    assert policy.clip_evidence(row,row,np.zeros((8192,5)))['passed']


@pytest.mark.parametrize('change',['legacy-flat','missing','boolean','float','wrong','after-wrong','after-missing'])
def test_gain_must_be_typed_and_equal_before_and_after(change):
    before=perf_record('p4xc');after=copy.deepcopy(before)
    if change=='legacy-flat':before['aec']=dict(reference_gain_db=0)
    elif change=='missing':before['aec']['reference_health'].pop('gain_db')
    elif change in ('boolean','float','wrong'):before['aec']['reference_health']['gain_db']={'boolean':False,'float':0.,'wrong':3}[change]
    elif change=='after-wrong':after['aec']['reference_health']['gain_db']=3
    else:after['aec']['reference_health'].pop('gain_db')
    with pytest.raises(ContractError,match='reference_health.gain_db'):policy.reference_gain(before,after,0)


@pytest.mark.skipif(not (REAL/'smoke-p4xc-aec-normal/perf-001.json').exists(),reason='retained P4/S3 15b-early real PERF archive not installed; set DG_P4_AEC_RECORDS')
def test_every_real_perf_event_and_guard_key_path_replays_on_both_boards():
    report=schema.audit(REAL)
    assert report['passed'] and not report['qualification'] and not report['hardware_used']
    assert len(report['reports'])==4
    assert sum(row['perf_records'] for row in report['reports'])==24
    for row in report['reports']:
        assert all(not fields for fields in row['optional_absent_reads'].values())
        assert 'PERF.aec.reference_health.gain_db' in row['read_fields']['reference_gain']
        assert row['guard_lines']>=10 and row['nearend_trials']==1
        assert len(row['outcomes']['echo_score'])==10
