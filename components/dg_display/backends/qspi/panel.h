#pragma once
#include "dg_display/present.h"
#include "dg_board/backlight.h"
// Included after the backend transfer hooks (also faked by the host contract).
namespace dg_display {namespace detail {
// Queue depth one; retain the latest submission fence. Older IDs return
// Unavailable explicitly, without an unbounded submission history.
class QspiPanel final:public PanelBackend {
    uint64_t next_=0,completed_=0;SubmitId pending_;Released released_;
    unsigned generation_=0;PresentError completed_error_=PresentError::None;
public:
    PresentError init()override{
        if(qspi_initialized())return PresentError::None;
        try{census_init();return PresentError::None;}catch(...){return PresentError::Io;}
    }
    NativeInfo info()const noexcept override{return {NativeWidth,NativeHeight,true,PixelFormat::Rgb565BE,size_t(NativeWidth),false};}
    SubmitId submit(Rect r,const uint16_t* px,size_t stride,Released released)override{
        if(!detail::state || !detail::state->panel)return {0,PresentError::Unavailable};
        if(!valid_native(r,px,stride,info()))return {0,PresentError::Invalid};
        if(stride!=size_t(r.x1-r.x0))return {0,PresentError::Unsupported};
        if(pending_)return {0,PresentError::Busy};
        const auto generation=dma_generation();
        // send() drains the previous transaction before replacing timestamps.
        if(!detail::send(r,reinterpret_cast<uint8_t*>(const_cast<uint16_t*>(px))))return {0,PresentError::Io};
        pending_={++next_};generation_=generation;released_=released;return pending_;
    }
    PresentError wait_released(SubmitId id)override{
        if(!id || id.value>next_)return PresentError::Invalid;
        if(id.value!=next_)return PresentError::Unavailable;
        if(!pending_ || id.value!=pending_.value)return id.value==completed_?completed_error_:PresentError::Unavailable;
        const bool ok=!detail::state->dead && detail::await_dma();
        if(!ok && (detail::state->dead || detail::state->color_inflight)){
            released_={};pending_={};completed_=id.value;completed_error_=PresentError::Io;
            return completed_error_; // Terminal DMA may still own the buffer: no Released.
        }
        auto callback=released_;released_={};pending_={};
        completed_=id.value;completed_error_=ok?PresentError::None:PresentError::Io;
        callback();return completed_error_;
    }
    PresentError committed(SubmitId id)override{
        if(!id || id.value>next_)return PresentError::Invalid;
        if(id.value!=next_)return PresentError::Unavailable;
        if(detail::state->dead){
            released_={};pending_={};completed_=id.value;completed_error_=PresentError::Io;
            return completed_error_; // Match the original drain_dma() dead fast path.
        }
        if(pending_ && id.value==pending_.value){auto error=wait_released(id);if(error!=PresentError::None)return error;}
        if(id.value!=completed_)return PresentError::Unavailable;
        // EXACT S3 fence: consume the driver's DMA flags, then compare generation.
        if(!detail::drain_dma() || dma_generation()!=generation_)completed_error_=PresentError::Io;
        return completed_error_;
    }
    PresentError scan_fence(SubmitId,ScanCallback)override{return PresentError::Unsupported;}
    PresentError set_backlight(unsigned pct)override{
        if(pct>100)return PresentError::Invalid;
        if(!detail::state || !detail::state->panel)return PresentError::Unavailable;
        auto error=dg_board::backlight::set(pct);
        return error==ESP_OK?PresentError::None:PresentError::Io;
    }
};
}}
