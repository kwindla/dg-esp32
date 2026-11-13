#include <gtest/gtest.h>
#include "dg_board/touch.h"
#include "dg_board/i2c.h"
#include "../src/touch_backend.h"
#include "../src/p4xc/reset.h"
#include <cstring>
#include <vector>
// Compile the real sources in this test TU so SetUp can reset their private
// state. Production code needs no test hook or firmware change.
#include "../src/common/touch.cpp"
#include "../src/p4xc/touch_gt911.cpp"
namespace {
int token;StaticSemaphore_t mutex{};
bool ready=false,held=false,lock_ok=true;
int64_t clock_us=0;
unsigned fail_register=0,configured_size=800;
esp_err_t remove_error=ESP_OK;
unsigned address=0,available=0x5d,epoch=0,reads=0,clears=0,removes=0;
uint8_t status=0x80,contacts=0;uint16_t x=400,y=400;
esp_err_t read_error=ESP_OK,clear_error=ESP_OK;
const char* product="9271";
std::vector<unsigned> tried;
bool sample(uint32_t at,unsigned count){contacts=count;return dg_board::touch::poll(at,true);}
}
namespace dg_board {
i2c_master_bus_handle_t i2c::bus(){return ready?&token:nullptr;}
SemaphoreHandle_t i2c::mutex(){return &::mutex;}
uint32_t detail::lcd_reset_generation(){EXPECT_TRUE(held);return epoch;}
}
int64_t esp_timer_get_time(){return clock_us;}
int xSemaphoreTake(SemaphoreHandle_t m,TickType_t timeout){EXPECT_EQ(m,&mutex);EXPECT_EQ(timeout,11u);EXPECT_FALSE(held);held=lock_ok;return held;}
int xSemaphoreGive(SemaphoreHandle_t m){EXPECT_EQ(m,&mutex);EXPECT_TRUE(held);held=false;return 1;}
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t bus,const i2c_device_config_t* cfg,i2c_master_dev_handle_t* out){
    EXPECT_TRUE(held);EXPECT_EQ(bus,&token);EXPECT_EQ(cfg->scl_speed_hz,400000u);
    address=cfg->device_address;tried.push_back(address);*out=&token;return ESP_OK;
}
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t){EXPECT_TRUE(held);++removes;return remove_error;}
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t,const uint8_t* reg,size_t n,uint8_t* out,size_t bytes,int timeout){
    EXPECT_TRUE(held);EXPECT_EQ(n,2u);EXPECT_EQ(timeout,10);++reads;
    if(address!=available)return ESP_ERR_TIMEOUT;
    unsigned r=reg[0]<<8|reg[1];
    if(r==0x8140){EXPECT_EQ(bytes,4u);std::memcpy(out,product,4);return ESP_OK;}
    if(r==0x8048){EXPECT_EQ(bytes,4u);out[0]=out[2]=configured_size&255;out[1]=out[3]=configured_size>>8;return ESP_OK;}
    if(read_error!=ESP_OK && (!fail_register || fail_register==r))return read_error;
    if(r==0x814e){EXPECT_EQ(bytes,1u);*out=status|contacts;return ESP_OK;}
    EXPECT_EQ(r,0x814fu);EXPECT_EQ(bytes,8u);std::memset(out,0,bytes);
    out[1]=x;out[2]=x>>8;out[3]=y;out[4]=y>>8;return ESP_OK;
}
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t,const uint8_t* bytes,size_t n,int timeout){
    EXPECT_TRUE(held);EXPECT_EQ(n,3u);EXPECT_EQ(timeout,10);
    EXPECT_EQ(bytes[0],0x81);EXPECT_EQ(bytes[1],0x4e);EXPECT_EQ(bytes[2],0);++clears;return clear_error;
}
class Gt911 : public ::testing::Test {
    void SetUp() override {
        ready=held=false;lock_ok=true;clock_us=0;fail_register=0;configured_size=800;
        remove_error=read_error=clear_error=ESP_OK;address=epoch=reads=clears=removes=0;
        available=0x5d;status=0x80;contacts=0;x=y=400;product="9271";tried.clear();
        using namespace dg_board;
        device=nullptr;generation=0;next_probe_us=0;
        touch_state={};gesture={};injected_pending=false;injected_count=0;injected_ignored=0;
        published_initialized=false;consumed=0;accepting=polled_once=false;last_poll=0;publish();
    }
};
TEST_F(Gt911,IdentityDecodeCadenceMappingAndResetRecovery){
    using namespace dg_board;
    EXPECT_EQ(touch::init(),ESP_ERR_INVALID_STATE);ready=true;
    product="nope";EXPECT_EQ(touch::init(),ESP_ERR_TIMEOUT); // invalid ID at primary then absent alternate
    clock_us=1000000;product="9271";tried.clear();ASSERT_EQ(touch::init(),ESP_OK);
    EXPECT_EQ(tried,(std::vector<unsigned>{0x5d}));
    TouchPoint point;contacts=1;x=799;y=255;ASSERT_EQ(touch::read_raw(point),ESP_OK);
    EXPECT_EQ(point.x,799);EXPECT_EQ(point.y,255);EXPECT_EQ(point.contacts,1);
    for(auto corner:std::vector<std::pair<uint16_t,uint16_t>>{{40,40},{759,40},{40,759},{759,759}}){
        auto a=corner.first,b=corner.second;ASSERT_TRUE(detail::touch_to_logical(a,b));
        EXPECT_EQ(a,corner.first==40?0:359);EXPECT_EQ(b,corner.second==40?0:359);
    }
    for(auto outside:std::vector<std::pair<uint16_t,uint16_t>>{{39,40},{40,39},{760,400},{400,760},{800,0}}){
        auto a=outside.first,b=outside.second;EXPECT_FALSE(detail::touch_to_logical(a,b));
    }
    x=y=400;auto before=reads;
    EXPECT_FALSE(sample(100,1));auto after=reads;EXPECT_GT(after,before);
    EXPECT_FALSE(sample(110,0));EXPECT_EQ(reads,after);
    EXPECT_FALSE(sample(140,0));EXPECT_FALSE(sample(220,1));EXPECT_TRUE(sample(260,0));
    EXPECT_EQ(touch::status().interrupts,0u);EXPECT_EQ(touch::status().coalesced,0u);
    EXPECT_EQ(touch::status().gestures.double_taps,1u);
    // No-ready samples cannot invent a release and turn one contact into two.
    EXPECT_FALSE(sample(400,1));status=0;EXPECT_FALSE(sample(440,0));status=0x80;
    EXPECT_FALSE(sample(480,0));EXPECT_FALSE(sample(560,1));
    read_error=ESP_ERR_TIMEOUT;EXPECT_FALSE(sample(600,0));read_error=ESP_OK;
    EXPECT_FALSE(sample(640,0));EXPECT_EQ(touch::status().read_errors,1u);
    auto doubles=touch::status().gestures.double_taps;
    EXPECT_FALSE(sample(800,1));EXPECT_FALSE(sample(840,0));
    contacts=1;++epoch;available=0x14;tried.clear();
    EXPECT_FALSE(sample(920,1));EXPECT_EQ(tried,(std::vector<unsigned>{0x5d,0x14}));
    EXPECT_FALSE(sample(960,0));EXPECT_EQ(touch::status().gestures.double_taps,doubles);
    lock_ok=false;EXPECT_EQ(touch::read_raw(point),ESP_ERR_TIMEOUT);lock_ok=true;
    clear_error=ESP_ERR_TIMEOUT;EXPECT_EQ(touch::read_raw(point),ESP_ERR_TIMEOUT);clear_error=ESP_OK;
    contacts=11;EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_RESPONSE);
    contacts=2;ASSERT_EQ(touch::read_raw(point),ESP_OK);EXPECT_EQ(point.contacts,2);
    EXPECT_GT(clears,0u);EXPECT_GT(removes,0u);EXPECT_FALSE(held);
}
TEST_F(Gt911,UnsignedCadenceSurvivesWrapAndNeverCatchesUp){
    using namespace dg_board;ready=true;ASSERT_EQ(touch::init(),ESP_OK);
    EXPECT_FALSE(sample(0xfffffff0,0));auto before=reads;
    EXPECT_FALSE(sample(0xfffffffau,0));EXPECT_EQ(reads,before);
    EXPECT_FALSE(sample(4,0));EXPECT_GT(reads,before);before=reads;
    EXPECT_FALSE(sample(2000,0));EXPECT_GT(reads,before);before=reads;
    EXPECT_FALSE(sample(2000,0));EXPECT_EQ(reads,before);
}

