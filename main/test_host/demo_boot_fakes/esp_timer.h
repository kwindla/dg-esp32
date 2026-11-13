#pragma once
#include <chrono>
inline long long esp_timer_get_time() {
 static const auto start=std::chrono::steady_clock::now();
 return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-start).count()*100;
}
