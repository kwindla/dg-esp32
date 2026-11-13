import pytest
from quiet_nic import evaluate, wait_for_quiet


def snap(tx=0, rx=0, board=0, root=0, t=0):
    return dict(interface='wlp8s0',monotonic_s=t,tx_bytes=tx,rx_bytes=rx,
        qdiscs={'wlp8s0':[
            dict(kind='prio',handle='8e01:',bytes=root),
            dict(kind='netem',handle='8e03:',parent='8e01:3',bytes=board)],'ifb0':[dict(kind='netem',handle='8e03:',parent='8e01:3',bytes=0)]})


def test_board_flow_subtraction_and_exact_limit():
    a=snap(100,200,50,100)
    b=snap(20_010_100,400,10_050,20_010_100,300)
    r=evaluate(a,b)
    assert r['non_board_egress_bytes']==20_000_000
    assert r['valid'] is True and r['board_flow_rx_bytes']==0
    b['tx_bytes']+=1
    assert evaluate(a,b)['valid'] is False


def test_busy_arm_and_unknown_counters_cannot_pass():
    assert evaluate(snap(),snap(612_000_000,t=300))['valid'] is False
    b=snap(10,t=300);b['qdiscs']['wlp8s0']=[]
    assert evaluate(snap(),b)['valid'] is None
    assert evaluate(snap(100),snap(10,t=300))['valid'] is None


def test_nominal_five_minute_budget_keeps_actual_sampling_duration():
    r=evaluate(snap(),snap(19_000_000,t=325),seconds=300)
    assert r['elapsed_s']==325 and r['budget_seconds']==300
    assert r['budget_bytes']==20_000_000 and r['valid']


def test_preflight_scales_budget_and_waits_without_running_capture(tmp_path):
    state={'t':0,'n':0}
    def clock():return state['t']
    def sleep(n):state['t']+=n
    def sample(interface):
        state['n']+=1
        tx=[0,3_000_000,3_000_000,4_000_000][state['n']-1]
        s=snap(tx,t=state['t']);s['qdiscs']['wlp8s0']=[];return s
    r=wait_for_quiet(tmp_path/'gate.json',sampler=sample,sleeper=sleep,clock=clock)
    assert len(r['windows'])==2 and r['passed'] and r['waited_s']==60
    assert r['windows'][0]['valid'] is False
    assert r['windows'][1]['budget_bytes']==2_000_000


def test_preflight_times_out_at_ten_minutes(tmp_path):
    state={'t':0}
    def clock():return state['t']
    def sleep(n):state['t']+=n
    def sample(interface):return snap(state['t']*1_000_000,t=state['t'])
    with pytest.raises(RuntimeError,match='ten minutes'):
        wait_for_quiet(tmp_path/'gate.json',sampler=sample,sleeper=sleep,clock=clock)
    assert state['t']==600

def test_rx_budget_and_missing_rx_accounting():
    assert evaluate(snap(),snap(rx=20_000_000,t=300))['valid'] is True
    assert evaluate(snap(),snap(rx=20_000_001,t=300))['valid'] is False
    b=snap(t=300);b['qdiscs']['ifb0']=[]
    assert evaluate(snap(),b)['valid'] is None
    assert evaluate(snap(),snap(rx=2_000_001,t=30),conservative=True)['valid'] is False
