#include "dg_audio/pipeline.h"
#include "dg_audio/speaker.h"
#include "dg_media_token.h"
#include "fake_i2s.h"
#include <gtest/gtest.h>
#include <thread>
#include <chrono>
#include <algorithm>
struct Source:dg::MediaSource {
    std::atomic<uint64_t> generation{1};std::atomic<bool> connected{true};
    bool valid(uint64_t g) const override {return connected && generation==g;}
};
static bool wait(const std::function<bool()>& done) {
    for(unsigned i=0;i<1000;++i) {if(done())return true;std::this_thread::sleep_for(std::chrono::milliseconds(1));}return false;
}
static auto reader(std::shared_ptr<Source> source,std::atomic<unsigned>& reads,bool disconnect=false) {
    return [source,&reads,disconnect](int16_t* pcm,size_t n,dg::MediaToken& token) {
        unsigned call=++reads;std::fill_n(pcm,n,int16_t(100+call));token={source,source->generation};
        if(disconnect)source->connected=false;
        return int(n);
    };
}
#ifdef DG_BASELINE
// The old callback cannot carry the validity token; the same stimulus exposes
// what the original production loop submits after source invalidation.
template<class Read> auto adapt(Read read) {
    return [read](int16_t* pcm,size_t n) {dg::MediaToken token;return read(pcm,n,token);};
}
#else
template<class Read> auto adapt(Read read) {return read;}
#endif
TEST(PipelineI2s, ShortAndFailedWritesSubmitEachPcmSampleExactlyOnce) {
    fake::reset(false);fake::maximum_write=80;fake::failures=1;
    auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads)),[](const int16_t*,size_t){},[&]{return reads<1;});
    ASSERT_TRUE(wait([]{std::lock_guard<std::mutex> lock(fake::mutex);return fake::submitted.size()>=320;}));}
    std::lock_guard<std::mutex> lock(fake::mutex);
    ASSERT_GE(fake::submitted.size(),320u);
    EXPECT_TRUE(std::all_of(fake::submitted.begin(),fake::submitted.begin()+320,[](auto s){return s==101;}));
    EXPECT_TRUE(std::all_of(fake::submitted.begin()+320,fake::submitted.end(),[](auto s){return s==0;}));
    ASSERT_GE(fake::chunks.size(),5u);
    EXPECT_EQ((std::vector<unsigned>(fake::chunks.begin(),fake::chunks.begin()+5)),(std::vector<unsigned>{0,80,80,80,80}));
}
TEST(PipelineI2s, GenerationChangeBetweenPartialWritesDiscardsOldSuffix) {
    fake::reset(false);fake::maximum_write=80;auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    fake::on_write=[&](unsigned call){if(call==1)++source->generation;};
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads)),[](const int16_t*,size_t){},[&]{return reads<1;});
    ASSERT_TRUE(wait([&]{return reads>=1 && fake::now>=150000;}));}
    std::lock_guard<std::mutex> lock(fake::mutex);
    EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),101),80);
}
TEST(PipelineI2s, DisconnectAfterReadSubmitsNoRetiredPcm) {
    fake::reset(false);auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads,true)),[](const int16_t*,size_t){},[&]{return reads<1;});
    ASSERT_TRUE(wait([&]{return reads>=1 && fake::now>=150000;}));}
    std::lock_guard<std::mutex> lock(fake::mutex);EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),101),0);
}
TEST(PipelineI2s, ReconnectWithSameGenerationStillUsesNewSessionIdentity) {
    fake::reset(false);fake::maximum_write=80;auto old=std::make_shared<Source>(),fresh=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    fake::on_write=[&](unsigned call){if(call==1)old->connected=false;};
    auto read=[&](int16_t* pcm,size_t n,dg::MediaToken& token) {
        auto count=++reads;auto source=count==1?old:fresh;token={source,1};std::fill_n(pcm,n,count==1?111:222);return int(n);
    };
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(read),[](const int16_t*,size_t){},[&]{return reads<2;});
    ASSERT_TRUE(wait([&]{return reads>=2 && fake::now>=180000;}));}
    std::lock_guard<std::mutex> lock(fake::mutex);
    EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),111),80);
    EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),222),320);
}
TEST(PipelineI2s, CallbackBeforeWriteReturnExpiresSuffixAndRefillsExactZeros) {
    fake::reset(false);fake::maximum_write=80;auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    fake::on_write=[&](unsigned call){if(call==1)fake::advance(200);else if(call==2)fake::advance(60);};
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads)),[](const int16_t*,size_t){},[&]{return reads<3;});
    ASSERT_TRUE(wait([&]{return reads>=3 && fake::now>=400000;}));}
    std::lock_guard<std::mutex> lock(fake::mutex);
    EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),101),80);
    EXPECT_GT(std::count(fake::submitted.begin(),fake::submitted.end(),0),0);
    EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),103),320);
}
TEST(PipelineI2s, DeviceEpochChangeInvalidatesPreviouslyRenderedPcm) {
    fake::reset(false);dg_audio::Speaker speaker;speaker.start();fake::advance(30);
    auto p=speaker.opportunity(fake::now);ASSERT_TRUE(p.ready);ASSERT_TRUE(speaker.render(p.tick));
    int16_t pcm[320];std::fill_n(pcm,320,123);fake::maximum_write=80;
    EXPECT_EQ(speaker.write(pcm,320,5),80u);speaker.idle();speaker.start();
    EXPECT_FALSE(speaker.valid(p.tick));fake::advance(30);auto next=speaker.opportunity(fake::now);
    ASSERT_TRUE(next.ready);EXPECT_NE(next.tick.device_epoch,p.tick.device_epoch);
    EXPECT_TRUE(speaker.render(next.tick));EXPECT_EQ(speaker.write(pcm,320,5),80u);
    std::lock_guard<std::mutex> lock(fake::mutex);EXPECT_EQ(fake::submitted.size(),160u);
}

