#pragma once
#include "esp_err.h"
#include <cstdarg>
using vprintf_like_t=int(*)(const char*,va_list);
inline vprintf_like_t esp_log_set_vprintf(vprintf_like_t p){return p;}
