#include "dg_audio/mic.h"
#include "dg_audio/aec.h"
#include "codec/es7210_slots.h"
#include "driver/i2s_std.h"
#include <gtest/gtest.h>
#include <algorithm>
#include <array>
#include <cstring>
#include <vector>

namespace {
i2s_channel_obj_t channel;
std::vector<uint32_t> dma;
std::vector<int16_t> microphone,reference;
bool saw_raw_slots=false;
unsigned overflows=0,completions=0;
int read_error=ESP_OK,afe_sample=0;bool override_afe=false;
std::vector<dg_audio::AecReadTiming> timings;
}
namespace dg_audio::codec {void release_rx(i2s_chan_handle_t) {} i2s_channel_obj_t* rx() {return &channel;} unsigned capture_overflows() {return overflows;} unsigned capture_completions() {return completions;}}
int i2s_channel_read(i2s_channel_obj_t* p,void* output,size_t requested,size_t* bytes,uint32_t) {
    EXPECT_EQ(p,&channel);*bytes=std::min(requested,dma.size()*4);
    std::memcpy(output,dma.data(),*bytes);return read_error;
}
namespace dg_audio {
EchoCanceller::EchoCanceller()=default;
EchoCanceller::~EchoCanceller()=default;
bool EchoCanceller::process(const int16_t* mic,const int16_t* ref,size_t n,int16_t* out,const uint32_t* raw,AecReadTiming timing) {
    EXPECT_LE(timing.lower_us,timing.upper_us);timings.push_back(timing);
    microphone.assign(mic,mic+n);reference.assign(ref,ref+n);std::copy_n(mic,n,out);
    if(override_afe)std::fill_n(out,n,afe_sample);
    saw_raw_slots=raw && std::equal(raw,raw+2*n,dma.begin());return true;
}
}
TEST(EchoearMic, ActualMicReadFeedsMic3LowHalfOfFirstWordToAfe) {
    // Word0=MIC1,MIC3; word1=MIC2,MIC4. All channels differ, including signs.
    dma={0x0123fedcu,0x45678000u,0x80007fffu,0xffff1357u,0x7fff8000u,0x00002468u};
    dg_audio::Mic mic;std::array<int16_t,3> out{},raw{};
    ASSERT_EQ(mic.read(out.data(),3,10,raw.data()),3u);
    EXPECT_EQ(microphone,(std::vector<int16_t>{0x123,-32768,32767}));
    EXPECT_EQ(reference,(std::vector<int16_t>{-292,32767,-32768}));
    EXPECT_TRUE(std::equal(raw.begin(),raw.end(),microphone.begin()));
    EXPECT_TRUE(saw_raw_slots);EXPECT_EQ(mic.errors(),0u);
}
TEST(EchoearMic, DecoderNamesEverySignedChannelInWireOrder) {
    const auto slots=dg_audio::es7210_slots(0x80007fffu,0xffff0123u,dg_board::audio_hw(true));
    EXPECT_EQ(slots.mic1,-32768);EXPECT_EQ(slots.mic3,32767);
    EXPECT_EQ(slots.mic2,-1);EXPECT_EQ(slots.mic4,0x123);
    for(int32_t i=-32768;i<=32767;++i) {
        const auto bits=uint32_t(uint16_t(i));
        EXPECT_EQ(dg_audio::signed16(bits),i);
    }
}

TEST(EchoearMic, ZeroLengthReadCarriesGapToNextCapturedBlock) {
    dma.clear();timings.clear();
    dg_audio::Mic mic;std::array<int16_t,3> out{};
    EXPECT_EQ(mic.read(out.data(),3,10),0u);
    dma={0x0123fedcu,0x45678000u,0x80007fffu,0xffff1357u,0x7fff8000u,0x00002468u};
    EXPECT_EQ(mic.read(out.data(),3,10),3u);
    ASSERT_EQ(timings.size(),2u);EXPECT_TRUE(timings[1].gap);
}

TEST(EchoearMic, OverflowsMarkGapsAndLowerBoundIncludes75msDmaResidence) {
    dma={0,0};timings.clear();overflows=0;
    dg_audio::Mic mic;int16_t out;
    mic.read(&out,1,10);mic.read(&out,1,10);
    ASSERT_EQ(timings.size(),2u);EXPECT_FALSE(timings[1].gap);
    // Fake clock advances 20 ms per call: prior begin = prior end - 20 ms.
    EXPECT_EQ(timings[1].lower_us,timings[0].upper_us-20000-75000);
    ++overflows;mic.read(&out,1,10);EXPECT_TRUE(timings.back().gap);
    mic.read(&out,1,10);EXPECT_FALSE(timings.back().gap);overflows=0;
}
TEST(EchoearMic, AdcAfeAndI2sFailuresHaveSeparateCounters) {
    dma={0,0};override_afe=true;afe_sample=32767;completions=17;
    {
        dg_audio::Mic mic;int16_t out;
        mic.read(&out,1,10);
        EXPECT_EQ(mic.clips(),0u);EXPECT_EQ(mic.adc_clips(),0u);
        dma={0x7fff0000,0};afe_sample=0;mic.read(&out,1,10);
        EXPECT_EQ(mic.clips(),1u);EXPECT_EQ(mic.adc_clips(),1u);
        read_error=ESP_ERR_TIMEOUT;dma.clear();mic.read(&out,1,10);
        const auto s=dg_audio::microphone_status();
        EXPECT_EQ(s.adc_clips,1u);EXPECT_EQ(s.gain_clips,0u);
        EXPECT_EQ(s.read_errors,1u);EXPECT_EQ(s.read_timeouts,1u);
        EXPECT_EQ(s.read_frames,2u);EXPECT_EQ(s.rx_completions,17u);
    }
    override_afe=false;read_error=ESP_OK;completions=0;
}

TEST(EchoearMic, TableClockUses240FrameDescriptorsAndFiveDescriptorResidence) {
    // At 11 completions a 4-descriptor ring plus the active descriptor cannot
    // yet use the first observation (count 6). At 12 it can. Swapping the
    // constructor's frames/descriptors leaves both bounds on the fallback.
    for(unsigned last:{11u,12u}) {
        dma={0,0};timings.clear();overflows=0;completions=6;
        dg_audio::Mic mic;int16_t out;
        mic.read(&out,1,10);completions=7;mic.read(&out,1,10);
        completions=last;mic.read(&out,1,10);
        ASSERT_EQ(timings.size(),3u);
        const auto first_begin=timings[0].upper_us-20000;
        const auto second_begin=timings[1].upper_us-20000;
        EXPECT_EQ(timings[2].lower_us,last==12?first_begin:second_begin-75000);
    }
    completions=0;
}
