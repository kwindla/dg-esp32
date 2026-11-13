#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
namespace pipecat::esp_peer {
// External synchronization: O(1) owner updates, copy only after freezing.
// Bins [0,1)ms ... [199,200)ms, then >=200ms. Exact max is independent.
struct UplinkSnapshot {
    uint32_t generation=0;
    int64_t start_us=0,end_us=0,last_ok_age_us=0;
    uint64_t n=0,attempts=0,ok=0,max_us=0;
    std::array<uint64_t,201> hist{};
};
class UplinkIntervals {
    UplinkSnapshot value;
    int64_t first_ok=-1,last_ok=-1;
    bool frozen=false,printed=false;
public:
    explicit UplinkIntervals(uint32_t generation=0) {value.generation=generation;}
    void submit(int64_t now,bool success) {
        if(frozen)return;
        if(first_ok<0) {
            if(success){first_ok=last_ok=now;value.start_us=now+2000000;}
            return;
        }
        if(now>=value.start_us) {
            ++value.attempts;
            if(success) {
                ++value.ok;
                // Exclude the interval crossing the warm-up boundary.
                if(last_ok>=value.start_us) {
                    const auto us=uint64_t(now-last_ok);++value.n;
                    ++value.hist[std::min(uint64_t(200),us/1000)];
                    value.max_us=std::max(value.max_us,us);
                }
            }
        }
        if(success)last_ok=now;
    }
    void freeze(int64_t now) {
        if(frozen)return;
        frozen=true;value.end_us=now;
        value.last_ok_age_us=last_ok<0?0:std::max(int64_t(0),now-last_ok);
    }
    void copy(UplinkSnapshot& output) const {output=value;}
    bool claim(int64_t now,UplinkSnapshot& output) {
        freeze(now);if(printed)return false;printed=true;output=value;return true;
    }
};
}
