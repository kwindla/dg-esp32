#pragma once
#include <cstdint>
#include <stdexcept>
#include <string_view>
namespace dg_audio {
// Keep even the minimum above a continuous write's queue/block/100 ms timeout
// reservation lead. The previous measured setting (500 ms) remains selectable.
constexpr bool valid_guard_rearm_ms(unsigned ms) { return ms>=500 && ms<=10000; }
inline unsigned parse_guard_value(std::string_view text,unsigned low,unsigned high) {
    while(!text.empty() && text.front()==' ')text.remove_prefix(1);
    while(!text.empty() && text.back()==' ')text.remove_suffix(1);
    if(text.empty() || text.size()>5)throw std::invalid_argument("AEC guard integer out of range");
    unsigned ms=0;
    for(char c:text) {
        if(c<'0' || c>'9')throw std::invalid_argument("AEC guard integer required");
        ms=ms*10+unsigned(c-'0');
    }
    if(ms<low || ms>high)throw std::invalid_argument("AEC guard integer out of range");
    return ms;
}
inline unsigned parse_guard_ms(std::string_view text) {return parse_guard_value(text,0,1000);}
inline unsigned parse_guard_rearm_ms(std::string_view text) {return parse_guard_value(text,500,10000);}
enum class GatePolicy { Auto, Open, Closed };
// Pure state machine; caller serializes access. Times are monotonic microseconds.
class Gate {
public:
    void speaking(bool active, int64_t now);
    void audible(int64_t now);
    bool closed(int64_t now);
    bool capture_closed(int64_t now,int64_t source_lower_us,bool source_valid);
    // Reserve before I2S can accept audible PCM; confirm only its accepted prefix.
    void prepare_write(int64_t first_us) { pending_=last_end_<0 || first_us-last_end_>=rearm_silence_us_; }
    void finish_write(int64_t first_us,int64_t end_us);
    void reset();
    void guard_ms(unsigned ms) { guard_us_=int64_t(ms)*1000; }
    unsigned guard_ms() const {return unsigned(guard_us_/1000);}
    int64_t last_end() const {return last_end_;}
    unsigned onsets() const {return onsets_;}
    int64_t guard_until() const {return guard_until_;}
    void rearm_ms(unsigned ms) {
        if(!valid_guard_rearm_ms(ms))throw std::invalid_argument("AEC re-arm must be 500..10000 ms");
        rearm_silence_us_=int64_t(ms)*1000;
    }
    unsigned rearm_ms() const {return unsigned(rearm_silence_us_/1000);}
    void set_policy(GatePolicy policy) { policy_ = policy; }
    void set_aec_active(bool active) { aec_active_=active; }
private:
    GatePolicy policy_ = GatePolicy::Auto;
    bool speaking_ = false;
    bool aec_active_ = false;
    bool pending_=false;
    int64_t guard_us_=400000,guard_until_=-1,last_end_=-1;
    int64_t rearm_silence_us_=2000000;
    unsigned onsets_=0;
    int64_t event_us_ = -1, audible_us_ = -1;
};
}
