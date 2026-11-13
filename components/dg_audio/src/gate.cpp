#include "dg_audio/aec_config.h"
#include "dg_audio/gate.h"
#include <algorithm>
#if defined(__has_include)
#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif
#endif
namespace dg_audio {
#if CONFIG_DG_ECHOEAR_AEC
constexpr int64_t speaking_fallback_us=2000000;   // AEC image: unchanged from the qualified build
#else
// Half-duplex (mute/normal) image, decisions 130/131: the RTVI speaking latch holds the gate closed from bot-started
// until bot-stopped (or a stop/disconnect, which clear it). This is only the fallback for a bot-stopped that never
// arrives: 20 s without audible playback. A reply whose audible playback pauses or fades for longer reopens early.
constexpr int64_t speaking_fallback_us=20000000;
#endif
void Gate::speaking(bool active, int64_t now) { speaking_ = active; event_us_ = now; }
void Gate::audible(int64_t now) { audible_us_ = now; }
void Gate::reset() {
    speaking_=pending_=false;event_us_=audible_us_=guard_until_=last_end_=-1;
}
void Gate::finish_write(int64_t first_us,int64_t end_us) {
    pending_=false;
    if(first_us<0 || end_us<=first_us)return; // no accepted non-silent samples
    if(last_end_<0 || first_us-last_end_>=rearm_silence_us_) {
        guard_until_=first_us+guard_us_;++onsets_;
    }
    last_end_=std::max(last_end_,end_us);
}
bool Gate::closed(int64_t now) {return capture_closed(now,now,true);}
bool Gate::capture_closed(int64_t now,int64_t source_lower_us,bool source_valid) {
    if (speaking_ && now - std::max(event_us_, audible_us_) >= speaking_fallback_us) speaking_ = false;
    if (policy_ != GatePolicy::Auto) return policy_ == GatePolicy::Closed;
    if (aec_active_) {
        // With the canceller running the mic stays open, except around the
        // start of a reply: while a write that starts one is pending, and
        // until capture has moved past the end of the guard window. Capture
        // whose position is unknown counts as still inside the window.
        if (!guard_us_) return false;
        const bool guard_running = guard_until_ >= 0;
        const bool capture_inside_guard = !source_valid || source_lower_us < guard_until_;
        return pending_ || (guard_running && capture_inside_guard);
    }
    // Half-duplex: closed while the bot speaks and for the tail after its last audible sample.
    const bool in_tail = audible_us_ >= 0 && now - audible_us_ < 300000;
    return speaking_ || in_tail;
}
}
