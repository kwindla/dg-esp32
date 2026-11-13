#include <gtest/gtest.h>
#include "dg_audio/gate.h"
using namespace dg_audio;
TEST(Gate, InitiallyOpen) { Gate g; EXPECT_FALSE(g.closed(0)); }
TEST(Gate, TailBoundary) { Gate g; g.audible(100); EXPECT_TRUE(g.closed(300099)); EXPECT_FALSE(g.closed(300100)); }
TEST(Gate, EarlyStopCannotOverrideAudio) { Gate g; g.speaking(true,0); g.audible(100000); g.speaking(false,110000); EXPECT_TRUE(g.closed(399999)); EXPECT_FALSE(g.closed(400000)); }
// The speaking fallback is 2 s in the AEC build (unchanged, qualified) and 20 s in the half-duplex build (decisions
// 130/131). This file runs in both configurations (audio_tests: non-AEC; gate_aec_config_tests: CONFIG_DG_ECHOEAR_AEC=1).
#if CONFIG_DG_ECHOEAR_AEC
constexpr int64_t kFallback=2000000;
#else
constexpr int64_t kFallback=20000000;
#endif
TEST(Gate, MissedStopWatchdog) { Gate g; g.speaking(true,0); g.audible(100000); EXPECT_TRUE(g.closed(100000+kFallback-1)); EXPECT_FALSE(g.closed(100000+kFallback)); }
TEST(Gate, NoAudioWatchdog) { Gate g; g.speaking(true,100); EXPECT_TRUE(g.closed(100+kFallback-1)); EXPECT_FALSE(g.closed(100+kFallback)); }
TEST(Gate, ContinuousAudioRefreshesWatchdog) { Gate g; g.speaking(true,0); for(int i=0;i<100;++i) { g.audible(i*100000); EXPECT_TRUE(g.closed(i*100000+99999)); } EXPECT_TRUE(g.closed(9900000+kFallback-1)); EXPECT_FALSE(g.closed(9900000+kFallback)); }
TEST(Gate, FadingTailWithoutBotStopped) {
    // mute-02 A-4 shape: audible playback ends at 11.1 s, the reply's quiet tail continues, bot-stopped never arrives
    // within 2 s. Half-duplex build: still closed at 2 s and until the 20 s fallback. AEC build: the qualified 2 s.
    Gate g; g.speaking(true,0); g.audible(11100000);
    EXPECT_TRUE(g.closed(11100000+2000000-1));
#if CONFIG_DG_ECHOEAR_AEC
    EXPECT_FALSE(g.closed(11100000+2000000));
#else
    EXPECT_TRUE(g.closed(11100000+2000000)); EXPECT_TRUE(g.closed(13110000)); EXPECT_TRUE(g.closed(11100000+kFallback-1));
    EXPECT_FALSE(g.closed(11100000+kFallback));
#endif
}
TEST(Gate, BotStoppedOpensAfterTheAudibleHold) {
    Gate g; g.speaking(true,0); g.audible(5000000); g.speaking(false,5100000);
    EXPECT_TRUE(g.closed(5299999)); EXPECT_FALSE(g.closed(5300000));       // 300 ms after the last audible playback
    Gate h; h.speaking(true,0); h.audible(1000000); h.speaking(false,4000000);
    EXPECT_FALSE(h.closed(4000001));                                        // bot-stopped long after audio: opens now
}
TEST(Gate, StopOrDisconnectClearsSpeaking) {
    // The session stop hook calls Pipeline::speaking(false) (main/console.cpp) on disconnect/session end.
    Gate g; g.speaking(true,0); g.audible(100000);
    g.speaking(false,150000); EXPECT_FALSE(g.closed(400000));
    Gate h; h.speaking(true,0); h.reset(); EXPECT_FALSE(h.closed(1));
}
TEST(Gate, HysteresisAcrossSilentFrames) { Gate g; g.audible(0); EXPECT_TRUE(g.closed(200000)); g.audible(250000); EXPECT_TRUE(g.closed(500000)); EXPECT_FALSE(g.closed(550000)); }
TEST(Gate, PolicyOverridesAndReturnsToAuto) { Gate g; g.audible(0); g.set_policy(GatePolicy::Open); EXPECT_FALSE(g.closed(10)); g.set_policy(GatePolicy::Auto); EXPECT_TRUE(g.closed(10)); g.set_policy(GatePolicy::Closed); EXPECT_TRUE(g.closed(10000000)); g.set_policy(GatePolicy::Auto); EXPECT_FALSE(g.closed(10000000)); }
TEST(Gate, ExperimentalAecAllowsBargeInButHonorsExplicitMute) {
    Gate g;g.set_aec_active(true);g.speaking(true,0);g.audible(50000);
    EXPECT_FALSE(g.closed(50001));
    g.set_policy(GatePolicy::Closed);EXPECT_TRUE(g.closed(50002));
    g.set_policy(GatePolicy::Auto);EXPECT_FALSE(g.closed(50003));
    g.set_aec_active(false);EXPECT_TRUE(g.closed(50004));
    g.speaking(false,60000);EXPECT_TRUE(g.closed(60001));
    EXPECT_FALSE(g.closed(350000));
}

