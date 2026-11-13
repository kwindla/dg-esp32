#include "dg_display/character.h"
#include <algorithm>
#include <cmath>

namespace dg_display {
unsigned Character::random(unsigned low,unsigned high) noexcept {
    rng_^=rng_<<13;rng_^=rng_>>17;rng_^=rng_<<5;
    return low+rng_%(high-low+1);
}
void Character::reset(int64_t now,uint32_t seed) noexcept {
    *this=Character{};rng_=seed?seed:1;
    blink_due_=now+random(3000,6000);whisker_due_=now+random(2000,5000);
    mouth_due_=now;last_audible_=now-1000;
}
CharacterFrame Character::tick(int64_t now,uint32_t power,bool mic_open,uint32_t mic_power) noexcept {
    if(power>1600)last_audible_=now; // RMS >40 PCM counts, after output volume.
    const bool was_speaking=speaking_;
    speaking_=now-last_audible_<60;
    attentive_=!speaking_ && mic_open && mic_power>std::max(40000.0f,noise_power_*16);
    if(mic_open && !attentive_ && !speaking_)noise_power_=.995f*noise_power_+.005f*mic_power;
    if(speaking_) {
        frame_.eyes=1;frame_.whiskers=0;blink_start_=-1;whisker_end_=0;
        if(!was_speaking){blink_due_=now+random(3000,6000);whisker_due_=now+random(2000,5000);}
    } else {
        if(was_speaking){blink_due_=now+random(3000,6000);whisker_due_=now+random(2000,5000);}
        if(blink_start_<0 && now>=blink_due_){blink_start_=now;++blinks_;blink_due_=now+random(3000,6000);}
        if(blink_start_>=0){const auto age=now-blink_start_;frame_.eyes=age<60?2:age<120?3:age<180?2:1;if(age>=180)blink_start_=-1;}
        if(whisker_end_ && now>=whisker_end_){frame_.whiskers=0;whisker_end_=0;}
        if(now>=whisker_due_ && !whisker_end_){
            frame_.whiskers=random(1,2);++twitches_;whisker_end_=now+random(300,500);
            // Listening to a user biases toward the lower end, still subtle.
            whisker_due_=now+random(2000,attentive_?3000:5000);
        }
    }
    if(now>=mouth_due_) {
        // No catch-up: even a delayed display task changes the mouth at most
        // once, then waits a full100 ms. Includes entry/exit transitions.
        mouth_due_=now+100;
        if(!speaking_){frame_.mouth=0;envelope_=0;}
        else {
            const float rms=std::sqrt(float(power));
            envelope_+=(rms-envelope_)*(rms>envelope_?.6f:.35f);
            // Hysteresis against small energy fluctuations at a sprite boundary.
            constexpr unsigned sprites[]={1,2,3,5,4};
            constexpr float boundaries[]={120,650,1400,2200};
            const unsigned target=envelope_<120?0:envelope_<650?1:envelope_<1400?2:envelope_<2200?3:4;
            unsigned previous=0;while(previous<4 && sprites[previous]!=frame_.mouth)++previous;
            if(frame_.mouth==0 || target==previous ||
               (target>previous && envelope_>boundaries[target-1]+60) ||
               (target<previous && envelope_<boundaries[target]-60))frame_.mouth=sprites[target];
        }
    }
    return frame_;
}
}
