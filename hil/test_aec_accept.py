import json
import numpy as np
import pytest
from echoear_aec_accept import validate_plan, require_zero_i2s
from echoear_aec_accept_analysis import classify_opportunity, gate_at_near, summarize, wer
from echoear_aec_stimulus import active_transport_witness


def classify(**updates):
    values=dict(clean=True,delivered=True,near_valid=True,gate=False,mic3='active',future=True)
    values.update(updates)
    return classify_opportunity(**values)


def test_only_witnessed_pause_leaves_the_denominator():
    assert classify()=='eligible'
    assert classify(mic3='silent')=='no-opportunity'
    assert classify(future=False)=='unscorable-future'
    assert classify(mic3='uncertain')=='unscorable-mic3'
    assert classify(future=None)=='unscorable-future'
    assert classify(near_valid=False,mic3='silent')=='unscorable-near-onset'
    assert classify(delivered=False)=='not-delivered-no-active-tts'


def test_in_window_sentence_is_separate_even_if_words_are_lost():
    value=wer('Tell me a short story about a fox','A short story about a fox')
    assert value==pytest.approx(.25)
    row=dict(id='early',kind='sentence',volume_db=0,recognized=False,wer=value,
             credited_barge_in=False,opportunity=classify(gate=True))
    result=summarize([row])['0:sentence']
    assert result['eligible']==0
    assert result['in_window'][0]['wer']==value
    assert classify(gate=True,mic3='silent')=='in-window'
    assert classify(gate=None)=='unscorable-gate'


def test_source_gate_and_near_uncertainty_are_not_fitted_to_afe():
    blocks=np.array([[0,320,100,200,0],[320,640,200,300,0],[640,960,300,400,0]])
    serial='GATE closed t=900 source_lower_us=100 source_valid=1\nGATE open t=1000 source_lower_us=300 source_valid=1\n'
    assert gate_at_near(serial,blocks,400) is True
    assert gate_at_near(serial,blocks,800,uncertainty=0) is False
    assert gate_at_near('',blocks,400) is None


def test_current_silent_chunk_supersedes_prior_active_tts(tmp_path):
    path=tmp_path/'events.jsonl'
    def write(rows):path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    active=dict(kind='tts-transport-write',non_silent=True,monotonic_ns=1_000_000_000)
    write([active])
    assert active_transport_witness([path],0,1.05)['active']
    assert not active_transport_witness([path],0,1.2)['active']
    silent=dict(active,non_silent=False,monotonic_ns=1_040_000_000)
    write([active,silent])
    assert not active_transport_witness([path],0,1.05)['active']
    assert not active_transport_witness([path],2_000_000_000,2.05)['active']


def test_draft_and_changed_gates_fail_before_board_access():
    p=dict(status='draft',attempts_per_cell=1,cases=[dict(guard_ms=400,volume_db=0,stop_offset_s=1)])
    with pytest.raises(ValueError):validate_plan(p)
    p['status']='declared';validate_plan(p)
    p['qualification']=True
    with pytest.raises(ValueError):validate_plan(p)
    p.update(review_approval='Astra-approved',require_active_tts=True);validate_plan(p)
    p['cases'][0]['guard_ms']=300
    with pytest.raises(ValueError):validate_plan(p)


def test_hardware_counters_are_absolute_not_delta_only():
    healthy=dict(i2s_underflows=0,i2s_write_errors=0,i2s_write_timeouts=0)
    require_zero_i2s([healthy,healthy])
    for key in healthy:
        with pytest.raises(RuntimeError):require_zero_i2s([healthy,dict(healthy,**{key:1})])
    with pytest.raises(RuntimeError):require_zero_i2s([{}])


def test_render_ledger_detects_the_retained_one_block_deficit():
    from echoear_aec_accept_analysis import check_render_ledger
    def snap(c,loss=0):return dict(playout=dict(clock=dict(completed_samples=c,submitted_samples=c+640-c%320-loss)))
    assert check_render_ledger([snap(c) for c in (480,720,960,1200)])['passed']
    assert not check_render_ledger([snap(480),snap(720,320)])['passed']
    assert not check_render_ledger([])['passed']
    assert not check_render_ledger([{}])['passed']


def test_disconnect_requires_every_intersecting_frame_and_tight_bracket():
    from echoear_aec_accept_analysis import disconnect_activity
    anchors=[dict(coordinate=0,lower_s=0,upper_s=.001),dict(coordinate=50000,lower_s=.05,upper_s=.051)]
    blocks=np.array([[i*320,(i+1)*320,i*20000,(i+1)*20000,0] for i in range(4)])
    assert disconnect_activity(blocks,[True]*4,anchors,.025)['passed']
    assert not disconnect_activity(blocks,[True,False,True,True],anchors,.025)['passed']
    assert not disconnect_activity(blocks,[True]*4,[anchors[0],dict(anchors[1],upper_s=.2)],.025)['passed']
    assert not disconnect_activity(blocks,[True]*4,anchors,.2)['passed']


def test_reconnect_capture_keeps_anchoring_without_stats(tmp_path):
    import time,threading
    from echoear_aec_reconnect import capture_reconnect
    calls=[];capture_thread=threading.get_ident()
    class Board:
        def lines_since(self,cursor):return [(1,0,'PERF {"generation":1}')]
        def wait_for(self,*args,**kwargs):time.sleep(.07)
    def command(board,text,timeout):
        calls.append(text);assert text in ('disconnect','connect')
        time.sleep(.06);return 0,1
    def anchor(board):
        assert threading.get_ident()==capture_thread
        calls.append('anchor');return dict(coordinate=len(calls),lower_s=0,upper_s=0)
    def schedule(paths,start,offset,play,**kwargs):
        assert offset==1 and kwargs['require_active'] and kwargs['active_max_age_s']==.12
        time.sleep(.07);return dict(jabra=play(),skipped=False)
    def sleep(seconds):time.sleep(.02 if seconds==2.3 else seconds)
    result=capture_reconnect(Board(),command,lambda *a: calls.append('speak'),lambda:[],lambda _:calls.append('prepare'),tmp_path,
        dict(id='r1',second_reply_window_seconds=.1),anchor=anchor,start=lambda *a:anchor(None),stop=lambda *a:anchor(None),schedule=schedule,sleep=sleep)
    assert result['timer']['jabra']['elapsed_seconds']>=.06
    assert calls.count('anchor')>=6
    assert 'anchor' in calls[calls.index('disconnect')+1:calls.index('connect')]
    assert 'anchor' in calls[calls.index('connect')+1:]
    assert (tmp_path/'old-final.json').exists()


def test_reconnect_epoch_checks_use_fresh_identity_not_prefill_delta():
    from echoear_aec_reconnect import epoch_checks
    p=dict(pc_id='new',generation=3,lost=0,server_lost=0,concealed=0,i2s_underflows=0,i2s_write_errors=0,i2s_write_timeouts=0,
           data_admission=dict(received=2,dispatched=2),tasks=[dict(name='peer-rx',stack_free_bytes=1508)])
    result=epoch_checks(p,p,[p],fresh=True)
    assert result['passed'] and result['peer_rx_min_bytes']==1508
    assert not epoch_checks(dict(p,lost=1),dict(p,lost=1),[p],fresh=True)['passed']
