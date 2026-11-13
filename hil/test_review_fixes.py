"""Review regressions: causal timing, independent clocks and honest census."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from step7_support import acoustic_match,write_pcm
from step8_summary import profile
import soak_test

def test_soak_finishes_greeting_before_new_reply_gate_edge():
    from unittest.mock import Mock
    board=Mock()
    board.latest_entry.return_value=(240,1.5,'GATE closed t=1500000')
    assert soak_test.wait_pending_playout(board)
    board.wait_for.assert_called_once_with('GATE open',15,since=240)

def test_soak_does_not_wait_for_another_open_edge_when_already_open():
    from unittest.mock import Mock
    board=Mock()
    board.latest_entry.return_value=(250,2.0,'GATE open t=2000000')
    assert not soak_test.wait_pending_playout(board)
    board.wait_for.assert_not_called()

def test_acoustic_onset_before_server_send_is_rejected(tmp_path):
    rng=np.random.default_rng(123)
    ref=rng.normal(0,2500,16000);x=rng.normal(0,2,64000)
    x[16000:32000]+=ref
    recording=tmp_path/'recording.wav';reference=tmp_path/'reference.wav'
    write_pcm(recording,x);write_pcm(reference,ref)
    good=acoustic_match(recording,reference,.8,100,.7,server_send_at=100.9)
    assert good['correlation']>.95
    with pytest.raises(AssertionError,match='precedes server send'):
        acoustic_match(recording,reference,.8,100,.7,server_send_at=101.5)

def test_missing_quiet_prefix_reports_unknown_instead_of_nan(tmp_path):
    rng=np.random.default_rng(122);ref=rng.normal(0,2500,8000)
    recording=tmp_path/'recording.wav';reference=tmp_path/'reference.wav'
    write_pcm(recording,np.r_[np.zeros(4000),ref,np.zeros(4000)]);write_pcm(reference,ref)
    row=acoustic_match(recording,reference,.1,0,.1)
    assert row['correlation']>.99
    assert row['rms_rise_db'] is None
    json.dumps(row,allow_nan=False)

def test_task_cpu_uses_device_elapsed_despite_serial_stall(tmp_path):
    def point(host,device,runtime):
        task=dict(id=1,name='peer-loop',runtime_us=runtime,core=0,stack_free_bytes=8000)
        perf=dict(device_us=device,tasks=[task],encode_p50_us=0,encode_p95_us=0,decode_p50_us=0,decode_p95_us=0)
        perf.update({key:0 for key in ('rebuffers','drift_drops','plc_frames','i2s_underflows','decode_errors','queue_dropped','sent','received')})
        return dict(observed_monotonic_s=host,observed_unix_s=host,perf=perf)
    path=tmp_path/'result.json'
    path.write_text(json.dumps(dict(phases=[dict(name='idle',before=point(0,1000000,100),after=point(4,3000000,1000100))])))
    row=profile(path)[0]
    assert row['tasks'][0]['cpu_percent']==50  # host-clock denominator would give 25
    assert row['device_duration_s']==2

def rows(generation,rate,start=0,base=100000):
    return [dict(elapsed_s=start+minute*60,perf=dict(generation=generation,heap={
        caps:dict(free=base-rate*minute) for caps in ('internal','psram','dma')})) for minute in range(21)]

def test_heap_slope_catches_small_steady_leak_and_separates_generations():
    leaking=soak_test.heap_trends(rows(1,100))
    assert not leaking['1']['internal']['passed']  # only 400 B depth, below old gate
    stable=soak_test.heap_trends(rows(1,0)+rows(2,0,start=300,base=20000))
    assert all(r['passed'] for group in stable.values() for r in group.values())
    assert stable['2']['internal']['growth_bytes_per_minute']==pytest.approx(0,abs=1e-6)

def test_heap_slope_keeps_transient_and_unknown_measurements_explicit():
    data=rows(1,0)
    for caps in ('internal','psram','dma'):data[2]['perf']['heap'][caps]['free']-=5000
    assert all(r['passed'] for r in soak_test.heap_trends(data)['1'].values())
    assert soak_test.heap_trends(data[:1])['1']['internal']['passed'] is None

def test_missing_closed_stack_size_is_unknown():
    closed=dict(tx_stack=8192,tx_stack_bytes=32768,owner_stack=8192,rx_stack=4096,decode_stack=8192)
    margins=soak_test.closed_stack_margins(closed)
    assert margins['tx_stack']==25
    assert set(margins)=={'tx_stack'}
    from soak_memory import closed_stack_unknown_sizes
    assert set(closed_stack_unknown_sizes(closed))=={'owner_stack','rx_stack','decode_stack'}

def test_activity_above_target_is_rejected_during_config_check(tmp_path):
    spec=importlib.util.spec_from_file_location('checker',Path(__file__).resolve().parents[1]/'scripts/check-sdkconfig.py')
    checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)
    text='CONFIG_DG_PLAYBACK_ACTIVITY_DBFS=-20\nCONFIG_DG_PLAYBACK_TARGET_DBFS=-22\n'
    (tmp_path/'sdkconfig').write_text(text);(tmp_path/'sdkconfig.defaults').write_text(text)
    assert checker.check(tmp_path)
    text=text.replace('ACTIVITY_DBFS=-20','ACTIVITY_DBFS=-40')
    (tmp_path/'sdkconfig').write_text(text);(tmp_path/'sdkconfig.defaults').write_text(text)
    assert not checker.check(tmp_path)

def test_mic_parser_accepts_single_native_channel_and_legacy_loop_format():
    from audio_loop_test import reports
    board=SimpleNamespace(lines_since=lambda cursor:[(0,1,'MIC rmsR=-30.0 peak=1000 band_snr=20 band_power=-31'),
        (1,2,'MIC rmsL=-180 rmsR=-30.0 peak=1000 band_snr=20 band_power=-31')])
    parsed=reports(board,0)
    assert len(parsed)==2
    assert parsed[0]['rms_dbfs']==parsed[1]['rms_dbfs']==-30


def test_offline_play_follows_vad_stop_without_waiting_for_telemetry(monkeypatch):
    import e2e_common
    clock=[10.0];commands=[]
    monkeypatch.setattr(e2e_common.time,'monotonic',lambda:clock[0])
    def sleep(delay):clock[0]+=delay
    monkeypatch.setattr(e2e_common.time,'sleep',sleep)
    board=SimpleNamespace(command=lambda name,timeout:commands.append((name,clock[0])))
    events=lambda:[dict(event='vad-stop',t=10.0)]  # no telemetry at all
    result=e2e_common.request_offline_play(board,events,10)
    assert commands==[('play',10.5)]
    assert result['offline_play_requested_at']==10.5


def test_vad_stop_before_alsa_completion_still_schedules_fixed_delay(monkeypatch):
    import e2e_common
    clock=[10.0];commands=[]
    monkeypatch.setattr(e2e_common.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(e2e_common.time,'sleep',lambda delay:clock.__setitem__(0,clock[0]+delay))
    board=SimpleNamespace(command=lambda name,timeout:commands.append((name,clock[0])))
    e2e_common.request_offline_play(board,lambda:[dict(event='vad-stop',t=9.8)],10)
    assert commands==[('play',pytest.approx(10.3))]


def test_gate_activity_executes_runtime_gain_expression(tmp_path):
    """Compile the production gate predicate without hardware driver calls."""
    import subprocess
    root=Path(__file__).resolve().parents[1]
    source=(root/'components/dg_audio/src/pipeline.cpp').read_text()
    body=source.split('size_t write(const int16_t* pcm,size_t n,bool eligible=true,uint32_t timeout_ms=100) {',1)[1].split('if(active && eligible)',1)[0]
    program='''#include <atomic>
#include <algorithm>
#include <cstdlib>
#include <cstdint>
#include <cstddef>
constexpr unsigned CONFIG_DG_SPEAKER_DMA_DESCRIPTORS=4,CONFIG_DG_SPEAKER_DMA_FRAMES=240,CONFIG_DG_PLAYBACK_GAIN_PERCENT=33;
struct Fake {
 std::atomic<unsigned> playback_gain{33};int playback_target=0;
 bool detects(const int16_t* pcm,size_t n) {
'''+body+'''return active;}
};
int main() {
 Fake gate;int16_t sample=20;
 if(!gate.detects(&sample,1))return 1;
 gate.playback_gain=100;
 if(gate.detects(&sample,1))return 2;
 gate.playback_gain=1;sample=2;
 if(!gate.detects(&sample,1))return 3;
 gate.playback_gain=100;gate.playback_target=-22;
 if(!gate.detects(&sample,1))return 4;
 gate.playback_target=0;
 if(gate.detects(&sample,1))return 5;
 return 0;
}
'''
    cpp=tmp_path/'activity.cpp';exe=tmp_path/'activity';cpp.write_text(program)
    subprocess.run(['g++','-std=c++17',str(cpp),'-o',str(exe)],check=True,capture_output=True)
    completed=subprocess.run([str(exe)],capture_output=True)
    assert completed.returncode==0, 'Gate threshold did not track runtime gain'


def test_hil_sources_do_not_embed_a_users_home_directory():
    marker='/'+'home'+'/'
    root=Path(__file__).resolve().parent
    embedded=[p.name for p in root.rglob('*.py') if '.venv' not in p.parts and marker in p.read_text()]
    assert not embedded, f'Hardcoded home paths in: {embedded}'


def speaker_callback_program():
    """Compile the production ISR against the task-owned clock ledger."""
    root=Path(__file__).resolve().parents[1]
    body=(root/'components/dg_audio/src/speaker.cpp').read_text().split('bool Speaker::sent(i2s_chan_handle_t,i2s_event_data_t* event,void* context) {',1)[1].split('bool Speaker::overflow',1)[0]
    return root, r'''#include <atomic>
#include <cstdint>
#include "dg_audio/speaker_clock.h"
using dg_audio::SpeakerClock;
int64_t now=0;
int64_t esp_timer_get_time(){return now;}
struct Event {unsigned size;};
struct Speaker {
 std::atomic<unsigned> completed_{0},submitted_{0},underflows_{0},callback_sequence_{0},callback_us_{0};
 static bool sent(Event* event,void* context) {
'''+body+r'''};
'''


def compile_speaker_callback(tmp_path,main):
    import subprocess
    root,program=speaker_callback_program()
    cpp=tmp_path/'credit.cpp';exe=tmp_path/'credit';cpp.write_text(program+main)
    subprocess.run(['g++','-std=c++17','-I'+str(root/'components/dg_audio/include'),
        '-I'+str(root/'components/dg_diagnostics/include'),str(cpp),'-o',str(exe)],check=True,capture_output=True)
    subprocess.run([str(exe)],check=True,capture_output=True)


def test_speaker_empty_queue_credit_does_not_poison_next_stream(tmp_path):
    """Paused completions cannot donate credits or poison a fresh device epoch."""
    compile_speaker_callback(tmp_path,r'''int main() {
 Speaker s;Event e{960};
 for(int i=0;i<1000;++i){now+=15000;Speaker::sent(&e,&s);}
 if(s.underflows_ || s.submitted_ || s.completed_!=240000)return 1;
 SpeakerClock c;c.start(s.completed_,s.callback_us_,now,960);
 now+=15000;Speaker::sent(&e,&s);c.observe(s.completed_,s.callback_us_,now);
 now+=5000;c.observe(s.completed_,s.callback_us_,now);
 auto tick=c.next(now);if(!tick.ready || tick.tick.index!=0 || tick.tick.silence_blocks)return 2;
 if(!c.rendered(tick.tick))return 3;c.submitted(320);
 if(c.completed()!=240 || c.submitted()!=1280 || c.next(now).ready)return 4;
 c.stop();if(c.valid(tick.tick))return 5;
 return 0;
}
''')

def test_playback_idle_consumes_ticks_and_latches_drain_once():
    root=Path(__file__).resolve().parents[1]
    source=(root/'components/dg_audio/src/pipeline.cpp').read_text()
    body=source.split('    unsigned idle_playback(',1)[1].split('    void playback_run()',1)[0]
    assert 'vTaskDelay' not in body
    assert 'speaker.render(opportunity.tick)' in body
    assert 'std::fill_n(pcm,320,0)' in body
    assert 'dsp.flush(pcm,320)' in body
    assert 'speaker.begin_drain()' in body
    # Executable scheduling/tail and drain-latch checks live in PipelineI2s.
    speaker=(root/'components/dg_audio/include/dg_audio/speaker.h').read_text()
    assert 'if(!draining_) {drain_target_=submitted_.load();draining_=true;}' in speaker

def test_dma_deficit_accounting_preserves_concurrent_write_credit(tmp_path):
    """A callback before write completion cannot erase or double its actual suffix."""
    compile_speaker_callback(tmp_path,r'''int main(){
 Speaker s;Event e{960};SpeakerClock c;c.start(0,0,0,960);
 now=20000;Speaker::sent(&e,&s);c.observe(s.completed_,s.callback_us_,now);
 now=25000;c.observe(s.completed_,s.callback_us_,now);
 auto p=c.next(now);if(!p.ready || !c.rendered(p.tick))return 1;
 // Partial write returns only after the next completion interrupt.
 now=30000;Speaker::sent(&e,&s);c.observe(s.completed_,s.callback_us_,now);
 c.submitted(80);c.submitted(240);
 if(s.submitted_ || s.underflows_ || c.submitted()!=1280 || c.completed()!=480)return 2;
 if(!c.valid(p.tick) || c.next(now).ready)return 3;
 return 0;
}
''')


def test_transport_partial_reads_expire_on_tick_and_generation_without_replay(tmp_path):
    """Execute the production adapter; a new tick discards only the old suffix."""
    import subprocess
    root=Path(__file__).resolve().parents[1]
    body=(root/'components/pipecat_esp_peer/src/transport.cpp').read_text().split(
        'int32_t EspPeerTransport::read_bot_audio(int16_t* frames, size_t count,dg::MediaToken& token) {',1)[1].split(
        'bool EspPeerTransport::cancel_bot_audio(',1)[0]
    program=r'''#include <algorithm>
#include <memory>
#include <mutex>
#include <atomic>
#include <cstdint>
#include "dg_render_tick.h"
#include "dg_media_token.h"
struct Play {uint64_t gen=1;uint64_t generation(){return gen;}};
struct Session:dg::MediaSource {
 bool stop=false,remote=false,failed=false;
 struct Renderer {void invalidate(){}} pcm_renderer;
 bool valid(uint64_t generation) const override {return !stop && !remote && !failed && p.gen==generation;}
 std::mutex read_mutex;Play p;Play* playout=&p;
 std::atomic<unsigned> cancel_serial{0};unsigned consumed_cancel_serial=0;
 std::atomic<uint64_t> cancelled_pcm_samples{0};
 uint64_t pcm_generation=0;size_t offset=0;unsigned decode_errors=0,pulls=0;
 struct PCM {int16_t pcm[320]{};int count=0;} remainder;
 struct Options {dg::RenderTick (*render_clock)()=[](){return dg::render_tick;};} options;
 dg::RenderTick last_render{};
 int pull(dg::RenderTick tick){
  if(!tick.valid || (pulls && tick.index<=last_render.index))return 0;
  last_render=tick;pcm_generation=p.gen;++pulls;
  std::fill_n(remainder.pcm,320,int16_t(pulls));return 320;
 }
 void fault(){failed=true;}
};
struct Impl {std::shared_ptr<Session> s=std::make_shared<Session>();auto current(){return s;}};
struct Transport {Impl impl;Impl* impl_=&impl;
 int32_t read_bot_audio(int16_t* frames,size_t count) {dg::MediaToken token;return read_bot_audio(frames,count,token);}
 int32_t read_bot_audio(int16_t* frames,size_t count,dg::MediaToken& token){
'''+body+r'''};
int main(){
 Transport t;int16_t output[320]{};auto& s=*t.impl.s;
 dg::render_tick.valid=true;dg::render_tick.device_epoch=1;
 if(t.read_bot_audio(output,80)!=80 || output[0]!=1 || s.pulls!=1)return 1;
 if(t.read_bot_audio(output,80)!=80 || output[0]!=1 || s.pulls!=1)return 2;
 dg::render_tick.index=1;
 if(t.read_bot_audio(output,80)!=80 || output[0]!=2 || s.pulls!=2)return 3;
 ++s.p.gen;++s.cancel_serial; // same-tick cancellation invalidates 240-sample suffix
 if(t.read_bot_audio(output,80)!=0 || s.pulls!=2 || s.cancelled_pcm_samples!=240)return 4;
 dg::render_tick.index=2;
 if(t.read_bot_audio(output,320)!=320 || output[0]!=3 || s.pulls!=3)return 5;
 s.remote=true;dg::render_tick.index=3;
 if(t.read_bot_audio(output,320)!=0 || s.pulls!=3)return 6;
 return 0;
}
'''
    cpp=tmp_path/'partial.cpp';exe=tmp_path/'partial';cpp.write_text(program)
    subprocess.run(['g++','-std=c++17','-I'+str(root/'components/dg_diagnostics/include'),
        str(cpp),'-o',str(exe)],check=True,capture_output=True)
    subprocess.run([str(exe)],check=True,capture_output=True)
