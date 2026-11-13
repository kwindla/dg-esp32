import copy
import json
import numpy as np
import pytest
from step8e2b_analysis import device_send_cadence, lcd_memory_gate, owner_gate
from uplink_continuity import link_tail,score_uplink
from test_soak_memory import rows
from soak_memory import transient_memory


def test_room_baseline_contamination_threshold():
    from alsa import room_baseline_level
    at_limit=32768*10**(-50/20)
    assert room_baseline_level([at_limit])['noise_contaminated'] is False
    assert room_baseline_level([at_limit*1.01])['noise_contaminated'] is True
    assert room_baseline_level([at_limit*.99])['noise_contaminated'] is False
    for invalid in ([], [float('nan')], [float('inf')]):
        with pytest.raises(ValueError):room_baseline_level(invalid)


def test_room_baseline_retains_wav_and_reports_startup_exclusion(tmp_path,monkeypatch,capsys):
    import alsa
    import wave
    monkeypatch.setattr(alsa,'resolve_jabra',lambda:('Jabra','qualified-device'))
    monkeypatch.setattr(alsa,'set_volumes',lambda card,path:path.write_text('qualified readbacks'))
    class Recording:
        ready_at=123.5
        def __init__(self,path,device,seconds,rate):
            assert device=='qualified-device' and seconds==3 and rate==16000
            self.path=path
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def wait(self):
            samples=np.concatenate((np.full(3200,30000),np.full(44800,50))).astype('<i2')
            with wave.open(str(self.path),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(samples.tobytes())
    monkeypatch.setattr(alsa,'record_wav',Recording)
    out=tmp_path/'baseline';result=alsa.record_room_baseline(out,'voice03')
    assert result['noise_contaminated'] is False
    assert result['baseline_dbfs']==pytest.approx(20*np.log10(50/32768))
    assert json.loads((out/'result.json').read_text())==result
    assert (out/'room.wav').stat().st_size==96044
    assert 'ROOM BASELINE: voice03:' in capsys.readouterr().out


def test_noisy_room_stops_voice_arm_before_server_or_playback(tmp_path,monkeypatch):
    import alsa
    import agent_conversation_test as agent
    from types import SimpleNamespace
    monkeypatch.setattr(agent,'credentials',lambda:None)
    monkeypatch.setattr(agent,'validate',lambda:{})
    monkeypatch.setattr(alsa,'record_room_baseline',lambda *a:dict(baseline_dbfs=-40,noise_contaminated=True))
    def forbidden(*a,**k):raise AssertionError('Acoustic arm continued after noisy baseline')
    monkeypatch.setattr(agent,'resolve_jabra',forbidden)
    monkeypatch.setattr(agent,'BotServer',forbidden)
    out=tmp_path/'voice'
    assert agent.run(SimpleNamespace(out=out,spl=65,soak_minutes=20,fault_free=True))==1
    result=json.loads((out/'result.json').read_text())
    assert result['acoustic_hold']=='noise' and result['turns']==[]
    assert result['room_baseline']['noise_contaminated'] is True


def test_clean_transient_failure_beats_disturbed_over_limit():
    data=rows([0,5,6,7],[100000]*4)
    data[2]['perf']['heap']['internal']['window_minimum_free']=94000
    data[2]['perf']['rebuffers']=1;data[3]['perf']['rebuffers']=1
    data[3]['perf']['heap']['internal']['window_minimum_free']=94000
    gate=transient_memory(data,data[-1]['perf'],[])
    assert gate['transient_generations']['1']['windows'][0]['disturbed_over_limit']
    assert gate['heap_transient_depth_passed'] is False


def test_console_window_keeps_raw_and_healthy_cadence():
    stamps=[i*20000 for i in range(500)]
    stamps[250:]=[t+55000 for t in stamps[250:]]
    trace=[dict(kind=12,reserved=1,ssrc=9,device_us=t) for t in stamps]
    gate=device_send_cadence(trace,0,stamps[-1],console_reads=[5000000])
    assert gate['raw_over60ms']==1 and gate['raw_gaps_ms']['maximum']==75
    assert not gate['hard_ceiling_passed'] and gate['target_passed']
    assert gate['over60ms']==1 and not gate['excluded']
    assert gate['console_informational']['count']>0
    assert not device_send_cadence(trace,0,stamps[-1])['hard_ceiling_passed']


def population(count,slow):
    stamps=[0]
    for i in range(count):stamps.append(stamps[-1]+(slow if i==10 else 20))
    return [dict(pc_id='p',ssrc=1,seq=i%65536,timestamp=i*960,observed_ms=t) for i,t in enumerate(stamps)]


def test_extreme_link_rate_and_session_death_boundaries():
    for count,slow,expected in [(2000,151,True),(1999,151,False),(2000,2000,True),(2000,2001,False)]:
        packets=population(count,slow)
        gate=link_tail(packets,0,packets[-1]['observed_ms']/1000,startup_seconds=0)
        assert gate['passed'] is expected
        assert gate['over_extreme_threshold']==1 and gate['hard_cap_ms']==2000


def test_post_jitter_buffer_input_clock_is_informational():
    packets=population(2000,20);end=packets[-1]['observed_ms']/1000
    result=score_uplink(packets,[dict(event='input-clock',pc_id='p',t=10,gap_s=.5)],'p',0,end)
    assert result['passed'] and result['pipeline_max_reported_gap_ms']==500


def test_sustained_owner_rate_does_not_weaken_ordinary_sweep():
    slow=[dict(value=44000)]
    assert owner_gate(slow,1000,sustained=True)['passed']
    assert not owner_gate(slow,999,sustained=True)['passed']
    assert not owner_gate(slow,10000)['passed']
    assert not owner_gate([dict(value=60001)],100000,sustained=True)['passed']
    assert owner_gate([],0,sustained=True)['passed'] is None


def test_lcd_memory_basis_boundaries_and_missing_reconnect():
    heap={c:dict(window_session_minimum_free=96*1024,minimum_free=80*1024,sampled_largest_minimum=32*1024) for c in ('internal','dma')}
    sample=dict(heap=heap,failed_allocations=dict(internal=0),heap_window=dict(schema=1,start_us=0,end_us=100,session_samples=1))
    assert lcd_memory_gate([sample],reconnects=1)['passed']
    assert lcd_memory_gate([sample],reconnects=0)['passed'] is None
    for field in heap['internal']:
        bad=copy.deepcopy(sample);bad['heap']['internal'][field]-=1
        assert lcd_memory_gate([bad],reconnects=1)['passed'] is False
    sample['failed_allocations']['internal']=1
    assert lcd_memory_gate([sample],reconnects=1)['passed'] is False


def test_one_direction_impairment_accounts_opposite_without_impairing_it(monkeypatch,tmp_path):
    import netem
    root={d:dict(qdisc=[dict(root=True,handle='0:',kind='noqueue')],filter=[],**{'class':[]}) for d in ('wlp8s0','ifb0')}
    calls=[]
    monkeypatch.setattr(netem,'snapshot',lambda *args:root)
    monkeypatch.setattr(netem.fcntl,'flock',lambda *args:None)
    monkeypatch.setattr(netem,'restore',lambda *args:None)
    def command(*args,**kwargs):
        calls.append(args)
        return json.dumps([dict(handle='8e03:',parent='8e01:3',bytes=len(calls)*100)])
    monkeypatch.setattr(netem,'command',command)
    with netem.Netem('192.168.4.29',['loss','10%'],'downlink',state=tmp_path/'state.json'):
        shaped=[cmd for cmd in calls if 'netem' in cmd]
        assert len(shaped)==2
        assert any('wlp8s0' in c and '10%' in c for c in shaped)
        assert any('ifb0' in c and '0%' in c for c in shaped)


def test_noisy_jabra_baseline_marks_timing_unavailable(tmp_path):
    import numpy as np
    from step7_support import acoustic_match,write_pcm
    t=np.arange(8000)/16000;reply=8000*np.sin(2*np.pi*1000*t)
    source=tmp_path/'source.wav';recording=tmp_path/'recording.wav'
    write_pcm(source,reply)
    for noise,sane in [(100,True),(1000,False)]:
        x=np.r_[np.full(16000,noise),reply,np.zeros(3200)]
        write_pcm(recording,x)
        result=acoustic_match(recording,source,1,0,.8)
        assert result['acoustic_timing_available'] is sane
        assert result['correlation']>.9


def test_unavailable_acoustic_timing_is_reported_without_failing_content():
    from agent_conversation_test import summarize, CHECKS
    from unittest.mock import patch
    turn=dict(checks={name:True for name in CHECKS},passed=True,failures=[],prompt='test')
    turn['checks']['gate']=None
    with patch('agent_conversation_test.classify_turn',return_value=dict(passed=True,classification='clean')):
        result=summarize([turn])
    assert result['passes']==1
    assert result['checks']['gate']==dict(passed=0,unavailable=1,total=1)


def test_actual_failed_allocation_callback_is_atomic_and_registered(tmp_path):
    from test_step8e2b_transport import ROOT,function,compile_run
    source=(ROOT/'main/performance.cpp').read_text()
    begin=source.index('DRAM_ATTR std::atomic<uint32_t> failed_internal')
    end=source.index('unsigned cert_us=',begin)
    callback=source[begin:end]
    init=function(source,'void performance_init()')
    compile_run(tmp_path,'''#include <atomic>
#include <cstdint>
#include <cassert>
#include <thread>
#define DRAM_ATTR
#define IRAM_ATTR
#define ESP_ERROR_CHECK(call) assert((call)==0)
constexpr uint32_t MALLOC_CAP_INTERNAL=1,MALLOC_CAP_DMA=2,MALLOC_CAP_SPIRAM=4;
using Callback=void(*)(size_t,uint32_t,const char*);Callback registered=nullptr;
int heap_caps_register_failed_alloc_callback(Callback cb){registered=cb;return 0;}
'''+callback+init+'''
int main(){
 performance_init();assert(registered);
 std::thread a([]{for(unsigned i=0;i<10000;++i)registered(16,MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA,"ISR");});
 std::thread b([]{for(unsigned i=0;i<10000;++i)registered(32,MALLOC_CAP_SPIRAM,"task");});
 a.join();b.join();assert(failed_total==20000 && failed_internal==10000 && failed_dma==10000 && failed_psram==10000);
 assert(failed_last==((uint64_t(16)<<32)|(MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA)) || failed_last==((uint64_t(32)<<32)|MALLOC_CAP_SPIRAM));
 registered(64,0,"generic");assert(failed_internal==10001 && failed_total==20001);
}
''',flags=('-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer'))


def test_native_logging_wrappers_never_use_console_sink_on_audio_or_owner(tmp_path):
    from test_step8e2b_transport import ROOT,compile_run
    source=(ROOT/'components/dg_diagnostics/diagnostics.cpp').read_text()
    body=source[source.index('static int deferred_logv'):source.index('extern "C" unsigned dg_deferred_log_drops')]
    compile_run(tmp_path,'''#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <cassert>
#include <initializer_list>
constexpr size_t log_bytes=192;constexpr int pdTRUE=1,taskSCHEDULER_NOT_STARTED=0;
std::atomic<unsigned> log_drops{0};void* log_queue=reinterpret_cast<void*>(1);
bool full=false;unsigned queued=0,printed=0;char last[log_bytes]{};
int xQueueSend(void*,const char* line,int wait){assert(wait==0);if(full)return 0;++queued;std::strcpy(last,line);return 1;}
bool xPortInIsrContext(){return false;}int xTaskGetSchedulerState(){return 1;}
const char* task="console";const char* pcTaskGetName(void*){return task;}
using esp_log_level_t=int;struct esp_log_config_t {struct {int log_level;} opts;esp_log_config_t(int n):opts{n}{}};
constexpr int ESP_LOG_NONE=0;int fake_level=3;int esp_log_level_get_timeout(const char*){return fake_level;}
extern "C" void __real_esp_log_va(esp_log_config_t,const char*,const char*,va_list){++printed;}
extern "C" void __real_esp_log_writev(int,const char*,const char*,va_list){++printed;}
extern "C" int __real_vprintf(const char*,va_list){++printed;return 1;}
extern "C" int __real_puts(const char*){++printed;return 1;}
'''+body+'''
void va_wrappers(int level){
 auto call=[&](const char* format,...){va_list args;va_start(args,format);
 __wrap_esp_log_va(level,"udp",format,args);__wrap_esp_log_writev(level,"udp",format,args);va_end(args);};
 call("debug");
}
int main(){
 task="peer-loop";fake_level=2;
 for(int level:{3,4,5}){__wrap_esp_log(level,"udp","filtered");__wrap_esp_log_write(level,"udp","filtered");va_wrappers(level);}
 assert(queued==0 && printed==0 && log_drops==0);
 fake_level=ESP_LOG_NONE;
 for(int level:{0,1,2,3,4,5}){__wrap_esp_log(level,"udp","timeout");__wrap_esp_log_write(level,"udp","timeout");va_wrappers(level);}
 assert(queued==0 && printed==0 && log_drops==0);fake_level=5;
 __wrap_esp_log(0,"udp","NONE");__wrap_esp_log_write(0,"udp","NONE");va_wrappers(0);
 assert(queued==0 && printed==0 && log_drops==0);fake_level=2;
 __wrap_esp_log(2,"udp","warn");__wrap_esp_log_write(1,"udp","error");va_wrappers(2);
 assert(queued==4 && printed==0);queued=0;fake_level=3;
 for(auto name:{"audio-capture","audio-playback","audio-tx","tone-source","peer-loop","peer-rx"}){
  task=name;unsigned before=queued;
  __wrap_esp_log(1,"udp","error %d",7);assert(std::strcmp(last,"error 7")==0);
  __wrap_esp_log_write(1,"udp","error %d",8);__wrap_printf("error %d",9);__wrap_puts("error");
  assert(queued==before+4 && printed==0);
  full=true;before=log_drops.load();__wrap_esp_log(1,"udp","error");__wrap_printf("error");__wrap_puts("error");
  assert(log_drops==before+3 && printed==0);full=false;
 }
 task="console";__wrap_esp_log(1,"stats","stats");__wrap_esp_log_write(1,"stats","stats");__wrap_printf("stats");__wrap_puts("stats");
 assert(printed==4);task="diagnostic-log";__wrap_printf("drained");assert(printed==5);
}
''')


def test_link_coverage_spans_forced_reconnect_without_cross_session_gap():
    a=population(1000,20)
    b=[dict(r,pc_id='second',observed_ms=r['observed_ms']+25000) for r in population(1000,20)]
    gate=link_tail(a+b,0,45,startup_seconds=0)
    assert gate['passed'] is True and gate['intervals']==2000
    assert gate['eligible_maximum_ms']==20
    assert not link_tail(a+b,0,46,startup_seconds=0)['covered']


def test_missing_perf_reset_cannot_pass_lcd_session_floor():
    sample=dict(heap={c:dict(window_session_minimum_free=100000,minimum_free=90000,sampled_largest_minimum=40000) for c in ('internal','dma')},
        failed_allocations=dict(internal=0),heap_window=dict(schema=1,start_us=0,end_us=100,session_samples=1))
    second=copy.deepcopy(sample);second['heap_window'].update(start_us=200,end_us=300)
    gate=lcd_memory_gate([sample,second],reconnects=1)
    assert gate['passed'] is None and not gate['window_coverage_complete']
