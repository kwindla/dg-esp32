#pragma once
#include <cstddef>
constexpr unsigned MALLOC_CAP_INTERNAL=1,MALLOC_CAP_8BIT=2,MALLOC_CAP_SPIRAM=4;
void* heap_caps_calloc(size_t,size_t,unsigned);
void heap_caps_free(void*);
size_t heap_caps_get_free_size(unsigned);
size_t heap_caps_get_largest_free_block(unsigned);
