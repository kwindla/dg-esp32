#include "demo_ui.h"
#include "demo_touch_delivery.h"
#include "touch_init_retry.h"
#include "dg_board/touch.h"
#include "dg_display/demo_screen.h"
#include "dg_diagnostics.h"
#include "pthread_config.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstdio>
#include <cstring>
#include <stdexcept>
namespace dg {
void DemoUi::start(std::function<bool()> request){
    if(worker_.joinable())throw std::runtime_error("touch worker already started");
    request_=std::move(request);quit_=false;
    PthreadConfig config;auto cfg=config.current();cfg.stack_size=4096;
    cfg.stack_alloc_caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;cfg.pin_to_core=0;cfg.prio=2;cfg.thread_name="demo-touch";config.set(cfg);
    worker_=std::thread([this]{
        // The worker owns the probe, as DuoTouch's does: a controller that did
        // not answer at boot is probed again instead of leaving touch off.
        TouchInitRetry init;
        while(!quit_){
            if(!init.ready()){
                const int64_t now=esp_timer_get_time()/1000;
                if(init.due(now)){
                    const auto status=dg_board::touch::init();
                    if(status==ESP_OK)init.succeeded();
                    else dg_deferred_log("DEMO_TOUCH_INIT result=%d retry_ms=%u",int(status),init.failed(now));
                }
                if(!init.ready()){vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS(10)));continue;}
            }
            const auto delivery=deliver_demo_touch(
                [this]{return DemoTouchWindow{enabled_ && phase_==DemoPhase::Live && !pending_,generation_.load()};},
                [](bool accept){return dg_board::touch::poll(uint32_t(esp_timer_get_time()/1000),accept);},
                [this]{
                    pending_=true;
                    if(request_()){++requests_;}
                    else {++request_rejected_;pending_=false;}
                });
            if(delivery==DemoTouchDelivery::PhaseChanged)++phase_ignored_;
            vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS(10)));
        }
    });
}
void DemoUi::stop(){quit_=true;if(worker_.joinable())worker_.join();}
void DemoUi::lifecycle(DemoPhase phase,uint32_t generation)noexcept{
    generation_=generation;phase_=phase;
    if(phase==DemoPhase::Live)pending_=false;
    dg_display::demo_screen(phase==DemoPhase::Live?dg_display::DemoScreen::Live:
        phase==DemoPhase::Connecting?dg_display::DemoScreen::Connecting:dg_display::DemoScreen::Disconnected);
    dg_deferred_log("DEMO_LIFECYCLE {\"phase\":%u,\"generation\":%u,\"device_us\":%lld}",unsigned(phase),unsigned(generation),(long long)esp_timer_get_time());
}
void DemoUi::command(const char* command,unsigned argument,void(*out)(const char*)){
    if(!std::strcmp(command,"touch-enable")){
        if(argument>1)throw std::runtime_error("touch-enable 0|1");
        enabled_=argument!=0;return;
    }
    if(!std::strcmp(command,"touch-inject")){
        if(!worker_.joinable() || !dg_board::touch::inject_double_tap())throw std::runtime_error("touch injector unavailable/busy");
        return;
    }
    const auto s=dg_board::touch::status();char record[768];
    std::snprintf(record,sizeof(record),"\nDEMO_TOUCH_STATUS {\"initialized\":%s,\"enabled\":%s,\"phase\":%u,\"generation\":%u,\"pending\":%s,\"requests\":%u,\"request_rejected\":%u,\"phase_ignored\":%u,\"driver\":{\"interrupts\":%u,\"reads\":%u,\"read_errors\":%u,\"coalesced\":%u,\"taps\":%u,\"double_taps\":%u,\"ignored\":%u,\"rejected\":%u,\"injected\":%u,\"injected_ignored\":%u}}\n",
        s.initialized?"true":"false",enabled_?"true":"false",unsigned(phase_.load()),unsigned(generation_.load()),pending_?"true":"false",unsigned(requests_.load()),unsigned(request_rejected_.load()),unsigned(phase_ignored_.load()),unsigned(s.interrupts),unsigned(s.reads),unsigned(s.read_errors),unsigned(s.coalesced),unsigned(s.gestures.taps),unsigned(s.gestures.double_taps),unsigned(s.gestures.ignored),unsigned(s.gestures.rejected),unsigned(s.injected),unsigned(s.injected_ignored));
    out(record);
}
}
