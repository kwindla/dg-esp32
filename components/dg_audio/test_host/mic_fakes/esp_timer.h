#pragma once
#include <cstdint>
inline int64_t esp_timer_get_time() {static int64_t t=1000000;return t+=20000;}
