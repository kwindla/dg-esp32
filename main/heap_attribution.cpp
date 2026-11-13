#include "heap_attribution.h"
#include "heap_new_callers.h"
#include "heap_tcp_state.h"
#include "esp_heap_caps.h"
#include "esp_heap_trace.h"
#include "esp_memory_utils.h"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>

namespace dg { namespace {
constexpr unsigned Capacity=4096,Depth=8,Page=3;
static_assert(CONFIG_HEAP_TRACING_STACK_DEPTH==Depth,"diagnosis needs eight allocation frames");
heap_trace_record_t* buffer=nullptr;
unsigned window=0;
bool running=false,stopped=false;
heap_trace_summary_t summary{};
size_t before_free=0,before_largest=0;
void checked(esp_err_t error){if(error!=ESP_OK)throw std::runtime_error("heap attribution API failed");}
void header(const char* kind,void (*out)(const char*)){char line[640];
    std::snprintf(line,sizeof line,"\nHEAP_ATTRIBUTION {\"schema\":1,\"kind\":\"%s\",\"id\":%u,\"capacity\":%u,\"depth\":%u,\"records\":%u,\"overflow\":%s,\"high_water\":%u,\"allocations\":%u,\"frees\":%u,\"before_free\":%u,\"before_largest\":%u,\"after_free\":%u,\"after_largest\":%u,\"isr_allocations\":\"excluded-PSRAM-buffer\",\"new_caller_capacity\":%u,\"new_caller_overflow\":%s}\n",
        kind,window,Capacity,Depth,unsigned(summary.count),summary.has_overflowed?"true":"false",unsigned(summary.high_water_mark),unsigned(summary.total_allocations),unsigned(summary.total_frees),unsigned(before_free),unsigned(before_largest),unsigned(heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)),unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)),unsigned(HeapNewCapacity),heap_new_overflow()?"true":"false");out(line);
}
} // namespace
void heap_attribution_command(const char* args,bool idle,void (*out)(const char*)){
    if(!idle)throw std::runtime_error("heap attribution commands require Idle; export after session closes");
    char action[16]{},value[32]{},extra=0;unsigned page=0;
    const int fields=std::sscanf(args,"%15s %31s %c",action,value,&extra);
    if(fields<1 || (std::strcmp(action,"page")?fields!=1:fields!=2) ||
        (fields==2 && (std::strspn(value,"0123456789")!=std::strlen(value) || std::strlen(value)>4)))throw std::runtime_error("heap-attribution init|start|stop|page N");
    if(fields==2)page=unsigned(std::strtoul(value,nullptr,10));
    if(!std::strcmp(action,"tcp")){heap_tcp_state(window,out);return;}
    if(!std::strcmp(action,"init")){
        if(running)throw std::runtime_error("trace still running");
        if(!buffer){
            buffer=static_cast<heap_trace_record_t*>(heap_caps_calloc(Capacity,sizeof(*buffer),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
            if(!buffer)throw std::runtime_error("heap attribution PSRAM buffer unavailable");
            const auto error=heap_trace_init_standalone(buffer,Capacity);
            if(error!=ESP_OK){heap_caps_free(buffer);buffer=nullptr;checked(error);}
        }
        if(!heap_new_init())throw std::runtime_error("C++ caller PSRAM table unavailable");
        header("init",out);return;
    }
    if(!buffer || !heap_new_ready())throw std::runtime_error("heap-attribution init first");
    if(!std::strcmp(action,"start")){
        if(running || window==UINT32_MAX)throw std::runtime_error("invalid heap attribution window");
        before_free=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
        before_largest=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
        heap_new_begin();const auto error=heap_trace_start(HEAP_TRACE_LEAKS);
        if(error!=ESP_OK){heap_new_end();checked(error);}
        running=true;stopped=false;++window;summary={};header("start",out);return;
    }
    if(!std::strcmp(action,"stop")){
        if(!running)throw std::runtime_error("no running heap attribution window");
        checked(heap_new_stop(heap_trace_stop));running=false;checked(heap_trace_summary(&summary));stopped=true;header("stop",out);return;
    }
    if(std::strcmp(action,"page") || !stopped || running || page>=(summary.count+Page-1)/Page)throw std::runtime_error("invalid stopped heap attribution page");
    const unsigned first=page*Page,end=unsigned(summary.count)<first+Page?unsigned(summary.count):first+Page;
    for(unsigned index=first;index<end;++index){
        heap_trace_record_t record{};checked(heap_trace_get(index,&record));char line[512];
        int at=std::snprintf(line,sizeof line,"\nHEAP_ATTRIBUTION {\"schema\":1,\"kind\":\"block\",\"id\":%u,\"index\":%u,\"address\":%u,\"bytes\":%u,\"internal\":%s,\"freed\":%s,\"callers\":[",window,index,unsigned(reinterpret_cast<uintptr_t>(record.address)),unsigned(record.size),esp_ptr_internal(record.address)?"true":"false",record.freed?"true":"false");
        for(unsigned i=0;i<Depth;++i)at+=std::snprintf(line+at,sizeof(line)-at,"%s%u",i?",":"",unsigned(reinterpret_cast<uintptr_t>(record.alloced_by[i])));
        std::snprintf(line+at,sizeof(line)-at,"],\"new_caller\":%u,\"external\":%s}\n",unsigned(heap_new_caller(record.address)),esp_ptr_external_ram(record.address)?"true":"false");out(line);
    }
    char line[160];std::snprintf(line,sizeof line,"\nHEAP_ATTRIBUTION {\"schema\":1,\"kind\":\"page\",\"id\":%u,\"first\":%u,\"next\":%u}\n",window,first,end);out(line);
}
} // namespace dg
