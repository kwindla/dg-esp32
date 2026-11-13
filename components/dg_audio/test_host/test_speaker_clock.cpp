#include "dg_audio/speaker_clock.h"
#include <gtest/gtest.h>
using dg_audio::SpeakerClock;
TEST(SpeakerClock, DeviceDerivedWaitReachesEveryIntervalWithBoundedWakeups) {
    SpeakerClock c;c.start(0,0,0,960);
    unsigned decisions=0,wakeups=0;int64_t now=0;
    while(now<=1000000) {
        auto callbacks=unsigned(now/15000);
        c.observe(callbacks*240,callbacks*15000,now);
        auto p=c.next(now);++wakeups;
        if(p.ready) {
            EXPECT_EQ(p.tick.index,decisions);
            EXPECT_EQ(p.tick.deadline_us,int64_t(decisions+1)*20000);
            ASSERT_TRUE(c.rendered(p.tick));c.submitted(320);++decisions;
        } else {
            EXPECT_LE(p.wait_us,15000u);
            now+=std::max(1000u,p.wait_us/1000*1000);
        }
    }
    // Whole-descriptor capacity defers the first opportunity by one descriptor;
    // this window therefore contains 49 decisions without reducing steady throughput.
    EXPECT_EQ(decisions,49u);EXPECT_LT(wakeups,200u);
    EXPECT_EQ(c.silence_samples(),960u);
    // Without another DMA completion, repeated advised waits cannot create
    // device progress or authorize additional media pulls.
    SpeakerClock stalled;stalled.start(0,0,0,960);
    stalled.observe(480,30000,30000);
    auto first=stalled.next(30000);ASSERT_TRUE(first.ready);
    stalled.rendered(first.tick);stalled.submitted(320);
    stalled.observe(480,30000,2000000);
    EXPECT_FALSE(stalled.next(2000000).ready);
}
TEST(SpeakerClock, Four240CompletionsEqualThree320Blocks) {
    SpeakerClock c;c.start(0,0,0,960);unsigned decisions=0;
    for(unsigned samples=240;samples<=960;samples+=240) {
        auto now=samples*1000000ll/16000;c.observe(samples,uint32_t(now),now);
        auto p=c.next(now);if(p.ready) {ASSERT_TRUE(c.rendered(p.tick));c.submitted(320);++decisions;}
    }
    EXPECT_EQ(decisions,3u);EXPECT_EQ(c.completed(),960u);EXPECT_EQ(c.remainder(),0u);
    EXPECT_EQ(c.submitted(),1920u);
}
TEST(SpeakerClock, InterpolationIsDeviceAnchoredAndUnique) {
    SpeakerClock c;c.start(0,0,0,960);c.observe(480,30000,30000);
    auto p=c.next(30000);ASSERT_TRUE(p.ready);EXPECT_EQ(p.tick.deadline_us,20000);
    EXPECT_TRUE(c.rendered(p.tick));EXPECT_FALSE(c.rendered(p.tick));
    EXPECT_FALSE(c.next(30000).ready);
    c.observe(480,30000,90000);EXPECT_FALSE(c.next(90000).ready); // no independent oscillator
}
TEST(SpeakerClock, EightyAnd200msStallsRefillOnlyAndRetireUniqueIntervals) {
    for(unsigned pause:{80000u,200000u}) {
        SpeakerClock c;c.start(0,0,0,960);c.observe(480,30000,30000);
        auto first=c.next(30000);ASSERT_TRUE(first.ready);c.rendered(first.tick);c.submitted(320);
        unsigned samples=pause*16/1000;c.observe(samples,pause,pause);
        auto refill=c.next(pause);EXPECT_FALSE(refill.ready);EXPECT_GT(refill.refill_samples,0u);
        c.submitted(refill.refill_samples);c.submitted_silence(refill.refill_samples);
        EXPECT_EQ(c.next(pause).refill_samples,0u); // refill is not charged twice
        unsigned later=pause+20000;c.observe(later*16/1000,later,later);
        auto next=c.next(later);ASSERT_TRUE(next.ready);
        EXPECT_EQ(next.tick.index-first.tick.index-1,next.tick.silence_blocks);
        EXPECT_GT(next.tick.silence_blocks,0u);EXPECT_TRUE(c.rendered(next.tick));
    }
}
TEST(SpeakerClock, PartialWriteCallbackBeforeReturnNoReplayOrExtraTick) {
    SpeakerClock c;c.start(0,0,0,960);c.observe(480,30000,30000);
    auto p=c.next(30000);c.rendered(p.tick);
    c.observe(480,30000,30000);c.submitted(80);EXPECT_TRUE(c.valid(p.tick));
    c.submitted(240);EXPECT_EQ(c.submitted(),1280u);EXPECT_FALSE(c.next(30000).ready);
    c.observe(960,60000,60000);EXPECT_FALSE(c.valid(p.tick));
    auto n=c.next(60000);EXPECT_TRUE(n.ready);EXPECT_NE(n.tick.index,p.tick.index);
}
TEST(SpeakerClock, SampleAndMicrosecondWrapCarryRemainderAndStopInvalidates) {
    SpeakerClock c;unsigned raw=UINT32_MAX-119,stamp=UINT32_MAX-9999;
    c.start(raw,stamp,int64_t(stamp),960);raw+=240;stamp+=15000;
    c.observe(raw,stamp,int64_t(UINT32_MAX)+5001);EXPECT_EQ(c.completed(),240u);EXPECT_EQ(c.remainder(),240u);
    raw+=240;stamp+=15000;c.observe(raw,stamp,int64_t(UINT32_MAX)+20001);
    EXPECT_EQ(c.completed(),480u);EXPECT_EQ(c.remainder(),160u);auto p=c.next(int64_t(UINT32_MAX)+20001);
    ASSERT_TRUE(p.ready);c.rendered(p.tick);c.stop();EXPECT_FALSE(c.valid(p.tick));EXPECT_FALSE(c.next(40000).ready);
}

