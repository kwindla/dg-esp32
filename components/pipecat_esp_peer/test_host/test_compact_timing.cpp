#include "compact_timing.h"
#include <gtest/gtest.h>
#include <limits>
using pipecat::esp_peer::CompactTiming;
TEST(CompactTiming, BoundsVariableDurationsWithoutClippingLargeIntervals) {
    CompactTiming h;
    EXPECT_EQ(h.quantile(50),0u);
    for(unsigned us : {0u,1u,10u,100u,1000u,10000u,100000u,1000000u})h.add(us);
    EXPECT_EQ(h.count,8u);
    EXPECT_GE(h.quantile(50),100u);EXPECT_LE(h.quantile(50),127u);
    EXPECT_EQ(h.quantile(100),1000000u);
    h.add(std::numeric_limits<unsigned>::max());
    EXPECT_EQ(h.quantile(100),std::numeric_limits<unsigned>::max());
}
