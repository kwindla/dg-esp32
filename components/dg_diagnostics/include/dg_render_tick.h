#pragma once
#include <cstdint>
#include <atomic>
namespace dg {
// Published by Pipeline on its consumer thread; the app's provider reads it
// under the existing media lease. No wait or borrowed session lives here.
struct RenderTick {
    uint64_t device_epoch=0, index=0, completed_samples=0, submitted_samples=0;
    int64_t deadline_us=0;
    uint32_t silence_blocks=0, silence_samples=0, remainder_samples=0;
    bool valid=false;

};
inline thread_local RenderTick render_tick;
struct DeviceProgress {uint64_t epoch=0, expired_ticks=0;bool valid=false;};
// One speaker publishes its raw DMA completions. Readers extend no wall clock;
// admission uses only fully completed samples to retire expired intervals.
class DeviceClock {
    std::atomic<uint32_t> sequence_{0}, epoch_{0}, completed_{0}, preload_{0};
    std::atomic<bool> running_{false};
public:
    void start(uint64_t epoch,unsigned preload) {
        ++sequence_;epoch_=uint32_t(epoch);completed_=0;preload_=preload;running_=true;++sequence_;
    }
    void stop() {running_=false;}
    void completed(unsigned samples) {completed_=samples;}
    DeviceProgress snapshot() const {
        uint32_t sequence,epoch,completed,preload;bool running;
        do {sequence=sequence_.load();epoch=epoch_;completed=completed_;preload=preload_;running=running_;}
        while((sequence&1) || sequence_.load()!=sequence);
        return {epoch,completed>=preload?uint64_t(completed-preload)/320+1:0,running};
    }
};
inline DeviceClock device_clock;
}
