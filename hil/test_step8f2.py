import json
import numpy as np
import pytest
from playback_quality import TRANSPORT_COUNTERS,transport_gate,decoded_match,acoustic_content_verdict,counter_damage
from step8e2b_analysis import device_send_cadence,owner_gate,link_backpressure_windows,console_windows,lcd_memory_gate
from netem import impairment_flow_gate


def test_insert_and_trim_are_edited_without_transport_damage():
    before=dict(playout_schema=2,**{k:0 for k in TRANSPORT_COUNTERS})
    for key in ('drift_inserts','latency_trim'):
        after=dict(before,**{key:1})
        gate=transport_gate(before,after)
        assert gate['passed'] and gate['classification']=='edited'
        assert not counter_damage(gate['deltas'])
    after=dict(before,lost=1,drift_inserts=1)
    assert not transport_gate(before,after)['passed']


@pytest.mark.parametrize('insert_at',[32000,31773])
def test_decoded_piecewise_insert_mid_reply_and_wrong_content(insert_at):
    rng=np.random.default_rng(81)
    source=rng.normal(0,3000,64000)
    decoded=np.r_[np.zeros(1200),source[:insert_at],np.zeros(320),source[insert_at:],np.zeros(1000)]
    matched=decoded_match(decoded,source)
    assert matched['passed'] and matched['correlation']>=.95
    assert matched['blocks'][5]['offset_samples']==1200
    assert matched['blocks'][-1]['offset_samples']==1520
    assert matched['whole_reply_correlation']<.95
    wrong=rng.normal(0,3000,len(decoded))
    assert not decoded_match(wrong,source)['passed']
    decoded[17000:25000]=rng.normal(0,3000,8000)
    assert not decoded_match(decoded,source)['passed']


def test_acoustic_stt_mismatch_is_informational_only_with_both_correlations():
    segment=dict(decoded_content=dict(passed=True),board=dict(correlation=.93))
    assert acoustic_content_verdict(False,[segment]) is None
    assert acoustic_content_verdict(True,[segment]) is True
    segment['board']['correlation']=.59
    assert acoustic_content_verdict(False,[segment]) is False
    assert acoustic_content_verdict(False,[]) is False


def trace():
    return [dict(kind=12,reserved=1,ssrc=2,device_us=t) for t in (0,20000,40000,120000,140000)]


@pytest.mark.parametrize('part',[1,3,10])
def test_device_backpressure_slowcalls_keep_raw_tail(part):
    rows=trace()+[dict(kind=11,aux=part,device_us=110000,value=70000)]
    gate=device_send_cadence(rows,0,140000)
    assert gate['raw_gaps_ms']['maximum']==80 and gate['raw_over60ms']==1
    assert gate['hard_ceiling_passed']
    assert gate['link_backpressure']['count']==2
    assert gate['link_backpressure']['gaps_ms']['maximum']==80
    slow=[dict(kind=8,device_us=120000,value=80000)]
    owner=owner_gate(slow,100,trace=rows)
    assert owner['passed'] and owner['raw_over40']==1 and owner['raw_maximum_ms']==80
    assert owner['link_backpressure']['count']==1 and owner['link_backpressure']['maximum_ms']==80
    rows[-1]['value']=19999
    assert not device_send_cadence(rows,0,140000)['hard_ceiling_passed']
    rows[-1].update(value=70000,aux=0)
    assert not device_send_cadence(rows,0,140000)['hard_ceiling_passed']


@pytest.mark.parametrize('group,key',[('peer_profile','socket_send_enomem'),('drop_reasons','send_cutoff')])
def test_backpressure_perf_brackets_require_increment_and_same_generation(group,key):
    samples=[dict(device_us=30000,generation=1,**{group:{key:0}}),dict(device_us=130000,generation=1,**{group:{key:1}})]
    drop=dict(kind=7,flags=1,value=10,reserved=1,device_us=100000)
    gate=device_send_cadence(trace()+[drop],0,140000,snapshots=samples)
    assert gate['hard_ceiling_passed'] is None and gate['link_backpressure']['count']==4
    samples[1]['generation']=2
    assert not link_backpressure_windows(trace=[drop],snapshots=samples)
    samples[1]['generation']=1;samples[1][group][key]=0
    assert not link_backpressure_windows(trace=[drop],snapshots=samples)


def test_console_windows_drop_dumps_and_keep_stats_informational(tmp_path):
    path=tmp_path/'serial.log'
    path.write_text('CONSOLE_WINDOW command=dump-arrivals start_us=20000 end_us=120000\nCONSOLE_WINDOW command=stats start_us=50000 end_us=60000\nCONSOLE_WINDOW start_us=1 end_us=999999\n')
    windows=console_windows(path)
    assert len(windows)==1 and windows[0]['command']=='stats'
    gate=device_send_cadence(trace(),0,140000,windows)
    assert not gate['hard_ceiling_passed'] and gate['raw_over60ms']==gate['over60ms']==1


