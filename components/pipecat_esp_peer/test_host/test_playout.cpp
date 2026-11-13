#include "playout.h"
#include "playout_run_selftest.h"
#include <gtest/gtest.h>
#include <thread>
#include <cmath>
using namespace pipecat::esp_peer;
using K=Playout::Kind;
// Fixed policy constants: the reserve, the wall-clock prefill deadline after
// the first accepted ingress, and the contiguous backlog cap.
constexpr unsigned R=Playout::target_frames, B=Playout::target_frames+3;
constexpr int64_t T0=int64_t(Playout::target_frames)*20000;
struct Buffer {
    Playout::Storage store{};
    Playout p{store};
    uint8_t payload[2]{0x48,42};
    Buffer() {p.authorize(7,1);}
    RtpAudioView packet(int64_t seq,int64_t ts) {return {7,uint32_t(ts),uint16_t(seq),false,payload,2};}
    bool put(int64_t seq,int64_t ts,int64_t now=0) {return p.insert(packet(seq,ts),now);}
    Playout::Decision tick(unsigned i,int64_t now,bool prior=true,unsigned skip=0,uint64_t epoch=1) {
        dg::RenderTick t;t.valid=true;t.index=i;t.deadline_us=now;t.device_epoch=epoch;t.silence_blocks=skip;
        return p.pull(t,prior);
    }
    void finish(const Playout::Decision& d,bool success=true) {p.finish(d,success);}
    Playout::Snapshot stats() {return p.snapshot(0);}
};
TEST(Playout, FixedIngressDeadlineOnePacketAndDuplicateTicks) {
    Buffer b;ASSERT_TRUE(b.put(10,123456,1000));
    for(unsigned i=0;i<R+1;++i) {auto d=b.tick(i,i*20000);EXPECT_EQ(d.kind,K::Prefill);b.finish(d);}
    auto d=b.tick(R+1,(R+1)*20000);ASSERT_EQ(d.kind,K::Decode);EXPECT_EQ(d.payload[1],42);b.finish(d);
    auto watermark=b.stats().watermark;
    EXPECT_EQ(b.tick(R+1,(R+1)*20000).kind,K::Wait);EXPECT_EQ(b.tick(R,(R+1)*20000).kind,K::Wait);
    EXPECT_EQ(b.stats().watermark,watermark);EXPECT_EQ(b.stats().counters.prefills,1u);
}
TEST(Playout, SingleAndBurstsOneThroughTwelveExactlyOnce) {
    for(unsigned n=1;n<=12;++n) {
        Buffer b;ASSERT_TRUE(b.put(100,120960));
        auto d=b.tick(0,T0);ASSERT_EQ(d.kind,K::Decode);b.finish(d);
        ASSERT_TRUE(b.put(101+n,120960+(n+1)*960,T0+10000));
        for(unsigned i=1;i<=n;++i) {
            d=b.tick(i,T0+i*20000);EXPECT_EQ(d.kind,i<=5?K::Plc:K::Zero);b.finish(d);
        }
        d=b.tick(n+1,T0+(n+1)*20000);EXPECT_EQ(d.kind,K::Decode);b.finish(d);
        auto c=b.stats().counters;EXPECT_EQ(c.missing_slots,n);EXPECT_EQ(c.plc_calls,std::min(n,5u));
        EXPECT_EQ(c.post_plc_zeros,n>5?n-5:0);EXPECT_EQ(c.starvation,0u);
        EXPECT_EQ(c.rejected[unsigned(IngressError::Late)],0u);EXPECT_EQ(c.discontinuity,0u);
        EXPECT_EQ(c.latency_trim,0u);EXPECT_EQ(c.ts_gap,0u);
        for(unsigned i=0;i<6;++i)EXPECT_EQ(c.missing_run_ended[i],i==std::min(n,6u)-1?1u:0u);
    }
}
TEST(Playout, PlaybackSelftestRunHistogram) {
    EXPECT_EQ(playout_run_selftest(),7u);
}
TEST(Playout, OpenRunEndsOnceAtDeviceRetirementAndDisconnect) {
    Buffer b;ASSERT_TRUE(b.put(0,0));b.finish(b.tick(0,T0));
    b.finish(b.tick(1,T0+20000));b.finish(b.tick(2,T0+40000));
    EXPECT_EQ(b.stats().counters.missing_run_ended[1],0u);
    b.finish(b.tick(4,T0+80000,true,1)); // Device silence retires the open two-run.
    EXPECT_EQ(b.stats().counters.missing_run_ended[1],1u);
    auto c=b.stats().counters;
    b.p.disconnect();
    auto ended=b.stats().counters.missing_run_ended;
    EXPECT_EQ(ended[0],c.missing_run_ended[0]+1);
    b.p.disconnect();EXPECT_EQ(b.stats().counters.missing_run_ended,ended);
}
TEST(Playout, ReorderAndStartupAnchorNeverRestartPrefill) {
    Buffer b;ASSERT_TRUE(b.put(102,1920,0));ASSERT_TRUE(b.put(100,0,20000));
    ASSERT_TRUE(b.put(104,3840,40000));ASSERT_TRUE(b.put(101,960,60000));ASSERT_TRUE(b.put(103,2880,80000));
    for(unsigned i=0;i<5;++i) {auto d=b.tick(i,T0+i*20000);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,i*960);b.finish(d);}
    EXPECT_EQ(b.stats().counters.plc_calls,0u);EXPECT_EQ(b.stats().counters.prefills,1u);
}
TEST(Playout, TimestampHolesOneSixAndTwelveAreBoundedAndNotLossCounts) {
    for(unsigned n:{1u,6u,12u}) {
        Buffer b;b.put(10,123456);auto d=b.tick(0,T0);b.finish(d);
        ASSERT_TRUE(b.put(11,123456+(n+1)*960,T0+10000));
        EXPECT_EQ(b.stats().counters.ts_gap,1u);
        EXPECT_FALSE(b.put(11,123456+(n+1)*960,T0+10001));
        for(unsigned i=1;i<=n;++i) {d=b.tick(i,T0+i*20000);EXPECT_EQ(d.kind,i<=5?K::Plc:K::Zero);b.finish(d);}
        d=b.tick(n+1,T0+(n+1)*20000);EXPECT_EQ(d.kind,K::Decode);b.finish(d);
        EXPECT_EQ(b.stats().counters.ts_gap,1u);EXPECT_EQ(b.stats().counters.discontinuity,0u);
        EXPECT_EQ(b.stats().counters.missing_slots,n);
    }
}
TEST(Playout, ReorderedTimestampGapAdjacencyCountedOnce) {
    Buffer b;b.put(10,0);b.put(12,3840);b.put(11,960);
    EXPECT_EQ(b.stats().counters.ts_gap,1u);
    EXPECT_FALSE(b.put(11,960));EXPECT_EQ(b.stats().counters.ts_gap,1u);
}
TEST(Playout, HugeJumpNeedsAdvancingCoherentConfirmerAndFixedPrefill) {
    Buffer b;b.put(10,123456);auto d=b.tick(0,T0);b.finish(d);
    ASSERT_TRUE(b.put(11,123456+960*1000000ll,T0+10000));
    EXPECT_EQ(b.stats().future_span,0u);EXPECT_EQ(b.stats().counters.discontinuity,0u);
    EXPECT_EQ(b.stats().watermark,124416);
    ASSERT_TRUE(b.put(12,123456+960*1000001ll,T0+20000));
    EXPECT_EQ(b.stats().counters.discontinuity,1u);
    EXPECT_EQ(b.tick(1,T0+20000).kind,K::Prefill);
    EXPECT_EQ(b.tick(2,(T0+10000)+T0+10000).kind,K::Decode); // candidate ingress + fixed prefill
}
TEST(Playout, MissingConfirmerCannotPreventStarvationOrMoveWatermark) {
    Buffer b;b.put(10,0);auto d=b.tick(0,T0);b.finish(d);
    b.put(11,960*1000000ll,T0+10000);
    for(unsigned i=1;i<=30;++i) {d=b.tick(i,T0+i*20000);b.finish(d);}
    EXPECT_EQ(b.stats().counters.starvation,1u);EXPECT_EQ(b.stats().counters.discontinuity,0u);
    auto stamp=b.stats().watermark;b.tick(31,T0+620000);EXPECT_EQ(b.stats().watermark,stamp);
}
TEST(Playout, OneCorruptJumpAndFalseConfirmLeaveReferenceIntact) {
    Buffer b;b.put(10,0);b.put(11,960*1000000ll,20000);ASSERT_TRUE(b.put(11,960,40000));
    ASSERT_TRUE(b.put(12,1920,60000));EXPECT_EQ(b.stats().counters.discontinuity,0u);
    for(unsigned i=0;i<3;++i) {auto d=b.tick(i,T0+i*20000);EXPECT_EQ(d.kind,K::Decode);b.finish(d);}
}
TEST(Playout, PersistentOffGridAndBackwardTimestampWithAdvancingSequenceRebase) {
    for(int64_t stamp:{1921ll,-960ll}) {
        Buffer b;b.put(10,0);auto d=b.tick(0,T0);b.finish(d);
        ASSERT_TRUE(b.put(11,stamp,T0+20000));ASSERT_TRUE(b.put(12,stamp+960,T0+40000));
        EXPECT_EQ(b.stats().counters.discontinuity,1u);
        d=b.tick(1,(T0+20000)+T0);EXPECT_EQ(d.kind,K::Decode);b.finish(d);d=b.tick(2,(T0+20000)+T0+20000);EXPECT_EQ(d.kind,K::Decode);b.finish(d);
    }
}
TEST(Playout, BothWrapsAndRandomTimestampPhase) {
    for(int64_t stamp:{int64_t(1001), int64_t(UINT32_MAX)-1000}) {
        Buffer b;b.put(65535,stamp);b.put(1,stamp+1920);b.put(0,stamp+960);
        for(unsigned i=0;i<3;++i) {auto d=b.tick(i,T0+i*20000);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,stamp+i*960);b.finish(d);}
        EXPECT_EQ(b.stats().counters.discontinuity,0u);EXPECT_FALSE(b.put(65535,stamp,T0+100000));
    }
}
TEST(Playout, SsrcChangesRequireSignalingAndRejectStragglers) {
    Buffer b;b.put(10,0);auto packet=b.packet(11,960);packet.ssrc=8;
    EXPECT_FALSE(b.p.insert(packet,20000));EXPECT_EQ(b.stats().counters.discontinuity,0u);
    b.p.authorize(8,2);ASSERT_TRUE(b.p.insert(packet,40000));EXPECT_EQ(b.stats().counters.discontinuity,1u);
    EXPECT_FALSE(b.put(12,1920,60000));EXPECT_FALSE(b.put(13,2880,80000));
    EXPECT_EQ(b.stats().counters.discontinuity,1u);
    b.p.authorize(7,3);EXPECT_TRUE(b.put(12,1920,100000));EXPECT_EQ(b.stats().counters.discontinuity,2u);
}
TEST(Playout, DuplicateWhileWritingReadingAfterDecodeAndPlc) {
    Buffer b;auto token=b.p.reserve(b.packet(10,0),0);ASSERT_TRUE(token);
    EXPECT_FALSE(b.put(10,0));ASSERT_TRUE(b.p.publish(token,b.payload,2));
    auto d=b.tick(0,T0);ASSERT_EQ(d.kind,K::Decode);EXPECT_FALSE(b.put(10,0));
    EXPECT_EQ(d.payload[1],42);b.finish(d);EXPECT_FALSE(b.put(10,0));
    d=b.tick(1,T0+20000);EXPECT_EQ(d.kind,K::Plc);EXPECT_FALSE(b.put(11,960,T0+20001));
    EXPECT_EQ(b.stats().counters.rejected[unsigned(IngressError::Duplicate)],2u);
    EXPECT_EQ(b.stats().counters.rejected[unsigned(IngressError::Late)],2u);
}
TEST(Playout, CopyPublishAtDeadlineBothOrderings) {
    for(bool before:{false,true}) {
        Buffer b;b.put(10,0);auto d=b.tick(0,T0);b.finish(d);
        auto token=b.p.reserve(b.packet(11,960),T0+19999);ASSERT_TRUE(token);
        if(before) {ASSERT_TRUE(b.p.publish(token,b.payload,2));}
        d=b.tick(1,T0+20000);EXPECT_EQ(d.kind,before?K::Decode:K::Plc);b.finish(d);
        if(!before) {EXPECT_FALSE(b.p.publish(token,b.payload,2));}
    }
}
TEST(Playout, OwnershipSurvivesConcurrentGenerationInvalidation) {
    Buffer b;b.put(10,0);auto token=b.p.reserve(b.packet(11,960),20000);ASSERT_TRUE(token);
    auto d=b.tick(0,T0);ASSERT_EQ(d.kind,K::Decode);
    std::thread producer([&] {b.p.authorize(8,2);auto p=b.packet(50,50000);p.ssrc=8;EXPECT_TRUE(b.p.insert(p,T0+20000));});
    producer.join();EXPECT_EQ(d.payload[1],42);
    EXPECT_FALSE(b.p.publish(token,b.payload,2));b.finish(d);
    EXPECT_EQ(b.stats().occupancy,1u);
    b.p.disconnect();EXPECT_FALSE(b.put(12,1920));EXPECT_EQ(b.tick(1,T0+20000).kind,K::Disconnected);
}
TEST(Playout, ContiguousBacklogTrimsButHolesNeverTrim) {
    Buffer b;for(unsigned i=0;i<32;++i)ASSERT_TRUE(b.put(100+i,960*i));
    EXPECT_EQ(b.stats().occupancy,32u);EXPECT_EQ(b.stats().future_span,32u);
    EXPECT_FALSE(b.put(132,32*960));auto d=b.tick(0,T0);
    ASSERT_EQ(d.kind,K::Decode);EXPECT_EQ(d.trimmed,32u-R);EXPECT_EQ(d.timestamp,(32-R)*960);b.finish(d);
    EXPECT_EQ(b.stats().ready_depth,R-1);EXPECT_EQ(b.stats().counters.latency_trim,32u-R);
    Buffer sparse;sparse.put(100,0);sparse.put(113,13*960);d=sparse.tick(0,T0);sparse.finish(d);
    EXPECT_EQ(sparse.stats().future_span,13u);EXPECT_EQ(sparse.stats().counters.latency_trim,0u);
}
TEST(Playout, PresentDecodeErrorIsNotMissingAndNoRepeatOnOldTick) {
    Buffer b;b.put(10,0);auto d=b.tick(0,T0);b.finish(d,false);
    EXPECT_EQ(b.stats().counters.decode_errors,1u);EXPECT_EQ(b.stats().counters.missing_slots,0u);
    EXPECT_EQ(b.tick(0,T0).kind,K::Wait);
}
TEST(Playout, DeviceSilenceRetiresNeverRenderedOnlyOnceAndRejectsLate) {
    Buffer b;for(unsigned i=0;i<8;++i)b.put(10+i,i*960);
    auto d=b.tick(0,T0);b.finish(d);
    d=b.tick(5,T0+100000,true,4);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,5*960);b.finish(d);
    EXPECT_EQ(b.stats().counters.media_advance,6u);EXPECT_EQ(b.stats().counters.device_silence_blocks,4u);
    EXPECT_EQ(b.tick(5,T0+100000,true,4).kind,K::Wait);EXPECT_FALSE(b.put(12,1920));
    EXPECT_EQ(b.tick(7,T0+140000,true,0).kind,K::Decode); // resynchronize skipped ledger
}
TEST(Playout, FreshStarvationRecoveryHasFixedPrefillAndFrozenOldWatermark) {
    Buffer b;b.put(10,0);auto d=b.tick(0,T0);b.finish(d);
    for(unsigned i=1;i<=30;++i) {d=b.tick(i,T0+i*20000);b.finish(d);}
    EXPECT_EQ(b.stats().counters.starvation,1u);auto stamp=b.stats().watermark;
    EXPECT_FALSE(b.put(11,960,T0+610000));ASSERT_TRUE(b.put(110,100*960,T0+620000));
    EXPECT_GE(b.stats().watermark,stamp);EXPECT_EQ(b.tick(31,T0+620000).kind,K::Prefill);
    EXPECT_EQ(b.tick(32,(T0+620000)+T0).kind,K::Decode);EXPECT_EQ(b.stats().counters.prefills,2u);
}
TEST(Playout, CleanTenMinuteDriftBothSignsOnePercentAnd500ppm) {
    for(double rate:{1.01,.99,1.0005,.9995}) {
        Buffer b;double arrival=0;unsigned source=0,ticks=0;
        for(int64_t now=0;now<600000000;now+=20000,++ticks) {
            while(arrival<=now) {ASSERT_TRUE(b.put(source,source*960ll,int64_t(arrival)));++source;arrival+=20000/rate;}
            auto d=b.tick(ticks,now);b.finish(d);
        }
        auto c=b.stats().counters;
        EXPECT_EQ(c.missing_slots,0u)<<rate;EXPECT_EQ(c.starvation,0u)<<rate;
        EXPECT_EQ(c.latency_trim,0u)<<rate;EXPECT_EQ(c.rejected[unsigned(IngressError::Window)],0u);
        EXPECT_EQ(c.media_advance,c.decoded_packets+c.drift_drop);
        EXPECT_EQ(c.render_decisions-R,c.decoded_packets+c.drift_insert);
        auto expected=unsigned(std::abs(rate-1)*30000);
        if(rate>1) {EXPECT_NEAR(c.drift_drop,expected,10);EXPECT_EQ(c.drift_insert,0u);}
        else {EXPECT_NEAR(c.drift_insert,expected,10);EXPECT_EQ(c.drift_drop,0u);}
        EXPECT_LE(b.stats().ready_depth,B);
    }
}
TEST(Playout, ZeroDriftJitterAndIsolatedLossDoNotTriggerCorrections) {
    Buffer b;unsigned next=0;
    for(unsigned i=0;i<3000;++i) {
        int64_t now=i*20000ll;
        // deterministic +/- one tick arrival jitter, all within reserve
        unsigned to=i+(i%3==0?1:0);
        while(next<=to) {if(next%101!=50)b.put(next,next*960ll,now);++next;}
        auto d=b.tick(i,now);b.finish(d);
    }
    auto c=b.stats().counters;EXPECT_EQ(c.drift_drop,0u);EXPECT_EQ(c.drift_insert,0u);
    EXPECT_EQ(c.latency_trim,0u);EXPECT_EQ(c.starvation,0u);EXPECT_EQ(c.plc_calls,c.missing_slots);
}

