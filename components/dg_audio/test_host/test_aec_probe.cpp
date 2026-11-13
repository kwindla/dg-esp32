#include "dg_audio/aec_probe.h"
#include <gtest/gtest.h>
#include <algorithm>
#include <cstdint>

TEST(AecProbe, CanonicalPcmMatchesHostRecipeAndHasSilentMargins) {
    dg_audio::AecProbe source;uint64_t hash=14695981039346656037ull;int peak=0;
    for(unsigned i=0;i<dg_audio::AecProbe::samples;++i) {
        const auto sample=source.next(i);const auto bits=uint16_t(sample);
        if(i<3200 || i>=35200)EXPECT_EQ(sample,0);
        peak=std::max(peak,std::abs(int(sample)));
        hash=(hash^uint8_t(bits))*1099511628211ull;
        hash=(hash^uint8_t(bits>>8))*1099511628211ull;
    }
    EXPECT_EQ(peak,3276);EXPECT_EQ(hash,0x4aa11914f0b0cd60ull);
}

#include "dg_audio/reference_gain.h"
TEST(ReferenceGain, LadderAndStrictConsoleArguments) {
    for(unsigned db=0;db<=33;db+=3) {
        EXPECT_EQ(dg_audio::parse_reference_gain(std::to_string(db)),db);
        EXPECT_EQ(dg_audio::reference_gain_register(db),0x10u+db/3);
    }
    for(const char* invalid:{"", "-3", "+3", "1", "34", "36", "15x", "15 3", "999999999999999999999", "3.0"})
        EXPECT_THROW(dg_audio::parse_reference_gain(invalid),std::invalid_argument);
    EXPECT_EQ(dg_audio::parse_reference_gain(" 24 "),24u);
}
TEST(ReferenceGain, RelativePowerDoesNotOverflowAtFullScale) {
    EXPECT_FALSE(dg_audio::reference_power_low(320ULL*32768*32768,320,32768u*32768,33));
    EXPECT_TRUE(dg_audio::reference_power_low(0,320,1025,0));
    EXPECT_FALSE(dg_audio::reference_power_low(320,320,1000000,0));
}
