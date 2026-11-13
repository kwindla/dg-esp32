#include <gtest/gtest.h>
#include "pipecat/rtvi/messages.h"
#include <set>
#include "sdkconfig.h"
TEST(EspPlatform, About) {
    auto about = pipecat::rtvi::default_about_client();
    EXPECT_EQ(about.platform, "esp32");
    EXPECT_EQ(about.platform_version, "v6.1-test");
    EXPECT_EQ(about.library_version, "1.1.0");
#if DG_TEST_BOARD == DG_TEST_P4XC
    EXPECT_EQ(about.platform_details.at("target"), "esp32p4");
#else
    EXPECT_EQ(about.platform_details.at("target"), "esp32s3");
#endif
}
TEST(EspPlatform, MessageIds) {
    std::set<std::string> ids;
    for (int i=0; i<1000; ++i) {
        auto message = pipecat::rtvi::Message::client_ready(pipecat::rtvi::default_about_client());
        EXPECT_EQ(message.id.size(), 10u);
        EXPECT_TRUE(ids.insert(message.id).second);
    }
}
