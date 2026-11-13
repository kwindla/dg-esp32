#include "duo_touch.h"
#include "dg_board/touch.h"
#include "dg_diagnostics.h"
#include "esp_heap_caps.h"
#include "esp_pthread.h"
#include "freertos/task.h"
#include "check.h"
#include <atomic>
#include <chrono>
#include <cstring>
#include <functional>
#include <thread>
#include <vector>
namespace {
std::atomic<unsigned> init_calls{0},poll_calls{0},failures{0},logs{0},injected{0};
bool physical=false,driver=false,last_accept=false;std::function<void()> during_poll;
std::thread::id init_thread;
esp_pthread_cfg_t current{},launched{};
struct Trace {
    struct Tap {bool injected;uint32_t generation;};std::vector<Tap> taps;
    static bool send(void* p,bool source,uint32_t generation){static_cast<Trace*>(p)->taps.push_back({source,generation});return true;}
};
}
esp_pthread_cfg_t esp_pthread_get_default_config(){return {};}
int esp_pthread_get_cfg(esp_pthread_cfg_t* p){*p=current;return ESP_OK;}
int esp_pthread_set_cfg(const esp_pthread_cfg_t* p){current=*p;if(!std::strcmp(p->thread_name,"duo-touch"))launched=*p;return ESP_OK;}
int64_t esp_timer_get_time(){return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
void vTaskDelay(TickType_t ticks){CHECK(ticks==20);std::this_thread::sleep_for(std::chrono::milliseconds(1));}
extern "C" void dg_deferred_log(const char*,...){++logs;}
namespace dg_board::touch {
esp_err_t init(){init_thread=std::this_thread::get_id();++init_calls;if(failures){--failures;return ESP_FAIL;}return ESP_OK;}
bool poll(uint32_t,bool enabled){++poll_calls;last_accept=enabled;const bool tapped=(physical || driver) && enabled;
    if(driver)++injected;
    physical=driver=false;
    if(during_poll){auto hook=std::move(during_poll);during_poll={};hook();}
    return tapped;
}
TouchStatus status(){TouchStatus s;s.initialized=true;s.injected=injected;return s;}
}
int main(){
    Trace trace;dg::DuoTouch touch(Trace::send,&trace);touch.admission(true,7);failures=3;
    touch.poll(0);CHECK(init_calls==1 && logs==1 && !poll_calls);
    for(unsigned now=20;now<1000;now+=20)touch.poll(now);
    CHECK(init_calls==1);touch.poll(1000);CHECK(init_calls==2);
    touch.poll(2999);CHECK(init_calls==2);touch.poll(3000);CHECK(init_calls==3);
    touch.poll(6999);CHECK(init_calls==3);touch.poll(7000);CHECK(init_calls==4 && poll_calls==1);
    touch.poll(7001);CHECK(poll_calls==1);physical=true;touch.poll(7020);
    CHECK(trace.taps.size()==1 && !trace.taps[0].injected && trace.taps[0].generation==7);
    driver=true;touch.poll(7040);CHECK(trace.taps.size()==2 && trace.taps[1].injected);
    touch.admission(false,7);physical=true;touch.poll(7060);CHECK(!last_accept && trace.taps.size()==2);
    touch.admission(true,7);physical=true;during_poll=[&]{touch.admission(false,7);touch.admission(true,7);};
    touch.poll(7080);CHECK(trace.taps.size()==2); // same-generation admission change
    physical=true;during_poll=[&]{touch.admission(true,9);};touch.poll(7100);CHECK(trace.taps.size()==2);
    physical=true;touch.poll(7120);CHECK(trace.taps.size()==3 && trace.taps.back().generation==9);
    // Backoff caps at 30s, rather than retrying hardware on every 20ms poll.
    Trace unused;dg::DuoTouch absent(Trace::send,&unused);failures=10;const auto count=init_calls.load();
    for(auto now:{0,1000,3000,7000,15000,31000,61000})absent.poll(now);
    CHECK(init_calls==count+7);absent.poll(90999);CHECK(init_calls==count+7);absent.poll(91000);CHECK(init_calls==count+8);
    // Exercise the actual thread entry/configuration, not an app-control poll.
    failures=0;const auto before=init_calls.load();dg::DuoTouch worker(Trace::send,&unused);
    worker.admission(true,12);worker.start();
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
    while(init_calls==before && std::chrono::steady_clock::now()<deadline)std::this_thread::yield();
    worker.stop();CHECK(init_calls==before+1 && init_thread!=std::this_thread::get_id());
    CHECK(launched.stack_size==4096 && launched.stack_alloc_caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    CHECK(launched.pin_to_core==0 && launched.prio==2 && !std::strcmp(launched.thread_name,"duo-touch"));
    std::puts("duo touch: PSRAM thread, 20ms cadence, init backoff, source and generation/admission fences passed");
}
