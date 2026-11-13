#include "dg_audio/aec.h"
#include "dg_audio/character_signals.h"
#include "esp_afe_aec.h"
#include <gtest/gtest.h>
#include <array>
#include <algorithm>
#include <stdexcept>
#include <cstdlib>
#include <cstdio>
#include <csignal>

#ifdef DG_AEC_SDK_ABORT_TEST
void aec_test_allocation_failed(size_t,uint32_t);
#else
void aec_test_allocation_failed(size_t,uint32_t) {std::abort();}
#endif

namespace {
int live=0,allocations=0,chunk=512,factory_stage=0,fail_stage=0,omit=0;
bool short_result=false,invalid_pcm=false;
void* vendor_allocate(size_t bytes) {
    if(++factory_stage==fail_stage) {
        // The representative factory intentionally has the pinned factory's
        // unchecked uses. The actual SDK failure function must stop it first.
        aec_test_allocation_failed(bytes,0x404);return nullptr;
    }
    void* p=std::calloc(1,std::max(size_t(1),bytes));
    if(p)++allocations;
    return p;
}
void vendor_free(void* p) {if(p) {--allocations;std::free(p);}}
class AecWrapper:public testing::Test {
    void SetUp() override {
        ASSERT_EQ(live,0);ASSERT_EQ(allocations,0);chunk=512;short_result=invalid_pcm=false;
        factory_stage=fail_stage=omit=0;dg_audio::aec_reference_gain(0);
    }
    void TearDown() override {
        EXPECT_EQ(live,0);EXPECT_EQ(allocations,0);EXPECT_FALSE(dg_audio::aec_status().enabled);
    }
};
}
afe_aec_handle_t* afe_aec_create(const char* channels,int filter,int type,int mode) {
    EXPECT_STREQ(channels,"MR");EXPECT_EQ(filter,4);
    EXPECT_EQ(type,AFE_TYPE_FD);EXPECT_EQ(mode,AFE_MODE_LOW_COST);
    if(omit==6)return nullptr;
    auto* h=static_cast<afe_aec_handle_t*>(vendor_allocate(sizeof(afe_aec_handle_t)));++live;
    h->mode=AEC_MODE_FD_LOW_COST;h->frame_size=chunk;
    h->pcm_config={2,1,nullptr,1,nullptr,16000};
    h->pcm_config.mic_ids=static_cast<uint8_t*>(vendor_allocate(1));h->pcm_config.mic_ids[0]=0;
    h->pcm_config.ref_ids=static_cast<uint8_t*>(vendor_allocate(1));h->pcm_config.ref_ids[0]=1;
    h->handle=static_cast<aec_handle_t*>(vendor_allocate(sizeof(aec_handle_t)));
    h->handle->aec_handle=vendor_allocate(256);
    *static_cast<uint8_t*>(h->handle->aec_handle)=0;
    h->data=static_cast<int16_t*>(vendor_allocate(4*std::max(1,chunk)));
    if(omit==1) {vendor_free(h->data);h->data=nullptr;}
    if(omit==2) {vendor_free(h->pcm_config.mic_ids);h->pcm_config.mic_ids=nullptr;}
    if(omit==3) {vendor_free(h->pcm_config.ref_ids);h->pcm_config.ref_ids=nullptr;}
    if(omit==4) {vendor_free(h->handle->aec_handle);h->handle->aec_handle=nullptr;}
    if(omit==5) {vendor_free(h->handle->aec_handle);vendor_free(h->handle);h->handle=nullptr;}
    if(invalid_pcm)h->pcm_config.total_ch_num=4;
    return h;
}
int afe_aec_get_chunksize(afe_aec_handle_t* h) {return h->frame_size;}
size_t afe_aec_process(afe_aec_handle_t*,const int16_t* input,int16_t* output) {
    for(int i=0;i<chunk;++i)output[i]=input[2*i];
    return short_result?0:2*chunk;
}
void afe_aec_destroy(afe_aec_handle_t* h) {
    // Deliberately reject incomplete public state: the old forgiving fake hid
    // a vendor-destructor precondition violation.
    if(!h || !h->handle || !h->handle->aec_handle || !h->data ||
       !h->pcm_config.mic_ids || !h->pcm_config.ref_ids) {
        std::fputs("INVALID_VENDOR_DESTROY\n",stderr);std::_Exit(86);
    }
    --live;vendor_free(h->pcm_config.mic_ids);vendor_free(h->pcm_config.ref_ids);
    if(h->handle) {vendor_free(h->handle->aec_handle);vendor_free(h->handle);}
    vendor_free(h->data);vendor_free(h);
}

