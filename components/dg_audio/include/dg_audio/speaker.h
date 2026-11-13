#pragma once
#include <cstddef>
#include <cstdint>
#include <atomic>
#include <functional>
#include "speaker_clock.h"
#include "driver/i2s_common.h"
struct i2s_channel_obj_t;
namespace dg_audio {
class Speaker {
public:
    Speaker(); ~Speaker();
    Speaker(const Speaker&) = delete;
    Speaker& operator=(const Speaker&) = delete;
    void start();
    SpeakerClock::Opportunity opportunity(int64_t now_us);
    bool render(const dg::RenderTick& tick) {return clock_.rendered(tick);}
    bool valid(const dg::RenderTick& tick);
    SpeakerClock& clock() {return clock_;}
    size_t refill(unsigned samples);
    size_t write(const int16_t* mono, size_t frames, uint32_t timeout_ms, bool eligible=true);
    std::function<void(bool,int64_t,int64_t)> onset_observer;
    uint64_t errors() const { return errors_; }
    uint64_t timeouts() const { return timeouts_; }
    void idle();
    bool running() const { return enabled_; }
    // Drain the already committed tail while scheduled zero writes keep the
    // driver cursor current. Silence written during this drain is not a tail.
    void begin_drain() { if(!draining_) {drain_target_=submitted_.load();draining_=true;} }
    bool drained() const { return int32_t(completed_.load()-(draining_?drain_target_:submitted_.load()))>=0; }
    // Empty source queue is intentional silence, while DMA may still drain.
    void pause_monitoring() { monitoring_=false; }
    unsigned underflows() const { return underflows_.load(); }
    unsigned queue_overflows() const { return queue_overflows_.load(); }
    bool audible() const { return audible_; }
    void audible_threshold(unsigned value) { audible_threshold_=value; }
    void volume(unsigned q15) { volume_ = q15 > 32768 ? 32768 : q15; peak_=0; }
    unsigned peak() const { return peak_.load(); }
private:
    static bool sent(i2s_chan_handle_t, i2s_event_data_t*, void*);
    static bool overflow(i2s_chan_handle_t, i2s_event_data_t*, void*);
    std::atomic<unsigned> submitted_{0}, completed_{0}, underflows_{0}, queue_overflows_{0};
    std::atomic<unsigned> callback_us_{0}, callback_sequence_{0};
    SpeakerClock clock_;
    std::atomic<bool> monitoring_{false};
    bool enabled_=false, draining_=false;
    unsigned drain_target_=0, refill_remaining_=0;
    i2s_channel_obj_t* channel_ = nullptr;
    int16_t* stereo_ = nullptr;
    std::atomic<unsigned> peak_{0};
    std::atomic<unsigned> volume_{32768};
    unsigned audible_threshold_=32;
    bool audible_ = false;
    uint64_t errors_ = 0, timeouts_ = 0;
};
}
