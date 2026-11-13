#include "dg_audio/aec_config.h"
#include "dg_net.h"
#include "dg_display.h"
#include "dg_display/runtime.h"
#include "app.h"
#include "audio_slot.h"
#include "rx_recording.h"
#include "fixture_load.h"
#include "demo_ui.h"
#include "app_events.h"
#include "demo_boot.h"
#include "sdkconfig.h"
#if CONFIG_DG_DUO
#include "duo_boot.h"
#include "dg_display/duo_runtime.h"
#include "dg_board/touch.h"
#endif
// Preserve the legacy certificate ESP_ERROR_CHECK source-line constant.
#line 9
#include "dg_audio/reference_gain.h"
#include "dg_board/power.h"
#include "netprobe.h"
#include "performance.h"
#include "sdkconfig.h"
#include "dg_audio/aec.h"
#if CONFIG_DG_CACHE_COUNTERS
#include "cache_metrics.h"
#endif
#include "esp_wifi.h"
#include "dg_diagnostics.h"
#include "dg_console.h"
#include "dg_dump_check.h"
#include "dg_dump_output.h"
#include "dg_board/board.h"
#include "dg_board/audio_hw.h"
#include "console_support.h"
#include "dg_display/backend_commands.h"
#include "dg_audio/pipeline.h"
#include "pipecat/client.h"
#include "pipecat/errors.h"
#include "pipecat/esp/platform.h"
#include "pipecat/esp_peer/transport.h"
#include "pipecat/esp_peer/codec_benchmark.h"
#include "pipecat/esp_peer/stat_json.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "pthread_config.h"
#include "esp_app_desc.h"
#include "esp_peer.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "esp_memory_utils.h"
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <chrono>
#include <thread>
#include <vector>
extern const uint8_t speech_start[] asm("_binary_prompt_france_wav_start");
extern const uint8_t speech_end[] asm("_binary_prompt_france_wav_end");
extern const uint8_t silence_start[] asm("_binary_lean_resume_opus_start");
extern const uint8_t silence_end[] asm("_binary_lean_resume_opus_end");
static const char* TAG = "dg-esp32";
struct UnsupportedCommand {const char* build=nullptr;};
// Queue whole evidence/ack records. Unplugging USB must never stop a session.
static void console_line(const char* text) {
    const bool display_perf=std::strstr(text,"\nPERF ")!=nullptr;
    if(display_perf)dg_display::runtime_phase_begin(dg_display::RuntimePhase::Perf);
    struct EndPhase {bool active;~EndPhase(){if(active)dg_display::runtime_phase_end(dg_display::RuntimePhase::Perf);}} end_phase{display_perf};
    // Callers historically framed each complete line with both a leading and
    // trailing LF. Consecutive PCM/MIC_PCM/ARRIVAL records then emitted LF LF.
    // Every buffered item already ends in LF, as do normal log records.
    if (*text=='\n') ++text;
    // Preserve IDF log-v2 prefix/body/trailer serialization. No holder waits
    // for USB: all runtime stdio/log sinks now enqueue with zero timeout.
    flockfile(stdout);
    dg_console::write(text,std::strlen(text));
    funlockfile(stdout);
}
// UART uses a separate reserved ACK mailbox; no stdout lock is held while
// waiting for physical TX. USB retains the qualified console_line byte path.
static void console_ack(const char* text){
    if(dg_board::info().console!=dg_board::ConsoleKind::Uart){console_line(text);return;}
    dg_console::acknowledge(text,std::strlen(text));
}
// Best-effort output acceptance, not USB delivery: expiry/full-queue drops
// are intentional and counted in PERF. Session/display work must continue.
bool app_console_line(const char* text) noexcept {
    try {console_line(text);return true;}catch(...) {return false;}
}
static void pause_ms(unsigned ms) { vTaskDelay(std::max(TickType_t(1), pdMS_TO_TICKS(ms))); }
void log_heap(const char* phase) {
    dg_heap_phase(phase, false);
}
static void resources(const char* phase) {
    log_heap(phase); dg_diagnostics_flush();
    const auto census_free_before=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    const auto census_min_before=heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    std::vector<TaskStatus_t> tasks(uxTaskGetNumberOfTasks() + 4);
    const auto census_free_allocated=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    auto count = uxTaskGetSystemState(tasks.data(), tasks.size(), nullptr);
    const auto census_min_collected=heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    ESP_LOGI(TAG,"TASK_CENSUS phase=%s bytes=%u external=%d before=%u allocated=%u min_before=%u min_collected=%u",
        phase,unsigned(tasks.size()*sizeof(TaskStatus_t)),int(esp_ptr_external_ram(tasks.data())),
        unsigned(census_free_before),unsigned(census_free_allocated),unsigned(census_min_before),unsigned(census_min_collected));
    for (UBaseType_t i = 0; i < count; ++i)
        ESP_LOGI(TAG, "STACK task=%s free_bytes=%u priority=%u runtime_us=%u core=%d task_id=%u", tasks[i].pcTaskName,
                 unsigned(tasks[i].usStackHighWaterMark), unsigned(tasks[i].uxCurrentPriority), unsigned(tasks[i].ulRunTimeCounter),
                 int(xTaskGetCoreID(tasks[i].xHandle)), unsigned(tasks[i].xTaskNumber));
}
static std::atomic<dg_audio::Pipeline*> audio{nullptr};
// Console compatibility facade. The app owns session lifetimes and reconnect.
class ClientAccess {
    dg::App& app;
public:
    explicit ClientAccess(dg::App& a):app(a){}
    pipecat::TransportState state() const {return app.ready()?pipecat::TransportState::Ready:(app.idle()?pipecat::TransportState::Initialized:pipecat::TransportState::Connecting);}
    int send_user_audio(const int16_t* p,size_t n) {return app.send_audio(p,n);}
    std::future<nlohmann::json> send_client_request(const std::string& kind,const nlohmann::json& data) {
        std::future<nlohmann::json> result;app.with_client([&](auto& client){result=client.send_client_request(kind,data);});return result;
    }
    void disconnect() {app.disconnect();}
};
void run_console() {
    try {
        if (!*CONFIG_DG_WIFI_SSID || !*dg::demo_settings().url) { ESP_LOGI(TAG, "TRANSPORT SKIPPED: local configuration absent"); return; }
        esp_log_level_set("wifi", ESP_LOG_ERROR);
        pipecat::esp_peer::stat_init();
        dg_display::backend_boot();
        // NVS/WiFi initialization runs on the internal app_main stack.
        auto generate_cert=[] {
            auto began=esp_timer_get_time();ESP_ERROR_CHECK(esp_peer_pre_generate_cert());
            performance_cert_duration(unsigned(esp_timer_get_time()-began));
            ESP_LOGI(TAG,"CERT duration_us=%lld",(long long)(esp_timer_get_time()-began));
        };
#if !CONFIG_DG_CERT_OVERLAP_WIFI
        generate_cert();
#endif
        pipecat::esp::configure_threads();
        std::unique_ptr<dg::App> app;
        // The audio threads borrow the App for one call at a time; teardown
        // clears the slot and waits for them before the App is destroyed.
        AudioSlot<dg::App> app_audio;
        const auto retire_audio=[&]{app_audio.clear();while(!app_audio.idle())std::this_thread::sleep_for(std::chrono::milliseconds(1));};
        std::atomic<bool> capture_wanted{false};
        constexpr size_t capacity = 16000 * 30, dump_chunk = 192;
        // The dump's chunk copy lives behind the recording, off the console stack.
        auto* recording = static_cast<int16_t*>(heap_caps_malloc((capacity + dump_chunk) * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (!recording) throw std::bad_alloc();
        std::unique_ptr<int16_t, decltype(&std::free)> recording_owner(recording, &std::free);
        dg::RxRecording rx(recording, capacity);
        // A microphone fixture loaded over the console (mic-load-*). Declared
        // before the pipeline, which reads it while `mic-fixture 2` is on.
        std::unique_ptr<int16_t, decltype(&std::free)> fixture_owner(nullptr, &std::free);
        dg::FixtureLoad loaded_fixture;
        std::atomic<bool> tone_running{false}, tone_stop{false}; std::mutex tone_mutex;
        resources("before-audio");
        dg_audio::Pipeline pipeline(
            [&](int16_t* pcm, size_t n) { auto a=app_audio.borrow();return a.pointer?a.pointer->send_audio(pcm,n):0; },
            [&](int16_t* pcm, size_t n,dg::MediaToken& token) { auto a=app_audio.borrow();return a.pointer?a.pointer->read_audio(pcm,n,token):0; },
            [&](const int16_t* pcm, size_t n) { rx.append(pcm,n); },
            [&] {auto a=app_audio.borrow();return a.pointer && a.pointer->audio_ready();});
        audio=&pipeline;
        resources("after-audio");
        dg_board::power::init();
        std::thread tone;
        dg::DemoUi demo_ui;
        dg::ScopeExit retire_app([&]{demo_ui.stop();retire_audio();app.reset();audio=nullptr;});
        try {
        app=std::make_unique<dg::App>(dg::App::Hooks{
            [&] {rx.reset();
#if CONFIG_DG_DUO
                if(!dg_display::duo_session_start())throw std::runtime_error("duo idle write did not finish");
#else
                dg_display::session_start();
#endif
                resources("before-connect");},
            [&] {pipeline.enable_capture(capture_wanted);dg_display::r4_gdma_ready_once();resources("ready");},
            [&] {pipeline.enable_capture(false);pipeline.speaking(false);tone_stop=true;dg_display::session_down();},
            [&](bool active) {pipeline.speaking(active);},
            [&] {std::lock_guard<std::mutex> lock(tone_mutex);if(tone.joinable())tone.join();},
            [&](dg::DemoPhase phase,uint32_t generation){
#if !CONFIG_DG_DUO
                demo_ui.lifecycle(phase,generation);
#else
                (void)phase;(void)generation;
#endif
            },
            [&]{return pipeline.capability();}},dg::demo_settings().url);
        app_audio.publish(app.get());
#if !CONFIG_DG_DUO
        demo_ui.start([&]{auto current=app_audio.borrow();return current.pointer && current.pointer->request_reconnect();});
#endif
#if CONFIG_DG_CERT_OVERLAP_WIFI
        generate_cert();
#endif
        ClientAccess client(*app);
        try {app->wait_wifi();}catch(const std::exception& e){ESP_LOGW(TAG,"WiFi pending: %s",e.what());}
#if CONFIG_DG_DUO
        dg::DuoBoot demo(*app,pipeline,capture_wanted);
#else
        dg::DemoBoot demo(*app,pipeline,capture_wanted);
#endif
        auto connect = [&] {
#if CONFIG_DG_DUO
            if(!demo.ready())throw std::runtime_error("duo install required");
#endif
            app->connect();};
        auto disconnect = [&] {
            // The state machine clears desired_connected before stopping media.
            app->disconnect();
            dg_display::session_end();
            {std::lock_guard<std::mutex> lock(tone_mutex);if(tone.joinable())tone.join();}
            console_line(("\nPERF "+performance_snapshot(app->stats())+"\n").c_str());resources("after-disconnect");
        };
            console_line("\nREADY transport-console\n");
            char line[4352]; size_t used = 0; bool overflow=false;
            for (;;) {
                // A UART command cannot consume the next command's ACK space.
                if(dg_console::ack_pending()){pause_ms(1);continue;}
                int c = std::getchar();
                if (c == EOF) { clearerr(stdin); pause_ms(5); continue; }
                if (c == '\r') continue;
                if (c != '\n') { if (used < sizeof(line) - 1) line[used++] = char(c); else overflow=true; continue; }
                line[used] = 0; used = 0;
                unsigned seq = 0; char command[48]{}; unsigned argument = 0;
                if (std::sscanf(line, "#%u %47s %u", &seq, command, &argument) < 2) continue;
                dg_display::RuntimePhaseScope display_phase(dg_display::RuntimePhase::Console);
                const auto console_begin=esp_timer_get_time();
                const bool console_read=!std::strcmp(command,"stats") || !std::strcmp(command,"wifi-stats") || !std::strcmp(command,"wifi-observe");
                try {
                    if(overflow){overflow=false;throw std::runtime_error("oversize command rejected intact");}
                    bool aec=dg_audio::aec_config::enabled,cache=false;
#if CONFIG_DG_CACHE_COUNTERS
                    cache=true;
#endif
                    switch(dg::console_support(command,dg_board::audio_hw().analog_gain_control,aec,cache,dg_board::info().cache_counters)) {
                    case dg::ConsoleSupport::Board:throw UnsupportedCommand{};
                    case dg::ConsoleSupport::AecDisabled:throw UnsupportedCommand{"aec-disabled"};
                    case dg::ConsoleSupport::CacheDisabled:throw UnsupportedCommand{"cache-counters-disabled"};
                    default:break;
                    }
                    if (!std::strcmp(command, "ping")) {}
                    else if (!std::strcmp(command,"aec-clock")) {
                        char stamp[96];std::snprintf(stamp,sizeof stamp,"\nAEC_CLOCK seq=%u device_us=%lld\n",
                            seq,(long long)esp_timer_get_time());console_line(stamp);
                    }
                    else if (!std::strcmp(command,"request-reconnect")) {
#if CONFIG_DG_DUO
                        if(!demo.ready())throw std::runtime_error("duo install required");
#endif
                        const bool admitted=app->request_reconnect();
                        console_line((std::string("\nRECONNECT admitted=")+(admitted?"1":"0")+"\n").c_str());
                    }
                    else if (!std::strcmp(command,"power")) {console_line(("\nPOWER "+power_status_json()+"\n").c_str());}
#if CONFIG_DG_DUO
                    else if (!std::strcmp(command,"duo")) {
                        const char* args=std::strstr(line,"duo")+3;while(*args==' ')++args;
                        if(!std::strcmp(args,"status")){
                            if(!app->duo_command(0))throw std::runtime_error("duo status mailbox busy");
                        }else if(!std::strcmp(args,"tap")){
                            if(!app->duo_command(2))throw std::runtime_error("duo tap mailbox busy");
                        }else demo.command(args,console_line);
                    }
#endif
                    else if (!std::strcmp(command,"demo")) {
                        const char* action=std::strstr(line,"demo")+4;while(*action==' ')++action;
                        if(!std::strcmp(action,"hold"))demo.hold();
                        else if(!std::strcmp(action,"run"))demo.resume();
                        else if(!std::strcmp(action,"status"))console_line(("\nDEMO "+demo.status()+"\n").c_str());
                        else if(!std::strncmp(action,"configure ",10)) {
                            auto data=nlohmann::json::parse(action+10);dg::DemoSettings settings{};
                            const auto url=data.at("url").get<std::string>();
                            if(url.size()>=sizeof(settings.url))throw std::runtime_error("demo URL too long");
                            std::strcpy(settings.url,url.c_str());settings.volume_db=data.at("volume_db").get<int>();
                            settings.enabled=data.at("enabled").get<bool>();settings.configured=true;
                            demo.configure(settings);
                        }else throw std::runtime_error("demo hold|run|status|configure JSON");
                    }
                    #if CONFIG_DG_CACHE_COUNTERS
                    else if (!std::strcmp(command, "cache-counters")) {
                        if(argument>1)throw std::runtime_error("cache-counters requires 0 or 1");
                        cache_metrics_enable(argument);
                    }
                    #endif
                    else if (!std::strcmp(command, "disp")) {
                        const char* args=std::strstr(line,"disp")+4;
                        dg_display::command(args,app->idle(),console_line);
                    }
                    else if (!std::strcmp(command,"touch-status") || !std::strcmp(command,"touch-inject") || !std::strcmp(command,"touch-enable")) {
#if CONFIG_DG_DUO
                        if(!std::strcmp(command,"touch-inject")){
                            if(!dg_board::touch::inject_double_tap())throw std::runtime_error("touch injector busy");
                        }else if(!std::strcmp(command,"touch-enable")){
                            if(argument>1 || !app->duo_command(1,argument))throw std::runtime_error("touch-enable 0|1 / busy");
                        }else demo_ui.command(command,argument,console_line);
#else
                        demo_ui.command(command,argument,console_line);
#endif
                    }
                    else if (!std::strcmp(command, "netprobe")) {
                        const char* args=std::strstr(line,"netprobe")+8;dg::netprobe::command(args,console_line);
                    }
                    else if (!std::strcmp(command, "server-url")) {
                        char url[128]{};if(std::sscanf(line,"#%*u %*s %127s",url)!=1)throw std::runtime_error("URL required");
                        app->server_url(url);
                    }
                    else if (!std::strcmp(command, "silence-bench")) {
                        if(!app->idle() || tone_running)throw std::runtime_error("Benchmark requires Idle");
                        bool quiet=pipeline.quiet();pipeline.enable_capture(false);pipeline.quiet(true);pipeline.benchmark_idle(true);
                        nlohmann::json result;
                        try {result=pipecat::esp_peer::silence_resume_benchmark(silence_start,silence_end-silence_start);}
                        catch(...) {pipeline.benchmark_idle(false);pipeline.quiet(quiet);throw;}
                        pipeline.benchmark_idle(false);pipeline.quiet(quiet);
                        if(result.contains("frames")) {
                            for(const auto& frame:result["frames"])console_line(("\nSILENCE_FRAME "+frame.dump()+"\n").c_str());
                            result["frames"]=result["frames"].size();
                        }
                        console_line(("\nSILENCE_BENCH "+result.dump()+"\n").c_str());
                    }
#if CONFIG_DG_PLAYBACK_BENCH
                    else if (!std::strcmp(command, "playback-selftest")) {
                        auto state=client.state();
                        if ((state!=pipecat::TransportState::Disconnected && state!=pipecat::TransportState::Initialized) || tone_running)
                            throw std::runtime_error("Benchmark requires no session");
                        pipeline.benchmark_idle(true);
                        try {console_line(("\nPLAYBACK_SELFTEST "+pipecat::esp_peer::playback_selftest().dump()+"\n").c_str());}
                        catch(...) {pipeline.benchmark_idle(false);throw;}
                        pipeline.benchmark_idle(false);
                    }
#endif
                    else if (!std::strcmp(command, "codec-bench")) {
                        auto state=client.state();
                        if ((state!=pipecat::TransportState::Disconnected && state!=pipecat::TransportState::Initialized) || tone_running)
                            throw std::runtime_error("Benchmark requires no session");
                        bool quiet=pipeline.quiet();pipeline.enable_capture(false);pipeline.quiet(true);pipeline.benchmark_idle(true);
                        nlohmann::json result;
                        try {result=pipecat::esp_peer::codec_benchmark(reinterpret_cast<const int16_t*>(speech_start+44),(speech_end-speech_start-44)/2);}
                        catch (...) {pipeline.benchmark_idle(false);pipeline.quiet(quiet);throw;}
                        pipeline.benchmark_idle(false);pipeline.quiet(quiet);
                        if (result.contains("cases")) {
                            for(auto& measured:result["cases"])console_line(("\nCODEC_CASE "+measured.dump()+"\n").c_str());
                            result["cases"]=result["cases"].size();
                        }
                        console_line(("\nCODEC_BENCH "+result.dump()+"\n").c_str());
                    }
                    else if (!std::strcmp(command, "audio")) {
                        pipeline.enable_capture(false);
                        app->send_audio(nullptr,0);
                        capture_wanted=argument!=0;pipeline.enable_capture(capture_wanted && app->ready());
                        char ack[96];
                        std::snprintf(ack,sizeof(ack),"\nAUDIO_STATE enabled=%d device_us=%lld\n",
                            int(capture_wanted.load()),(long long)esp_timer_get_time());
                        console_line(ack);
                    }
#if CONFIG_DG_PLAYBACK_BENCH
                    else if (!std::strcmp(command, "playback-pause")) pipeline.pause_playback(argument);
#endif
                    else if (!std::strcmp(command, "quiet")) pipeline.quiet(argument != 0);
                    else if (!std::strcmp(command, "mic-stats")) { pipeline.stats(); pipeline.quiet(false); }
                    else if (!std::strcmp(command, "gate")) {
                        char mode[16]{}; std::sscanf(line,"#%*u %*s %15s",mode);
                        if(!std::strcmp(mode,"auto")) pipeline.set_gate_policy(dg_audio::GatePolicy::Auto);
                        else if(!std::strcmp(mode,"open")) pipeline.set_gate_policy(dg_audio::GatePolicy::Open);
                        else if(!std::strcmp(mode,"closed")) pipeline.set_gate_policy(dg_audio::GatePolicy::Closed);
                        else throw std::runtime_error("gate mode");
                    }
                    else if (!std::strcmp(command, "rx-clear")) rx.reset();
                    else if (!std::strcmp(command, "mic-record")) pipeline.diagnostic_capture(argument!=0);
#if CONFIG_DG_ECHOEAR_AEC
                    else if (!std::strcmp(command,"aec-guard-rearm-ms")) {
                        pipeline.aec_guard_rearm_ms(dg_audio::parse_guard_rearm_ms(
                            std::strstr(line,"aec-guard-rearm-ms")+18));
                    }
                    else if (!std::strcmp(command,"aec-guard-ms")) {
                        pipeline.aec_guard_ms(dg_audio::parse_guard_ms(std::strstr(line,"aec-guard-ms")+12));
                    }
                    else if (!std::strcmp(command,"aec-ref-gain")) {
                        if(!app->idle())throw std::runtime_error("Reference gain requires Idle");
                        pipeline.reference_gain(dg_audio::parse_reference_gain(std::strstr(line,"aec-ref-gain")+12));
                    }
                    else if (!std::strcmp(command,"aec-ref-gain-status")) pipeline.reference_gain_status();
                    else if (!std::strcmp(command,"aec-record")) dg_audio::aec_record(argument!=0);
                    else if (!std::strcmp(command,"aec-record-slots")) dg_audio::aec_record(argument!=0,true);
                    else if (!std::strcmp(command,"aec-probe")) {
                        pipeline.aec_probe([&] {char ack[96];std::snprintf(ack,sizeof ack,"\nOK #%u aec-probe\n",seq);console_ack(ack);});
                        char ack[64];std::snprintf(ack,sizeof ack,"\nDONE #%u aec-probe\n",seq);console_line(ack);continue;
                    }
                    else if (!std::strcmp(command,"dump-aec")) {
                        dg_console::DumpCheck check;
                        const auto capture=dg_audio::aec_capture();
                        const auto status=dg_audio::aec_status();char output[560];
                        std::snprintf(output,sizeof output,"\nAEC_DUMP BEGIN %sframes=%u channels=%u rate=16000 order=%s adapter_delay=%u measured_delay=unknown truncated=%d ns=0 agc=0\n",
                            capture.channels==5?"schema=2 ":"",unsigned(capture.frames),capture.channels,capture.channels==5?"mic1,mic2,mic3,mic4,processed":"mic,ref,processed",
                            status.adapter_delay_samples,int(capture.truncated));dg_console::dump_line(output,console_line);
                        for(size_t i=0;i<capture.timing_count;++i) {
                            const auto& t=capture.timing[i];
                            std::snprintf(output,sizeof output,"\nAEC_TIME start=%u end=%u lower_us=%lld upper_us=%lld gap=%d\n",
                                unsigned(t.start),unsigned(t.end),(long long)t.time.lower_us,
                                (long long)t.time.upper_us,int(t.time.gap));console_line(output);
                            // The console ring drops on overflow; bound each burst below PCM pacing.
                            if(dg_board::info().console==dg_board::ConsoleKind::Uart)
                                dg_console::pace_dump(std::strlen(output));
                            else if((i&3)==3)pause_ms(4);
                        }
                        for(size_t at=0;at<capture.frames*capture.channels;at+=192) {
                            unsigned char encoded[513];size_t length;
                            const auto n=std::min(size_t(192),capture.frames*capture.channels-at);
                            mbedtls_base64_encode(encoded,sizeof encoded,&length,
                                reinterpret_cast<const unsigned char*>(capture.triples+at),n*2);
                            std::snprintf(output,sizeof output,"\nAEC_PCM %u %.*s\n",unsigned(at),int(length),encoded);
                            // Full 30 s all-slot exports must also drain under
                            // live AEC/display load (4 ms overflowed on the bench).
                            check.record(output);dg_console::dump_line(output,console_line,8);
                        }
                        check.finish("AEC_DUMP",output,sizeof output);dg_console::dump_line(output,console_line);
                        std::snprintf(output,sizeof output,"\nAEC_DUMP END frames=%u timing_blocks=%u\n",unsigned(capture.frames),unsigned(capture.timing_count));dg_console::dump_line(output,console_line);
                    }
#endif
                    else if (!std::strcmp(command, "mic-gain")) pipeline.microphone_gain(argument);
                    else if (!std::strcmp(command, "mic-analog-gain")) pipeline.microphone_analog_gain(argument);
                    else if (!std::strcmp(command, "mic-gain-status")) pipeline.microphone_gain_status();
                    else if (!std::strcmp(command, "playback-volume")) {
                        int db=0;
                        if(std::sscanf(line,"#%*u %*s %d",&db)!=1) throw std::runtime_error("Volume args");
                        pipeline.playback_volume(db);
                    }
                    else if (!std::strcmp(command, "playback-loudness")) {
                        int target=0;unsigned ceiling=0;
                        if(std::sscanf(line,"#%*u %*s %d %u",&target,&ceiling)!=2) throw std::runtime_error("Loudness args");
                        pipeline.playback_loudness(target,ceiling);
                    }
                    else if (!std::strcmp(command, "playback-settings")) {
                        unsigned gain=0,ceiling=0;
                        if(std::sscanf(line,"#%*u %*s %u %u",&gain,&ceiling)!=2) throw std::runtime_error("DSP args");
                        pipeline.playback_settings(gain,ceiling);
                    }
                    else if (!std::strcmp(command, "dump-mic")) {
                        dg_console::DumpCheck check;
                        const int16_t* pcm=nullptr; size_t count=pipeline.diagnostic_pcm(pcm); char output[560];
                        std::snprintf(output,sizeof output,"\nMIC_DUMP BEGIN samples=%u rate=16000 raw=1 dsp_bypass=1\n",unsigned(count)); dg_console::dump_line(output,console_line);
                        for(size_t at=0;at<count;at+=192) {
                            unsigned char encoded[513];size_t length;size_t n=std::min(size_t(192),count-at);
                            mbedtls_base64_encode(encoded,sizeof encoded,&length,reinterpret_cast<const unsigned char*>(pcm+at),n*2);
                            std::snprintf(output,sizeof output,"\nMIC_PCM %u %.*s\n",unsigned(at),int(length),encoded);check.record(output);dg_console::dump_line(output,console_line,4);
                        }
                        check.finish("MIC_DUMP",output,sizeof output);dg_console::dump_line(output,console_line);
                        std::snprintf(output,sizeof output,"\nMIC_DUMP END samples=%u\n",unsigned(count));dg_console::dump_line(output,console_line);
                    }
                    else if (!std::strcmp(command, "tone")) {
                        unsigned hz=0,ms=0,amp=50;
                        if(std::sscanf(line,"#%*u %*s %u %u %u",&hz,&ms,&amp)<2) throw std::runtime_error("tone args");
                        pipeline.tone(hz,ms,amp,[&] { char ack[64]; std::snprintf(ack,sizeof ack,"\nOK #%u\n",seq); console_ack(ack); });
                        char ack[64]; std::snprintf(ack,sizeof ack,"\nDONE #%u\n",seq); console_line(ack); continue;
                    }
                    else if (!std::strcmp(command,"heap-sample-compare")) {performance_compare_sampling(argument!=0);}
#if CONFIG_DG_HEAP_ATTRIBUTION
                    else if (!std::strcmp(command,"heap-attribution")) {
                        dg::heap_attribution_command(std::strstr(line,"heap-attribution")+16,app->idle(),console_line);
                    }
#endif
                    else if (!std::strcmp(command,"help"))
                        console_line("\nuplink-intervals: freezes this session permanently; call once, just before intentional stop.\n");
                    else if (!std::strcmp(command,"uplink-intervals")) app->dump_uplink_intervals();
                    else if (!std::strcmp(command,"console-drops")) {
                        const auto s=dg_console::stats();char text[160];
                        std::snprintf(text,sizeof text,"\nCONSOLE_DROPS full=%u transport_drops=%u ack_pending=%u\n",s.full,s.transport_drops,s.ack_pending);console_line(text);
                    }
                    else if (!std::strcmp(command, "stats")) { pipeline.stats(); console_line(("\nPERF "+performance_snapshot(app->stats())+"\n").c_str()); resources("stats"); }
                    else if (!std::strcmp(command, "dump-arrivals")) {
                        // Bounded batches, never print from a transport callback.
                        alignas(8) unsigned char rows[32*16];unsigned char encoded[32*16*2];
                        size_t count;dg_console::DumpCheck check;
                        dg_console::dump_line("\nARRIVALS BEGIN version=1 record_bytes=32\n",console_line);
                        // Snapshot budget prevents a continuous producer from
                        // extending a dump indefinitely. New records stay queued.
                        for(unsigned batches=0;batches<512 && (count=app->drain_trace(rows,16));++batches) {
                            size_t length=0;
                            mbedtls_base64_encode(encoded,sizeof encoded,&length,rows,count*32);
                            std::string line="\nARRIVAL "+std::string(reinterpret_cast<char*>(encoded),length)+"\n";
                            check.record(line.c_str());dg_console::dump_line(line.c_str(),console_line,4);
                        }
                        char check_line[128];check.finish("ARRIVALS",check_line,sizeof check_line);dg_console::dump_line(check_line,console_line);
                        dg_console::dump_line("\nARRIVALS END\n",console_line);
                    }
                    else if (!std::strcmp(command, "mic-tone")) {
                        if(argument>1)throw std::runtime_error("Tone expects 0 or 1");
                        static int16_t pcm[320];
                        for(unsigned i=0;i<320;++i)pcm[i]=int16_t(8000*std::sin(2*3.141592653589793*1000*i/16000));
                        pipeline.diagnostic_fixture(argument?pcm:nullptr,argument?320:0);
                    }
                    else if (!std::strcmp(command, "mic-fixture")) {
                        // 0 off, 1 the built-in prompt, 2 the fixture loaded with mic-load-*.
                        if (argument > 2) throw std::runtime_error("Fixture expects 0, 1 or 2");
                        if (argument == 2 && !loaded_fixture.pcm()) throw std::runtime_error("No loaded fixture");
                        if (argument == 2) pipeline.diagnostic_fixture(loaded_fixture.pcm(), loaded_fixture.samples());
                        else pipeline.diagnostic_fixture(argument ? reinterpret_cast<const int16_t*>(speech_start+44) : nullptr,
                                                    argument ? (speech_end-speech_start-44)/2 : 0);
                    }
                    else if (!std::strcmp(command, "mic-load-begin")) {
                        // diagnostic_fixture() refuses while capture is on and
                        // without speaker diagnostics, and it empties the slot
                        // the capture loop reads before the old storage goes.
                        pipeline.diagnostic_fixture(nullptr, 0);
                        loaded_fixture.clear(); fixture_owner.reset();
                        if (!argument || argument > dg::FixtureLoad::max_samples) throw std::runtime_error("mic-load-begin 1..480000 samples");
                        fixture_owner.reset(static_cast<int16_t*>(heap_caps_malloc(size_t(argument) * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
                        if (!fixture_owner) throw std::runtime_error("No memory for the fixture");
                        loaded_fixture.begin(fixture_owner.get(), argument);
                    }
                    else if (!std::strcmp(command, "mic-load-data")) {
                        pipeline.diagnostic_fixture(nullptr, 0);  // Capture stays off for the whole load.
                        // "#seq mic-load-data <sample offset> <samples> <crc32> <base64>"
                        loaded_fixture.data_line(std::strstr(line, command) + std::strlen(command));
                    }
                    else if (!std::strcmp(command, "mic-load-end")) {
                        pipeline.diagnostic_fixture(nullptr, 0);
                        unsigned crc = 0;
                        if (std::sscanf(line, "#%*u %*s %*u %x", &crc) != 1) throw std::runtime_error("mic-load-end samples crc32");
                        char note[80]; std::snprintf(note, sizeof note, "\nMIC_LOAD samples=%u crc32=%08x\n", unsigned(loaded_fixture.end(argument, crc)), crc);
                        console_line(note);
                    }
                    else if (!std::strcmp(command, "peer-option")) {
                        char name[24];unsigned value;
                        if(std::sscanf(line,"#%*u %*s %23s %u",name,&value)!=2)throw std::runtime_error("Option args");
                        app->diagnostic_option(name,value);
                    }
                    else if (!std::strcmp(command, "wifi-drop")) {
                        const auto error=esp_wifi_disconnect();
                        if(error==ESP_ERR_NOT_SUPPORTED)throw UnsupportedCommand{};
                        if(error!=ESP_OK)throw std::runtime_error("WiFi disconnect failed");
                    }
                    else if (!std::strcmp(command, "wifi-log")) {
                        if(argument>1)throw std::runtime_error("wifi-log requires 0 or 1");
                        esp_log_level_set("wifi",argument?ESP_LOG_INFO:ESP_LOG_ERROR);
                    }
                    else if (!std::strcmp(command,"wifi-observe")) {
                        const auto status=dg_wifi_observe_status();
                        console_line(("\nWIFI_OBSERVE BEGIN "+wifi_observation_status_json(status)+"\n").c_str());
                        for(uint64_t sequence=status.first_sequence;status.retained && sequence<=status.last_sequence;++sequence) {
                            dg_wifi_observation_t row{};
                            if(!dg_wifi_observe_read(sequence,&row))throw std::runtime_error("WiFi observation overwritten during export");
                            console_line(("\nWIFI_OBSERVE ROW "+wifi_observation_row_json(row)+"\n").c_str());
                            vTaskDelay(pdMS_TO_TICKS(4)+1);
                        }
                        console_line(("\nWIFI_OBSERVE END last_sequence="+std::to_string(status.last_sequence)+"\n").c_str());
                    }
                    else if (!std::strcmp(command, "wifi-stats")) {
                        // The regular WiFi logger is ERROR-only. Temporarily
                        // enable the requested RX/TX and buffer dump modules.
                        auto previous_level=esp_log_level_get("wifi");
                        esp_log_level_set("wifi",ESP_LOG_INFO);
                        auto error=esp_wifi_statis_dump(WIFI_STATIS_RXTX|WIFI_STATIS_BUFFER);
                        esp_log_level_set("wifi",previous_level);
                        if(error==ESP_ERR_NOT_SUPPORTED)throw UnsupportedCommand{};
                        if(error!=ESP_OK)throw std::runtime_error("WiFi statistics dump failed");
                    }
                    else if (!std::strcmp(command, "fault-channel-next")) app->fault_next_channel();
                    else if (!std::strcmp(command, "disconnect")) disconnect();
                    else if (!std::strcmp(command, "connect")) connect();
                    else if (!std::strcmp(command, "rtp-stats")) { client.send_client_request("rtp-stats", nlohmann::json::object()).get(); }
                    else if (!std::strcmp(command, "play-fixture")) {
                        char fixture[64]{};
                        if(std::sscanf(line,"#%*u %*s %63s",fixture)!=1) throw std::runtime_error("Fixture name required");
                        auto response=client.send_client_request("play", {{"fixture", fixture}});
                        if(response.wait_for(std::chrono::seconds(15))!=std::future_status::ready)throw std::runtime_error("Play response timeout");
                        response.get();
                    }
                    else if (!std::strcmp(command, "play") || !std::strcmp(command, "play-long")) {
                        unsigned loops = argument ? argument : 1;
                        if (loops > 30 || (std::strcmp(command,"play-long") && loops != 1))
                            throw std::runtime_error("playback loops");
                        ESP_LOGI(TAG, "PLAY BEGIN samples=%u", unsigned(rx.samples()));
                        auto response=client.send_client_request("play", {{"fixture", "reply_pineapple.wav"},
                            {"duration_ms", !std::strcmp(command,"play-long") ? 60000 : 0},
                            {"loops", loops}});
                        if(response.wait_for(std::chrono::seconds(15))!=std::future_status::ready)throw std::runtime_error("Play response timeout");
                        response.get();
                        ESP_LOGI(TAG, "PLAY ACK samples=%u", unsigned(rx.samples()));
                    } else if (!std::strcmp(command, "message")) {
                        if (argument != 1024 && argument != 15360 && argument != 17408) throw std::runtime_error("size");
                        auto msg = pipecat::rtvi::Message::client_message("echo", {{"padding", ""}, {"bytes", argument}});
                        size_t overhead = nlohmann::json(msg).dump().size();
                        msg.data["d"]["padding"] = std::string(argument - overhead, 'x');
                        try {
                            auto echoed = client.send_client_request("echo", msg.data["d"]).get();
                            if (echoed != msg.data["d"]) throw std::runtime_error("Echo mismatch");
                            ESP_LOGI(TAG, "MESSAGE echoed bytes=%u", argument);
                        }
                        catch (const pipecat::MessageTooLargeError&) { ESP_LOGI(TAG, "MESSAGE rejected bytes=%u", argument); }
                    } else if (!std::strcmp(command, "tone-tx") || !std::strcmp(command, "speech-tx")) {
                        std::lock_guard<std::mutex> lock(tone_mutex);
                        if (!app->ready() || tone_running) throw std::runtime_error("busy");
                        if (tone.joinable()) tone.join();
                        tone_stop=false; tone_running = true;
                        dg::PthreadConfig threads;auto cfg = threads.current();
                        cfg.stack_size = 8192; cfg.prio = 12; cfg.pin_to_core = 1; cfg.thread_name = "tone-source";
                        cfg.stack_alloc_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
                        threads.set(cfg);
                        bool speech = !std::strcmp(command, "speech-tx");
                        tone = std::thread([&, speech] {
                            int16_t pcm[320], sine[320];
                            // Generate once: software double-precision sin at the
                            // source priority otherwise starves encode/decode.
                            for (unsigned i = 0; i < 320; ++i)
                                sine[i] = int16_t(8000 * std::sin(2 * 3.141592653589793 * 1000 * i / 16000));
                            unsigned accepted = 0; auto tick = xTaskGetTickCount();
                            // Committed fixture is canonical 44-byte-header PCM16 mono.
                            size_t speech_samples = (speech_end - speech_start - 44) / 2;
                            unsigned total = speech ? 100 + (speech_samples + 319) / 320 : 500;
                            for (unsigned frame = 0; frame < total && !tone_stop; ++frame) {
                                if (!speech && frame == 250) log_heap("streaming");
                                for (unsigned i = 0; i < 320; ++i) {
                                    if (speech) {
                                        int at = (int(frame) - 50) * 320 + i;
                                        pcm[i] = at >= 0 && size_t(at) < speech_samples ?
                                            reinterpret_cast<const int16_t*>(speech_start + 44)[at] : 0;
                                    } else pcm[i] = (frame >= 50 && frame < 450) ? sine[i] : 0;
                                }
                                accepted += client.send_user_audio(pcm, 320); vTaskDelayUntil(&tick, pdMS_TO_TICKS(20));
                            }
                            dg_deferred_log("STACK task=tone-source free_bytes=%u priority=12",
                                     unsigned(uxTaskGetStackHighWaterMark(nullptr)));
                            dg_deferred_log("%s DONE frames=%u accepted=%u", speech ? "SPEECH" : "TONE", total, accepted); tone_running = false;
                        });
                    } else if (!std::strcmp(command, "dump-rx")) {
                        dg_console::DumpCheck check;
                        // One recording or a refusal: a reconnect during the paced
                        // output resets the recording and playback rewrites its start.
                        const auto exported = rx.snapshot(); int16_t* const chunk = recording + capacity;
                        size_t count = exported.samples; char line[560];
                        std::snprintf(line, sizeof line, "\nDUMP BEGIN samples=%u\n", unsigned(count)); dg_console::dump_line(line,console_line);
                        for (size_t at = 0; at < count; at += dump_chunk) {
                            unsigned char encoded[513]; size_t length; size_t n = std::min(dump_chunk, count - at);
                            if (!rx.read(exported, at, chunk, n)) {
                                std::snprintf(line, sizeof line, "\nDUMP ABORTED at=%u reason=recording-reset\n", unsigned(at)); dg_console::dump_line(line,console_line);
                                throw std::runtime_error("recording was reset during the dump");
                            }
                            mbedtls_base64_encode(encoded, sizeof encoded, &length, reinterpret_cast<unsigned char*>(chunk), n * 2);
                            std::snprintf(line, sizeof line, "\nPCM %u %.*s\n", unsigned(at), int(length), encoded);check.record(line);dg_console::dump_line(line,console_line,4);
                        }
                        check.finish("DUMP",line,sizeof line);dg_console::dump_line(line,console_line);
                        std::snprintf(line, sizeof line, "\nDUMP END samples=%u\n", unsigned(count)); dg_console::dump_line(line,console_line);
                    } else throw std::runtime_error("command");
                    char ack[96]; std::snprintf(ack, sizeof ack, "\nOK #%u %s\n", seq, command); console_ack(ack);
                } catch (const UnsupportedCommand& error) {
                    console_line((std::string(error.build?"\nunsupported build=":"\nunsupported board=")+
                        (error.build?error.build:dg_board::info().name)+" cmd="+command+"\n").c_str());
                    char ack[96];std::snprintf(ack,sizeof ack,"\nERR #%u %s\n",seq,command);console_ack(ack);
                } catch (const std::exception& error) {
                    if (!std::strcmp(command,"disp")) console_line(("\nDISPLAY_ERROR "+std::string(error.what())+"\n").c_str());
                    if (!std::strcmp(command,"codec-bench")) console_line(("\nCODEC_ERROR "+std::string(error.what())+"\n").c_str());
                    if (!std::strncmp(command,"mic-load-",9) || !std::strcmp(command,"mic-fixture")) console_line(("\nMIC_LOAD_ERROR "+std::string(error.what())+"\n").c_str());
                    char ack[96]; std::snprintf(ack, sizeof ack, "\nERR #%u %s\n", seq, command); console_ack(ack);
                } catch (...) { char ack[96]; std::snprintf(ack, sizeof ack, "\nERR #%u %s\n", seq, command); console_ack(ack); }
                dg_diagnostics_flush(); std::fflush(stdout);
                if(console_read) {
                    char window[128];std::snprintf(window,sizeof window,
                        "\nCONSOLE_WINDOW command=%s start_us=%lld end_us=%lld\n",
                        command,
                        (long long)console_begin,(long long)esp_timer_get_time());console_line(window);
                }
            }
        } catch (...) {
            demo_ui.stop();pipeline.enable_capture(false);
            try {if(app)app->disconnect();}catch(...){}
            retire_audio();app.reset();
            {std::lock_guard<std::mutex> lock(tone_mutex);if(tone.joinable())tone.join();}
            audio=nullptr;throw;
        }
    } catch (...) { ESP_LOGE(TAG, "TRANSPORT FAIL"); }
}
