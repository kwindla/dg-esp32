#include "dg_audio/playback_dsp.h"
#include <gtest/gtest.h>
#include <cmath>
#include <random>
using dg_audio::PlaybackDsp;
TEST(PlaybackDsp, GainAndSymmetry) {
    PlaybackDsp d(.2f,.3f);EXPECT_NEAR(d.sample(.5f),.1f,1e-7);EXPECT_NEAR(d.sample(-.5f),-.1f,1e-7);EXPECT_EQ(d.engagements(),0u);
}
TEST(PlaybackDsp, KneeValueAndSlopeAreContinuous) {
    PlaybackDsp d(1,.2f);float knee=.16f,delta=.00001f;
    float below=d.sample(knee-delta),at=d.sample(knee),above=d.sample(knee+delta);
    EXPECT_NEAR(at,knee,1e-7);EXPECT_NEAR((at-below)/delta,1,.001);EXPECT_NEAR((above-at)/delta,1,.001);
    EXPECT_EQ(d.engagements(),1u);
}
TEST(PlaybackDsp, NoOvershootWithHighpassAndFullScaleTransitions) {
    PlaybackDsp d(5,.1f,300);
    for(unsigned i=0;i<100000;++i) EXPECT_LE(std::abs(d.sample(i%37<18?1.f:-1.f)),.1f);
    EXPECT_GT(d.engagements(),0u);
}
static float response(float hz) {
    PlaybackDsp d(1,1,300);double power=0;
    for(unsigned i=0;i<32000;++i) {
        float y=d.sample(.01f*std::sin(2*3.141592653589793*hz*i/16000));if(i>=16000)power+=y*y;
    }
    return std::sqrt(power/16000)/(.01/std::sqrt(2.));
}
TEST(PlaybackDsp, ButterworthHighpassResponse) {
    EXPECT_NEAR(response(300),1/std::sqrt(2.),.002);EXPECT_LT(response(50),.03);EXPECT_GT(response(1500),.998);
}
TEST(PlaybackDsp, InvalidConfigurationAndPcmBound) {
    EXPECT_THROW(PlaybackDsp(1,0),std::invalid_argument);
    EXPECT_THROW(PlaybackDsp(1,1,8000),std::invalid_argument);
    PlaybackDsp d(10,.05f);int16_t pcm[]={-32768,32767,0};d.process(pcm,3);EXPECT_GE(pcm[0],-1639);EXPECT_LE(pcm[1],1639);EXPECT_EQ(pcm[2],0);
}
TEST(PlaybackDspSpeech, TargetAndSilenceHold) {
    dg_audio::SpeechLevelConfig c;c.target_dbfs=-26;
    PlaybackDsp d(.5,500,c);double power=0;
    for(unsigned i=0;i<16000*12;++i) {
        float y=d.sample(.025f*std::sin(2*3.141592653589793*1000*i/16000));
        if(i>=16000*11) power+=y*y;
    }
    EXPECT_NEAR(10*std::log10(power/16000),-26,.25);
    for(unsigned i=0;i<3200;++i)d.sample(0);
    float gain=d.normalizer_gain();
    for(unsigned i=0;i<16000*10;++i)EXPECT_NEAR(d.sample(0),0,1e-10);
    EXPECT_EQ(d.normalizer_gain(),gain);
    d.reset();EXPECT_EQ(d.normalizer_gain(),gain);
}
TEST(PlaybackDspSpeech, FullScaleBurstsBoundedAcrossPacketBoundaries) {
    dg_audio::SpeechLevelConfig c;c.target_dbfs=-20;
    PlaybackDsp d(.2,500,c);
    std::vector<int16_t> pcm(160000);
    for(size_t i=0;i<pcm.size();++i)pcm[i]=int16_t((i%211<13?32767:491)*(i%2?1:-1));
    for(size_t at=0;at<pcm.size();at+=137)d.process(pcm.data()+at,std::min(size_t(137),pcm.size()-at));
    for(auto sample:pcm)EXPECT_LE(std::abs(int(sample)),6554);
    EXPECT_GT(d.engagements(),0u);EXPECT_GT(d.peak_exceedances(),0u);
}
TEST(PlaybackDspSpeech, LookaheadAndReset) {
    dg_audio::SpeechLevelConfig c;c.lookahead_ms=2.5;
    PlaybackDsp d(.2,0,c);EXPECT_EQ(d.latency_samples(),80u);
    EXPECT_EQ(d.sample(.9),0);
    for(unsigned i=0;i<79;++i)EXPECT_EQ(d.sample(0),0);
    EXPECT_NEAR(d.sample(0),.2,1e-6);
    d.sample(.9);d.reset();for(unsigned i=0;i<100;++i)EXPECT_EQ(d.sample(0),0);
    c.lookahead_ms=3;EXPECT_THROW(PlaybackDsp(.2,500,c),std::invalid_argument);
}
TEST(PlaybackDspSpeech, ChunkInvariantAndBoundedGain) {
    dg_audio::SpeechLevelConfig c;c.max_gain_db=6;c.activity_dbfs=-65;
    PlaybackDsp a(.2,500,c),b(.2,500,c);
    std::vector<int16_t> x(16000*5),y;
    for(size_t i=0;i<x.size();++i)x[i]=int16_t(100*std::sin(2*3.141592653589793*1000*i/16000));
    y=x;a.process(x.data(),x.size());
    for(size_t i=0;i<y.size();i+=137)b.process(y.data()+i,std::min(size_t(137),y.size()-i));
    EXPECT_EQ(x,y);EXPECT_LE(a.normalizer_gain(),std::pow(10.f,.3f));
}

