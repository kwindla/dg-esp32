#include "duo_boot.h"
#include "duo_settings.h"
#include "dg_duo/install.h"
#include "dg_display/runtime.h"
#include "dg_display/demo_assets.h"
#include "pthread_config.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include <chrono>
#include <cstring>
#include <cstdio>
#include <stdexcept>
namespace dg {
namespace {
struct Io:dg_duo::InstallIo {
    bool connecting=false;
    void progress(){if(!connecting)connecting=dg_display::duo_status_screen(true);}
    dg_display::Error prepare()override{
        auto e=dg_display::duo_prepare();if(e!=dg_display::Error::None)return e;
        const auto& screen=dg_display::demo_assets[0];dg_duo::DuoAsset a{screen.hash,screen.bytes};
        if(dg_display::duo_inspect(a)==dg_display::Error::None && dg_display::duo_restore(a)==dg_display::Error::None)
            dg_display::duo_status_screen(false);
        return e;
    }
    dg_display::Error inspect(const dg_duo::DuoAsset& a)override{return dg_display::duo_inspect(a);}
    dg_display::Error restore(const dg_duo::DuoAsset& a)override{auto e=dg_display::duo_restore(a);if(e==dg_display::Error::None)progress();return e;}
    dg_display::Error fetch(const dg_duo::DuoAsset& a,const char* o)override{auto e=dg_display::duo_fetch(a,o);if(e==dg_display::Error::None)progress();return e;}
    dg_display::Error pin(const dg_duo::DuoAsset& a)override{return dg_display::duo_pin(a);}
    void unpin(const dg_duo::DuoAsset& a)noexcept override{dg_display::duo_unpin(a);}
    dg_display::Error admit(const dg_duo::DuoAsset* a,size_t n)override{return dg_display::duo_admit(a,n);}
};
}
DuoBoot::DuoBoot(App& a,dg_audio::Pipeline& p,std::atomic<bool>& c):app(a),audio(p),capture(c){
    PthreadConfig scoped;auto cfg=scoped.current();cfg.pin_to_core=0;cfg.prio=2;cfg.stack_size=16384;
    cfg.thread_name="duo-boot";cfg.stack_alloc_caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;scoped.set(cfg);
    worker_=std::thread([this]{run();});
}
DuoBoot::~DuoBoot(){quit_=true;if(worker_.joinable())worker_.join();}
void DuoBoot::hold(){
    // Latch first: the worker checks this before it activates anything, and
    // again when an install returns.
    held_=true;
    // The worker keeps the gate for a whole asset install. Like
    // DemoBoot::hold(), wait a bounded time and then say so, instead of
    // stalling the console for as long as the install takes.
    std::unique_lock<std::mutex> lock(gate_,std::try_to_lock);
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(35);
    while(!lock && std::chrono::steady_clock::now()<deadline){
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        if(lock.try_lock())break;
    }
    if(!lock)throw std::runtime_error("duo install still busy; hold is latched");
    activated_=false;capture=false;audio.enable_capture(false);app.disconnect();dg_display::session_end();}
void DuoBoot::resume(){held_=false;}
void DuoBoot::configure(const DemoSettings& s){std::lock_guard<std::mutex> lock(gate_);
    if(!held_ || !app.idle())throw std::runtime_error("demo hold / Idle required");
    auto e=demo_settings_save(s);if(e!=ESP_OK)throw std::runtime_error(esp_err_to_name(e));
}
std::string DuoBoot::status()const{return std::string("{\"duo\":true,\"ready\":")+(ready_?"true":"false")+",\"held\":"+(held_?"true":"false")+"}";}
void DuoBoot::command(const char* args,void (*out)(const char*)){
    while(*args==' ')++args;
    if(!std::strcmp(args,"mem")){const auto m=dg_display::duo_memory();char b[192];
        std::snprintf(b,sizeof(b),"\nDUO_MEM {\"used\":%u,\"largest\":%u,\"pinned\":%u}\n",unsigned(m.used),unsigned(m.largest),m.pinned);out(b);return;}
    if(!std::strncmp(args,"origin ",7)){
        std::unique_lock<std::mutex> lock(gate_,std::try_to_lock);
        if(!lock || !app.idle())throw std::runtime_error("duo origin requires Idle / installer not busy");
        auto s=duo_settings();const char* origin=args+7;if(!std::strcmp(origin,"clear"))origin="";
        if(!duo_valid_origin(origin))throw std::runtime_error("duo origin requires http://host[:port] or clear");
        std::strcpy(s.origin,origin);auto e=duo_settings_save(s);if(e!=ESP_OK)throw std::runtime_error(esp_err_to_name(e));return;
    }throw std::runtime_error("duo origin URL|clear / mem");
}
void DuoBoot::run(){
    using namespace std::chrono_literals;bool installed=false;
    while(!quit_){
        std::this_thread::sleep_for(100ms);if(held_)continue;
        std::unique_lock<std::mutex> lock(gate_);if(held_ || !app.idle())continue;
        if(!installed){
            try{
                Io io;dg_duo::DuoAsset assets[96]{};auto count=dg_duo::install_assets(assets,96);
                const auto s=duo_settings();auto r=dg_duo::install(io,assets,count,s.origin);
                installed=r.error==dg_display::Error::None;
                ready_=installed;
                const bool screen=installed || dg_display::duo_status_screen(false);
                ESP_LOGI("duo-boot","DUO_INSTALL result=%s reason=%s restored=%u fetched=%u pinned=%u screen=%s",
                    installed?"ready":"error",r.missing_origin?"missing-origin":dg_display::error_name(r.error),r.restored,r.fetched,r.pinned,screen?"available":"unavailable");
            }catch(const std::exception& e){installed=false;ready_=false;
                const bool screen=dg_display::duo_status_screen(false);
                ESP_LOGW("duo-boot","DUO_INSTALL result=error reason=%s screen=%s",e.what(),screen?"available":"unavailable");}
            if(!installed){lock.unlock();for(unsigned n=0;n<50 && !quit_ && !held_;++n)std::this_thread::sleep_for(100ms);continue;}
        }
        if(held_){activated_=false;continue;}
        const auto s=demo_settings();
        if(!activated_ && s.enabled && s.configured && !quit_){
            try{app.server_url(s.url);audio.playback_volume(s.volume_db);audio.quiet(true);
                capture=true;app.request_connect();activated_=true;
            }catch(const std::exception& e){ESP_LOGW("duo-boot","connect failed: %s",e.what());}
        }
    }
}
}
