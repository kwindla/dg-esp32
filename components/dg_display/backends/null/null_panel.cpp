#include "dg_display/null_panel.h"
namespace dg_display {
SubmitId NullPanel::submit(Rect r,const uint16_t* px,size_t stride,Released released){
    if(!ready_)return {0,PresentError::Unavailable};
    if(!valid_native(r,px,stride,info_))return {0,PresentError::Invalid};
    SubmitId id{++next_};released();return id;
}
PresentError NullPanel::committed(SubmitId id){
    return ready_ && id && id.value<=next_?PresentError::None:PresentError::Invalid;
}
}
