#include "duo_touch.h"
#include "dg_board/touch.h"
#include "dg_diagnostics.h"
#include "pthread_config.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <algorithm>
#include <stdexcept>
namespace dg {
void DuoTouch::start(){
    if(worker_.joinable())throw std::runtime_error("duo touch already started");
    PthreadConfig config;auto cfg=config.current();cfg.stack_size=4096;
    cfg.stack_alloc_caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;cfg.pin_to_core=0;cfg.prio=2;cfg.thread_name="duo-touch";config.set(cfg);
    quit_=false;worker_=std::thread([this]{while(!quit_){
        poll(esp_timer_get_time()/1000);
        vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS(20)));
    }});
}
void DuoTouch::stop(){quit_=true;if(worker_.joinable())worker_.join();}
void DuoTouch::admission(bool active,uint32_t generation){
    const auto before=window_.load();
    if(uint32_t(before>>32)==generation && bool(before&1)==active)return;
    // A version also fences a disable/re-enable in the same app generation.
    window_.store((uint64_t(generation)<<32)|(uint64_t(++serial_&0x7fffffffu)<<1)|unsigned(active));
}
void DuoTouch::poll(int64_t now){
    if(!init_.ready()){
        if(!init_.due(now))return;
        const auto error=dg_board::touch::init();
        if(error!=ESP_OK){dg_deferred_log("DUO_TOUCH_INIT result=%d retry_ms=%u",int(error),init_.failed(now));return;}
        init_.succeeded();
    }
    if(now<next_poll_)return;
    next_poll_=now+20;
    const auto before=window_.load();
    const auto injected=dg_board::touch::status().injected;
    const bool tapped=dg_board::touch::poll(uint32_t(now),bool(before&1));
    if(tapped && before==window_.load())
        send_(context_,dg_board::touch::status().injected!=injected,uint32_t(before>>32));
}
}
