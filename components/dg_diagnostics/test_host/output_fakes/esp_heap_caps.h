#pragma once
#define heap_caps_malloc hash_caps_malloc
#define heap_caps_free hash_caps_free
#include "../fakes/esp_heap_caps.h"
#undef heap_caps_malloc
#undef heap_caps_free
constexpr unsigned MALLOC_CAP_SPIRAM=4;
inline void* heap_caps_malloc(size_t n,unsigned caps){
    return (caps&MALLOC_CAP_SPIRAM)?std::malloc(n):hash_caps_malloc(n,caps);
}
inline void heap_caps_free(void* p){
    if(hash_fake::is_internal(p,1))hash_caps_free(p);else std::free(p);
}