def test_soak_leaves_first_generation_settled_before_reconnect():
    from agent_conversation_test import soak_schedule
    assert soak_schedule(20,True)==((360,'reconnect'),)
    with pytest.raises(AssertionError):soak_schedule(5,False)
    with pytest.raises(AssertionError):soak_schedule(6,True)


@pytest.mark.parametrize('direction,required',[('both',('tx','rx')),('downlink',('tx',)),('uplink',('rx',))])
def test_impairment_with_no_owned_board_bytes_is_invalid(direction,required):
    assert not impairment_flow_gate(dict(tx=0,rx=0),dict(tx=0,rx=0),direction)['valid']
    assert impairment_flow_gate(dict(tx=0,rx=0),{k:100000 for k in required},direction,session_seconds=60)['valid']
    for key in required:
        after=dict(tx=100000,rx=100000);after[key]=0
        assert not impairment_flow_gate(dict(tx=0,rx=0),after,direction)['valid']


def test_actual_owner_heap_sampler_walks_at_one_hz_only(tmp_path):
    from test_step8e2b_transport import ROOT,function,compile_run
    body=function((ROOT/'main/performance.cpp').read_text(),'extern "C" void dg_heap_window_sample')
    compile_run(tmp_path,'''#include <array>
#include <atomic>
#include <mutex>
#include <algorithm>
#include <cstdint>
#include <cassert>
constexpr std::array<unsigned,3> heap_caps{1,2,3};
std::array<size_t,3> window_min{999,999,999},session_window_min{999,999,999},largest_min{999,999,999},shadow_min{999,999,999};
std::mutex heap_window_mutex;std::atomic<bool> compare_sampling{false};
int window_samples=0,session_window_samples=0,shadow_samples=0,walks=0,free_reads=0;
int64_t next_largest_sample_us=0,now=0;
int64_t esp_timer_get_time(){return now;}
size_t heap_caps_get_free_size(unsigned){++free_reads;return 100;}
size_t heap_caps_get_largest_free_block(unsigned caps){assert(caps!=3);++walks;return 90;}
'''+body+'''
int main(){for(int i=0;i<100;++i){now=i*10000;dg_heap_window_sample(true);}
assert(walks==2 && free_reads==300 && window_samples==100 && session_window_samples==100);
now=1000000;dg_heap_window_sample(false);assert(walks==4 && free_reads==303 && session_window_samples==100);
assert(largest_min[0]==90 && largest_min[2]==90);
}
''')


def test_invalid_impairment_context_restores_and_preserves_verdict(monkeypatch,tmp_path):
    import netem
    original={d:dict(qdisc=[dict(root=True,handle='0:',kind='noqueue')],filter=[],**{'class':[]}) for d in ('wlp8s0','ifb0')}
    restored=[]
    monkeypatch.setattr(netem,'snapshot',lambda *a:original)
    monkeypatch.setattr(netem,'restore',lambda *a:restored.append(True))
    monkeypatch.setattr(netem.fcntl,'flock',lambda *a:None)
    monkeypatch.setattr(netem,'command',lambda *a,**k:json.dumps([dict(handle='8e03:',parent='8e01:3',bytes=0)]))
    state=tmp_path/'tc.json'
    with pytest.raises(RuntimeError,match='Invalid impairment arm'):
        with netem.Netem('192.168.4.29',['loss','3%'],'both',state=state):pass
    data=json.loads(state.read_text())
    assert restored and data['status']=='restored' and data['impairment_flow']['valid'] is False


def test_blackout_zero_sent_phase_does_not_invalidate_observed_whole_arm(monkeypatch,tmp_path):
    import netem
    original={d:dict(qdisc=[dict(root=True,handle='0:',kind='noqueue')],filter=[],**{'class':[]}) for d in ('wlp8s0','ifb0')}
    byte_count=[0]
    monkeypatch.setattr(netem,'snapshot',lambda *a:original)
    monkeypatch.setattr(netem,'restore',lambda *a:None)
    monkeypatch.setattr(netem.fcntl,'flock',lambda *a:None)
    monkeypatch.setattr(netem,'command',lambda *a,**k:json.dumps([dict(handle='8e03:',parent='8e01:3',bytes=byte_count[0])]))
    state=tmp_path/'tc.json'
    with netem.Netem('192.168.4.29',['loss','0%'],'both',state=state) as arm:
        byte_count[0]=1000
        arm.update(['loss','100%'])
        arm.update(['loss','0%'])
        byte_count[0]=2000
    data=json.loads(state.read_text())
    assert data['impairment_periods'][0]['valid'] is False
    assert data['impairment_flow']['valid'] is True and data['status']=='restored'