TEST_F(AecWrapper, CaptureKeepsAllThreeChannelsAndQueriedDelay) {
    dg_audio::EchoCanceller aec;
    EXPECT_EQ(live,1);EXPECT_EQ(dg_audio::aec_status().adapter_delay_samples,512u);
    std::array<int16_t,320> mic,ref,out;mic.fill(1000);ref.fill(-2000);
    dg_audio::aec_record(true);
    EXPECT_THROW(dg_audio::aec_capture(),std::runtime_error);
    for(int i=0;i<4;++i)ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data()));
    dg_audio::aec_record(false);
    const auto capture=dg_audio::aec_capture();
    ASSERT_EQ(capture.frames,1280u);EXPECT_FALSE(capture.truncated);
    for(size_t i=0;i<capture.frames;++i) {
        EXPECT_EQ(capture.triples[3*i],1000);EXPECT_EQ(capture.triples[3*i+1],-2000);
        EXPECT_EQ(capture.triples[3*i+2],i<512?0:1000);
    }
    const auto status=dg_audio::aec_status();
    EXPECT_EQ(status.calls,2u);EXPECT_EQ(status.wall_us,2u);
    EXPECT_EQ(status.input_samples,1280u);EXPECT_EQ(status.output_samples,1280u);
    EXPECT_EQ(status.errors,0u);
    dg_audio::aec_record(true);dg_audio::aec_record(false);
    EXPECT_EQ(dg_audio::aec_capture().frames,0u);
}

TEST_F(AecWrapper, OptionalRawSlotCapturePreservesAllFourChannelsAndProcessed) {
    dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic,ref,out;mic.fill(1000);ref.fill(-2000);
    std::array<uint32_t,640> raw;
    for(size_t i=0;i<320;++i) {raw[2*i]=(1000u<<16)|uint16_t(-2000);raw[2*i+1]=(333u<<16)|uint16_t(-444);}
    dg_audio::aec_record(true,true);
    for(int i=0;i<4;++i)ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),raw.data()));
    dg_audio::aec_record(false);
    auto capture=dg_audio::aec_capture();ASSERT_EQ(capture.channels,5u);ASSERT_EQ(capture.frames,1280u);
    for(size_t i=0;i<capture.frames;++i) {
        const auto* p=capture.triples+5*i;
        EXPECT_EQ(p[0],1000);EXPECT_EQ(p[1],333);EXPECT_EQ(p[2],-2000);EXPECT_EQ(p[3],-444);
        EXPECT_EQ(p[4],i<512?0:1000);
    }
    dg_audio::aec_record(true);ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data()));
    dg_audio::aec_record(false);EXPECT_EQ(dg_audio::aec_capture().channels,3u);
}

TEST_F(AecWrapper, MissingRawSlotsFailsInsteadOfFabricatingReferenceEvidence) {
    dg_audio::EchoCanceller aec;std::array<int16_t,320> mic{},ref{},out{};
    dg_audio::aec_record(true,true);
    EXPECT_FALSE(aec.process(mic.data(),ref.data(),320,out.data()));
    dg_audio::aec_record(false);
    EXPECT_EQ(dg_audio::aec_capture().frames,0u);EXPECT_EQ(dg_audio::aec_status().errors,1u);
}

