#pragma once
#include <nlohmann/json.hpp>
#include <new>
namespace pipecat::esp_peer {
void* stat_allocate(size_t bytes);
void stat_free(void* pointer);
void stat_init();
// Diagnostics build nested JSON on non-media tasks. Explicit PSRAM allocation
// keeps histogram growth out of the media workers' internal-heap minimum.
template<class T> struct StatAllocator {
    using value_type=T;
    StatAllocator()=default;
    template<class U> StatAllocator(const StatAllocator<U>&) {}
    T* allocate(size_t n) {
        auto* p=static_cast<T*>(stat_allocate(n*sizeof(T)));
        if(!p)throw std::bad_alloc();
        return p;
    }
    void deallocate(T* p,size_t) {stat_free(p);}
    template<class U> bool operator==(const StatAllocator<U>&) const {return true;}
    template<class U> bool operator!=(const StatAllocator<U>&) const {return false;}
};
// The JSON allocator covers nodes/containers, but std::string's character
// buffer has its own allocator. Long keys (including every task's stack fields)
// must use the same PSRAM arena instead of dipping the internal heap per PERF.
using StatString=std::basic_string<char,std::char_traits<char>,StatAllocator<char>>;
using StatJson=nlohmann::basic_json<std::map,std::vector,StatString,bool,int64_t,uint64_t,double,StatAllocator>;
std::string stat_dump(const StatJson& value);
}
