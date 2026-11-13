#pragma once
#include "scene.h"
#include <cstddef>
#include <cstdint>
namespace dg_display {
enum class PresentError : uint8_t {None,Invalid,Unsupported,Unavailable,Busy,Io,Inactive};
// Pixels are RGB565 in wire byte order (big endian), including Native submissions.
enum class PixelFormat : uint8_t {Rgb565BE};
struct NativeInfo {int32_t w=0,h=0;bool round=false;PixelFormat format=PixelFormat::Rgb565BE;size_t stride_px=0;bool scan_fence=false;};
struct SubmitId {
    uint64_t value=0;PresentError error=PresentError::None;
    explicit operator bool()const noexcept{return value && error==PresentError::None;}
};
// Success transfers buffer ownership until Released, exactly once on the owner
// task. Failure leaves ownership with the caller and does not invoke Released.
struct Released {void (*fn)(void*)=nullptr;void* context=nullptr;void operator()()const noexcept{if(fn)fn(context);}};
enum class ScanOutcome : uint8_t {Presented,Superseded};
struct ScanCallback {void (*fn)(void*,SubmitId,ScanOutcome)=nullptr;void* context=nullptr;};
class PanelBackend {
public:
    virtual ~PanelBackend()=default;
    virtual PresentError init()=0;
    virtual NativeInfo info()const noexcept=0;
    // Native rectangles only; backend owns copying/cache maintenance. No
    // allocation or cache writeback is imposed on the engine or native UI.
    virtual SubmitId submit(Rect native,const uint16_t* px,size_t stride_px,Released released={})=0;
    // May wait for the staging release. This event says nothing about scan-out.
    virtual PresentError wait_released(SubmitId)=0;
    // May block. Complete transfer to panel/framebuffer, including error drain.
    // Wire acknowledgements use this fence, never scan_fence.
    virtual PresentError committed(SubmitId)=0;
    // Optional: full scan started after commit, or a later overlapping commit
    // won before that scan. Callback must follow commit; unsupported returns an error.
    virtual PresentError scan_fence(SubmitId,ScanCallback)=0;
    virtual PresentError set_backlight(unsigned pct)=0;
    // Display-owner service point; backends may dispatch deferred scan callbacks.
    virtual void poll()noexcept{}
};
bool valid_native(Rect,const uint16_t*,size_t stride_px,NativeInfo) noexcept;
struct LogicalLayout {int32_t w=0,h=0,scale=1,x=0,y=0;uint16_t border=0;};
class LogicalAdapter {
public:
    LogicalAdapter(PanelBackend& backend,LogicalLayout layout,uint16_t* scratch=nullptr,size_t pixels=0) noexcept
        :backend_(backend),layout_(layout),scratch_(scratch),pixels_(pixels){}
    PresentError validate()const noexcept;
    SubmitId submit(Rect logical,const uint16_t*,size_t stride_px,Released released={});
    PresentError fill_border(SubmitId* last=nullptr);
    PanelBackend& backend()noexcept{return backend_;}
private:
    PanelBackend& backend_;LogicalLayout layout_;uint16_t* scratch_;size_t pixels_;SubmitId scratch_inflight_;
    PresentError release_scratch();
};
enum class PresentMode : uint8_t {Logical,Native};
class Presenter {
public:
    // Re-present requests a full logical scene, including updates accumulated
    // in Native mode. The caller keeps owning/ticking that scene in either mode.
    Presenter(LogicalAdapter& adapter,PresentError (*represent)(void*)=nullptr,void* context=nullptr)noexcept
        :adapter_(adapter),represent_(represent),context_(context){}
    NativeInfo info()const noexcept{return adapter_.backend().info();}
    PresentMode mode()const noexcept{return mode_;}
    PresentError set_mode(PresentMode);
    SubmitId submit_logical(Rect,const uint16_t*,size_t stride_px,Released released={});
    SubmitId submit_native(Rect,const uint16_t*,size_t stride_px,Released released={});
    PresentError wait_released(SubmitId id){return adapter_.backend().wait_released(id);}
    PresentError committed(SubmitId id){return adapter_.backend().committed(id);}
    PresentError scan_fence(SubmitId id,ScanCallback cb){return adapter_.backend().scan_fence(id,cb);}
private:
    LogicalAdapter& adapter_;PresentError (*represent_)(void*);void* context_;
    PresentMode mode_=PresentMode::Logical;SubmitId last_;
};
// Selected by the firmware component's backend source list.
PanelBackend& panel_backend() noexcept;
}