TEST_F(AecWrapper, AllSlotCaptureKeepsThirtySecondBoundAndReportsTruncation) {
    chunk=320;dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic{},ref{},out{};std::array<uint32_t,640> raw{};
    dg_audio::aec_record(true,true);
    for(int i=0;i<1501;++i)ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),raw.data()));
    dg_audio::aec_record(false);
    auto captured=dg_audio::aec_capture();EXPECT_EQ(captured.frames,480000u);
    EXPECT_EQ(captured.channels,5u);EXPECT_TRUE(captured.truncated);
    EXPECT_EQ(captured.triples[5*479999+4],0);
}

TEST_F(AecWrapper, ShortProcessorResultFailsAndNeverReusesPayload) {
    chunk=320;dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic,ref,out;mic.fill(1234);ref.fill(0);
    short_result=true;
    for(int i=0;i<2;++i) {
        out.fill(999);EXPECT_FALSE(aec.process(mic.data(),ref.data(),320,out.data()));
        EXPECT_TRUE(std::all_of(out.begin(),out.end(),[](auto x){return x==0;}));
    }
    EXPECT_EQ(dg_audio::aec_status().errors,2u);
}

TEST_F(AecWrapper, SaturationAndCaptureCapacityRemainExplicit) {
    chunk=320;dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic,ref,out;mic.fill(-32768);ref.fill(32767);
    dg_audio::aec_record(true);
    for(int i=0;i<1501;++i)ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data()));
    dg_audio::aec_record(false);
    const auto capture=dg_audio::aec_capture();
    EXPECT_EQ(capture.frames,480000u);EXPECT_TRUE(capture.truncated);
    const auto status=dg_audio::aec_status();
    EXPECT_EQ(status.reference_clips,1501u*320);EXPECT_EQ(status.output_clips,1500u*320);
}

TEST_F(AecWrapper, InvalidQueriedFrameCleansUpAndDoesNotPublishActiveState) {
    chunk=0;EXPECT_THROW(dg_audio::EchoCanceller{},std::runtime_error);
    EXPECT_EQ(live,0);EXPECT_FALSE(dg_audio::aec_status().enabled);
    EXPECT_THROW(dg_audio::aec_record(true),std::runtime_error);
}

TEST_F(AecWrapper, OversizedInputIsRejectedBeforeReadingOrWritingPayload) {
    dg_audio::EchoCanceller aec;
    int16_t mic=1,ref=2,out=3;
    EXPECT_FALSE(aec.process(&mic,&ref,321,&out));EXPECT_EQ(out,3);
    EXPECT_EQ(dg_audio::aec_status().calls,0u);EXPECT_EQ(dg_audio::aec_status().errors,1u);
}

TEST_F(AecWrapper, ReturnedIncompletePublicStateAbortsWithoutCallingVendorDestructor) {
    for(omit=1;omit<=6;++omit) {
        EXPECT_EXIT({dg_audio::EchoCanceller aec;},testing::KilledBySignal(SIGABRT),"Fatal incomplete AFE engine");
    }
}

TEST_F(AecWrapper, ReturnedInvalidChannelMappingIsDestroyedBeforePublication) {
    invalid_pcm=true;
    EXPECT_THROW(dg_audio::EchoCanceller{},std::runtime_error);
    EXPECT_EQ(live,0);EXPECT_EQ(allocations,0);EXPECT_FALSE(dg_audio::aec_status().enabled);
}

