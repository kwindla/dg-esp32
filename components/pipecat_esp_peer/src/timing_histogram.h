#pragma once
#include <atomic>
#include <algorithm>
#include <cstdint>
namespace pipecat::esp_peer {
// Fixed 100 us buckets; quantiles are upper bounds. Exact maximum retained.
// One writer; snapshots need no media lock.
struct TimingHistogram {
    static constexpr unsigned width=100, buckets=2001;
    std::atomic<unsigned> bins[buckets]{};
    std::atomic<unsigned> count{0}, maximum{0};
    void add(unsigned us) {
        auto& bin=bins[std::min(us/width,buckets-1)];
        bin.store(bin.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);
        auto max=maximum.load(std::memory_order_relaxed);
        if(us>max)maximum.store(us,std::memory_order_relaxed);
        count.store(count.load(std::memory_order_relaxed)+1,std::memory_order_release);
    }
    unsigned quantile(unsigned percent) const {
        unsigned n=count.load(), target=(uint64_t(n)*percent+99)/100, seen=0;
        if(!n) return 0;
        for(unsigned i=0;i<buckets;++i) {
            seen+=bins[i].load(); if(seen>=target) return i==buckets-1?maximum.load():std::min(maximum.load(),(i+1)*width-1);
        }
        return maximum.load();
    }
};
}
