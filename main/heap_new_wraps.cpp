#include "heap_new_callers.h"
#include <new>
#define HEAP_NEW(symbol,parameters,arguments) \
    extern "C" void* __real_##symbol parameters; \
    extern "C" __attribute__((noinline)) void* __wrap_##symbol parameters { \
        void* p=__real_##symbol arguments; \
        dg::heap_new_remember(p,reinterpret_cast<uintptr_t>(__builtin_return_address(0)));return p;}
#define HEAP_DELETE(symbol,parameters,arguments) \
    extern "C" void __real_##symbol parameters; \
    extern "C" __attribute__((noinline)) void __wrap_##symbol parameters { \
        dg::heap_new_forget(p);__real_##symbol arguments;}
#include "heap_new_wraps.inc"
