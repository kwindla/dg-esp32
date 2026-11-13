#pragma once
#include <cstddef>
#include <cstdint>
#include <atomic>
#include "aec_capture_clock.h"
#include "rx_capture_clock.h"
struct i2s_channel_obj_t;
namespace dg_audio {
class EchoCanceller;
struct MicStatus {
    uint64_t adc_clips=0,gain_clips=0,read_errors=0,read_timeouts=0,read_frames=0;
    unsigned rx_completions=0,rx_overflows=0;
};
MicStatus microphone_status();
class Mic {
public:
    Mic(); ~Mic();
    Mic(const Mic&) = delete;
    Mic& operator=(const Mic&) = delete;
    size_t read(int16_t* out, size_t frames, uint32_t timeout_ms, int16_t* raw_pcm = nullptr);
    const AecCaptureClock& capture_clock() const {return capture_clock_;}
    uint64_t clips() const { return clips_; }
    uint64_t errors() const { return errors_; }
    void gain_percent(unsigned gain);
    unsigned gain_percent() const { return gain_percent_.load(); }
    uint64_t limited_samples() const { return limited_samples_.load(); }
    uint64_t adc_clips() const { return adc_clips_.load(); }
private:
    friend MicStatus microphone_status();
    i2s_channel_obj_t* channel_ = nullptr;
    int32_t* raw_ = nullptr;
    float previous_ = 0, filtered_ = 0;
    std::atomic<uint64_t> clips_{0},errors_{0},gain_clips_{0},read_timeouts_{0},read_frames_{0};
    std::atomic<unsigned> gain_percent_{0};
    std::atomic<uint64_t> limited_samples_{0}, adc_clips_{0};
    EchoCanceller* aec_ = nullptr;
    AecCaptureClock capture_clock_;
    RxCaptureClock rx_clock_;
    int64_t previous_read_begin_us_=0;
    unsigned previous_rx_overflows_=0;
    bool capture_gap_pending_=false;
};
}
