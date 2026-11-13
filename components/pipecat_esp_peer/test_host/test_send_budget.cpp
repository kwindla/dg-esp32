#include "send_budget.h"
#include <gtest/gtest.h>
#include <deque>
using pipecat::esp_peer::SendBudget;
TEST(SendBudget, PreAndPostShareFourSends) {
    SendBudget b;for(unsigned i=0;i<2;++i){ASSERT_TRUE(b.available());b.observe(100,3,3);}
    for(unsigned i=0;i<2;++i){ASSERT_TRUE(b.available());b.observe(100,3,3);}
    EXPECT_FALSE(b.available());EXPECT_FALSE(b.cutoff);
}
TEST(SendBudget, SlowSendAtBoundaryOrNewEnomemSuppressesPost) {
    for(bool error:{false,true}) {SendBudget b;b.observe(error?1:20000,8,error?9:8);
        EXPECT_TRUE(b.cutoff);EXPECT_FALSE(b.available());}
    SendBudget b;b.observe(19999,7,7);EXPECT_TRUE(b.available());
}
TEST(SendBudget, StaleSnapshotIsBoundedAndKeepsFreshFifoArrivals) {
    EXPECT_EQ(SendBudget::stale_snapshot(99),8u);
    // Each popped old frame is immediately replaced by a fresh producer frame.
    std::deque<int> queue{1,2,3};auto n=SendBudget::stale_snapshot(queue.size());
    for(unsigned i=0;i<n;++i){EXPECT_LE(queue.front(),3);queue.pop_front();queue.push_back(10+i);}
    EXPECT_EQ(queue,(std::deque<int>{10,11,12}));
}
