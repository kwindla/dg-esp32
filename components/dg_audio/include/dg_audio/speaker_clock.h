#pragma once
#include "dg_render_tick.h"
#include <algorithm>
#include <cstdint>
namespace dg_audio {
// Task-owned output ledger. ISR supplies only 32-bit samples/time. Position is
// extended before block arithmetic; 240-sample events are never rounded up.
class SpeakerClock {
public:
    static constexpr uint32_t block=320, rate=16000;
    struct Opportunity {
        dg::RenderTick tick;
        uint32_t refill_samples=0;
        uint32_t wait_us=1000;
        bool ready=false;
    };
    void start(uint32_t raw_samples,uint32_t callback_us,int64_t now,uint32_t preload) {
        ++epoch_;raw_=raw_samples;completed_=0;submitted_=preload;position_=0;
        (void)callback_us;(void)now;running_=true;
        // The initial DMA ring is intentional silence. Start a block when a
        // full block of capacity is released; residence is preload-block.
        lead_=preload-block;first_output_=preload;next_=0;rendered_=false;
        silence_samples_=preload;pending_skips_=0;refilling_=false;
    }
    void stop() {running_=false;++epoch_;pending_skips_=0;}
    void observe(uint32_t raw_samples,uint32_t callback_us,int64_t now) {
        if(!running_)return;
        completed_+=uint32_t(raw_samples-raw_);raw_=raw_samples;
        if(completed_>submitted_) {silence_samples_+=completed_-submitted_;submitted_=completed_;}
        // Interpolate only within one DMA descriptor. Every estimate is
        // re-anchored by a real device completion, never a free-running timer.
        uint32_t elapsed=uint32_t(uint32_t(now)-callback_us);
        position_=completed_+std::min<uint64_t>(239,uint64_t(elapsed)*rate/1000000);
    }
    Opportunity next(int64_t now) {
        Opportunity out;if(!running_)return out;
        const uint64_t output=first_output_+next_*block;
        if(position_>=output) {
            // The current unrendered interval expired. PCM rendered earlier
            // has already consumed its tick and isn't retired a second time.
            // With output already at the playhead, the IDF three-deep EOF
            // queue is full. Its early-pop rule abandons the partial writer
            // descriptor. The oldest retained descriptor next plays one DMA
            // period beyond completed_, not at the old submitted_ cursor.
            const auto cursor=completed_+240;
            if(cursor>submitted_) {silence_samples_+=cursor-submitted_;submitted_=cursor;}
            // Refill only to the normal pre-write equilibrium. Adding another
            // block here fills too far and every later write can block.
            uint64_t resumed=((completed_+block-1)/block)*block+lead_;
            uint64_t next=(resumed-first_output_+block-1)/block;
            pending_skips_+=uint32_t(next-next_);next_=next;
            resumed=first_output_+next_*block;
            out.refill_samples=uint32_t(resumed>submitted_?resumed-submitted_:0);refilling_=true;
            return out;
        }
        if(refilling_ && submitted_<output)return out; // A partial refill cannot authorize PCM.
        if(position_+lead_<output) {
            // Sleep toward the next device-derived opportunity rather than
            // polling every millisecond. Reobserve within one descriptor;
            // this advice never advances the device or source ledger.
            out.wait_us=uint32_t(std::min<uint64_t>(15000,
                (output-position_-lead_)*1000000/rate));
            return out;
        }
        // Interpolation estimates the playhead, but cannot release DMA memory.
        // A 320-sample write needs whole completed 240-sample descriptors;
        // otherwise every third interval waits ~10 ms inside a 5 ms write.
        if(completed_+lead_<output) {
            out.wait_us=uint32_t((240-(position_-completed_))*1000000/rate);
            return out;
        }
        refilling_=false;out.ready=true;
        out.tick={epoch_,next_,completed_,submitted_,
            now-int64_t((position_+lead_-output)*1000000/rate),
            pending_skips_,uint32_t(silence_samples_),uint32_t(completed_%block),true};
        return out;
    }
    bool rendered(const dg::RenderTick& tick) {
        if(!running_ || tick.device_epoch!=epoch_ || tick.index!=next_)return false;
        ++next_;pending_skips_=0;rendered_=true;return true;
    }
    bool valid(const dg::RenderTick& tick) const {
        return running_ && tick.device_epoch==epoch_ && rendered_ && tick.index+1==next_ &&
            position_<first_output_+tick.index*block;
    }
    // Callback-before-return is harmless: ISR never declares a deficit using
    // the still-uncommitted write. Commit the actual suffix exactly once.
    void submitted(uint32_t samples) {submitted_=std::max(submitted_,completed_)+samples;}
    void submitted_silence(uint32_t samples) {silence_samples_+=samples;}
    uint64_t epoch() const {return epoch_;}
    uint64_t completed() const {return completed_;}
    uint64_t submitted() const {return submitted_;}
    uint64_t silence_samples() const {return silence_samples_;}
    unsigned remainder() const {return completed_%block;}
    bool running() const {return running_;}
private:
    uint64_t epoch_=0, completed_=0, submitted_=0, position_=0, first_output_=0, next_=0, silence_samples_=0;
    uint32_t raw_=0, lead_=0, pending_skips_=0;
    bool running_=false,rendered_=false,refilling_=false;
};
}
