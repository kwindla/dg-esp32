#include "dg_audio/speaker.h"
#include "dg_board/audio_hw.h"
#include "fake_i2s.h"
#include <gtest/gtest.h>
namespace {dg_board::AudioHw hw;}
namespace dg_board {const AudioHw& audio_hw(bool) {return hw;}}
TEST(SpeakerSlots,SelectedPcmSlotControlsPackingAndMeasurementOnStereoDma) {
    for(auto slot:{dg_board::I2sSlot::Left,dg_board::I2sSlot::Right,dg_board::I2sSlot::Stereo}) {
        fake::reset(false);hw={};hw.tx.slot=dg_board::I2sSlot::Stereo;
        hw.tx.selected_slot=slot;hw.tx.dma_descriptors=4;hw.tx.dma_frames=240;
        dg_audio::Speaker speaker;const int16_t pcm[]={321,-123};
        ASSERT_EQ(speaker.write(pcm,2,10),2u);EXPECT_EQ(speaker.peak(),321u);
        const int16_t left=slot==dg_board::I2sSlot::Right?0:321;
        const int16_t right=slot==dg_board::I2sSlot::Left?0:321;
        EXPECT_EQ(fake::submitted_stereo,(std::vector<int16_t>{left,right,int16_t(-123*bool(left)),int16_t(-123*bool(right))}));
    }
}