TEST(Playout, SignaledSameSourceNewTrackInvalidatesLeaseAndPrefillsOnce) {
    Buffer b;b.put(10,0);b.put(11,960);auto old=b.tick(0,T0);
    ASSERT_EQ(old.kind,K::Decode);b.p.authorize(7,2);ASSERT_TRUE(b.put(50,50000,T0+20000));
    EXPECT_EQ(old.payload[1],42);b.finish(old);
    auto d=b.tick(1,T0+40000);EXPECT_EQ(d.kind,K::Prefill);EXPECT_TRUE(d.reset_decoder);
    EXPECT_EQ(d.reset_reason,1u);EXPECT_EQ(d.reset_retired,1u);b.finish(d);
    d=b.tick(2,(T0+20000)+T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,50000);b.finish(d);
    EXPECT_EQ(b.stats().counters.discontinuity,1u);EXPECT_EQ(b.stats().counters.prefills,2u);
}
TEST(Playout, RebaseTraceExcludesPreservedCandidateFromRetiredPackets) {
    Buffer b;b.put(10,0);b.put(11,960);auto d=b.tick(0,T0);b.finish(d);
    ASSERT_TRUE(b.put(12,9600000,T0+10000));ASSERT_TRUE(b.put(13,9600960,T0+30000));
    d=b.tick(1,T0+40000);EXPECT_EQ(d.reset_reason,2u);EXPECT_EQ(d.reset_retired,1u);
    EXPECT_EQ(d.previous_watermark,960);EXPECT_EQ(d.timestamp,9600000);b.finish(d);
}

