#include "dg_duo/animator.h"
#include <algorithm>
#include <cmath>
#include <cstring>
namespace dg_duo {
using namespace dg_display;
static_assert(angel.layer_count<=4 && devil.layer_count<=4,"faceplate plus at most three animated layers");
unsigned DuoAnimator::random(unsigned low,unsigned high)noexcept{
    rng_^=rng_<<13;rng_^=rng_>>17;rng_^=rng_<<5;return low+rng_%(high-low+1);
}
bool DuoAnimator::emit(bool full){
    Request q{};
    auto show=[&](unsigned layer,const Frame& f){auto& op=q.operations[q.count++];
        op.kind=Operation::Show;op.layer=layer;op.x=f.x;op.y=f.y;std::strcpy(op.hash,f.hash);};
    // Follow the descriptor's compositing order; currently faceplate/eyes/mouth.
    for(unsigned layer=0;layer<pack_->layer_count;++layer){
        switch(pack_->order[layer]){
        case Layer::Faceplate:if(full)show(layer,pack_->faceplate);break;
        case Layer::Eyes:if(full || eyes_!=shown_eyes_)show(layer,pack_->eyes[eyes_]);break;
        case Layer::Mouth:if(full || mouth_!=shown_mouth_)show(layer,pack_->mouths[pack_->animator.mouth[mouth_]]);break;
        }
    }
    if(full)for(unsigned layer=pack_->layer_count;layer<8;++layer){auto& op=q.operations[q.count++];op.layer=layer;op.kind=Operation::Hide;}
    if(!q.count)return true;
    if(!sink_(context_,q)){retry_=!full;return false;}
    shown_mouth_=mouth_;shown_eyes_=eyes_;retry_=false;return true;
}
bool DuoAnimator::set_active(Character character,int64_t now,uint32_t seed){
    const auto before=*this;
    active_=character;pack_=character==Character::Angel?&angel:&devil;
    rng_=seed?seed:1;mouth_=0;eyes_=pack_->animator.open;shown_mouth_=shown_eyes_=99;retry_=false;
    envelope_=0;noise_power_=2500;speaking_=false;mouth_due_=now;last_audible_=now-1000;
    blink_start_=-1;look_end_=0;blink_due_=now+random(3000,6000);look_due_=now+random(2000,5000);
    if(emit(true))return true;
    *this=before;return false;
}
bool DuoAnimator::tick(int64_t now,uint32_t power,bool mic_open,uint32_t mic_power){
    if(!pack_)return false;
    // Retain the exact unshown frame even if backpressure spans a blink phase.
    if(retry_){
        if(!emit(false))return false;
        // Preserve the frame's remaining hold time after admission, otherwise
        // a delayed half-blink could be superseded before the next render.
        const auto delay=std::max(int64_t(0),now-retry_at_);
        blink_due_+=delay;look_due_+=delay;mouth_due_+=delay;
        if(blink_start_>=0)blink_start_+=delay;
        if(look_end_)look_end_+=delay;
        return true;
    }
    if(power>1600)last_audible_=now;
    const bool was_speaking=speaking_;speaking_=now-last_audible_<60;
    const bool attentive=!speaking_ && mic_open && mic_power>std::max(40000.0f,noise_power_*16);
    if(mic_open && !attentive && !speaking_)noise_power_=.995f*noise_power_+.005f*mic_power;
    if(speaking_!=was_speaking){blink_due_=now+random(3000,6000);look_due_=now+random(2000,5000);}
    if(speaking_){eyes_=pack_->animator.open;blink_start_=-1;look_end_=0;}
    else{
        if(blink_start_<0 && now>=blink_due_){blink_start_=now;blink_due_=now+random(3000,6000);look_end_=0;}
        if(blink_start_>=0){const auto age=now-blink_start_;
            eyes_=age<60?pack_->animator.half:age<120?pack_->animator.blink:age<180?pack_->animator.half:pack_->animator.open;
            if(age>=180)blink_start_=-1;
        }else{
            if(look_end_ && now>=look_end_){eyes_=pack_->animator.open;look_end_=0;}
            if(now>=look_due_ && !look_end_){eyes_=random(0,1)?pack_->animator.look_left:pack_->animator.look_right;
                look_end_=now+random(300,500);look_due_=now+random(2000,attentive?3000:5000);}
        }
    }
    if(now>=mouth_due_){
        mouth_due_=now+100; // Same envelope, hold time and hysteresis as the cat.
        if(!speaking_){mouth_=0;envelope_=0;}
        else{
            const float rms=std::sqrt(float(power));envelope_+=(rms-envelope_)*(rms>envelope_?.6f:.35f);
            constexpr float boundaries[]={120,650,1400,2200};
            const unsigned target=envelope_<120?0:envelope_<650?1:envelope_<1400?2:envelope_<2200?3:4;
            if(!mouth_ || target==mouth_ || (target>mouth_ && envelope_>boundaries[target-1]+60) ||
               (target<mouth_ && envelope_<boundaries[target]-60))mouth_=target;
        }
    }
    if(emit(false))return true;
    retry_at_=now;return false;
}
}