TEST(PipelineI2s, InvalidationReleasesRetainedMediaSourceBeforePark) {
    fake::reset(false);auto source=std::make_shared<Source>();std::weak_ptr<Source> weak=source;
    std::atomic<unsigned> reads{0};
    auto read=[&](int16_t* pcm,size_t n,dg::MediaToken& token) {
        auto call=++reads;if(call!=1)return 0;
        token={source,1};std::fill_n(pcm,n,101);return int(n);
    };
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},read,[](const int16_t*,size_t){});
     ASSERT_TRUE(wait([&]{return reads>=2;}));source->connected=false;source.reset();
     ASSERT_TRUE(wait([&]{return weak.expired();}));pipeline.benchmark_idle(true);
     EXPECT_TRUE(weak.expired());pipeline.benchmark_idle(false);}
}
TEST(PipelineI2s, AttenuationKeepsFullScalePcmBoundedAndReportsAcceptedPeak) {
    fake::reset(false);dg_audio::Speaker speaker;
    speaker.volume(16423); // -6 dB, rounded Q15
    int16_t pcm[2]={32767,-32768};
    EXPECT_EQ(speaker.write(pcm,2,5),2u);
    EXPECT_EQ(speaker.peak(),16423u);
    EXPECT_EQ(fake::submitted,(std::vector<int16_t>{16422,-16423}));
    speaker.volume(32768);EXPECT_EQ(speaker.peak(),0u);
    EXPECT_EQ(speaker.write(pcm,2,5),2u);EXPECT_EQ(speaker.peak(),32768u);
}

TEST(PipelineI2s, VolumeResetDuringWriteDoesNotResurrectOldPeak) {
    fake::reset(false);dg_audio::Speaker speaker;
    int16_t loud[1]={30000},quiet[1]={100};
    ASSERT_EQ(speaker.write(loud,1,5),1u);ASSERT_EQ(speaker.peak(),30000u);
    fake::on_write=[&](unsigned){speaker.volume(16384);};
    EXPECT_EQ(speaker.write(quiet,1,5),1u);
    // The already-scaled block straddles the reset and contributes only itself.
    EXPECT_EQ(speaker.peak(),100u);
    EXPECT_EQ(speaker.write(quiet,1,5),1u);EXPECT_EQ(speaker.peak(),50u);
}

TEST(PipelineI2s, DirectToneCannotParkAnActiveMediaConsumer) {
    fake::reset(false);
    dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},
        [](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){},[]{return true;});
    bool onset=false;
    EXPECT_THROW(pipeline.tone(1500,1200,20,[&]{onset=true;}),std::runtime_error);
    EXPECT_FALSE(onset);
}


