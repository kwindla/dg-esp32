"""Display epoch/connection selectors, including the actual failed restart sequence."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import pytest
import display_session as d
if os.environ.get('DG_DISPLAY_SESSION_SOURCE'):
    spec=importlib.util.spec_from_file_location('session_under_test',os.environ['DG_DISPLAY_SESSION_SOURCE'])
    d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

ROOT=Path(__file__).resolve().parents[1]


def real_sequence():return json.loads((ROOT/'hil/fixtures/display_restart_events.json').read_text())['events']


def test_real_1499_1519_1520_1523_sequence_selects_new_bridge():
    events=real_sequence();session=d.display_session(events,422078877468684)
    sent=d.display_sent(events,session,'p4-contention-load')
    assert session['pc_id']==events[1]['pc_id'] and session['epoch']==events[1]['epoch']
    assert sent==events[2] and sent['packet']['d']['seq']==1
    assert d.display_result(events,session,sent['packet']['d'])==events[3]
    assert d.stale_bridge_sends(events,session)==[events[0]]
    # The previous id-only selector latched the old seq 92, with no result.
    old=next(e for e in events if e.get('event')=='display-sent')
    assert old['packet']['d']['seq']==92
    assert d.display_result(events,session,old['packet']['d']) is None


@pytest.mark.parametrize('field,value',[('pc_id','old-pc'),('epoch',422078877468683),('id','other-request')])
def test_sent_requires_connection_epoch_and_id(field,value):
    events=real_sequence();session=d.display_session(events,events[1]['epoch'])
    poison=copy.deepcopy(events[2])
    if field=='pc_id':poison[field]=value
    else:poison['packet']['d'][field]=value
    assert d.display_sent([poison,*events],session,'p4-contention-load')==events[2]


@pytest.mark.parametrize('field,value',[('pc_id','old-pc'),('epoch',422078877468683),('seq',92)])
def test_result_requires_connection_epoch_and_sequence(field,value):
    events=real_sequence();session=d.display_session(events,events[1]['epoch'])
    poison=copy.deepcopy(events[3])
    if field=='pc_id':poison[field]=value
    else:poison['result'][field]=value
    assert d.display_result([poison,*events],session,events[2]['packet']['d'])==events[3]


def test_ready_must_match_device_epoch_and_have_connection_identity():
    events=real_sequence();assert d.display_session(events,1) is None
    with pytest.raises(ValueError,match='device display epoch'):d.display_session(events,None)
    bad=dict(events[1],pc_id='')
    with pytest.raises(ValueError,match='pc_id'):d.display_session([bad],bad['epoch'])
    old=dict(events[1],epoch=events[1]['epoch']-1,pc_id='old')
    assert d.display_session([old,*events],events[1]['epoch'])['pc_id']==events[1]['pc_id']


def test_schema_fixture_matches_original_archive_when_installed():
    path=Path.home()/'src/evidence/echoear/p4/step-13b/contention/bot/events.jsonl'
    if not path.is_file():pytest.skip('coordinator archive not installed; portable schema tests still run')
    lines=path.read_text().splitlines();fixture=json.loads((ROOT/'hil/fixtures/display_restart_events.json').read_text())
    for index,expected in zip(fixture['zero_based_indices'],fixture['events']):
        actual=json.loads(lines[index])
        for key,value in expected.items():
            if key=='packet':assert {k:actual[key]['d'][k] for k in value['d']}==value['d']
            elif key=='result':assert {k:actual[key][k] for k in value}==value
            else:assert actual[key]==value


def test_ack_cannot_be_correlated_with_a_packet_from_another_epoch():
    events=real_sequence();session=d.display_session(events,events[1]['epoch'])
    wrong=dict(events[2]['packet']['d'],epoch=events[1]['epoch']-1)
    assert d.display_result(events,session,wrong) is None