def test_voice_owner_gate_counts_reset_generations_and_requires_trace_coverage(tmp_path):
    from step8e2b_analysis import voice_owner_gate
    turns=[dict(before_perf=dict(device_us=0,generation=1),after_perf=dict(device_us=100000,generation=1)),
           dict(before_perf=dict(device_us=200000,generation=2),after_perf=dict(device_us=300000,generation=2))]
    (tmp_path/'result.json').write_text(json.dumps(dict(turns=turns)))
    samples=[dict(device_us=t,generation=g,heap_window={},peer_profile=dict(loop_iteration=dict(calls=n),trace=dict(overwritten=0)))
        for t,g,n in ((0,1,0),(100000,1,10),(200000,2,0),(300000,2,10))]
    (tmp_path/'serial.log').write_text('\n'.join('PERF '+json.dumps(s) for s in samples))
    rows=[dict(kind=12,device_us=t) for t in (0,100000,200000,300000)]
    rows.append(dict(kind=8,device_us=180000,value=80000))
    (tmp_path/'arrivals.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    gate=voice_owner_gate(tmp_path)
    assert gate['calls']==20 and gate['passed'] and gate['raw_over40']==1
    assert gate['excluded'][0]['categories']==['reconnect']
    assert gate['maximum_ms'] is None
    (tmp_path/'arrivals.jsonl').write_text('')
    assert voice_owner_gate(tmp_path)['passed'] is None


def test_decoded_leading_block_cannot_anchor_to_another_occurrence():
    rng=np.random.default_rng(25)
    source=np.r_[np.zeros(800),rng.normal(0,3000,40000)]
    true_reply=source.copy();true_reply[:1600]+=rng.normal(0,50,1600)
    decoded=np.r_[source[:1600],rng.normal(0,3000,3200),true_reply,np.zeros(1000)]
    gate=decoded_match(decoded,source)
    assert gate['whole_reply_correlation']>.99
    assert gate['passed'] and gate['correlation']>.99
    assert gate['offset_samples']==4800


def test_pcm_rescore_preserves_independent_failures_and_original_evidence(tmp_path,monkeypatch):
    import playback_quality,step7_support
    from step8f2_analysis import rescore_voice_content
    from agent_conversation_test import CHECKS
    monkeypatch.setattr(playback_quality,'decoded_match',lambda *a:dict(passed=True,correlation=.99))
    monkeypatch.setattr(step7_support,'pcm',lambda *a:np.ones(320))
    before=dict(playout_schema=2,pc_id='p',generation=1,received=0,decode_frames=0,pulled_frames=0,**{k:0 for k in TRANSPORT_COUNTERS})
    after=dict(before,received=10,decode_frames=10,pulled_frames=10)
    checks={k:True for k in CHECKS};checks.update(vad=False,playback=False,gate=False,one_reply=False)
    row=dict(prompt='test',passed=False,checks=checks,failures=[dict(check='vad'),dict(check='playback'),dict(check='gate')],
        before_perf=before,after_perf=after,reply_window_transport=dict(before=before,after=after),
        events=[dict(event='llm-start',response_id=1),dict(event='llm-response',response_id=1),dict(event='final-transcript')],
        gate_events=[(9,'closed'),(12,'open')],acoustic_stt_passed=True,
        tts_segments=[dict(cancelled=False,response_id=1,source_path='reference.wav',correlation=.8,board=dict(correlation=.9),
            jabra_level=dict(passed=True),decoded_content=dict(passed=False),verified=False,complete=False,onset=10,end=11)])
    source=tmp_path/'result.json';source.write_text(json.dumps(dict(turns=[row])))
    (tmp_path/'turn001').mkdir();(tmp_path/'turn001/rx.wav').touch()
    original=source.read_bytes();result=rescore_voice_content(tmp_path)
    corrected=result['rows'][0]['corrected_checks']
    assert corrected['playback'] and corrected['gate'] and corrected['one_reply']
    assert corrected['vad'] is False and not result['rows'][0]['corrected_passed']
    assert result['original_passes']==result['corrected_passes']==0
    assert source.read_bytes()==original
    row['checks'].pop('vad');row['failures']=[]
    source.write_text(json.dumps(dict(turns=[row])))
    assert not rescore_voice_content(tmp_path)['rows'][0]['corrected_passed']
    (tmp_path/'turn001/rx.wav').unlink()
    unavailable=rescore_voice_content(tmp_path)['rows'][0]
    assert unavailable['rescore_available'] is False and unavailable['corrected_checks']==checks