extern "C" unsigned dg_audio_mic_in_turn_clips();
extern "C" unsigned dg_audio_mic_gate_open_clips();
extern "C" unsigned dg_audio_mic_gate_closed_clips();
TEST(PipelineI2s, ClipCountersFollowActualMuteDecisionAndExcludeSettling) {
    for(bool muted:{false,true}) {
        fake::reset(false);fake::mic_samples=true;
        const auto total=dg_audio_mic_in_turn_clips();
        const auto opened=dg_audio_mic_gate_open_clips();
        const auto closed=dg_audio_mic_gate_closed_clips();
        std::atomic<unsigned> blocks{0},nonzero{0};
        {
            dg_audio::Pipeline pipeline([&](int16_t* pcm,size_t n) {
                if(std::any_of(pcm,pcm+n,[](auto s){return s!=0;}))++nonzero;
                ++blocks;return int(n);
            },[](int16_t*,size_t,dg::MediaToken&){return 0;},[](const int16_t*,size_t){});
            pipeline.set_gate_policy(muted?dg_audio::GatePolicy::Closed:dg_audio::GatePolicy::Open);
            pipeline.enable_capture(true);
            ASSERT_TRUE(wait([&]{return blocks>=12;}));
        }
        const auto delta=dg_audio_mic_in_turn_clips()-total;
        const auto open_delta=dg_audio_mic_gate_open_clips()-opened;
        const auto closed_delta=dg_audio_mic_gate_closed_clips()-closed;
        EXPECT_GT(delta,0u);EXPECT_EQ(delta,open_delta+closed_delta);
        EXPECT_EQ(muted?open_delta:closed_delta,0u);
        EXPECT_EQ(muted?closed_delta:open_delta,delta);
        EXPECT_LT(delta,blocks*3); // First100ms is excluded, as before.
        if(muted)EXPECT_EQ(nonzero,0u);else EXPECT_GT(nonzero,0u);
    }
}

TEST(SpeakerOnset, PostVolumeAcceptedPrefixAndFailedWrites) {
    fake::reset(false);
    dg_audio::Speaker speaker;
    std::vector<std::tuple<bool,int64_t,int64_t>> events;
    speaker.onset_observer=[&](bool pending,int64_t first,int64_t end) {events.emplace_back(pending,first,end);};
    int16_t pcm[320];std::fill_n(pcm,320,1000);
    speaker.volume(0);EXPECT_EQ(speaker.write(pcm,320,0),320u);EXPECT_TRUE(events.empty());
    speaker.volume(32768);fake::failures=1;EXPECT_EQ(speaker.write(pcm,320,0),0u);
    ASSERT_EQ(events.size(),2u);EXPECT_TRUE(std::get<0>(events[0]));EXPECT_EQ(std::get<1>(events[1]),-1);
    events.clear();fake::maximum_write=160;std::fill_n(pcm,160,0);
    EXPECT_EQ(speaker.write(pcm,320,0),160u);ASSERT_EQ(events.size(),2u);
    EXPECT_EQ(std::get<1>(events[1]),-1); // nonsilent suffix was not accepted
    events.clear();std::fill_n(pcm,160,1000);
    EXPECT_EQ(speaker.write(pcm,320,0),160u);ASSERT_EQ(events.size(),2u);
    EXPECT_GE(std::get<1>(events[1]),0);EXPECT_EQ(std::get<2>(events[1])-std::get<1>(events[1]),10000);
}

TEST(SpeakerOnset, ContinuousSpeechBlockedWriteDoesNotReserveAnotherClosure) {
    for(unsigned rearm_ms:{500u,2000u}) {
    SCOPED_TRACE(rearm_ms);
    fake::reset(false);
    dg_audio::Gate gate;gate.set_aec_active(true);gate.rearm_ms(rearm_ms);
    dg_audio::Speaker speaker;
    speaker.onset_observer=[&](bool pending,int64_t first,int64_t end) {
        if(pending)gate.prepare_write(first);else gate.finish_write(first,end);
    };
    int16_t pcm[320];std::fill_n(pcm,320,1000);
    // Real Speaker queue/completion arithmetic, beyond the initial guard.
    for(unsigned i=0;i<50;++i) {
        ASSERT_EQ(speaker.write(pcm,320,100),320u);fake::advance(20);
    }
    ASSERT_EQ(gate.onsets(),1u);ASSERT_FALSE(gate.closed(fake::now));
    const auto deadline=gate.guard_until();
    unsigned blocked_calls=0;
    fake::on_write=[&](unsigned) {
        ++blocked_calls;
        EXPECT_FALSE(gate.closed(fake::now)); // reservation is observable inside I2S
        fake::advance(100); // maximum production write timeout, with DMA progress
        EXPECT_FALSE(gate.closed(fake::now));
    };
    EXPECT_EQ(speaker.write(pcm,320,100),320u);
    fake::maximum_write=160;EXPECT_EQ(speaker.write(pcm,320,100),160u);
    fake::failures=1;EXPECT_EQ(speaker.write(pcm,320,100),0u);
    EXPECT_EQ(blocked_calls,3u);EXPECT_EQ(gate.onsets(),1u);
    EXPECT_EQ(gate.guard_until(),deadline);EXPECT_FALSE(gate.closed(fake::now));
    }
}