TEST_F(AecWrapper, FactoryAllocationFailuresStopBeforeUncheckedUseOrPublication) {
#ifndef DG_AEC_SDK_ABORT_TEST
    GTEST_SKIP()<<"Configure DG_IDF_HEAP_FAILURE_SOURCE to exercise the installed SDK failure routine";
#else
    // Outer handle, mic/ref maps, nested engine, engine workspace, scratch.
    // Fatal OOM cannot unwind safely through proprietary code: the process
    // terminates (board abort/reboot), rather than returning a partial AFE.
    for(int stage=1;stage<=6;++stage) {
        EXPECT_DEATH({fail_stage=stage;dg_audio::EchoCanceller aec;},"SDK_ALLOCATION_FAILURE");
        EXPECT_FALSE(dg_audio::aec_status().enabled);
    }
#endif
}

TEST_F(AecWrapper, CaptureTimingRetainsBoundsGapsAndResets) {
    dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic{},ref{},out{};
    dg_audio::aec_record(true);
    ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{100,300,false}));
    ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{200,400,true}));
    dg_audio::aec_record(false);
    const auto capture=dg_audio::aec_capture();
    ASSERT_EQ(capture.timing_count,2u);
    EXPECT_EQ(capture.timing[0].start,0u);EXPECT_EQ(capture.timing[0].end,320u);
    EXPECT_EQ(capture.timing[0].time.lower_us,100);EXPECT_EQ(capture.timing[0].time.upper_us,300);
    EXPECT_FALSE(capture.timing[0].time.gap);
    EXPECT_EQ(capture.timing[1].start,320u);EXPECT_EQ(capture.timing[1].end,640u);
    EXPECT_TRUE(capture.timing[1].time.gap);
    dg_audio::aec_record(true);dg_audio::aec_record(false);
    EXPECT_EQ(dg_audio::aec_capture().timing_count,0u);
}

TEST_F(AecWrapper, ReferenceHealthCountsLowReferenceOnlyDuringLocalAudiblePlayback) {
    chunk=320;dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic{},ref{},out{};
    int64_t end=10000000;
    auto feed=[&](unsigned power,bool gap=false) {
        end+=200000;
        dg_audio::character_signals.speaker((end-95000)/1000,power);
        dg_audio::character_signals.speaker(end/1000,power);
        ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{end-95000,end,gap}));
    };
    for(int i=0;i<6;++i)feed(2000);
    auto s=dg_audio::aec_status();EXPECT_EQ(s.reference_low_samples,1920u);
    EXPECT_EQ(s.reference_low_windows,1u);EXPECT_EQ(s.reference_low_longest_samples,1920u);
    feed(0);ref.fill(2);feed(2000);ref.fill(0);feed(2000,true);
    EXPECT_EQ(dg_audio::aec_status().reference_low_samples,1920u);
    for(int i=0;i<5;++i)feed(2000);
    s=dg_audio::aec_status();EXPECT_EQ(s.reference_low_windows,2u);
    EXPECT_EQ(s.reference_low_longest_samples,1920u);
    EXPECT_EQ(s.duration_buckets[0],s.calls);
}

TEST_F(AecWrapper, ReferenceHealthRequiresBothBoundsAndScalesWithGain) {
    chunk=320;dg_audio::EchoCanceller aec;
    std::array<int16_t,320> mic{},ref{},out{};
    dg_audio::character_signals.speaker(20000,1000000);
    ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{19900000,20000000,false}));
    EXPECT_EQ(dg_audio::aec_status().reference_low_samples,0u);
    dg_audio::character_signals.speaker(19900,1000000);
    ref.fill(2);
    ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{19900000,20000000,false}));
    EXPECT_EQ(dg_audio::aec_status().reference_low_samples,0u);
    dg_audio::aec_reference_gain(30);
    ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{19900000,20000000,false}));
    EXPECT_EQ(dg_audio::aec_status().reference_low_samples,320u);
    EXPECT_EQ(dg_audio::aec_status().reference_gain_db,30u);
    ref.fill(40);
    ASSERT_TRUE(aec.process(mic.data(),ref.data(),320,out.data(),nullptr,{19900000,20000000,false}));
    EXPECT_EQ(dg_audio::aec_status().reference_low_samples,320u);
}
