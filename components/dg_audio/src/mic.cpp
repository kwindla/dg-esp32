#include "dg_audio/aec_config.h"
#include "dg_audio/mic.h"
#include "dg_audio/mic_protection.h"
#include "dg_audio/aec.h"
#include "codec/es7210_slots.h"
#include "codec/audio_codec.h"

#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "sdkconfig.h"
#if CONFIG_DG_ECHOEAR_AEC
#include "esp_timer.h"
#endif
#include <algorithm>
#include <cstdlib>
#include <new>
#include <stdexcept>
namespace dg_audio {
namespace {std::atomic<Mic*> active_mic{nullptr};}
Mic::Mic():rx_clock_(codec::hardware().rx.dma_frames,codec::hardware().rx.dma_descriptors)
{
    gain_percent_ = CONFIG_DG_MIC_GAIN_PERCENT;
    raw_ = static_cast<int32_t*>(heap_caps_malloc(320 * 8, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!raw_) throw std::bad_alloc();
    // A constructor that throws runs no destructor: give back what it took.
    try {
        channel_ = codec::rx();
#if CONFIG_DG_ECHOEAR_AEC
        try {aec_=new EchoCanceller;}catch(...) {codec::release_rx(channel_);throw;}
#endif
    } catch(...) {free(raw_);raw_=nullptr;throw;}
    active_mic=this;
}
Mic::~Mic() {
    active_mic=nullptr;
#if CONFIG_DG_ECHOEAR_AEC
    delete aec_;
#endif
    codec::release_rx(channel_);
    free(raw_);
}
void Mic::gain_percent(unsigned gain) {
    if (gain < 1 || gain > 1600) throw std::runtime_error("Microphone gain must be 1..1600 percent");
    gain_percent_ = gain;
}
size_t Mic::read(int16_t* out, size_t frames, uint32_t timeout_ms, int16_t* raw_pcm) {
    size_t bytes = 0; frames = std::min(frames, size_t(320));
    const auto& hw=codec::hardware();
    const size_t frame_bytes=2*hw.rx.data_bits/8;
    const auto* words=reinterpret_cast<const uint32_t*>(raw_);
    auto sample = [&](size_t i) {return audio_sample(words+i*(frame_bytes/4),hw.microphones[0],frame_bytes/4);};
#if CONFIG_DG_ECHOEAR_AEC
    const auto read_begin=esp_timer_get_time();
    const auto completion_begin=codec::capture_completions();
    const auto overflow_begin=codec::capture_overflows();
    const int64_t residence_us=int64_t(hw.rx.dma_descriptors+1)*
        hw.rx.dma_frames*1000000/hw.rx.clock.sample_rate_hz;
    const auto lower=rx_clock_.begin(completion_begin,overflow_begin,read_begin,
        previous_read_begin_us_-residence_us);
#endif
    auto err = i2s_channel_read(channel_, raw_, frames * frame_bytes, &bytes, timeout_ms);
#if CONFIG_DG_ECHOEAR_AEC
    const auto read_end=esp_timer_get_time();
    const auto overflow_end=codec::capture_overflows();
    rx_clock_.end(codec::capture_completions(),overflow_end,bytes/frame_bytes);
    AecReadTiming timing{lower,read_end,
        capture_gap_pending_ || previous_read_begin_us_==0 || overflow_begin!=previous_rx_overflows_ ||
        overflow_end!=overflow_begin || err!=ESP_OK || bytes!=frames*frame_bytes};
    capture_clock_.read(bytes/frame_bytes,timing.lower_us,timing.gap);
    capture_gap_pending_=bytes==0 && timing.gap;
    previous_read_begin_us_=read_begin;previous_rx_overflows_=overflow_end;
#endif
    if (err != ESP_OK || bytes != frames * frame_bytes) ++errors_;
    if (err == ESP_ERR_TIMEOUT) ++read_timeouts_;
    read_frames_+=bytes/frame_bytes;
    if (raw_pcm) {
        for (size_t i = 0; i < bytes / frame_bytes; ++i) raw_pcm[i] = sample(i);
    }
#if CONFIG_DG_ECHOEAR_AEC
    int16_t mic[320],reference[320],processed[320];
    for(size_t i=0;i<bytes/frame_bytes;++i) {
        const auto slots=es7210_slots(uint32_t(raw_[2*i]),uint32_t(raw_[2*i+1]),hw);
        mic[i]=slots.mic1;reference[i]=slots.mic3;
    }
    // AFE failures have their own authoritative counter in aec_status().
    aec_->process(mic,reference,bytes/frame_bytes,processed,reinterpret_cast<const uint32_t*>(raw_),timing);
#endif
    const float gain = gain_percent_.load() / 100.0f;
    unsigned limited_samples=0,adc_clips=0;
    for (size_t i = 0; i < bytes / frame_bytes; ++i) {
        const float adc_input = sample(i);
#if CONFIG_DG_ECHOEAR_AEC
        float input = processed[i];
#else
        float input = adc_input;
#endif
        // RC high-pass: alpha = RC/(RC+dt), fc=50 Hz at 16 kHz.
        filtered_ = .980742f * (filtered_ + input - previous_); previous_ = input;
        float protected_input=filtered_;
        // A digital limiter cannot recover saturated ADC input. Keep it fatal
        // to the same open/closed clip gates instead of hiding it downstream.
        if(hw.adc_clip_counting && (adc_input>=32767 || adc_input<=-32768)) {++adc_clips;++clips_;}
        // AFE saturation is counted only by EchoCanceller::output_clips.
        bool limited=false;
        if(hw.microphone_limiter)protected_input=protect_microphone(filtered_,gain,limited);
        limited_samples+=limited;
        int32_t sample = static_cast<int32_t>(protected_input * gain);
        if (sample > 32767 || sample < -32768) {++clips_;++gain_clips_;}
        out[i] = std::clamp(sample, int32_t(-32768), int32_t(32767));
    }
    limited_samples_.fetch_add(limited_samples,std::memory_order_relaxed);
    adc_clips_.fetch_add(adc_clips,std::memory_order_relaxed);
    return bytes / frame_bytes;
}
MicStatus microphone_status() {
    MicStatus result;
    // Same board-lifetime ownership as the canceller; readers stop before destruction.
    if(auto* p=active_mic.load()) {
        result.adc_clips=p->adc_clips_;result.gain_clips=p->gain_clips_;
        result.read_errors=p->errors_;result.read_timeouts=p->read_timeouts_;result.read_frames=p->read_frames_;
    }
    result.rx_completions=codec::capture_completions();result.rx_overflows=codec::capture_overflows();
    return result;
}
}
