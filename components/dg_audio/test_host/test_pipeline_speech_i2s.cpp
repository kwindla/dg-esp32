#include "dg_audio/pipeline.h"
#include "dg_audio/playback_dsp.h"
#include "dg_media_token.h"
#include "fake_i2s.h"
#include <gtest/gtest.h>
#include <thread>
#include <chrono>
#include <algorithm>
#include <cmath>
struct SpeechSource:dg::MediaSource {
    std::atomic<bool> connected{true};
    bool valid(uint64_t g) const override {return connected && g==1;}
};
TEST(PipelineSpeechI2s, ReconnectClearsUnsubmittedSuffixAndDelayedSpeechHistory) {
    fake::reset(false);fake::maximum_write=80;
    auto old=std::make_shared<SpeechSource>(),fresh=std::make_shared<SpeechSource>();std::atomic<unsigned> reads{0};
    int16_t first[320],second[320];
    for(unsigned i=0;i<320;++i) {
        first[i]=8000*std::sin(2*3.141592653589793*440*i/16000);
        second[i]=10000*std::sin(2*3.141592653589793*1370*i/16000);
    }
    fake::on_write=[&](unsigned call){if(call==1)old->connected=false;};
    auto read=[&](int16_t* out,size_t n,dg::MediaToken& token) {
        auto call=++reads;token={call==1?old:fresh,1};std::copy_n(call==1?first:second,n,out);return int(n);
    };
#ifdef DG_BASELINE
    auto provider=[&](int16_t* out,size_t n) {dg::MediaToken token;return read(out,n,token);};
#else
    auto provider=read;
#endif
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},provider,[](const int16_t*,size_t){},[&]{return reads<2;});
        bool done=false;for(unsigned i=0;i<1000;++i) {
            if(reads>=2 && fake::now>=180000) {done=true;break;}
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
        ASSERT_TRUE(done);
    }
    dg_audio::SpeechLevelConfig config;config.target_dbfs=-22;
    dg_audio::PlaybackDsp reference(1,0,config);
    reference.reset();reference.process(first,320);
    reference.reset();reference.reset();reference.process(second,320);
    int16_t tail[320]{};auto tail_samples=reference.flush(tail,320);
    std::lock_guard<std::mutex> lock(fake::mutex);
    ASSERT_GE(fake::submitted.size(),640u+tail_samples);
    for(unsigned i=0;i<80;++i)EXPECT_EQ(fake::submitted[i],first[i]);
    EXPECT_TRUE(std::all_of(fake::submitted.begin()+80,fake::submitted.begin()+320,[](auto s){return s==0;}));
    for(unsigned i=0;i<320;++i)EXPECT_EQ(fake::submitted[320+i],second[i])<<i;
    auto tail_at=std::search(fake::submitted.begin()+640,fake::submitted.end(),tail,tail+tail_samples);
    ASSERT_NE(tail_at,fake::submitted.end());
    EXPECT_TRUE(std::all_of(fake::submitted.begin()+640,tail_at,[](auto s){return s==0;}));
    EXPECT_TRUE(std::all_of(tail_at+tail_samples,fake::submitted.end(),[](auto s){return s==0;}));
    // The speech limiter's 64-sample delay begins anew in the new Session.
    EXPECT_TRUE(std::all_of(fake::submitted.begin()+320,fake::submitted.begin()+384,[](auto s){return s==0;}));
}

TEST(PipelineSpeechI2s, ExpiredDeviceTickPreservesValidSourceDspHistory) {
    fake::reset(false);auto source=std::make_shared<SpeechSource>();std::atomic<unsigned> reads{0};
    int16_t first[320],third[320];
    for(unsigned i=0;i<320;++i) {
        first[i]=8000*std::sin(2*3.141592653589793*440*i/16000);
        third[i]=9000*std::sin(2*3.141592653589793*1370*i/16000);
    }
    auto read=[&](int16_t* out,size_t n,dg::MediaToken& token) {
        auto call=++reads;token={source,1};std::copy_n(call==1?first:third,n,out);
        if(call==2)fake::advance(200); // Tick expires before DSP, media stays valid.
        return int(n);
    };
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},read,[](const int16_t*,size_t){},[&]{return reads<3;});
     bool done=false;for(unsigned i=0;i<1000;++i) {
         if(reads>=3 && fake::now>=500000){done=true;break;}
         std::this_thread::sleep_for(std::chrono::milliseconds(1));
     }ASSERT_TRUE(done);}
    dg_audio::SpeechLevelConfig config;config.target_dbfs=-22;
    dg_audio::PlaybackDsp reference(1,0,config);reference.reset();reference.reset();
    reference.process(first,320);reference.process(third,320);
    int16_t tail[320]{};auto n=reference.flush(tail,320);
    std::lock_guard<std::mutex> lock(fake::mutex);
    ASSERT_GE(fake::submitted.size(),640u+n);
    for(unsigned i=0;i<320;++i)EXPECT_EQ(fake::submitted[i],first[i]);
    auto at=std::search(fake::submitted.begin()+320,fake::submitted.end(),third,third+320);
    ASSERT_NE(at,fake::submitted.end());
    EXPECT_TRUE(std::all_of(fake::submitted.begin()+320,at,[](auto s){return s==0;}));
    auto tail_at=std::search(at+320,fake::submitted.end(),tail,tail+n);
    ASSERT_NE(tail_at,fake::submitted.end());
    EXPECT_TRUE(std::all_of(at+320,tail_at,[](auto s){return s==0;}));
    EXPECT_TRUE(std::all_of(tail_at+n,fake::submitted.end(),[](auto s){return s==0;}));
}

TEST(PipelineSpeechI2s, ZeroReadThenSameSourcePreservesLookaheadHistory) {
    fake::reset(false);auto source=std::make_shared<SpeechSource>();std::atomic<unsigned> reads{0};
    int16_t first[320],third[320];
    for(unsigned i=0;i<320;++i) {
        first[i]=8000*std::sin(2*3.141592653589793*440*i/16000);
        third[i]=9000*std::sin(2*3.141592653589793*1370*i/16000);
    }
    auto read=[&](int16_t* out,size_t n,dg::MediaToken& token) {
        auto call=++reads;if(call==2)return 0;
        token={source,1};std::copy_n(call==1?first:third,n,out);return int(n);
    };
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},read,[](const int16_t*,size_t){},[&]{return reads<3;});
     bool done=false;for(unsigned i=0;i<1000;++i) {
         if(reads>=3 && fake::now>=200000){done=true;break;}
         std::this_thread::sleep_for(std::chrono::milliseconds(1));
     }ASSERT_TRUE(done);}
    dg_audio::SpeechLevelConfig config;config.target_dbfs=-22;
    dg_audio::PlaybackDsp reference(1,0,config);reference.reset();reference.reset();
    reference.process(first,320);reference.process(third,320);
    std::lock_guard<std::mutex> lock(fake::mutex);
    ASSERT_GE(fake::submitted.size(),960u);
    for(unsigned i=0;i<320;++i)EXPECT_EQ(fake::submitted[640+i],third[i])<<i;
}
