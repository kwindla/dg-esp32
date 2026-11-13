#include <gtest/gtest.h>
#include "demo_touch_delivery.h"

TEST(DemoTouchDelivery,ConnectingPollConsumesWithoutForwarding) {
    unsigned forwarded=0;
    const auto result=dg::deliver_demo_touch(
        []{return dg::DemoTouchWindow{false,2};},
        [](bool accept){EXPECT_FALSE(accept);return false;},
        [&]{++forwarded;});
    EXPECT_EQ(result,dg::DemoTouchDelivery::None);EXPECT_EQ(forwarded,0u);
}
TEST(DemoTouchDelivery,PhaseChangeDuringPollRejectsRecognizedTap) {
    dg::DemoTouchWindow window{true,1};unsigned forwarded=0;
    const auto result=dg::deliver_demo_touch([&]{return window;},
        [&](bool accept){EXPECT_TRUE(accept);window={false,2};return true;},
        [&]{++forwarded;});
    EXPECT_EQ(result,dg::DemoTouchDelivery::PhaseChanged);EXPECT_EQ(forwarded,0u);
}
TEST(DemoTouchDelivery,FreshLiveGenerationCannotReceiveOldTap) {
    dg::DemoTouchWindow window{true,1};unsigned forwarded=0;
    const auto result=dg::deliver_demo_touch([&]{return window;},
        [&](bool){window={true,3};return true;},[&]{++forwarded;});
    EXPECT_EQ(result,dg::DemoTouchDelivery::PhaseChanged);EXPECT_EQ(forwarded,0u);
}
TEST(DemoTouchDelivery,StableLiveForwardsOnce) {
    unsigned forwarded=0;
    const auto result=dg::deliver_demo_touch(
        []{return dg::DemoTouchWindow{true,1};},[](bool accept){return accept;},
        [&]{++forwarded;});
    EXPECT_EQ(result,dg::DemoTouchDelivery::Forwarded);EXPECT_EQ(forwarded,1u);
}
