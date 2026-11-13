"""Compile the production callback and send phase, rather than mirror their branches."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]


def function(source,marker):
    assert source.count(marker)==1
    start=source.index(marker);opening=source.index('{',start);depth=1;end=opening+1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    return source[start:end]


def compile_run(tmp_path,source,flags=()):
    file=tmp_path/'test.cpp';file.write_text(source);binary=tmp_path/'test'
    compiled=subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Werror',
        *flags,'-I'+str(ROOT/'components/pipecat_esp_peer/src'),'-I'+str(ROOT/'components/nlohmann_json/include'),'-I'+str(ROOT/'third_party/pipecat-client-cxx/include'),
        str(file),str(ROOT/'components/pipecat_esp_peer/src/msg_queue.cpp'),'-o',str(binary)],
        capture_output=True,text=True)
    assert compiled.returncode==0,compiled.stderr
    subprocess.run([str(binary)],check=True)


def test_actual_inbound_callback_preserves_session_on_normal_and_invalid_overflow(tmp_path):
    cpp=(ROOT/'components/pipecat_esp_peer/src/transport.cpp').read_text()
    body=function(cpp,'static int data(esp_peer_data_frame_t*')
    begin=cpp.index('if(result!=MessageQueue::Result::Queued && result!=MessageQueue::Result::ReplacedControl)')
    end=cpp.index(';',begin)+1
    rx_drop_branch=cpp[begin:end]
    compile_run(tmp_path,'''#include "inbound_queue.h"
#include "message_admission.h"
#include "msg_queue.h"
#include <cassert>
#include <string>
using namespace pipecat::esp_peer;
struct esp_peer_data_frame_t {char* data;int size,type=1,stream_id=0;};
constexpr int ESP_PEER_DATA_CHANNEL_STRING=1;
struct PeerPart {enum {DataCallback};};struct PeerTimer {explicit PeerTimer(int){}};
struct Critical {explicit Critical(int){}};int64_t now=1000;int64_t now_ms(){return now/1000;}
int64_t esp_timer_get_time(){return now;}
struct Session {
 bool stop=false;int msg_mux=0,drops=0,faults=0,data_received=0;
 MessageWorkBudget message_work;
 struct {size_t max_message_bytes=512;} options;
 char storage[InboundQueue::capacity*512]{};InboundQueue inbound{storage,sizeof(storage),512};
 enum {InvalidData,MessageQueueFull,AdmissionBudget};
 void assert_owner(){};void drop(int,unsigned=0,int=0){++drops;}void fault(){++faults;}
 void receive_result(MessageQueue::Result result){
'''+rx_drop_branch+'''
 }
'''+body+'''
};
void deliver(Session& s,std::string text){now+=1000;s.message_work.begin();esp_peer_data_frame_t f{text.data(),int(text.size())};Session::data(&f,&s);}
int main(){
 Session s;for(unsigned i=0;i<64;++i)deliver(s,R"({"label":"rtvi-ai","type":"bot-llm-text","data":{"text":"ok"}})");
 assert(s.inbound.depth()==64 && s.faults==0 && s.drops==0);
 deliver(s,R"({"label":"rtvi-ai","type":"bot-llm-text","data":{"text":"overflow"}})");
 assert(s.drops==1 && s.faults==0);deliver(s,"{invalid");assert(s.drops==2 && s.faults==0);
 deliver(s,R"({"type":"signalling","message":{"type":"peerLeft"}})");assert(s.faults==0 && s.drops==3);
 deliver(s,R"({"label":"rtvi-ai","type":"bot-ready","data":{}})");assert(s.faults==0 && s.drops==4);
 // Shape/budget work is bounded without killing the session on the owner.
 deliver(s,std::string(513,'x'));assert(s.drops==5 && s.faults==0);
 Session fresh;fresh.message_work.begin();
 std::string text=R"({"type":"bot-interrupted"})";esp_peer_data_frame_t f{text.data(),int(text.size())};
 for(unsigned i=0;i<17;++i)Session::data(&f,&fresh);
 assert(fresh.inbound.depth()==16 && fresh.drops==1 && fresh.faults==0);
 char messages[32*512]{};MessageQueue q(messages,sizeof(messages),512);
 auto result=q.push(R"({"type":"signalling","message":{"type":"peerLeft"}})",0);
 assert(result==MessageQueue::Result::Queued);
 result=q.push(R"({"label":"rtvi-ai","type":"bot-ready","data":{}})",0);
 assert(result==MessageQueue::Result::ReplacedControl);
 int before=s.drops;s.receive_result(result);assert(s.drops==before);
 s.receive_result(MessageQueue::Result::Full);assert(s.drops==before+1);
}
''')


def test_actual_send_phase_shares_cutoff_and_retains_concurrently_enqueued_fresh_frames(tmp_path):
    cpp=(ROOT/'components/pipecat_esp_peer/src/transport.cpp').read_text()
    body=function(cpp,'void send_audio_phase(SendBudget&')
    compile_run(tmp_path,'''#include "send_budget.h"
#include <atomic>
#include <cassert>
#include <deque>
#include <vector>
#include <functional>
using namespace pipecat::esp_peer;
int64_t now=0;int64_t esp_timer_get_time(){return now;}
struct Packet {uint32_t pts=0;int size=10;int64_t time_us=0;uint8_t data[10]{};};
struct esp_peer_audio_frame_t {uint32_t pts;uint8_t* data;int size;};
struct PeerPart {enum {SendAudio};};struct PeerTimer {explicit PeerTimer(int){}};
struct Trace {std::atomic<unsigned> socket_send_enomem{8};void notify_owner(){}};Trace global_trace;
int native_calls=0,duration=0;bool enomem=false;
int esp_peer_send_audio(void*,esp_peer_audio_frame_t*){++native_calls;now+=duration;if(enomem)++global_trace.socket_send_enomem;return 0;}
struct Queue {std::deque<Packet> q;bool concurrent=false;unsigned pops=0;std::function<void()> produce;
 bool send(Packet& p){if(q.size()>=4)return false;q.push_back(p);return true;}
 bool recv(Packet& p){if(q.empty())return false;p=q.front();q.pop_front();if(concurrent && ++pops>1){Packet fresh;fresh.pts=100+pops;q.push_back(fresh);if(q.size()<4)q.push_back(fresh);if(produce)produce();}return true;}
 unsigned depth()const{return q.size();}};
struct Buffer {Packet packet;Packet* data=&packet;Packet& operator*(){return packet;}};
struct Session {
 bool opened=true,stop=false,remote=false,failed=false;void* peer=nullptr;Trace* trace=&global_trace;
 Queue encoded;Buffer owner_audio;
 std::atomic<unsigned> latency_max_us{0},latency_total_us{0},latency_count{0},send_errors{0},sent{0},bytes_sent{0},send_budget_cutoffs{0},stale_audio_skipped{0};
 enum {EncodedQueue,SendCutoff};std::vector<unsigned> dropped;
 unsigned reasons[2]{};
 void drop(int reason,unsigned pts){++reasons[reason];dropped.push_back(pts);}
 void produce(Packet& packet){if (!encoded.send(packet)) drop(EncodedQueue,packet.pts);else trace->notify_owner();}
'''+body+'''
};
void fill(Session& s){for(unsigned i=0;i<4;++i){Packet p;p.pts=i;s.encoded.q.push_back(p);}}
int main(){
 Session s;fill(s);Packet overflow;overflow.pts=99;s.produce(overflow);
 s.encoded.concurrent=true;s.encoded.produce=[&]{s.produce(overflow);};duration=20000;SendBudget budget;
 s.send_audio_phase(budget,2);assert(native_calls==1 && budget.cutoff && s.stale_audio_skipped==3);
 assert((s.dropped==std::vector<unsigned>{99,99,1,99,2,99,3}));assert(s.encoded.depth()==4);assert(s.reasons[Session::EncodedQueue]==4 && s.reasons[Session::SendCutoff]==3);
 for(auto p:s.encoded.q)assert(p.pts>=100);
 s.send_audio_phase(budget,4-budget.sent);assert(native_calls==1);
 Session fresh;fill(fresh);duration=10;enomem=false;SendBudget shared;
 fresh.send_audio_phase(shared,2);fresh.send_audio_phase(shared,4-shared.sent);assert(shared.sent==4 && fresh.sent==4);
 Session error;fill(error);enomem=true;SendBudget e;error.send_audio_phase(e,2);assert(e.cutoff && error.sent==1);
}
''')


def test_owner_channel_open_disarms_only_native_setup_deadline(tmp_path):
    cpp=(ROOT/'components/pipecat_esp_peer/src/transport.cpp').read_text()
    body=function(cpp,'static int channel_open(esp_peer_data_channel_info_t*')
    compile_run(tmp_path,'''#include <cassert>
#include <cstring>
#include <cstdint>
struct esp_peer_data_channel_info_t {const char* label;unsigned stream_id;};
int64_t esp_timer_get_time(){return 123;}
struct Session {
 bool stop=false,opened=false;unsigned stream=0;int64_t channel_us=0;unsigned owner_assertions=0;
 struct {bool diagnostic_channel_timeout=false;} options;
 struct Trace {int64_t connect_deadline_us=5000;} object;Trace* trace=&object;
 void assert_owner(){++owner_assertions;}
'''+body+'''
};
int main(){
 Session s;esp_peer_data_channel_info_t other{"other",1},chat{"chat",2};
 Session::channel_open(&other,&s);assert(s.trace->connect_deadline_us==5000 && !s.opened);
 s.options.diagnostic_channel_timeout=true;Session::channel_open(&chat,&s);assert(!s.opened);
 s.options.diagnostic_channel_timeout=false;Session::channel_open(&chat,&s);
 assert(s.opened && s.trace->connect_deadline_us==0 && s.stream==2 && s.owner_assertions==3);
}
''')


def test_native_terminal_callback_retires_media_before_notifying_rx(tmp_path):
    cpp=(ROOT/'components/pipecat_esp_peer/src/transport.cpp').read_text()
    retire=function(cpp,'void retire_media()')
    state=function(cpp,'static int state(esp_peer_state_t')
    close=function(cpp,'static int channel_close(esp_peer_data_channel_info_t*')
    compile_run(tmp_path,'''#include <cassert>
using esp_peer_state_t=int;
constexpr int ESP_PEER_STATE_CONNECTED=1,ESP_PEER_STATE_DISCONNECTED=2,
 ESP_PEER_STATE_CONNECT_FAILED=3,ESP_PEER_STATE_DATA_CHANNEL_CLOSED=4,
 ESP_PEER_STATE_DATA_CHANNEL_DISCONNECTED=5;
struct esp_peer_data_channel_info_t {};
struct PeerTrace {enum {Terminal};};
int pacer_wakes=0;void wake(){++pacer_wakes;}void (*dg_audio_media_invalidated)()=wake;
struct Session {
 bool stop=false,remote=false;unsigned assertions=0,events=0;
 struct Playout {int invalidations=0;void disconnect(){++invalidations;}} p;Playout* playout=&p;
 struct Trace {int wakes=0,marks=0;void notify_owner(){++wakes;}void mark(int,unsigned,int){++marks;}} t;Trace* trace=&t;
 void assert_owner(){++assertions;}
 void event(int value){if(value!=ESP_PEER_STATE_CONNECTED)assert(remote && p.invalidations>0);++events;}
'''+retire+'\n'+state+'\n'+close+'''
};
int main(){
 Session s;Session::state(ESP_PEER_STATE_CONNECTED,&s);assert(!s.remote && !s.p.invalidations);
 Session::state(ESP_PEER_STATE_DISCONNECTED,&s);
 assert(s.remote && s.p.invalidations==1 && s.t.wakes==1 && pacer_wakes==1 && s.t.marks==1);
 s.stop=true;Session::channel_close(nullptr,&s);assert(s.p.invalidations==1);
 Session live;Session::channel_close(nullptr,&live);
 assert(live.remote && live.p.invalidations==1 && live.t.wakes==1 && pacer_wakes==2);
}
''')


def test_actual_playback_selftest_guard_runs_before_park_and_unparks_on_error(tmp_path):
    cpp=(ROOT/'main/console.cpp').read_text()
    branch=function(cpp,'else if (!std::strcmp(command, "playback-selftest"))')
    compile_run(tmp_path,'''#include <cassert>
#include <cstring>
#include <stdexcept>
#include <string>
namespace pipecat {
enum class TransportState {Disconnected,Initialized,Connecting,Connected};
namespace esp_peer {
 bool fail=false;int calls=0;
 struct Json {std::string dump(){return "{}";}};
 Json playback_selftest(){++calls;if(fail)throw std::runtime_error("fixture failure");return {};}
}}
struct Client {pipecat::TransportState current;auto state(){return current;}};
struct Pipeline {int parks=0;bool paused=false;void benchmark_idle(bool value){paused=value;if(value)++parks;}};
void console_line(const char*){}
void invoke(Client& client,Pipeline& pipeline,bool tone_running){
 const char* command="playback-selftest";if(false){}
'''+branch+'''
}
int main(){
 Client c{pipecat::TransportState::Connected};Pipeline p;
 try{invoke(c,p,false);assert(false);}catch(const std::runtime_error&){}
 assert(!p.parks && !pipecat::esp_peer::calls);
 c.current=pipecat::TransportState::Disconnected;
 try{invoke(c,p,true);assert(false);}catch(const std::runtime_error&){}
 assert(!p.parks);
 invoke(c,p,false);assert(p.parks==1 && !p.paused);
 pipecat::esp_peer::fail=true;c.current=pipecat::TransportState::Initialized;
 try{invoke(c,p,false);assert(false);}catch(const std::runtime_error&){}
 assert(p.parks==2 && !p.paused);
}
''')


def test_retire_media_from_rx_while_owner_native_loop_is_running(tmp_path):
    cpp=(ROOT/'components/pipecat_esp_peer/src/transport.cpp').read_text()
    retire=function(cpp,'void retire_media()')
    send=function(cpp,'void send_audio_phase(SendBudget&')
    native=cpp[cpp.index('{PeerTimer timer(PeerPart::Main);if (esp_peer_main_loop(peer)) fault();}'):]
    native=native[:native.index('\n')]
    compile_run(tmp_path,'''#include "send_budget.h"
#include <atomic>
#include <thread>
#include <cassert>
#include <algorithm>
using namespace pipecat::esp_peer;
std::atomic<bool> in_native{false},release_native{false};
int esp_peer_main_loop(void*){in_native=true;while(!release_native)std::this_thread::yield();return 0;}
int64_t esp_timer_get_time(){return 10000;}
struct PeerPart {enum {Main,SendAudio};};struct PeerTimer {explicit PeerTimer(int){}};
struct Packet {unsigned pts=1;int size=10,time_us=0;unsigned char data[10];};
struct esp_peer_audio_frame_t {unsigned pts;unsigned char* data;int size;};
std::atomic<unsigned> native_sends{0};int esp_peer_send_audio(void*,esp_peer_audio_frame_t*){++native_sends;return 0;}
std::atomic<unsigned> playback_wakes{0};void wake(){++playback_wakes;}void (*dg_audio_media_invalidated)()=wake;
struct Session {
 std::atomic<bool> opened{true},stop{false},remote{false},failed{false};void* peer=nullptr;
 struct Playout {std::atomic<unsigned> invalidations{0};void disconnect(){++invalidations;}} p;Playout* playout=&p;
 struct Trace {std::atomic<unsigned> socket_send_enomem{0},wakes{0};void notify_owner(){++wakes;}} t;Trace* trace=&t;
 struct Queue {bool recv(Packet&){return true;}unsigned depth(){return 0;}} encoded;
 struct Buffer {Packet packet;Packet* data=&packet;Packet& operator*(){return packet;}} owner_audio;
 std::atomic<unsigned> latency_max_us{0},latency_total_us{0},latency_count{0},send_errors{0},sent{0},bytes_sent{0},send_budget_cutoffs{0},stale_audio_skipped{0};
 enum {SendCutoff};void drop(int,unsigned){}
'''+retire+'''
 void fault(){failed=true;retire_media();}
'''+send+'''
 void owner_iteration(){SendBudget budget;
'''+native+'''
 send_audio_phase(budget,4);}
};
int main(){
 Session s;std::thread owner([&]{s.owner_iteration();});
 while(!in_native)std::this_thread::yield();
 std::thread rx([&]{s.retire_media();release_native=true;});rx.join();owner.join();
 assert(s.remote && s.p.invalidations==1 && s.t.wakes==1 && playback_wakes==1);
 assert(native_sends==0 && s.sent==0);
}
''',flags=('-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer'))
