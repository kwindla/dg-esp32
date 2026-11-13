#include "dg_audio/pipeline.h"
#include "fake_i2s.h"
#include <gtest/gtest.h>
extern "C" int dg_audio_output_volume_db();
TEST(PipelineDefaults,EchoEarTableSetsMinus6DbAndQ15ForActualSpeakerOutput) {
    fake::reset(false);
    dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},
        [](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){},[]{return false;});
    EXPECT_EQ(dg_audio_output_volume_db(),-6);
    // Full-scale samples in the flat interior of a direct tone bypass DSP.
    // 32767 * Q15(16423) / 32768 truncates to 16422; adjacent Q15 gains
    // produce a different integer, so this pins the Speaker's actual gain.
    pipeline.tone(1000,40,100,[]{});
    std::lock_guard<std::mutex> lock(fake::mutex);
    ASSERT_GE(fake::submitted.size(),640u);
    EXPECT_EQ(fake::submitted[164],16422);EXPECT_EQ(fake::submitted[172],-16422);
}