TEST(Playout, WindowEdgeAdjacentGapConfirmsFromCandidateIngress) {
    Buffer b;b.put(0,0);auto d=b.tick(0,T0);b.finish(d);
    ASSERT_TRUE(b.put(31,31*960,T0+10000));
    ASSERT_TRUE(b.put(32,33*960,T0+20000));
    EXPECT_EQ(b.stats().counters.discontinuity,0u);
    ASSERT_TRUE(b.put(33,34*960,T0+40000));
    EXPECT_EQ(b.stats().counters.discontinuity,1u);
    d=b.tick(1,(T0+20000)+T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,33*960);b.finish(d);
}
TEST(Playout, WindowEdgeGapMissingAndFalseConfirmersDoNotRebase) {
    for(bool false_confirm:{false,true}) {
        Buffer b;b.put(0,0);b.finish(b.tick(0,T0));b.put(31,31*960,T0+10000);
        ASSERT_TRUE(b.put(32,33*960,T0+20000));
        if(false_confirm)b.put(33,35*960,T0+40000);
        EXPECT_EQ(b.stats().watermark,960);EXPECT_EQ(b.stats().counters.discontinuity,0u);
        EXPECT_EQ(b.stats().future_span,31u);
    }
}
TEST(Playout, StartupFloorCannotWalkBackwardAndPreservesFutureWindow) {
    for(int64_t initial:std::array<int64_t,3>{96000,1000,int64_t(UINT32_MAX)-1000}) {
        Buffer b;ASSERT_TRUE(b.put(100,initial));ASSERT_TRUE(b.put(100-R,initial-R*960,1000));
        ASSERT_TRUE(b.put(120,initial+20*960,2000));
        EXPECT_FALSE(b.put(100-2*R,initial-2*R*960,3000));
        EXPECT_EQ(b.stats().watermark,initial-R*960);EXPECT_EQ(b.stats().future_span,R+21);
        auto d=b.tick(0,T0);EXPECT_EQ(d.timestamp,initial-R*960);b.finish(d);
    }
}
TEST(Playout, RejectedReservationCannotMoveStartupWatermark) {
    Buffer b;for(int i=0;i<32;++i)ASSERT_TRUE(b.put(100+i,i*960));
    EXPECT_FALSE(b.put(99,-960,1000));EXPECT_EQ(b.stats().watermark,0);
}
TEST(Playout, StarvationResumeAcceptsEarlierReorderWithoutRestartingDeadline) {
    Buffer b;b.put(0,0);b.finish(b.tick(0,T0));
    for(unsigned i=1;i<=30;++i)b.finish(b.tick(i,T0+i*20000));
    ASSERT_TRUE(b.put(111,111*960,T0+620000));ASSERT_TRUE(b.put(110,110*960,T0+621000));
    auto d=b.tick(31,(T0+620000)+T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,110*960);b.finish(d);
    EXPECT_EQ(b.stats().counters.prefills,2u);
}
TEST(Playout, SameSourceSequenceRestartRebasesAfterTwoCoherentPackets) {
    Buffer b;b.put(40000,0);b.put(40001,960);b.finish(b.tick(0,T0));
    ASSERT_TRUE(b.put(0,1920,T0+10000));EXPECT_EQ(b.stats().counters.discontinuity,0u);
    ASSERT_TRUE(b.put(1,2880,T0+30000));EXPECT_EQ(b.stats().counters.discontinuity,1u);
    auto d=b.tick(1,(T0+10000)+T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,1920);b.finish(d);
}
TEST(Playout, SkippedTickResynchronizesAndKeepsRendering) {
    Buffer b;for(unsigned i=0;i<8;++i)b.put(i,i*960);
    b.finish(b.tick(0,T0));auto d=b.tick(2,T0+40000);
    EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,1920);b.finish(d);
    EXPECT_EQ(b.stats().counters.device_silence_blocks,1u);
    d=b.tick(3,T0+60000);EXPECT_EQ(d.kind,K::Decode);b.finish(d);
}
TEST(Playout, DriftExpansionWithInvalidPriorStillPreservesTimeline) {
    Buffer b;double arrival=0;unsigned source=0;
    for(unsigned i=0;i<1500;++i) {
        int64_t now=i*20000ll;
        while(arrival<=now) {ASSERT_TRUE(b.put(source,source*960ll,int64_t(arrival)));++source;arrival+=20000/.99;}
        b.finish(b.tick(i,now,false));
    }
    auto c=b.stats().counters;EXPECT_GT(c.drift_insert,0u);EXPECT_EQ(c.missing_slots,0u);
    EXPECT_EQ(c.rejected[unsigned(IngressError::Late)],0u);
}
TEST(Playout, BadDecodeDoesNotRestartConcealmentRun) {
    Buffer b;b.put(0,0);b.finish(b.tick(0,T0));
    for(unsigned i=1;i<=4;++i)b.finish(b.tick(i,T0+i*20000));
    ASSERT_TRUE(b.put(5,5*960,T0+90000));auto d=b.tick(5,T0+100000);ASSERT_EQ(d.kind,K::Decode);b.finish(d,false);
    d=b.tick(6,T0+120000);EXPECT_EQ(d.kind,K::Plc);EXPECT_EQ(d.missing_run,5u);b.finish(d);
    d=b.tick(7,T0+140000);EXPECT_EQ(d.kind,K::Zero);EXPECT_EQ(d.missing_run,6u);b.finish(d);
    auto c=b.stats().counters;EXPECT_EQ(c.decode_errors,1u);EXPECT_EQ(c.missing_slots,6u);EXPECT_EQ(c.plc_calls,5u);
}
TEST(Playout, DecodeErrorRestartsCleanDriftPersistence) {
    // Sustained R+2 remaining frames (ready R+3, at the backlog cap) is
    // above the drift band; the decode error at tick 98 must restart it.
    Buffer b;for(unsigned i=0;i<B;++i)b.put(i,i*960);
    unsigned source=B;
    for(unsigned i=0;i<99;++i) {
        auto d=b.tick(i,T0+i*20000);b.finish(d,i!=98);
        ASSERT_TRUE(b.put(source,source*960ll,T0+i*20000));++source;
    }
    auto d=b.tick(99,T0+99*20000);EXPECT_EQ(d.kind,K::Decode);b.finish(d);
    EXPECT_EQ(b.stats().counters.drift_drop,0u);
}
TEST(Playout, IndependentWrapPermutationsKeepOrdering) {
    for(int64_t seq:{12ll,65534ll})for(int64_t ts:std::array<int64_t,2>{1001,int64_t(UINT32_MAX)-1000}) {
        for(bool reverse:{false,true}) {
            Buffer b;b.put(seq,ts);
            for(int i:reverse?std::array<int,3>{3,2,1}:std::array<int,3>{1,2,3})ASSERT_TRUE(b.put(seq+i,ts+i*960));
            for(unsigned i=0;i<4;++i) {auto d=b.tick(i,T0+i*20000);ASSERT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,ts+i*960);b.finish(d);}
            EXPECT_EQ(b.stats().counters.discontinuity,0u);
        }
    }
}
TEST(Playout, FullCapacityRetainsOutstandingWritingAndReadingAcrossReset) {
    Buffer b;b.put(0,0);auto read=b.tick(0,T0);ASSERT_EQ(read.kind,K::Decode);
    auto writing=b.p.reserve(b.packet(1,960),T0+10000);ASSERT_TRUE(writing);
    b.p.authorize(8,2);
    for(unsigned i=0;i<30;++i) {auto p=b.packet(i,i*960);p.ssrc=8;ASSERT_TRUE(b.p.insert(p,T0+20000));}
    auto p=b.packet(30,30*960);p.ssrc=8;EXPECT_FALSE(b.p.insert(p,T0+20000));
    EXPECT_EQ(b.stats().occupancy,32u);EXPECT_EQ(read.payload[1],42);
    EXPECT_FALSE(b.p.publish(writing,b.payload,2));b.finish(read);
    EXPECT_EQ(b.stats().occupancy,30u);EXPECT_TRUE(b.p.insert(p,T0+20000));
}
TEST(Playout, OneSecondDeviceStallRetiresBeforeIngressWindowAdmission) {
    Buffer b;for(unsigned i=0;i<R+1;++i)b.put(i,i*960);
    b.finish(b.tick(0,T0));
    // DMA continues while playback is parked. Its expired count includes tick
    // zero, which was already rendered, and therefore never retires it twice.
    for(unsigned i=1;i<=50;++i) {
        dg::DeviceProgress progress{1,i+1,true};
        auto p=b.packet(R+i,(R+i)*960);
        ASSERT_TRUE(b.p.insert(p,T0+i*20000,progress));
    }
    auto d=b.tick(51,T0+51*20000,true,50);ASSERT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,51*960);b.finish(d);
    auto c=b.stats().counters;EXPECT_EQ(c.starvation,0u);EXPECT_EQ(c.prefills,1u);
    EXPECT_EQ(c.rejected[unsigned(IngressError::Window)],0u);EXPECT_EQ(c.device_silence_blocks,50u);
    EXPECT_EQ(c.media_advance,c.decoded_packets+c.device_silence_blocks);
}
TEST(Playout, PrefillAndStarvedZerosHaveDistinctKindAndNoMissingAccounting) {
    Buffer b;b.put(0,0);auto d=b.tick(0,0);EXPECT_EQ(d.kind,K::Prefill);b.finish(d);
    b.finish(b.tick(1,T0));
    for(unsigned i=2;i<=30;++i)b.finish(b.tick(i,T0-20000+i*20000));
    d=b.tick(31,T0+600000);EXPECT_EQ(d.kind,K::Prefill);
    auto before=b.stats().counters;b.finish(d);b.finish(b.tick(32,T0+620000));
    EXPECT_EQ(b.stats().counters.missing_slots,before.missing_slots);
}

