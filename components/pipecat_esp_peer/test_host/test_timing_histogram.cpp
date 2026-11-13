#include <gtest/gtest.h>
#include "timing_histogram.h"
using pipecat::esp_peer::TimingHistogram;
TEST(TimingHistogram, EmptyAndRoundedQuantiles) {
    TimingHistogram h;
    EXPECT_EQ(h.quantile(50),0u);
    for(unsigned i=0;i<100;++i) h.add(i*100);
    EXPECT_EQ(h.quantile(50),4999u); EXPECT_EQ(h.quantile(95),9499u);
    EXPECT_EQ(h.maximum.load(),9900u); EXPECT_EQ(h.count.load(),100u);
}
TEST(TimingHistogram, OverflowRetainsExactMaximum) {
    TimingHistogram h; h.add(300000); h.add(600000);
    EXPECT_EQ(h.quantile(95),600000u); EXPECT_EQ(h.maximum.load(),600000u);
}
