#include "dg_display/present.h"
#include <gtest/gtest.h>
#include <vector>
#include "esp_err.h"
#include "dg_board/backlight.h"
using namespace dg_display;
namespace dg_display {
constexpr int NativeWidth=360,NativeHeight=360;
namespace detail {
struct FakeDma {bool dead=false,color_inflight=false;void* panel=this;};
FakeDma fake;FakeDma* state=&fake;
std::vector<unsigned> events;
bool timeout=false,drain_ok=true;unsigned generation=0,drain_generation=0,backlight=0;
int backlight_error=ESP_OK;
bool await_dma(){events.push_back(1);if(timeout){state->dead=true;return false;}state->color_inflight=false;return true;}
bool drain_dma(){events.push_back(3);generation+=drain_generation;return drain_ok;}
bool send(Rect,uint8_t*){state->color_inflight=true;return !state->dead;}
}
bool qspi_initialized(){return true;}
void census_init(){}
unsigned dma_generation(){return detail::generation;}
}
namespace dg_board {esp_err_t backlight::set(unsigned pct){dg_display::detail::backlight=pct;return dg_display::detail::backlight_error;}}
#include "../backends/qspi/panel.h"
namespace {
class QspiFence:public testing::Test {
protected:
    detail::QspiPanel panel;uint16_t pixels[8]{};unsigned releases=0;
    void SetUp()override{detail::fake={};detail::events.clear();detail::timeout=false;detail::drain_ok=true;detail::generation=0;detail::drain_generation=0;detail::backlight_error=ESP_OK;}
    SubmitId submit(){return panel.submit({0,0,8,1},pixels,8,{[](void* p){++*static_cast<unsigned*>(p);detail::events.push_back(2);},&releases});}
};
}
TEST_F(QspiFence,DeadTimeoutClearsPendingAndCommitDoesNotWaitAgain){
    auto id=submit();ASSERT_TRUE(id);detail::timeout=true;
    EXPECT_EQ(panel.wait_released(id),PresentError::Io);
    EXPECT_EQ(panel.committed(id),PresentError::Io);EXPECT_EQ(panel.wait_released(id),PresentError::Io);
    EXPECT_EQ(detail::events,(std::vector<unsigned>{1}));EXPECT_EQ(releases,0u);
    // A cleared pending slot reaches the dead send failure, never Busy.
    EXPECT_EQ(submit().error,PresentError::Io);
}
TEST_F(QspiFence,AlreadyDeadCommitAndReleaseReturnWithoutWaiting){
    auto id=submit();ASSERT_TRUE(id);detail::state->dead=true;
    EXPECT_EQ(panel.committed(id),PresentError::Io);EXPECT_EQ(panel.wait_released(id),PresentError::Io);
    EXPECT_TRUE(detail::events.empty());EXPECT_EQ(releases,0u);
    EXPECT_EQ(submit().error,PresentError::Io);
}
TEST_F(QspiFence,WaitReleaseDrainThenGenerationAreTheCommitFence){
    auto id=submit();ASSERT_TRUE(id);EXPECT_EQ(panel.committed(id),PresentError::None);
    EXPECT_EQ(detail::events,(std::vector<unsigned>{1,2,3}));EXPECT_EQ(releases,1u);
    auto changed=submit();ASSERT_TRUE(changed);detail::drain_generation=1;
    EXPECT_EQ(panel.committed(changed),PresentError::Io);EXPECT_EQ(releases,2u);
}
TEST_F(QspiFence,DrainErrorIsReportedAfterRelease){
    auto id=submit();ASSERT_TRUE(id);detail::drain_ok=false;
    EXPECT_EQ(panel.committed(id),PresentError::Io);EXPECT_EQ(detail::events,(std::vector<unsigned>{1,2,3}));
}
TEST_F(QspiFence,BacklightDelegatesToBoardAndPropagatesErrors){
    EXPECT_EQ(panel.set_backlight(101),PresentError::Invalid);
    EXPECT_EQ(panel.set_backlight(35),PresentError::None);EXPECT_EQ(detail::backlight,35u);
    detail::backlight_error=ESP_ERR_TIMEOUT;EXPECT_EQ(panel.set_backlight(0),PresentError::Io);
}
