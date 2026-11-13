from copy import deepcopy
import pytest
from step8c2_analysis import absent_intervals,guard,outage_verdict

def blocks(qio=0,dio=0):
    return [dict(arm=arm,complete=True,outages_ge_1s=value if i==0 else 0)
            for i in range(3) for arm,value in [('qio',qio),('dio',dio)]]

def test_qio_outage_rule_requires_complete_alternating_population():
    assert outage_verdict(blocks(1,0))['verdict']=='reject QIO'
    for q,d in [(0,0),(1,1),(0,1)]:assert outage_verdict(blocks(q,d))['requalify']
    assert not outage_verdict(blocks()[:-1])['complete']
    rows=blocks();rows[0]['complete']=False
    assert not outage_verdict(rows)['requalify']

def test_outage_edges_include_an_entire_empty_or_terminal_block():
    assert absent_intervals([],0,15_000_000_000,'arrival_ns')[0]['gap_ms']==15000
    rows=[dict(arrival_ns=100_000_000),dict(arrival_ns=200_000_000)]
    gaps=absent_intervals(rows,0,2_000_000_000,'arrival_ns')
    assert len(gaps)==1 and gaps[0]['endpoint'] and gaps[0]['gap_ms']==1800
    assert not absent_intervals([dict(arrival_ns=500_000_000)],0,999_999_999,'arrival_ns')

def test_materiality_is_strict_and_idle_has_opposite_direction():
    row=lambda delta:dict(A=dict(n=8),B=dict(n=8),delta_pp=delta)
    tasks={'audio-tx':row(2.0),'IDLE1':row(-5.0),'pipecat#12':row(3.0)}
    floors={name:dict(noise_floor_pp=3.0 if name=='pipecat#12' else .1) for name in tasks}
    assert guard(tasks,floors)['passed']
    tasks['audio-tx']['delta_pp']=2.001
    assert not guard(tasks,floors)['passed']
    tasks['audio-tx']['delta_pp']=-10;tasks['IDLE1']['delta_pp']=-5.001
    assert not guard(tasks,floors)['passed']

def test_guard_rejects_missing_task_or_short_coverage():
    tasks={'audio-tx':dict(A=dict(n=8),B=dict(n=7),delta_pp=-10)}
    with pytest.raises(ValueError):guard(tasks,{'audio-tx':dict(noise_floor_pp=.1)})
    with pytest.raises(ValueError):guard(tasks,{})


def test_receive_select_budget(tmp_path):
    import subprocess
    from pathlib import Path
    header=Path(__file__).resolve().parents[1]/'components/pipecat_esp_peer/src/receive_budget_policy.h'
    source=tmp_path/'policy.cpp'
    source.write_text('#include "'+str(header)+'"\n'+r'''
#include <cassert>
using namespace pipecat::esp_peer;
int main() {
    ReceiveBudget budget;
    budget.begin(true,true,100);
    assert(budget.deadline_us==0 && budget.awaiting_read);
    budget.received();
    assert(budget.deadline_us==4100 && !budget.awaiting_read);
    budget.received();assert(budget.deadline_us==4100);
    fd_set read,error,write;FD_ZERO(&read);FD_ZERO(&error);FD_ZERO(&write);
    FD_SET(3,&read);FD_SET(4,&error);FD_SET(5,&write);
    timeval original{0,20000},bounded{};timeval* timeout=&original;
    assert(bound_receive_select(0,20100,&read,nullptr,&error,timeout,bounded)==-1);
    assert(timeout==&original && original.tv_usec==20000 && FD_ISSET(3,&read));
    assert(bound_receive_select(4100,2100,&read,nullptr,&error,timeout,bounded)==-1);
    assert(timeout==&bounded && bounded.tv_sec==0 && bounded.tv_usec==2000);
    assert(FD_ISSET(3,&read) && FD_ISSET(4,&error));
    timeout=&original;
    assert(bound_receive_select(4100,20100,&read,&write,&error,timeout,bounded)==-1);
    assert(timeout==&original && FD_ISSET(3,&read) && FD_ISSET(4,&error) && FD_ISSET(5,&write));
    assert(bound_receive_select(4100,20100,&read,nullptr,&error,timeout,bounded)==0);
    assert(!FD_ISSET(3,&read) && !FD_ISSET(4,&error));
    original.tv_usec=100;timeout=&original;
    assert(bound_receive_select(4100,2100,&read,nullptr,nullptr,timeout,bounded)==-1);
    assert(timeout==&original);
    timeout=nullptr;
    assert(bound_receive_select(4100,2100,&read,nullptr,nullptr,timeout,bounded)==-1);
    assert(timeout==&bounded && bounded.tv_usec==2000);
    budget.finish();assert(!budget.deadline_us && !budget.awaiting_read);
    budget.begin(true,true,100);budget.received();
    FD_SET(3,&read);FD_SET(4,&error);timeout=&original;
    assert(bound_receive_select(budget.deadline_us,20100,&read,nullptr,&error,timeout,bounded)==0);
    assert(!FD_ISSET(3,&read) && !FD_ISSET(4,&error));
    budget.finish();
    budget.begin(false,true,100);budget.received();assert(!budget.deadline_us);
    budget.begin(true,false,100);assert(budget.deadline_us==4100 && !budget.awaiting_read);
}
''')
    executable=tmp_path/'policy'
    subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',str(source),'-o',str(executable)],check=True,capture_output=True)
    subprocess.run([str(executable)],check=True)