TEST(Playout, CorrectionCarriesDecisionDepthAndFilterWithoutChangingPolicy) {
    Buffer b;unsigned next=0;bool saw=false;
    for(unsigned tick=0;tick<200;++tick) {
        // Two frames above the target remain (R+2): the existing one-second
        // persistence plus 50-sample filter must produce one drop, not a trim.
        while(next<=tick+R+2){ASSERT_TRUE(b.put(next,next*960ll,tick*20000ll));++next;}
        auto d=b.tick(tick,T0+tick*20000ll);
        if(d.skipped) {
            EXPECT_EQ(d.ready_depth,R+3);EXPECT_EQ(d.drift_filter_sum,(R+2)*50);
            EXPECT_EQ(d.drift_filter_count,50u);EXPECT_GE(d.drift_persistence_us,1000000);
            EXPECT_EQ(d.trimmed,0u);saw=true;b.finish(d);break;
        }
        b.finish(d);
    }
    EXPECT_TRUE(saw);EXPECT_EQ(b.stats().counters.drift_drop,1u);
}

TEST(Playout, EqualClocksStartupBurstStartsAtTargetReserveWithoutAnEdit) {
    Buffer b;unsigned next=0;int64_t first_decode=-1;
    for(unsigned tick=0;tick<1000;++tick) {
        // Sender initially releases three frames, then one every 20 ms.
        // There is no rate mismatch: just 40 ms of initial source lead.
        const auto now=tick*20000ll;
        while(next<=tick+2){ASSERT_TRUE(b.put(next,next*960ll,now));++next;}
        auto d=b.tick(tick,now);
        if(d.kind==K::Decode && first_decode<0){first_decode=now;EXPECT_EQ(d.ready_depth,R+1);}
        b.finish(d);
    }
    EXPECT_EQ(first_decode,int64_t(R-2)*20000); // R+1 contiguous: three lead frames plus R-2 ticks
    auto c=b.stats().counters;
    EXPECT_EQ(c.drift_drop,0u);EXPECT_EQ(c.drift_insert,0u);
    EXPECT_EQ(c.missing_slots,0u);EXPECT_EQ(c.latency_trim,0u);
}

