#pragma once
#include <cstddef>
#include <cstdint>
namespace dg {
constexpr size_t HeapNewCapacity=8192;
bool heap_new_init();
bool heap_new_ready();
void heap_new_begin();
void heap_new_end();
int heap_new_stop(int (*)());
void heap_new_remember(void*,uintptr_t);
void heap_new_forget(void*);
uintptr_t heap_new_caller(const void*);
bool heap_new_overflow();
}
