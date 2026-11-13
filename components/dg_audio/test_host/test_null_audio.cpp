#include <gtest/gtest.h>
#include "dg_audio/mic.h"
#include "dg_audio/speaker.h"
#include "dg_render_tick.h"
#include "freertos/task.h"
#include <array>
#include <algorithm>
namespace {
int64_t now=1000000,sleep_overshoot=0;
class NullAudio:public testing::Test {
    void SetUp()override{now=1000000;sleep_overshoot=0;}
};
}
int64_t esp_timer_get_time(){return now;}
void vTaskDelay(TickType_t ms){now+=int64_t(ms)*1000+sleep_overshoot;}
// There are deliberately no I2S or GPIO implementations in this binary:
// an accidental hardware call from the null backend fails the link.
TEST_F(NullAudio,SilenceCadenceTimeoutAndNoCatchUpBurst){
    dg_audio::Mic mic;std::array<int16_t,320> pcm,raw;
    for(unsigned frame=0;frame<600;++frame){
        pcm.fill(123);raw.fill(-123);const auto before=now;
        EXPECT_EQ(mic.read(pcm.data(),320,100,raw.data()),320u);
        EXPECT_EQ(now-before,20000);EXPECT_TRUE(std::all_of(pcm.begin(),pcm.end(),[](auto s){return s==0;}));
        EXPECT_EQ(pcm,raw);
    }
    now+=100000;const auto resumed=now;
    EXPECT_EQ(mic.read(pcm.data(),320,100),320u);EXPECT_EQ(now-resumed,0);
    EXPECT_EQ(mic.read(pcm.data(),320,5),0u);
    EXPECT_EQ(mic.errors(),1u);EXPECT_EQ(dg_audio::microphone_status().read_timeouts,1u);
    EXPECT_EQ(dg_audio::microphone_status().read_frames,601u*320);
}
TEST_F(NullAudio,ProcessingTimePreservesTwentyMsIntervals){
    dg_audio::Mic mic;std::array<int16_t,320> pcm;
    mic.read(pcm.data(),320,100);const auto before=now;now+=3000;
    mic.read(pcm.data(),320,100);EXPECT_EQ(now-before,20000);
}
TEST_F(NullAudio,LateCallerReceivesImmediatelyThenRestartsTheGrid){
    dg_audio::Mic mic;std::array<int16_t,320> pcm;
    mic.read(pcm.data(),320,100);
    for(int64_t late:{21000,49000,100000}){
        const auto before=now;now+=late;
        EXPECT_EQ(mic.read(pcm.data(),320,100),320u);EXPECT_EQ(now-before,late);
        const auto delivered=now;now+=3000;
        EXPECT_EQ(mic.read(pcm.data(),320,100),320u);EXPECT_EQ(now-delivered,20000);
    }
}
TEST_F(NullAudio,AbsoluteDeadlinesPreventSleepOvershootDrift){
    dg_audio::Mic mic;std::array<int16_t,320> pcm;sleep_overshoot=137;
    const auto began=now;
    for(unsigned i=0;i<3000;++i)EXPECT_EQ(mic.read(pcm.data(),320,100),320u);
    EXPECT_GE(now-began,60000000);EXPECT_LE(now-began,60001200);
}
TEST_F(NullAudio,CapsLargeReadsWithoutTouchingTheSuffix){
    dg_audio::Mic mic;std::array<int16_t,640> pcm,raw;pcm.fill(123);raw.fill(-123);
    const auto began=now;EXPECT_EQ(mic.read(pcm.data(),640,100,raw.data()),320u);
    EXPECT_EQ(now-began,20000);EXPECT_EQ(dg_audio::microphone_status().read_frames,320u);
    for(size_t i=0;i<640;++i){EXPECT_EQ(pcm[i],i<320?0:123);EXPECT_EQ(raw[i],i<320?0:-123);}
}
TEST_F(NullAudio,PlaybackDiscardsAndSoftwareClockProgresses){
    dg_audio::Speaker speaker;std::array<int16_t,320> pcm;pcm.fill(20000);
    speaker.start();const auto epoch=speaker.clock().epoch();
    bool rendered=false;
    for(unsigned ms=0;ms<200;++ms){
        auto op=speaker.opportunity(now);
        if(op.refill_samples)speaker.refill(op.refill_samples);
        if(op.ready){
            ASSERT_TRUE(speaker.render(op.tick));EXPECT_TRUE(speaker.valid(op.tick));
            EXPECT_EQ(speaker.write(pcm.data(),320,5),320u);rendered=true;
        }
        now+=1000;
    }
    EXPECT_TRUE(rendered);EXPECT_GT(speaker.clock().completed(),2000u);
    EXPECT_FALSE(speaker.audible());EXPECT_EQ(speaker.errors(),0u);
    speaker.begin_drain();now+=100000;speaker.opportunity(now);EXPECT_TRUE(speaker.drained());
    speaker.idle();EXPECT_FALSE(dg::device_clock.snapshot().valid);
    speaker.start();EXPECT_GT(speaker.clock().epoch(),epoch);
}
