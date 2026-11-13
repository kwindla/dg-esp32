#pragma once
#include "dg_board/audio_hw.h"
#include "dg_audio/pcm.h"
#include <cstddef>
namespace dg_audio {
// AudioSlot is the signed PCM16 location within one DMA frame. In ES7210
// TDM (Rev22 Fig2e) wire order is MIC1,MIC3,MIC2,MIC4; stereo32 places
// the earlier sample in the high half. Normal stereo16 and MEMS use the
// same extraction with their own board table. Absent microphones are silent.
constexpr int16_t audio_sample(const uint32_t* frame,dg_board::AudioSlot slot,size_t words) {
    return slot.word<0 || size_t(slot.word)>=words || (slot.shift!=0 && slot.shift!=16)
        ?0:signed16(frame[slot.word]>>slot.shift);
}
template<size_t Words> constexpr int16_t audio_sample(const uint32_t (&frame)[Words],dg_board::AudioSlot slot) {
    return audio_sample(frame,slot,Words);
}
struct Es7210Slots {int16_t mic1,mic2,mic3,mic4;};
inline Es7210Slots es7210_slots(uint32_t first,uint32_t second,const dg_board::AudioHw& hw) {
    const uint32_t frame[]={first,second};const auto& map=hw.microphones;
    return {audio_sample(frame,map[0]),audio_sample(frame,map[1]),audio_sample(frame,map[2]),audio_sample(frame,map[3])};
}
inline void playback_slots(int16_t* frame,int16_t sample,dg_board::I2sSlot slot) {
    frame[0]=slot==dg_board::I2sSlot::Right?0:sample;
    frame[1]=slot==dg_board::I2sSlot::Left?0:sample;
}
inline int16_t playback_sample(const int16_t* frame,dg_board::I2sSlot slot) {
    return frame[slot==dg_board::I2sSlot::Right?1:0];
}
}
