#include "dg_board/canvas.h"
#include "dg_display/panel_config.h"
#include <gtest/gtest.h>
TEST(Canvas,BoardDimensionsAndAlignmentPreserveCurrentLogicalContract){
    EXPECT_EQ(dg_board::CanvasWidth,DG_EXPECT_CANVAS);
    EXPECT_EQ(dg_board::CanvasHeight,DG_EXPECT_CANVAS);
    EXPECT_EQ(dg_display::PanelWidth,DG_EXPECT_CANVAS);
    EXPECT_EQ(dg_display::PanelHeight,DG_EXPECT_CANVAS);
    EXPECT_EQ(dg_board::PanelXAlign,DG_EXPECT_CANVAS==412?4:1);
    EXPECT_EQ(dg_board::DmaXAlign,DG_EXPECT_CANVAS==412?4:8);
    EXPECT_EQ(dg_board::DmaYEvenIfRow8,DG_EXPECT_CANVAS==412);
    EXPECT_EQ(dg_display::StagingBytes,20480u);
    EXPECT_EQ(dg_display::StagingAlignment,64u);
}
