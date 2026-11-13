#include "heap_new_callers.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include <atomic>
#include <cstring>

namespace dg { namespace {
struct Entry {void* address;uintptr_t caller;};
Entry* entries=nullptr;
std::atomic<bool> active{false},overflow{false};
portMUX_TYPE mux=portMUX_INITIALIZER_UNLOCKED;
const auto Tombstone=reinterpret_cast<void*>(uintptr_t(1));
size_t slot(const void* p){return ((reinterpret_cast<uintptr_t>(p)>>4)*2654435761u)&(HeapNewCapacity-1);}
struct Guard {Guard(){portENTER_CRITICAL(&mux);}~Guard(){portEXIT_CRITICAL(&mux);}};
}
bool heap_new_init(){
    if(!entries)entries=static_cast<Entry*>(heap_caps_calloc(HeapNewCapacity,sizeof(Entry),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    return entries!=nullptr;
}
bool heap_new_ready(){return entries!=nullptr;}
void heap_new_begin(){Guard guard;active=false;if(!entries){overflow=true;return;}std::memset(entries,0,HeapNewCapacity*sizeof(Entry));overflow=false;active=true;}
void heap_new_end(){Guard guard;active=false;}
int heap_new_stop(int (*stop)()){Guard guard;const int result=stop();if(!result)active=false;return result;}
bool heap_new_overflow(){return overflow.load();}
void heap_new_remember(void* p,uintptr_t caller){
    if(!p || !active.load())return;
    if(xPortInIsrContext()){overflow=true;return;}
    Guard guard;if(!active.load())return;
    size_t first=HeapNewCapacity;
    for(size_t n=0,index=slot(p);n<HeapNewCapacity;++n,index=(index+1)&(HeapNewCapacity-1)){
        auto& entry=entries[index];
        if(entry.address==p){entry.caller=caller;return;}
        if(entry.address==Tombstone && first==HeapNewCapacity)first=index;
        if(!entry.address){entries[first==HeapNewCapacity?index:first]={p,caller};return;}
    }
    if(first!=HeapNewCapacity)entries[first]={p,caller};else overflow=true;
}
void heap_new_forget(void* p){
    if(!p || !active.load())return;
    if(xPortInIsrContext()){overflow=true;return;}
    Guard guard;if(!active.load())return;
    for(size_t n=0,index=slot(p);n<HeapNewCapacity;++n,index=(index+1)&(HeapNewCapacity-1)){
        auto& entry=entries[index];if(!entry.address)return;
        if(entry.address==p){entry={Tombstone,0};return;}
    }
}
uintptr_t heap_new_caller(const void* p){
    if(!entries || !p)return 0;
    Guard guard;
    for(size_t n=0,index=slot(p);n<HeapNewCapacity;++n,index=(index+1)&(HeapNewCapacity-1)){
        auto& entry=entries[index];if(!entry.address)return 0;
        if(entry.address==p)return entry.caller;
    }
    return 0;
}
}
