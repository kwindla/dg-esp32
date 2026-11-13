#include "dg_diagnostics.h"
#include "dg_console.h"
#include "dg_heap_region.h"
#include "sdkconfig.h"
#include "esp_heap_caps.h"
#include "heap_private.h"
#include "esp_timer.h"
#include "esp_log.h"
// IDF v6.1 private bounded log-level lookup; verify this API on IDF upgrades.
#include "esp_private/log_level.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <cstdarg>
#include <cstdio>
#include <atomic>
#include <algorithm>
#include <cstring>
#include <new>
namespace {
constexpr size_t log_capacity=128, log_bytes=192;
QueueHandle_t log_queue=nullptr;
StaticQueue_t log_control;
std::atomic<unsigned> log_drops{0};
void log_worker(void*) {
    char line[log_bytes];
    for (;;) if(xQueueReceive(log_queue,line,portMAX_DELAY)==pdTRUE) {
        // The stdio wrapper queues with zero wait, including on absent USB.
        std::printf("%s%s",line,line[0] && line[std::strlen(line)-1]=='\n'?"":"\n");
    }
}
constexpr size_t capacity = 128, max_heaps = 12;
struct Region { uintptr_t start; size_t length; multi_heap_info_t info; unsigned kind; };
struct Sample {
    char name[32], task[20]; bool transport; int64_t time; unsigned stack;
    unsigned free, largest, minimum, dma, psram, main_largest, reserve_free, rtc_free, count;
    Region regions[max_heaps];
};
[[maybe_unused]] Sample* samples;
[[maybe_unused]] portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
[[maybe_unused]] unsigned head, tail, overflow;
std::atomic<int64_t> start_request{0}, dtls_start{0};
std::atomic<int64_t> ready_time{0};
std::atomic<unsigned> dtls_us{0}, sampled_min{UINT32_MAX}, dtls_local_min{0};
[[maybe_unused]] esp_timer_handle_t sampler;
[[maybe_unused]] void sample_min(void*) {
    if (!dtls_start.load()) return;
    unsigned value = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    unsigned old = sampled_min.load();
    while (value < old && !sampled_min.compare_exchange_weak(old, value)) {}
}
}
static int deferred_logv(const char* format, va_list args) {
    char line[log_bytes]{};
    int n=vsnprintf(line,sizeof(line),format,args);
    if(n<0 || size_t(n)>=sizeof(line) || !log_queue || xQueueSend(log_queue,line,0)!=pdTRUE)
        log_drops.fetch_add(1,std::memory_order_relaxed);
    return n;
}
extern "C" void dg_deferred_log(const char* format, ...) {
    va_list args;va_start(args,format);deferred_logv(format,args);va_end(args);
}
// Intercept vendor/driver error logs before ESP logging or stdio takes its
// output lock. Console and boot callers retain the normal synchronous sink.
static bool realtime_log_context() {
    if(xPortInIsrContext() || xTaskGetSchedulerState()==taskSCHEDULER_NOT_STARTED)return false;
    const char* name=pcTaskGetName(nullptr);
    return !std::strcmp(name,"audio-capture") || !std::strcmp(name,"audio-playback") ||
        !std::strcmp(name,"audio-tx") || !std::strcmp(name,"tone-source") ||
        !std::strcmp(name,"peer-loop") || !std::strcmp(name,"peer-rx");
}
extern "C" void __real_esp_log_va(esp_log_config_t,const char*,const char*,va_list);
extern "C" void __real_esp_log_writev(esp_log_level_t,const char*,const char*,va_list);
extern "C" int __real_vprintf(const char*,va_list);
extern "C" int __real_puts(const char*);
extern "C" void __wrap_esp_log(esp_log_config_t config,const char* tag,const char* format,...) {
    if(!dg_console::ready() && !dg_console::connected())return;
    if(config.opts.log_level<=ESP_LOG_NONE || esp_log_level_get_timeout(tag)<config.opts.log_level)return;
    va_list args;va_start(args,format);
    if(realtime_log_context())deferred_logv(format,args);else __real_esp_log_va(config,tag,format,args);
    va_end(args);
}
extern "C" void __wrap_esp_log_va(esp_log_config_t config,const char* tag,const char* format,va_list args) {
    if(!dg_console::ready() && !dg_console::connected())return;
    if(config.opts.log_level<=ESP_LOG_NONE || esp_log_level_get_timeout(tag)<config.opts.log_level)return;
    if(realtime_log_context())deferred_logv(format,args);else __real_esp_log_va(config,tag,format,args);
}
extern "C" void __wrap_esp_log_write(esp_log_level_t level,const char* tag,const char* format,...) {
    if(!dg_console::ready() && !dg_console::connected())return;
    if(level<=ESP_LOG_NONE || esp_log_level_get_timeout(tag)<level)return;
    va_list args;va_start(args,format);
    if(realtime_log_context())deferred_logv(format,args);else __real_esp_log_writev(level,tag,format,args);
    va_end(args);
}
extern "C" void __wrap_esp_log_writev(esp_log_level_t level,const char* tag,const char* format,va_list args) {
    if(!dg_console::ready() && !dg_console::connected())return;
    if(level<=ESP_LOG_NONE || esp_log_level_get_timeout(tag)<level)return;
    if(realtime_log_context())deferred_logv(format,args);else __real_esp_log_writev(level,tag,format,args);
}
extern "C" int __wrap_vprintf(const char* format,va_list args) {
    if(!dg_console::ready() && !dg_console::connected())return 0;
    if(realtime_log_context())return deferred_logv(format,args);
    if(!dg_console::ready())return __real_vprintf(format,args);
    flockfile(stdout);
    const int n=dg_console::vprint(format,args);
    funlockfile(stdout);return n;
}
extern "C" int __wrap_printf(const char* format,...) {
    va_list args;va_start(args,format);int n=__wrap_vprintf(format,args);va_end(args);return n;
}
static int deferred_puts(const char* line) {
    // Deferred success is not delivery: truncation/full-queue drops are reported
    // by dg_deferred_log_drops(), rather than the synchronous puts EOF contract.
    dg_deferred_log("%s\n",line);return int(std::strlen(line)+1);
}
extern "C" int __wrap_puts(const char* line) {
    if(!dg_console::ready() && !dg_console::connected())return 0;
    if(!realtime_log_context()) {
        if(!dg_console::ready())return __real_puts(line);
        flockfile(stdout);
        dg_console::write(line,std::strlen(line));dg_console::write("\n",1);
        funlockfile(stdout);return int(std::strlen(line)+1);
    }
    return deferred_puts(line);
}
extern "C" unsigned dg_deferred_log_drops() {return log_drops.load(std::memory_order_relaxed);}
extern "C" void dg_diagnostics_init() {
    auto* storage=static_cast<uint8_t*>(heap_caps_malloc(log_capacity*log_bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!storage)throw std::bad_alloc();
    log_queue=xQueueCreateStatic(log_capacity,log_bytes,storage,&log_control);
    if(xTaskCreatePinnedToCoreWithCaps(log_worker,"diagnostic-log",4096,nullptr,1,nullptr,0,
        MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)!=pdPASS)throw std::bad_alloc();
#if CONFIG_DG_PHASE1_DIAGNOSTICS
    samples = static_cast<Sample*>(heap_caps_calloc(capacity, sizeof(Sample), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!samples) throw std::bad_alloc();
    esp_timer_create_args_t args{}; args.callback = sample_min; args.name = "dtls-heap";
    ESP_ERROR_CHECK(esp_timer_create(&args, &sampler));
#endif
}
extern "C" void dg_heap_phase(const char* name, bool transport) {
    Sample s{}; std::strncpy(s.name, name, sizeof(s.name)-1); s.transport = transport;
    std::strncpy(s.task, pcTaskGetName(nullptr), sizeof(s.task)-1);
    s.stack=uxTaskGetStackHighWaterMark(nullptr);
    s.time = esp_timer_get_time(); s.free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    s.largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    s.minimum = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    s.dma = heap_caps_get_largest_free_block(MALLOC_CAP_DMA); s.psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
#if CONFIG_DG_PHASE1_DIAGNOSTICS
    // Registry is immutable after startup/reserve registration. The allocator
    // locks each region; this is a phase snapshot, not an atomic cross-heap sum.
    heap_t* h;
    SLIST_FOREACH(h, &registered_heaps, next) {
        if (!h->heap || s.count == max_heaps) continue;
        auto& r = s.regions[s.count++]; r.start=h->start; r.length=h->end-h->start;
        multi_heap_get_info(h->heap, &r.info);
        r.kind=dg_console::heap_region(h->start,get_all_caps(h)&MALLOC_CAP_INTERNAL,h->caps[0]==0);
        if(r.kind==4)s.rtc_free+=r.info.total_free_bytes;
        if(r.kind==3)s.reserve_free+=r.info.total_free_bytes;
        if(r.kind==0)s.main_largest=std::max(s.main_largest,unsigned(r.info.largest_free_block));
    }
    portENTER_CRITICAL(&mux);
    if (head-tail < capacity) samples[head++ % capacity] = s; else ++overflow;
    portEXIT_CRITICAL(&mux);
#else
    dg_deferred_log("HEAP phase=%s internal=%u largest=%u minimum=%u dma=%u psram=%u", name,s.free,s.largest,s.minimum,s.dma,s.psram);
#endif
}
extern "C" void dg_diagnostics_flush() {
#if CONFIG_DG_PHASE1_DIAGNOSTICS
    static const char* kinds[]={"main","rom","sram2","reserve","rtc","external"};
    for (;;) {
        Sample s{}; bool got;
        portENTER_CRITICAL(&mux); got=tail!=head; if(got) s=samples[tail++ % capacity]; portEXIT_CRITICAL(&mux);
        if(!got) break;
        unsigned lb=s.free>CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL?s.free-CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL:0;
        unsigned excluded=s.reserve_free+s.rtc_free;
        ESP_LOGI(s.transport?"esp-peer":"dg-esp32", "%s%s t=%lld internal=%u largest=%u minimum=%u dma=%u psram=%u reserve=%u excluding_reserve_lb=%u main_largest=%u reserve_free=%u rtc_free=%u general_internal=%u task=%s stack=%u",
            s.transport?"PHASE ":"HEAP phase=",s.name,(long long)s.time,s.free,s.largest,s.minimum,s.dma,s.psram,
            CONFIG_SPIRAM_MALLOC_RESERVE_INTERNAL,lb,s.main_largest,s.reserve_free,s.rtc_free,s.free>excluded?s.free-excluded:0,s.task,s.stack);
        // Deferred output only: let USB-Serial/JTAG drain between census
        // lines. Sampling and all media callbacks remain free of this wait.
        vTaskDelay(1);
        for(unsigned i=0;i<s.count;++i) {
            const auto& r=s.regions[i];
            ESP_LOGI("dg-diag","REGION phase=%s t=%lld kind=%s start=0x%08x length=%u free=%u largest=%u minimum=%u",
                s.name,(long long)s.time,kinds[r.kind],unsigned(r.start),unsigned(r.length),unsigned(r.info.total_free_bytes),
                unsigned(r.info.largest_free_block),unsigned(r.info.minimum_free_bytes));
            vTaskDelay(1);
        }
    }
    ESP_LOGI("dg-diag","DTLS duration_us=%u sampled_min=%u local_min_sum=%u sample_period_us=1000 overflow=%u",
        dtls_us.load(),sampled_min.load(),dtls_local_min.load(),overflow);
#endif
}
extern "C" void dg_start_request() { ready_time=0;start_request=esp_timer_get_time(); }
extern "C" dg_connection_timings_t dg_connection_timings() {return {start_request.load(),ready_time.load()};}
extern "C" void dg_bot_ready() {
    const auto now=esp_timer_get_time();
    ready_time=now;
    dg_deferred_log("START_READY request_us=%lld ready_us=%lld duration_us=%lld",(long long)start_request.load(),(long long)now,(long long)(now-start_request.load()));
}
extern "C" uint32_t dg_dtls_duration_us() { return dtls_us.load(); }
extern "C" void dg_dtls_cancel() {
#if CONFIG_DG_PHASE1_DIAGNOSTICS
    if(dtls_start.exchange(0)) {
        esp_timer_stop(sampler); heap_caps_monitor_local_minimum_free_size_stop();
    }
#endif
}
extern "C" int __real_dtls_srtp_handshake(void*);
extern "C" int __wrap_dtls_srtp_handshake(void* ctx) {
#if CONFIG_DG_PHASE1_DIAGNOSTICS
    if(!dtls_start.load()) {
        dtls_us=0; sampled_min=heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
        heap_caps_monitor_local_minimum_free_size_start();
        dtls_start=esp_timer_get_time(); ESP_ERROR_CHECK(esp_timer_start_periodic(sampler,1000));
        dg_heap_phase("dtls-entry",true);
    }
#endif
    int ret=__real_dtls_srtp_handshake(ctx);
#if CONFIG_DG_PHASE1_DIAGNOSTICS
    if(ret==0) {
        dtls_us=esp_timer_get_time()-dtls_start.load();
        dtls_local_min=heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
        esp_timer_stop(sampler); heap_caps_monitor_local_minimum_free_size_stop(); dtls_start=0;
        dg_heap_phase("dtls-exit",true);
    }
#endif
    return ret;
}
