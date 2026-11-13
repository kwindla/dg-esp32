#pragma once
#include <mutex>
using TaskHandle_t=void*;
struct portMUX_TYPE {std::mutex mutex;};
#define portMUX_INITIALIZER_UNLOCKED {}
#define portENTER_CRITICAL(p) ((p)->mutex.lock())
#define portEXIT_CRITICAL(p) ((p)->mutex.unlock())
#define pdPASS 1
#define pdMS_TO_TICKS(ms) (ms)
