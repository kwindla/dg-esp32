import pytest
from step8e_analysis import session_state_audit


def capture():
    def sample(t, **perf):
        return dict(observed_monotonic_s=t,perf=dict(pc_id='old',tasks_exited=False,**perf))
    return dict(started_s=10,elapsed_s=300,initial_pc_id='old',session_survived=True,
                before=sample(9),samples=[sample(100)],after=sample(311),state_history=[])


def test_closed_retained_identity_cannot_prove_session_survival():
    result=capture();result['after']['perf']['tasks_exited']=True
    audit=session_state_audit(result)
    assert not audit['session_survived'] and audit['retired_counter_tail_unknown']
    assert audit['original_session_survived']
    assert audit['replacements_ready']==0


@pytest.mark.parametrize('line',[
    'dg-app: SESSION FAIL gen=1 reason=remote disconnected retry_ms=1000',
    'dg-app: STATE Disconnected gen=2 desired=1',
    'dg-app: STATE WifiConnecting gen=2 desired=1',
])
def test_state_failure_overrides_same_snapshot_identity(line):
    result=capture();result['state_history']=[dict(host_s=100,line=line)]
    assert not session_state_audit(result)['session_survived']


def test_setup_and_cleanup_edges_do_not_destroy_live_capture_survival():
    result=capture();result['state_history']=[
        dict(host_s=1,line='dg-app: STATE WifiConnecting gen=0'),
        dict(host_s=312,line='dg-app: STATE Disconnected gen=2')]
    audit=session_state_audit(result)
    assert audit['session_survived'] and not audit['retired_counter_tail_unknown']


def test_ready_replacement_is_not_original_session_survival():
    result=capture();result['after']['perf']['pc_id']='new'
    result['reconnections']=[dict(t=200,pc_id='new')]
    audit=session_state_audit(result)
    assert not audit['session_survived'] and audit['replacements_ready']==1
