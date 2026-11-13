#include "dg_audio/mic_protection.h"
#include <gtest/gtest.h>
#include <cmath>
using namespace dg_audio;
TEST(MicProtection, BoundsEverySupportedGainAndFullScaleFilterTransitions) {
    for(unsigned percent=1;percent<=1600;++percent) {
        const float gain=percent/100.0f;
        float previous=-1;
        for(int input=0;input<=131072;input+=127) {
            bool limited=false,negative_limited=false;
            float y=protect_microphone(float(input),gain,limited)*gain;
            EXPECT_LE(y,mic_ceiling); EXPECT_GE(y,previous);previous=y;
            EXPECT_FLOAT_EQ(-y,protect_microphone(-float(input),gain,negative_limited)*gain);
            EXPECT_EQ(limited,negative_limited);
        }
    }
}
TEST(MicProtection, CalibratedSpeechIsBitExactBelowKnee) {
    for(int input=-6500;input<=6500;++input) {
        bool limited=true;
        EXPECT_FLOAT_EQ(protect_microphone(float(input),1.25f,limited),float(input));
        EXPECT_FALSE(limited);
    }
}
TEST(MicProtection, KneeIsContinuousWithUnitSlope) {
    bool limited=false;
    const float knee=mic_knee/1.25f;
    EXPECT_FLOAT_EQ(protect_microphone(knee,1.25f,limited),knee);
    EXPECT_FALSE(limited);
    EXPECT_NEAR(protect_microphone(knee+1,1.25f,limited),knee+1,.001f);
    EXPECT_TRUE(limited);
}
