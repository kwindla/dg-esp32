#include "codec/es7210_slots.h"
#include <gtest/gtest.h>
TEST(SlotMap,EveryBoardMicrophoneHasTheOriginalSignedPcmLocation) {
    const uint32_t frame[]={0x80007fff,0xffff0123};
    const auto& normal=dg_board::audio_hw(false);
#if DG_TEST_P4_TDM
    const int expected[]={-32768,-1,32767,0};
#elif DG_TEST_PAIRED_CODEC
    const int expected[]={32767,-32768,0,0};
#else
    const int expected[]={-1,0,0,0};
#endif
    for(unsigned i=0;i<4;++i)EXPECT_EQ(dg_audio::audio_sample(frame,normal.microphones[i]),expected[i]);
    const auto& aec=dg_board::audio_hw(true);
#if DG_TEST_P4_TDM
    const int tdm[]={-32768,-1,32767,0};
#elif DG_TEST_PAIRED_CODEC
    const int tdm[]={-32768,-1,32767,0x123};
#else
    const int tdm[]={-1,0,0,0};
#endif
    for(unsigned i=0;i<4;++i)EXPECT_EQ(dg_audio::audio_sample(frame,aec.microphones[i]),tdm[i]);
}
TEST(SlotMap,ExtractionFollowsTheSuppliedMapIncludingAbsentChannels) {
    auto hw=dg_board::audio_hw(true);hw.microphones={dg_board::AudioSlot{1,0},{0,0},{1,16},{}};
    const auto slots=dg_audio::es7210_slots(0x12348000,0x7fffffff,hw);
    EXPECT_EQ(slots.mic1,-1);EXPECT_EQ(slots.mic2,-32768);EXPECT_EQ(slots.mic3,32767);EXPECT_EQ(slots.mic4,0);
}
TEST(SlotMap,PlaybackPacksAndMeasuresTheSelectedSlots) {
    int16_t frame[2];
    for(auto slot:{dg_board::I2sSlot::Left,dg_board::I2sSlot::Right,dg_board::I2sSlot::Stereo}) {
        dg_audio::playback_slots(frame,-32768,slot);
        EXPECT_EQ(frame[0],slot==dg_board::I2sSlot::Right?0:-32768);
        EXPECT_EQ(frame[1],slot==dg_board::I2sSlot::Left?0:-32768);
        EXPECT_EQ(dg_audio::playback_sample(frame,slot),-32768);
    }
}

TEST(SlotMap,PrimaryRxSelectionAndEverySlotFitTheDmaFrame) {
    for(bool aec:{false,true}) {
        const auto& hw=dg_board::audio_hw(aec);
        const unsigned words=2*hw.rx.data_bits/32;
        for(auto slot:hw.microphones)if(slot.word>=0) {
            EXPECT_LT(unsigned(slot.word),words);EXPECT_TRUE(slot.shift==0 || slot.shift==16);
        }
        ASSERT_NE(hw.rx.selected_slot,dg_board::I2sSlot::Stereo);
        const bool right=hw.rx.selected_slot==dg_board::I2sSlot::Right;
        EXPECT_EQ(hw.microphones[0].word,hw.rx.data_bits==16?0:int(right));
        EXPECT_EQ(hw.microphones[0].shift,hw.rx.data_bits==16?(right?16u:0u):16u);
    }
}
TEST(SlotMap,PairedCodecKeepsRxAndTxDmaGeometryEqual) {
    for(bool aec:{false,true}) {
        const auto& hw=dg_board::audio_hw(aec);
        if(hw.codec==dg_board::CodecKind::Es8311Es7210) {
            EXPECT_EQ(hw.rx.dma_descriptors,hw.tx.dma_descriptors);
            EXPECT_EQ(hw.rx.dma_frames,hw.tx.dma_frames);
        }
    }
}
TEST(SlotMap,InvalidWordOrShiftIsSilentWithoutReadingAnotherFrame) {
    const uint32_t frame[]={0x12348000};
    EXPECT_EQ(dg_audio::audio_sample(frame,{-1,0}),0);
    EXPECT_EQ(dg_audio::audio_sample(frame,{1,0}),0);
    EXPECT_EQ(dg_audio::audio_sample(frame,{2,16}),0);
    EXPECT_EQ(dg_audio::audio_sample(frame,{0,32}),0);
    EXPECT_EQ(dg_audio::audio_sample(frame,{0,0}),-32768);
}
