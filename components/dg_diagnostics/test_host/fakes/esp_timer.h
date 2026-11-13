#pragma once
#include <cstdint>
struct esp_timer_create_args_t {void (*callback)(void*);const char* name;};
using esp_timer_handle_t=void*;
int esp_timer_create(const esp_timer_create_args_t*,esp_timer_handle_t*);
int esp_timer_start_once(esp_timer_handle_t,uint64_t);
