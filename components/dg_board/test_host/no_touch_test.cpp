#include <gtest/gtest.h>
#include "dg_board/touch.h"
TEST(NoTouch,UnsupportedRawAndGesturePathsStayInert){
    EXPECT_EQ(dg_board::touch::init(),ESP_ERR_NOT_SUPPORTED);
    dg_board::TouchPoint point{12,34,1};
    EXPECT_EQ(dg_board::touch::read_raw(point),ESP_ERR_NOT_SUPPORTED);
    EXPECT_EQ(point.x,12u);EXPECT_EQ(point.y,34u);EXPECT_EQ(point.contacts,1u);
    EXPECT_FALSE(dg_board::touch::poll(1,true));EXPECT_FALSE(dg_board::touch::inject_double_tap());
    EXPECT_FALSE(dg_board::touch::poll(100,false));
    const auto status=dg_board::touch::status();EXPECT_FALSE(status.initialized);EXPECT_EQ(status.interrupts,0u);EXPECT_EQ(status.reads,0u);EXPECT_EQ(status.injected,0u);
}
