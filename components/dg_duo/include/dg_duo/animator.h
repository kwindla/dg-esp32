#pragma once
#include "dg_display/engine.h"
#include "duo_pack.gen.h"
#include <cstdint>
namespace dg_duo {
enum class Character {Angel,Devil};
// Single owner, allocation-free. Sink admits one atomic engine scene batch.
class DuoAnimator {
public:
    using Sink=bool (*)(void*,const dg_display::Request&);
    explicit DuoAnimator(Sink sink,void* context=nullptr):sink_(sink),context_(context){}
    bool set_active(Character,int64_t now_ms=0,uint32_t seed=0xeca7u);
    bool tick(int64_t now_ms,uint32_t speaker_power,bool mic_open,uint32_t mic_power);
    Character active()const noexcept{return active_;}
    unsigned mouth_level()const noexcept{return mouth_;}
    unsigned eye_frame()const noexcept{return eyes_;}
    int64_t next_blink()const noexcept{return blink_due_;}
    int64_t next_look()const noexcept{return look_due_;}
private:
    Sink sink_;void* context_;Character active_=Character::Angel;const Pack* pack_=nullptr;
    uint32_t rng_=1;unsigned mouth_=0,eyes_=0,shown_mouth_=99,shown_eyes_=99;
    int64_t mouth_due_=0,last_audible_=-1000,blink_due_=0,blink_start_=-1,look_due_=0,look_end_=0;
    float envelope_=0,noise_power_=2500;bool speaking_=false;
    bool retry_=false;
    int64_t retry_at_=0;
    unsigned random(unsigned,unsigned)noexcept;
    bool emit(bool full);
};
}
