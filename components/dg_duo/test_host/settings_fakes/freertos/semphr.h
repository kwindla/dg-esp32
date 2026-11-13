#pragma once
#include "FreeRTOS.h"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>
struct StaticSemaphore_t {std::mutex mutex;std::condition_variable changed;bool ready=false;};
using SemaphoreHandle_t=StaticSemaphore_t*;
inline std::atomic<unsigned> settings_fake_waiters{0};
inline SemaphoreHandle_t xSemaphoreCreateBinaryStatic(StaticSemaphore_t* p){p->ready=false;return p;}
inline int xSemaphoreTake(SemaphoreHandle_t p,TickType_t ms){
    std::unique_lock<std::mutex> lock(p->mutex);
    if(ms){++settings_fake_waiters;const bool ready=p->changed.wait_for(lock,std::chrono::milliseconds(ms),[&]{return p->ready;});--settings_fake_waiters;if(!ready)return 0;}
    if(!p->ready)return 0;
    p->ready=false;return pdTRUE;
}
inline int xSemaphoreGive(SemaphoreHandle_t p){{std::lock_guard<std::mutex> lock(p->mutex);p->ready=true;}p->changed.notify_one();return pdTRUE;}