#include "dg_audio/aec_capture_clock.h"
TEST(Gate, LocalOnsetWithoutBotStartClosesBeforeAcceptedWrite) {
    Gate g;g.set_aec_active(true);g.prepare_write(60000);
    EXPECT_TRUE(g.capture_closed(0,0,true));
    g.finish_write(60000,80000);
    EXPECT_TRUE(g.capture_closed(500000,459999,true));
    EXPECT_FALSE(g.capture_closed(500000,460000,true));
    EXPECT_EQ(g.onsets(),1u);
}
TEST(Gate, ShortReplyStopAndLateRepeatedSpeakingCannotReleaseOrRestart) {
    Gate g;g.set_aec_active(true);g.finish_write(60000,80000);
    g.speaking(true,90000);g.speaking(true,100000);g.speaking(false,110000);
    EXPECT_TRUE(g.capture_closed(900000,459999,true));
    EXPECT_FALSE(g.capture_closed(900000,460000,true));
    g.speaking(true,1000000);g.audible(1100000);
    EXPECT_FALSE(g.capture_closed(1100000,1000000,true));
    EXPECT_EQ(g.onsets(),1u);
}
TEST(Gate, ContinuousAcceptedSpeechDoesNotReserveOrExtendExpiredGuard) {
    Gate g;g.set_aec_active(true);g.finish_write(0,20000);
    for(int64_t t=20000;t<2000000;t+=20000) {
        g.prepare_write(t);
        EXPECT_EQ(g.capture_closed(t,t,true),t<400000);
        g.finish_write(t,t+20000);
    }
    EXPECT_EQ(g.onsets(),1u);EXPECT_EQ(g.guard_until(),400000);
}
TEST(Gate, LocalSilenceGapRearmsAcrossSegmentsNotRemoteEvents) {
    Gate g;g.rearm_ms(500);g.set_aec_active(true);g.finish_write(0,20000);
    g.speaking(false,30000);g.speaking(true,450000);
    g.finish_write(519999,520000);EXPECT_EQ(g.onsets(),1u);
    g.prepare_write(1020000);EXPECT_TRUE(g.capture_closed(1000000,900000,true));
    g.finish_write(1020000,1040000);EXPECT_EQ(g.onsets(),2u);
    EXPECT_EQ(g.guard_until(),1420000);
}
TEST(Gate, ManualPolicyReconnectAndDisabledGuard) {
    Gate g;g.set_aec_active(true);g.finish_write(0,20000);
    g.set_policy(GatePolicy::Open);EXPECT_FALSE(g.closed(1));
    g.set_policy(GatePolicy::Closed);g.reset();EXPECT_TRUE(g.closed(1000000));
    g.set_policy(GatePolicy::Auto);EXPECT_FALSE(g.closed(1));
    g.finish_write(2,20002);EXPECT_EQ(g.onsets(),2u);
    g.guard_ms(0);EXPECT_FALSE(g.closed(3));
}
TEST(Gate, ZeroByteWriteRollsBackReservationWithoutRearmOrOnset) {
    Gate g;g.rearm_ms(500);g.set_aec_active(true);g.prepare_write(0);EXPECT_TRUE(g.closed(0));
    g.finish_write(-1,-1);EXPECT_FALSE(g.closed(1));EXPECT_EQ(g.onsets(),0u);
    g.finish_write(2,20002);g.prepare_write(600000);g.finish_write(-1,-1);
    EXPECT_FALSE(g.closed(600000));EXPECT_EQ(g.onsets(),1u);
}
TEST(Gate, DefaultRearmSkipsSentencePauseButGuardsNextReply) {
    Gate g;g.set_aec_active(true);EXPECT_EQ(g.rearm_ms(),2000u);
    g.finish_write(0,1000000);
    // A 1.6 s sentence pause, even with 120 ms reservation uncertainty.
    g.prepare_write(2720000);EXPECT_FALSE(g.closed(2600000));
    g.finish_write(2600000,3600000);EXPECT_EQ(g.onsets(),1u);
    g.prepare_write(5599999);EXPECT_FALSE(g.closed(5599999));
    g.finish_write(-1,-1); // no samples: do not advance last_end
    g.prepare_write(5600000);EXPECT_TRUE(g.closed(5600000));
    g.finish_write(5600000,5620000);EXPECT_EQ(g.onsets(),2u);
    EXPECT_EQ(g.guard_until(),6000000);
    EXPECT_TRUE(g.capture_closed(7000000,5999999,true));
    EXPECT_FALSE(g.capture_closed(7000000,6000000,true));
}
TEST(Gate, ConfigurableRearmBoundsAndResetPersistence) {
    Gate g;g.set_aec_active(true);
    for(unsigned ms:{500u,2000u,10000u}) {
        g.rearm_ms(ms);g.reset();EXPECT_EQ(g.rearm_ms(),ms);
        g.finish_write(0,20000);
        auto before=g.onsets();auto onset=20000+int64_t(ms)*1000;
        g.prepare_write(onset-1);EXPECT_FALSE(g.closed(onset));
        g.finish_write(-1,-1);
        g.prepare_write(onset);EXPECT_TRUE(g.closed(onset));
        g.finish_write(onset,onset+20000);EXPECT_EQ(g.onsets(),before+1);
    }
    for(unsigned bad:{0u,499u,10001u,~0u}) {
        EXPECT_THROW(g.rearm_ms(bad),std::invalid_argument);
        EXPECT_EQ(g.rearm_ms(),10000u);
    }
}
TEST(Gate, RearmCommandRejectsOverflowAndExtraArguments) {
    EXPECT_EQ(parse_guard_rearm_ms(" 500 "),500u);
    EXPECT_EQ(parse_guard_rearm_ms("2000"),2000u);
    EXPECT_EQ(parse_guard_rearm_ms("10000"),10000u);
    for(auto bad:{""," ","0","499","10001","-2000","+2000","2e3","2000.0",
                  "2000 x","2000 1","4294969296","999999999999999999999"})
        EXPECT_THROW(parse_guard_rearm_ms(bad),std::invalid_argument)<<bad;
}
TEST(Gate, ExpiryFollowsActualDelayedSamplesIncludingPartialReadsAndBacklog) {
    Gate g;g.set_aec_active(true);g.finish_write(0,20000);
    AecCaptureClock clock;
    // Wall clock can be arbitrarily late while buffered source data is old.
    for(int64_t sample=0;sample<10240;sample+=160) {
        clock.read(160,sample*1000000/16000,false);
        bool expected=sample<1024 || (sample-1024)/160*10000<400000;
        EXPECT_EQ(g.capture_closed(9000000,clock.lower_us(),clock.valid()),expected);
    }
    clock.read(0,999999,true);EXPECT_TRUE(g.capture_closed(9000000,clock.lower_us(),clock.valid()));
}
TEST(Gate, InvalidDelayedCaptureTimingFailsClosedUntilKnownSamples) {
    Gate g;g.set_aec_active(true);g.finish_write(0,20000);AecCaptureClock clock;
    for(int i=0;i<10;++i)clock.read(320,1000000,true);
    EXPECT_TRUE(g.capture_closed(2000000,clock.lower_us(),clock.valid()));
    for(int i=0;i<5;++i)clock.read(320,1000000,false);
    EXPECT_FALSE(g.capture_closed(2000000,clock.lower_us(),clock.valid()));
}