TEST_F(Gt911,NoReadyCannotSplitOneLongContactIntoTwoTaps){
    using namespace dg_board;ready=true;ASSERT_EQ(touch::init(),ESP_OK);
    auto before=touch::status().gestures;
    EXPECT_FALSE(sample(100,1));status=0;EXPECT_FALSE(sample(140,0));
    status=0x80;EXPECT_FALSE(sample(240,1));status=0;EXPECT_FALSE(sample(280,0));
    status=0x80;EXPECT_FALSE(sample(400,0));
    auto after=touch::status().gestures;
    EXPECT_EQ(after.taps,before.taps);EXPECT_EQ(after.double_taps,before.double_taps);
    EXPECT_EQ(after.rejected,before.rejected+1);
}
TEST_F(Gt911,PollMapsNativeTapSeparationToLogicalGestureRadius){
    using namespace dg_board;ready=true;ASSERT_EQ(touch::init(),ESP_OK);
    x=300;EXPECT_FALSE(sample(100,1));EXPECT_FALSE(sample(140,0));
    x=400;EXPECT_FALSE(sample(240,1));EXPECT_TRUE(sample(280,0)); // 100 native = 50 logical
}
TEST_F(Gt911,BorderContactCancelsRatherThanReleases){
    using namespace dg_board;ready=true;ASSERT_EQ(touch::init(),ESP_OK);
    auto before=touch::status().gestures;
    x=400;EXPECT_FALSE(sample(100,1));x=39;EXPECT_FALSE(sample(140,1));
    x=400;EXPECT_FALSE(sample(180,0));
    auto after=touch::status().gestures;
    EXPECT_EQ(after.taps,before.taps);EXPECT_EQ(after.rejected,before.rejected+1);
}
TEST_F(Gt911,SizeMismatchReadyBitAndPointFailureAcknowledgement){
    using namespace dg_board;ready=true;configured_size=799;
    EXPECT_NE(touch::init(),ESP_OK);configured_size=800;clock_us=1000000;
    ASSERT_EQ(touch::init(),ESP_OK);TouchPoint point{77,88,3};
    status=0;contacts=1;auto before=clears;
    EXPECT_EQ(touch::read_raw(point),ESP_ERR_NOT_FINISHED);EXPECT_EQ(point.contacts,3);EXPECT_EQ(clears,before);
    status=0x80;fail_register=0x814f;read_error=ESP_ERR_TIMEOUT;
    EXPECT_EQ(touch::read_raw(point),ESP_ERR_TIMEOUT);EXPECT_EQ(clears,before+1);
    read_error=ESP_OK;
    for(unsigned n=6;n<=10;++n){contacts=n;EXPECT_EQ(touch::read_raw(point),ESP_OK);EXPECT_EQ(point.contacts,n);}
}
TEST_F(Gt911,FailedReprobeBacksOffAndNeverReusesFailedRemovalHandle){
    using namespace dg_board;ready=true;ASSERT_EQ(touch::init(),ESP_OK);TouchPoint point;
    ++epoch;available=0;EXPECT_EQ(touch::read_raw(point),ESP_ERR_TIMEOUT);auto before=reads;
    for(unsigned n=0;n<50;++n)EXPECT_EQ(touch::read_raw(point),ESP_ERR_NOT_FINISHED);
    EXPECT_EQ(reads,before);clock_us=999999;EXPECT_EQ(touch::read_raw(point),ESP_ERR_NOT_FINISHED);
    available=0x14;clock_us=1000000;EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_STATE);
    EXPECT_EQ(touch::read_raw(point),ESP_OK);
    // A failed removal must leave no transmit-capable stale device pointer.
    ++epoch;remove_error=ESP_ERR_INVALID_STATE;EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_STATE);
    remove_error=ESP_OK;before=tried.size();available=0x5d;clock_us=2000000;
    EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_STATE);EXPECT_GT(tried.size(),before);
}