extern "C" unsigned dg_audio_i2s_underflows();
extern "C" unsigned dg_audio_i2s_write_timeouts();
extern "C" unsigned dg_audio_i2s_write_errors();
TEST(PipelineI2s, RingCancelMuteIdleReanchorHasNoTimeoutOrExpiredInterval) {
    for(unsigned idle_ms:{20u,50u,70u,80u,90u,200u}) {
        SCOPED_TRACE(idle_ms);
        fake::reset();
        auto source=std::make_shared<Source>();
        std::atomic<bool> cancelled{false},idle{false};
        std::atomic<unsigned> before{0},after{0};
        auto errors=dg_audio_i2s_write_errors(),timeouts=dg_audio_i2s_write_timeouts();
        auto read=[&](int16_t* pcm,size_t n,dg::MediaToken& token) {
            auto now=fake::now.load();
            token={source,source->generation};
            // Cancel after the read acquired its lease, before the write check.
            if(now>=500000 && !cancelled.exchange(true))++source->generation;
            const auto tick=dg::render_tick;
            EXPECT_EQ(tick.submitted_samples-tick.completed_samples,
                      640-tick.completed_samples%320)<<now;
            if(now>=650000 && !idle.exchange(true))return 0; // mute expired before RTP re-anchor
            bool muted=now>=500000 && now<650000;
            if(now<500000)++before;
            if(now>=650000+idle_ms*1000)++after;
            std::fill_n(pcm,n,muted?0:123);return int(n);
        };
        auto ready=[&] {return !idle || fake::now>=650000+idle_ms*1000;};
        {
            dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},read,[](const int16_t*,size_t){},ready);
            ASSERT_TRUE(wait([&]{return fake::now>=3500000;}));
            EXPECT_EQ(dg_audio_i2s_underflows(),0u);
            EXPECT_EQ(dg_audio_i2s_write_errors(),errors);
            EXPECT_EQ(dg_audio_i2s_write_timeouts(),timeouts);
        }
        EXPECT_TRUE(cancelled);EXPECT_TRUE(idle);EXPECT_GT(before,10u);EXPECT_GT(after,100u);
    }
}
TEST(PipelineI2s, RingTimesOutWhenNoDescriptorIsFreeAndIdleDoesNotInventEpochs) {
    fake::reset();dg_audio::Speaker speaker;
    speaker.start();auto epoch=speaker.clock().epoch();
    int16_t pcm[320]{};
    EXPECT_EQ(speaker.write(pcm,320,5),0u);EXPECT_EQ(speaker.timeouts(),1u);
    fake::advance(25); // two real EOFs since enable
    auto p=speaker.opportunity(fake::now);ASSERT_TRUE(p.ready);speaker.render(p.tick);
    EXPECT_EQ(speaker.write(pcm,320,5),320u);
    speaker.idle();EXPECT_EQ(speaker.clock().epoch(),epoch+1);
    for(unsigned i=0;i<100;++i)speaker.idle();
    EXPECT_EQ(speaker.clock().epoch(),epoch+1);
}

TEST(PipelineI2s, DrainTargetIsLatchedWhileIdleTicksContinueWritingSilence) {
    fake::reset();dg_audio::Speaker speaker;speaker.start();
    fake::advance(30);auto p=speaker.opportunity(fake::now);
    ASSERT_TRUE(p.ready);speaker.render(p.tick);int16_t zeros[320]{};
    ASSERT_EQ(speaker.write(zeros,320,5),320u);speaker.begin_drain();
    const auto target=speaker.clock().submitted();
    while(speaker.clock().completed()<target) {
        fake::advance(5);auto next=speaker.opportunity(fake::now);
        if(next.ready) {speaker.render(next.tick);ASSERT_EQ(speaker.write(zeros,320,5),320u);}
        speaker.begin_drain();
    }
    EXPECT_TRUE(speaker.drained());
    EXPECT_GT(speaker.clock().submitted(),target); // later zeros did not move target
}