TEST(Gate, GuardCommandUsesStrictWholeArgumentParser) {
    EXPECT_EQ(parse_guard_ms(" 0 "),0u);EXPECT_EQ(parse_guard_ms("400"),400u);
    EXPECT_EQ(parse_guard_ms("1000"),1000u);
    for(auto bad:{""," ","-1","+400","1001","400.0","400 x","4294967696"})
        EXPECT_THROW(parse_guard_ms(bad),std::invalid_argument)<<bad;
}

#include "dg_audio/rx_capture_clock.h"
TEST(RxCaptureClock, TightensSteadyReadsWithoutExtrapolatingSampleClock) {
    RxCaptureClock clock(240,4);
    // Model continuous reads of 320 samples, completed in 15 ms DMA blocks.
    // Counter reads precede each blocking read; true sample time is known here.
    int64_t now=0;unsigned consumed=0;
    for(unsigned i=0;i<40;++i) {
        auto lower=clock.begin(now/15000,0,now,now-95000);
        EXPECT_LE(lower,int64_t(consumed)*1000000/16000);
        // Worst phase: observation before the preceding 15 ms boundary
        // is two 20 ms reads old.
        if(i>5) EXPECT_GE(lower,int64_t(consumed)*1000000/16000-40000);
        consumed+=320;now=((consumed+239)/240)*15000;
        clock.end(now/15000,0,320);
    }
}
TEST(RxCaptureClock, StartupOverflowPartialReadAndDelayedConsumerStayConservative) {
    RxCaptureClock clock(240,4);
    // First 17 buffers discarded before reader creation; first retained sample
    // starts at completion 17. No observation yet: preserve old broad bound.
    EXPECT_EQ(clock.begin(20,17,300000,205000),205000);
    clock.end(20,17,80);
    EXPECT_LE(clock.begin(20,17,301000,205000),255000);
    clock.end(20,17,240);
    // Driver skips a partial current buffer without an overflow notification.
    // Our cursor undercounts discarded samples, making the bound older only.
    auto bound=clock.begin(21,17,315000,206000);
    EXPECT_LE(bound,285000);clock.end(21,17,320);
    // Long scheduling pause, queue overflows; next retained source starts >=60.
    bound=clock.begin(64,60,960000,220000);
    EXPECT_LE(bound,900000);clock.end(64,60,320);
}
TEST(RxCaptureClock, CounterWrapAndZeroReadsDoNotInventFutureSampleTimes) {
    RxCaptureClock clock(240,4);
    const uint32_t base=0xfffffffeu;
    const int64_t t=int64_t(base)*15000;
    auto lower=clock.begin(base,base-3,t,t-95000);
    EXPECT_LE(lower,t-45000);clock.end(base,base-3,0);
    lower=clock.begin(1,0,t+45000,t-95000);
    EXPECT_LE(lower,t+30000);clock.end(1,0,160);
}
