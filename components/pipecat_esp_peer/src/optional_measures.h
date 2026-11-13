#pragma once
#include <cassert>
#include <cstddef>
#include <new>

namespace pipecat::esp_peer {
// Keep the always-enabled iteration counter resident. Optional detailed
// measurements use the SAME internal allocator, only when requested before
// the peer worker starts. No allocation, lookup lock or PSRAM in timer calls.
template<class T,size_t Count,size_t Hot>
class OptionalMeasures {
    static_assert(Hot<Count && Count>1);
public:
    using Allocate=void* (*)(size_t);
    using Release=void (*)(void*);
    OptionalMeasures(bool detailed,Allocate allocate,Release release):release_(release) {
        if(!detailed)return;
        auto* memory=static_cast<T*>(allocate(optional_bytes()));
        if(!memory)throw std::bad_alloc();
        size_t made=0;
        try {for(;made<Count-1;++made)new(memory+made)T;}
        catch(...) {
            while(made)memory[--made].~T();
            release_(memory);throw;
        }
        optional_=memory;
    }
    ~OptionalMeasures() {
        if(!optional_)return;
        for(size_t i=0;i<Count-1;++i)optional_[i].~T();
        release_(optional_);
    }
    OptionalMeasures(const OptionalMeasures&)=delete;
    OptionalMeasures& operator=(const OptionalMeasures&)=delete;
    static constexpr size_t optional_bytes() {return (Count-1)*sizeof(T);}
    bool enabled(size_t i) const {return i<Count && (i==Hot || optional_);}
    T& operator[](size_t i) {
        assert(enabled(i));return i==Hot?hot_:optional_[i<Hot?i:i-1];
    }
    const T& operator[](size_t i) const {
        assert(enabled(i));return i==Hot?hot_:optional_[i<Hot?i:i-1];
    }
private:
    T hot_{};
    T* optional_=nullptr;
    Release release_;
};
}
