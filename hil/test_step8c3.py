"""Archived soak regressions and the actual socket-read wrapper integration."""
import copy
import json
from pathlib import Path
import subprocess
import pytest
from soak_memory import transient_memory, heap_trends, growth_verdict, host_uplink_gap_windows

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('name,expected', [('final-soak-confirmation',None),('final-dio-soak',None),('final-soak',None)])
def test_archived_soak_verdicts(name,expected):
    fixture=json.loads((ROOT/'hil/fixtures/step8c3'/f'{name}.json').read_text())
    rows=fixture['observations'];original=copy.deepcopy(rows)
    result=transient_memory(rows,fixture['closed'],fixture['faults'])
    assert result['heap_transient_depth_passed'] is expected
    assert result['reconnect_transient_passed'] is fixture['original_checks']['reconnect_transient_budget']
    assert growth_verdict(heap_trends(rows)) is fixture['original_checks']['heap_growth_rate']
    assert rows==original
    if name=='final-soak-confirmation':
        event=next(w for w in result['network_disturbances'] if 380<w['elapsed_s']<400)
        assert 'rebuffers' in event['disturbance_reasons'] and event['plc_increment']>0
        assert 'plc_frames' not in event['disturbance_reasons']
        assert 'host_uplink_gap_ge_1s' in event['disturbance_reasons']
        assert 60000<event['legacy_cumulative_depth_bytes']['psram']<65536
        assert event['minimum_free_step_bytes']['psram']==15592
        assert event['passed'] is None

@pytest.mark.parametrize('cap,depth,expected',[('psram',65536,True),('psram',65537,False),('internal',4096,True),('internal',4097,False),('dma',4096,True),('dma',4097,False)])
def test_capability_depth_limits_use_actual_depth(cap,depth,expected):
    def row(t):
        return dict(elapsed_s=t,ready_elapsed_s=0,perf=dict(generation=1,rebuffers=0,plc_frames=0,
            heap={c:dict(free=100000,minimum_free=90000,window_minimum_free=100000) for c in ('psram','internal','dma')},
            heap_window=dict(schema=1,start_us=max(0,t-60)*1000000,end_us=t*1000000,samples=1)))
    rows=[row(0),row(300),row(360),row(420)]
    if cap!='psram':
        for r in rows:r['perf']['heap'][cap]['minimum_free']=99000
    for r in rows[2:]:
        r['perf']['heap'][cap]['minimum_free']=100000-depth
        r['perf']['heap'][cap]['window_minimum_free']=100000-depth
    result=transient_memory(rows,rows[-1]['perf'],[])
    assert result['heap_transient_depth_passed'] is expected
    if depth>10000:
        window=result['transient_generations']['1']['windows'][0]
        assert window['connect_trough_headroom_bytes'][cap]==10000
        assert window['actual_depth_bytes'][cap]==depth
        assert window['minimum_free_step_bytes'][cap]==depth-10000


def test_host_gap_classifies_without_playout_counter_changes_and_never_crosses_sessions():
    rows=[dict(elapsed_s=t,observed_monotonic_s=t,perf=dict(pc_id=pc)) for t,pc in [(0,'a'),(2,'a'),(3,'a'),(4,'b'),(6,'b')]]
    packets=[dict(pc_id=pc,ssrc=1,observed_ms=t) for t,pc in [(500,'a'),(2000,'a'),(2200,'a'),(4500,'b'),(4600,'b')]]
    annotated=host_uplink_gap_windows(rows,packets)
    assert annotated[1]['host_uplink_max_gap_ms']==1500
    assert annotated[-1]['host_uplink_max_gap_ms']==100
    assert all('host_uplink_max_gap_ms' not in r for r in rows)


