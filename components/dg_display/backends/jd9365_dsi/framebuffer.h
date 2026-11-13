#pragma once
#include "dg_display/present.h"
#include <atomic>
#include <array>
namespace dg_display {
// Single task owns all methods except frame_complete(), called by the DSI ISR.
// Source pixels remain BE; only the driver-owned scan-out framebuffer is LE.
class DsiFramebuffer:public PanelBackend {
public:
    static constexpr size_t History=256,FrameHistory=16;
    struct Event {SubmitId id;ScanOutcome outcome;uint32_t committed_us=0,completed_us=0;bool timed=false;};
    struct Hooks {
        void* context=nullptr;
        bool (*msync)(void*,void*,size_t)=nullptr;
        uint32_t (*clock)(void*)=nullptr;
        void (*settled)(void*,const Event&)=nullptr;
    };
    // Counter origin is arbitrary; a nonzero origin lets host tests cross wrap.
    constexpr explicit DsiFramebuffer(uint32_t initial_frame=0)noexcept:frame_(initial_frame){}
    PresentError attach(uint16_t* framebuffer,NativeInfo,Hooks);
    PresentError init()override{return ready_?PresentError::None:PresentError::Unavailable;}
    NativeInfo info()const noexcept override{return info_;}
    SubmitId submit(Rect,const uint16_t*,size_t,Released={})override;
    PresentError wait_released(SubmitId id)override{return committed(id);}
    PresentError committed(SubmitId)override;
    PresentError scan_fence(SubmitId,ScanCallback)override;
    PresentError set_backlight(unsigned)override{return PresentError::Unsupported;}
    void poll()noexcept override;
    void frame_complete(uint32_t us)noexcept;
private:
    struct Entry {
        SubmitId id;Rect rect;uint32_t frame=0,committed_us=0,completed_us=0;
        ScanCallback callback;ScanOutcome outcome=ScanOutcome::Presented;
        bool done=false,registered=false,timed=false;
    };
    Entry* find(SubmitId)noexcept;
    void finish(Entry&,ScanOutcome,uint32_t,bool)noexcept;
    void poll_frame(uint32_t)noexcept;
    uint16_t* framebuffer_=nullptr;NativeInfo info_;Hooks hooks_;
    bool ready_=false,failed_=false;uint64_t next_=0;
    std::array<Entry,History> entries_{};
    std::atomic<uint32_t> frame_{0};
    std::array<std::atomic<uint32_t>,FrameHistory> frame_us_{};
};
}
