#pragma once
#include <cstddef>
#include <cstdint>
#define CONFIG_HEAP_TRACING_STACK_DEPTH 8
using esp_err_t=int;
constexpr int ESP_OK=0;
enum heap_trace_mode_t {HEAP_TRACE_LEAKS};
struct heap_trace_record_t {void* address;size_t size;bool freed;void* alloced_by[8];};
struct heap_trace_summary_t {size_t count,has_overflowed,high_water_mark,total_allocations,total_frees;};
esp_err_t heap_trace_init_standalone(heap_trace_record_t*,size_t);
esp_err_t heap_trace_start(heap_trace_mode_t);
esp_err_t heap_trace_stop();
esp_err_t heap_trace_summary(heap_trace_summary_t*);
esp_err_t heap_trace_get(size_t,heap_trace_record_t*);
