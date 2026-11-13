#pragma once
#include <algorithm>
#include <cstdlib>
#include <cstdint>
#include <stdexcept>
#include <vector>
constexpr unsigned MALLOC_CAP_INTERNAL=1,MALLOC_CAP_8BIT=2;
namespace hash_fake {
struct Range {const void* pointer;size_t bytes;};
inline std::vector<Range> internal,updates,operations,callers;
inline unsigned allocations=0;
inline bool inside(const Range& range,const void* pointer,size_t bytes) {
    auto at=reinterpret_cast<uintptr_t>(pointer),begin=reinterpret_cast<uintptr_t>(range.pointer);
    return at>=begin && at-begin<=range.bytes && bytes<=range.bytes-(at-begin);
}
inline bool is_internal(const void* pointer,size_t bytes) {
    return std::any_of(internal.begin(),internal.end(),[&](const auto& r){return inside(r,pointer,bytes);});
}
}
inline void* heap_caps_malloc(size_t bytes,unsigned caps) {
    if(caps!=(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT))throw std::runtime_error("Hash scratch caps");
    auto* result=std::malloc(bytes);if(result){hash_fake::internal.push_back({result,bytes});++hash_fake::allocations;}
    return result;
}
inline void heap_caps_free(void* pointer) {
    auto& ranges=hash_fake::internal;
    ranges.erase(std::remove_if(ranges.begin(),ranges.end(),[&](const auto& r){return r.pointer==pointer;}),ranges.end());
    std::free(pointer);
}
