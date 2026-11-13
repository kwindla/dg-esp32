#pragma once
#include "sdkconfig.h"
#if CONFIG_DG_PLAYBACK_BREAKDOWN
#include "esp_timer.h"
#include <atomic>
#include <algorithm>
namespace dg {
struct PlaybackCost {
    std::atomic<uint64_t> calls{0},wall_us{0};
    std::atomic<unsigned> max_us{0};
    void add(int64_t began) {
        unsigned elapsed=esp_timer_get_time()-began;
        wall_us+=elapsed;++calls;max_us=std::max(max_us.load(),elapsed);
    }
};
}
#endif
