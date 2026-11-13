#include "dg_power_decode.h"
#include <gtest/gtest.h>
TEST(PowerGauge, LittleEndianSignedDischargeAndKelvinConversion) {
    EXPECT_EQ(dg_board::gauge_word(0x34,0x12),0x1234);
    EXPECT_EQ(dg_board::gauge_current(0xff38),-200);
    EXPECT_EQ(dg_board::gauge_current(32767),32767);
    EXPECT_EQ(dg_board::gauge_current(32768),-32768);
    EXPECT_EQ(dg_board::gauge_temperature_centideg(2982),2505);
}
