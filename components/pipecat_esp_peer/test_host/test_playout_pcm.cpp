#include "playout_pcm.h"
#include "playout_opus_fixture.h"
#include <gtest/gtest.h>
#include <stdexcept>
#include <cmath>
// Host uses the real libopus decoder. The identical renderer is also exercised
// by the vendor OpusDecoder on the board's playback-selftest command.
extern "C" {
void* opus_decoder_create(int,int,int*);
void opus_decoder_destroy(void*);
int opus_decode(void*,const unsigned char*,int,int16_t*,int,int);
int opus_decoder_ctl(void*,int,...);
}
using namespace pipecat::esp_peer;
using K=Playout::Kind;
struct RealDecoder {
    void* handle=nullptr;unsigned resets=0;
    RealDecoder() {int error;handle=opus_decoder_create(16000,1,&error);if(error)throw std::runtime_error("create");}
    ~RealDecoder() {opus_decoder_destroy(handle);}
    void reset() {++resets;if(opus_decoder_ctl(handle,4028))throw std::runtime_error("reset");}
    size_t decode(uint8_t* p,size_t n,int16_t* out,size_t capacity,bool plc) {
        int samples=opus_decode(handle,plc?nullptr:p,plc?0:int(n),out,int(capacity),0);
        if(samples!=320)throw std::runtime_error("decode");
        return samples;
    }
};
static Playout::Decision packet() {
    Playout::Decision d;d.kind=K::Decode;d.payload=const_cast<uint8_t*>(playout_opus_fixture);d.size=sizeof(playout_opus_fixture);return d;
}
TEST(PlayoutPcm, RealOpusFiveFramePlcFadeMatchesEverySample) {
    RealDecoder decoder,reference;PlayoutPcm pcm;int16_t out[320],raw[320];auto d=packet();
    ASSERT_TRUE(pcm.render(d,decoder,out).decoded);reference.decode(d.payload,d.size,raw,320,false);
    static constexpr float end[]={1,1,2.f/3,1.f/3,0};
    for(unsigned run=1;run<=5;++run) {
        d.kind=K::Plc;d.missing_run=run;auto result=pcm.render(d,decoder,out);ASSERT_FALSE(result.error);
        reference.decode(nullptr,0,raw,320,true);
        float to=end[run-1],from=run==1?1:end[run-2];
        for(unsigned i=0;i<320;++i)EXPECT_EQ(out[i],int16_t(raw[i]*(from+(to-from)*(i+1)/320.f)))<<run<<":"<<i;
    }
    EXPECT_EQ(out[319],0);
}
TEST(PlayoutPcm, RealOpusBadDecodeInvalidatesExpansionAndRecoveryKeepsFade) {
    RealDecoder decoder;PlayoutPcm pcm;int16_t out[320];auto d=packet();ASSERT_TRUE(pcm.render(d,decoder,out).decoded);
    uint8_t bad=255;d.payload=&bad;d.size=1;auto result=pcm.render(d,decoder,out);
    EXPECT_TRUE(result.error);EXPECT_FALSE(result.decoded);EXPECT_FALSE(pcm.valid());
    EXPECT_TRUE(std::all_of(out,out+320,[](auto s){return s==0;}));
    d.kind=K::Insert;pcm.render(d,decoder,out);EXPECT_TRUE(std::all_of(out,out+320,[](auto s){return s==0;}));
    d.kind=K::Plc;d.missing_run=5;pcm.render(d,decoder,out);EXPECT_EQ(out[319],0);
}
TEST(PlayoutPcm, RealOpusLongMuteResetsBeforeRecovery) {
    RealDecoder decoder,fresh;PlayoutPcm pcm;int16_t out[320],expected[320];auto d=packet();pcm.render(d,decoder,out);
    d.kind=K::Zero;d.missing_run=25;pcm.render(d,decoder,out);
    d=packet();ASSERT_TRUE(pcm.render(d,decoder,out).decoded);fresh.decode(d.payload,d.size,expected,320,false);
    EXPECT_EQ(decoder.resets,1u);for(unsigned i=0;i<320;++i)EXPECT_EQ(out[i],expected[i]);
}
struct QuietDecoder {
    void reset() {}
    size_t decode(uint8_t*,size_t,int16_t* out,size_t,bool) {std::fill_n(out,320,1);return 320;}
};
TEST(PlayoutPcm, InactiveNonzeroDecodedPcmExpandsToExactZero) {
    RealDecoder decoder;PlayoutPcm pcm;int16_t out[320];auto d=packet();
    d.payload=const_cast<uint8_t*>(playout_quiet_fixture);d.size=sizeof(playout_quiet_fixture);
    ASSERT_TRUE(pcm.render(d,decoder,out).decoded);
    EXPECT_TRUE(std::any_of(out,out+320,[](auto s){return s!=0;}));
    EXPECT_TRUE(pcm.valid());EXPECT_FALSE(PlayoutPcm::active(out));
    d.kind=K::Insert;pcm.render(d,decoder,out);EXPECT_TRUE(std::all_of(out,out+320,[](auto s){return s==0;}));
}
TEST(PlayoutPcm, FailedResetProducesZerosAndReleasesReadingOwnership) {
    struct Broken:QuietDecoder {void reset() {throw std::runtime_error("reset");}} decoder;
    Playout::Storage storage{};Playout playout(storage);playout.authorize(7,1);auto p=packet();
    RtpAudioView packet{7,0,0,false,p.payload,p.size};ASSERT_TRUE(playout.insert(packet,0));
    dg::RenderTick tick;tick.valid=true;tick.device_epoch=1;tick.deadline_us=int64_t(Playout::target_frames)*20000;
    auto d=playout.pull(tick,false);ASSERT_TRUE(d.lease);ASSERT_TRUE(d.reset_decoder);
    PlayoutPcm pcm;int16_t out[320];auto result=pcm.render(d,decoder,out);playout.finish(d,result.decoded);
    EXPECT_TRUE(result.error);EXPECT_EQ(playout.snapshot(100000).occupancy,0u);
    EXPECT_EQ(playout.snapshot(100000).counters.decode_errors,1u);
}
TEST(PlayoutPcm, CancellationZerosAndResetsRealOpusBeforeNextReply) {
    RealDecoder decoder,fresh;PlayoutPcm pcm,reference;int16_t out[320],expected[320];
    auto d=packet();ASSERT_TRUE(pcm.render(d,decoder,out).decoded);ASSERT_TRUE(pcm.valid());
    d.kind=K::Cancelled;d.reset_decoder=true;
    auto result=pcm.render(d,decoder,out);EXPECT_FALSE(result.decoded);EXPECT_FALSE(result.error);
    EXPECT_EQ(decoder.resets,1u);EXPECT_FALSE(pcm.valid());
    for(auto sample:out)EXPECT_EQ(sample,0);
    d=packet();ASSERT_TRUE(pcm.render(d,decoder,out).decoded);ASSERT_TRUE(reference.render(d,fresh,expected).decoded);
    for(unsigned i=0;i<320;++i)EXPECT_EQ(out[i],expected[i]);
}
