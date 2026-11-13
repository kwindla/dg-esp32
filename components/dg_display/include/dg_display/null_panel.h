#pragma once
#include "present.h"
namespace dg_display {
// Headless backend has no panel and intentionally cannot promise scan-out.
class NullPanel final:public PanelBackend {
public:
    explicit NullPanel(NativeInfo info)noexcept:info_(info){info_.scan_fence=false;}
    PresentError init()override{ready_=true;return PresentError::None;}
    NativeInfo info()const noexcept override{return info_;}
    SubmitId submit(Rect,const uint16_t*,size_t,Released={})override;
    PresentError wait_released(SubmitId id)override{return committed(id);}
    PresentError committed(SubmitId)override;
    PresentError scan_fence(SubmitId,ScanCallback)override{return PresentError::Unsupported;}
    PresentError set_backlight(unsigned pct)override{return pct>100?PresentError::Invalid:PresentError::Unsupported;}
private:
    NativeInfo info_;bool ready_=false;uint64_t next_=0;
};
}
