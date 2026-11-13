#include "dg_display/present.h"
namespace dg_display {
PresentError Presenter::set_mode(PresentMode mode){
    if(mode!=PresentMode::Logical && mode!=PresentMode::Native)return PresentError::Invalid;
    if(mode==mode_)return PresentError::None;
    if(last_){auto error=committed(last_);if(error!=PresentError::None)return error;}
    if(mode==PresentMode::Logical){
        if(!represent_)return PresentError::Unavailable;
        auto error=adapter_.fill_border(&last_);if(error!=PresentError::None)return error;
        // Callback may take the non-recursive engine guard: caller must not hold it.
        // Set the owner before the callback, which may synchronously re-present.
        mode_=mode;
        error=represent_(context_);
        if(error!=PresentError::None){mode_=PresentMode::Native;return error;}
    }else mode_=mode;
    return PresentError::None;
}
SubmitId Presenter::submit_logical(Rect r,const uint16_t* px,size_t stride,Released released){
    if(mode_!=PresentMode::Logical)return {0,PresentError::Inactive};
    auto id=adapter_.submit(r,px,stride,released);if(id)last_=id;return id;
}
SubmitId Presenter::submit_native(Rect r,const uint16_t* px,size_t stride,Released released){
    if(mode_!=PresentMode::Native)return {0,PresentError::Inactive};
    if(!valid_native(r,px,stride,adapter_.backend().info()))return {0,PresentError::Invalid};
    auto id=adapter_.backend().submit(r,px,stride,released);if(id)last_=id;return id;
}
}
