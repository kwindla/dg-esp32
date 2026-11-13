#include "dg_display/present.h"
#include <algorithm>
#include <limits>
namespace dg_display {
bool valid_native(Rect r,const uint16_t* px,size_t stride,NativeInfo info) noexcept {
    return px && !r.empty() && r.x0>=0 && r.y0>=0 && r.x1<=info.w && r.y1<=info.h &&
        stride>=size_t(r.x1-r.x0) && stride<=std::numeric_limits<size_t>::max()/size_t(r.y1-r.y0)/sizeof(uint16_t);
}
PresentError LogicalAdapter::validate()const noexcept {
    const auto info=backend_.info();
    if(layout_.w<=0 || layout_.h<=0 || layout_.scale<=0 || layout_.x<0 || layout_.y<0 ||
       int64_t(layout_.w)*layout_.scale+layout_.x>info.w || int64_t(layout_.h)*layout_.scale+layout_.y>info.h)
        return PresentError::Invalid;
    return PresentError::None;
}
PresentError LogicalAdapter::release_scratch(){
    if(!scratch_inflight_)return PresentError::None;
    auto error=backend_.wait_released(scratch_inflight_);
    if(error==PresentError::None)scratch_inflight_={};
    return error;
}
SubmitId LogicalAdapter::submit(Rect r,const uint16_t* px,size_t stride,Released released){
    auto error=validate();if(error!=PresentError::None)return {0,error};
    NativeInfo logical;logical.w=layout_.w;logical.h=layout_.h;
    if(!valid_native(r,px,stride,logical))return {0,PresentError::Invalid};
    Rect native{layout_.x+r.x0*layout_.scale,layout_.y+r.y0*layout_.scale,
                layout_.x+r.x1*layout_.scale,layout_.y+r.y1*layout_.scale};
    if(layout_.scale==1)return backend_.submit(native,px,stride,released);
    const size_t width=size_t(native.x1-native.x0),height=size_t(native.y1-native.y0);
    if(!scratch_ || width>pixels_/height)return {0,PresentError::Unavailable};
    error=release_scratch();if(error!=PresentError::None)return {0,error};
    // Replicate the opaque RGB565 words, preserving their wire byte order.
    for(size_t y=0;y<height;++y)for(size_t x=0;x<width;++x)
        scratch_[y*width+x]=px[(y/size_t(layout_.scale))*stride+x/size_t(layout_.scale)];
    auto id=backend_.submit(native,scratch_,width);
    if(!id)return id;
    scratch_inflight_=id;
    released(); // The source has been copied; this is not a commit or scan fence.
    return id;
}
PresentError LogicalAdapter::fill_border(SubmitId* last){
    auto error=validate();if(error!=PresentError::None)return error;
    error=release_scratch();if(error!=PresentError::None)return error;
    const auto info=backend_.info();const int32_t right=layout_.x+layout_.w*layout_.scale,bottom=layout_.y+layout_.h*layout_.scale;
    const Rect borders[]={{0,0,info.w,layout_.y},{0,bottom,info.w,info.h},
        {0,layout_.y,layout_.x,bottom},{right,layout_.y,info.w,bottom}};
    for(const auto& border:borders){
        if(border.empty())continue;
        const auto width=size_t(border.x1-border.x0);
        if(!scratch_ || width>pixels_)return PresentError::Unavailable;
        std::fill_n(scratch_,pixels_,layout_.border);
        const auto rows=int32_t(std::min(pixels_/width,size_t(border.y1-border.y0)));
        for(auto y=border.y0;y<border.y1;y+=rows){
            auto id=backend_.submit({border.x0,y,border.x1,std::min(y+rows,border.y1)},scratch_,width);
            if(!id)return id.error;
            if(last)*last=id;
            error=backend_.committed(id);if(error!=PresentError::None)return error;
        }
    }
    return PresentError::None;
}
}
