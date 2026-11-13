#include <gtest/gtest.h>
#include "touch_init_retry.h"
TEST(TouchInitRetry, FirstProbeIsImmediateAndSuccessEndsProbing) {
    dg::TouchInitRetry retry;
    EXPECT_FALSE(retry.ready());EXPECT_TRUE(retry.due(0));EXPECT_TRUE(retry.due(123456));
    retry.succeeded();
    EXPECT_TRUE(retry.ready());EXPECT_FALSE(retry.due(0));EXPECT_FALSE(retry.due(1ll<<40));
}
TEST(TouchInitRetry, AFailedProbeAtBootIsRepeatedUntilTheControllerAnswers) {
    dg::TouchInitRetry retry;
    EXPECT_EQ(retry.failed(5000),1000u);
    EXPECT_FALSE(retry.due(5999));EXPECT_TRUE(retry.due(6000));
    EXPECT_EQ(retry.failed(6000),2000u);
    EXPECT_FALSE(retry.due(7999));EXPECT_TRUE(retry.due(8000));
    retry.succeeded();
    EXPECT_TRUE(retry.ready());EXPECT_FALSE(retry.due(8000));
}
TEST(TouchInitRetry, RepeatedFailureBacksOffToThirtySecondsAndKeepsProbing) {
    dg::TouchInitRetry retry;int64_t now=0;
    for(unsigned expected:{1000u,2000u,4000u,8000u,16000u,30000u,30000u,30000u}) {
        ASSERT_TRUE(retry.due(now));
        EXPECT_EQ(retry.failed(now),expected);
        EXPECT_FALSE(retry.due(now+expected-1));
        now+=expected;
    }
    EXPECT_TRUE(retry.due(now));EXPECT_FALSE(retry.ready());
}
