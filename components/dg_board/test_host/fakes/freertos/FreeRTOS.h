#pragma once
#include <cstdint>
using TickType_t=unsigned;
constexpr TickType_t portMAX_DELAY=~TickType_t(0);
#define pdMS_TO_TICKS(ms) (ms)
constexpr int pdTRUE=1;
