#pragma once
#include <atomic>
#include <algorithm>
#include <cstdint>
namespace pipecat::esp_peer {
// Peer-call diagnostics need bounded quantiles, counts and exact mean/max,
// without a separate 8 KiB histogram for each nested call. One writer.
struct CompactTiming {
    std::atomic<unsigned> bins[32]{};
    std::atomic<unsigned> count{0},maximum{0};
    void add(unsigned us) {
        unsigned index=31-__builtin_clz(us|1u);
        auto& bin=bins[index];
        bin.store(bin.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);
        if(us>maximum.load(std::memory_order_relaxed))maximum.store(us,std::memory_order_relaxed);
        count.store(count.load(std::memory_order_relaxed)+1,std::memory_order_release);
    }
    unsigned quantile(unsigned percent) const {
        unsigned n=count.load(),seen=0,target=(uint64_t(n)*percent+99)/100;
        if(!n)return 0;
        for(unsigned i=0;i<32;++i) {
            seen+=bins[i].load(std::memory_order_relaxed);
            if(seen>=target)return std::min(maximum.load(),unsigned((uint64_t(1)<<(i+1))-1));
        }
        return maximum.load();
    }
};
}
