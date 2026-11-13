#pragma once
#include "dg_duo/protocol.h"
#include "dg_duo/animator.h"
#include "dg_display/duo_commit.h"
#include "dg_display/duo_admission.h"
namespace dg {
// Conservative wire budget: more than 15% below the reported 496-byte boundary.
inline constexpr size_t DuoRecordLimit=420;
// All methods run on app-control; touch and console callers enqueue app events.
class DuoUi {
public:
    DuoUi();
    dg_duo::Mailbox outbound;
    dg_duo::Device device;
    void lifecycle(bool live,uint32_t generation,int64_t now);
    bool receive(const char*,bool truncated,uint32_t generation,int64_t now);
    void tick(int64_t now);
    void status();
    void tap(bool injected,int64_t now); // shared gesture/console admission path
    void touch_enabled(bool enabled){touch_enabled_=enabled;}
    bool accepts_touch()const{return touch_enabled_ && live_;}
private:
    dg_duo::DuoAnimator animator_;
    bool live_=false,touch_enabled_=true,scene_due_=false,animated_=false;
    int64_t next_animation_=0,next_status_=0;
    dg_duo::Outcome proof_;
    dg_display::DuoTicket ticket_;
    dg_display::DuoPostResult admission_;
    bool swapping_=false,render_pending_=false;
    int64_t busy_since_=-1;
    bool busy_faulted_=false;
    void proof(uint64_t scene_revision);
    void admission_failed(const char* phase,int64_t now);
};
}
