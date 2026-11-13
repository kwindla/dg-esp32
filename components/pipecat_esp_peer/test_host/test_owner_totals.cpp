#include "owner_totals.h"
#include <gtest/gtest.h>
#include <thread>
using pipecat::esp_peer::OwnerTotals;
TEST(OwnerTotals, BothTotalsCross32BitBoundary) {
    OwnerTotals t;
    for(unsigned i=0;i<50000;++i)t.add(24000000,100000);
    auto [cycles,wall]=t.read();
    EXPECT_EQ(wall,5000000000ULL);EXPECT_EQ(cycles,1200000000000ULL);
}
TEST(OwnerTotals, ConcurrentReaderNeverObservesTornPair) {
    OwnerTotals t;std::atomic<bool> start{false},done{false};unsigned mismatches=0;
    std::thread writer([&] {
        while(!start)std::this_thread::yield();
        for(unsigned i=0;i<50000;++i)t.add(24000000,100000);
        done=true;
    });
    start=true;uint64_t previous=0;
    do {
        auto [cycles,wall]=t.read();
        mismatches+=cycles!=wall*240 || wall<previous;previous=wall;
    } while(!done);
    writer.join();EXPECT_EQ(mismatches,0u);EXPECT_EQ(t.read().second,5000000000ULL);
}
