#pragma once
#include <cstdlib>
#include <cstddef>
constexpr unsigned MALLOC_CAP_INTERNAL=1,MALLOC_CAP_8BIT=2,MALLOC_CAP_SPIRAM=4;
inline void* heap_caps_malloc(size_t n,unsigned) {return std::malloc(n);}
inline void* heap_caps_aligned_calloc(size_t,size_t n,size_t bytes,unsigned) {return std::calloc(n,bytes);}
inline size_t heap_caps_get_free_size(unsigned) {return 1000000;}
