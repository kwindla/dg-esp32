#pragma once
#include <atomic>
#include <cstdint>
#include <utility>
namespace pipecat::esp_peer {
// Only the peer owner writes. Publish through native-width atomics so readers
// get coherent 64-bit totals without an emulated 64-bit atomic on every call.
class OwnerTotals {
    uint64_t cycles_=0,wall_=0;
    uint32_t version_=0;
    std::atomic<uint32_t> version{0},cycle_lo{0},cycle_hi{0},wall_lo{0},wall_hi{0};
public:
    void add(uint32_t cycles,unsigned us) {
        version.store(++version_);
        cycles_+=cycles;wall_+=us;
        cycle_lo.store(uint32_t(cycles_));cycle_hi.store(uint32_t(cycles_>>32));
        wall_lo.store(uint32_t(wall_));wall_hi.store(uint32_t(wall_>>32));
        version.store(++version_);
    }
    std::pair<uint64_t,uint64_t> read() const {
        for(;;) {
            auto first=version.load();if(first&1)continue;
            auto cl=cycle_lo.load(),ch=cycle_hi.load(),wl=wall_lo.load(),wh=wall_hi.load();
            if(first==version.load())return {uint64_t(ch)<<32|cl,uint64_t(wh)<<32|wl};
        }
    }
};
}
