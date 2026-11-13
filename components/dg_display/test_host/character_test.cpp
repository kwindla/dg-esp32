#include "dg_display/character.h"
#include "dg_audio/character_signals.h"
#include <gtest/gtest.h>
#include <algorithm>
#include <array>

using dg_display::Character;
TEST(Character,IdleTimingAndStaticMouth){
    Character cat;cat.reset(0);unsigned blinks=0,twitches=0;int64_t blink_start=-1,whisker_start=-1;
    for(int64_t now=0;now<120000;now+=10){
        const auto frame=cat.tick(now,0,false,0);EXPECT_EQ(frame.mouth,0u);
        if(cat.blinks()!=blinks){
            if(blink_start>=0){EXPECT_GE(now-blink_start,3000);EXPECT_LE(now-blink_start,6010);}
            blink_start=now;blinks=cat.blinks();
        }
        if(blink_start>=0 && now-blink_start<180)EXPECT_NE(frame.eyes,1u);
        if(blink_start>=0 && now-blink_start==180)EXPECT_EQ(frame.eyes,1u);
        if(cat.twitches()!=twitches){
            if(whisker_start>=0){EXPECT_GE(now-whisker_start,2000);EXPECT_LE(now-whisker_start,5010);}
            whisker_start=now;twitches=cat.twitches();
        }
        if(whisker_start>=0 && now-whisker_start<300)EXPECT_NE(frame.whiskers,0u);
        if(whisker_start>=0 && now-whisker_start>=510)EXPECT_EQ(frame.whiskers,0u);
    }
    EXPECT_GT(blinks,15u);EXPECT_GT(twitches,20u);
}
TEST(Character,SpeakingCancelsBlinkAndWhiskersAndMouthIsAtMostTenHz){
    Character cat;cat.reset(0);const auto blink=cat.next_blink();
    cat.tick(blink,0,false,0);
    EXPECT_NE(cat.tick(blink+20,0,false,0).eyes,1u);
    unsigned previous=0;int64_t changed=-1000;unsigned changes=0;
    for(int64_t now=blink+30;now<blink+10030;now+=5){
        const auto frame=cat.tick(now,((now/400)%2)?16000000:10000,true,1000000);
        EXPECT_TRUE(cat.speaking());EXPECT_FALSE(cat.attentive());
        EXPECT_EQ(frame.eyes,1u);EXPECT_EQ(frame.whiskers,0u);
        if(frame.mouth!=previous){EXPECT_GE(now-changed,100);changed=now;previous=frame.mouth;++changes;}
    }
    EXPECT_LE(changes,100u);EXPECT_GT(changes,10u);
}
TEST(Character,PlayoutSilenceReturnsToListeningWithinTwoHundredMs){
    Character cat;cat.reset(0);dg_audio::CharacterSignals audio;
    for(unsigned now=0;now<=1000;now+=20){audio.speaker(now,9000000);auto s=audio.read(now);cat.tick(now,s.speaker_power,false,0);}
    for(unsigned now=1020;now<=1200;now+=20){auto s=audio.read(now);cat.tick(now,s.speaker_power,false,0);}
    EXPECT_FALSE(cat.speaking());EXPECT_EQ(cat.tick(1200,0,false,0).mouth,0u);
}
TEST(Character,OpenMouthReturnsToSmileAcrossEveryUpdatePhase){
    for(unsigned end=1000;end<1100;++end){
        SCOPED_TRACE(end);
        Character cat;cat.reset(0);dg_audio::CharacterSignals audio;
        unsigned last_change=0,previous=0,returned=0;
        for(unsigned now=0;now<=end+200;++now){
            if(now<=end)audio.speaker(now,9000000);
            if(now%20)continue; // Production display polling cadence.
            auto sample=audio.read(now);
            auto frame=cat.tick(now,sample.speaker_power,false,0);
            if(frame.mouth!=previous){
                if(now)EXPECT_GE(now-last_change,100u);
                last_change=now;previous=frame.mouth;
            }
            if(now==1000)ASSERT_NE(frame.mouth,0u);
            if(now>end && frame.mouth==0 && !cat.speaking()){
                returned=now;break;
            }
        }
        ASSERT_NE(returned,0u);
        EXPECT_LE(returned-end,200u);
    }
}
TEST(Character,UserAttentionOnlyWithOpenMicAndSubtleWhiskerTiming){
    Character cat;cat.reset(0);
    cat.tick(0,0,false,9000000);EXPECT_FALSE(cat.attentive());
    cat.tick(20,0,true,2500);EXPECT_FALSE(cat.attentive());
    cat.tick(40,0,true,1000000);EXPECT_TRUE(cat.attentive());
    auto due=cat.next_whisker();cat.tick(due,0,true,1000000);
    EXPECT_GE(cat.next_whisker()-due,2000);EXPECT_LE(cat.next_whisker()-due,3000);
}
TEST(Character,DelayedOwnerDoesNotCatchUpWithBurstMouthUpdates){
    Character cat;cat.reset(0);cat.tick(0,1000000,false,0);
    auto frame=cat.tick(10000,25000000,false,0);
    for(int64_t now=10001;now<10100;++now)EXPECT_EQ(cat.tick(now,10000,false,0).mouth,frame.mouth);
}
TEST(CharacterAudio,FutureEnergyWaitsForPlayoutAndExpires){
    dg_audio::CharacterSignals audio;audio.speaker(100,1000);audio.speaker(120,2000);
    EXPECT_EQ(audio.read(99).speaker_power,0u);EXPECT_EQ(audio.read(100).speaker_power,1000u);
    EXPECT_EQ(audio.read(119).speaker_power,1000u);EXPECT_EQ(audio.read(120).speaker_power,2000u);
    EXPECT_EQ(audio.read(151).speaker_power,0u);
    audio.capture(150,12345,true);EXPECT_EQ(audio.read(151).mic_power,12345u);
    audio.capture(160,12345,false);EXPECT_FALSE(audio.read(160).mic_open);
    audio.capture(170,12345,true);EXPECT_FALSE(audio.read(371).mic_open);
}
TEST(CharacterAudio,TimestampWrapAndBoundedRing){
    dg_audio::CharacterSignals audio;audio.speaker(0xfffffff0u,123);
    EXPECT_EQ(audio.read(5).speaker_power,123u);
    for(unsigned n=0;n<100;++n)audio.speaker(1000+n*20,n+1);
    EXPECT_EQ(audio.read(2980).speaker_power,100u);
    EXPECT_EQ(audio.read(1000).speaker_power,0u);
}
