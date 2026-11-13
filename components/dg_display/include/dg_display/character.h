#pragma once
#include <cstdint>

namespace dg_display {
// Frame indexes match the mouth/eyes/whisker ordering in animation.json.
struct CharacterFrame {unsigned mouth=0,eyes=1,whiskers=0;};
class Character {
public:
    void reset(int64_t now_ms,uint32_t seed=0xeca7u) noexcept;
    CharacterFrame tick(int64_t now_ms,uint32_t speaker_power,bool mic_open,uint32_t mic_power) noexcept;
    bool speaking()const noexcept{return speaking_;}
    bool attentive()const noexcept{return attentive_;}
    unsigned blinks()const noexcept{return blinks_;}
    unsigned twitches()const noexcept{return twitches_;}
    int64_t next_blink()const noexcept{return blink_due_;}
    int64_t next_whisker()const noexcept{return whisker_due_;}
private:
    unsigned random(unsigned low,unsigned high) noexcept;
    CharacterFrame frame_;
    uint32_t rng_=1;unsigned blinks_=0,twitches_=0;
    int64_t blink_due_=0,blink_start_=-1,whisker_due_=0,whisker_end_=0;
    int64_t mouth_due_=0,last_audible_=-1000;
    float envelope_=0,noise_power_=2500;
    bool speaking_=false,attentive_=false;
};
}