#ifndef DG_BASELINE
#include <cstdarg>
#include <cstring>
#include <future>
#include "driver/i2s_std.h"
// The pipeline reports through this weak hook; count the stall reports.
static std::atomic<unsigned> stall_reports{0};
extern "C" void dg_deferred_log(const char* format,...) {
    if(!std::strncmp(format,"SPEAKER_STALL",13))++stall_reports;
}
// Sol C-6: with a driver that accepts nothing, the write loops used to retry
// for ever under speaker_mutex, so no speaker control could run.
TEST(PipelineI2s, DriverThatAcceptsNothingGivesUpTheIntervalAndFreesTheControls) {
    for(bool error:{true,false}) {
        fake::reset(false);stall_reports=0;
        if(error)fake::failures=1u<<30;  // Every write fails at once.
        else fake::maximum_write=0;       // Every write returns having taken nothing.
        auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
        {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads)),[](const int16_t*,size_t){},[&]{return reads<1;});
        ASSERT_TRUE(wait([&]{return reads>=1;}));
        // The control needs the mutex the write loops hold.
        auto control=std::async(std::launch::async,[&]{pipeline.playback_volume(-12);});
        ASSERT_EQ(control.wait_for(std::chrono::seconds(5)),std::future_status::ready) << "error=" << error;
        control.get();
        ASSERT_TRUE(wait([&]{return stall_reports>=1;})) << "error=" << error;
        }  // The destructor returns although the driver never recovered.
        std::lock_guard<std::mutex> lock(fake::mutex);
        EXPECT_TRUE(fake::submitted.empty()) << "error=" << error;  // Nothing was replayed or invented.
        EXPECT_EQ(reads,1u) << "error=" << error;  // One media decision for the interval.
    }
}
TEST(PipelineI2s, DriverThatRecoversBeforeTheStallLimitStillRetiresItsIntervalInFull) {
    // 60 failed writes take about 120 ms: the media interval expires, as it
    // always did, and its silence is written once the driver takes data again.
    fake::reset(false);stall_reports=0;fake::failures=60;fake::maximum_write=80;
    auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads)),[](const int16_t*,size_t){},[&]{return reads<1;});
    ASSERT_TRUE(wait([]{std::lock_guard<std::mutex> lock(fake::mutex);return fake::submitted.size()>=320;}));}
    std::lock_guard<std::mutex> lock(fake::mutex);
    EXPECT_TRUE(std::all_of(fake::submitted.begin(),fake::submitted.begin()+320,[](auto s){return s==0 || s==101;}));
    EXPECT_LE(std::count(fake::submitted.begin(),fake::submitted.end(),101),320);  // No sample twice.
    EXPECT_EQ(stall_reports,0u);
    EXPECT_EQ(reads,1u);
}
// Sol WP2A A1: the idle tail retries too, and nothing but the stall limit can
// end it once the DMA stops reporting completions. A rendered tick stays valid
// until a completion moves the clock, so a failing writer whose fake clock
// keeps advancing does not reach this.
namespace fake {extern i2s_chan_handle_t channel;}
TEST(PipelineI2s, IdleTailWithStoppedDmaGivesUpTheIntervalAndFreesTheControls) {
    fake::reset(false);stall_reports=0;
    std::atomic<bool> stopped{false};
    fake::on_write=[&](unsigned call) {
        std::lock_guard<std::mutex> lock(fake::mutex);
        if(call==1)fake::failures=1u<<30;  // The PCM interval was accepted in full; every later write fails.
        if(call==2) {i2s_channel_disable(fake::channel);stopped=true;}  // The first idle write: no completion from here on.
    };
    auto source=std::make_shared<Source>();std::atomic<unsigned> reads{0};
    {dg_audio::Pipeline pipeline([](int16_t*,size_t){return 0;},adapt(reader(source,reads)),[](const int16_t*,size_t){},[&]{return reads<1;});
    ASSERT_TRUE(wait([&]{return stopped.load();}));
    auto control=std::async(std::launch::async,[&]{pipeline.playback_volume(-12);});
    const auto state=control.wait_for(std::chrono::seconds(5));
    if(state!=std::future_status::ready) {
        // Let the control and the destructor finish, so the failure is reported and not a hang.
        {std::lock_guard<std::mutex> lock(fake::mutex);fake::failures=0;i2s_channel_enable(fake::channel);}
        fake::advance(1000);
    }
    ASSERT_EQ(state,std::future_status::ready) << "the idle tail kept speaker_mutex with no DMA progress";
    control.get();
    ASSERT_TRUE(wait([&]{return stall_reports>=1;}));
    }  // The destructor returns although the driver never recovered.
    std::lock_guard<std::mutex> lock(fake::mutex);
    EXPECT_EQ(reads,1u);  // One media decision; the idle tail read nothing.
    EXPECT_EQ(fake::submitted.size(),320u);  // The accepted interval, once, and nothing after it.
    EXPECT_EQ(std::count(fake::submitted.begin(),fake::submitted.end(),101),320);
}
#endif
