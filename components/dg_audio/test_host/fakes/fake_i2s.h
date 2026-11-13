#pragma once
#include <cstdint>
#include <vector>
#include <functional>
#include <mutex>
#include <atomic>
namespace fake {
extern std::mutex mutex;
extern std::atomic<int64_t> now;
extern std::atomic<bool> mic_samples;
extern bool aec_enabled;
extern bool ring_enabled;
extern unsigned aec_gain;
extern std::vector<int16_t> submitted,submitted_stereo;
extern std::vector<unsigned> chunks;
extern std::function<void(unsigned)> on_write;
extern unsigned maximum_write, failures, writes;
// Legacy fault-injection tests can opt out of pacing; DMA tests use the ring.
void reset(bool use_ring=true);
void advance(unsigned ms);
}