TEST(SpeakerClock, SilenceCountsOnlyCompletedAutoClearAndCommittedRefillSuffix) {
    SpeakerClock c;c.start(0,0,0,960);
    c.observe(1600,100000,100000);
    EXPECT_EQ(c.silence_samples(),1600u); // preload plus completed auto-clear
    auto p=c.next(100000);ASSERT_GT(p.refill_samples,80u);
    EXPECT_EQ(c.silence_samples(),1840u); // includes the auto-cleared descriptor abandoned by IDF
    c.submitted(80);c.submitted_silence(80);
    EXPECT_EQ(c.silence_samples(),1920u);EXPECT_EQ(c.submitted(),1920u);
    EXPECT_FALSE(c.next(100000).ready);
}
TEST(SpeakerClock, RepeatedUnderrunsNeverRetireAnAlreadyRenderedIntervalTwice) {
    SpeakerClock c;c.start(0,0,0,960);uint64_t previous=0;bool seen=false;
    for(unsigned pause:{20000u,100000u,120000u,320000u,340000u,420000u,440000u}) {
        c.observe(pause*16/1000,pause,pause);auto p=c.next(pause);
        if(p.refill_samples) {c.submitted(p.refill_samples);c.submitted_silence(p.refill_samples);}
        if(p.ready) {
            if(seen) {EXPECT_EQ(p.tick.index-previous-1,p.tick.silence_blocks);}
            previous=p.tick.index;seen=true;ASSERT_TRUE(c.rendered(p.tick));c.submitted(320);
            EXPECT_FALSE(c.rendered(p.tick)); // no repeated retirement of this interval
        }
    }
    EXPECT_TRUE(seen);EXPECT_GT(c.silence_samples(),960u);
}

TEST(SpeakerClock, InterpolatedPlayheadCannotReleasePartialDmaDescriptor) {
    SpeakerClock c;c.start(0,0,0,960);
    c.observe(240,15000,20000);
    EXPECT_FALSE(c.next(20000).ready); // 320 elapsed but only 240 writable
    c.observe(240,15000,29999);
    EXPECT_FALSE(c.next(29999).ready);
    c.observe(480,30000,30000);
    auto p=c.next(30000);ASSERT_TRUE(p.ready);
    EXPECT_EQ(p.tick.deadline_us,20000); // original media interval retained
    EXPECT_GE(c.completed()+960-c.submitted(),320u);
}

#include "fakes/tx_ring.h"
TEST(SpeakerClock, ExpiredIntervalResumesThePhysicalRingEquilibrium) {
    // Exercise every DMA/block phase, including the partial-descriptor early
    // pop. A refill must fit actual IDF queue capacity, not just ledger space.
    for(unsigned skip_at:{200000u,215000u,230000u,245000u}) {
        SpeakerClock c;c.start(0,0,0,960);TxRing ring;ASSERT_EQ(ring.preload(960),960u);
        unsigned writes=0,refills=0;bool skipped=false;
        for(unsigned now=0;now<2000000;now+=1000) {
            if(!skipped && now>=skip_at) {now+=60000;skipped=true;}
            while(ring.completed<unsigned(now/15000)*240)ring.complete();
            c.observe(ring.completed,unsigned(now/15000)*15000,now);
            auto p=c.next(now);
            if(p.refill_samples) {
                ++refills;
                ASSERT_NE(ring.write_position(),UINT64_MAX);
            EXPECT_EQ(c.submitted(),ring.write_position());
                auto remaining=p.refill_samples;
                while(remaining) {
                    auto n=ring.write(remaining);ASSERT_GT(n,0u)<<now;
                    c.submitted(n);c.submitted_silence(n);remaining-=n;
                }
            }
            if(!p.ready)continue;
            // PERF captures this PRE-write ledger in RenderTick.
            EXPECT_EQ(c.submitted()-c.completed(),640-c.completed()%320)<<now;
            ASSERT_NE(ring.write_position(),UINT64_MAX);
            EXPECT_EQ(c.submitted(),ring.write_position())<<now;
            ASSERT_TRUE(c.rendered(p.tick));unsigned remaining=320;
            while(remaining) {
                auto n=ring.write(remaining);ASSERT_GT(n,0u)<<now;
                c.submitted(n);remaining-=n;
            }
            EXPECT_EQ(c.submitted()-c.completed(),960-c.completed()%320)<<now;
            ++writes;
        }
        EXPECT_EQ(refills,1u);EXPECT_GT(writes,80u);
    }
}
