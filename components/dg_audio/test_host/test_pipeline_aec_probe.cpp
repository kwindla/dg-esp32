#include "dg_audio/pipeline.h"
#include "dg_audio/aec_probe.h"
#include "fake_i2s.h"
#include <gtest/gtest.h>
#include <algorithm>

TEST(PipelineAecGuard, RearmRequiresCaptureDisabledAndAcceptsOnlyBoundedValues) {
    fake::reset(false);
    dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},
        [](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){},[]{return false;});
    for(unsigned ms:{500u,2000u,10000u})EXPECT_NO_THROW(pipeline.aec_guard_rearm_ms(ms));
    for(unsigned bad:{0u,499u,10001u,~0u})EXPECT_THROW(pipeline.aec_guard_rearm_ms(bad),std::invalid_argument);
    pipeline.enable_capture(true);
    EXPECT_THROW(pipeline.aec_guard_rearm_ms(2000),std::runtime_error);
    pipeline.enable_capture(false);
    EXPECT_NO_THROW(pipeline.aec_guard_rearm_ms(2000));
}

TEST(PipelineAecProbe, ActiveMediaRejectsBeforeAcknowledgementOrOutput) {
    fake::reset(false);bool acknowledged=false;
    dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},
        [](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){},[]{return true;});
    EXPECT_THROW(pipeline.aec_probe([&]{acknowledged=true;}),std::runtime_error);
    EXPECT_FALSE(acknowledged);
}
TEST(PipelineAecProbe, ProductionProbeSubmitsExactContinuousCanonicalPcm) {
    fake::reset(false);size_t start=0;
    dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},
        [](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){},[]{return false;});
    pipeline.aec_probe([&]{std::lock_guard<std::mutex> lock(fake::mutex);start=fake::submitted.size();});
    std::lock_guard<std::mutex> lock(fake::mutex);
    ASSERT_GE(fake::submitted.size(),start+dg_audio::AecProbe::samples);
    dg_audio::AecProbe expected;
    for(unsigned i=0;i<dg_audio::AecProbe::samples;++i)ASSERT_EQ(fake::submitted[start+i],expected.next(i));
}

TEST(PipelineAecGuard, CapabilityUsesInitializedEngineAndRuntimeSettings) {
    fake::reset(false);
    dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},
        [](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){},[]{return false;});
    EXPECT_TRUE(pipeline.capability().aec);EXPECT_EQ(pipeline.capability().guard_ms,400u);
    pipeline.aec_guard_ms(300);fake::aec_gain=15;
    auto c=pipeline.capability();EXPECT_TRUE(c.aec);EXPECT_EQ(c.guard_ms,300u);EXPECT_EQ(c.ref_gain_db,15u);
    fake::aec_enabled=false;c=pipeline.capability();
    EXPECT_FALSE(c.aec);EXPECT_EQ(c.guard_ms,0u);EXPECT_EQ(c.ref_gain_db,0u);
}
