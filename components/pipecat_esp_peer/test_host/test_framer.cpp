#include "framer.h"
#include "uplink_frame.h"
#include <gtest/gtest.h>
#include <numeric>
#include <vector>
using namespace pipecat::esp_peer;
TEST(Framer, ArbitraryChunksPreserveSamplesAndMilliseconds) {
    std::array<PcmFrame, 4> storage;
    Framer f(storage);
    std::vector<int16_t> input(960);
    std::iota(input.begin(), input.end(), -480);
    EXPECT_EQ(f.push(input.data(), 17), 17u);
    EXPECT_EQ(f.depth(), 0u);
    EXPECT_EQ(f.push(input.data() + 17, 700), 700u);
    EXPECT_EQ(f.pending_samples(), 77u);
    EXPECT_EQ(f.push(input.data() + 717, 243), 243u);
    PcmFrame out;
    for (size_t i = 0; i < 3; ++i) {
        ASSERT_TRUE(f.pop(out));
        EXPECT_EQ(out.pts_ms, i * 20);
        EXPECT_TRUE(std::equal(out.samples.begin(), out.samples.end(), input.begin() + i * 320));
    }
    EXPECT_FALSE(f.pop(out));
}
TEST(Framer, OverloadPreservesTimeAndOldestAudio) {
    std::array<PcmFrame, 4> storage;
    Framer f(storage);
    std::array<int16_t, 1920> input{};
    EXPECT_EQ(f.push(input.data(), input.size()), 1280u);
    EXPECT_EQ(f.depth(), 4u);
    EXPECT_EQ(f.dropped(), 2u);
    PcmFrame out;
    for (unsigned i = 0; i < 4; ++i) { ASSERT_TRUE(f.pop(out)); EXPECT_EQ(out.pts_ms, i * 20); }
    f.push(input.data(), 320);
    ASSERT_TRUE(f.pop(out));
    EXPECT_EQ(out.pts_ms, 120u);
}
TEST(Framer, WrapResetAndEmptyInput) {
    std::array<PcmFrame, 4> storage;
    Framer f(storage);
    std::array<int16_t, 640> input{};
    f.reset(UINT32_MAX - 19);
    f.push(input.data(), input.size());
    PcmFrame out;
    ASSERT_TRUE(f.pop(out)); EXPECT_EQ(out.pts_ms, UINT32_MAX - 19);
    ASSERT_TRUE(f.pop(out)); EXPECT_EQ(out.pts_ms, 0u);
    EXPECT_EQ(pts_distance(0, UINT32_MAX - 19), 20);
    EXPECT_EQ(pts_distance(UINT32_MAX - 19, 0), -20);
    f.push(input.data(), 1);
    f.reset();
    EXPECT_EQ(f.pending_samples(), 0u);
    EXPECT_FALSE(f.pop(out));
    EXPECT_EQ(f.push(nullptr, 0), 0u);
    EXPECT_THROW(f.push(nullptr, 1), std::invalid_argument);
}
TEST(Framer, PartialFrameCanSurviveFullQueueIfConsumerCatchesUp) {
    std::array<PcmFrame, 4> storage;
    Framer f(storage);
    std::array<int16_t, 1280> input{};
    f.push(input.data(), input.size());
    EXPECT_EQ(f.push(input.data(), 10), 10u);
    PcmFrame out;
    ASSERT_TRUE(f.pop(out));
    EXPECT_EQ(f.push(input.data() + 10, 310), 310u);
    for (unsigned i = 1; i <= 4; ++i) {
        ASSERT_TRUE(f.pop(out)); EXPECT_EQ(out.pts_ms, i * 20);
    }
    EXPECT_EQ(f.dropped(), 0u);
}
TEST(UplinkFrame, FortyMillisecondsPreservesPcmAndWraparoundPts) {
    UplinkFrame batch;
    PcmFrame first, second;
    first.pts_ms=UINT32_MAX-19; second.pts_ms=0;
    first.samples.fill(123); second.samples.fill(-456);
    EXPECT_FALSE(batch.push(first,40));
    ASSERT_TRUE(batch.push(second,40));
    EXPECT_EQ(batch.pts_ms,UINT32_MAX-19);
    EXPECT_TRUE(std::all_of(batch.samples.begin(),batch.samples.begin()+320,[](auto x){return x==123;}));
    EXPECT_TRUE(std::all_of(batch.samples.begin()+320,batch.samples.end(),[](auto x){return x==-456;}));
    EXPECT_EQ(batch.discarded,0u);
}
TEST(UplinkFrame, MissingCaptureFrameDoesNotCompressTimeline) {
    UplinkFrame batch;
    PcmFrame frame;
    frame.pts_ms=100; frame.samples.fill(1);
    EXPECT_FALSE(batch.push(frame,40));
    frame.pts_ms=140; frame.samples.fill(2);
    EXPECT_FALSE(batch.push(frame,40));
    EXPECT_EQ(batch.discarded,1u);
    frame.pts_ms=160; frame.samples.fill(3);
    ASSERT_TRUE(batch.push(frame,40));
    EXPECT_EQ(batch.pts_ms,140u);
    EXPECT_EQ(batch.samples.front(),2); EXPECT_EQ(batch.samples.back(),3);
}
TEST(UplinkFrame, CaptureDisabledReenabledCannotSpliceOldHalfFrame) {
    std::array<PcmFrame,4> storage;Framer framer(storage);UplinkFrame batch;
    std::array<int16_t,640> pcm;pcm.fill(1);PcmFrame frame;
    framer.push(pcm.data(),320);ASSERT_TRUE(framer.pop(frame));EXPECT_FALSE(batch.push(frame,40));
    framer.push(pcm.data(),12);framer.discontinuity();
    pcm.fill(2);framer.push(pcm.data(),640);ASSERT_TRUE(framer.pop(frame));EXPECT_FALSE(batch.push(frame,40));
    ASSERT_TRUE(framer.pop(frame));ASSERT_TRUE(batch.push(frame,40));
    EXPECT_EQ(batch.discarded,1u);EXPECT_EQ(framer.dropped(),1u);
    EXPECT_TRUE(std::all_of(batch.samples.begin(),batch.samples.end(),[](auto x){return x==2;}));
}
