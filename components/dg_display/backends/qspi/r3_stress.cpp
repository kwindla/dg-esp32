#include "dg_display/r3_stress.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <atomic>
#include <cstring>
#include <cstdio>
#include <algorithm>
#include <stdexcept>
namespace dg_display {
namespace {
constexpr unsigned BufferBytes=262144,ChunkBytes=8192,Caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
std::atomic<bool> running{false},exited{true},active{false},dma_started_active{false};
std::atomic<int64_t> started{0},ended{0},burst_start{0},burst_end{0};
std::atomic<uint64_t> copied{0},busy_us{0},bursts{0},overlaps{0};
unsigned burst_us=0,period_us=0,phase_us=0,stress_core=1;uint8_t *source=nullptr,*destination=nullptr;
volatile uint32_t checksum=0;
using Copy=void*(*)(void*,const void*,size_t);
// Indirect volatile call plus observed destination bytes prevents dead-store removal.
Copy volatile copy_routine=std::memcpy;
void worker(void*){
    if(phase_us)vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS((phase_us+999)/1000)));
    started=esp_timer_get_time();unsigned offset=0;
    while(running.load()){
        const auto began=esp_timer_get_time();burst_start=began;burst_end=0;active=true;
        uint64_t bytes=0;
        do{copy_routine(destination+offset,source+offset,ChunkBytes);checksum=checksum+destination[offset]+destination[offset+ChunkBytes-1];bytes+=ChunkBytes;offset=(offset+ChunkBytes)%BufferBytes;}while(running.load() && esp_timer_get_time()-began<burst_us);
        const auto done=esp_timer_get_time();burst_end=done;active=false;copied.fetch_add(bytes);busy_us.fetch_add(done-began);++bursts;
        const auto remaining=int64_t(period_us)-(done-began);
        // At least one tick idle per burst, including maximum nominal intensity.
        vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS(unsigned(std::max(int64_t(0),remaining))/1000)));
    }
    ended=esp_timer_get_time();exited=true;vTaskDeleteWithCaps(nullptr);
}
}
void r3_stress_start(unsigned burst,unsigned period,unsigned phase,unsigned core){
    if(running || !exited)throw std::runtime_error("stressor already running");
    if(core>1)throw std::runtime_error("stress core0|1");
    if(burst<500 || burst>20000 || period<=burst || period>2000000 || phase>=period)throw std::runtime_error("stress burst500..20000 period>burst..2000000 phase<period");
    source=static_cast<uint8_t*>(heap_caps_aligned_alloc(64,BufferBytes,Caps));destination=static_cast<uint8_t*>(heap_caps_aligned_alloc(64,BufferBytes,Caps));
    if(!source || !destination){heap_caps_free(source);heap_caps_free(destination);source=destination=nullptr;throw std::bad_alloc();}
    for(unsigned i=0;i<BufferBytes;++i)source[i]=uint8_t(i*17+(i>>8));
    std::memset(destination,0,BufferBytes);
    burst_us=burst;period_us=period;phase_us=phase;stress_core=core;copied=0;busy_us=0;bursts=0;overlaps=0;started=0;ended=0;burst_start=0;burst_end=0;checksum=0;exited=false;running=true;
    if(xTaskCreatePinnedToCoreWithCaps(worker,"r3-psram-copy",4096,nullptr,1,nullptr,int(stress_core),Caps)!=pdPASS){running=false;exited=true;heap_caps_free(source);heap_caps_free(destination);source=destination=nullptr;throw std::bad_alloc();}
}
void r3_stress_stop(){
    running=false;const auto began=esp_timer_get_time();while(!exited){if(esp_timer_get_time()-began>2000000)throw std::runtime_error("stressor stop timeout");vTaskDelay(1);}
    heap_caps_free(source);heap_caps_free(destination);source=destination=nullptr;
}
void r3_stress_dma_start() noexcept{dma_started_active=active.load();}
void r3_stress_dma_done(int64_t start,int64_t end) noexcept{
    const auto b=burst_start.load(),e=burst_end.load();
    if(dma_started_active.load() || active.load() || (b && b<=end && (!e || e>=start)))++overlaps;
}
void r3_stress_status(void (*out)(const char*)){
    char b[768];const auto now=ended.load()?ended.load():esp_timer_get_time();
    std::snprintf(b,sizeof(b),"\nDISPLAY_R3_STRESS {\"running\":%s,\"allocations_live\":%s,\"buffer_bytes_each\":%u,\"chunk_bytes\":%u,\"core\":%u,\"priority\":1,\"stack_bytes\":4096,\"burst_us\":%u,\"period_us\":%u,\"phase_us\":%u,\"elapsed_us\":%lld,\"busy_us\":%llu,\"copied_bytes\":%llu,\"bursts\":%llu,\"dma_overlap_stripes\":%llu,\"checksum\":%lu}\n",running?"true":"false",source?"true":"false",BufferBytes,ChunkBytes,stress_core,burst_us,period_us,phase_us,(long long)(started.load()?now-started.load():0),(unsigned long long)busy_us.load(),(unsigned long long)copied.load(),(unsigned long long)bursts.load(),(unsigned long long)overlaps.load(),(unsigned long)checksum);out(b);
}
}
