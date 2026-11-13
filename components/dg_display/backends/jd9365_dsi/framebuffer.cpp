#include "framebuffer.h"
#include <limits>
namespace dg_display {
PresentError DsiFramebuffer::attach(uint16_t* fb,NativeInfo info,Hooks hooks){
    if(ready_)return PresentError::Busy;
    if(!fb || info.w<=0 || info.h<=0 || info.stride_px<size_t(info.w) ||
       info.stride_px>std::numeric_limits<size_t>::max()/size_t(info.h)/2 || !hooks.msync || !hooks.clock)
        return PresentError::Invalid;
    framebuffer_=fb;info_=info;info_.format=PixelFormat::Rgb565BE;info_.scan_fence=true;
    hooks_=hooks;ready_=true;return PresentError::None;
}
DsiFramebuffer::Entry* DsiFramebuffer::find(SubmitId id)noexcept{
    if(!id || id.value>next_)return nullptr;
    auto& entry=entries_[(id.value-1)%History];return entry.id.value==id.value?&entry:nullptr;
}
PresentError DsiFramebuffer::committed(SubmitId id){
    if(failed_)return PresentError::Io;
    if(!ready_ || !id || id.value>next_)return PresentError::Invalid;
    return find(id)?PresentError::None:PresentError::Unavailable;
}
void DsiFramebuffer::finish(Entry& entry,ScanOutcome outcome,uint32_t us,bool timed)noexcept{
    entry.done=true;entry.outcome=outcome;entry.completed_us=us;entry.timed=timed;
    const auto callback=entry.callback;entry.callback={};
    if(hooks_.settled)hooks_.settled(hooks_.context,{entry.id,outcome,entry.committed_us,us,timed});
    if(callback.fn)callback.fn(callback.context,entry.id,outcome);
}
void DsiFramebuffer::frame_complete(uint32_t us)noexcept{
    // Only 32-bit lock-free atomics in the ISR; no callbacks, allocation or IO.
    static_assert(std::atomic<uint32_t>::is_always_lock_free);
    const auto next=frame_.load(std::memory_order_relaxed)+1;
    frame_us_[next%FrameHistory].store(us,std::memory_order_relaxed);
    frame_.store(next,std::memory_order_release);
}
void DsiFramebuffer::poll()noexcept{
    if(!ready_ || failed_)return;
    poll_frame(frame_.load(std::memory_order_acquire));
}
void DsiFramebuffer::poll_frame(uint32_t frame)noexcept{
    for(auto& entry:entries_){
        if(!entry.id || entry.done)continue;
        if(uint32_t(frame-entry.frame)<2)continue;
        const auto target=entry.frame+2;
        const auto before=frame_.load(std::memory_order_acquire);
        const auto us=frame_us_[target%FrameHistory].load(std::memory_order_relaxed);
        const auto after=frame_.load(std::memory_order_acquire);
        // An owner delayed beyond the timestamp ring still gets its outcome;
        // never invent a measured latency for an overwritten timestamp.
        const bool timed=before==after && uint32_t(after-target)<FrameHistory-1;
        finish(entry,ScanOutcome::Presented,us,timed);
    }
}
SubmitId DsiFramebuffer::submit(Rect rect,const uint16_t* px,size_t stride,Released released){
    if(failed_)return {0,PresentError::Io};
    if(!ready_)return {0,PresentError::Unavailable};
    if(!valid_native(rect,px,stride,info_))return {0,PresentError::Invalid};
    poll();
    auto& slot=entries_[next_%History];
    if(slot.id && !slot.done)return {0,PresentError::Busy};
    auto* source=reinterpret_cast<const uint8_t*>(px);
    auto* destination=reinterpret_cast<uint8_t*>(framebuffer_);
    for(int32_t y=rect.y0;y<rect.y1;++y)for(int32_t x=rect.x0;x<rect.x1;++x){
        const auto from=(size_t(y-rect.y0)*stride+size_t(x-rect.x0))*2;
        const auto to=(size_t(y)*info_.stride_px+size_t(x))*2;
        destination[to]=source[from+1];destination[to+1]=source[from];
    }
    // Whole rows, as in IDF's DPI draw_bitmap path. UNALIGNED in the IDF
    // hook permits cache-line expansion within the driver-owned allocation.
    auto* start=framebuffer_+size_t(rect.y0)*info_.stride_px;
    const auto bytes=size_t(rect.y1-rect.y0)*info_.stride_px*2;
    if(!hooks_.msync(hooks_.context,start,bytes)){failed_=true;return {0,PresentError::Io};}
    const auto us=hooks_.clock(hooks_.context);
    // Resolve scans that finished during copy/msync before deciding overlap.
    const auto frame=frame_.load(std::memory_order_acquire);
    poll_frame(frame);
    for(auto& old:entries_)if(old.id && !old.done && old.rect.x0<rect.x1 && rect.x0<old.rect.x1 &&
        old.rect.y0<rect.y1 && rect.y0<old.rect.y1)finish(old,ScanOutcome::Superseded,us,true);
    slot={};slot.id={++next_};slot.rect=rect;slot.frame=frame;slot.committed_us=us;
    released();return slot.id;
}
PresentError DsiFramebuffer::scan_fence(SubmitId id,ScanCallback callback){
    auto error=committed(id);if(error!=PresentError::None)return error;
    if(!callback.fn)return PresentError::Invalid;
    auto* entry=find(id);if(entry->registered)return PresentError::Invalid;
    entry->registered=true;entry->callback=callback;
    if(entry->done){entry->callback={};callback.fn(callback.context,id,entry->outcome);}
    else poll();
    return PresentError::None;
}
}