TEST(Playout, EarlyPrefillRequiresContiguousReserveAndRetainsDeadlineFallback) {
    Buffer full;for(unsigned i=0;i<R+1;++i)full.put(i,i*960);
    auto d=full.tick(0,60000);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.ready_depth,R+1);full.finish(d);
    // More than R+1 frames in total, but only three contiguous.
    Buffer holes;for(unsigned i=0;i<=R+3;++i)if(i!=3)holes.put(i,i*960);
    d=holes.tick(0,60000);EXPECT_EQ(d.kind,K::Prefill);holes.finish(d);
    d=holes.tick(1,T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.ready_depth,3u);holes.finish(d);
}

TEST(Playout, CancellationRetiresOwnedPacketsWithoutLossOrDecoderWorkOnControl) {
    Buffer b;b.put(10,0);b.put(11,960);
    auto writing=b.p.reserve(b.packet(12,1920),1000);ASSERT_TRUE(writing);
    auto reading=b.tick(0,T0);ASSERT_EQ(reading.kind,K::Decode);
    auto generation=b.p.generation();ASSERT_TRUE(b.p.cancel(T0+10000));
    EXPECT_GT(b.p.generation(),generation);EXPECT_FALSE(b.p.cancel(T0+100000));
    EXPECT_FALSE(b.put(13,2880,T0+20000));
    EXPECT_FALSE(b.p.publish(writing,b.payload,2));b.finish(reading);
    auto d=b.tick(1,T0+20000);EXPECT_EQ(d.kind,K::Cancelled);EXPECT_TRUE(d.reset_decoder);
    EXPECT_EQ(d.reset_reason,4u);b.finish(d);
    d=b.tick(2,T0+10000+150000);EXPECT_EQ(d.kind,K::Idle);b.finish(d);
    auto c=b.stats().counters;
    EXPECT_EQ(c.cancellations,1u);EXPECT_EQ(c.cancelled_packets,4u);
    EXPECT_EQ(c.cancelled_render_intervals,1u);EXPECT_EQ(c.missing_slots,0u);
    EXPECT_EQ(c.plc_calls,0u);EXPECT_EQ(c.decoded_packets,0u);
    for(auto n:c.rejected)EXPECT_EQ(n,0u);
    EXPECT_EQ(b.stats().occupancy,0u);EXPECT_FALSE(b.p.ready());
}
TEST(Playout, BotStartedEndsMuteAndNewReplyReanchorsWithoutStarvation) {
    Buffer b;b.put(10,0);b.finish(b.tick(0,T0));b.p.cancel(T0+10000);
    // The known audio-before-BotStarted limitation: early new audio is discarded.
    EXPECT_FALSE(b.put(20,9600,T0+20000));b.p.resume();
    EXPECT_TRUE(b.put(21,10560,T0+30000));
    auto d=b.tick(1,T0+30000);EXPECT_EQ(d.kind,K::Prefill);EXPECT_TRUE(d.reset_decoder);b.finish(d);
    d=b.tick(2,(T0+30000)+T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,10560);b.finish(d);
    EXPECT_EQ(b.stats().counters.starvation,0u);EXPECT_EQ(b.stats().counters.plc_calls,0u);
    EXPECT_TRUE(b.p.cancel((T0+30000)+T0+10000));
}
TEST(Playout, MuteExpiryAllowsLateOldRtpBestEffortLimitationAndDisconnectIsFinal) {
    Buffer b;b.put(10,0);b.p.cancel(1000);
    EXPECT_FALSE(b.put(11,960,150999));EXPECT_TRUE(b.put(11,960,151000));
    auto d=b.tick(0,151000+T0);EXPECT_EQ(d.kind,K::Decode);EXPECT_EQ(d.timestamp,960);b.finish(d);
    b.p.disconnect();EXPECT_FALSE(b.p.ready());EXPECT_FALSE(b.p.cancel(161000+T0));
    EXPECT_FALSE(b.put(12,1920,171000+T0));EXPECT_EQ(b.tick(1,171000+T0).kind,K::Disconnected);
}
TEST(Playout, SilentAndRedundantInterruptsDoNotCreateMissingMediaOrExtendMute) {
    Buffer b;ASSERT_TRUE(b.p.cancel(1000));EXPECT_FALSE(b.p.cancel(90000));
    auto d=b.tick(0,20000);EXPECT_EQ(d.kind,K::Cancelled);EXPECT_TRUE(d.reset_decoder);b.finish(d);
    d=b.tick(1,151000);EXPECT_EQ(d.kind,K::Idle);b.finish(d);
    EXPECT_FALSE(b.p.ready());EXPECT_EQ(b.stats().counters.cancelled_packets,0u);
    EXPECT_EQ(b.stats().counters.cancellations,1u);EXPECT_EQ(b.stats().counters.plc_calls,0u);
    EXPECT_EQ(b.stats().counters.missing_slots,0u);EXPECT_EQ(b.stats().counters.starvation,0u);
    b.p.resume();EXPECT_TRUE(b.p.cancel(160000));EXPECT_EQ(b.stats().counters.cancellations,2u);
}

#include "interrupt_sequence.h"
TEST(Playout, HigherIdInterruptAfterRtpReanchorCancelsBeforeBotStarted) {
    Buffer b;InterruptSequence ids;
    auto notice=[](unsigned id){return nlohmann::json{{"type","bot-interrupted"},{"data",{{"interrupt_seq",id}}}};};
    b.put(10,0);ASSERT_FALSE(ids.duplicate(notice(1)));ASSERT_TRUE(b.p.cancel(1000));
    EXPECT_FALSE(b.put(11,960,150000));ASSERT_TRUE(b.put(20,9600,151000));
    EXPECT_TRUE(ids.duplicate(notice(1))); // delayed insurance copy stays harmless
    ASSERT_FALSE(ids.duplicate(notice(2)));ASSERT_TRUE(b.p.cancel(152000));
    EXPECT_EQ(b.stats().counters.cancellations,2u);EXPECT_EQ(b.stats().occupancy,0u);
    EXPECT_EQ(b.tick(0,153000).kind,K::Cancelled);
}
