#pragma once
#include <mutex>
using portMUX_TYPE=std::mutex;
#define portMUX_INITIALIZER_UNLOCKED {}
#define portENTER_CRITICAL(mux) (mux)->lock()
#define portEXIT_CRITICAL(mux) (mux)->unlock()
inline bool heap_test_isr=false;
inline bool xPortInIsrContext(){return heap_test_isr;}