TEST_F(Gt911,ProbeLoopRemovalFailureDoesNotLeaveUsableHandle){
    using namespace dg_board;ready=true;product="nope";remove_error=ESP_ERR_INVALID_STATE;
    EXPECT_EQ(touch::init(),ESP_ERR_INVALID_STATE);
    remove_error=ESP_OK;product="9271";clock_us=1000000;const auto before=tried.size();
    TouchPoint point;EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_STATE);
    EXPECT_GT(tried.size(),before);EXPECT_EQ(touch::read_raw(point),ESP_OK);
}

TEST_F(Gt911,ResetRemovalFailureClearsHandleBeforeRetry){
    using namespace dg_board;ready=true;ASSERT_EQ(touch::init(),ESP_OK);TouchPoint point;
    ++epoch;remove_error=ESP_ERR_INVALID_STATE;const auto before=reads;
    EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_STATE);
    EXPECT_EQ(removes,1u);EXPECT_EQ(reads,before);
    remove_error=ESP_OK;
    EXPECT_EQ(touch::read_raw(point),ESP_ERR_NOT_FINISHED);
    EXPECT_EQ(removes,1u);EXPECT_EQ(reads,before);
    clock_us=1000000;
    EXPECT_EQ(touch::read_raw(point),ESP_ERR_INVALID_STATE);
    EXPECT_EQ(removes,1u); // No second rm_device on the stale handle.
    EXPECT_EQ(touch::read_raw(point),ESP_OK);
}
