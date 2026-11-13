#pragma once
#include "pthread_config.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <atomic>
#include <thread>
#include <stdexcept>
namespace pipecat::esp_peer {
// Own the configured pthread stack for the transport's lifetime.
// Only the serialized connect/disconnect caller starts or joins session jobs.
// Notification slot 0 belongs to this worker; session methods do not use it.
class PersistentWorker {
    StaticSemaphore_t completion_control_{};
    SemaphoreHandle_t completion_;
    std::atomic<TaskHandle_t> handle_{nullptr};
    std::atomic<bool> shutdown_{false};
    std::atomic<void*> context_{nullptr};
    std::atomic<void (*)(void*)> job_{nullptr};
    bool pending_=false;
    std::thread thread_;
public:
    PersistentWorker(const char* name, unsigned stack, unsigned priority, int core,
                     unsigned caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
        : completion_(xSemaphoreCreateBinaryStatic(&completion_control_)) {
        dg::PthreadConfig threads;
        auto cfg=esp_pthread_get_default_config(); cfg.thread_name=name; cfg.stack_size=stack;
        cfg.prio=priority; cfg.pin_to_core=core; cfg.stack_alloc_caps=caps;

        try {
            threads.set(cfg);
            thread_=std::thread([this] {
                handle_=xTaskGetCurrentTaskHandle(); xSemaphoreGive(completion_);
                for(;;) {
                    ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
                    if(shutdown_.load()) break;
                    job_.load()(context_.load());
                    context_=nullptr; job_=nullptr; xSemaphoreGive(completion_);
                }
            });
        } catch (...) { vSemaphoreDelete(completion_); throw; }
        // Establish initialization before exposing the worker to connect.
        xSemaphoreTake(completion_,portMAX_DELAY);
    }
    ~PersistentWorker() {
        wait(); shutdown_=true; xTaskNotifyGive(handle_.load()); thread_.join(); vSemaphoreDelete(completion_);
    }
    void start(void* context, void (*job)(void*)) {
        if(pending_ || shutdown_.load()) throw std::runtime_error("Worker busy");
        pending_=true; context_=context; job_=job; xTaskNotifyGive(handle_.load());
    }
    void wait() {
        if(pending_) { xSemaphoreTake(completion_,portMAX_DELAY); pending_=false; }
    }
};
}
