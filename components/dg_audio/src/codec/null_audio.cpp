#include "audio_codec.h"
#include "dg_audio/mic.h"
#include "dg_audio/speaker.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include <algorithm>
#include <stdexcept>
#if CONFIG_DG_ECHOEAR_AEC
#error "Null audio is AEC-off only; P4 AEC arrives after the real codec backend"
#endif
namespace dg_audio::codec {
void init() {ESP_ERROR_CHECK(hardware().codec==dg_board::CodecKind::Null?ESP_OK:ESP_ERR_INVALID_STATE);}
unsigned capture_completions() {return 0;}
unsigned capture_overflows() {return 0;}
void playback_active(bool) {}
void microphone_analog_gain(unsigned) {throw std::runtime_error(hardware().analog_gain_error);}
unsigned microphone_gain_status(unsigned) {return 0;}
unsigned reference_analog_gain(unsigned) {throw std::runtime_error(hardware().reference_gain_error);}
unsigned reference_gain_status() {throw std::runtime_error(hardware().reference_gain_error);}
}
namespace dg_audio {
namespace {std::atomic<Mic*> active_mic{nullptr};}
Mic::Mic():rx_clock_(320,1) {codec::init();gain_percent_=CONFIG_DG_MIC_GAIN_PERCENT;active_mic=this;}
Mic::~Mic() {active_mic=nullptr;}
void Mic::gain_percent(unsigned gain) {
    if(gain<1 || gain>1600)throw std::runtime_error("Microphone gain must be 1..1600 percent");
    gain_percent_=gain;
}
size_t Mic::read(int16_t* out,size_t frames,uint32_t timeout_ms,int16_t* raw_pcm) {
    frames=std::min(frames,size_t(320));if(!frames)return 0;
    const auto now=esp_timer_get_time();
    const int64_t period=int64_t(frames)*1000000/16000;
    // Early calls wait on the absolute grid, independent of sleep overshoot.
    // A late caller already earned one frame: deliver it now and restart the
    // grid there, without skipping a whole interval or bursting old frames.
    if(!previous_read_begin_us_)previous_read_begin_us_=now;
    const auto deadline=std::max(previous_read_begin_us_+period,now);
    if(deadline-now>int64_t(timeout_ms)*1000) {
        if(timeout_ms)vTaskDelay(pdMS_TO_TICKS(timeout_ms));
        ++errors_;++read_timeouts_;return 0;
    }
    while(esp_timer_get_time()<deadline) {
        const auto remaining=deadline-esp_timer_get_time();
        if(remaining>0)vTaskDelay(std::max<TickType_t>(1,pdMS_TO_TICKS((remaining+999)/1000)));
    }
    previous_read_begin_us_=deadline;
    std::fill_n(out,frames,0);if(raw_pcm)std::fill_n(raw_pcm,frames,0);
    read_frames_+=frames;return frames;
}
MicStatus microphone_status() {
    MicStatus result;
    if(auto* mic=active_mic.load()) {
        result.read_errors=mic->errors_;result.read_timeouts=mic->read_timeouts_;result.read_frames=mic->read_frames_;
    }
    return result;
}
Speaker::Speaker() {codec::init();}
Speaker::~Speaker() {idle();}
void Speaker::start() {
    draining_=false;if(enabled_)return;
    // A software sink uses the existing render ledger at the same nominal
    // rate. It has no hardware callbacks, DMA buffers, pins or scan-out claim.
    constexpr unsigned preload=960;
    completed_=0;submitted_=preload;callback_us_=uint32_t(esp_timer_get_time());
    clock_.start(0,callback_us_,esp_timer_get_time(),preload);
    dg::device_clock.start(clock_.epoch(),preload);enabled_=true;
}
SpeakerClock::Opportunity Speaker::opportunity(int64_t now) {
    if(!enabled_)return {};
    const auto stamp=callback_us_.load();
    const auto elapsed=uint32_t(uint32_t(now)-stamp);
    const auto samples=unsigned(uint64_t(elapsed)*16000/1000000/240)*240;
    if(samples) {
        completed_+=samples;callback_us_=stamp+uint64_t(samples)*1000000/16000;
        dg::device_clock.completed(completed_);
    }
    clock_.observe(completed_,callback_us_,now);return clock_.next(now);
}
bool Speaker::valid(const dg::RenderTick& tick) {
    opportunity(esp_timer_get_time());return clock_.valid(tick);
}
size_t Speaker::refill(unsigned samples) {
    submitted_+=samples;clock_.submitted(samples);clock_.submitted_silence(samples);return samples;
}
size_t Speaker::write(const int16_t*,size_t frames,uint32_t,bool) {
    frames=std::min(frames,size_t(320));if(!frames)return 0;
    if(!enabled_)start();
    submitted_+=frames;clock_.submitted(frames);audible_=false;return frames;
}
void Speaker::idle() {
    if(enabled_){dg::device_clock.stop();clock_.stop();enabled_=false;draining_=false;}
}
}