def test_actual_recvfrom_wrapper_current_gating_and_stale_read(tmp_path):
    # Compile the production current() and recvfrom wrapper bodies with minimal
    # FreeRTOS/native-call mocks, so ret accounting and gating are not duplicated.
    cpp=(ROOT/'components/pipecat_esp_peer/src/peer_trace.cpp').read_text()
    current=cpp[cpp.index('static PeerTrace* current()'):cpp.index('\n}\nusing namespace pipecat::esp_peer;',cpp.index('static PeerTrace* current()'))]
    wrapper=cpp[cpp.index('ssize_t __wrap_lwip_recvfrom'):cpp.index('\nint __real_dtls_srtp_decrypt',cpp.index('ssize_t __wrap_lwip_recvfrom'))]
    # The slice must contain exactly the one intended function definition.
    assert wrapper.count('{')-wrapper.count('}')==0
    import re
    assert len(re.findall(r'(?m)^\w[\w:* ]*\([^;]*\)\s*\{',wrapper))==1
    source=tmp_path/'wrapper.cpp'
    source.write_text(f'''#include "{ROOT}/components/pipecat_esp_peer/src/receive_budget_policy.h"
#include <sys/socket.h>
#include <cassert>
#include <cerrno>
int64_t now_us=100;int64_t esp_timer_get_time() {{return now_us;}}
using pipecat::esp_peer::ReceiveBudget;
struct PeerTrace {{ enum {{Wire}}; void* owner; ReceiveBudget receive_budget; void rtp(int,void*,int) {{++calls;}} int calls=0,receive_budget_yields=0; }};
struct PeerPart {{enum {{Recv}};}};
struct PeerTimer {{explicit PeerTimer(int) {{}}}};
int core=0;void* task=reinterpret_cast<void*>(1);PeerTrace* active_peer_trace=nullptr;
int xPortGetCoreID() {{return core;}} void* xTaskGetCurrentTaskHandle() {{return task;}}
ssize_t native_result=0;int native_calls=0;
ssize_t __real_lwip_recvfrom(int,void*,size_t,int,struct sockaddr*,socklen_t*) {{++native_calls;return native_result;}}
{current}
{wrapper}
int main() {{
 PeerTrace trace;trace.owner=task;active_peer_trace=&trace;
 auto read=[]{{return __wrap_lwip_recvfrom(3,nullptr,0,0,nullptr,nullptr);}};
 trace.receive_budget.begin(true,true,100);
 native_result=-1;assert(read()==-1);assert(!trace.receive_budget.deadline_us);
 core=1;native_result=10;assert(read()==10);assert(!trace.receive_budget.deadline_us);
 core=0;task=reinterpret_cast<void*>(2);read();assert(!trace.receive_budget.deadline_us);
 task=trace.owner;active_peer_trace=nullptr;read();assert(!trace.receive_budget.deadline_us);
 active_peer_trace=&trace;native_result=0;assert(read()==0);assert(trace.receive_budget.deadline_us==4100);
 trace.receive_budget.finish();native_result=12;read();assert(!trace.receive_budget.deadline_us);
 trace.receive_budget.begin(false,true,100);read();assert(!trace.receive_budget.deadline_us);
 trace.receive_budget.begin(true,true,200);native_result=1;read();assert(trace.receive_budget.deadline_us==4200);
 assert(native_calls==8);assert(trace.calls==5);
 now_us=4200;assert(read()==-1 && errno==EWOULDBLOCK);
 assert(native_calls==8 && trace.receive_budget_yields==1);
}}
''')
    executable=tmp_path/'wrapper'
    subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',str(source),'-o',str(executable)],check=True,capture_output=True)
    subprocess.run([str(executable)],check=True)


