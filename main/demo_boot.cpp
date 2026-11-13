#include "demo_boot.h"
#include "demo_hold.h"
#include "demo_assets.h"
#include "dg_display/runtime.h"
#if __has_include("dg_display/demo_assets.h")
#include "dg_display/demo_assets.h"
#endif
#include "pthread_config.h"
#include "sdkconfig.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include <chrono>
#include <algorithm>
#include <stdexcept>
namespace dg {
using namespace std::chrono_literals;
DemoBoot::DemoBoot(App& a,dg_audio::Pipeline& p,std::atomic<bool>& c):app(a),audio(p),capture(c) {
#if CONFIG_DG_DEMO_AUTOSTART
    PthreadConfig scoped;auto config=scoped.current();config.pin_to_core=0;config.prio=2;
    config.stack_size=16384;config.thread_name="demo-boot";
    config.stack_alloc_caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;scoped.set(config);
    worker=std::thread([this]{run();});
#else
    intent.held=true;
#endif
}
DemoBoot::~DemoBoot() {quit=true;if(worker.joinable())worker.join();}
void DemoBoot::hold() {
    intent.hold();
    const auto deadline=esp_timer_get_time()+35000000;
    while(initializing && esp_timer_get_time()<deadline)std::this_thread::sleep_for(10ms);
    if(initializing)throw std::runtime_error("boot restore still busy");
    capture=false;audio.enable_capture(false);
    finish_demo_hold([&]{app.disconnect();},[]{dg_display::session_end();});
}
void DemoBoot::resume() {
#if CONFIG_DG_DEMO_AUTOSTART
    intent.resume();
#else
    throw std::runtime_error("Autostart requires a demo image");
#endif
}
void DemoBoot::configure(const DemoSettings& s) {
    if(!intent.held || initializing || !app.idle())throw std::runtime_error("demo hold / Idle required");
    const auto error=demo_settings_save(s);
    if(error!=ESP_OK)throw std::runtime_error(esp_err_to_name(error));
}
std::string DemoBoot::status() const {
    const auto s=demo_settings();
    return "{\"configured\":"+std::string(s.configured?"true":"false")+
        ",\"enabled\":"+(s.enabled?"true":"false")+",\"held\":"+(intent.held?"true":"false")+
        ",\"initializing\":"+(initializing?"true":"false")+",\"attempts\":"+std::to_string(attempts)+
        ",\"failures\":"+std::to_string(failures)+",\"volume_db\":"+std::to_string(s.volume_db)+"}";
}
void DemoBoot::run() {
    auto next=esp_timer_get_time()+3000000;unsigned backoff=1000;
    while(!quit) {
        if(intent.held){intent.activated=false;std::this_thread::sleep_for(50ms);continue;}
        const auto settings=demo_settings();
        if(intent.activated || !settings.enabled || !settings.configured || !app.idle() || esp_timer_get_time()<next) {
            std::this_thread::sleep_for(50ms);continue;
        }
        initializing=true;
        // Recheck after publication so hold() cannot race into configuration IO.
        if(intent.held){initializing=false;continue;}
        ++attempts;
        try {
            app.server_url(settings.url);
#if __has_include("dg_display/demo_assets.h")
            // Do not show the live cat while restoring disconnected/connecting
            // screens. All image reads complete before connect/audio starts.
            dg_display::standalone_assets(CharacterAssets,16,false);
            for(const auto& asset:dg_display::demo_assets)dg_display::preload_persisted(asset.hash);
            dg_display::command("demo on",true,[](const char*){});
#else
            dg_display::standalone_assets(CharacterAssets,16);
#endif
            audio.playback_volume(settings.volume_db);audio.quiet(true);
            if(!intent.held && !quit){capture=true;app.request_connect();intent.activated=true;}
            backoff=1000;
            ESP_LOGI("demo-boot","RESTORED assets=16 no_http=1 connect_intent=%d",int(intent.activated));
        }catch(const std::exception& e) {
            ++failures;next=esp_timer_get_time()+int64_t(backoff)*1000;
            ESP_LOGW("demo-boot","restore failed: %s retry_ms=%u",e.what(),backoff);
            backoff=std::min(30000u,backoff*2);
        }
        initializing=false;
    }
}
}
