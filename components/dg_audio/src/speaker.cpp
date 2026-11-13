#include "dg_audio/speaker.h"
#include "dg_audio/character_signals.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "sdkconfig.h"
#include "codec/audio_codec.h"
#include "codec/es7210_slots.h"
#include "esp_timer.h"
#include "esp_log.h"
#include <algorithm>
#include <cstdlib>
#include <new>
#include <cstring>
namespace dg_audio {
Speaker::Speaker() {
    stereo_ = static_cast<int16_t*>(heap_caps_malloc(320 * 4, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!stereo_) throw std::bad_alloc();
    // A constructor that throws runs no destructor: give the scratch back.
    try {channel_ = codec::tx();} catch(...) {free(stereo_);stereo_=nullptr;throw;}
    i2s_event_callbacks_t callbacks{};callbacks.on_sent=sent;callbacks.on_send_q_ovf=overflow;
    ESP_ERROR_CHECK(i2s_channel_register_event_callback(channel_,&callbacks,this));
}
Speaker::~Speaker() {
    idle();
    codec::release_tx(channel_);
    free(stereo_);
}
void Speaker::start() {
    draining_=false;
    if(!enabled_) {
        // Prefill the complete DMA ring with intentional silence before the
        // clock starts. Credits include the configured cushion, including zeros.
        std::memset(stereo_,0,320*4);size_t loaded=0;
        for(unsigned i=0;i<(codec::hardware().tx.dma_descriptors*codec::hardware().tx.dma_frames+319)/320;++i) {
            size_t n=0;ESP_ERROR_CHECK(i2s_channel_preload_data(channel_,stereo_,320*4,&n));loaded+=n/4;
        }
        submitted_=loaded;completed_=0;monitoring_=true;
        callback_us_=uint32_t(esp_timer_get_time());
        clock_.start(0,callback_us_.load(),esp_timer_get_time(),loaded);
        dg::device_clock.start(clock_.epoch(),loaded);
        ESP_ERROR_CHECK(i2s_channel_enable(channel_));enabled_=true;
        codec::playback_active(true);
    }
}
SpeakerClock::Opportunity Speaker::opportunity(int64_t now) {
    if(!enabled_)return {};
    unsigned sequence,completed,stamp;
    do {
        sequence=callback_sequence_.load();
        completed=completed_.load();stamp=callback_us_.load();
    } while((sequence&1) || callback_sequence_.load()!=sequence);
    clock_.observe(completed,stamp,now);
    auto result=clock_.next(now);
    if(result.refill_samples) {
        ++underflows_;
        // next() accounts for the driver's abandoned/auto-cleared interval.
        // Keep onset and drain accounting on that same physical write cursor.
        submitted_=uint32_t(clock_.submitted());
    }
    return result;
}
bool Speaker::valid(const dg::RenderTick& tick) {
    // Observe without creating/refilling another output opportunity.
    unsigned sequence,completed,stamp;
    do {sequence=callback_sequence_.load();completed=completed_.load();stamp=callback_us_.load();}
    while((sequence&1) || callback_sequence_.load()!=sequence);
    clock_.observe(completed,stamp,esp_timer_get_time());
    return clock_.valid(tick);
}
size_t Speaker::refill(unsigned samples) {
    ESP_LOGW("speaker", "I2S refill completed=%u submitted=%u refill_samples=%u device_us=%lld",
        completed_.load(),submitted_.load(),samples,(long long)esp_timer_get_time());
    refill_remaining_=samples;
    int16_t zeros[320]{};size_t total=0;
    // A refill is a bounded hardware operation, never a transport read.
    for(unsigned i=0;samples && i<8;++i) {
        auto n=write(zeros,std::min(samples,320u),20);
        clock_.submitted_silence(n);
        total+=n;samples-=n;refill_remaining_=samples;if(!n)break;
    }
    refill_remaining_=0;return total;
}
size_t Speaker::write(const int16_t* mono, size_t frames, uint32_t timeout_ms, bool eligible) {
    frames = std::min(frames, size_t(320)); audible_ = false;
    if(!frames) return 0;
    if(!enabled_)start();
    // One gain snapshot per block, even if a standalone caller changes volume.
    const auto volume=volume_.load();
    const auto slot=codec::hardware().tx.selected_slot;
    for (size_t i=0; i<frames; ++i) {
        int16_t s = int32_t(mono[i]) * int32_t(volume) / 32768;
        playback_slots(stereo_+2*i,s,slot);
    }
    // This check sees the same post-volume buffer the driver will receive.
    bool candidate=false;
    if(eligible && onset_observer)
        for(size_t i=0;i<frames;++i)candidate |= std::abs(int(playback_sample(stereo_+2*i,slot)))>32;
    const bool observed=eligible && candidate && bool(onset_observer);
    if(observed) {
        const auto queued=std::max(0,int(int32_t(submitted_.load()-completed_.load())));
        // Reserve conservatively across a partial/blocking write as well as
        // a non-silent sample late in this block. Only confirmation rearms.
        onset_observer(true,esp_timer_get_time()+int64_t(queued+frames)*1000000/16000+
                            int64_t(timeout_ms)*1000,-1);
    }
    size_t bytes = 0;
    auto err = i2s_channel_write(channel_, stereo_, frames * 4, &bytes, timeout_ms);
    submitted_.fetch_add(bytes/4);
    clock_.submitted(bytes/4);
    monitoring_=true;
    if(observed) {
        size_t first=bytes/4,last=0;
        for(size_t i=0;i<bytes/4;++i)if(std::abs(int(playback_sample(stereo_+2*i,slot)))>32) {first=std::min(first,i);last=i+1;}
        const auto queued=std::max(0,int(int32_t(submitted_.load()-completed_.load())));
        // Completion progress gives a conservative DAC upper bound, including
        // the partly consumed descriptor. A blocked write is never timed early.
        const auto base=esp_timer_get_time()+int64_t(std::max(0,queued-int(bytes/4)))*1000000/16000;
        onset_observer(false,last?base+int64_t(first)*1000000/16000:-1,
                             last?base+int64_t(last)*1000000/16000:-1);
    }
    unsigned peak = 0;uint64_t character_power=0;
    for (size_t i=0; i<bytes/4; ++i) {
        const auto magnitude = unsigned(std::abs(int(playback_sample(stereo_+2*i,slot))));
        audible_ |= magnitude > audible_threshold_;
        peak = std::max(peak,magnitude);
        character_power+=uint64_t(magnitude)*magnitude;
    }
    if(bytes) {
        // Use accepted post-volume PCM, at the middle of its audible interval.
        // Submitted minus DMA-completed samples includes the prefilled cushion;
        // this is deliberately downstream of decode/jitter/DSP lookahead.
        const auto queued=std::max(0,int(int32_t(submitted_.load()-completed_.load())));
        const auto until_middle=std::max(0,queued-int(bytes/8));
        character_signals.speaker(uint32_t((esp_timer_get_time()+int64_t(until_middle)*1000000/16000)/1000),
                                  uint32_t(character_power/(bytes/4)));
    }
    // Publish only this write's accepted samples. Do not resurrect a previous
    // peak if volume() reset it while I2S was accepting this block. An in-flight
    // block may still contribute its own peak across that reset boundary.
    auto previous=peak_.load();
    while(previous<peak && !peak_.compare_exchange_weak(previous,peak)) {}
    if (err != ESP_OK) {
        ++errors_;
        // First failure and one per 64 thereafter keep diagnostics bounded.
        if(errors_ == 1 || (err == ESP_ERR_TIMEOUT && timeouts_ == 0) || errors_%64 == 0)
            ESP_LOGW("speaker", "I2S write err=%s bytes=%u/%u timeout_ms=%u completed=%u submitted=%u errors=%llu refill_samples=%u device_us=%lld",
                esp_err_to_name(err),unsigned(bytes),unsigned(frames*4),unsigned(timeout_ms),
                completed_.load(),submitted_.load(),static_cast<unsigned long long>(errors_),refill_remaining_,(long long)esp_timer_get_time());
    }
    if (err == ESP_ERR_TIMEOUT) ++timeouts_;
    return bytes/4;
}
void Speaker::idle() {
    character_signals.speaker(uint32_t(esp_timer_get_time()/1000),0);
    // Intentional idle is excluded from the active-stream underflow metric.
    monitoring_=false;
    if(enabled_) {
        dg::device_clock.stop();clock_.stop();draining_=false;
        codec::playback_active(false);
        ESP_ERROR_CHECK(i2s_channel_disable(channel_));enabled_=false;
    }
}
bool Speaker::sent(i2s_chan_handle_t,i2s_event_data_t* event,void* context) {
    auto& s=*static_cast<Speaker*>(context);
    s.callback_sequence_.fetch_add(1);
    auto completed=s.completed_.fetch_add(event->size/4)+event->size/4;
    dg::device_clock.completed(completed);
    s.callback_us_=uint32_t(esp_timer_get_time());
    s.callback_sequence_.fetch_add(1);
    return false;
}
bool Speaker::overflow(i2s_chan_handle_t,i2s_event_data_t*,void* context) {
    auto& s=*static_cast<Speaker*>(context);
    if(s.monitoring_) ++s.queue_overflows_;
    return false;
}
}