def test_cpu_permutations_use_boots_and_preserve_pairs():
    from step8c3_analysis import cpu_statistics
    runs=[]
    for pair in range(1,7):
        for arm in ('A','B'):
            value=30+pair+(20 if arm=='B' else 0)
            tasks=[dict(name=name,cpu=cpu,core=core,priority=priority) for name,cpu,core,priority in
                   [('audio-tx',value,1,11),('pipecat#17',.1,1,5),('pipecat#18',.01,1,5)]]
            runs.append(dict(pair=pair,arm=arm,summary=dict(stimulus_complete=True,cpu_windows=[dict(tasks=tasks) for _ in range(4)])))
    result=cpu_statistics(dict(completed=True,runs=runs));task=result['tasks']['audio-tx']
    assert task['delta_pp']==20 and task['pair_signs']==['+']*6
    assert task['paired_permutation_p']==.03125 and task['paired_permutations']==64
    assert task['unpaired_permutation_p']==pytest.approx(12/924)
    assert task['boots_per_arm']==6 and task['windows_per_arm']==24
    assert not result['passed']
    with pytest.raises(ValueError,match='Incomplete CPU'):cpu_statistics(dict(completed=False))
    runs[0]['summary']['cpu_windows'].pop()
    with pytest.raises(ValueError,match='four complete windows'):cpu_statistics(dict(completed=True,runs=runs))


def test_modes_keep_ambiguous_and_switching_labels_without_borrowing_absolute_cutoffs():
    from step8c3_analysis import mode_label
    assert mode_label(26,26,'A')=='quiet'
    assert mode_label(31,20,'A')=='busy'
    assert mode_label(26,20,'A')=='ambiguous'
    assert mode_label(9,52,'B')=='quiet'
    assert mode_label(11,49,'B')=='busy'
    assert mode_label(9,49,'B')=='ambiguous'


def test_mode_statistics_reports_switching_and_keeps_complete_boot_indicators():
    from step8c3_analysis import mode_statistics
    runs=[]
    for arm in ('A','B'):
        windows=[]
        for index in range(4):
            peer,idle=((31,20) if index==0 else (26,26)) if arm=='A' else (9,52)
            windows.append(dict(tasks=[dict(name=name,cpu=value) for name,value in
                [('peer-loop',peer),('IDLE1',idle),('audio-tx',40),('audio-playback',20),('audio-capture',10)]]))
        runs.append(dict(pair=1,arm=arm,summary=dict(cpu_windows=windows)))
    result=mode_statistics(dict(runs=runs))
    assert result['boots'][0]['mode']=='switching'
    assert result['boots'][0]['window_modes']==['busy','quiet','quiet','quiet']
    assert len(result['boots'])==2 and len(result['boots'][0]['peer_loop_windows'])==4
    assert result['strata']['quiet']['boots_per_arm']=={'A':0,'B':1}
    assert result['strata']['switching']['boots_per_arm']=={'A':1,'B':0}
    assert not result['strata']['quiet']['available'] and not result['aa_tail_allowance_used']


def test_candidate_revalidation_allows_only_build_identity_metadata():
    from step8c3_qualify import metadata_only_binary_change
    old=bytes(1000);new=bytearray(old)
    for index in (48,79,112,143,176,207,967,999):new[index]=1
    assert metadata_only_binary_change(old,new)
    for index in (0,47,80,111,144,175,208,400,966):
        changed=bytearray(new);changed[index]=1
        assert not metadata_only_binary_change(old,changed)
    assert not metadata_only_binary_change(old,old+b'0')


def test_primary_paired_mean_avoids_interior_median_permutation_ties():
    from step8c3_analysis import cpu_statistics
    import itertools
    import numpy as np
    runs=[]
    for pair in range(1,7):
        for arm in ('A','B'):
            tasks=[dict(name=name,cpu=value,core=1,priority=priority) for name,value,priority in
                [('audio-tx',pair*10+(1 if arm=='B' else 0),11),('pipecat#1',.1,5),('pipecat#2',.01,5)]]
            runs.append(dict(pair=pair,arm=arm,summary=dict(stimulus_complete=True,cpu_windows=[dict(tasks=tasks) for _ in range(4)])))
    task=cpu_statistics(dict(completed=True,runs=runs))['tasks']['audio-tx']
    a=np.array(task['boot_medians_A']);b=np.array(task['boot_medians_B'])
    median_ties=sum(abs(np.median(np.where(swap,a,b))-np.median(np.where(swap,b,a)))>=1 for swap in itertools.product((False,True),repeat=6))
    assert median_ties>2
    assert task['paired_mean_delta_pp']==1 and task['paired_permutation_p']==2/64
