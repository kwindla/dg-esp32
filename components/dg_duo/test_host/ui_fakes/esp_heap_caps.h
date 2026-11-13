#pragma once
#include <cstddef>
constexpr unsigned MALLOC_CAP_SPIRAM=1,MALLOC_CAP_8BIT=2,MALLOC_CAP_INTERNAL=4;
size_t heap_caps_get_free_size(unsigned);
size_t heap_caps_get_minimum_free_size(unsigned);