TEST(PlaybackDspSpeech, BruteForceCeilingWithRandomChunksAndResets) {
    std::mt19937 rng(822);
    for(unsigned config=0;config<400;++config) {
        dg_audio::SpeechLevelConfig c;
        c.target_dbfs=-12-float(rng()%29);c.max_gain_db=rng()%37;
        c.lookahead_ms=.5f+float(rng()%201)/100;c.limiter_release_ms=50+rng()%51;
        c.compression_ratio=1+rng()%4;c.compression_threshold_dbfs=-30+int(rng()%31);
        float ceiling=(5+rng()%96)/100.f;
        PlaybackDsp d(ceiling,config%2?500:0,c);
        std::vector<int16_t> pcm(48000);
        for(size_t i=0;i<pcm.size();++i) {
            switch(config%6) {
                case 0: pcm[i]=i%800<400?32767:-32768;break; // Full-scale square.
                case 1: pcm[i]=i<16000?0:32767;break; // DC step.
                case 2: pcm[i]=i%319==0?32767:0;break; // Impulses.
                case 3: pcm[i]=i%211<13?(i%2?32767:-32768):0;break;
                case 4: pcm[i]=int16_t(rng());break;
                default: pcm[i]=i%997==0?32767:int16_t(9000*std::sin(i*.39));
            }
        }
        int bound=int(std::lround(ceiling*32767))+1;
        for(size_t at=0;at<pcm.size();) {
            size_t n=std::min(size_t(1+rng()%641),pcm.size()-at);
            if(rng()%17==0)d.reset();
            d.process(pcm.data()+at,n);
            for(size_t i=at;i<at+n;++i) ASSERT_LE(std::abs(int(pcm[i])),bound)<<config<<" "<<i;
            at+=n;
        }
    }
}
TEST(PlaybackDspSpeech, AttackRampHoldAndSlowRelease) {
    dg_audio::SpeechLevelConfig c;c.target_dbfs=-26;c.compression_ratio=1;c.lookahead_ms=2;
    PlaybackDsp d(.05,0,c);
    std::vector<float> output(2000);
    for(size_t i=0;i<output.size();++i) output[i]=d.sample(i==100?1.f:.1f);
    // Onset of the attack is 4 ms before the delayed peak. A 2 ms
    // boxcar ramps gain smoothly, then holds through and after the peak.
    EXPECT_GT(output[100],output[115]);EXPECT_GT(output[115],output[130]);
    for(size_t i=101;i<132;++i) EXPECT_NEAR(output[i]-output[i-1],output[110]-output[109],1e-7);
    EXPECT_NEAR(output[164],.05,1e-6);
    for(size_t i=165;i<197;++i) EXPECT_NEAR(output[i],.005,1e-6);
    EXPECT_GT(output[230],output[197]);EXPECT_LT(output[230],.012);
    EXPECT_GT(output[1200],output[230]);
}
TEST(PlaybackDspSpeech, CompressionStillDeliversTargetRms) {
    dg_audio::SpeechLevelConfig c;c.target_dbfs=-19;c.max_gain_db=30;
    PlaybackDsp d(.5,500,c);double power=0;
    for(unsigned i=0;i<16000*20;++i) {
        float y=d.sample(.02f*std::sin(2*3.141592653589793*1000*i/16000));
        if(i>=16000*19)power+=y*y;
    }
    EXPECT_NEAR(10*std::log10(power/16000),c.target_dbfs,.3);
}
TEST(PlaybackDspSpeech, FlushPreservesTailAndResetIsCounted) {
    dg_audio::SpeechLevelConfig c;c.compression_ratio=1;
    PlaybackDsp d(.5,0,c),reference(.5,0,c);
    std::vector<int16_t> input(101,3000),full(input);
    full.resize(input.size()+d.latency_samples());reference.process(full.data(),full.size());
    d.process(input.data(),input.size());std::vector<int16_t> tail(d.latency_samples());
    EXPECT_EQ(d.flush(tail.data(),tail.size()),tail.size());
    EXPECT_EQ(d.flush(tail.data(),tail.size()),0u);
    input.insert(input.end(),tail.begin(),tail.end());EXPECT_EQ(input,full);
    EXPECT_EQ(d.resets(),0u);d.reset();EXPECT_EQ(d.resets(),1u);
}
TEST(PlaybackDspSpeech, CompressionKneeHasContinuousValueAndSlope) {
    dg_audio::SpeechLevelConfig c;c.target_dbfs=-26;
    float threshold=std::pow(10.f,c.compression_threshold_dbfs/20),width=threshold*.25f;
    auto compressed=[&](float magnitude) {
        PlaybackDsp d(1,0,c);
        d.sample(magnitude/d.normalizer_gain());
        float output=0;for(size_t i=0;i<d.latency_samples();++i)output=d.sample(0);
        return output;
    };
    float delta=1e-5f;
    for(float edge:{threshold-width*.5f,threshold+width*.5f}) {
        float left=(compressed(edge)-compressed(edge-delta))/delta;
        float right=(compressed(edge+delta)-compressed(edge))/delta;
        EXPECT_NEAR(left,right,.003);
        EXPECT_NEAR(left,edge<threshold?1.f:1/c.compression_ratio,.003);
    }
}
TEST(PlaybackDspSpeech, UnreachableTargetDoesNotChaseLimiterLoss) {
    dg_audio::SpeechLevelConfig c;c.target_dbfs=-16;c.max_gain_db=30;c.compression_ratio=1;
    PlaybackDsp small(.01f,0,c),large(1.f,0,c);
    for(unsigned i=0;i<16000*30;++i) {
        float x=.02f*std::sin(2*3.141592653589793*1000*i/16000);
        ASSERT_LE(std::abs(small.sample(x)),.010001f);large.sample(x);
    }
    EXPECT_NEAR(small.normalizer_gain(),large.normalizer_gain(),1e-6f);
    EXPECT_LT(small.normalizer_gain(),12.f);
}
TEST(PlaybackDspSpeech, LimiterReleaseTimeConstantIs75ms) {
    dg_audio::SpeechLevelConfig c;c.compression_ratio=1;c.activity_dbfs=-30;
    PlaybackDsp d(.05f,0,c);
    float at50=0,at125=0;
    for(int i=0;i<2400;++i) {
        float out=d.sample(i==100?1.f:.001f);
        if(i==100+64+64+800)at50=out/(.001f*d.normalizer_gain());
        if(i==100+64+64+2000)at125=out/(.001f*d.normalizer_gain());
    }
    EXPECT_NEAR((1-at125)/(1-at50),std::exp(-1.f),.025f);
}
