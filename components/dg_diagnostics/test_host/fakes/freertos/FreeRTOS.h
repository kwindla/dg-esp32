#pragma once
#include <cstdint>
using TaskHandle_t=void*;
using QueueHandle_t=void*;
constexpr unsigned pdTRUE=1,pdPASS=1,portMAX_DELAY=UINT32_MAX;
constexpr unsigned pdMS_TO_TICKS(unsigned ms) {return ms;}
