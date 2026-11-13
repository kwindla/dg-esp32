#include "app.h"
#include "liveness.h"
#include "start_request.h"
#include <algorithm>
namespace dg {
const char* state_name(State s) {
    switch(s) {case State::Boot:return "Boot";case State::WifiConnecting:return "WifiConnecting";
    case State::Idle:return "Idle";case State::Starting:return "Starting";case State::Connected:return "Connected";
    case State::Ready:return "Ready";case State::Disconnected:return "Disconnected";} return "Unknown";
}
std::optional<DemoPhase> demo_transition(State previous,State current) {
    if(previous==current)return {};
    if(current==State::Starting)return DemoPhase::Connecting;
    if(current==State::Ready)return DemoPhase::Live;
    if(current==State::Connected)return {}; // Still connecting until bot-ready.
    if(previous==State::Starting || previous==State::Connected || previous==State::Ready)
        return DemoPhase::Disconnected;
    return {};
}
void StateMachine::stop(Actions& actions) {
    if(session_) {session_=false;stopping_=true;++generation_;actions.add(ActionKind::Stop,generation_);}
}
void StateMachine::advance(Actions& a,int64_t now) {
    if(stopping_)return;
    if(!wifi_) {
        state_=State::WifiConnecting;
        if(!joining_ && now>=wifi_retry_at_) {joining_=true;a.add(ActionKind::JoinWifi,generation_);}
    } else if(desired_ && !session_ && now>=retry_at_) {
        session_=true;++generation_;state_=State::Starting;a.add(ActionKind::Start,generation_);
    } else if(!desired_ && !session_)state_=State::Idle;
}
Actions StateMachine::dispatch(const Event& e,int64_t now) {
    Actions a;
    switch(e.kind) {
    case EventKind::Boot:state_=State::WifiConnecting;break;
    case EventKind::Connect:desired_=true;retry_at_=0;backoff_=1000;break;
    case EventKind::Reconnect:
        if(state_!=State::Ready || e.generation!=generation_)return a;
        desired_=true;stop(a);state_=State::Disconnected;retry_at_=now+300;backoff_=1000;break;
    case EventKind::Disconnect:desired_=false;retry_at_=0;stop(a);state_=wifi_?State::Idle:State::WifiConnecting;break;
    case EventKind::WifiDown:case EventKind::IpChanged:
        if(e.network_at_us && e.network_at_us<=wifi_up_at_us_)return a;
        wifi_=false;wifi_retry_at_=now;stop(a);state_=State::WifiConnecting;break;
    case EventKind::WifiUp:wifi_up_at_us_=std::max(wifi_up_at_us_,e.network_at_us);wifi_=true;joining_=false;wifi_backoff_=1000;if(!session_)state_=State::Idle;break;
    case EventKind::WifiFailed:
        joining_=false;wifi_=false;wifi_retry_at_=now+wifi_backoff_;wifi_backoff_=std::min(30000u,wifi_backoff_*2);break;
    case EventKind::Connected:
        if(accepts(e.generation) && state_==State::Starting)state_=State::Connected;
        break;
    case EventKind::Ready:
        if(accepts(e.generation) && (state_==State::Starting || state_==State::Connected)) {
            state_=State::Ready;ready_at_=now;
        }break;
    case EventKind::Failure:
        if(e.generation==generation_ && stopping_) {stopping_=false;break;}
        if(e.generation==generation_ && joining_ && !session_) {
            joining_=false;wifi_retry_at_=now+wifi_backoff_;
            wifi_backoff_=std::min(30000u,wifi_backoff_*2);break;
        }
        if(!accepts(e.generation))return a;
        if(state_==State::Ready && now-ready_at_>=60000)backoff_=1000;
        stop(a);state_=State::Disconnected;retry_at_=now+backoff_;backoff_=std::min(30000u,backoff_*2);break;
    case EventKind::Stopped:
        if(e.generation!=generation_)return a;
        stopping_=false;break;
    case EventKind::Tick:
        if(state_==State::Ready && now-ready_at_>=60000)backoff_=1000;
        break;
    default: return a;
    }
    advance(a,now);return a;
}
}
#ifndef DG_APP_HOST
#include "dg_net.h"
#if CONFIG_DG_DUO
#include "duo_ui.h"
#include "duo_touch.h"
#endif
#include "dg_display/runtime.h"
#include "audio_slot.h"
#include "app_wait.h"
#include "app_events.h"
#include "dg_audio/pipeline.h"
#include "pipecat/client.h"
#include "pipecat/esp/platform.h"
#include "pipecat/esp_peer/transport.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "pthread_config.h"
#include "sdkconfig.h"
#include "dg_diagnostics.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <mutex>
#include <thread>
#include <chrono>
#include <cstdarg>
bool app_console_line(const char* text) noexcept;
namespace dg::detail {
static const char* TAG="dg-app";
static void app_log(const char* format,...) {
    char line[1280];int prefix=std::snprintf(line,sizeof(line),"\nI (%lu) dg-app: ",static_cast<unsigned long>(esp_log_timestamp()));
    va_list args;va_start(args,format);
    std::vsnprintf(line+prefix,sizeof(line)-prefix-2,format,args);va_end(args);
    auto length=std::strlen(line);line[length]='\n';line[length+1]='\0';app_console_line(line);
}
// Each core client owns a forwarding view. The app owns the transport workers.
// disconnect() joins all session jobs, and client destruction drains its event
// loop, before another view can initialize a new immutable observer binding.
struct TransportView final:pipecat::Transport {
    pipecat::EspPeerTransport& peer;
    explicit TransportView(pipecat::EspPeerTransport& p):peer(p){}
    void initialize(pipecat::TransportObserver* o) override {peer.initialize(o);}
    void set_start_bot_params(const pipecat::APIRequest& r) override {peer.set_start_bot_params(r);}
    void connect(const nlohmann::json& p) override {peer.connect(p);}
    void disconnect() override {peer.disconnect();}
    void send_ready_message(const pipecat::rtvi::Message& m) override {peer.send_ready_message(m);}
    void send_message(const pipecat::rtvi::Message& m) override {peer.send_message(m);}
    size_t max_message_size() const override {return peer.max_message_size();}
    int32_t send_user_audio(const int16_t* p,size_t n) override {return peer.send_user_audio(p,n);}
    int32_t read_bot_audio(int16_t* p,size_t n) override {return peer.read_bot_audio(p,n);}
};
struct AppImpl;
struct Callbacks final:pipecat::PipecatClientCallbacks {
    AppImpl& app;const uint32_t generation;
    Callbacks(AppImpl& a,uint32_t g):app(a),generation(g){}
    void post(EventKind kind,const char* text="");
    void on_connected() override {post(EventKind::Connected);}
    void on_bot_ready(const pipecat::rtvi::BotReadyData&) override {post(EventKind::Ready);}
    void on_disconnected() override {post(EventKind::Failure,"remote disconnected");}
    void on_error(const pipecat::rtvi::ErrorData& d) override {if(d.fatal)post(EventKind::Failure,d.error.c_str());}
    void on_user_started_speaking() override {post(EventKind::UserStart);}
    void on_user_stopped_speaking() override {post(EventKind::UserStop);}
    void on_user_transcript(const pipecat::rtvi::TranscriptData& d) override {post(EventKind::UserText,d.text.c_str());}
    void on_bot_tts_text(const pipecat::rtvi::BotTTSTextData& d) override {post(EventKind::BotText,d.text.c_str());}
    void on_bot_started_speaking() override {post(EventKind::BotStart);}
    void on_bot_interrupted() override;
    void on_bot_stopped_speaking() override {post(EventKind::BotStop);}
    void on_server_message(const nlohmann::json& d) override {try {post(EventKind::Server,d.dump().c_str());}catch(...) {post(EventKind::Failure,"server message allocation");}}
    void on_metrics(const pipecat::rtvi::MetricsData& d) override {try {post(EventKind::Metrics,d.raw.dump().c_str());}catch(...) {post(EventKind::Failure,"metrics allocation");}}
};
struct Session {
    Callbacks callbacks;
    pipecat::EspPeerTransport* transport=nullptr;
    std::unique_ptr<pipecat::PipecatClient> client;
    Session(AppImpl& app,uint32_t gen,pipecat::EspPeerTransport& peer,bool fault):callbacks(app,gen),transport(&peer) {
        pipecat::esp::configure_threads();
        pipecat::PipecatClientOptions options;
        if(fault)peer.fault_next_channel_open();
        options.transport=std::make_unique<TransportView>(peer);options.callbacks=&callbacks;
        client=std::make_unique<pipecat::PipecatClient>(std::move(options));client->initialize();
    }
};
struct AppImpl {
    struct DisplayAck {uint32_t generation=0;dg_display::WireReply reply;};
    App::Hooks hooks;StateMachine model;
#if CONFIG_DG_DUO
    DuoUi duo;
    DuoTouch touch{[](void* p,bool injected,uint32_t gen){
        Event e{EventKind::DuoCommand,gen};e.text[0]='2';e.text[1]=char(injected);
        return static_cast<AppImpl*>(p)->post(e);},this};
#endif
    StaticQueue_t event_control{},work_control{},ack_control{};
    uint8_t* event_storage=nullptr;uint8_t* work_storage=nullptr;uint8_t* ack_storage=nullptr;
    QueueHandle_t events=nullptr,work=nullptr,acks=nullptr;
    std::thread control,worker;
    std::atomic<bool> quit{false},next_fault{false};
    PendingActions pending;
    std::atomic<unsigned> commands_pending{0},data_event_drops{0};
    std::atomic<int64_t> wifi_down_at{0},ip_changed_at{0};
    std::atomic<uint32_t> overflow_generation{0};
    std::atomic<State> state{State::Boot};std::atomic<uint32_t> generation{0};
    std::mutex mutex;AppWait waiters;
    std::unique_ptr<pipecat::EspPeerTransport> transport;
    std::shared_ptr<Session> session;
    AudioSlot<pipecat::EspPeerTransport> media;
    std::string server_url=CONFIG_DG_SERVER_URL;
    std::string last_stats="{}";unsigned turn=0;DeviceLiveness liveness;
    explicit AppImpl(App::Hooks h,std::string url):hooks(std::move(h)) {
        if(!url.empty())server_url=std::move(url);
        event_storage=static_cast<uint8_t*>(heap_caps_malloc(32*sizeof(Event),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        work_storage=static_cast<uint8_t*>(heap_caps_malloc(8*sizeof(Action),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        ack_storage=static_cast<uint8_t*>(heap_caps_malloc(8*sizeof(DisplayAck),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
        if(!event_storage || !work_storage || !ack_storage) {free(event_storage);free(work_storage);free(ack_storage);throw std::bad_alloc();}
        events=xQueueCreateStatic(32,sizeof(Event),event_storage,&event_control);
        work=xQueueCreateStatic(8,sizeof(Action),work_storage,&work_control);
        acks=xQueueCreateStatic(8,sizeof(DisplayAck),ack_storage,&ack_control);
        pipecat::EspPeerTransportOptions peer;
        peer.render_clock=[] {return dg::render_tick;};
        peer.loop_delay_ms=CONFIG_DG_PEER_LOOP_DELAY_MS;peer.agent_recv_timeout_ms=CONFIG_DG_PEER_RECV_TIMEOUT_MS;
        peer.jitter_cache_timeout_ms=CONFIG_DG_PEER_JITTER_TIMEOUT_MS;peer.opus_bitrate=CONFIG_DG_OPUS_BITRATE;
#if CONFIG_DG_CODEC_EAGER_INITIALIZATION
        peer.eager_codec_initialization=true;
#endif
#if CONFIG_DG_OPUS_FEC
        peer.opus_fec=true;
#else
        peer.opus_fec=false;
#endif
        peer.owner_priority=CONFIG_DG_PEER_OWNER_PRIORITY;
#if CONFIG_DG_OPUS_UPLINK_FRAME40
        peer.uplink_frame_ms=40;
#endif
#if CONFIG_DG_PEER_FULL_CALL_TIMING
        peer.full_call_timing=true;
#endif
#if CONFIG_DG_PEER_ARRIVAL_TRACE
        peer.arrival_trace=true;
#endif
#if CONFIG_DG_PEER_DIRECT_SOCKET_SELECT
        peer.direct_socket_select=true;
#endif

        peer.playout_target_frames=CONFIG_DG_PLAYOUT_TARGET_FRAMES;peer.playout_backlog_frames=CONFIG_DG_PLAYOUT_MAX_TARGET_FRAMES;
        transport=std::make_unique<pipecat::EspPeerTransport>(peer);
#if CONFIG_DG_DUO
        touch.start();
#endif
        PthreadConfig threads;auto old=threads.current();
        auto cfg=old;cfg.pin_to_core=0;cfg.prio=4;cfg.stack_size=16384;cfg.thread_name="app-control";cfg.stack_alloc_caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
        threads.set(cfg);control=std::thread([this]{control_run();});
        cfg.stack_size=16384;cfg.thread_name="app-session";
        threads.set(cfg);worker=std::thread([this]{worker_run();});
        dg_net_set_disconnect_callback([](unsigned,void* p){static_cast<AppImpl*>(p)->wifi_down_at.store(esp_timer_get_time());},this);
        dg_net_set_ip_callback([](bool changed,void* p){if(changed)static_cast<AppImpl*>(p)->ip_changed_at.store(esp_timer_get_time());},this);
        post({EventKind::Boot});
    }
    ~AppImpl() {
#if CONFIG_DG_DUO
        touch.stop();
#endif
        dg_net_set_disconnect_callback(nullptr,nullptr);dg_net_set_ip_callback(nullptr,nullptr);
        command(EventKind::Disconnect);
        waiters.wait(std::chrono::seconds(40),[&]{return !busy();});
        quit=true;worker.join();control.join();hooks.stop();hooks.join_producers();media.clear();
        while(!media.idle())std::this_thread::sleep_for(std::chrono::milliseconds(1));
        transport->disconnect();
        session.reset();vQueueDelete(events);vQueueDelete(work);vQueueDelete(acks);free(event_storage);free(work_storage);free(ack_storage);
    }
    std::shared_ptr<Session> current() {std::lock_guard<std::mutex> lock(mutex);return session;}
    bool post(const Event& e) {
        // Reserve eight slots for lifecycle/worker completions. Data overload
        // is counted shedding, never a synthetic terminal session failure.
        if(droppable_event(e) && uxQueueSpacesAvailable(events)<=8){++data_event_drops;return false;}
        if(xQueueSend(events,&e,0)!=pdTRUE){if(droppable_event(e))++data_event_drops;else if(auto g=overflow_session(e))overflow_generation=g;return false;}
        return true;
    }
    bool busy() const {return commands_pending.load()!=0 || pending.busy();}
    void command(EventKind kind) {++commands_pending;complete(kind);}
    __attribute__((noinline)) void complete(EventKind kind,uint32_t gen=0,const char* text="",int64_t network_at=0) {
        // Keep the 1 KiB queue item out of the session worker's already deep
        // client/transport call stack, and reuse this frame for all completions.
        auto e=text_event(kind,gen,text);e.network_at_us=network_at;xQueueSend(events,&e,portMAX_DELAY);
    }
    void publish() {
        if(state!=model.state() || generation!=model.generation()) {
            state=model.state();generation=model.generation();waiters.changed();
        }
    }
    void handle_event(const Event& e) {
        bool accepts=model.accepts(e.generation);
        auto previous=model.state();auto previous_gen=model.generation();
        auto now=esp_timer_get_time()/1000;auto actions=model.dispatch(e,now);
        if(e.kind==EventKind::Failure && accepts)app_log("SESSION FAIL gen=%u reason=%s retry_ms=%lld",e.generation,e.text,(long long)(model.retry_at()-now));
        if(previous!=model.state() || previous_gen!=model.generation())app_log("STATE %s gen=%u desired=%d t=%lld",state_name(model.state()),model.generation(),model.desired_connected(),(long long)esp_timer_get_time());
#if CONFIG_DG_DUO
        if(e.kind==EventKind::DuoCommand && e.generation==model.generation()){
            if(e.text[0]=='0')duo.status();
            else if(e.text[0]=='1')duo.touch_enabled(e.text[1]!=0);
            else if(e.text[0]=='2')duo.tap(e.text[1]!=0,now);
        }
#endif
        if(accepts) {
            switch(e.kind) {
#if CONFIG_DG_DUO
            case EventKind::DuoSendFailed:duo.device.error(dg_duo::Fault::Send);break;
#endif
            case EventKind::BotStart:transport->resume_bot_audio();hooks.speaking(true);break;
            case EventKind::BotInterrupt: {
                const auto at=esp_timer_get_time();
                bool applied=transport->cancel_bot_audio(at);
                app_log("AUDIO_CANCEL callback_us=%lld device_us=%lld gen=%u applied=%d mute_ms=150",(long long)e.network_at_us,(long long)at,e.generation,int(applied));
                if(applied)hooks.speaking(false);
                break;
            }
            case EventKind::BotStop:hooks.speaking(false);break;
            case EventKind::Ready:if(previous==State::Ready)break;
                liveness.ready(e.generation,esp_timer_get_time());
                try {hooks.ready();}catch(...) {post(text_event(EventKind::Failure,e.generation,"audio ready hook failed"));break;}
                app_log("on_bot_ready t=%lld",(long long)esp_timer_get_time());dg_bot_ready();{
                uint64_t epoch=0,next=0;
                if(dg_display::wire_ready(epoch,next))if(auto session=current())
                    session->client->send_client_message("display-ready",{{"v",1},{"epoch",epoch},{"next_seq",next},{"wire_bytes",1023},{"max_ops",8}});
                }break;
            case EventKind::UserStart:++turn;break;
            case EventKind::UserText:app_log("RTVI user[%u]: %s",turn,e.text);break;
            case EventKind::BotText:app_log("RTVI bot[%u]: %s",turn,e.text);break;
            case EventKind::Server: {
#if CONFIG_DG_DUO
                if(duo.receive(e.text,e.truncated,e.generation,now))break;
#endif
                if(liveness.receive(e.text,e.truncated,esp_timer_get_time())!=DeviceLiveness::Ping::NotPing)break;
                dg_display::WireReply reply;
                if(dg_display::server_message(e.text,e.truncated,reply)){
                    const DisplayAck ack{e.generation,reply};
                    if(xQueueSend(acks,&ack,0)!=pdTRUE)dg_display::wire_ack_outcome(dg_display::WireAckOutcome::QueueFull);
                    break;
                }
                app_log("SERVER %s",e.text);
                try {
                    TurnTranscript transcript;
                    if(turn_transcript(e,transcript)) {
                        turn=transcript.turn;
                        app_log("RTVI user[%u]: %s",turn,transcript.text.c_str());
                    } else app_log("SERVER ignored message truncated=%d",e.truncated);
                }catch(...) {app_log("SERVER ignored parse error");}
                break;
            }
            case EventKind::Metrics:ESP_LOGI(TAG,"METRICS %s",e.text);break;
            default:break;
            }
        }
        for(size_t i=0;i<actions.count;++i) {
            const auto& a=actions.items[i];
            if(a.kind==ActionKind::Stop) {
                try {stop_capture(*transport,[&]{hooks.stop();});}catch(...) {app_log("STOP hook error");}
            }
            if(a.kind==ActionKind::Start)turn=0;
            pending.add(a);
        }
#if CONFIG_DG_DUO
        if(previous_gen!=model.generation() || (previous!=State::Ready && model.state()==State::Ready))
            duo.lifecycle(model.state()==State::Ready,model.generation(),now);
        touch.admission(duo.accepts_touch(),model.generation());
#endif
        publish();
        if(model.state()==State::Ready) {
            const auto silence=liveness.silence(esp_timer_get_time());
            if(silence!=DeviceLiveness::Silence::None)
                app_log("LIVENESS no ping for 15s gen=%u pings=%u device_us=%lld",model.generation(),liveness.pings.load(),(long long)esp_timer_get_time());
            // The normal retry reconnects a lost session.
            if(silence==DeviceLiveness::Silence::Lost)post(text_event(EventKind::Failure,model.generation(),"no bot ping for 15s"));
        }
        if(auto phase=demo_transition(previous,model.state());phase && hooks.lifecycle) {
            try {hooks.lifecycle(*phase,model.generation());}
            catch(...) {app_log("LIFECYCLE hook error");}
        }
        if(e.kind==EventKind::Connect || e.kind==EventKind::Disconnect) {
            --commands_pending;waiters.changed();
        }
    }
    void control_run() {
        app_log("STATE Boot gen=0");
        while(!quit || pending.busy()) {
            Event e; if(xQueueReceive(events,&e,pdMS_TO_TICKS(20))!=pdTRUE)e={EventKind::Tick};
            event_and_overflow(e,overflow_generation.exchange(0),[this](const Event& event){handle_event(event);});
            for(auto entry:{std::pair{EventKind::WifiDown,wifi_down_at.exchange(0)},
                            std::pair{EventKind::IpChanged,ip_changed_at.exchange(0)}}) {
                if(entry.second) {Event flag{entry.first};flag.network_at_us=entry.second;handle_event(flag);}
            }
#if CONFIG_DG_DUO
            duo.tick(esp_timer_get_time()/1000);
#endif
            pending.flush([this](const Action& a){return xQueueSend(work,&a,0)==pdTRUE;});
        }
    }
    void send_pong() {
        auto token=liveness.take();if(!token)return;
        auto gen=uint32_t(token>>32),seq=uint32_t(token);
        try {
            auto s=current();if(!s || !session_work_current(gen,s->callbacks.generation,generation,state==State::Ready))return;
            s->client->send_client_message("pong",{{"seq",seq}});++liveness.pongs;
        }catch(...){app_log("LIVENESS pong send failed");}
    }
    void send_display_ack() {
        // One ack per idle worker iteration; session start/stop actions win.
        // The queue is finite, in PSRAM, and never blocks app-control.
        DisplayAck ack;
        if(xQueueReceive(acks,&ack,0)!=pdTRUE)return;
        try {
            auto s=current();
            if(!s || !session_work_current(ack.generation,s->callbacks.generation,generation,state==State::Ready)){
                dg_display::wire_ack_outcome(dg_display::WireAckOutcome::Stale);return;
            }
            const auto& r=ack.reply;
            s->client->send_client_message("display-result",{{"id",r.id},{"epoch",r.epoch},{"seq",r.sequence},{"accepted",r.accepted},{"error",dg_display::error_name(r.error)},{"duplicate",r.duplicate},{"parse_failed",r.parse_failed},{"wait_us",r.wait_us}});
            dg_display::wire_ack_outcome(dg_display::WireAckOutcome::Sent);
        }catch(...){dg_display::wire_ack_outcome(dg_display::WireAckOutcome::SendFailed);}
    }
#if CONFIG_DG_DUO
    void send_duo() {
        dg_duo::ClientMessage m;if(!duo.outbound.take(m))return;
        try{
            auto s=current();
            if(!s || !dg_duo::Mailbox::current(m,s->callbacks.generation,generation,state==State::Ready))return;
            s->client->send_client_message("duo",{{"v",1},{"op",m.op==dg_duo::Op::Hello?"hello":"toggle"},
                {"session",m.session},{"seq",m.seq},{"have",dg_duo::name(m.have)}});
        }catch(...){post(Event{EventKind::DuoSendFailed,m.generation});}
    }
#endif
    void worker_run() {
        while(!quit || pending.busy()) {
            Action a{};if(xQueueReceive(work,&a,pdMS_TO_TICKS(20))!=pdTRUE){transport->drain_wire_evidence();send_pong();send_display_ack();
#if CONFIG_DG_DUO
                send_duo();
#endif
                continue;}
            // Declared outside try so errors are logged before completion.
            ScopeExit completion([&] {
                if(a.kind==ActionKind::Stop)complete(EventKind::Stopped,a.generation);
                pending.completed();waiters.changed();
            });
            try {
                if(a.kind==ActionKind::JoinWifi) {
                    auto error=dg_net_wifi_connect(20000);
                    if(error==ESP_OK) {app_log("WIFI READY t=%lld",(long long)esp_timer_get_time());complete(EventKind::WifiUp,0,"",esp_timer_get_time());}
                    else complete(EventKind::WifiFailed);
                } else if(a.kind==ActionKind::Start) {
                    {std::lock_guard<std::mutex> lock(mutex);session.reset();}
                    hooks.before_connect();
                    auto s=std::make_shared<Session>(*this,a.generation,*transport,next_fault.exchange(false));
                    {std::lock_guard<std::mutex> lock(mutex);std::string("{}").swap(last_stats);session=s;}
                    media.publish(s->transport);
                    pipecat::APIRequest request;{std::lock_guard<std::mutex> lock(mutex);request.endpoint=dg_net::http_url_join(server_url,"/start");}
                    const auto audio=hooks.audio_capability?hooks.audio_capability():dg_audio::Capability{};
                    request.request_data=start_request(audio);
                    app_log("AUDIO_CAPABILITY aec=%d ref_gain_db=%u guard_ms=%u",audio.aec,audio.ref_gain_db,audio.guard_ms);
                    // HTTPS (public demo endpoint): the S3 TLS handshake alone takes ~10 s.
                    request.timeout=std::chrono::seconds(request.endpoint.rfind("https://",0)==0?30:10);
                    s->client->start_bot_and_connect(request);complete(EventKind::Ready,a.generation);
                } else {
                    auto cleanup=[&](auto fn) {
                        try {fn();}catch(const std::exception& e){app_log("STOP cleanup error: %s",e.what());}
                        catch(...){app_log("STOP cleanup error");}
                    };
                    cleanup([&]{hooks.join_producers();});
                    media.clear();
                    while(!media.idle())std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    std::shared_ptr<Session> s;
                    {std::lock_guard<std::mutex> lock(mutex);s=std::move(session);}
                    // Reject new console/API borrows and drain existing ones
                    // before retiring this core client. Its forwarding view
                    // must never survive into the next transport session.
                    while(s && s.use_count()>1)std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    if(s) {cleanup([&]{s->client->disconnect_bot();});cleanup([&]{s->client->disconnect();});}
                    // The core can already be Disconnected after a remote
                    // failure and skip its transport call. Always join the
                    // actual session before destroying its observer/client.
                    cleanup([&]{transport->disconnect();});
                    if(s) {
                        cleanup([&]{auto snapshot=s->transport->stats_dump();
                        std::lock_guard<std::mutex> lock(mutex);last_stats=std::move(snapshot);});
                        // Destroy the closed client and join its core threads
                        // before publishing teardown completion.
                        s.reset();}

                }
            } catch(const std::exception& error) {
                app_log("Worker error: %s",error.what());
                if(a.kind==ActionKind::JoinWifi)complete(EventKind::WifiFailed);
                else if(a.kind==ActionKind::Start)complete(EventKind::Failure,a.generation,error.what());
            } catch(...) {
                app_log("Worker error: session operation failed");
                if(a.kind==ActionKind::JoinWifi)complete(EventKind::WifiFailed);
                else if(a.kind==ActionKind::Start)complete(EventKind::Failure,a.generation,"session operation failed");
            }
        }
    }
    template<class Predicate> void wait(Predicate done,unsigned seconds) {
        if(!waiters.wait(std::chrono::seconds(seconds),done))throw std::runtime_error("App command timed out; policy continues");
    }
};
void Callbacks::on_bot_interrupted() {
    Event e{EventKind::BotInterrupt,generation};e.network_at_us=esp_timer_get_time();app.post(e);
}
void Callbacks::post(EventKind kind,const char* text) {
    app.post(text_event(kind,generation,text));
}
}
namespace dg {
App::App(Hooks h,std::string url):impl_(new detail::AppImpl(std::move(h),std::move(url))){}
App::~App()=default;
#if CONFIG_DG_DUO
bool App::duo_command(unsigned command,unsigned argument){
    Event e{EventKind::DuoCommand,impl_->generation.load()};e.text[0]=char('0'+command);e.text[1]=char(command==2?1:argument);
    return impl_->post(e);
}
#endif
void App::server_url(const std::string& url) {
    if(!idle())throw std::runtime_error("URL requires idle app");
    if(url.rfind("http://",0)!=0 && url.rfind("https://",0)!=0)throw std::runtime_error("HTTP URL required");
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->server_url=url;
}
void App::request_connect() {impl_->command(EventKind::Connect);}
bool App::request_reconnect() {
    const auto generation=impl_->generation.load();
    const auto event=reconnect_event(impl_->state.load(),generation);
    return event && impl_->post(*event);
}
void App::connect() {impl_->command(EventKind::Connect);impl_->wait([&]{return impl_->state==State::Ready && !impl_->busy();},90);}
void App::disconnect() {impl_->command(EventKind::Disconnect);impl_->wait([&]{return impl_->state!=State::Ready && impl_->state!=State::Starting && impl_->state!=State::Connected && !impl_->busy();},40);}
void App::wait_wifi() {impl_->wait([&]{return impl_->state==State::Idle && !impl_->busy();},60);}
void App::fault_next_channel() {impl_->next_fault=true;}
bool App::ready() const {return impl_->state==State::Ready;}
bool App::idle() const {return impl_->state==State::Idle && !impl_->busy();}
int App::send_audio(const int16_t* pcm,size_t n) {auto lease=impl_->media.borrow();return lease.pointer?lease.pointer->send_user_audio(pcm,n):0;}
bool App::audio_ready() {auto lease=impl_->media.borrow();return lease.pointer && lease.pointer->media_ready();}
int App::read_audio(int16_t* pcm,size_t n) {MediaToken token;return read_audio(pcm,n,token);}
int App::read_audio(int16_t* pcm,size_t n,MediaToken& token) {
    token={};auto lease=impl_->media.borrow();
    return lease.pointer?lease.pointer->read_bot_audio(pcm,n,token):0;
}
void App::with_client(const std::function<void(pipecat::PipecatClient&)>& fn) {auto s=impl_->current();if(!s)throw std::runtime_error("No session");fn(*s->client);}
std::string App::stats() {
    auto s=impl_->current();std::string result;
    if(s)result=s->transport->stats_dump();
    else {std::lock_guard<std::mutex> lock(impl_->mutex);result=impl_->last_stats;}
    if(!result.empty() && result.back()=='}')result.insert(result.size()-1,std::string(result.size()>2?",":"")+"\"app_data_drops\":"+std::to_string(impl_->data_event_drops.load()));
    if(!result.empty() && result.back()=='}')result.insert(result.size()-1,
        ",\"liveness\":{\"pings\":"+std::to_string(impl_->liveness.pings.load())+
        ",\"pongs\":"+std::to_string(impl_->liveness.pongs.load())+
        ",\"timeouts\":"+std::to_string(impl_->liveness.timeouts.load())+"}");
    return result;
}
void App::dump_uplink_intervals() {impl_->transport->dump_uplink_intervals();}
size_t App::drain_trace(void* records,size_t capacity) {return impl_->transport->drain_trace(records,capacity);}
void App::diagnostic_option(const std::string& name,unsigned value) {
    if(!idle())throw std::runtime_error("Options require Idle app");
    impl_->transport->diagnostic_option(name,value);
}
}
#endif
